# Copyright (c) 2026, YOB and Shayona
"""Selling UOM: one meaning for the buyer's quantity (Phase 23B-5U).

THE DEFECT THIS FIXES
---------------------
    Item `sales_uom = Box`, conversion factor 10, Item Price 100 / Nos

    product page   1000 per Box        <- ERPNext's own answer
    Cart           100 per Nos         <- YOB overriding it
    Draft SO       100 per Nos

`add_to_cart` wrote `uom = stock_uom` onto the Cart row, and both
`calculate_cart_using_sales_order` and `create_sales_order_from_cart` passed
`row.uom or row.stock_uom` back to ERPNext. With a UOM already in context,
`get_basic_details` has no decision left to make -- so the storefront quoted one
unit and charged another for the same buyer input.

WHAT REPLACES IT
----------------
Nothing derives a UOM in YOB. A new Cart line is sent with NO uom, so ERPNext
resolves `sales_uom or stock_uom` itself; `sync_sales_order_to_cart` then records
what it resolved. Later reprices send that recorded unit back, which is what
keeps a buyer's "2" meaning 2 Strips from the product page to the Sales Order --
and stops a merchant's later `sales_uom` edit from silently reinterpreting a
quantity somebody already chose.

The conversion factor is NEVER sent. ERPNext re-derives it every time from the
Item's UOM table, so `stock_qty = qty * conversion_factor` -- the number Pricing
Rules compare against -- stays ERPNext's.

WHAT THE BUYER SENDS
--------------------
Quantity. Nothing else. There is no UOM, conversion-factor or warehouse
parameter on any storefront endpoint, and this file asserts that too.
"""

import inspect
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_days, flt, today

CUSTOMER = "YOB Demo Buyer"
SEED_ITEM = "YOB-BOLT-M10"
CONTACT = "Demo Buyer-YOB Demo Buyer"
BILLING = "YOB Demo Billing-Billing"
SHIPPING = "YOB Demo Shipping-Shipping"


def _seeded():
    return bool(frappe.db.exists("Customer", CUSTOMER) and frappe.db.exists("Item", SEED_ITEM))


def _executable_source(module) -> str:
    """Module source with docstrings and comments removed.

    Round-tripping through the AST drops comments for free; docstrings are popped
    explicitly. Used so a "YOB must not do X" scan reads what the code DOES, not
    what its comments say about X.
    """

    import ast

    tree = ast.parse(inspect.getsource(module))

    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body.pop(0)

    return ast.unparse(tree)


