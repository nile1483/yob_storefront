# Copyright (c) 2026, YOB and Shayona
"""The app must install itself completely (Phase 25B).

THE DEFECT THIS CLOSES
----------------------
`Item.custom_slug` and `Item.custom_category` were created BY HAND on the
existing benches. The Custom Field fixture in `hooks.py` is commented out and
nothing else installed them, so a fresh `yob_storefront` install had no slug and
no category field -- and every Phase 22-24 catalog path silently depends on both
(`get_item` looks products up by `custom_slug`, the listing filters on
`custom_category`). Phase 25A found it; `install.ensure_custom_fields()` now owns
all of them, and this file proves it.

The installer must also be safe to run on a site that already has data, which is
the harder half: `create_custom_fields` updates a field DEFINITION but must never
touch stored VALUES.
"""

import unittest

import frappe

EXPECTED_ITEM_FIELDS = {
    "custom_slug": "Data",
    "custom_category": "Link",
    "custom_storefront_tab": "Tab Break",
    "custom_storefront_filter_set": "Link",
    "custom_storefront_filters": "Table",
}

EXPECTED_DOCTYPES = (
    "YOB Storefront Filter",
    "YOB Storefront Filter Value",
    "YOB Storefront Filter Set",
    "YOB Storefront Filter Set Filter",
    "YOB Storefront Item Filter",
    "YOB Storefront Menu",
    "YOB Storefront Menu Item",
    "YOB Storefront Page",
    "YOB Storefront Page Block",
    "YOB Storefront Block",
    "YOB Storefront Block Slide",
    "YOB Storefront Block Promo Card",
)


