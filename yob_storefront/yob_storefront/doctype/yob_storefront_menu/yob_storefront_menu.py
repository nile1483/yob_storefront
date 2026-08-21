# Copyright (c) 2026, YOB and Shayona
"""One navigation area, addressed by a stable key.

`menu_key` is the document name and the identity a storefront will ask for, so it
is validated on save and fixed thereafter -- a navigation area that renames itself
breaks whatever asked for it.
"""

from frappe.model.document import Document

from yob_storefront.utils.storefront_content import validate_key


class YOBStorefrontMenu(Document):
    def validate(self):
        validate_key(self.menu_key, "Menu Key")
