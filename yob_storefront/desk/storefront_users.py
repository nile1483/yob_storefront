# Copyright (c) 2026, YOB and Shayona
"""Desk management of STOREFRONT access from the Customer form.

WHY THIS EXISTS
---------------
Granting a buyer storefront access means creating one
``YOB User Application Access`` row. An administrator had to leave the Customer,
open that DocType, and fill in four fields of which three are dictated by
context -- ``application``, ``profile_doctype`` and ``profile_name``. Getting any
of them wrong produces an access record that silently resolves to the wrong
Customer, or to none.

This module is the Customer form's view of those rows. It is a THIN ADAPTER:
every write goes through ``frappe.get_doc(...).insert()`` / ``.save()`` /
``.delete()`` on the real DocType, so the controller's own validation and the
``doc_events`` role-sync hooks in ``permissions.storefront_role`` run exactly as
they do from the Desk list, from Data Import, and from ``bench execute``.

WHAT IT IS NOT
--------------
* NOT a second source of truth. Nothing is persisted on Customer -- no child
  table, no mirror. The Customer form renders what this module reads back out of
  ``YOB User Application Access`` and holds no copy of it. A child table would
  have to be reconciled on every Customer save, including the overwhelming
  majority that never touch this section, and a divergence between the two would
  be a security bug rather than a display bug.
* NOT a role manager. ``YOB Storefront Buyer`` is granted and revoked only by
  the existing hooks on the access record. This module never touches
  ``User.roles``.
* NOT reachable by a buyer. Every entry point gates on the real DocPerms, which
  grant ``YOB User Application Access`` to System Manager alone.
"""

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime

from yob_storefront.utils.context import STOREFRONT_APP

ACCESS_DOCTYPE = "YOB User Application Access"
PROFILE_DOCTYPE = "Customer"

#: Accounts that must never be given a storefront profile, whatever an
#: administrator types. `Guest` is the unauthenticated identity and
#: `Administrator` bypasses every permission check in Frappe, so mapping either
#: to a Customer would hand that Customer's storefront to everyone, or hand the
#: whole site to one buyer session.
RESERVED_USERS = {"Guest", "Administrator"}


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def _assert_may_read(customer: str) -> None:
    """Readers need the Customer AND the access record.

    Two checks rather than one. The Customer check is record-level, so a user
    restricted to some customers cannot enumerate another's buyers; the access
    check is what keeps the whole feature inside the role that owns storefront
    access. A storefront buyer holds neither and is refused before any query.
    """

    frappe.has_permission(PROFILE_DOCTYPE, "read", doc=customer, throw=True)
    frappe.has_permission(ACCESS_DOCTYPE, "read", throw=True)


def _assert_may_write(customer: str, ptype: str) -> None:
    """Writers need to SEE the Customer and hold the matching verb on the grant.

    ``ptype`` is the real operation -- ``create``, ``write`` or ``delete`` -- so
    an administrator who may edit an existing grant but not create one is
    refused at the right place rather than at the broadest one.

    The Customer check is READ, not write, and that is deliberate. None of these
    operations modifies the Customer document; they modify an access record that
    happens to point at it. Demanding Customer write would mean that granting one
    buyer a login also required the right to edit the customer master -- a
    strictly broader permission than the task needs, which is backwards. Read is
    what the panel's own location already requires (an operator who cannot open
    the Customer form cannot reach this section) and, being record-level, it is
    what keeps a User Permission restriction from being bypassed here.

    The real authority is the second check. ``YOB User Application Access`` is
    granted to System Manager alone, so that is the role gate on every write.
    """

    frappe.has_permission(PROFILE_DOCTYPE, "read", doc=customer, throw=True)
    frappe.has_permission(ACCESS_DOCTYPE, ptype, throw=True)


