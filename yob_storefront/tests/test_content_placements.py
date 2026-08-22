# Copyright (c) 2026, YOB and Shayona
"""System route content placements: the model and the runtime (Phase 25G).

WHAT THIS PINS
--------------
A merchant may drop a reusable Content Block into an EXISTING application page.
Two boundaries make that safe rather than a page builder, and both are asserted
here:

* the (route, slot) pair must be one the APPLICATION renders -- a merchant can
  choose among positions, never invent one;
* a Block placed on a route projects through the SAME `project_block()` a
  Storefront Page uses, so the two mechanisms cannot grow different wire
  contracts.

The Product Grid budget is the third: three grids per rendered response, counted
across the whole ROUTE rather than per slot, because `get_route_content` returns
every slot in one answer.
"""

import inspect
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import flt

from yob_storefront.utils.system_slots import SYSTEM_CONTENT_SLOTS

CUSTOMER = "YOB Demo Buyer"
SEED_ITEM = "YOB-BOLT-M10"
PLACEMENT = "YOB Storefront Content Placement"


def _seeded():
    return bool(frappe.db.exists("Customer", CUSTOMER) and frappe.db.exists("Item", SEED_ITEM))


class PlacementBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data on the test site")

    def setUp(self):
        frappe.set_user("Administrator")
        from yob_storefront.api import cms as cms_api

        self.cms = cms_api
        self.customer = frappe.get_doc("Customer", CUSTOMER)

        p = patch.object(cms_api, "get_storefront_customer", return_value=self.customer)
        p.start()
        self.addCleanup(p.stop)

        self.commits = []
        cp = patch.object(frappe.db, "commit", side_effect=lambda *a, **k: self.commits.append(1))
        cp.start()
        self.addCleanup(cp.stop)

        self.item_group = frappe.db.get_value("Item", SEED_ITEM, "item_group")
        self.uom = frappe.db.get_value("Item", SEED_ITEM, "stock_uom")
        self.hsn = frappe.db.get_value("Item", SEED_ITEM, "gst_hsn_code")
        self.price_list = frappe.get_single("Selling Settings").selling_price_list

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()
        self.assertEqual(self.commits, [], "a commit escaped the test rollback")

    # ------------------------------------------------------------- fixtures

    def make_block(self, block_type="Rich Text", block_name=None, **kw):
        doc = {"doctype": "YOB Storefront Block",
               "block_name": block_name or f"_G25 {block_type} {frappe.generate_hash(length=6)}",
               "block_type": block_type, "enabled": 1}

        if block_type == "Rich Text":
            doc.setdefault("content", "<p>hello</p>")
        if block_type == "Image Banner":
            doc.setdefault("desktop_image", "/files/g25.png")

        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True)

    def place(self, route_key, slot_key, block, sequence=0, enabled=1):
        return frappe.get_doc({
            "doctype": PLACEMENT, "route_key": route_key, "slot_key": slot_key,
            "block": block, "sequence": sequence, "enabled": enabled,
        }).insert(ignore_permissions=True)

    def make_category(self, slug):
        return frappe.get_doc({
            "doctype": "Category", "category_name": slug, "slug": slug,
            "is_group": 0, "is_active": 1}).insert(ignore_permissions=True)

    def make_item(self, code, category, price=100, **kw):
        doc = {"doctype": "Item", "item_code": code, "item_name": code,
               "item_group": self.item_group, "stock_uom": self.uom, "is_stock_item": 0,
               "is_sales_item": 1, "gst_hsn_code": self.hsn,
               "custom_slug": code.lower(), "custom_category": category}
        doc.update(kw)
        item = frappe.get_doc(doc).insert(ignore_permissions=True)

        if price is not None:
            frappe.get_doc({
                "doctype": "Item Price", "item_code": item.name,
                "price_list": self.price_list, "price_list_rate": price,
                "selling": 1, "uom": self.uom}).insert(ignore_permissions=True)
        return item

    def grid_block(self, category, item_limit=6, **kw):
        return self.make_block("Product Grid", storefront_category=category,
                               item_limit=item_limit, card_type="Square",
                               sort_by="Name A-Z", **kw)

    # ------------------------------------------------------------- the wire

    def route(self, route_key):
        frappe.clear_cache()
        return inspect.unwrap(self.cms.get_route_content)(
            auth_context={}, route_key=route_key)

    def page(self, slug):
        frappe.clear_cache()
        return inspect.unwrap(self.cms.get_page)(auth_context={}, slug=slug)

    def data(self, response):
        self.assertNotIn("errors", response, f"request failed: {response}")
        return response["data"]

    def code_of(self, response):
        return response["errors"][0]["code"] if "errors" in response else None

    def slots_of(self, route_key):
        return {s["key"]: s["blocks"] for s in self.data(self.route(route_key))["slots"]}


