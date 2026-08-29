# Copyright (c) 2026, YOB and Shayona
"""Product Detail merchandising: gallery, sections, blocks (Phase 27A).

THE RULE THIS FILE EXISTS FOR
-----------------------------
Exactly one entity owns a public product's images and content:

    simple Item        -> its own
    variant TEMPLATE   -> the whole family's
    generated variant  -> NOTHING

`VariantOwnershipCase` proves the refusal against DIRECT document saves, not Desk
visibility, because Data Import, the REST API and `bench execute` never run a
Client Script -- and a variant that acquired a gallery through one of them would
render a product page nobody authored.

There is deliberately no variant->template fallback to test, because a variant
can hold nothing to fall back FROM.
"""

import unittest
from unittest.mock import patch

import frappe

from yob_storefront.utils.product_merchandising import BLOCK_TYPES

SEED_ITEM = "YOB-BOLT-M10"
SECTION = "YOB Storefront Product Content Section"
GALLERY_FIELD = "custom_storefront_gallery"


def _seeded():
    return bool(frappe.db.exists("Item", SEED_ITEM))


class MerchandisingBase(unittest.TestCase):
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

        self.item_group = frappe.db.get_value("Item", SEED_ITEM, "item_group")
        self.uom = frappe.db.get_value("Item", SEED_ITEM, "stock_uom")
        self.hsn = frappe.db.get_value("Item", SEED_ITEM, "gst_hsn_code")

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()
        frappe.flags.attribute_values = None
        self.assertEqual(self.commits, [], "a commit escaped the test rollback")

    # ------------------------------------------------------------- fixtures

    def make_item(self, code, **kw):
        doc = {"doctype": "Item", "item_code": code, "item_name": code,
               "item_group": self.item_group, "stock_uom": self.uom,
               "is_stock_item": 0, "is_sales_item": 1, "gst_hsn_code": self.hsn,
               "custom_slug": code.lower()}
        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True)

    def make_family(self, code="_P27-FAM"):
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

        template = self.make_item(code, has_variants=1,
                                  attributes=[{"attribute": "Colour"},
                                              {"attribute": "Size"}])

        variants = []
        for size in ("Medium", "Large"):
            variant = create_variant(template.name, {"Colour": "Red", "Size": size})
            variant.insert(ignore_permissions=True)
            variants.append(variant.name)

        return template, variants

    def gallery_row(self, **kw):
        row = {"image": "/files/p27.png", "sort_order": 0}
        row.update(kw)
        return row

    def add_gallery(self, item, rows):
        doc = frappe.get_doc("Item", item)
        doc.set(GALLERY_FIELD, rows)
        doc.save(ignore_permissions=True)
        return doc

    def make_section(self, item, title="Description", blocks=None, **kw):
        doc = {"doctype": SECTION, "item": item, "title": title,
               "enabled": 1, "blocks": blocks or []}
        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True)

    def make_spec_group(self, name="_P27 Specs", rows=None, item=None):
        return frappe.get_doc({
            "doctype": "YOB Storefront Product Spec Group", "group_name": name,
            "item": item or self.host().name,
            "rows": rows or [{"key_label": "Material", "value_text": "Steel"}],
        }).insert(ignore_permissions=True)

    def make_table(self, name="_P27 Table", item=None, column_count="2",
                   labels=None, rows=None):
        doc = {"doctype": "YOB Storefront Product Table", "table_name": name,
               "item": item or self.host().name, "column_count": column_count,
               "rows": rows or [{"col_1": "A", "col_2": "B"}]}

        for n, label in enumerate(labels or ["Spec", "Value"], start=1):
            doc[f"column_{n}_label"] = label

        return frappe.get_doc(doc).insert(ignore_permissions=True)

    def host(self):
        """A simple Item to own structured content in tests that need one."""

        if not getattr(self, "_host", None):
            self._host = self.make_item(f"_P27-HOST-{frappe.generate_hash(length=5)}")
        return self._host


# =========================================================
# ITEM LAYOUT
# =========================================================

