# Copyright (c) 2026, YOB and Shayona
"""Address changes and pricing freshness before commitment (Phase 23B-5W).

THE QUESTION
------------
`set_cart_billing_address` and `set_cart_shipping_address` save the link and do
NOT reprice. Billing/shipping jurisdiction can decide the tax template, so the
question is whether stale financials can reach a financial commitment.

THE ANSWER THESE TESTS PIN
--------------------------
No, and by two independent mechanisms rather than by the setter:

1. `proceed_to_payment` reprices under the Cart row lock and issues the Payment
   Request against THAT state, so the obligation is created from the addresses
   currently on the cart -- never from whatever totals happened to be stored.

2. `ensure_payment_request_committed` re-reads and RE-PRICES the cart in memory
   (`validate_payment_request_source_current`) and compares a fingerprint that
   includes `billing_address` and `shipping_address` as well as the money. An
   address changed after issuance therefore answers `payment_request_stale`, and
   commitment is refused rather than performed on the old numbers.

So the setter's silence is not the gap it looks like: the authoritative reprice
happens at the two moments that matter, and the buyer's own view refreshes on
the next `get_cart`.

THE FIXTURE
-----------
A `Tax Rule` keyed on `billing_state` makes an address change a MONEY change:
a Gujarat address matches no rule (no tax), a Maharashtra one resolves the
site's 18% out-of-state template. That is ERPNext deciding tax from
jurisdiction, which is the real-world case this phase is about.
"""

import inspect
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import flt

from yob_storefront.services.commitment_service import ensure_payment_request_committed
from yob_storefront.tests.test_payment_lifecycle import (
    BILLING,
    CUSTOMER,
    LifecycleCase,
    _error_code,
    _raw,
)

COMPANY = "Shayona Technology"
OUT_OF_STATE_TEMPLATE = "Output GST Out-state - ST"


class AddressFreshnessCase(LifecycleCase):
    """Shared fixtures: a second jurisdiction, and a state-driven tax rule."""

    def setUp(self):
        super().setUp()
        from yob_storefront.api import cart as cart_api

        self.cart_api = cart_api

    # ----------------------------------------------------------- fixtures

    #: India Compliance validates that a pincode belongs to its state, so the
    #: fixture carries a real one per jurisdiction rather than reusing Gujarat's.
    PINCODES = {"Gujarat": "382445", "Maharashtra": "400001"}

    def make_address(self, title, state, address_type="Billing", city="Ahmedabad"):
        return frappe.get_doc({
            "doctype": "Address", "address_title": title, "address_type": address_type,
            "address_line1": f"1 {title} Road", "city": city, "state": state,
            "country": "India", "pincode": self.PINCODES[state],
            "links": [{"link_doctype": "Customer", "link_name": CUSTOMER}],
        }).insert(ignore_permissions=True).name

    def taxed_state_rule(self, state="Maharashtra"):
        """ERPNext resolves 18% for this state and nothing for any other."""

        if not frappe.db.exists("Sales Taxes and Charges Template", OUT_OF_STATE_TEMPLATE):
            self.skipTest(f"{OUT_OF_STATE_TEMPLATE} is not configured on this bench")

        # Deliberately UNDATED. `_get_party_details` passes `posting_date`, which a
        # Sales Order does not have (it carries `transaction_date`), and
        # `get_tax_template` then requires both rule dates to be NULL. A dated rule
        # silently never matches server-side -- an ERPNext detail worth knowing, and
        # the reason this fixture looks bare.
        frappe.get_doc({
            "doctype": "Tax Rule", "tax_type": "Sales",
            "sales_tax_template": OUT_OF_STATE_TEMPLATE,
            "billing_state": state, "company": COMPANY,
        }).insert(ignore_permissions=True)

    # ----------------------------------------------------------- the paths

    def set_billing(self, address):
        """The REAL endpoint, with identity resolution stubbed."""

        with patch.object(self.cart_api, "get_storefront_customer", return_value=self.customer):
            return _raw(self.cart_api.set_cart_billing_address)(
                auth_context={}, billing_address=address)

    def set_shipping(self, address):
        with patch.object(self.cart_api, "get_storefront_customer", return_value=self.customer):
            return _raw(self.cart_api.set_cart_shipping_address)(
                auth_context={}, shipping_address=address)

    def get_cart(self):
        with patch.object(self.cart_api, "get_storefront_customer", return_value=self.customer):
            return _raw(self.cart_api.get_cart)(auth_context={})

    def stored(self, cart, *fields):
        return frappe.db.get_value("Cart", cart.name, list(fields), as_dict=True)