class SellingUomBase(unittest.TestCase):
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
        self.stock_uom = frappe.db.get_value("Item", SEED_ITEM, "stock_uom")
        self.hsn = frappe.db.get_value("Item", SEED_ITEM, "gst_hsn_code")
        self.price_list = frappe.get_single("Selling Settings").selling_price_list
        self.customer = frappe.get_doc("Customer", CUSTOMER)

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()
        self.assertEqual(self.commits, [], "a commit escaped the test rollback")

    # ------------------------------------------------------------- fixtures

    def make_item(self, code, price=100, price_uom=None, sales_uom=None,
                  factor=10, is_stock_item=1, **kw):
        """An Item that sells in `sales_uom` (factor `factor`) and stocks in Nos."""

        doc = {"doctype": "Item", "item_code": code, "item_name": code,
               "item_group": self.item_group, "stock_uom": self.stock_uom,
               "is_stock_item": is_stock_item, "is_sales_item": 1,
               "gst_hsn_code": self.hsn, "custom_slug": code.lower()}

        if sales_uom:
            doc["sales_uom"] = sales_uom
            doc["uoms"] = [{"uom": self.stock_uom, "conversion_factor": 1},
                           {"uom": sales_uom, "conversion_factor": factor}]
        doc.update(kw)
        frappe.get_doc(doc).insert(ignore_permissions=True)

        if price is not None:
            self.make_price(code, price, uom=price_uom or self.stock_uom)
        return code

    def make_price(self, item, rate, uom=None, price_list=None):
        return frappe.get_doc({
            "doctype": "Item Price", "item_code": item,
            "price_list": price_list or self.price_list, "price_list_rate": rate,
            "selling": 1, "uom": uom or self.stock_uom}).insert(ignore_permissions=True).name

    def price_rule(self, item, min_qty, discount=20, **kw):
        doc = {"doctype": "Pricing Rule", "title": f"_U5 rule {item}",
               "apply_on": "Item Code", "price_or_product_discount": "Price",
               "rate_or_discount": "Discount Percentage", "discount_percentage": discount,
               "min_qty": min_qty, "selling": 1, "company": self.company,
               "currency": "INR", "items": [{"item_code": item}],
               "valid_from": add_days(today(), -1)}
        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True).name

    def free_rule(self, qualifying, gift, min_qty=2, free_qty=1):
        return frappe.get_doc({
            "doctype": "Pricing Rule", "title": f"_U5 free {qualifying}",
            "apply_on": "Item Code", "price_or_product_discount": "Product",
            "min_qty": min_qty, "free_item": gift, "free_qty": free_qty, "selling": 1,
            "company": self.company, "currency": "INR",
            "items": [{"item_code": qualifying}],
            "valid_from": add_days(today(), -1)}).insert(ignore_permissions=True).name

    def reload_customer(self):
        frappe.clear_document_cache("Customer", CUSTOMER)
        self.customer = frappe.get_doc("Customer", CUSTOMER)
        return self.customer

    # ------------------------------------------------------------- the paths

    def preview(self, item, qty=1):
        from yob_storefront.services.pricing_service import get_item_pricing

        frappe.clear_cache()
        return get_item_pricing(customer=self.reload_customer(), item_code=item, qty=qty,
                                company=self.company, currency=self.currency)

    def add_to_cart(self, item, qty, fresh=True):
        frappe.clear_cache()
        customer = self.reload_customer()
        cart = self.cart_api.get_or_create_cart(customer)

        if fresh:
            cart.set("items", [])
            cart.coupon_code = None
            cart.save(ignore_permissions=True)

        response = self.try_add(item, qty)
        self.assertNotIn("errors", response, f"add_to_cart failed: {response}")

        cart = self.cart_api.get_or_create_cart(customer)
        cart.reload()
        return cart

    def try_add(self, item, qty):
        """The raw envelope, so a refusal can be inspected rather than raised."""

        with patch.object(self.cart_api, "get_storefront_customer",
                          return_value=self.reload_customer()):
            return inspect.unwrap(self.cart_api.add_to_cart)(
                auth_context={}, item_code=item, qty=qty)

    def stored_row(self, cart):
        return frappe.db.get_value(
            "Cart Item", {"parent": cart.name},
            ["item_code", "quantity", "uom", "stock_uom", "conversion_factor", "rate"],
            as_dict=True)

    def with_addresses(self, cart):
        cart.contact_person = CONTACT
        cart.billing_address = BILLING
        cart.shipping_address = SHIPPING
        cart.save(ignore_permissions=True)
        return cart

    def reprice(self, cart):
        from yob_storefront.services.cart_service import reprice_cart

        reprice_cart(cart, self.reload_customer())
        cart.save(ignore_permissions=True)
        return cart

    def pricing_order(self, cart):
        from yob_storefront.services.pricing_service import calculate_cart_using_sales_order

        return calculate_cart_using_sales_order(cart, self.reload_customer())

    def draft_order(self, cart):
        from yob_storefront.services.order_service import create_sales_order_from_cart

        return create_sales_order_from_cart(self.with_addresses(cart))

    def projection(self, cart):
        from yob_storefront.services.pricing_service import (
            calculate_cart_using_sales_order, sync_sales_order_to_cart)

        so = calculate_cart_using_sales_order(cart, self.reload_customer())
        return sync_sales_order_to_cart(cart, so), so

    def row_for(self, so, item):
        return next(r for r in so.items if r.item_code == item and not r.get("is_free_item"))

    # ------------------------------------------------------------- assertion

    def assert_one_meaning(self, item, qty, uom, factor):
        """Preview == Cart intent == pricing SO == Draft SO, unit and quantity alike."""

        preview = self.preview(item, qty=qty)
        cart = self.add_to_cart(item, qty)
        row = cart.items[0]
        pricing_so = self.pricing_order(cart)
        priced = self.row_for(pricing_so, item)
        draft = self.draft_order(cart)
        ordered = self.row_for(draft, item)

        for label, value in (("preview", preview["uom"]), ("cart intent", row.uom),
                             ("cart pricing", priced.uom), ("draft order", ordered.uom)):
            self.assertEqual(value, uom, f"{label} used the wrong unit")

        for label, value in (("preview", preview["conversion_factor"]),
                             ("cart intent", row.conversion_factor),
                             ("cart pricing", priced.conversion_factor),
                             ("draft order", ordered.conversion_factor)):
            self.assertEqual(flt(value), flt(factor), f"{label} conversion factor")

        self.assertEqual(flt(row.quantity), flt(qty), "the buyer's quantity changed")
        self.assertEqual(flt(priced.qty), flt(qty))
        self.assertEqual(flt(ordered.qty), flt(qty))

        # ERPNext derives this; nobody stores or computes it in YOB.
        self.assertEqual(flt(priced.stock_qty), flt(qty) * flt(factor))
        self.assertEqual(flt(ordered.stock_qty), flt(qty) * flt(factor))
        self.assertEqual(flt(preview["stock_qty"]), flt(qty) * flt(factor),
                         "the preview reported a different stock quantity")

        self.assertEqual(flt(preview["rate"]), flt(priced.rate),
                         "the page quoted a different rate from the cart")
        self.assertEqual(flt(cart.grand_total, 2), flt(draft.grand_total, 2),
                         "Cart and Draft Sales Order disagree")
        return preview, cart, priced, ordered