class ItemLayoutCase(MerchandisingBase):
    """Item > Storefront is organised Filters / Gallery / Product Content."""

    def test_the_tab_is_named_storefront(self):
        field = frappe.get_meta("Item").get_field("custom_storefront_tab")

        self.assertIsNotNone(field)
        self.assertEqual(field.fieldtype, "Tab Break")
        self.assertEqual(field.label, "Storefront",
                         "the tab still reads as filters-only")

    def test_the_three_groups_appear_in_order(self):
        """Asserted by SEQUENCE of section breaks, not by absolute index."""

        meta = frappe.get_meta("Item")
        order = [f.fieldname for f in meta.fields]
        start = order.index("custom_storefront_tab")

        groups = []
        for fieldname in order[start + 1:]:
            field = meta.get_field(fieldname)
            if field.fieldtype == "Tab Break":
                break
            if field.fieldtype == "Section Break" and field.label:
                groups.append(field.label)

        self.assertEqual(groups, ["Filters", "Gallery", "Product Content"])

    def test_existing_filter_configuration_survived(self):
        meta = frappe.get_meta("Item")

        for fieldname, fieldtype in (("custom_storefront_filter_set", "Link"),
                                     ("custom_storefront_filters", "Table")):
            field = meta.get_field(fieldname)
            self.assertIsNotNone(field, f"{fieldname} was lost")
            self.assertEqual(field.fieldtype, fieldtype)

        self.assertEqual(
            meta.get_field("custom_storefront_filters").options,
            "YOB Storefront Item Filter")

    def test_stored_filter_assignments_are_untouched(self):
        """A label/layout change must not disturb data."""

        filt = frappe.get_doc({
            "doctype": "YOB Storefront Filter", "filter_key": "p27material",
            "label": "Material", "enabled": 1}).insert(ignore_permissions=True)
        value = frappe.get_doc({
            "doctype": "YOB Storefront Filter Value", "filter": filt.name,
            "value": "Steel", "enabled": 1}).insert(ignore_permissions=True)
        fset = frappe.get_doc({
            "doctype": "YOB Storefront Filter Set", "set_name": "_P27 Set",
            "filters": [{"filter": filt.name}]}).insert(ignore_permissions=True)

        item = self.make_item("_P27-FILTERED",
                              custom_storefront_filter_set=fset.name,
                              custom_storefront_filters=[
                                  {"filter": filt.name, "filter_value": value.name}])

        reloaded = frappe.get_doc("Item", item.name)

        self.assertEqual(len(reloaded.custom_storefront_filters), 1)
        self.assertEqual(reloaded.custom_storefront_filters[0].filter, filt.name)

    def test_the_gallery_field_is_a_table_of_the_dedicated_doctype(self):
        field = frappe.get_meta("Item").get_field(GALLERY_FIELD)

        self.assertIsNotNone(field)
        self.assertEqual(field.fieldtype, "Table")
        self.assertEqual(field.options, "YOB Storefront Product Gallery Image")

    def test_the_gallery_image_field_is_an_attach_image(self):
        field = frappe.get_meta("YOB Storefront Product Gallery Image").get_field("image")

        self.assertEqual(field.fieldtype, "Attach Image")
        self.assertTrue(field.reqd)


# =========================================================
# GALLERY
# =========================================================

class GalleryCase(MerchandisingBase):

    def test_a_simple_item_may_have_no_gallery(self):
        item = self.make_item("_P27-G-EMPTY")

        self.assertEqual(frappe.get_doc("Item", item.name).get(GALLERY_FIELD), [])

    def test_a_simple_item_may_have_several_images(self):
        item = self.make_item("_P27-G-MANY")
        self.add_gallery(item.name, [self.gallery_row(sort_order=i) for i in range(4)])

        self.assertEqual(len(frappe.get_doc("Item", item.name).get(GALLERY_FIELD)), 4)

    def test_a_template_may_have_a_gallery(self):
        """The family template owns the whole family's images."""

        template, _variants = self.make_family("_P27-G-FAM")
        self.add_gallery(template.name, [self.gallery_row()])

        self.assertEqual(len(frappe.get_doc("Item", template.name).get(GALLERY_FIELD)), 1)

    def test_no_primary_is_valid(self):
        item = self.make_item("_P27-G-NOPRIM")
        self.add_gallery(item.name, [self.gallery_row(), self.gallery_row()])

        rows = frappe.get_doc("Item", item.name).get(GALLERY_FIELD)
        self.assertEqual(sum(r.is_primary for r in rows), 0)

    def test_one_primary_is_valid(self):
        item = self.make_item("_P27-G-ONEPRIM")
        self.add_gallery(item.name,
                         [self.gallery_row(is_primary=1), self.gallery_row()])

        rows = frappe.get_doc("Item", item.name).get(GALLERY_FIELD)
        self.assertEqual(sum(r.is_primary for r in rows), 1)

    def test_two_primaries_are_refused(self):
        item = self.make_item("_P27-G-TWOPRIM")

        with self.assertRaises(frappe.ValidationError):
            self.add_gallery(item.name,
                             [self.gallery_row(is_primary=1),
                              self.gallery_row(is_primary=1)])

    def test_a_second_primary_is_refused_rather_than_silently_unset(self):
        """A refusal names the conflict; a silent fix edits an earlier decision."""

        item = self.make_item("_P27-G-KEEP")
        self.add_gallery(item.name, [self.gallery_row(is_primary=1), self.gallery_row()])

        doc = frappe.get_doc("Item", item.name)
        doc.get(GALLERY_FIELD)[1].is_primary = 1

        with self.assertRaises(frappe.ValidationError):
            doc.save(ignore_permissions=True)

        # The stored state is untouched: still exactly one primary, the first.
        rows = frappe.get_doc("Item", item.name).get(GALLERY_FIELD)
        self.assertEqual([r.is_primary for r in rows], [1, 0])

    def test_ordering_is_deterministic_when_sort_orders_tie(self):
        """All zeros must still produce a stable, repeatable sequence."""

        item = self.make_item("_P27-G-TIE")
        self.add_gallery(item.name, [
            self.gallery_row(image="/files/a.png"),
            self.gallery_row(image="/files/b.png"),
            self.gallery_row(image="/files/c.png"),
        ])

        def ordered():
            rows = frappe.get_doc("Item", item.name).get(GALLERY_FIELD)
            return [r.image for r in sorted(rows, key=lambda r: (r.sort_order or 0, r.idx))]

        self.assertEqual(ordered(), ["/files/a.png", "/files/b.png", "/files/c.png"])
        self.assertEqual(ordered(), ordered())

    def test_sort_order_wins_over_row_position(self):
        item = self.make_item("_P27-G-SORT")
        self.add_gallery(item.name, [
            self.gallery_row(image="/files/third.png", sort_order=30),
            self.gallery_row(image="/files/first.png", sort_order=10),
            self.gallery_row(image="/files/second.png", sort_order=20),
        ])

        rows = frappe.get_doc("Item", item.name).get(GALLERY_FIELD)
        ordered = [r.image for r in sorted(rows, key=lambda r: (r.sort_order or 0, r.idx))]

        self.assertEqual(ordered,
                         ["/files/first.png", "/files/second.png", "/files/third.png"])

    def test_alt_text_and_caption_are_optional(self):
        item = self.make_item("_P27-G-OPT")
        self.add_gallery(item.name, [self.gallery_row()])

        row = frappe.get_doc("Item", item.name).get(GALLERY_FIELD)[0]

        self.assertFalse(row.alt_text)
        self.assertFalse(row.caption)

    def test_no_arbitrary_style_field_exists_on_a_gallery_row(self):
        fields = {f.fieldname for f in
                  frappe.get_meta("YOB Storefront Product Gallery Image").fields}

        for banned in ("css_class", "style", "width", "template", "component",
                       "layout", "background"):
            self.assertNotIn(banned, fields)


