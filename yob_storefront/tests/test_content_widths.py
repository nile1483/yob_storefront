# Copyright (c) 2026, YOB and Shayona
"""Content width belongs to the PLACEMENT, and is independent of style (25K).

TWO AXES, NOT ONE
-----------------
`section_style` says what the full-width band looks like. `content_width` says
whether the block spans that band or stays inside the fixed container. They are
separate questions about separate elements, and `IndependenceCase` proves every
combination is storable and projectable -- deriving one from the other would
silently take away a choice a merchant is entitled to make.

THE REUSE RULE, AGAIN
---------------------
Width is a property of WHERE a block was placed, so the identical hero Banner can
run full width on the home route and contained inside a dynamic page without
being duplicated. That is the same rule `section_style` follows, proved here
against the second key.
"""

import inspect
import unittest
from unittest.mock import patch

import frappe

from yob_storefront.utils.content_widths import CONTENT_WIDTHS
from yob_storefront.utils.section_styles import SECTION_STYLES

CUSTOMER = "YOB Demo Buyer"
SEED_ITEM = "YOB-BOLT-M10"
PLACEMENT = "YOB Storefront Content Placement"


def _seeded():
    return bool(frappe.db.exists("Customer", CUSTOMER) and frappe.db.exists("Item", SEED_ITEM))


