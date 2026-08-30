# Copyright (c) 2026, YOB and Shayona
"""Variant families: the server-authoritative contract (Phase 24A audit, 24B build).

WHAT A BUYER CONTROLS
---------------------
Attributes and quantity. Never a UOM, warehouse, conversion factor, price list,
rate or SKU string. Once attributes resolve to an actual variant SKU, every Phase
23 guarantee applies unchanged -- the same SellingContext, the same temporary
Sales Order, the same Cart and Draft Sales Order paths.

THE SHAPE OF THE CONTRACT
-------------------------
```text
family slug -> catalog.get_item        -> attributes[] + variants[]  (NO price)
buyer picks -> catalog.resolve_variant -> find_variant -> revalidate -> full detail
            -> cart.add_to_cart(item_code, qty)  -> revalidated again
```

THE TRAP IT AVOIDS
------------------
Attribute VALUES are global: `Colour` holds every colour any product ever used,
so a cross-product invents combinations nobody can buy. Red/M and Blue/L existing
does not make Red/L real. `variants[]` is built from the ACTUAL variant records,
and `find_variant` answers None for anything else.
"""

import inspect
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


def _seeded():
    return bool(frappe.db.exists("Customer", CUSTOMER) and frappe.db.exists("Item", SEED_ITEM))


def _executable_source(module) -> str:
    """Module source with docstrings and comments removed.

    A "YOB must not do X" scan has to read what the code DOES. These modules
    discuss `make_variant_item_code` at length precisely to say they never call
    it, and a raw text scan would force that explanation out of the file.
    """

    import ast

    tree = ast.parse(inspect.getsource(module))

    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body.pop(0)

    return ast.unparse(tree)


class VariantBase(unittest.TestCase):
    """One template, two attributes, and a deliberately MISSING combination."""

    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data on the test site")

    def setUp(self):
        frappe.set_user("Administrator")
        from yob_storefront.utils.store import get_store_settings

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
        self.assertEqual(self.commits, [], "a commit escaped the test rollback")

    # ------------------------------------------------------------- fixtures

    def attribute_value(self, attribute, wanted):
        """A real Item Attribute Value, so ERPNext's own validation passes."""

        values = frappe.get_all("Item Attribute Value", filters={"parent": attribute},
                                pluck="attribute_value")
        if wanted in values:
            return wanted
        if not values:
            raise unittest.SkipTest(f"{attribute} has no values on this bench")
        return values[0]

    def make_template(self, code="_V24-TSHIRT", **kw):
        doc = {"doctype": "Item", "item_code": code, "item_name": code,
               "item_group": self.item_group, "stock_uom": self.stock_uom,
               "is_stock_item": 1, "is_sales_item": 1, "gst_hsn_code": self.hsn,
               "custom_slug": code.lower(), "custom_category": self.category,
               "has_variants": 1,
               "attributes": [{"attribute": COLOUR}, {"attribute": SIZE}]}
        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True)

    def make_variant(self, template, colour, size, price=100):
        from erpnext.controllers.item_variant import create_variant

        variant = create_variant(template.name, {COLOUR: colour, SIZE: size})
        variant.insert(ignore_permissions=True)

        if price is not None:
            self.make_price(variant.name, price)
        return variant

    def make_price(self, item, rate, uom=None):
        return frappe.get_doc({
            "doctype": "Item Price", "item_code": item, "price_list": self.price_list,
            "price_list_rate": rate, "selling": 1,
            "uom": uom or self.stock_uom}).insert(ignore_permissions=True).name

    def family(self, code="_V24-TSHIRT"):
        """Template + Red/Medium + Blue/Large. Red/Large deliberately absent."""

        red = self.attribute_value(COLOUR, "Red")
        blue = self.attribute_value(COLOUR, "Blue")
        medium = self.attribute_value(SIZE, "Medium")
        large = self.attribute_value(SIZE, "Large")

        if red == blue or medium == large:
            raise unittest.SkipTest("this bench lacks two distinct values per attribute")

        template = self.make_template(code)
        variants = {
            (red, medium): self.make_variant(template, red, medium).name,
            (blue, large): self.make_variant(template, blue, large).name,
        }
        self.values = {"red": red, "blue": blue, "medium": medium, "large": large}
        return template, variants

    def price_rule(self, title, item_codes=None, item_groups=None, discount=25,
                   priority=None, **kw):
        doc = {"doctype": "Pricing Rule", "title": title,
               "apply_on": "Item Group" if item_groups else "Item Code",
               "price_or_product_discount": "Price",
               "rate_or_discount": "Discount Percentage", "discount_percentage": discount,
               "min_qty": 1, "selling": 1, "company": self.company, "currency": "INR",
               "valid_from": add_days(today(), -1)}
        if item_groups:
            doc["item_groups"] = [{"item_group": g} for g in item_groups]
        else:
            doc["items"] = [{"item_code": c} for c in item_codes]
        if priority:
            doc["priority"] = priority
        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True).name

    # ------------------------------------------------------------- the paths

    def preview(self, item, qty=1):
        from yob_storefront.services.pricing_service import get_item_pricing

        frappe.clear_cache()
        return get_item_pricing(customer=frappe.get_doc("Customer", CUSTOMER),
                                item_code=item, qty=qty, company=self.company,
                                currency=self.currency)


