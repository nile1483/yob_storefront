# Copyright (c) 2026, YOB and Shayona
"""Item Price storefront metadata: MRP, MOQ and the quantity multiplier.

WHAT THESE THREE FIELDS ARE
---------------------------
Metadata on the Item Price row ERPNext priced against. Two different purposes,
deliberately not one feature:

    MOQ + Quantity Multiplier  ->  storefront quantity-input GUIDANCE
    MRP                        ->  informational display ONLY

THE ARCHITECTURAL BOUNDARY THIS FILE DEFENDS
--------------------------------------------
None of the three participates in any calculation or any validation.

* MRP never reaches ERPNext pricing. Changing it must leave base price, rate,
  discount, tax and total byte-identical, and the backend derives no "saving".
* MOQ and the multiplier are never enforced. A cart or order quantity that
  ignores them must behave exactly as it did before these fields existed.

`NoBackendEnforcementCase` is the important one: it proves the guidance stayed
guidance. If a future change adds a `minimum_order_qty` refusal, it fails there
and nowhere else in the suite would notice.

WHICH ITEM PRICE
----------------
The SAME row the rate came from. ERPNext discards that identity, so
`pricing_service.resolve_item_price_source()` recovers it by calling ERPNext's
own `get_item_price()`. `ResolvedSourceCase` proves the metadata follows the
customer-specific row, the price-list, and the variant -> template fallback --
never "any Item Price for this SKU".
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


class GuidanceBase(unittest.TestCase):
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
        self.category = frappe.db.get_value("Item", SEED_ITEM, "custom_category")
        self.price_list = frappe.get_single("Selling Settings").selling_price_list
        self.company = frappe.get_single("YOB Store Settings").company

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()
        self.assertEqual(self.commits, [], "a commit escaped the test rollback")

    # ------------------------------------------------------------- fixtures

    def make_item(self, code, **kw):
        doc = {
            "doctype": "Item", "item_code": code, "item_name": kw.pop("item_name", code),
            "item_group": self.item_group, "stock_uom": self.uom, "is_stock_item": 0,
            "is_sales_item": 1, "gst_hsn_code": self.hsn,
            "custom_slug": kw.pop("custom_slug", code.lower()),
            "custom_category": self.category,
        }
        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True).name

    def make_price(self, item_code, rate=100, *, moq=None, multiplier=None, mrp=None,
                   **kw):
        doc = {
            "doctype": "Item Price", "item_code": item_code,
            "price_list": kw.pop("price_list", self.price_list),
            "price_list_rate": rate, "selling": 1, "uom": kw.pop("uom", self.uom),
        }
        if moq is not None:
            doc["custom_moq"] = moq
        if multiplier is not None:
            doc["custom_quantity_multiplier"] = multiplier
        if mrp is not None:
            doc["custom_mrp"] = mrp
        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True).name

    def make_rule(self, item_code, *, discount=10, **kw):
        doc = {
            "doctype": "Pricing Rule", "title": f"P29 Rule {item_code}",
            "apply_on": "Item Code", "price_or_product_discount": "Price",
            "selling": 1, "company": self.company, "currency": "INR",
            "items": [{"item_code": item_code}],
            "valid_from": add_days(today(), -1),
            "rate_or_discount": "Discount Percentage",
            "discount_percentage": discount,
        }
        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True).name

    # ------------------------------------------------------------- helpers

    def detail(self, slug, qty=1):
        frappe.clear_cache()
        response = inspect.unwrap(self.api.get_item)(slug=slug, qty=qty, auth_context={})
        self.assertNotIn("errors", response, f"get_item failed: {response}")
        return response["data"]

    def control(self, slug, qty=1):
        return self.detail(slug, qty)["quantity_control"]


# =========================================================================
# THE ITEM PRICE FIELDS THEMSELVES
# =========================================================================

class ItemPriceFieldCase(GuidanceBase):

    def test_the_three_fields_exist_on_item_price(self):
        meta = frappe.get_meta("Item Price")

        for fieldname in ("custom_moq", "custom_quantity_multiplier", "custom_mrp"):
            self.assertIsNotNone(meta.get_field(fieldname),
                                 f"{fieldname} is missing from Item Price")

    def test_the_field_types_are_quantity_and_currency_compatible(self):
        meta = frappe.get_meta("Item Price")

        self.assertEqual(meta.get_field("custom_moq").fieldtype, "Float")
        self.assertEqual(meta.get_field("custom_quantity_multiplier").fieldtype, "Float")
        self.assertEqual(meta.get_field("custom_mrp").fieldtype, "Currency")

    def test_mrp_reuses_the_item_prices_own_currency(self):
        """No second currency field: two on one row could disagree."""

        meta = frappe.get_meta("Item Price")

        self.assertEqual(meta.get_field("custom_mrp").options, "currency")
        self.assertIsNone(meta.get_field("custom_mrp_currency"),
                          "a second MRP currency field was introduced")

    def test_the_fields_are_optional(self):
        """An Item Price saves untouched, exactly as before Phase 29A."""

        item = self.make_item("_P29-BARE")
        name = self.make_price(item)

        row = frappe.db.get_value(
            "Item Price", name,
            ["custom_moq", "custom_quantity_multiplier", "custom_mrp"], as_dict=True)

        self.assertFalse(row.custom_moq)
        self.assertFalse(row.custom_quantity_multiplier)
        self.assertFalse(row.custom_mrp)

    def test_no_mathematical_relationship_is_enforced(self):
        """MRP below the selling rate is odd but not this backend's business."""

        item = self.make_item("_P29-CHEAPMRP")

        name = self.make_price(item, rate=100, mrp=1)

        self.assertEqual(frappe.db.get_value("Item Price", name, "custom_mrp"), 1)


