# Copyright (c) 2026, YOB and Shayona
"""A named collection of Filters.

Used for TWO different jobs, and the distinction matters:

* on an **Item**, a Filter Set is an ADMIN SCOPE -- it decides which Filters an
  administrator may attach while maintaining that product;
* on a **Category**, a Filter Set decides which Filters the storefront will
  eventually EXPOSE for that category's listing.

They are deliberately independent. An Item may carry Voltage, Colour, Material,
IP Rating and Mount Type while its Category exposes only Voltage and Colour --
the richer item metadata is not erased or restricted by the category's choice.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class YOBStorefrontFilterSet(Document):
    def validate(self):
        self.validate_no_duplicate_filter()

    def validate_no_duplicate_filter(self):
        """One Filter may appear once. A repeat is a data-entry slip, never intent."""

        seen = set()

        for row in self.filters or []:
            if not row.filter:
                continue

            if row.filter in seen:
                frappe.throw(
                    _("Filter {0} appears more than once in this set (row {1}).")
                    .format(row.filter, row.idx),
                    frappe.DuplicateEntryError,
                )

            seen.add(row.filter)