def _assert_customer_saved(customer: str | None) -> str:
    """The Customer must already exist before anything may point at it.

    A Customer form in `Not Saved` state has a provisional name like
    ``new-customer-abc123``. Creating an access row against it would produce a
    grant to a Customer that never comes into existence, or -- worse -- one that
    later collides with a real docname.
    """

    customer = (customer or "").strip()

    if not customer:
        frappe.throw(_("Customer is required."))

    if not frappe.db.exists(PROFILE_DOCTYPE, customer):
        frappe.throw(_("Save the Customer before adding storefront users."))

    return customer


def _load_access(name: str | None):
    """Load one access row, refusing anything this feature does not own.

    The docname arrives from the browser, so the application and profile type
    are re-checked here. Without that, this module's update and delete methods
    would be a general editor for EVERY YOB application's access records --
    a STOREFRONT-shaped endpoint quietly editing a grant for some other
    application entirely.
    """

    # Permission FIRST, then existence. The other order lets an unauthorised
    # caller tell a real access docname from a made-up one by the difference
    # between "no longer exists" and PermissionError -- a small leak, but a free
    # one to close.
    frappe.has_permission(ACCESS_DOCTYPE, "read", throw=True)

    name = (name or "").strip()

    if not name or not frappe.db.exists(ACCESS_DOCTYPE, name):
        frappe.throw(_("This storefront access record no longer exists."))

    doc = frappe.get_doc(ACCESS_DOCTYPE, name)

    if doc.application != STOREFRONT_APP or doc.profile_doctype != PROFILE_DOCTYPE:
        frappe.throw(_("This access record is not a storefront Customer grant."))

    return doc


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


def _status(row) -> str:
    """One word for what this grant is doing RIGHT NOW.

    Derived from the same three fields ``yob_auth.security.access.resolve_access``
    tests, and in the same order, so the badge an administrator reads cannot
    disagree with the answer a buyer gets at the login screen.
    """

    if not row.get("enabled"):
        return "Disabled"

    current = now_datetime()

    if row.get("valid_from") and get_datetime(row["valid_from"]) > current:
        return "Scheduled"

    if row.get("valid_until") and get_datetime(row["valid_until"]) < current:
        return "Expired"

    if not row.get("user_enabled"):
        return "User Disabled"

    return "Active"


@frappe.whitelist()
def list_storefront_users(customer=None):
    """Every STOREFRONT grant pointing at this Customer.

    Read straight from the access DocType on each call -- there is no cached or
    mirrored copy anywhere -- so the section always shows what the security layer
    will actually act on.
    """

    customer = (customer or "").strip()

    if not customer or not frappe.db.exists(PROFILE_DOCTYPE, customer):
        # An unsaved Customer has nothing to show. Answering with an empty list
        # rather than throwing keeps the form quiet while it is being created.
        return []

    _assert_may_read(customer)

    rows = frappe.get_all(
        ACCESS_DOCTYPE,
        filters={
            "application": STOREFRONT_APP,
            "profile_doctype": PROFILE_DOCTYPE,
            "profile_name": customer,
        },
        fields=["name", "user", "enabled", "valid_from", "valid_until"],
        order_by="creation asc",
        limit_page_length=0,
    )

    for row in rows:
        user = frappe.db.get_value(
            "User", row.user, ["full_name", "enabled", "user_type"], as_dict=True
        ) or frappe._dict()
        row.full_name = user.get("full_name")
        row.user_enabled = user.get("enabled")
        row.user_type = user.get("user_type")
        # Reported, never managed here: the hooks own it. Showing it is what lets
        # an administrator see that disabling a grant really did revoke the role.
        row.has_buyer_role = bool(
            frappe.db.exists("Has Role", {"parent": row.user,
                                          "role": "YOB Storefront Buyer"})
        )
        row.status = _status(row)

    return rows


# ---------------------------------------------------------------------------
# Validation shared by both creation paths
# ---------------------------------------------------------------------------


