# Copyright (c) 2026, YOB and Shayona
"""One customer's targets, for an administrator. READ ONLY.

WHAT THIS IS
------------
The internal counterpart of the storefront Target Progress block: an operational
Desk view of every `YOB Customer Target` belonging to one customer, with the
summary an administrator wants before picking up the phone.

It is NOT the storefront widget. It shows internal fields the buyer never sees
(`reward_status`, `eligible_incentive`, `company`) and it is reachable only with
the `System Manager` role.

WHERE THE NUMBERS COME FROM
---------------------------
`services/target_admin`, which reads the rows Phase 30A's scheduled job already
calculated. Opening this report performs no calculation, writes nothing, and
touches no Sales Order -- `last_calculated_on` is published instead so the
administrator can see the age of the figures.

`achievement_percentage` is shown EXACTLY as stored, uncapped: a customer at 125%
reads 125%. `remaining` is presentation arithmetic over two stored columns.

A TARGET WHOSE CALCULATION FAILED IS SHOWN, AND MARKED
------------------------------------------------------
Phase 30A keeps a target's previous successful figures and records
`calculation_error` when a recalculation fails, so those figures may be stale.
Here the record STAYS VISIBLE -- an administrator needs to know it exists and
needs attention -- and carries `Calculation State: Needs Attention` beside its
stored numbers, with a `Calculation Issues` count in the summary.

The stored error text is internal and is never published; the reader learns that
a figure is unreliable, not which exception produced it. The cross-customer
dashboard goes further and excludes these records from every achievement metric.
"""

import frappe
from frappe import _
from frappe.utils import flt

from yob_storefront.services import target_admin


def execute(filters=None):
    filters = frappe._dict(filters or {})

    if not filters.get("customer"):
        # Required rather than defaulted: a report titled "one customer's
        # targets" that silently showed everybody's would be the cross-customer
        # dashboard wearing the wrong name.
        frappe.throw(_("Select a Customer."), frappe.ValidationError)

    selection = _selection(filters)

    # `valid_only=False`: an errored target is still listed. The administrator
    # is the person who has to act on it, so hiding it here would be the one
    # place it must not be hidden.
    rows = target_admin.target_rows(selection, valid_only=False)

    return _columns(), rows, None, None, _summary(selection, rows)


def _selection(filters):
    """Only the keys `target_admin` recognises; everything else is dropped."""

    keys = ("customer", "company", "target_period", "target_rule", "status",
            "target_type", "target_scope")

    return {key: filters.get(key) for key in keys if filters.get(key)}


