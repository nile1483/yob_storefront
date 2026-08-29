# Copyright (c) 2026, YOB and Shayona
"""One ordered content section of a product page.

WHY THIS IS A STANDALONE DOCTYPE
--------------------------------
A section owns an ordered list of blocks. Frappe supports exactly ONE level of
child table -- verified: not one of the 350 child DocTypes in Frappe or ERPNext
owns a `Table` field, and `load_children_from_db` never recurses -- so
`Item -> sections -> blocks` is impossible as nested children. Making the section
a normal document buys real ordered blocks, real validation and a real grid
editor, at the cost of one link back to the Item. That trade was made
deliberately; the alternative was a JSON blob, which is not an admin experience.

WHAT IT IS NOT
--------------
Not a page, not a route, not a layout. There is no tab key, accordion mode,
component name, CSS class, width or wrapper here, and none may be added: product
content is structured merchandising DATA, and Angular owns how it looks. It is
also NOT the Phase 25 CMS -- `YOB Storefront Page` and `YOB Storefront Block`
serve merchant-authored marketing pages, and the two models stay separate even
though both contain images and rich text.
"""

import frappe
from frappe import _
from frappe.model.document import Document

from yob_storefront.utils.product_merchandising import (
    reject_variant_ownership,
    validate_block,
)


class YOBStorefrontProductContentSection(Document):
    def validate(self):
        self.validate_owner()
        self.validate_blocks()

    def validate_owner(self):
        """Only a simple Item or a variant TEMPLATE may own content."""

        if not self.item:
            frappe.throw(_("A product is required."), frappe.ValidationError)

        if not frappe.db.exists("Item", self.item):
            frappe.throw(_("Item {0} does not exist.").format(self.item),
                         frappe.ValidationError)

        reject_variant_ownership(self.item, _("Product content"))

    def validate_blocks(self):
        # The owner is passed down so a block linking to structured data can be
        # checked against THIS product: a spec group or table owned by another
        # product is refused rather than silently shared.
        for row in self.blocks or []:
            validate_block(row, row.idx, owner_item=self.item)