# =========================================================
# THE REGISTRY
# =========================================================

class RegistryCase(PlacementBase):

    def test_every_requested_route_is_registered(self):
        expected = {"home", "catalog", "category", "product", "cart",
                    "account", "orders", "order_detail"}

        self.assertEqual(set(SYSTEM_CONTENT_SLOTS), expected)

    def test_transaction_critical_routes_have_no_slots(self):
        """Checkout and payment are excluded by DECISION, not by omission."""

        from yob_storefront.utils.system_slots import EXCLUDED_ROUTES, is_route

        for route in ("login", "checkout", "payment", "payment_callback"):
            self.assertFalse(is_route(route), f"{route} must not accept content")
            self.assertIn(route, EXCLUDED_ROUTES, f"{route} needs a recorded reason")

    def test_the_doctype_offers_exactly_the_registered_routes(self):
        """The Select options and the registry are one list, or they will drift."""

        options = frappe.get_meta(PLACEMENT).get_field("route_key").options
        offered = {line for line in (options or "").split("\n") if line}

        self.assertEqual(offered, set(SYSTEM_CONTENT_SLOTS))


# =========================================================
# THE MODEL
# =========================================================

class PlacementModelCase(PlacementBase):

    def test_a_valid_home_hero_placement(self):
        block = self.make_block()
        placement = self.place("home", "hero", block.name)

        self.assertEqual(placement.route_key, "home")
        self.assertEqual(placement.slot_key, "hero")

    def test_a_valid_cart_placement(self):
        block = self.make_block()

        self.assertTrue(self.place("cart", "above_cart", block.name).name)

    def test_an_unknown_route_is_refused(self):
        block = self.make_block()

        with self.assertRaises(frappe.ValidationError):
            self.place("checkout", "above_cart", block.name)

    def test_an_unknown_slot_is_refused(self):
        block = self.make_block()

        with self.assertRaises(frappe.ValidationError):
            self.place("cart", "not_a_position", block.name)

    def test_a_real_slot_from_another_route_is_refused(self):
        """`hero` exists and `cart` exists; `cart.hero` is rendered nowhere."""

        block = self.make_block()

        with self.assertRaises(frappe.ValidationError):
            self.place("cart", "hero", block.name)

    def test_an_exact_duplicate_is_refused(self):
        block = self.make_block()
        self.place("cart", "above_cart", block.name)

        with self.assertRaises(frappe.DuplicateEntryError):
            self.place("cart", "above_cart", block.name)

    def test_the_same_block_may_sit_in_another_slot(self):
        block = self.make_block()
        self.place("cart", "above_cart", block.name)

        self.assertTrue(self.place("cart", "below_cart", block.name).name)

    def test_the_same_block_may_sit_on_another_route(self):
        block = self.make_block()
        self.place("cart", "above_cart", block.name)

        self.assertTrue(self.place("home", "hero", block.name).name)

    def test_a_block_may_be_on_a_page_and_on_a_route(self):
        """The whole point of a reusable Block: authored once, placed many times."""

        block = self.make_block()

        frappe.get_doc({
            "doctype": "YOB Storefront Page", "slug": "g25-about", "title": "About",
            "enabled": 1, "blocks": [{"block": block.name}],
        }).insert(ignore_permissions=True)

        self.assertTrue(self.place("home", "hero", block.name).name)

    def test_a_missing_block_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self.place("home", "hero", "_G25 No Such Block")

    def test_ordering_is_deterministic(self):
        first = self.make_block(block_name="_G25 Seq A")
        second = self.make_block(block_name="_G25 Seq B")
        third = self.make_block(block_name="_G25 Seq C")

        self.place("home", "hero", second.name, sequence=20)
        self.place("home", "hero", first.name, sequence=10)
        self.place("home", "hero", third.name, sequence=30)

        names = [b["block_name"] for b in self.slots_of("home")["hero"]]

        self.assertEqual(names, ["_G25 Seq A", "_G25 Seq B", "_G25 Seq C"])


