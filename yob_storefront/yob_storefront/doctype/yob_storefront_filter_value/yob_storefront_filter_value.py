# Copyright (c) 2026, YOB and Shayona
"""One selectable value of one Filter.

WHY THIS IS A MASTER AND NOT A CHILD ROW
----------------------------------------
The prototype made it a child table of Filter and then pointed an Item's Link
field at it. Frappe child rows are not addressable masters: a Link to one has no
referential protection and no usable picker, which is why the prototype needed a
custom query -- and that query returned the display TEXT, so what got stored was
a string that broke the moment anybody renamed a value.

As a master with an opaque `hash` name, the Item's link survives renaming the
display text, and uniqueness can be scoped where the business actually needs it.

UNIQUENESS IS PER FILTER, NEVER GLOBAL
--------------------------------------
`Colour → Red` and `Paint Finish → Red` must both exist. The prototype's global
`unique` on the text made `Red` usable under exactly one Filter in the entire
system.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr

from yob_storefront.utils.storefront_content import MACHINE_KEY_PATTERN, validate_key


class YOBStorefrontFilterValue(Document):
    def validate(self):
        self.value = cstr(self.value).strip()

        if not self.value:
            frappe.throw(_("Value is required."), frappe.ValidationError)

        self.set_value_key()
        self.validate_unique_within_filter()

    def set_value_key(self):
        """A URL-safe token, derived from the display text when left blank.

        Derived once and then kept: the key is what a storefront URL carries, so
        re-deriving it on every save would silently break links whenever a
        merchant corrected a typo in the label.
        """

        if not self.value_key:
            self.value_key = frappe.scrub(self.value).replace("_", "-").strip("-")

        validate_key(self.value_key, "Value Key")

        if not self.value_key:
            frappe.throw(
                _("Value Key could not be derived from {0}. Enter one.").format(self.value),
                frappe.ValidationError,
            )

    def validate_unique_within_filter(self):
        for field, label in (("value", _("Value")), ("value_key", _("Value Key"))):
            clash = frappe.db.get_value(
                self.doctype,
                {
                    "filter": self.filter,
                    field: self.get(field),
                    "name": ["!=", self.name or ""],
                },
                "name",
            )

            if clash:
                frappe.throw(
                    _("{0} {1} already exists for Filter {2}.")
                    .format(label, self.get(field), self.filter),
                    frappe.DuplicateEntryError,
                )
