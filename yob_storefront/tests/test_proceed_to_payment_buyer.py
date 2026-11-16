# Copyright (c) 2026, YOB and Shayona
"""Proceed to Payment, executed as a real storefront buyer (Phase 32J).

THE DEFECT
----------
Clicking Proceed to Payment answered `PermissionError: Not permitted` for every
real buyer on a GST-registered company. The gate:

    checkout.proceed_to_payment
      -> trusted_execution()                     <- identity becomes the processor
         -> create_sales_order_from_cart
            -> party.py:304 set_address_details
               -> get_regional_address_details
                  -> india_compliance transaction.py:942 get_gst_details
                     -> accounts_controller.py:3648 get_taxes_and_charges
                        -> require_permission("Sales Taxes and Charges Template",
                                              <template>, "select")

`erpnext.require_permission()` is new in 16.52.1. Phase 32B cleared this exact
gate for the storefront BUYER -- but the commitment runs inside Phase 32C's
trusted boundary, so the user at that line is
`payment-processor@yob.internal`, and the buyer's grant does nothing for it.
Instrumenting the call showed both sides of the boundary in one trace:

    template select, user=storefront@yob.test            permitted=True
    template select, user=payment-processor@yob.internal  permitted=False

So the operation is correctly INSIDE trusted execution; the processor was simply
missing one narrow permission. The fix grants `select` -- not `read` -- on
`Sales Taxes and Charges Template` to `YOB Payment Processor`, mirroring what
Phase 32B gave the buyer, and nothing else.

WHY THE EXISTING SUITE MISSED IT -- TWO INDEPENDENT BLIND SPOTS
---------------------------------------------------------------
1. `test_payment_lifecycle`, the module that drives Proceed, calls
   `frappe.set_user("Administrator")` in setUp, and Administrator bypasses
   `has_permission` outright.
2. More fundamentally, the test site's company is `gst_category: Unregistered`
   with no GSTIN, so India Compliance short-circuits and NO tax template is ever
   resolved. The gate is unreachable there even for a non-Administrator. It is
   the same condition that makes two GST assertions in the suite skip rather
   than pass.

Either alone would have hidden this. So this module does BOTH: it registers the
company for GST inside the test transaction, and it runs as a real Website User.
`test_the_gst_fixture_actually_arms_the_gate` exists so the fixture cannot
silently stop arming it -- a green test that no longer reaches the gate would be
worse than no test.
"""

import inspect
import unittest
from unittest.mock import patch

import frappe

from yob_storefront.install import PAYMENT_PROCESSOR_ROLE, PAYMENT_PROCESSOR_USER
from yob_storefront.permissions.storefront_role import (
    STOREFRONT_BUYER_ROLE,
    ensure_role_and_permissions,
    sync_storefront_role,
)

ITEM = "YOB-BOLT-M10"
CUSTOMER = "YOB Demo Buyer"
BUYER = "storefront@yob.test"
CONTACT = "Demo Buyer-YOB Demo Buyer"
BILLING = "YOB Demo Billing-Billing"
SHIPPING = "YOB Demo Shipping-Shipping"

#: A structurally valid Gujarat GSTIN. India Compliance validates the format, so
#: this cannot be an arbitrary string; it is the same state code (24) as the
#: seeded customer addresses so the transaction resolves an IN-STATE template.
COMPANY_GSTIN = "24AEQPV5389F1ZS"

TAX_TEMPLATE_DOCTYPE = "Sales Taxes and Charges Template"

#: The out-of-state jurisdiction (Phase 33). Gujarat is 24, Maharashtra is 27,
#: so a customer address here makes the transaction inter-state and India
#: Compliance must resolve IGST instead of CGST+SGST. The address carries NO
#: GSTIN: an unregistered customer still pays IGST out of state, which is also
#: the Phase 33 finding that the ADDRESS STATE -- not the customer's
#: `gst_category` -- decides the rate.
OUT_OF_STATE = "Maharashtra"
OUT_OF_STATE_CITY = "Mumbai"
OUT_OF_STATE_PINCODE = "400001"


def _seeded() -> bool:
    return bool(frappe.db.exists("Customer", CUSTOMER) and frappe.db.exists("Item", ITEM))