# =========================================================
# 1. THE ERPNEXT VARIANT MODEL
# =========================================================

class VariantModelCase(VariantBase):
    """Facts about ERPNext, not about YOB. The 24B design rests on every one."""

    def test_find_variant_resolves_an_exact_attribute_set(self):
        """attributes -> SKU, decided by ERPNext. No naming algorithm anywhere."""

        from erpnext.controllers.item_variant import find_variant

        template, variants = self.family()
        red, medium = self.values["red"], self.values["medium"]

        self.assertEqual(find_variant(template.name, {COLOUR: red, SIZE: medium}),
                         variants[(red, medium)])

    def test_a_combination_that_was_never_generated_resolves_to_nothing(self):
        """THE rule the storefront must obey: Red/Large exists only as a guess."""

        from erpnext.controllers.item_variant import find_variant

        template, _ = self.family()

        self.assertIsNone(
            find_variant(template.name, {COLOUR: self.values["red"], SIZE: self.values["large"]}),
            "ERPNext resolved a combination that has no variant record")

    def test_a_partial_attribute_set_resolves_to_nothing(self):
        """Every attribute must be chosen; there is no 'first match' fallback."""

        from erpnext.controllers.item_variant import find_variant

        template, _ = self.family()

        self.assertIsNone(find_variant(template.name, {COLOUR: self.values["red"]}))

    def test_valid_combinations_are_the_variant_rows_not_the_value_lists(self):
        """Where a server-authoritative matrix must come from.

        Attribute VALUES are global -- `Colour` holds every colour any product
        ever used. The only truthful source of "what can be bought" is the
        `Item Variant Attribute` rows of the variants that actually exist.
        """

        template, variants = self.family()

        rows = frappe.get_all("Item Variant Attribute",
                              filters={"parenttype": "Item",
                                       "parent": ["in", list(variants.values())]},
                              fields=["parent", "attribute", "attribute_value"])

        combinations = {}
        for row in rows:
            combinations.setdefault(row.parent, {})[row.attribute] = row.attribute_value

        self.assertEqual(
            sorted(tuple(sorted(c.items())) for c in combinations.values()),
            sorted(tuple(sorted({COLOUR: colour, SIZE: size}.items()))
                   for colour, size in variants),
            "the variant rows do not describe the generated combinations")

        global_colours = frappe.get_all("Item Attribute Value", filters={"parent": COLOUR},
                                        pluck="attribute_value")
        global_sizes = frappe.get_all("Item Attribute Value", filters={"parent": SIZE},
                                      pluck="attribute_value")

        self.assertGreater(len(global_colours) * len(global_sizes), len(variants),
                           "fixture is too small to show the cross-product trap")

    def test_the_template_declares_which_attributes_are_selectable(self):
        template, _ = self.family()

        rows = frappe.get_all("Item Variant Attribute", filters={"parent": template.name},
                              fields=["attribute", "numeric_values", "from_range",
                                      "to_range", "increment"], order_by="idx")

        self.assertEqual([r.attribute for r in rows], [COLOUR, SIZE],
                         "attribute ORDER comes from the template and must be preserved")
        # Numeric attributes are a range, not a value list -- a selector cannot
        # assume every attribute is an enumeration.
        self.assertIn("numeric_values", rows[0])

    def test_variant_based_on_records_how_the_family_is_built(self):
        template, variants = self.family()

        self.assertEqual(template.variant_based_on, "Item Attribute")
        self.assertEqual(
            frappe.db.get_value("Item", list(variants.values())[0], "variant_based_on"),
            "Item Attribute",
            "a Manufacturer-based family would need a different selector entirely")

    def test_a_template_cannot_carry_an_item_price(self):
        """So there is no such thing as a family price to show before selection."""

        template, _ = self.family()

        with self.assertRaises(frappe.ValidationError):
            self.make_price(template.name, 500)

    def test_erpnext_refuses_to_price_a_template(self):
        template, _ = self.family()

        with self.assertRaises(frappe.ValidationError) as caught:
            self.preview(template.name)

        self.assertIn("template", str(caught.exception).lower())

    def test_a_variant_keeps_the_templates_stock_uom(self):
        """`Item Variant Settings.allow_different_uom` governs this, and is off."""

        template, variants = self.family()
        variant = frappe.get_doc("Item", list(variants.values())[0])
        variant.stock_uom = "Box"

        if frappe.get_single_value("Item Variant Settings", "allow_different_uom"):
            self.skipTest("this bench allows variant-specific stock UOM")

        with self.assertRaises(frappe.ValidationError):
            variant.save(ignore_permissions=True)

    def test_a_variant_may_have_its_own_selling_uom(self):
        """Selling UOM is per SKU even though stock UOM is not."""

        template, variants = self.family()
        code = variants[(self.values["blue"], self.values["large"])]

        variant = frappe.get_doc("Item", code)
        variant.sales_uom = "Box"
        variant.set("uoms", [{"uom": self.stock_uom, "conversion_factor": 1},
                             {"uom": "Box", "conversion_factor": 10}])
        variant.save(ignore_permissions=True)
        frappe.clear_document_cache("Item", code)

        pricing = self.preview(code)

        self.assertEqual(pricing["uom"], "Box")
        self.assertEqual(flt(pricing["conversion_factor"]), 10.0)
        self.assertEqual(flt(pricing["rate"]), 1000.0, "100/Nos should convert to 1000/Box")

    def test_each_variant_carries_its_own_price(self):
        template, variants = self.family()
        blue = variants[(self.values["blue"], self.values["large"])]
        frappe.db.set_value("Item Price", {"item_code": blue}, "price_list_rate", 650)
        frappe.clear_cache()

        self.assertEqual(flt(self.preview(blue)["rate"]), 650.0)
        self.assertEqual(
            flt(self.preview(variants[(self.values["red"], self.values["medium"])])["rate"]),
            100.0, "a sibling's price leaked across the family")

    def test_stock_belongs_to_the_variant_not_the_template(self):
        from erpnext.stock.utils import get_bin
        from yob_storefront.api.catalog import resolve_stock_availability

        template, variants = self.family()
        red = variants[(self.values["red"], self.values["medium"])]
        warehouse = frappe.get_single_value("Stock Settings", "default_warehouse")
        frappe.db.set_value("Bin", get_bin(red, warehouse).name, "actual_qty", 7)

        self.assertEqual(resolve_stock_availability(self.customer, red)["actual_qty"], 7.0)
        self.assertIsNone(
            resolve_stock_availability(self.customer, template.name)["actual_qty"],
            "a template is not transactable, so its quantity is unknown, not zero")


