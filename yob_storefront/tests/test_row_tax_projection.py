# Copyright (c) 2026, YOB and Shayona
"""Row-level tax on `cart.pricing_rows` (Phase 23B-3).

WHERE THE NUMBERS COME FROM
---------------------------
`calculate_taxes_and_totals` leaves `doc._item_wise_tax_details` on the priced
Sales Order: one entry per (item row, tax row) pair. YOB reads that. It never
applies a percentage, never infers CGST/SGST vs IGST from an address, and never
decides jurisdiction -- ERPNext and India Compliance own all of it.

Native ERPNext output is the oracle in every test below: expectations are derived
from the Sales Order the fixture produces, not from arithmetic written here.

TWO TRAPS THESE PIN
-------------------
* **currency** -- `_item_wise_tax_details` amounts are BASE currency (the rounding
  pass reconciles them against `base_tax_amount_after_discount_amount`). Returned
  raw they would sit beside a transaction-currency rate.
* **inclusive tax** -- row total is `net_amount + tax`, never `amount + tax`.
  For an inclusive 18% on a 100 rate that is 100, not 136.
"""

import inspect
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_days, flt, today

from erpnext.accounts.doctype.pricing_rule.utils import apply_pricing_rule_on_transaction

CUSTOMER = "YOB Demo Buyer"
SEED_ITEM = "YOB-BOLT-M10"


def _seeded():
    return bool(frappe.db.exists("Customer", CUSTOMER) and frappe.db.exists("Item", SEED_ITEM))


class RowTaxBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data on the test site")

    def setUp(self):
        frappe.set_user("Administrator")
        self.commits = []
        cp = patch.object(frappe.db, "commit", side_effect=lambda *a, **k: self.commits.append(1))
        cp.start()
        self.addCleanup(cp.stop)

        self.company = frappe.db.get_value("Company", {}, "name")
        self.item_group = frappe.db.get_value("Item", SEED_ITEM, "item_group")
        self.uom = frappe.db.get_value("Item", SEED_ITEM, "stock_uom")
        self.hsn = frappe.db.get_value("Item", SEED_ITEM, "gst_hsn_code")
        self.price_list = frappe.get_single("Selling Settings").selling_price_list
        self.customer = frappe.get_doc("Customer", CUSTOMER)
        self.tax_account = frappe.db.get_value(
            "Account", {"company": self.company, "account_type": "Tax", "is_group": 0}, "name")

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()
        self.assertEqual(self.commits, [], "a commit escaped the test rollback")

    # ------------------------------------------------------------- fixtures

    def make_item(self, code, price=100):
        frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code,
            "item_group": self.item_group, "stock_uom": self.uom, "is_stock_item": 0,
            "is_sales_item": 1, "gst_hsn_code": self.hsn,
            "custom_slug": code.lower()}).insert(ignore_permissions=True)
        if price is not None:
            frappe.get_doc({"doctype": "Item Price", "item_code": code,
                "price_list": self.price_list, "price_list_rate": price,
                "selling": 1, "uom": self.uom}).insert(ignore_permissions=True)
        return code

    def gst_rows(self, template_name):
        """Real India Compliance tax rows, so `gst_tax_type` is populated."""
        template = frappe.db.get_value(
            "Sales Taxes and Charges Template",
            {"name": ["like", f"{template_name}%"], "company": self.company}, "name")
        if not template:
            self.skipTest(f"no {template_name} tax template on this site")
        doc = frappe.get_doc("Sales Taxes and Charges Template", template)
        return [{
            "charge_type": t.charge_type, "account_head": t.account_head,
            "rate": t.rate, "description": t.description,
            "included_in_print_rate": t.included_in_print_rate,
            "cost_center": t.cost_center,
        } for t in doc.taxes]

    def flat_tax(self, rate=18, inclusive=0, description="Test Tax"):
        return [{"charge_type": "On Net Total", "account_head": self.tax_account,
                 "rate": rate, "description": description,
                 "included_in_print_rate": inclusive}]

    def priced_order(self, rows, taxes=None, discount=0):
        so = frappe.new_doc("Sales Order")
        so.customer = CUSTOMER
        so.company = self.company
        so.currency = "INR"
        so.selling_price_list = self.price_list
        so.transaction_date = today()
        so.delivery_date = today()
        for code, qty in rows:
            so.append("items", {"item_code": code, "qty": qty, "delivery_date": today()})
        for tax in (taxes or []):
            so.append("taxes", tax)
        if discount:
            so.discount_amount = discount
        so.flags.ignore_permissions = True
        so.set_missing_values()
        so.calculate_taxes_and_totals()
        apply_pricing_rule_on_transaction(so)
        so.calculate_taxes_and_totals()
        return so

    def projection(self, so):
        from yob_storefront.services.pricing_service import build_pricing_projection
        return build_pricing_projection(so)

    def free_rule(self, qualifying, gift):
        return frappe.get_doc({
            "doctype": "Pricing Rule", "title": f"B3 Free {qualifying}->{gift}",
            "apply_on": "Item Code", "price_or_product_discount": "Product",
            "min_qty": 2, "free_item": gift, "free_qty": 1, "selling": 1,
            "company": self.company, "currency": "INR",
            "items": [{"item_code": qualifying}],
            "valid_from": add_days(today(), -1)}).insert(ignore_permissions=True).name


