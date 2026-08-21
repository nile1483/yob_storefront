# Copyright (c) 2026, YOB and Shayona
"""Server-side integrity for the merchandising filters stored on an Item.

WHY THIS IS NOT A CLIENT SCRIPT
-------------------------------
The prototype enforced these rules in the browser. Data Import, the REST API,
`bench execute` and a Desk grid paste all bypass a Client Script, and every one
of them is a normal way merchant data actually arrives. Validation lives here,
on `Item.validate`, so there is exactly one gate and no way around it.

THE RULES
---------
* every row's Filter must belong to the Item's own **Filter Set** -- the admin
  scope that keeps a hundred global Filters from being offered on every product;
* every row's Value must belong to that row's Filter;
* a disabled Filter or Value cannot be NEWLY assigned; existing rows are left
  alone, so disabling a Filter never rewrites products behind a merchant's back;
* the exact pair (Filter, Value) may not repeat on one Item;
* several different values under ONE Filter are fine -- Colour → Red AND Blue is
  ordinary merchandising, not an error.

WHERE FILTERS MAY LIVE
----------------------
On whatever the catalogue lists: a simple Item, or a variant TEMPLATE. A
generated variant child is never listed (Phase 24B lists one card per family), so
rows there would be silently ignored -- and silence is the failure mode this
phase exists to remove. They are refused with a message naming the template.
"""

import frappe
from frappe import _
from frappe.utils import cint

FILTER_SET_FIELD = "custom_storefront_filter_set"
FILTER_TABLE_FIELD = "custom_storefront_filters"


def validate_item_storefront_filters(doc, method=None):
    """`Item.validate` hook. Cheap and silent when nothing storefront is set."""

    rows = doc.get(FILTER_TABLE_FIELD) or []
    filter_set = doc.get(FILTER_SET_FIELD)

    if not rows and not filter_set:
        return

    _reject_on_variant_child(doc, rows, filter_set)

    if not rows:
        return

    if not filter_set:
        frappe.throw(
            _("Select a Storefront Filter Set before adding storefront filters."),
            frappe.ValidationError,
        )

    allowed = _filters_in_set(filter_set)
    previous = _previously_stored_pairs(doc)
    seen = set()

    for row in rows:
        if not row.filter or not row.filter_value:
            frappe.throw(
                _("Row {0}: both Filter and Value are required.").format(row.idx),
                frappe.ValidationError,
            )

        if row.filter not in allowed:
            frappe.throw(
                _("Row {0}: Filter {1} is not part of Filter Set {2}.")
                .format(row.idx, row.filter, filter_set),
                frappe.ValidationError,
            )

        value = frappe.db.get_value(
            "YOB Storefront Filter Value", row.filter_value,
            ["filter", "enabled"], as_dict=True)

        if not value:
            frappe.throw(
                _("Row {0}: Value {1} does not exist.").format(row.idx, row.filter_value),
                frappe.ValidationError,
            )

        if value.filter != row.filter:
            frappe.throw(
                _("Row {0}: Value {1} belongs to Filter {2}, not {3}.")
                .format(row.idx, row.filter_value, value.filter, row.filter),
                frappe.ValidationError,
            )

        pair = (row.filter, row.filter_value)

        if pair in seen:
            frappe.throw(
                _("Row {0}: {1} is already selected for this item.")
                .format(row.idx, row.filter_value),
                frappe.DuplicateEntryError,
            )

        seen.add(pair)

        # Disabled definitions block a NEW assignment only. An existing row
        # survives so that disabling a Filter never silently edits catalogue
        # data that was valid when it was entered.
        if pair in previous:
            continue

        if not cint(value.enabled):
            frappe.throw(
                _("Row {0}: Value {1} is disabled and cannot be assigned.")
                .format(row.idx, row.filter_value),
                frappe.ValidationError,
            )

        if not cint(frappe.db.get_value("YOB Storefront Filter", row.filter, "enabled")):
            frappe.throw(
                _("Row {0}: Filter {1} is disabled and cannot be assigned.")
                .format(row.idx, row.filter),
                frappe.ValidationError,
            )


def _reject_on_variant_child(doc, rows, filter_set):
    """A generated variant is not a listing entity, so it carries no facets."""

    if not doc.get("variant_of"):
        return

    if rows or filter_set:
        frappe.throw(
            _("Storefront filters belong on the variant template {0}, not on an "
              "individual variant. The catalogue lists the family, not this SKU.")
            .format(doc.variant_of),
            frappe.ValidationError,
        )


def _filters_in_set(filter_set):
    return set(frappe.get_all(
        "YOB Storefront Filter Set Filter",
        filters={"parent": filter_set, "parenttype": "YOB Storefront Filter Set"},
        pluck="filter",
    ))


def _previously_stored_pairs(doc):
    """Pairs already saved on this Item, so re-saving cannot fail on a disable."""

    if doc.is_new():
        return set()

    return {
        (row.filter, row.filter_value)
        for row in frappe.get_all(
            "YOB Storefront Item Filter",
            filters={"parent": doc.name, "parenttype": "Item",
                     "parentfield": FILTER_TABLE_FIELD},
            fields=["filter", "filter_value"],
        )
    }