class ProceedFixture:
    """The shared, GST-armed, real-buyer fixture.

    Extracted unchanged when Phase 33 added the out-of-state case below: both
    jurisdictions need the same company registration, the same throwaway commit
    patch and the same real Website User, and two copies of that would be two
    chances to arm only one of them. It is a plain mixin, not a TestCase, so it
    contributes no tests of its own.
    """

    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data on the test site")
        ensure_role_and_permissions()
        sync_storefront_role(BUYER)
        frappe.db.commit()
        frappe.clear_cache()

    def setUp(self):
        frappe.set_user("Administrator")

        # Commit is recorded rather than executed for this whole class: the
        # payment path contains a deliberate durability commit before any
        # provider call, and executing it would end the transaction and leak a
        # Sales Order, a Cart and a Payment Request into the test site.
        self.commits = []
        commit_patch = patch.object(
            frappe.db, "commit", side_effect=lambda *a, **k: self.commits.append(True))
        commit_patch.start()
        self.addCleanup(commit_patch.stop)

        self.customer = frappe.get_doc("Customer", CUSTOMER)
        self.company = frappe.get_single("YOB Store Settings").company
        self.register_company_for_gst()

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.local.request = None
        frappe.db.rollback()
        frappe.clear_cache()

    # --------------------------------------------------------------- fixture

    def register_company_for_gst(self):
        """Make the company GST-registered, which is what arms the gate.

        Rolled back in tearDown. India Compliance refuses to register a company
        that has no address carrying a GSTIN ("Please set Company Address Name
        to ensure Company GST"), so the address comes first.
        """

        frappe.db.delete("Dynamic Link", {"link_doctype": "Company",
                                          "link_name": self.company})
        frappe.get_doc({
            "doctype": "Address", "address_title": "_32J Company GST",
            "address_type": "Billing", "address_line1": "1 Company Road",
            "city": "Ahmedabad", "state": "Gujarat", "pincode": "380001",
            "country": "India", "gstin": COMPANY_GSTIN,
            "gst_category": "Registered Regular", "is_primary_address": 1,
            "links": [{"link_doctype": "Company", "link_name": self.company}],
        }).insert(ignore_permissions=True)

        frappe.db.set_value("Company", self.company, {
            "gstin": COMPANY_GSTIN, "gst_category": "Registered Regular"})
        frappe.clear_cache()

    def make_cart(self, qty=3):
        """A checkout-ready Cart, built through the real pricing path."""

        from yob_storefront.api.cart import get_or_create_cart
        from yob_storefront.services.cart_service import reprice_cart

        cart = get_or_create_cart(self.customer)
        cart.set("items", [])
        cart.append("items", {"item_code": ITEM, "quantity": qty})
        cart.contact_person = CONTACT
        cart.billing_address = BILLING
        cart.shipping_address = SHIPPING
        reprice_cart(cart, self.customer)
        cart.save(ignore_permissions=True)
        return cart

    # ----------------------------------------------------------- as a buyer

    def as_buyer(self):
        from werkzeug.test import EnvironBuilder
        from werkzeug.wrappers import Request

        frappe.local.request = Request(
            EnvironBuilder(headers={"X-YOB-Original-Host": "storefront.test"}).get_environ())
        frappe.set_user(BUYER)

    def proceed(self):
        """The exact endpoint the Proceed button calls."""

        from yob_storefront.api import checkout

        with patch.object(checkout, "get_storefront_customer", return_value=self.customer):
            return inspect.unwrap(checkout.proceed_to_payment)(auth_context={})

    def assert_still_unprivileged(self):
        """A working Proceed must not have cost the buyer any ERP access."""

        for doctype, ptype in (("Sales Order", "read"), ("Sales Order", "write"),
                               ("Payment Request", "read"), ("Payment Request", "write"),
                               ("Payment Request", "create"),
                               ("Customer", "read"), ("Customer", "select"),
                               ("Company", "read"), ("Company", "select"),
                               ("Account", "read")):
            with self.subTest(permission=f"{doctype}.{ptype}"):
                self.assertFalse(frappe.has_permission(doctype, ptype),
                                 f"the buyer gained {doctype} {ptype}")

        self.assertIn(STOREFRONT_BUYER_ROLE, frappe.get_roles())
        self.assertEqual(frappe.db.get_value("User", BUYER, "user_type"), "Website User")