# =========================================================
# VARIANT OWNERSHIP
# =========================================================

class VariantOwnershipCase(MerchandisingBase):
    """Enforced against direct saves, not Desk visibility."""

    def test_a_generated_variant_cannot_hold_gallery_rows(self):
        _template, variants = self.make_family("_P27-V-GAL")

        with self.assertRaises(frappe.ValidationError):
            self.add_gallery(variants[0], [self.gallery_row()])

    def test_a_generated_variant_cannot_own_a_content_section(self):
        _template, variants = self.make_family("_P27-V-SEC")

        with self.assertRaises(frappe.ValidationError):
            self.make_section(variants[0])

    def test_the_refusal_names_the_template(self):
        template, variants = self.make_family("_P27-V-MSG")

        with self.assertRaises(frappe.ValidationError) as caught:
            self.make_section(variants[0])

        self.assertIn(template.name, str(caught.exception))

    def test_a_template_owns_both_and_its_children_own_neither(self):
        template, variants = self.make_family("_P27-V-BOTH")

        self.add_gallery(template.name, [self.gallery_row()])
        self.make_section(template.name, title="Specifications")

        self.assertEqual(len(frappe.get_doc("Item", template.name).get(GALLERY_FIELD)), 1)
        self.assertEqual(frappe.db.count(SECTION, {"item": template.name}), 1)

        for variant in variants:
            self.assertEqual(frappe.db.count(SECTION, {"item": variant}), 0)
            self.assertEqual(
                frappe.get_doc("Item", variant).get(GALLERY_FIELD), [],
                "a generated variant acquired merchandising")

    def test_ownership_is_judged_by_variant_of_not_by_the_code(self):
        """A naming convention is a coincidence, not a data model."""

        looks_like_a_variant = self.make_item("_P27-FAM-RED-MEDIUM")

        self.make_section(looks_like_a_variant.name)

        self.assertEqual(frappe.db.count(SECTION, {"item": looks_like_a_variant.name}), 1)

    def test_no_fallback_chain_exists(self):
        """A variant holds nothing, so there is nothing to inherit FROM."""

        import inspect as py_inspect

        from yob_storefront.utils import product_merchandising

        source = py_inspect.getsource(product_merchandising)

        for word in ("fallback", "inherit", "cascade"):
            self.assertNotIn(f"def {word}", source)


# =========================================================
# SECTIONS
# =========================================================

class SectionCase(MerchandisingBase):

    def test_a_simple_item_can_own_a_section(self):
        item = self.make_item("_P27-S-SIMPLE")
        section = self.make_section(item.name, title="Description")

        self.assertEqual(section.item, item.name)
        self.assertEqual(section.title, "Description")

    def test_a_template_can_own_a_section(self):
        template, _ = self.make_family("_P27-S-FAM")

        self.assertTrue(self.make_section(template.name).name)

    def test_the_title_is_required(self):
        item = self.make_item("_P27-S-NOTITLE")

        with self.assertRaises(frappe.MandatoryError):
            self.make_section(item.name, title=None)

    def test_the_item_is_required(self):
        # The controller's own guard fires before Frappe's mandatory check, and
        # MandatoryError subclasses ValidationError -- so assert the base class
        # rather than pinning which of the two guards happens to win.
        with self.assertRaises(frappe.ValidationError):
            self.make_section(None)

    def test_an_unknown_item_is_refused(self):
        with self.assertRaises((frappe.ValidationError, frappe.LinkValidationError)):
            self.make_section("_P27 No Such Item")

    def test_a_section_belongs_to_exactly_one_item(self):
        field = frappe.get_meta(SECTION).get_field("item")

        self.assertEqual(field.fieldtype, "Link")
        self.assertEqual(field.options, "Item")
        self.assertTrue(field.reqd)

    def test_enabled_defaults_on_and_can_be_turned_off(self):
        item = self.make_item("_P27-S-ENABLED")

        self.assertTrue(self.make_section(item.name, title="On").enabled)
        self.assertFalse(self.make_section(item.name, title="Off", enabled=0).enabled)

    def test_sections_order_deterministically(self):
        item = self.make_item("_P27-S-ORDER")
        self.make_section(item.name, title="Third", sort_order=30)
        self.make_section(item.name, title="First", sort_order=10)
        self.make_section(item.name, title="Second", sort_order=20)

        titles = frappe.get_all(SECTION, filters={"item": item.name},
                                fields=["title"], order_by="sort_order asc, name asc",
                                pluck="title")

        self.assertEqual(titles, ["First", "Second", "Third"])

    def test_a_section_may_mix_block_types(self):
        item = self.make_item("_P27-S-MIXED")
        group = self.make_spec_group("_P27 Mixed Specs", item=item.name)

        table = self.make_table("_P27 Mixed Table", item=item.name)

        section = self.make_section(item.name, blocks=[
            {"block_type": "rich_text", "content": "<p>About</p>", "sort_order": 10},
            {"block_type": "key_value", "spec_group": group.name, "sort_order": 20},
            {"block_type": "table", "product_table": table.name, "sort_order": 25},
            {"block_type": "image", "image": "/files/p27.png", "sort_order": 30},
            {"block_type": "download", "download_file": "/files/spec.pdf",
             "download_label": "Datasheet", "sort_order": 40},
            {"block_type": "video", "video_url": "https://example.com/v", "sort_order": 50},
        ])

        types = [b.block_type for b in frappe.get_doc(SECTION, section.name).blocks]

        self.assertEqual(types, ["rich_text", "key_value", "table", "image",
                                 "download", "video"])

    def test_block_order_is_preserved(self):
        item = self.make_item("_P27-S-BLOCKORDER")
        section = self.make_section(item.name, blocks=[
            {"block_type": "rich_text", "content": "<p>one</p>"},
            {"block_type": "rich_text", "content": "<p>two</p>"},
            {"block_type": "rich_text", "content": "<p>three</p>"},
        ])

        blocks = frappe.get_doc(SECTION, section.name).blocks

        self.assertEqual([b.idx for b in blocks], [1, 2, 3])
        self.assertIn("one", blocks[0].content)
        self.assertIn("three", blocks[2].content)

    def test_no_layout_or_tab_field_exists(self):
        fields = {f.fieldname for f in frappe.get_meta(SECTION).fields}

        for banned in ("tab", "tab_key", "accordion", "layout", "css_class",
                       "component", "template", "width", "placement", "route"):
            self.assertNotIn(banned, fields)


