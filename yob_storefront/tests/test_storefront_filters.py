# Copyright (c) 2026, YOB and Shayona
"""Merchandising filters: integrity proved on the SERVER (Phase 25B).

TWO FILTER SETS, TWO JOBS
-------------------------
    Item.custom_storefront_filter_set   ADMIN SCOPE -- which Filters an
                                        administrator may attach to this product
    Category.storefront_filter_set      DISPLAY -- which Filters that category's
                                        listing will expose to buyers

They are independent by design. An Industrial Switch may carry Voltage, Colour,
Material, IP Rating and Mount Type while its category exposes only Voltage and
Colour; the category must never erase the richer item metadata.

NOT VARIANT ATTRIBUTES
----------------------
ERPNext variant attributes resolve an actual SKU (Phase 24). These narrow a
listing. They share nothing, and nothing here reads the other.

EVERY RULE IS SERVER-SIDE
-------------------------
The prototype enforced them in a Client Script, which Data Import, the REST API
and `bench execute` all walk straight past. These tests use the ORM directly --
no Desk, no JavaScript -- which is exactly how a bulk import arrives.
"""

import unittest

import frappe

SEED_ITEM = "YOB-BOLT-M10"


class FilterBase(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.item_group = frappe.db.get_value("Item", SEED_ITEM, "item_group") or "All Item Groups"
        self.stock_uom = frappe.db.get_value("Item", SEED_ITEM, "stock_uom") or "Nos"
        self.hsn = frappe.db.get_value("Item", SEED_ITEM, "gst_hsn_code")

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()

    # ------------------------------------------------------------- fixtures

    def make_filter(self, key, label=None, enabled=1):
        return frappe.get_doc({
            "doctype": "YOB Storefront Filter", "filter_key": key,
            "label": label or key.title(), "enabled": enabled,
        }).insert(ignore_permissions=True)

    def make_value(self, filter_name, value, enabled=1, value_key=None):
        return frappe.get_doc({
            "doctype": "YOB Storefront Filter Value", "filter": filter_name,
            "value": value, "value_key": value_key, "enabled": enabled,
        }).insert(ignore_permissions=True)

    def make_set(self, name, filters):
        return frappe.get_doc({
            "doctype": "YOB Storefront Filter Set", "set_name": name,
            "filters": [{"filter": f} for f in filters],
        }).insert(ignore_permissions=True)

    def make_item(self, code, **kw):
        doc = {"doctype": "Item", "item_code": code, "item_name": code,
               "item_group": self.item_group, "stock_uom": self.stock_uom,
               "is_stock_item": 0, "is_sales_item": 1, "gst_hsn_code": self.hsn}
        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True)

    def electrical(self):
        """The brief's own example: a rich item, a narrower category."""

        voltage = self.make_filter("voltage", "Voltage")
        colour = self.make_filter("colour", "Colour")
        material = self.make_filter("material", "Material")

        value_map = {
            "240v": self.make_value(voltage.name, "240V"),
            "415v": self.make_value(voltage.name, "415V"),
            "red": self.make_value(colour.name, "Red"),
            "blue": self.make_value(colour.name, "Blue"),
            "steel": self.make_value(material.name, "Steel"),
        }

        item_set = self.make_set("Electrical Product Filters",
                                 [voltage.name, colour.name, material.name])
        category_set = self.make_set("Electrical Customer Filters",
                                     [voltage.name, colour.name])

        return frappe._dict(voltage=voltage, colour=colour, material=material,
                            vals=value_map, item_set=item_set, category_set=category_set)


# =========================================================
# FILTER AND VALUE DEFINITIONS
# =========================================================