# =========================================================
# 1-4. WHERE THE UNIT COMES FROM
# =========================================================

class SellingUomResolutionCase(SellingUomBase):

    def test_sales_uom_differs_from_stock_uom(self):
        """THE reproduced defect: Box, factor 10, price 100 per Nos."""

        item = self.make_item("_U5-BOX", price=100, sales_uom="Box", factor=10)

        preview, cart, priced, ordered = self.assert_one_meaning(item, 2, "Box", 10)

        self.assertEqual(preview["rate"], 1000, "ERPNext converts the per-Nos price")
        self.assertEqual(cart.items[0].rate, 1000, "the cart charged the per-Nos rate")
        self.assertEqual(flt(cart.grand_total, 2), 2000.0)
        self.assertEqual(cart.items[0].stock_uom, self.stock_uom,
                         "the stock unit must still be recorded for labelling")

    def test_item_without_sales_uom_falls_back_to_stock_uom(self):
        item = self.make_item("_U5-PLAIN", price=100)

        _, cart, _, _ = self.assert_one_meaning(item, 3, self.stock_uom, 1)

        self.assertEqual(cart.items[0].rate, 100)
        self.assertEqual(flt(cart.grand_total, 2), 300.0)

    def test_price_in_stock_uom_is_converted_by_erpnext(self):
        """100 per Nos, selling in Strips of 10 -> 1000 per Strip."""

        item = self.make_item("_U5-STRIP", price=100, sales_uom="Box", factor=10)
        preview = self.preview(item, qty=2)

        self.assertEqual(preview["base_price"], 1000)
        self.assertEqual(preview["total_amount"], 2000)

    def test_price_defined_directly_in_the_selling_uom_wins(self):
        """An exact-UOM Item Price is used as-is, never multiplied."""

        item = self.make_item("_U5-DIRECT", price=100, sales_uom="Box", factor=10)
        self.make_price(item, 900, uom="Box")

        preview, cart, priced, _ = self.assert_one_meaning(item, 1, "Box", 10)

        self.assertEqual(preview["rate"], 900,
                         "the direct Box price lost to the converted Nos price")
        self.assertEqual(priced.rate, 900)
        self.assertEqual(flt(cart.grand_total, 2), 900.0)


# =========================================================
# 5-8. STOCK, VARIANTS, RULES
# =========================================================

class SellingUomTransactionCase(SellingUomBase):

    def test_pricing_rule_thresholds_use_erpnext_stock_qty(self):
        """A rule with min_qty 10 fires on ONE Box of 10 -- ERPNext's own rule."""

        item = self.make_item("_U5-RULE", price=100, sales_uom="Box", factor=10)
        self.price_rule(item, min_qty=10, discount=20)

        cart = self.add_to_cart(item, 1)
        row = cart.items[0]
        _, so = self.projection(cart)
        priced = self.row_for(so, item)

        self.assertEqual(row.uom, "Box")
        self.assertEqual(flt(priced.stock_qty), 10.0)
        self.assertEqual(flt(row.discount_percentage), 20.0,
                         "the rule did not see the stock quantity")
        self.assertEqual(flt(row.rate), 800.0)

    def test_entered_quantity_alone_does_not_satisfy_a_rule(self):
        """The other half: 1 entered unit is not 1 stock unit."""

        item = self.make_item("_U5-RULE2", price=100, sales_uom="Box", factor=10)
        self.price_rule(item, min_qty=11, discount=20)

        cart = self.add_to_cart(item, 1)

        self.assertFalse(flt(cart.items[0].discount_percentage),
                         "a rule above the stock quantity was applied anyway")

    def test_non_stock_item_converges_too(self):
        item = self.make_item("_U5-SERVICE", price=100, sales_uom="Box", factor=10,
                              is_stock_item=0)

        _, cart, priced, _ = self.assert_one_meaning(item, 2, "Box", 10)

        self.assertEqual(flt(priced.stock_qty), 20.0,
                         "ERPNext still derives stock_qty for a non-stock item")

    def test_variant_sells_in_its_own_selling_uom(self):
        from erpnext.controllers.item_variant import create_variant

        attribute = frappe.db.get_value("Item Attribute", {"name": "Size"}, "name")
        if not attribute:
            self.skipTest("no Item Attribute on this bench")

        template = self.make_item("_U5-TMPL", price=None, has_variants=1,
                                  attributes=[{"attribute": attribute}])
        value = frappe.db.get_value("Item Attribute Value", {"parent": attribute},
                                    "attribute_value")
        variant = create_variant(template, {attribute: value})
        variant.sales_uom = "Box"
        variant.set("uoms", [{"uom": self.stock_uom, "conversion_factor": 1},
                             {"uom": "Box", "conversion_factor": 10}])
        variant.insert(ignore_permissions=True)
        self.make_price(variant.name, 100)

        _, cart, _, _ = self.assert_one_meaning(variant.name, 2, "Box", 10)

        self.assertEqual(cart.items[0].item_code, variant.name,
                         "the cart holds the template rather than the variant SKU")
        self.assertEqual(cart.items[0].rate, 1000)

    def test_stock_availability_stays_in_stock_units(self):
        """Price per Box, availability in Nos. Two different facts, both labelled."""

        from erpnext.stock.utils import get_bin
        from yob_storefront.api.catalog import resolve_stock_availability

        item = self.make_item("_U5-AVAIL", price=100, sales_uom="Box", factor=10)
        warehouse = frappe.get_single_value("Stock Settings", "default_warehouse")
        frappe.db.set_value("Bin", get_bin(item, warehouse).name, "actual_qty", 125)

        preview = self.preview(item, qty=2)
        stock = resolve_stock_availability(self.reload_customer(), item)

        self.assertEqual(preview["uom"], "Box")
        self.assertEqual(preview["stock_uom"], self.stock_uom)
        self.assertEqual(preview["conversion_factor"], 10)
        self.assertEqual(stock["actual_qty"], 125.0, "availability was converted")
        self.assertEqual(stock["stock_uom"], self.stock_uom)