# =========================================================================
# NORMALISATION
# =========================================================================

class NormalisationCase(GuidanceBase):
    """Blank, zero and negative all mean NOT CONFIGURED."""

    def test_unconfigured_values_publish_as_null(self):
        item = self.make_item("_P29-NULL")
        self.make_price(item)

        detail = self.detail("_p29-null")

        self.assertIsNone(detail["mrp"])
        self.assertIsNone(detail["quantity_control"]["moq"])
        self.assertIsNone(detail["quantity_control"]["quantity_multiplier"])

    def test_zero_is_treated_as_unconfigured(self):
        item = self.make_item("_P29-ZERO")
        self.make_price(item, moq=0, multiplier=0, mrp=0)

        detail = self.detail("_p29-zero")

        self.assertIsNone(detail["mrp"])
        self.assertIsNone(detail["quantity_control"]["moq"])
        self.assertIsNone(detail["quantity_control"]["quantity_multiplier"])

    def test_negative_values_are_treated_as_unconfigured(self):
        """Normalised at the runtime boundary, so a row written directly to the
        database cannot publish a negative step."""

        from yob_storefront.services.pricing_service import configured_number

        for bad in (-1, -0.5, -1000):
            self.assertIsNone(configured_number(bad), f"{bad} was published")

        for good in (0.5, 1, 10, 2.5):
            self.assertEqual(configured_number(good), good)

    def test_positive_values_are_published(self):
        item = self.make_item("_P29-SET")
        self.make_price(item, rate=100, moq=10, multiplier=6, mrp=250)

        detail = self.detail("_p29-set")

        self.assertEqual(detail["mrp"], 250)
        self.assertEqual(detail["quantity_control"]["moq"], 10)
        self.assertEqual(detail["quantity_control"]["quantity_multiplier"], 6)

    def test_fractional_guidance_survives(self):
        """Float, not Int: a step of 0.5 is legitimate for a weighed product."""

        item = self.make_item("_P29-FRACTION")
        self.make_price(item, moq=2.5, multiplier=0.5)

        control = self.control("_p29-fraction")

        self.assertEqual(control["moq"], 2.5)
        self.assertEqual(control["quantity_multiplier"], 0.5)