class FilterDefinitionCase(FilterBase):

    def test_the_same_text_may_exist_under_different_filters(self):
        colour = self.make_filter("colour")
        finish = self.make_filter("paint_finish", "Paint Finish")

        self.make_value(colour.name, "Red")
        self.make_value(finish.name, "Red")

        self.assertEqual(
            frappe.db.count("YOB Storefront Filter Value", {"value": "Red"}), 2,
            "the prototype's global unique made Red usable under one filter only")

    def test_a_value_cannot_repeat_within_one_filter(self):
        colour = self.make_filter("colour")
        self.make_value(colour.name, "Red")

        with self.assertRaises(frappe.DuplicateEntryError):
            self.make_value(colour.name, "Red")

    def test_the_value_key_is_derived_and_also_unique_per_filter(self):
        colour = self.make_filter("colour")
        value = self.make_value(colour.name, "Deep Red")

        self.assertEqual(value.value_key, "deep-red")

        with self.assertRaises(frappe.DuplicateEntryError):
            self.make_value(colour.name, "Something else", value_key="deep-red")

    def test_a_filter_key_must_be_machine_safe(self):
        with self.assertRaises(frappe.ValidationError):
            self.make_filter("Voltage Rating!")

    def test_a_filter_set_cannot_hold_the_same_filter_twice(self):
        colour = self.make_filter("colour")

        with self.assertRaises(frappe.DuplicateEntryError):
            self.make_set("Broken Set", [colour.name, colour.name])

    def test_a_filter_with_values_is_not_deleted_silently(self):
        colour = self.make_filter("colour")
        self.make_value(colour.name, "Red")

        with self.assertRaises(frappe.LinkExistsError):
            colour.delete()


# =========================================================
# ITEM ASSIGNMENTS
# =========================================================

class ItemFilterCase(FilterBase):

    def assign(self, item, rows, filter_set=None):
        item.custom_storefront_filter_set = filter_set
        item.set("custom_storefront_filters", rows)
        item.save(ignore_permissions=True)
        return item

    def test_an_item_carries_richer_metadata_than_its_category_exposes(self):
        """The corrected Phase 25B semantics, end to end."""

        f = self.electrical()
        item = self.make_item("_F25-SWITCH", custom_slug="f25-switch")

        category = frappe.get_doc({
            "doctype": "Category", "category_name": "_F25 Industrial Switches",
            "slug": "f25-industrial-switches", "is_active": 1,
            "storefront_filter_set": f.category_set.name,
        }).insert(ignore_permissions=True)

        item.custom_category = category.name
        self.assign(item, [
            {"filter": f.voltage.name, "filter_value": f.vals["240v"].name},
            {"filter": f.colour.name, "filter_value": f.vals["red"].name},
            {"filter": f.material.name, "filter_value": f.vals["steel"].name},
        ], filter_set=f.item_set.name)

        item.reload()

        self.assertEqual(len(item.custom_storefront_filters), 3,
                         "the category's narrower set erased item metadata")
        self.assertNotEqual(item.custom_storefront_filter_set, category.storefront_filter_set,
                            "the two sets are independent and this fixture must prove it")

    def test_multiple_values_under_one_filter_are_allowed(self):
        f = self.electrical()
        item = self.make_item("_F25-MULTI")

        self.assign(item, [
            {"filter": f.colour.name, "filter_value": f.vals["red"].name},
            {"filter": f.colour.name, "filter_value": f.vals["blue"].name},
        ], filter_set=f.item_set.name)

        item.reload()
        self.assertEqual(len(item.custom_storefront_filters), 2)

    def test_the_exact_same_pair_twice_is_rejected(self):
        f = self.electrical()
        item = self.make_item("_F25-DUP")

        with self.assertRaises(frappe.DuplicateEntryError):
            self.assign(item, [
                {"filter": f.colour.name, "filter_value": f.vals["red"].name},
                {"filter": f.colour.name, "filter_value": f.vals["red"].name},
            ], filter_set=f.item_set.name)

    def test_a_filter_outside_the_items_set_is_rejected(self):
        f = self.electrical()
        outside = self.make_filter("ip_rating", "IP Rating")
        value = self.make_value(outside.name, "IP65")
        item = self.make_item("_F25-OUTSIDE")

        with self.assertRaises(frappe.ValidationError):
            self.assign(item, [{"filter": outside.name, "filter_value": value.name}],
                        filter_set=f.item_set.name)

    def test_a_value_from_another_filter_is_rejected(self):
        f = self.electrical()
        item = self.make_item("_F25-CROSSED")

        with self.assertRaises(frappe.ValidationError):
            self.assign(item, [
                {"filter": f.colour.name, "filter_value": f.vals["240v"].name},
            ], filter_set=f.item_set.name)

    def test_rows_without_a_filter_set_are_rejected(self):
        f = self.electrical()
        item = self.make_item("_F25-NOSET")

        with self.assertRaises(frappe.ValidationError):
            self.assign(item, [
                {"filter": f.colour.name, "filter_value": f.vals["red"].name},
            ], filter_set=None)

    def test_a_disabled_filter_cannot_be_newly_assigned(self):
        f = self.electrical()
        frappe.db.set_value("YOB Storefront Filter", f.colour.name, "enabled", 0)
        frappe.clear_document_cache("YOB Storefront Filter", f.colour.name)
        item = self.make_item("_F25-DISABLED-FILTER")

        with self.assertRaises(frappe.ValidationError):
            self.assign(item, [
                {"filter": f.colour.name, "filter_value": f.vals["red"].name},
            ], filter_set=f.item_set.name)

    def test_a_disabled_value_cannot_be_newly_assigned(self):
        f = self.electrical()
        frappe.db.set_value("YOB Storefront Filter Value", f.vals["red"].name, "enabled", 0)
        item = self.make_item("_F25-DISABLED-VALUE")

        with self.assertRaises(frappe.ValidationError):
            self.assign(item, [
                {"filter": f.colour.name, "filter_value": f.vals["red"].name},
            ], filter_set=f.item_set.name)

    def test_disabling_later_does_not_break_an_existing_assignment(self):
        """Disabling a filter must not rewrite catalogue data behind a merchant."""

        f = self.electrical()
        item = self.make_item("_F25-GRANDFATHERED")
        self.assign(item, [
            {"filter": f.colour.name, "filter_value": f.vals["red"].name},
        ], filter_set=f.item_set.name)

        frappe.db.set_value("YOB Storefront Filter Value", f.vals["red"].name, "enabled", 0)
        frappe.db.set_value("YOB Storefront Filter", f.colour.name, "enabled", 0)
        frappe.clear_document_cache("YOB Storefront Filter", f.colour.name)

        item.reload()
        item.item_name = "_F25-GRANDFATHERED renamed"
        item.save(ignore_permissions=True)          # must not raise

        item.reload()
        self.assertEqual(len(item.custom_storefront_filters), 1)