# =========================================================
# 9-11. PROMOTIONS, TAX, PARITY
# =========================================================

class SellingUomMoneyCase(SellingUomBase):

    def gst_taxes(self):
        template = frappe.db.get_value("Sales Taxes and Charges Template",
                                       {"name": ["like", "Output GST In-state%"],
                                        "company": self.company}, "name")
        if not template:
            self.skipTest("no in-state GST template on this bench")
        doc = frappe.get_doc("Sales Taxes and Charges Template", template)
        return [{"charge_type": t.charge_type, "account_head": t.account_head,
                 "rate": t.rate, "description": t.description,
                 "included_in_print_rate": t.included_in_print_rate,
                 "cost_center": t.cost_center} for t in doc.taxes]

    def test_promotion_row_carries_erpnexts_own_unit(self):
        """A free row is ERPNext's output, and so is the unit it is counted in.

        `get_product_discount_rule` builds it as
        `uom = pricing_rule.free_item_uom or item_data.stock_uom` and derives the
        factor for that unit. So a same-SKU promotion on an Item sold in Boxes
        arrives in NOS unless the rule says otherwise -- surprising, but it is
        ERPNext's answer and the merchant changes it on the rule, not here.

        The paid row keeps the buyer's Box either way. Forcing the promotion into
        the paid row's unit would be YOB inventing a free quantity ERPNext never
        granted.
        """

        item = self.make_item("_U5-PROMO", price=100, sales_uom="Box", factor=10)
        self.free_rule(item, item, min_qty=2, free_qty=1)

        cart = self.add_to_cart(item, 2)
        projection, _ = self.projection(cart)

        self.assertEqual(len(cart.items), 1, "a promotion was persisted as buyer intent")
        self.assertEqual(cart.items[0].uom, "Box")
        self.assertEqual({r["line_role"] for r in projection}, {"Paid", "Promotion"})

        paid = next(r for r in projection if not r["is_free_item"])
        promotion = next(r for r in projection if r["is_free_item"])

        self.assertEqual(paid["uom"], "Box")
        self.assertEqual(flt(paid["conversion_factor"]), 10.0)
        self.assertEqual(flt(paid["stock_qty"]), 20.0)

        self.assertEqual(promotion["uom"], self.stock_uom,
                         "the free row did not use ERPNext's own free-item unit")
        self.assertEqual(flt(promotion["conversion_factor"]), 1.0)
        self.assertEqual(flt(promotion["qty"]), 1.0)

        # Whatever the unit, the row stays internally consistent.
        for row in projection:
            self.assertEqual(flt(row["stock_qty"]),
                             flt(row["qty"]) * flt(row["conversion_factor"]))

    def test_the_rule_decides_the_free_item_unit(self):
        """`free_item_uom` on the Pricing Rule is the merchant's control, not ours."""

        item = self.make_item("_U5-PROMOBOX", price=100, sales_uom="Box", factor=10)
        rule = self.free_rule(item, item, min_qty=2, free_qty=1)
        frappe.db.set_value("Pricing Rule", rule, "free_item_uom", "Box")
        frappe.clear_document_cache("Pricing Rule", rule)

        cart = self.add_to_cart(item, 2)
        projection, _ = self.projection(cart)
        promotion = next(r for r in projection if r["is_free_item"])

        self.assertEqual(promotion["uom"], "Box")
        self.assertEqual(flt(promotion["conversion_factor"]), 10.0)
        self.assertEqual(flt(promotion["stock_qty"]), 10.0)

    def test_different_sku_gift_keeps_its_own_unit(self):
        qualifying = self.make_item("_U5-BUY", price=100, sales_uom="Box", factor=10)
        gift = self.make_item("_U5-GIFT", price=50)
        self.free_rule(qualifying, gift, min_qty=2, free_qty=1)

        cart = self.add_to_cart(qualifying, 2)
        projection, _ = self.projection(cart)

        paid = next(r for r in projection if r["item_code"] == qualifying)
        free = next(r for r in projection if r["item_code"] == gift)

        self.assertEqual(paid["uom"], "Box")
        self.assertEqual(free["uom"], self.stock_uom,
                         "the gift was priced in the qualifying item's unit")

    def test_tax_is_calculated_on_the_selling_unit_amount(self):
        item = self.make_item("_U5-TAX", price=100, sales_uom="Box", factor=10)
        cart = self.add_to_cart(item, 2)
        self.with_addresses(cart)

        so = self.pricing_order(cart)
        for tax in self.gst_taxes():
            so.append("taxes", tax)
        so.calculate_taxes_and_totals()

        from yob_storefront.services.pricing_service import build_pricing_projection

        row = build_pricing_projection(so, cart)[0]

        self.assertEqual(flt(row["net_amount"], 2), 2000.0, "tax base is not 2 Boxes")
        self.assertEqual(flt(row["tax_amount"], 2), flt(so.total_taxes_and_charges, 2))
        self.assertEqual(flt(row["total_amount"], 2), flt(so.grand_total, 2))
        self.assertEqual(row["uom"], "Box")

    def test_cart_to_draft_order_parity_in_a_selling_uom(self):
        item = self.make_item("_U5-PARITY", price=100, sales_uom="Box", factor=10)
        self.price_rule(item, min_qty=10, discount=20)

        cart = self.add_to_cart(item, 3)
        draft = self.draft_order(cart)
        row = self.row_for(draft, item)

        self.assertEqual(flt(cart.grand_total, 2), flt(draft.grand_total, 2))
        self.assertEqual(flt(cart.net_total, 2), flt(draft.net_total, 2))
        self.assertEqual(cart.currency, draft.currency)
        self.assertEqual(row.uom, cart.items[0].uom)
        self.assertEqual(flt(row.qty), flt(cart.items[0].quantity))
        self.assertEqual(flt(row.rate), flt(cart.items[0].rate))
        self.assertEqual(flt(row.stock_qty), 30.0)