# =========================================================
# BLOCKS
# =========================================================

class BlockCase(MerchandisingBase):

    def setUp(self):
        super().setUp()
        self.item = self.make_item("_P27-B-HOST")
        self._host = self.item

    def block(self, **kw):
        return self.make_section(self.item.name, title=f"B{frappe.generate_hash(length=4)}",
                                 blocks=[kw])

    def test_every_supported_type_is_accepted(self):
        group = self.make_spec_group("_P27 Block Specs", item=self.item.name)
        table = self.make_table("_P27 Block Table", item=self.item.name)
        valid = {
            "rich_text": {"content": "<p>hello</p>"},
            "key_value": {"spec_group": group.name},
            "table": {"product_table": table.name},
            "image": {"image": "/files/p27.png"},
            "download": {"download_file": "/files/p27.pdf", "download_label": "Doc"},
            "video": {"video_url": "https://example.com/watch"},
        }

        self.assertEqual(set(valid), set(BLOCK_TYPES))

        for block_type, fields in valid.items():
            self.assertTrue(self.block(block_type=block_type, **fields).name,
                            f"{block_type} was refused")

    def test_an_unknown_block_type_is_refused(self):
        for bogus in ("richtext", "Rich Text", "tables", "html", "carousel", ""):
            with self.assertRaises(frappe.ValidationError, msg=f"{bogus!r} accepted"):
                self.block(block_type=bogus, content="<p>x</p>")

    def test_each_type_requires_its_own_field(self):
        for block_type in BLOCK_TYPES:
            with self.assertRaises(frappe.ValidationError,
                                   msg=f"{block_type} saved with nothing in it"):
                self.block(block_type=block_type)

    def test_another_types_field_does_not_satisfy_a_block(self):
        """An image URL in a video block must not make it a valid video."""

        with self.assertRaises(frappe.ValidationError):
            self.block(block_type="video", image="/files/p27.png")

    def test_fields_of_other_types_are_cleared_on_save(self):
        """A block that changed type cannot keep a stale value."""

        section = self.block(block_type="rich_text", content="<p>keep</p>",
                             video_url="https://example.com/stale",
                             image="/files/stale.png")

        row = frappe.get_doc(SECTION, section.name).blocks[0]

        self.assertIn("keep", row.content)
        self.assertFalse(row.video_url, "a stale video URL survived")
        self.assertFalse(row.image, "a stale image survived")

    def test_a_video_must_be_a_url_not_embed_markup(self):
        for bad in ('<iframe src="https://x.test"></iframe>',
                    '<script>alert(1)</script>',
                    "javascript:alert(1)",
                    "not-a-url",
                    "ftp://example.com/v"):
            with self.assertRaises(frappe.ValidationError, msg=f"{bad!r} accepted"):
                self.block(block_type="video", video_url=bad)

    def test_rich_text_is_sanitised_on_save(self):
        section = self.block(block_type="rich_text",
                             content='<p>ok</p><script>alert(1)</script>')

        stored = frappe.get_doc(SECTION, section.name).blocks[0].content

        self.assertIn("ok", stored)
        self.assertNotIn("<script", stored.lower())

    def test_the_image_block_is_distinct_from_the_gallery(self):
        """Both hold images; they are different models and must stay so."""

        block_fields = {f.fieldname for f in
                        frappe.get_meta("YOB Storefront Product Content Block").fields}
        gallery_fields = {f.fieldname for f in
                          frappe.get_meta("YOB Storefront Product Gallery Image").fields}

        self.assertIn("image_caption", block_fields)
        self.assertIn("caption", gallery_fields)
        self.assertNotIn("is_primary", block_fields,
                         "a content image must not compete to be the gallery primary")

    def test_a_download_uses_frappe_attach_semantics(self):
        field = frappe.get_meta("YOB Storefront Product Content Block").get_field("download_file")

        self.assertEqual(field.fieldtype, "Attach")

    def test_no_style_or_layout_field_exists_on_a_block(self):
        fields = {f.fieldname for f in
                  frappe.get_meta("YOB Storefront Product Content Block").fields}

        for banned in ("css_class", "tailwind", "style", "width", "breakpoint",
                       "template", "component", "background", "html_wrapper"):
            self.assertNotIn(banned, fields)


