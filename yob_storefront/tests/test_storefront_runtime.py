# Copyright (c) 2026, YOB and Shayona
"""Storefront runtime contracts: navigation, filters and content (Phase 25C).

WHAT THESE PIN
--------------
The three things a client actually receives, and the boundaries that keep them
honest:

* a MENU is published only when the menu, the item, its parent and its
  destination all still agree -- otherwise the node is dropped, never shipped as
  a dead link;
* FILTERS come from the Category's own Filter Set and nothing else, with values
  restricted to what is actually assigned in that category, and no pricing;
* a PAGE is ordered, discriminated blocks, and a Product Grid is answered by the
  EXISTING catalogue service so it inherits Phase 22-24 behaviour whole.

Database identity never leaves the server: a destination carries a semantic type
and a public slug, never `link_category` / `link_page` / `link_item`.
"""

import inspect
import json
import pathlib
import unittest
from unittest.mock import patch

import frappe

CUSTOMER = "YOB Demo Buyer"
SEED_ITEM = "YOB-BOLT-M10"


def _seeded():
    return bool(frappe.db.exists("Customer", CUSTOMER) and frappe.db.exists("Item", SEED_ITEM))


class RuntimeBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data on the test site")

    def setUp(self):
        frappe.set_user("Administrator")
        from yob_storefront.api import catalog as catalog_api, cms as cms_api

        self.catalog = catalog_api
        self.cms = cms_api
        self.customer = frappe.get_doc("Customer", CUSTOMER)

        for module in (catalog_api, cms_api):
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

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()
        self.assertEqual(self.commits, [], "a commit escaped the test rollback")

    # ------------------------------------------------------------- fixtures

    def make_category(self, slug="r25-cat", is_group=0, is_active=1, filter_set=None):
        return frappe.get_doc({
            "doctype": "Category", "category_name": slug, "slug": slug,
            "is_group": is_group, "is_active": is_active,
            "storefront_filter_set": filter_set,
        }).insert(ignore_permissions=True)

    def make_item(self, code, category=None, price=100, filters=None, filter_set=None, **kw):
        doc = {"doctype": "Item", "item_code": code, "item_name": kw.pop("item_name", code),
               "item_group": self.item_group, "stock_uom": self.uom, "is_stock_item": 0,
               "is_sales_item": 1, "gst_hsn_code": self.hsn,
               "custom_slug": kw.pop("custom_slug", code.lower()),
               "custom_category": category}
        if filter_set:
            doc["custom_storefront_filter_set"] = filter_set
        if filters:
            doc["custom_storefront_filters"] = filters
        doc.update(kw)
        item = frappe.get_doc(doc).insert(ignore_permissions=True)

        if price is not None:
            frappe.get_doc({
                "doctype": "Item Price", "item_code": item.name,
                "price_list": self.price_list, "price_list_rate": price,
                "selling": 1, "uom": self.uom}).insert(ignore_permissions=True)
        return item

    def make_filter(self, key, label=None, enabled=1):
        return frappe.get_doc({
            "doctype": "YOB Storefront Filter", "filter_key": key,
            "label": label or key.title(), "enabled": enabled}).insert(ignore_permissions=True)

    def make_value(self, filter_name, value, enabled=1, sequence=0):
        return frappe.get_doc({
            "doctype": "YOB Storefront Filter Value", "filter": filter_name,
            "value": value, "enabled": enabled, "sequence": sequence,
        }).insert(ignore_permissions=True)

    def make_set(self, name, filters):
        return frappe.get_doc({
            "doctype": "YOB Storefront Filter Set", "set_name": name,
            "filters": [{"filter": f, "sequence": i} for i, f in enumerate(filters)],
        }).insert(ignore_permissions=True)

    def make_menu(self, key="r25-main", enabled=1):
        return frappe.get_doc({
            "doctype": "YOB Storefront Menu", "menu_key": key, "menu_name": key.title(),
            "enabled": enabled}).insert(ignore_permissions=True)

    def make_node(self, menu, label, item_type, parent=None, **kw):
        doc = {"doctype": "YOB Storefront Menu Item", "menu": menu, "label": label,
               "item_type": item_type, "parent_yob_storefront_menu_item": parent}
        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True)

    def make_block(self, block_type, **kw):
        doc = {"doctype": "YOB Storefront Block",
               "block_name": kw.pop("block_name", f"_R25 {block_type}"),
               "block_type": block_type}
        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True)

    def make_page(self, slug="r25-page", enabled=1, blocks=None, **kw):
        doc = {"doctype": "YOB Storefront Page", "slug": slug,
               "title": kw.pop("title", "R25 Page"), "enabled": enabled,
               "blocks": blocks or []}
        doc.update(kw)
        return frappe.get_doc(doc).insert(ignore_permissions=True)

    # ------------------------------------------------------------- the wire

    def menu(self, key):
        frappe.clear_cache()
        return inspect.unwrap(self.cms.get_menu)(auth_context={}, menu_key=key)

    def page(self, slug):
        frappe.clear_cache()
        return inspect.unwrap(self.cms.get_page)(auth_context={}, slug=slug)

    def filters_of(self, slug):
        frappe.clear_cache()
        return inspect.unwrap(self.catalog.get_category_filters)(
            auth_context={}, scope_value=slug)

    def listing(self, **kw):
        frappe.clear_cache()
        return inspect.unwrap(self.catalog.get_items)(auth_context={}, **kw)

    def data(self, response):
        self.assertNotIn("errors", response, f"request failed: {response}")
        return response["data"]

    def code_of(self, response):
        return response["errors"][0]["code"] if "errors" in response else None


# =========================================================
# NAVIGATION
# =========================================================

