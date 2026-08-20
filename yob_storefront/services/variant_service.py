# Copyright (c) 2026, YOB and Shayona
"""Variant families: the server-authoritative matrix, resolver and SKU gate.

WHAT A BUYER CONTROLS
---------------------
Attributes and quantity. Nothing else. Everything downstream of a resolved SKU --
selling UOM, conversion factor, warehouse, availability, Item Price, Pricing
Rules, tax, promotions, Cart and Draft Sales Order -- is the unchanged Phase 23
architecture. Nothing here prices anything.

WHY THE MATRIX IS BUILT FROM VARIANT ROWS
-----------------------------------------
`Item Attribute Value` is GLOBAL: `Colour` holds every colour any product ever
used. A cross-product of attribute values therefore invents combinations nobody
can buy -- Red/M and Blue/L existing does not make Red/L real. The only truthful
source is the `Item Variant Attribute` rows of the variants ERPNext actually
generated, which is what `variant_matrix()` reads. Numeric attributes are treated
the same way: the values offered are the ones that OCCUR, never a range expanded
by YOB.

WHY YOB NEVER BUILDS A SKU
--------------------------
`make_variant_item_code` is ERPNext's naming algorithm and reproducing it -- here
or in a browser -- would be a second source of truth for identity. Resolution
goes through `erpnext.controllers.item_variant.find_variant`, which matches on
the stored attribute rows and answers None for a combination that does not exist.

MANUFACTURER-BASED FAMILIES ARE NOT SUPPORTED
---------------------------------------------
`variant_based_on = "Manufacturer"` has no attribute selector to render: its
variants are distinguished by a manufacturer part number, not by Colour/Size. YOB
FAILS CLOSED for them -- they are excluded from the catalogue and their template
answers `variant_family_unsupported` -- rather than inventing a selector.
Attribute-based families are unaffected.
"""

import frappe
from frappe.utils import cint, getdate, today

#: The only variant mode the storefront can present as an attribute selector.
ATTRIBUTE_BASED = "Item Attribute"


def family_of(item_code):
    """`(has_variants, variant_of, variant_based_on)` for one item, cached."""

    return frappe.get_cached_value(
        "Item", item_code, ["has_variants", "variant_of", "variant_based_on"],
        as_dict=True) or frappe._dict()


def is_template(item_code) -> bool:
    return bool(cint(family_of(item_code).get("has_variants")))


def is_attribute_family(item_code) -> bool:
    info = family_of(item_code)

    return bool(cint(info.get("has_variants"))
                and info.get("variant_based_on") == ATTRIBUTE_BASED)


# =========================================================
# SALABILITY OF ONE ACTUAL SKU
# =========================================================

def salable_variants(template):
    """Every variant of `template` a buyer could actually be sold, ordered.

    Catalogue eligibility only -- enabled, sales item, in life. Stock is NOT a
    filter: "we have none right now" is a different statement from "this
    combination does not exist", and collapsing them would delete real products
    from the selector whenever they sold out.
    """

    return frappe.get_all(
        "Item",
        filters={
            "variant_of": template,
            "disabled": 0,
            "is_sales_item": 1,
            "has_variants": 0,
        },
        or_filters=[
            ["end_of_life", "is", "not set"],
            ["end_of_life", ">=", today()],
        ],
        fields=["name", "item_name", "image"],
        order_by="name asc",
    )


def is_salable_sku(item_code) -> bool:
    """Can this exact code be transacted? Template and template-only checks."""

    item = frappe.get_cached_value(
        "Item", item_code,
        ["name", "disabled", "is_sales_item", "has_variants", "end_of_life", "variant_of"],
        as_dict=True)

    if not item or cint(item.disabled) or not cint(item.is_sales_item):
        return False

    if cint(item.has_variants):
        return False

    if item.end_of_life and getdate(item.end_of_life) < getdate(today()):
        return False

    # A variant whose template vanished is an orphan; ERPNext still prices it, but
    # the storefront presents it through a family that no longer exists.
    if item.variant_of and not frappe.db.exists("Item", item.variant_of):
        return False

    return True


# =========================================================
# THE MATRIX
# =========================================================

