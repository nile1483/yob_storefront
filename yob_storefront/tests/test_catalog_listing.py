# Copyright (c) 2026, YOB and Shayona
"""`catalog.get_items` -- bounded listing, price eligibility, cursor paging.

WHAT THIS REPLACES
------------------
Phase 22A measured `get_category()`: every Item in a category loaded, one temporary
Sales Order per Item (~51 ms each), 100 items in 5.1 s growing linearly, and a
single end-of-life Item aborting the entire response with a 500.

WHAT IS PINNED HERE
-------------------
* an Item is visible only when an applicable **base Item Price > 0** resolves for
  this customer, BEFORE Pricing Rules -- so a fixed-rate rule can no longer make an
  unpriced Item appear;
* eligibility is about the BASE price, so a 100% discount rule still lists the Item
  even though the final rate is 0;
* expensive pricing stays near the page size, never the category size;
* one bad Item is skipped, not fatal;
* the cursor neither duplicates nor drops Items.

ISOLATION
---------
Every fixture is created inside the test and rolled back. `server_error()` calls
`frappe.db.rollback()`, which destroys savepoints, so teardown is a full rollback.
`frappe.db.commit` is neutered: nothing here may outlive the test.
"""

import inspect
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_days, today

CUSTOMER = "YOB Demo Buyer"
SEED_ITEM = "YOB-BOLT-M10"


def _seeded():
    return bool(frappe.db.exists("Customer", CUSTOMER) and frappe.db.exists("Item", SEED_ITEM))


class ListingCase(unittest.TestCase):
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
        self.cat = self.make_category("t22-cat")

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()
        self.assertEqual(self.commits, [], "a commit escaped the test rollback")

    # ------------------------------------------------------------- fixtures

    def make_category(self, slug, is_group=0):
        return frappe.get_doc({
            "doctype": "Category", "category_name": slug, "slug": slug,
            "is_active": 1, "is_group": is_group,
        }).insert(ignore_permissions=True).name

    def make_item(self, code, category=None, price=100, **kw):
        """An Item that is catalog-eligible unless `price` is None or overridden."""
        doc = {
            "doctype": "Item", "item_code": code, "item_name": kw.pop("item_name", code),
            "item_group": self.item_group, "stock_uom": self.uom, "is_stock_item": 0,
            "is_sales_item": 1, "gst_hsn_code": self.hsn, "custom_slug": code.lower(),
            "custom_category": category if category is not None else self.cat,
        }
        doc.update(kw)
        item = frappe.get_doc(doc).insert(ignore_permissions=True)
        if price is not None:
            self.make_price(item.name, price)
        return item.name

    def make_price(self, item_code, rate, **kw):
        doc = {
            "doctype": "Item Price", "item_code": item_code, "price_list": self.price_list,
            "price_list_rate": rate, "selling": 1, "uom": self.uom,
        }
        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True).name

    def make_rule(self, item_code, rate=None, discount=None):
        doc = {
            "doctype": "Pricing Rule", "title": f"T22 Rule {item_code}",
            "apply_on": "Item Code", "price_or_product_discount": "Price",
            "selling": 1, "company": frappe.db.get_value("Company", {}, "name"),
            "currency": "INR", "items": [{"item_code": item_code}],
            "valid_from": add_days(today(), -1),
        }
        if rate is not None:
            doc.update({"rate_or_discount": "Rate", "rate": rate})
        else:
            doc.update({"rate_or_discount": "Discount Percentage", "discount_percentage": discount})
        return frappe.get_doc(doc).insert(ignore_permissions=True).name

    # ------------------------------------------------------------- helpers

    def listing(self, **kw):
        kw.setdefault("scope_value", "t22-cat")
        frappe.clear_cache()
        return inspect.unwrap(self.api.get_items)(auth_context={}, **kw)

    def names(self, response):
        self.assertNotIn("errors", response, f"listing failed: {response}")
        return [i["name"] for i in response["data"]["items"]]

    def code_of(self, response):
        return response["errors"][0]["code"] if "errors" in response else None

    def context(self):
        # Re-read the Customer rather than reusing the doc captured in setUp:
        # tests change `default_price_list` with db_set, which updates the row and
        # the instance it was called on, never a copy someone else is holding.
        from yob_storefront.services.catalog_listing_service import PricingContext
        frappe.clear_document_cache("Customer", CUSTOMER)
        return PricingContext(frappe.get_doc("Customer", CUSTOMER))

    def eligible(self, item_code):
        from yob_storefront.services.catalog_listing_service import is_catalog_eligible
        frappe.clear_cache()
        row = frappe.db.get_value("Item", item_code, ["stock_uom", "variant_of"], as_dict=True)
        return is_catalog_eligible(self.context(), item_code, row.stock_uom, row.variant_of)


# =========================================================================
# BASE ITEM PRICE ELIGIBILITY
# =========================================================================

