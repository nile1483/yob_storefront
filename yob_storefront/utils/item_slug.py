# Copyright (c) 2026, YOB and Shayona
"""The public storefront slug: unique, and never inherited by a variant.

A slug is a PUBLIC URL segment, so exactly one Item may answer to it. Two things
have to hold and neither is enforced by the field itself:

* **uniqueness** -- `custom_slug` is a plain Data field. A database `unique`
  index cannot be used because every Item without a slug stores the same empty
  string, and those must be allowed to coexist.
* **not inherited** -- ERPNext's `copy_attributes_to_variant` copies any field
  that is `reqd` or listed in `Item Variant Settings`. `custom_slug` was both, so
  every variant was born carrying its template's slug and `get_item(slug)`
  answered with an arbitrary sibling (Phase 24A). The patch removes it from that
  list and drops `reqd`; this guard is what keeps a duplicate from being written
  by any other route.

Variants deliberately have no public URL: a buyer reaches one by choosing
attributes on its family's page, and the server resolves it (Phase 24B).
"""

import frappe


def validate_unique_slug(doc, method=None):
    """Refuse a second Item claiming the same public slug."""

    slug = (doc.get("custom_slug") or "").strip()

    if not slug:
        return

    clash = frappe.db.get_value(
        "Item", {"custom_slug": slug, "name": ["!=", doc.name]}, "name")

    if clash:
        frappe.throw(
            frappe._("Slug {0} is already used by Item {1}").format(slug, clash),
            frappe.DuplicateEntryError,
        )