class ProceedToPaymentBuyerCase(ProceedFixture, unittest.TestCase):
    """The Proceed endpoint, as a Website User, on a GST-registered company.

    IN-STATE: company and customer are both in Gujarat, so India Compliance
    resolves the in-state template and CGST+SGST. The out-of-state counterpart
    is `ProceedToPaymentOutOfStateCase` below.
    """

    # ------------------------------------------------------------- the guard

    def test_the_gst_fixture_actually_arms_the_gate(self):
        """Without this, every assertion below could pass vacuously.

        The whole suite missed this defect partly because an unregistered
        company makes the tax-template path unreachable. If this fixture ever
        stops registering the company, the tests below stop testing anything --
        so the arming is asserted, not assumed.
        """

        from yob_storefront.services.pricing_service import (
            calculate_cart_using_sales_order,
        )

        self.assertEqual(
            frappe.db.get_value("Company", self.company, "gst_category"),
            "Registered Regular")
        self.assertTrue(frappe.db.get_value("Company", self.company, "gstin"))

        # Asked of the PRODUCTION pricing path rather than by calling India
        # Compliance directly: `get_gst_details` needs the company GSTIN,
        # company address and billing GSTIN that `set_address_details` fills in
        # first, so a hand-built argument dict resolves no template and would
        # make this guard lie about the fixture.
        cart = self.make_cart()
        priced = calculate_cart_using_sales_order(cart, self.customer)

        self.assertTrue(priced.taxes_and_charges,
                        "no tax template resolved on a priced order, so "
                        "require_permission is never reached and the tests below "
                        "prove nothing")
        self.assertTrue(frappe.db.exists(TAX_TEMPLATE_DOCTYPE,
                                         priced.taxes_and_charges))
        self.assertTrue(priced.taxes, "the priced order carries no tax rows")

    # ---------------------------------------------------------- the fix

    def test_the_processor_holds_select_on_the_tax_template(self):
        """The narrow grant, and that it is `select` rather than `read`."""

        frappe.set_user(PAYMENT_PROCESSOR_USER)
        try:
            self.assertTrue(frappe.has_permission(TAX_TEMPLATE_DOCTYPE, "select"),
                            "the processor cannot resolve the tax template; Proceed "
                            "to Payment will answer PermissionError")
            self.assertFalse(frappe.has_permission(TAX_TEMPLATE_DOCTYPE, "read"),
                             "the processor gained read on the tax master; `select` "
                             "is all the ERPNext gate asks for")
        finally:
            frappe.set_user("Administrator")

    def test_the_grant_did_not_strip_the_standard_roles(self):
        """A Custom DocPerm REPLACES a DocType's whole permission set.

        `setup_custom_perms` must copy the standard rows first, or this grant
        silently removes Accounts Manager and friends from the tax master.
        """

        roles = set(frappe.get_all("Custom DocPerm",
                                   filters={"parent": TAX_TEMPLATE_DOCTYPE},
                                   pluck="role"))

        self.assertIn(PAYMENT_PROCESSOR_ROLE, roles)
        for standard in ("Accounts Manager", "Accounts User", "Sales User",
                         "Sales Manager", "Stock User"):
            with self.subTest(role=standard):
                self.assertIn(standard, roles)

    def test_the_processor_gained_nothing_else(self):
        """The identity stays as narrow as Phase 32C left it."""

        frappe.set_user(PAYMENT_PROCESSOR_USER)
        try:
            for doctype in ("Sales Order", "Customer", "Payment Request"):
                with self.subTest(doctype=doctype):
                    self.assertFalse(frappe.has_permission(doctype, "read"))
                    self.assertFalse(frappe.has_permission(doctype, "write"))
        finally:
            frappe.set_user("Administrator")

        self.assertEqual(frappe.db.get_value("User", PAYMENT_PROCESSOR_USER, "enabled"), 0,
                         "the processor identity became able to authenticate")

    # ------------------------------------------------------- THE REGRESSION

    def test_a_real_buyer_can_proceed_to_payment(self):
        """THE regression. This answered PermissionError before Phase 32J."""

        cart = self.make_cart()
        self.as_buyer()

        response = self.proceed()

        self.assertNotIn("errors", response, f"Proceed failed: {response}")
        data = response["data"]
        self.assertTrue(data["payment_request"])
        self.assertTrue(data["token"])
        self.assertEqual(data["payment_url"], f"/payment/{data['token']}")
        self.assert_still_unprivileged()
        del cart

    def test_the_attempt_matches_the_phase_32c_shape(self):
        """Draft Sales Order, PR referencing it, Cart recorded on the PR."""

        cart = self.make_cart()
        self.as_buyer()

        data = self.proceed()["data"]

        frappe.set_user("Administrator")
        pr = frappe.get_doc("Payment Request", data["payment_request"])

        self.assertEqual(pr.reference_doctype, "Sales Order",
                         "the Payment Request still references the Cart")
        self.assertTrue(pr.reference_name)
        self.assertEqual(pr.get("custom_cart"), cart.name)
        self.assertIsNone(pr.get("custom_committed_at"),
                          "the attempt is committed before any provider call")
        self.assertTrue(pr.get("custom_checkout_token"))
        self.assertTrue(pr.get("custom_checkout_expiry"))
        self.assertTrue(pr.get("custom_source_fingerprint"))

        so = frappe.get_doc("Sales Order", pr.reference_name)
        self.assertEqual(so.docstatus, 0, "the attempt Sales Order is not Draft")
        self.assertEqual(so.customer, CUSTOMER)
        self.assertEqual(so.disable_rounded_total, 1)

    def test_the_attempt_carries_the_gst_taxes_that_triggered_the_gate(self):
        """Proof the gate was cleared rather than side-stepped.

        If the template had not resolved, the order would simply carry no tax and
        the PermissionError would never have occurred -- so the presence of GST
        rows is what distinguishes a real fix from a disarmed test.
        """

        self.make_cart()
        self.as_buyer()

        data = self.proceed()["data"]

        frappe.set_user("Administrator")
        pr = frappe.get_doc("Payment Request", data["payment_request"])
        so = frappe.get_doc("Sales Order", pr.reference_name)

        self.assertTrue(so.taxes_and_charges,
                        "no tax template reached the attempt Sales Order")
        self.assertTrue(so.taxes, "the attempt carries no tax rows")
        self.assertGreater(sum(t.tax_amount for t in so.taxes), 0)

    def test_amounts_match_across_cart_sales_order_and_payment_request(self):
        cart = self.make_cart()
        self.as_buyer()

        data = self.proceed()["data"]

        frappe.set_user("Administrator")
        pr = frappe.get_doc("Payment Request", data["payment_request"])
        so = frappe.get_doc("Sales Order", pr.reference_name)
        cart.reload()

        self.assertEqual(cart.grand_total, so.grand_total)
        self.assertEqual(so.grand_total, pr.grand_total)
        self.assertEqual(pr.currency, cart.currency)

    def test_the_processor_gets_a_document_specific_share_only(self):
        """Read-only DocShare on that one attempt, never a blanket grant."""

        self.make_cart()
        self.as_buyer()

        data = self.proceed()["data"]

        frappe.set_user("Administrator")
        pr = frappe.get_doc("Payment Request", data["payment_request"])
        shares = frappe.get_all(
            "DocShare",
            filters={"share_doctype": "Sales Order", "share_name": pr.reference_name},
            fields=["user", "read", "write", "share", "submit", "everyone"])

        self.assertEqual(len(shares), 1, f"expected one share, got {shares}")
        share = shares[0]
        self.assertEqual(share["user"], PAYMENT_PROCESSOR_USER)
        self.assertEqual(share["read"], 1)
        for field in ("write", "share", "submit", "everyone"):
            with self.subTest(field=field):
                self.assertEqual(share[field], 0)

    # ------------------------------------------------------- idempotency

    def test_repeating_proceed_unchanged_reuses_the_attempt(self):
        """One Proceed, one attempt. Clicking twice must not create a second."""

        self.make_cart()
        self.as_buyer()

        first = self.proceed()["data"]
        second = self.proceed()["data"]

        self.assertEqual(first["payment_request"], second["payment_request"],
                         "a second Payment Request was created")

        frappe.set_user("Administrator")
        pr = frappe.get_doc("Payment Request", first["payment_request"])
        self.assertEqual(
            frappe.db.count("Sales Order", {"name": pr.reference_name}), 1)

    def test_a_changed_cart_produces_a_replacement_attempt(self):
        """A different cart is a different obligation, per Phase 32C."""

        cart = self.make_cart(qty=3)
        self.as_buyer()
        first = self.proceed()["data"]

        frappe.set_user("Administrator")
        cart.reload()
        cart.items[0].quantity = 9
        from yob_storefront.services.cart_service import reprice_cart
        reprice_cart(cart, self.customer)
        cart.save(ignore_permissions=True)

        self.as_buyer()
        second = self.proceed()["data"]

        self.assertNotEqual(first["payment_request"], second["payment_request"],
                            "a repriced cart reused a stale attempt")

        frappe.set_user("Administrator")
        old = frappe.get_doc("Payment Request", first["payment_request"])
        new = frappe.get_doc("Payment Request", second["payment_request"])
        self.assertTrue(frappe.db.exists("Payment Request", old.name),
                        "the superseded attempt was deleted rather than retained")
        self.assertNotEqual(old.reference_name, new.reference_name)
        self.assertGreater(new.grand_total, old.grand_total)


