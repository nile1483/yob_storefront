# Copyright (c) 2026, YOB and Shayona
"""Cross-customer target management, for sales management. READ ONLY.

THE QUESTIONS THIS ANSWERS
--------------------------
How many targets are running, who has achieved, who is close enough to chase,
who is falling behind, what is ending soon, and what is the incentive exposure.

ONE REPORT, FIVE VIEWS
----------------------
`view` switches the table body; the KPI cards and the achievement distribution
are computed for every view because they answer the same question regardless of
which list is on screen. Five separate reports would duplicate ten filters and
the KPI block four times, and `§23` of the brief asks for two reports, not six.

NOTHING IS CALCULATED HERE
--------------------------
Every figure is read or aggregated from `YOB Customer Target` rows the scheduled
job already produced, through `services/target_admin`. Opening this report
triggers no recalculation and writes nothing; `Last Calculated` is published so
the freshness of the data is visible rather than assumed.

STALE FIGURES ARE EXCLUDED, AND THE EXCLUSION IS REPORTED
---------------------------------------------------------
A target whose last recalculation failed keeps its previous figures (Phase 30A),
which may be arbitrarily old. Those records take no part in the achievement rate,
the buckets, Near Target, At Risk, Rule Performance, incentive exposure or the
monetary totals.

They are still COUNTED: `Total Targets` is every selected record and
`Calculation Issues` is how many of them are unreliable, so the difference
between the headline and the metrics is always explainable.

`Ending Soon` is the single exception and includes them, flagged `Needs
Attention`, because that list is selected by DATE and the deadline is still
authoritative. See `target_admin.ending_soon()`.

THE TWO THINGS THAT ARE NEVER SUMMED
------------------------------------
Money across currencies, and quantities across UOMs. See `target_admin`, which
enforces both and is the only place either total is produced.

BUSINESS DEADLINE vs REPORTING WINDOW
-------------------------------------
Every date list here uses `YOB Target Period.end_date`. The rule's
`report_end_date` controls only when recalculation runs; using it for "ending
soon" would report a target as closing when only its reporting window was.
"""

import frappe
from frappe import _
from frappe.utils import flt

from yob_storefront.services import target_admin

#: Table body per view. KPIs and the chart are shared.
VIEWS = ("Overview", "Near Target", "At Risk", "Ending Soon", "Rule Performance")


def execute(filters=None):
    filters = frappe._dict(filters or {})
    view = filters.get("view") or "Overview"

    if view not in VIEWS:
        frappe.throw(_("{0} is not a valid view.").format(view), frappe.ValidationError)

    selection = _selection(filters)

    columns, rows = _body(view, filters, selection)

    return (columns, rows, _message(view, filters, selection),
            _chart(selection), _summary(selection))


def _selection(filters):
    """Only the keys `target_admin` allow-lists. Everything else is dropped."""

    keys = ("company", "target_period", "target_rule", "customer", "customer_group",
            "status", "target_type", "target_scope", "scope_value")

    return {key: filters.get(key) for key in keys if filters.get(key)}


# =========================================================
# BODY
# =========================================================

def _body(view, filters, selection):
    if view == "Near Target":
        return _target_columns(), target_admin.near_target(
            selection, floor=filters.get("near_target_floor"))

    if view == "At Risk":
        return _target_columns(), target_admin.at_risk(
            selection, days=filters.get("horizon_days"),
            ceiling=filters.get("at_risk_ceiling"))

    if view == "Ending Soon":
        return _target_columns(), target_admin.ending_soon(
            selection, days=filters.get("horizon_days"))

    if view == "Rule Performance":
        return _rule_columns(), target_admin.rule_performance(selection)

    # Overview: the whole selection, worst achievement first, so the rows that
    # need attention are at the top rather than buried alphabetically.
    return _target_columns(), target_admin.target_rows(
        selection, order_by="ct.achievement_percentage ASC, tp.end_date ASC", limit=500)