# =========================================================================
# MRP IS INFORMATIONAL ONLY
# =========================================================================

class MRPIsInformationalCase(GuidanceBase):

    MONEY = ("base_price", "rate", "discount_percentage", "discount_amount",
             "net_amount", "tax_amount", "total_amount")

    def money_of(self, detail):
        return {key: detail[key] for key in self.MONEY}

    def test_changing_mrp_alone_changes_no_monetary_value(self):
        """THE guard. MRP must never reach ERPNext pricing."""

        item = self.make_item("_P29-MONEY")
        price = self.make_price(item, rate=100)

        without = self.money_of(self.detail("_p29-money"))

        frappe.db.set_value("Item Price", price, "custom_mrp", 999)
        frappe.clear_cache()

        with_mrp = self.detail("_p29-money")

        self.assertEqual(self.money_of(with_mrp), without,
                         "MRP leaked into a monetary value")
        self.assertEqual(with_mrp["mrp"], 999)

    def test_no_saving_or_discount_is_derived_from_mrp(self):
        """No `savings`, `you_save` or MRP-derived percentage is invented."""

        item = self.make_item("_P29-NOSAVE")
        self.make_price(item, rate=700, mrp=1000)

        detail = self.detail("_p29-nosave")

        self.assertEqual(detail["mrp"], 1000)
        self.assertEqual(detail["base_price"], 700)
        # The discount fields describe the ERPNext price list -> rate movement,
        # which MRP is not part of.
        self.assertFalse(detail["discount_amount"])

        for invented in ("savings", "you_save", "mrp_discount_percentage",
                         "discount_off_mrp", "mrp_savings"):
            self.assertNotIn(invented, detail)

    def test_mrp_survives_a_pricing_rule_that_disallows_quantity_control(self):
        """MRP has no conflict concept: it is not a quantity behaviour."""

        item = self.make_item("_P29-MRPRULE")
        self.make_price(item, rate=1000, moq=10, multiplier=5, mrp=1500)
        self.make_rule(item, discount=30)

        detail = self.detail("_p29-mrprule")

        self.assertFalse(detail["quantity_control"]["allowed"])
        self.assertEqual(detail["mrp"], 1500, "MRP was withheld with the guidance")


# =========================================================================
# QUANTITY GUIDANCE AND `allowed`
# =========================================================================

