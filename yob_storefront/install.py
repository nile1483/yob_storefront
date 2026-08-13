# Copyright (c) 2026, YOB and Shayona
"""Install-time setup for yob_storefront."""

import frappe

from yob_storefront.permissions.storefront_role import ensure_role_and_permissions
from yob_storefront.utils.context import STOREFRONT_APP


def after_install():
    _ensure_storefront_application()
    ensure_role_and_permissions()
    ensure_custom_fields()
    ensure_payment_gateways()
    ensure_payment_processor_identity()


#: Dedicated identity for the internal Cart -> Sales Order commitment reached
#: from the PUBLIC /payment/<token> flow. Never a payer, never interactive.
PAYMENT_PROCESSOR_ROLE = "YOB Payment Processor"
PAYMENT_PROCESSOR_USER = "payment-processor@yob.internal"


def ensure_payment_processor_identity():
    """Create the internal payment-processor role and its disabled user.

    WHY THIS EXISTS. ``/payment/<token>`` is public: the payer is Frappe's
    ``Guest``. After the token has been resolved and every Payment Request,
    source, financial and eligibility check has passed, YOB still has to run
    ERPNext's Cart -> Sales Order work, and ERPNext performs nested permission
    checks against the CURRENT EXECUTION USER on documents YOB never touches --
    notably ``get_item_details``, which loads its own cached Item and calls
    ``item.check_permission()``. No document flag can reach that.

    Frappe 16.30.0 offers no request-local permission-bypass context: verified
    in ``frappe/permissions.py``, whose only user-level short-circuit is
    ``user == "Administrator"``. Controller ``has_permission`` hooks cannot help
    either -- ``permissions.py:495`` only honours a FALSY return, so a hook can
    deny but never grant.

    That leaves a dedicated execution identity. It is deliberately NOT
    Administrator and NOT ``YOB Storefront Buyer``.

    ``enabled = 0`` is intentional and verified on this version: a disabled user
    keeps its roles and permissions when entered through Frappe's
    execution-user switch,
    but cannot authenticate interactively and has no usable session. The
    identity is therefore reachable only from server code inside the trusted
    boundary.

    Permissions start at the MINIMUM actually demonstrated as necessary --
    Item read, the one gate proven to block a Guest payer. Customer, Address,
    Contact and Sales Order are deliberately NOT granted: the Sales Order's own
    ``flags.ignore_permissions`` already covers the party path. Anything more is
    added only when a test proves another nested document needs it.
    """

    from frappe.permissions import add_permission

    if not frappe.db.exists("Role", PAYMENT_PROCESSOR_ROLE):
        frappe.get_doc({
            "doctype": "Role",
            "role_name": PAYMENT_PROCESSOR_ROLE,
            # No Desk. This identity is never used by a human.
            "desk_access": 0,
            "is_custom": 1,
        }).insert(ignore_permissions=True)

    # Each entry was added ONLY after a test proved the commitment path needs
    # it -- never preemptively:
    #
    #   Item     get_item_details loads its own cached Item and calls
    #            item.check_permission(). Proven by the Guest endpoint test.
    #   Account  ERPNext tax/party resolution reads the tax Account
    #            ("User don't have permissions to select/read this account").
    #            Proven by the same test once the Item gate was cleared.
    #
    # Customer, Address, Contact and Sales Order are deliberately absent: the
    # Sales Order's own flags.ignore_permissions already covers the party path,
    # and the tests pass without them.
    for doctype in ("Item", "Account"):
        if frappe.db.exists("Custom DocPerm",
                            {"parent": doctype, "role": PAYMENT_PROCESSOR_ROLE}):
            continue

        # setup_custom_perms first: inserting a Custom DocPerm without it makes
        # Frappe REPLACE the doctype's whole permission set, which once
        # destroyed every standard Item role on this bench.
        from frappe.permissions import setup_custom_perms

        setup_custom_perms(doctype)
        add_permission(doctype, PAYMENT_PROCESSOR_ROLE, 0)

        # add_permission grants read AND export by default. Export is not part
        # of the commitment path, so it is removed: this identity reads the two
        # documents ERPNext resolves internally, and does nothing else.
        frappe.db.set_value(
            "Custom DocPerm",
            {"parent": doctype, "role": PAYMENT_PROCESSOR_ROLE},
            {"read": 1, "export": 0, "write": 0, "create": 0, "delete": 0,
             "report": 0, "share": 0, "print": 0, "email": 0},
        )

    if not frappe.db.exists("User", PAYMENT_PROCESSOR_USER):
        user = frappe.get_doc({
            "doctype": "User",
            "email": PAYMENT_PROCESSOR_USER,
            "first_name": "YOB Payment Processor",
            "user_type": "System User",
            "send_welcome_email": 0,
            "enabled": 0,                 # cannot authenticate; see docstring
        })
        user.append("roles", {"role": PAYMENT_PROCESSOR_ROLE})
        user.flags.ignore_permissions = True
        user.insert(ignore_permissions=True)

        print(f"yob_storefront: created internal identity {PAYMENT_PROCESSOR_USER}")

    # Re-assert on every migrate: the role must stay attached even if an
    # administrator edited the user.
    if not frappe.db.exists("Has Role", {"parent": PAYMENT_PROCESSOR_USER,
                                         "role": PAYMENT_PROCESSOR_ROLE}):
        user = frappe.get_doc("User", PAYMENT_PROCESSOR_USER)
        user.append("roles", {"role": PAYMENT_PROCESSOR_ROLE})
        user.flags.ignore_permissions = True
        user.save(ignore_permissions=True)


