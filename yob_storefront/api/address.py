import frappe
from yob_core.api.boundary import yob_api
from yob_auth.security.decorators import require_application
from yob_storefront.utils.context import STOREFRONT_APP, get_storefront_customer
from yob_storefront.api.response import (
    ADDRESS_IN_USE,
    ADDRESS_NOT_FOUND,
    CONTACT_IN_USE,
    CONTACT_NOT_FOUND,
    HTTP_CONFLICT,
    HTTP_CREATED,
    HTTP_NOT_FOUND,
    HTTP_UNPROCESSABLE,
    VALIDATION_FAILED,
    error_response,
    server_error,
    success_response,
)
from yob_storefront.utils.cache import CUSTOMER_CACHE_PREFIX
from frappe.utils import cint

# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------


def supplied(data, key):
    """True when the caller SENT ``key``, whatever value it carries.

    Presence, not truthiness. ``""``, ``0`` and ``False`` are legitimate
    supplied values: the difference between "the client did not touch this
    field" and "the client deliberately cleared it" is exactly the difference
    between preserving stored data and destroying it.

    ``if data.get(key):`` cannot express that. It reads a deliberate clear as
    absence, so a cleared field silently keeps its old value -- and, in the
    mirror-image bug this replaces, an ABSENT field was read as an empty value
    and wiped what was stored.
    """

    return key in data


# Request key -> Address fieldname. The request name is the published contract
# and does not always match ERPNext's: the client sends `email`, the DocType
# stores `email_id`.
ADDRESS_VALUE_FIELDS = {
    "address_title": "address_title",
    "address_type": "address_type",
    "address_line1": "address_line1",
    "address_line2": "address_line2",
    "city": "city",
    "state": "state",
    "country": "country",
    "pincode": "pincode",
    "email": "email_id",
    "phone": "phone",
    "fax": "fax",
    "tax_category": "tax_category",
    "gstin": "gstin",
    "gst_category": "gst_category",
    "gst_state": "gst_state",
    "gst_state_number": "gst_state_number",
}

# Checkbox fields: stored as 0/1, so a supplied value is coerced with cint.
ADDRESS_FLAG_FIELDS = {
    "is_primary_address": "is_primary_address",
    "is_shipping_address": "is_shipping_address",
    "disabled": "disabled",
}

ADDRESS_FIELD_TO_REQUEST_KEY = {
    fieldname: key
    for key, fieldname in {**ADDRESS_VALUE_FIELDS, **ADDRESS_FLAG_FIELDS}.items()
}


def apply_address_fields(doc, data):
    """Merge the SUPPLIED address fields onto ``doc``.

    Omitted fields are not touched, so on an update they keep their stored
    value. This is the whole of the partial-update contract; everything else
    below is validation and error mapping.
    """

    for key, fieldname in ADDRESS_VALUE_FIELDS.items():
        if supplied(data, key):
            value = data.get(key)
            doc.set(fieldname, value.strip() if isinstance(value, str) else value)

    for key, fieldname in ADDRESS_FLAG_FIELDS.items():
        if supplied(data, key):
            doc.set(fieldname, cint(data.get(key)))


def missing_required_address_field(doc):
    """The first required Address field left blank, or ``None``.

    Read from the LIVE meta rather than a hardcoded list, so requirements this
    module does not own are honoured without being restated here. On this site
    that matters: india_compliance adds ``gst_category`` as a required custom
    field, and a hardcoded list would miss it and let the save fail as a
    generic 500 instead of an attributable validation error.

    Checking here rather than relying on Frappe's own mandatory check is what
    makes FIELD ATTRIBUTION possible -- ``MandatoryError`` arrives as one string
    naming the doctype and docname, not as structured data.
    """

    for df in frappe.get_meta("Address").fields:
        if not df.reqd:
            continue

        value = doc.get(df.fieldname)
        if value is None or (isinstance(value, str) and not value.strip()):
            return df.fieldname

    return None


