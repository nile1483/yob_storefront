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


# =========================================================================
# BROWSE CATEGORY CHIPS  (Phase 28A)
# =========================================================================

class BrowseCategoriesCase(CategoryMetadataCase):
    """`get_browse_categories` -- every enabled category, flat, for `/products`.

    It exists because `get_categories` answers ONE level at a time. Drawing a chip
    row from that would need one request per node, so the chips get a shape of
    their own -- and that shape is metadata ONLY: no Item query, no price, no
    stock, no SellingContext.
    """

    def browse(self):
        frappe.clear_cache()
        response = inspect.unwrap(self.api.get_browse_categories)(auth_context={})
        self.assertNotIn("errors", response, f"browse categories failed: {response}")
        return response

    def rows(self):
        return {row["slug"]: row for row in self.browse()["data"]["categories"]}

    # ------------------------------------------------------------- coverage

    def tree(self):
        """Groups at depth 0 and 1, listable categories at depths 0, 1 and 2.

        Shaped so "groups are excluded" and "any depth is included" are proven by
        the SAME fixture -- otherwise excluding groups could quietly collapse the
        answer to one level and both assertions would still pass separately.
        """

        top = self.make_category("c28-top", is_group=1)
        mid = self.make_category("c28-mid", is_group=1, parent=top)

        return frappe._dict(
            top=top,
            mid=mid,
            flat=self.make_category("c28-flat"),
            under_top=self.make_category("c28-under-top", parent=top),
            leaf=self.make_category("c28-leaf", parent=mid),
        )

    def test_listable_categories_from_every_level_are_returned(self):
        """Depth does not decide whether a chip exists -- listability does."""

        t = self.tree()
        rows = self.rows()

        for slug in ("c28-flat", "c28-under-top", "c28-leaf"):
            self.assertIn(slug, rows, f"{slug} is missing from the browse chips")

        self.assertEqual(rows["c28-flat"]["level"], 0)
        self.assertEqual(rows["c28-under-top"]["level"], 1)
        self.assertEqual(rows["c28-leaf"]["level"], 2)

        self.assertIsNone(rows["c28-flat"]["parent_category"])
        self.assertEqual(rows["c28-under-top"]["parent_category"], t.top)
        self.assertEqual(rows["c28-leaf"]["parent_category"], t.mid)

    def test_group_categories_are_excluded(self):
        """`get_items` refuses a group, so a group must never become a chip."""

        self.tree()
        rows = self.rows()

        self.assertNotIn("c28-top", rows, "a group category was published as a chip")
        self.assertNotIn("c28-mid", rows, "a group category was published as a chip")

    def test_excluding_groups_does_not_collapse_the_tree_to_one_level(self):
        """Only the non-listable NODES drop out; their listable descendants stay.

        The regression this guards: implementing "listable only" as "roots only",
        or as "stop at the first group", would hide most of the catalogue's
        categories and nothing else in this file would notice.
        """

        self.tree()

        levels = {row["level"] for row in self.browse()["data"]["categories"]}

        self.assertTrue({0, 1, 2}.issubset(levels),
                        f"listable categories below a group were dropped: {sorted(levels)}")

    def test_no_descendant_aggregation_is_implied(self):
        """A group is not republished as a chip that lists its subtree.

        `get_items` has no descendant recursion -- a category scope is exactly one
        category -- so a chip meaning "everything under Power Tools" would promise
        something the listing cannot answer.
        """

        t = self.tree()
        self.make_item("C28-UNDER-LEAF", t.leaf)

        rows = self.rows()

        self.assertNotIn("c28-top", rows)
        self.assertNotIn("c28-mid", rows)
        self.assertEqual(
            [row["slug"] for row in self.browse()["data"]["categories"]
             if row["name"] in (t.top, t.mid)], [])

    def test_every_published_chip_is_accepted_by_get_items(self):
        """THE contract, proven end to end rather than asserted about flags.

        A chip that answers `category_not_listable` when clicked is the defect
        this correction exists to remove, so the check is to actually call the
        listing with what was published.
        """

        t = self.tree()
        mine = {t.flat, t.under_top, t.leaf}

        published = [row for row in self.browse()["data"]["categories"]
                     if row["name"] in mine]

        self.assertEqual(len(published), 3, "a listable fixture was not published")

        for row in published:
            listing = inspect.unwrap(self.api.get_items)(
                auth_context={}, scope_value=row["slug"])
            self.assertNotIn("errors", listing,
                             f"chip {row['slug']!r} was refused by get_items: {listing}")

        # The other half of the guarantee: the groups that were withheld are
        # exactly the ones the listing would have refused.
        for group_slug in ("c28-top", "c28-mid"):
            refused = inspect.unwrap(self.api.get_items)(
                auth_context={}, scope_value=group_slug)
            self.assertEqual(refused["errors"][0]["code"], "category_not_listable")

    def test_a_disabled_category_is_excluded(self):
        live = self.make_category("c28-live")
        dead = self.make_category("c28-dead")
        frappe.db.set_value("Category", dead, "is_active", 0)

        rows = self.rows()

        self.assertIn("c28-live", rows)
        self.assertNotIn("c28-dead", rows)

    def test_a_disabled_parent_does_not_hide_an_enabled_child(self):
        """Matching every other storefront path, which decides per category.

        `get_categories`, `get_category` and `get_items` all read this category's
        own `is_active` and never walk its ancestors. Cascading here would make
        one endpoint disagree with the rest, so the child stays -- and its `level`
        still counts the disabled parent, because depth is a fact about the
        taxonomy rather than about who is enabled today.
        """

        parent = self.make_category("c28-off-parent", is_group=1)
        child = self.make_category("c28-on-child", parent=parent)
        frappe.db.set_value("Category", parent, "is_active", 0)

        rows = self.rows()

        self.assertNotIn("c28-off-parent", rows)
        self.assertIn("c28-on-child", rows)
        self.assertEqual(rows["c28-on-child"]["level"], 1)

        # `parent_category` still names the withheld parent. It is a grouping key,
        # not a chip reference: rewriting it to the nearest published ancestor
        # would misreport the taxonomy, and blanking it would lose real structure.
        self.assertEqual(rows["c28-on-child"]["parent_category"], parent)

    def test_a_category_with_no_slug_is_excluded(self):
        """No public identity means nothing `get_items` could ever resolve."""

        unrouted = frappe.get_doc({
            "doctype": "Category", "category_name": "c28-unrouted",
            "is_active": 1, "is_group": 0}).insert(ignore_permissions=True).name

        names = {row["name"] for row in self.browse()["data"]["categories"]}

        self.assertNotIn(unrouted, names)

    def test_no_synthetic_all_category_is_invented(self):
        """`All` is client state: catalogue-wide browsing is the ABSENCE of a
        category, not a category the merchant owns."""

        self.make_category("c28-real")

        rows = self.browse()["data"]["categories"]

        for row in rows:
            self.assertNotIn(row["slug"], (None, "", "all"))
            self.assertTrue(frappe.db.exists("Category", row["name"]),
                            f"{row['name']} is not a stored Category")

    # ------------------------------------------------------------- shape

    def test_the_payload_is_metadata_only(self):
        self.make_category("c28-shape")

        row = self.rows()["c28-shape"]

        self.assertEqual(
            set(row),
            {"name", "category_name", "slug", "parent_category", "display_order",
             "level"})

    def test_is_group_is_not_published(self):
        """Every row is listable by construction, so the flag could only be 0.

        Publishing a constant invites a client to branch on a case that cannot
        occur, and it would have to keep being true. Adding the field back if
        group chips ever gain a meaning is additive; removing it later would not be.
        """

        self.tree()

        for row in self.browse()["data"]["categories"]:
            self.assertNotIn("is_group", row)

    def test_ordering_is_deterministic(self):
        first = self.make_category("c28-ord-a")
        second = self.make_category("c28-ord-b")
        frappe.db.set_value("Category", first, "display_order", 2)
        frappe.db.set_value("Category", second, "display_order", 1)

        slugs = [row["slug"] for row in self.browse()["data"]["categories"]]

        self.assertLess(slugs.index("c28-ord-b"), slugs.index("c28-ord-a"),
                        "display_order was not honoured")

        # Shallowest first, so a chip row can be drawn without sorting again.
        levels = [row["level"] for row in self.browse()["data"]["categories"]]
        self.assertEqual(levels, sorted(levels))

    def test_the_count_matches_the_payload(self):
        self.make_category("c28-count")

        response = self.browse()

        self.assertEqual(response["meta"]["count"],
                         len(response["data"]["categories"]))

    # ------------------------------------------------------------- cost

    def test_no_item_pricing_or_listing_work_happens(self):
        """The Phase 22B guard, applied to the chips before they can regress."""

        category = self.make_category("c28-cost")
        for i in range(5):
            self.make_item(f"C28-COST{i}", category)

        with patch("yob_storefront.services.pricing_service.get_item_pricing",
                   side_effect=AssertionError("browse categories priced an Item")),              patch("yob_storefront.services.catalog_listing_service.list_items",
                   side_effect=AssertionError("browse categories ran the listing")),              patch("yob_storefront.services.catalog_listing_service.fetch_candidates",
                   side_effect=AssertionError("browse categories queried candidates")):
            rows = self.browse()["data"]["categories"]

        self.assertTrue(any(row["slug"] == "c28-cost" for row in rows))

    def test_no_item_table_is_queried(self):
        """Not merely "no pricing" -- the chips must not read Item at all."""

        category = self.make_category("c28-noitem")
        for i in range(5):
            self.make_item(f"C28-NOITEM{i}", category)

        queried = []
        real_get_all = frappe.get_all

        def spy(doctype, *args, **kwargs):
            if doctype == "Item":
                queried.append(doctype)
            return real_get_all(doctype, *args, **kwargs)

        with patch.object(frappe, "get_all", side_effect=spy):
            self.browse()

        self.assertEqual(queried, [], "the browse chips read the Item table")

    def test_browse_failures_leak_nothing(self):
        with patch.object(self.api, "get_storefront_customer",
                          side_effect=frappe.PermissionError("nope")):
            response = inspect.unwrap(self.api.get_browse_categories)(auth_context={})

        self.assertEqual(response["errors"][0]["code"], "application_access_denied")

        blob = frappe.as_json(response)
        for leak in ("Traceback", "_server_messages", "/app/", "href", "tabCategory"):
            self.assertNotIn(leak, blob)


if __name__ == "__main__":
    unittest.main()
