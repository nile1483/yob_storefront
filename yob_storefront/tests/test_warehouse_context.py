# Copyright (c) 2026, YOB and Shayona
"""Warehouse and transaction context, end to end (Phase 23B-5W).

WHAT THIS PHASE ASSERTS
-----------------------
One storefront transaction must resolve ONE warehouse, and every surface that
speaks about it must speak about the same one:

    product preview SO row == cart pricing SO row == Draft SO row == the
    warehouse whose quantity the product page displays

WHERE THE WAREHOUSE COMES FROM
------------------------------
ERPNext, always. `get_item_details` -> `get_basic_details` ->
`get_item_warehouse_`, whose precedence is

    Sales Order `set_warehouse` -> Item Default (per company) -> Item Group
    default -> Brand default -> the row's own warehouse -> Stock Settings
    default (only when it belongs to the same company)

YOB reimplements NO part of that chain and supplies no warehouse of its own.
The buyer cannot influence it: no storefront endpoint takes a warehouse and
neither Cart nor Cart Item stores one. Warehouse is trusted server/ERPNext-
derived transaction context, not a buyer choice.

The comparisons below read each row AFTER `set_missing_values()`, because that
is where ERPNext actually decides -- the rows YOB builds carry no warehouse at
all.

AVAILABILITY IS THREE-VALUED
----------------------------
    None  quantity does not apply (non-stock) or is unknown (ERPNext resolved
          no warehouse). Never to be rendered as "out of stock".
    0.0   a real answer: we have none.
    n     the quantity in the warehouse this transaction would draw on, read
          with ERPNext's own `get_bin_details` so a GROUP warehouse aggregates
          its children exactly as the Sales Order line does.
"""

import inspect
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import frappe

CUSTOMER = "YOB Demo Buyer"
SEED_ITEM = "YOB-BOLT-M10"
CONTACT = "Demo Buyer-YOB Demo Buyer"
BILLING = "YOB Demo Billing-Billing"
SHIPPING = "YOB Demo Shipping-Shipping"


def _seeded():
    return bool(frappe.db.exists("Customer", CUSTOMER) and frappe.db.exists("Item", SEED_ITEM))


@contextmanager
def captured_sales_orders():
    """Every Sales Order ERPNext actually calculated inside the block.

    The preview path returns a pricing DICT, not its Sales Order, so the only
    honest way to read the warehouse ERPNext put on that row is to observe the
    real document as production built it. Spying on the calculation entry point
    the pricing code already calls avoids rebuilding the order here -- a copy
    would be free to diverge from the code under test, which is precisely the
    class of bug this phase exists to catch.
    """

    from erpnext.controllers.accounts_controller import AccountsController

    seen = []
    real = AccountsController.calculate_taxes_and_totals

    def spy(self, *args, **kwargs):
        out = real(self, *args, **kwargs)
        if self.doctype == "Sales Order" and not any(s is self for s in seen):
            seen.append(self)
        return out

    with patch.object(AccountsController, "calculate_taxes_and_totals", spy):
        yield seen


