# Copyright (c) 2026, YOB and Shayona
"""Gallery and Product Content as a buyer receives them (Phase 27B).

WHAT THIS PROTECTS
------------------
1. **One request.** Gallery and content arrive with `catalog.get_item`, on BOTH
   branches -- a simple product and a variant family. A product page never needs
   a second call, and `resolve_variant` never grows one.

2. **One owner.** The template's merchandising is the family's. Choosing a size
   changes the SKU, never the gallery. Generated variants contribute nothing,
   even if a direct database edit gave them rows.

3. **Stored is not published.** Phase 27A deliberately KEEPS the cells of a
   narrowed table so a merchant can widen it again. That makes this layer the
   one that must hide them, and `TableProjectionCase` is where that is proved.

4. **Fail closed, but keep the page up.** Corrupt or cross-product data is
   skipped, never published and never allowed to 500 a product page.
"""

import inspect
import json
import unittest
from unittest.mock import patch

import frappe

SEED_ITEM = "YOB-BOLT-M10"
CUSTOMER = "YOB Demo Buyer"
SECTION = "YOB Storefront Product Content Section"
GALLERY_FIELD = "custom_storefront_gallery"

BLOCK_TYPES = ("rich_text", "key_value", "table", "image", "download", "video")


def _seeded():
    return bool(frappe.db.exists("Customer", CUSTOMER) and frappe.db.exists("Item", SEED_ITEM))


class DetailBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data on the test site")

    def setUp(self):
        frappe.set_user("Administrator")
        from yob_storefront.api import catalog as catalog_api

        self.catalog = catalog_api
        self.customer = frappe.get_doc("Customer", CUSTOMER)

        p = patch.object(catalog_api, "get_storefront_customer", return_value=self.customer)
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
        frappe.flags.attribute_values = None
        self.assertEqual(self.commits, [], "a commit escaped the test rollback")

    # ------------------------------------------------------------- fixtures

    def make_item(self, code, price=100, **kw):
        doc = {"doctype": "Item", "item_code": code, "item_name": code,
               "item_group": self.item_group, "stock_uom": self.uom,
               "is_stock_item": 0, "is_sales_item": 1, "gst_hsn_code": self.hsn,
               "custom_slug": code.lower()}
        doc.update(kw)
        item = frappe.get_doc(doc).insert(ignore_permissions=True)

        if price is not None:
            frappe.get_doc({
                "doctype": "Item Price", "item_code": item.name,
                "price_list": self.price_list, "price_list_rate": price,
                "selling": 1, "uom": self.uom}).insert(ignore_permissions=True)
        return item

    def make_family(self, code, price=900):
        from erpnext.controllers.item_variant import create_variant

        for attribute, value in (("Colour", "Red"), ("Size", "Medium"), ("Size", "Large")):
            if not frappe.db.exists("Item Attribute", attribute):
                self.skipTest(f"Item Attribute {attribute!r} is not configured here")
            if not frappe.db.exists("Item Attribute Value",
                                    {"parent": attribute, "attribute_value": value}):
                doc = frappe.get_doc("Item Attribute", attribute)
                doc.append("item_attribute_values",
                           {"attribute_value": value, "abbr": value[:3].upper()})
                doc.save(ignore_permissions=True)
                frappe.clear_document_cache("Item Attribute", attribute)
        frappe.flags.attribute_values = None

        template = self.make_item(code, price=None, has_variants=1,
                                  attributes=[{"attribute": "Colour"},
                                              {"attribute": "Size"}])

        variants = []
        for size in ("Medium", "Large"):
            variant = create_variant(template.name, {"Colour": "Red", "Size": size})
            variant.insert(ignore_permissions=True)
            frappe.get_doc({
                "doctype": "Item Price", "item_code": variant.name,
                "price_list": self.price_list, "price_list_rate": price,
                "selling": 1, "uom": self.uom}).insert(ignore_permissions=True)
            variants.append(variant.name)

        return template, variants

    def set_gallery(self, item, rows):
        doc = frappe.get_doc("Item", item)
        doc.set(GALLERY_FIELD, rows)
        doc.save(ignore_permissions=True)

    def make_section(self, item, title="Description", blocks=None, **kw):
        doc = {"doctype": SECTION, "item": item, "title": title,
               "enabled": 1, "blocks": blocks or []}
        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True)

    def make_spec_group(self, name, item, rows=None):
        return frappe.get_doc({
            "doctype": "YOB Storefront Product Spec Group", "group_name": name,
            "item": item,
            "rows": rows or [{"key_label": "Material", "value_text": "Steel"}],
        }).insert(ignore_permissions=True)

    def make_table(self, name, item, column_count="2", labels=None, rows=None):
        doc = {"doctype": "YOB Storefront Product Table", "table_name": name,
               "item": item, "column_count": column_count,
               "rows": rows or [{"col_1": "A", "col_2": "B"}]}
        for n, label in enumerate(labels or ["Spec", "Value"], start=1):
            doc[f"column_{n}_label"] = label
        return frappe.get_doc(doc).insert(ignore_permissions=True)

    # ------------------------------------------------------------- the wire

    def detail(self, slug):
        frappe.clear_cache()
        response = inspect.unwrap(self.catalog.get_item)(
            auth_context={}, slug=slug, qty="1")
        self.assertNotIn("errors", response, f"get_item failed: {response}")
        return response["data"]

    def blocks_of(self, slug, section_index=0):
        return self.detail(slug)["sections"][section_index]["blocks"]


# =========================================================
# CONTRACT PRESENCE
# =========================================================

