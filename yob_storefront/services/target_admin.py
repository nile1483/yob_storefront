# Copyright (c) 2026, YOB and Shayona
"""Administrative aggregation over already-calculated Customer Targets.

READ ONLY. NOT A SECOND ENGINE.
-------------------------------
Every number here is read or summed from `YOB Customer Target` rows that Phase
30A's scheduled job produced. Nothing in this module computes an achievement
percentage, converts a UOM, resolves eligibility or evaluates an incentive, and
opening a report never recalculates anything. The canonical flow is unchanged:

    Sales Orders -> target calculation service -> YOB Customer Target -> views

The one derived value is `remaining`, which is presentation arithmetic over two
stored columns (`max(target - achieved, 0)`) and not a re-derivation of anything.
`achievement_percentage` is read as stored and is never capped: 125% is 125.

WHY SQL RATHER THAN PYTHON
-------------------------
A dashboard that loaded every Customer Target and counted in Python would scale
with the catalogue of targets rather than with the answer. Counts, sums and
buckets are done by the database with one query per question, and the row lists
are `LIMIT`-ed. Rule and customer labels come from the rows themselves or one
batched read -- never one lookup per row.

STALE FIGURES ARE NEVER TRUSTED, AND NEVER HIDDEN
-------------------------------------------------
When a recalculation fails, Phase 30A keeps the target's PREVIOUS successful
figures and records `calculation_error`. Those figures may be arbitrarily old, so
they must not reach a number management acts on -- but the record must not vanish
either, or an administrator cannot see that it needs attention.

So the split is explicit:

    counted everywhere        Total Targets, Customers, Rules, Calculation Issues
    valid records only        status counts, achievement rate, buckets,
                              incentive exposure, amount totals, quantity stats,
                              Near Target, At Risk, Rule Performance
    included but FLAGGED      Ending Soon

`Total Targets` therefore counts every selected record while the achievement
metrics count only the reliable ones, and `Calculation Issues` explains the
difference rather than leaving a silent gap.

**Ending Soon includes errored targets, flagged.** It is the one list selected
purely BY DATE, and the date is still authoritative -- hiding a real approaching
deadline would be the worse failure. Near Target and At Risk are selected AND
RANKED by achievement, so a stale percentage would corrupt both the membership
and the ordering; they exclude errored rows entirely.

`calculation_error` text is NEVER published. `calculation_state` carries
`OK` / `Needs Attention` instead, so an administrator learns that a figure is
unreliable without reading an internal exception.

TWO THINGS THAT MUST NEVER BE SUMMED
------------------------------------
* **Money across currencies.** `Customer Target.currency` is the company's base
  currency, so a multi-company selection holds several. Monetary totals are
  therefore always returned GROUPED BY CURRENCY; a caller that wants one number
  must narrow to one company. No exchange rate is applied anywhere.
* **Quantities across UOMs.** 500 Boxes and 250 Kg have no common total. Quantity
  targets are reported as COUNTS and achievement statistics; a target quantity is
  only ever shown on an individual row beside its own UOM.
"""

import frappe
from frappe.utils import cint, flt, getdate, today

TARGET = "YOB Customer Target"

#: Statuses that represent a target the customer reached.
REACHED_STATUSES = ("Achieved", "Completed")

#: Every status, in the order management reads them.
ALL_STATUSES = ("Not Started", "In Progress", "Achieved", "Expired", "Completed")

#: What `calculation_state` publishes. The stored `calculation_error` text is
#: internal and never leaves the server.
STATE_OK = "OK"
STATE_NEEDS_ATTENTION = "Needs Attention"

#: Default boundary for "close enough to chase", as a percentage.
NEAR_TARGET_FLOOR = 80.0

#: Default "ending soon" horizon, in days.
ENDING_SOON_DAYS = 7

#: Default achievement ceiling for the At Risk list, as a percentage.
AT_RISK_CEILING = 75.0

#: Achievement distribution. Lower bound inclusive, upper exclusive; the last
#: bucket is open-ended because achievement is NOT capped at 100.
ACHIEVEMENT_BUCKETS = (
    ("0%", 0.0, 0.0),
    (">0-25%", 0.0, 25.0),
    ("25-50%", 25.0, 50.0),
    ("50-75%", 50.0, 75.0),
    ("75-<100%", 75.0, 100.0),
    ("100-<120%", 100.0, 120.0),
    ("120%+", 120.0, None),
)

