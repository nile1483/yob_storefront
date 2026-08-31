# Copyright (c) 2026, YOB and Shayona
"""One node of a storefront menu: a Group, or a destination.

SHAPE
-----
    root:  Group            -> may hold destination children
    root:  destination      -> allowed, a top-level link
    child: destination only -> a Group may not be a child
    grandchildren           -> forbidden

One level of nesting is a deliberate product constraint, not a technical one: a
storefront header renders two levels, and a tree that can nest further would let
a merchant build navigation the storefront cannot show.

DESTINATIONS
------------
Typed, and validated against the records they point at:

    Home                 -> the storefront root
    Catalog              -> the catalogue root
    All Products         -> the catalogue-wide browse (Phase 28C)
    Storefront Category  -> Link to Category, must be active and not a group
    Storefront Page      -> Link to YOB Storefront Page
    External URL         -> an internal route or an http(s) URL, checked by
                            utils.storefront_content

The first three carry NO target field: the route is fixed by the type. A merchant
picks `All Products` and gets `/products`; there is no box to type a route into,
so there is nothing to mistype and nothing to re-validate at projection.

ERPNext Item Group is deliberately absent: it is an internal ERP and pricing
concept, never storefront taxonomy.

The destination fields of the OTHER types are cleared on save, so a menu item
that changed type cannot keep a stale target that a projection might later read.
"""

import frappe
from frappe import _
from frappe.utils.nestedset import NestedSet

from yob_storefront.utils.storefront_content import apply_destination

PARENT_FIELD = "parent_yob_storefront_menu_item"

GROUP = "Group"

#: type -> the field that carries its destination (types with no field are routes)
DESTINATION_FIELDS = {
    "Storefront Category": "storefront_category",
    "Storefront Page": "storefront_page",
    "External URL": "external_url",
}

#: Destinations whose route is fixed by the type, so they need no target field.
ROUTE_TYPES = {"Home", "Catalog", "All Products"}

ITEM_TYPES = {GROUP, *ROUTE_TYPES, *DESTINATION_FIELDS}


class YOBStorefrontMenuItem(NestedSet):
    nsm_parent_field = PARENT_FIELD

    def validate(self):
        self.validate_item_type()
        self.apply_type_specific_fields()
        self.validate_hierarchy()

    def validate_item_type(self):
        if self.item_type not in ITEM_TYPES:
            frappe.throw(
                _("Invalid Item Type: {0}").format(self.item_type), frappe.ValidationError)

        # Derived, never merchant-set: a Group is exactly an item of type Group.
        self.is_group = 1 if self.item_type == GROUP else 0

    def apply_type_specific_fields(self):
        """Keep this type's destination, clear the others, prove the target.

        The SAME helper content blocks use. A menu and a banner both answer "where
        does this go?", and letting each own its answer is how two incompatible
        routing systems get built.
        """

        apply_destination(self, type_field="item_type", field_map=DESTINATION_FIELDS,
                          new_tab_field="open_in_new_tab")

    def validate_hierarchy(self):
        parent = self.get(PARENT_FIELD)

        if not parent:
            return

        if parent == self.name:
            frappe.throw(_("A menu item cannot be its own parent."), frappe.ValidationError)

        if self.item_type == GROUP:
            frappe.throw(
                _("A Group organises other items and cannot itself be a child."),
                frappe.ValidationError)

        parent_row = frappe.db.get_value(
            self.doctype, parent, ["menu", "is_group", PARENT_FIELD], as_dict=True)

        if not parent_row:
            frappe.throw(
                _("Parent Menu Item {0} does not exist.").format(parent), frappe.ValidationError)

        if not parent_row.is_group:
            frappe.throw(_("Parent Menu Item must be a Group."), frappe.ValidationError)

        if parent_row.get(PARENT_FIELD):
            frappe.throw(
                _("Only one level of nesting is allowed."), frappe.ValidationError)

        if not self.menu:
            self.menu = parent_row.menu
        elif self.menu != parent_row.menu:
            frappe.throw(
                _("Parent Menu Item belongs to Menu {0}, not {1}.")
                .format(parent_row.menu, self.menu),
                frappe.ValidationError)

    def on_update(self):
        super().on_update()
        self.validate_menu_of_children()

    def validate_menu_of_children(self):
        """Children follow their parent's Menu; a move must not split a subtree."""

        children = frappe.get_all(
            self.doctype, filters={PARENT_FIELD: self.name}, pluck="name")

        for child in children:
            if frappe.db.get_value(self.doctype, child, "menu") != self.menu:
                frappe.db.set_value(self.doctype, child, "menu", self.menu)
