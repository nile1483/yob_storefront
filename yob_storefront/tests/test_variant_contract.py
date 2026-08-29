# Copyright (c) 2026, YOB and Shayona
"""The published variant contract, proven end to end (Phase 24D-1).

WHY THIS FILE EXISTS SEPARATELY
-------------------------------
`test_variant_catalog.py` proves the BEHAVIOUR. This proves the WIRE: the exact
method, the exact encoding a browser sends, the exact key sets a client builds
DTOs from, and one complete journey from a family page to a committed Draft Sales
Order. If any of it drifts, `reference/api/` and the Angular DTOs are wrong, which
is a class of breakage no behavioural test catches.

THE SCENARIO
------------
    Template     Colour: Orange | Yellow      Size: Medium | Large
    Variants     Orange/Medium and Yellow/Large ONLY

Orange/Large and Yellow/Medium are attribute values that exist independently and a
combination that does not. The storefront must never offer them, and the resolver
must never invent them.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
Server-side state for a partial selection. A half-chosen selection is a browser
concern; the server answers a complete one and revalidates the resolved SKU again
at Add to Cart. See `PartialSelectionIsStatelessCase`.
"""

import inspect
import json
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_days, flt, today

CUSTOMER = "YOB Demo Buyer"
SEED_ITEM = "YOB-BOLT-M10"
CONTACT = "Demo Buyer-YOB Demo Buyer"
BILLING = "YOB Demo Billing-Billing"
SHIPPING = "YOB Demo Shipping-Shipping"

COLOUR = "Colour"
SIZE = "Size"

#: The published key set of the shared product-detail serializer. Angular's
#: resolved-item DTO is built from exactly this, so a change here is a contract
#: change that must reach `reference/api/` in the same commit.
PRODUCT_DETAIL_KEYS = {
    "name", "item_name", "image", "item_group", "custom_slug", "qty",
    "is_template", "is_purchasable", "variant_of", "selected",
    "base_price", "rate", "discount_percentage", "discount_amount",
    "net_amount", "tax_amount", "tax_label", "total_amount",
    "uom", "stock_uom", "conversion_factor", "stock_qty",
    "is_stock_item", "warehouse", "actual_qty",
    "pricing", "pricing_rule_label", "pricing_rule_apply_on", "available_rules",
}

#: Merchandising, added to a PRODUCT PAGE by `get_item` (Phase 27B).
#:
#: Deliberately NOT part of `PRODUCT_DETAIL_KEYS` above. That constant is the
#: shared serializer -- what `resolve_variant` returns -- and merchandising
#: belongs to the public product entity, not to a resolved SKU: choosing a size
#: changes price and stock, never the gallery. The split is the contract.
MERCHANDISING_KEYS = {"gallery", "sections"}

#: What `get_item` returns for a simple product: the serializer plus the page's
#: own merchandising.
PRODUCT_PAGE_KEYS = PRODUCT_DETAIL_KEYS | MERCHANDISING_KEYS

#: The published key set of a variant family page.
FAMILY_KEYS = {
    "name", "item_name", "item_group", "image", "custom_slug",
    "is_template", "is_purchasable", "variant_of", "attributes", "variants",
} | MERCHANDISING_KEYS

#: The published key set of a listing card, identical for both kinds.
LISTING_CARD_KEYS = {
    "name", "item_name", "slug", "stock_uom", "uom", "conversion_factor", "image",
    "has_variants", "price_state", "base_price", "rate", "discount_percentage",
    "discount_amount", "net_amount", "tax_amount", "total_amount",
    "pricing_rule_label",
}


def _seeded():
    return bool(frappe.db.exists("Customer", CUSTOMER) and frappe.db.exists("Item", SEED_ITEM))


class ContractBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data on the test site")

    def setUp(self):
        frappe.set_user("Administrator")
        from yob_storefront.api import cart as cart_api, catalog
        from yob_storefront.utils.store import get_store_settings

        self.catalog = catalog
        self.cart_api = cart_api

        self.commits = []
        cp = patch.object(frappe.db, "commit", side_effect=lambda *a, **k: self.commits.append(1))
        cp.start()
        self.addCleanup(cp.stop)

        store = get_store_settings()
        self.company = store.company
        self.currency = store.default_currency
        self.item_group = frappe.db.get_value("Item", SEED_ITEM, "item_group")
        self.stock_uom = frappe.db.get_value("Item", SEED_ITEM, "stock_uom")
        self.hsn = frappe.db.get_value("Item", SEED_ITEM, "gst_hsn_code")
        self.category = frappe.db.get_value("Item", SEED_ITEM, "custom_category")
        self.price_list = frappe.get_single("Selling Settings").selling_price_list
        self.customer = frappe.get_doc("Customer", CUSTOMER)

        for attribute in (COLOUR, SIZE):
            if not frappe.db.exists("Item Attribute", attribute):
                raise unittest.SkipTest(f"Item Attribute {attribute!r} is not configured here")

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()
        frappe.flags.attribute_values = None
        self.assertEqual(self.commits, [], "a commit escaped the test rollback")

    # ------------------------------------------------------------- fixture

    def ensure_value(self, attribute, value):
        """The scenario's own attribute values, added if this bench lacks them.

        `Item Attribute Value` is global, so Orange and Yellow may or may not
        already exist here. Adding them is rolled back with everything else.
        """

        if not frappe.db.exists("Item Attribute Value",
                                {"parent": attribute, "attribute_value": value}):
            doc = frappe.get_doc("Item Attribute", attribute)
            doc.append("item_attribute_values",
                       {"attribute_value": value, "abbr": value[:3].upper()})
            doc.save(ignore_permissions=True)
            frappe.clear_document_cache("Item Attribute", attribute)

        # ERPNext caches the whole value map on frappe.flags for the request.
        frappe.flags.attribute_values = None
        return value

    def scenario(self, code="_V24D-TEE"):
        """Colour x Size, with Orange/Large and Yellow/Medium NOT generated."""

        from erpnext.controllers.item_variant import create_variant

        orange = self.ensure_value(COLOUR, "Orange")
        yellow = self.ensure_value(COLOUR, "Yellow")
        medium = self.ensure_value(SIZE, "Medium")
        large = self.ensure_value(SIZE, "Large")

        template = frappe.get_doc({
            "doctype": "Item", "item_code": code, "item_name": code,
            "item_group": self.item_group, "stock_uom": self.stock_uom,
            "is_stock_item": 1, "is_sales_item": 1, "gst_hsn_code": self.hsn,
            "custom_slug": code.lower(), "custom_category": self.category,
            "has_variants": 1,
            "attributes": [{"attribute": COLOUR}, {"attribute": SIZE}],
        }).insert(ignore_permissions=True)

        variants = {}
        for colour, size, price in ((orange, medium, 100), (yellow, large, 250)):
            variant = create_variant(template.name, {COLOUR: colour, SIZE: size})
            variant.insert(ignore_permissions=True)
            frappe.get_doc({
                "doctype": "Item Price", "item_code": variant.name,
                "price_list": self.price_list, "price_list_rate": price,
                "selling": 1, "uom": self.stock_uom}).insert(ignore_permissions=True)
            variants[(colour, size)] = variant.name

        self.values = {"orange": orange, "yellow": yellow,
                       "medium": medium, "large": large}
        frappe.clear_cache()
        return template, variants

    # ------------------------------------------------------------- the wire

    def get_item(self, slug, qty="1"):
        """Called the way Frappe delivers an HTTP GET: every value a STRING."""

        frappe.clear_cache()
        with patch.object(self.catalog, "get_storefront_customer", return_value=self.customer):
            return inspect.unwrap(self.catalog.get_item)(auth_context={}, slug=slug, qty=qty)

    def resolve(self, template, attributes, qty="1"):
        frappe.clear_cache()
        payload = attributes if isinstance(attributes, str) else json.dumps(attributes)
        with patch.object(self.catalog, "get_storefront_customer", return_value=self.customer):
            return inspect.unwrap(self.catalog.resolve_variant)(
                auth_context={}, template=template, attributes=payload, qty=qty)

    def listing(self):
        slug = frappe.db.get_value("Category", self.category, "slug")
        if not slug:
            self.skipTest("the seeded item has no storefront category")

        frappe.clear_cache()
        with patch.object(self.catalog, "get_storefront_customer", return_value=self.customer):
            response = inspect.unwrap(self.catalog.get_items)(
                auth_context={}, scope_type="category", scope_value=slug, page_size="48")
        self.assertNotIn("errors", response, response)
        return {row["name"]: row for row in response["data"]["items"]}

    def add_to_cart(self, item_code, qty="2"):
        frappe.clear_cache()
        with patch.object(self.cart_api, "get_storefront_customer", return_value=self.customer):
            return inspect.unwrap(self.cart_api.add_to_cart)(
                auth_context={}, item_code=item_code, qty=qty)

    def fresh_cart(self):
        cart = self.cart_api.get_or_create_cart(self.customer)
        cart.set("items", [])
        cart.coupon_code = None
        cart.save(ignore_permissions=True)
        return cart

    def error_of(self, response):
        self.assertIn("errors", response, f"expected a refusal, got {response}")
        return response["errors"][0]