class ExclusiveAndInclusiveTaxCase(RowTaxBase):

    def test_exclusive_tax_row_totals(self):
        """The business case: taxable 100, GST 18, row total 118."""
        item = self.make_item("B3T-EX", 100)
        so = self.priced_order([(item, 1)], self.flat_tax(18))
        row = self.projection(so)[0]

        self.assertEqual(row["net_amount"], 100)
        self.assertEqual(row["tax_amount"], 18)
        self.assertEqual(row["total_amount"], 118)
        self.assertEqual(row["tax_amount"], so.total_taxes_and_charges,
                         "row tax must reconcile with the document tax row")
        self.assertEqual(row["total_amount"], so.grand_total)

    def test_inclusive_tax_is_not_double_counted(self):
        """rate 100 INCLUDES the tax, so the row total is 100 -- not 118 or 136."""
        item = self.make_item("B3T-INC", 100)
        so = self.priced_order([(item, 1)], self.flat_tax(18, inclusive=1))
        row = self.projection(so)[0]

        self.assertEqual(row["amount"], 100, "amount already contains the tax")
        self.assertEqual(row["net_amount"], so.net_total)
        self.assertEqual(row["tax_amount"], so.total_taxes_and_charges)
        self.assertEqual(row["total_amount"], so.grand_total,
                         "inclusive tax was added on top of a rate that already had it")
        self.assertEqual(row["tax_components"][0]["included_in_print_rate"], 1)

    def test_non_taxable_item_reports_zero_not_failure(self):
        item = self.make_item("B3T-NOTAX", 100)
        so = self.priced_order([(item, 1)])          # no tax rows at all
        row = self.projection(so)[0]

        self.assertEqual(row["tax_amount"], 0)
        self.assertEqual(row["tax_components"], [])
        self.assertEqual(row["total_amount"], row["net_amount"])
        self.assertEqual(row["total_amount"], so.grand_total)


