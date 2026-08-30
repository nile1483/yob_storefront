# Copyright (c) 2026, YOB and Shayona
"""Header product suggestions: same products, far less work (Phase 26A).

THE TWO THINGS THIS PROTECTS
----------------------------
1. **One product universe.** A suggestion must be the same public entity the
   catalogue lists -- same eligibility, same family collapse, same slug rule.
   `ParityCase` asserts that directly by comparing against `get_items`, because
   a second "searchable" definition would drift from the listing silently and
   nobody would notice until a merchant asked why a product appears in one place
   and not the other.

2. **No Sales Order.** A typeahead fires on every keystroke past the third.
   Stage 3 costs ~51 ms per product, and a dropdown shows no money, so it must
   never run. `WorkCase` spies on `price_candidate` and on the pricing service
   itself rather than trusting the code to stay that way.
"""

import inspect
import unittest
from unittest.mock import patch

import frappe

CUSTOMER = "YOB Demo Buyer"
SEED_ITEM = "YOB-BOLT-M10"

#: Every monetary or transaction-context key that must never reach a dropdown.
FORBIDDEN_FIELDS = {
    "rate", "base_price", "price", "amount", "net_amount", "total_amount",
    "discount_percentage", "discount_amount", "tax_amount", "tax_components",
    "uom", "stock_uom", "conversion_factor", "stock_qty", "actual_qty",
    "warehouse", "price_list", "pricing_rule_label", "pricing_rules",
    "price_state", "has_variants",
}

ALLOWED_FIELDS = {"item_code", "item_name", "slug", "image", "is_template"}


def _seeded():
    return bool(frappe.db.exists("Customer", CUSTOMER) and frappe.db.exists("Item", SEED_ITEM))


class SuggestionBase(unittest.TestCase):
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

    def make_category(self, slug="p26-cat"):
        return frappe.get_doc({
            "doctype": "Category", "category_name": slug, "slug": slug,
            "is_group": 0, "is_active": 1}).insert(ignore_permissions=True)

    def make_item(self, code, category=None, price=100, name=None, slug=None, **kw):
        doc = {"doctype": "Item", "item_code": code,
               "item_name": name or code,
               "item_group": self.item_group, "stock_uom": self.uom,
               "is_stock_item": 0, "is_sales_item": 1, "gst_hsn_code": self.hsn,
               "custom_slug": slug if slug is not None else code.lower(),
               "custom_category": category}
        doc.update(kw)
        item = frappe.get_doc(doc).insert(ignore_permissions=True)

        if price is not None:
            frappe.get_doc({
                "doctype": "Item Price", "item_code": item.name,
                "price_list": self.price_list, "price_list_rate": price,
                "selling": 1, "uom": self.uom}).insert(ignore_permissions=True)
        return item

    def make_family(self, code, category, name=None, price=900):
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

        template = frappe.get_doc({
            "doctype": "Item", "item_code": code, "item_name": name or code,
            "item_group": self.item_group, "stock_uom": self.uom, "is_stock_item": 0,
            "is_sales_item": 1, "gst_hsn_code": self.hsn, "custom_slug": code.lower(),
            "custom_category": category, "has_variants": 1,
            "attributes": [{"attribute": "Colour"}, {"attribute": "Size"}],
        }).insert(ignore_permissions=True)

        variants = []
        for size in ("Medium", "Large"):
            variant = create_variant(template.name, {"Colour": "Red", "Size": size})
            variant.insert(ignore_permissions=True)
            if price is not None:
                frappe.get_doc({
                    "doctype": "Item Price", "item_code": variant.name,
                    "price_list": self.price_list, "price_list_rate": price,
                    "selling": 1, "uom": self.uom}).insert(ignore_permissions=True)
            variants.append(variant.name)

        return template, variants

    # ------------------------------------------------------------- the wire

    def suggest(self, search):
        """Called the way Frappe delivers a GET: the value is a STRING."""

        frappe.clear_cache()
        return inspect.unwrap(self.catalog.get_product_suggestions)(
            auth_context={}, search=search)

    def items(self, search):
        response = self.suggest(search)
        self.assertNotIn("errors", response, f"request failed: {response}")
        return response["data"]["items"]

    def codes(self, search):
        return [row["item_code"] for row in self.items(search)]