#: Filters a caller may narrow by. Anything else is ignored rather than passed to
#: SQL -- a report filter set is browser input even inside Desk.
ALLOWED_FILTERS = {
    "customer": "ct.customer",
    "company": "ct.company",
    "target_rule": "ct.target_rule",
    "target_period": "ct.target_period",
    "status": "ct.status",
    "target_type": "ct.target_type",
    "target_scope": "ct.target_scope",
    "scope_value": "ct.scope_value",
    "incentive_type": "ct.incentive_type",
    "reward_status": "ct.reward_status",
    "customer_group": "cust.customer_group",
}


# =========================================================
# PREDICATE
# =========================================================

def build_conditions(filters=None, *, valid_only=True):
    """`(sql, values)` for the selection, from an allow-list only.

    A fieldname never comes from the caller: `ALLOWED_FILTERS` maps a known key
    to a known column, so an unexpected filter is dropped instead of reaching the
    query. Values are always bound.

    `valid_only` (the default) excludes records whose last calculation failed,
    because their stored figures may be stale. Callers that COUNT records rather
    than measure them -- and the customer-specific view, where the administrator
    needs to see the record exists -- pass `False` and read `calculation_state`.
    """

    filters = filters or {}
    conditions = ["1 = 1"]
    values = {}

    for key, column in ALLOWED_FILTERS.items():
        value = filters.get(key)

        if value in (None, "", []):
            continue

        if isinstance(value, (list, tuple, set)):
            values[key] = tuple(value)
            conditions.append(f"{column} IN %({key})s")
        else:
            values[key] = value
            conditions.append(f"{column} = %({key})s")

    if valid_only:
        # A target whose last calculation failed carries no trustworthy figures,
        # so it is excluded from every MEASUREMENT. Including it would let a
        # possibly months-old percentage move an achievement rate, a bucket or a
        # call list. It is still counted by `headline_counts()` and surfaced by
        # `calculation_issues()`, so nothing disappears unexplained.
        conditions.append("IFNULL(ct.calculation_error, '') = ''")

    return " AND ".join(conditions), values


def _from_clause():
    """The shared FROM. `YOB Target Period` carries the BUSINESS deadline.

    `report_start_date`/`report_end_date` live on the rule and control only when
    recalculation runs; they are deliberately not joined here, so no admin list
    can accidentally treat a reporting window as a deadline.
    """

    return """
        FROM `tabYOB Customer Target` ct
        LEFT JOIN `tabYOB Target Period` tp ON tp.name = ct.target_period
        LEFT JOIN `tabCustomer` cust ON cust.name = ct.customer
        LEFT JOIN `tabYOB Target Rule` tr ON tr.name = ct.target_rule
    """


# =========================================================
# ROWS
# =========================================================

ROW_FIELDS = """
    ct.name AS customer_target, ct.customer, cust.customer_group, ct.company,
    ct.target_rule, ct.target_period, tp.start_date, tp.end_date,
    ct.target_scope, ct.scope_value, ct.target_type,
    ct.target_amount, ct.achieved_amount, ct.currency,
    ct.target_quantity, ct.achieved_quantity, ct.target_uom,
    ct.achievement_percentage, ct.status, ct.reward_status,
    ct.incentive_type, ct.current_slab, ct.eligible_incentive, ct.final_incentive,
    ct.last_calculated_on,
    CASE WHEN IFNULL(ct.calculation_error, '') = ''
         THEN 'OK' ELSE 'Needs Attention' END AS calculation_state
"""


def target_rows(filters=None, *, order_by=None, limit=None, valid_only=True):
    """The selected Customer Targets, with `remaining` and `days_remaining` added.

    `remaining` is `max(target - achieved, 0)` in whichever unit the target uses;
    the other is None, so a row never implies a total it does not have.

    `valid_only=False` includes targets whose last calculation failed. Each row
    carries `calculation_state`, so the caller can mark the stored figures as
    unreliable rather than presenting them as current.
    """

    where, values = build_conditions(filters, valid_only=valid_only)
    order = order_by or "ct.customer ASC, tp.end_date ASC, ct.target_rule ASC"
    limit_sql = f"LIMIT {cint(limit)}" if limit else ""

    rows = frappe.db.sql(
        f"SELECT {ROW_FIELDS} {_from_clause()} WHERE {where} ORDER BY {order} {limit_sql}",
        values, as_dict=True)

    for row in rows:
        _decorate(row)

    return rows