# =========================================================
# THE PRODUCT GRID BUDGET
# =========================================================

class GridBudgetCase(PlacementBase):

    def setUp(self):
        super().setUp()
        self.category = self.make_category("g25-grid-cat")
        self.make_item("_G25-GRID-1", self.category.name)

    def test_three_product_grids_are_allowed_across_a_route(self):
        for index, slot in enumerate(("above_cart", "below_cart", "above_cart")):
            grid = self.grid_block(self.category.name, block_name=f"_G25 Grid {index}")
            self.place("cart", slot, grid.name)

        self.assertEqual(
            frappe.db.count(PLACEMENT, {"route_key": "cart", "enabled": 1}), 3)

    def test_a_fourth_product_grid_on_the_route_is_refused(self):
        for index, slot in enumerate(("above_cart", "below_cart", "above_cart")):
            grid = self.grid_block(self.category.name, block_name=f"_G25 Grid {index}")
            self.place("cart", slot, grid.name)

        fourth = self.grid_block(self.category.name, block_name="_G25 Grid 4")

        with self.assertRaises(frappe.ValidationError):
            self.place("cart", "below_cart", fourth.name)

    def test_the_budget_is_counted_across_slots_not_within_one(self):
        """Three grids in three positions cost what three on one page cost."""

        for index in range(3):
            grid = self.grid_block(self.category.name, block_name=f"_G25 Spread {index}")
            self.place("cart", "above_cart" if index % 2 == 0 else "below_cart", grid.name)

        fourth = self.grid_block(self.category.name, block_name="_G25 Spread 4")

        with self.assertRaises(frappe.ValidationError):
            self.place("cart", "below_cart", fourth.name)

    def test_other_block_types_do_not_count_toward_the_budget(self):
        for index in range(6):
            block = self.make_block("Rich Text", block_name=f"_G25 Text {index}")
            self.place("cart", "above_cart", block.name)

        grid = self.grid_block(self.category.name, block_name="_G25 Grid After Text")

        self.assertTrue(self.place("cart", "below_cart", grid.name).name)

    def test_a_disabled_grid_does_not_consume_the_budget(self):
        for index in range(3):
            grid = self.grid_block(self.category.name, block_name=f"_G25 Off {index}")
            self.place("cart", "above_cart", grid.name, enabled=0)

        fourth = self.grid_block(self.category.name, block_name="_G25 Off Live")

        self.assertTrue(self.place("cart", "below_cart", fourth.name).name)

    def test_the_route_budget_is_the_page_budget(self):
        """One constant, so the two mechanisms cannot be tuned apart."""

        from yob_storefront.utils.storefront_content import MAX_PRODUCT_GRIDS
        from yob_storefront.yob_storefront.doctype.yob_storefront_page \
            .yob_storefront_page import MAX_PRODUCT_GRIDS as PAGE_LIMIT

        self.assertIs(MAX_PRODUCT_GRIDS, PAGE_LIMIT)


# =========================================================
# THE RUNTIME
# =========================================================