# =========================================================
# INPUT
# =========================================================

class InputCase(SuggestionBase):

    def setUp(self):
        super().setUp()
        self.category = self.make_category("p26-input")
        self.make_item("_P26-DRILL", self.category.name, name="Cordless Drill")

    def test_blank_search_answers_empty(self):
        self.assertEqual(self.items(""), [])

    def test_none_answers_empty(self):
        self.assertEqual(self.items(None), [])

    def test_one_character_answers_empty(self):
        self.assertEqual(self.items("d"), [])

    def test_two_characters_answer_empty(self):
        self.assertEqual(self.items("dr"), [])

    def test_three_characters_actually_search(self):
        self.assertIn("_P26-DRILL", self.codes("dri"))

    def test_surrounding_whitespace_is_trimmed(self):
        self.assertIn("_P26-DRILL", self.codes("   dri   "))

    def test_whitespace_only_is_not_a_search(self):
        """Two real characters padded to five must not cross the floor."""

        self.assertEqual(self.items("  d  "), [])

    def test_inner_whitespace_is_collapsed_into_and_terms(self):
        self.assertIn("_P26-DRILL", self.codes("cordless   drill"))
        self.assertEqual(self.codes("cordless hammer"), [])

    def test_a_short_search_is_not_an_error(self):
        """A buyer mid-word has done nothing wrong."""

        response = self.suggest("dr")

        self.assertNotIn("errors", response)
        self.assertEqual(response["data"]["items"], [])

    def test_an_overlong_search_is_refused_like_the_listing(self):
        response = self.suggest("x" * 200)

        self.assertEqual(response["errors"][0]["code"], "search_too_long")

    def test_wildcards_are_data_not_syntax(self):
        """`%` must find products containing a percent sign, not everything."""

        self.make_item("_P26-PCT", self.category.name, name="100% Cotton Rag")

        self.assertEqual(self.codes("%%%"), [])
        self.assertIn("_P26-PCT", self.codes("100%"))

    def test_an_underscore_is_not_a_single_character_wildcard(self):
        self.assertEqual(self.codes("d_i"), [])

    def test_a_quote_is_safe(self):
        self.assertEqual(self.items("' OR 1=1 --"), [])

    def test_the_endpoint_accepts_no_filter_or_scope_parameters(self):
        """Nothing from the browser may become a query beyond the search text."""

        accepted = set(inspect.signature(
            inspect.unwrap(self.catalog.get_product_suggestions)).parameters)

        self.assertEqual(accepted, {"search", "auth_context"})


# =========================================================
# PRODUCT SEMANTICS
# =========================================================