class MenuRuntimeCase(RuntimeBase):

    def test_an_enabled_menu_is_returned(self):
        menu = self.make_menu()
        self.make_node(menu.name, "Home", "Home")

        data = self.data(self.menu("r25-main"))

        self.assertEqual(data["key"], "r25-main")
        self.assertEqual([i["label"] for i in data["items"]], ["Home"])

    def test_a_disabled_menu_is_not_published(self):
        self.make_menu(enabled=0)

        self.assertEqual(self.code_of(self.menu("r25-main")), "menu_not_found")

    def test_an_unknown_menu_answers_the_same_as_a_disabled_one(self):
        self.assertEqual(self.code_of(self.menu("no-such-menu")), "menu_not_found")

    def test_a_disabled_item_is_omitted(self):
        menu = self.make_menu()
        self.make_node(menu.name, "Home", "Home")
        self.make_node(menu.name, "Hidden", "Catalog", enabled=0)

        data = self.data(self.menu("r25-main"))

        self.assertEqual([i["label"] for i in data["items"]], ["Home"])

    def test_a_disabled_group_suppresses_its_children(self):
        menu = self.make_menu()
        group = self.make_node(menu.name, "Tools", "Group", enabled=0)
        self.make_node(menu.name, "Catalogue", "Catalog", parent=group.name)

        data = self.data(self.menu("r25-main"))

        self.assertEqual(data["items"], [])

    def test_a_group_with_no_publishable_child_is_dropped(self):
        menu = self.make_menu()
        group = self.make_node(menu.name, "Empty", "Group")
        self.make_node(menu.name, "Gone", "Catalog", parent=group.name, enabled=0)

        self.assertEqual(self.data(self.menu("r25-main"))["items"], [])

    def test_ordering_is_deterministic(self):
        menu = self.make_menu()
        for label, sequence in (("Third", 30), ("First", 10), ("Second", 20)):
            self.make_node(menu.name, label, "Home", sequence=sequence)

        data = self.data(self.menu("r25-main"))

        self.assertEqual([i["label"] for i in data["items"]], ["First", "Second", "Third"])

    def test_a_group_publishes_its_children(self):
        menu = self.make_menu()
        group = self.make_node(menu.name, "Tools", "Group")
        self.make_node(menu.name, "Catalogue", "Catalog", parent=group.name)

        node = self.data(self.menu("r25-main"))["items"][0]

        self.assertEqual(node["type"], "group")
        self.assertIsNone(node["destination"])
        self.assertEqual([c["label"] for c in node["children"]], ["Catalogue"])
        self.assertEqual(node["children"][0]["children"], [],
                         "a grandchild appeared in the projection")

    def test_catalog_destination(self):
        menu = self.make_menu()
        self.make_node(menu.name, "Shop", "Catalog")

        destination = self.data(self.menu("r25-main"))["items"][0]["destination"]

        self.assertEqual(destination["type"], "catalog")
        self.assertEqual(destination["href"], "/catalog")
        self.assertFalse(destination["external"])

    def test_category_destination_uses_the_public_slug(self):
        menu = self.make_menu()
        category = self.make_category(slug="power-tools-r25")
        self.make_node(menu.name, "Power Tools", "Storefront Category",
                       storefront_category=category.name)

        destination = self.data(self.menu("r25-main"))["items"][0]["destination"]

        self.assertEqual(destination["type"], "storefront_category")
        self.assertEqual(destination["target"], "power-tools-r25")
        self.assertEqual(destination["href"], "/catalog/power-tools-r25")

    def test_a_category_disabled_after_linking_drops_the_item(self):
        menu = self.make_menu()
        category = self.make_category(slug="r25-later-off")
        self.make_node(menu.name, "Gone", "Storefront Category",
                       storefront_category=category.name)

        frappe.db.set_value("Category", category.name, "is_active", 0)

        self.assertEqual(self.data(self.menu("r25-main"))["items"], [],
                         "a dead link was published")

    def test_page_destination_carries_type_and_slug(self):
        menu = self.make_menu()
        page = self.make_page(slug="r25-about")
        self.make_node(menu.name, "About", "Storefront Page", storefront_page=page.name)

        destination = self.data(self.menu("r25-main"))["items"][0]["destination"]

        self.assertEqual(destination["type"], "storefront_page")
        self.assertEqual(destination["target"], "r25-about")
        # Angular has no dynamic page route yet; a real route is not invented here.
        self.assertIsNone(destination["href"])

    def test_an_unpublished_page_destination_is_dropped(self):
        menu = self.make_menu()
        page = self.make_page(slug="r25-draft", enabled=0)
        self.make_node(menu.name, "Draft", "Storefront Page", storefront_page=page.name)

        self.assertEqual(self.data(self.menu("r25-main"))["items"], [])

    def test_safe_external_url_is_preserved(self):
        menu = self.make_menu()
        self.make_node(menu.name, "Blog", "External URL",
                       external_url="https://example.com/blog", open_in_new_tab=1)

        destination = self.data(self.menu("r25-main"))["items"][0]["destination"]

        self.assertEqual(destination["type"], "external_url")
        self.assertEqual(destination["href"], "https://example.com/blog")
        self.assertTrue(destination["external"])
        self.assertTrue(destination["open_in_new_tab"])

    # ------------------------------------------------- Phase 28C: All Products

    def test_all_products_projects_the_fixed_products_route(self):
        """The whole contract of the type, in one assertion."""

        menu = self.make_menu()
        self.make_node(menu.name, "Shop All", "All Products")

        destination = self.data(self.menu("r25-main"))["items"][0]["destination"]

        self.assertEqual(destination["type"], "all_products")
        self.assertEqual(destination["href"], "/products")
        self.assertFalse(destination["external"])

    def test_all_products_carries_no_target_and_never_opens_a_new_tab(self):
        """A fixed route has nothing to point AT, so `target` stays null -- the
        same shape Home and Catalog have always had. `open_in_new_tab` is cleared
        on save for every type but External URL, so the backend can never imply
        `target="_blank"` here.
        """

        menu = self.make_menu()
        self.make_node(menu.name, "Products", "All Products", open_in_new_tab=1)

        destination = self.data(self.menu("r25-main"))["items"][0]["destination"]

        self.assertIsNone(destination["target"])
        self.assertFalse(destination["open_in_new_tab"])
        self.assertFalse(destination["external"])

    def test_the_all_products_label_is_the_merchants_not_the_types(self):
        menu = self.make_menu()
        self.make_node(menu.name, "Shop Everything", "All Products")

        item = self.data(self.menu("r25-main"))["items"][0]

        self.assertEqual(item["label"], "Shop Everything")
        self.assertEqual(item["destination"]["href"], "/products")

    def test_all_products_leaks_no_database_identity(self):
        menu = self.make_menu()
        self.make_node(menu.name, "Products", "All Products")

        payload = json.dumps(self.data(self.menu("r25-main")))

        for internal in ("item_type", "storefront_category", "storefront_page",
                         "external_url", "All Products", "lft", "rgt", "doctype",
                         "parent_yob_storefront_menu_item"):
            self.assertNotIn(internal, payload, f"{internal} leaked to the client")

    def test_all_products_projects_through_the_shared_projector(self):
        """Called directly, so the route cannot quietly be menu-only logic."""

        from yob_storefront.services.storefront_destination import (
            MENU_FIELDS,
            project_destination,
        )

        # `open_in_new_tab` is set deliberately: a fixed route ignores it, exactly
        # as Home and Catalog always have, so a stale 1 left on a row cannot make
        # an in-app link open in a new tab.
        stored = frappe._dict({"item_type": "All Products", "open_in_new_tab": 1})

        self.assertEqual(
            project_destination(stored, MENU_FIELDS),
            {"type": "all_products", "target": None, "href": "/products",
             "external": False, "open_in_new_tab": False})

    def test_every_target_less_type_declares_its_route(self):
        """Guards the two maps against drifting apart.

        A type registered in TYPE_MAP with no target field but no entry in
        IMPLIED_ROUTES would raise a KeyError inside the projection rather than
        answering anything -- caught here instead of in production.
        """

        from yob_storefront.services.storefront_destination import (
            IMPLIED_ROUTES,
            TYPE_MAP,
        )

        target_less = {machine for machine, field in TYPE_MAP.values() if field is None}

        self.assertEqual(target_less, set(IMPLIED_ROUTES),
                         "a fixed-route type has no route, or vice versa")

    def test_the_other_fixed_routes_are_unchanged(self):
        """Adding a third route type must not disturb the two that existed."""

        menu = self.make_menu()
        self.make_node(menu.name, "Home", "Home")
        self.make_node(menu.name, "Shop", "Catalog")

        found = {item["destination"]["type"]: item["destination"]
                 for item in self.data(self.menu("r25-main"))["items"]}

        self.assertEqual(found["home"]["href"], "/")
        self.assertEqual(found["catalog"]["href"], "/catalog")
        for destination in found.values():
            self.assertFalse(destination["external"])
            self.assertIsNone(destination["target"])

    # ------------------------------------------------- Phase 28A internal routes

    def test_an_internal_route_projects_as_an_in_app_link(self):
        """The defect this fixes: `validate_destination` has accepted a
        single-leading-slash route since Phase 25B, but the projector demanded a
        scheme AND a netloc -- so a route a merchant had legitimately saved
        projected as None and the menu item silently disappeared.

        Phase 28C added a first-class `All Products` type for this destination,
        which is the type a merchant should now pick. It did NOT retire this
        capability: an External URL holding `/products` still works, because
        stored menus already rely on it and generic internal routes are useful
        for destinations that have no type of their own.
        """

        menu = self.make_menu()
        self.make_node(menu.name, "All Products", "External URL",
                       external_url="/products")

        destination = self.data(self.menu("r25-main"))["items"][0]["destination"]

        self.assertIsNotNone(destination, "the menu item vanished at runtime")
        self.assertEqual(destination["type"], "external_url")
        self.assertEqual(destination["target"], "/products")
        self.assertEqual(destination["href"], "/products")
        self.assertFalse(destination["external"],
                         "an in-app route must not be flagged as leaving the site")

    def test_the_two_ways_to_reach_products_agree_on_the_route(self):
        """`All Products` and a stored `/products` route are different STORED
        destinations that land on the same page. The machine types differ on
        purpose -- one is a fixed contract, the other is merchant input -- but a
        buyer must not be able to tell which button they clicked.
        """

        menu = self.make_menu()
        self.make_node(menu.name, "Typed", "All Products")
        self.make_node(menu.name, "Routed", "External URL", external_url="/products")

        found = {item["label"]: item["destination"]
                 for item in self.data(self.menu("r25-main"))["items"]}

        self.assertEqual(found["Typed"]["href"], found["Routed"]["href"], "/products")
        self.assertFalse(found["Typed"]["external"])
        self.assertFalse(found["Routed"]["external"])

        self.assertEqual(found["Typed"]["type"], "all_products")
        self.assertEqual(found["Routed"]["type"], "external_url")

    def test_the_internal_route_rule_is_generic_not_products_specific(self):
        """Nothing here knows what `/products` means -- any safe route projects."""

        menu = self.make_menu()
        for index, route in enumerate(
                ("/account", "/orders", "/catalog/power-tools", "/products?sort=newest")):
            self.make_node(menu.name, f"R{index}", "External URL", external_url=route)

        items = self.data(self.menu("r25-main"))["items"]

        self.assertEqual(len(items), 4, "a valid internal route was dropped")

        for item in items:
            self.assertFalse(item["destination"]["external"])
            self.assertEqual(item["destination"]["href"], item["destination"]["target"])

    def test_an_absolute_url_is_still_external(self):
        """The correction must not turn real outbound links into in-app routes."""

        menu = self.make_menu()
        self.make_node(menu.name, "Blog", "External URL",
                       external_url="https://example.com/blog")
        self.make_node(menu.name, "Plain", "External URL",
                       external_url="http://example.com/x")

        for item in self.data(self.menu("r25-main"))["items"]:
            self.assertTrue(item["destination"]["external"],
                            f"{item['destination']['href']} lost its external flag")

    def test_a_scheme_relative_target_is_still_refused(self):
        """`//example.com` is read by a browser as scheme-relative and leaves the
        storefront. It must not be mistaken for an internal route just because it
        starts with a slash -- the save-time regex refuses it and so does this."""

        from yob_storefront.services.storefront_destination import (
            MENU_FIELDS,
            project_destination,
        )

        for unsafe in ("//example.com", "///example.com", "//evil.test/path"):
            stored = frappe._dict({"item_type": "External URL", "external_url": unsafe,
                                   "open_in_new_tab": 0})

            self.assertIsNone(project_destination(stored, MENU_FIELDS),
                              f"{unsafe!r} projected as a link")

    def test_unsafe_targets_are_refused_exactly_as_before(self):
        """Existing rejection behaviour is unchanged for everything non-route."""

        from yob_storefront.services.storefront_destination import (
            MENU_FIELDS,
            project_destination,
        )

        for unsafe in ("javascript:alert(1)", "data:text/html,<script>", "vbscript:x",
                       "file:///etc/passwd", "mailto:a@b.c", "not a url",
                       "https://", "ftp://example.com/x"):
            stored = frappe._dict({"item_type": "External URL", "external_url": unsafe,
                                   "open_in_new_tab": 0})

            self.assertIsNone(project_destination(stored, MENU_FIELDS),
                              f"{unsafe!r} projected as a link")

    def test_an_internal_route_still_honours_open_in_new_tab(self):
        """The stored type is unchanged, so its own field keeps working."""

        menu = self.make_menu()
        self.make_node(menu.name, "Docs", "External URL", external_url="/pages/docs",
                       open_in_new_tab=1)

        destination = self.data(self.menu("r25-main"))["items"][0]["destination"]

        self.assertTrue(destination["open_in_new_tab"])
        self.assertFalse(destination["external"])

    def test_no_database_identity_leaks(self):
        menu = self.make_menu()
        # Name deliberately different from the slug, so "the docname must not
        # appear" is a real assertion rather than a coincidence.
        category = self.make_category(slug="r25-leak")
        frappe.rename_doc("Category", category.name, "R25 Internal Name", force=True)
        category = frappe.get_doc("Category", "R25 Internal Name")
        group = self.make_node(menu.name, "Tools", "Group")
        self.make_node(menu.name, "Cat", "Storefront Category", parent=group.name,
                       storefront_category=category.name)

        payload = json.dumps(self.data(self.menu("r25-main")))

        # `storefront_category` appears as a semantic TYPE and that is correct;
        # what must never appear is Frappe field names or the docname.
        for internal in ("link_category", "parent_yob_storefront_menu_item",
                         "lft", "rgt", "doctype", "is_group", category.name):
            self.assertNotIn(internal, payload, f"{internal} leaked to the client")


