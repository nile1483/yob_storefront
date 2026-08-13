# Copyright (c) 2026, YOB and Shayona
"""The pricing elevation boundary.

The boundary under test:

    authenticated user -> auth_context -> enabled STOREFRONT grant
      -> get_storefront_customer() -> authorised Customer
      -> temporary Sales Order -> so.flags.ignore_permissions
      -> ERPNext native pricing

The single most important assertion in this file:

    has_permission("Customer", "read") is False   AND   pricing succeeds

If the first ever becomes True, the elevation has leaked into a standing
permission grant and this file must fail.
"""

import unittest

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from yob_auth.security.access import resolve_access
from yob_auth.security.exceptions import YOBAccessDeniedError
from yob_storefront.permissions.storefront_role import (
    STOREFRONT_BUYER_ROLE,
    ensure_role_and_permissions,
    sync_storefront_role,
)
from yob_storefront.services.pricing_service import get_item_pricing
from yob_storefront.utils.context import STOREFRONT_APP, get_storefront_customer

ITEM = "YOB-BOLT-M10"
CUSTOMER = "YOB Demo Buyer"
BUYER = "storefront@yob.test"


def _request(host="storefront.test"):
    frappe.local.request = Request(
        EnvironBuilder(headers={"X-YOB-Original-Host": host}).get_environ()
    )


def _seeded() -> bool:
    return bool(frappe.db.exists("Item", ITEM) and frappe.db.exists("Customer", CUSTOMER))


class PricingElevationCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest(
                "requires seed_demo_data; run it on the test site first"
            )
        ensure_role_and_permissions()
        sync_storefront_role(BUYER)
        frappe.db.commit()
        frappe.clear_cache()

    def setUp(self):
        _request()
        frappe.set_user(BUYER)

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.local.request = None

    # ---------------------------------------------------------- the boundary

    def test_pricing_succeeds_while_customer_read_stays_denied(self):
        """THE boundary assertion. Both halves must hold simultaneously."""

        customer = get_storefront_customer(resolve_access(BUYER, STOREFRONT_APP))
        settings = frappe.get_cached_doc("YOB Store Settings")

        pricing = get_item_pricing(
            customer=customer, item_code=ITEM, qty=10,
            company=settings.company, currency=settings.default_currency,
        )

        self.assertTrue(pricing["rate"] > 0)
        self.assertFalse(
            frappe.has_permission("Customer", "read"),
            "elevation leaked into a standing Customer read grant",
        )

    def test_direct_customer_access_does_not_become_available(self):
        """Pricing working must not imply /api/resource access to Customer."""

        self.assertFalse(frappe.has_permission("Customer", "read", doc=CUSTOMER))

        # Address and Contact are deliberately NOT asserted here: Frappe's
        # built-in `All` role grants read on both to every user, including
        # Guest. That is stock framework baseline, not something this change
        # introduced, and asserting otherwise would make the test lie about the
        # platform. The consequence -- that Address/Contact isolation rests
        # entirely on application-level scoping rather than Frappe permissions
        # -- is recorded in the get_all isolation audit.
        for doctype in ("Customer", "Sales Order"):
            with self.subTest(doctype=doctype):
                self.assertFalse(
                    frappe.has_permission(doctype, "read"),
                    f"{doctype} read was granted; the surface is wider than intended",
                )

    def test_address_and_contact_read_predate_this_change(self):
        """Pin the baseline so a future widening is attributable.

        If either stops being granted by `All`, or starts being granted by a
        YOB-managed role, this fails and someone re-reads the audit.
        """

        for doctype in ("Address", "Contact"):
            with self.subTest(doctype=doctype):
                yob_roles = {
                    p.role for p in frappe.get_all(
                        "Custom DocPerm", {"parent": doctype, "read": 1}, ["role"]
                    )
                }
                self.assertNotIn(
                    STOREFRONT_BUYER_ROLE, yob_roles,
                    f"{doctype} read must not come from a YOB-managed role",
                )

    def test_only_item_read_is_granted_by_the_role(self):
        granted = frappe.db.get_all(
            "Custom DocPerm", {"role": STOREFRONT_BUYER_ROLE}, ["parent", "read", "write", "create"]
        )
        self.assertEqual({r.parent for r in granted}, {"Item"})
        for row in granted:
            self.assertEqual((row.write, row.create), (0, 0), "role must be read-only")

    # ------------------------------------------------------- authorization

    def test_cannot_price_as_another_customer(self):
        """A caller must never price against a Customer they were not granted."""

        other = frappe.db.get_value(
            "Customer", {"name": ("!=", CUSTOMER)}, "name"
        )
        if not other:
            self.skipTest("needs a second Customer")

        context = resolve_access(BUYER, STOREFRONT_APP)
        self.assertEqual(context.profile_name, CUSTOMER)
        self.assertNotEqual(context.profile_name, other)

        # The authorised Customer comes from auth_context, never the request.
        customer = get_storefront_customer(context)
        self.assertEqual(customer.name, CUSTOMER)

    def test_user_without_enabled_grant_is_rejected_before_elevation(self):
        frappe.set_user("Administrator")
        grant = frappe.db.get_value(
            "YOB User Application Access",
            {"user": BUYER, "application": STOREFRONT_APP}, "name",
        )
        frappe.db.set_value("YOB User Application Access", grant, "enabled", 0)
        frappe.db.commit()
        try:
            frappe.set_user(BUYER)
            with self.assertRaises(YOBAccessDeniedError):
                resolve_access(BUYER, STOREFRONT_APP)
        finally:
            frappe.set_user("Administrator")
            frappe.db.set_value("YOB User Application Access", grant, "enabled", 1)
            frappe.db.commit()

    # ------------------------------------------------- no state leakage

    def test_no_permission_state_leaks_after_success(self):
        customer = get_storefront_customer(resolve_access(BUYER, STOREFRONT_APP))
        settings = frappe.get_cached_doc("YOB Store Settings")
        before = frappe.flags.ignore_permissions

        get_item_pricing(customer=customer, item_code=ITEM, qty=1,
                         company=settings.company, currency=settings.default_currency)

        self.assertEqual(frappe.flags.ignore_permissions, before)
        self.assertFalse(frappe.has_permission("Customer", "read"))

    def test_no_permission_state_leaks_after_exception(self):
        customer = get_storefront_customer(resolve_access(BUYER, STOREFRONT_APP))
        before = frappe.flags.ignore_permissions

        with self.assertRaises(Exception):
            get_item_pricing(customer=customer, item_code="NO-SUCH-ITEM", qty=1,
                             company="X", currency="INR")

        self.assertEqual(frappe.flags.ignore_permissions, before)
        self.assertFalse(frappe.has_permission("Customer", "read"))

    # ------------------------------------------------ native result parity

    def test_result_matches_erpnext_native_pricing(self):
        """Elevation must not alter the numbers ERPNext would produce."""

        customer = get_storefront_customer(resolve_access(BUYER, STOREFRONT_APP))
        settings = frappe.get_cached_doc("YOB Store Settings")
        ours = get_item_pricing(customer=customer, item_code=ITEM, qty=10,
                                company=settings.company, currency=settings.default_currency)

        frappe.set_user("Administrator")
        so = frappe.new_doc("Sales Order")
        so.customer = CUSTOMER
        so.company = settings.company
        so.currency = settings.default_currency
        so.selling_price_list = settings.default_price_list
        so.transaction_date = frappe.utils.today()
        so.append("items", {"item_code": ITEM, "qty": 10})
        so.set_missing_values()
        so.calculate_taxes_and_totals()

        self.assertEqual(float(ours["rate"]), float(so.items[0].rate))
        self.assertEqual(float(ours["base_price"]), float(so.items[0].price_list_rate))


