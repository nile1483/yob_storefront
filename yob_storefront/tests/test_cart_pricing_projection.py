# Copyright (c) 2026, YOB and Shayona
"""The Cart pricing projection: paid intent vs ERPNext promotion output.

WHAT WAS BROKEN
---------------
`sync_sales_order_to_cart` wrote every Sales Order row onto the Cart child table,
keyed by `item_code`. Phase 23A caught two consequences:

  * a same-SKU free row (rate 0) landed on the SAME cart row as its paid row and,
    arriving last, overwrote base_price/rate/amount with ZERO -- the buyer saw a
    free line beside a non-zero total;
  * a different-SKU gift had no cart row at all and was silently dropped, so an
    earned free product never appeared anywhere.

THE SEPARATION
--------------
Cart Item  = customer PAID INTENT, persisted, never promotional.
Projection = the authoritative pricing RESULT, derived per reprice, transient.

Persisting promotions would also reopen the recursion hazard: a free quantity
written back as intent would qualify for its own promotion on the next reprice.
"""

import inspect
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_days, today

CUSTOMER = "YOB Demo Buyer"
SEED_ITEM = "YOB-BOLT-M10"


def _seeded():
    return bool(frappe.db.exists("Customer", CUSTOMER) and frappe.db.exists("Item", SEED_ITEM))


class ProjectionBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data on the test site")

    def setUp(self):
        frappe.set_user("Administrator")
        from yob_storefront.api import cart as cart_api
        from yob_storefront.utils.store import get_store_settings

        self.cart_api = cart_api
        self.commits = []
        cp = patch.object(frappe.db, "commit", side_effect=lambda *a, **k: self.commits.append(1))
        cp.start()
        self.addCleanup(cp.stop)

        store = get_store_settings()
        self.company = store.company
        self.item_group = frappe.db.get_value("Item", SEED_ITEM, "item_group")
        self.uom = frappe.db.get_value("Item", SEED_ITEM, "stock_uom")
        self.hsn = frappe.db.get_value("Item", SEED_ITEM, "gst_hsn_code")
        self.price_list = frappe.get_single("Selling Settings").selling_price_list
        self.customer = frappe.get_doc("Customer", CUSTOMER)

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()
        self.assertEqual(self.commits, [], "a commit escaped the test rollback")

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

    def free_rule(self, qualifying, gift, min_qty=2, free_qty=1, recursive=0):
        doc = {"doctype": "Pricing Rule", "title": f"B1 Free {qualifying}->{gift}",
               "apply_on": "Item Code", "price_or_product_discount": "Product",
               "min_qty": min_qty, "free_item": gift, "free_qty": free_qty,
               "selling": 1, "company": self.company, "currency": "INR",
               "items": [{"item_code": qualifying}], "valid_from": add_days(today(), -1)}
        if recursive:
            doc.update({"is_recursive": 1, "recurse_for": min_qty})
        return frappe.get_doc(doc).insert(ignore_permissions=True).name

    def cart_with(self, item, qty):
        frappe.clear_cache()
        cart = self.cart_api.get_or_create_cart(self.customer)
        cart.set("items", [])
        cart.coupon_code = None
        cart.save(ignore_permissions=True)
        with patch.object(self.cart_api, "get_storefront_customer", return_value=self.customer):
            inspect.unwrap(self.cart_api.add_to_cart)(
                auth_context={}, item_code=item, qty=qty)
        cart = self.cart_api.get_or_create_cart(self.customer)
        cart.reload()
        return cart

    def projection_for(self, cart):
        """The authoritative pricing result, and the order it came from."""
        from yob_storefront.services.pricing_service import (
            calculate_cart_using_sales_order, sync_sales_order_to_cart)
        so = calculate_cart_using_sales_order(cart, self.customer)
        return sync_sales_order_to_cart(cart, so), so

    def paid(self, projection):
        return [r for r in projection if r["line_role"] == "Paid"]

    def promotions(self, projection):
        return [r for r in projection if r["line_role"] == "Promotion"]


class SameItemPromotionCase(ProjectionBase):
    """Buy 2 A -> 1 A free. Paid and free share a SKU and must stay separate."""

    def test_paid_and_promotion_are_separate_rows(self):
        item = self.make_item("B1P-A")
        rule = self.free_rule(item, item)
        cart = self.cart_with(item, 2)
        projection, so = self.projection_for(cart)

        paid, promo = self.paid(projection), self.promotions(projection)
        self.assertEqual(len(paid), 1, "expected exactly one paid row")
        self.assertEqual(len(promo), 1, "the free row is missing from the projection")

        self.assertEqual(paid[0]["item_code"], item)
        self.assertEqual(paid[0]["qty"], 2)
        self.assertGreater(paid[0]["rate"], 0,
                           "the paid row was overwritten to zero by the free row")
        self.assertEqual(paid[0]["is_free_item"], 0)

        self.assertEqual(promo[0]["item_code"], item, "same SKU, different role")
        self.assertEqual(promo[0]["qty"], 1)
        self.assertEqual(promo[0]["rate"], 0)
        self.assertEqual(promo[0]["is_free_item"], 1)
        self.assertIn(rule, promo[0]["pricing_rules"],
                      "the promotion row lost its originating rule")

    def test_persistent_cart_row_is_not_zeroed(self):
        """The visible defect: the Cart line showed rate 0 beside a non-zero total."""
        item = self.make_item("B1P-Z")
        self.free_rule(item, item)
        cart = self.cart_with(item, 2)

        row = next(r for r in cart.items if r.item_code == item)
        self.assertEqual(row.quantity, 2, "free qty leaked into purchased qty")
        self.assertGreater(row.rate, 0, "the paid Cart line was overwritten to zero")
        self.assertGreater(row.base_price, 0)

    def test_cart_totals_match_the_sales_order(self):
        item = self.make_item("B1P-T")
        self.free_rule(item, item)
        cart = self.cart_with(item, 2)
        _, so = self.projection_for(cart)

        self.assertEqual(cart.net_total, so.net_total)
        self.assertEqual(cart.grand_total, so.grand_total)
        self.assertEqual(so.net_total, 200, "the free row must not be charged for")

    def test_no_promotion_row_is_persisted_as_cart_intent(self):
        item = self.make_item("B1P-N")
        self.free_rule(item, item)
        cart = self.cart_with(item, 2)

        self.assertEqual(len(cart.items), 1,
                         "an ERPNext promotion row was persisted as customer intent")


