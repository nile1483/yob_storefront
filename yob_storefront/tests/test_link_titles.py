# Copyright (c) 2026, YOB and Shayona
"""Human-readable Desk titles for hash-named DocTypes (Phase 32G).

WHAT THIS PHASE ASSERTS
-----------------------
A DocType whose `name` is a random hash must not force a Desk user to read
`en27ns29k8` where a business title exists. Frappe answers this natively with
two pieces of DocType metadata:

    title_field                 which field carries the human title
    show_title_field_in_link    render that title wherever this DocType is
                                the target of a Link

and `name` is left completely alone. The stored Link value, the document route,
the API payload and every database reference keep the hash. Desk may show
`Banner Image` while the Link column stores `h3f0q0oo3d`; that is the design,
not a leak.

WHY ONLY THESE FIVE
-------------------
`show_title_field_in_link` is consulted ONLY on link-target paths -- verified
against Frappe 16.53 source: `desk/search.py` (link search + autosuggest +
`get_link_title`), `desk/listview.py` (link columns in a list view),
`desk/link_preview.py`, `desk/form/load.py`, `www/printview.py` and
`core/doctype/version` (audit diffs). So the flag earns its keep exactly where

    the name is a hash   AND   a field holds a real business title

and the three DocTypes that are also the target of a visible Link field
(`YOB Storefront Block`, `YOB Storefront Filter Value`,
`YOB Storefront Menu Item`) are where a buyer-facing admin actually reads the
hash today -- each of those inbound Link fields is `in_list_view`, so the hash
is on screen in a list column right now.

The other two (`YOB Customer Target`,
`YOB Storefront Product Content Section`) had NO `title_field` at all, so their
own list views and form headings showed the hash. `title_field` fixes that on
its own; the flag is set alongside it for consistency and so that adding an
inbound Link later needs no metadata change. That it is inert for them TODAY is
deliberate and recorded here rather than left to be rediscovered.

Everything else is excluded on purpose, and `IntentionalExclusionCase` says why.
"""

import unittest
from unittest.mock import patch

import frappe

#: DocType -> the field Phase 32G approved as its Desk title.
#:
#: Every one of these is `reqd = 1` on its DocType, which is what makes the
#: title safe: a required field cannot be blank on a normal record, so no
#: existing document can come back as an empty Link label. `TitleFieldIsSafeCase`
#: re-asserts that rather than trusting this comment.
APPROVED = {
    "YOB Storefront Block": "block_name",
    "YOB Storefront Filter Value": "value",
    "YOB Storefront Menu Item": "label",
    "YOB Customer Target": "customer",
    "YOB Storefront Product Content Section": "title",
}

#: Storefront DocTypes deliberately left without the flag, and the reason.
#:
#: Carried as data so the reason travels with the assertion. A future phase that
#: wants to promote one of these has to delete its line here, which is a visible
#: decision rather than a silent default.
EXCLUDED = {
    "Razorpay Payment Log":
        "payment technical/audit record; not a Desk Link target and no business title",
    "YOB Storefront Content Placement":
        "no suitable standalone title -- route_key/slot_key are generic Selects and "
        "`block` is itself a Link, so any label would describe something else",
    "Cart":
        "named CART-YYYY-MM-#### -- already the useful business identifier",
    "Category":
        "named by category_name -- the name IS the title",
    "Payment Method":
        "named by payment_method_name -- the name IS the title",
    "YOB Storefront Page":
        "named by its public slug, which is the identity the storefront routes on",
    "YOB Storefront Menu":
        "named by menu_key, the identity `cms.get_menu` is called with",
    "YOB Storefront Filter":
        "named by filter_key, the identity a filter selection is sent as",
    "YOB Target Rule":
        "named by rule_name -- the name IS the title",
    "YOB Target Period":
        "named by period_name -- the name IS the title",
}

#: Fieldtypes that must never become a Desk title.
#:
#: A Select is a status or a generic type, a Date alone identifies nothing, a
#: Text Editor is HTML, and a Password/Currency/Int is either sensitive or
#: meaningless out of context. Phase 32G chose only `Data` and `Link` fields.
FORBIDDEN_TITLE_TYPES = {
    "Select", "Date", "Datetime", "Time", "Text Editor", "Code", "JSON",
    "Password", "Currency", "Float", "Int", "Percent", "Check", "Attach",
    "Attach Image", "Signature", "Barcode", "Geolocation",
}


