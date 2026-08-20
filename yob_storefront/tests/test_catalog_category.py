# Copyright (c) 2026, YOB and Shayona
"""`catalog.get_category` -- category metadata only, no products.

WHAT THIS PINS
--------------
The embedded product payload was retired in Phase 22B-3. `get_category` used to
load every Item in a leaf category and price each one through a throwaway Sales
Order: Phase 22A measured ~51 ms per Item, growing linearly (100 items in 5.1 s),
with one end-of-life Item returning a 500 for the entire category.

The strongest guard against that returning is not "the response has no `items`
key" -- someone could delete the key and keep the loop. It is **zero pricing
calls**, regardless of how many Items the category holds. That is what the class
below asserts, and it is why the assertion is on the pricing seam rather than on
the response shape.

Category metadata, subcategories, not-found behaviour and the authorization
boundary are unchanged and are re-asserted here so the cleanup cannot quietly
take them with it.
"""

import inspect
import unittest
from unittest.mock import patch

import frappe

CUSTOMER = "YOB Demo Buyer"
SEED_ITEM = "YOB-BOLT-M10"


def _seeded():
    return bool(frappe.db.exists("Customer", CUSTOMER) and frappe.db.exists("Item", SEED_ITEM))


class CategoryMetadataCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data on the test site")

    def setUp(self):
        frappe.set_user("Administrator")
        from yob_storefront.api import catalog as catalog_api

        self.api = catalog_api
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
        self.assertEqual(self.commits, [], "a commit escaped the test rollback")

    # ------------------------------------------------------------- fixtures

    def make_category(self, slug, is_group=0, parent=None):
        doc = {
            "doctype": "Category", "category_name": slug, "slug": slug,
            "is_active": 1, "is_group": is_group,
        }
        if parent:
            doc["parent_category"] = parent
        return frappe.get_doc(doc).insert(ignore_permissions=True).name

    def make_item(self, code, category, price=100):
        item = frappe.get_doc({
            "doctype": "Item", "item_code": code, "item_name": code,
            "item_group": self.item_group, "stock_uom": self.uom, "is_stock_item": 0,
            "is_sales_item": 1, "gst_hsn_code": self.hsn, "custom_slug": code.lower(),
            "custom_category": category,
        }).insert(ignore_permissions=True).name
        frappe.get_doc({
            "doctype": "Item Price", "item_code": item, "price_list": self.price_list,
            "price_list_rate": price, "selling": 1, "uom": self.uom,
        }).insert(ignore_permissions=True)
        return item

    # ------------------------------------------------------------- helpers

    def category(self, slug):
        frappe.clear_cache()
        return inspect.unwrap(self.api.get_category)(slug=slug, auth_context={})

    def pricing_calls_for(self, slug):
        """Call get_category with the pricing seam spied on.

        The seam is `pricing_service.get_item_pricing` itself, not the name
        re-exported into `catalog`, so the count stays honest even if the import
        style in catalog.py changes.
        """
        calls = []
        with patch("yob_storefront.services.pricing_service.get_item_pricing",
                   side_effect=AssertionError("get_category priced an Item")) as spy:
            spy.side_effect = lambda *a, **k: calls.append(k.get("item_code"))
            response = self.category(slug)
        return response, calls

    # ============================================== THE REGRESSION

    def test_leaf_category_with_many_items_prices_nothing(self):
        """THE guard. Pricing work must be zero no matter how many Items exist."""

        cat = self.make_category("c22-many")
        for i in range(12):
            self.make_item(f"C22-M{i:02d}", cat)

        response, calls = self.pricing_calls_for("c22-many")

        self.assertNotIn("errors", response, f"category failed: {response}")
        self.assertEqual(calls, [],
                         f"get_category priced {len(calls)} Items; the retired "
                         f"unbounded path is back")

    def test_pricing_work_is_independent_of_item_count(self):
        """0, 1 and many Items must all cost the same: nothing."""

        empty = self.make_category("c22-empty")
        one = self.make_category("c22-one")
        self.make_item("C22-ONE", one)
        many = self.make_category("c22-lots")
        for i in range(30):
            self.make_item(f"C22-L{i:02d}", many)

        for slug in ("c22-empty", "c22-one", "c22-lots"):
            response, calls = self.pricing_calls_for(slug)
            self.assertNotIn("errors", response, f"{slug} failed: {response}")
            self.assertEqual(calls, [], f"{slug} performed pricing work")

    def test_no_item_query_runs(self):
        """Not merely "no pricing" -- the category must not read Item at all."""

        cat = self.make_category("c22-noquery")
        for i in range(5):
            self.make_item(f"C22-Q{i}", cat)

        queried = []
        real_get_all = frappe.get_all

        def spy(doctype, *args, **kwargs):
            if doctype == "Item":
                queried.append(doctype)
            return real_get_all(doctype, *args, **kwargs)

        with patch.object(frappe, "get_all", side_effect=spy):
            response = self.category("c22-noquery")

        self.assertNotIn("errors", response)
        self.assertEqual(queried, [], "get_category still queries the Item table")

    def test_an_item_that_used_to_break_the_category_is_now_irrelevant(self):
        """A past-end-of-life Item once returned a 500 for the whole category.

        It cannot any more, because no Item is looked at.
        """

        from frappe.utils import add_days, today

        cat = self.make_category("c22-eol")
        self.make_item("C22-GOOD", cat)
        bad = self.make_item("C22-EOL", cat)
        frappe.db.set_value("Item", bad, "end_of_life", add_days(today(), -1))

        response, calls = self.pricing_calls_for("c22-eol")

        self.assertNotIn("errors", response, "a bad Item still breaks get_category")
        self.assertEqual(calls, [])

    # ============================================== CONTRACT

    def test_items_and_item_count_are_gone(self):
        cat = self.make_category("c22-shape")
        self.make_item("C22-S1", cat)

        response = self.category("c22-shape")
        data = response["data"]

        self.assertNotIn("items", data, "the retired product payload came back")
        self.assertNotIn("item_count", response.get("meta") or {},
                         "item_count implies counting Items")
        self.assertIn("category", data)
        self.assertIn("subcategories", data)

    def test_category_metadata_is_preserved(self):
        self.make_category("c22-meta")
        data = self.category("c22-meta")["data"]

        category = data["category"]
        for field in ("name", "category_name", "slug", "thumbnail", "banner",
                      "meta_title", "meta_description", "description",
                      "is_group", "parent_category"):
            self.assertIn(field, category, f"category metadata lost `{field}`")

    def test_group_category_still_returns_children(self):
        parent = self.make_category("c22-parent", is_group=1)
        self.make_category("c22-kid-a", parent=parent)
        self.make_category("c22-kid-b", parent=parent)

        response, calls = self.pricing_calls_for("c22-parent")
        data = response["data"]

        self.assertEqual(calls, [], "a group category performed pricing work")
        self.assertEqual(
            sorted(c["slug"] for c in data["subcategories"]), ["c22-kid-a", "c22-kid-b"])
        self.assertEqual(response["meta"]["subcategory_count"], 2)

    def test_leaf_category_returns_no_subcategories(self):
        self.make_category("c22-leaf")
        response = self.category("c22-leaf")

        self.assertEqual(response["data"]["subcategories"], [])
        self.assertEqual(response["meta"]["subcategory_count"], 0)

    def test_unknown_and_inactive_categories_still_answer_not_found(self):
        self.assertEqual(self.category("no-such-slug")["errors"][0]["code"],
                         "category_not_found")

        hidden = self.make_category("c22-hidden")
        frappe.db.set_value("Category", hidden, "is_active", 0)
        self.assertEqual(self.category("c22-hidden")["errors"][0]["code"],
                         "category_not_found")

    def test_missing_slug_is_a_validation_error(self):
        response = inspect.unwrap(self.api.get_category)(auth_context={})
        self.assertEqual(response["errors"][0]["code"], "validation_failed")

    def test_expected_failures_leak_nothing(self):
        blob = frappe.as_json(self.category("no-such-slug"))
        for leak in ("Traceback", "_server_messages", "/app/", "href", "tabItem"):
            self.assertNotIn(leak, blob)

    def test_products_are_served_by_get_items_instead(self):
        """The other half of the contract: the products still exist, elsewhere."""

        cat = self.make_category("c22-moved")
        item = self.make_item("C22-MOVED", cat)

        self.assertNotIn("items", self.category("c22-moved")["data"])

        listing = inspect.unwrap(self.api.get_items)(
            auth_context={}, scope_value="c22-moved")
        self.assertNotIn("errors", listing, f"get_items failed: {listing}")
        self.assertEqual([i["name"] for i in listing["data"]["items"]], [item])


if __name__ == "__main__":
    unittest.main()