def _decorate(row):
    """Presentation-only values. No achievement is recomputed."""

    is_amount = row.get("target_type") == "Amount"

    if is_amount:
        row["remaining_amount"] = max(
            flt(row.get("target_amount")) - flt(row.get("achieved_amount")), 0.0)
        row["remaining_quantity"] = None
    else:
        row["remaining_amount"] = None
        row["remaining_quantity"] = max(
            flt(row.get("target_quantity")) - flt(row.get("achieved_quantity")), 0.0)

    end = row.get("end_date")
    row["days_remaining"] = (getdate(end) - getdate(today())).days if end else None

    return row


# =========================================================
# COUNTS AND KPIs
# =========================================================

def status_counts(filters=None):
    """`{status: count}` for every status, zeros included.

    Zeros are present so a caller renders a stable set of cards rather than
    discovering that a status is simply missing.
    """

    where, values = build_conditions(filters)

    rows = frappe.db.sql(
        f"SELECT ct.status, COUNT(*) AS n {_from_clause()} WHERE {where} GROUP BY ct.status",
        values, as_dict=True)

    counts = {status: 0 for status in ALL_STATUSES}

    for row in rows:
        if row.status in counts:
            counts[row.status] = cint(row.n)

    return counts


def headline_counts(filters=None):
    """Record COUNTS over the whole selection, errored records included.

    `Total Targets` means "records that exist", so a failed calculation must not
    reduce it -- that would be a silent gap. The achievement metrics are computed
    over valid records only and `calculation_issues()` reports the difference, so
    `Total Targets - Calculation Issues` is always explainable.
    """

    where, values = build_conditions(filters, valid_only=False)

    row = frappe.db.sql(
        f"""
        SELECT COUNT(*) AS targets,
               COUNT(DISTINCT ct.customer) AS customers,
               COUNT(DISTINCT ct.target_rule) AS rules
        {_from_clause()} WHERE {where}
        """,
        values, as_dict=True)

    answer = row[0] if row else {}

    return {
        "targets": cint(answer.get("targets")),
        # DISTINCT because one customer may hold several targets and must count
        # once in "customers with targets".
        "customers": cint(answer.get("customers")),
        "rules": cint(answer.get("rules")),
    }


def calculation_issues(filters=None):
    """How many selected targets have an unreliable last calculation.

    Published so management can see that records were excluded from the
    achievement metrics, and how many. The stored reason stays internal.
    """

    where, values = build_conditions(filters, valid_only=False)

    rows = frappe.db.sql(
        f"""
        SELECT COUNT(*) AS n {_from_clause()}
        WHERE {where} AND IFNULL(ct.calculation_error, '') != ''
        """,
        values)

    return cint(rows[0][0]) if rows and rows[0] else 0


def achievement_rate(filters=None):
    """Share of the selected targets that reached their target, as a percentage.

        achievement_rate = (Achieved + Completed) / all selected targets * 100

    DELIBERATELY the whole selection as the denominator. The alternative --
    excluding `Not Started` as "not yet an outcome" -- is wrong here, because
    Phase 30A assigns `Not Started` both to a period that has not opened AND to
    an open period with zero sales. Those are opposite facts under one label, so
    a denominator that dropped them would quietly flatter the rate exactly when
    nobody was buying.

    Narrow by Target Period if you want the rate for a concluded period only.
    Returns 0.0 for an empty selection rather than dividing by zero.

    Computed over VALID records only: a target whose figures may be months stale
    can neither count as reached nor as missed, so it is excluded from both sides
    of the fraction rather than guessed at. `calculation_issues()` reports how
    many were left out.
    """

    counts = status_counts(filters)
    total = sum(counts.values())

    if not total:
        return 0.0

    reached = sum(counts[status] for status in REACHED_STATUSES)

    return reached / total * 100.0