def _validate_user_for_storefront(user: str) -> None:
    """Refuse an account that must not become a storefront buyer.

    System Users are refused rather than converted. ``user_type`` is not a
    cosmetic flag -- it is what makes an account eligible for Desk -- so
    flipping it from this form would quietly demote a colleague's account, and
    granting storefront access to a Desk account hands a buyer session the Desk
    eligibility the onboarding standard deliberately withholds.

    The administrator is told which account to use instead; nothing is changed
    for them.
    """

    if user in RESERVED_USERS:
        frappe.throw(_("{0} cannot be given storefront access.").format(frappe.bold(user)))

    user_type = frappe.db.get_value("User", user, "user_type")

    if user_type != "Website User":
        frappe.throw(
            _(
                "{0} is a {1}. Storefront buyers must be Website Users, and this "
                "will not change the account's User Type for you. Create a "
                "Website User for the buyer instead."
            ).format(frappe.bold(user), frappe.bold(user_type or _("System User")))
        )


def _assert_not_already_granted(user: str, customer: str) -> None:
    """One STOREFRONT grant per User, enforced here with a readable reason.

    The access controller already refuses a duplicate ``(user, application)``
    pair, but its message cannot name the Customer because the DocType knows
    nothing about what a profile means. This check runs first purely so the
    administrator is told WHICH Customer already holds the account -- the
    controller remains the authority and would refuse this anyway.

    A grant is deliberately never MOVED between Customers. Reassigning a buyer
    silently is how one company's user ends up looking at another company's
    orders, so the existing grant must be removed as its own deliberate act.
    """

    existing = frappe.db.get_value(
        ACCESS_DOCTYPE,
        {"user": user, "application": STOREFRONT_APP},
        ["name", "profile_name"],
        as_dict=True,
    )

    if not existing:
        return

    if existing.profile_name == customer:
        frappe.throw(_("This user already has storefront access to this customer."))

    frappe.throw(
        _(
            "{0} already has STOREFRONT access to {1}. A storefront user can "
            "currently belong to only one Customer."
        ).format(frappe.bold(user), frappe.bold(existing.profile_name or _("another Customer")))
    )


def _insert_access(customer, user, enabled, valid_from, valid_until):
    """Create the grant through the real DocType. No shortcuts.

    ``application``, ``profile_doctype`` and ``profile_name`` are set from
    context and are never accepted from the caller: this adapter exists so an
    administrator cannot get them wrong, and so this endpoint cannot be used to
    map a User into some other application.

    Inserted WITHOUT ``ignore_permissions`` -- the caller has already been shown
    to hold create on the DocType, so the framework check is a second, free
    confirmation rather than an obstacle.
    """

    doc = frappe.get_doc({
        "doctype": ACCESS_DOCTYPE,
        "user": user,
        "application": STOREFRONT_APP,
        "profile_doctype": PROFILE_DOCTYPE,
        "profile_name": customer,
        "enabled": 1 if cint(enabled) else 0,
        "valid_from": valid_from or None,
        "valid_until": valid_until or None,
    })
    doc.insert()

    return doc


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


@frappe.whitelist()
def add_storefront_user(customer=None, user=None, enabled=1, valid_from=None, valid_until=None):
    """Grant an EXISTING Website User storefront access to this Customer."""

    customer = _assert_customer_saved(customer)
    _assert_may_write(customer, "create")

    user = (user or "").strip()

    if not user:
        frappe.throw(_("User is required."))

    if not frappe.db.exists("User", user):
        frappe.throw(_("User {0} does not exist.").format(frappe.bold(user)))

    _validate_user_for_storefront(user)
    _assert_not_already_granted(user, customer)

    doc = _insert_access(customer, user, enabled, valid_from, valid_until)

    return {"name": doc.name, "user": doc.user}