# =========================================================
# KEY / VALUE
# =========================================================

class SpecGroupCase(MerchandisingBase):

    def test_rows_are_a_real_grid_not_a_json_blob(self):
        field = frappe.get_meta("YOB Storefront Product Spec Group").get_field("rows")

        self.assertEqual(field.fieldtype, "Table")
        self.assertEqual(field.options, "YOB Storefront Product Spec Row")

    def test_no_json_or_code_field_is_used_for_structured_data(self):
        """The thing the phase brief explicitly forbids: 'paste JSON here'."""

        for doctype in ("YOB Storefront Product Spec Group",
                        "YOB Storefront Product Content Block"):
            for field in frappe.get_meta(doctype).fields:
                self.assertNotIn(field.fieldtype, ("JSON", "Code"),
                                 f"{doctype}.{field.fieldname} is a {field.fieldtype}")

    def test_keys_and_values_are_stored_separately_and_ordered(self):
        group = self.make_spec_group("_P27 Ordered", rows=[
            {"key_label": "Weight", "value_text": "2 kg", "sort_order": 20},
            {"key_label": "Material", "value_text": "Steel", "sort_order": 10},
        ])

        rows = frappe.get_doc("YOB Storefront Product Spec Group", group.name).rows
        ordered = sorted(rows, key=lambda r: (r.sort_order or 0, r.idx))

        self.assertEqual([(r.key_label, r.value_text) for r in ordered],
                         [("Material", "Steel"), ("Weight", "2 kg")])

    def test_a_duplicate_key_is_refused(self):
        with self.assertRaises(frappe.DuplicateEntryError):
            self.make_spec_group("_P27 Dupe", rows=[
                {"key_label": "Material", "value_text": "Steel"},
                {"key_label": "material", "value_text": "Aluminium"},
            ])

    def test_the_block_enum_is_exactly_the_six_locked_types(self):
        self.assertEqual(list(BLOCK_TYPES),
                         ["rich_text", "key_value", "table", "image", "download", "video"])

    def test_the_stored_select_offers_exactly_those_six(self):
        options = frappe.get_meta(
            "YOB Storefront Product Content Block").get_field("block_type").options
        offered = [line for line in (options or "").split("\n") if line]

        self.assertEqual(offered, list(BLOCK_TYPES))


# =========================================================
# PRODUCT TABLE
# =========================================================

