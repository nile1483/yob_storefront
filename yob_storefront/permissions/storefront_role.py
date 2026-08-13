# Copyright (c) 2026, YOB and Shayona
"""Lifecycle for the YOB-managed `YOB Storefront Buyer` role.

Why this exists
---------------
ERPNext's pricing engine calls ``item.check_permission()`` unconditionally
(``erpnext/stock/get_item_details.py``), on an Item document it fetches
internally. Neither ``so.flags.ignore_permissions`` nor
``frappe.flags.ignore_permissions`` can reach that check, so the pricing caller
genuinely needs Frappe-level **read on Item**. This role grants that and
nothing else.

What this deliberately is NOT
-----------------------------
* No Customer read. ``has_permission("Customer", "read")`` must stay False --
  that is the tested security boundary.
* No ``User Permission`` rows. Those are user-global and would restrict a
  multi-hat System User's entire Desk.
* No generic reconciliation engine. Deferred until Website Users are given
  genuine portal access as a decision in its own right.

Because the role only ADDS read on catalogue data and never restricts anything,
it is safe for both Website Users and System Users.
"""

import frappe

STOREFRONT_BUYER_ROLE = "YOB Storefront Buyer"
STOREFRONT_APP_CODE = "STOREFRONT"

# Only what ERPNext pricing actually requires. Adding to this list widens the
# permission surface, so each entry needs a reason.
MANAGED_READ_DOCTYPES = ("Item",)


def ensure_role_and_permissions():
    """Create the role and its read-only DocPerms. Idempotent.

    Called from install and after_migrate. Uses `Custom DocPerm` so ERPNext's
    own `item.json` is never edited.
    """

    if not frappe.db.exists("Role", STOREFRONT_BUYER_ROLE):
        frappe.get_doc({
            "doctype": "Role",
            "role_name": STOREFRONT_BUYER_ROLE,
            # External buyers must never reach Desk through this role.
            "desk_access": 0,
        }).insert(ignore_permissions=True)

    from frappe.permissions import setup_custom_perms

    for doctype in MANAGED_READ_DOCTYPES:
        # CRITICAL: a single Custom DocPerm REPLACES the DocType's entire
        # permission set. frappe/model/meta.py set_custom_permissions():
        #
        #     if custom_perms:
        #         self.permissions = [Document(d) for d in custom_perms]
        #
        # So inserting our row without first copying the standard DocPerms
        # silently strips Item access from Sales User, Stock User, Item
        # Manager and every other built-in role. Administrator keeps working
        # (it bypasses permission checks), which is why this hides in testing.
        #
        # setup_custom_perms() copies the standard rows into Custom DocPerm
        # first -- exactly what Frappe's own Role Permission Manager does
        # before adding a rule. It is a no-op once custom perms exist.
        setup_custom_perms(doctype)

        if frappe.db.exists("Custom DocPerm",
                            {"parent": doctype, "role": STOREFRONT_BUYER_ROLE}):
            continue

        frappe.get_doc({
            "doctype": "Custom DocPerm",
            "parent": doctype,
            "parenttype": "DocType",
            "parentfield": "permissions",
            "role": STOREFRONT_BUYER_ROLE,
            "permlevel": 0,
            # Read ONLY. `export` is set explicitly to 0 rather than left to
            # the field default, which is 1 -- a storefront buyer must not be
            # able to export the item master.
            "read": 1,
            "export": 0,
            "select": 0,
            "write": 0,
            "create": 0,
            "delete": 0,
            "report": 0,
            "share": 0,
            "print": 0,
            "email": 0,
        }).insert(ignore_permissions=True)


def _has_enabled_storefront_grant(user: str) -> bool:
    """True when any ENABLED STOREFRONT grant still exists for the user."""

    return bool(frappe.db.exists("YOB User Application Access", {
        "user": user,
        "application": STOREFRONT_APP_CODE,
        "enabled": 1,
    }))


def _set_role(user: str, wanted: bool) -> None:
    if not user or not frappe.db.exists("User", user):
        return

    doc = frappe.get_doc("User", user)
    present = any(r.role == STOREFRONT_BUYER_ROLE for r in doc.roles)

    if wanted and not present:
        doc.append("roles", {"role": STOREFRONT_BUYER_ROLE})
    elif not wanted and present:
        doc.set("roles", [r for r in doc.roles if r.role != STOREFRONT_BUYER_ROLE])
    else:
        return

    doc.save(ignore_permissions=True)


def sync_storefront_role(user: str) -> None:
    """Bring the user's role into line with their remaining enabled grants.

    Recomputed from ALL enabled grants, never from the single grant being
    changed -- another enabled grant may still require the role.
    """

    _set_role(user, _has_enabled_storefront_grant(user))


# ---------------------------------------------------------------
# doc_events handlers on `YOB User Application Access`
#
# Registered by yob_storefront, NOT yob_auth: yob_auth must never know about a
# solution app. The handlers no-op for other applications so a future VENDOR or
# WORKER grant on the same DocType is untouched.
# ---------------------------------------------------------------

def on_application_access_update(doc, method=None):
    if doc.application != STOREFRONT_APP_CODE:
        return
    sync_storefront_role(doc.user)


def on_application_access_trash(doc, method=None):
    """The row is gone by the time we recompute, so this is still correct."""

    if doc.application != STOREFRONT_APP_CODE:
        return
    sync_storefront_role(doc.user)