class SemanticsCase(SuggestionBase):

    def setUp(self):
        super().setUp()
        self.category = self.make_category("p26-sem")

    def test_a_simple_item_can_appear(self):
        self.make_item("_P26-SIMPLE", self.category.name, name="Zenith Widget")

        self.assertIn("_P26-SIMPLE", self.codes("zenith"))

    def test_a_family_appears_once_as_the_family(self):
        template, variants = self.make_family("_P26-TEE", self.category.name,
                                              name="Zenith Tee")

        codes = self.codes("zenith")

        self.assertEqual(codes.count(template.name), 1, "the family was duplicated")
        for variant in variants:
            self.assertNotIn(variant, codes, "a generated variant became a suggestion")

    def test_a_family_is_flagged_as_a_template(self):
        template, _ = self.make_family("_P26-TEE2", self.category.name,
                                       name="Zenith Polo")

        row = next(r for r in self.items("zenith") if r["item_code"] == template.name)

        self.assertTrue(row["is_template"])

    def test_a_simple_item_is_not_flagged_as_a_template(self):
        self.make_item("_P26-PLAIN", self.category.name, name="Zenith Plain")

        row = next(r for r in self.items("zenith") if r["item_code"] == "_P26-PLAIN")

        self.assertFalse(row["is_template"])

    def test_a_disabled_item_is_omitted(self):
        self.make_item("_P26-OFF", self.category.name, name="Zenith Disabled",
                       disabled=1)

        self.assertNotIn("_P26-OFF", self.codes("zenith"))

    def test_a_non_sales_item_is_omitted(self):
        self.make_item("_P26-NOSALE", self.category.name, name="Zenith Internal",
                       is_sales_item=0)

        self.assertNotIn("_P26-NOSALE", self.codes("zenith"))

    def test_an_item_without_a_public_slug_is_omitted(self):
        """No slug, no product page -- so nothing to navigate to."""

        self.make_item("_P26-NOSLUG", self.category.name, name="Zenith Unrouted",
                       slug="")

        self.assertNotIn("_P26-NOSLUG", self.codes("zenith"))

    def test_an_item_with_no_price_is_omitted(self):
        self.make_item("_P26-FREE", self.category.name, name="Zenith Unpriced",
                       price=None)

        self.assertNotIn("_P26-FREE", self.codes("zenith"))

    def test_a_zero_priced_item_is_omitted(self):
        """The catalogue rule: an applicable base price strictly above zero."""

        self.make_item("_P26-ZERO", self.category.name, name="Zenith Zero", price=0)

        self.assertNotIn("_P26-ZERO", self.codes("zenith"))

    def test_a_manufacturer_family_is_omitted(self):
        """Fail closed, exactly as the catalogue does: no attribute selector."""

        frappe.get_doc({
            "doctype": "Item", "item_code": "_P26-MFR", "item_name": "Zenith Mfr",
            "item_group": self.item_group, "stock_uom": self.uom, "is_stock_item": 0,
            "is_sales_item": 1, "gst_hsn_code": self.hsn, "custom_slug": "_p26-mfr",
            "custom_category": self.category.name, "has_variants": 1,
            "variant_based_on": "Manufacturer",
        }).insert(ignore_permissions=True)

        self.assertNotIn("_P26-MFR", self.codes("zenith"))

    def test_search_is_global_and_ignores_category(self):
        """The header searches everywhere, not the category being browsed."""

        other = self.make_category("p26-other")
        self.make_item("_P26-HERE", self.category.name, name="Zenith Here")
        self.make_item("_P26-THERE", other.name, name="Zenith There")

        codes = self.codes("zenith")

        self.assertIn("_P26-HERE", codes)
        self.assertIn("_P26-THERE", codes)

    def test_an_uncategorised_product_is_still_reachable(self):
        self.make_item("_P26-NOCAT", None, name="Zenith Homeless")

        self.assertIn("_P26-NOCAT", self.codes("zenith"))


# =========================================================
# SEARCHABLE IDENTITY  (Phase 26A-1)
# =========================================================

class IdentitySearchCase(SuggestionBase):
    """Name OR item code, through the SAME predicate the listing uses."""

    def setUp(self):
        super().setUp()
        self.category = self.make_category("p26-ident")

    def test_the_display_name_still_matches(self):
        self.make_item("_P26-ID-A", self.category.name, name="Quantum Spanner")

        self.assertIn("_P26-ID-A", self.codes("spanner"))

    def test_an_exact_item_code_matches(self):
        self.make_item("_P26-ID-EXACT", self.category.name, name="Nothing Alike")

        self.assertIn("_P26-ID-EXACT", self.codes("_P26-ID-EXACT"))

    def test_an_item_code_fragment_matches(self):
        """The real case: a buyer types part of a code from a quote."""

        self.make_item("STO-P26-2026-00042", self.category.name,
                       name="Nothing Alike Either", slug="sto-p26-42")

        self.assertIn("STO-P26-2026-00042", self.codes("P26-2026"))

    def test_a_word_may_come_from_either_column(self):
        both = "_P26-ID-10"
        self.make_item(both, self.category.name, name="Hex Bolt", slug="hex-bolt-10")
        self.make_item("_P26-ID-99", self.category.name, name="Washer",
                       slug="washer-99")

        codes = self.codes("hex 10")

        self.assertIn(both, codes)
        self.assertNotIn("_P26-ID-99", codes)

    def test_a_family_found_by_its_code_returns_the_family_only(self):
        template, variants = self.make_family("_P26-ID-FAM", self.category.name,
                                              name="Codeless Family")

        codes = self.codes("_P26-ID-FAM")

        self.assertEqual(codes.count(template.name), 1)
        for variant in variants:
            self.assertNotIn(variant, codes,
                             "a generated variant leaked in via its own code")

    def test_a_variant_code_fragment_never_returns_the_child(self):
        """A variant's code contains its template's, so this is the leak to fear.

        Searching the family stem must answer with the FAMILY, never with the
        generated children whose codes start with the same text.
        """

        template, variants = self.make_family("_P26-ID-STEM", self.category.name,
                                              name="Stem Family")

        codes = self.codes("_P26-ID-STEM")

        self.assertIn(template.name, codes)
        for variant in variants:
            self.assertNotIn(variant, codes)

    def test_description_is_not_searchable(self):
        self.make_item("_P26-ID-DESC", self.category.name, name="Plain Thing",
                       description="ZZONLYINDESCRIPTION")

        self.assertEqual(self.codes("ZZONLYINDESCRIPTION"), [])

    def test_code_search_costs_no_extra_pricing(self):
        from yob_storefront.services import catalog_listing_service as svc

        self.make_item("_P26-ID-COST", self.category.name, name="Cost Probe")

        with patch.object(svc, "price_candidate") as priced:
            by_name = self.codes("Cost Probe")
            by_code = self.codes("_P26-ID-COST")

        self.assertIn("_P26-ID-COST", by_name)
        self.assertIn("_P26-ID-COST", by_code)
        self.assertFalse(priced.called, "a code search built a Sales Order")