# =========================================================
# TASK 2 -- THE WIRE ANGULAR ACTUALLY USES
# =========================================================

class RequestEncodingCase(ContractBase):

    def test_resolve_variant_is_a_get_endpoint(self):
        methods = frappe.allowed_http_methods_for_whitelisted_func.get(
            self.catalog.resolve_variant)

        self.assertEqual(list(methods), ["GET"])

    def test_attributes_arrive_as_a_json_string(self):
        """Exactly what a query parameter delivers: a string, not a mapping."""

        template, variants = self.scenario()

        response = self.resolve(
            template.name,
            json.dumps({COLOUR: self.values["orange"], SIZE: self.values["medium"]}))

        self.assertNotIn("errors", response, response)
        self.assertEqual(response["data"]["name"],
                         variants[(self.values["orange"], self.values["medium"])])

    def test_a_mapping_is_accepted_too(self):
        """Internal callers and a JSON body both hand over a dict."""

        template, variants = self.scenario()

        with patch.object(self.catalog, "get_storefront_customer", return_value=self.customer):
            response = inspect.unwrap(self.catalog.resolve_variant)(
                auth_context={}, template=template.name,
                attributes={COLOUR: self.values["orange"], SIZE: self.values["medium"]})

        self.assertEqual(response["data"]["name"],
                         variants[(self.values["orange"], self.values["medium"])])

    def test_malformed_attributes_are_refused_not_crashed(self):
        template, _ = self.scenario()

        for payload in ("not json at all", "[1,2,3]", '"a string"', "{}"):
            with self.subTest(payload=payload):
                error = self.error_of(self.resolve(template.name, payload))
                self.assertEqual(error["code"], "variant_attributes_required")

    def test_qty_one_as_a_string_prices_the_initial_preview(self):
        template, _ = self.scenario()

        data = self.resolve(template.name,
                            {COLOUR: self.values["orange"], SIZE: self.values["medium"]},
                            qty="1")["data"]

        self.assertEqual(data["qty"], 1.0)
        self.assertEqual(flt(data["rate"]), 100.0)
        self.assertEqual(flt(data["net_amount"]), 100.0)

    def test_a_larger_quantity_reprices_the_preview(self):
        template, _ = self.scenario()

        data = self.resolve(template.name,
                            {COLOUR: self.values["orange"], SIZE: self.values["medium"]},
                            qty="4")["data"]

        self.assertEqual(data["qty"], 4.0)
        self.assertEqual(flt(data["net_amount"]), 400.0)

    def test_every_refusal_uses_the_documented_code_and_status(self):
        template, _ = self.scenario()

        cases = (
            ({COLOUR: self.values["orange"]}, "variant_attributes_required", 422),
            ({COLOUR: self.values["orange"], SIZE: self.values["large"]},
             "variant_not_available", 422),
        )

        for attributes, code, status in cases:
            with self.subTest(code=code):
                error = self.error_of(self.resolve(template.name, attributes))
                self.assertEqual(error["code"], code)
                self.assertEqual(error["field"], "attributes")
                self.assertEqual(frappe.local.response.get("http_status_code"), status)
                self.assertNotIn("traceback", json.dumps(error).lower())

    def test_a_manufacturer_family_page_answers_the_documented_code(self):
        template = frappe.get_doc({
            "doctype": "Item", "item_code": "_V24D-MFG", "item_name": "_V24D-MFG",
            "item_group": self.item_group, "stock_uom": self.stock_uom,
            "is_stock_item": 1, "is_sales_item": 1, "gst_hsn_code": self.hsn,
            "custom_slug": "_v24d-mfg", "custom_category": self.category,
            "has_variants": 1, "variant_based_on": "Manufacturer"}).insert(
            ignore_permissions=True)

        error = self.error_of(self.get_item(template.custom_slug))

        self.assertEqual(error["code"], "variant_family_unsupported")
        self.assertEqual(frappe.local.response.get("http_status_code"), 422)


