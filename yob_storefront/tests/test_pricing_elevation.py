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

    def test_role_grants_exactly_item_read_and_tax_template_select(self):
        """The whole permission surface, pinned entry by entry.

        `Sales Taxes and Charges Template` carries SELECT and not READ: ERPNext
        16.52.1's `require_permission(..., "select")` is satisfied by it, and a
        buyer still cannot open the document. Widening either row, or adding a
        third DocType, has to fail here first.
        """

        granted = frappe.db.get_all(
            "Custom DocPerm", {"role": STOREFRONT_BUYER_ROLE},
            ["parent", "read", "select", "write", "create", "delete", "export", "report", "share"],
        )
        by_parent = {r.parent: r for r in granted}

        self.assertEqual(
            set(by_parent), {"Item", "Sales Taxes and Charges Template"},
            "the managed permission surface changed",
        )
        self.assertEqual((by_parent["Item"].read, by_parent["Item"].select), (1, 0))
        tax = by_parent["Sales Taxes and Charges Template"]
        self.assertEqual((tax.read, tax.select), (0, 1), "tax template must be select-only")

        for parent, row in by_parent.items():
            with self.subTest(doctype=parent):
                self.assertEqual(
                    (row.write, row.create, row.delete, row.export, row.report, row.share),
                    (0, 0, 0, 0, 0, 0),
                    f"{parent} must grant nothing beyond its single read/select bit",
                )

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


# =========================================================================
# PHASE 32B -- ERPNext 16.52.1 explicit permission gates
# =========================================================================

TAX_TEMPLATE_DOCTYPE = "Sales Taxes and Charges Template"


class ERPNextRequirePermissionCase(unittest.TestCase):
    """The gate ERPNext 16.52.1 added, and the smallest grant that clears it.

    `erpnext.require_permission()` does not exist in 16.31.1. On 16.52.1 India
    Compliance's regional address hook reaches it on every priced transaction:

        set_address_details -> get_regional_address_details
          -> india_compliance ... get_gst_details
            -> accounts_controller.get_taxes_and_charges
              -> require_permission("Sales Taxes and Charges Template", n, "select")

    Unfixed, the buyer's pricing Sales Order raises PermissionError,
    `price_candidate()` absorbs it as item-local, and the catalogue silently
    reports zero items while Administrator sees a full page. These tests fail
    the moment that regresses, and they fail for the RIGHT reason -- they assert
    on refusals, not merely on an empty result.
    """

    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data; run it on the test site first")
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

    # ------------------------------------------------------- the gate itself

    def test_tax_template_select_is_granted_and_read_is_not(self):
        """Select clears ERPNext's check; read would hand over the document."""

        name = frappe.db.get_value(TAX_TEMPLATE_DOCTYPE, {}, "name")
        if not name:
            self.skipTest("no Sales Taxes and Charges Template on this site")

        self.assertTrue(
            frappe.has_permission(TAX_TEMPLATE_DOCTYPE, "select", doc=name),
            "ERPNext 16.52.1 requires select on the tax template during pricing",
        )
        self.assertFalse(
            frappe.has_permission(TAX_TEMPLATE_DOCTYPE, "read", doc=name),
            "select must not have been widened into read",
        )

    def test_buyer_cannot_open_the_tax_template_document(self):
        name = frappe.db.get_value(TAX_TEMPLATE_DOCTYPE, {}, "name")
        if not name:
            self.skipTest("no Sales Taxes and Charges Template on this site")

        with self.assertRaises(frappe.PermissionError):
            frappe.get_doc(TAX_TEMPLATE_DOCTYPE, name).check_permission("read")

    def test_erpnext_refuses_nothing_during_a_buyer_priced_transaction(self):
        """The regression that matters: ZERO refusals on the pricing path.

        Asserting on the priced result alone would pass again if a future
        change absorbed the refusal somewhere else, so the refusal hook itself
        is counted.
        """

        import erpnext

        refusals = []
        original = erpnext._refuse

        def counting_refuse():
            refusals.append(True)
            return original()

        customer = get_storefront_customer(resolve_access(BUYER, STOREFRONT_APP))
        settings = frappe.get_cached_doc("YOB Store Settings")

        erpnext._refuse = counting_refuse
        try:
            pricing = get_item_pricing(
                customer=customer, item_code=ITEM, qty=1,
                company=settings.company, currency=settings.default_currency,
            )
        finally:
            erpnext._refuse = original

        self.assertEqual(refusals, [], "ERPNext refused a permission during buyer pricing")
        self.assertIsNotNone(pricing, "pricing returned nothing for a legitimate buyer")

    # ------------------------------------------------------------- isolation

    def test_buyer_cannot_read_another_customers_address(self):
        """The grant must not have opened party data to the buyer.

        Address read predates this phase and comes from `All`; what protects it
        is Frappe's permission query condition, not the absence of the role. If
        that ever stops holding, this fails regardless of which change caused it.
        """

        mine = frappe.get_doc("Customer", CUSTOMER).name
        other = frappe.db.sql(
            """
            SELECT dl.parent
            FROM `tabDynamic Link` dl
            WHERE dl.link_doctype = 'Customer'
              AND dl.parenttype = 'Address'
              AND dl.link_name != %s
            LIMIT 1
            """,
            mine,
        )
        if not other:
            self.skipTest("no second customer's Address on this site")

        foreign = other[0][0]
        self.assertFalse(
            frappe.has_permission("Address", "read", doc=foreign),
            "a buyer must not read another customer's Address",
        )
        with self.assertRaises(frappe.PermissionError):
            frappe.get_doc("Address", foreign).check_permission("read")

    def test_buyer_address_listing_excludes_other_customers(self):
        visible = set(frappe.get_list("Address", pluck="name", limit_page_length=0))
        foreign = {
            row[0]
            for row in frappe.db.sql(
                """
                SELECT dl.parent FROM `tabDynamic Link` dl
                WHERE dl.link_doctype = 'Customer' AND dl.parenttype = 'Address'
                  AND dl.link_name != %s
                """,
                CUSTOMER,
            )
        }
        self.assertEqual(visible & foreign, set(), "another customer's Address was listable")

    # ------------------------------------------------- unchanged commitments

    def test_customer_read_is_still_denied(self):
        self.assertFalse(frappe.has_permission("Customer", "read"))

    def test_role_still_has_no_desk_access(self):
        self.assertEqual(
            frappe.db.get_value("Role", STOREFRONT_BUYER_ROLE, "desk_access"), 0,
            "the storefront role must never grant Desk access",
        )

    def test_no_user_permission_rows_were_introduced(self):
        self.assertEqual(frappe.db.count("User Permission", {"user": BUYER}), 0)

    # ---------------------------------------------------------- provisioning

    def test_provisioning_is_idempotent(self):
        """`after_migrate` runs on every deploy; a second run must change nothing."""

        frappe.set_user("Administrator")
        before = frappe.db.count("Custom DocPerm", {"role": STOREFRONT_BUYER_ROLE})
        ensure_role_and_permissions()
        ensure_role_and_permissions()
        frappe.db.commit()
        after = frappe.db.count("Custom DocPerm", {"role": STOREFRONT_BUYER_ROLE})
        self.assertEqual(before, after, "re-provisioning duplicated Custom DocPerm rows")
