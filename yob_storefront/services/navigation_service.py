# Copyright (c) 2026, YOB and Shayona
"""The storefront's view of a merchant's navigation.

PUBLISHING RULES
----------------
A node reaches a buyer only when everything above it agrees:

    menu enabled  AND  node enabled  AND  (parent enabled, if any)
    AND its destination still resolves

The last clause matters as much as the flags. A menu item pointing at a category
that was later disabled, a page that was unpublished, or a product that lost its
slug is dropped rather than published as a dead link -- the same rule the
destination projection applies (`project_destination` answers None).

A Group is organisational: it carries no destination and is published only for
the children it still has. A Group whose children all vanished is dropped too,
because an expandable menu entry that opens onto nothing is worse than absence.

ORDER
-----
`sequence, lft, name` -- the same deterministic order the Desk tree shows, so a
merchant sees what a buyer will get.
"""

import frappe

from yob_storefront.services.storefront_destination import MENU_FIELDS, project_destination

DOCTYPE = "YOB Storefront Menu Item"
PARENT_FIELD = "parent_yob_storefront_menu_item"

FIELDS = ["name", "label", "item_type", "enabled", "is_group", PARENT_FIELD,
          "storefront_category", "storefront_page", "external_url", "open_in_new_tab",
          "sequence", "lft"]


def get_menu_tree(menu_key):
    """`(menu, items)` for a published menu, or `(None, [])` when there is none."""

    menu = frappe.db.get_value(
        "YOB Storefront Menu", {"menu_key": menu_key},
        ["name", "menu_key", "menu_name", "enabled"], as_dict=True)

    if not menu or not menu.enabled:
        # A disabled menu is indistinguishable from a missing one on purpose: the
        # storefront has nothing to render either way, and the difference is
        # merchant configuration a buyer has no business inferring.
        return None, []

    rows = frappe.get_all(
        DOCTYPE, filters={"menu": menu.name, "enabled": 1}, fields=FIELDS,
        order_by="sequence asc, lft asc, name asc", limit_page_length=0)

    by_parent = {}
    roots = []

    for row in rows:
        parent = row.get(PARENT_FIELD)
        if parent:
            by_parent.setdefault(parent, []).append(row)
        else:
            roots.append(row)

    items = []

    for row in roots:
        node = _project_node(row, by_parent)
        if node:
            items.append(node)

    return menu, items


def _project_node(row, by_parent):
    """One node, or None when it has nothing publishable to offer."""

    if row.is_group:
        children = [
            child for child in (
                _project_leaf(candidate) for candidate in by_parent.get(row.name, [])
            ) if child
        ]

        if not children:
            return None

        return {
            "label": row.label,
            "type": "group",
            "destination": None,
            "children": children,
        }

    return _project_leaf(row)


def _project_leaf(row):
    destination = project_destination(row, MENU_FIELDS)

    if not destination:
        return None

    return {
        "label": row.label,
        "type": destination["type"],
        "destination": destination,
        "children": [],
    }