# =========================================================
# RESPONSE SHAPE
# =========================================================

class ShapeCase(SuggestionBase):

    def setUp(self):
        super().setUp()
        self.category = self.make_category("p26-shape")

    def test_a_suggestion_carries_only_the_approved_fields(self):
        self.make_item("_P26-SHAPE", self.category.name, name="Quasar Tool",
                       image="/files/quasar.png")

        row = next(r for r in self.items("quasar") if r["item_code"] == "_P26-SHAPE")

        self.assertEqual(set(row), ALLOWED_FIELDS)

    def test_no_monetary_or_transaction_field_is_ever_present(self):
        self.make_item("_P26-MONEY", self.category.name, name="Quasar Priced")
        template, _ = self.make_family("_P26-FAM", self.category.name,
                                       name="Quasar Family")

        rows = self.items("quasar")
        self.assertTrue(rows)

        for row in rows:
            leaked = FORBIDDEN_FIELDS & set(row)
            self.assertEqual(leaked, set(),
                             f"{row['item_code']} leaked {sorted(leaked)}")

    def test_no_document_internals_leak(self):
        self.make_item("_P26-INTERNAL", self.category.name, name="Quasar Internal")

        wire = frappe.as_json(self.items("quasar"))

        for internal in ("owner", "modified", "docstatus", "doctype", "idx",
                         "custom_category", "item_group", "variant_of",
                         "custom_slug", "disabled", "is_sales_item"):
            self.assertNotIn(f'"{internal}"', wire, f"{internal} reached the client")

    def test_a_missing_image_is_null_not_empty_string(self):
        self.make_item("_P26-NOIMG", self.category.name, name="Quasar Imageless")

        row = next(r for r in self.items("quasar") if r["item_code"] == "_P26-NOIMG")

        self.assertIsNone(row["image"])

    def test_the_image_keeps_the_catalogue_media_convention(self):
        """The stored relative path, exactly as a listing card returns it."""

        self.make_item("_P26-IMG", self.category.name, name="Quasar Pictured",
                       image="/files/quasar.png")

        row = next(r for r in self.items("quasar") if r["item_code"] == "_P26-IMG")

        self.assertEqual(row["image"], "/files/quasar.png")

    def test_the_slug_is_the_public_one(self):
        self.make_item("_P26-SLUG", self.category.name, name="Quasar Slugged",
                       slug="quasar-slugged")

        row = next(r for r in self.items("quasar") if r["item_code"] == "_P26-SLUG")

        self.assertEqual(row["slug"], "quasar-slugged")


# =========================================================
# BOUNDS AND ORDER
# =========================================================