def safe_validation_detail(exc, fallback):
    """Framework validation text, stripped of anything Desk-shaped.

    Frappe and india_compliance write these messages for a human, but they are
    Desk humans: the text can carry HTML and ``/app/...`` anchors naming other
    documents. A storefront caller has no Desk, so tags are removed and any
    message still carrying a link or URL is dropped for a generic sentence
    rather than leaked.
    """

    from frappe.utils import strip_html

    text = " ".join(strip_html(str(exc) or "").split())

    if not text or "/app/" in text or "http" in text.lower() or "<" in text:
        return fallback

    return text[:200]


def validation_error_response(exc, fallback, field=None):
    """A Frappe/ERPNext/India-Compliance validation refusal as a YOB error.

    Without this the caller sees one of two wrong things: a generic 500 (the
    request was fine, the DATA was not), or -- because a bare ValidationError
    carries ``http_status_code`` 417 -- the core boundary's passthrough with the
    raw exception string as detail.
    """

    return error_response(
        VALIDATION_FAILED,
        safe_validation_detail(exc, fallback),
        field=field,
        status_code=HTTP_UNPROCESSABLE,
    )


def delete_owned_doc(doctype, name, in_use_code, in_use_detail):
    """Delete a record the caller already owns, or answer 409 if it is linked.

    Returns ``None`` on success, or an error envelope when Frappe refuses.

    Two things have to be undone when Frappe refuses:

    1. ``on_trash`` has already run by the time the link check fires, so the
       attempt is rolled back to a savepoint rather than left half-applied.

    2. Frappe refuses by calling ``frappe.throw`` with a Desk anchor naming the
       linked document. That message lands in ``frappe.local.message_log``, and
       the framework serialises that log into ``_server_messages`` on the HTTP
       response -- so it would reach the browser ALONGSIDE our clean envelope,
       carrying HTML, a ``/app/...`` URL and the referring docname. The log is
       therefore truncated back to its pre-delete length. Truncated, not
       cleared: messages queued earlier in the request are not ours to discard.

    Only ``LinkExistsError`` is converted. Anything else propagates, because a
    genuine fault must not be reported to the customer as "this is in use".
    """

    messages_before = len(frappe.local.message_log)

    frappe.db.savepoint("yob_account_delete")
    try:
        frappe.delete_doc(doctype, name, ignore_permissions=True)
    except frappe.LinkExistsError:
        frappe.db.rollback(save_point="yob_account_delete")
        frappe.local.message_log = frappe.local.message_log[:messages_before]

        return error_response(
            in_use_code,
            in_use_detail,
            field="name",
            status_code=HTTP_CONFLICT,
        )

    return None


def check_address_owner(address_name, customer):
    return frappe.db.exists(
        "Dynamic Link",
        {
            "parenttype": "Address",
            "parent": address_name,
            "link_doctype": "Customer",
            "link_name": customer.name
        }
    )


def check_contact_owner(contact_name, customer):
    return frappe.db.exists(
        "Dynamic Link",
        {
            "parenttype": "Contact",
            "parent": contact_name,
            "link_doctype": "Customer",
            "link_name": customer.name
        }
    )


def get_contacts_cache_key(customer_name):
    return f"{CUSTOMER_CACHE_PREFIX}{customer_name}:contacts"

def clear_customer_contact_cache(customer_name):
    frappe.cache().delete_value(get_contacts_cache_key(customer_name))

def get_addresses_cache_key(customer_name):
    return f"{CUSTOMER_CACHE_PREFIX}{customer_name}:addresses"


def clear_customer_address_cache(customer_name):
    frappe.cache().delete_value(get_addresses_cache_key(customer_name))


# =========================================================
# CONTACTS (POC)
# =========================================================

