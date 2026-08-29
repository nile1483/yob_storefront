# Copyright (c) 2026, YOB and Shayona
"""One image in a product's gallery.

Validation lives on the Item (`utils.item_gallery.validate_item_gallery`), where
the rules can see every row at once: "at most one primary" and "a generated
variant owns no gallery" are both statements about the SET, not about one row.
"""

from frappe.model.document import Document


class YOBStorefrontProductGalleryImage(Document):
	pass
