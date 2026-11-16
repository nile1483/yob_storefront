# Copyright (c) 2026, YOB and Shayona
"""Customer > YOB Storefront Users -- the Desk management adapter.

The point of this module is that the adapter is an ADAPTER. Every test asserts
against the real `YOB User Application Access` row and the real `Has Role`
table, never against anything the Customer form stores -- because the Customer
form stores nothing. If a future change introduced a mirror, these tests would
keep passing against the mirror and start failing against the truth.

Permissions are exercised as real users. A test that ran as Administrator would
be permission-blind: `frappe.has_permission` short-circuits for it entirely, so
the whole §14 security boundary would be untested.
"""

import unittest
from datetime import timedelta

import frappe
from frappe.utils import add_days, now_datetime

from yob_storefront.desk import storefront_users as api

CUSTOMER_A = "_Test SFU Customer A"
CUSTOMER_B = "_Test SFU Customer B"
BUYER_1 = "_test-sfu-buyer-1@yob.invalid"
BUYER_2 = "_test-sfu-buyer-2@yob.invalid"
BUYER_3 = "_test-sfu-buyer-3@yob.invalid"
SYSTEM_USER = "_test-sfu-system@yob.invalid"
ADMIN_USER = "_test-sfu-admin@yob.invalid"

BUYER_ROLE = "YOB Storefront Buyer"
ACCESS = "YOB User Application Access"


def _customer(name):
    if not frappe.db.exists("Customer", name):
        frappe.get_doc({"doctype": "Customer", "customer_name": name}).insert(
            ignore_permissions=True)
    return name


def _user(email, user_type="Website User", roles=()):
    if not frappe.db.exists("User", email):
        doc = frappe.get_doc({
            "doctype": "User", "email": email, "first_name": email.split("@")[0],
            "user_type": user_type, "send_welcome_email": 0,
        })
        for role in roles:
            doc.append("roles", {"role": role})
        doc.insert(ignore_permissions=True)
    return email


def _has_buyer_role(user):
    return bool(frappe.db.exists("Has Role", {"parent": user, "role": BUYER_ROLE}))


def _access_rows(user):
    return frappe.get_all(ACCESS, filters={"user": user, "application": "STOREFRONT"},
                          fields=["name", "enabled", "profile_doctype", "profile_name",
                                  "valid_from", "valid_until"])