class ContractPresenceCase(DetailBase):
    """Both branches always carry both arrays."""

    def test_a_simple_product_without_merchandising(self):
        item = self.make_item("_P27B-BARE")
        data = self.detail(item.custom_slug)

        self.assertEqual(data["gallery"], [])
        self.assertEqual(data["sections"], [])

    def test_a_family_without_merchandising(self):
        template, _ = self.make_family("_P27B-BAREFAM")
        data = self.detail(template.custom_slug)

        self.assertEqual(data["is_template"], 1)
        self.assertEqual(data["gallery"], [])
        self.assertEqual(data["sections"], [])

    def test_both_keys_are_always_arrays_never_null(self):
        item = self.make_item("_P27B-ARRAYS")
        template, _ = self.make_family("_P27B-ARRAYFAM")

        for slug in (item.custom_slug, template.custom_slug):
            data = self.detail(slug)

            for key in ("gallery", "sections"):
                self.assertIn(key, data, f"{key} missing for {slug}")
                self.assertIsInstance(data[key], list, f"{key} is not a list")

    def test_the_base_image_field_still_exists_and_is_not_synthesised(self):
        """`image` stays the legacy Item image; no fake gallery row is invented."""

        item = self.make_item("_P27B-BASEIMG", image="/files/legacy.png")
        data = self.detail(item.custom_slug)

        self.assertEqual(data["image"], "/files/legacy.png")
        self.assertEqual(data["gallery"], [],
                         "an Item image was synthesised into a gallery row")

    def test_existing_detail_fields_are_untouched(self):
        item = self.make_item("_P27B-UNTOUCHED", price=150)
        data = self.detail(item.custom_slug)

        for field in ("name", "item_name", "custom_slug", "image", "rate",
                      "base_price", "uom", "stock_uom", "is_template",
                      "is_purchasable"):
            self.assertIn(field, data, f"{field} disappeared from Product Detail")

        self.assertEqual(float(data["rate"]), 150.0)


# =========================================================
# GALLERY
# =========================================================

class GalleryProjectionCase(DetailBase):

    def test_multiple_rows_in_merchant_order(self):
        item = self.make_item("_P27B-G-MANY")
        self.set_gallery(item.name, [
            {"image": "/files/third.png", "sort_order": 30},
            {"image": "/files/first.png", "sort_order": 10},
            {"image": "/files/second.png", "sort_order": 20},
        ])

        gallery = self.detail(item.custom_slug)["gallery"]

        self.assertEqual([g["image"] for g in gallery],
                         ["/files/first.png", "/files/second.png", "/files/third.png"])

    def test_ordering_is_deterministic_across_calls(self):
        item = self.make_item("_P27B-G-TIE")
        self.set_gallery(item.name, [{"image": f"/files/{n}.png"} for n in "abc"])

        first = [g["image"] for g in self.detail(item.custom_slug)["gallery"]]
        second = [g["image"] for g in self.detail(item.custom_slug)["gallery"]]

        self.assertEqual(first, second)
        self.assertEqual(first, ["/files/a.png", "/files/b.png", "/files/c.png"])

    def test_a_primary_is_reported_without_being_moved(self):
        """Order and primacy are separate facts; the merchant chose the order."""

        item = self.make_item("_P27B-G-PRIM")
        self.set_gallery(item.name, [
            {"image": "/files/one.png", "sort_order": 10},
            {"image": "/files/two.png", "sort_order": 20, "is_primary": 1},
            {"image": "/files/three.png", "sort_order": 30},
        ])

        gallery = self.detail(item.custom_slug)["gallery"]

        self.assertEqual([g["image"] for g in gallery],
                         ["/files/one.png", "/files/two.png", "/files/three.png"])
        self.assertEqual([g["is_primary"] for g in gallery], [False, True, False])

    def test_no_primary_manufactures_none(self):
        item = self.make_item("_P27B-G-NOPRIM")
        self.set_gallery(item.name, [{"image": "/files/a.png"},
                                     {"image": "/files/b.png"}])

        gallery = self.detail(item.custom_slug)["gallery"]

        self.assertEqual([g["is_primary"] for g in gallery], [False, False])

    def test_is_primary_is_a_boolean_not_an_integer(self):
        item = self.make_item("_P27B-G-BOOL")
        self.set_gallery(item.name, [{"image": "/files/a.png", "is_primary": 1}])

        self.assertIs(self.detail(item.custom_slug)["gallery"][0]["is_primary"], True)

    def test_blank_alt_and_caption_are_null(self):
        item = self.make_item("_P27B-G-NULLS")
        self.set_gallery(item.name, [{"image": "/files/a.png"}])

        row = self.detail(item.custom_slug)["gallery"][0]

        self.assertIsNone(row["alt_text"])
        self.assertIsNone(row["caption"])

    def test_alt_and_caption_are_carried_through(self):
        item = self.make_item("_P27B-G-TEXT")
        self.set_gallery(item.name, [{"image": "/files/a.png", "alt_text": "Front",
                                      "caption": "Front view"}])

        row = self.detail(item.custom_slug)["gallery"][0]

        self.assertEqual(row["alt_text"], "Front")
        self.assertEqual(row["caption"], "Front view")

    def test_the_relative_file_path_is_preserved(self):
        item = self.make_item("_P27B-G-PATH")
        self.set_gallery(item.name, [{"image": "/files/relative.png"}])

        image = self.detail(item.custom_slug)["gallery"][0]["image"]

        self.assertEqual(image, "/files/relative.png")
        self.assertFalse(image.startswith("http"), "an absolute URL was produced")

    def test_no_child_row_metadata_leaks(self):
        item = self.make_item("_P27B-G-CLEAN")
        self.set_gallery(item.name, [{"image": "/files/a.png"}])

        row = self.detail(item.custom_slug)["gallery"][0]

        self.assertEqual(set(row), {"image", "alt_text", "caption", "is_primary"})

    def test_a_family_publishes_the_template_gallery(self):
        template, _ = self.make_family("_P27B-G-FAM")
        self.set_gallery(template.name, [{"image": "/files/family.png"}])

        gallery = self.detail(template.custom_slug)["gallery"]

        self.assertEqual([g["image"] for g in gallery], ["/files/family.png"])