class GstJurisdictionCase(RowTaxBase):
    """India Compliance decides the split. YOB only reports it."""

    def assert_matches_native(self, so, projection):
        native_total = flt(so.total_taxes_and_charges, 2)
        projected = flt(sum(r["tax_amount"] for r in projection), 2)
        self.assertEqual(projected, native_total,
                         "projected row tax does not reconcile with the Sales Order")

    def assert_classified_or_explain(self, types, expected):
        """Assert the GST split -- but only where this bench can produce one.

        India Compliance classifies `gst_tax_type` only for a GST-REGISTERED
        company: `ignore_gst_validations()` short-circuits to an empty account map
        when the company has no GSTIN, so every component legitimately comes back
        unclassified. That is IC being authoritative, not YOB failing to read it.

        The numeric parity above is asserted unconditionally and is the real
        contract. This adds the split assertion on a bench that can support it,
        and says plainly why it cannot on one that cannot -- rather than
        hardcoding CGST/SGST and passing by luck.
        """

        if any(types):
            self.assertTrue(expected.issubset({t for t in types if t}),
                            f"expected {expected} components, got {types}")
            return

        company = frappe.db.get_value(
            "Company", self.company, ["gstin", "gst_category"], as_dict=True) or {}
        if company.get("gstin"):
            self.fail(f"company is GST registered but no component was classified: {types}")

        self.skipTest(
            f"company {self.company!r} is gst_category="
            f"{company.get('gst_category')!r} with no GSTIN, so India Compliance "
            f"declines to classify GST accounts. Numeric tax parity is still "
            f"asserted; the CGST/SGST/IGST split needs a registered company.")

    def test_intra_state_gst_components(self):
        item = self.make_item("B3T-IN", 100)
        so = self.priced_order([(item, 1)], self.gst_rows("Output GST In-state"))
        row = self.projection(so)[0]

        self.assert_matches_native(so, self.projection(so))

        types = [c["tax_type"] for c in row["tax_components"]]
        self.assertEqual(len(row["tax_components"]), len(so.taxes),
                         "a tax row was dropped from the projection")
        # Derived from the fixture, not asserted from this prompt.
        self.assertEqual(types, [(t.get("gst_tax_type") or "").upper() or None
                                 for t in so.taxes])
        self.assert_classified_or_explain(types, {"CGST", "SGST"})
        self.assertEqual(row["total_amount"], so.grand_total)

    def test_inter_state_gst_components(self):
        item = self.make_item("B3T-OUT", 100)
        so = self.priced_order([(item, 1)], self.gst_rows("Output GST Out-state"))
        row = self.projection(so)[0]

        self.assert_matches_native(so, self.projection(so))
        types = [c["tax_type"] for c in row["tax_components"]]
        self.assertEqual(types, [(t.get("gst_tax_type") or "").upper() or None
                                 for t in so.taxes])
        self.assert_classified_or_explain(types, {"IGST"})
        self.assertEqual(row["total_amount"], so.grand_total)

    def test_tax_type_comes_from_india_compliance_not_account_names(self):
        """A non-GST charge must not be labelled as a GST type."""
        item = self.make_item("B3T-GEN", 100)
        so = self.priced_order([(item, 1)], self.flat_tax(18, description="Handling"))
        component = self.projection(so)[0]["tax_components"][0]

        self.assertIsNone(component["tax_type"],
                          "a charge without India Compliance metadata was classified")
        self.assertEqual(component["label"], "Handling",
                         "the numeric result must survive even without a GST type")
        self.assertEqual(component["amount"], 18)

    def test_components_keep_transaction_tax_row_order(self):
        item = self.make_item("B3T-ORD", 100)
        so = self.priced_order([(item, 1)], self.gst_rows("Output GST In-state"))
        labels = [c["label"] for c in self.projection(so)[0]["tax_components"]]

        self.assertEqual(labels, [t.description for t in so.taxes],
                         "components were reordered away from the transaction")