class ProductDestinationCase(RuntimeBase):

    def test_a_simple_product_destination_resolves_to_its_route(self):
        block = self.make_block("Image Banner", desktop_image="/files/b.png",
                                link_type="Product", link_item=SEED_ITEM)

        from yob_storefront.services.storefront_destination import project_destination

        destination = project_destination(block)
        slug = frappe.db.get_value("Item", SEED_ITEM, "custom_slug")

        self.assertEqual(destination["type"], "product")
        self.assertEqual(destination["target"], slug)
        self.assertEqual(destination["href"], f"/catalog/item/{slug}")

    def test_a_variant_family_destination_is_valid(self):
        from yob_storefront.services.storefront_destination import project_destination

        attribute = frappe.db.get_value("Item Attribute", {"name": "Size"}, "name")
        if not attribute:
            self.skipTest("no Item Attribute on this bench")

        template = self.make_item("_R25-FAMILY", price=None, has_variants=1,
                                  custom_slug="r25-family",
                                  attributes=[{"attribute": attribute}])

        block = self.make_block("Image Banner", desktop_image="/files/b.png",
                                link_type="Product", link_item=template.name)

        destination = project_destination(block)

        self.assertEqual(destination["target"], "r25-family")

    def test_a_generated_variant_is_never_projected(self):
        """Phase 24 routing: the family owns the URL, not the child."""

        from erpnext.controllers.item_variant import create_variant
        from yob_storefront.services.storefront_destination import project_destination

        attribute = frappe.db.get_value("Item Attribute", {"name": "Size"}, "name")
        if not attribute:
            self.skipTest("no Item Attribute on this bench")

        value = frappe.db.get_value("Item Attribute Value", {"parent": attribute},
                                    "attribute_value")
        template = self.make_item("_R25-TMPL", price=None, has_variants=1,
                                  custom_slug="r25-tmpl",
                                  attributes=[{"attribute": attribute}])
        variant = create_variant(template.name, {attribute: value})
        variant.insert(ignore_permissions=True)

        block = self.make_block("Image Banner", desktop_image="/files/b.png",
                                link_type="Product", link_item=template.name)
        # Point it at the variant AFTER validation, as a stale link would be.
        frappe.db.set_value("YOB Storefront Block", block.name, "link_item", variant.name)
        block.reload()

        self.assertIsNone(project_destination(block),
                          "a generated variant was published as a public product")

    def test_an_unsafe_stored_url_is_never_projected(self):
        """Validated on save, re-checked here: a value written straight to the
        database, or before this rule existed, must never reach a browser."""

        from yob_storefront.services.storefront_destination import project_destination

        block = self.make_block("Image Banner", desktop_image="/files/b.png",
                                link_type="External URL",
                                link_external_url="https://example.com")
        frappe.db.set_value("YOB Storefront Block", block.name,
                            "link_external_url", "javascript:alert(1)")
        block.reload()

        self.assertIsNone(project_destination(block))


# =========================================================
# CATEGORY FILTERS
# =========================================================

