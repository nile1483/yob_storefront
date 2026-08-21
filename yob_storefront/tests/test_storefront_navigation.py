# Copyright (c) 2026, YOB and Shayona
"""Navigation and content administration (Phase 25B).

SHAPE OF A MENU
---------------
    root Group          -> holds destination children
    root destination    -> a top-level link, allowed
    child destination   -> allowed
    child Group         -> rejected
    grandchild          -> rejected

One level of nesting is a product constraint: a storefront header renders two
levels, so a deeper tree would let a merchant build navigation nothing can show.

DESTINATIONS ARE VALIDATED AGAINST REAL RECORDS
-----------------------------------------------
Storefront Category (active, not a group), Storefront Page, or an http(s) URL.
ERPNext Item Group is deliberately not a destination: it is internal ERP and
pricing structure, never storefront taxonomy.

CONTENT
-------
Blocks are reusable; a Page is an ordered composition of them. This phase builds
the ADMIN model only -- no runtime projection, which is Phase 25C.
"""

import unittest

import frappe

SEED_ITEM = "YOB-BOLT-M10"


class NavigationBase(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()

    def make_menu(self, key="main", name="Main Menu", enabled=1):
        return frappe.get_doc({
            "doctype": "YOB Storefront Menu", "menu_key": key,
            "menu_name": name, "enabled": enabled,
        }).insert(ignore_permissions=True)

    def make_item(self, menu, label, item_type, parent=None, **kw):
        doc = {"doctype": "YOB Storefront Menu Item", "menu": menu, "label": label,
               "item_type": item_type, "parent_yob_storefront_menu_item": parent}
        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True)

    def make_category(self, name="_N25 Tools", slug="n25-tools", is_group=0, is_active=1):
        return frappe.get_doc({
            "doctype": "Category", "category_name": name, "slug": slug,
            "is_group": is_group, "is_active": is_active,
        }).insert(ignore_permissions=True)

    def make_page(self, slug="n25-about", title="About", enabled=1, blocks=None):
        return frappe.get_doc({
            "doctype": "YOB Storefront Page", "slug": slug, "title": title,
            "enabled": enabled, "blocks": blocks or [],
        }).insert(ignore_permissions=True)

    def make_block(self, block_type, **kw):
        doc = {"doctype": "YOB Storefront Block",
               "block_name": kw.pop("block_name", f"_N25 {block_type}"),
               "block_type": block_type}
        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True)


# =========================================================
# MENUS
# =========================================================

class MenuStructureCase(NavigationBase):

    def test_a_menu_key_must_be_url_safe(self):
        with self.assertRaises(frappe.ValidationError):
            self.make_menu("Main Menu!")

    def test_a_group_holds_destination_children(self):
        menu = self.make_menu()
        group = self.make_item(menu.name, "Tools", "Group")
        child = self.make_item(menu.name, "Catalogue", "Catalog", parent=group.name)

        self.assertTrue(group.is_group, "is_group is derived from the type")
        self.assertFalse(child.is_group)
        self.assertEqual(child.parent_yob_storefront_menu_item, group.name)

    def test_a_root_destination_is_allowed(self):
        menu = self.make_menu()
        home = self.make_item(menu.name, "Home", "Home")

        self.assertIsNone(home.parent_yob_storefront_menu_item)

    def test_a_group_cannot_be_a_child(self):
        menu = self.make_menu()
        group = self.make_item(menu.name, "Tools", "Group")

        with self.assertRaises(frappe.ValidationError):
            self.make_item(menu.name, "Hand Tools", "Group", parent=group.name)

    def test_grandchildren_are_rejected(self):
        menu = self.make_menu()
        group = self.make_item(menu.name, "Tools", "Group")
        child = self.make_item(menu.name, "Catalogue", "Catalog", parent=group.name)

        with self.assertRaises(frappe.ValidationError):
            self.make_item(menu.name, "Deeper", "Home", parent=child.name)

    def test_a_child_must_belong_to_the_parents_menu(self):
        main = self.make_menu("main", "Main")
        footer = self.make_menu("footer", "Footer")
        group = self.make_item(main.name, "Tools", "Group")

        with self.assertRaises(frappe.ValidationError):
            self.make_item(footer.name, "Catalogue", "Catalog", parent=group.name)

    def test_a_child_inherits_the_menu_when_left_blank(self):
        menu = self.make_menu()
        group = self.make_item(menu.name, "Tools", "Group")

        child = frappe.get_doc({
            "doctype": "YOB Storefront Menu Item", "label": "Catalogue",
            "item_type": "Catalog", "parent_yob_storefront_menu_item": group.name,
        }).insert(ignore_permissions=True)

        self.assertEqual(child.menu, menu.name)

    def test_children_are_returned_in_a_deterministic_order(self):
        menu = self.make_menu()
        group = self.make_item(menu.name, "Tools", "Group")

        for label, sequence in (("Third", 30), ("First", 10), ("Second", 20)):
            self.make_item(menu.name, label, "Home", parent=group.name, sequence=sequence)

        ordered = frappe.get_all(
            "YOB Storefront Menu Item",
            filters={"parent_yob_storefront_menu_item": group.name},
            order_by="sequence asc, lft asc, name asc", pluck="label")

        self.assertEqual(ordered, ["First", "Second", "Third"])


