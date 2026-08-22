# Copyright (c) 2026, YOB and Shayona
"""Phase 25F -- the whole storefront chain, end to end, in one scenario.

WHAT THIS IS FOR
----------------
Every link below is already covered by a test somewhere. This proves something
those tests cannot: that the links FIT. Each step here is fed the PUBLISHED
OUTPUT of the step before it -- never a constant a test author typed -- so a
category slug that navigation returns is the slug the filters are asked for, and
the SKU the resolver answers is the SKU that reaches the Cart.

    Frappe admin configuration
      -> Menu                       cms.get_menu
      -> category slug              destination.target, the only identity published
      -> Category Filter Set        catalog.get_category_filters
      -> filtered listing           catalog.get_items(storefront_filters=...)
      -> dynamic Page               cms.get_page
      -> all five Blocks
      -> Product Grid               the SAME catalogue service
      -> simple product / family
      -> variant resolution         catalog.resolve_variant
      -> quantity
      -> Cart                       cart.add_to_cart

WHY A HAND-OFF TEST CATCHES WHAT UNIT TESTS DO NOT
--------------------------------------------------
A unit test asserts that `get_category_filters("power-tools")` works. It cannot
notice that navigation publishes a docname where the filter endpoint expects a
slug, because the test supplied the slug itself. The seam between two correct
components is exactly where a contract breaks, and it is the only thing this
file is about.

The buyer's authority stays where Phase 23-24 put it: this chain sends an
`item_code` and a `qty` and nothing else. No UOM, warehouse, price list, rate or
SKU string is constructed anywhere below.
"""

import inspect
import json
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import flt

CUSTOMER = "YOB Demo Buyer"
SEED_ITEM = "YOB-BOLT-M10"
COLOUR = "Colour"
SIZE = "Size"


def _seeded():
    return bool(frappe.db.exists("Customer", CUSTOMER) and frappe.db.exists("Item", SEED_ITEM))