def _app_doctypes():
    """Every DocType this app owns, read from the live schema."""

    return frappe.get_all("DocType", filters={"module": "YOB Storefront"}, pluck="name")


# =========================================================
# 1. THE APPROVED METADATA
# =========================================================

class ApprovedTitleMetadataCase(unittest.TestCase):
    """Source-controlled DocType JSON, as the runtime actually loaded it.

    Read through `frappe.get_meta` rather than the JSON file, because the point
    is that `bench migrate` applied it -- a correct file that never reached the
    database would leave production needing a manual Customize Form change,
    which this phase explicitly refuses to require.
    """

    def test_every_approved_doctype_has_its_title_field(self):
        for doctype, field in APPROVED.items():
            with self.subTest(doctype=doctype):
                meta = frappe.get_meta(doctype)
                self.assertEqual(
                    meta.title_field, field,
                    f"{doctype} should title on {field!r}, found {meta.title_field!r}")

    def test_every_approved_doctype_shows_the_title_in_links(self):
        for doctype in APPROVED:
            with self.subTest(doctype=doctype):
                self.assertEqual(
                    frappe.get_meta(doctype).show_title_field_in_link, 1,
                    f"{doctype} has a title_field but Desk will still render the hash")

    def test_the_title_field_actually_exists_on_the_doctype(self):
        """A title_field naming a field that was renamed away is silently blank."""

        for doctype, field in APPROVED.items():
            with self.subTest(doctype=doctype):
                self.assertIsNotNone(
                    frappe.get_meta(doctype).get_field(field),
                    f"{doctype}.{field} does not exist")

    def test_approved_doctypes_are_the_hash_named_ones(self):
        """The flag is earned by having an unreadable name, not by being nice.

        If someone changes one of these to `autoname: field:...` the name becomes
        the title and this metadata is redundant -- worth noticing, because two
        sources of the same truth drift.
        """

        for doctype in APPROVED:
            with self.subTest(doctype=doctype):
                autoname = frappe.db.get_value("DocType", doctype, "autoname")
                self.assertEqual(
                    autoname, "hash",
                    f"{doctype} is no longer hash-named (autoname={autoname!r}); "
                    "re-check whether it still needs a title_field at all")


# =========================================================
# 2. THE TITLE IS SAFE TO SHOW
# =========================================================

class TitleFieldIsSafeCase(unittest.TestCase):
    """A title must be populated, readable, and not something private."""

    def test_every_approved_title_field_is_required(self):
        """Required is what guarantees no record renders as a blank label.

        This is the whole existing-data argument: a field that cannot be empty on
        a normal save cannot produce an empty Link label on an old record either,
        so no cosmetic data migration is needed.
        """

        for doctype, field in APPROVED.items():
            with self.subTest(doctype=doctype):
                self.assertTrue(
                    frappe.get_meta(doctype).get_field(field).reqd,
                    f"{doctype}.{field} is optional -- blank rows would render as "
                    "empty Link labels")

    def test_no_approved_title_field_has_a_forbidden_type(self):
        for doctype, field in APPROVED.items():
            with self.subTest(doctype=doctype):
                fieldtype = frappe.get_meta(doctype).get_field(field).fieldtype
                self.assertNotIn(
                    fieldtype, FORBIDDEN_TITLE_TYPES,
                    f"{doctype}.{field} is a {fieldtype}; a status, a date or a blob "
                    "does not identify a document")

    def test_no_storefront_doctype_titles_on_a_forbidden_type(self):
        """Including any DocType a later phase adds, not just this phase's five."""

        for doctype in _app_doctypes():
            meta = frappe.get_meta(doctype)
            if not meta.title_field:
                continue
            field = meta.get_field(meta.title_field)
            if not field:
                continue
            with self.subTest(doctype=doctype):
                self.assertNotIn(
                    field.fieldtype, FORBIDDEN_TITLE_TYPES,
                    f"{doctype} titles on {meta.title_field} ({field.fieldtype})")

    def test_no_title_field_names_anything_secret(self):
        """Nothing credential-shaped may become a visible Desk label."""

        banned = ("secret", "token", "password", "otp", "hash", "salt",
                  "signature", "api_key", "private")

        for doctype in _app_doctypes():
            meta = frappe.get_meta(doctype)
            if not meta.title_field:
                continue
            with self.subTest(doctype=doctype):
                hits = [w for w in banned if w in meta.title_field.lower()]
                self.assertFalse(
                    hits, f"{doctype} titles on {meta.title_field!r} ({hits})")


