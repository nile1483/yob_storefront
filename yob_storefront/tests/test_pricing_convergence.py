# Copyright (c) 2026, YOB and Shayona
"""Product preview, Cart and Draft Sales Order must agree (Phase 23B-1).

THE DEFECT THIS PINS
--------------------
The product page showed 600 and the Cart charged 1000 for the same item, same
customer, same session. Two independent resolvers answered one question:

    product preview   Customer -> Customer Group -> Selling Settings
    cart              YOB Store Settings.default_price_list  (customer ignored)

The Cart also froze its price list at CREATION, so changing a customer's price
list never reached an existing cart. Both now resolve through
`services/pricing_context.SellingContext`.

WHAT IS AND IS NOT A DEFECT
---------------------------
A preview is a single-item transaction; a Cart is the whole document. They may
legitimately differ once quantity rules, mixed conditions, promotions, coupons or
document discounts engage. These tests therefore compare only cases where NO
document-level effect exists -- there, any difference is a context bug.
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


class PricingBase(unittest.TestCase):
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
        self.currency = store.default_currency
        self.item_group = frappe.db.get_value("Item", SEED_ITEM, "item_group")
        self.uom = frappe.db.get_value("Item", SEED_ITEM, "stock_uom")
        self.hsn = frappe.db.get_value("Item", SEED_ITEM, "gst_hsn_code")
        self.default_pl = frappe.get_single("Selling Settings").selling_price_list
        self.customer = frappe.get_doc("Customer", CUSTOMER)

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()
        self.assertEqual(self.commits, [], "a commit escaped the test rollback")

    # ------------------------------------------------------------- fixtures

    def make_item(self, code, price=100, price_list=None, is_stock_item=0, **kw):
        doc = {"doctype": "Item", "item_code": code, "item_name": code,
               "item_group": self.item_group, "stock_uom": self.uom,
               "is_stock_item": is_stock_item, "is_sales_item": 1,
               "gst_hsn_code": self.hsn, "custom_slug": code.lower()}
        doc.update(kw)
        frappe.get_doc(doc).insert(ignore_permissions=True)
        if price is not None:
            self.make_price(code, price, price_list)
        return code

    def make_price(self, item, rate, price_list=None, **kw):
        doc = {"doctype": "Item Price", "item_code": item,
               "price_list": price_list or self.default_pl,
               "price_list_rate": rate, "selling": 1, "uom": self.uom}
        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True).name

    def make_price_list(self, name):
        return frappe.get_doc({"doctype": "Price List", "price_list_name": name,
            "selling": 1, "enabled": 1, "currency": "INR"}).insert(ignore_permissions=True).name

    def reload_customer(self):
        frappe.clear_document_cache("Customer", CUSTOMER)
        self.customer = frappe.get_doc("Customer", CUSTOMER)
        return self.customer

    # ------------------------------------------------------------- the paths

    def preview(self, item, qty=1):
        """What the product page shows."""
        from yob_storefront.services.pricing_service import get_item_pricing
        frappe.clear_cache()
        return get_item_pricing(customer=self.reload_customer(), item_code=item, qty=qty,
                                company=self.company, currency=self.currency)

    def cart_with(self, item, qty=1):
        """A Cart holding only that item, priced."""
        frappe.clear_cache()
        customer = self.reload_customer()
        cart = self.cart_api.get_or_create_cart(customer)
        cart.set("items", [])
        cart.coupon_code = None
        cart.save(ignore_permissions=True)
        with patch.object(self.cart_api, "get_storefront_customer", return_value=customer):
            response = inspect.unwrap(self.cart_api.add_to_cart)(
                auth_context={}, item_code=item, qty=qty)
        self.assertNotIn("errors", response, f"add_to_cart failed: {response}")
        cart = self.cart_api.get_or_create_cart(customer)
        cart.reload()
        return cart

    def draft_order(self, cart):
        from yob_storefront.services.order_service import create_sales_order_from_cart
        return create_sales_order_from_cart(cart)

    def assert_converges(self, item, label):
        """Preview == Cart == Draft SO, on the values that decide the money."""
        preview = self.preview(item)
        cart = self.cart_with(item)
        row = next(r for r in cart.items if r.item_code == item)
        order_row = next(r for r in self.draft_order(cart).items if r.item_code == item)

        self.assertEqual(preview["rate"], row.rate,
                         f"{label}: product page {preview['rate']} != cart {row.rate}")
        self.assertEqual(preview["base_price"], row.base_price, f"{label}: base price")
        self.assertEqual(row.rate, order_row.rate, f"{label}: cart != draft sales order")
        self.assertEqual(preview["uom"], order_row.uom, f"{label}: uom")
        self.assertEqual(order_row.conversion_factor, 1, f"{label}: conversion factor")
        self.assertEqual(order_row.stock_qty, order_row.qty, f"{label}: stock qty")
        return preview, row, order_row


class PriceConvergenceCase(PricingBase):

    def test_generic_item_price(self):
        self.assert_converges(self.make_item("B1-GEN", 100), "generic")

    def test_customer_specific_item_price_wins_everywhere(self):
        item = self.make_item("B1-SPEC", 100)
        self.make_price(item, 60, customer=CUSTOMER)
        _, row, _ = self.assert_converges(item, "customer-specific")
        self.assertEqual(row.rate, 60, "the customer's own price did not win")

    def test_customer_price_list_is_honoured_by_the_cart(self):
        """THE regression: product page 600, cart 1000."""
        alt = self.make_price_list("B1 PL Customer")
        item = self.make_item("B1-PL", 1000)          # store default list
        self.make_price(item, 600, price_list=alt)    # the customer's own list
        frappe.get_doc("Customer", CUSTOMER).db_set(
            "default_price_list", alt, update_modified=False)

        _, row, _ = self.assert_converges(item, "customer price list")
        self.assertEqual(row.rate, 600,
                         "the Cart ignored the Customer's price list and used the "
                         "store default")

    def test_customer_group_price_list_is_honoured_by_the_cart(self):
        alt = self.make_price_list("B1 PL Group")
        item = self.make_item("B1-GRP", 1000)
        self.make_price(item, 700, price_list=alt)
        customer = frappe.get_doc("Customer", CUSTOMER)
        customer.db_set("default_price_list", None, update_modified=False)
        frappe.get_doc("Customer Group", customer.customer_group).db_set(
            "default_price_list", alt, update_modified=False)

        _, row, _ = self.assert_converges(item, "customer group price list")
        self.assertEqual(row.rate, 700)

    def test_stale_cart_price_list_is_re_resolved(self):
        """A cart created before the customer's price list changed must catch up."""
        alt = self.make_price_list("B1 PL Later")
        item = self.make_item("B1-STALE", 1000)
        self.make_price(item, 400, price_list=alt)

        cart = self.cart_with(item)
        self.assertEqual(next(r for r in cart.items).rate, 1000)

        frappe.get_doc("Customer", CUSTOMER).db_set(
            "default_price_list", alt, update_modified=False)

        cart = self.cart_with(item)
        self.assertEqual(next(r for r in cart.items).rate, 400,
                         "the Cart kept the price list it was created with")

    def test_fallback_price_list(self):
        alt = self.make_price_list("B1 PL Empty")
        item = self.make_item("B1-FB", 250)           # priced on the DEFAULT list only
        frappe.get_doc("Customer", CUSTOMER).db_set(
            "default_price_list", alt, update_modified=False)

        original = frappe.get_single("Selling Settings").fallback_to_default_price_list
        self.addCleanup(frappe.db.set_single_value, "Selling Settings",
                        "fallback_to_default_price_list", original)
        frappe.db.set_single_value("Selling Settings", "fallback_to_default_price_list", 1)

        _, row, _ = self.assert_converges(item, "fallback")
        self.assertEqual(row.rate, 250)

    def test_pricing_rule_applies_identically(self):
        item = self.make_item("B1-RULE", 100)
        frappe.get_doc({"doctype": "Pricing Rule", "title": "B1 Rule",
            "apply_on": "Item Code", "price_or_product_discount": "Price",
            "rate_or_discount": "Discount Percentage", "discount_percentage": 20,
            "min_qty": 1, "selling": 1, "company": self.company, "currency": "INR",
            "items": [{"item_code": item}],
            "valid_from": add_days(today(), -1)}).insert(ignore_permissions=True)

        _, row, order_row = self.assert_converges(item, "pricing rule")
        self.assertEqual(row.rate, 80)
        self.assertTrue(order_row.pricing_rules, "the rule did not reach the order")

    def test_stock_and_non_stock_items_both_converge(self):
        self.assert_converges(self.make_item("B1-NONSTOCK", 100, is_stock_item=0), "non-stock")
        self.assert_converges(self.make_item("B1-STOCK", 100, is_stock_item=1), "stock")