class BoundsCase(SuggestionBase):

    def setUp(self):
        super().setUp()
        self.category = self.make_category("p26-bounds")

    def test_at_most_eight_suggestions_are_returned(self):
        from yob_storefront.services.product_suggestion_service import MAX_SUGGESTIONS

        for index in range(MAX_SUGGESTIONS + 4):
            self.make_item(f"_P26-MANY-{index:02d}", self.category.name,
                           name=f"Nebula Item {index:02d}")

        self.assertEqual(len(self.items("nebula")), MAX_SUGGESTIONS)

    def test_the_limit_is_server_owned_and_not_a_parameter(self):
        accepted = set(inspect.signature(
            inspect.unwrap(self.catalog.get_product_suggestions)).parameters)

        for knob in ("limit", "page_size", "count", "size"):
            self.assertNotIn(knob, accepted, f"a client can ask for {knob}")

    def test_ordering_is_deterministic_and_by_name(self):
        for suffix in ("Gamma", "Alpha", "Beta"):
            self.make_item(f"_P26-ORD-{suffix.upper()}", self.category.name,
                           name=f"Nebula {suffix}")

        first = [r["item_name"] for r in self.items("nebula")]
        second = [r["item_name"] for r in self.items("nebula")]

        self.assertEqual(first, second, "the same query answered in two orders")
        self.assertEqual(first, sorted(first), "suggestions are not name-ordered")


# =========================================================
# PARITY WITH THE CATALOGUE
# =========================================================

class ParityCase(SuggestionBase):
    """One product universe. A suggestion is what the listing would list."""

    def setUp(self):
        super().setUp()
        self.category = self.make_category("p26-parity")

    def listing_codes(self, search):
        frappe.clear_cache()
        response = inspect.unwrap(self.catalog.get_items)(
            auth_context={}, scope_type="category", scope_value=self.category.slug,
            search=search, page_size="24")
        self.assertNotIn("errors", response, response)
        return {row["name"] for row in response["data"]["items"]}

    def test_suggestions_and_the_listing_agree_on_the_same_products(self):
        self.make_item("_P26-P-OK", self.category.name, name="Vertex Visible")
        self.make_item("_P26-P-ZERO", self.category.name, name="Vertex Zero", price=0)
        self.make_item("_P26-P-OFF", self.category.name, name="Vertex Off", disabled=1)
        self.make_item("_P26-P-NOSLUG", self.category.name, name="Vertex Unrouted",
                       slug="")
        template, variants = self.make_family("_P26-P-FAM", self.category.name,
                                              name="Vertex Family")

        suggested = set(self.codes("vertex"))
        listed = self.listing_codes("vertex")

        self.assertEqual(suggested, listed,
                         "the typeahead and the catalogue disagree about which "
                         "products are public")

        self.assertIn(template.name, suggested)
        for variant in variants:
            self.assertNotIn(variant, suggested)

    def test_parity_holds_for_an_item_code_search_too(self):
        """One predicate, so both endpoints must answer a CODE search alike."""

        self.make_item("_P26-PAR-CODE", self.category.name, name="Unrelated Name")
        self.make_item("_P26-PAR-ZERO", self.category.name, name="Unrelated Zero",
                       price=0)

        suggested = set(self.codes("_P26-PAR"))
        listed = self.listing_codes("_P26-PAR")

        self.assertEqual(suggested, listed)
        self.assertIn("_P26-PAR-CODE", suggested)
        self.assertNotIn("_P26-PAR-ZERO", suggested,
                         "eligibility was skipped for a code match")


# =========================================================
# WORK DONE
# =========================================================