class DifferentItemPromotionCase(ProjectionBase):
    """Buy 2 A -> 1 B free. B has no Cart intent row at all."""

    def test_gift_appears_in_the_projection(self):
        bought = self.make_item("B1P-BUY")
        gift = self.make_item("B1P-GIFT")
        self.free_rule(bought, gift)
        cart = self.cart_with(bought, 2)
        projection, so = self.projection_for(cart)

        promo = self.promotions(projection)
        self.assertEqual(len(promo), 1,
                         "the gift was dropped -- it has no Cart row to map onto")
        self.assertEqual(promo[0]["item_code"], gift)
        self.assertEqual(promo[0]["rate"], 0)

        self.assertEqual([r.item_code for r in cart.items], [bought],
                         "the gift must not become customer intent")
        self.assertEqual(cart.grand_total, so.grand_total)


class RecursivePromotionCase(ProjectionBase):

    def test_repricing_is_idempotent_and_free_qty_never_compounds(self):
        item = self.make_item("B1P-REC")
        self.free_rule(item, item, min_qty=2, free_qty=1, recursive=1)
        cart = self.cart_with(item, 2)

        seen = []
        for _ in range(3):
            projection, so = self.projection_for(cart)
            row = next(r for r in cart.items if r.item_code == item)
            seen.append((row.quantity, len(cart.items),
                         sum(p["qty"] for p in self.promotions(projection)),
                         so.net_total))

        self.assertEqual(len(set(seen)), 1,
                         f"repricing was not idempotent: {seen}")
        qty, rows, free_qty, net = seen[0]
        self.assertEqual(qty, 2, "purchased quantity absorbed the free quantity")
        self.assertEqual(rows, 1, "a promotion row was persisted")
        self.assertEqual(free_qty, 1)
        self.assertEqual(net, 200)


class ProjectionContractCase(ProjectionBase):

    def test_pricing_rules_are_normalized_from_both_erpnext_formats(self):
        """Paid rows carry a JSON array, free rows a bare string. Both must parse."""
        from yob_storefront.services.pricing_service import normalize_pricing_rules

        self.assertEqual(normalize_pricing_rules('["PRLE-0001"]'), ["PRLE-0001"])
        self.assertEqual(normalize_pricing_rules("PRLE-0001"), ["PRLE-0001"])
        self.assertEqual(normalize_pricing_rules(["A", "B"]), ["A", "B"])
        self.assertEqual(normalize_pricing_rules('["A","B"]'), ["A", "B"],
                         "multiple rule identities must not be dropped")
        self.assertEqual(normalize_pricing_rules(None), [])
        self.assertEqual(normalize_pricing_rules(""), [])

    def test_projection_reaches_the_cart_response_additively(self):
        item = self.make_item("B1P-RESP")
        self.free_rule(item, item)
        self.cart_with(item, 2)

        with patch.object(self.cart_api, "get_storefront_customer", return_value=self.customer):
            response = inspect.unwrap(self.cart_api.get_cart)(auth_context={})

        data = response["data"]
        self.assertIn("cart", data, "the existing contract must be preserved")
        self.assertIn("items", data["cart"], "Angular still reads cart.items")
        self.assertIn("pricing_rows", data["cart"], "the projection is not exposed")

        roles = [r["line_role"] for r in data["cart"]["pricing_rows"]]
        self.assertIn("Paid", roles)
        self.assertIn("Promotion", roles)

    def test_client_cannot_forge_a_free_item(self):
        """add_to_cart takes item_code and qty. There is nothing else to send."""
        signature = inspect.signature(inspect.unwrap(self.cart_api.add_to_cart))
        for forbidden in ("is_free_item", "rate", "price_list_rate", "discount_percentage",
                          "pricing_rules", "free_qty", "warehouse", "price_list", "customer"):
            self.assertNotIn(forbidden, signature.parameters,
                             f"add_to_cart accepts `{forbidden}` from the browser")

        self.assertIsNone(frappe.get_meta("Cart Item").get_field("is_free_item"),
                          "Cart Item must have no is_free_item field to forge")


if __name__ == "__main__":
    unittest.main()
