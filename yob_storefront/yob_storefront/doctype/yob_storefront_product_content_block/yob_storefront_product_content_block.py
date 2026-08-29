# Copyright (c) 2026, YOB and Shayona
"""One block inside a product content section.

Validation lives in the PARENT (`YOB Storefront Product Content Section`), not
here: a child row is validated as part of its parent's save, and the rules it
needs -- known type, its own field present, stale fields of other types cleared --
are all expressed in `utils.product_merchandising.validate_block`, which the
section calls per row so the error can name the row number.
"""

from frappe.model.document import Document


class YOBStorefrontProductContentBlock(Document):
	pass