def _columns():
    """Link columns so Desk gives drill-through for free.

    `fieldtype: Link` plus `options` is all Frappe needs to make a cell navigate,
    so Target Rule, Target Period, Customer Target and Customer all open their own
    documents with no custom JavaScript.
    """

    return [
        {"label": _("Target Rule"), "fieldname": "target_rule", "fieldtype": "Link",
         "options": "YOB Target Rule", "width": 180},
        {"label": _("Target Period"), "fieldname": "target_period", "fieldtype": "Link",
         "options": "YOB Target Period", "width": 150},
        {"label": _("Start"), "fieldname": "start_date", "fieldtype": "Date", "width": 100},
        {"label": _("End"), "fieldname": "end_date", "fieldtype": "Date", "width": 100},
        {"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 110},
        {"label": _("Achievement %"), "fieldname": "achievement_percentage",
         "fieldtype": "Percent", "width": 120},
        # Deliberately beside the achievement, not at the end of 24 columns: the
        # whole point is that a reader cannot see the percentage without seeing
        # whether it can be trusted.
        {"label": _("Calculation State"), "fieldname": "calculation_state",
         "fieldtype": "Data", "width": 150},

        {"label": _("Scope"), "fieldname": "target_scope", "fieldtype": "Data", "width": 130},
        {"label": _("Scope Value"), "fieldname": "scope_value", "fieldtype": "Data",
         "width": 150},
        {"label": _("Type"), "fieldname": "target_type", "fieldtype": "Data", "width": 90},

        # `options: currency` points each money cell at the row's OWN currency
        # column, so a multi-company report renders every row in its own base
        # currency instead of one chosen for the whole table.
        {"label": _("Target Amount"), "fieldname": "target_amount", "fieldtype": "Currency",
         "options": "currency", "width": 130},
        {"label": _("Achieved Amount"), "fieldname": "achieved_amount",
         "fieldtype": "Currency", "options": "currency", "width": 140},
        {"label": _("Remaining Amount"), "fieldname": "remaining_amount",
         "fieldtype": "Currency", "options": "currency", "width": 150},

        {"label": _("Target Qty"), "fieldname": "target_quantity", "fieldtype": "Float",
         "width": 110},
        {"label": _("Achieved Qty"), "fieldname": "achieved_quantity", "fieldtype": "Float",
         "width": 120},
        {"label": _("Remaining Qty"), "fieldname": "remaining_quantity",
         "fieldtype": "Float", "width": 120},
        {"label": _("UOM"), "fieldname": "target_uom", "fieldtype": "Link",
         "options": "UOM", "width": 90},

        {"label": _("Incentive Type"), "fieldname": "incentive_type", "fieldtype": "Data",
         "width": 140},
        {"label": _("Current Slab"), "fieldname": "current_slab", "fieldtype": "Data",
         "width": 130},
        {"label": _("Final Incentive"), "fieldname": "final_incentive",
         "fieldtype": "Currency", "options": "currency", "width": 140},
        {"label": _("Reward"), "fieldname": "reward_status", "fieldtype": "Data",
         "width": 100},

        {"label": _("Company"), "fieldname": "company", "fieldtype": "Link",
         "options": "Company", "width": 150},
        {"label": _("Last Calculated"), "fieldname": "last_calculated_on",
         "fieldtype": "Datetime", "width": 160},
        {"label": _("Customer Target"), "fieldname": "customer_target", "fieldtype": "Link",
         "options": "YOB Customer Target", "width": 130},
        {"label": _("Currency"), "fieldname": "currency", "fieldtype": "Link",
         "options": "Currency", "width": 90, "hidden": 1},
    ]


def _summary(selection, rows):
    """Status counts, then money PER CURRENCY, then quantity as counts only.

    Two deliberate refusals:

    * money is never summed across currencies -- one card per currency appears
      instead, so a two-company selection cannot render rupees and dollars as one
      number;
    * quantity targets are counted, never summed -- `500 Boxes + 250 Kg` has no
      total, and showing `750` would be worse than showing nothing.
    """

    counts = target_admin.status_counts(selection)
    issues = target_admin.calculation_issues(selection)

    # `Total Targets` counts every record, including errored ones; the status and
    # money cards below count only the reliable ones. `Calculation Issues`
    # explains the difference, so a gap is never silent.
    cards = [{"label": _("Total Targets"), "value": len(rows), "datatype": "Int"}]

    if issues:
        cards.append({"label": _("Calculation Issues"), "value": issues,
                      "datatype": "Int", "indicator": "Red"})

    for status in target_admin.ALL_STATUSES:
        cards.append({"label": _(status), "value": counts[status], "datatype": "Int"})

    for money in target_admin.amount_totals(selection):
        currency = money["currency"]
        cards.append({
            "label": _("Target Amount") + (f" ({currency})" if currency else ""),
            "value": money["target_amount"], "datatype": "Currency",
            "currency": currency})
        cards.append({
            "label": _("Achieved Amount") + (f" ({currency})" if currency else ""),
            "value": money["achieved_amount"], "datatype": "Currency",
            "currency": currency})

    for exposure in target_admin.incentive_exposure(selection):
        currency = exposure["currency"]
        cards.append({
            "label": _("Incentive Exposure") + (f" ({currency})" if currency else ""),
            "value": exposure["final_incentive"], "datatype": "Currency",
            "currency": currency})

    quantity = target_admin.quantity_summary(selection)

    if quantity["targets"]:
        cards.append({"label": _("Quantity Targets"), "value": quantity["targets"],
                      "datatype": "Int"})
        cards.append({"label": _("Avg Quantity Achievement"),
                      "value": flt(quantity["average_achievement"]),
                      "datatype": "Percent"})

    return cards