class CategoryFilterCase(RuntimeBase):

    def electrical(self, category_slug="r25-switches"):
        """Item metadata richer than the category exposes -- the 25B semantics."""

        voltage = self.make_filter("voltage", "Voltage")
        colour = self.make_filter("colour", "Colour")
        material = self.make_filter("material", "Material")

        vals = {
            "v230": self.make_value(voltage.name, "230V", sequence=1),
            "v415": self.make_value(voltage.name, "415V", sequence=2),
            "red": self.make_value(colour.name, "Red", sequence=1),
            "blue": self.make_value(colour.name, "Blue", sequence=2),
            "steel": self.make_value(material.name, "Steel"),
        }

        item_set = self.make_set("R25 Product Filters",
                                 [voltage.name, colour.name, material.name])
        category_set = self.make_set("R25 Customer Filters", [voltage.name, colour.name])
        category = self.make_category(slug=category_slug, filter_set=category_set.name)

        return frappe._dict(voltage=voltage, colour=colour, material=material, vals=vals,
                            item_set=item_set, category_set=category_set, category=category)

    def with_item(self, f, code="_R25-SWITCH", pairs=None):
        rows = [{"filter": filt, "filter_value": value} for filt, value in (pairs or [])]
        return self.make_item(code, category=f.category.name, filters=rows,
                              filter_set=f.item_set.name)

    def test_a_category_returns_its_own_filter_set(self):
        f = self.electrical()
        self.with_item(f, pairs=[(f.voltage.name, f.vals["v230"].name),
                                 (f.colour.name, f.vals["red"].name),
                                 (f.material.name, f.vals["steel"].name)])

        data = self.data(self.filters_of("r25-switches"))
        keys = [row["key"] for row in data["filters"]]

        self.assertEqual(keys, ["voltage", "colour"],
                         "the category exposed something other than its own set")
        self.assertNotIn("material", keys,
                         "richer item metadata leaked into the category's filter UI")

    def test_a_category_without_a_filter_set_exposes_nothing(self):
        self.make_category(slug="r25-bare")

        self.assertEqual(self.data(self.filters_of("r25-bare"))["filters"], [])

    def test_no_inheritance_from_a_parent_category(self):
        f = self.electrical()
        parent = self.make_category(slug="r25-parent", is_group=1,
                                    filter_set=f.category_set.name)
        child = self.make_category(slug="r25-child")
        frappe.db.set_value("Category", child.name, "parent_category", parent.name)

        self.assertEqual(self.data(self.filters_of("r25-child"))["filters"], [],
                         "a parent's filter set was inherited")

    def test_filter_and_value_order_are_preserved(self):
        f = self.electrical()
        self.with_item(f, pairs=[(f.voltage.name, f.vals["v230"].name),
                                 (f.voltage.name, f.vals["v415"].name),
                                 (f.colour.name, f.vals["blue"].name),
                                 (f.colour.name, f.vals["red"].name)])

        data = self.data(self.filters_of("r25-switches"))

        self.assertEqual([row["key"] for row in data["filters"]], ["voltage", "colour"])
        voltage = data["filters"][0]
        self.assertEqual([v["label"] for v in voltage["values"]], ["230V", "415V"])

    def test_a_disabled_filter_is_omitted(self):
        f = self.electrical()
        self.with_item(f, pairs=[(f.voltage.name, f.vals["v230"].name),
                                 (f.colour.name, f.vals["red"].name)])
        frappe.db.set_value("YOB Storefront Filter", f.colour.name, "enabled", 0)

        keys = [row["key"] for row in self.data(self.filters_of("r25-switches"))["filters"]]

        self.assertEqual(keys, ["voltage"])

    def test_a_disabled_value_is_omitted(self):
        f = self.electrical()
        self.with_item(f, pairs=[(f.voltage.name, f.vals["v230"].name),
                                 (f.voltage.name, f.vals["v415"].name)])
        frappe.db.set_value("YOB Storefront Filter Value", f.vals["v415"].name, "enabled", 0)

        data = self.data(self.filters_of("r25-switches"))
        voltage = next(row for row in data["filters"] if row["key"] == "voltage")

        self.assertEqual([v["label"] for v in voltage["values"]], ["230V"])

    def test_values_not_used_in_this_category_are_omitted(self):
        """A facet that would return nothing is not offered."""

        f = self.electrical()
        self.with_item(f, pairs=[(f.voltage.name, f.vals["v230"].name)])

        data = self.data(self.filters_of("r25-switches"))
        voltage = next(row for row in data["filters"] if row["key"] == "voltage")

        self.assertEqual([v["label"] for v in voltage["values"]], ["230V"])
        self.assertNotIn("colour", [row["key"] for row in data["filters"]],
                         "a filter with no assigned value in this category was offered")

    def test_a_variant_family_contributes_its_metadata(self):
        f = self.electrical()
        attribute = frappe.db.get_value("Item Attribute", {"name": "Size"}, "name")
        if not attribute:
            self.skipTest("no Item Attribute on this bench")

        self.make_item("_R25-FAM", category=f.category.name, price=None,
                       has_variants=1, custom_slug="r25-fam",
                       attributes=[{"attribute": attribute}],
                       filter_set=f.item_set.name,
                       filters=[{"filter": f.colour.name,
                                 "filter_value": f.vals["blue"].name}])

        data = self.data(self.filters_of("r25-switches"))
        colour = next(row for row in data["filters"] if row["key"] == "colour")

        self.assertEqual([v["label"] for v in colour["values"]], ["Blue"])

    def test_no_counts_are_returned(self):
        f = self.electrical()
        self.with_item(f, pairs=[(f.voltage.name, f.vals["v230"].name)])

        value = self.data(self.filters_of("r25-switches"))["filters"][0]["values"][0]

        self.assertEqual(set(value), {"key", "label"},
                         "a count or extra field appeared in the facet contract")

    def test_filter_definitions_cost_no_pricing(self):
        from yob_storefront.services import pricing_service

        f = self.electrical()
        self.with_item(f, pairs=[(f.voltage.name, f.vals["v230"].name)])

        with patch.object(pricing_service, "get_item_pricing",
                          side_effect=AssertionError("facets priced an item")) as spy:
            self.data(self.filters_of("r25-switches"))

        self.assertEqual(spy.call_count, 0)

    def test_a_group_category_is_refused(self):
        self.make_category(slug="r25-groupcat", is_group=1)

        self.assertEqual(self.code_of(self.filters_of("r25-groupcat")),
                         "category_not_listable")


# =========================================================
# FILTERED LISTING
# =========================================================