# =========================================================
# 1. WHAT THE SETTER DOES, AND DOES NOT, DO
# =========================================================

class AddressSetterCase(AddressFreshnessCase):

    def test_setter_saves_the_address_and_leaves_the_price_alone(self):
        """Documented, not accidental: the setter is an acknowledgement.

        It must not half-price the cart either -- saving a new jurisdiction and
        leaving the OLD tax on the row would be worse than leaving both alone,
        because the stored numbers would then look freshly calculated.
        """

        cart = self.make_cart()
        self.taxed_state_rule()
        elsewhere = self.make_address("_W5W MH Billing", "Maharashtra", city="Mumbai")

        before = self.stored(cart, "grand_total", "tax_total", "modified")

        response = self.set_billing(elsewhere)
        self.assertIsNone(_error_code(response), response)

        after = self.stored(cart, "grand_total", "billing_address", "tax_total")

        self.assertEqual(after.billing_address, elsewhere)
        self.assertEqual(flt(after.grand_total, 2), flt(before.grand_total, 2))
        self.assertEqual(flt(after.tax_total, 2), flt(before.tax_total, 2))
        self.assertEqual(set(response["data"]), {"billing_address", "shipping_address"},
                         "the setter response shape changed; the frontend contract "
                         "says callers must re-read the cart")

    def test_get_cart_reprices_the_new_jurisdiction(self):
        """The buyer's own view refreshes on the very next read."""

        cart = self.make_cart()
        untaxed = self.stored(cart, "grand_total", "net_total", "tax_total")
        self.assertEqual(flt(untaxed.tax_total, 2), 0.0,
                         "fixture assumes no tax applies to the seeded address")

        self.taxed_state_rule()
        self.set_billing(self.make_address("_W5W MH Billing", "Maharashtra", city="Mumbai"))

        response = self.get_cart()
        self.assertIsNone(_error_code(response), response)

        data = response["data"]["cart"]
        after = self.stored(cart, "grand_total", "tax_total")

        self.assertGreater(flt(after.tax_total, 2), 0.0,
                           "the new jurisdiction's tax never reached the cart")
        self.assertEqual(flt(data["grand_total"], 2), flt(after.grand_total, 2))
        self.assertEqual(flt(after.grand_total, 2),
                         flt(untaxed.net_total, 2) + flt(after.tax_total, 2))


# =========================================================
# 2. ISSUANCE PRICES THE CURRENT ADDRESS
# =========================================================

class ProceedFreshnessCase(AddressFreshnessCase):

    def test_proceed_prices_the_address_now_on_the_cart(self):
        """Even when the STORED totals were left behind by the setter."""

        cart = self.make_cart()
        self.taxed_state_rule()
        self.set_billing(self.make_address("_W5W MH Billing", "Maharashtra", city="Mumbai"))

        stale = self.stored(cart, "grand_total").grand_total

        data = self.assert_created(self.proceed())

        fresh = self.stored(cart, "grand_total", "tax_total")
        pr = self.pr_row(data["payment_request"], "grand_total", "currency")

        self.assertGreater(flt(fresh.tax_total, 2), 0.0,
                           "proceed did not reprice the new jurisdiction")
        self.assertNotEqual(flt(pr.grand_total, 2), flt(stale, 2),
                            "the obligation was issued for the pre-address total")
        self.assertEqual(flt(pr.grand_total, 2), flt(fresh.grand_total, 2))

    def test_proceed_does_not_trust_stored_totals(self):
        """A corrupted stored total must not survive issuance.

        The reprice under the lock is authoritative, so the Payment Request is
        created from a recalculation rather than from whatever the row said.
        """

        cart = self.make_cart()
        correct = self.stored(cart, "grand_total").grand_total

        frappe.db.set_value("Cart", cart.name, "grand_total", 1.0, update_modified=False)
        frappe.clear_document_cache("Cart", cart.name)

        data = self.assert_created(self.proceed())
        pr = self.pr_row(data["payment_request"], "grand_total")

        self.assertEqual(flt(pr.grand_total, 2), flt(correct, 2))
        self.assertEqual(flt(self.stored(cart, "grand_total").grand_total, 2), flt(correct, 2))


# =========================================================
# 3. A CHANGE AFTER ISSUANCE CANNOT COMMIT
# =========================================================