class MenuDestinationCase(NavigationBase):

    def test_a_category_destination_must_be_active_and_listable(self):
        menu = self.make_menu()
        inactive = self.make_category("_N25 Hidden", "n25-hidden", is_active=0)
        group_cat = self.make_category("_N25 Parent", "n25-parent", is_group=1)
        listable = self.make_category()

        with self.assertRaises(frappe.ValidationError):
            self.make_item(menu.name, "Hidden", "Storefront Category",
                           storefront_category=inactive.name)

        with self.assertRaises(frappe.ValidationError):
            self.make_item(menu.name, "Parent", "Storefront Category",
                           storefront_category=group_cat.name)

        ok = self.make_item(menu.name, "Tools", "Storefront Category",
                            storefront_category=listable.name)
        self.assertEqual(ok.storefront_category, listable.name)

    def test_a_destination_type_requires_its_own_target(self):
        menu = self.make_menu()

        with self.assertRaises(frappe.ValidationError):
            self.make_item(menu.name, "Broken", "Storefront Category")

        with self.assertRaises(frappe.ValidationError):
            self.make_item(menu.name, "Broken", "External URL")

    def test_changing_type_clears_the_previous_destination(self):
        menu = self.make_menu()
        category = self.make_category()
        node = self.make_item(menu.name, "Tools", "Storefront Category",
                              storefront_category=category.name)

        node.item_type = "Catalog"
        node.save(ignore_permissions=True)
        node.reload()

        self.assertIsNone(node.storefront_category,
                          "a stale destination survived a type change")

    def test_unsafe_external_urls_are_rejected(self):
        menu = self.make_menu()

        for unsafe in ("javascript:alert(1)", "data:text/html;base64,PHN2Zz4=",
                       "vbscript:msgbox", "//evil.example/x", "not a url"):
            with self.subTest(url=unsafe):
                with self.assertRaises(frappe.ValidationError):
                    self.make_item(menu.name, "Bad", "External URL", external_url=unsafe)

    def test_safe_external_urls_are_accepted(self):
        menu = self.make_menu()

        node = self.make_item(menu.name, "Blog", "External URL",
                              external_url="https://example.com/blog",
                              open_in_new_tab=1)

        self.assertEqual(node.external_url, "https://example.com/blog")

    def test_item_group_is_not_a_destination_type(self):
        """ERPNext Item Group stays internal ERP structure."""

        options = frappe.get_meta("YOB Storefront Menu Item").get_field("item_type").options

        self.assertNotIn("Item Group", options)
        self.assertIn("Storefront Category", options)


# =========================================================
# PAGES AND BLOCKS
# =========================================================

class PageCase(NavigationBase):

    def test_a_page_slug_must_be_safe_and_unique(self):
        self.make_page(slug="n25-about")

        with self.assertRaises(frappe.ValidationError):
            self.make_page(slug="Not A Slug", title="Bad")

        with self.assertRaises(Exception):
            self.make_page(slug="n25-about", title="Duplicate")

    def test_blocks_keep_their_placement_order(self):
        first = self.make_block("Rich Text", content="<p>One</p>")
        second = self.make_block("Rich Text", content="<p>Two</p>", block_name="_N25 Two")

        page = self.make_page(blocks=[
            {"block": first.name, "sequence": 10},
            {"block": second.name, "sequence": 20},
        ])

        page.reload()
        self.assertEqual([row.block for row in page.blocks], [first.name, second.name])

    def test_the_same_block_cannot_be_placed_twice(self):
        block = self.make_block("Rich Text", content="<p>One</p>")

        with self.assertRaises(frappe.DuplicateEntryError):
            self.make_page(blocks=[{"block": block.name}, {"block": block.name}])

    def test_a_page_may_hold_at_most_three_product_grids(self):
        category = self.make_category()
        grids = [
            self.make_block("Product Grid", block_name=f"_N25 Grid {i}",
                            storefront_category=category.name, item_limit=6)
            for i in range(4)
        ]

        with self.assertRaises(frappe.ValidationError):
            self.make_page(blocks=[{"block": g.name} for g in grids])

        page = self.make_page(slug="n25-three", title="Three",
                              blocks=[{"block": g.name} for g in grids[:3]])
        self.assertEqual(len(page.blocks), 3)