# =========================================================
# 3. THE DELIBERATE EXCLUSIONS
# =========================================================

class IntentionalExclusionCase(unittest.TestCase):
    """Logs, technical records and already-readable names stay as they are.

    These assertions are the counterweight to the ones above: without them the
    tidy answer to "titles are nicer" is to set the flag on all 38 DocTypes,
    which would put an audit log's generic type into a Link label and tell a
    merchant nothing.
    """

    def test_excluded_doctypes_do_not_show_a_title_in_links(self):
        for doctype, reason in EXCLUDED.items():
            with self.subTest(doctype=doctype):
                self.assertFalse(
                    frappe.get_meta(doctype).show_title_field_in_link,
                    f"{doctype} was excluded because: {reason}")

    def test_the_payment_log_has_no_title_field_at_all(self):
        """A payment record's fields are provider/technical state, not a label."""

        self.assertIsNone(frappe.get_meta("Razorpay Payment Log").title_field)

    def test_content_placement_has_no_title_field(self):
        """Its own fields describe WHERE it sits, never WHAT it is.

        `route_key` and `slot_key` are Selects shared by every placement on that
        route, so a label built from them would read `home / hero` on several
        different rows while claiming to identify one.
        """

        self.assertIsNone(frappe.get_meta("YOB Storefront Content Placement").title_field)

    def test_readable_names_were_not_given_redundant_titles(self):
        """A DocType named by a business field needs no second title source."""

        for doctype in ("Category", "Payment Method", "YOB Target Rule",
                        "YOB Target Period", "YOB Storefront Menu",
                        "YOB Storefront Filter", "YOB Storefront Page"):
            with self.subTest(doctype=doctype):
                autoname = frappe.db.get_value("DocType", doctype, "autoname") or ""
                self.assertTrue(
                    autoname.startswith("field:") or autoname.startswith("CART-"),
                    f"{doctype} autoname is {autoname!r} -- if it became a hash it now "
                    "needs a title_field")

    def test_child_tables_and_singles_are_never_given_the_flag(self):
        """Nothing links to a child row, and a Single has exactly one record."""

        for doctype in _app_doctypes():
            meta = frappe.get_meta(doctype)
            if not (meta.istable or meta.issingle):
                continue
            with self.subTest(doctype=doctype):
                self.assertFalse(
                    meta.show_title_field_in_link,
                    f"{doctype} is a child table or Single; a Link title is meaningless")


# =========================================================
# 4. IDENTITY IS UNCHANGED -- THE FUNCTIONAL PROOF
# =========================================================

PLACEMENT = "YOB Storefront Content Placement"