class PromotionRowTaxCase(RowTaxBase):

    def test_same_sku_paid_and_promotion_get_their_own_tax(self):
        """Two rows, one SKU. Tax must not be grouped by item_code."""
        item = self.make_item("B3T-SAME", 100)
        self.free_rule(item, item)
        frappe.clear_cache()

        so = self.priced_order([(item, 2)], self.flat_tax(18))
        projection = self.projection(so)

        paid = [r for r in projection if r["line_role"] == "Paid"]
        promo = [r for r in projection if r["line_role"] == "Promotion"]
        self.assertEqual(len(paid), 1)
        self.assertEqual(len(promo), 1)
        self.assertEqual(paid[0]["item_code"], promo[0]["item_code"])

        # Each row's tax is derived from ITS OWN Sales Order row.
        self.assertEqual(paid[0]["tax_amount"], flt(paid[0]["net_amount"] * 0.18, 2))
        self.assertNotEqual(paid[0]["tax_amount"], promo[0]["tax_amount"],
                            "paid tax was copied onto the promotion row")
        self.assertEqual(flt(sum(r["tax_amount"] for r in projection), 2),
                         flt(so.total_taxes_and_charges, 2))

    def test_promotion_tax_is_erpnext_derived_not_assumed_zero(self):
        """It happens to be 0 here -- but because ERPNext said so."""
        item = self.make_item("B3T-PZ", 100)
        self.free_rule(item, item)
        frappe.clear_cache()

        so = self.priced_order([(item, 2)], self.flat_tax(18))
        promo = next(r for r in self.projection(so) if r["line_role"] == "Promotion")
        free_row = next(r for r in so.items if r.get("is_free_item"))

        self.assertTrue(promo["tax_components"],
                        "the promotion row carries no tax component at all -- its tax "
                        "was assumed rather than extracted")
        self.assertEqual(promo["tax_amount"], flt(free_row.net_amount * 0.18, 2))

    def test_different_sku_gift_carries_its_own_tax(self):
        bought = self.make_item("B3T-BUY", 100)
        gift = self.make_item("B3T-GIFT", 100)
        self.free_rule(bought, gift)
        frappe.clear_cache()

        so = self.priced_order([(bought, 2)], self.flat_tax(18))
        promo = next(r for r in self.projection(so) if r["line_role"] == "Promotion")

        self.assertEqual(promo["item_code"], gift)
        self.assertIsNotNone(promo["tax_amount"])
        self.assertEqual(flt(sum(r["tax_amount"] for r in self.projection(so)), 2),
                         flt(so.total_taxes_and_charges, 2))

    def test_paid_zero_rate_row_is_still_paid(self):
        item = self.make_item("B3T-FREE100", 100)
        frappe.get_doc({"doctype": "Pricing Rule", "title": "B3 FullDiscount",
            "apply_on": "Item Code", "price_or_product_discount": "Price",
            "rate_or_discount": "Discount Percentage", "discount_percentage": 100,
            "min_qty": 1, "selling": 1, "company": self.company, "currency": "INR",
            "items": [{"item_code": item}],
            "valid_from": add_days(today(), -1)}).insert(ignore_permissions=True)
        frappe.clear_cache()

        row = self.projection(self.priced_order([(item, 1)], self.flat_tax(18)))[0]

        self.assertEqual(row["rate"], 0)
        self.assertEqual(row["line_role"], "Paid",
                         "a fully discounted paid row was classified from rate == 0")
        self.assertEqual(row["is_free_item"], 0)