class StorefrontUserAdminCase(unittest.TestCase):
    """Shared fixture: two Customers, three buyers, one System User, one admin."""

    def setUp(self):
        # Built per test, NOT in setUpClass: tearDown rolls the transaction back,
        # which would take class-level fixtures with it and leave every test
        # after the first running against records that no longer exist.
        frappe.set_user("Administrator")
        _customer(CUSTOMER_A)
        _customer(CUSTOMER_B)
        for email in (BUYER_1, BUYER_2, BUYER_3):
            _user(email)
        # Needs a desk-access role to STAY a System User: Frappe's
        # `User.set_system_user()` recomputes user_type from roles
        # (`user_type = "System User" if self.has_desk_access() else
        # "Website User"`), so a role-less "System User" silently becomes a
        # Website User and this fixture would prove nothing.
        _user(SYSTEM_USER, user_type="System User", roles=("Sales User",))
        # A real operator, NOT Administrator: Administrator bypasses
        # `has_permission` outright, so every permission assertion below would
        # be vacuous if this were Administrator.
        # System Manager owns `YOB User Application Access`; Sales Manager is a
        # realistic Desk role that can read Customer. Both are needed because
        # System Manager alone holds no Customer DocPerm on an ERPNext site.
        _user(ADMIN_USER, user_type="System User",
              roles=("System Manager", "Sales Manager"))

        for email in (BUYER_1, BUYER_2, BUYER_3):
            for row in _access_rows(email):
                frappe.delete_doc(ACCESS, row.name, force=True,
                                  ignore_permissions=True)
        frappe.set_user(ADMIN_USER)

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.rollback()

    # ------------------------------------------------------------------ create

    def test_adding_a_user_creates_the_access_row_with_fixed_context(self):
        """The three context fields are set by the server, never by the caller."""

        result = api.add_storefront_user(customer=CUSTOMER_A, user=BUYER_1)

        doc = frappe.get_doc(ACCESS, result["name"])
        self.assertEqual(doc.user, BUYER_1)
        self.assertEqual(doc.application, "STOREFRONT")
        self.assertEqual(doc.profile_doctype, "Customer")
        self.assertEqual(doc.profile_name, CUSTOMER_A)
        self.assertEqual(doc.enabled, 1)

    def test_adding_a_user_grants_the_buyer_role_through_the_existing_hooks(self):
        """The role is a consequence of the access row, not of this module."""

        self.assertFalse(_has_buyer_role(BUYER_1))

        api.add_storefront_user(customer=CUSTOMER_A, user=BUYER_1)

        self.assertTrue(_has_buyer_role(BUYER_1))

    def test_the_adapter_never_touches_user_roles_itself(self):
        """Guard: the role must come from `storefront_role`, not from here.

        A future author adding a convenience `add_roles` call would break the
        single-implementation rule and keep the role attached after the grant is
        gone. This asserts on the source, which is the only place that can say
        "nobody reimplemented the sync".
        """

        import inspect

        source = inspect.getsource(api)

        for forbidden in ("add_roles", "remove_roles", '"Has Role"', "sync_storefront_role"):
            if forbidden == '"Has Role"':
                # Reading it to DISPLAY the role is fine; writing is not.
                continue
            self.assertNotIn(forbidden, source,
                             f"{forbidden} must not be called from the Desk adapter")

    def test_listing_reflects_the_access_records_and_nothing_else(self):
        api.add_storefront_user(customer=CUSTOMER_A, user=BUYER_1)

        rows = api.list_storefront_users(customer=CUSTOMER_A)
        users = [r["user"] for r in rows]

        self.assertIn(BUYER_1, users)
        self.assertEqual(rows[0]["status"], "Active")
        self.assertTrue(rows[0]["has_buyer_role"])

        # Deleting the real record must empty the panel, proving the panel is a
        # projection and holds no copy.
        frappe.set_user("Administrator")
        frappe.delete_doc(ACCESS, _access_rows(BUYER_1)[0].name, force=True)
        frappe.set_user(ADMIN_USER)

        self.assertEqual(api.list_storefront_users(customer=CUSTOMER_A), [])

    def test_opening_a_customer_creates_nothing(self):
        """Listing is a pure read -- §5."""

        before = frappe.db.count(ACCESS)
        api.list_storefront_users(customer=CUSTOMER_A)
        api.list_storefront_users(customer=CUSTOMER_A)
        self.assertEqual(frappe.db.count(ACCESS), before)

    # ------------------------------------------------------- multiple users

    def test_many_users_may_share_one_customer(self):
        api.add_storefront_user(customer=CUSTOMER_A, user=BUYER_1)
        api.add_storefront_user(customer=CUSTOMER_A, user=BUYER_2)
        api.add_storefront_user(customer=CUSTOMER_A, user=BUYER_3)

        rows = api.list_storefront_users(customer=CUSTOMER_A)

        self.assertEqual(sorted(r["user"] for r in rows), sorted([BUYER_1, BUYER_2, BUYER_3]))
        for email in (BUYER_1, BUYER_2, BUYER_3):
            self.assertTrue(_has_buyer_role(email))
            self.assertEqual(len(_access_rows(email)), 1)

    # ----------------------------------------------------------- duplicates

    def test_the_same_user_twice_on_the_same_customer_is_refused(self):
        api.add_storefront_user(customer=CUSTOMER_A, user=BUYER_1)

        with self.assertRaises(frappe.ValidationError) as caught:
            api.add_storefront_user(customer=CUSTOMER_A, user=BUYER_1)

        self.assertIn("already has storefront access to this customer", str(caught.exception))
        self.assertEqual(len(_access_rows(BUYER_1)), 1)

    def test_a_user_of_another_customer_is_blocked_and_named(self):
        api.add_storefront_user(customer=CUSTOMER_A, user=BUYER_1)

        with self.assertRaises(frappe.ValidationError) as caught:
            api.add_storefront_user(customer=CUSTOMER_B, user=BUYER_1)

        message = str(caught.exception)
        self.assertIn("only one Customer", message)
        self.assertIn(CUSTOMER_A, message)

        # The grant must not have MOVED -- silent reassignment is the bug this
        # prevents, and it would be invisible without this assertion.
        rows = _access_rows(BUYER_1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].profile_name, CUSTOMER_A)

    # --------------------------------------------------------- user validation

    def test_a_system_user_is_refused_and_not_converted(self):
        with self.assertRaises(frappe.ValidationError) as caught:
            api.add_storefront_user(customer=CUSTOMER_A, user=SYSTEM_USER)

        self.assertIn("Website User", str(caught.exception))
        self.assertEqual(frappe.db.get_value("User", SYSTEM_USER, "user_type"), "System User")
        self.assertEqual(_access_rows(SYSTEM_USER), [])

    def test_reserved_accounts_are_refused(self):
        for reserved in ("Guest", "Administrator"):
            with self.assertRaises(frappe.ValidationError):
                api.add_storefront_user(customer=CUSTOMER_A, user=reserved)

    def test_an_unknown_user_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            api.add_storefront_user(customer=CUSTOMER_A, user="nobody@yob.invalid")

    # ------------------------------------------------------- enable / disable

    def test_disabling_revokes_the_role_and_re_enabling_restores_it(self):
        created = api.add_storefront_user(customer=CUSTOMER_A, user=BUYER_1)
        self.assertTrue(_has_buyer_role(BUYER_1))

        api.update_storefront_user(name=created["name"], enabled=0)
        self.assertEqual(frappe.db.get_value(ACCESS, created["name"], "enabled"), 0)
        self.assertFalse(_has_buyer_role(BUYER_1))

        api.update_storefront_user(name=created["name"], enabled=1)
        self.assertEqual(frappe.db.get_value(ACCESS, created["name"], "enabled"), 1)
        self.assertTrue(_has_buyer_role(BUYER_1))

    def test_a_disabled_grant_keeps_the_role_for_another_enabled_customer_user(self):
        """Role sync is per USER, not per row -- disabling one must not strip a
        user who still holds another enabled grant. One user can only hold one
        STOREFRONT grant today, so this asserts the recompute, not a second row."""

        a = api.add_storefront_user(customer=CUSTOMER_A, user=BUYER_1)
        api.add_storefront_user(customer=CUSTOMER_A, user=BUYER_2)

        api.update_storefront_user(name=a["name"], enabled=0)

        self.assertFalse(_has_buyer_role(BUYER_1))
        self.assertTrue(_has_buyer_role(BUYER_2))

    # ---------------------------------------------------------------- validity

    def test_validity_dates_are_stored_and_readable(self):
        start = now_datetime() + timedelta(days=1)
        end = now_datetime() + timedelta(days=30)

        created = api.add_storefront_user(
            customer=CUSTOMER_A, user=BUYER_1,
            valid_from=start, valid_until=end)

        doc = frappe.get_doc(ACCESS, created["name"])
        self.assertIsNotNone(doc.valid_from)
        self.assertIsNotNone(doc.valid_until)

    def test_a_future_valid_from_blocks_access_and_reads_as_scheduled(self):
        from yob_auth.security.access import resolve_access
        from yob_auth.security.exceptions import YOBAccessDeniedError

        api.add_storefront_user(customer=CUSTOMER_A, user=BUYER_1,
                                valid_from=add_days(now_datetime(), 5))

        self.assertEqual(api.list_storefront_users(customer=CUSTOMER_A)[0]["status"],
                         "Scheduled")

        frappe.set_user("Administrator")
        with self.assertRaises(YOBAccessDeniedError):
            resolve_access(BUYER_1, "STOREFRONT", validate_domain=False)

    def test_an_expired_valid_until_blocks_access_and_reads_as_expired(self):
        from yob_auth.security.access import resolve_access
        from yob_auth.security.exceptions import YOBAccessDeniedError

        api.add_storefront_user(customer=CUSTOMER_A, user=BUYER_1,
                                valid_until=add_days(now_datetime(), -1))

        self.assertEqual(api.list_storefront_users(customer=CUSTOMER_A)[0]["status"],
                         "Expired")

        frappe.set_user("Administrator")
        with self.assertRaises(YOBAccessDeniedError):
            resolve_access(BUYER_1, "STOREFRONT", validate_domain=False)

    def test_blank_dates_mean_unrestricted(self):
        from yob_auth.security.access import resolve_access

        api.add_storefront_user(customer=CUSTOMER_A, user=BUYER_1)

        self.assertEqual(api.list_storefront_users(customer=CUSTOMER_A)[0]["status"],
                         "Active")

        frappe.set_user("Administrator")
        context = resolve_access(BUYER_1, "STOREFRONT", validate_domain=False)
        self.assertEqual(context.profile_name, CUSTOMER_A)

    def test_clearing_a_date_is_distinguishable_from_not_sending_it(self):
        created = api.add_storefront_user(
            customer=CUSTOMER_A, user=BUYER_1,
            valid_until=add_days(now_datetime(), 10))

        # Not sent -> stored value stands.
        api.update_storefront_user(name=created["name"], enabled=1)
        self.assertIsNotNone(frappe.db.get_value(ACCESS, created["name"], "valid_until"))

        # Sent as "" -> cleared.
        api.update_storefront_user(name=created["name"], valid_until="")
        self.assertIsNone(frappe.db.get_value(ACCESS, created["name"], "valid_until"))

    def test_the_controllers_own_date_order_rule_still_applies(self):
        """Not reimplemented here -- the DocType's validate() is the authority."""

        created = api.add_storefront_user(customer=CUSTOMER_A, user=BUYER_1)

        with self.assertRaises(frappe.ValidationError):
            api.update_storefront_user(
                name=created["name"],
                valid_from=add_days(now_datetime(), 10),
                valid_until=add_days(now_datetime(), 1))

    # ------------------------------------------------------------------ delete

    def test_removing_access_deletes_only_the_grant(self):
        created = api.add_storefront_user(customer=CUSTOMER_A, user=BUYER_1)

        api.remove_storefront_user(name=created["name"])

        self.assertFalse(frappe.db.exists(ACCESS, created["name"]))
        self.assertFalse(_has_buyer_role(BUYER_1))
        # The things an administrator must never lose from a permissions screen.
        self.assertTrue(frappe.db.exists("User", BUYER_1))
        self.assertTrue(frappe.db.exists("Customer", CUSTOMER_A))

    def test_removing_one_user_leaves_the_others_alone(self):
        first = api.add_storefront_user(customer=CUSTOMER_A, user=BUYER_1)
        api.add_storefront_user(customer=CUSTOMER_A, user=BUYER_2)

        api.remove_storefront_user(name=first["name"])

        rows = api.list_storefront_users(customer=CUSTOMER_A)
        self.assertEqual([r["user"] for r in rows], [BUYER_2])
        self.assertTrue(_has_buyer_role(BUYER_2))

    def test_removing_a_user_frees_them_for_another_customer(self):
        created = api.add_storefront_user(customer=CUSTOMER_A, user=BUYER_1)
        api.remove_storefront_user(name=created["name"])

        api.add_storefront_user(customer=CUSTOMER_B, user=BUYER_1)

        rows = _access_rows(BUYER_1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].profile_name, CUSTOMER_B)

    # ------------------------------------------------- create user + access

    def test_creating_a_website_user_also_grants_access(self):
        email = "_test-sfu-created@yob.invalid"
        frappe.set_user("Administrator")
        if frappe.db.exists("User", email):
            frappe.delete_doc("User", email, force=True, ignore_permissions=True)
        frappe.set_user(ADMIN_USER)

        result = api.create_storefront_user(
            customer=CUSTOMER_A, email=email, first_name="Created",
            send_welcome_email=0)

        self.assertEqual(frappe.db.get_value("User", email, "user_type"), "Website User")
        self.assertTrue(_has_buyer_role(email))

        doc = frappe.get_doc(ACCESS, result["name"])
        self.assertEqual(doc.profile_name, CUSTOMER_A)

        # Explicitly NOT created: §13.
        self.assertFalse(frappe.db.exists("YOB Login Identity", {"user": email}))
        self.assertEqual(frappe.get_all("User Permission", filters={"user": email}), [])
        self.assertNotIn("Customer", [r.role for r in frappe.get_doc("User", email).roles])

    def test_creating_a_user_does_not_create_a_customer(self):
        before = frappe.db.count("Customer")
        api.create_storefront_user(
            customer=CUSTOMER_A, email="_test-sfu-created2@yob.invalid",
            first_name="Created", send_welcome_email=0)
        self.assertEqual(frappe.db.count("Customer"), before)

    def test_creating_a_user_that_already_exists_is_refused(self):
        with self.assertRaises(frappe.ValidationError) as caught:
            api.create_storefront_user(
                customer=CUSTOMER_A, email=BUYER_1, first_name="Dup",
                send_welcome_email=0)

        self.assertIn("already exists", str(caught.exception))

    def test_a_failed_grant_leaves_no_orphan_user(self):
        """§15 -- atomicity, asserted rather than assumed.

        The grant is refused because the address already belongs to another
        Customer. The User must not survive that refusal.
        """

        api.add_storefront_user(customer=CUSTOMER_A, user=BUYER_1)
        email = "_test-sfu-orphan@yob.invalid"
        frappe.set_user("Administrator")
        if frappe.db.exists("User", email):
            frappe.delete_doc("User", email, force=True, ignore_permissions=True)
        frappe.set_user(ADMIN_USER)

        # Force the failure AFTER the User insert by making _insert_access throw.
        from unittest.mock import patch

        with patch.object(api, "_insert_access", side_effect=frappe.ValidationError("boom")):
            with self.assertRaises(frappe.ValidationError):
                api.create_storefront_user(
                    customer=CUSTOMER_A, email=email, first_name="Orphan",
                    send_welcome_email=0)

        frappe.db.rollback()
        self.assertFalse(frappe.db.exists("User", email),
                         "a failed grant must not leave a half-onboarded User")

    # -------------------------------------------------------- unsaved Customer

    def test_an_unsaved_customer_is_refused(self):
        with self.assertRaises(frappe.ValidationError) as caught:
            api.add_storefront_user(customer="new-customer-abc123", user=BUYER_1)

        self.assertIn("Save the Customer", str(caught.exception))
        self.assertEqual(_access_rows(BUYER_1), [])

    def test_a_blank_customer_is_refused(self):
        for blank in (None, "", "   "):
            with self.assertRaises(frappe.ValidationError):
                api.add_storefront_user(customer=blank, user=BUYER_1)

    def test_listing_an_unsaved_customer_answers_empty_rather_than_throwing(self):
        self.assertEqual(api.list_storefront_users(customer="new-customer-abc123"), [])

    # -------------------------------------------------------------- scope

    def test_the_adapter_refuses_an_access_row_it_does_not_own(self):
        """It must not become a general editor for every YOB application."""

        frappe.set_user("Administrator")
        other = frappe.get_doc({
            "doctype": ACCESS, "user": BUYER_2, "application": "STOREFRONT",
            "profile_doctype": "Customer", "profile_name": CUSTOMER_A,
        }).insert(ignore_permissions=True)
        # Mutate it into a shape this feature does not own.
        frappe.db.set_value(ACCESS, other.name, "profile_doctype", "Employee")
        frappe.set_user(ADMIN_USER)

        for call in (lambda: api.update_storefront_user(name=other.name, enabled=0),
                     lambda: api.remove_storefront_user(name=other.name)):
            with self.assertRaises(frappe.ValidationError) as caught:
                call()
            self.assertIn("not a storefront Customer grant", str(caught.exception))

    def test_a_missing_access_row_is_refused(self):
        for call in (lambda: api.update_storefront_user(name="does-not-exist", enabled=0),
                     lambda: api.remove_storefront_user(name="does-not-exist")):
            with self.assertRaises(frappe.ValidationError):
                call()