class LinkIdentityCase(unittest.TestCase):
    """A title is a LABEL. The Link value, and the record, stay the hash.

    This is the assertion that makes the whole phase safe to ship: if a title
    ever became the stored value, every existing Link row would break and a
    renamed title would silently repoint a reference.
    """

    def setUp(self):
        frappe.set_user("Administrator")
        self.commits = []
        commit_patch = patch.object(
            frappe.db, "commit", side_effect=lambda *a, **k: self.commits.append(1))
        commit_patch.start()
        self.addCleanup(commit_patch.stop)

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()
        self.assertEqual(self.commits, [], "a commit escaped the test rollback")

    def make_block(self, block_name):
        return frappe.get_doc({
            "doctype": "YOB Storefront Block", "block_name": block_name,
            "block_type": "Rich Text", "content": "<p>32G</p>", "enabled": 1,
        }).insert(ignore_permissions=True)

    def test_the_document_name_is_still_a_hash(self):
        block = self.make_block("_32G Readable Title")

        self.assertNotEqual(block.name, "_32G Readable Title")
        self.assertEqual(block.block_name, "_32G Readable Title")
        # Frappe's random names are lowercase alphanumerics of a fixed length.
        self.assertTrue(block.name.isalnum() and block.name.islower(), block.name)

    def test_a_link_field_stores_the_name_not_the_title(self):
        block = self.make_block("_32G Stored Value")

        placement = frappe.get_doc({
            "doctype": PLACEMENT, "route_key": "home", "slot_key": "hero",
            "block": block.name, "enabled": 1,
        }).insert(ignore_permissions=True)

        self.assertEqual(placement.block, block.name)
        self.assertEqual(
            frappe.db.get_value(PLACEMENT, placement.name, "block"), block.name,
            "the stored Link value is not the document name")
        self.assertNotEqual(placement.block, block.block_name)

    def test_the_displayed_title_comes_from_the_title_field(self):
        """`get_link_title` is what Desk calls to label a Link value."""

        from frappe.desk.search import get_link_title

        block = self.make_block("_32G Displayed Title")

        self.assertEqual(
            get_link_title("YOB Storefront Block", block.name),
            "_32G Displayed Title")

    def test_a_doctype_without_the_flag_still_displays_its_name(self):
        """The control case: the flag is what changes the label, nothing else."""

        from frappe.desk.search import get_link_title

        block = self.make_block("_32G Control")
        placement = frappe.get_doc({
            "doctype": PLACEMENT, "route_key": "home", "slot_key": "main",
            "block": block.name, "enabled": 1,
        }).insert(ignore_permissions=True)

        # Content Placement is deliberately excluded, so it labels as its hash.
        self.assertEqual(get_link_title(PLACEMENT, placement.name), placement.name)

    def test_duplicate_titles_do_not_collide_or_merge(self):
        """Two Blocks may legitimately share a title; they remain two documents.

        Duplicate titles are explicitly acceptable in this design precisely
        because identity never moved to the title. If it had, this is the test
        that would fail.
        """

        first = self.make_block("_32G Same Name")
        second = self.make_block("_32G Same Name")

        self.assertNotEqual(first.name, second.name)
        self.assertEqual(first.block_name, second.block_name)

        here = frappe.get_doc({
            "doctype": PLACEMENT, "route_key": "home", "slot_key": "hero",
            "block": first.name, "enabled": 1,
        }).insert(ignore_permissions=True)
        there = frappe.get_doc({
            "doctype": PLACEMENT, "route_key": "home", "slot_key": "bottom",
            "block": second.name, "enabled": 1,
        }).insert(ignore_permissions=True)

        self.assertNotEqual(here.block, there.block,
                            "two identically titled Blocks were conflated")
        self.assertEqual(frappe.db.get_value(PLACEMENT, here.name, "block"), first.name)
        self.assertEqual(frappe.db.get_value(PLACEMENT, there.name, "block"), second.name)

    def test_link_search_finds_the_document_by_its_title(self):
        """Frappe searches `title_field` natively -- no `search_fields` needed.

        `desk/search.py` seeds its or-filters with `["name"] + [title_field]`
        before it ever looks at `search_fields`, which is why Phase 32G added no
        `search_fields` anywhere. Asserted rather than taken on trust, because
        a title nobody can search for is only half the feature.
        """

        from frappe.desk.search import search_link

        block = self.make_block("_32GUniqueSearchable")

        results = search_link(doctype="YOB Storefront Block", txt="_32GUniqueSearchable")

        values = [r.get("value") for r in results]
        self.assertIn(block.name, values,
                      "link search could not find the Block by its title")

        # `value` is the identity the Link stores; `label` is only what is shown.
        row = next(r for r in results if r.get("value") == block.name)
        self.assertEqual(row.get("label"), "_32GUniqueSearchable",
                         "link search returned the hash as the label")
        self.assertEqual(row["value"], block.name,
                         "link search offered the title as the value to store")

    def test_renaming_the_title_does_not_move_the_link(self):
        """Editing a label must never repoint a reference."""

        block = self.make_block("_32G Before")
        placement = frappe.get_doc({
            "doctype": PLACEMENT, "route_key": "home", "slot_key": "hero",
            "block": block.name, "enabled": 1,
        }).insert(ignore_permissions=True)

        block.block_name = "_32G After"
        block.save(ignore_permissions=True)

        self.assertEqual(
            frappe.db.get_value(PLACEMENT, placement.name, "block"), block.name,
            "a title edit moved the stored Link value")
        self.assertEqual(frappe.db.get_value("YOB Storefront Block", block.name,
                                             "block_name"), "_32G After")


if __name__ == "__main__":
    unittest.main()