class RouteRuntimeCase(PlacementBase):

    def test_a_route_returns_every_declared_slot(self):
        data = self.data(self.route("cart"))

        self.assertEqual(data["route_key"], "cart")
        self.assertEqual([s["key"] for s in data["slots"]], ["above_cart", "below_cart"])

    def test_a_route_with_no_placements_is_not_an_error(self):
        slots = self.slots_of("account")

        self.assertEqual(slots, {"above_content": [], "below_content": []})

    def test_a_block_appears_in_its_own_slot_only(self):
        block = self.make_block(block_name="_G25 Only Above")
        self.place("cart", "above_cart", block.name)

        slots = self.slots_of("cart")

        self.assertEqual([b["block_name"] for b in slots["above_cart"]], ["_G25 Only Above"])
        self.assertEqual(slots["below_cart"], [])

    def test_a_disabled_placement_is_kept_but_not_published(self):
        block = self.make_block(block_name="_G25 Hidden")
        placement = self.place("cart", "above_cart", block.name, enabled=0)

        self.assertTrue(frappe.db.exists(PLACEMENT, placement.name), "the record was lost")
        self.assertEqual(self.slots_of("cart")["above_cart"], [])

    def test_a_disabled_block_is_omitted_and_nothing_is_substituted(self):
        live = self.make_block(block_name="_G25 Live")
        dead = self.make_block(block_name="_G25 Dead", enabled=0)

        self.place("cart", "above_cart", live.name, sequence=1)
        self.place("cart", "above_cart", dead.name, sequence=2)

        names = [b["block_name"] for b in self.slots_of("cart")["above_cart"]]

        self.assertEqual(names, ["_G25 Live"], "a non-renderable block was substituted")

    def test_an_unknown_route_is_refused_and_never_remapped(self):
        response = self.route("checkout")

        self.assertEqual(self.code_of(response), "content_route_unknown")

    def test_a_missing_route_is_a_validation_error(self):
        self.assertEqual(self.code_of(self.route(None)), "validation_failed")

    def test_every_registered_route_answers(self):
        for route_key in SYSTEM_CONTENT_SLOTS:
            data = self.data(self.route(route_key))

            self.assertEqual(data["route_key"], route_key)
            self.assertEqual([s["key"] for s in data["slots"]],
                             list(dict(SYSTEM_CONTENT_SLOTS[route_key][1])))


# =========================================================
# ONE PROJECTOR, TWO MECHANISMS
# =========================================================

class ProjectionParityCase(PlacementBase):
    """The same Block, through a Page and through a Route, must be identical.

    This is the assertion that stops a second block projector appearing. If
    anyone ever writes route-specific block logic, the two payloads diverge and
    this fails -- which is the only cheap way to notice.
    """

    def page_and_route(self, block):
        frappe.get_doc({
            "doctype": "YOB Storefront Page", "slug": "g25-parity", "title": "Parity",
            "enabled": 1, "blocks": [{"block": block.name}],
        }).insert(ignore_permissions=True)

        self.place("home", "hero", block.name)

        from_page = self.data(self.page("g25-parity"))["blocks"][0]
        from_route = self.slots_of("home")["hero"][0]

        return from_page, from_route

    def test_image_banner_is_identical(self):
        block = self.make_block(
            "Image Banner", block_name="_G25 Banner", desktop_image="/files/d.png",
            mobile_image="/files/m.png", alt_text="alt", desktop_height_px=400,
            mobile_height_px=200, link_type="Catalog")

        page, route = self.page_and_route(block)

        self.assertEqual(page, route)
        self.assertEqual(route["type"], "image_banner")

    def test_rich_text_is_identical(self):
        block = self.make_block("Rich Text", block_name="_G25 Text",
                                content_title="T", content="<p>x</p>",
                                text_alignment="Center")

        page, route = self.page_and_route(block)

        self.assertEqual(page, route)
        self.assertEqual(route["type"], "rich_text")

    def test_banner_carousel_is_identical(self):
        block = self.make_block(
            "Banner Carousel", block_name="_G25 Carousel", auto_play=1,
            interval_ms=4000, desktop_height_px=400, mobile_height_px=200,
            slides=[{"desktop_image": "/files/s.png", "title": "S",
                     "link_type": "Catalog"}])

        page, route = self.page_and_route(block)

        self.assertEqual(page, route)
        self.assertEqual(route["type"], "banner_carousel")
        self.assertEqual(len(route["slides"]), 1)

    def test_promo_grid_is_identical(self):
        block = self.make_block(
            "Promo Grid", block_name="_G25 Promo", cards_per_row="3",
            desktop_height_px=300, mobile_height_px=200,
            promo_cards=[{"desktop_image": "/files/c.png", "title": "C",
                          "link_type": "Catalog"}])

        page, route = self.page_and_route(block)

        self.assertEqual(page, route)
        self.assertEqual(route["type"], "promo_grid")

    def test_product_grid_is_identical(self):
        category = self.make_category("g25-parity-cat")
        self.make_item("_G25-PARITY-1", category.name, price=150)

        block = self.grid_block(category.name, block_name="_G25 Parity Grid")

        page, route = self.page_and_route(block)

        self.assertEqual(page, route)
        self.assertEqual(route["type"], "product_grid")
        self.assertTrue(route["items"], "the grid rendered no products")


