# Copyright (c) 2026, YOB and Shayona
"""Section styles belong to the PLACEMENT, not the Block (Phase 25I).

THE ONE FACT THIS FILE EXISTS FOR
---------------------------------
A `YOB Storefront Block` is authored once and placed many times. Its band -- the
full-width strip behind it -- is a property of WHERE it was placed, so the same
`Welcome Text` can be muted on an About page and dark on the home route without
being duplicated. `ReuseCase` proves exactly that, on one Block, in one test.

WHAT THE BACKEND DOES NOT DECIDE
--------------------------------
What `dark` looks like. Colour, padding, breakpoints, text colour, the fixed
content width and the full-bleed behaviour are all Angular's, in source-controlled
CSS. The backend stores one of five approved words and refuses everything else,
which is what keeps this a controlled vocabulary rather than a CSS field.
"""

import inspect
import unittest
from unittest.mock import patch

import frappe

from yob_storefront.utils.section_styles import SECTION_STYLES

CUSTOMER = "YOB Demo Buyer"
SEED_ITEM = "YOB-BOLT-M10"
PLACEMENT = "YOB Storefront Content Placement"


def _seeded():
    return bool(frappe.db.exists("Customer", CUSTOMER) and frappe.db.exists("Item", SEED_ITEM))


class StyleBase(unittest.TestCase):
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
               "block_name": block_name or f"_I25 {block_type} {frappe.generate_hash(length=6)}",
               "block_type": block_type, "enabled": 1}

        if block_type == "Rich Text":
            doc.setdefault("content", "<p>hello</p>")
        if block_type == "Image Banner":
            doc.setdefault("desktop_image", "/files/i25.png")

        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True)

    def make_page(self, slug, blocks):
        return frappe.get_doc({
            "doctype": "YOB Storefront Page", "slug": slug, "title": "I25",
            "enabled": 1, "blocks": blocks}).insert(ignore_permissions=True)

    def place(self, route_key, slot_key, block, **kw):
        doc = {"doctype": PLACEMENT, "route_key": route_key, "slot_key": slot_key,
               "block": block, "enabled": 1}
        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True)

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

    # ------------------------------------------------------------- the wire

    def page_blocks(self, slug):
        frappe.clear_cache()
        response = inspect.unwrap(self.cms.get_page)(auth_context={}, slug=slug)
        self.assertNotIn("errors", response, response)
        return response["data"]["blocks"]

    def route_slots(self, route_key):
        frappe.clear_cache()
        response = inspect.unwrap(self.cms.get_route_content)(
            auth_context={}, route_key=route_key)
        self.assertNotIn("errors", response, response)
        return {s["key"]: s["blocks"] for s in response["data"]["slots"]}


# =========================================================
# THE MODEL
# =========================================================

class StyleModelCase(StyleBase):

    def test_a_page_block_defaults_to_default(self):
        block = self.make_block()
        self.make_page("i25-default", [{"block": block.name}])

        self.assertEqual(self.page_blocks("i25-default")[0]["section_style"], "default")

    def test_a_placement_defaults_to_default(self):
        block = self.make_block()
        placement = self.place("home", "hero", block.name)

        self.assertEqual(placement.section_style, "default")

    def test_every_registered_style_is_accepted(self):
        for style in SECTION_STYLES:
            block = self.make_block(block_name=f"_I25 OK {style}")
            placement = self.place("home", "hero", block.name, section_style=style)

            self.assertEqual(placement.section_style, style)

    def test_a_tailwind_class_is_refused(self):
        block = self.make_block()

        with self.assertRaises(frappe.ValidationError):
            self.place("home", "hero", block.name, section_style="bg-red-500")

    def test_a_css_declaration_is_refused(self):
        block = self.make_block()

        with self.assertRaises(frappe.ValidationError):
            self.place("home", "hero", block.name, section_style="background:#fff")

    def test_arbitrary_text_is_refused(self):
        block = self.make_block()

        with self.assertRaises(frappe.ValidationError):
            self.place("home", "hero", block.name, section_style="absolute")

    def test_a_page_block_refuses_an_unknown_style(self):
        block = self.make_block()

        with self.assertRaises(frappe.ValidationError):
            self.make_page("i25-bad",
                           [{"block": block.name, "section_style": "bg-red-500"}])

    def test_a_historical_blank_row_projects_as_default(self):
        """A row written before the field existed must render as it always did.

        Written straight to the database so the Desk default cannot fill it in --
        this is what an existing site's rows genuinely look like after migrate.
        """

        block = self.make_block()
        page = self.make_page("i25-legacy", [{"block": block.name}])

        frappe.db.set_value("YOB Storefront Page Block", page.blocks[0].name,
                            "section_style", None, update_modified=False)

        self.assertEqual(self.page_blocks("i25-legacy")[0]["section_style"], "default")

    def test_a_historical_blank_placement_projects_as_default(self):
        block = self.make_block()
        placement = self.place("home", "hero", block.name)

        frappe.db.set_value(PLACEMENT, placement.name, "section_style", None,
                            update_modified=False)

        self.assertEqual(self.route_slots("home")["hero"][0]["section_style"], "default")

    def test_the_field_offers_exactly_the_registry(self):
        for doctype in ("YOB Storefront Page Block", PLACEMENT):
            field = frappe.get_meta(doctype).get_field("section_style")

            self.assertIsNotNone(field, f"{doctype} has no section_style")
            self.assertEqual(field.fieldtype, "Select")
            self.assertEqual(
                [line for line in (field.options or "").split("\n") if line],
                list(SECTION_STYLES),
                f"{doctype} offers something other than the registry")
            self.assertEqual(field.default, "default")

    def test_the_style_is_not_on_the_reusable_block(self):
        """The whole point: presentation must not be a property of the content."""

        self.assertIsNone(
            frappe.get_meta("YOB Storefront Block").get_field("section_style"),
            "section_style leaked onto the Block; two placements could no longer differ")


