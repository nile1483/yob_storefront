# Copyright (c) 2026, YOB and Shayona
"""Stop ERPNext copying the storefront slug onto every variant.

WHAT WAS WRONG
--------------
`custom_slug` is a public URL segment, and exactly one Item may answer to it.
It was created `reqd = 1` and it appears in `Item Variant Settings`, and
`copy_attributes_to_variant` copies any field that is either -- so every variant
ERPNext generated was born carrying its TEMPLATE's slug. Three Items then shared
one slug and `catalog.get_item(slug)` returned whichever the database offered
first: clicking the Red card could open the Blue product page (reproduced in
Phase 24A).

WHAT THIS CHANGES
-----------------
1. `custom_slug` is no longer copied to variants (removed from Item Variant
   Settings).
2. `custom_slug` is no longer mandatory, so a variant can exist without claiming
   a public URL. It stays mandatory in PRACTICE for anything a buyer navigates
   to, because the catalogue only lists rows that have one.

Individual variants deliberately have no public URL: they are reached by
choosing attributes on their family's page, and the server resolves the SKU.

SAFETY
------
Idempotent, and it repairs rather than assumes: any variant still holding its
template's slug is cleared. A slug held by exactly one Item is left untouched, so
simple Items and templates keep their URLs. Nothing is renamed and no slug is
invented.
"""

import frappe


def execute():
    _stop_copying_to_variants()
    _make_optional()
    _clear_inherited_slugs()


def _stop_copying_to_variants():
    settings = frappe.get_single("Item Variant Settings")

    keep = [row for row in settings.fields if row.field_name != "custom_slug"]

    if len(keep) == len(settings.fields):
        return

    settings.set("fields", [])
    for row in keep:
        settings.append("fields", {"field_name": row.field_name})

    settings.flags.ignore_permissions = True
    settings.save()


def _make_optional():
    name = frappe.db.get_value("Custom Field", {"dt": "Item", "fieldname": "custom_slug"})

    if name and frappe.db.get_value("Custom Field", name, "reqd"):
        frappe.db.set_value("Custom Field", name, "reqd", 0)
        frappe.clear_cache(doctype="Item")


def _clear_inherited_slugs():
    """Drop a slug a variant only has because it was copied from its template."""

    inherited = frappe.db.sql(
        """
        SELECT v.name
        FROM `tabItem` v
        JOIN `tabItem` t ON t.name = v.variant_of
        WHERE IFNULL(v.custom_slug, '') != ''
          AND v.custom_slug = t.custom_slug
        """,
        as_dict=True,
    )

    for row in inherited:
        # db_set on the field only: an Item save would re-run ERPNext validation
        # (and India Compliance) on records this patch has no business touching.
        frappe.db.set_value("Item", row.name, "custom_slug", None, update_modified=False)
        frappe.clear_document_cache("Item", row.name)

    if inherited:
        print(f"cleared inherited storefront slug on {len(inherited)} variant(s)")