# =========================================================
# SECTIONS
# =========================================================

class SectionProjectionCase(DetailBase):

    def rich(self, text="<p>hello</p>"):
        return {"block_type": "rich_text", "content": text}

    def test_sections_appear_in_merchant_order(self):
        item = self.make_item("_P27B-S-ORDER")
        self.make_section(item.name, title="Third", sort_order=30, blocks=[self.rich()])
        self.make_section(item.name, title="First", sort_order=10, blocks=[self.rich()])
        self.make_section(item.name, title="Second", sort_order=20, blocks=[self.rich()])

        titles = [s["title"] for s in self.detail(item.custom_slug)["sections"]]

        self.assertEqual(titles, ["First", "Second", "Third"])

    def test_section_order_is_deterministic_across_calls(self):
        item = self.make_item("_P27B-S-DET")
        for title in ("A", "B", "C"):
            self.make_section(item.name, title=title, blocks=[self.rich()])

        first = [s["title"] for s in self.detail(item.custom_slug)["sections"]]
        second = [s["title"] for s in self.detail(item.custom_slug)["sections"]]

        self.assertEqual(first, second)

    def test_a_disabled_section_is_omitted(self):
        item = self.make_item("_P27B-S-OFF")
        self.make_section(item.name, title="Live", blocks=[self.rich()])
        self.make_section(item.name, title="Hidden", enabled=0, blocks=[self.rich()])

        titles = [s["title"] for s in self.detail(item.custom_slug)["sections"]]

        self.assertEqual(titles, ["Live"])

    def test_a_section_with_no_publishable_blocks_is_omitted(self):
        """An empty heading is worse than no heading."""

        item = self.make_item("_P27B-S-EMPTY")
        self.make_section(item.name, title="Real", blocks=[self.rich()])

        empty = self.make_section(item.name, title="Empty", blocks=[self.rich()])
        # Corrupt the only block so nothing projects, as a legacy row might be.
        frappe.db.sql("UPDATE `tabYOB Storefront Product Content Block` "
                      "SET content = '' WHERE parent = %s", empty.name)

        sections = self.detail(item.custom_slug)["sections"]

        self.assertEqual([s["title"] for s in sections], ["Real"])
        for section in sections:
            self.assertTrue(section["blocks"], "an empty section was published")

    def test_a_section_exposes_only_title_and_blocks(self):
        item = self.make_item("_P27B-S-CLEAN")
        self.make_section(item.name, title="Specs", blocks=[self.rich()])

        section = self.detail(item.custom_slug)["sections"][0]

        self.assertEqual(set(section), {"title", "blocks"})

    def test_no_section_internals_leak_anywhere(self):
        item = self.make_item("_P27B-S-NOLEAK")
        self.make_section(item.name, title="Specs", blocks=[self.rich()])

        wire = frappe.as_json(self.detail(item.custom_slug)["sections"])

        for internal in ("item", "sort_order", "enabled", "owner", "modified",
                         "creation", "docstatus", "parent", "parenttype",
                         "parentfield", "idx", "doctype", "name"):
            self.assertNotIn(f'"{internal}"', wire, f"{internal} leaked")

    def test_block_order_within_a_section_is_the_merchant_order(self):
        item = self.make_item("_P27B-S-BLOCKS")
        self.make_section(item.name, blocks=[
            {"block_type": "rich_text", "content": "<p>third</p>", "sort_order": 30},
            {"block_type": "rich_text", "content": "<p>first</p>", "sort_order": 10},
            {"block_type": "rich_text", "content": "<p>second</p>", "sort_order": 20},
        ])

        blocks = self.blocks_of(item.custom_slug)

        self.assertEqual([b["content"] for b in blocks],
                         ["<p>first</p>", "<p>second</p>", "<p>third</p>"])

    def test_blocks_are_not_grouped_by_type(self):
        item = self.make_item("_P27B-S-NOGROUP")
        group = self.make_spec_group("_P27B Mix Specs", item.name)
        self.make_section(item.name, blocks=[
            {"block_type": "rich_text", "content": "<p>one</p>", "sort_order": 10},
            {"block_type": "key_value", "spec_group": group.name, "sort_order": 20},
            {"block_type": "rich_text", "content": "<p>two</p>", "sort_order": 30},
        ])

        types = [b["type"] for b in self.blocks_of(item.custom_slug)]

        self.assertEqual(types, ["rich_text", "key_value", "rich_text"])

    def test_a_family_publishes_the_template_sections(self):
        template, _ = self.make_family("_P27B-S-FAM")
        self.make_section(template.name, title="Family Specs", blocks=[self.rich()])

        titles = [s["title"] for s in self.detail(template.custom_slug)["sections"]]

        self.assertEqual(titles, ["Family Specs"])


# =========================================================
# BLOCK SHAPES
# =========================================================