# =========================================================
# TASK 2 -- THE DTO KEY SETS ANGULAR BUILDS AGAINST
# =========================================================

class PublishedShapeCase(ContractBase):
    """A key set is a contract. Adding or dropping one breaks a typed client."""

    def test_family_payload_shape(self):
        template, _ = self.scenario()

        data = self.get_item(template.custom_slug)["data"]

        self.assertEqual(set(data), FAMILY_KEYS)
        self.assertEqual(data["is_template"], 1)
        self.assertEqual(data["is_purchasable"], 0)
        self.assertIsNone(data["variant_of"])

        for attribute in data["attributes"]:
            self.assertEqual(set(attribute), {"attribute", "numeric", "values"})

        for variant in data["variants"]:
            self.assertEqual(set(variant), {"item_code", "attributes"})

    def test_resolved_payload_shape(self):
        template, _ = self.scenario()

        data = self.resolve(template.name,
                            {COLOUR: self.values["orange"], SIZE: self.values["medium"]})["data"]

        # The SERIALIZER's key set, with no merchandising: a resolved SKU is a
        # transaction fact, and the family page already carried the gallery.
        self.assertEqual(set(data), PRODUCT_DETAIL_KEYS)
        self.assertEqual(set(data) & MERCHANDISING_KEYS, set(),
                         "resolve_variant grew product merchandising")
        self.assertEqual(data["is_template"], 0)
        self.assertEqual(data["is_purchasable"], 1)
        self.assertEqual(data["variant_of"], template.name)
        self.assertEqual(data["selected"],
                         {COLOUR: self.values["orange"], SIZE: self.values["medium"]})

    def test_a_simple_item_uses_the_same_serializer(self):
        """One DTO for a resolved variant and a simple product, plus the page's
        own merchandising."""

        slug = frappe.db.get_value("Item", SEED_ITEM, "custom_slug")

        data = self.get_item(slug)["data"]

        self.assertEqual(set(data), PRODUCT_PAGE_KEYS)
        # The serializer half is still exactly what `resolve_variant` returns.
        self.assertEqual(set(data) - MERCHANDISING_KEYS, PRODUCT_DETAIL_KEYS)
        self.assertEqual(data["is_purchasable"], 1)
        self.assertIsNone(data["variant_of"])
        self.assertIsNone(data["selected"])

    def test_listing_card_shapes(self):
        template, variants = self.scenario()

        cards = self.listing()

        family = cards[template.name]
        self.assertEqual(set(family), LISTING_CARD_KEYS)
        self.assertEqual(family["has_variants"], 1)
        self.assertEqual(family["price_state"], "select_options")
        self.assertIsNone(family["rate"])

        simple = cards[SEED_ITEM]
        self.assertEqual(set(simple), LISTING_CARD_KEYS)
        self.assertEqual(simple["has_variants"], 0)
        self.assertEqual(simple["price_state"], "priced")
        self.assertTrue(simple["rate"])

        for code in variants.values():
            self.assertNotIn(code, cards, "an individual variant was listed")


# =========================================================
# TASK 3 -- THE COMPLETE JOURNEY
# =========================================================

