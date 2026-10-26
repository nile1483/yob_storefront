# Copyright (c) 2026, YOB and Shayona
"""What a customer actually achieved: qualifying sales, aggregated.

THE ONE PLACE A TRANSACTION DATE RANGE IS DECIDED
-------------------------------------------------
`qualifying_date_range()` is it. Nothing else in Target Management builds a date
filter, because the defect being replaced was exactly a second opinion about
dates: the reference implementation emitted

    (transaction_date BETWEEN target_start AND target_end)
    OR
    (transaction_date BETWEEN report_start AND report_end)

into the achievement SQL, so a January target whose report window ran to 31
March counted February and March sales. The report window gates the JOB, never
the data, and it is deliberately not a parameter of any function in this module.

    qualifying:  target_start <= transaction_date <= min(target_end, calc_date)
    both bounds inclusive

The `min(target_end, calc_date)` half is what makes a mid-period run honest: on
10 January a 1-25 January target reports the sales that exist so far, not a
window reaching into the future.

COMPANY IS A FILTER, NOT A LABEL
--------------------------------
Every query filters `Sales Order.company`. The reference stored a company on the
rule and never used it, so a multi-company site credited every company's orders
to the same target.

ONLY SUBMITTED SALES ORDERS
---------------------------
`docstatus = 1`. A draft is not a sale and a cancelled order is not one either.
"""

import frappe
from frappe.utils import flt, getdate

#: Scope -> how a qualifying item row is recognised.
#:
#: `Item Group` and `Brand` read the ITEM MASTER rather than the denormalised
#: copies on Sales Order Item: an admin configuring "Brand = Acme" today means
#: the items that are Acme now. Exact match, no subtree -- the storefront has no
#: descendant convention and inventing one here would silently widen every
#: existing rule.
SCOPE_CONDITIONS = {
    "Item": "soi.item_code = %(scope_value)s",
    "Item Group": "item.item_group = %(scope_value)s",
    "Brand": "item.brand = %(scope_value)s",
    "Total Sales Order": None,
}


class TargetCalculationError(Exception):
    """A configuration or data problem that makes an honest total impossible.

    Raised rather than returning 0 or a partial sum. A target that cannot be
    calculated correctly must keep its previous figures and say why; a false
    total is worse than a visibly failed one because nobody goes looking for it.
    """


def qualifying_date_range(period_start, period_end, calculation_date):
    """`(from_date, to_date)` for the transactions that count, or None.

    `None` means the window has not opened yet -- the calculation date is before
    the period starts, so there is nothing to count rather than an error.
    """

    start = getdate(period_start)
    end = getdate(period_end)
    today_ = getdate(calculation_date)

    if today_ < start:
        return None

    # Never count beyond the period, and never count beyond the day being
    # calculated. The earlier of the two wins.
    return start, min(end, today_)


# =========================================================
# AMOUNT
# =========================================================

def achieved_amount(*, customer, company, scope, scope_value, from_date, to_date):
    """Qualifying `base_net_amount`, summed.

    `base_net_amount` is ERPNext's net of tax, in the COMPANY'S base currency
    (`options: Company:company:default_currency`), on the ITEM ROW. The row is
    what makes a scoped target possible: a document total cannot answer "how much
    Acme brand did this customer buy", so it is deliberately not used even for
    Total Sales Order, where summing the qualifying rows gives the same answer
    and keeps one query shape.
    """

    conditions, values = _base_conditions(
        customer=customer, company=company, scope=scope, scope_value=scope_value,
        from_date=from_date, to_date=to_date)

    rows = frappe.db.sql(
        f"""
        SELECT COALESCE(SUM(soi.base_net_amount), 0)
        FROM `tabSales Order` so
        INNER JOIN `tabSales Order Item` soi ON soi.parent = so.name
        LEFT JOIN `tabItem` item ON item.name = soi.item_code
        WHERE {" AND ".join(conditions)}
        """,
        values,
    )

    return flt(rows[0][0]) if rows else 0.0


# =========================================================
# QUANTITY
# =========================================================

def achieved_quantity(*, customer, company, scope, scope_value, from_date, to_date,
                      target_uom):
    """Qualifying quantity, converted into `target_uom` before being added up.

    NOT `SUM(qty)`. The storefront sells one item in several units, so raw `qty`
    adds 5 Box to 30 Nos and answers 35 -- the reference's defect. With 1 Box =
    10 Nos the honest answer in Nos is 80.

    THE CONVERSION CHAIN
    --------------------
        row qty in row uom  ->  stock_qty in stock uom  ->  target_uom

    The first leg is ERPNext's own, already stored on the row: `stock_qty` is
    what ERPNext computed through `conversion_factor` when the order was saved,
    so it is the authoritative transaction-time figure and is not recomputed.

    The second leg asks ERPNext for the factor through
    `get_conversion_factor(item_code, target_uom)`, which reads the Item's own
    `UOM Conversion Detail` (falling back variant -> template) and then the
    global `UOM Conversion Factor`. No conversion rule is invented here.

    Factors are cached per `(item_code, target_uom)` for the call, so a 500-row
    order costs one lookup per distinct item rather than one per row.

    A row that cannot be converted raises `TargetCalculationError`. Skipping it
    would under-report and adding it raw would mix units; both produce a number
    that looks fine and is wrong.
    """

    conditions, values = _base_conditions(
        customer=customer, company=company, scope=scope, scope_value=scope_value,
        from_date=from_date, to_date=to_date)

    rows = frappe.db.sql(
        f"""
        SELECT soi.item_code, soi.stock_uom,
               COALESCE(SUM(soi.stock_qty), 0) AS stock_qty
        FROM `tabSales Order` so
        INNER JOIN `tabSales Order Item` soi ON soi.parent = so.name
        LEFT JOIN `tabItem` item ON item.name = soi.item_code
        WHERE {" AND ".join(conditions)}
        GROUP BY soi.item_code, soi.stock_uom
        """,
        values,
        as_dict=True,
    )

    total = 0.0
    factors = {}

    for row in rows:
        total += _in_target_uom(row, target_uom, factors)

    return total