class BlockShapeCase(DetailBase):

    def setUp(self):
        super().setUp()
        self.item = self.make_item("_P27B-BLOCKS")

    def only_block(self, **block):
        self.make_section(self.item.name, title=f"S{frappe.generate_hash(length=4)}",
                          blocks=[block])
        return self.blocks_of(self.item.custom_slug)[0]

    def test_rich_text_shape(self):
        block = self.only_block(block_type="rich_text", content="<p>About this</p>")

        self.assertEqual(set(block), {"type", "content"})
        self.assertEqual(block["type"], "rich_text")
        self.assertIn("About this", block["content"])

    def test_rich_text_cannot_publish_a_script_even_from_the_database(self):
        """Re-sanitised at the READ boundary: a direct DB edit never passed save."""

        section = self.make_section(self.item.name, title="Injected",
                                    blocks=[{"block_type": "rich_text",
                                             "content": "<p>safe</p>"}])

        frappe.db.sql(
            "UPDATE `tabYOB Storefront Product Content Block` SET content = %s "
            "WHERE parent = %s",
            ('<p>safe</p><script>alert(1)</script>'
             '<img src=x onerror="alert(2)">', section.name))

        content = self.blocks_of(self.item.custom_slug)[0]["content"]

        self.assertIn("safe", content)
        self.assertNotIn("<script", content.lower())
        self.assertNotIn("onerror", content.lower())

    def test_image_shape(self):
        block = self.only_block(block_type="image", image="/files/block.png",
                                image_alt_text="Alt", image_caption="Cap")

        self.assertEqual(set(block), {"type", "image", "alt_text", "caption"})
        self.assertEqual(block["image"], "/files/block.png")
        self.assertEqual(block["alt_text"], "Alt")
        self.assertEqual(block["caption"], "Cap")

    def test_image_blank_text_is_null(self):
        block = self.only_block(block_type="image", image="/files/plain.png")

        self.assertIsNone(block["alt_text"])
        self.assertIsNone(block["caption"])

    def test_image_block_carries_no_art_direction_fields(self):
        """Not the Phase 25 CMS media row: no mobile/desktop variants."""

        block = self.only_block(block_type="image", image="/files/plain.png")

        for cms_field in ("desktop_image", "mobile_image", "destination"):
            self.assertNotIn(cms_field, block)

    def test_download_shape(self):
        block = self.only_block(block_type="download", download_file="/files/m.pdf",
                                download_label="Manual",
                                download_description="PDF, 2.4 MB")

        self.assertEqual(set(block), {"type", "file", "label", "description"})
        self.assertEqual(block["file"], "/files/m.pdf")
        self.assertEqual(block["label"], "Manual")
        self.assertEqual(block["description"], "PDF, 2.4 MB")

    def test_download_exposes_no_file_doctype_internals(self):
        block = self.only_block(block_type="download", download_file="/files/m.pdf",
                                download_label="Manual")

        wire = frappe.as_json(block)

        for internal in ("file_name", "file_url", "is_private", "folder",
                         "attached_to_doctype", "content_hash", "file_size"):
            self.assertNotIn(internal, wire)

        self.assertFalse(block["file"].startswith("/home/"),
                         "a filesystem path was published")

    def test_video_shape(self):
        block = self.only_block(block_type="video",
                                video_url="https://example.com/watch?v=1")

        self.assertEqual(set(block), {"type", "url"})
        self.assertEqual(block["url"], "https://example.com/watch?v=1")

    def test_a_corrupted_video_url_is_omitted(self):
        section = self.make_section(self.item.name, title="Video",
                                    blocks=[{"block_type": "video",
                                             "video_url": "https://ok.test/v"}])

        for corrupt in ('<iframe src="https://x.test"></iframe>',
                        "javascript:alert(1)", "ftp://x.test/v", "not a url"):
            frappe.db.sql(
                "UPDATE `tabYOB Storefront Product Content Block` SET video_url = %s "
                "WHERE parent = %s", (corrupt, section.name))

            sections = self.detail(self.item.custom_slug)["sections"]

            self.assertEqual(sections, [],
                             f"{corrupt!r} was published instead of skipped")

    def test_every_block_declares_its_type(self):
        group = self.make_spec_group("_P27B All Specs", self.item.name)
        table = self.make_table("_P27B All Table", self.item.name)

        self.make_section(self.item.name, title="Everything", blocks=[
            {"block_type": "rich_text", "content": "<p>x</p>", "sort_order": 10},
            {"block_type": "key_value", "spec_group": group.name, "sort_order": 20},
            {"block_type": "table", "product_table": table.name, "sort_order": 30},
            {"block_type": "image", "image": "/files/a.png", "sort_order": 40},
            {"block_type": "download", "download_file": "/files/a.pdf",
             "download_label": "Doc", "sort_order": 50},
            {"block_type": "video", "video_url": "https://ok.test/v", "sort_order": 60},
        ])

        blocks = self.blocks_of(self.item.custom_slug)

        self.assertEqual([b["type"] for b in blocks], list(BLOCK_TYPES))
        for block in blocks:
            self.assertIn("type", block)

    def test_no_block_carries_styling_or_layout_metadata(self):
        block = self.only_block(block_type="rich_text", content="<p>x</p>")

        for banned in ("section_style", "content_width", "css_class", "style",
                       "block_name", "sort_order", "trusted_html", "template"):
            self.assertNotIn(banned, block)


# =========================================================
# KEY / VALUE
# =========================================================