class CrossStackScenarioCase(ContractBase):
    """Family page -> resolver -> actual SKU -> Cart -> Draft Sales Order."""

    def test_the_family_advertises_exactly_the_real_combinations(self):
        template, variants = self.scenario()

        data = self.get_item(template.custom_slug)["data"]

        offered = {tuple(sorted(row["attributes"].items())) for row in data["variants"]}
        expected = {tuple(sorted({COLOUR: colour, SIZE: size}.items()))
                    for colour, size in variants}

        self.assertEqual(offered, expected)
        self.assertEqual({row["item_code"] for row in data["variants"]},
                         set(variants.values()))

    def test_orange_large_is_never_advertised_as_valid(self):
        """Both values are selectable; the PAIR is not, and never resolves."""

        template, _ = self.scenario()

        data = self.get_item(template.custom_slug)["data"]
        by_attribute = {row["attribute"]: row["values"] for row in data["attributes"]}

        self.assertIn(self.values["orange"], by_attribute[COLOUR])
        self.assertIn(self.values["large"], by_attribute[SIZE])

        impossible = tuple(sorted({COLOUR: self.values["orange"],
                                   SIZE: self.values["large"]}.items()))
        self.assertNotIn(impossible,
                         {tuple(sorted(row["attributes"].items()))
                          for row in data["variants"]},
                         "a combination with no variant was advertised")

        error = self.error_of(self.resolve(template.name,
                                           {COLOUR: self.values["orange"],
                                            SIZE: self.values["large"]}))
        self.assertEqual(error["code"], "variant_not_available")

    def test_resolving_orange_medium_returns_that_exact_sku(self):
        from erpnext.controllers.item_variant import find_variant

        template, variants = self.scenario()
        expected = variants[(self.values["orange"], self.values["medium"])]

        data = self.resolve(template.name,
                            {COLOUR: self.values["orange"], SIZE: self.values["medium"]})["data"]

        self.assertEqual(data["name"], expected)
        self.assertEqual(data["name"], find_variant(template.name,
                                                    {COLOUR: self.values["orange"],
                                                     SIZE: self.values["medium"]}),
                         "the endpoint disagreed with ERPNext's own resolver")

    def test_price_uom_and_stock_belong_to_the_resolved_sku(self):
        from erpnext.stock.utils import get_bin

        template, variants = self.scenario()
        orange = variants[(self.values["orange"], self.values["medium"])]
        yellow = variants[(self.values["yellow"], self.values["large"])]

        # Stock and a selling unit that exist on ONE sibling only.
        warehouse = frappe.get_single_value("Stock Settings", "default_warehouse")
        frappe.db.set_value("Bin", get_bin(orange, warehouse).name, "actual_qty", 12)
        frappe.db.set_value("Bin", get_bin(yellow, warehouse).name, "actual_qty", 99)

        variant = frappe.get_doc("Item", orange)
        variant.sales_uom = "Box"
        variant.set("uoms", [{"uom": self.stock_uom, "conversion_factor": 1},
                             {"uom": "Box", "conversion_factor": 10}])
        variant.save(ignore_permissions=True)
        frappe.clear_document_cache("Item", orange)

        data = self.resolve(template.name,
                            {COLOUR: self.values["orange"], SIZE: self.values["medium"]},
                            qty="2")["data"]

        self.assertEqual(data["name"], orange)
        self.assertEqual(flt(data["rate"]), 1000.0, "the Box rate is the variant's own")
        self.assertEqual(data["uom"], "Box")
        self.assertEqual(data["stock_uom"], self.stock_uom)
        self.assertEqual(flt(data["conversion_factor"]), 10.0)
        self.assertEqual(flt(data["stock_qty"]), 20.0)
        self.assertEqual(flt(data["actual_qty"]), 12.0, "a sibling's stock leaked in")

    def test_the_journey_ends_in_a_draft_order_for_that_sku(self):
        from yob_storefront.services.order_service import create_sales_order_from_cart

        template, variants = self.scenario()
        orange = variants[(self.values["orange"], self.values["medium"])]

        # A Pricing Rule on the exact variant, so ERPNext -- not YOB -- decides
        # the rate all the way through.
        frappe.get_doc({"doctype": "Pricing Rule", "title": "_V24D exact",
            "apply_on": "Item Code", "price_or_product_discount": "Price",
            "rate_or_discount": "Discount Percentage", "discount_percentage": 20,
            "min_qty": 1, "selling": 1, "company": self.company, "currency": "INR",
            "items": [{"item_code": orange}], "priority": 10,
            "valid_from": add_days(today(), -1)}).insert(ignore_permissions=True)
        frappe.clear_cache()

        resolved = self.resolve(template.name,
                                {COLOUR: self.values["orange"], SIZE: self.values["medium"]},
                                qty="3")["data"]

        self.assertEqual(flt(resolved["rate"]), 80.0, "the rule never reached the preview")
        self.assertTrue(resolved["pricing_rule_label"])

        self.fresh_cart()
        added = self.add_to_cart(resolved["name"], qty="3")
        self.assertNotIn("errors", added, added)

        cart = self.cart_api.get_or_create_cart(self.customer)
        cart.reload()
        row = cart.items[0]

        self.assertEqual(len(cart.items), 1)
        self.assertEqual(row.item_code, orange, "the cart holds a different SKU")
        self.assertEqual(flt(row.quantity), 3.0)
        self.assertEqual(flt(row.rate), flt(resolved["rate"]),
                         "the cart charged a rate the preview never showed")

        cart.contact_person = CONTACT
        cart.billing_address = BILLING
        cart.shipping_address = SHIPPING
        cart.save(ignore_permissions=True)

        draft = create_sales_order_from_cart(cart)
        ordered = next(r for r in draft.items if not r.get("is_free_item"))

        self.assertEqual(ordered.item_code, orange,
                         "the Draft Sales Order names a different SKU")
        self.assertEqual(flt(ordered.qty), 3.0)
        self.assertEqual(flt(ordered.rate), 80.0)
        self.assertEqual(flt(cart.grand_total, 2), flt(draft.grand_total, 2),
                         "Cart and Draft Sales Order disagree")
        self.assertEqual(flt(cart.net_total, 2), flt(draft.net_total, 2))
        self.assertEqual(cart.currency, draft.currency)

    def test_the_family_itself_can_never_be_bought(self):
        template, _ = self.scenario()
        self.fresh_cart()

        error = self.error_of(self.add_to_cart(template.name))

        self.assertEqual(error["code"], "item_is_template")
        self.assertEqual(frappe.local.response.get("http_status_code"), 422)
        self.assertFalse(frappe.db.exists("Cart Item", {"item_code": template.name}))