# =========================================================
# 12. REPEATED ADDS, AND A MERCHANT WHO CHANGES THE UNIT LATER
# =========================================================

class ExistingCartSafetyCase(SellingUomBase):
    """A stored quantity must never quietly come to mean something else."""

    def test_repeated_add_keeps_one_row_and_one_unit(self):
        item = self.make_item("_U5-REPEAT", price=100, sales_uom="Box", factor=10)

        self.add_to_cart(item, 2)
        cart = self.add_to_cart(item, 3, fresh=False)

        self.assertEqual(len(cart.items), 1, "a second row appeared for one SKU")
        self.assertEqual(cart.items[0].uom, "Box")
        self.assertEqual(flt(cart.items[0].quantity), 5.0)
        self.assertEqual(flt(cart.items[0].rate), 1000.0)
        self.assertEqual(flt(cart.grand_total, 2), 5000.0)

    def test_merchant_changing_sales_uom_does_not_reinterpret_a_stored_quantity(self):
        """2 Boxes stay 2 Boxes. The product page moves on; the cart does not."""

        item = self.make_item("_U5-SWITCH", price=100, sales_uom="Box", factor=10)
        cart = self.add_to_cart(item, 2)

        self.assertEqual(flt(cart.grand_total, 2), 2000.0)

        frappe.db.set_value("Item", item, "sales_uom", self.stock_uom)
        frappe.clear_document_cache("Item", item)

        self.reprice(cart)

        self.assertEqual(cart.items[0].uom, "Box",
                         "the buyer's 2 Boxes were silently reinterpreted")
        self.assertEqual(flt(cart.items[0].quantity), 2.0)
        self.assertEqual(flt(cart.grand_total, 2), 2000.0)
        self.assertEqual(cart.flags.get("uom_changed_items"), [],
                         "nothing about this line actually changed")

        # New shoppers get the merchant's new unit; the old line keeps its own.
        self.assertEqual(self.preview(item)["uom"], self.stock_uom)

    def test_a_changed_conversion_factor_is_reported_not_hidden(self):
        """ERPNext stays the authority on the factor; the buyer gets told."""

        item = self.make_item("_U5-FACTOR", price=100, sales_uom="Box", factor=10)
        cart = self.add_to_cart(item, 2)

        doc = frappe.get_doc("Item", item)
        for row in doc.uoms:
            if row.uom == "Box":
                row.conversion_factor = 12
        doc.save(ignore_permissions=True)
        frappe.clear_document_cache("Item", item)

        self.reprice(cart)

        self.assertEqual(cart.items[0].uom, "Box")
        self.assertEqual(flt(cart.items[0].conversion_factor), 12.0,
                         "the cart froze a conversion factor ERPNext has changed")
        self.assertEqual(flt(cart.items[0].rate), 1200.0)
        self.assertEqual(cart.flags.get("uom_changed_items"), [item],
                         "the buyer was not told the unit is worth something else")

    def test_a_removed_conversion_is_reported(self):
        """Merchant deletes the Box conversion entirely.

        ERPNext then values a Box at 1 stock unit -- exactly what it does for a
        Desk-entered Sales Order draft in the same state. YOB does not paper over
        it, and does not hide it either.
        """

        item = self.make_item("_U5-DROPPED", price=100, sales_uom="Box", factor=10)
        cart = self.add_to_cart(item, 2)

        doc = frappe.get_doc("Item", item)
        doc.sales_uom = self.stock_uom
        doc.set("uoms", [row for row in doc.uoms if row.uom == self.stock_uom])
        doc.save(ignore_permissions=True)
        frappe.clear_document_cache("Item", item)

        self.reprice(cart)

        self.assertEqual(cart.flags.get("uom_changed_items"), [item])
        self.assertEqual(flt(cart.items[0].conversion_factor), 1.0)

    def test_the_cart_response_carries_the_unit_and_the_reconciliation(self):
        item = self.make_item("_U5-RESPONSE", price=100, sales_uom="Box", factor=10)
        self.add_to_cart(item, 2)

        with patch.object(self.cart_api, "get_storefront_customer",
                          return_value=self.reload_customer()):
            response = inspect.unwrap(self.cart_api.get_cart)(auth_context={})

        data = response["data"]
        row = data["cart"]["items"][0]
        priced = data["cart"]["pricing_rows"][0]

        self.assertEqual(row["uom"], "Box", "Angular cannot label the quantity")
        self.assertEqual(row["stock_uom"], self.stock_uom)
        self.assertEqual(flt(row["conversion_factor"]), 10.0)
        self.assertEqual(priced["uom"], "Box")
        self.assertEqual(flt(priced["stock_qty"]), 20.0)
        self.assertIn("uom_changed_items", data)
        self.assertEqual(data["uom_changed_items"], [])

    def test_a_live_payment_link_goes_stale_when_the_unit_changes(self):
        """The unit is part of the obligation, so a changed factor cannot commit."""

        from yob_storefront.services.commitment_service import (
            ensure_payment_request_committed)
        from yob_storefront.tests.test_payment_lifecycle import _error_code, _raw
        from yob_storefront.api import checkout

        item = self.make_item("_U5-PAYMENT", price=100, sales_uom="Box", factor=10)
        cart = self.with_addresses(self.add_to_cart(item, 2))

        with patch.object(checkout, "get_storefront_customer", return_value=self.customer):
            issued = _raw(checkout.proceed_to_payment)(auth_context={})

        token = issued["data"]["token"]

        doc = frappe.get_doc("Item", item)
        for row in doc.uoms:
            if row.uom == "Box":
                row.conversion_factor = 12
        doc.save(ignore_permissions=True)
        frappe.clear_document_cache("Item", item)

        before = frappe.db.count("Sales Order")
        result = ensure_payment_request_committed(token=token)

        self.assertEqual(_error_code(result), "payment_request_stale", result)
        self.assertEqual(frappe.db.count("Sales Order"), before)


