# Copyright (c) 2026, YOB and Shayona
"""Target Management orchestration: which rules are due, for whom, and the run.

INTERNAL ONLY
-------------
Nothing here is whitelisted. The reference exposed
`@frappe.whitelist() generate_customer_targets(customer, ...)` and wrote with
`ignore_permissions=True`, so any authenticated session could name another
customer and trigger writes against their targets. Phase 30A needs no public
surface at all, and the storefront read contract is a later, reviewed phase that
will derive the customer from `yob_auth` context rather than from a parameter.

The only permission elevation is `flags.from_target_calculation` on the write of
a Customer Target, set inside this module. It is a flag on one document, not a
global switch and not a reachable method: the DocType refuses every write that
does not carry it.

WHAT "DUE" MEANS
----------------
    submitted  AND  enabled  AND  period enabled
    AND  report_start_date <= calculation_date <= report_end_date

The report window gates THIS decision and nothing else. It never reaches the
achievement SQL -- see `target_achievement.qualifying_date_range()`.

IDEMPOTENT BY CONSTRUCTION
--------------------------
One Customer Target per `customer + target_rule`, upserted. Running twice over
unchanged sales rewrites the same figures onto the same rows.

ONE BAD RULE IS NOT EVERYONE'S PROBLEM
--------------------------------------
Each customer/rule pair is calculated inside its own savepoint. A rule with an
unconvertible UOM records its error on its own row and the run continues; the
reference let one exception end the job.
"""

import frappe
from frappe.utils import flt, getdate, now_datetime, today

from yob_storefront.services import target_achievement as achievement
from yob_storefront.services import target_incentive as incentive
from yob_storefront.services.target_achievement import TargetCalculationError

RULE_DOCTYPE = "YOB Target Rule"
TARGET_DOCTYPE = "YOB Customer Target"


# =========================================================
# DUE RULES
# =========================================================

def due_target_rules(calculation_date=None):
    """Submitted, enabled rules whose report window contains the given date.

    The period's own `enabled` flag is joined rather than checked per rule: a
    disabled period must stop processing immediately, and a rule may legitimately
    stay submitted while its period is switched off.
    """

    calculation_date = getdate(calculation_date or today())

    return frappe.db.sql(
        """
        SELECT
            tr.name, tr.company, tr.target_period,
            tr.target_scope, tr.scope_value,
            tr.target_type, tr.target_amount, tr.currency,
            tr.target_quantity, tr.target_uom,
            tr.incentive_type, tr.incentive_amount, tr.incentive_percentage,
            tr.report_start_date, tr.report_end_date,
            tp.start_date AS period_start, tp.end_date AS period_end
        FROM `tabYOB Target Rule` tr
        INNER JOIN `tabYOB Target Period` tp ON tp.name = tr.target_period
        WHERE tr.docstatus = 1
          AND tr.enabled = 1
          AND tp.enabled = 1
          AND tr.report_start_date <= %(calculation_date)s
          AND tr.report_end_date >= %(calculation_date)s
        ORDER BY tr.name
        """,
        {"calculation_date": calculation_date},
        as_dict=True,
    )


# =========================================================
# ELIGIBILITY
# =========================================================

def eligible_customers(target_rule):
    """Customers matching ANY condition row. OR semantics, de-duplicated.

    De-duplication is WITHIN one rule only: a customer named directly and also
    present in a listed group is one customer, so they get one Customer Target
    for this rule.

    It is NOT across rules. Several rules may apply to the same customer and each
    produces its own independent Customer Target -- that is required, because the
    storefront will later show a customer their January and February targets side
    by side. There is deliberately no "best rule wins".
    """

    rows = frappe.get_all(
        "YOB Target Rule Condition",
        filters={"parent": target_rule, "parenttype": RULE_DOCTYPE},
        fields=["condition_type", "condition_value"],
        limit_page_length=0,
    )

    customers = []
    groups = []

    for row in rows:
        if not row.condition_value:
            continue
        if row.condition_type == "Customer":
            customers.append(row.condition_value)
        elif row.condition_type == "Customer Group":
            groups.append(row.condition_value)

    found = []

    if customers:
        # Filtered through the Customer table so a condition naming a deleted or
        # disabled customer cannot produce a target for a party that cannot buy.
        found += frappe.get_all(
            "Customer", filters={"name": ["in", customers], "disabled": 0},
            pluck="name", limit_page_length=0)

    if groups:
        # Exact group match, no subtree. The storefront has no descendant
        # convention, and widening this silently would change every existing rule.
        found += frappe.get_all(
            "Customer", filters={"customer_group": ["in", groups], "disabled": 0},
            pluck="name", limit_page_length=0)

    return sorted(dict.fromkeys(found))


# =========================================================
# UPSERT
# =========================================================

def upsert_customer_target(customer, rule_name):
    """The one Customer Target for this pair, created if absent.

    Insert-then-recover rather than check-then-insert. Two scheduler workers
    reaching the same pair would both pass an existence check and both insert;
    the unique index makes one fail, and that loser re-reads the row the winner
    created instead of raising.
    """

    name = frappe.db.get_value(
        TARGET_DOCTYPE, {"customer": customer, "target_rule": rule_name}, "name")

    if name:
        return frappe.get_doc(TARGET_DOCTYPE, name)

    doc = frappe.new_doc(TARGET_DOCTYPE)
    doc.customer = customer
    doc.target_rule = rule_name
    doc.flags.from_target_calculation = True

    try:
        doc.insert(ignore_permissions=True)
    except frappe.UniqueValidationError:
        frappe.db.rollback(save_point="yob_target_upsert")
        existing = frappe.db.get_value(
            TARGET_DOCTYPE, {"customer": customer, "target_rule": rule_name}, "name")
        if not existing:
            raise
        return frappe.get_doc(TARGET_DOCTYPE, existing)

    return doc