class InstallCase(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()

    def test_every_storefront_doctype_is_app_owned(self):
        for doctype in EXPECTED_DOCTYPES:
            with self.subTest(doctype=doctype):
                row = frappe.db.get_value(
                    "DocType", doctype, ["module", "custom"], as_dict=True)

                self.assertIsNotNone(row, f"{doctype} is missing after migrate")
                self.assertEqual(row.module, "yob_storefront")
                self.assertFalse(row.custom,
                                 f"{doctype} is a Customize-Form artefact, not app-owned")

    def test_item_custom_fields_exist(self):
        for fieldname, fieldtype in EXPECTED_ITEM_FIELDS.items():
            with self.subTest(field=fieldname):
                field = frappe.get_meta("Item").get_field(fieldname)

                self.assertIsNotNone(field, f"Item.{fieldname} was never installed")
                self.assertEqual(field.fieldtype, fieldtype)

    def test_the_public_slug_is_not_mandatory(self):
        """Phase 24B: a generated variant carries no slug, so `reqd` would block it."""

        self.assertFalse(frappe.get_meta("Item").get_field("custom_slug").reqd)

    def test_category_carries_its_own_filter_set(self):
        field = frappe.get_meta("Category").get_field("storefront_filter_set")

        self.assertIsNotNone(field)
        self.assertEqual(field.options, "YOB Storefront Filter Set")

    def test_the_installer_is_idempotent(self):
        """Running setup twice changes nothing a second time."""

        from yob_storefront.install import ensure_custom_fields

        before = frappe.db.get_all(
            "Custom Field", filters={"dt": "Item"},
            fields=["fieldname", "fieldtype", "options", "reqd", "insert_after"],
            order_by="fieldname")

        ensure_custom_fields()
        frappe.clear_cache(doctype="Item")

        after = frappe.db.get_all(
            "Custom Field", filters={"dt": "Item"},
            fields=["fieldname", "fieldtype", "options", "reqd", "insert_after"],
            order_by="fieldname")

        self.assertEqual(after, before, "a second run changed the field definitions")

    def test_reinstalling_preserves_stored_values(self):
        """The half that matters on a live site: data survives a re-run."""

        from yob_storefront.install import ensure_custom_fields

        item = frappe.db.get_value(
            "Item", {"custom_slug": ["!=", ""]}, ["name", "custom_slug", "custom_category"],
            as_dict=True)

        if not item:
            self.skipTest("no slugged item on this bench")

        ensure_custom_fields()

        self.assertEqual(
            frappe.db.get_value("Item", item.name, ["custom_slug", "custom_category"],
                                as_dict=True),
            {"custom_slug": item.custom_slug, "custom_category": item.custom_category},
            "the installer rewrote stored catalogue data")

    def test_desk_behaviour_ships_as_app_files_not_client_scripts(self):
        """A Client Script is mutable site data; these must be app-owned files."""

        import pathlib

        from yob_storefront import hooks

        self.assertIn("Item", hooks.doctype_js)
        self.assertIn("YOB Storefront Menu Item", hooks.doctype_tree_js)

        app_root = pathlib.Path(frappe.get_app_path("yob_storefront"))

        for relative in list(hooks.doctype_js.values()) + list(hooks.doctype_tree_js.values()):
            self.assertTrue((app_root / relative).exists(), f"{relative} is missing")

        self.assertFalse(
            frappe.db.exists("Client Script", {"dt": "Item", "enabled": 1}),
            "an Item Client Script exists; storefront Desk logic must ship as app files")

    #: The Phase 25 administration, as it must appear in BOTH Desk structures.
    PHASE_25_GROUPS = ("Catalog Filters", "Navigation", "Content")
    PHASE_25_DOCTYPES = (
        "YOB Storefront Filter", "YOB Storefront Filter Value",
        "YOB Storefront Filter Set", "YOB Storefront Menu",
        "YOB Storefront Menu Item", "YOB Storefront Page", "YOB Storefront Block",
    )

    def test_the_workspace_page_exposes_the_new_administration(self):
        """The CENTRE of the Workspace page: `Workspace Link` cards.

        This is only half of Desk. See the sidebar test below -- the two are
        different DocTypes fed by different files, and passing this one says
        nothing at all about the left sidebar.
        """

        links = frappe.get_all(
            "Workspace Link", filters={"parent": "YOB Storefront"},
            fields=["label", "type", "link_to"], order_by="idx")

        cards = {row.label for row in links if row.type == "Card Break"}
        targets = {row.link_to for row in links if row.type == "Link"}

        for card in self.PHASE_25_GROUPS:
            self.assertIn(card, cards, f"the workspace page has no {card} section")

        for doctype in self.PHASE_25_DOCTYPES:
            self.assertIn(doctype, targets,
                          f"{doctype} is not reachable from the workspace page")

    def test_the_left_sidebar_exposes_the_new_administration(self):
        """The LEFT DESK SIDEBAR: `Workspace Sidebar` / `Workspace Sidebar Item`.

        THE DEFECT THIS CLOSES
        ----------------------
        Phase 25B reported that "the existing Workspace gained Catalog Filters,
        Navigation and Content sections", and its test agreed -- but the test
        only ever read `Workspace Link`, the cards drawn in the MIDDLE of the
        workspace page. Frappe v16 draws the left sidebar from an entirely
        separate DocType, loaded from a separate app-level file
        (`yob_storefront/workspace_sidebar/yob_storefront.json`), which Phase 25B
        never touched. Both structures were internally consistent, so nothing
        failed; the sidebar simply never gained the three groups, and on
        `yob.localhost` it still showed the pre-Phase-25 list.

        Asserting the same labels against the OTHER DocType is the whole point.
        """

        items = frappe.get_all(
            "Workspace Sidebar Item", filters={"parent": "YOB Storefront"},
            fields=["label", "type", "link_to"], order_by="idx")

        self.assertTrue(items, "the storefront has no left sidebar at all")

        groups = {row.label for row in items if row.type == "Section Break"}
        targets = {row.link_to for row in items if row.type == "Link"}

        for group in self.PHASE_25_GROUPS:
            self.assertIn(group, groups, f"the LEFT SIDEBAR has no {group} group")

        for doctype in self.PHASE_25_DOCTYPES:
            self.assertIn(doctype, targets,
                          f"{doctype} is not reachable from the LEFT SIDEBAR")

    def test_the_sidebar_keeps_everything_it_had_before_phase_25(self):
        """Adding groups must not quietly drop the ones already there."""

        items = frappe.get_all(
            "Workspace Sidebar Item", filters={"parent": "YOB Storefront"},
            fields=["label", "type", "link_to"], order_by="idx")

        groups = {row.label for row in items if row.type == "Section Break"}
        targets = {row.link_to for row in items if row.type == "Link"}

        for group in ("Catalog", "Orders", "Payments"):
            self.assertIn(group, groups, f"the pre-existing {group} group was lost")

        for doctype in ("YOB Store Settings", "Category", "Item", "Item Price",
                        "Pricing Rule", "Cart", "Customer", "Payment Method",
                        "Payment Method Assignment", "Razorpay Payment Log"):
            self.assertIn(doctype, targets, f"the pre-existing {doctype} link was lost")

    def test_the_sidebar_source_is_app_owned_and_synchronises(self):
        """The file must be where Frappe looks, and newer than the stored row.

        `sync_for` reads `workspace_sidebar` as an APP-LEVEL folder, and
        `import_file_by_path` SKIPS a non-DocType record whose file `modified` is
        not newer than the row already in the database. An edit that forgets to
        bump the timestamp installs on a fresh site and silently does nothing on
        every existing one -- which is exactly the failure mode a merchant sees.
        """

        import json
        import pathlib

        path = (pathlib.Path(frappe.get_app_path("yob_storefront"))
                / "workspace_sidebar" / "yob_storefront.json")

        self.assertTrue(path.exists(),
                        "the sidebar is not at the app-level path Frappe imports")

        source = json.loads(path.read_text())

        self.assertEqual(source["doctype"], "Workspace Sidebar")
        self.assertEqual(source["name"], "YOB Storefront")

        source_groups = {i.get("label") for i in source["items"]
                         if i.get("type") == "Section Break"}
        for group in self.PHASE_25_GROUPS:
            self.assertIn(group, source_groups, f"the SOURCE file has no {group} group")

        stored = frappe.db.get_value("Workspace Sidebar", "YOB Storefront", "modified")

        if stored:
            self.assertLessEqual(
                frappe.utils.get_datetime(stored),
                frappe.utils.get_datetime(source["modified"]),
                "the stored sidebar is NEWER than the source file, so `bench migrate` "
                "will skip it -- bump `modified` in the JSON")

    def test_only_one_storefront_workspace_exists(self):
        self.assertEqual(
            frappe.db.count("Workspace", {"module": "yob_storefront"}), 1,
            "a competing workspace appeared")


if __name__ == "__main__":
    unittest.main()