class ProductMetadataCase(PricingBase):

    def detail(self, slug):
        from yob_storefront.api import catalog as catalog_api
        frappe.clear_cache()
        with patch.object(catalog_api, "get_storefront_customer",
                          return_value=self.reload_customer()):
            return inspect.unwrap(catalog_api.get_item)(slug=slug, auth_context={})

    def test_uom_reflects_the_priced_transaction(self):
        self.make_item("B1-UOM", 100)
        data = self.detail("b1-uom")["data"]
        self.assertEqual(data["uom"], self.uom, "uom must come from the priced row")
        self.assertEqual(data["stock_uom"], self.uom)

    def test_non_stock_item_reports_no_availability(self):
        self.make_item("B1-SERVICE", 100, is_stock_item=0)
        data = self.detail("b1-service")["data"]
        self.assertEqual(data["is_stock_item"], 0)
        self.assertIsNone(data["actual_qty"],
                          "a non-stock item must not report 0 -- that reads as sold out")
        self.assertIsNone(data["warehouse"])

    def test_stock_item_reports_actual_qty_for_the_resolved_warehouse(self):
        item = self.make_item("B1-INSTOCK", 100, is_stock_item=1)
        data = self.detail("b1-instock")["data"]
        self.assertEqual(data["is_stock_item"], 1)

        if data["warehouse"] is None:
            # Legitimate: no default warehouse resolves on this site. It must then
            # report nothing rather than invent a number.
            self.assertIsNone(data["actual_qty"])
            return

        expected = frappe.db.get_value(
            "Bin", {"item_code": item, "warehouse": data["warehouse"]}, "actual_qty") or 0
        self.assertEqual(data["actual_qty"], expected,
                         "actual_qty must be this SKU in the resolved warehouse")


if __name__ == "__main__":
    unittest.main()