class WorkCase(SuggestionBase):

    def setUp(self):
        super().setUp()
        self.category = self.make_category("p26-work")
        self.make_item("_P26-W1", self.category.name, name="Torus Widget")
        self.make_item("_P26-W2", self.category.name, name="Torus Gadget")

    def test_below_three_characters_no_catalogue_work_happens_at_all(self):
        """Not merely a cheap query -- no query, and no customer lookup."""

        from yob_storefront.services import catalog_listing_service as svc

        with patch.object(svc, "fetch_candidates") as candidates, \
                patch.object(self.catalog, "get_storefront_customer") as customer:
            for text in ("", None, "a", "ab", "  a  "):
                self.assertEqual(self.items(text), [])

        self.assertFalse(candidates.called, "a candidate query ran below the floor")
        self.assertFalse(customer.called, "the customer was resolved below the floor")

    def test_no_sales_order_is_ever_built(self):
        """Stage 3 is ~51ms per product and returns money nobody asked for."""

        from yob_storefront.services import catalog_listing_service as svc

        with patch.object(svc, "price_candidate") as priced:
            self.assertTrue(self.items("torus"))

        self.assertFalse(priced.called,
                         "the typeahead built a Sales Order to answer a dropdown")

    def test_the_pricing_service_is_never_called(self):
        from yob_storefront.services import pricing_service

        with patch.object(pricing_service, "get_item_pricing") as pricing:
            self.assertTrue(self.items("torus"))

        self.assertFalse(pricing.called, "the typeahead priced a product")

    def test_eligibility_costs_one_cheap_lookup_per_candidate(self):
        """Stage 2 IS the visibility rule, so it runs -- but bounded and cheap.

        `get_price_list_rate_for` is a single ranked query, not a document build.
        Asserting the count keeps it honest: if a future change made it call per
        variant or per price list, this number would move.
        """

        from yob_storefront.services import catalog_listing_service as svc

        with patch.object(svc, "is_catalog_eligible",
                          wraps=svc.is_catalog_eligible) as eligible:
            rows = self.items("torus")

        self.assertEqual(len(rows), 2)
        self.assertEqual(eligible.call_count, 2,
                         "eligibility was checked more than once per candidate")

    def test_the_candidate_scan_is_bounded(self):
        from yob_storefront.services import catalog_listing_service as svc
        from yob_storefront.services.product_suggestion_service import (
            MAX_SUGGESTION_SCAN,
        )

        limits = []
        real = svc.fetch_candidates

        def recording(ctx, category, terms, sort, after_keys, limit, selection=None):
            limits.append(limit)
            return real(ctx, category, terms, sort, after_keys, limit, selection)

        with patch.object(svc, "fetch_candidates", side_effect=recording):
            self.items("torus")

        self.assertTrue(limits)
        for limit in limits:
            self.assertLessEqual(limit, MAX_SUGGESTION_SCAN)

    def test_no_variant_resolution_happens(self):
        from yob_storefront.services import variant_service

        with patch.object(variant_service, "variant_matrix") as matrix:
            self.items("torus")

        self.assertFalse(matrix.called, "the typeahead resolved a variant matrix")

    def test_nothing_is_written(self):
        """A read endpoint. The rollback guard in tearDown backs this up."""

        before = frappe.db.count("Cart")

        self.items("torus")

        self.assertEqual(frappe.db.count("Cart"), before)
        self.assertEqual(self.commits, [])


# =========================================================
# CUSTOMER BOUNDARY
# =========================================================

class CustomerCase(SuggestionBase):

    def setUp(self):
        super().setUp()
        self.category = self.make_category("p26-cust")

    def test_a_product_priced_only_for_another_customer_is_not_suggested(self):
        """Autocomplete must not leak what this buyer could not browse."""

        other = frappe.get_doc({
            "doctype": "Customer", "customer_name": "_P26 Other Buyer",
            "customer_group": self.customer.customer_group,
            "territory": self.customer.territory}).insert(ignore_permissions=True)

        private_list = frappe.get_doc({
            "doctype": "Price List", "price_list_name": "_P26 Private",
            "selling": 1, "enabled": 1, "currency": "INR"}).insert(ignore_permissions=True)
        frappe.db.set_value("Customer", other.name, "default_price_list", private_list.name)

        item = self.make_item("_P26-PRIV", self.category.name, name="Cipher Private",
                              price=None)
        frappe.get_doc({
            "doctype": "Item Price", "item_code": item.name,
            "price_list": private_list.name, "price_list_rate": 500,
            "selling": 1, "uom": self.uom,
            "customer": other.name}).insert(ignore_permissions=True)

        self.assertNotIn("_P26-PRIV", self.codes("cipher"),
                         "a product priced for another customer was suggested")

    def test_the_customer_comes_only_from_the_auth_context(self):
        accepted = set(inspect.signature(
            inspect.unwrap(self.catalog.get_product_suggestions)).parameters)

        for spoofable in ("customer", "customer_name", "company", "price_list"):
            self.assertNotIn(spoofable, accepted,
                             f"a browser could pass {spoofable}")


if __name__ == "__main__":
    unittest.main()