class WarehouseBase(unittest.TestCase):
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
        self.price_list = frappe.get_single("Selling Settings").selling_price_list
        self.site_warehouse = frappe.get_single_value("Stock Settings", "default_warehouse")
        self.customer = frappe.get_doc("Customer", CUSTOMER)

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()
        self.assertEqual(self.commits, [], "a commit escaped the test rollback")

    # ------------------------------------------------------------- fixtures

    def make_item(self, code, price=100, is_stock_item=1, warehouse=None, **kw):
        """An item ERPNext will price. `warehouse` sets its per-company default.

        A new Item inherits the global default warehouse into `item_defaults`
        (erpnext/stock/doctype/item/item.py), so an explicit default is written
        rather than assumed, and `strip_default_warehouses` clears the inherited
        row for the deliberately unresolvable case.
        """

        doc = {"doctype": "Item", "item_code": code, "item_name": code,
               "item_group": self.item_group, "stock_uom": self.uom,
               "is_stock_item": is_stock_item, "is_sales_item": 1,
               "gst_hsn_code": self.hsn, "custom_slug": code.lower()}
        doc.update(kw)
        frappe.get_doc(doc).insert(ignore_permissions=True)

        if warehouse:
            frappe.db.delete("Item Default", {"parent": code})
            item = frappe.get_doc("Item", code)
            item.append("item_defaults", {"company": self.company,
                                          "default_warehouse": warehouse})
            item.save(ignore_permissions=True)
            frappe.clear_document_cache("Item", code)

        if price is not None:
            self.make_price(code, price)
        return code

    def make_price(self, item, rate, price_list=None, uom=None):
        return frappe.get_doc({
            "doctype": "Item Price", "item_code": item,
            "price_list": price_list or self.price_list, "price_list_rate": rate,
            "selling": 1, "uom": uom or self.uom}).insert(ignore_permissions=True).name

    def strip_default_warehouses(self, item_code):
        """Leave ERPNext with nothing to resolve, at every level of its chain."""

        frappe.db.delete("Item Default", {"parent": item_code})
        frappe.db.set_single_value("Stock Settings", "default_warehouse", None)
        frappe.clear_cache()
        frappe.clear_document_cache("Item", item_code)

    def other_warehouse(self):
        return frappe.db.get_value("Warehouse", {
            "company": self.company, "is_group": 0, "disabled": 0,
            "name": ["!=", self.site_warehouse]}, "name")

    def set_bin_qty(self, item_code, warehouse, qty):
        from erpnext.stock.utils import get_bin

        bin_doc = get_bin(item_code, warehouse)
        frappe.db.set_value("Bin", bin_doc.name, "actual_qty", qty)
        return bin_doc.name

    def reload_customer(self):
        frappe.clear_document_cache("Customer", CUSTOMER)
        self.customer = frappe.get_doc("Customer", CUSTOMER)
        return self.customer

    # ------------------------------------------------------------- the paths

    def preview_order(self, item, qty=1):
        """The temporary Sales Order the PRODUCT PAGE priced, as production built it."""

        from yob_storefront.services.pricing_service import get_item_pricing

        frappe.clear_cache()
        with captured_sales_orders() as seen:
            pricing = get_item_pricing(customer=self.reload_customer(), item_code=item,
                                       qty=qty, company=self.company, currency=self.currency)
        self.assertTrue(seen, "the preview path priced no Sales Order")
        return seen[-1], pricing

    def cart_with(self, item, qty=1, addresses=True):
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

        if addresses:
            cart.contact_person = CONTACT
            cart.billing_address = BILLING
            cart.shipping_address = SHIPPING
            cart.save(ignore_permissions=True)
        return cart

    def cart_pricing_order(self, cart):
        from yob_storefront.services.pricing_service import calculate_cart_using_sales_order

        return calculate_cart_using_sales_order(cart, self.reload_customer())

    def draft_order(self, cart):
        from yob_storefront.services.order_service import create_sales_order_from_cart

        return create_sales_order_from_cart(cart)

    def availability(self, item):
        from yob_storefront.api.catalog import resolve_stock_availability

        return resolve_stock_availability(self.reload_customer(), item)

    def row_for(self, so, item):
        return next(r for r in so.items if r.item_code == item)


# =========================================================
# 1. WAREHOUSE CONVERGENCE
# =========================================================