def incentive_exposure(filters=None):
    """`[{currency, eligible_incentive, final_incentive, targets}]`, per currency.

    ALWAYS grouped, never one number. `Customer Target.currency` is the company's
    base currency, so a selection spanning two companies holds two currencies and
    a single total would be a lie told in whichever symbol got rendered. No
    exchange rate is applied anywhere in Phase 31.

    A caller wanting one monetary figure narrows to one company; the reports say
    so in the UI rather than guessing.
    """

    where, values = build_conditions(filters)

    rows = frappe.db.sql(
        f"""
        SELECT IFNULL(ct.currency, '') AS currency,
               SUM(ct.eligible_incentive) AS eligible_incentive,
               SUM(ct.final_incentive) AS final_incentive,
               COUNT(*) AS targets
        {_from_clause()} WHERE {where}
        GROUP BY IFNULL(ct.currency, '')
        ORDER BY SUM(ct.final_incentive) DESC
        """,
        values, as_dict=True)

    return [{
        "currency": row.currency or None,
        "eligible_incentive": flt(row.eligible_incentive),
        "final_incentive": flt(row.final_incentive),
        "targets": cint(row.targets),
    } for row in rows]


def amount_totals(filters=None):
    """Amount-target money, per currency. Quantity targets are excluded entirely.

    Mixing them would produce a "total target" that is part money and part
    unitless quantity -- the fake monetary total this phase must not publish.
    """

    amount_filters = dict(filters or {})
    amount_filters["target_type"] = "Amount"

    where, values = build_conditions(amount_filters)

    rows = frappe.db.sql(
        f"""
        SELECT IFNULL(ct.currency, '') AS currency,
               SUM(ct.target_amount) AS target_amount,
               SUM(ct.achieved_amount) AS achieved_amount,
               COUNT(*) AS targets
        {_from_clause()} WHERE {where}
        GROUP BY IFNULL(ct.currency, '')
        ORDER BY SUM(ct.target_amount) DESC
        """,
        values, as_dict=True)

    return [{
        "currency": row.currency or None,
        "target_amount": flt(row.target_amount),
        "achieved_amount": flt(row.achieved_amount),
        "targets": cint(row.targets),
    } for row in rows]


def quantity_summary(filters=None):
    """Quantity targets as COUNTS and achievement statistics only.

    No summed target quantity, by design: `500 Boxes + 250 Kg` has no total, and
    publishing `750` would be worse than publishing nothing. A quantity and its
    UOM appear together on individual rows.
    """

    quantity_filters = dict(filters or {})
    quantity_filters["target_type"] = "Quantity"

    where, values = build_conditions(quantity_filters)

    rows = frappe.db.sql(
        f"""
        SELECT COUNT(*) AS targets,
               COUNT(DISTINCT IFNULL(ct.target_uom, '')) AS distinct_uoms,
               AVG(ct.achievement_percentage) AS average_achievement
        {_from_clause()} WHERE {where}
        """,
        values, as_dict=True)

    answer = rows[0] if rows else {}
    counts = status_counts(quantity_filters)

    return {
        "targets": cint(answer.get("targets")),
        "distinct_uoms": cint(answer.get("distinct_uoms")),
        "average_achievement": flt(answer.get("average_achievement")),
        "reached": sum(counts[status] for status in REACHED_STATUSES),
    }


# =========================================================
# DISTRIBUTION
# =========================================================