class KeyValueProjectionCase(DetailBase):

    def setUp(self):
        super().setUp()
        self.item = self.make_item("_P27B-KV")

    def test_shape_and_order(self):
        group = self.make_spec_group("_P27B KV Specs", self.item.name, rows=[
            {"key_label": "Pressure", "value_text": "16 bar", "sort_order": 20},
            {"key_label": "Material", "value_text": "Stainless Steel", "sort_order": 10},
        ])
        self.make_section(self.item.name,
                          blocks=[{"block_type": "key_value", "spec_group": group.name}])

        block = self.blocks_of(self.item.custom_slug)[0]

        self.assertEqual(set(block), {"type", "items"})
        self.assertEqual(block["items"], [
            {"key": "Material", "value": "Stainless Steel"},
            {"key": "Pressure", "value": "16 bar"},
        ])

    def test_no_spec_group_internals_leak(self):
        group = self.make_spec_group("_P27B KV Clean", self.item.name)
        self.make_section(self.item.name,
                          blocks=[{"block_type": "key_value", "spec_group": group.name}])

        wire = frappe.as_json(self.blocks_of(self.item.custom_slug)[0])

        for internal in ("group_name", "spec_group", "key_label", "value_text",
                         "item", "idx", "parent", "sort_order"):
            self.assertNotIn(f'"{internal}"', wire, f"{internal} leaked")

    def test_a_cross_product_spec_group_is_never_published(self):
        """Fail closed even though Phase 27A refuses to store this."""

        other = self.make_item("_P27B-KV-OTHER")
        foreign = self.make_spec_group("_P27B Foreign", other.name)

        section = self.make_section(
            self.item.name,
            blocks=[{"block_type": "key_value",
                     "spec_group": self.make_spec_group("_P27B Mine",
                                                        self.item.name).name}])

        # Bypass validation exactly as a direct database edit would.
        frappe.db.sql("UPDATE `tabYOB Storefront Product Content Block` "
                      "SET spec_group = %s WHERE parent = %s", (foreign.name, section.name))

        self.assertEqual(self.detail(self.item.custom_slug)["sections"], [],
                         "another product's specifications were published")

    def test_a_missing_spec_group_is_skipped_without_crashing(self):
        section = self.make_section(
            self.item.name,
            blocks=[{"block_type": "key_value",
                     "spec_group": self.make_spec_group("_P27B Doomed",
                                                        self.item.name).name}])

        frappe.db.sql("UPDATE `tabYOB Storefront Product Content Block` "
                      "SET spec_group = %s WHERE parent = %s",
                      ("_P27B No Such Group", section.name))

        self.assertEqual(self.detail(self.item.custom_slug)["sections"], [])

    def test_a_broken_block_does_not_remove_its_healthy_siblings(self):
        group = self.make_spec_group("_P27B Healthy", self.item.name)
        section = self.make_section(self.item.name, blocks=[
            {"block_type": "rich_text", "content": "<p>survives</p>", "sort_order": 10},
            {"block_type": "key_value", "spec_group": group.name, "sort_order": 20},
        ])

        frappe.db.sql("UPDATE `tabYOB Storefront Product Content Block` "
                      "SET spec_group = %s WHERE parent = %s AND block_type = 'key_value'",
                      ("_P27B Vanished", section.name))

        blocks = self.blocks_of(self.item.custom_slug)

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "rich_text")


# =========================================================
# TABLE  -- the high-value regression area
# =========================================================

