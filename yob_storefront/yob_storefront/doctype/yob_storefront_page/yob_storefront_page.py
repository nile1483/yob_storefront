# Copyright (c) 2026, YOB and Shayona
"""A storefront page: identity, and an ordered list of blocks.

This answers the one question the prototype left open -- *which blocks belong to
which page, and in what order* -- and nothing more. It is not a layout builder:
there are no columns, no nesting, no per-page theming. A page is a slug and a
sequence.

The Product Grid cap lives here rather than at render time because a page that
cannot be rendered cheaply should fail when a merchant saves it, not when a buyer
opens it.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from yob_storefront.utils.storefront_content import MAX_PRODUCT_GRIDS, validate_key

__all__ = ["MAX_PRODUCT_GRIDS", "YOBStorefrontPage"]


class YOBStorefrontPage(Document):
    def validate(self):
        validate_key(self.slug, "Slug")
        self.validate_blocks()

    def validate_blocks(self):
        rows = self.blocks or []
        seen = set()
        grids = 0

        for row in rows:
            if row.block in seen:
                frappe.throw(
                    _("Block {0} is placed more than once (row {1}).")
                    .format(row.block, row.idx),
                    frappe.DuplicateEntryError)

            seen.add(row.block)

            block = frappe.db.get_value(
                "YOB Storefront Block", row.block, ["block_type", "enabled"], as_dict=True)

            if not block:
                frappe.throw(
                    _("Row {0}: Block {1} does not exist.").format(row.idx, row.block),
                    frappe.ValidationError)

            if block.block_type == "Product Grid" and cint(row.enabled):
                grids += 1

        if grids > MAX_PRODUCT_GRIDS:
            frappe.throw(
                _("A page may hold at most {0} Product Grid blocks; this one has {1}.")
                .format(MAX_PRODUCT_GRIDS, grids),
                frappe.ValidationError)