# =========================================================
# THE PROOF: ONE BLOCK, TWO BANDS
# =========================================================

class ReuseCase(StyleBase):

    def test_one_block_projects_with_different_styles_in_two_placements(self):
        """The same Rich Text, muted on a page and dark on a route."""

        block = self.make_block("Rich Text", block_name="_I25 Welcome",
                                content_title="Welcome", content="<p>Hello</p>",
                                text_alignment="Left")

        self.make_page("i25-about",
                       [{"block": block.name, "section_style": "muted"}])
        self.place("home", "hero", block.name, section_style="dark")

        from_page = self.page_blocks("i25-about")[0]
        from_route = self.route_slots("home")["hero"][0]

        self.assertEqual(from_page["section_style"], "muted")
        self.assertEqual(from_route["section_style"], "dark")

        # ...and NOTHING else about the block differs. One projector, one payload;
        # only the band changed.
        self.assertEqual({k: v for k, v in from_page.items() if k != "section_style"},
                         {k: v for k, v in from_route.items() if k != "section_style"},
                         "the same Block projected two different payloads")

    def test_two_placements_of_one_block_on_the_same_route_may_differ(self):
        block = self.make_block(block_name="_I25 Twice")

        self.place("cart", "above_cart", block.name, section_style="accent")
        self.place("cart", "below_cart", block.name, section_style="brand_soft")

        slots = self.route_slots("cart")

        self.assertEqual(slots["above_cart"][0]["section_style"], "accent")
        self.assertEqual(slots["below_cart"][0]["section_style"], "brand_soft")

    def test_changing_a_placement_style_does_not_touch_the_block(self):
        block = self.make_block(block_name="_I25 Untouched")
        before = frappe.db.get_value("YOB Storefront Block", block.name, "modified")

        self.place("home", "hero", block.name, section_style="dark")

        self.assertEqual(
            frappe.db.get_value("YOB Storefront Block", block.name, "modified"), before,
            "placing a block with a style modified the reusable Block")


# =========================================================
# EVERY BLOCK TYPE CARRIES IT
# =========================================================

class AllTypesCase(StyleBase):

    def blocks_of_every_type(self):
        category = self.make_category("i25-types-cat")
        self.make_item("_I25-TYPES-1", category.name)
        media = {"desktop_image": "/files/i.png", "link_type": "Catalog"}

        return [
            self.make_block("Image Banner", block_name="_I25 IB",
                            desktop_image="/files/i.png", link_type="Catalog"),
            self.make_block("Rich Text", block_name="_I25 RT", content="<p>x</p>"),
            self.make_block("Banner Carousel", block_name="_I25 BC",
                            slides=[dict(media)]),
            self.make_block("Product Grid", block_name="_I25 PG",
                            storefront_category=category.name, item_limit=6,
                            card_type="Square", sort_by="Name A-Z"),
            self.make_block("Promo Grid", block_name="_I25 PM", cards_per_row="2",
                            promo_cards=[dict(media)]),
        ]

    def test_all_five_types_publish_the_style_on_a_page(self):
        blocks = self.blocks_of_every_type()
        self.make_page("i25-all", [{"block": b.name, "section_style": "accent"}
                                   for b in blocks])

        projected = self.page_blocks("i25-all")

        self.assertEqual(len(projected), 5)
        for block in projected:
            self.assertEqual(block["section_style"], "accent",
                             f"{block['type']} lost its section style")

    def test_all_five_types_publish_the_style_on_a_route(self):
        blocks = self.blocks_of_every_type()

        # Three Product Grids is the route budget, and only one is a grid here.
        for index, block in enumerate(blocks):
            self.place("home", "main", block.name, section_style="dark",
                       sequence=index)

        projected = self.route_slots("home")["main"]

        self.assertEqual(len(projected), 5)
        for block in projected:
            self.assertEqual(block["section_style"], "dark")

    def test_no_block_type_projector_knows_about_styles(self):
        """Presentation is wrapped around a payload, never branched inside one.

        A projector that read `section_style` would be the first line of
        style-specific business logic -- deciding that `dark` means white text is
        Angular's job and must never migrate into the backend.
        """

        import ast
        import pathlib

        path = (pathlib.Path(frappe.get_app_path("yob_storefront"))
                / "services" / "content_service.py")
        tree = ast.parse(path.read_text())

        offenders = []

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("_"):
                body = ast.dump(node)
                for style in SECTION_STYLES:
                    if f"'{style}'" in body:
                        offenders.append(f"{node.name} mentions {style}")

        self.assertEqual(offenders, [], "; ".join(offenders))