class EligibilityCase(ListingCase):
    """The visibility rule: an applicable base Item Price > 0, before Pricing Rules."""

    def test_positive_generic_price_is_eligible(self):
        self.assertTrue(self.eligible(self.make_item("T22-GEN", price=100)))

    def test_no_item_price_is_excluded(self):
        item = self.make_item("T22-NOPRICE", price=None)
        self.assertFalse(self.eligible(item))
        self.assertNotIn(item, self.names(self.listing()))

    def test_zero_item_price_is_excluded(self):
        item = self.make_item("T22-ZERO", price=0)
        self.assertFalse(self.eligible(item))
        self.assertNotIn(item, self.names(self.listing()))

    def test_fixed_rate_rule_without_item_price_is_excluded(self):
        """Phase 22A proved ERPNext yields rate=999 here. YOB still excludes it."""
        item = self.make_item("T22-RULEONLY", price=None)
        self.make_rule(item, rate=999)

        self.assertFalse(self.eligible(item), "a Pricing Rule made an unpriced Item visible")
        self.assertNotIn(item, self.names(self.listing()))

    def test_zero_price_plus_fixed_rule_is_excluded(self):
        item = self.make_item("T22-ZERORULE", price=0)
        self.make_rule(item, rate=777)

        self.assertFalse(self.eligible(item))
        self.assertNotIn(item, self.names(self.listing()))

    def test_full_discount_rule_stays_eligible_even_at_zero_final_rate(self):
        """THE distinction: eligibility is about the BASE price, not the final rate."""
        item = self.make_item("T22-FREE", price=100)
        self.make_rule(item, discount=100)

        self.assertTrue(self.eligible(item))

        row = next(i for i in self.listing()["data"]["items"] if i["name"] == item)
        self.assertEqual(row["base_price"], 100.0)
        self.assertEqual(row["rate"], 0.0, "ERPNext should have discounted to zero")

    def test_customer_specific_price_beats_generic(self):
        item = self.make_item("T22-SPEC", price=100)
        self.make_price(item, 60, customer=CUSTOMER)
        frappe.clear_cache()

        from yob_storefront.services.catalog_listing_service import resolve_base_item_price
        self.assertEqual(resolve_base_item_price(self.context(), item, self.uom), 60)

    def test_customer_b_does_not_get_customer_a_price(self):
        item = self.make_item("T22-ISO", price=100)
        self.make_price(item, 60, customer=CUSTOMER)
        other = frappe.get_doc({
            "doctype": "Customer", "customer_name": "T22 Other",
            "customer_type": "Company",
            "customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
            "territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
        }).insert(ignore_permissions=True)
        frappe.clear_cache()

        from yob_storefront.services.catalog_listing_service import (
            PricingContext, resolve_base_item_price)
        self.assertEqual(resolve_base_item_price(PricingContext(other), item, self.uom), 100,
                         "Customer B received Customer A's private rate")

    def test_future_price_is_excluded(self):
        item = self.make_item("T22-FUT", price=None)
        self.make_price(item, 50, valid_from=add_days(today(), 10))
        self.assertFalse(self.eligible(item))

    def test_expired_price_is_excluded(self):
        item = self.make_item("T22-EXP", price=None)
        self.make_price(item, 50, valid_from=add_days(today(), -10),
                        valid_upto=add_days(today(), -1))
        self.assertFalse(self.eligible(item))

    def test_currently_valid_dated_price_is_eligible(self):
        item = self.make_item("T22-VALID", price=None)
        self.make_price(item, 50, valid_from=add_days(today(), -5),
                        valid_upto=add_days(today(), 5))
        self.assertTrue(self.eligible(item))

    def test_blank_uom_price_is_eligible(self):
        """A blank Item Price UOM matches any UOM in ERPNext's own query."""
        item = self.make_item("T22-BLANKUOM", price=None)
        self.make_price(item, 50, uom=None)
        self.assertTrue(self.eligible(item))

    def test_incompatible_uom_only_price_is_excluded(self):
        item = self.make_item("T22-BADUOM", price=None)
        other_uom = frappe.db.get_value("UOM", {"name": ["!=", self.uom]}, "name")
        doc = frappe.get_doc("Item", item)
        doc.append("uoms", {"uom": other_uom, "conversion_factor": 10})
        doc.save(ignore_permissions=True)
        self.make_price(item, 80, uom=other_uom)

        self.assertFalse(self.eligible(item))

    def test_price_list_resolution_prefers_customer_then_group(self):
        """Customer -> Customer Group -> Selling Settings (Phase 22A section C)."""
        alt = frappe.get_doc({
            "doctype": "Price List", "price_list_name": "T22 Alt", "selling": 1,
            "enabled": 1, "currency": "INR"}).insert(ignore_permissions=True).name

        cust = frappe.get_doc("Customer", CUSTOMER)
        cust.db_set("default_price_list", alt, update_modified=False)
        frappe.clear_cache()
        self.assertEqual(self.context().price_list, alt, "Customer default did not win")

        cust.db_set("default_price_list", None, update_modified=False)
        grp = frappe.get_doc("Customer Group", cust.customer_group)
        grp.db_set("default_price_list", alt, update_modified=False)
        frappe.clear_cache()
        self.assertEqual(self.context().price_list, alt, "Customer Group default did not win")

        grp.db_set("default_price_list", None, update_modified=False)
        frappe.clear_cache()
        self.assertEqual(self.context().price_list, self.price_list,
                         "did not fall back to Selling Settings")


class FallbackPriceListCase(ListingCase):
    """`fallback_to_default_price_list` -- restored in tearDown even on failure."""

    def setUp(self):
        super().setUp()
        self.original_fallback = frappe.get_single(
            "Selling Settings").fallback_to_default_price_list
        self.addCleanup(self._restore_fallback)

        self.alt = frappe.get_doc({
            "doctype": "Price List", "price_list_name": "T22 Fallback Alt", "selling": 1,
            "enabled": 1, "currency": "INR"}).insert(ignore_permissions=True).name
        cust = frappe.get_doc("Customer", CUSTOMER)
        cust.db_set("default_price_list", self.alt, update_modified=False)

    def _restore_fallback(self):
        frappe.db.set_single_value(
            "Selling Settings", "fallback_to_default_price_list", self.original_fallback)

    def set_fallback(self, value):
        frappe.db.set_single_value("Selling Settings", "fallback_to_default_price_list", value)
        frappe.clear_cache()

    def test_selected_list_miss_with_fallback_disabled_is_excluded(self):
        item = self.make_item("T22-FB-OFF", price=100)     # price is on the DEFAULT list
        self.set_fallback(0)
        self.assertFalse(self.eligible(item))

    def test_selected_list_miss_with_fallback_enabled_is_included(self):
        item = self.make_item("T22-FB-ON", price=100)
        self.set_fallback(1)
        self.assertTrue(self.eligible(item))

    def test_zero_on_selected_list_falls_back_like_erpnext(self):
        """Phase 22A left this open; ERPNext's own guards settle it.

        `get_item_details.py:125` falls back when `not out.price_list_rate`, and 0 is
        falsy -- so a zero on the selected list DOES fall through to the default
        list, unlike the variant fallback which tests `is None`. The resolver mirrors
        that rather than tidying the inconsistency away.
        """
        item = self.make_item("T22-FB-ZERO", price=100)          # 100 on default list
        self.make_price(item, 0, price_list=self.alt)            # 0 on selected list

        self.set_fallback(1)
        from yob_storefront.services.catalog_listing_service import resolve_base_item_price
        self.assertEqual(resolve_base_item_price(self.context(), item, self.uom), 100,
                         "a zero on the selected list must fall back, as ERPNext does")
        self.assertTrue(self.eligible(item))

        self.set_fallback(0)
        self.assertFalse(self.eligible(item), "with fallback off the zero stands")


# =========================================================================
# LISTING QUERY
# =========================================================================