# =========================================================
# THE MERGE GUARD (Phase 23B-5U-1)
# =========================================================

class ExistingLineMergeGuardCase(SellingUomBase):
    """A quantity entered in today's unit can never land on yesterday's line.

    A Cart line keeps the selling UOM ERPNext resolved when that intent was first
    priced. If the merchant later changes the item's selling UOM, the product page
    starts showing the new one -- so the "2" a buyer types means Boxes while the
    stored line still counts Nos. Merging them would file 2 Boxes as 2 Nos.

    There is no safe silent answer: converting rewrites intent the buyer already
    gave, and a second row would need duplicate-SKU carts, which YOB does not
    have. The add is refused, the two units are named, and the buyer removes the
    line and adds it again -- still without ever choosing a unit.
    """

    def switch_selling_uom(self, item, uom):
        """The merchant's edit, nothing else."""

        frappe.db.set_value("Item", item, "sales_uom", uom)
        frappe.clear_document_cache("Item", item)
        frappe.clear_cache()

    def test_add_is_refused_when_the_selling_unit_moved(self):
        item = self.make_item("_U51-MOVED", price=100, sales_uom=None)
        cart = self.add_to_cart(item, 2)

        self.assertEqual(cart.items[0].uom, self.stock_uom)

        # Merchant now sells it in Boxes of 10.
        doc = frappe.get_doc("Item", item)
        doc.sales_uom = "Box"
        doc.set("uoms", [{"uom": self.stock_uom, "conversion_factor": 1},
                         {"uom": "Box", "conversion_factor": 10}])
        doc.save(ignore_permissions=True)
        frappe.clear_document_cache("Item", item)
        frappe.clear_cache()

        self.assertEqual(self.preview(item)["uom"], "Box",
                         "the product page did not move to the new unit")

        response = self.try_add(item, 2)
        error = response["errors"][0]

        self.assertEqual(error["code"], "cart_item_uom_changed")
        self.assertEqual(error["field"], "item_code")
        self.assertEqual(error["details"], {"item_code": item,
                                            "existing_uom": self.stock_uom,
                                            "current_uom": "Box"})
        self.assertEqual(frappe.local.response.get("http_status_code"), 409)

    def test_the_refused_add_changes_nothing_at_all(self):
        item = self.make_item("_U51-INTACT", price=100, sales_uom="Box", factor=10)
        cart = self.add_to_cart(item, 2)

        before = self.stored_row(cart)
        before_cart = frappe.db.get_value(
            "Cart", cart.name, ["grand_total", "net_total", "total_quantity", "modified"],
            as_dict=True)

        self.switch_selling_uom(item, self.stock_uom)

        response = self.try_add(item, 3)
        self.assertEqual(response["errors"][0]["code"], "cart_item_uom_changed")

        after = self.stored_row(cart)
        after_cart = frappe.db.get_value(
            "Cart", cart.name, ["grand_total", "net_total", "total_quantity", "modified"],
            as_dict=True)

        self.assertEqual(after, before, "the refused add mutated the cart line")
        self.assertEqual(after_cart, before_cart, "the refused add mutated the cart")
        self.assertEqual(flt(after.quantity), 2.0)
        self.assertEqual(after.uom, "Box")
        self.assertEqual(flt(after.conversion_factor), 10.0)
        self.assertEqual(flt(after.rate), 1000.0)

    def test_no_duplicate_row_is_created(self):
        item = self.make_item("_U51-NODUP", price=100, sales_uom="Box", factor=10)
        cart = self.add_to_cart(item, 1)

        self.switch_selling_uom(item, self.stock_uom)
        self.try_add(item, 1)

        self.assertEqual(frappe.db.count("Cart Item", {"parent": cart.name}), 1,
                         "a duplicate-SKU row appeared")

    def test_unchanged_unit_merges_normally(self):
        item = self.make_item("_U51-SAME", price=100, sales_uom="Box", factor=10)

        self.add_to_cart(item, 2)
        cart = self.add_to_cart(item, 3, fresh=False)

        self.assertEqual(len(cart.items), 1)
        self.assertEqual(cart.items[0].uom, "Box")
        self.assertEqual(flt(cart.items[0].quantity), 5.0)
        self.assertEqual(flt(cart.grand_total, 2), 5000.0)

    def test_plain_stock_uom_item_still_merges(self):
        """The guard must not fire for the ordinary case."""

        item = self.make_item("_U51-PLAIN", price=100)

        self.add_to_cart(item, 2)
        cart = self.add_to_cart(item, 4, fresh=False)

        self.assertEqual(flt(cart.items[0].quantity), 6.0)
        self.assertEqual(cart.items[0].uom, self.stock_uom)
        self.assertEqual(flt(cart.grand_total, 2), 600.0)

    def test_removing_and_re_adding_adopts_the_new_unit(self):
        """The documented way out, and the buyer still picks no unit."""

        item = self.make_item("_U51-REDO", price=100, sales_uom="Box", factor=10)
        self.add_to_cart(item, 2)

        self.switch_selling_uom(item, self.stock_uom)

        with patch.object(self.cart_api, "get_storefront_customer",
                          return_value=self.reload_customer()):
            removed = inspect.unwrap(self.cart_api.remove_from_cart)(
                auth_context={}, item_code=item)
        self.assertNotIn("errors", removed, removed)

        cart = self.add_to_cart(item, 2, fresh=False)

        self.assertEqual(len(cart.items), 1)
        self.assertEqual(cart.items[0].uom, self.stock_uom,
                         "the fresh line did not take ERPNext's current unit")
        self.assertEqual(flt(cart.items[0].rate), 100.0)
        self.assertEqual(flt(cart.grand_total, 2), 200.0)

    def test_parity_survives_the_guard(self):
        """A cart that passes the guard still commits identically."""

        item = self.make_item("_U51-PARITY", price=100, sales_uom="Box", factor=10)
        self.add_to_cart(item, 2)
        cart = self.add_to_cart(item, 1, fresh=False)

        draft = self.draft_order(cart)
        row = self.row_for(draft, item)

        self.assertEqual(flt(cart.grand_total, 2), flt(draft.grand_total, 2))
        self.assertEqual(row.uom, "Box")
        self.assertEqual(flt(row.qty), 3.0)
        self.assertEqual(flt(row.stock_qty), 30.0)

    def test_the_guard_asks_erpnext_rather_than_reading_sales_uom(self):
        """The comparison value comes from the same call the order uses."""

        from yob_storefront.services.pricing_context import context_for

        item = self.make_item("_U51-SOURCE", price=100, sales_uom="Box", factor=10)
        context = context_for(self.reload_customer())

        self.assertEqual(context.resolved_selling_uom(item), "Box")
        self.assertEqual(context.resolved_selling_uom(item), self.preview(item)["uom"])

        plain = self.make_item("_U51-SOURCE2", price=100)
        self.assertEqual(context_for(self.reload_customer()).resolved_selling_uom(plain),
                         self.stock_uom)

        self.assertNotIn("sales_uom", _executable_source(self.cart_api),
                         "the guard reads the Item field instead of asking ERPNext")

    def test_an_item_erpnext_cannot_describe_is_not_a_mismatch(self):
        """No comparison possible must not become a refusal."""

        from yob_storefront.services.pricing_context import context_for

        item = self.make_item("_U51-UNKNOWN", price=100, sales_uom="Box", factor=10)
        cart = self.add_to_cart(item, 1)

        with patch.object(context_for(self.reload_customer()).__class__,
                          "resolved_selling_uom", return_value=None):
            response = self.try_add(item, 2)

        self.assertNotIn("errors", response, response)
        cart.reload()
        self.assertEqual(flt(cart.items[0].quantity), 3.0)