# =========================================================
# REGRESSION
# =========================================================

class StyleRegressionCase(StyleBase):

    def setUp(self):
        super().setUp()
        self.category = self.make_category("i25-reg-cat")
        self.simple = self.make_item("_I25-SIMPLE", self.category.name, price=150)

    def grid(self, **kw):
        return self.make_block("Product Grid", storefront_category=self.category.name,
                               item_limit=6, card_type="Square", sort_by="Name A-Z", **kw)

    def test_a_styled_product_grid_still_uses_the_listing_service(self):
        from yob_storefront.services import catalog_listing_service as svc

        block = self.grid(block_name="_I25 Grid")
        self.place("cart", "above_cart", block.name, section_style="dark")

        with patch.object(svc, "list_items", wraps=svc.list_items) as spy:
            slots = self.route_slots("cart")

        self.assertTrue(spy.called, "the grid stopped using list_items()")
        self.assertEqual(slots["above_cart"][0]["section_style"], "dark")

    def test_a_styled_grid_prices_a_simple_item_unchanged(self):
        from frappe.utils import flt

        block = self.grid(block_name="_I25 Grid Priced")
        self.place("cart", "above_cart", block.name, section_style="muted")

        card = next(c for c in self.route_slots("cart")["above_cart"][0]["items"]
                    if c["name"] == self.simple.name)

        self.assertEqual(card["price_state"], "priced")
        self.assertEqual(flt(card["rate"]), 150.0)

    def test_styling_adds_no_pricing_calls(self):
        """A band is metadata. It must cost nothing."""

        from yob_storefront.services import catalog_listing_service as svc

        plain = self.grid(block_name="_I25 Plain")
        self.place("cart", "above_cart", plain.name)

        with patch.object(svc, "price_candidate", wraps=svc.price_candidate) as spy:
            self.route_slots("cart")
        without = spy.call_count

        frappe.db.set_value(PLACEMENT,
                            frappe.db.get_value(PLACEMENT, {"block": plain.name}),
                            "section_style", "dark")

        with patch.object(svc, "price_candidate", wraps=svc.price_candidate) as spy:
            self.route_slots("cart")
        with_style = spy.call_count

        self.assertEqual(without, with_style,
                         "a section style changed how many products were priced")

    def test_ordering_is_unchanged_by_styles(self):
        first = self.make_block(block_name="_I25 One")
        second = self.make_block(block_name="_I25 Two")
        third = self.make_block(block_name="_I25 Three")

        self.make_page("i25-order", [
            {"block": second.name, "sequence": 20, "section_style": "dark"},
            {"block": first.name, "sequence": 10},
            {"block": third.name, "sequence": 30, "section_style": "muted"},
        ])

        names = [b["block_name"] for b in self.page_blocks("i25-order")]

        self.assertEqual(names, ["_I25 One", "_I25 Two", "_I25 Three"])

    def test_a_disabled_placement_is_still_omitted(self):
        block = self.make_block(block_name="_I25 Off")
        self.place("home", "hero", block.name, section_style="dark", enabled=0)

        self.assertEqual(self.route_slots("home")["hero"], [])

    def test_page_and_route_still_share_one_projector(self):
        import ast
        import inspect as py_inspect

        from yob_storefront.services import content_service

        for func in (content_service.get_page, content_service.route_content):
            tree = ast.parse(py_inspect.getsource(func))
            called = {n.func.id for n in ast.walk(tree)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}

            self.assertIn("project_block", called,
                          f"{func.__name__} no longer uses the shared projector")


if __name__ == "__main__":
    unittest.main()