class ListingQueryCase(ListingCase):

    def test_scope_is_exactly_one_category_without_recursion(self):
        parent = self.make_category("t22-parent", is_group=1)
        child = self.make_category("t22-child")
        frappe.db.set_value("Category", child, "parent_category", parent)
        inside = self.make_item("T22-IN")
        self.make_item("T22-CHILD", category=child)

        names = self.names(self.listing())
        self.assertEqual(names, [inside], "descendant items leaked into the scope")

    def test_group_category_is_not_listable(self):
        self.make_category("t22-group", is_group=1)
        self.assertEqual(self.code_of(self.listing(scope_value="t22-group")),
                         "category_not_listable")

    def test_unknown_category(self):
        self.assertEqual(self.code_of(self.listing(scope_value="nope")), "category_not_found")

    def test_reserved_scopes_are_refused(self):
        for scope in ("collection", "all"):
            self.assertEqual(self.code_of(self.listing(scope_type=scope)), "unsupported_scope")

    def test_missing_scope_is_the_whole_catalogue(self):
        """Inverted deliberately in Phase 28A; this asserted `validation_failed`.

        An absent `scope_value` used to be a client bug because there was nowhere
        to browse without a category. `/products` is that place, so the absence is
        now the catalogue-wide scope -- see `CatalogWideCase` for what it returns.
        """

        inside = self.make_item("T22-SCOPELESS")

        # `newest` so a freshly created fixture is on the first page: the test
        # site's catalogue holds thousands of listable products, and under
        # `name_asc` this one sorts far past page 1. See `CatalogWideCase`.
        self.assertIn(inside, self.names(self.listing(scope_value=None, sort="newest")))

    def test_reserved_scope_types_are_still_unreachable(self):
        """Making the VALUE optional did not open the reserved scope TYPES.

        `all` remains refused: catalogue-wide browsing is the absence of a
        category, not a second addressing mode a client could guess at.
        """

        self.assertEqual(
            self.code_of(self.listing(scope_type="all", scope_value=None)),
            "unsupported_scope")

    def test_search_matches_item_name(self):
        red = self.make_item("T22-A", item_name="Red Cotton Shirt")
        self.make_item("T22-B", item_name="Blue Wool Coat")

        self.assertEqual(self.names(self.listing(search="cotton")), [red])

    def test_multi_word_search_is_AND_not_OR(self):
        both = self.make_item("T22-BOTH", item_name="Red Cotton Shirt")
        self.make_item("T22-ONE", item_name="Red Wool Coat")

        self.assertEqual(self.names(self.listing(search="red cotton")), [both])

    def test_search_matches_the_item_code_too(self):
        """Changed deliberately in Phase 26A-1; this test previously asserted the
        opposite.

        Header search must find a product by a code fragment a buyer has read off
        a quote or a previous order, and `get_items` shares the one predicate --
        so the listing gained the same reach rather than the typeahead getting a
        private rule of its own.
        """

        coded = self.make_item("T22-ZZUNIQUE", item_name="Plain Shirt")

        self.assertEqual(self.names(self.listing(search="ZZUNIQUE")), [coded])

    def test_a_word_may_be_satisfied_by_either_column(self):
        """AND across words, OR across the two identity columns."""

        both = self.make_item("T22-HEX10", item_name="Hex Bolt")
        self.make_item("T22-HEX99", item_name="Washer")

        # "hex" from the name, "10" from the code.
        self.assertEqual(self.names(self.listing(search="hex 10")), [both])

    def test_search_still_ignores_everything_but_name_and_code(self):
        """No description, category, Item Group or Brand search crept in."""

        self.make_item("T22-DESC", item_name="Plain Shirt",
                       description="ZZDESCRIPTIONONLY fabric")

        self.assertEqual(self.names(self.listing(search="ZZDESCRIPTIONONLY")), [])

    def test_blank_search_returns_everything(self):
        a = self.make_item("T22-S1")
        b = self.make_item("T22-S2")
        self.assertEqual(sorted(self.names(self.listing(search="   "))), sorted([a, b]))

    def test_wildcard_characters_are_literal(self):
        self.make_item("T22-PCT", item_name="Plain Shirt")
        # If % leaked into LIKE syntax this would match everything.
        self.assertEqual(self.names(self.listing(search="%")), [])

    def test_excessive_search_is_rejected(self):
        self.assertEqual(self.code_of(self.listing(search="x" * 200)), "search_too_long")
        self.assertEqual(self.code_of(self.listing(search="a b c d e f g h")), "search_too_long")

    def test_empty_filters_accepted_and_non_empty_rejected(self):
        self.make_item("T22-F1")
        self.assertNotIn("errors", self.listing(filters=[]))
        self.assertNotIn("errors", self.listing(filters=None))
        self.assertEqual(self.code_of(self.listing(filters='[{"key":"color"}]')),
                         "unsupported_filters")

    def test_sort_modes(self):
        a = self.make_item("T22-X1", item_name="Alpha")
        b = self.make_item("T22-X2", item_name="Beta")

        self.assertEqual(self.names(self.listing(sort="name_asc")), [a, b])
        self.assertEqual(self.names(self.listing(sort="name_desc")), [b, a])
        # `newest` is creation desc; b was created after a.
        self.assertEqual(self.names(self.listing(sort="newest")), [b, a])

    def test_default_sort_is_name_asc(self):
        a = self.make_item("T22-Y1", item_name="Alpha")
        b = self.make_item("T22-Y2", item_name="Beta")
        self.assertEqual(self.names(self.listing()), [a, b])

    def test_identical_names_break_ties_deterministically(self):
        first = self.make_item("T22-DUP-A", item_name="Same Name")
        second = self.make_item("T22-DUP-B", item_name="Same Name")
        # Tie broken by Item `name`, so the order is stable rather than arbitrary.
        self.assertEqual(self.names(self.listing(sort="name_asc")), [first, second])
        self.assertEqual(self.names(self.listing(sort="name_desc")), [second, first])

    def test_invalid_sort_rejected(self):
        for bad in ("price_asc", "modified desc", "name; DROP TABLE tabItem"):
            self.assertEqual(self.code_of(self.listing(sort=bad)), "unsupported_sort")

    def test_page_size_bounds(self):
        for i in range(3):
            self.make_item(f"T22-P{i}")

        self.assertEqual(self.listing()["data"]["pagination"]["page_size"], 24)
        self.assertEqual(len(self.names(self.listing(page_size=1))), 1)

        # Default and maximum are the same number since Phase 28A.
        self.assertNotIn("errors", self.listing(page_size=24))

        # 48 was accepted before Phase 28A. It is refused now, and refused the
        # same way every other out-of-range value always was -- never clamped to
        # 24, because a silent clamp hides the client bug that produced it.
        for bad in (0, -1, 25, 48, 49, 5000, "abc"):
            self.assertEqual(self.code_of(self.listing(page_size=bad)), "page_size_invalid",
                             f"page_size={bad} was not refused")

    def test_the_page_size_ceiling_also_binds_the_catalogue_wide_browse(self):
        """One rule, both scopes -- the ceiling is a property of `get_items`."""

        self.assertNotIn("errors", self.listing(scope_value=None, page_size=24))
        self.assertEqual(
            self.code_of(self.listing(scope_value=None, page_size=25)),
            "page_size_invalid")