class FilteredListingCase(RuntimeBase):
    """OR within one filter, AND across filters -- inside the Phase 22B pipeline."""

    def catalogue(self):
        material = self.make_filter("material", "Material")
        finish = self.make_filter("finish", "Finish")

        steel = self.make_value(material.name, "Steel")
        aluminium = self.make_value(material.name, "Aluminium")
        black = self.make_value(finish.name, "Black")
        chrome = self.make_value(finish.name, "Chrome")

        item_set = self.make_set("R25 Listing Filters", [material.name, finish.name])
        category = self.make_category(slug="r25-listing", filter_set=item_set.name)

        def product(code, pairs):
            return self.make_item(
                code, category=category.name, filter_set=item_set.name,
                filters=[{"filter": f, "filter_value": v} for f, v in pairs]).name

        products = {
            "steel_black": product("_R25-SB", [(material.name, steel.name),
                                               (finish.name, black.name)]),
            "steel_chrome": product("_R25-SC", [(material.name, steel.name),
                                                (finish.name, chrome.name)]),
            "alu_black": product("_R25-AB", [(material.name, aluminium.name),
                                             (finish.name, black.name)]),
            "plain": product("_R25-PL", []),
        }

        return frappe._dict(products=products, category=category)

    def filtered(self, selection, **kw):
        kw.setdefault("scope_value", "r25-listing")
        kw.setdefault("page_size", 24)
        return self.listing(storefront_filters=json.dumps(selection), **kw)

    def names(self, response):
        return {row["name"] for row in self.data(response)["items"]}

    def test_or_within_one_filter(self):
        c = self.catalogue()

        names = self.names(self.filtered({"material": ["steel", "aluminium"]}))

        self.assertEqual(names, {c.products["steel_black"], c.products["steel_chrome"],
                                 c.products["alu_black"]})
        self.assertNotIn(c.products["plain"], names)

    def test_and_across_filters(self):
        c = self.catalogue()

        names = self.names(self.filtered({"material": ["steel"], "finish": ["black"]}))

        self.assertEqual(names, {c.products["steel_black"]})

    def test_multi_value_and_multi_filter(self):
        c = self.catalogue()

        names = self.names(self.filtered(
            {"material": ["steel", "aluminium"], "finish": ["black"]}))

        self.assertEqual(names, {c.products["steel_black"], c.products["alu_black"]})

    def test_no_selection_leaves_the_listing_unchanged(self):
        c = self.catalogue()

        unfiltered = self.names(self.listing(scope_value="r25-listing", page_size=24))

        self.assertIn(c.products["plain"], unfiltered)

    def test_an_item_matching_twice_appears_once(self):
        """EXISTS, not a JOIN: a row must not multiply by its matching assignments."""

        c = self.catalogue()

        rows = self.data(self.filtered({"material": ["steel", "aluminium"]}))["items"]
        names = [row["name"] for row in rows]

        self.assertEqual(len(names), len(set(names)), "the filter query duplicated rows")

    def test_filtering_adds_no_pricing_calls(self):
        """Filtering happens in Stage 1, so a narrower page prices FEWER items."""

        from yob_storefront.services import catalog_listing_service as svc

        c = self.catalogue()
        real = svc.price_candidate
        priced = []

        def spy(ctx, row):
            priced.append(row["name"])
            return real(ctx, row)

        with patch.object(svc, "price_candidate", side_effect=spy):
            names = self.names(self.filtered({"material": ["steel"],
                                              "finish": ["black"]}))

        self.assertEqual(names, {c.products["steel_black"]})
        self.assertEqual(set(priced), names,
                         "an item outside the filtered page was priced")

    def test_malformed_selection_is_refused(self):
        self.catalogue()

        for payload in ("not json", "[1,2,3]", '"text"'):
            with self.subTest(payload=payload):
                response = self.listing(scope_value="r25-listing",
                                        storefront_filters=payload)
                self.assertEqual(self.code_of(response), "storefront_filter_invalid")

    def test_an_unknown_filter_is_refused(self):
        self.catalogue()

        response = self.filtered({"colour": ["red"]})

        self.assertEqual(self.code_of(response), "storefront_filter_unknown")

    def test_a_filter_outside_the_categorys_set_is_refused(self):
        c = self.catalogue()
        other = self.make_filter("weight", "Weight")
        self.make_value(other.name, "Heavy")

        response = self.filtered({"weight": ["heavy"]})

        self.assertEqual(self.code_of(response), "storefront_filter_unknown")

    def test_an_unknown_value_is_refused(self):
        self.catalogue()

        response = self.filtered({"material": ["titanium"]})

        self.assertEqual(self.code_of(response), "storefront_filter_value_unknown")

    def test_a_value_from_another_filter_is_refused(self):
        self.catalogue()

        response = self.filtered({"material": ["black"]})

        self.assertEqual(self.code_of(response), "storefront_filter_value_unknown")

    def test_a_disabled_value_cannot_be_selected(self):
        self.catalogue()
        value = frappe.db.get_value(
            "YOB Storefront Filter Value", {"value_key": "steel"}, "name")
        frappe.db.set_value("YOB Storefront Filter Value", value, "enabled", 0)

        self.assertEqual(self.code_of(self.filtered({"material": ["steel"]})),
                         "storefront_filter_value_unknown")

    def test_a_disabled_filter_cannot_be_selected(self):
        self.catalogue()
        material = frappe.db.get_value("YOB Storefront Filter", {"filter_key": "material"})
        frappe.db.set_value("YOB Storefront Filter", material, "enabled", 0)

        self.assertEqual(self.code_of(self.filtered({"material": ["steel"]})),
                         "storefront_filter_unknown")

    def test_selection_requires_a_category_context(self):
        """Facets are a property of the category being browsed."""

        from yob_storefront.services.storefront_filter_service import (
            FilterSelectionError,
            parse_selection,
        )

        with self.assertRaises(FilterSelectionError) as caught:
            parse_selection({"material": ["steel"]}, category=None)

        self.assertEqual(caught.exception.code, "storefront_filter_context_required")

    def test_an_empty_selection_is_not_an_error(self):
        self.catalogue()

        self.assertNotIn("errors", self.filtered({}))
        self.assertNotIn("errors", self.filtered({"material": []}))

    # ------------------------------------------------- Phase 28A composition

    def test_the_endpoint_refuses_a_selection_with_no_category(self):
        """Reachable for the first time in Phase 28A, now that `scope_value` is
        optional. Refused rather than ignored: silently dropping the selection
        would return a WIDER page than the buyer asked for."""

        self.catalogue()

        response = self.listing(
            scope_value=None,
            storefront_filters=json.dumps({"material": ["steel"]}))

        self.assertEqual(self.code_of(response), "storefront_filter_context_required")

    def searchable(self, c):
        """Three more products in the same category: steel/hex, steel/washer,
        aluminium/hex. Only one is both steel AND a hex bolt."""

        material = frappe.db.get_value("YOB Storefront Filter", {"filter_key": "material"})
        steel = frappe.db.get_value(
            "YOB Storefront Filter Value", {"value_key": "steel"}, "name")
        aluminium = frappe.db.get_value(
            "YOB Storefront Filter Value", {"value_key": "aluminium"}, "name")
        item_set = frappe.db.get_value(
            "Item", c.products["steel_black"], "custom_storefront_filter_set")

        def product(code, name, value):
            return self.make_item(
                code, category=c.category.name, filter_set=item_set, item_name=name,
                filters=[{"filter": material, "filter_value": value}]).name

        return frappe._dict(
            wanted=product("_R25-SRCH-OK", "Rivsun Hex Bolt", steel),
            wrong_search=product("_R25-SRCH-NO", "Rivsun Washer", steel),
            wrong_filter=product("_R25-SRCH-ALU", "Rivsun Hex Nut", aluminium),
        )

    def test_a_category_a_filter_and_a_search_compose(self):
        """All three narrow the SAME query -- no `/products`-specific search path,
        and no filter path that ignores the search."""

        c = self.catalogue()
        p = self.searchable(c)

        names = self.names(self.filtered({"material": ["steel"]}, search="rivsun hex"))

        self.assertEqual(names, {p.wanted})
        self.assertNotIn(p.wrong_search, names, "the search was ignored")
        self.assertNotIn(p.wrong_filter, names, "the filter was ignored")

    def test_a_filtered_search_matches_the_item_code_too(self):
        """The OR-across-columns rule survives composition with a filter."""

        c = self.catalogue()
        p = self.searchable(c)

        names = self.names(self.filtered({"material": ["steel"]}, search="SRCH-OK"))

        self.assertEqual(names, {p.wanted})

    def test_a_category_and_a_search_without_filters(self):
        c = self.catalogue()
        p = self.searchable(c)

        names = {row["name"] for row in self.data(
            self.listing(scope_value="r25-listing", search="rivsun hex"))["items"]}

        self.assertEqual(names, {p.wanted, p.wrong_filter},
                         "dropping the filter must widen the page, not change it")


