import frappe
from yob_core.api.boundary import yob_api
from yob_auth.security.decorators import require_application
from yob_storefront.utils.context import STOREFRONT_APP, get_storefront_customer
from yob_storefront.api.response import (
    ADDRESS_NOT_FOUND,
    CONTACT_NOT_FOUND,
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

        doc.insert(ignore_permissions=True)

        clear_customer_contact_cache(customer)

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

        if data.get("first_name"):
            contact.first_name = data.get("first_name").strip()

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

        contact.save(ignore_permissions=True)

        clear_customer_contact_cache(customer.name)

        return success_response({
            "name": contact.name,
            "full_name": contact.full_name,
            "email": data.get("email").strip() if data.get("email") else None,
            "phone": data.get("phone").strip() if data.get("phone") else None,
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

    # try:
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

    frappe.delete_doc("Contact", name, ignore_permissions=True)

    clear_customer_contact_cache(customer.name)

    return success_response({}, notice="Contact deleted")

    # except Exception:
    #     return server_error("Delete Contact Error", "Failed to delete contact")


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

        doc.address_title = data.get("address_title")
        doc.address_type = data.get("address_type") or "Billing"
        doc.address_line1 = data.get("address_line1")
        doc.address_line2 = data.get("address_line2")

        doc.city = data.get("city")
        doc.state = data.get("state")
        doc.country = data.get("country")
        doc.pincode = data.get("pincode")

        # Contact info
        doc.email_id = data.get("email")
        doc.phone = data.get("phone")
        doc.fax = data.get("fax")

        # ERPNext flags
        doc.is_primary_address = cint(data.get("is_primary_address") or 0)
        doc.is_shipping_address = cint(data.get("is_shipping_address") or 0)
        doc.disabled = cint(data.get("disabled") or 0)

        # Tax / GST
        doc.tax_category = data.get("tax_category")
        doc.gstin = data.get("gstin")
        doc.gst_category = data.get("gst_category")
        doc.gst_state = data.get("gst_state")
        doc.gst_state_number = data.get("gst_state_number")

        doc.append("links", {
            "link_doctype": "Customer",
            "link_name": customer.name
        })

        doc.insert(ignore_permissions=True)

        clear_customer_address_cache(customer)

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

        doc.address_title = data.get("address_title")
        doc.address_type = data.get("address_type") or "Billing"
        doc.address_line1 = data.get("address_line1")
        doc.address_line2 = data.get("address_line2")

        doc.city = data.get("city")
        doc.state = data.get("state")
        doc.country = data.get("country")
        doc.pincode = data.get("pincode")

        # Contact info
        doc.email_id = data.get("email")
        doc.phone = data.get("phone")
        doc.fax = data.get("fax")

        # ERPNext flags
        doc.is_primary_address = cint(data.get("is_primary_address") or 0)
        doc.is_shipping_address = cint(data.get("is_shipping_address") or 0)
        doc.disabled = cint(data.get("disabled") or 0)

        # Tax / GST
        doc.tax_category = data.get("tax_category")
        doc.gstin = data.get("gstin")
        doc.gst_category = data.get("gst_category")
        doc.gst_state = data.get("gst_state")
        doc.gst_state_number = data.get("gst_state_number")

        doc.save(ignore_permissions=True)

        clear_customer_address_cache(customer)

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

        frappe.delete_doc("Address", name, ignore_permissions=True)

        clear_customer_address_cache(customer)

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