# =========================================================
# PRODUCT GRID RUNTIME
# =========================================================

class RouteProductGridCase(PlacementBase):

    def setUp(self):
        super().setUp()
        self.category = self.make_category("g25-runtime-cat")
        self.simple = self.make_item("_G25-SIMPLE", self.category.name, price=150)

    def test_the_grid_calls_the_existing_listing_service(self):
        from yob_storefront.services import catalog_listing_service as svc

        grid = self.grid_block(self.category.name, block_name="_G25 Uses List")
        self.place("cart", "above_cart", grid.name)

        with patch.object(svc, "list_items", wraps=svc.list_items) as spy:
            self.slots_of("cart")

        self.assertTrue(spy.called, "the route grid did not use list_items()")

    def test_a_simple_item_is_priced_normally(self):
        grid = self.grid_block(self.category.name, block_name="_G25 Priced")
        self.place("cart", "above_cart", grid.name)

        card = next(c for c in self.slots_of("cart")["above_cart"][0]["items"]
                    if c["name"] == self.simple.name)

        self.assertEqual(card["price_state"], "priced")
        self.assertEqual(flt(card["rate"]), 150.0)

    def test_a_family_stays_select_options(self):
        from erpnext.controllers.item_variant import create_variant

        for attribute in ("Colour", "Size"):
            if not frappe.db.exists("Item Attribute", attribute):
                self.skipTest(f"Item Attribute {attribute!r} is not configured here")

        for attribute, value in (("Colour", "Red"), ("Size", "Medium")):
            if not frappe.db.exists("Item Attribute Value",
                                    {"parent": attribute, "attribute_value": value}):
                doc = frappe.get_doc("Item Attribute", attribute)
                doc.append("item_attribute_values",
                           {"attribute_value": value, "abbr": value[:3].upper()})
                doc.save(ignore_permissions=True)
                frappe.clear_document_cache("Item Attribute", attribute)
        frappe.flags.attribute_values = None

        template = frappe.get_doc({
            "doctype": "Item", "item_code": "_G25-FAMILY", "item_name": "_G25-FAMILY",
            "item_group": self.item_group, "stock_uom": self.uom, "is_stock_item": 0,
            "is_sales_item": 1, "gst_hsn_code": self.hsn, "custom_slug": "_g25-family",
            "custom_category": self.category.name, "has_variants": 1,
            "attributes": [{"attribute": "Colour"}, {"attribute": "Size"}],
        }).insert(ignore_permissions=True)

        variant = create_variant(template.name, {"Colour": "Red", "Size": "Medium"})
        variant.insert(ignore_permissions=True)
        frappe.get_doc({
            "doctype": "Item Price", "item_code": variant.name,
            "price_list": self.price_list, "price_list_rate": 900,
            "selling": 1, "uom": self.uom}).insert(ignore_permissions=True)

        grid = self.grid_block(self.category.name, block_name="_G25 Family Grid")
        self.place("cart", "above_cart", grid.name)
        frappe.clear_cache()

        cards = {c["name"]: c for c in self.slots_of("cart")["above_cart"][0]["items"]}

        self.assertIn(template.name, cards, "the family card is missing")
        self.assertEqual(cards[template.name]["price_state"], "select_options")
        self.assertIsNone(cards[template.name]["rate"],
                          "a family borrowed a child variant's price")
        self.assertNotIn(variant.name, cards, "a generated variant was merchandised")

    def test_the_placement_layer_implements_no_pricing_of_its_own(self):
        """An executable-code scan, the same guard `content_service` carries."""

        import ast
        import pathlib

        for relative in ("utils/system_slots.py",
                         "yob_storefront/doctype/yob_storefront_content_placement"
                         "/yob_storefront_content_placement.py"):
            path = pathlib.Path(frappe.get_app_path("yob_storefront")) / relative
            tree = ast.parse(path.read_text())

            # Comments and docstrings may DISCUSS pricing; executable code may not
            # perform it. Strings are stripped so prose cannot fail the scan.
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    node.value = ""

            source = ast.dump(tree)

            for forbidden in ("Item Price", "Pricing Rule", "get_price_list_rate",
                              "conversion_factor", "actual_qty", "SellingContext"):
                self.assertNotIn(
                    forbidden, source,
                    f"{relative} performs pricing; that belongs to the catalogue")

    def test_two_customers_can_receive_different_grid_prices(self):
        other = frappe.get_doc({
            "doctype": "Customer", "customer_name": "_G25 Other Buyer",
            "customer_group": self.customer.customer_group,
            "territory": self.customer.territory}).insert(ignore_permissions=True).name

        premium = frappe.get_doc({
            "doctype": "Price List", "price_list_name": "_G25 Premium",
            "selling": 1, "enabled": 1, "currency": "INR"}).insert(ignore_permissions=True)
        frappe.get_doc({
            "doctype": "Item Price", "item_code": self.simple.name,
            "price_list": premium.name, "price_list_rate": 777,
            "selling": 1, "uom": self.uom}).insert(ignore_permissions=True)
        frappe.db.set_value("Customer", other, "default_price_list", premium.name)

        grid = self.grid_block(self.category.name, block_name="_G25 Customer Grid")
        self.place("cart", "above_cart", grid.name)

        first = self.slots_of("cart")["above_cart"][0]["items"][0]["rate"]

        with patch.object(self.cms, "get_storefront_customer",
                          return_value=frappe.get_doc("Customer", other)):
            frappe.clear_cache()
            second = self.slots_of("cart")["above_cart"][0]["items"][0]["rate"]

        self.assertEqual(flt(first), 150.0)
        self.assertEqual(flt(second), 777.0,
                         "a second customer received the first customer's price")

    def test_the_hydrated_response_is_never_globally_cached(self):
        """No RESPONSE cache may hold a route's customer-priced answer.

        Targeted at the response-cache APIs specifically -- `frappe.cache()` and
        the app's own `utils.cache` -- rather than the substring "cache", because
        `frappe.get_cached_doc` is a per-document read that the projector uses
        legitimately and that shares no data between customers.

        The risk this guards is real and cheap to introduce: a route's structure
        looks eminently cacheable right up until a slot holds a Product Grid, at
        which point the response carries one buyer's prices to every other buyer.
        """

        import ast
        import pathlib

        path = (pathlib.Path(frappe.get_app_path("yob_storefront"))
                / "services" / "content_service.py")
        tree = ast.parse(path.read_text())

        offenders = []

        for node in ast.walk(tree):
            # `frappe.cache()` -- the request-independent store
            if (isinstance(node, ast.Attribute) and node.attr == "cache"
                    and isinstance(node.value, ast.Name) and node.value.id == "frappe"):
                offenders.append("frappe.cache")

            # the app's own cache helpers
            if isinstance(node, ast.ImportFrom) and "utils.cache" in (node.module or ""):
                offenders.append(node.module)

        self.assertEqual(
            offenders, [],
            "route and page content must not be cached across customers")

    def test_the_projector_is_shared_rather_than_reimplemented(self):
        """`route_content` must call `project_block`, not carry its own copy."""

        import ast
        import inspect as py_inspect

        from yob_storefront.services import content_service

        tree = ast.parse(py_inspect.getsource(content_service.route_content))

        called = {node.func.id for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}

        self.assertIn("project_block", called,
                      "route content built blocks some other way")

        # ...and it must not know any block TYPE by name. A type mentioned here
        # would be the first line of a second projector.
        source = py_inspect.getsource(content_service.route_content)

        for block_type in ("image_banner", "rich_text", "banner_carousel",
                           "product_grid", "promo_grid"):
            self.assertNotIn(
                f'"{block_type}"', source,
                f"route content names {block_type}; block types belong to the projector")


if __name__ == "__main__":
    unittest.main()