# =========================================================================
# CURSOR
# =========================================================================

class CursorCase(ListingCase):

    def make_run(self, count=7):
        return [self.make_item(f"T22-C{i:02d}", item_name=f"Item {i:02d}") for i in range(count)]

    def test_pages_cover_every_item_exactly_once(self):
        expected = self.make_run(7)

        seen, cursor, pages = [], None, 0
        while True:
            pages += 1
            r = self.listing(page_size=3, cursor=cursor)
            seen.extend(self.names(r))
            page = r["data"]["pagination"]
            if not page["has_more"]:
                self.assertIsNone(page["next_cursor"])
                break
            cursor = page["next_cursor"]
            self.assertIsNotNone(cursor)
            self.assertLess(pages, 10, "pagination did not terminate")

        self.assertEqual(seen, expected, "items duplicated, dropped or reordered")
        self.assertEqual(len(seen), len(set(seen)), "duplicate item across pages")

    def test_exact_full_page_boundary_reports_no_more(self):
        self.make_run(3)
        r = self.listing(page_size=3)
        self.assertEqual(len(self.names(r)), 3)
        self.assertFalse(r["data"]["pagination"]["has_more"],
                         "has_more was true with nothing left to show")
        self.assertIsNone(r["data"]["pagination"]["next_cursor"])

    def test_ineligible_items_between_valid_ones_do_not_corrupt_paging(self):
        expected = []
        for i in range(6):
            expected.append(self.make_item(f"T22-M{i:02d}", item_name=f"Good {i:02d}"))
            # Interleaved unpriced items: candidates that never survive Stage 2.
            self.make_item(f"T22-BAD{i:02d}", item_name=f"Good {i:02d}x", price=None)

        seen, cursor = [], None
        for _ in range(6):
            r = self.listing(page_size=2, cursor=cursor)
            seen.extend(self.names(r))
            if not r["data"]["pagination"]["has_more"]:
                break
            cursor = r["data"]["pagination"]["next_cursor"]

        self.assertEqual(seen, expected)

    def test_cursor_is_rejected_when_the_query_changes(self):
        self.make_run(5)
        cursor = self.listing(page_size=2)["data"]["pagination"]["next_cursor"]
        other_cat = self.make_category("t22-other")
        self.make_item("T22-OTHER", category=other_cat)

        self.assertEqual(self.code_of(self.listing(page_size=2, cursor=cursor, sort="newest")),
                         "cursor_invalid")
        self.assertEqual(self.code_of(self.listing(page_size=2, cursor=cursor, search="item")),
                         "cursor_invalid")
        self.assertEqual(
            self.code_of(self.listing(page_size=2, cursor=cursor, scope_value="t22-other")),
            "cursor_invalid")

    def test_cursor_from_another_customer_is_rejected(self):
        self.make_run(5)
        cursor = self.listing(page_size=2)["data"]["pagination"]["next_cursor"]

        other = frappe.get_doc({
            "doctype": "Customer", "customer_name": "T22 Cursor Other",
            "customer_type": "Company",
            "customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
            "territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
        }).insert(ignore_permissions=True)

        with patch.object(self.api, "get_storefront_customer", return_value=other):
            self.assertEqual(self.code_of(self.listing(page_size=2, cursor=cursor)),
                             "cursor_invalid")

    def test_malformed_cursors_are_safe_validation_errors(self):
        self.make_run(3)
        import base64, json

        bad_version = base64.urlsafe_b64encode(
            json.dumps({"v": 99, "b": "x", "k": ["a", "b"]}).encode()).decode().rstrip("=")

        for bad in ("!!!not-base64!!!", "", "x" * 900, bad_version,
                    base64.urlsafe_b64encode(b'{"v":1}').decode().rstrip("=")):
            r = self.listing(page_size=2, cursor=bad)
            if bad == "":
                self.assertNotIn("errors", r, "an empty cursor means 'first page'")
                continue
            self.assertEqual(self.code_of(r), "cursor_invalid", f"cursor {bad[:20]!r}")
            blob = frappe.as_json(r)
            for leak in ("Traceback", "_server_messages", "/app/", "href"):
                self.assertNotIn(leak, blob)


# =========================================================================
# FAILURE ISOLATION
# =========================================================================

class FailureIsolationCase(ListingCase):
    """One bad Item must never blank the page -- the Phase 22A defect."""

    def test_end_of_life_item_is_skipped_not_fatal(self):
        good = [self.make_item(f"T22-OK{i}") for i in range(3)]
        self.make_item("T22-EOL", end_of_life=add_days(today(), -1))

        r = self.listing()
        self.assertNotIn("errors", r, "one bad Item still breaks the listing")
        self.assertEqual(sorted(self.names(r)), sorted(good))

    def test_template_item_is_skipped(self):
        good = self.make_item("T22-REAL")
        template = self.make_item("T22-TMPL", item_name="T22 Tmpl", price=100)
        # `has_variants` is set directly: inserting a template through the controller
        # demands an Item Attribute table, which is fixture noise here. What matters
        # is that a row carrying this flag reaches the candidate query and is
        # excluded by it -- ERPNext refuses to transact a template, and Phase 22A
        # showed one reaching the pricing engine and killing the whole category.
        frappe.db.set_value("Item", template, "has_variants", 1, update_modified=False)
        frappe.clear_document_cache("Item", template)

        self.assertEqual(self.names(self.listing()), [good])

    def test_unexpected_pricing_fault_still_fails_the_request(self):
        """Item-local problems are skipped; a programming fault must NOT be hidden."""
        self.make_item("T22-BOOM")

        from yob_storefront.services import catalog_listing_service as svc
        with patch.object(svc, "get_item_pricing", create=True):
            with patch("yob_storefront.services.pricing_service.get_item_pricing",
                       side_effect=MemoryError("infrastructure fault")):
                r = self.listing()

        self.assertEqual(self.code_of(r), "internal_server_error",
                         "a system fault was silently swallowed as a skipped item")


# =========================================================================
# BOUNDED WORK
# =========================================================================