class WarehouseConvergenceCase(WarehouseBase):
    """Preview == Cart pricing == Draft Sales Order == displayed availability."""

    def assert_converges(self, item, qty=1):
        preview_so, _ = self.preview_order(item, qty=1)
        cart = self.cart_with(item, qty=qty)
        pricing_so = self.cart_pricing_order(cart)
        draft_so = self.draft_order(cart)
        stock = self.availability(item)

        resolved = self.row_for(preview_so, item).warehouse

        self.assertTrue(resolved, "ERPNext resolved no warehouse for the preview row")
        self.assertEqual(self.row_for(pricing_so, item).warehouse, resolved,
                         "cart pricing warehouse differs from the product preview")
        self.assertEqual(self.row_for(draft_so, item).warehouse, resolved,
                         "the Draft Sales Order warehouse differs from the priced cart")
        self.assertEqual(stock["warehouse"], resolved,
                         "displayed stock is read from a different warehouse")
        return resolved

    def test_all_four_paths_resolve_the_same_warehouse(self):
        self.assert_converges(SEED_ITEM, qty=3)

    def test_item_default_warehouse_reaches_all_four_paths(self):
        """A per-item default must reach every path, not just the order.

        The site default is what any YOB-side shortcut would fall back to, so an
        item pointed somewhere else is what makes the assertion meaningful.
        """

        other = self.other_warehouse()
        if not other:
            self.skipTest("this bench has only one non-group warehouse")

        item = self.make_item("_W5W-DEFAULT", warehouse=other)

        self.assertNotEqual(other, self.site_warehouse,
                            "the fixture must differ from the site default to prove anything")
        self.assertEqual(self.assert_converges(item), other)

    def test_quantity_does_not_change_the_resolved_warehouse(self):
        """Preview prices qty 1; the cart prices what the buyer asked for."""

        item = self.make_item("_W5W-QTY")

        preview_so, _ = self.preview_order(item, qty=1)
        cart = self.cart_with(item, qty=9)
        pricing_so = self.cart_pricing_order(cart)

        self.assertEqual(self.row_for(pricing_so, item).qty, 9)
        self.assertEqual(self.row_for(pricing_so, item).warehouse,
                         self.row_for(preview_so, item).warehouse)

    def test_yob_supplies_no_warehouse_and_copies_no_precedence(self):
        """The rows YOB builds carry NO warehouse; ERPNext fills them in.

        Also a source scan: reimplementing the Item Default -> Item Group ->
        Brand -> Stock Settings chain in YOB would be a second source of truth,
        free to disagree with the order it is meant to describe.
        """

        from yob_storefront.services import order_service, pricing_service

        for module in (pricing_service, order_service):
            source = inspect.getsource(module)
            self.assertNotIn("default_warehouse", source,
                             f"{module.__name__} re-implements warehouse precedence")
            self.assertNotIn("set_warehouse", source,
                             f"{module.__name__} forces a warehouse onto the order")

        item = self.make_item("_W5W-NOINPUT")
        cart = self.cart_with(item, qty=1)

        # What production hands ERPNext, before ERPNext decides anything.
        blank = frappe.new_doc("Sales Order")
        blank.customer = self.customer.name
        blank.company = self.company
        blank.currency = self.currency
        blank.selling_price_list = self.price_list
        blank.append("items", {"item_code": item, "qty": 1})

        self.assertFalse(blank.items[0].warehouse)
        self.assertFalse(blank.set_warehouse)

        priced = self.cart_pricing_order(cart)
        self.assertTrue(self.row_for(priced, item).warehouse,
                        "ERPNext, not YOB, must be the one that fills this in")


# =========================================================
# 2. AVAILABILITY SEMANTICS
# =========================================================