class QuantityControlCase(GuidanceBase):

    def test_a_plain_item_price_context_allows_the_guidance(self):
        item = self.make_item("_P29-PLAIN")
        self.make_price(item, rate=100, moq=10, multiplier=6)

        control = self.control("_p29-plain")

        self.assertTrue(control["allowed"])
        self.assertEqual(control["moq"], 10)
        self.assertEqual(control["quantity_multiplier"], 6)

    def test_an_applied_pricing_rule_disallows_the_guidance(self):
        """A rule can change behaviour at a quantity threshold, so "start at 10,
        step by 6" stops being a promise the storefront can keep."""

        item = self.make_item("_P29-RULE")
        self.make_price(item, rate=100, moq=10, multiplier=6)
        self.make_rule(item, discount=15)

        detail = self.detail("_p29-rule")

        self.assertIsNotNone(detail["pricing_rule_label"],
                             "the fixture did not actually apply a rule")
        self.assertFalse(detail["quantity_control"]["allowed"])

    def test_allowed_tracks_the_authoritative_preview_not_a_prediction(self):
        """`allowed` is read from the SAME `pricing_rules` the preview published,
        so it can never disagree with `pricing_rule_label`."""

        item = self.make_item("_P29-TRACK")
        self.make_price(item, rate=100, moq=4, multiplier=2)

        before = self.detail("_p29-track")
        self.assertIsNone(before["pricing_rule_label"])
        self.assertTrue(before["quantity_control"]["allowed"])

        self.make_rule(item, discount=5)
        frappe.clear_cache()

        after = self.detail("_p29-track")
        self.assertIsNotNone(after["pricing_rule_label"])
        self.assertFalse(after["quantity_control"]["allowed"])

    def test_the_configured_values_are_still_published_when_disallowed(self):
        """Withheld from APPLICATION, not from view: Desk and support still need
        to see what a merchant configured."""

        item = self.make_item("_P29-SHOWN")
        self.make_price(item, rate=100, moq=12, multiplier=4)
        self.make_rule(item, discount=20)

        control = self.control("_p29-shown")

        self.assertFalse(control["allowed"])
        self.assertEqual(control["moq"], 12)
        self.assertEqual(control["quantity_multiplier"], 4)

    def test_moq_and_multiplier_share_one_compatibility_flag(self):
        """A pair, deliberately. There is no per-field allow state to disagree
        about: a start with an unsafe step is as unusable as the reverse."""

        item = self.make_item("_P29-PAIR")
        self.make_price(item, rate=100, moq=10, multiplier=6)
        self.make_rule(item, discount=10)

        control = self.control("_p29-pair")

        self.assertEqual(set(control), {"moq", "quantity_multiplier", "allowed"})
        for invented in ("moq_allowed", "multiplier_allowed", "step_allowed"):
            self.assertNotIn(invented, control)

    def test_either_value_alone_is_published(self):
        """The pair rule is about CONFLICT, not about configuring both."""

        moq_only = self.make_item("_P29-MOQONLY")
        self.make_price(moq_only, moq=10)

        step_only = self.make_item("_P29-STEPONLY")
        self.make_price(step_only, multiplier=5)

        first = self.control("_p29-moqonly")
        self.assertEqual(first["moq"], 10)
        self.assertIsNone(first["quantity_multiplier"])
        self.assertTrue(first["allowed"])

        second = self.control("_p29-steponly")
        self.assertIsNone(second["moq"])
        self.assertEqual(second["quantity_multiplier"], 5)
        self.assertTrue(second["allowed"])


# =========================================================================
# THE RESOLVED ITEM PRICE SOURCE
# =========================================================================

class ResolvedSourceCase(GuidanceBase):
    """The metadata follows the row ERPNext priced against -- never any other."""

    def test_a_customer_specific_price_supplies_the_metadata(self):
        """The generic row's values must NOT win when a customer row exists."""

        item = self.make_item("_P29-CUSTOMER")
        self.make_price(item, rate=100, moq=1, multiplier=1, mrp=111)
        self.make_price(item, rate=80, moq=25, multiplier=5, mrp=222,
                        customer=self.customer.name)

        detail = self.detail("_p29-customer")

        self.assertEqual(detail["base_price"], 80, "the fixture did not price the customer row")
        self.assertEqual(detail["mrp"], 222)
        self.assertEqual(detail["quantity_control"]["moq"], 25)
        self.assertEqual(detail["quantity_control"]["quantity_multiplier"], 5)

    def test_metadata_is_not_taken_from_an_arbitrary_other_row(self):
        """A row on a DIFFERENT price list must never supply the values."""

        other_list = frappe.get_doc({
            "doctype": "Price List", "price_list_name": "_P29 Other",
            "selling": 1, "currency": "INR", "enabled": 1,
        }).insert(ignore_permissions=True).name

        item = self.make_item("_P29-OTHERLIST")
        self.make_price(item, rate=100, moq=7, mrp=123)
        self.make_price(item, rate=50, moq=99, mrp=999, price_list=other_list)

        detail = self.detail("_p29-otherlist")

        self.assertEqual(detail["quantity_control"]["moq"], 7)
        self.assertEqual(detail["mrp"], 123)

    def test_a_price_with_no_metadata_publishes_nulls_not_a_neighbours_values(self):
        item = self.make_item("_P29-NEIGHBOUR")
        self.make_price(item, rate=100)

        # A different SKU carrying values, to prove nothing searches by item alone.
        noisy = self.make_item("_P29-NOISY")
        self.make_price(noisy, rate=100, moq=50, multiplier=7, mrp=777)

        detail = self.detail("_p29-neighbour")

        self.assertIsNone(detail["mrp"])
        self.assertIsNone(detail["quantity_control"]["moq"])

    def test_the_resolver_returns_the_row_erpnext_would_pick(self):
        """Called directly: the identity, not the rate."""

        from yob_storefront.services.pricing_service import resolve_item_price_source

        item = self.make_item("_P29-SOURCE")
        self.make_price(item, rate=100, moq=1)
        specific = self.make_price(item, rate=80, moq=9,
                                   customer=self.customer.name)

        found = resolve_item_price_source(
            item, price_list=self.price_list, customer=self.customer.name,
            uom=self.uom, stock_uom=self.uom, transaction_date=today())

        self.assertEqual(found, specific)

    def test_an_item_with_no_price_at_all_resolves_to_nothing(self):
        from yob_storefront.services.pricing_service import resolve_item_price_source

        item = self.make_item("_P29-UNPRICED")

        self.assertIsNone(resolve_item_price_source(
            item, price_list=self.price_list, customer=self.customer.name,
            uom=self.uom, stock_uom=self.uom, transaction_date=today()))