class BoundedWorkCase(ListingCase):
    """The point of the phase: expensive pricing tracks the PAGE, not the category."""

    def populate(self, count):
        for i in range(count):
            self.make_item(f"T22-BIG{i:03d}", item_name=f"Bulk {i:03d}")

    def count_pricing_calls(self, **kw):
        from yob_storefront.services import catalog_listing_service as svc

        calls = []
        real = svc.price_candidate

        def counting(ctx, row):
            calls.append(row["name"])
            return real(ctx, row)

        with patch.object(svc, "price_candidate", side_effect=counting):
            response = self.listing(**kw)
        return response, calls

    def test_one_page_does_not_price_the_whole_category(self):
        self.populate(110)

        response, calls = self.count_pricing_calls(page_size=24)

        self.assertEqual(len(self.names(response)), 24)
        self.assertTrue(response["data"]["pagination"]["has_more"])
        # page + lookahead, not 110. Phase 22A priced every item in the category.
        self.assertLessEqual(len(calls), 25,
                             f"priced {len(calls)} items to show 24")
        self.assertGreaterEqual(len(calls), 24)

    def test_second_page_is_also_bounded(self):
        self.populate(110)

        first, _ = self.count_pricing_calls(page_size=24)
        cursor = first["data"]["pagination"]["next_cursor"]

        second, calls = self.count_pricing_calls(page_size=24, cursor=cursor)

        self.assertEqual(len(self.names(second)), 24)
        self.assertLessEqual(len(calls), 25)
        self.assertEqual(set(self.names(first)) & set(self.names(second)), set(),
                         "Load More repeated items from the first page")

    def test_ineligible_items_do_not_reach_expensive_pricing(self):
        for i in range(40):
            self.make_item(f"T22-NP{i:03d}", price=None)      # never eligible
        self.populate(5)

        response, calls = self.count_pricing_calls(page_size=24)

        self.assertEqual(len(self.names(response)), 5)
        self.assertLessEqual(len(calls), 5,
                             "unpriced items were sent to the Sales Order engine")

    def test_candidate_queries_are_bounded(self):
        self.populate(110)

        from yob_storefront.services import catalog_listing_service as svc
        sizes = []
        real = svc.fetch_candidates

        def recording(ctx, category, terms, sort, after_keys, limit, selection=None):
            sizes.append(limit)
            return real(ctx, category, terms, sort, after_keys, limit, selection)

        with patch.object(svc, "fetch_candidates", side_effect=recording):
            self.listing(page_size=24)

        self.assertTrue(sizes, "no candidate query ran")
        self.assertTrue(all(s <= svc.MAX_CANDIDATE_BATCH for s in sizes),
                        f"unbounded candidate query: {sizes}")
        # The batch COUNT is bounded by the per-request scan budget rather than by a
        # fixed number of rounds. The old fixed cap was removed because ending the
        # scan on it also ended pagination, stranding later eligible Items.
        self.assertLessEqual(len(sizes), svc.MAX_CANDIDATE_SCAN // min(sizes) + 1)


# =========================================================================
# SECURITY
# =========================================================================

class ListingSecurityCase(ListingCase):

    def test_customer_comes_from_auth_context_only(self):
        """No request parameter can change the pricing identity."""
        sig = inspect.signature(inspect.unwrap(self.api.get_items))
        for forbidden in ("customer", "customer_name", "price_list", "company", "party"):
            self.assertNotIn(forbidden, sig.parameters,
                             f"get_items accepts `{forbidden}` from the browser")

    def test_sort_cannot_inject_sql(self):
        self.make_item("T22-SEC")
        r = self.listing(sort="name_asc, (SELECT SLEEP(5))")
        self.assertEqual(self.code_of(r), "unsupported_sort")

    def test_search_cannot_inject_sql(self):
        keep = self.make_item("T22-SAFE", item_name="Safe Item")
        for probe in ("' OR '1'='1", "'; DROP TABLE `tabItem`; --", "\\"):
            r = self.listing(search=probe)
            self.assertNotIn("errors", r, f"probe {probe!r} faulted")
            self.assertEqual(self.names(r), [], f"probe {probe!r} matched rows")

        self.assertTrue(frappe.db.exists("Item", keep), "Item table damaged")

    def test_expected_failures_leak_nothing(self):
        blob = frappe.as_json(self.listing(scope_value="no-such-category"))
        for leak in ("Traceback", "_server_messages", "/app/", "href", "tabItem"):
            self.assertNotIn(leak, blob)


if __name__ == "__main__":
    unittest.main()


# =========================================================================
# CANDIDATE SCAN CONTINUATION  (Phase 22B-1A)
# =========================================================================

class CandidateScanContinuationCase(ListingCase):
    """Stage-1 false positives must never make a later eligible Item unreachable.

    Stage 1 is deliberately a superset, so a run of candidates can pass the cheap
    query and then fail exact Stage-2 eligibility. If the scan gives up in that run
    it must say so honestly -- a short page with `has_more=true` and a cursor that
    has MOVED PAST the rejected candidates. Reporting `has_more=false` there would
    strand every remaining product with no way for the client to ask again.

    Cheap to provoke: the candidate batch is derived from `page_size`, so a small
    page and a few dozen unpriced Items span many batches without heavyweight
    fixtures.
    """

    def unpriced_run(self, count, prefix="T22-FP"):
        """Items that pass Stage 1 but fail Stage 2 -- priced 0 on the price list.

        A zero Item Price satisfies the broad `EXISTS`? No: the EXISTS demands
        `price_list_rate > 0`. So these use a CUSTOMER-SPECIFIC zero beside a
        generic positive price, which is exactly the section-14 ranking trap: the
        broad test sees the generic 100 and admits the row, while ERPNext ranks the
        customer-specific 0 first and Stage 2 correctly rejects it.
        """
        for i in range(count):
            item = self.make_item(f"{prefix}{i:03d}", item_name=f"Aaa {i:03d}", price=100)
            self.make_price(item, 0, customer=CUSTOMER)

    def test_false_positives_are_genuinely_stage_one_passing(self):
        """The fixture must exercise the real trap, not just be unpriced."""
        self.unpriced_run(1)
        from yob_storefront.services import catalog_listing_service as svc

        ctx = self.context()
        rows = svc.fetch_candidates(ctx, self.cat, [], "name_asc", None, 10)

        self.assertEqual([r["name"] for r in rows], ["T22-FP000"],
                         "the fixture no longer passes Stage 1; the test would be vacuous")
        self.assertFalse(self.eligible("T22-FP000"), "the fixture must fail Stage 2")

    def test_valid_item_after_more_false_positives_than_one_batch(self):
        self.unpriced_run(20)
        target = self.make_item("T22-ZZ-GOOD", item_name="Zzz Good")

        r = self.listing(page_size=2)

        self.assertEqual(self.names(r), [target],
                         "an eligible Item behind a run of false positives was lost")

    def test_valid_items_beyond_the_former_scan_budget_stay_reachable(self):
        """The regression: 30 false positives at page_size=2 spans 10 batches.

        The former hard cap was 8 batches, so the scan stopped mid-run and answered
        `has_more=false` -- making these Items unreachable by any client action.
        """
        self.unpriced_run(30)
        good = [self.make_item(f"T22-ZZ{i}", item_name=f"Zzz {i}") for i in range(2)]

        seen, cursor = [], None
        for _ in range(20):
            r = self.listing(page_size=2, cursor=cursor)
            seen.extend(self.names(r))
            page = r["data"]["pagination"]
            if not page["has_more"]:
                self.assertIsNone(page["next_cursor"])
                break
            cursor = page["next_cursor"]
            self.assertIsNotNone(cursor, "has_more=true with no cursor is a dead end")
        else:
            self.fail("pagination never terminated")

        self.assertEqual(seen, good, "items were lost, duplicated or reordered")

    def test_cursor_progresses_even_when_a_page_returns_nothing(self):
        """A scanned region of pure false positives must still advance the cursor."""
        self.unpriced_run(30)
        good = self.make_item("T22-ZZ-LAST", item_name="Zzz Last")

        cursors, seen, cursor = [], [], None
        for _ in range(20):
            r = self.listing(page_size=2, cursor=cursor)
            seen.extend(self.names(r))
            page = r["data"]["pagination"]
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
            self.assertNotIn(cursor, cursors,
                             "the cursor repeated -- Load More would loop forever")
            cursors.append(cursor)

        self.assertEqual(seen, [good])

    def test_expensive_pricing_tracks_eligible_items_not_candidates(self):
        """Stage 3 cost must follow the products shown, not the rows scanned."""
        self.unpriced_run(40)
        self.make_item("T22-ZZ-ONE", item_name="Zzz One")

        from yob_storefront.services import catalog_listing_service as svc
        priced, checked = [], []
        real_price, real_elig = svc.price_candidate, svc.is_catalog_eligible

        def count_price(ctx, row):
            priced.append(row["name"])
            return real_price(ctx, row)

        def count_elig(ctx, item, uom, variant_of=None):
            checked.append(item)
            return real_elig(ctx, item, uom, variant_of)

        with patch.object(svc, "price_candidate", side_effect=count_price), \
             patch.object(svc, "is_catalog_eligible", side_effect=count_elig):
            r = self.listing(page_size=24)

        self.assertEqual(self.names(r), ["T22-ZZ-ONE"])
        self.assertEqual(len(priced), 1,
                         f"built {len(priced)} Sales Orders to show 1 product")
        self.assertGreaterEqual(len(checked), 40,
                                "Stage 2 should have examined every candidate")

    def test_candidate_reads_stay_batch_limited(self):
        self.unpriced_run(30)
        self.make_item("T22-ZZ-END", item_name="Zzz End")

        from yob_storefront.services import catalog_listing_service as svc
        limits = []
        real = svc.fetch_candidates

        def recording(ctx, category, terms, sort, after_keys, limit, selection=None):
            limits.append(limit)
            return real(ctx, category, terms, sort, after_keys, limit, selection)

        with patch.object(svc, "fetch_candidates", side_effect=recording):
            self.listing(page_size=2)

        self.assertTrue(limits)
        self.assertTrue(all(lim <= svc.MAX_CANDIDATE_BATCH for lim in limits),
                        f"an unbounded candidate query ran: {limits}")

    def test_exhaustion_is_still_reported_honestly(self):
        self.unpriced_run(10)
        good = self.make_item("T22-ZZ-FIN", item_name="Zzz Fin")

        r = self.listing(page_size=24)

        self.assertEqual(self.names(r), [good])
        self.assertFalse(r["data"]["pagination"]["has_more"])
        self.assertIsNone(r["data"]["pagination"]["next_cursor"])

    def test_scan_budget_stop_never_claims_exhaustion(self):
        """Service-level: thousands of false positives without heavyweight fixtures.

        The endpoint tests above prove real continuation; this proves the loop's
        contract at a scale no fixture should build. If the scan stops on its work
        budget it must report `has_more=true` and a cursor strictly past everything
        it examined -- never a terminal state.
        """
        from yob_storefront.services import catalog_listing_service as svc

        total = svc.MAX_CANDIDATE_SCAN + 500
        synthetic = [
            {"name": f"SYN-{i:05d}", "item_name": f"Syn {i:05d}", "custom_slug": f"s{i}",
             "image": None, "stock_uom": self.uom, "variant_of": None,
             "creation": f"2026-01-01 00:00:{i % 60:02d}"}
            for i in range(total)
        ]

        def fake_fetch(ctx, category, terms, sort, after_keys, limit, selection=None):
            start = 0
            if after_keys:
                start = next((i + 1 for i, r in enumerate(synthetic)
                              if r["item_name"] == after_keys[0] and r["name"] == after_keys[1]), 0)
            return synthetic[start:start + limit]

        with patch.object(svc, "fetch_candidates", side_effect=fake_fetch), \
             patch.object(svc, "is_catalog_eligible", return_value=False), \
             patch.object(svc, "price_candidate", side_effect=AssertionError("must not price")):
            page, has_more, cursor, scanned = svc.list_items(
                self.context(), self.cat, [], "name_asc", 24, None, "category", "t22-cat")

        self.assertEqual(page, [], "nothing was eligible, so nothing may be returned")
        self.assertTrue(has_more, "the scan stopped on budget and claimed exhaustion")
        self.assertIsNotNone(cursor, "has_more=true with no cursor strands the client")
        self.assertLessEqual(scanned, svc.MAX_CANDIDATE_SCAN + svc.MAX_CANDIDATE_BATCH,
                             "the scan blew through its own work budget")


# =========================================================================
# CATALOGUE-WIDE BROWSE  (Phase 28A)
# =========================================================================

class CatalogWideCase(ListingCase):
    """`get_items` with NO category: the listing behind Angular's `/products`.

    THE POINT OF EVERY TEST HERE
    ----------------------------
    Dropping the category must change the SCOPE and nothing else. There is one
    pipeline, so each rule already proven for a category page is re-proven with
    the category removed -- a product must not be public catalogue-wide while
    invisible in its own category, nor the reverse.

    ISOLATION
    ---------
    The test site's catalogue holds thousands of listable Items, so a bare
    catalogue-wide page is mostly seed data. Two devices keep these assertions
    deterministic without weakening what they prove:

    * a nonsense token in the fixtures' `item_name`, searched for -- the scope is
      still catalogue-wide, the token only narrows what comes back;
    * `sort="newest"`, which puts freshly created fixtures on the first page.

    Neither touches the category predicate, which is what is actually under test.
    """

    TOKEN = "Zqbrowse"

    def browse(self, **kw):
        """Catalogue-wide: `scope_value` is simply absent."""
        kw.setdefault("scope_value", None)
        frappe.clear_cache()
        return inspect.unwrap(self.api.get_items)(auth_context={}, **kw)

    def tokened(self, **kw):
        kw.setdefault("search", self.TOKEN)
        return self.browse(**kw)

    def token_item(self, code, category=None, **kw):
        kw.setdefault("item_name", f"{self.TOKEN} {code}")
        return self.make_item(code, category=category, **kw)

    # ------------------------------------------------- the feature itself

    def test_browsing_without_a_category_crosses_categories(self):
        """THE requirement: products from more than one category in one answer."""

        other = self.make_category("t28-other")
        here = self.token_item("T28-IN-DEFAULT")
        there = self.token_item("T28-IN-OTHER", category=other)

        self.assertEqual(set(self.names(self.tokened())), {here, there})

        # And the category scope is untouched: each still answers for itself only.
        self.assertEqual(self.names(self.listing(search=self.TOKEN)), [here])
        self.assertEqual(
            self.names(self.listing(scope_value="t28-other", search=self.TOKEN)),
            [there])

    def test_a_bare_catalogue_page_needs_no_search(self):
        """The token is a test device, not a requirement of the scope."""

        fresh = self.token_item("T28-BARE")

        self.assertIn(fresh, self.names(self.browse(sort="newest")))

    def test_an_uncategorised_product_is_still_public(self):
        """A product with no Storefront Category has nowhere to be listed today.

        `/products` is the first place it can appear, and nothing in the
        eligibility rules ever required a category -- only a slug and a price.
        """

        orphan = self.token_item("T28-NOCAT", category="")

        self.assertIn(orphan, self.names(self.tokened()))

    # ------------------------------------------------- inherited exclusions

    def test_generated_variants_do_not_leak_into_the_catalogue(self):
        """ONE card per family catalogue-wide too, never one per SKU."""

        from erpnext.controllers.item_variant import create_variant

        colour = "Colour"
        if not frappe.db.exists("Item Attribute", colour):
            self.skipTest("Item Attribute 'Colour' is not configured here")

        values = frappe.get_all("Item Attribute Value", filters={"parent": colour},
                                pluck="attribute_value")
        if len(values) < 2:
            self.skipTest("Colour has fewer than two values on this bench")

        template = frappe.get_doc({
            "doctype": "Item", "item_code": "T28-FAM",
            "item_name": f"{self.TOKEN} Family", "item_group": self.item_group,
            "stock_uom": self.uom, "is_stock_item": 0, "is_sales_item": 1,
            "gst_hsn_code": self.hsn, "custom_slug": "t28-fam",
            "custom_category": self.cat, "has_variants": 1,
            "attributes": [{"attribute": colour}],
        }).insert(ignore_permissions=True)

        children = []
        for value in values[:2]:
            variant = create_variant(template.name, {colour: value})
            variant.insert(ignore_permissions=True)
            self.make_price(variant.name, 100)
            children.append(variant.name)

        names = self.names(self.tokened())

        self.assertIn(template.name, names, "the family is missing catalogue-wide")
        for child in children:
            self.assertNotIn(child, names, "a generated variant was listed as a product")

    def test_price_eligibility_is_unchanged_catalogue_wide(self):
        """No Item Price and a zero Item Price both stay invisible."""

        priced = self.token_item("T28-PRICED", price=100)
        unpriced = self.token_item("T28-UNPRICED", price=None)
        zero = self.token_item("T28-ZERO", price=0)

        names = set(self.names(self.tokened()))

        self.assertEqual(names, {priced})
        self.assertNotIn(unpriced, names)
        self.assertNotIn(zero, names)

    def test_a_fixed_rate_rule_still_cannot_publish_an_unpriced_item(self):
        """The Phase 22B rule, re-proven with the category removed."""

        unpriced = self.token_item("T28-RULEONLY", price=None)
        self.make_rule(unpriced, rate=999)

        self.assertNotIn(unpriced, self.names(self.tokened()))

    def test_public_listing_eligibility_is_unchanged_catalogue_wide(self):
        """Disabled, non-selling, unrouted and end-of-life products stay out."""

        visible = self.token_item("T28-OK")
        disabled = self.token_item("T28-OFF", disabled=1)
        not_selling = self.token_item("T28-NOSELL", is_sales_item=0)
        unrouted = self.token_item("T28-NOSLUG", custom_slug="")
        dead = self.token_item("T28-EOL", end_of_life=add_days(today(), -1))

        names = set(self.names(self.tokened()))

        self.assertEqual(names, {visible})
        for excluded in (disabled, not_selling, unrouted, dead):
            self.assertNotIn(excluded, names)

    def test_another_customers_price_does_not_publish_a_product(self):
        """Customer isolation is a property of the pipeline, not of the scope."""

        other = frappe.get_doc({
            "doctype": "Customer", "customer_name": "T28 Other Buyer",
            "customer_type": "Company",
            "customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
            "territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
        }).insert(ignore_permissions=True)

        theirs = self.token_item("T28-THEIRS", price=None)
        self.make_price(theirs, 100, customer=other.name)

        self.assertNotIn(theirs, self.names(self.tokened()))

    # ------------------------------------------------- filters

    def test_merchandising_filters_require_a_category(self):
        """Which facets exist is a property of a category (Phase 25C).

        Refused rather than ignored: silently dropping a selection would return a
        wider result set than the buyer asked for and look like a backend bug.
        """

        self.assertEqual(
            self.code_of(self.browse(storefront_filters='{"material":["steel"]}')),
            "storefront_filter_context_required")

    def test_an_empty_filter_selection_is_not_a_filter(self):
        """An Angular page that always sends the parameter must not be refused."""

        self.token_item("T28-EMPTYSEL")

        for empty in (None, "", "{}", '{"material":[]}'):
            self.assertNotIn("errors", self.browse(storefront_filters=empty),
                             f"storefront_filters={empty!r} was wrongly refused")

    # ------------------------------------------------- search

    def test_search_matches_the_name_catalogue_wide(self):
        wanted = self.token_item("T28-S-NAME", item_name=f"{self.TOKEN} Cotton Shirt")
        self.token_item("T28-S-OTHER", item_name=f"{self.TOKEN} Wool Coat")

        self.assertEqual(self.names(self.browse(search=f"{self.TOKEN} cotton")), [wanted])

    def test_search_matches_the_item_code_catalogue_wide(self):
        wanted = self.token_item("T28-ZZGLOBALCODE", item_name=f"{self.TOKEN} Plain")

        self.assertEqual(self.names(self.browse(search="ZZGLOBALCODE")), [wanted])

    def test_a_word_may_come_from_either_column_catalogue_wide(self):
        """AND across words, OR across the two identity columns -- one predicate,
        so `/products`, a category page and the header typeahead cannot disagree."""

        both = self.token_item("T28-HEX10", item_name=f"{self.TOKEN} Hex Bolt")
        self.token_item("T28-HEX99", item_name=f"{self.TOKEN} Washer")

        self.assertEqual(self.names(self.browse(search=f"{self.TOKEN} hex 10")), [both])

    def test_search_limits_still_apply(self):
        self.assertEqual(self.code_of(self.browse(search="x" * 200)), "search_too_long")
        self.assertEqual(self.code_of(self.browse(search="a b c d e f g h")),
                         "search_too_long")

    # ------------------------------------------------- pagination

    def paged(self, page_size=2, **kw):
        """Walk the whole token-narrowed catalogue, page by page."""

        seen = []
        pages = 0
        cursor = None

        while True:
            response = self.tokened(page_size=page_size, cursor=cursor, **kw)
            self.assertNotIn("errors", response, response)
            pagination = response["data"]["pagination"]

            seen.extend(row["name"] for row in response["data"]["items"])
            pages += 1

            if not pagination["has_more"]:
                self.assertIsNone(pagination["next_cursor"],
                                  "a terminal page still handed back a cursor")
                return seen, pages

            cursor = pagination["next_cursor"]
            self.assertIsNotNone(cursor, "has_more=true with no cursor strands the client")
            self.assertLess(pages, 20, "pagination did not terminate")

    def test_pages_cover_every_product_exactly_once(self):
        made = sorted(self.token_item(f"T28-PG{i}", item_name=f"{self.TOKEN} P{i}")
                      for i in range(5))

        seen, pages = self.paged(page_size=2)

        self.assertEqual(sorted(seen), made, "a product was duplicated or skipped")
        self.assertEqual(len(seen), len(set(seen)), "a product appeared on two pages")
        self.assertEqual(pages, 3, "5 items at page_size=2 is three pages")

    def test_the_first_page_is_bounded_by_page_size(self):
        for i in range(5):
            self.token_item(f"T28-FP{i}", item_name=f"{self.TOKEN} F{i}")

        response = self.tokened(page_size=2)
        pagination = response["data"]["pagination"]

        self.assertEqual(len(response["data"]["items"]), 2)
        self.assertEqual(pagination["returned_count"], 2)
        self.assertEqual(pagination["page_size"], 2)
        self.assertTrue(pagination["has_more"])

    def test_an_exact_full_page_still_reports_the_end_honestly(self):
        """`has_more` proves a further product survived the FULL pipeline."""

        for i in range(2):
            self.token_item(f"T28-EX{i}", item_name=f"{self.TOKEN} E{i}")

        pagination = self.tokened(page_size=2)["data"]["pagination"]

        self.assertFalse(pagination["has_more"])
        self.assertIsNone(pagination["next_cursor"])

    def test_ineligible_products_between_valid_ones_do_not_corrupt_paging(self):
        made = []
        for i in range(4):
            made.append(self.token_item(f"T28-MIX-OK{i}", item_name=f"{self.TOKEN} M{i}a"))
            self.token_item(f"T28-MIX-NO{i}", item_name=f"{self.TOKEN} M{i}b", price=None)

        seen, _pages = self.paged(page_size=2)

        self.assertEqual(sorted(seen), sorted(made))

    def test_paging_holds_for_every_sort(self):
        made = sorted(self.token_item(f"T28-SORT{i}", item_name=f"{self.TOKEN} S{i}")
                      for i in range(5))

        for sort in ("name_asc", "name_desc", "newest"):
            seen, _pages = self.paged(page_size=2, sort=sort)
            self.assertEqual(sorted(seen), made, f"{sort} duplicated or skipped a product")

    # ------------------------------------------------- cursor binding

    def test_a_catalogue_cursor_cannot_be_replayed_inside_a_category(self):
        """The scope joins the cursor binding, so the two cannot be crossed.

        Resuming a category page from a catalogue-wide keyset position would
        silently return a nonsense page rather than an error.
        """

        for i in range(3):
            self.token_item(f"T28-BIND{i}", item_name=f"{self.TOKEN} B{i}")

        cursor = self.tokened(page_size=1)["data"]["pagination"]["next_cursor"]
        self.assertIsNotNone(cursor)

        self.assertEqual(
            self.code_of(self.listing(search=self.TOKEN, page_size=1, cursor=cursor)),
            "cursor_invalid")

    def test_a_category_cursor_cannot_be_replayed_catalogue_wide(self):
        for i in range(3):
            self.token_item(f"T28-BIND2-{i}", item_name=f"{self.TOKEN} C{i}")

        cursor = self.listing(search=self.TOKEN,
                              page_size=1)["data"]["pagination"]["next_cursor"]
        self.assertIsNotNone(cursor)

        self.assertEqual(
            self.code_of(self.tokened(page_size=1, cursor=cursor)), "cursor_invalid")

    def test_a_malformed_cursor_is_still_a_clean_validation_error(self):
        for bad in ("not-base64!!", "eyJ2IjoxfQ", "x" * 600):
            response = self.browse(cursor=bad)
            self.assertEqual(self.code_of(response), "cursor_invalid",
                             f"cursor={bad!r} was not refused cleanly")
            self.assertNotIn("traceback", str(response).lower())

    # ------------------------------------------------- shape and cost

    def test_the_response_shape_is_identical_to_a_category_page(self):
        """Angular renders one ListingCard. A scope must not change the contract."""

        self.token_item("T28-SHAPE")

        catalogue = self.tokened()["data"]
        scoped = self.listing(search=self.TOKEN)["data"]

        self.assertEqual(set(catalogue), set(scoped))
        self.assertEqual(set(catalogue["pagination"]), set(scoped["pagination"]))
        self.assertEqual(set(catalogue["query"]), set(scoped["query"]))
        self.assertEqual(set(catalogue["items"][0]), set(scoped["items"][0]))

        # The echoed query states the scope truthfully rather than inventing one.
        self.assertIsNone(catalogue["query"]["scope_value"])

    def test_a_catalogue_page_prices_only_its_page(self):
        """Dropping the category must not drop the Phase 22B work bound."""

        from yob_storefront.services import pricing_service

        for i in range(6):
            self.token_item(f"T28-COST{i}", item_name=f"{self.TOKEN} K{i}")

        calls = []
        real = pricing_service.get_item_pricing
        with patch.object(pricing_service, "get_item_pricing",
                          side_effect=lambda *a, **k: (calls.append(k.get("item_code")),
                                                       real(*a, **k))[1]):
            self.tokened(page_size=2)

        self.assertLessEqual(len(calls), 3,
                             f"a page of 2 priced {len(calls)} products: {calls}")