class AvailabilitySemanticsCase(WarehouseBase):

    def test_non_stock_item_reports_no_quantity(self):
        item = self.make_item("_W5W-SERVICE", is_stock_item=0)
        stock = self.availability(item)

        self.assertEqual(stock["is_stock_item"], 0)
        self.assertIsNone(stock["warehouse"])
        self.assertIsNone(stock["actual_qty"], "a service item must not read as out of stock")

    def test_unresolved_warehouse_reports_unknown_not_zero(self):
        item = self.make_item("_W5W-NOWH")
        self.strip_default_warehouses(item)

        stock = self.availability(item)

        self.assertEqual(stock["is_stock_item"], 1)
        self.assertIsNone(stock["warehouse"], "ERPNext should have resolved nothing here")
        self.assertIsNone(stock["actual_qty"],
                          "unknown was reported as zero -- 'we have none' is a different claim")

    def test_zero_stock_reports_zero_not_unknown(self):
        """The other side of the same distinction."""

        item = self.make_item("_W5W-EMPTY")
        stock = self.availability(item)

        self.assertTrue(stock["warehouse"])
        self.assertEqual(stock["actual_qty"], 0.0)

    def test_actual_qty_comes_from_the_resolved_warehouse(self):
        """Stock elsewhere must not be counted; this order cannot draw on it."""

        other = self.other_warehouse()
        if not other:
            self.skipTest("this bench has only one non-group warehouse")

        item = self.make_item("_W5W-ELSEWHERE", warehouse=self.site_warehouse)
        self.set_bin_qty(item, self.site_warehouse, 6)
        self.set_bin_qty(item, other, 500)

        stock = self.availability(item)

        self.assertEqual(stock["warehouse"], self.site_warehouse)
        self.assertEqual(stock["actual_qty"], 6.0,
                         "stock from another warehouse leaked into the product page")

    def test_group_warehouse_matches_erpnexts_own_row_quantity(self):
        """REGRESSION (23B-5W). A raw Bin read under-reported a GROUP warehouse.

        ERPNext resolves the group onto the order line and reports the aggregate
        of its children there (`update_bin_details` passes
        `include_child_warehouses=True`). The product page read the group's own
        Bin row, which does not exist, and showed 0 while the order line showed 9.
        """

        group = frappe.db.get_value("Warehouse", {"company": self.company, "is_group": 1}, "name")
        child = frappe.db.get_value("Warehouse", {"company": self.company, "is_group": 0,
                                                  "disabled": 0,
                                                  "parent_warehouse": group}, "name")
        if not (group and child):
            self.skipTest("this bench has no group warehouse with a child")

        item = self.make_item("_W5W-GROUP", warehouse=group)
        self.set_bin_qty(item, child, 9)

        preview_so, _ = self.preview_order(item)
        row = self.row_for(preview_so, item)
        stock = self.availability(item)

        self.assertEqual(row.warehouse, group, "the fixture did not resolve the group")
        self.assertEqual(stock["warehouse"], group)
        self.assertEqual(stock["actual_qty"], row.get("actual_qty"),
                         "displayed stock disagrees with ERPNext's own order line")
        self.assertEqual(stock["actual_qty"], 9.0)

    def test_variant_reports_its_own_sku(self):
        """A variant is the transactable item; its template's stock is not its own."""

        from erpnext.controllers.item_variant import create_variant

        attribute = frappe.db.get_value("Item Attribute", {"name": "Size"}, "name")
        if not attribute:
            self.skipTest("no Item Attribute on this bench")

        template = self.make_item("_W5W-TMPL", price=None, has_variants=1,
                                  attributes=[{"attribute": attribute}])
        value = frappe.db.get_value("Item Attribute Value", {"parent": attribute},
                                    "attribute_value")
        variant = create_variant(template, {attribute: value})
        variant.insert(ignore_permissions=True)
        self.make_price(variant.name, 250)

        self.set_bin_qty(variant.name, self.site_warehouse, 4)
        self.set_bin_qty(template, self.site_warehouse, 99)

        stock = self.availability(variant.name)

        self.assertEqual(stock["actual_qty"], 4.0, "the variant reported its template's stock")
        self.assertTrue(stock["warehouse"])

        # The template is not transactable: ERPNext refuses to describe it, so
        # quantity is unknown rather than zero.
        self.assertIsNone(self.availability(template)["actual_qty"])


# =========================================================
# 3. WHEN ERPNEXT GENUINELY REQUIRES A WAREHOUSE
# =========================================================

class UnresolvedWarehouseCommitmentCase(WarehouseBase):
    """A merchant with no warehouse default anywhere must FAIL CLOSED.

    ERPNext requires a warehouse on a stock line only at Sales Order validate
    (`SalesOrder.validate_warehouse` -> `WarehouseRequired`). The preview and
    cart-pricing orders are in-memory and never validated, so browsing and
    pricing still work; the commitment is what refuses.

    YOB deliberately does NOT invent a warehouse to fill the gap. Any value it
    chose would be a second precedence chain, and shipping from a warehouse the
    merchant never nominated is worse than refusing.
    """

    def test_draft_sales_order_refuses_and_creates_nothing(self):
        from erpnext.selling.doctype.sales_order.sales_order import WarehouseRequired

        item = self.make_item("_W5W-COMMIT-NOWH")
        cart = self.cart_with(item, qty=1)
        self.strip_default_warehouses(item)

        before = frappe.db.count("Sales Order")

        with self.assertRaises(WarehouseRequired):
            self.draft_order(cart)

        self.assertEqual(frappe.db.count("Sales Order"), before,
                         "a Sales Order survived a refused commitment")
        self.assertEqual(frappe.db.get_value("Cart", cart.name, "status"), "Draft")

    def test_pricing_and_preview_still_work_without_a_warehouse(self):
        item = self.make_item("_W5W-PRICE-NOWH")
        self.strip_default_warehouses(item)

        preview_so, pricing = self.preview_order(item)
        cart = self.cart_with(item, qty=2)
        priced = self.cart_pricing_order(cart)

        self.assertIsNone(self.row_for(preview_so, item).warehouse)
        self.assertIsNone(self.row_for(priced, item).warehouse)
        self.assertEqual(pricing["rate"], 100)
        self.assertEqual(priced.grand_total, 200)


