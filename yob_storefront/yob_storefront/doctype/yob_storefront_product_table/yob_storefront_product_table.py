# Copyright (c) 2026, YOB and Shayona
"""A bounded, Frappe-native table for a product content `table` block.

WHY FIXED COLUMNS
-----------------
Frappe supports one level of child table, so a table cannot own both a columns
list and a rows list of variable cells. The three ways out were: pasted JSON
(refused -- it is not an admin experience), a normalised
`(row_index, column, value)` grid (an admin typing row indices by hand), or a
bounded fixed width. Two to six columns covers product specification and
comparison tables, keeps the editor an ordinary Frappe grid, and needs no custom
widget.

WIDTH IS A VIEW, NOT A DELETION
-------------------------------
`column_count` decides how many columns are ACTIVE. Labels and cells beyond it
are hidden in Desk, excluded from validation and excluded from the runtime
projection -- but they are **kept in the database exactly as entered**.

Narrowing a table is a presentation decision, and it must not destroy work: a
merchant who sets six columns to three to simplify a page, then changes their
mind, gets their original columns 4-6 back untouched. Clearing them would make an
innocuous-looking dropdown change silently unrecoverable.

The cost is that inactive columns hold data nothing reads, and that cost is paid
deliberately at the READ boundary instead: the Phase 27B projection must emit
only `1..cint(column_count)` and ignore whatever is stored beyond it. Persisted
does not mean published.

Row order is the grid's own `idx` -- dragged, never typed.
"""

import frappe
from frappe import _
from frappe.utils import cint
from frappe.model.document import Document

from yob_storefront.utils.product_merchandising import (
	MAX_TABLE_COLUMNS,
	MIN_TABLE_COLUMNS,
	reject_variant_ownership,
)


class YOBStorefrontProductTable(Document):
	def validate(self):
		self.validate_owner()
		self.validate_columns()

	def validate_owner(self):
		if not frappe.db.exists("Item", self.item):
			frappe.throw(_("Item {0} does not exist.").format(self.item),
				     frappe.ValidationError)

		reject_variant_ownership(self.item, _("A product table"))

	def validate_columns(self):
		count = cint(self.column_count)

		if not MIN_TABLE_COLUMNS <= count <= MAX_TABLE_COLUMNS:
			frappe.throw(
				_("A table has between {0} and {1} columns; this one says {2}.")
				.format(MIN_TABLE_COLUMNS, MAX_TABLE_COLUMNS, self.column_count or 0),
				frappe.ValidationError)

		# Every column a buyer will SEE needs a heading. An unlabelled active
		# column renders as an empty header cell nobody can interpret. Columns
		# past the count are not seen, so they are not judged -- an inactive
		# blank label must never block a save.
		missing = [n for n in range(1, count + 1)
			   if not (self.get(f"column_{n}_label") or "").strip()]

		if missing:
			frappe.throw(
				_("Columns {0} need labels.")
				.format(", ".join(str(n) for n in missing)),
				frappe.ValidationError)