def _in_merchant_order(attribute, values):
    """The offered values, in the order the MERCHANT arranged them.

    `Item Attribute Value` is an ordered child table, which is how a merchant
    expresses that Small comes before Medium comes before Large. Sorting
    alphabetically would answer "Large, Medium, Small" and sorting by first
    appearance would answer whatever order the variant codes happen to take --
    both are inventions. Values ERPNext no longer lists fall to the end in stable
    order rather than disappearing.
    """

    catalogue = frappe.get_all("Item Attribute Value", filters={"parent": attribute},
                               fields=["attribute_value"], order_by="idx asc",
                               pluck="attribute_value")

    rank = {value: index for index, value in enumerate(catalogue)}

    return sorted(values, key=lambda value: (rank.get(value, len(rank)), value))


def variant_matrix(template):
    """Selectable attributes and the ACTUAL combinations, for one family.

    Returns::

        {
          "variant_of": <template code>,
          "attributes": [{"attribute", "numeric", "values": [...]}, ...],
          "variants":   [{"item_code", "attributes": {...}}, ...],
        }

    `attributes` keeps the TEMPLATE's own order (`Item Variant Attribute.idx`),
    because that is the order a merchant chose to present them in. Its `values`
    are exactly the values that occur in the salable variants below -- never the
    global attribute list, and never a numeric range expanded into steps.

    A variant missing one of the template's attributes is dropped: it cannot be
    addressed by a complete selection, so offering it would create a combination
    the resolver could never return.
    """

    definitions = frappe.get_all(
        "Item Variant Attribute",
        filters={"parent": template, "parenttype": "Item"},
        fields=["attribute", "numeric_values"],
        order_by="idx asc",
    )

    variants = salable_variants(template)
    codes = [row.name for row in variants]

    rows = frappe.get_all(
        "Item Variant Attribute",
        filters={"parent": ["in", codes], "parenttype": "Item"},
        fields=["parent", "attribute", "attribute_value"],
    ) if codes else []

    by_variant = {}
    for row in rows:
        by_variant.setdefault(row.parent, {})[row.attribute] = row.attribute_value

    wanted = [d.attribute for d in definitions]

    combinations = []
    for variant in variants:
        attributes = by_variant.get(variant.name, {})

        if any(attribute not in attributes for attribute in wanted):
            continue

        combinations.append({
            "item_code": variant.name,
            "attributes": {attribute: attributes[attribute] for attribute in wanted},
        })

    offered = {}
    for combination in combinations:
        for attribute, value in combination["attributes"].items():
            values = offered.setdefault(attribute, set())
            values.add(value)

    ordered = {
        attribute: _in_merchant_order(attribute, values)
        for attribute, values in offered.items()
    }
    offered = ordered

    return {
        "variant_of": template,
        "attributes": [
            {
                "attribute": definition.attribute,
                "numeric": cint(definition.numeric_values),
                "values": offered.get(definition.attribute, []),
            }
            for definition in definitions
        ],
        "variants": combinations,
    }


def attributes_of(item_code):
    """The stored attribute map of one variant, in its own row order."""

    rows = frappe.get_all(
        "Item Variant Attribute",
        filters={"parent": item_code, "parenttype": "Item"},
        fields=["attribute", "attribute_value"], order_by="idx asc")

    return {row.attribute: row.attribute_value for row in rows}


# =========================================================
# RESOLUTION
# =========================================================

def resolve(template, attributes):
    """A complete attribute selection -> the actual SKU, or None.

    ERPNext's `find_variant` does the matching, against the attribute rows it
    wrote itself. This adds exactly two things: a completeness check, so a partial
    selection is reported as such rather than as "no such product", and a
    salability check, so a disabled or retired variant is never handed back as
    resolvable.

    Returns ``(item_code, reason)`` where reason is None on success and otherwise
    one of ``"incomplete"`` / ``"not_available"``.
    """

    from erpnext.controllers.item_variant import find_variant

    required = [row.attribute for row in frappe.get_all(
        "Item Variant Attribute", filters={"parent": template, "parenttype": "Item"},
        fields=["attribute"], order_by="idx asc")]

    selection = {key: value for key, value in (attributes or {}).items() if value not in (None, "")}

    if not required or sorted(selection) != sorted(required):
        return None, "incomplete"

    try:
        item_code = find_variant(template, selection)
    except frappe.ValidationError:
        # ERPNext rejects a value that is not a legal Item Attribute Value at all.
        # From the storefront's side that is the same answer as "no such
        # combination": a selection nobody can buy.
        return None, "not_available"

    if not item_code or not is_salable_sku(item_code):
        return None, "not_available"

    return item_code, None