# =========================================================
# 2. PRICING RULES ACROSS A FAMILY
# =========================================================

class VariantPricingRuleCase(VariantBase):
    """Merchants price a family in four different ways. All must keep working."""

    def test_a_rule_on_the_exact_variant_applies_only_there(self):
        template, variants = self.family()
        red = variants[(self.values["red"], self.values["medium"])]
        blue = variants[(self.values["blue"], self.values["large"])]

        self.price_rule("_V24 exact", item_codes=[red], discount=25, priority=10)

        self.assertEqual(flt(self.preview(red)["rate"]), 75.0)
        self.assertEqual(flt(self.preview(blue)["rate"]), 100.0)

    def test_a_rule_on_the_template_reaches_every_variant(self):
        """ERPNext's own rule matching includes `variant_of`.

        This is how a merchant discounts a whole family, and it means a
        storefront must never assume a variant's rules are keyed to its own code.
        """

        template, variants = self.family()
        blue = variants[(self.values["blue"], self.values["large"])]

        self.price_rule("_V24 template", item_codes=[template.name], discount=40, priority=9)
        frappe.clear_cache()

        pricing = self.preview(blue)

        self.assertEqual(flt(pricing["rate"]), 60.0,
                         "a template-level rule did not reach its variant")
        self.assertTrue(pricing["pricing_rules"])

    def test_an_item_group_rule_reaches_variants(self):
        template, variants = self.family()
        blue = variants[(self.values["blue"], self.values["large"])]

        self.price_rule("_V24 group", item_groups=[self.item_group], discount=10, priority=8)
        frappe.clear_cache()

        self.assertEqual(flt(self.preview(blue)["rate"]), 90.0)

    def test_a_promotion_can_grant_a_variant(self):
        from erpnext.accounts.doctype.pricing_rule.utils import apply_pricing_rule_on_transaction

        template, variants = self.family()
        red = variants[(self.values["red"], self.values["medium"])]
        blue = variants[(self.values["blue"], self.values["large"])]

        frappe.get_doc({"doctype": "Pricing Rule", "title": "_V24 free variant",
            "apply_on": "Item Code", "price_or_product_discount": "Product",
            "min_qty": 2, "free_item": red, "free_qty": 1, "selling": 1,
            "company": self.company, "currency": "INR",
            "items": [{"item_code": blue}],
            "valid_from": add_days(today(), -1)}).insert(ignore_permissions=True)
        frappe.clear_cache()

        so = frappe.new_doc("Sales Order")
        so.customer = CUSTOMER
        so.company = self.company
        so.currency = self.currency
        so.selling_price_list = self.price_list
        so.transaction_date = today()
        so.append("items", {"item_code": blue, "qty": 2})
        so.flags.ignore_permissions = True
        so.set_missing_values()
        so.calculate_taxes_and_totals()
        apply_pricing_rule_on_transaction(so)
        so.calculate_taxes_and_totals()

        free = [r for r in so.items if r.get("is_free_item")]

        self.assertEqual(len(free), 1, "the promotion did not grant the variant")
        self.assertEqual(free[0].item_code, red, "a template or sibling was granted instead")
        self.assertEqual(flt(free[0].rate), 0.0)


# =========================================================
# 3. WHAT THE STOREFRONT DOES TODAY
# =========================================================