def achievement_distribution(filters=None):
    """`[{label, targets}]` over `ACHIEVEMENT_BUCKETS`.

    One pass in SQL with a CASE, rather than a query per bucket. The top bucket is
    open-ended because achievement is not capped -- a customer at 300% belongs
    somewhere, and that somewhere is not "100%".
    """

    where, values = build_conditions(filters)

    cases = []

    for index, (label, low, high) in enumerate(ACHIEVEMENT_BUCKETS):
        if low == 0.0 and high == 0.0:
            cases.append(f"WHEN IFNULL(ct.achievement_percentage, 0) <= 0 THEN {index}")
        elif high is None:
            cases.append(
                f"WHEN IFNULL(ct.achievement_percentage, 0) >= {low} THEN {index}")
        else:
            cases.append(
                f"WHEN IFNULL(ct.achievement_percentage, 0) > {low} "
                f"AND IFNULL(ct.achievement_percentage, 0) < {high} THEN {index}"
                if low == 0.0 else
                f"WHEN IFNULL(ct.achievement_percentage, 0) >= {low} "
                f"AND IFNULL(ct.achievement_percentage, 0) < {high} THEN {index}")

    rows = frappe.db.sql(
        f"""
        SELECT CASE {' '.join(cases)} ELSE -1 END AS bucket, COUNT(*) AS n
        {_from_clause()} WHERE {where}
        GROUP BY bucket
        """,
        values, as_dict=True)

    found = {cint(row.bucket): cint(row.n) for row in rows}

    return [{"label": label, "targets": found.get(index, 0)}
            for index, (label, _low, _high) in enumerate(ACHIEVEMENT_BUCKETS)]


# =========================================================
# OPERATIONAL LISTS
# =========================================================

def near_target(filters=None, *, floor=None, limit=200):
    """Below target but close enough to chase.

        floor <= achievement_percentage < 100   (floor defaults to 80)
        and the target has not already been reached

    Sorted CLOSEST FIRST, then by the soonest business deadline -- the order a
    salesperson works a call list in.

    The status check is belt and braces beside the `< 100` bound: a `Completed`
    target settled below 100% is an administrative fact, not a chase.
    """

    floor = flt(floor) if floor not in (None, "") else NEAR_TARGET_FLOOR
    where, values = build_conditions(filters)
    values["near_floor"] = floor
    values["reached"] = REACHED_STATUSES

    rows = frappe.db.sql(
        f"""
        SELECT {ROW_FIELDS} {_from_clause()}
        WHERE {where}
          AND ct.achievement_percentage >= %(near_floor)s
          AND ct.achievement_percentage < 100
          AND ct.status NOT IN %(reached)s
        ORDER BY ct.achievement_percentage DESC, tp.end_date ASC
        LIMIT {cint(limit)}
        """,
        values, as_dict=True)

    return [_decorate(row) for row in rows]


def at_risk(filters=None, *, days=None, ceiling=None, limit=200):
    """Still running, ending soon, and materially behind.

        end_date between today and today + days   (days defaults to 7)
        and achievement_percentage < ceiling      (ceiling defaults to 75)
        and the target has not already been reached

    Deterministic, not predictive: no run-rate, no extrapolation, no model.

    **A target whose period has already ended is NOT at risk** -- it is Expired,
    which is a separate outcome with its own count. Risk is about a deadline a
    customer can still act on, so `end_date >= today` is part of the rule.
    """

    days = cint(days) if days not in (None, "") else ENDING_SOON_DAYS
    ceiling = flt(ceiling) if ceiling not in (None, "") else AT_RISK_CEILING

    where, values = build_conditions(filters)
    values.update({"risk_ceiling": ceiling, "horizon_days": days,
                   "reached": REACHED_STATUSES})

    rows = frappe.db.sql(
        f"""
        SELECT {ROW_FIELDS} {_from_clause()}
        WHERE {where}
          AND tp.end_date IS NOT NULL
          AND tp.end_date >= CURDATE()
          AND tp.end_date <= DATE_ADD(CURDATE(), INTERVAL %(horizon_days)s DAY)
          AND IFNULL(ct.achievement_percentage, 0) < %(risk_ceiling)s
          AND ct.status NOT IN %(reached)s
        ORDER BY tp.end_date ASC, ct.achievement_percentage ASC
        LIMIT {cint(limit)}
        """,
        values, as_dict=True)

    return [_decorate(row) for row in rows]