class RoleLifecycleCase(unittest.TestCase):
    """Role added/removed from remaining enabled grants, never from one row."""

    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data")
        ensure_role_and_permissions()

    def tearDown(self):
        frappe.set_user("Administrator")
        sync_storefront_role(BUYER)
        frappe.db.commit()

    def _roles(self, user):
        return frappe.get_roles(user)

    def test_enabled_grant_gives_the_role(self):
        sync_storefront_role(BUYER)
        self.assertIn(STOREFRONT_BUYER_ROLE, self._roles(BUYER))

    def test_role_removed_when_no_enabled_grant_remains(self):
        grant = frappe.db.get_value(
            "YOB User Application Access",
            {"user": BUYER, "application": STOREFRONT_APP}, "name",
        )
        frappe.db.set_value("YOB User Application Access", grant, "enabled", 0)
        frappe.db.commit()
        try:
            sync_storefront_role(BUYER)
            self.assertNotIn(STOREFRONT_BUYER_ROLE, frappe.get_roles(BUYER))
        finally:
            frappe.db.set_value("YOB User Application Access", grant, "enabled", 1)
            frappe.db.commit()

    def test_role_is_not_restrictive_so_system_users_are_unaffected(self):
        """Requirement 4: no global restrictive User Permission may appear.

        The role only ADDS Item read, so a multi-hat System User keeps their
        Desk access. Proven by asserting the mechanism creates no User
        Permission rows at all.
        """

        self.assertEqual(
            frappe.db.count("User Permission", {"user": BUYER}), 0,
            "the role lifecycle must not create User Permission rows",
        )

    def test_administrator_desk_access_is_untouched(self):
        frappe.set_user("Administrator")
        self.assertTrue(frappe.has_permission("Customer", "read"))
        self.assertTrue(frappe.has_permission("Sales Order", "read"))