class BlockCase(NavigationBase):

    def test_an_image_banner_requires_a_desktop_image(self):
        with self.assertRaises(frappe.ValidationError):
            self.make_block("Image Banner")

    def test_rich_text_is_sanitised_on_save(self):
        block = self.make_block(
            "Rich Text",
            content='<p>Hello</p><script>alert(1)</script><img src=x onerror="alert(2)">')

        self.assertNotIn("<script", block.content.lower())
        self.assertNotIn("onerror", block.content.lower())
        self.assertIn("Hello", block.content)

    def test_rich_text_that_is_only_markup_is_rejected(self):
        with self.assertRaises(frappe.ValidationError):
            self.make_block("Rich Text", content="<script>alert(1)</script>")

    def test_a_carousel_needs_at_least_one_slide(self):
        with self.assertRaises(frappe.ValidationError):
            self.make_block("Banner Carousel")

    def test_a_carousel_interval_is_bounded_when_autoplay_is_on(self):
        slide = [{"desktop_image": "/files/x.png"}]

        with self.assertRaises(frappe.ValidationError):
            self.make_block("Banner Carousel", slides=slide, auto_play=1, interval_ms=100)

        block = self.make_block("Banner Carousel", slides=slide, auto_play=1,
                                interval_ms=5000)
        self.assertEqual(block.interval_ms, 5000)

    def test_a_slide_destination_is_typed_and_validated(self):
        """Slides use the same typed destination as a menu, not a free text box."""

        category = self.make_category()

        block = self.make_block("Banner Carousel", slides=[
            {"desktop_image": "/files/x.png", "link_type": "Storefront Category",
             "link_category": category.name}])

        self.assertEqual(block.slides[0].link_category, category.name)

        with self.assertRaises(frappe.ValidationError):
            self.make_block("Banner Carousel", block_name="_N25 Bad Slide", slides=[
                {"desktop_image": "/files/x.png", "link_type": "External URL",
                 "link_external_url": "javascript:alert(1)"}])

    def test_a_slide_destination_requires_its_target(self):
        with self.assertRaises(frappe.ValidationError):
            self.make_block("Banner Carousel", slides=[
                {"desktop_image": "/files/x.png", "link_type": "Storefront Category"}])

    def test_slides_keep_their_row_order(self):
        """Ordering is the child row `idx`; no second sequence field exists."""

        block = self.make_block("Banner Carousel", slides=[
            {"desktop_image": "/files/one.png", "title": "One"},
            {"desktop_image": "/files/two.png", "title": "Two"},
            {"desktop_image": "/files/three.png", "title": "Three"}])

        block.reload()

        self.assertEqual([row.title for row in block.slides], ["One", "Two", "Three"])
        self.assertEqual([row.idx for row in block.slides], [1, 2, 3])
        self.assertIsNone(
            frappe.get_meta("YOB Storefront Block Slide").get_field("sequence"),
            "a second ordering field was added; idx is the order")

    def test_a_product_grid_uses_a_storefront_category(self):
        category = self.make_category()
        block = self.make_block("Product Grid", storefront_category=category.name,
                                item_limit=8, sort_by="Newest")

        self.assertEqual(block.storefront_category, category.name)
        self.assertIsNone(frappe.get_meta("YOB Storefront Block").get_field("item_group"),
                          "ERPNext Item Group must not be a Product Grid source")

    def test_a_product_grid_limit_is_bounded(self):
        category = self.make_category()

        for bad in (0, 13, 500):
            with self.subTest(limit=bad):
                with self.assertRaises(frappe.ValidationError):
                    self.make_block("Product Grid", block_name=f"_N25 G{bad}",
                                    storefront_category=category.name, item_limit=bad)

    def test_a_blank_item_limit_falls_back_to_the_doctype_default(self):
        """Blank is not zero: an unset limit becomes the documented maximum."""

        category = self.make_category()
        block = self.make_block("Product Grid", block_name="_N25 Default",
                                storefront_category=category.name)

        self.assertEqual(block.item_limit, 12)

    def test_price_sorting_is_not_offered(self):
        """Sorting by price would mean pricing every candidate first."""

        options = frappe.get_meta("YOB Storefront Block").get_field("sort_by").options

        self.assertNotIn("Price", options)

    def test_a_promo_grid_needs_a_card_and_a_valid_width(self):
        with self.assertRaises(frappe.ValidationError):
            self.make_block("Promo Grid", cards_per_row="2")

        with self.assertRaises(frappe.ValidationError):
            self.make_block("Promo Grid", promo_cards=[{"desktop_image": "/files/x.png"}],
                            cards_per_row="7")

        block = self.make_block("Promo Grid",
                                promo_cards=[{"desktop_image": "/files/x.png"}],
                                cards_per_row="3")
        self.assertEqual(len(block.promo_cards), 1)

    def test_stale_fields_from_another_type_are_cleared(self):
        category = self.make_category()
        block = self.make_block("Product Grid", storefront_category=category.name)

        block.block_type = "Rich Text"
        block.content = "<p>Now text</p>"
        block.save(ignore_permissions=True)
        block.reload()

        self.assertIsNone(block.storefront_category,
                          "a stale category survived a block-type change")
        self.assertFalse(block.item_limit)

    def test_offer_grid_is_not_a_block_type(self):
        """`Offer` means an ERPNext Pricing Rule in YOB; this is `Promo Grid`."""

        options = frappe.get_meta("YOB Storefront Block").get_field("block_type").options

        self.assertNotIn("Offer Grid", options)
        self.assertIn("Promo Grid", options)


