# Copyright (c) 2026, YOB and Shayona
"""A merchandising facet: Voltage, Material, Finish.

NOT an ERPNext variant attribute. Colour-as-a-variant-attribute resolves an
actual SKU (Phase 24); Colour-as-a-storefront-filter narrows a listing. The two
may share a word and share nothing else, and neither reads the other.
"""

import frappe
from frappe.model.document import Document

from yob_storefront.utils.storefront_content import (
    MACHINE_KEY_PATTERN,
    validate_key,
)


class YOBStorefrontFilter(Document):
    def validate(self):
        # `filter_key` is the document NAME (autoname field:filter_key) and the
        # identity APIs and URLs will use, so it is validated before it can be
        # written -- not left to whatever a merchant types.
        validate_key(self.filter_key, "Key", MACHINE_KEY_PATTERN)

    def on_trash(self):
        """A Filter in use is not deleted quietly.

        Frappe's link check covers Filter Set rows and Item rows, but its message
        is generic; naming the value here is what tells an administrator where to
        look.
        """

        values = frappe.db.count("YOB Storefront Filter Value", {"filter": self.name})

        if values:
            frappe.throw(
                frappe._("Delete this Filter's {0} value(s) first.").format(values),
                frappe.LinkExistsError,
            )
