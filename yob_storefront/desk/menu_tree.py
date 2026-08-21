# Copyright (c) 2026, YOB and Shayona
"""Desk tree loader for storefront menu items.

Desk-only, exactly like Frappe's own tree loaders. The storefront's navigation
projection is a separate Phase 25C endpoint behind the YOB API boundary; this one
answers the Desk tree widget and applies no publishing rules.
"""

import frappe
from frappe import _

DOCTYPE = "YOB Storefront Menu Item"
PARENT_FIELD = "parent_yob_storefront_menu_item"


@frappe.whitelist()
def get_children(doctype=DOCTYPE, parent=None, menu=None, is_root=False, **kwargs):
    if doctype != DOCTYPE:
        frappe.throw(_("Invalid DocType for the storefront menu tree."))

    frappe.has_permission(DOCTYPE, "read", throw=True)

    is_root = str(is_root).lower() in ("1", "true")
    filters = [["docstatus", "<", 2]]

    if menu:
        filters.append(["menu", "=", menu])

    if parent and not is_root and parent != "All Menu Items":
        filters.append([PARENT_FIELD, "=", parent])
    else:
        filters.append([PARENT_FIELD, "in", ["", None]])

    return frappe.get_list(
        DOCTYPE,
        fields=["name as value", "label as title", "is_group as expandable",
                "item_type", "enabled", "menu", PARENT_FIELD],
        filters=filters,
        # The same deterministic order the storefront projection will use.
        order_by="sequence asc, lft asc, name asc",
        limit_page_length=0,
    )


@frappe.whitelist()
def add_node():
    """Create a node from the Desk tree dialog."""

    from frappe.desk.treeview import make_tree_args

    args = make_tree_args(**frappe._dict(frappe.form_dict))
    parent = args.get(PARENT_FIELD)

    if not parent or parent in {"All Menu Items", DOCTYPE} or not frappe.db.exists(DOCTYPE, parent):
        args[PARENT_FIELD] = None

    frappe.get_doc(args).insert()