class TableProjectionCase(DetailBase):

    def setUp(self):
        super().setUp()
        self.item = self.make_item("_P27B-TBL")

    def publish(self, table_name):
        self.make_section(self.item.name, title=f"T{frappe.generate_hash(length=4)}",
                          blocks=[{"block_type": "table", "product_table": table_name}])
        return self.blocks_of(self.item.custom_slug)[0]

    def test_a_two_column_table(self):
        table = self.make_table("_P27B Two", self.item.name, column_count="2",
                                labels=["Size", "Weight"],
                                rows=[{"col_1": "S", "col_2": "2 kg"},
                                      {"col_1": "M", "col_2": "2.4 kg"}])

        block = self.publish(table.name)

        self.assertEqual(set(block), {"type", "columns", "rows"})
        self.assertEqual(block["columns"], ["Size", "Weight"])
        self.assertEqual(block["rows"], [["S", "2 kg"], ["M", "2.4 kg"]])

    def test_a_six_column_table(self):
        table = self.make_table(
            "_P27B Six", self.item.name, column_count="6",
            labels=["A", "B", "C", "D", "E", "F"],
            rows=[{f"col_{n}": f"v{n}" for n in range(1, 7)}])

        block = self.publish(table.name)

        self.assertEqual(block["columns"], ["A", "B", "C", "D", "E", "F"])
        self.assertEqual(block["rows"], [["v1", "v2", "v3", "v4", "v5", "v6"]])

    def test_row_order_follows_the_grid(self):
        table = self.make_table("_P27B Ordered", self.item.name, column_count="2",
                                labels=["K", "V"],
                                rows=[{"col_1": "first", "col_2": "1"},
                                      {"col_1": "second", "col_2": "2"},
                                      {"col_1": "third", "col_2": "3"}])

        rows = self.publish(table.name)["rows"]

        self.assertEqual([r[0] for r in rows], ["first", "second", "third"])

    def test_every_row_is_exactly_as_wide_as_the_header(self):
        table = self.make_table("_P27B Ragged", self.item.name, column_count="3",
                                labels=["A", "B", "C"],
                                rows=[{"col_1": "1"},
                                      {"col_1": "1", "col_2": "2", "col_3": "3"}])

        block = self.publish(table.name)

        for row in block["rows"]:
            self.assertEqual(len(row), len(block["columns"]))

        self.assertEqual(block["rows"][0], ["1", "", ""])

    def test_narrowing_publishes_only_the_active_columns(self):
        """The database keeps columns 4-6; the wire must not show them."""

        table = self.make_table(
            "_P27B Narrowed", self.item.name, column_count="6",
            labels=["A", "B", "C", "D", "E", "F"],
            rows=[{f"col_{n}": f"v{n}" for n in range(1, 7)}])

        doc = frappe.get_doc("YOB Storefront Product Table", table.name)
        doc.column_count = "3"
        doc.save(ignore_permissions=True)

        block = self.publish(table.name)

        self.assertEqual(block["columns"], ["A", "B", "C"])
        self.assertEqual(block["rows"], [["v1", "v2", "v3"]])

        wire = frappe.as_json(block)
        for hidden in ("D", "E", "F", "v4", "v5", "v6"):
            self.assertNotIn(f'"{hidden}"', wire, f"{hidden} leaked past the width")

        # ...and the data is still THERE, which is the Phase 27A guarantee.
        stored = frappe.get_doc("YOB Storefront Product Table", table.name)
        self.assertEqual(stored.column_6_label, "F")
        self.assertEqual(stored.rows[0].col_6, "v6")

    def test_widening_again_republishes_the_retained_values(self):
        table = self.make_table(
            "_P27B Restored", self.item.name, column_count="6",
            labels=["A", "B", "C", "D", "E", "F"],
            rows=[{f"col_{n}": f"v{n}" for n in range(1, 7)}])

        doc = frappe.get_doc("YOB Storefront Product Table", table.name)
        doc.column_count = "3"
        doc.save(ignore_permissions=True)

        self.assertEqual(self.publish(table.name)["columns"], ["A", "B", "C"])

        doc = frappe.get_doc("YOB Storefront Product Table", table.name)
        doc.column_count = "6"
        doc.save(ignore_permissions=True)

        block = self.publish(table.name)

        self.assertEqual(block["columns"], ["A", "B", "C", "D", "E", "F"])
        self.assertEqual(block["rows"], [["v1", "v2", "v3", "v4", "v5", "v6"]])

    def test_the_string_column_count_is_read_through_cint(self):
        """A Select stores `"3"`; comparing it as an integer would silently fail."""

        table = self.make_table("_P27B Stringy", self.item.name, column_count="3",
                                labels=["A", "B", "C"],
                                rows=[{"col_1": "1", "col_2": "2", "col_3": "3"}])

        stored = frappe.db.get_value("YOB Storefront Product Table", table.name,
                                     "column_count")
        self.assertIsInstance(stored, str, "column_count is no longer a string")

        self.assertEqual(len(self.publish(table.name)["columns"]), 3)

    def test_a_corrupted_column_count_is_skipped(self):
        table = self.make_table("_P27B Corrupt", self.item.name, column_count="3",
                                labels=["A", "B", "C"],
                                rows=[{"col_1": "1", "col_2": "2", "col_3": "3"}])

        for bad in ("0", "1", "9", "", "abc"):
            frappe.db.sql("UPDATE `tabYOB Storefront Product Table` "
                          "SET column_count = %s WHERE name = %s", (bad, table.name))

            self.make_section(self.item.name, title=f"Bad{bad or 'blank'}",
                              blocks=[{"block_type": "table",
                                       "product_table": table.name}])

            sections = self.detail(self.item.custom_slug)["sections"]

            self.assertEqual(sections, [],
                             f"column_count={bad!r} produced a published table")

            frappe.db.sql("DELETE FROM `tabYOB Storefront Product Content Section` "
                          "WHERE item = %s", self.item.name)

    def test_a_missing_label_on_an_active_column_is_skipped(self):
        table = self.make_table("_P27B Unlabelled", self.item.name, column_count="3",
                                labels=["A", "B", "C"],
                                rows=[{"col_1": "1", "col_2": "2", "col_3": "3"}])

        frappe.db.sql("UPDATE `tabYOB Storefront Product Table` "
                      "SET column_3_label = '' WHERE name = %s", table.name)

        self.make_section(self.item.name, title="Unlabelled",
                          blocks=[{"block_type": "table", "product_table": table.name}])

        self.assertEqual(self.detail(self.item.custom_slug)["sections"], [])

    def test_a_cross_product_table_is_never_published(self):
        other = self.make_item("_P27B-TBL-OTHER")
        foreign = self.make_table("_P27B Foreign Table", other.name)
        mine = self.make_table("_P27B My Table", self.item.name)

        section = self.make_section(
            self.item.name,
            blocks=[{"block_type": "table", "product_table": mine.name}])

        frappe.db.sql("UPDATE `tabYOB Storefront Product Content Block` "
                      "SET product_table = %s WHERE parent = %s",
                      (foreign.name, section.name))

        self.assertEqual(self.detail(self.item.custom_slug)["sections"], [],
                         "another product's table was published")

    def test_a_missing_table_is_skipped_without_crashing(self):
        mine = self.make_table("_P27B Doomed Table", self.item.name)
        section = self.make_section(
            self.item.name,
            blocks=[{"block_type": "table", "product_table": mine.name}])

        frappe.db.sql("UPDATE `tabYOB Storefront Product Content Block` "
                      "SET product_table = %s WHERE parent = %s",
                      ("_P27B No Such Table", section.name))

        self.assertEqual(self.detail(self.item.custom_slug)["sections"], [])


# =========================================================
# OWNERSHIP AT RUNTIME
# =========================================================

class RuntimeOwnershipCase(DetailBase):

    def test_a_generated_variants_own_rows_never_reach_a_family_page(self):
        """Even if a direct DB edit gave a child content, the family ignores it."""

        template, variants = self.make_family("_P27B-OWN-FAM")
        self.set_gallery(template.name, [{"image": "/files/template.png"}])
        self.make_section(template.name, title="Family",
                          blocks=[{"block_type": "rich_text",
                                   "content": "<p>family</p>"}])

        # Force merchandising onto a child, bypassing Phase 27A validation.
        child_section = frappe.get_doc({
            "doctype": SECTION, "item": template.name, "title": "Child",
            "enabled": 1,
            "blocks": [{"block_type": "rich_text", "content": "<p>child</p>"}],
        }).insert(ignore_permissions=True)
        frappe.db.sql("UPDATE `tabYOB Storefront Product Content Section` "
                      "SET item = %s WHERE name = %s", (variants[0], child_section.name))

        data = self.detail(template.custom_slug)

        self.assertEqual([g["image"] for g in data["gallery"]], ["/files/template.png"])
        self.assertEqual([s["title"] for s in data["sections"]], ["Family"])

    def test_the_resolver_refuses_a_generated_variant_outright(self):
        from yob_storefront.services.product_merchandising_service import (
            merchandising_owner,
            project_merchandising,
        )

        template, variants = self.make_family("_P27B-OWN-REFUSE")

        self.assertEqual(merchandising_owner(template.name), template.name)
        self.assertIsNone(merchandising_owner(variants[0]),
                          "a generated variant was accepted as an owner")
        self.assertEqual(project_merchandising(variants[0]),
                         {"gallery": [], "sections": []})

    def test_the_resolver_never_scans_variants_for_content(self):
        import inspect as py_inspect

        from yob_storefront.services import product_merchandising_service

        source = py_inspect.getsource(product_merchandising_service)

        self.assertNotIn("variant_of\": [\"!=", source)
        self.assertNotIn("salable_variants", source)
        self.assertNotIn("variant_matrix", source)