class CurrentStorefrontBehaviourCase(VariantBase):
    """The storefront has no variant concept: it sells variants as flat products."""

    def listing(self):
        from yob_storefront.api import catalog

        slug = frappe.db.get_value("Category", self.category, "slug")
        if not slug:
            self.skipTest("the seeded item has no storefront category")

        frappe.clear_cache()
        with patch.object(catalog, "get_storefront_customer", return_value=self.customer):
            response = inspect.unwrap(catalog.get_items)(
                auth_context={}, scope_type="category", scope_value=slug, page_size=24)
        self.assertNotIn("errors", response, response)
        return response["data"]["items"]

    def detail(self, slug):
        from yob_storefront.api import catalog

        frappe.clear_cache()
        with patch.object(catalog, "get_storefront_customer", return_value=self.customer):
            return inspect.unwrap(catalog.get_item)(auth_context={}, slug=slug, qty=1)

    def test_a_family_is_listed_once_and_its_variants_are_not(self):
        """One card per family (Decision 2), never one per Colour/Size pair."""

        template, variants = self.family()
        rows = {row["name"]: row for row in self.listing()}

        self.assertIn(template.name, rows, "the family is missing from the catalogue")

        for code in variants.values():
            self.assertNotIn(code, rows, "an individual variant was listed as a product")

    def test_a_family_card_carries_options_not_an_invented_price(self):
        """ERPNext refuses an Item Price on a template, so no rate is quoted.

        Naming a "representative" variant's rate would quote a number no buyer is
        charged, and pricing every variant would restore the unbounded per-item
        loop Phase 22B removed. The card states its condition instead.
        """

        template, _ = self.family()
        card = {row["name"]: row for row in self.listing()}[template.name]

        self.assertEqual(card["has_variants"], 1)
        self.assertEqual(card["price_state"], "select_options")
        self.assertEqual(card["slug"], template.custom_slug)

        for money in ("base_price", "rate", "net_amount", "total_amount",
                      "discount_percentage", "tax_amount"):
            self.assertIsNone(card[money], f"a family card quoted `{money}`")

    def test_a_simple_item_card_is_unchanged_and_declares_its_state(self):
        card = {row["name"]: row for row in self.listing()}.get(SEED_ITEM)

        self.assertIsNotNone(card, "the seeded simple item left the catalogue")
        self.assertEqual(card["has_variants"], 0)
        self.assertEqual(card["price_state"], "priced")
        self.assertTrue(card["rate"])

    def test_a_family_with_no_salable_variant_is_not_listed(self):
        template = self.make_template("_V24-EMPTY")

        self.assertNotIn(template.name, {row["name"] for row in self.listing()},
                         "a family that can sell nothing was offered")

    def test_a_variant_carries_no_public_slug(self):
        """REGRESSION (24A found it, 24B fixed it).

        `custom_slug` was `reqd` AND listed in `Item Variant Settings`, so
        `copy_attributes_to_variant` copied it: three Items shared one slug and
        `get_item(slug)` answered with whichever the database offered first.
        A patch removed it from that list and dropped `reqd`; variants are reached
        through their family page instead.
        """

        template, variants = self.family()

        for code in variants.values():
            self.assertFalse(frappe.db.get_value("Item", code, "custom_slug"),
                             "a variant claimed a public URL of its own")

        self.assertEqual(frappe.db.count("Item", {"custom_slug": template.custom_slug}), 1,
                         "more than one Item answers to this slug")

    def test_a_duplicate_public_slug_is_refused(self):
        """Uniqueness is guarded by a hook: unslugged Items share the empty
        string, so a database unique index cannot express this."""

        template, _ = self.family()

        with self.assertRaises(frappe.exceptions.DuplicateEntryError):
            self.make_template("_V24-CLONE", custom_slug=template.custom_slug)

    def test_the_family_page_is_addressable_and_describes_itself(self):
        """REGRESSION (24A gap 2). The public URL belongs to the FAMILY."""

        template, variants = self.family()

        data = self.detail(template.custom_slug)["data"]

        self.assertEqual(data["name"], template.name)
        self.assertEqual(data["is_template"], 1)
        self.assertEqual(data["is_purchasable"], 0,
                         "a family must never present itself as buyable")
        self.assertIn("attributes", data)
        self.assertIn("variants", data)

        for money in ("rate", "base_price", "total_amount"):
            self.assertNotIn(money, data, "a family page quoted a price")

    def test_the_matrix_offers_only_real_combinations(self):
        template, variants = self.family()

        data = self.detail(template.custom_slug)["data"]

        offered = sorted(tuple(sorted(row["attributes"].items())) for row in data["variants"])
        actual = sorted(tuple(sorted({COLOUR: colour, SIZE: size}.items()))
                        for colour, size in variants)

        self.assertEqual(offered, actual)
        self.assertEqual({row["item_code"] for row in data["variants"]},
                         set(variants.values()))

        # The pair a cross-product would invent.
        self.assertNotIn(tuple(sorted({COLOUR: self.values["red"],
                                       SIZE: self.values["large"]}.items())), offered,
                         "a combination with no variant was offered as valid")

    def test_attribute_values_are_restricted_to_what_exists(self):
        template, _ = self.family()

        data = self.detail(template.custom_slug)["data"]
        by_attribute = {row["attribute"]: row for row in data["attributes"]}

        self.assertEqual([row["attribute"] for row in data["attributes"]], [COLOUR, SIZE],
                         "attribute order must follow the template")

        self.assertEqual(set(by_attribute[COLOUR]["values"]),
                         {self.values["red"], self.values["blue"]})
        self.assertEqual(set(by_attribute[SIZE]["values"]),
                         {self.values["medium"], self.values["large"]})

        every_colour = frappe.get_all("Item Attribute Value", filters={"parent": COLOUR},
                                      pluck="attribute_value")
        self.assertLess(len(by_attribute[COLOUR]["values"]), len(every_colour),
                        "the global attribute list leaked into the selector")

    def test_attribute_values_keep_the_merchant_ordering(self):
        """`Item Attribute Value` is an ordered table; that order is the answer."""

        template, _ = self.family()
        data = self.detail(template.custom_slug)["data"]
        sizes = {row["attribute"]: row["values"] for row in data["attributes"]}[SIZE]

        catalogue = frappe.get_all("Item Attribute Value", filters={"parent": SIZE},
                                   order_by="idx asc", pluck="attribute_value")
        expected = [value for value in catalogue if value in sizes]

        self.assertEqual(sizes, expected)

    def test_a_non_salable_variant_never_appears_in_the_matrix(self):
        template, variants = self.family()
        blue = variants[(self.values["blue"], self.values["large"])]
        frappe.db.set_value("Item", blue, "disabled", 1)
        frappe.clear_document_cache("Item", blue)
        frappe.clear_cache()

        data = self.detail(template.custom_slug)["data"]

        self.assertNotIn(blue, {row["item_code"] for row in data["variants"]})
        self.assertNotIn(self.values["blue"],
                         {row["attribute"]: row["values"]
                          for row in data["attributes"]}[COLOUR],
                         "a disabled variant's value stayed selectable")

    def test_a_manufacturer_family_fails_closed(self):
        """No attribute selector exists for it, so YOB refuses rather than invents."""

        template = self.make_template("_V24-MFG", variant_based_on="Manufacturer",
                                      attributes=[])

        response = self.detail(template.custom_slug)

        self.assertEqual(response["errors"][0]["code"], "variant_family_unsupported")
        self.assertNotIn(template.name, {row["name"] for row in self.listing()},
                         "an unsupported family was still offered in the catalogue")

    def test_a_variant_prices_through_the_ordinary_phase_23_path(self):
        """Once the SKU is known, nothing about variants is special."""

        _, variants = self.family()
        red = variants[(self.values["red"], self.values["medium"])]

        pricing = self.preview(red)

        self.assertEqual(flt(pricing["rate"]), 100.0)
        self.assertEqual(pricing["uom"], self.stock_uom)
        self.assertEqual(flt(pricing["conversion_factor"]), 1.0)
        self.assertEqual(pricing["item"]["name"], red)