# =========================================================================
# VARIANTS
# =========================================================================

class VariantGuidanceCase(GuidanceBase):
    """`resolve_variant` republishes these as the SKU facts they are."""

    COLOUR = "Colour"

    def family(self, code="_P29-FAM"):
        from erpnext.controllers.item_variant import create_variant

        if not frappe.db.exists("Item Attribute", self.COLOUR):
            self.skipTest("Item Attribute 'Colour' is not configured here")

        values = frappe.get_all("Item Attribute Value",
                                filters={"parent": self.COLOUR}, pluck="attribute_value")
        if len(values) < 2:
            self.skipTest("Colour has fewer than two values on this bench")

        template = frappe.get_doc({
            "doctype": "Item", "item_code": code, "item_name": code,
            "item_group": self.item_group, "stock_uom": self.uom, "is_stock_item": 0,
            "is_sales_item": 1, "gst_hsn_code": self.hsn, "custom_slug": code.lower(),
            "custom_category": self.category, "has_variants": 1,
            "attributes": [{"attribute": self.COLOUR}],
        }).insert(ignore_permissions=True)

        variants = {}
        for value in values[:2]:
            variant = create_variant(template.name, {self.COLOUR: value})
            variant.insert(ignore_permissions=True)
            variants[value] = variant.name

        return template, variants

    def resolve(self, template, attributes, qty=1):
        import json

        frappe.clear_cache()
        response = inspect.unwrap(self.api.resolve_variant)(
            template=template, attributes=json.dumps(attributes), qty=qty,
            auth_context={})
        self.assertNotIn("errors", response, f"resolve_variant failed: {response}")
        return response["data"]

    def test_resolve_variant_publishes_the_resolved_skus_metadata(self):
        template, variants = self.family()
        first, second = list(variants.items())

        self.make_price(first[1], rate=100, moq=10, multiplier=2, mrp=150)
        self.make_price(second[1], rate=200, moq=30, multiplier=9, mrp=350)

        one = self.resolve(template.name, {self.COLOUR: first[0]})
        two = self.resolve(template.name, {self.COLOUR: second[0]})

        self.assertEqual(one["mrp"], 150)
        self.assertEqual(one["quantity_control"]["moq"], 10)

        self.assertEqual(two["mrp"], 350)
        self.assertEqual(two["quantity_control"]["moq"], 30)
        self.assertEqual(two["quantity_control"]["quantity_multiplier"], 9)

    def test_resolve_variant_uses_the_same_single_public_location(self):
        """The shared serializer, so the nested payload is clean here too."""

        template, variants = self.family("_P29-FAM-HOME")
        colour, code = next(iter(variants.items()))
        self.make_price(code, rate=100, moq=8, multiplier=4, mrp=300)

        detail = self.resolve(template.name, {self.COLOUR: colour})

        self.assertEqual(detail["mrp"], 300)
        self.assertEqual(detail["quantity_control"]["moq"], 8)

        self.assertNotIn("mrp", detail["pricing"])
        self.assertNotIn("quantity_control", detail["pricing"])

    def test_resolve_variant_still_carries_no_merchandising(self):
        """Phase 27's line stays exactly where it was."""

        template, variants = self.family("_P29-FAM-CLEAN")
        colour, code = next(iter(variants.items()))
        self.make_price(code, rate=100, moq=5, mrp=200)

        detail = self.resolve(template.name, {self.COLOUR: colour})

        self.assertNotIn("gallery", detail)
        self.assertNotIn("sections", detail)
        self.assertIn("mrp", detail)
        self.assertIn("quantity_control", detail)

    def test_erpnext_refuses_an_item_price_on_a_template(self):
        """WHY the variant -> template fallback is near-unreachable in practice.

        ERPNext's own `ItemPrice.validate` rejects a price on an item with
        `has_variants`, which is the same constraint the catalogue already relies
        on (a family card carries no price because ERPNext cannot price a
        template). So the fallback at `get_item_details.py:1043` exists but has
        nothing to find, unless a template acquired a price BEFORE it became one.

        Pinned here so the next reader does not mistake the fallback below for
        the ordinary path -- and so a future ERPNext that permits template prices
        shows up as a failure here rather than as silently changed metadata.
        """

        template, _variants = self.family("_P29-FAM-REFUSE")

        with self.assertRaises(frappe.ValidationError):
            self.make_price(template.name, rate=100, moq=14)

    def template_price(self, template, **kw):
        """A template Item Price, via the only transition that produces one.

        `has_variants` is lowered around the insert with `db.set_value`, which
        skips validation. That reproduces a REAL stored state -- an item priced
        while simple, then turned into a family -- without pretending ERPNext
        would accept the insert directly. It is the only way to exercise the
        fallback at all.
        """

        frappe.db.set_value("Item", template, "has_variants", 0)
        frappe.clear_cache()
        try:
            return self.make_price(template, **kw)
        finally:
            frappe.db.set_value("Item", template, "has_variants", 1)
            frappe.clear_cache()

    def test_metadata_follows_the_variant_to_template_price_fallback(self):
        """ERPNext falls back variant -> template (`get_item_details.py:1043`),
        so the metadata must follow the SAME row, not answer null."""

        from yob_storefront.services.pricing_service import resolve_item_price_source

        template, variants = self.family("_P29-FAM-FALL")
        colour, code = next(iter(variants.items()))

        # Priced on the TEMPLATE only; the variant carries no Item Price.
        source = self.template_price(template.name, rate=100, moq=14,
                                     multiplier=7, mrp=400)

        found = resolve_item_price_source(
            code, price_list=self.price_list, customer=self.customer.name,
            uom=self.uom, stock_uom=self.uom, transaction_date=today())

        self.assertEqual(found, source,
                         "the fallback did not reach the template's Item Price")

    def test_a_variants_own_price_beats_its_templates(self):
        """The fallback is a LAST resort, exactly as ERPNext's `is None` guard
        makes it: a variant that has its own price never consults the template."""

        from yob_storefront.services.pricing_service import resolve_item_price_source

        template, variants = self.family("_P29-FAM-OWN")
        colour, code = next(iter(variants.items()))

        self.template_price(template.name, rate=100, moq=14, mrp=400)
        own = self.make_price(code, rate=90, moq=3, mrp=120)

        found = resolve_item_price_source(
            code, price_list=self.price_list, customer=self.customer.name,
            uom=self.uom, stock_uom=self.uom, transaction_date=today())

        self.assertEqual(found, own)


