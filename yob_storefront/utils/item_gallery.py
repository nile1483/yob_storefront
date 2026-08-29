# Copyright (c) 2026, YOB and Shayona
"""`Item.validate` gate for the product Gallery (Phase 27A).

Two rules, both enforced on the server because Desk visibility is not a control:
Data Import, the REST API and `bench execute` never run a Client Script, and a
generated variant that acquired a gallery through one of them would render a
product page nobody authored.

    1. a generated variant owns NO gallery rows
    2. at most ONE row may be primary

Rule 2 REFUSES rather than repairs. Silently unsetting the other row would edit a
merchant's earlier decision to make the current save succeed, and they would never
learn which image they lost -- a validation error names the conflict and lets them
choose.
"""

import frappe
from frappe import _
from frappe.utils import cint

from yob_storefront.utils.product_merchandising import reject_variant_ownership

GALLERY_FIELD = "custom_storefront_gallery"


def validate_item_gallery(doc, method=None):
    """Cheap and silent when the product has no gallery at all."""

    rows = doc.get(GALLERY_FIELD) or []

    if not rows:
        return

    reject_variant_ownership(doc.name, _("A product gallery"))

    primaries = [row for row in rows if cint(row.get("is_primary"))]

    if len(primaries) > 1:
        frappe.throw(
            _("Only one gallery image can be the primary one; rows {0} are all "
              "marked primary. Clear the ones you do not want.")
            .format(", ".join(str(row.idx) for row in primaries)),
            frappe.ValidationError)