# =========================================================
# 4. A TEMPLATE MUST NEVER BECOME A TRANSACTION
# =========================================================

class TemplateIsNotSalableCase(VariantBase):

    def fresh_cart(self):
        """An empty, JUST-SAVED cart.

        The save matters: `validate_cart_expiry` compares `Cart.modified` against
        `YOB Store Settings.cart_expiry`, and the seeded cart on this bench is days
        old. Without this the expiry path fires mid-test and hides what is being
        measured (see `test_adding_to_an_expired_cart_reports_honestly`).
        """

        from yob_storefront.api.cart import get_or_create_cart

        cart = get_or_create_cart(self.customer)
        cart.set("items", [])
        cart.coupon_code = None
        cart.save(ignore_permissions=True)
        return cart

    def add_to_cart(self, item_code, qty=1):
        from yob_storefront.api import cart as cart_api

        frappe.clear_cache()
        with patch.object(cart_api, "get_storefront_customer", return_value=self.customer):
            return inspect.unwrap(cart_api.add_to_cart)(
                auth_context={}, item_code=item_code, qty=qty)

    def open_cart(self):
        from yob_storefront.api.cart import get_or_create_cart

        cart = get_or_create_cart(self.customer)
        cart.reload()
        return cart

    def test_a_template_never_becomes_cart_intent(self):
        """The safety property, and it holds today: the add fails closed.

        The assertion is "no Cart Item names this template", not a before/after
        comparison: the 500 path runs production `server_error()`, which calls
        `frappe.db.rollback()` and takes this test's own uncommitted state with it.
        That rollback is exactly why nothing is left behind, so it is asserted at
        the database rather than against an in-memory snapshot.
        """

        template, _ = self.family()
        self.fresh_cart()

        response = self.add_to_cart(template.name)

        self.assertIn("errors", response, "a template was added to the cart")
        self.assertFalse(frappe.db.exists("Cart Item", {"item_code": template.name}),
                         "a template survived as buyer intent")
        self.assertFalse(
            [row for row in self.open_cart().items if row.item_code == template.name])

    def test_a_template_add_answers_a_client_fixable_code(self):
        """REGRESSION (24A gap 4). It used to answer `internal_server_error`.

        ERPNext raises a perfectly clear ValidationError -- "Item X is a template,
        please select one of its variants" -- but YOB had no guard of its own, so
        the boundary treated it as an unexpected fault: a 500 envelope, a logged
        traceback and a full rollback for what is really a bad request.
        """

        template, _ = self.family()
        self.fresh_cart()

        error = self.add_to_cart(template.name)["errors"][0]

        self.assertEqual(error["code"], "item_is_template")
        self.assertEqual(error["field"], "item_code")
        self.assertEqual(frappe.local.response.get("http_status_code"), 422)

    def test_the_salability_guard_rejects_a_template(self):
        """REGRESSION (24A gap 3). `has_variants` is now part of the gate."""

        from yob_storefront.services.pricing_service import validate_item_saleable

        template, _ = self.family()

        with self.assertRaises(frappe.ValidationError):
            validate_item_saleable(template.name)

    def test_a_disabled_sku_is_refused_at_the_boundary(self):
        """The gate answers before anything is read or written."""

        _, variants = self.family()
        red = variants[(self.values["red"], self.values["medium"])]
        self.fresh_cart()

        frappe.db.set_value("Item", red, "disabled", 1)
        frappe.clear_document_cache("Item", red)
        frappe.clear_cache()

        error = self.add_to_cart(red)["errors"][0]

        self.assertEqual(error["code"], "item_not_purchasable")
        self.assertFalse(frappe.db.exists("Cart Item", {"item_code": red}))

    def test_an_orphaned_variant_is_refused(self):
        """`variant_of` must still point at a real family."""

        from yob_storefront.services.variant_service import is_salable_sku

        _, variants = self.family()
        red = variants[(self.values["red"], self.values["medium"])]

        self.assertTrue(is_salable_sku(red))

        frappe.db.set_value("Item", red, "variant_of", "_V24-DOES-NOT-EXIST")
        frappe.clear_document_cache("Item", red)

        self.assertFalse(is_salable_sku(red))

    def test_adding_to_an_expired_cart_reports_honestly(self):
        """REGRESSION (24A gap 5), unrelated to variants but found alongside them.

        `add_to_cart` appended the row and THEN called `validate_cart_expiry`,
        which emptied the expired cart -- including the row just added -- saved it,
        and let the endpoint answer "Item added" over an empty cart. The expiry
        check now runs BEFORE the append, so the response and the stored state
        cannot disagree.
        """

        from yob_storefront.utils.store import get_store_settings

        if not get_store_settings().get("cart_expiry"):
            self.skipTest("cart expiry is not configured on this bench")

        _, variants = self.family()
        item = variants[(self.values["red"], self.values["medium"])]

        cart = self.fresh_cart()
        expired = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-365)
        frappe.db.set_value("Cart", cart.name, "modified", expired, update_modified=False)
        frappe.clear_document_cache("Cart", cart.name)

        response = self.add_to_cart(item, qty=2)

        self.assertNotIn("errors", response, response)

        rows = response["data"]["items"]
        self.assertTrue(rows, "the endpoint reported success over an empty cart")
        self.assertEqual(rows[0]["item_code"], item)
        self.assertEqual(float(rows[0]["quantity"]), 2.0)

        stored = self.open_cart()
        self.assertEqual([(row.item_code, float(row.quantity)) for row in stored.items],
                         [(item, 2.0)], "the response and the stored cart disagree")

    def test_a_disabled_variant_is_refused(self):
        from yob_storefront.services.pricing_service import validate_item_saleable

        _, variants = self.family()
        red = variants[(self.values["red"], self.values["medium"])]
        frappe.db.set_value("Item", red, "disabled", 1)
        frappe.clear_document_cache("Item", red)

        with self.assertRaises(frappe.ValidationError):
            validate_item_saleable(red)

    def test_a_non_sales_variant_is_refused(self):
        from yob_storefront.services.pricing_service import validate_item_saleable

        _, variants = self.family()
        red = variants[(self.values["red"], self.values["medium"])]
        frappe.db.set_value("Item", red, "is_sales_item", 0)
        frappe.clear_document_cache("Item", red)

        with self.assertRaises(frappe.ValidationError):
            validate_item_saleable(red)