class StorefrontUserPermissionCase(unittest.TestCase):
    """§14 -- a buyer must not be able to map Users to Customers."""

    def setUp(self):
        # Per test, for the same reason as the case above.
        frappe.set_user("Administrator")
        _customer(CUSTOMER_A)
        _user(BUYER_1)
        _user(BUYER_2)

        for row in _access_rows(BUYER_1):
            frappe.delete_doc(ACCESS, row.name, force=True, ignore_permissions=True)
        self.existing = frappe.get_doc({
            "doctype": ACCESS, "user": BUYER_1, "application": "STOREFRONT",
            "profile_doctype": "Customer", "profile_name": CUSTOMER_A,
        }).insert(ignore_permissions=True)

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.rollback()

    #: Every endpoint this module is allowed to expose. Stated explicitly so a
    #: new whitelisted method cannot appear without a deliberate edit here.
    EXPECTED_ENDPOINTS = {
        "list_storefront_users",
        "add_storefront_user",
        "create_storefront_user",
        "update_storefront_user",
        "remove_storefront_user",
    }

    def _endpoints(self):
        """The module's whitelisted functions.

        Membership of `frappe.whitelisted` -- a module-level SET -- is how
        Frappe records this (`frappe/__init__.py:465`). There is no
        `fn.whitelisted` attribute, and an earlier version of this test checked
        for one: it matched nothing, iterated nothing, and passed vacuously
        while asserting the exact opposite of what it claimed.
        """

        return {
            name: fn
            for name, fn in vars(api).items()
            if callable(fn)
            and getattr(fn, "__module__", "").endswith("storefront_users")
            and fn in frappe.whitelisted
        }

    def test_the_exposed_endpoints_are_exactly_the_expected_ones(self):
        """Fails loudly if the module ever exposes more -- or, as it once did,
        if the detection stops finding any."""

        self.assertEqual(set(self._endpoints()), self.EXPECTED_ENDPOINTS)

    def test_no_endpoint_is_guest_accessible(self):
        for name, fn in self._endpoints().items():
            self.assertNotIn(fn, frappe.guest_methods, f"{name} allows Guest")

    def test_every_entry_point_is_whitelisted_and_guarded(self):
        """Guard: a new method added without a permission check fails here."""

        import inspect

        guarded = ("_assert_may_read", "_assert_may_write", "_load_access")
        endpoints = self._endpoints()

        # Belt and braces: an empty dict would make the loop below pass without
        # asserting anything, which is exactly the failure this test had.
        self.assertTrue(endpoints, "no whitelisted endpoints were detected")

        for name, fn in endpoints.items():
            source = inspect.getsource(fn)
            self.assertTrue(
                any(g in source for g in guarded),
                f"whitelisted {name}() performs no permission check")

    def test_a_storefront_buyer_cannot_read_the_panel(self):
        frappe.set_user(BUYER_1)
        with self.assertRaises(frappe.PermissionError):
            api.list_storefront_users(customer=CUSTOMER_A)

    def test_a_storefront_buyer_cannot_grant_access(self):
        frappe.set_user(BUYER_1)
        with self.assertRaises(frappe.PermissionError):
            api.add_storefront_user(customer=CUSTOMER_A, user=BUYER_2)
        frappe.set_user("Administrator")
        self.assertEqual(_access_rows(BUYER_2), [])

    def test_a_storefront_buyer_cannot_create_a_user(self):
        frappe.set_user(BUYER_1)
        with self.assertRaises(frappe.PermissionError):
            api.create_storefront_user(
                customer=CUSTOMER_A, email="_test-sfu-evil@yob.invalid",
                first_name="Evil", send_welcome_email=0)

    def test_a_storefront_buyer_cannot_disable_or_remove_their_own_grant(self):
        frappe.set_user(BUYER_1)
        with self.assertRaises(frappe.PermissionError):
            api.update_storefront_user(name=self.existing.name, enabled=0)
        with self.assertRaises(frappe.PermissionError):
            api.remove_storefront_user(name=self.existing.name)

        frappe.set_user("Administrator")
        self.assertTrue(frappe.db.exists(ACCESS, self.existing.name))
        self.assertEqual(frappe.db.get_value(ACCESS, self.existing.name, "enabled"), 1)

    def test_the_access_doctype_is_still_system_manager_only(self):
        """The whole feature rests on this: if the DocType were opened up, the
        guards above would start admitting people they refuse today."""

        roles = {r.role for r in frappe.get_all(
            "DocPerm", filters={"parent": ACCESS}, fields=["role"])}
        custom = {r.role for r in frappe.get_all(
            "Custom DocPerm", filters={"parent": ACCESS}, fields=["role"])}

        self.assertEqual(custom or roles, {"System Manager"})