def ensure_payment_gateways():
    """Register the Frappe `Payment Gateway` records YOB has drivers for.

    Frappe Payments -- not YOB -- owns gateway configuration and credentials.
    This creates only the gateway REGISTRATION so a Payment Method can link to
    it and `get_payment_gateway_controller` can resolve. It writes no API key,
    no secret, and no `gateway_controller` override: an administrator supplies
    credentials in `Razorpay Settings`, and YOB never stores a copy.

    Idempotent, and safe on a site whose gateway is already configured.
    """

    for provider in ("Razorpay",):
        if frappe.db.exists("Payment Gateway", provider):
            continue

        doc = frappe.new_doc("Payment Gateway")
        doc.gateway = provider
        doc.insert(ignore_permissions=True)

        print(f"yob_storefront: registered Payment Gateway '{provider}'")


def ensure_custom_fields():
    """Register YOB-owned custom fields on dependency DocTypes. Idempotent.

    Added through create_custom_fields rather than by editing ERPNext's DocType
    JSON, which YOB must never modify. `create_custom_fields` also UPDATES an
    existing field and then runs the schema sync, so this is the supported way
    to add the unique constraint below to fields that already exist.

    The three checkout fields:

    * ``custom_checkout_token``  -- the bearer credential for the guest payment
      page. UNIQUE, because the security invariant is "one non-empty token ->
      at most one Payment Request", and a resolver that has to pick between two
      matching rows is picking which obligation somebody pays.
    * ``custom_checkout_expiry`` -- when that credential dies.
    * ``custom_source_fingerprint`` -- SHA-256 of the payment-source snapshot
      the Payment Request was ISSUED for. Immutable after issuance: it
      identifies the obligation, so refreshing it from a changed Cart would
      defeat its whole purpose.

    ``no_copy`` on all three: an amended or duplicated Payment Request must not
    inherit a live payment credential, and under the unique constraint a copied
    token would fail the insert outright.
    """

    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    create_custom_fields(
        {
            "Payment Request": [
                {
                    "fieldname": "custom_checkout_token",
                    "label": "Checkout Token",
                    "fieldtype": "Data",
                    "insert_after": "reference_name",
                    "read_only": 1,
                    "no_copy": 1,
                    # Frappe coerces a blank value on a unique field to NULL
                    # (base_document.py get_valid_dict), and MariaDB permits
                    # unlimited NULLs in a unique index -- so every superseded
                    # and every non-storefront Payment Request coexists freely,
                    # while two live credentials cannot collide.
                    "unique": 1,
                    "description": (
                        "Bearer credential for the guest checkout page. Cleared "
                        "when the obligation is superseded or paid."
                    ),
                },
                {
                    "fieldname": "custom_checkout_expiry",
                    "label": "Checkout Expiry",
                    "fieldtype": "Datetime",
                    "insert_after": "custom_checkout_token",
                    "read_only": 1,
                    "no_copy": 1,
                },
                {
                    "fieldname": "custom_provider_claim_at",
                    "label": "Provider Creation Claim",
                    "fieldtype": "Datetime",
                    "read_only": 1,
                    "no_copy": 1,
                    "description": (
                        "When this Payment Request claimed the right to create a "
                        "provider payment. Durable BEFORE the network call, so a "
                        "crashed or timed-out attempt is recovered rather than "
                        "repeated. Set = a create was or may have been issued."
                    ),
                },
                {
                    "fieldname": "custom_source_fingerprint",
                    "label": "Source Fingerprint",
                    "fieldtype": "Data",
                    "length": 64,
                    "read_only": 1,
                    "no_copy": 1,
                    "description": (
                        "SHA-256 of the payment-source snapshot this request was "
                        "issued for. Immutable after issuance."
                    ),
                },
            ]
        },
        ignore_validate=True,
    )


def after_migrate():
    """Re-assert the YOB-managed role and its DocPerms on every migrate.

    Idempotent, and cheap. Without it an upgraded site would keep pricing broken
    for existing buyers until someone reinstalled, and an upgraded site would
    lack custom_source_fingerprint.
    """

    ensure_role_and_permissions()
    ensure_custom_fields()
    ensure_payment_gateways()


def _ensure_storefront_application():
    """Register the STOREFRONT application in yob_auth.

    Without this record every storefront endpoint returns 403, because
    ``require_application("STOREFRONT", ...)`` cannot resolve the application.
    Creating it here makes a fresh install functional out of the box.

    This registers the application *definition* only. It grants nobody access:
    a user still needs an explicit ``YOB User Application Access`` record, which
    is deliberately NOT created by code -- access grants must be an
    administrator's conscious act.

    ``domains`` is intentionally left empty because the allowed host is
    site-specific, and because domain validation requires the edge proxy to send
    ``X-YOB-Original-Host``. See docs/yob_storefront_deployment.md.
    """

    if frappe.db.exists("YOB Application", STOREFRONT_APP):
        return

    doc = frappe.new_doc("YOB Application")
    doc.application_code = STOREFRONT_APP
    doc.application_name = "YOB Storefront"
    doc.enabled = 1
    doc.domains = ""
    # Enforced centrally: every access grant for this application must resolve
    # to a Customer, which is what the storefront endpoints require.
    doc.required_profile_doctype = "Customer"
    doc.allow_password_login = 1
    doc.allow_email_otp = 0
    doc.allow_mobile_otp = 0
    doc.insert(ignore_permissions=True)

    print(f"yob_storefront: created YOB Application '{STOREFRONT_APP}'")