@frappe.whitelist()
def create_storefront_user(customer=None, email=None, first_name=None,
                           send_welcome_email=0, enabled=1,
                           valid_from=None, valid_until=None):
    """Create a Website User AND its grant, in one transaction.

    Atomic without any explicit transaction handling: a Frappe request is one
    transaction, the User controller performs no commit of its own (verified in
    ``frappe/core/doctype/user/user.py``), and every failure below raises. So a
    refusal at the grant stage rolls the User back with it, and no half-onboarded
    account is left behind. ``frappe.db.commit()`` is deliberately NOT called --
    calling it here would publish the User before the grant is known to succeed
    and create exactly the orphan this docstring promises not to.

    The User is validated BEFORE it is created, so the common mistakes -- an
    address that is already a System User, or one that already belongs to another
    Customer -- are refused without writing anything at all.
    """

    customer = _assert_customer_saved(customer)
    _assert_may_write(customer, "create")
    frappe.has_permission("User", "create", throw=True)

    email = (email or "").strip().lower()
    first_name = (first_name or "").strip()

    if not email:
        frappe.throw(_("Email is required."))

    if not first_name:
        frappe.throw(_("First Name is required."))

    if frappe.db.exists("User", email):
        # Not an error the administrator can do nothing about: tell them the
        # account is there and which action to use, rather than refusing flatly.
        frappe.throw(
            _("User {0} already exists. Use {1} to grant it access.").format(
                frappe.bold(email), frappe.bold(_("Add Existing User"))
            )
        )

    # Checked before the User is written, so a refusal costs nothing.
    _assert_not_already_granted(email, customer)

    user_doc = frappe.get_doc({
        "doctype": "User",
        "email": email,
        "first_name": first_name,
        # The onboarding standard: a storefront buyer is a Website User and
        # gains no Desk eligibility. `YOB Storefront Buyer` carries
        # `desk_access = 0`, so Frappe's own `set_system_user()` leaves this
        # alone when the role-sync hook attaches the role a moment later.
        "user_type": "Website User",
        "send_welcome_email": 1 if cint(send_welcome_email) else 0,
    })
    user_doc.insert()

    # No role is assigned here. The hooks on the access record below grant
    # `YOB Storefront Buyer`, and they are also what revoke it later.
    doc = _insert_access(customer, user_doc.name, enabled, valid_from, valid_until)

    return {"name": doc.name, "user": user_doc.name}


@frappe.whitelist()
def update_storefront_user(name=None, enabled=None, valid_from=None, valid_until=None):
    """Change ``enabled`` / ``valid_from`` / ``valid_until`` on one grant.

    Saved through the document so the controller's date-order check runs, and so
    the ``on_update`` hook re-syncs ``YOB Storefront Buyer``: disabling revokes
    the role, re-enabling restores it. That sync is NOT reimplemented here --
    there is one implementation of it and this is not it.

    Only the three fields an administrator may edit are touched. ``user``,
    ``application``, ``profile_doctype`` and ``profile_name`` are immutable from
    this form: changing the user or the profile is a different grant, and must be
    a remove plus an add so it is visible as such.
    """

    doc = _load_access(name)
    _assert_may_write(doc.profile_name, "write")

    if enabled is not None:
        doc.enabled = 1 if cint(enabled) else 0

    # Sent explicitly as "" by the UI to clear a date, so presence -- not
    # truthiness -- decides whether the field is touched. `None` means the
    # caller did not send the field and the stored value stands.
    if valid_from is not None:
        doc.valid_from = valid_from or None

    if valid_until is not None:
        doc.valid_until = valid_until or None

    doc.save()

    return {"name": doc.name, "enabled": doc.enabled}


@frappe.whitelist()
def remove_storefront_user(name=None):
    """Delete ONE grant. Storefront access only.

    The ``on_trash`` hook recomputes the user's role from their REMAINING
    enabled grants, so the role is dropped when this was the last one and kept
    when it was not.

    Deliberately leaves the User, the Contact, the Address, the Cart and any
    Sales Order untouched. Those are business records of the Customer, not of
    the access grant, and destroying them from a permissions screen would be
    unrecoverable.
    """

    doc = _load_access(name)
    customer, user = doc.profile_name, doc.user
    _assert_may_write(customer, "delete")

    doc.delete()

    return {"user": user, "customer": customer}
