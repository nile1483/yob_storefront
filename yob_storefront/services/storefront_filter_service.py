# Copyright (c) 2026, YOB and Shayona
"""Merchandising filters at runtime: what to show, and what a buyer chose.

TWO RESPONSIBILITIES, DELIBERATELY SEPARATE
-------------------------------------------
* `category_filters()` -- which facets a category page should DISPLAY.
* `parse_selection()`  -- what a buyer picked, validated against that same
  category, and normalised into something the listing query and the cursor
  fingerprint can both consume.

WHICH FILTERS
-------------
`Category.storefront_filter_set` and nothing else. No walk up the category tree,
no fallback to the Item's own set, no fallback to every global Filter: a category
with no Filter Set exposes no filters, which is a merchant's explicit choice and
must not be second-guessed. The Item's Filter Set is an admin scope and is
irrelevant here -- an item may legitimately carry Voltage, Colour, Material and
IP Rating while its category exposes only Voltage and Colour.

WHICH VALUES
------------
Only values actually ASSIGNED to a listing entity in that category, so a category
page never offers a facet that would return nothing. Listing entities are what
the catalogue lists (Phase 24B): simple Items and variant TEMPLATES. Generated
variants carry no filter rows and are not facet entities.

Determined by one indexed query over stored assignments. **No pricing**: building
a Sales Order to decide which chips to draw is exactly the unbounded work Phase
22B removed. That also means no counts -- `Red (17)` needs the full eligibility
pipeline per value, so the first cut ships `Red` and stops there.
"""

import frappe
from frappe import _

from yob_storefront.api.response import (
    STOREFRONT_FILTER_CONTEXT_REQUIRED,
    STOREFRONT_FILTER_INVALID,
    STOREFRONT_FILTER_UNKNOWN,
    STOREFRONT_FILTER_VALUE_UNKNOWN,
)

ITEM_FILTER_FIELD = "custom_storefront_filters"


class FilterSelectionError(Exception):
    """A refusal carrying its own stable code.

    The service decides WHAT was wrong; the API adapter turns that into an
    envelope. Building the response here would put the public wire format inside
    a service and hide it from the contract scan that polices endpoints.
    """

    def __init__(self, code, message, field="storefront_filters"):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


# =========================================================
# DEFINITIONS
# =========================================================

def category_filters(category):
    """Facets to display for one Storefront Category. Never inherited."""

    filter_set = frappe.db.get_value("Category", category, "storefront_filter_set")

    if not filter_set:
        return []

    if not frappe.db.get_value("YOB Storefront Filter Set", filter_set, "enabled"):
        return []

    rows = frappe.get_all(
        "YOB Storefront Filter Set Filter",
        filters={"parent": filter_set, "parenttype": "YOB Storefront Filter Set"},
        fields=["filter", "sequence", "idx"],
        order_by="sequence asc, idx asc", limit_page_length=0)

    if not rows:
        return []

    definitions = {
        row.name: row
        for row in frappe.get_all(
            "YOB Storefront Filter",
            filters={"name": ["in", [r.filter for r in rows]], "enabled": 1},
            fields=["name", "filter_key", "label", "sequence"], limit_page_length=0)
    }

    in_use = _values_in_category(category)

    projected = []

    for row in rows:
        definition = definitions.get(row.filter)

        if not definition:
            continue                    # disabled filters are simply not offered

        values = _values_of(row.filter, in_use)

        if not values:
            # A facet with nothing to pick is noise on a category page.
            continue

        projected.append({
            "key": definition.filter_key,
            "label": definition.label,
            "values": values,
        })

    return projected


def _values_in_category(category):
    """Value names assigned to any listing entity in this category.

    One query over the stored child rows joined to the Item. The Item conditions
    mirror catalog eligibility as far as stored columns allow -- enabled, sales
    item, in life, and NOT a generated variant. Price eligibility is deliberately
    not consulted: that is Stage 2/3 work and would cost a pricing pass.
    """

    rows = frappe.db.sql(
        """
        SELECT DISTINCT f.filter_value
        FROM `tabYOB Storefront Item Filter` f
        JOIN `tabItem` i ON i.name = f.parent
        WHERE f.parenttype = 'Item'
          AND f.parentfield = %(field)s
          AND i.custom_category = %(category)s
          AND i.disabled = 0
          AND i.is_sales_item = 1
          AND IFNULL(i.variant_of, '') = ''
          AND (i.end_of_life IS NULL OR i.end_of_life = '0000-00-00'
               OR i.end_of_life >= %(today)s)
        """,
        {"field": ITEM_FILTER_FIELD, "category": category,
         "today": frappe.utils.today()},
        pluck="filter_value",
    )

    return set(rows)