# =========================================================
# THE BUYER SENDS QUANTITY, AND NOTHING ELSE
# =========================================================

class NoBuyerUnitInputCase(SellingUomBase):

    FORBIDDEN = ("uom", "stock_uom", "sales_uom", "conversion_factor", "stock_qty",
                 "warehouse", "price_list", "rate")

    def test_no_storefront_endpoint_accepts_a_unit_parameter(self):
        import importlib
        import pkgutil

        import yob_storefront.api as api_pkg

        checked = 0

        for module_info in pkgutil.iter_modules(api_pkg.__path__):
            module = importlib.import_module(f"yob_storefront.api.{module_info.name}")

            for name, obj in vars(module).items():
                if not callable(obj) or getattr(obj, "__module__", None) != module.__name__:
                    continue
                if obj not in frappe.whitelisted:
                    continue

                checked += 1
                params = inspect.signature(inspect.unwrap(obj)).parameters

                for forbidden in self.FORBIDDEN:
                    self.assertNotIn(
                        forbidden, params,
                        f"{module.__name__}.{name} accepts `{forbidden}` from the browser")

        self.assertGreater(checked, 10, "the endpoint scan found almost nothing")

    def test_yob_never_decides_a_unit_or_a_conversion_factor(self):
        """No YOB module may reimplement `sales_uom or stock_uom`, or convert.

        Scans EXECUTABLE code only -- the modules discuss `sales_uom` at length in
        their comments, and a scan that counted prose would force the explanation
        out of the file. `add_to_cart` may still read `Item.stock_uom` as a label
        for an unpriced row, so the scan targets the services that build
        transactions.
        """

        from yob_storefront.services import order_service, pricing_service

        for module in (pricing_service, order_service):
            source = _executable_source(module)
            self.assertNotIn("sales_uom", source,
                             f"{module.__name__} re-implements ERPNext's UOM choice")
            self.assertNotIn("* conversion_factor", source,
                             f"{module.__name__} converts quantities itself")
            self.assertNotIn("qty * ", source,
                             f"{module.__name__} derives a stock quantity itself")

    def test_the_row_builder_sends_no_conversion_factor(self):
        """ERPNext must re-derive it, so a corrected factor reaches open carts."""

        from yob_storefront.services.pricing_service import cart_row_to_order_item

        row = frappe._dict({"item_code": "X", "quantity": 2, "uom": "Box",
                            "conversion_factor": 10, "stock_uom": "Nos"})

        self.assertEqual(cart_row_to_order_item(row),
                         {"item_code": "X", "qty": 2, "uom": "Box"})

        unpriced = frappe._dict({"item_code": "X", "quantity": 2, "uom": None,
                                 "conversion_factor": None, "stock_uom": "Nos"})

        self.assertEqual(cart_row_to_order_item(unpriced), {"item_code": "X", "qty": 2},
                         "an unpriced row must leave the unit to ERPNext")


if __name__ == "__main__":
    unittest.main()