# =========================================================
# 4. THE BUYER CANNOT CHOOSE A WAREHOUSE
# =========================================================

class NoBuyerWarehouseInputCase(WarehouseBase):
    """Warehouse is server context. There is nothing for a browser to send."""

    FORBIDDEN = ("warehouse", "set_warehouse", "from_warehouse", "target_warehouse",
                 "source_warehouse")

    def test_no_storefront_endpoint_accepts_a_warehouse_parameter(self):
        import importlib
        import pkgutil

        import yob_storefront.api as api_pkg

        checked = 0

        for module_info in pkgutil.iter_modules(api_pkg.__path__):
            module = importlib.import_module(f"yob_storefront.api.{module_info.name}")

            for name, obj in vars(module).items():
                if not callable(obj) or getattr(obj, "__module__", None) != module.__name__:
                    continue
                # Frappe records the object it decorated in `frappe.whitelisted`;
                # with YOB's decorator stack that IS the module-level name.
                if obj not in frappe.whitelisted:
                    continue

                checked += 1
                params = inspect.signature(inspect.unwrap(obj)).parameters

                for forbidden in self.FORBIDDEN:
                    self.assertNotIn(
                        forbidden, params,
                        f"{module.__name__}.{name} accepts `{forbidden}` from the browser")

        self.assertGreater(checked, 10, "the endpoint scan found almost nothing")

    def test_store_settings_warehouse_never_reaches_a_transaction(self):
        """`YOB Store Settings.default_warehouse` is INERT, and must stay so.

        The field exists and `cms.get_config` publishes it to the browser, so it
        is a standing temptation: any code that used it would become a second
        warehouse authority, disagreeing with the order ERPNext builds. It is
        therefore pinned behaviourally -- pointing it somewhere else must change
        nothing about the resolved warehouse -- rather than by a source scan alone.
        """

        other = self.other_warehouse()
        if not other:
            self.skipTest("this bench has only one non-group warehouse")

        item = self.make_item("_W5W-STORESET")
        before = self.availability(item)["warehouse"]

        frappe.db.set_single_value("YOB Store Settings", "default_warehouse", other)
        frappe.clear_cache()
        from yob_storefront.utils.store import get_store_settings

        self.assertEqual(get_store_settings().get("default_warehouse"), other,
                         "the fixture did not take effect")

        self.assertEqual(self.availability(item)["warehouse"], before,
                         "the storefront setting overrode ERPNext's own resolution")

        preview_so, _ = self.preview_order(item)
        cart = self.cart_with(item, qty=1)
        self.assertEqual(self.row_for(preview_so, item).warehouse, before)
        self.assertEqual(self.row_for(self.cart_pricing_order(cart), item).warehouse, before)

    def test_cart_documents_have_no_warehouse_field(self):
        for doctype in ("Cart", "Cart Item"):
            meta = frappe.get_meta(doctype)
            for forbidden in ("warehouse", "set_warehouse", "from_warehouse"):
                self.assertIsNone(meta.get_field(forbidden),
                                  f"{doctype} stores a buyer-facing `{forbidden}`")


# =========================================================
# 5. TRANSACTION CONTEXT: PREVIEW vs CART
# =========================================================