# =========================================================
# WORK DONE
# =========================================================

class WorkCase(DetailBase):

    def build_content(self, item):
        group = self.make_spec_group("_P27B Work Specs", item.name)
        table = self.make_table("_P27B Work Table", item.name)

        for n in range(3):
            self.make_section(item.name, title=f"S{n}", sort_order=n * 10, blocks=[
                {"block_type": "rich_text", "content": "<p>x</p>", "sort_order": 10},
                {"block_type": "key_value", "spec_group": group.name, "sort_order": 20},
                {"block_type": "table", "product_table": table.name, "sort_order": 30},
                {"block_type": "image", "image": "/files/a.png", "sort_order": 40},
            ])

    def test_merchandising_adds_no_pricing_work(self):
        """Content is merchandising. It must not price anything."""

        from yob_storefront.services import pricing_service

        bare = self.make_item("_P27B-W-BARE")
        rich = self.make_item("_P27B-W-RICH")
        self.set_gallery(rich.name, [{"image": f"/files/{n}.png"} for n in range(6)])
        self.build_content(rich)

        with patch.object(pricing_service, "get_item_pricing",
                          wraps=pricing_service.get_item_pricing) as spy:
            self.detail(bare.custom_slug)
        without = spy.call_count

        with patch.object(pricing_service, "get_item_pricing",
                          wraps=pricing_service.get_item_pricing) as spy:
            self.detail(rich.custom_slug)
        with_content = spy.call_count

        self.assertEqual(without, with_content,
                         "adding gallery and content changed the pricing work")

    def test_no_variant_resolution_happens_for_merchandising(self):
        from yob_storefront.services import variant_service

        item = self.make_item("_P27B-W-NOVAR")
        self.build_content(item)

        with patch.object(variant_service, "variant_matrix") as matrix:
            self.detail(item.custom_slug)

        self.assertFalse(matrix.called)

    def test_query_count_does_not_grow_with_the_number_of_blocks(self):
        """The N+1 proof: cost tracks the content MODEL, not the block count."""

        from yob_storefront.services import product_merchandising_service as svc

        small = self.make_item("_P27B-Q-SMALL")
        self.make_section(small.name, blocks=[
            {"block_type": "rich_text", "content": "<p>x</p>"}])

        large = self.make_item("_P27B-Q-LARGE")
        self.build_content(large)          # 3 sections x 4 blocks = 12 blocks

        def count_queries(item_code):
            calls = []
            real = frappe.get_all

            def counting(*args, **kwargs):
                calls.append(args[0] if args else kwargs.get("doctype"))
                return real(*args, **kwargs)

            with patch.object(frappe, "get_all", side_effect=counting):
                svc.project_merchandising(item_code)

            return len(calls)

        small_queries = count_queries(small.name)
        large_queries = count_queries(large.name)

        # 12 blocks must not cost 12 more reads. The ceiling is the model:
        # gallery, sections, blocks, spec groups, spec rows, tables, table rows.
        self.assertLessEqual(large_queries, 7,
                             f"projection used {large_queries} queries for 12 blocks")
        self.assertLessEqual(large_queries - small_queries, 4,
                             "query count grew with the number of blocks")

    def test_nothing_is_written_by_a_product_page(self):
        item = self.make_item("_P27B-W-READONLY")
        self.build_content(item)

        self.detail(item.custom_slug)

        self.assertEqual(self.commits, [])

    def test_no_response_cache_is_introduced(self):
        """`get_item` is customer-priced; merchandising must not make it cacheable."""

        import ast
        import pathlib

        path = (pathlib.Path(frappe.get_app_path("yob_storefront"))
                / "services" / "product_merchandising_service.py")
        tree = ast.parse(path.read_text())

        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr == "cache"
                    and isinstance(node.value, ast.Name) and node.value.id == "frappe"):
                self.fail("a response cache was introduced")


# =========================================================
# REGRESSION
# =========================================================