def ending_soon(filters=None, *, days=None, limit=200):
    """Active targets whose BUSINESS deadline is near.

    The deadline is `YOB Target Period.end_date`. `YOB Target Rule.report_end_date`
    is deliberately not consulted: it controls only when recalculation runs, and
    using it here would tell management a target was closing when in fact only the
    reporting window was.

    Unlike At Risk this applies no achievement bound -- a target at 95% with two
    days left is still ending soon, and that is useful to see.

    THE ONE LIST THAT INCLUDES AN ERRORED TARGET
    --------------------------------------------
    This list is selected purely BY DATE, and the date is still authoritative
    however stale the achievement is: hiding a real approaching deadline would be
    the worse failure. Such a row arrives with
    `calculation_state = "Needs Attention"` so its percentage is visibly not to
    be trusted.

    Near Target and At Risk are selected AND RANKED by achievement, so a stale
    percentage would corrupt both membership and ordering -- they exclude errored
    rows outright.
    """

    days = cint(days) if days not in (None, "") else ENDING_SOON_DAYS

    where, values = build_conditions(filters, valid_only=False)
    values.update({"horizon_days": days, "reached": REACHED_STATUSES})

    rows = frappe.db.sql(
        f"""
        SELECT {ROW_FIELDS} {_from_clause()}
        WHERE {where}
          AND tp.end_date IS NOT NULL
          AND tp.end_date >= CURDATE()
          AND tp.end_date <= DATE_ADD(CURDATE(), INTERVAL %(horizon_days)s DAY)
          AND ct.status NOT IN %(reached)s
        ORDER BY tp.end_date ASC,
                 CASE WHEN IFNULL(ct.calculation_error, '') = '' THEN 0 ELSE 1 END ASC,
                 ct.achievement_percentage ASC
        LIMIT {cint(limit)}
        """,
        values, as_dict=True)

    return [_decorate(row) for row in rows]


# =========================================================
# RULE PERFORMANCE
# =========================================================

def rule_performance(filters=None, *, limit=200):
    """Per-Target-Rule aggregation over the EXISTING Customer Targets.

    No eligibility is recomputed: a rule's population is the Customer Targets the
    calculation job already generated for it, which is the only honest definition
    available here.

    `currency` comes from `MIN(currency)` with a `distinct_currencies` count
    beside it: one rule belongs to one company and therefore one base currency in
    practice, and the count makes a violation of that visible instead of silently
    summing two.
    """

    where, values = build_conditions(filters)
    values["reached"] = REACHED_STATUSES

    rows = frappe.db.sql(
        f"""
        SELECT ct.target_rule,
               MIN(ct.target_period) AS target_period,
               MIN(ct.target_type) AS target_type,
               COUNT(*) AS targets,
               COUNT(DISTINCT ct.customer) AS customers,
               SUM(CASE WHEN ct.status IN %(reached)s THEN 1 ELSE 0 END) AS reached,
               SUM(CASE WHEN ct.status = 'Expired' THEN 1 ELSE 0 END) AS expired,
               SUM(CASE WHEN ct.status = 'In Progress' THEN 1 ELSE 0 END) AS in_progress,
               AVG(ct.achievement_percentage) AS average_achievement,
               SUM(ct.final_incentive) AS final_incentive,
               MIN(IFNULL(ct.currency, '')) AS currency,
               COUNT(DISTINCT IFNULL(ct.currency, '')) AS distinct_currencies,
               MIN(tp.end_date) AS end_date
        {_from_clause()} WHERE {where}
        GROUP BY ct.target_rule
        ORDER BY AVG(ct.achievement_percentage) DESC, ct.target_rule ASC
        LIMIT {cint(limit)}
        """,
        values, as_dict=True)

    for row in rows:
        targets = cint(row.targets)
        row["achievement_rate"] = (cint(row.reached) / targets * 100.0) if targets else 0.0
        row["average_achievement"] = flt(row.average_achievement)
        row["final_incentive"] = flt(row.final_incentive)
        # Blanked rather than shown against the wrong symbol if a rule somehow
        # spans currencies.
        if cint(row.distinct_currencies) > 1:
            row["currency"] = None
            row["final_incentive"] = None

    return rows


# =========================================================
# FRESHNESS
# =========================================================

def last_calculated(filters=None):
    """The newest `last_calculated_on` in the selection, or None.

    Published so an administrator can see how fresh the figures are. Phase 31
    deliberately offers NO recalculation action: the scheduled job owns that, and
    a Desk button would be a second way to start it with different security
    semantics.
    """

    where, values = build_conditions(filters)

    rows = frappe.db.sql(
        f"SELECT MAX(ct.last_calculated_on) AS newest {_from_clause()} WHERE {where}",
        values)

    return rows[0][0] if rows and rows[0] else None