class ProductTableCase(MerchandisingBase):
    """A bounded 2-6 column table: a real grid, no JSON, no typed row indexes."""

    def setUp(self):
        super().setUp()
        self.item = self.make_item("_P27-T-HOST")
        self._host = self.item

    def test_a_two_column_table(self):
        table = self.make_table("_P27 Two", column_count="2",
                                labels=["Spec", "Value"],
                                rows=[{"col_1": "Weight", "col_2": "2 kg"}])

        stored = frappe.get_doc("YOB Storefront Product Table", table.name)

        self.assertEqual(stored.column_count, "2")
        self.assertEqual(stored.column_1_label, "Spec")
        self.assertEqual(stored.column_2_label, "Value")
        self.assertEqual((stored.rows[0].col_1, stored.rows[0].col_2), ("Weight", "2 kg"))

    def test_a_six_column_table(self):
        table = self.make_table(
            "_P27 Six", column_count="6",
            labels=["A", "B", "C", "D", "E", "F"],
            rows=[{f"col_{n}": f"v{n}" for n in range(1, 7)}])

        stored = frappe.get_doc("YOB Storefront Product Table", table.name)

        self.assertEqual([stored.get(f"column_{n}_label") for n in range(1, 7)],
                         ["A", "B", "C", "D", "E", "F"])
        self.assertEqual([stored.rows[0].get(f"col_{n}") for n in range(1, 7)],
                         [f"v{n}" for n in range(1, 7)])

    def test_fewer_than_two_columns_is_refused(self):
        for bad in ("1", "0", ""):
            with self.assertRaises(frappe.ValidationError, msg=f"{bad!r} accepted"):
                self.make_table(f"_P27 Bad {bad or 'blank'}", column_count=bad)

    def test_more_than_six_columns_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self.make_table("_P27 Seven", column_count="7",
                            labels=["A", "B", "C", "D", "E", "F"])

    def test_the_select_offers_only_two_through_six(self):
        options = frappe.get_meta(
            "YOB Storefront Product Table").get_field("column_count").options
        offered = [line for line in (options or "").split("\n") if line]

        self.assertEqual(offered, ["2", "3", "4", "5", "6"])

    def test_every_active_column_needs_a_label(self):
        with self.assertRaises(frappe.ValidationError):
            self.make_table("_P27 Unlabelled", column_count="4",
                            labels=["A", "B"])          # 3 and 4 left blank

    def test_a_blank_label_does_not_count_as_a_label(self):
        with self.assertRaises(frappe.ValidationError):
            self.make_table("_P27 Whitespace", column_count="3",
                            labels=["A", "B", "   "])

    def narrowed_six_column_table(self, name):
        """A full six-column table, then narrowed to three."""

        table = self.make_table(
            name, column_count="6",
            labels=["A", "B", "C", "D", "E", "F"],
            rows=[{f"col_{n}": f"v{n}" for n in range(1, 7)},
                  {f"col_{n}": f"w{n}" for n in range(1, 7)}])

        doc = frappe.get_doc("YOB Storefront Product Table", table.name)
        doc.column_count = "3"
        doc.save(ignore_permissions=True)

        return table.name

    def test_narrowing_preserves_the_inactive_labels_and_cells(self):
        """Width is a view, not a deletion.

        Changing a dropdown from 6 to 3 must not destroy work. A merchant who
        simplifies a page and then changes their mind gets columns 4-6 back --
        clearing them would make an innocuous-looking change unrecoverable.
        """

        name = self.narrowed_six_column_table("_P27 Narrowed")
        stored = frappe.get_doc("YOB Storefront Product Table", name)

        self.assertEqual(stored.column_count, "3")

        for n in range(1, 7):
            self.assertTrue(stored.get(f"column_{n}_label"),
                            f"column_{n}_label was destroyed by narrowing")
            self.assertEqual(stored.rows[0].get(f"col_{n}"), f"v{n}",
                             f"col_{n} was destroyed by narrowing")
            self.assertEqual(stored.rows[1].get(f"col_{n}"), f"w{n}")

    def test_widening_again_restores_the_original_values(self):
        name = self.narrowed_six_column_table("_P27 Restored")

        doc = frappe.get_doc("YOB Storefront Product Table", name)
        doc.column_count = "6"
        doc.save(ignore_permissions=True)

        stored = frappe.get_doc("YOB Storefront Product Table", name)

        self.assertEqual([stored.get(f"column_{n}_label") for n in range(1, 7)],
                         ["A", "B", "C", "D", "E", "F"])
        self.assertEqual([stored.rows[0].get(f"col_{n}") for n in range(1, 7)],
                         [f"v{n}" for n in range(1, 7)])

    def test_a_blank_inactive_label_does_not_block_a_save(self):
        """Inactive columns are not judged by the active-label rule."""

        table = self.make_table("_P27 Blank Tail", column_count="2",
                                labels=["A", "B"],
                                rows=[{"col_1": "1", "col_2": "2"}])

        stored = frappe.get_doc("YOB Storefront Product Table", table.name)

        self.assertEqual(stored.column_count, "2")
        for n in (3, 4, 5, 6):
            self.assertFalse(stored.get(f"column_{n}_label"))

    def test_narrowing_over_a_blank_inactive_column_still_saves(self):
        """Columns 4-6 blank while active=6 would fail; narrowing must succeed."""

        table = self.make_table("_P27 Narrow Over Blank", column_count="3",
                                labels=["A", "B", "C"],
                                rows=[{"col_1": "1", "col_2": "2", "col_3": "3"}])

        doc = frappe.get_doc("YOB Storefront Product Table", table.name)
        doc.column_count = "2"
        doc.save(ignore_permissions=True)

        self.assertEqual(
            frappe.db.get_value("YOB Storefront Product Table", table.name,
                                "column_3_label"),
            "C", "an inactive label was cleared on narrowing")

    def test_row_order_survives_a_width_change(self):
        name = self.narrowed_six_column_table("_P27 Order Kept")
        rows = frappe.get_doc("YOB Storefront Product Table", name).rows

        self.assertEqual([r.idx for r in rows], [1, 2])
        self.assertEqual([r.col_1 for r in rows], ["v1", "w1"])

    def test_row_order_is_the_grid_order(self):
        table = self.make_table("_P27 Ordered", column_count="2",
                                labels=["K", "V"],
                                rows=[{"col_1": "first", "col_2": "1"},
                                      {"col_1": "second", "col_2": "2"},
                                      {"col_1": "third", "col_2": "3"}])

        rows = frappe.get_doc("YOB Storefront Product Table", table.name).rows

        self.assertEqual([r.idx for r in rows], [1, 2, 3])
        self.assertEqual([r.col_1 for r in rows], ["first", "second", "third"])

    def test_no_row_index_field_is_typed_by_hand(self):
        fields = {f.fieldname for f in
                  frappe.get_meta("YOB Storefront Product Table Row").fields}

        for banned in ("row_index", "row_number", "sort_order", "sequence", "position"):
            self.assertNotIn(banned, fields)

        self.assertEqual(fields, {f"col_{n}" for n in range(1, 7)})

    def test_no_json_or_code_field_represents_the_table(self):
        for doctype in ("YOB Storefront Product Table",
                        "YOB Storefront Product Table Row"):
            for field in frappe.get_meta(doctype).fields:
                self.assertNotIn(field.fieldtype, ("JSON", "Code", "Text Editor"),
                                 f"{doctype}.{field.fieldname} is a {field.fieldtype}")

    def test_a_table_block_owns_only_its_own_field(self):
        table = self.make_table("_P27 Owned", item=self.item.name)

        section = self.make_section(self.item.name, blocks=[{
            "block_type": "table", "product_table": table.name,
            "content": "<p>stale</p>", "video_url": "https://example.com/stale",
            "spec_group": None}])

        row = frappe.get_doc(SECTION, section.name).blocks[0]

        self.assertEqual(row.product_table, table.name)
        self.assertFalse(row.content, "a stale rich text survived on a table block")
        self.assertFalse(row.video_url, "a stale video URL survived on a table block")

    def test_a_table_block_without_a_table_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self.make_section(self.item.name, blocks=[{"block_type": "table"}])

    def test_the_desk_column_script_ships_as_an_app_file(self):
        import pathlib

        from yob_storefront import hooks

        relative = hooks.doctype_js["YOB Storefront Product Table"]
        path = pathlib.Path(frappe.get_app_path("yob_storefront")) / relative

        self.assertTrue(path.exists())
        self.assertFalse(
            frappe.db.exists("Client Script", {"dt": "YOB Storefront Product Table"}),
            "a Client Script exists; Desk logic must ship as app files")