# =========================================================
# VARIANTS (Phase 24 stays authoritative)
# =========================================================

class VariantFilterCase(FilterBase):

    def family(self):
        from erpnext.controllers.item_variant import create_variant

        attribute = frappe.db.get_value("Item Attribute", {"name": "Size"}, "name")
        if not attribute:
            self.skipTest("no Item Attribute on this bench")

        value = frappe.db.get_value("Item Attribute Value", {"parent": attribute},
                                    "attribute_value")

        template = self.make_item("_F25-TMPL", has_variants=1,
                                  attributes=[{"attribute": attribute}],
                                  custom_slug="f25-tmpl")
        variant = create_variant(template.name, {attribute: value})
        variant.insert(ignore_permissions=True)

        return template, variant

    def test_a_variant_template_may_carry_filters(self):
        """The family is what the catalogue lists, so it is where facets belong."""

        f = self.electrical()
        template, _ = self.family()

        template.custom_storefront_filter_set = f.item_set.name
        template.set("custom_storefront_filters", [
            {"filter": f.colour.name, "filter_value": f.vals["red"].name},
        ])
        template.save(ignore_permissions=True)

        template.reload()
        self.assertEqual(len(template.custom_storefront_filters), 1)

    def test_a_generated_variant_cannot_carry_filters(self):
        """Merchants never duplicate facets onto every generated SKU."""

        f = self.electrical()
        _, variant = self.family()

        variant.custom_storefront_filter_set = f.item_set.name
        variant.set("custom_storefront_filters", [
            {"filter": f.colour.name, "filter_value": f.vals["red"].name},
        ])

        with self.assertRaises(frappe.ValidationError):
            variant.save(ignore_permissions=True)

    def test_variant_attributes_are_not_storefront_filters(self):
        """Two systems, no shared state."""

        _, variant = self.family()

        self.assertTrue(variant.attributes, "the fixture has no variant attributes")
        self.assertFalse(variant.get("custom_storefront_filters"))


if __name__ == "__main__":
    unittest.main()