# =========================================================
# TASK 4 -- RE-ANCHORING NEEDS NO SERVER STATE
# =========================================================

class PartialSelectionIsStatelessCase(ContractBase):
    """Angular clears an incompatible selection; the server never knows.

    The matrix is presentation guidance. Authority is the resolver, which only
    answers a COMPLETE selection, and Add to Cart, which revalidates the SKU
    again. So a browser may re-anchor, clear and re-choose as often as it likes
    without the server holding anything that could go stale.
    """

    def test_a_partial_selection_stores_nothing(self):
        template, _ = self.scenario()

        before = {doctype: frappe.db.count(doctype)
                  for doctype in ("Cart", "Cart Item", "Item", "Sales Order")}

        for attempt in range(3):
            self.error_of(self.resolve(template.name, {COLOUR: self.values["orange"]}))
            self.error_of(self.resolve(template.name, {SIZE: self.values["large"]}))

        after = {doctype: frappe.db.count(doctype) for doctype in before}

        self.assertEqual(after, before, "a partial selection left something behind")

    def test_resolution_is_repeatable_and_order_independent(self):
        template, variants = self.scenario()
        orange = variants[(self.values["orange"], self.values["medium"])]

        first = self.resolve(template.name,
                             {COLOUR: self.values["orange"], SIZE: self.values["medium"]})
        # Re-anchored: the buyer changed Size first, then Colour. Same answer.
        second = self.resolve(template.name,
                              {SIZE: self.values["medium"], COLOUR: self.values["orange"]})

        self.assertEqual(first["data"]["name"], orange)
        self.assertEqual(second["data"]["name"], orange)
        self.assertEqual(first["data"], second["data"])

    def test_the_resolver_writes_nothing(self):
        """A read endpoint: no insert, no save, no delete anywhere in its path."""

        import ast

        from yob_storefront.services import variant_service

        for module in (variant_service,):
            tree = ast.parse(inspect.getsource(module))
            calls = {node.func.attr for node in ast.walk(tree)
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}

            for forbidden in ("insert", "save", "delete", "submit", "set_value"):
                self.assertNotIn(forbidden, calls,
                                 f"{module.__name__} mutates data on a read path")

    def test_add_to_cart_revalidates_the_resolved_sku(self):
        """The second gate: a SKU that stopped being salable after resolution."""

        template, variants = self.scenario()
        orange = variants[(self.values["orange"], self.values["medium"])]

        resolved = self.resolve(template.name,
                                {COLOUR: self.values["orange"], SIZE: self.values["medium"]})["data"]
        self.assertEqual(resolved["name"], orange)

        self.fresh_cart()
        frappe.db.set_value("Item", orange, "disabled", 1)
        frappe.clear_document_cache("Item", orange)

        error = self.error_of(self.add_to_cart(orange))

        self.assertEqual(error["code"], "item_not_purchasable")
        self.assertFalse(frappe.db.exists("Cart Item", {"item_code": orange}))


if __name__ == "__main__":
    unittest.main()