def _target_columns():
    """One row shape for all four target-level views, so a reader re-learns
    nothing when switching between them."""

    return [
        {"label": _("Customer"), "fieldname": "customer", "fieldtype": "Link",
         "options": "Customer", "width": 180},
        {"label": _("Customer Group"), "fieldname": "customer_group", "fieldtype": "Link",
         "options": "Customer Group", "width": 140},
        {"label": _("Target Rule"), "fieldname": "target_rule", "fieldtype": "Link",
         "options": "YOB Target Rule", "width": 170},
        {"label": _("Achievement %"), "fieldname": "achievement_percentage",
         "fieldtype": "Percent", "width": 120},
        {"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 110},
        # Always `OK` except in Ending Soon, which is the one view that admits an
        # errored target because its deadline is still authoritative.
        {"label": _("Calculation State"), "fieldname": "calculation_state",
         "fieldtype": "Data", "width": 150},

        {"label": _("Remaining Amount"), "fieldname": "remaining_amount",
         "fieldtype": "Currency", "options": "currency", "width": 150},
        {"label": _("Remaining Qty"), "fieldname": "remaining_quantity",
         "fieldtype": "Float", "width": 120},
        {"label": _("UOM"), "fieldname": "target_uom", "fieldtype": "Link",
         "options": "UOM", "width": 80},

        # The BUSINESS deadline, from YOB Target Period. Never report_end_date.
        {"label": _("Target End"), "fieldname": "end_date", "fieldtype": "Date",
         "width": 110},
        {"label": _("Days Left"), "fieldname": "days_remaining", "fieldtype": "Int",
         "width": 90},

        {"label": _("Final Incentive"), "fieldname": "final_incentive",
         "fieldtype": "Currency", "options": "currency", "width": 140},
        {"label": _("Current Slab"), "fieldname": "current_slab", "fieldtype": "Data",
         "width": 120},

        {"label": _("Type"), "fieldname": "target_type", "fieldtype": "Data", "width": 90},
        {"label": _("Scope"), "fieldname": "target_scope", "fieldtype": "Data",
         "width": 130},
        {"label": _("Scope Value"), "fieldname": "scope_value", "fieldtype": "Data",
         "width": 140},
        {"label": _("Target Period"), "fieldname": "target_period", "fieldtype": "Link",
         "options": "YOB Target Period", "width": 140},
        {"label": _("Company"), "fieldname": "company", "fieldtype": "Link",
         "options": "Company", "width": 150},
        {"label": _("Last Calculated"), "fieldname": "last_calculated_on",
         "fieldtype": "Datetime", "width": 155},
        {"label": _("Customer Target"), "fieldname": "customer_target", "fieldtype": "Link",
         "options": "YOB Customer Target", "width": 130},
        {"label": _("Currency"), "fieldname": "currency", "fieldtype": "Link",
         "options": "Currency", "width": 80, "hidden": 1},
    ]


def _rule_columns():
    return [
        {"label": _("Target Rule"), "fieldname": "target_rule", "fieldtype": "Link",
         "options": "YOB Target Rule", "width": 200},
        {"label": _("Target Period"), "fieldname": "target_period", "fieldtype": "Link",
         "options": "YOB Target Period", "width": 150},
        {"label": _("Target End"), "fieldname": "end_date", "fieldtype": "Date",
         "width": 110},
        {"label": _("Customers"), "fieldname": "customers", "fieldtype": "Int",
         "width": 100},
        {"label": _("Targets"), "fieldname": "targets", "fieldtype": "Int", "width": 90},
        {"label": _("Reached"), "fieldname": "reached", "fieldtype": "Int", "width": 90},
        {"label": _("In Progress"), "fieldname": "in_progress", "fieldtype": "Int",
         "width": 110},
        {"label": _("Expired"), "fieldname": "expired", "fieldtype": "Int", "width": 90},
        {"label": _("Achievement Rate"), "fieldname": "achievement_rate",
         "fieldtype": "Percent", "width": 140},
        {"label": _("Avg Achievement"), "fieldname": "average_achievement",
         "fieldtype": "Percent", "width": 140},
        {"label": _("Incentive Exposure"), "fieldname": "final_incentive",
         "fieldtype": "Currency", "options": "currency", "width": 160},
        {"label": _("Type"), "fieldname": "target_type", "fieldtype": "Data", "width": 90},
        {"label": _("Currency"), "fieldname": "currency", "fieldtype": "Link",
         "options": "Currency", "width": 80, "hidden": 1},
    ]


# =========================================================
# KPIs
# =========================================================

def _summary(selection):
    """The management KPI row.

    `Achievement Rate` is `(Achieved + Completed) / all selected targets`. The
    formula and the reason for that denominator are documented on
    `target_admin.achievement_rate()`.

    Monetary cards are emitted PER CURRENCY. One company selected gives one card;
    two companies on different base currencies give two, because adding them
    would be a number in no currency at all.
    """

    counts = target_admin.status_counts(selection)
    headline = target_admin.headline_counts(selection)
    issues = target_admin.calculation_issues(selection)

    cards = [
        {"label": _("Customers With Targets"), "value": headline["customers"],
         "datatype": "Int"},
        # Every selected record. The status and achievement cards below cover
        # only the reliable ones, and `Calculation Issues` is the difference.
        {"label": _("Total Targets"), "value": headline["targets"], "datatype": "Int"},
        {"label": _("Calculation Issues"), "value": issues, "datatype": "Int",
         "indicator": "Red" if issues else "Green"},
    ]

    for status in target_admin.ALL_STATUSES:
        cards.append({"label": _(status), "value": counts[status], "datatype": "Int"})

    cards.append({"label": _("Achievement Rate"),
                  "value": flt(target_admin.achievement_rate(selection)),
                  "datatype": "Percent"})

    for exposure in target_admin.incentive_exposure(selection):
        currency = exposure["currency"]
        cards.append({
            "label": _("Incentive Exposure") + (f" ({currency})" if currency else ""),
            "value": exposure["final_incentive"],
            "datatype": "Currency", "currency": currency})

    quantity = target_admin.quantity_summary(selection)

    if quantity["targets"]:
        # Counted, never summed: see `target_admin.quantity_summary`.
        cards.append({"label": _("Quantity Targets"), "value": quantity["targets"],
                      "datatype": "Int"})

    return cards


def _chart(selection):
    """Achievement distribution. The top bucket is open-ended, not a 100% cap."""

    distribution = target_admin.achievement_distribution(selection)

    return {
        "data": {
            "labels": [bucket["label"] for bucket in distribution],
            "datasets": [{"name": _("Customer Targets"),
                          "values": [bucket["targets"] for bucket in distribution]}],
        },
        "type": "bar",
        "colors": ["#7575ff"],
        "fieldtype": "Int",
    }


def _message(view, filters, selection):
    """A short note under the KPIs: what the view means, and how fresh it is.

    The currency caveat is stated in the UI rather than left implicit, because a
    reader seeing two Incentive Exposure cards should know why there are two.
    """

    parts = []

    if view == "Near Target":
        floor = flt(filters.get("near_target_floor") or target_admin.NEAR_TARGET_FLOOR)
        parts.append(_("Below target but reachable: achievement from {0}% up to 100%, "
                       "closest first.").format(floor))
    elif view == "At Risk":
        days = int(filters.get("horizon_days") or target_admin.ENDING_SOON_DAYS)
        ceiling = flt(filters.get("at_risk_ceiling") or target_admin.AT_RISK_CEILING)
        parts.append(_("Still running, ending within {0} days, and below {1}% "
                       "achievement. Targets whose period has already ended are "
                       "Expired, not At Risk.").format(days, ceiling))
    elif view == "Ending Soon":
        days = int(filters.get("horizon_days") or target_admin.ENDING_SOON_DAYS)
        parts.append(_("Active targets whose Target Period ends within {0} days. "
                       "This is the business deadline, not the recalculation "
                       "window.").format(days))
        parts.append(_("This view also lists targets marked Needs Attention, "
                       "because the deadline is authoritative even when the last "
                       "calculation failed. Their achievement figures are stale "
                       "and must not be relied on."))
    elif view == "Rule Performance":
        parts.append(_("Aggregated from existing Customer Targets. Rule eligibility "
                       "is not recalculated."))

    issues = target_admin.calculation_issues(selection)

    if issues:
        parts.append(_("{0} selected target(s) have an unreliable last calculation "
                       "and are excluded from the achievement, incentive and "
                       "monetary figures above. Total Targets still counts them."
                       ).format(issues))

    exposures = target_admin.incentive_exposure(selection)

    if len(exposures) > 1:
        parts.append(_("This selection spans {0} currencies, so monetary totals are "
                       "reported per currency. Select a Company for a single total; "
                       "no exchange rate is applied.").format(len(exposures)))

    newest = target_admin.last_calculated(selection)

    if newest:
        parts.append(_("Last calculated: {0}. Opening this report does not "
                       "recalculate.").format(frappe.format(newest, {"fieldtype":
                                                                     "Datetime"})))

    return "<br>".join(parts) or None
