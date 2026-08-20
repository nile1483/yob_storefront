# Copyright (c) 2026, YOB and Shayona
"""ERPNext transaction semantics the Cart architecture depends on (Phase 23A).

These pin FACTS ABOUT ERPNEXT, not YOB behaviour, because the deferred Cart
redesign is built on them. If ERPNext ever changes one, the redesign's premise
changes with it and we need to know immediately rather than discover it through a
mispriced order.

Nothing here asserts current Cart behaviour that Phase 23A identified as wrong --
that belongs in the audit report, not in a test that would cement it.
"""

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_days, today

from erpnext.accounts.doctype.pricing_rule.utils import apply_pricing_rule_on_transaction

CUSTOMER = "YOB Demo Buyer"
SEED_ITEM = "YOB-BOLT-M10"


def _seeded():
    return bool(frappe.db.exists("Customer", CUSTOMER) and frappe.db.exists("Item", SEED_ITEM))


class PricingTransactionSemanticsCase(unittest.TestCase):
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

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()
        self.assertEqual(self.commits, [], "a commit escaped the test rollback")

    # ------------------------------------------------------------- fixtures

    def make_item(self, code, price=100, alt_uom=None, conversion=None):
        item = frappe.get_doc({
            "doctype": "Item", "item_code": code, "item_name": code,
            "item_group": self.item_group, "stock_uom": self.uom, "is_stock_item": 0,
            "is_sales_item": 1, "gst_hsn_code": self.hsn, "custom_slug": code.lower(),
        }).insert(ignore_permissions=True)
        if alt_uom:
            item.append("uoms", {"uom": alt_uom, "conversion_factor": conversion})
            item.save(ignore_permissions=True)
        if price is not None:
            frappe.get_doc({
                "doctype": "Item Price", "item_code": code, "price_list": self.price_list,
                "price_list_rate": price, "selling": 1, "uom": self.uom,
            }).insert(ignore_permissions=True)
        return code

    def qty_rule(self, item, min_qty, discount=10, max_qty=None):
        doc = {
            "doctype": "Pricing Rule", "title": f"P23 Qty {item}", "apply_on": "Item Code",
            "price_or_product_discount": "Price", "rate_or_discount": "Discount Percentage",
            "discount_percentage": discount, "min_qty": min_qty, "selling": 1,
            "company": self.company, "currency": "INR", "items": [{"item_code": item}],
            "valid_from": add_days(today(), -1),
        }
        if max_qty:
            doc["max_qty"] = max_qty
        return frappe.get_doc(doc).insert(ignore_permissions=True).name

    def free_rule(self, qualifying, free_item, min_qty=2, free_qty=1):
        return frappe.get_doc({
            "doctype": "Pricing Rule", "title": f"P23 Free {qualifying}->{free_item}",
            "apply_on": "Item Code", "price_or_product_discount": "Product",
            "min_qty": min_qty, "free_item": free_item, "free_qty": free_qty,
            "selling": 1, "company": self.company, "currency": "INR",
            "items": [{"item_code": qualifying}], "valid_from": add_days(today(), -1),
        }).insert(ignore_permissions=True).name

    def priced_order(self, rows):
        """A Sales Order priced exactly as ERPNext would price it."""
        so = frappe.new_doc("Sales Order")
        so.customer = CUSTOMER
        so.company = self.company
        so.currency = "INR"
        so.selling_price_list = self.price_list
        so.transaction_date = today()
        so.delivery_date = today()
        for code, qty, uom in rows:
            line = {"item_code": code, "qty": qty, "delivery_date": today()}
            if uom:
                line["uom"] = uom
            so.append("items", line)
        so.flags.ignore_permissions = True
        so.set_missing_values()
        so.calculate_taxes_and_totals()
        apply_pricing_rule_on_transaction(so)
        so.calculate_taxes_and_totals()
        return so

    # ==================================================== ROW-LEVEL QUANTITY

    def test_quantity_rules_are_evaluated_per_row_not_per_document(self):
        """THE fact the Cart redesign turns on.

        Splitting a quantity across two rows LOSES a quantity-threshold discount.
        So independent customer-intent lines cannot be handed to ERPNext as
        independent pricing rows -- a commercial merge projection is required, or
        buyers silently pay more for adding the same item twice.
        """

        item = self.make_item("P23-QTY")
        self.qty_rule(item, min_qty=5, discount=10)
        frappe.clear_cache()

        one_row = self.priced_order([(item, 5, None)])
        split = self.priced_order([(item, 3, None), (item, 2, None)])

        self.assertEqual(one_row.items[0].rate, 90, "the threshold rule did not apply")
        self.assertEqual(one_row.net_total, 450)

        self.assertEqual([r.rate for r in split.items], [100, 100],
                         "split rows unexpectedly received the threshold discount")
        self.assertEqual(split.net_total, 500)

        self.assertGreater(split.net_total, one_row.net_total,
                           "same total quantity, and the split order costs MORE")

    def test_quantity_thresholds_compare_stock_qty_not_entered_qty(self):
        """`min_qty`/`max_qty` are measured in stock UOM, after conversion.

        A merge key that ignored UOM would therefore change which rules apply.
        """

        alt = frappe.db.get_value("UOM", {"name": ["!=", self.uom]}, "name")
        item = self.make_item("P23-UOM", alt_uom=alt, conversion=5)
        self.qty_rule(item, min_qty=5, max_qty=7, discount=10)
        frappe.clear_cache()

        one_box = self.priced_order([(item, 1, alt)]).items[0]
        self.assertEqual(one_box.stock_qty, 5)
        self.assertEqual(one_box.conversion_factor, 5)
        self.assertTrue(one_box.pricing_rules,
                        "qty=1 with stock_qty=5 should satisfy min_qty=5")

        two_boxes = self.priced_order([(item, 2, alt)]).items[0]
        self.assertEqual(two_boxes.stock_qty, 10)
        self.assertFalse(two_boxes.pricing_rules,
                         "stock_qty=10 exceeds max_qty=7 and must be excluded")

    # ==================================================== PROMOTION ROWS

    def test_same_item_promotion_produces_a_second_row_not_a_bigger_one(self):
        """The free product is a SEPARATE row sharing the paid row's item_code.

        So `item_code` alone cannot identify a transaction row, and a projection
        keyed on it would collapse the promotion into the paid line.
        """

        item = self.make_item("P23-SAME")
        rule = self.free_rule(item, item)
        frappe.clear_cache()

        so = self.priced_order([(item, 2, None)])

        self.assertEqual(len(so.items), 2, "the free row was not generated")
        paid = [r for r in so.items if not r.is_free_item]
        free = [r for r in so.items if r.is_free_item]

        self.assertEqual(len(paid), 1)
        self.assertEqual(len(free), 1)
        self.assertEqual(paid[0].item_code, free[0].item_code, "same SKU, two roles")
        self.assertEqual(paid[0].qty, 2)
        self.assertEqual(paid[0].rate, 100)
        self.assertEqual(free[0].qty, 1)
        self.assertEqual(free[0].rate, 0)
        self.assertIn(rule, str(free[0].pricing_rules),
                      "the free row must carry its originating rule")
        self.assertEqual(so.net_total, 200, "the free row must not be charged for")

    def test_different_item_promotion_adds_a_row_for_an_unordered_item(self):
        """A row can exist for an item the customer never asked for."""

        bought = self.make_item("P23-BUY")
        gift = self.make_item("P23-GIFT")
        self.free_rule(bought, gift)
        frappe.clear_cache()

        so = self.priced_order([(bought, 2, None)])

        free = [r for r in so.items if r.is_free_item]
        self.assertEqual(len(free), 1)
        self.assertEqual(free[0].item_code, gift,
                         "the promotion introduced an item that is not in the cart")
        self.assertEqual(free[0].rate, 0)
        self.assertEqual(so.net_total, 200)

    def test_recursive_promotion_excludes_free_qty_from_the_qualifying_quantity(self):
        """Free rows must never feed the quantity that earned them.

        If they did, a promotion would recurse on its own output and give away
        unbounded stock. ERPNext computes the free quantity from the PAID quantity.
        """

        item = self.make_item("P23-REC")
        frappe.get_doc({
            "doctype": "Pricing Rule", "title": "P23 Recursive", "apply_on": "Item Code",
            "price_or_product_discount": "Product", "min_qty": 2, "free_item": item,
            "free_qty": 1, "is_recursive": 1, "recurse_for": 2, "selling": 1,
            "company": self.company, "currency": "INR", "items": [{"item_code": item}],
            "valid_from": add_days(today(), -1),
        }).insert(ignore_permissions=True)
        frappe.clear_cache()

        for paid_qty, expected_free in ((2, 1), (4, 2)):
            so = self.priced_order([(item, paid_qty, None)])
            free_qty = sum(r.qty for r in so.items if r.is_free_item)
            paid = sum(r.qty for r in so.items if not r.is_free_item)

            self.assertEqual(paid, paid_qty, "the paid quantity was altered")
            self.assertEqual(free_qty, expected_free,
                             f"paid {paid_qty} earned {free_qty} free, expected "
                             f"{expected_free} -- free qty may be recursing on itself")
            self.assertEqual(so.net_total, paid_qty * 100,
                             "free rows must not be charged for")


if __name__ == "__main__":
    unittest.main()