# =========================================================
# 4b. RESOLUTION: ATTRIBUTES -> ACTUAL SKU
# =========================================================

class ResolveVariantCase(VariantBase):
    """`resolve_variant` is the only way a selection becomes a SKU."""

    def resolve(self, template, attributes, qty=1):
        import json

        from yob_storefront.api import catalog

        frappe.clear_cache()
        with patch.object(catalog, "get_storefront_customer", return_value=self.customer):
            return inspect.unwrap(catalog.resolve_variant)(
                auth_context={}, template=template,
                attributes=json.dumps(attributes), qty=qty)

    def test_a_complete_selection_resolves_to_the_actual_variant(self):
        from erpnext.controllers.item_variant import find_variant

        template, variants = self.family()
        red = variants[(self.values["red"], self.values["medium"])]

        response = self.resolve(template.name,
                                {COLOUR: self.values["red"], SIZE: self.values["medium"]},
                                qty=2)

        self.assertNotIn("errors", response, response)
        data = response["data"]

        self.assertEqual(data["name"], red)
        self.assertEqual(data["name"],
                         find_variant(template.name, {COLOUR: self.values["red"],
                                                      SIZE: self.values["medium"]}),
                         "the endpoint disagreed with ERPNext's own resolver")
        self.assertEqual(data["variant_of"], template.name)
        self.assertEqual(data["is_purchasable"], 1)
        self.assertEqual(data["is_template"], 0)

    def test_the_resolved_payload_carries_everything_a_page_needs(self):
        """One serializer for simple items and resolved variants alike."""

        template, variants = self.family()
        data = self.resolve(template.name,
                            {COLOUR: self.values["red"], SIZE: self.values["medium"]},
                            qty=2)["data"]

        for field in ("name", "item_name", "image", "item_group", "qty", "selected",
                      "base_price", "rate", "discount_percentage", "net_amount",
                      "tax_amount", "total_amount", "uom", "stock_uom",
                      "conversion_factor", "stock_qty", "is_stock_item", "warehouse",
                      "actual_qty", "available_rules"):
            self.assertIn(field, data, f"the resolved payload lacks `{field}`")

        self.assertEqual(flt(data["rate"]), 100.0)
        self.assertEqual(flt(data["net_amount"]), 200.0)
        self.assertEqual(data["uom"], self.stock_uom)
        self.assertEqual(flt(data["conversion_factor"]), 1.0)
        self.assertEqual(flt(data["stock_qty"]), 2.0)

    def test_the_selection_echoed_back_is_erpnexts_not_the_request(self):
        """`attributes` is a selection, never authority."""

        template, variants = self.family()
        red = variants[(self.values["red"], self.values["medium"])]

        data = self.resolve(template.name,
                            {COLOUR: self.values["red"], SIZE: self.values["medium"]})["data"]

        stored = frappe.get_all("Item Variant Attribute", filters={"parent": red},
                                fields=["attribute", "attribute_value"], order_by="idx")

        self.assertEqual(data["selected"],
                         {row.attribute: row.attribute_value for row in stored})

    def test_a_partial_selection_does_not_resolve(self):
        template, _ = self.family()

        response = self.resolve(template.name, {COLOUR: self.values["red"]})

        self.assertEqual(response["errors"][0]["code"], "variant_attributes_required")
        self.assertEqual(frappe.local.response.get("http_status_code"), 422)

    def test_a_combination_that_does_not_exist_is_refused(self):
        template, _ = self.family()

        response = self.resolve(template.name,
                                {COLOUR: self.values["red"], SIZE: self.values["large"]})

        self.assertEqual(response["errors"][0]["code"], "variant_not_available")
        self.assertEqual(response["errors"][0]["field"], "attributes")

    def test_a_value_that_is_not_an_attribute_value_at_all_is_refused(self):
        template, _ = self.family()

        response = self.resolve(template.name,
                                {COLOUR: "Ultraviolet", SIZE: self.values["medium"]})

        self.assertEqual(response["errors"][0]["code"], "variant_not_available")

    def test_a_disabled_variant_cannot_be_resolved(self):
        template, variants = self.family()
        red = variants[(self.values["red"], self.values["medium"])]
        frappe.db.set_value("Item", red, "disabled", 1)
        frappe.clear_document_cache("Item", red)

        response = self.resolve(template.name,
                                {COLOUR: self.values["red"], SIZE: self.values["medium"]})

        self.assertEqual(response["errors"][0]["code"], "variant_not_available")

    def test_a_non_sales_variant_cannot_be_resolved(self):
        template, variants = self.family()
        red = variants[(self.values["red"], self.values["medium"])]
        frappe.db.set_value("Item", red, "is_sales_item", 0)
        frappe.clear_document_cache("Item", red)

        response = self.resolve(template.name,
                                {COLOUR: self.values["red"], SIZE: self.values["medium"]})

        self.assertEqual(response["errors"][0]["code"], "variant_not_available")

    def test_a_simple_item_is_not_a_family(self):
        response = self.resolve(SEED_ITEM, {COLOUR: self.attribute_value(COLOUR, "Red")})

        self.assertIn(response["errors"][0]["code"],
                      ("variant_not_available", "variant_attributes_required"))

    def test_a_manufacturer_family_cannot_be_resolved(self):
        template = self.make_template("_V24-MFG-RESOLVE", variant_based_on="Manufacturer",
                                      attributes=[])

        response = self.resolve(template.name,
                                {COLOUR: self.attribute_value(COLOUR, "Red")})

        self.assertEqual(response["errors"][0]["code"], "variant_not_available")

    def test_a_resolved_variant_with_its_own_selling_uom(self):
        """Phase 23B-5U's contract, reached through variant resolution."""

        template, variants = self.family()
        blue = variants[(self.values["blue"], self.values["large"])]

        variant = frappe.get_doc("Item", blue)
        variant.sales_uom = "Box"
        variant.set("uoms", [{"uom": self.stock_uom, "conversion_factor": 1},
                             {"uom": "Box", "conversion_factor": 10}])
        variant.save(ignore_permissions=True)
        frappe.clear_document_cache("Item", blue)

        data = self.resolve(template.name,
                            {COLOUR: self.values["blue"], SIZE: self.values["large"]},
                            qty=2)["data"]

        self.assertEqual(data["uom"], "Box")
        self.assertEqual(data["stock_uom"], self.stock_uom)
        self.assertEqual(flt(data["conversion_factor"]), 10.0)
        self.assertEqual(flt(data["stock_qty"]), 20.0)
        self.assertEqual(flt(data["rate"]), 1000.0)

    def test_resolution_to_cart_to_draft_order_keeps_parity(self):
        """The whole point: after resolution it is an ordinary Phase 23 order."""

        from yob_storefront.api import cart as cart_api
        from yob_storefront.services.order_service import create_sales_order_from_cart

        template, variants = self.family()
        red = variants[(self.values["red"], self.values["medium"])]

        resolved = self.resolve(template.name,
                                {COLOUR: self.values["red"], SIZE: self.values["medium"]},
                                qty=3)["data"]

        cart = cart_api.get_or_create_cart(self.customer)
        cart.set("items", [])
        cart.coupon_code = None
        cart.save(ignore_permissions=True)

        with patch.object(cart_api, "get_storefront_customer", return_value=self.customer):
            added = inspect.unwrap(cart_api.add_to_cart)(
                auth_context={}, item_code=resolved["name"], qty=3)
        self.assertNotIn("errors", added, added)

        cart = cart_api.get_or_create_cart(self.customer)
        cart.reload()
        row = cart.items[0]

        self.assertEqual(row.item_code, red)
        self.assertEqual(flt(row.rate), flt(resolved["rate"]),
                         "the cart charged a different rate from the resolved page")

        cart.contact_person = CONTACT
        cart.billing_address = BILLING
        cart.shipping_address = SHIPPING
        cart.save(ignore_permissions=True)

        draft = create_sales_order_from_cart(cart)
        ordered = next(r for r in draft.items if r.item_code == red)

        self.assertEqual(flt(cart.grand_total, 2), flt(draft.grand_total, 2))
        self.assertEqual(ordered.uom, row.uom)
        self.assertEqual(flt(ordered.qty), 3.0)
        self.assertEqual(flt(ordered.stock_qty), 3.0 * flt(row.conversion_factor))