# =========================================================================
# THE BOUNDARY: NO BACKEND ENFORCEMENT
# =========================================================================

class NoBackendEnforcementCase(GuidanceBase):
    """MOQ and the multiplier are GUIDANCE. Nothing refuses a quantity.

    This is the most important case in the file. Every other test would still
    pass if a future change started rejecting sub-MOQ quantities in the cart --
    this one would not.
    """

    def setUp(self):
        super().setUp()
        from yob_storefront.api import cart as cart_api

        self.cart_api = cart_api
        p = patch.object(cart_api, "get_storefront_customer", return_value=self.customer)
        p.start()
        self.addCleanup(p.stop)

        inspect.unwrap(cart_api.clear_cart)(auth_context={})

    def add(self, item_code, qty):
        return inspect.unwrap(self.cart_api.add_to_cart)(
            item_code=item_code, qty=qty, auth_context={})

    def test_a_quantity_below_moq_is_accepted(self):
        """MOQ 10, multiplier 5, request 3 -- and it goes through."""

        item = self.make_item("_P29-NOENFORCE")
        self.make_price(item, rate=100, moq=10, multiplier=5)

        response = self.add(item, 3)

        self.assertNotIn("errors", response,
                         f"MOQ became backend enforcement: {response}")

        rows = [r for r in response["data"]["items"] if r["item_code"] == item]
        self.assertEqual(len(rows), 1)
        self.assertEqual(float(rows[0]["quantity"]), 3.0,
                         "the quantity was silently corrected to satisfy MOQ")

    def test_a_quantity_off_the_multiplier_sequence_is_accepted(self):
        """MOQ 10 + step 6 offers 10, 16, 22. 13 is not in that sequence and is
        still a perfectly valid order."""

        item = self.make_item("_P29-OFFSTEP")
        self.make_price(item, rate=100, moq=10, multiplier=6)

        response = self.add(item, 13)

        self.assertNotIn("errors", response, f"the multiplier became a rule: {response}")
        rows = [r for r in response["data"]["items"] if r["item_code"] == item]
        self.assertEqual(float(rows[0]["quantity"]), 13.0)

    def test_no_new_quantity_error_code_exists(self):
        """Named explicitly so adding one is a deliberate, visible act."""

        from yob_storefront.api import response as storefront_response

        published = {value for name, value in vars(storefront_response).items()
                     if name.isupper() and isinstance(value, str)}

        for invented in ("minimum_order_qty", "invalid_quantity_step",
                         "quantity_below_moq", "quantity_multiplier_invalid",
                         "moq_not_met"):
            self.assertNotIn(invented, published,
                             f"{invented} turned guidance into enforcement")

    def test_guidance_does_not_change_what_the_cart_charges(self):
        """The cart prices a sub-MOQ quantity exactly as it always did."""

        item = self.make_item("_P29-CHARGE")
        price = self.make_price(item, rate=100)

        plain = self.add(item, 3)
        plain_rows = [r for r in plain["data"]["items"] if r["item_code"] == item]
        before = float(plain_rows[0]["amount"])

        inspect.unwrap(self.cart_api.clear_cart)(auth_context={})
        frappe.db.set_value("Item Price", price, "custom_moq", 10)
        frappe.db.set_value("Item Price", price, "custom_quantity_multiplier", 5)
        frappe.db.set_value("Item Price", price, "custom_mrp", 999)
        frappe.clear_cache()

        after_response = self.add(item, 3)
        after_rows = [r for r in after_response["data"]["items"]
                      if r["item_code"] == item]

        self.assertEqual(float(after_rows[0]["amount"]), before,
                         "storefront metadata changed a cart amount")