class FilteredCursorCase(FilteredListingCase):
    """A cursor belongs to the query that produced it, filters included."""

    def cursor_for(self, selection):
        response = self.filtered(selection, page_size=1)
        return self.data(response)["pagination"]["next_cursor"]

    def test_a_cursor_is_bound_to_its_selection(self):
        self.catalogue()
        cursor = self.cursor_for({"material": ["steel"]})

        self.assertTrue(cursor, "the fixture produced no cursor to test with")

        replayed = self.filtered({"material": ["aluminium"]}, page_size=1, cursor=cursor)

        self.assertEqual(self.code_of(replayed), "cursor_invalid")

    def test_a_cursor_cannot_move_to_another_category(self):
        self.catalogue()
        other_set = self.make_set("R25 Other", [
            frappe.db.get_value("YOB Storefront Filter", {"filter_key": "material"})])
        self.make_category(slug="r25-other", filter_set=other_set.name)

        cursor = self.cursor_for({"material": ["steel"]})

        replayed = self.listing(scope_value="r25-other", page_size=1, cursor=cursor,
                                storefront_filters=json.dumps({"material": ["steel"]}))

        self.assertEqual(self.code_of(replayed), "cursor_invalid")

    def test_a_cursor_survives_a_reordered_but_equivalent_selection(self):
        """`["steel","aluminium"]` and `["aluminium","steel"]` are one query."""

        self.catalogue()
        cursor = self.cursor_for({"material": ["steel", "aluminium"]})

        self.assertTrue(cursor)

        resumed = self.filtered({"material": ["aluminium", "steel"]}, page_size=1,
                                cursor=cursor)

        self.assertNotIn("errors", resumed,
                         "value order changed the logical query")

    def test_a_duplicated_value_does_not_change_the_query(self):
        self.catalogue()
        cursor = self.cursor_for({"material": ["steel"]})

        resumed = self.filtered({"material": ["steel", "steel"]}, page_size=1,
                                cursor=cursor)

        self.assertNotIn("errors", resumed)

    def test_pages_cover_filtered_items_exactly_once(self):
        c = self.catalogue()
        seen = []
        cursor = None

        for _ in range(5):
            response = self.filtered({"material": ["steel", "aluminium"]},
                                     page_size=1, cursor=cursor)
            data = self.data(response)
            seen.extend(row["name"] for row in data["items"])
            cursor = data["pagination"]["next_cursor"]
            if not data["pagination"]["has_more"]:
                break

        expected = {c.products["steel_black"], c.products["steel_chrome"], c.products["alu_black"]}

        self.assertEqual(len(seen), len(set(seen)), "an item appeared on two pages")
        self.assertEqual(set(seen), expected)


# =========================================================
# PAGES AND BLOCKS
# =========================================================

class PageRuntimeCase(RuntimeBase):

    def test_a_published_page_is_returned(self):
        block = self.make_block("Rich Text", content="<p>Hello</p>")
        self.make_page(slug="r25-about", title="About Us",
                       blocks=[{"block": block.name}], meta_title="About | YOB")

        data = self.data(self.page("r25-about"))

        self.assertEqual(data["slug"], "r25-about")
        self.assertEqual(data["title"], "About Us")
        self.assertEqual(data["meta_title"], "About | YOB")
        self.assertEqual(len(data["blocks"]), 1)

    def test_an_unpublished_page_is_unavailable(self):
        self.make_page(slug="r25-draft", enabled=0)

        self.assertEqual(self.code_of(self.page("r25-draft")), "page_not_found")

    def test_an_unknown_page_answers_the_same(self):
        self.assertEqual(self.code_of(self.page("nothing-here")), "page_not_found")

    def test_block_order_is_preserved(self):
        first = self.make_block("Rich Text", block_name="_R25 One", content="<p>1</p>")
        second = self.make_block("Rich Text", block_name="_R25 Two", content="<p>2</p>")
        third = self.make_block("Rich Text", block_name="_R25 Three", content="<p>3</p>")

        self.make_page(slug="r25-ordered", blocks=[
            {"block": third.name, "sequence": 30},
            {"block": first.name, "sequence": 10},
            {"block": second.name, "sequence": 20}])

        blocks = self.data(self.page("r25-ordered"))["blocks"]

        self.assertEqual([b["block_name"] for b in blocks],
                         ["_R25 One", "_R25 Two", "_R25 Three"])

    def test_a_disabled_block_placement_is_skipped(self):
        block = self.make_block("Rich Text", content="<p>Hidden</p>")
        self.make_page(slug="r25-hidden", blocks=[{"block": block.name, "enabled": 0}])

        self.assertEqual(self.data(self.page("r25-hidden"))["blocks"], [])

    def test_every_block_is_discriminated_by_type(self):
        category = self.make_category(slug="r25-blockcat")
        blocks = [
            self.make_block("Image Banner", block_name="_R25 IB",
                            desktop_image="/files/b.png"),
            self.make_block("Rich Text", block_name="_R25 RT", content="<p>x</p>"),
            self.make_block("Banner Carousel", block_name="_R25 BC",
                            slides=[{"desktop_image": "/files/s.png"}]),
            self.make_block("Product Grid", block_name="_R25 PG",
                            storefront_category=category.name, item_limit=6),
            self.make_block("Promo Grid", block_name="_R25 PM", cards_per_row="2",
                            promo_cards=[{"desktop_image": "/files/c.png"}]),
        ]
        self.make_page(slug="r25-all", blocks=[{"block": b.name} for b in blocks])

        types = [b["type"] for b in self.data(self.page("r25-all"))["blocks"]]

        self.assertEqual(types, ["image_banner", "rich_text", "banner_carousel",
                                 "product_grid", "promo_grid"])

    def test_image_banner_shape(self):
        category = self.make_category(slug="r25-banner-cat")
        block = self.make_block(
            "Image Banner", desktop_image="/files/d.png", mobile_image="/files/m.png",
            alt_text="A banner", desktop_height_px=400, mobile_height_px=200,
            link_type="Storefront Category", link_category=category.name)
        self.make_page(slug="r25-banner", blocks=[{"block": block.name}])

        payload = self.data(self.page("r25-banner"))["blocks"][0]

        self.assertEqual(payload["type"], "image_banner")
        self.assertEqual(payload["desktop_image"], "/files/d.png")
        self.assertEqual(payload["mobile_image"], "/files/m.png")
        self.assertEqual(payload["alt_text"], "A banner")
        self.assertEqual(payload["desktop_height_px"], 400)
        self.assertEqual(payload["mobile_height_px"], 200)
        self.assertEqual(payload["destination"]["href"], "/catalog/r25-banner-cat")
        # No other type's fields ride along.
        self.assertNotIn("content", payload)
        self.assertNotIn("items", payload)

    def test_rich_text_shape_and_sanitation(self):
        block = self.make_block(
            "Rich Text", content_title="Notice", text_alignment="Center",
            content='<p>Safe</p><script>alert(1)</script><img src=x onerror="go()">')
        self.make_page(slug="r25-rt", blocks=[{"block": block.name}])

        payload = self.data(self.page("r25-rt"))["blocks"][0]

        self.assertEqual(payload["title"], "Notice")
        self.assertEqual(payload["text_alignment"], "center")
        self.assertIn("Safe", payload["html"])
        self.assertNotIn("<script", payload["html"].lower())
        self.assertNotIn("onerror", payload["html"].lower())

    def test_carousel_shape_and_slide_order(self):
        block = self.make_block(
            "Banner Carousel", auto_play=1, interval_ms=4000,
            desktop_height_px=500,
            slides=[{"desktop_image": "/files/1.png", "title": "One"},
                    {"desktop_image": "/files/2.png", "title": "Two",
                     "link_type": "Catalog"}])
        self.make_page(slug="r25-carousel", blocks=[{"block": block.name}])

        payload = self.data(self.page("r25-carousel"))["blocks"][0]

        self.assertTrue(payload["auto_play"])
        self.assertEqual(payload["interval_ms"], 4000)
        self.assertEqual([s["title"] for s in payload["slides"]], ["One", "Two"])
        self.assertIsNone(payload["slides"][0]["destination"])
        self.assertEqual(payload["slides"][1]["destination"]["href"], "/catalog")

    def test_promo_grid_shape_and_card_order(self):
        block = self.make_block(
            "Promo Grid", cards_per_row="3",
            promo_cards=[{"desktop_image": "/files/a.png", "title": "A"},
                         {"desktop_image": "/files/b.png", "title": "B"}])
        self.make_page(slug="r25-promo", blocks=[{"block": block.name}])

        payload = self.data(self.page("r25-promo"))["blocks"][0]

        self.assertEqual(payload["cards_per_row"], 3)
        self.assertEqual([c["title"] for c in payload["cards"]], ["A", "B"])
        self.assertNotIn("pricing_rule", json.dumps(payload).lower(),
                         "a promo card is content, never ERPNext pricing")

    def test_destination_projection_is_identical_everywhere(self):
        """Menu, banner, slide and card all answer with one shape."""

        category = self.make_category(slug="r25-shared-dest")
        menu = self.make_menu()
        self.make_node(menu.name, "Cat", "Storefront Category",
                       storefront_category=category.name)

        banner = self.make_block("Image Banner", block_name="_R25 D1",
                                 desktop_image="/files/b.png",
                                 link_type="Storefront Category",
                                 link_category=category.name)
        carousel = self.make_block("Banner Carousel", block_name="_R25 D2", slides=[
            {"desktop_image": "/files/s.png", "link_type": "Storefront Category",
             "link_category": category.name}])
        promo = self.make_block("Promo Grid", block_name="_R25 D3", cards_per_row="1",
                                promo_cards=[{"desktop_image": "/files/c.png",
                                              "link_type": "Storefront Category",
                                              "link_category": category.name}])
        self.make_page(slug="r25-dest", blocks=[{"block": b.name}
                                                for b in (banner, carousel, promo)])

        menu_destination = self.data(self.menu("r25-main"))["items"][0]["destination"]
        blocks = self.data(self.page("r25-dest"))["blocks"]

        found = [menu_destination, blocks[0]["destination"],
                 blocks[1]["slides"][0]["destination"], blocks[2]["cards"][0]["destination"]]

        for destination in found:
            self.assertEqual(destination, menu_destination,
                             "one surface projects destinations differently")

    def test_an_internal_route_projects_identically_on_every_surface(self):
        """Phase 28A changed the SHARED projector, so the fix must reach the CMS
        surfaces too -- not only the menu it was found through."""

        menu = self.make_menu()
        self.make_node(menu.name, "Products", "External URL", external_url="/products")

        banner = self.make_block("Image Banner", block_name="_R25 R1",
                                 desktop_image="/files/b.png",
                                 link_type="External URL",
                                 link_external_url="/products")
        carousel = self.make_block("Banner Carousel", block_name="_R25 R2", slides=[
            {"desktop_image": "/files/s.png", "link_type": "External URL",
             "link_external_url": "/products"}])
        promo = self.make_block("Promo Grid", block_name="_R25 R3", cards_per_row="1",
                                promo_cards=[{"desktop_image": "/files/c.png",
                                              "link_type": "External URL",
                                              "link_external_url": "/products"}])
        self.make_page(slug="r25-route", blocks=[{"block": b.name}
                                                 for b in (banner, carousel, promo)])

        menu_destination = self.data(self.menu("r25-main"))["items"][0]["destination"]
        blocks = self.data(self.page("r25-route"))["blocks"]

        found = [menu_destination, blocks[0]["destination"],
                 blocks[1]["slides"][0]["destination"], blocks[2]["cards"][0]["destination"]]

        for destination in found:
            self.assertIsNotNone(destination, "a surface dropped the internal route")
            self.assertEqual(destination["href"], "/products")
            self.assertFalse(destination["external"])