def _in_target_uom(row, target_uom, factors):
    """One grouped row's stock quantity, expressed in `target_uom`."""

    stock_uom = row.get("stock_uom")

    # Already the target unit: no conversion data needed, and requiring a
    # conversion row for an item sold only in its stock UOM would fail
    # perfectly ordinary configurations.
    if stock_uom and stock_uom == target_uom:
        return flt(row.get("stock_qty"))

    key = (row.get("item_code"), target_uom)

    if key not in factors:
        factors[key] = _conversion_factor(row.get("item_code"), target_uom)

    factor = factors[key]

    if not factor:
        raise TargetCalculationError(
            f"Item {row.get('item_code')} has no conversion between its stock unit "
            f"{stock_uom!r} and the target unit {target_uom!r}, so its quantity cannot "
            f"be counted toward this target."
        )

    # `conversion_factor` is STOCK UNITS PER TARGET UNIT (1 Box = 10 Nos gives
    # 10 for Box), so stock -> target divides.
    return flt(row.get("stock_qty")) / factor


def _conversion_factor(item_code, target_uom):
    """A REAL conversion from the item's stock unit into `target_uom`, or 0.

    ERPNext's own sources are used, in ERPNext's own order:

    1. the Item's `UOM Conversion Detail`, falling back from a variant to its
       template exactly as `get_item_details.get_conversion_factor()` does;
    2. the global `UOM Conversion Factor` table, through ERPNext's own
       `get_uom_conv_factor()`, which also handles the inverse and intermediate
       pairs (Kg -> mg via g).

    WHY `get_conversion_factor()` IS NOT CALLED DIRECTLY
    ---------------------------------------------------
    Its last line is `return {"conversion_factor": conversion_factor or 1.0}`.
    That 1.0 is a sensible default for a form field, and a correctness hazard
    here: an item with NO conversion to the target unit would convert 1:1 and be
    added in silently. That is the mixed-unit defect this module exists to
    remove, arriving by the back door.

    So the lookup is reused and the default is not. A missing conversion returns
    0, and the caller refuses to produce a total.
    """

    from erpnext.stock.doctype.item.item import get_uom_conv_factor

    if not item_code or not target_uom:
        return 0.0

    item = frappe.get_cached_value(
        "Item", item_code, ["variant_of", "stock_uom"], as_dict=True)

    if not item:
        return 0.0

    if target_uom == item.stock_uom:
        return 1.0

    item_codes = [item_code]
    if item.variant_of:
        item_codes.append(item.variant_of)

    rows = frappe.get_all(
        "UOM Conversion Detail",
        filters={"parent": ["in", item_codes], "parenttype": "Item", "uom": target_uom},
        fields=["conversion_factor"],
        limit_page_length=1,
    )

    if rows and flt(rows[0].conversion_factor):
        return flt(rows[0].conversion_factor)

    try:
        return flt(get_uom_conv_factor(target_uom, item.stock_uom))
    except (ZeroDivisionError, TypeError):
        # Narrow on purpose. ERPNext returns None for an unknown pair, which
        # `flt` already turns into 0, so the only real raise is arithmetic on a
        # malformed `UOM Conversion Factor` row -- `1 / value` with value 0.
        #
        # A bare `except Exception` here would swallow a genuine fault in
        # ERPNext's resolver and report it as "this item has no conversion",
        # which fails the target for the wrong stated reason.
        return 0.0


# =========================================================
# SHARED PREDICATE
# =========================================================

def _base_conditions(*, customer, company, scope, scope_value, from_date, to_date):
    """The predicate every achievement query shares.

    One builder so the amount and quantity totals can never disagree about which
    transactions qualified -- a target whose amount counted one set of orders and
    whose quantity counted another would be impossible to explain.
    """

    if scope not in SCOPE_CONDITIONS:
        raise TargetCalculationError(f"{scope!r} is not a valid target scope.")

    values = {
        "customer": customer,
        "company": company,
        "from_date": getdate(from_date),
        "to_date": getdate(to_date),
    }

    conditions = [
        "so.customer = %(customer)s",
        # Submitted only. A draft is not a sale; a cancelled order is not one.
        "so.docstatus = 1",
        # The filter the reference never applied.
        "so.company = %(company)s",
        # Inclusive both ends. The caller already narrowed `to_date` to the
        # calculation date where that is earlier than the period end.
        "so.transaction_date >= %(from_date)s",
        "so.transaction_date <= %(to_date)s",
    ]

    predicate = SCOPE_CONDITIONS[scope]

    if predicate:
        if not scope_value:
            raise TargetCalculationError(f"Scope {scope!r} requires a scope value.")
        values["scope_value"] = scope_value
        conditions.append(predicate)

    return conditions, values