@frappe.whitelist(methods=["GET"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def get_contacts(auth_context=None):
   
    try:
        customer = get_storefront_customer(auth_context)

        cache_key = get_contacts_cache_key(customer.name)
        cached = frappe.cache().get_value(cache_key)
        if cached:
            return success_response(
                cached,
                notice="Contacts loaded (cached)",
                meta={"count": len(cached)},
            )

        contacts = frappe.get_all(
            "Contact",
            filters={
                "links.link_doctype": "Customer",
                "links.link_name": customer.name
            },
            fields=[
                    "name",
                    "full_name",
                    "salutation",
                    "first_name",
                    "last_name",
                    "gender",
                    "company_name",
                    "designation",
                    "email_id",
                    "mobile_no",
                 ]
        )

        result = []
        
        for c in contacts:
            doc = frappe.get_doc("Contact", c["name"])
            
            result.append({
                "name": doc.name,
                "full_name": doc.full_name,
                "salutation": doc.salutation,
                "first_name": doc.first_name,
                "last_name": doc.last_name,
                "gender": doc.gender,
                "company_name": doc.company_name,
                "designation": doc.designation,
                "email": doc.email_ids[0].email_id if doc.email_ids else None,
                "phone": doc.phone_nos[0].phone if doc.phone_nos else None  
            })

        frappe.cache().set_value(cache_key, result, expires_in_sec=1800)
        return success_response(
            result,
            notice="Contacts loaded",
            meta={"count": len(result)},
        )

    except Exception:
        return server_error("Get Contacts Error", "Failed to load contacts")


@frappe.whitelist(methods=["POST"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def add_contact(auth_context=None):
    """Create a Contact master linked to the authenticated Customer.

    Mirrors ``add_address``. The Customer link comes from ``auth_context`` and
    is appended server-side, so a caller cannot attach a Contact to another
    Customer -- which is exactly why the storefront owns this endpoint instead
    of letting clients POST to ``/api/resource/Contact`` (where the caller would
    supply ``links`` themselves).
    """

    try:
        customer = get_storefront_customer(auth_context)
        data = frappe.form_dict

        first_name = (data.get("first_name") or "").strip()
        if not first_name:
            return error_response(
                VALIDATION_FAILED,
                "First name is required.",
                field="first_name",
                status_code=HTTP_UNPROCESSABLE,
            )

        doc = frappe.new_doc("Contact")
        doc.first_name = first_name
        doc.last_name = data.get("last_name")
        doc.salutation = data.get("salutation")
        doc.designation = data.get("designation")
        doc.company_name = data.get("company")
        doc.gender = data.get("gender")

        # Contact stores email/phone in child tables, not plain fields.
        if data.get("email"):
            doc.append("email_ids", {"email_id": data.get("email"), "is_primary": 1})

        if data.get("phone"):
            doc.append("phone_nos", {"phone": data.get("phone"), "is_primary_phone": 1})

        doc.append("links", {
            "link_doctype": "Customer",
            "link_name": customer.name
        })

        try:
            doc.insert(ignore_permissions=True)
        except frappe.ValidationError as exc:
            return validation_error_response(
                exc, "The contact could not be saved. Please check the values.")

        clear_customer_contact_cache(customer.name)

        return success_response({
            "name": doc.name,
            "first_name": doc.first_name,
            "last_name": doc.last_name,
            "salutation": doc.salutation,
            "designation": doc.designation,
            "email": doc.email_ids[0].email_id if doc.email_ids else None,
            "phone": doc.phone_nos[0].phone if doc.phone_nos else None,
        }, notice="Contact created", status_code=HTTP_CREATED)

    except Exception:
        return server_error("Add Contact Error", "Failed to create contact")


@frappe.whitelist(methods=["POST"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def update_contact(auth_context=None):

    try:
        customer = get_storefront_customer(auth_context)
        data = frappe.form_dict

        name = data.get("name")
        if not name:
            return error_response(
                VALIDATION_FAILED,
                "Contact name is required.",
                field="name",
                status_code=HTTP_UNPROCESSABLE,
            )

        if not check_contact_owner(name, customer):
            # Missing and not-owned contacts answer identically on purpose.
            return error_response(
                CONTACT_NOT_FOUND,
                "Contact not found.",
                field="name",
                status_code=HTTP_NOT_FOUND,
            )

        contact = frappe.get_doc("Contact", name)

        # basic fields
        if data.get("salutation") is not None:
            contact.salutation = data.get("salutation")

        if supplied(data, "first_name"):
            # Presence, not truthiness: an explicitly empty first_name used to
            # be silently ignored, so a caller clearing a required field was
            # told the update succeeded while nothing had changed.
            first_name = (data.get("first_name") or "").strip()
            if not first_name:
                return error_response(
                    VALIDATION_FAILED,
                    "First name is required.",
                    field="first_name",
                    status_code=HTTP_UNPROCESSABLE,
                )
            contact.first_name = first_name

        if data.get("last_name") is not None:
            contact.last_name = data.get("last_name")

        if data.get("designation") is not None:
            contact.designation = data.get("designation")

        if data.get("company") is not None:
            contact.company_name = data.get("company")

        if data.get("gender") is not None:
            contact.gender = data.get("gender")

        # update email
        if data.get("email") is not None:
            contact.email_ids = []
            if data.get("email"):
                contact.append("email_ids", {
                    "email_id": data.get("email").strip(),
                    "is_primary": 1
                })

        # update phone
        if data.get("phone") is not None:
            contact.phone_nos = []
            if data.get("phone"):
                contact.append("phone_nos", {
                    "phone": data.get("phone").strip(),
                    "is_primary_mobile_no": 1
                })

        try:
            contact.save(ignore_permissions=True)
        except frappe.ValidationError as exc:
            return validation_error_response(
                exc, "The contact could not be saved. Please check the values.")

        clear_customer_contact_cache(customer.name)

        return success_response({
            "name": contact.name,
            "full_name": contact.full_name,
            # Read back from the SAVED document, not echoed from the request.
            # Echoing meant a partial update that omitted `email` reported
            # `email: null` while the contact still had one -- a response that
            # contradicted the record it had just written.
            "email": contact.email_ids[0].email_id if contact.email_ids else None,
            "phone": contact.phone_nos[0].phone if contact.phone_nos else None,
            "designation": contact.designation,
            "company_name": contact.company_name,
            "gender": contact.gender,
            "salutation": contact.salutation,
            "first_name": contact.first_name,
            "last_name": contact.last_name
        }, notice="Contact updated")

    except Exception:
        return server_error("Update Contact Error", "Failed to update contact")


@frappe.whitelist(methods=["POST"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def delete_contact(name=None, auth_context=None):

    try:
        if not name:
            return error_response(
                VALIDATION_FAILED,
                "Contact name is required.",
                field="name",
                status_code=HTTP_UNPROCESSABLE,
            )

        customer = get_storefront_customer(auth_context)

        if not check_contact_owner(name, customer):
            return error_response(
                CONTACT_NOT_FOUND,
                "Contact not found.",
                field="name",
                status_code=HTTP_NOT_FOUND,
            )

        # The exception boundary here was commented out, so a Cart-selected or
        # order-referenced Contact escaped as a raw Frappe LinkExistsError --
        # HTTP 417 with `_server_messages` carrying Desk HTML and the name of
        # the referring document. Ordinary link integrity is a business
        # refusal, not a crash, and it now answers as one.
        conflict = delete_owned_doc(
            "Contact", name, CONTACT_IN_USE,
            "This contact is currently in use and can't be deleted.")
        if conflict:
            return conflict

        clear_customer_contact_cache(customer.name)

        return success_response({}, notice="Contact deleted")

    except Exception:
        return server_error("Delete Contact Error", "Failed to delete contact")


# =========================================================
# ADDRESSES
# =========================================================

@frappe.whitelist(methods=["GET"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def get_addresses(auth_context=None):
    
    try:
        customer = get_storefront_customer(auth_context) 
        cache_key = get_addresses_cache_key(customer.name)
        cached = frappe.cache().get_value(cache_key)
        if cached:
            return success_response(
                cached,
                notice="Addresses loaded (cached)",
                meta={"count": len(cached)},
            )

        addresses = frappe.get_all(
            "Address",
            filters={
                "links.link_doctype": "Customer",
                "links.link_name": customer.name
            },
            fields=[
                "name",
                "address_title",
                "address_type",
                "address_line1",
                "address_line2",
                "city",
                "state",
                "pincode",
                "country",
                "is_primary_address",
                "is_shipping_address",
            ]
        )
         
        result = []
        for addr in addresses:
            doc = frappe.get_doc("Address", addr["name"])
            addr["display"] = doc.get_display()
            result.append(addr)

        frappe.cache().set_value(cache_key, result, expires_in_sec=1800)
        return success_response(
            result,
            notice="Addresses loaded",
            meta={"count": len(result)},
        )

    except Exception:
        return server_error("Get Addresses Error", "Failed to load addresses")


@frappe.whitelist(methods=["POST"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def add_address(auth_context=None):
    try:
        customer = get_storefront_customer(auth_context)
        data = frappe.form_dict

        doc = frappe.new_doc("Address")

        # Same field map as update_address, so create and edit cannot drift
        # apart. On a new document "omitted" simply means "left at its default".
        doc.address_type = "Billing"          # default, overridden if supplied
        apply_address_fields(doc, data)

        doc.append("links", {
            "link_doctype": "Customer",
            "link_name": customer.name
        })

        missing = missing_required_address_field(doc)
        if missing:
            return error_response(
                VALIDATION_FAILED,
                f"{frappe.get_meta('Address').get_label(missing)} is required.",
                field=ADDRESS_FIELD_TO_REQUEST_KEY.get(missing, missing),
                status_code=HTTP_UNPROCESSABLE,
            )

        try:
            doc.insert(ignore_permissions=True)
        except frappe.ValidationError as exc:
            return validation_error_response(
                exc, "The address could not be saved. Please check the values.")

        clear_customer_address_cache(customer.name)

        return success_response({
            "name": doc.name,
            "address_title": doc.address_title,
            "address_type": doc.address_type,
            "address_line1": doc.address_line1,
            "address_line2": doc.address_line2,
            "city": doc.city,
            "state": doc.state,
            "pincode": doc.pincode,
            "country": doc.country,
            "phone": doc.phone,
            "email": doc.email_id,
            "is_primary_address": doc.is_primary_address,
            "is_shipping_address": doc.is_shipping_address,
            "display": doc.get_display()
        }, notice="Address created", status_code=HTTP_CREATED)

    except Exception:
        return server_error("Add Address Error", "Failed to create address")


@frappe.whitelist(methods=["POST"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def update_address(auth_context=None):
    try:
        customer = get_storefront_customer(auth_context)
        data = frappe.form_dict

        name = data.get("name")
        if not name:
            return error_response(
                VALIDATION_FAILED,
                "Address name is required.",
                field="name",
                status_code=HTTP_UNPROCESSABLE,
            )

        if not check_address_owner(name, customer):
            # Missing and not-owned addresses answer identically on purpose.
            return error_response(
                ADDRESS_NOT_FOUND,
                "Address not found.",
                field="name",
                status_code=HTTP_NOT_FOUND,
            )

        doc = frappe.get_doc("Address", name)

        # PARTIAL UPDATE. Only fields the caller actually sent are touched;
        # everything else keeps its stored value.
        #
        # This previously assigned every field unconditionally from form_dict,
        # which made an edit form that posts only the inputs it renders destroy
        # everything it does not -- address_line2, phone, email_id and the
        # is_primary_address / is_shipping_address flags. The call SUCCEEDED, so
        # nothing warned anyone that data had been lost.
        #
        # The document is NOT renamed: address_title is an ordinary field here.
        # Historical Sales Orders hold `customer_address` as a link, and a
        # rename would cascade into them.
        apply_address_fields(doc, data)

        missing = missing_required_address_field(doc)
        if missing:
            # Attributed here rather than left to Frappe's MandatoryError, which
            # arrives as one opaque string and would surface as a generic 500.
            return error_response(
                VALIDATION_FAILED,
                f"{frappe.get_meta('Address').get_label(missing)} is required.",
                field=ADDRESS_FIELD_TO_REQUEST_KEY.get(missing, missing),
                status_code=HTTP_UNPROCESSABLE,
            )

        try:
            doc.save(ignore_permissions=True)
        except frappe.ValidationError as exc:
            # Covers ERPNext and india_compliance rules -- the required state
            # for an Indian address, GSTIN format, pincode. Those validators
            # stay the single source of truth; this only translates their
            # refusal into the storefront's envelope.
            return validation_error_response(
                exc, "The address could not be saved. Please check the values.")

        clear_customer_address_cache(customer.name)

        return success_response({
            "name": doc.name,
            "address_title": doc.address_title,
            "address_type": doc.address_type,
            "address_line1": doc.address_line1,
            "address_line2": doc.address_line2,
            "city": doc.city,
            "state": doc.state,
            "pincode": doc.pincode,
            "country": doc.country,
            "phone": doc.phone,
            "email": doc.email_id,
            "is_primary_address": doc.is_primary_address,
            "is_shipping_address": doc.is_shipping_address,
            "display": doc.get_display()
        }, notice="Address updated")

    except Exception:
        return server_error("Update Address Error", "Failed to update address")


@frappe.whitelist(methods=["POST"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def delete_address(name=None, auth_context=None):

    try:
        if not name:
            return error_response(
                VALIDATION_FAILED,
                "Address name is required.",
                field="name",
                status_code=HTTP_UNPROCESSABLE,
            )

        customer = get_storefront_customer(auth_context)

        if not check_address_owner(name, customer):
            return error_response(
                ADDRESS_NOT_FOUND,
                "Address not found.",
                field="name",
                status_code=HTTP_NOT_FOUND,
            )

        # Link integrity is NOT worked around. A Cart selection, a historical
        # Sales Order or the Customer's own default address all legitimately
        # block the delete, and detaching them automatically would either
        # strand a live checkout or rewrite history.
        conflict = delete_owned_doc(
            "Address", name, ADDRESS_IN_USE,
            "This address is currently in use and can't be deleted.")
        if conflict:
            return conflict

        clear_customer_address_cache(customer.name)

        return success_response({}, notice="Address deleted")

    except Exception:
        return server_error("Delete Address Error", "Failed to delete address")
    

@frappe.whitelist(methods=["GET"])
@yob_api
def get_contact_for_customer(customer=None):
    """
    Returns the contact linked to the given customer.
    Priority:
    1) Contact linked via customer dynamic link
    2) Contact linked via customer's user email

    INTERNAL DESK API. This is called by the Cart form Client Script and
    accepts an arbitrary Customer, so it is authorized with standard Frappe
    DocType permissions -- NOT with storefront application access. External
    storefront customers have no Customer read permission and are rejected.
    """

    # Guard first: has_permission(doc=None) degrades to a general check that a
    # Desk user would pass, and the query below would then run with no customer.
    if not customer:
        frappe.throw("Customer is required", frappe.ValidationError)

    if not frappe.has_permission("Customer", "read", doc=customer):
        frappe.throw("Not permitted", frappe.PermissionError)

    # ---------------- FIRST: CONTACT LINKED TO CUSTOMER ----------------
    contact = frappe.db.sql("""
        SELECT dl.parent
        FROM `tabDynamic Link` dl
        WHERE dl.link_doctype = 'Customer'
          AND dl.link_name = %s
        LIMIT 1
    """, customer)

    if contact:
        return contact[0][0]

    # ---------------- SECOND: CONTACT LINKED TO CUSTOMER USER ----------------
    user = frappe.db.get_value("Customer", customer, "user")

    if user:
        contact = frappe.db.sql("""
            SELECT ce.parent
            FROM `tabContact Email` ce
            WHERE ce.email_id = %s
            LIMIT 1
        """, user)

        if contact:
            return contact[0][0]

    return None