def _values_of(filter_name, in_use):
    rows = frappe.get_all(
        "YOB Storefront Filter Value",
        filters={"filter": filter_name, "enabled": 1},
        fields=["name", "value", "value_key", "sequence"],
        order_by="sequence asc, value asc", limit_page_length=0)

    return [
        {"key": row.value_key, "label": row.value}
        for row in rows
        if row.name in in_use
    ]


# =========================================================
# SELECTION
# =========================================================

def parse_selection(raw, category):
    """A buyer's chosen facets -> a normalised, validated selection.

    Returns a list of ``(filter_name, [value_name, ...])`` ordered by filter key,
    with values sorted and de-duplicated. That normal form is what the listing
    query consumes AND what the cursor fingerprint hashes, so `["red","blue"]` and
    `["blue","red"]` are the same logical query and share one cursor.

    Every refusal names its own cause. Nothing here is ever interpreted as a
    database field: a key that is not an exposed Filter is rejected, not queried.
    """

    if raw in (None, "", b""):
        return []

    selection = raw

    if isinstance(selection, str):
        try:
            selection = frappe.parse_json(selection)
        except (ValueError, TypeError):
            raise FilterSelectionError(
                STOREFRONT_FILTER_INVALID, _("The filter selection is not valid."))

    if not isinstance(selection, dict):
        raise FilterSelectionError(
            STOREFRONT_FILTER_INVALID, _("The filter selection is not valid."))

    selection = {k: v for k, v in selection.items() if v not in (None, "", [])}

    if not selection:
        return []

    if not category:
        # Merchandising facets only mean something inside a category: the set of
        # filters on offer is a property of the category being browsed.
        raise FilterSelectionError(
            STOREFRONT_FILTER_CONTEXT_REQUIRED,
            _("Filters can only be applied while browsing a category."))

    exposed = _exposed_filters(category)
    normalised = []

    for key in sorted(selection):
        values = selection[key]

        if isinstance(values, str):
            values = [values]

        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise FilterSelectionError(
                STOREFRONT_FILTER_INVALID,
                _("The values for {0} are not valid.").format(key))

        definition = exposed.get(key)

        if not definition:
            raise FilterSelectionError(
                STOREFRONT_FILTER_UNKNOWN,
                _("{0} is not available for this category.").format(key))

        resolved = _resolve_values(definition, key, values)

        if resolved:
            normalised.append((definition, sorted(resolved)))

    return normalised


def _exposed_filters(category):
    """filter_key -> Filter docname, for the filters this category exposes."""

    filter_set = frappe.db.get_value("Category", category, "storefront_filter_set")

    if not filter_set:
        return {}

    if not frappe.db.get_value("YOB Storefront Filter Set", filter_set, "enabled"):
        return {}

    names = frappe.get_all(
        "YOB Storefront Filter Set Filter",
        filters={"parent": filter_set, "parenttype": "YOB Storefront Filter Set"},
        pluck="filter")

    if not names:
        return {}

    return {
        row.filter_key: row.name
        for row in frappe.get_all(
            "YOB Storefront Filter",
            filters={"name": ["in", names], "enabled": 1},
            fields=["name", "filter_key"], limit_page_length=0)
    }


def _resolve_values(filter_name, filter_key, value_keys):
    """Value keys -> Filter Value names, refusing anything that is not this
    filter's own enabled value."""

    resolved = []

    for value_key in dict.fromkeys(value_keys):
        name = frappe.db.get_value(
            "YOB Storefront Filter Value",
            {"filter": filter_name, "value_key": value_key, "enabled": 1}, "name")

        if not name:
            raise FilterSelectionError(
                STOREFRONT_FILTER_VALUE_UNKNOWN,
                _("{0} is not an available option for {1}.").format(value_key, filter_key))

        resolved.append(name)

    return resolved


def fingerprint_payload(selection):
    """The selection as a stable, hashable structure for the cursor binding."""

    return [[filter_name, values] for filter_name, values in selection]