# =========================================================
# STRUCTURED CONTENT OWNERSHIP
# =========================================================

class StructuredOwnershipCase(MerchandisingBase):
    """Structured content belongs to ONE product, and never to a variant."""

    def setUp(self):
        super().setUp()
        self.owner = self.make_item("_P27-O-OWNER")
        self.other = self.make_item("_P27-O-OTHER")
        self._host = self.owner

    # ---------------------------------------------------------- spec group

    def test_a_spec_group_requires_an_item(self):
        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc({
                "doctype": "YOB Storefront Product Spec Group",
                "group_name": "_P27 Ownerless",
                "rows": [{"key_label": "K", "value_text": "V"}],
            }).insert(ignore_permissions=True)

    def test_a_spec_group_may_be_reused_within_its_own_product(self):
        group = self.make_spec_group("_P27 Shared Within", item=self.owner.name)

        self.make_section(self.owner.name, title="One",
                          blocks=[{"block_type": "key_value", "spec_group": group.name}])
        self.make_section(self.owner.name, title="Two",
                          blocks=[{"block_type": "key_value", "spec_group": group.name}])

        self.assertEqual(frappe.db.count(SECTION, {"item": self.owner.name}), 2)

    def test_another_products_spec_group_is_refused(self):
        group = self.make_spec_group("_P27 Foreign Specs", item=self.other.name)

        with self.assertRaises(frappe.ValidationError):
            self.make_section(self.owner.name,
                              blocks=[{"block_type": "key_value",
                                       "spec_group": group.name}])

    def test_the_mismatch_message_names_both_products(self):
        group = self.make_spec_group("_P27 Named Specs", item=self.other.name)

        with self.assertRaises(frappe.ValidationError) as caught:
            self.make_section(self.owner.name,
                              blocks=[{"block_type": "key_value",
                                       "spec_group": group.name}])

        message = str(caught.exception)
        self.assertIn(self.other.name, message)
        self.assertIn(self.owner.name, message)

    def test_a_variant_cannot_own_a_spec_group(self):
        _template, variants = self.make_family("_P27-O-SPECFAM")

        with self.assertRaises(frappe.ValidationError):
            self.make_spec_group("_P27 Variant Specs", item=variants[0])

    # -------------------------------------------------------- product table

    def test_a_product_table_requires_an_item(self):
        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc({
                "doctype": "YOB Storefront Product Table",
                "table_name": "_P27 Ownerless Table", "column_count": "2",
                "column_1_label": "A", "column_2_label": "B",
                "rows": [{"col_1": "1", "col_2": "2"}],
            }).insert(ignore_permissions=True)

    def test_a_product_table_may_be_reused_within_its_own_product(self):
        table = self.make_table("_P27 Shared Table", item=self.owner.name)

        self.make_section(self.owner.name, title="One",
                          blocks=[{"block_type": "table", "product_table": table.name}])
        self.make_section(self.owner.name, title="Two",
                          blocks=[{"block_type": "table", "product_table": table.name}])

        self.assertEqual(frappe.db.count(SECTION, {"item": self.owner.name}), 2)

    def test_another_products_table_is_refused(self):
        table = self.make_table("_P27 Foreign Table", item=self.other.name)

        with self.assertRaises(frappe.ValidationError):
            self.make_section(self.owner.name,
                              blocks=[{"block_type": "table",
                                       "product_table": table.name}])

    def test_a_variant_cannot_own_a_product_table(self):
        _template, variants = self.make_family("_P27-O-TABFAM")

        with self.assertRaises(frappe.ValidationError):
            self.make_table("_P27 Variant Table", item=variants[0])

    def test_a_template_may_own_both_for_its_family(self):
        template, variants = self.make_family("_P27-O-FAM")

        group = self.make_spec_group("_P27 Family Specs", item=template.name)
        table = self.make_table("_P27 Family Table", item=template.name)

        self.make_section(template.name, blocks=[
            {"block_type": "key_value", "spec_group": group.name},
            {"block_type": "table", "product_table": table.name},
        ])

        self.assertEqual(frappe.db.count(SECTION, {"item": template.name}), 1)

        for variant in variants:
            self.assertEqual(frappe.db.count(SECTION, {"item": variant}), 0)

    def test_a_missing_linked_document_is_refused(self):
        with self.assertRaises((frappe.ValidationError, frappe.LinkValidationError)):
            self.make_section(self.owner.name,
                              blocks=[{"block_type": "table",
                                       "product_table": "_P27 No Such Table"}])


# =========================================================
# DOMAIN SEPARATION
# =========================================================