class TransactionContextConvergenceCase(WarehouseBase):
    """Preview and Cart resolve the same transaction, field by field.

    They do not share one `SellingContext` INSTANCE -- the preview builds its
    order from the store settings plus `get_price_list_for_customer`, while the
    cart goes through `context_for()`. What matters is whether they can ANSWER
    DIFFERENTLY, so each dimension is compared on the finished orders rather
    than on the objects that built them.
    """

    def orders_for(self, item, qty=1):
        preview_so, pricing = self.preview_order(item, qty=qty)
        cart = self.cart_with(item, qty=qty)
        return preview_so, self.cart_pricing_order(cart), cart, pricing

    def test_party_company_currency_and_date_match(self):
        item = self.make_item("_W5W-CTX")
        preview_so, pricing_so, cart, _ = self.orders_for(item)

        for field in ("customer", "company", "currency", "transaction_date",
                      "selling_price_list", "conversion_rate", "price_list_currency"):
            self.assertEqual(preview_so.get(field), pricing_so.get(field),
                             f"preview and cart disagree on `{field}`")

        # The cart's stored company/currency are what its pricing prefers, so
        # they must be the store's own. Nothing re-resolves them later.
        self.assertEqual(cart.company, self.company)
        self.assertEqual(cart.currency, self.currency)

    def test_price_list_is_resolved_by_one_function(self):
        from yob_storefront.services.pricing_context import context_for
        from yob_storefront.services.pricing_service import get_price_list_for_customer

        customer = self.reload_customer()
        resolved = get_price_list_for_customer(customer)

        self.assertEqual(context_for(customer).price_list, resolved)

        item = self.make_item("_W5W-PL")
        preview_so, pricing_so, cart, _ = self.orders_for(item)

        self.assertEqual(preview_so.selling_price_list, resolved)
        self.assertEqual(pricing_so.selling_price_list, resolved)
        self.assertEqual(cart.selling_price_list, resolved)

    def test_fallback_price_list_behaves_identically_in_both_paths(self):
        """Priced only on the default list, with the customer on another one."""

        selling = frappe.get_single("Selling Settings")

        original = selling.fallback_to_default_price_list
        self.addCleanup(frappe.db.set_single_value, "Selling Settings",
                        "fallback_to_default_price_list", original)
        frappe.db.set_single_value("Selling Settings", "fallback_to_default_price_list", 1)

        own = frappe.get_doc({"doctype": "Price List", "price_list_name": "_W5W Customer PL",
                              "selling": 1, "enabled": 1,
                              "currency": self.currency}).insert(ignore_permissions=True).name
        frappe.db.set_value("Customer", CUSTOMER, "default_price_list", own)
        frappe.clear_document_cache("Customer", CUSTOMER)

        item = self.make_item("_W5W-FALLBACK", price=None)
        self.make_price(item, 321, price_list=selling.selling_price_list)

        preview_so, pricing_so, _, pricing = self.orders_for(item)

        self.assertEqual(preview_so.selling_price_list, own)
        self.assertEqual(pricing_so.selling_price_list, own)
        self.assertEqual(pricing["rate"], 321, "the preview lost the fallback price")
        self.assertEqual(self.row_for(pricing_so, item).rate, 321,
                         "the cart lost the fallback price")

    def test_warehouse_matches_between_the_two_paths(self):
        item = self.make_item("_W5W-CTXWH")
        preview_so, pricing_so, _, _ = self.orders_for(item)

        self.assertEqual(self.row_for(preview_so, item).warehouse,
                         self.row_for(pricing_so, item).warehouse)

    def test_sales_uom_is_resolved_identically(self):
        """REGRESSION (23B-5W found it, 23B-5U fixed it).

        For an Item whose `sales_uom` differs from its stock UOM, ERPNext's own
        answer is the SALES uom, so the product preview prices one Box at 1000.
        `add_to_cart` used to store `uom = stock_uom` and every pricing call
        passed that on, suppressing ERPNext's resolution -- the cart charged 100
        for one Nos while the page quoted 1000 for one Box.

        The Cart now records what ERPNext resolved instead of dictating it. Full
        selling-UOM coverage lives in `test_selling_uom.py`; this stays here
        because it is the divergence this phase's context tests are about.
        """

        item = self.make_item("_W5W-SALESUOM", price=None, is_stock_item=0,
                              sales_uom="Box",
                              uoms=[{"uom": self.uom, "conversion_factor": 1},
                                    {"uom": "Box", "conversion_factor": 10}])
        self.make_price(item, 100)

        _, pricing_so, cart, pricing = self.orders_for(item)
        row = self.row_for(pricing_so, item)

        self.assertEqual(pricing["uom"], "Box", "the preview lost ERPNext's selling UOM")
        self.assertEqual(row.uom, pricing["uom"],
                         "product page and cart price different units")
        self.assertEqual(pricing["rate"], row.rate)
        self.assertEqual(cart.items[0].uom, "Box",
                         "the Cart did not record the unit it was priced in")


if __name__ == "__main__":
    unittest.main()