# =========================================================
# ONE TARGET
# =========================================================

def calculate_customer_target(rule, customer, calculation_date=None):
    """Create or update one Customer Target and return it.

    `rule` is a dict as `due_target_rules()` produces, so the caller may supply
    the same row to every customer without re-reading it.
    """

    calculation_date = getdate(calculation_date or today())

    frappe.db.savepoint("yob_target_upsert")
    doc = upsert_customer_target(customer, rule["name"])

    previous_status = doc.status

    # ---------------- snapshot ----------------
    # Copied onto the row so the figure can be understood later without
    # re-reading a rule that may since have been amended.
    doc.target_period = rule.get("target_period")
    doc.company = rule.get("company")
    doc.target_scope = rule.get("target_scope")
    doc.scope_value = rule.get("scope_value")
    doc.target_type = rule.get("target_type")
    doc.target_amount = flt(rule.get("target_amount"))
    doc.currency = rule.get("currency")
    doc.target_quantity = flt(rule.get("target_quantity"))
    doc.target_uom = rule.get("target_uom")
    doc.incentive_type = rule.get("incentive_type")

    window = achievement.qualifying_date_range(
        rule["period_start"], rule["period_end"], calculation_date)

    try:
        if window is None:
            # The period has not opened. Zero is the honest answer, not an error.
            achieved_amount = 0.0
            achieved_quantity = 0.0
        else:
            from_date, to_date = window

            achieved_amount = achievement.achieved_amount(
                customer=customer, company=rule["company"],
                scope=rule["target_scope"], scope_value=rule.get("scope_value"),
                from_date=from_date, to_date=to_date)

            achieved_quantity = 0.0

            if rule["target_type"] == "Quantity":
                achieved_quantity = achievement.achieved_quantity(
                    customer=customer, company=rule["company"],
                    scope=rule["target_scope"], scope_value=rule.get("scope_value"),
                    from_date=from_date, to_date=to_date,
                    target_uom=rule["target_uom"])

    except TargetCalculationError as exc:
        # The previous figures stay exactly as they were. Replacing them with 0
        # would look like a collapse in sales rather than a configuration fault.
        doc.calculation_error = str(exc)
        doc.flags.from_target_calculation = True
        doc.save(ignore_permissions=True)
        frappe.log_error(
            title="YOB Target Management: calculation skipped",
            message=f"rule={rule['name']} customer={customer}\n\n{exc}")
        return doc

    percentage = incentive.achievement_percentage(
        target_type=rule["target_type"],
        target_amount=rule.get("target_amount"),
        target_quantity=rule.get("target_quantity"),
        achieved_amount=achieved_amount,
        achieved_quantity=achieved_quantity)

    result = incentive.calculate_incentive(
        incentive_type=rule["incentive_type"],
        slabs=_slabs(rule["name"]),
        achievement=percentage,
        # Always the amount, even for a Quantity target: a percentage of a
        # quantity would be a quantity, not money.
        achieved_amount=achieved_amount,
        incentive_amount=rule.get("incentive_amount"),
        incentive_percentage=rule.get("incentive_percentage"))

    doc.achieved_amount = achieved_amount
    doc.achieved_quantity = achieved_quantity
    doc.achievement_percentage = percentage

    for field, value in result.items():
        setattr(doc, field, value)

    doc.status = incentive.calculate_status(
        achievement=percentage,
        period_start=rule["period_start"],
        period_end=rule["period_end"],
        calculation_date=calculation_date,
        previous_status=previous_status)

    if not doc.reward_status:
        doc.reward_status = "Pending"

    doc.calculation_error = None
    doc.last_calculated_on = now_datetime()

    doc.flags.from_target_calculation = True
    doc.save(ignore_permissions=True)

    return doc


def _slabs(rule_name):
    return frappe.get_all(
        "YOB Target Rule Incentive Slab",
        filters={"parent": rule_name, "parenttype": RULE_DOCTYPE},
        fields=["from_achievement", "to_achievement", "incentive_amount",
                "incentive_percentage", "max_incentive"],
        order_by="from_achievement asc",
        limit_page_length=0,
    )


# =========================================================
# THE RUN  (scheduler entry point)
# =========================================================

def recalculate_due_targets(calculation_date=None):
    """Recalculate every due rule for every eligible customer.

    Wired to the daily scheduler. Safe to call again at any time: it is an
    upsert over a deterministic input, so a second run over unchanged sales
    produces identical figures.

    Returns a small summary, which is what makes it testable without reading
    the database back.
    """

    calculation_date = getdate(calculation_date or today())

    summary = {"calculation_date": calculation_date, "rules": 0, "targets": 0,
               "failures": 0}

    for rule in due_target_rules(calculation_date):
        summary["rules"] += 1

        for customer in eligible_customers(rule["name"]):
            # Per pair, so one bad configuration cannot abort the rest of the run
            # or leave a half-written row behind.
            frappe.db.savepoint("yob_target_pair")
            try:
                calculate_customer_target(rule, customer, calculation_date)
                summary["targets"] += 1
            except Exception:
                frappe.db.rollback(save_point="yob_target_pair")
                summary["failures"] += 1
                frappe.log_error(
                    title="YOB Target Management: target failed",
                    message=(f"rule={rule['name']} customer={customer}\n\n"
                             f"{frappe.get_traceback()}"))

    return summary