class StaleAfterIssuanceCase(AddressFreshnessCase):

    def commit(self, token):
        return ensure_payment_request_committed(token=token)

    def test_jurisdiction_change_after_issuance_refuses_commitment(self):
        cart = self.make_cart()
        data = self.assert_created(self.proceed())

        self.taxed_state_rule()
        self.set_billing(self.make_address("_W5W MH Billing", "Maharashtra", city="Mumbai"))

        before = frappe.db.count("Sales Order")
        result = self.commit(data["token"])

        self.assertEqual(_error_code(result), "payment_request_stale", result)
        self.assertEqual(frappe.db.count("Sales Order"), before,
                         "a Sales Order was committed for a superseded obligation")
        self.assertEqual(self.stored(cart, "status").status, "Draft")
        self.assertEqual(
            frappe.db.get_value("Payment Request", data["payment_request"], "reference_doctype"),
            "Cart", "the obligation moved on despite being stale")

    def test_address_change_with_no_money_change_is_still_stale(self):
        """The address IS part of the obligation, not merely its price.

        A different delivery address is a different order even when the total is
        identical, so the fingerprint covers address identity as well as money.
        """

        cart = self.make_cart()
        data = self.assert_created(self.proceed())
        before = self.stored(cart, "grand_total").grand_total

        same_state = self.make_address("_W5W GJ Shipping", "Gujarat",
                                       address_type="Shipping")
        self.assertIsNone(_error_code(self.set_shipping(same_state)))

        result = self.commit(data["token"])
        after = self.stored(cart, "grand_total").grand_total

        self.assertEqual(flt(after, 2), flt(before, 2), "the fixture changed the money")
        self.assertEqual(_error_code(result), "payment_request_stale", result)

    def test_reissuing_after_the_change_commits_the_new_obligation(self):
        """The buyer is not stuck: Proceed re-prices and re-issues."""

        self.make_cart()
        first = self.assert_created(self.proceed())

        self.taxed_state_rule()
        self.set_billing(self.make_address("_W5W MH Billing", "Maharashtra", city="Mumbai"))

        second = self.assert_created(self.proceed())
        result = self.commit(second["token"])

        self.assertIsNone(_error_code(result), result)
        self.assertTrue(result["created"])
        self.assertNotEqual(first["token"], second["token"],
                            "the superseded credential still works")

        so = result["sales_order"]
        pr = self.pr_row(second["payment_request"], "grand_total")

        self.assertEqual(flt(so.grand_total, 2), flt(pr.grand_total, 2))
        self.assertGreater(flt(so.total_taxes_and_charges or 0, 2), 0.0,
                           "the committed order lost the new jurisdiction's tax")


# =========================================================
# 4. STRUCTURE: NO COMMITMENT PATH SKIPS THE CHECK
# =========================================================

class CommitmentStructureCase(unittest.TestCase):
    """The guarantee must not depend on the caller remembering to ask."""

    def test_commitment_revalidates_the_source_before_creating_an_order(self):
        from yob_storefront.services import commitment_service

        source = inspect.getsource(commitment_service.ensure_payment_request_committed)

        self.assertIn("validate_payment_request_source_current", source)
        self.assertIn("_commit_cart", source)
        self.assertLess(source.index("validate_payment_request_source_current"),
                        source.index("_commit_cart(pr"),
                        "the cart is committed before it is revalidated")

    def test_the_cart_is_committed_from_exactly_one_place(self):
        from yob_storefront.services import commitment_service

        module_source = inspect.getsource(commitment_service)

        self.assertEqual(module_source.count("create_sales_order_from_cart("), 1,
                         "a second Cart -> Sales Order call appeared; it would not "
                         "be covered by the staleness check above")

    def test_the_staleness_check_reprices_rather_than_reading_stored_totals(self):
        from yob_storefront.services import payment_request_service

        source = inspect.getsource(
            payment_request_service.validate_payment_request_source_current)

        self.assertIn("reprice_cart(cart, customer)", source,
                      "the source check compares stored numbers instead of "
                      "recalculating them")

    def test_address_fields_are_part_of_the_payment_fingerprint(self):
        from yob_storefront.services.payment_source import cart_payment_snapshot

        snapshot = cart_payment_snapshot(frappe.get_doc({
            "doctype": "Cart", "customer": CUSTOMER, "company": COMPANY,
            "currency": "INR", "billing_address": BILLING, "items": []}))

        for field in ("billing_address", "shipping_address", "contact_person",
                      "tax_total", "grand_total"):
            self.assertIn(field, snapshot,
                          f"`{field}` left the fingerprint; an address change "
                          f"would stop invalidating a live payment link")


if __name__ == "__main__":
    unittest.main()