class RegressionCase(DetailBase):

    def test_resolve_variant_carries_no_merchandising(self):
        """Selecting a size resolves a SKU. It must not reload a gallery."""

        import json as jsonlib

        template, variants = self.make_family("_P27B-R-VAR")
        self.set_gallery(template.name, [{"image": "/files/family.png"}])
        self.make_section(template.name, title="Family",
                          blocks=[{"block_type": "rich_text", "content": "<p>f</p>"}])

        frappe.clear_cache()
        response = inspect.unwrap(self.catalog.resolve_variant)(
            auth_context={}, template=template.name,
            attributes=jsonlib.dumps({"Colour": "Red", "Size": "Medium"}), qty="1")

        self.assertNotIn("errors", response, response)
        data = response["data"]

        self.assertIn(data["name"], variants)
        self.assertNotIn("gallery", data, "resolve_variant grew a gallery")
        self.assertNotIn("sections", data, "resolve_variant grew content")

    def test_product_suggestions_stay_lightweight(self):
        item = self.make_item("_P27B-R-SUGGEST")
        self.set_gallery(item.name, [{"image": "/files/a.png"}])
        self.make_section(item.name, blocks=[{"block_type": "rich_text",
                                              "content": "<p>x</p>"}])

        frappe.clear_cache()
        response = inspect.unwrap(self.catalog.get_product_suggestions)(
            auth_context={}, search="_P27B-R-SUGGEST")

        rows = response["data"]["items"]
        self.assertTrue(rows)

        for row in rows:
            self.assertEqual(set(row),
                             {"item_code", "item_name", "slug", "image", "is_template"})
            self.assertNotIn("gallery", row)
            self.assertNotIn("sections", row)

    def test_the_cms_block_union_is_untouched(self):
        """Phase 25 generic content keeps its own five types."""

        options = frappe.get_meta("YOB Storefront Block").get_field("block_type").options
        cms_types = {line for line in (options or "").split("\n") if line}

        self.assertEqual(cms_types, {"Image Banner", "Rich Text", "Banner Carousel",
                                     "Product Grid", "Promo Grid"})

    def test_the_two_block_unions_do_not_overlap_in_runtime_types(self):
        from yob_storefront.services.content_service import BLOCK_TYPES as CMS_TYPES

        cms_runtime = set(CMS_TYPES.values())
        product_runtime = set(BLOCK_TYPES)

        self.assertEqual(cms_runtime,
                         {"image_banner", "rich_text", "banner_carousel",
                          "product_grid", "promo_grid"})
        self.assertEqual(product_runtime,
                         {"rich_text", "key_value", "table", "image",
                          "download", "video"})

        # `rich_text` and `image` are shared WORDS in different unions -- they are
        # different shapes and must never be treated as one contract.
        self.assertNotEqual(cms_runtime, product_runtime)

    def test_a_cms_page_is_unaffected_by_product_content(self):
        from yob_storefront.api import cms as cms_api

        block = frappe.get_doc({
            "doctype": "YOB Storefront Block", "block_name": "_P27B CMS",
            "block_type": "Rich Text", "content": "<p>cms</p>",
            "enabled": 1}).insert(ignore_permissions=True)
        frappe.get_doc({
            "doctype": "YOB Storefront Page", "slug": "p27b-cms", "title": "CMS",
            "enabled": 1, "blocks": [{"block": block.name}],
        }).insert(ignore_permissions=True)

        with patch.object(cms_api, "get_storefront_customer", return_value=self.customer):
            frappe.clear_cache()
            response = inspect.unwrap(cms_api.get_page)(auth_context={}, slug="p27b-cms")

        page_block = response["data"]["blocks"][0]

        self.assertEqual(page_block["type"], "rich_text")
        self.assertIn("section_style", page_block, "the CMS block contract changed")
        self.assertIn("content_width", page_block)


# =========================================================
# PUBLISHED CONTRACT
# =========================================================

class PublishedContractCase(DetailBase):
    """The published block shapes must be the shapes the runtime emits.

    Bound to `x-product-block-fields` rather than to prose, and kept in a
    registry SEPARATE from the Phase 25 CMS `x-block-fields`: six product types,
    five CMS types, two domains that must never be merged.
    """

    HANDOFF = None

    def schemas(self):
        import pathlib

        path = (pathlib.Path(frappe.get_app_path("yob_storefront")).parent
                / "frontend-api-handoff" / "openapi.json")

        if not path.exists():
            self.skipTest("no published OpenAPI document in this checkout")

        return json.loads(path.read_text())["components"]["schemas"]

    def every_block_type(self):
        item = self.make_item("_P27B-C-ALL")
        group = self.make_spec_group("_P27B Contract Specs", item.name)
        table = self.make_table("_P27B Contract Table", item.name)

        self.make_section(item.name, title="Everything", blocks=[
            {"block_type": "rich_text", "content": "<p>x</p>", "sort_order": 10},
            {"block_type": "key_value", "spec_group": group.name, "sort_order": 20},
            {"block_type": "table", "product_table": table.name, "sort_order": 30},
            {"block_type": "image", "image": "/files/a.png", "sort_order": 40},
            {"block_type": "download", "download_file": "/files/a.pdf",
             "download_label": "Doc", "sort_order": 50},
            {"block_type": "video", "video_url": "https://ok.test/v", "sort_order": 60},
        ])

        return {b["type"]: b for b in self.blocks_of(item.custom_slug)}

    def test_every_projected_block_matches_its_published_fields(self):
        published = self.schemas()["ProductContentBlock"]["x-product-block-fields"]
        always = set(self.schemas()["ProductContentBlock"]["x-product-block-always-present"])
        blocks = self.every_block_type()

        self.assertEqual(set(blocks), set(published),
                         "the runtime emits a different set of types")

        for block_type, block in blocks.items():
            self.assertEqual(set(block) - always, set(published[block_type]),
                             f"{block_type} does not match its published fields")

    def test_the_published_enum_is_exactly_the_six(self):
        mapping = self.schemas()["ProductContentBlock"]["discriminator"]["mapping"]

        self.assertEqual(list(mapping), list(BLOCK_TYPES))

    def test_both_product_branches_publish_both_arrays_as_required(self):
        schemas = self.schemas()

        for name in ("ProductDetail", "VariantFamily"):
            required = schemas[name]["required"]

            self.assertIn("gallery", required, f"{name} does not require gallery")
            self.assertIn("sections", required, f"{name} does not require sections")

    def test_the_product_union_is_not_the_cms_union(self):
        schemas = self.schemas()

        self.assertIn("x-product-block-fields", schemas["ProductContentBlock"])
        self.assertNotIn("x-block-fields", schemas["ProductContentBlock"],
                         "the product union reuses the CMS registry")

        cms = set(schemas["ContentBlock"]["x-block-fields"])
        product = set(schemas["ProductContentBlock"]["x-product-block-fields"])

        self.assertNotEqual(cms, product)
        self.assertEqual(cms, {"image_banner", "rich_text", "banner_carousel",
                               "product_grid", "promo_grid"})

    def test_the_cms_block_contract_is_unchanged(self):
        cb = self.schemas()["ContentBlock"]

        self.assertEqual(cb["x-block-always-present"],
                         ["type", "block_name", "section_style", "content_width"])


if __name__ == "__main__":
    unittest.main()
