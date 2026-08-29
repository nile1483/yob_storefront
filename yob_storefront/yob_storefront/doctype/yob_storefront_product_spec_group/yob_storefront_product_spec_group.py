# Copyright (c) 2026, YOB and Shayona
"""An ordered set of key/value rows, referenced by a `key_value` content block.

WHY THIS IS A SEPARATE DOCUMENT
-------------------------------
Frappe supports one level of child table, so a `key_value` block sitting in a
section's grid cannot itself own rows. The alternative was a JSON textarea, which
is not an admin experience. A small linked document gives a real grid.

IT BELONGS TO ONE PRODUCT
-------------------------
`item` is required, and Product Content on a DIFFERENT product may not link here.
Reuse within one product is fine -- two sections may show the same specification
set -- but sharing across products would make one product's page mutable from
another product's admin screen, and nobody editing this document would know whose
pages they were changing. A generated variant may not own one either, by the same
`variant_of` rule that governs galleries and sections.
"""

import frappe
from frappe import _
from frappe.model.document import Document

from yob_storefront.utils.product_merchandising import reject_variant_ownership


class YOBStorefrontProductSpecGroup(Document):
	def validate(self):
		self.validate_owner()
		self.validate_rows()

	def validate_owner(self):
		if not frappe.db.exists("Item", self.item):
			frappe.throw(_("Item {0} does not exist.").format(self.item),
				     frappe.ValidationError)

		reject_variant_ownership(self.item, _("A specification set"))

	def validate_rows(self):
		seen = set()

		for row in self.rows or []:
			key = (row.key_label or "").strip().lower()

			if key in seen:
				frappe.throw(
					_("Row {0}: {1} is listed twice. A specification key appears once.")
					.format(row.idx, row.key_label),
					frappe.DuplicateEntryError)

			seen.add(key)