class ProceedToPaymentOutOfStateCase(ProceedFixture, unittest.TestCase):
    """The same real Proceed, inter-state: IGST instead of CGST+SGST.

    WHY THIS EXISTS
    ---------------
    Everything the in-state case proves about the permission gate it proves with
    ONE tax treatment. The Phase 33 audit recorded out-of-state GST as reachable
    only by hand, and it is a different code path in India Compliance: a
    different template, a different account, and a `place_of_supply` derived
    from the customer's address rather than the company's.

    It is also the case that distinguishes two explanations of the in-state
    result. If the storefront were somehow pinning a template, both
    jurisdictions would answer the same way; they do not, and the only thing
    this fixture changes is the customer's address state.

    The company fixture is identical to the in-state case -- same Gujarat
    registration, same GSTIN -- and is rolled back with everything else.
    """

    def setUp(self):
        super().setUp()
        self.addresses = self.customer_addresses_out_of_state()

    # --------------------------------------------------------------- fixture

    def customer_addresses_out_of_state(self):
        """A billing and a shipping address for the customer, in Maharashtra.

        New addresses rather than edits to the seeded Gujarat ones: an Address
        is shared with the Cart, the Contact and the order history, and
        rewriting the demo data's jurisdiction in place would change what every
        other module in the suite is looking at.
        """

        created = {}
        for kind in ("Billing", "Shipping"):
            doc = frappe.get_doc({
                "doctype": "Address", "address_title": f"_33 OOS {kind}",
                "address_type": kind, "address_line1": "9 Marine Drive",
                "city": OUT_OF_STATE_CITY, "state": OUT_OF_STATE,
                "pincode": OUT_OF_STATE_PINCODE, "country": "India",
                # Unregistered and GSTIN-less on purpose; see OUT_OF_STATE.
                "gst_category": "Unregistered",
                "links": [{"link_doctype": "Customer", "link_name": CUSTOMER}],
            }).insert(ignore_permissions=True)
            created[kind] = doc.name
        frappe.clear_cache()
        return created

    def make_cart(self, qty=3):
        """The parent cart, pointed at the out-of-state addresses."""

        from yob_storefront.api.cart import get_or_create_cart
        from yob_storefront.services.cart_service import reprice_cart

        cart = get_or_create_cart(self.customer)
        cart.set("items", [])
        cart.append("items", {"item_code": ITEM, "quantity": qty})
        cart.contact_person = CONTACT
        cart.billing_address = self.addresses["Billing"]
        cart.shipping_address = self.addresses["Shipping"]
        reprice_cart(cart, self.customer)
        cart.save(ignore_permissions=True)
        return cart

    # ----------------------------------------------------------- the guard

    def test_the_fixture_really_produces_an_inter_state_transaction(self):
        """Without this, every assertion below could be passing in-state."""

        from yob_storefront.services.pricing_service import (
            calculate_cart_using_sales_order,
        )

        self.assertEqual(
            frappe.db.get_value("Company", self.company, "gst_category"),
            "Registered Regular")

        cart = self.make_cart()
        priced = calculate_cart_using_sales_order(cart, self.customer)

        self.assertTrue(priced.taxes_and_charges, "no tax template resolved")
        self.assertIn("Out-state", priced.taxes_and_charges,
                      f"an in-state template resolved: {priced.taxes_and_charges}")
        self.assertTrue(priced.taxes, "the priced order carries no tax rows")

    # ------------------------------------------------------- THE REGRESSION

    def test_a_real_buyer_can_proceed_out_of_state(self):
        self.make_cart()
        self.as_buyer()

        response = self.proceed()

        self.assertNotIn("errors", response, f"Proceed failed: {response}")
        data = response["data"]
        self.assertTrue(data["payment_request"])
        self.assertTrue(data["token"])
        self.assert_still_unprivileged()

    def test_the_attempt_selected_the_out_of_state_template(self):
        self.make_cart()
        self.as_buyer()

        data = self.proceed()["data"]

        frappe.set_user("Administrator")
        pr = frappe.get_doc("Payment Request", data["payment_request"])
        so = frappe.get_doc("Sales Order", pr.reference_name)

        self.assertTrue(so.taxes_and_charges, "no tax template reached the attempt")
        self.assertIn("Out-state", so.taxes_and_charges)
        self.assertNotIn("In-state", so.taxes_and_charges)
        self.assertTrue(frappe.db.exists(TAX_TEMPLATE_DOCTYPE, so.taxes_and_charges))

    def test_the_attempt_carries_igst_and_neither_cgst_nor_sgst(self):
        self.make_cart()
        self.as_buyer()

        data = self.proceed()["data"]

        frappe.set_user("Administrator")
        pr = frappe.get_doc("Payment Request", data["payment_request"])
        so = frappe.get_doc("Sales Order", pr.reference_name)

        self.assertTrue(so.taxes, "the attempt carries no tax rows")
        types = {(row.get("gst_tax_type") or "").lower() for row in so.taxes}

        self.assertIn("igst", types, f"no IGST row on an inter-state order: {types}")
        self.assertNotIn("cgst", types, "CGST was charged on an inter-state order")
        self.assertNotIn("sgst", types, "SGST was charged on an inter-state order")
        self.assertGreater(sum(row.tax_amount for row in so.taxes), 0)

    def test_the_place_of_supply_follows_the_customer_address(self):
        """The Phase 33 finding, asserted: the ADDRESS STATE decides the rate."""

        self.make_cart()
        self.as_buyer()

        data = self.proceed()["data"]

        frappe.set_user("Administrator")
        pr = frappe.get_doc("Payment Request", data["payment_request"])
        so = frappe.get_doc("Sales Order", pr.reference_name)

        self.assertIn(OUT_OF_STATE, so.get("place_of_supply") or "",
                      f"place_of_supply is {so.get('place_of_supply')!r}, not the "
                      "customer's own state")

    def test_amounts_match_across_cart_sales_order_and_payment_request(self):
        """Parity must hold under IGST exactly as it does in-state."""

        cart = self.make_cart()
        self.as_buyer()

        data = self.proceed()["data"]

        frappe.set_user("Administrator")
        pr = frappe.get_doc("Payment Request", data["payment_request"])
        so = frappe.get_doc("Sales Order", pr.reference_name)
        cart.reload()

        self.assertEqual(cart.grand_total, so.grand_total)
        self.assertEqual(so.grand_total, pr.grand_total)
        self.assertEqual(pr.currency, cart.currency)

    def test_the_processor_permission_boundary_still_holds(self):
        """The inter-state template is a different document; `select` must cover it."""

        self.make_cart()
        self.as_buyer()

        data = self.proceed()["data"]

        frappe.set_user("Administrator")
        pr = frappe.get_doc("Payment Request", data["payment_request"])

        frappe.set_user(PAYMENT_PROCESSOR_USER)
        try:
            self.assertTrue(frappe.has_permission(TAX_TEMPLATE_DOCTYPE, "select"),
                            "the processor cannot resolve the inter-state template")
            self.assertFalse(frappe.has_permission(TAX_TEMPLATE_DOCTYPE, "read"),
                             "`select` is all the ERPNext gate asks for")

            # NOT `Sales Order`: the processor holds a document-specific DocShare
            # on the order it just committed, and Frappe answers doctype-level
            # `read` true for a user who has any shared document of that type.
            # The real boundary is that the share is one read-only row for this
            # one order, asserted below.
            for doctype in ("Customer", "Payment Request"):
                with self.subTest(doctype=doctype):
                    self.assertFalse(frappe.has_permission(doctype, "read"))
        finally:
            frappe.set_user("Administrator")

        shares = frappe.get_all(
            "DocShare",
            filters={"share_doctype": "Sales Order",
                     "share_name": pr.reference_name},
            fields=["user", "read", "write", "share", "submit", "everyone"])

        self.assertEqual(len(shares), 1, f"expected one share, got {shares}")
        self.assertEqual(shares[0]["user"], PAYMENT_PROCESSOR_USER)
        self.assertEqual(shares[0]["read"], 1)
        for field in ("write", "share", "submit", "everyone"):
            with self.subTest(field=field):
                self.assertEqual(shares[0][field], 0)


if __name__ == "__main__":
    unittest.main()