# =========================================================================
# ONE PUBLIC HOME
# =========================================================================

class SinglePublicLocationCase(GuidanceBase):
    """The metadata is published ONCE, at the top level of the detail.

    `pricing` is a passthrough of the pricing service and historically repeats
    `rate`, `base_price`, `uom` and others. That history is not a licence to give
    a NEW field two public homes: two locations are two things a client may read
    and two things that can drift apart.

    The nested payload must therefore look exactly as it did before Phase 29A.
    """

    def test_the_detail_publishes_the_metadata_at_the_top_level(self):
        item = self.make_item("_P29-HOME")
        self.make_price(item, rate=100, moq=10, multiplier=6, mrp=250)

        detail = self.detail("_p29-home")

        self.assertEqual(detail["mrp"], 250)
        self.assertEqual(detail["quantity_control"]["moq"], 10)
        self.assertEqual(detail["quantity_control"]["quantity_multiplier"], 6)
        self.assertTrue(detail["quantity_control"]["allowed"])

    def test_the_nested_pricing_payload_does_not_repeat_them(self):
        item = self.make_item("_P29-NONEST")
        self.make_price(item, rate=100, moq=10, multiplier=6, mrp=250)

        pricing = self.detail("_p29-nonest")["pricing"]

        self.assertNotIn("mrp", pricing)
        self.assertNotIn("quantity_control", pricing)

    def test_the_nested_payload_keeps_everything_it_carried_before(self):
        """Stripping two keys must not quietly remove a third."""

        item = self.make_item("_P29-INTACT")
        self.make_price(item, rate=100, moq=10, mrp=250)

        pricing = self.detail("_p29-intact")["pricing"]

        for key in ("item", "selling_price_list", "qty", "base_price", "rate",
                    "discount_percentage", "discount_amount", "total_discount",
                    "net_amount", "tax_amount", "tax_label", "total_amount",
                    "pricing_rules", "pricing_rule_label", "pricing_rule_apply_on",
                    "uom", "stock_uom", "conversion_factor", "stock_qty"):
            self.assertIn(key, pricing, f"{key} was lost from the pricing payload")

    def test_the_metadata_is_absent_from_pricing_even_when_unconfigured(self):
        """Absent, not present-and-null: the keys do not exist there at all."""

        item = self.make_item("_P29-NONE-NEST")
        self.make_price(item, rate=100)

        detail = self.detail("_p29-none-nest")

        self.assertIsNone(detail["mrp"])
        self.assertNotIn("mrp", detail["pricing"])
        self.assertNotIn("quantity_control", detail["pricing"])