class ProductGridRuntimeCase(RuntimeBase):
    """A grid is answered by the CATALOGUE, never by a second query engine."""

    def grid_page(self, slug="r25-grid", **block_kw):
        category = block_kw.pop("category", None) or self.make_category(slug="r25-gridcat")
        block = self.make_block("Product Grid", storefront_category=category.name,
                                **block_kw)
        self.make_page(slug=slug, blocks=[{"block": block.name}])
        return category, block

    def test_a_grid_returns_listing_cards(self):
        category, _ = self.grid_page(item_limit=6, sort_by="Name A-Z")
        self.make_item("_R25-G1", category=category.name, price=100)
        self.make_item("_R25-G2", category=category.name, price=200)

        payload = self.data(self.page("r25-grid"))["blocks"][0]

        self.assertEqual(payload["type"], "product_grid")
        self.assertEqual(payload["category"], "r25-gridcat")
        self.assertEqual(len(payload["items"]), 2)

        card = payload["items"][0]
        for field in ("name", "item_name", "slug", "has_variants", "price_state",
                      "rate", "uom", "stock_uom", "image"):
            self.assertIn(field, card, f"the grid card lost `{field}` from ListingCard")

    def test_a_grid_calls_the_existing_listing_service(self):
        from yob_storefront.services import content_service

        category, _ = self.grid_page()
        self.make_item("_R25-G3", category=category.name, price=100)

        with patch.object(content_service, "__name__", content_service.__name__):
            from yob_storefront.services import catalog_listing_service as svc
            real = svc.list_items
            calls = []

            def spy(*args, **kwargs):
                calls.append(args)
                return real(*args, **kwargs)

            with patch.object(svc, "list_items", side_effect=spy):
                self.data(self.page("r25-grid"))

        self.assertEqual(len(calls), 1, "the grid did not use the catalogue service once")

    def test_a_simple_item_is_priced_normally(self):
        category, _ = self.grid_page()
        self.make_item("_R25-G4", category=category.name, price=250)

        card = self.data(self.page("r25-grid"))["blocks"][0]["items"][0]

        self.assertEqual(card["price_state"], "priced")
        self.assertEqual(card["rate"], 250)
        self.assertEqual(card["has_variants"], 0)

    def test_a_variant_family_carries_no_fabricated_price(self):
        from erpnext.controllers.item_variant import create_variant

        attribute = frappe.db.get_value("Item Attribute", {"name": "Size"}, "name")
        if not attribute:
            self.skipTest("no Item Attribute on this bench")

        value = frappe.db.get_value("Item Attribute Value", {"parent": attribute},
                                    "attribute_value")
        category, _ = self.grid_page()

        template = self.make_item("_R25-GFAM", category=category.name, price=None,
                                  has_variants=1, custom_slug="r25-gfam",
                                  attributes=[{"attribute": attribute}])
        variant = create_variant(template.name, {attribute: value})
        variant.insert(ignore_permissions=True)
        frappe.get_doc({"doctype": "Item Price", "item_code": variant.name,
                        "price_list": self.price_list, "price_list_rate": 999,
                        "selling": 1, "uom": self.uom}).insert(ignore_permissions=True)

        cards = self.data(self.page("r25-grid"))["blocks"][0]["items"]
        family = next(c for c in cards if c["name"] == template.name)

        self.assertEqual(family["price_state"], "select_options")
        self.assertIsNone(family["rate"], "a child variant's price was fabricated")
        self.assertNotIn(variant.name, [c["name"] for c in cards],
                         "a generated variant was listed as its own card")

    def test_the_configured_limit_is_respected(self):
        category, _ = self.grid_page(item_limit=2)
        for i in range(4):
            self.make_item(f"_R25-LIM{i}", category=category.name, price=100)

        items = self.data(self.page("r25-grid"))["blocks"][0]["items"]

        self.assertEqual(len(items), 2)

    def test_an_empty_category_returns_an_empty_grid(self):
        self.grid_page()

        payload = self.data(self.page("r25-grid"))["blocks"][0]

        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["category"], "r25-gridcat")

    def test_a_category_disabled_later_never_falls_back_to_everything(self):
        category, _ = self.grid_page()
        self.make_item("_R25-GONE", category=category.name, price=100)
        frappe.db.set_value("Category", category.name, "is_active", 0)

        payload = self.data(self.page("r25-grid"))["blocks"][0]

        self.assertEqual(payload["items"], [], "a broken grid merchandised everything")
        self.assertIsNone(payload["category"])

    def test_a_category_turned_into_a_group_is_refused_safely(self):
        category, _ = self.grid_page()
        frappe.db.set_value("Category", category.name, "is_group", 1)

        payload = self.data(self.page("r25-grid"))["blocks"][0]

        self.assertEqual(payload["items"], [])

    def test_the_grid_contains_no_pricing_logic_of_its_own(self):
        """Scans EXECUTABLE code: the module explains at length what it does NOT
        do, and a text scan would force that explanation out of the file."""

        import ast as _ast
        import inspect as _inspect

        from yob_storefront.services import content_service

        tree = _ast.parse(_inspect.getsource(content_service))

        for node in _ast.walk(tree):
            body = getattr(node, "body", None)
            if (isinstance(node, (_ast.Module, _ast.FunctionDef, _ast.AsyncFunctionDef,
                                  _ast.ClassDef))
                    and body and isinstance(body[0], _ast.Expr)
                    and isinstance(body[0].value, _ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)

        source = _ast.unparse(tree)

        for forbidden in ("Item Price", "get_price_list_rate_for", "Pricing Rule",
                          "calculate_taxes_and_totals", "conversion_factor",
                          "get_bin_details"):
            self.assertNotIn(forbidden, source,
                             f"content_service implements `{forbidden}` itself")

    def test_grid_pricing_is_customer_specific(self):
        """Two customers, two price lists, two answers -- so no global caching."""

        other = frappe.db.get_value(
            "Customer", {"name": ["!=", CUSTOMER]}, "name")
        if not other:
            self.skipTest("only one Customer on this bench")

        category, _ = self.grid_page()
        item = self.make_item("_R25-CUST", category=category.name, price=100)

        premium = frappe.get_doc({
            "doctype": "Price List", "price_list_name": "_R25 Premium",
            "selling": 1, "enabled": 1, "currency": "INR"}).insert(ignore_permissions=True)
        frappe.get_doc({"doctype": "Item Price", "item_code": item.name,
                        "price_list": premium.name, "price_list_rate": 777,
                        "selling": 1, "uom": self.uom}).insert(ignore_permissions=True)
        frappe.db.set_value("Customer", other, "default_price_list", premium.name)

        first = self.data(self.page("r25-grid"))["blocks"][0]["items"][0]["rate"]

        with patch.object(self.cms, "get_storefront_customer",
                          return_value=frappe.get_doc("Customer", other)):
            frappe.clear_cache()
            second = self.data(self.page("r25-grid"))["blocks"][0]["items"][0]["rate"]

        self.assertEqual(first, 100)
        self.assertEqual(second, 777,
                         "a second customer received the first customer's price")


# =========================================================
# PUBLISHED SHAPE (Phase 25C-1)
# =========================================================

class PublishedBlockShapeCase(RuntimeBase):
    """The published block schemas must describe the blocks production emits.

    The Phase 24D-1 guard checks that every endpoint and error code is published;
    it says nothing about SHAPE, and Phase 25C shipped `slides` and `cards` as
    bare `array<object>` -- documented as "an array of something". A client cannot
    generate a DTO from that, so it hand-writes one and the two drift silently.

    This asserts against blocks that were actually PROJECTED, not against the
    source: a projector that starts emitting a field nobody published fails here,
    which is the drift that matters.
    """

    HANDOFF = (pathlib.Path(frappe.get_app_path("yob_storefront")).parent
               / "frontend-api-handoff" / "openapi.json")

    def schemas(self):
        if not self.HANDOFF.exists():
            self.skipTest("no published OpenAPI document in this checkout")
        return json.loads(self.HANDOFF.read_text())["components"]["schemas"]

    def every_block(self):
        """One page carrying all five types, each fully populated."""

        category = self.make_category(slug="r25-shape-cat")
        media = {"desktop_image": "/files/d.png", "mobile_image": "/files/m.png",
                 "title": "Card", "alt_text": "alt", "link_type": "Catalog"}

        blocks = [
            self.make_block("Image Banner", block_name="_R25 SHAPE IB",
                            desktop_image="/files/d.png", mobile_image="/files/m.png",
                            alt_text="alt", desktop_height_px=400,
                            mobile_height_px=200, link_type="Catalog"),
            self.make_block("Rich Text", block_name="_R25 SHAPE RT",
                            content_title="T", content="<p>x</p>",
                            text_alignment="Center"),
            self.make_block("Banner Carousel", block_name="_R25 SHAPE BC",
                            auto_play=1, interval_ms=4000, desktop_height_px=400,
                            mobile_height_px=200, slides=[dict(media)]),
            self.make_block("Promo Grid", block_name="_R25 SHAPE PM",
                            cards_per_row="3", desktop_height_px=400,
                            mobile_height_px=200, promo_cards=[dict(media)]),
            self.make_block("Product Grid", block_name="_R25 SHAPE PG",
                            storefront_category=category.name, item_limit=6),
        ]
        self.make_page(slug="r25-shape", blocks=[{"block": b.name} for b in blocks])

        return {b["type"]: b for b in self.data(self.page("r25-shape"))["blocks"]}

    def test_every_type_carries_exactly_the_published_fields(self):
        """`x-block-fields` is the contract; the projection is the truth.

        Compared as a whole map, so a field the runtime gained AND a field the
        contract invented both fail -- and the failure names the type.
        """

        block_schema = self.schemas()["ContentBlock"]
        published = block_schema["x-block-fields"]
        always = set(block_schema["x-block-always-present"])
        blocks = self.every_block()

        self.assertEqual(set(blocks), set(published),
                         "the published block types are not the types projected")

        for block_type, block in blocks.items():
            self.assertEqual(
                set(block) - always, set(published[block_type]),
                f"{block_type} does not carry the fields `x-block-fields` publishes")

            self.assertEqual(always & set(block), always,
                             f"{block_type} is missing a field every block must have")

    def test_no_block_field_is_published_without_a_schema(self):
        properties = self.schemas()["ContentBlock"]["properties"]

        for block_type, block in self.every_block().items():
            undocumented = sorted(set(block) - set(properties))

            self.assertEqual(
                undocumented, [],
                f"{block_type} emits fields absent from the published ContentBlock")

    def test_slides_and_cards_have_their_own_schemas(self):
        schemas = self.schemas()
        blocks = self.every_block()

        for prop, schema_name, block_type in (
                ("slides", "BannerCarouselSlide", "banner_carousel"),
                ("cards", "PromoCard", "promo_grid")):

            items = schemas["ContentBlock"]["properties"][prop]["items"]
            self.assertEqual(
                items, {"$ref": f"#/components/schemas/{schema_name}"},
                f"`{prop}` is still an anonymous object; a client cannot type it")

            row = blocks[block_type][prop][0]
            schema = schemas[schema_name]

            self.assertEqual(set(row), set(schema["properties"]),
                             f"{schema_name} does not describe what {prop} carries")
            self.assertEqual(set(schema["required"]), set(row),
                             f"{schema_name} must mark every always-present key required")

    def test_height_fields_are_published_for_exactly_the_types_that_carry_them(self):
        published = self.schemas()["ContentBlock"]["x-block-fields"]
        blocks = self.every_block()

        for field in ("desktop_height_px", "mobile_height_px"):
            documented = {t for t, fields in published.items() if field in fields}
            actual = {t for t, block in blocks.items() if field in block}

            self.assertEqual(
                documented, actual,
                f"{field}: published for {sorted(documented)}, returned for "
                f"{sorted(actual)}")

            self.assertEqual(
                actual, {"image_banner", "banner_carousel", "promo_grid"},
                f"{field} moved to another block type; the contract needs a decision, "
                f"not a silent update")

    def test_a_grid_is_never_given_a_merchant_height(self):
        """The one asymmetry worth pinning: a grid is sized by its cards."""

        grid = self.every_block()["product_grid"]

        self.assertNotIn("desktop_height_px", grid)
        self.assertNotIn("mobile_height_px", grid)