class DomainSeparationCase(MerchandisingBase):
    """Product merchandising is NOT the Phase 25 generic CMS."""

    def test_the_section_uses_its_own_block_doctype(self):
        field = frappe.get_meta(SECTION).get_field("blocks")

        self.assertEqual(field.options, "YOB Storefront Product Content Block")
        self.assertNotEqual(field.options, "YOB Storefront Block")

    def test_the_two_block_models_are_different_doctypes(self):
        product = frappe.get_meta("YOB Storefront Product Content Block")
        cms = frappe.get_meta("YOB Storefront Block")

        self.assertNotEqual(product.name, cms.name)
        self.assertTrue(product.istable, "the product block is a child row")
        self.assertFalse(cms.istable, "the CMS block is a reusable master")

    def test_the_cms_block_types_are_unchanged(self):
        options = frappe.get_meta("YOB Storefront Block").get_field("block_type").options
        cms_types = {line for line in (options or "").split("\n") if line}

        self.assertEqual(cms_types, {"Image Banner", "Rich Text", "Banner Carousel",
                                     "Product Grid", "Promo Grid"})

    def test_a_product_section_has_no_page_or_route_concept(self):
        fields = {f.fieldname for f in frappe.get_meta(SECTION).fields}

        for cms_concept in ("slug", "route_key", "slot_key", "section_style",
                            "content_width", "meta_title", "meta_description"):
            self.assertNotIn(cms_concept, fields)

    def test_phase_25_pages_and_placements_are_untouched(self):
        for doctype, field, expected in (
                ("YOB Storefront Page", "slug", "Data"),
                ("YOB Storefront Page Block", "section_style", "Select"),
                ("YOB Storefront Content Placement", "route_key", "Select"),
                ("YOB Storefront Content Placement", "content_width", "Select")):
            meta_field = frappe.get_meta(doctype).get_field(field)

            self.assertIsNotNone(meta_field, f"{doctype}.{field} vanished")
            self.assertEqual(meta_field.fieldtype, expected)


# =========================================================
# INSTALLATION
# =========================================================

class InstallCase(MerchandisingBase):

    DOCTYPES = ("YOB Storefront Product Gallery Image",
                "YOB Storefront Product Content Section",
                "YOB Storefront Product Content Block",
                "YOB Storefront Product Spec Group",
                "YOB Storefront Product Spec Row")

    def test_every_new_doctype_is_app_owned(self):
        for doctype in self.DOCTYPES:
            meta = frappe.get_meta(doctype)

            self.assertEqual(meta.module, "yob_storefront")
            self.assertFalse(meta.custom, f"{doctype} is a site customisation")

    def test_every_new_doctype_ships_as_a_file(self):
        import pathlib

        root = pathlib.Path(frappe.get_app_path("yob_storefront")) / "yob_storefront" / "doctype"

        for doctype in self.DOCTYPES:
            folder = doctype.lower().replace(" ", "_")
            path = root / folder / f"{folder}.json"

            self.assertTrue(path.exists(), f"{doctype} is not in the app")

    def test_the_installer_is_idempotent_for_the_new_fields(self):
        """Re-running must not duplicate a field or disturb stored values."""

        from yob_storefront.install import ensure_custom_fields

        item = self.make_item("_P27-IDEMPOTENT")
        self.add_gallery(item.name, [self.gallery_row(alt_text="kept")])

        before = frappe.db.count("Custom Field", {"dt": "Item"})
        ensure_custom_fields()
        after = frappe.db.count("Custom Field", {"dt": "Item"})

        self.assertEqual(before, after, "a repeated install duplicated a field")

        rows = frappe.get_doc("Item", item.name).get(GALLERY_FIELD)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].alt_text, "kept", "reinstalling disturbed stored data")

    def test_desk_behaviour_ships_as_app_files_not_client_scripts(self):
        import pathlib

        from yob_storefront import hooks

        files = hooks.doctype_js["Item"]
        self.assertIn("public/js/item_storefront_content.js", files)

        root = pathlib.Path(frappe.get_app_path("yob_storefront"))
        for relative in files:
            self.assertTrue((root / relative).exists(), f"{relative} is missing")

        self.assertFalse(
            frappe.db.exists("Client Script", {"dt": SECTION}),
            "a Client Script exists; Desk logic must ship as app files")

    def test_both_desk_structures_expose_product_content(self):
        """Workspace card AND the v16 left sidebar -- they are separate sources."""

        self.assertTrue(
            frappe.db.exists("Workspace Link", {
                "parent": "YOB Storefront", "link_to": SECTION}),
            "missing from the workspace page")

        self.assertTrue(
            frappe.db.exists("Workspace Sidebar Item", {
                "parent": "YOB Storefront", "link_to": SECTION}),
            "missing from the LEFT sidebar")

    def test_product_content_sits_under_catalog_not_under_cms_content(self):
        """Distinct from Phase 25 Pages / Content Blocks / Content Placements."""

        items = frappe.get_all(
            "Workspace Sidebar Item", filters={"parent": "YOB Storefront"},
            fields=["idx", "type", "label", "link_to"], order_by="idx")

        group = None
        for row in items:
            if row.type == "Section Break":
                group = row.label
            if row.link_to == SECTION:
                self.assertEqual(group, "Catalog",
                                 f"Product Content Sections landed under {group!r}")
                return

        self.fail("Product Content Sections is not in the sidebar")


if __name__ == "__main__":
    unittest.main()