if __name__ == "__main__":
    unittest.main()


# =========================================================
# TYPED DESTINATIONS (Phase 25B-1)
# =========================================================

class ContentDestinationCase(NavigationBase):
    """Menus and clickable content share ONE destination model.

    A merchant chooses a type and a record; they never type an Angular route, and
    no route-building lives in Desk JavaScript. Phase 25C turns the stored type
    plus target into a link.
    """

    SEED_ITEM = "YOB-BOLT-M10"

    def banner(self, **destination):
        return self.make_block("Image Banner", desktop_image="/files/banner.png",
                               **destination)

    def test_a_banner_may_have_no_destination(self):
        block = self.banner()

        self.assertFalse(block.link_type)
        self.assertIsNone(block.link_category)

    def test_every_supported_destination_type_is_offered(self):
        options = frappe.get_meta("YOB Storefront Block").get_field("link_type").options

        for expected in ("Catalog", "Storefront Category", "Storefront Page",
                         "Product", "External URL"):
            self.assertIn(expected, options)

    def test_catalog_needs_no_target(self):
        block = self.banner(link_type="Catalog")

        self.assertEqual(block.link_type, "Catalog")

    def test_a_category_destination_must_be_listable(self):
        group_cat = self.make_category("_N25 Group Cat", "n25-group-cat", is_group=1)

        with self.assertRaises(frappe.ValidationError):
            self.banner(link_type="Storefront Category", link_category=group_cat.name)

        listable = self.make_category()
        block = self.banner(link_type="Storefront Category", link_category=listable.name)

        self.assertEqual(block.link_category, listable.name)

    def test_a_page_destination_must_exist(self):
        page = self.make_page(slug="n25-landing", title="Landing")
        block = self.banner(link_type="Storefront Page", link_page=page.name)

        self.assertEqual(block.link_page, page.name)

    def test_a_product_destination_needs_a_public_slug(self):
        if not frappe.db.exists("Item", self.SEED_ITEM):
            self.skipTest("requires seed_demo_data on the test site")

        block = self.banner(link_type="Product", link_item=self.SEED_ITEM)
        self.assertEqual(block.link_item, self.SEED_ITEM)

        unslugged = frappe.get_doc({
            "doctype": "Item", "item_code": "_N25-NOSLUG", "item_name": "_N25-NOSLUG",
            "item_group": frappe.db.get_value("Item", self.SEED_ITEM, "item_group"),
            "stock_uom": frappe.db.get_value("Item", self.SEED_ITEM, "stock_uom"),
            "is_stock_item": 0, "is_sales_item": 1,
            "gst_hsn_code": frappe.db.get_value("Item", self.SEED_ITEM, "gst_hsn_code"),
        }).insert(ignore_permissions=True)

        with self.assertRaises(frappe.ValidationError):
            self.make_block("Image Banner", block_name="_N25 No Slug",
                            desktop_image="/files/b.png",
                            link_type="Product", link_item=unslugged.name)

    def test_a_generated_variant_is_not_a_public_destination(self):
        """Phase 24 routing stays authoritative: the family owns the URL."""

        from erpnext.controllers.item_variant import create_variant

        if not frappe.db.exists("Item", self.SEED_ITEM):
            self.skipTest("requires seed_demo_data on the test site")

        attribute = frappe.db.get_value("Item Attribute", {"name": "Size"}, "name")
        if not attribute:
            self.skipTest("no Item Attribute on this bench")

        value = frappe.db.get_value("Item Attribute Value", {"parent": attribute},
                                    "attribute_value")

        template = frappe.get_doc({
            "doctype": "Item", "item_code": "_N25-FAMILY", "item_name": "_N25-FAMILY",
            "item_group": frappe.db.get_value("Item", self.SEED_ITEM, "item_group"),
            "stock_uom": frappe.db.get_value("Item", self.SEED_ITEM, "stock_uom"),
            "is_stock_item": 0, "is_sales_item": 1, "has_variants": 1,
            "custom_slug": "n25-family",
            "gst_hsn_code": frappe.db.get_value("Item", self.SEED_ITEM, "gst_hsn_code"),
            "attributes": [{"attribute": attribute}],
        }).insert(ignore_permissions=True)

        variant = create_variant(template.name, {attribute: value})
        variant.insert(ignore_permissions=True)

        # The FAMILY is a legitimate destination.
        block = self.banner(link_type="Product", link_item=template.name)
        self.assertEqual(block.link_item, template.name)

        # The generated variant is not.
        with self.assertRaises(frappe.ValidationError):
            self.make_block("Image Banner", block_name="_N25 Variant Link",
                            desktop_image="/files/b.png",
                            link_type="Product", link_item=variant.name)

    def test_unsafe_external_destinations_are_rejected(self):
        for unsafe in ("javascript:alert(1)", "data:text/html,<svg>", "//evil.example"):
            with self.subTest(url=unsafe):
                with self.assertRaises(frappe.ValidationError):
                    self.make_block("Image Banner", block_name=f"_N25 {unsafe[:8]}",
                                    desktop_image="/files/b.png",
                                    link_type="External URL", link_external_url=unsafe)

    def test_changing_destination_type_clears_the_previous_target(self):
        category = self.make_category()
        block = self.banner(link_type="Storefront Category", link_category=category.name)

        block.link_type = "Catalog"
        block.save(ignore_permissions=True)
        block.reload()

        self.assertIsNone(block.link_category, "a stale target survived a type change")

    def test_a_non_banner_block_carries_no_destination(self):
        block = self.make_block("Rich Text", content="<p>Text</p>")

        self.assertFalse(block.link_type)

    def test_promo_cards_use_the_same_destination_model(self):
        category = self.make_category()

        block = self.make_block("Promo Grid", cards_per_row="2", promo_cards=[
            {"desktop_image": "/files/p.png", "link_type": "Storefront Category",
             "link_category": category.name}])

        self.assertEqual(block.promo_cards[0].link_category, category.name)

        with self.assertRaises(frappe.ValidationError):
            self.make_block("Promo Grid", block_name="_N25 Bad Card", cards_per_row="2",
                            promo_cards=[{"desktop_image": "/files/p.png",
                                          "link_type": "External URL",
                                          "link_external_url": "javascript:alert(1)"}])

    def test_the_free_text_link_field_is_gone(self):
        """Retired in 25B-1 with zero rows in existence, so nothing was migrated."""

        for doctype in ("YOB Storefront Block", "YOB Storefront Block Slide",
                        "YOB Storefront Block Promo Card"):
            self.assertIsNone(frappe.get_meta(doctype).get_field("link_url"), doctype)

    def test_menus_and_content_share_one_destination_validator(self):
        """The point of the refactor: one rule, not two routing systems."""

        import inspect

        from yob_storefront.utils import storefront_content
        from yob_storefront.yob_storefront.doctype.yob_storefront_block import (
            yob_storefront_block,
        )
        from yob_storefront.yob_storefront.doctype.yob_storefront_menu_item import (
            yob_storefront_menu_item,
        )

        for module in (yob_storefront_block, yob_storefront_menu_item):
            self.assertIn("apply_destination", inspect.getsource(module),
                          f"{module.__name__} validates destinations on its own")

        self.assertTrue(hasattr(storefront_content, "apply_destination"))