# =========================================================
# 5. WHAT THE BROWSER MAY SEND -- THE BASELINE FOR 24B
# =========================================================

class NoClientVariantAuthorityCase(VariantBase):
    """Whatever 24B adds, none of this may become client input."""

    #: Never accepted anywhere: these would let a browser dictate the transaction.
    FORBIDDEN = ("variant_of", "attribute_values", "has_variants", "uom",
                 "stock_uom", "sales_uom", "conversion_factor", "stock_qty",
                 "warehouse", "price_list", "rate")

    #: `template` + `attributes` are a SELECTION, and only the resolver takes them.
    #: They grant nothing: the server resolves the SKU through ERPNext and
    #: revalidates it, and the response is built from stored data, not the request.
    SELECTION_ONLY = {"catalog.resolve_variant": {"template", "attributes"}}

    def test_selection_inputs_exist_only_on_the_resolver(self):
        import importlib
        import pkgutil

        import yob_storefront.api as api_pkg

        for module_info in pkgutil.iter_modules(api_pkg.__path__):
            module = importlib.import_module(f"yob_storefront.api.{module_info.name}")

            for name, obj in vars(module).items():
                if not callable(obj) or getattr(obj, "__module__", None) != module.__name__:
                    continue
                if obj not in frappe.whitelisted:
                    continue

                endpoint = f"{module_info.name}.{name}"
                allowed = self.SELECTION_ONLY.get(endpoint, set())
                params = inspect.signature(inspect.unwrap(obj)).parameters

                for selection in ("template", "attributes"):
                    if selection in params:
                        self.assertIn(selection, allowed,
                                      f"{endpoint} accepts `{selection}`")

    def test_add_to_cart_takes_only_a_code_and_a_quantity(self):
        from yob_storefront.api import cart as cart_api

        params = inspect.signature(inspect.unwrap(cart_api.add_to_cart)).parameters

        self.assertEqual([p for p in params if p != "auth_context"], ["item_code", "qty"])

    def test_no_endpoint_accepts_variant_or_transaction_authority(self):
        import importlib
        import pkgutil

        import yob_storefront.api as api_pkg

        checked = 0

        for module_info in pkgutil.iter_modules(api_pkg.__path__):
            module = importlib.import_module(f"yob_storefront.api.{module_info.name}")

            for name, obj in vars(module).items():
                if not callable(obj) or getattr(obj, "__module__", None) != module.__name__:
                    continue
                if obj not in frappe.whitelisted:
                    continue

                checked += 1
                params = inspect.signature(inspect.unwrap(obj)).parameters

                for forbidden in self.FORBIDDEN:
                    self.assertNotIn(
                        forbidden, params,
                        f"{module.__name__}.{name} accepts `{forbidden}` from the browser")

        self.assertGreater(checked, 10, "the endpoint scan found almost nothing")

    def test_the_cart_stores_only_a_resolved_item_code(self):
        """Cart intent names a SKU. There is no attribute state to tamper with."""

        meta = frappe.get_meta("Cart Item")

        self.assertTrue(meta.get_field("item_code"))
        for forbidden in ("attributes", "variant_of", "template", "attribute_values"):
            self.assertIsNone(meta.get_field(forbidden),
                              f"Cart Item stores a client-forgeable `{forbidden}`")

    def test_yob_does_not_reproduce_erpnexts_variant_naming(self):
        """The SKU is ERPNext's to name; YOB must only ever look one up."""

        from yob_storefront.services import (catalog_listing_service, pricing_service,
                                             variant_service)
        from yob_storefront.api import catalog

        for module in (catalog, catalog_listing_service, pricing_service, variant_service):
            source = _executable_source(module)
            self.assertNotIn("make_variant_item_code", source)
            self.assertNotIn("create_variant", source,
                             f"{module.__name__} creates Items on a storefront path")


if __name__ == "__main__":
    unittest.main()