class ChainCase(unittest.TestCase):
    """One merchant configuration, walked from Desk to Cart."""

    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data on the test site")

    def setUp(self):
        frappe.set_user("Administrator")
        from yob_storefront.api import cart as cart_api, catalog as catalog_api, cms as cms_api

        self.catalog = catalog_api
        self.cms = cms_api
        self.cart_api = cart_api
        self.customer = frappe.get_doc("Customer", CUSTOMER)

        for module in (catalog_api, cms_api, cart_api):
            p = patch.object(module, "get_storefront_customer", return_value=self.customer)
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

        for attribute in (COLOUR, SIZE):
            if not frappe.db.exists("Item Attribute", attribute):
                raise unittest.SkipTest(f"Item Attribute {attribute!r} is not configured here")

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()
        frappe.flags.attribute_values = None
        self.assertEqual(self.commits, [], "a commit escaped the test rollback")

    # =====================================================================
    # THE MERCHANT'S CONFIGURATION -- everything a human would do in Desk
    # =====================================================================

    def configure(self):
        """Build the whole storefront the way an administrator would.

        Returns only what a MERCHANT knows -- names they typed. Nothing here is
        handed to the assertions as a public identity: the tests re-derive every
        slug and key from what the API publishes.
        """

        # ---------------- filters ----------------
        voltage = self.make_filter("voltage", "Voltage")
        colour = self.make_filter("colour", "Colour")
        material = self.make_filter("material", "Material")

        v230 = self.make_value(voltage.name, "230V", sequence=1)
        v415 = self.make_value(voltage.name, "415V", sequence=2)
        red = self.make_value(colour.name, "Red", sequence=1)
        steel = self.make_value(material.name, "Steel")

        # The 25B semantics, exercised for real: the ITEM set is wider than the
        # CATEGORY set. Material is metadata an item legitimately carries and a
        # category legitimately does not expose.
        item_set = self.make_set("_F25 Product Filters",
                                 [voltage.name, colour.name, material.name])
        category_set = self.make_set("_F25 Customer Filters", [voltage.name, colour.name])

        category = self.make_category("f25-switchgear", filter_set=category_set.name)

        # ---------------- products ----------------
        # A simple item that MATCHES the selection the buyer will make.
        simple = self.make_item(
            "_F25-SWITCH-230", category=category.name, price=150, filter_set=item_set.name,
            filters=[{"filter": voltage.name, "filter_value": v230.name},
                     {"filter": colour.name, "filter_value": red.name},
                     {"filter": material.name, "filter_value": steel.name}])

        # A simple item in the same category that does NOT match, so "filtered"
        # means something.
        self.make_item(
            "_F25-SWITCH-415", category=category.name, price=180, filter_set=item_set.name,
            filters=[{"filter": voltage.name, "filter_value": v415.name}])

        # A variant FAMILY that matches. Filters live on the template, never on
        # the generated children (Phase 25B).
        family, variants = self.make_family(
            "_F25-BREAKER", category=category.name, filter_set=item_set.name,
            filters=[{"filter": voltage.name, "filter_value": v230.name},
                     {"filter": colour.name, "filter_value": red.name}])

        # ---------------- navigation ----------------
        menu = self.make_menu("f25-main")
        self.make_node(menu.name, "Products", "Group")
        group = frappe.get_last_doc("YOB Storefront Menu Item",
                                    filters={"menu": menu.name, "is_group": 1})
        self.make_node(menu.name, "Switchgear", "Storefront Category",
                       parent=group.name, storefront_category=category.name)

        # ---------------- the page ----------------
        page_media = {"desktop_image": "/files/f25.png", "mobile_image": "/files/f25-sm.png",
                      "title": "Monsoon", "alt_text": "Monsoon offer",
                      "link_type": "Storefront Category", "link_category": category.name}

        blocks = [
            self.make_block("Image Banner", "_F25 Banner", desktop_image="/files/f25.png",
                            mobile_image="/files/f25-sm.png", alt_text="Switchgear",
                            desktop_height_px=420, mobile_height_px=220,
                            link_type="Storefront Category", link_category=category.name),
            self.make_block("Rich Text", "_F25 Prose", content_title="About",
                            content="<p>Industrial switchgear.</p>", text_alignment="Center"),
            self.make_block("Banner Carousel", "_F25 Carousel", auto_play=1, interval_ms=5000,
                            desktop_height_px=420, mobile_height_px=220,
                            slides=[dict(page_media)]),
            self.make_block("Product Grid", "_F25 Grid",
                            storefront_category=category.name, item_limit=6,
                            card_type="Square", sort_by="Name A-Z"),
            self.make_block("Promo Grid", "_F25 Promos", cards_per_row="3",
                            desktop_height_px=300, mobile_height_px=200,
                            promo_cards=[dict(page_media)]),
        ]
        self.make_page("f25-switchgear-landing", "Switchgear",
                       blocks=[{"block": b.name} for b in blocks])

        frappe.clear_cache()

        return frappe._dict(menu_key=menu.menu_key, page_slug="f25-switchgear-landing",
                            simple=simple.name, family=family.name, variants=variants)

    # ------------------------------------------------------------- fixtures

    def make_filter(self, key, label):
        return frappe.get_doc({
            "doctype": "YOB Storefront Filter", "filter_key": key, "label": label,
            "enabled": 1}).insert(ignore_permissions=True)

    def make_value(self, filter_name, value, sequence=0):
        return frappe.get_doc({
            "doctype": "YOB Storefront Filter Value", "filter": filter_name,
            "value": value, "enabled": 1, "sequence": sequence,
        }).insert(ignore_permissions=True)

    def make_set(self, name, filters):
        return frappe.get_doc({
            "doctype": "YOB Storefront Filter Set", "set_name": name,
            "filters": [{"filter": f, "sequence": i} for i, f in enumerate(filters)],
        }).insert(ignore_permissions=True)

    def make_category(self, slug, filter_set=None):
        return frappe.get_doc({
            "doctype": "Category", "category_name": slug, "slug": slug,
            "is_group": 0, "is_active": 1, "storefront_filter_set": filter_set,
        }).insert(ignore_permissions=True)

    def make_item(self, code, category, price, filters=None, filter_set=None, **kw):
        doc = {"doctype": "Item", "item_code": code, "item_name": code,
               "item_group": self.item_group, "stock_uom": self.uom, "is_stock_item": 0,
               "is_sales_item": 1, "gst_hsn_code": self.hsn,
               "custom_slug": code.lower(), "custom_category": category}
        if filter_set:
            doc["custom_storefront_filter_set"] = filter_set
        if filters:
            doc["custom_storefront_filters"] = filters
        doc.update(kw)

        item = frappe.get_doc(doc).insert(ignore_permissions=True)
        self.price(item.name, price)
        return item

    def price(self, item_code, rate):
        frappe.get_doc({
            "doctype": "Item Price", "item_code": item_code, "price_list": self.price_list,
            "price_list_rate": rate, "selling": 1, "uom": self.uom,
        }).insert(ignore_permissions=True)

    def make_family(self, code, category, filter_set, filters):
        """A template with two real variants. Only the template carries filters."""

        from erpnext.controllers.item_variant import create_variant

        values = {}
        for attribute, value in ((COLOUR, "Red"), (SIZE, "Medium"), (SIZE, "Large")):
            values[value] = self.ensure_attribute_value(attribute, value)

        template = frappe.get_doc({
            "doctype": "Item", "item_code": code, "item_name": code,
            "item_group": self.item_group, "stock_uom": self.uom, "is_stock_item": 0,
            "is_sales_item": 1, "gst_hsn_code": self.hsn, "custom_slug": code.lower(),
            "custom_category": category, "has_variants": 1,
            "custom_storefront_filter_set": filter_set,
            "custom_storefront_filters": filters,
            "attributes": [{"attribute": COLOUR}, {"attribute": SIZE}],
        }).insert(ignore_permissions=True)

        variants = {}
        for size, rate in (("Medium", 900), ("Large", 1400)):
            variant = create_variant(template.name, {COLOUR: "Red", SIZE: size})
            variant.insert(ignore_permissions=True)
            self.price(variant.name, rate)
            variants[size] = variant.name

        return template, variants

    def ensure_attribute_value(self, attribute, value):
        if not frappe.db.exists("Item Attribute Value",
                                {"parent": attribute, "attribute_value": value}):
            doc = frappe.get_doc("Item Attribute", attribute)
            doc.append("item_attribute_values",
                       {"attribute_value": value, "abbr": value[:3].upper()})
            doc.save(ignore_permissions=True)
            frappe.clear_document_cache("Item Attribute", attribute)

        frappe.flags.attribute_values = None
        return value

    def make_menu(self, key):
        return frappe.get_doc({
            "doctype": "YOB Storefront Menu", "menu_key": key, "menu_name": key.title(),
            "enabled": 1}).insert(ignore_permissions=True)

    def make_node(self, menu, label, item_type, parent=None, **kw):
        doc = {"doctype": "YOB Storefront Menu Item", "menu": menu, "label": label,
               "item_type": item_type, "enabled": 1,
               "parent_yob_storefront_menu_item": parent}
        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True)

    def make_block(self, block_type, block_name, **kw):
        doc = {"doctype": "YOB Storefront Block", "block_name": block_name,
               "block_type": block_type, "enabled": 1}
        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True)

    def make_page(self, slug, title, blocks):
        return frappe.get_doc({
            "doctype": "YOB Storefront Page", "slug": slug, "title": title,
            "enabled": 1, "blocks": blocks}).insert(ignore_permissions=True)

    # ------------------------------------------------------------- the wire

    def call(self, endpoint, **kw):
        """Exactly how Frappe delivers a GET: every value a string."""

        frappe.clear_cache()
        return inspect.unwrap(endpoint)(auth_context={}, **kw)

    def data(self, response, step):
        self.assertNotIn("errors", response, f"{step} failed: {response}")
        return response["data"]

    def fresh_cart(self):
        cart = self.cart_api.get_or_create_cart(self.customer)
        cart.set("items", [])
        cart.coupon_code = None
        cart.save(ignore_permissions=True)
        return cart

    # =====================================================================
    # LINK 1 -- Desk configuration reaches the storefront as navigation
    # =====================================================================

    def navigate(self, config):
        """The menu, and the ONE public identity it publishes for the category."""

        menu = self.data(self.call(self.cms.get_menu, menu_key=config.menu_key),
                         "cms.get_menu")

        group = menu["items"][0]
        self.assertEqual(group["type"], "group")

        leaf = group["children"][0]
        destination = leaf["destination"]

        self.assertEqual(destination["type"], "storefront_category")
        self.assertEqual(destination["href"], f"/catalog/{destination['target']}")

        return menu, destination

    def test_a_merchants_menu_becomes_navigation(self):
        config = self.configure()
        menu, destination = self.navigate(config)

        self.assertEqual(menu["key"], config.menu_key)
        self.assertEqual([i["label"] for i in menu["items"]], ["Products"])
        self.assertEqual([c["label"] for c in menu["items"][0]["children"]], ["Switchgear"])
        self.assertEqual(destination["target"], "f25-switchgear")

    def test_navigation_publishes_no_database_identity(self):
        config = self.configure()
        menu, _ = self.navigate(config)

        wire = json.dumps(menu)

        for leaked in ("storefront_category", "link_category", "link_page", "link_item"):
            if leaked == "storefront_category":
                continue                # the semantic TYPE, not a fieldname
            self.assertNotIn(leaked, wire, f"{leaked} reached the client")

        self.assertNotIn("f25-switchgear-landing".upper(), wire)
        self.assertNotIn("YOB Storefront Menu Item", wire)

    # =====================================================================
    # LINK 2 -- the published slug drives the category's own filters
    # =====================================================================

    def facets(self, destination):
        """Filters asked for with the slug NAVIGATION published, not a constant."""

        return self.data(
            self.call(self.catalog.get_category_filters, scope_value=destination["target"]),
            "catalog.get_category_filters")["filters"]

    def test_the_slug_from_navigation_resolves_its_category_filters(self):
        config = self.configure()
        _, destination = self.navigate(config)

        filters = self.facets(destination)

        self.assertEqual([f["key"] for f in filters], ["voltage", "colour"],
                         "the category exposed something other than its own Filter Set")

    def test_item_metadata_wider_than_the_category_stays_hidden(self):
        """The 25B rule, proved at runtime: two sets, two jobs."""

        config = self.configure()
        _, destination = self.navigate(config)

        keys = [f["key"] for f in self.facets(destination)]

        self.assertNotIn("material", keys,
                         "an Item Filter Set leaked into the storefront display scope")
        self.assertTrue(
            frappe.db.exists("YOB Storefront Item Filter",
                             {"parent": config.simple, "parentfield": "custom_storefront_filters"}),
            "the item should still HOLD the wider metadata")

    def test_only_values_present_in_the_category_are_offered(self):
        config = self.configure()
        _, destination = self.navigate(config)

        voltage = next(f for f in self.facets(destination) if f["key"] == "voltage")

        self.assertEqual({v["key"] for v in voltage["values"]}, {"230v", "415v"})

    # =====================================================================
    # LINK 3 -- the published facet keys narrow the listing
    # =====================================================================

    def filtered_listing(self, destination, filters):
        """A selection built ENTIRELY from what get_category_filters published."""

        voltage = next(f for f in filters if f["key"] == "voltage")
        chosen = next(v for v in voltage["values"] if v["label"] == "230V")

        selection = {voltage["key"]: [chosen["key"]]}

        listing = self.data(
            self.call(self.catalog.get_items, scope_type="category",
                      scope_value=destination["target"], page_size="24",
                      storefront_filters=json.dumps(selection)),
            "catalog.get_items(filtered)")

        return selection, listing

    def test_the_published_facet_keys_are_accepted_verbatim(self):
        config = self.configure()
        _, destination = self.navigate(config)

        selection, listing = self.filtered_listing(destination, self.facets(destination))

        names = {row["name"] for row in listing["items"]}

        self.assertIn(config.simple, names, "the matching simple item was filtered out")
        self.assertIn(config.family, names, "the matching variant family was filtered out")
        self.assertNotIn("_F25-SWITCH-415", names, "a non-matching item survived the filter")

    def test_an_unfiltered_listing_is_wider_than_the_filtered_one(self):
        config = self.configure()
        _, destination = self.navigate(config)

        _, filtered = self.filtered_listing(destination, self.facets(destination))
        everything = self.data(
            self.call(self.catalog.get_items, scope_type="category",
                      scope_value=destination["target"], page_size="24"),
            "catalog.get_items")

        self.assertGreater(len(everything["items"]), len(filtered["items"]),
                           "the filter narrowed nothing, so it proved nothing")

    def test_a_cursor_cannot_cross_from_one_selection_to_another(self):
        """The published pagination rule, at the seam where a client would break it."""

        config = self.configure()
        _, destination = self.navigate(config)

        page = self.data(
            self.call(self.catalog.get_items, scope_type="category",
                      scope_value=destination["target"], page_size="1"),
            "catalog.get_items")

        if not page["pagination"]["next_cursor"]:
            self.skipTest("one page held everything; no cursor to replay")

        selection, _ = self.filtered_listing(destination, self.facets(destination))

        replayed = self.call(
            self.catalog.get_items, scope_type="category", scope_value=destination["target"],
            page_size="1", cursor=page["pagination"]["next_cursor"],
            storefront_filters=json.dumps(selection))

        self.assertEqual(replayed["errors"][0]["code"], "cursor_invalid")

    # =====================================================================
    # LINK 4 -- the dynamic page, its five blocks, and its grid
    # =====================================================================

    def landing(self, config):
        return self.data(self.call(self.cms.get_page, slug=config.page_slug),
                         "cms.get_page")

    def test_the_page_returns_all_five_blocks_in_the_merchants_order(self):
        config = self.configure()

        blocks = self.landing(config)["blocks"]

        self.assertEqual([b["type"] for b in blocks],
                         ["image_banner", "rich_text", "banner_carousel",
                          "product_grid", "promo_grid"])

    def test_every_block_carries_its_own_renderable_content(self):
        config = self.configure()
        blocks = {b["type"]: b for b in self.landing(config)["blocks"]}

        self.assertEqual(blocks["image_banner"]["desktop_image"], "/files/f25.png")
        self.assertEqual(blocks["image_banner"]["desktop_height_px"], 420)
        self.assertIn("Industrial switchgear", blocks["rich_text"]["html"])
        self.assertEqual(blocks["rich_text"]["text_alignment"], "center")
        self.assertTrue(blocks["banner_carousel"]["auto_play"])
        self.assertEqual(len(blocks["banner_carousel"]["slides"]), 1)
        self.assertEqual(blocks["promo_grid"]["cards_per_row"], 3)
        self.assertEqual(len(blocks["promo_grid"]["cards"]), 1)

    def test_the_grid_is_bound_to_the_same_category_navigation_published(self):
        config = self.configure()
        _, destination = self.navigate(config)

        grid = next(b for b in self.landing(config)["blocks"] if b["type"] == "product_grid")

        self.assertEqual(grid["category"], destination["target"],
                         "the grid merchandises a different category than the menu opens")

    def test_a_slide_and_a_promo_card_share_one_destination_contract(self):
        config = self.configure()
        _, destination = self.navigate(config)
        blocks = {b["type"]: b for b in self.landing(config)["blocks"]}

        slide = blocks["banner_carousel"]["slides"][0]["destination"]
        card = blocks["promo_grid"]["cards"][0]["destination"]

        self.assertEqual(slide, card)
        self.assertEqual(slide, blocks["image_banner"]["destination"])
        self.assertEqual(slide["target"], destination["target"],
                         "a block and the menu disagree about the same category")

    # =====================================================================
    # LINK 5 -- grid cards ARE catalogue cards
    # =====================================================================

    def grid_cards(self, config):
        grid = next(b for b in self.landing(config)["blocks"] if b["type"] == "product_grid")
        return {row["name"]: row for row in grid["items"]}

    def test_the_grid_serves_the_same_cards_as_the_catalogue(self):
        config = self.configure()
        _, destination = self.navigate(config)

        cards = self.grid_cards(config)
        listing = self.data(
            self.call(self.catalog.get_items, scope_type="category",
                      scope_value=destination["target"], page_size="24"),
            "catalog.get_items")
        catalogue = {row["name"]: row for row in listing["items"]}

        self.assertTrue(cards, "the grid rendered no products")

        for name, card in cards.items():
            self.assertEqual(card, catalogue[name],
                             f"the grid's {name} card differs from the catalogue's")

    def test_a_family_card_carries_no_price_and_a_simple_one_does(self):
        config = self.configure()
        cards = self.grid_cards(config)

        simple = cards[config.simple]
        family = cards[config.family]

        self.assertEqual(simple["price_state"], "priced")
        self.assertEqual(flt(simple["rate"]), 150.0)

        self.assertEqual(family["price_state"], "select_options")
        self.assertTrue(family["has_variants"])
        self.assertIsNone(family["rate"], "a family borrowed a child variant's price")

    def test_no_generated_variant_is_ever_merchandised(self):
        config = self.configure()
        cards = self.grid_cards(config)

        for sku in config.variants.values():
            self.assertNotIn(sku, cards, "a generated variant appeared as its own card")

    # =====================================================================
    # LINK 6-8 -- family -> resolution -> quantity -> Cart
    # =====================================================================

    def test_the_grids_family_card_resolves_and_reaches_the_cart(self):
        """The last four links, each fed the one before it.

        The family page is opened by the SLUG the grid card published, the
        resolver is given the ATTRIBUTE VALUES that page advertised, and the Cart
        is sent the SKU the resolver returned -- with a quantity, and nothing
        else. No UOM, warehouse, price list or rate is constructed anywhere here.
        """

        config = self.configure()
        card = self.grid_cards(config)[config.family]

        # -- the family page, addressed by the card's own slug
        family = self.data(self.call(self.catalog.get_item, slug=card["slug"], qty="1"),
                           "catalog.get_item(family)")

        self.assertEqual(family["is_template"], 1)
        self.assertEqual(family["is_purchasable"], 0)

        # -- a complete selection, built from what the family advertised
        selection = {row["attribute"]: row["values"][0] for row in family["attributes"]}
        self.assertEqual(len(selection), 2, "the family advertised an incomplete matrix")

        # -- resolution: the server owns SKU naming, never the client
        resolved = self.data(
            self.call(self.catalog.resolve_variant, template=family["name"],
                      attributes=json.dumps(selection), qty="3"),
            "catalog.resolve_variant")

        self.assertIn(resolved["name"], config.variants.values())
        self.assertEqual(resolved["variant_of"], config.family)
        self.assertEqual(resolved["selected"], selection)

        # -- the Cart: item_code + qty, and that is the whole request
        self.fresh_cart()
        self.data(self.call(self.cart_api.add_to_cart, item_code=resolved["name"], qty="3"),
                  "cart.add_to_cart")

        cart = self.cart_api.get_or_create_cart(self.customer)
        cart.reload()
        row = cart.items[0]

        self.assertEqual(len(cart.items), 1)
        self.assertEqual(row.item_code, resolved["name"],
                         "the Cart holds a SKU the resolver never returned")
        self.assertEqual(flt(row.quantity), 3.0, "the buyer's quantity did not survive")
        self.assertEqual(flt(row.rate), flt(resolved["rate"]),
                         "the Cart charged a rate the preview never showed")

    def test_the_family_the_grid_advertised_can_never_be_bought(self):
        config = self.configure()
        card = self.grid_cards(config)[config.family]

        self.fresh_cart()
        refused = self.call(self.cart_api.add_to_cart, item_code=card["name"], qty="1")

        self.assertEqual(refused["errors"][0]["code"], "item_is_template")

        cart = self.cart_api.get_or_create_cart(self.customer)
        cart.reload()
        self.assertEqual(cart.items, [], "a template became Cart intent")

    def test_the_buyer_controls_only_the_product_and_the_quantity(self):
        """The governing rule, asserted against this chain's own endpoints."""

        endpoints = (self.cart_api.add_to_cart, self.catalog.resolve_variant,
                     self.catalog.get_items, self.cms.get_page)
        forbidden = {"uom", "conversion_factor", "stock_qty", "warehouse",
                     "price_list", "rate", "amount"}

        for endpoint in endpoints:
            accepted = set(inspect.signature(inspect.unwrap(endpoint)).parameters)
            self.assertEqual(
                accepted & forbidden, set(),
                f"{endpoint.__name__} accepts server-owned transaction context")