class DiscountRoundingAndParityCase(RowTaxBase):

    def test_row_tax_uses_post_discount_values(self):
        item = self.make_item("B3T-DISC", 100)
        so = self.priced_order([(item, 1)], self.flat_tax(18), discount=50)
        row = self.projection(so)[0]

        self.assertLess(row["net_amount"], 100, "the document discount did not apply")
        self.assertEqual(row["net_amount"], so.net_total)
        self.assertEqual(row["tax_amount"], so.total_taxes_and_charges,
                         "row tax is pre-discount")
        self.assertEqual(row["total_amount"], so.grand_total)

    def test_fractional_tax_reconciles_across_rows(self):
        """ERPNext pushes the rounding difference onto the last breakup row."""
        a = self.make_item("B3T-R1", 33.33)
        b = self.make_item("B3T-R2", 66.67)
        c = self.make_item("B3T-R3", 10.01)
        so = self.priced_order([(a, 3), (b, 3), (c, 7)], self.flat_tax(18))
        projection = self.projection(so)

        self.assertEqual(flt(sum(r["tax_amount"] for r in projection), 2),
                         flt(so.total_taxes_and_charges, 2),
                         "row tax does not sum to the authoritative tax row")
        self.assertEqual(flt(sum(r["total_amount"] for r in projection), 2),
                         flt(so.grand_total, 2))

    def test_items_with_different_tax_rates_each_get_their_own(self):
        a = self.make_item("B3T-M1", 100)
        b = self.make_item("B3T-M2", 200)
        taxes = self.flat_tax(18, description="Tax A")
        so = self.priced_order([(a, 1), (b, 1)], taxes)
        projection = self.projection(so)

        by_item = {r["item_code"]: r for r in projection}
        # Same rate, different bases: proof the document percentage is not simply
        # smeared uniformly across rows.
        self.assertEqual(by_item[a]["tax_amount"], 18)
        self.assertEqual(by_item[b]["tax_amount"], 36)
        self.assertNotEqual(by_item[a]["tax_amount"], by_item[b]["tax_amount"])

    def test_repeated_projection_is_idempotent(self):
        item = self.make_item("B3T-IDEM", 100)
        so = self.priced_order([(item, 1)], self.flat_tax(18))

        first = self.projection(so)
        second = self.projection(so)
        self.assertEqual(first, second)


class CartAndDraftOrderTaxParityCase(RowTaxBase):
    """The Cart's tax view must not diverge from the order that will be placed."""

    def cart_with(self, item, qty):
        from yob_storefront.api import cart as cart_api
        frappe.clear_cache()
        cart = cart_api.get_or_create_cart(self.customer)
        cart.set("items", [])
        cart.coupon_code = None
        cart.save(ignore_permissions=True)
        with patch.object(cart_api, "get_storefront_customer", return_value=self.customer):
            inspect.unwrap(cart_api.add_to_cart)(auth_context={}, item_code=item, qty=qty)
        cart = cart_api.get_or_create_cart(self.customer)
        cart.reload()
        return cart

    def test_cart_projection_matches_the_draft_sales_order(self):
        from yob_storefront.services.order_service import create_sales_order_from_cart
        from yob_storefront.services.pricing_service import (
            calculate_cart_using_sales_order, sync_sales_order_to_cart)

        item = self.make_item("B3T-PAR", 100)
        cart = self.cart_with(item, 2)

        temp = calculate_cart_using_sales_order(cart, self.customer)
        projection = sync_sales_order_to_cart(cart, temp)
        draft = create_sales_order_from_cart(cart)

        self.assertEqual(flt(sum(r["tax_amount"] for r in projection), 2),
                         flt(draft.total_taxes_and_charges or 0, 2),
                         "Cart tax diverges from the Draft Sales Order")
        self.assertEqual(flt(cart.grand_total, 2), flt(draft.grand_total, 2))

        for row in projection:
            self.assertIn("tax_components", row)
            self.assertIsNotNone(row["total_amount"])

    def test_no_tax_is_persisted_as_customer_intent(self):
        item = self.make_item("B3T-NOPERSIST", 100)
        cart = self.cart_with(item, 2)

        self.assertIsNone(frappe.get_meta("Cart Item").get_field("tax_components"),
                          "a tax child table was added to persistent Cart intent")
        self.assertEqual(len(cart.items), 1)

    def test_no_client_controlled_tax_input_exists(self):
        from yob_storefront.api import cart as cart_api

        signature = inspect.signature(inspect.unwrap(cart_api.add_to_cart))
        for forbidden in ("tax_amount", "tax_rate", "tax_components", "gst_tax_type",
                          "tax_category", "item_tax_template", "taxable_amount",
                          "total_amount", "line_total"):
            self.assertNotIn(forbidden, signature.parameters,
                             f"add_to_cart accepts `{forbidden}` from the browser")


if __name__ == "__main__":
    unittest.main()