# =========================================================================
# THE LISTING STAYS LIGHT
# =========================================================================

class ListingIsUnchangedCase(GuidanceBase):

    def test_listing_cards_carry_no_storefront_price_metadata(self):
        """Phase 22B's payload budget is not spent on buying-area facts."""

        item = self.make_item("_P29-CARD")
        self.make_price(item, rate=100, moq=10, multiplier=5, mrp=250)

        slug = frappe.db.get_value("Category", self.category, "slug")
        if not slug:
            self.skipTest("the seeded category has no slug")

        frappe.clear_cache()
        listing = inspect.unwrap(self.api.get_items)(
            auth_context={}, scope_value=slug, search="_P29-CARD")
        self.assertNotIn("errors", listing, listing)

        for row in listing["data"]["items"]:
            for absent in ("mrp", "quantity_control", "moq", "quantity_multiplier"):
                self.assertNotIn(absent, row, f"{absent} leaked into a listing card")

    def test_a_listing_page_does_not_pay_for_the_extra_lookups(self):
        """The metadata is opt-in, so the catalogue never resolves an Item Price
        source it has no use for."""

        from yob_storefront.services import pricing_service

        item = self.make_item("_P29-COST")
        self.make_price(item, rate=100, moq=10, mrp=250)

        slug = frappe.db.get_value("Category", self.category, "slug")
        if not slug:
            self.skipTest("the seeded category has no slug")

        with patch.object(pricing_service, "storefront_price_metadata",
                          side_effect=AssertionError("the listing resolved metadata")):
            frappe.clear_cache()
            listing = inspect.unwrap(self.api.get_items)(
                auth_context={}, scope_value=slug, search="_P29-COST")

        self.assertNotIn("errors", listing, listing)


if __name__ == "__main__":
    unittest.main()
