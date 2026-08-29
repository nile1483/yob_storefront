# Copyright (c) 2026, YOB and Shayona
"""One row of a product table.

Six fixed cells. Validation lives on the parent, which is the only place that
knows how many of them are actually in use.
"""

from frappe.model.document import Document


class YOBStorefrontProductTableRow(Document):
	pass