class WidthBase(unittest.TestCase):
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
               "block_name": block_name or f"_K25 {block_type} {frappe.generate_hash(length=6)}",
               "block_type": block_type, "enabled": 1}

        if block_type == "Rich Text":
            doc.setdefault("content", "<p>hello</p>")
        if block_type == "Image Banner":
            doc.setdefault("desktop_image", "/files/k25.png")

        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True)

    def make_page(self, slug, blocks):
        return frappe.get_doc({
            "doctype": "YOB Storefront Page", "slug": slug, "title": "K25",
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

class WidthModelCase(WidthBase):

    def test_a_page_block_defaults_to_contained(self):
        block = self.make_block()
        self.make_page("k25-default", [{"block": block.name}])

        self.assertEqual(self.page_blocks("k25-default")[0]["content_width"], "contained")

    def test_a_placement_defaults_to_contained(self):
        block = self.make_block()

        self.assertEqual(self.place("home", "hero", block.name).content_width, "contained")

    def test_both_registered_widths_are_accepted(self):
        for width in CONTENT_WIDTHS:
            block = self.make_block(block_name=f"_K25 OK {width}")
            placement = self.place("home", "hero", block.name, content_width=width)

            self.assertEqual(placement.content_width, width)

    def test_a_percentage_is_refused(self):
        block = self.make_block()

        with self.assertRaises(frappe.ValidationError):
            self.place("home", "hero", block.name, content_width="100%")

    def test_a_viewport_unit_is_refused(self):
        block = self.make_block()

        with self.assertRaises(frappe.ValidationError):
            self.place("home", "hero", block.name, content_width="100vw")

    def test_a_tailwind_class_is_refused(self):
        block = self.make_block()

        with self.assertRaises(frappe.ValidationError):
            self.place("home", "hero", block.name, content_width="max-w-none")

    def test_arbitrary_text_is_refused(self):
        block = self.make_block()

        with self.assertRaises(frappe.ValidationError):
            self.place("home", "hero", block.name, content_width="wide")

    def test_a_page_block_refuses_an_unknown_width(self):
        block = self.make_block()

        with self.assertRaises(frappe.ValidationError):
            self.make_page("k25-bad", [{"block": block.name, "content_width": "100%"}])

    def test_a_historical_blank_page_row_projects_as_contained(self):
        """A row written before the field existed must render as it always did."""

        block = self.make_block()
        page = self.make_page("k25-legacy", [{"block": block.name}])

        frappe.db.set_value("YOB Storefront Page Block", page.blocks[0].name,
                            "content_width", None, update_modified=False)

        self.assertEqual(self.page_blocks("k25-legacy")[0]["content_width"], "contained")

    def test_a_historical_blank_placement_projects_as_contained(self):
        block = self.make_block()
        placement = self.place("home", "hero", block.name)

        frappe.db.set_value(PLACEMENT, placement.name, "content_width", None,
                            update_modified=False)

        self.assertEqual(self.route_slots("home")["hero"][0]["content_width"], "contained")

    def test_the_field_offers_exactly_the_registry(self):
        for doctype in ("YOB Storefront Page Block", PLACEMENT):
            field = frappe.get_meta(doctype).get_field("content_width")

            self.assertIsNotNone(field, f"{doctype} has no content_width")
            self.assertEqual(field.fieldtype, "Select")
            self.assertEqual(
                [line for line in (field.options or "").split("\n") if line],
                list(CONTENT_WIDTHS))
            self.assertEqual(field.default, "contained")

    def test_only_two_widths_exist(self):
        """No narrow/wide/boxed/fluid. A third gets added deliberately."""

        self.assertEqual(list(CONTENT_WIDTHS), ["contained", "full_width"])

    def test_the_width_is_not_on_the_reusable_block(self):
        self.assertIsNone(
            frappe.get_meta("YOB Storefront Block").get_field("content_width"),
            "content_width leaked onto the Block; two placements could no longer differ")


# =========================================================
# THE PROOF: ONE BLOCK, TWO WIDTHS
# =========================================================

class ReuseCase(WidthBase):

    def test_one_banner_runs_full_width_on_a_route_and_contained_on_a_page(self):
        block = self.make_block("Image Banner", block_name="_K25 Hero",
                                desktop_image="/files/hero.png",
                                mobile_image="/files/hero-sm.png",
                                alt_text="Hero", link_type="Catalog")

        self.place("home", "hero", block.name,
                   section_style="default", content_width="full_width")
        self.make_page("k25-about", [{"block": block.name,
                                      "section_style": "muted",
                                      "content_width": "contained"}])

        from_route = self.route_slots("home")["hero"][0]
        from_page = self.page_blocks("k25-about")[0]

        self.assertEqual(from_route["content_width"], "full_width")
        self.assertEqual(from_page["content_width"], "contained")
        self.assertEqual(from_route["section_style"], "default")
        self.assertEqual(from_page["section_style"], "muted")

        # ...and the BLOCK's own payload is identical in both. Only the two
        # placement keys differ; one Block, one content, two presentations.
        placement_keys = {"section_style", "content_width"}
        self.assertEqual(
            {k: v for k, v in from_route.items() if k not in placement_keys},
            {k: v for k, v in from_page.items() if k not in placement_keys},
            "the same Block projected two different contents")

    def test_two_placements_on_one_route_may_differ_in_width(self):
        block = self.make_block(block_name="_K25 Twice")

        self.place("cart", "above_cart", block.name, content_width="full_width")
        self.place("cart", "below_cart", block.name, content_width="contained")

        slots = self.route_slots("cart")

        self.assertEqual(slots["above_cart"][0]["content_width"], "full_width")
        self.assertEqual(slots["below_cart"][0]["content_width"], "contained")

    def test_changing_a_placement_width_does_not_modify_the_block(self):
        block = self.make_block(block_name="_K25 Untouched")
        before = frappe.db.get_value("YOB Storefront Block", block.name, "modified")

        self.place("home", "hero", block.name, content_width="full_width")

        self.assertEqual(
            frappe.db.get_value("YOB Storefront Block", block.name, "modified"), before,
            "placing a block full width modified the reusable Block")


# =========================================================
# INDEPENDENCE
# =========================================================

class IndependenceCase(WidthBase):

    def test_every_style_and_width_combination_is_storable_and_projected(self):
        """Neither key constrains the other -- all ten pairs are legitimate."""

        slots = ("above_cart", "below_cart")
        expected = {}

        for index, style in enumerate(SECTION_STYLES):
            for offset, width in enumerate(CONTENT_WIDTHS):
                block = self.make_block(block_name=f"_K25 {style} {width}")
                slot = slots[(index + offset) % len(slots)]

                self.place("cart", slot, block.name, section_style=style,
                           content_width=width, sequence=index * 10 + offset)
                expected[block.block_name] = (style, width)

        seen = {}
        for blocks in self.route_slots("cart").values():
            for b in blocks:
                seen[b["block_name"]] = (b["section_style"], b["content_width"])

        self.assertEqual(seen, expected,
                         "a style/width pair did not survive the round trip")

    def test_width_is_not_derived_from_style(self):
        dark_full = self.make_block(block_name="_K25 Dark Full")
        dark_contained = self.make_block(block_name="_K25 Dark Contained")

        self.place("home", "hero", dark_full.name,
                   section_style="dark", content_width="full_width")
        self.place("home", "main", dark_contained.name,
                   section_style="dark", content_width="contained")

        slots = self.route_slots("home")

        self.assertEqual(slots["hero"][0]["content_width"], "full_width")
        self.assertEqual(slots["main"][0]["content_width"], "contained")
        self.assertEqual(slots["hero"][0]["section_style"], "dark")
        self.assertEqual(slots["main"][0]["section_style"], "dark")


# =========================================================
# EVERY BLOCK TYPE
# =========================================================

class AllTypesCase(WidthBase):

    def blocks_of_every_type(self):
        category = self.make_category("k25-types-cat")
        self.make_item("_K25-TYPES-1", category.name)
        media = {"desktop_image": "/files/k.png", "link_type": "Catalog"}

        return [
            self.make_block("Image Banner", block_name="_K25 IB",
                            desktop_image="/files/k.png", link_type="Catalog"),
            self.make_block("Rich Text", block_name="_K25 RT", content="<p>x</p>"),
            self.make_block("Banner Carousel", block_name="_K25 BC",
                            slides=[dict(media)]),
            self.make_block("Product Grid", block_name="_K25 PG",
                            storefront_category=category.name, item_limit=6,
                            card_type="Square", sort_by="Name A-Z"),
            self.make_block("Promo Grid", block_name="_K25 PM", cards_per_row="2",
                            promo_cards=[dict(media)]),
        ]

    def test_full_width_is_available_to_all_five_types(self):
        """Not restricted to banners: a merchant may want a full-width grid."""

        blocks = self.blocks_of_every_type()
        self.make_page("k25-all", [{"block": b.name, "content_width": "full_width"}
                                   for b in blocks])

        projected = self.page_blocks("k25-all")

        self.assertEqual(len(projected), 5)
        for block in projected:
            self.assertEqual(block["content_width"], "full_width",
                             f"{block['type']} lost its content width")

    def test_all_five_types_publish_the_width_on_a_route(self):
        blocks = self.blocks_of_every_type()

        for index, block in enumerate(blocks):
            self.place("home", "main", block.name, content_width="full_width",
                       sequence=index)

        projected = self.route_slots("home")["main"]

        self.assertEqual(len(projected), 5)
        for block in projected:
            self.assertEqual(block["content_width"], "full_width")

    def test_no_block_type_projector_interprets_a_width(self):
        """Containment is wrapped around a payload, never branched inside one."""

        import ast
        import pathlib

        path = (pathlib.Path(frappe.get_app_path("yob_storefront"))
                / "services" / "content_service.py")
        tree = ast.parse(path.read_text())

        offenders = []

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("_"):
                body = ast.dump(node)
                for width in CONTENT_WIDTHS:
                    if f"'{width}'" in body:
                        offenders.append(f"{node.name} mentions {width}")

        self.assertEqual(offenders, [], "; ".join(offenders))


# =========================================================
# REGRESSION
# =========================================================

class WidthRegressionCase(WidthBase):

    def setUp(self):
        super().setUp()
        self.category = self.make_category("k25-reg-cat")
        self.simple = self.make_item("_K25-SIMPLE", self.category.name, price=150)

    def grid(self, **kw):
        return self.make_block("Product Grid", storefront_category=self.category.name,
                               item_limit=6, card_type="Square", sort_by="Name A-Z", **kw)

    def projected_grid(self, width):
        """The same grid placement, projected at one width."""

        frappe.db.sql("DELETE FROM `tabYOB Storefront Content Placement`")
        block = frappe.db.get_value("YOB Storefront Block", {"block_name": "_K25 Grid"})

        if not block:
            block = self.grid(block_name="_K25 Grid").name

        self.place("cart", "above_cart", block, content_width=width)

        from yob_storefront.services import catalog_listing_service as svc

        with patch.object(svc, "price_candidate", wraps=svc.price_candidate) as spy:
            blocks = self.route_slots("cart")["above_cart"]

        return blocks[0], spy.call_count

    def test_contained_and_full_width_differ_only_in_that_field(self):
        contained, priced_contained = self.projected_grid("contained")
        full, priced_full = self.projected_grid("full_width")

        self.assertEqual(contained["content_width"], "contained")
        self.assertEqual(full["content_width"], "full_width")

        self.assertEqual({k: v for k, v in contained.items() if k != "content_width"},
                         {k: v for k, v in full.items() if k != "content_width"},
                         "a full-width grid returned different products or prices")

        self.assertEqual(priced_contained, priced_full,
                         "changing content width changed how many products were priced")

    def test_a_full_width_grid_still_uses_the_listing_service(self):
        from frappe.utils import flt
        from yob_storefront.services import catalog_listing_service as svc

        block = self.grid(block_name="_K25 Full Grid")
        self.place("cart", "above_cart", block.name, content_width="full_width")

        with patch.object(svc, "list_items", wraps=svc.list_items) as spy:
            blocks = self.route_slots("cart")["above_cart"]

        self.assertTrue(spy.called, "the grid stopped using list_items()")

        card = next(c for c in blocks[0]["items"] if c["name"] == self.simple.name)
        self.assertEqual(card["price_state"], "priced")
        self.assertEqual(flt(card["rate"]), 150.0)

    def test_a_full_width_grid_keeps_family_select_options(self):
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
            "doctype": "Item", "item_code": "_K25-FAMILY", "item_name": "_K25-FAMILY",
            "item_group": self.item_group, "stock_uom": self.uom, "is_stock_item": 0,
            "is_sales_item": 1, "gst_hsn_code": self.hsn, "custom_slug": "_k25-family",
            "custom_category": self.category.name, "has_variants": 1,
            "attributes": [{"attribute": "Colour"}, {"attribute": "Size"}],
        }).insert(ignore_permissions=True)

        variant = create_variant(template.name, {"Colour": "Red", "Size": "Medium"})
        variant.insert(ignore_permissions=True)
        frappe.get_doc({
            "doctype": "Item Price", "item_code": variant.name,
            "price_list": self.price_list, "price_list_rate": 900,
            "selling": 1, "uom": self.uom}).insert(ignore_permissions=True)

        block = self.grid(block_name="_K25 Family Grid")
        self.place("cart", "above_cart", block.name, content_width="full_width")
        frappe.clear_cache()

        cards = {c["name"]: c for c in self.route_slots("cart")["above_cart"][0]["items"]}

        self.assertIn(template.name, cards)
        self.assertEqual(cards[template.name]["price_state"], "select_options")
        self.assertIsNone(cards[template.name]["rate"])
        self.assertNotIn(variant.name, cards, "a generated variant was merchandised")

    def test_ordering_is_unchanged_by_widths(self):
        first = self.make_block(block_name="_K25 One")
        second = self.make_block(block_name="_K25 Two")
        third = self.make_block(block_name="_K25 Three")

        self.make_page("k25-order", [
            {"block": second.name, "sequence": 20, "content_width": "full_width"},
            {"block": first.name, "sequence": 10},
            {"block": third.name, "sequence": 30, "content_width": "full_width"},
        ])

        names = [b["block_name"] for b in self.page_blocks("k25-order")]

        self.assertEqual(names, ["_K25 One", "_K25 Two", "_K25 Three"])

    def test_a_disabled_full_width_placement_is_still_omitted(self):
        block = self.make_block(block_name="_K25 Off")
        self.place("home", "hero", block.name, content_width="full_width", enabled=0)

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
