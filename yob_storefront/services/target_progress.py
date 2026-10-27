# Copyright (c) 2026, YOB and Shayona
"""A Target Progress block, resolved for the buyer looking at it.

READ ONLY. NO RECALCULATION.
---------------------------
This reads the figures Phase 30A's scheduled job already stored on
`YOB Customer Target` and does not compute an achievement, convert a UOM, query a
Sales Order or touch a Target Rule. Opening a storefront page must never run an
aggregation over a period's sales, and `recalculate_due_targets()` is not
reachable from here -- a test asserts both.

THREE DATE CONCEPTS, AND ONLY ONE OF THEM IS HERE
-------------------------------------------------
    Target Period    which sales count         -- Phase 30A, YOB Target Period
    Report Window    when recalculation runs   -- Phase 30A, YOB Target Rule
    Show From/Until  when a block displays it  -- THIS layer, on the block row

A January target may still be displayed on 10 February: its sales period closed,
its report window may still be open, and its display window is a third thing
again. This module reads the third and never the other two -- it has no report
date in scope at all.

WHOSE TARGETS
-------------
`customer_doc` is passed in by `content_service.project_block()`, which receives
it from `cms.get_page` / `cms.get_route_content`, which obtain it from
`get_storefront_customer(auth_context)`. There is no customer parameter anywhere
on the path and nothing here accepts one, so a browser cannot name another party:
the only Customer this function can ever see is the authenticated one.

WHAT A CUSTOMER SEES
--------------------
Only the rules the block lists, only inside their display window, only while the
rule is still customer-visible, and only where a calculated target of their own
exists. A configured rule that does not apply to this buyer produces no card and
no explanation -- absence is the answer, because "you are not in Dealers" is
information about the merchant's configuration.
"""

import frappe
from frappe.utils import cint, flt, getdate, today

#: Statuses a customer may be shown. `Completed` is an administrative settlement
#: state and is surfaced, because a buyer whose reward was settled should see
#: that rather than a stale `Achieved`.
VISIBLE_STATUSES = ("Not Started", "In Progress", "Achieved", "Expired", "Completed")


def project_target_progress(block, customer_doc):
    """`{"targets": [...]}` for this block and this customer.

    Always returns the key, always a list. An empty list is an ordinary answer:
    the block is configured but nothing is displayable today.
    """

    if not customer_doc:
        # A page can only be fetched by an authenticated customer, so this is a
        # programming guard rather than a reachable state.
        return {"targets": []}

    rows = block.get("target_rows") or []
    on = getdate(today())

    targets = []

    # Row order IS display order, matching how slides and promo cards behave.
    for row in rows:
        entry = _project_row(row, customer_doc.name, on)
        if entry:
            targets.append(entry)

    return {"targets": targets}


def _project_row(row, customer, on):
    """One configured rule for one customer, or None when nothing is shown."""

    if not row.target_rule or not row.show_from or not row.show_until:
        return None

    # 1. The display window. Inclusive at both ends.
    if not (getdate(row.show_from) <= on <= getdate(row.show_until)):
        return None

    rule = frappe.db.get_value(
        "YOB Target Rule",
        row.target_rule,
        ["name", "rule_name", "description", "customer_visible", "docstatus",
         "target_period", "incentive_type"],
        as_dict=True,
    )

    # 2. Still customer-visible, still submitted. Re-checked rather than trusted:
    # the block was validated when it was saved, and a rule can be amended or
    # un-published afterwards.
    if not rule or not rule.customer_visible or cint(rule.docstatus) != 1:
        return None

    # 3 + 4. This customer's own calculated target for that rule. Filtered by
    # BOTH columns, so the composite index answers it and no other party's row
    # can be reached.
    target = frappe.db.get_value(
        "YOB Customer Target",
        {"customer": customer, "target_rule": rule.name},
        ["name", "target_type", "target_amount", "target_quantity", "currency",
         "target_uom", "achieved_amount", "achieved_quantity",
         "achievement_percentage", "status", "incentive_type", "current_slab",
         "eligible_incentive", "final_incentive", "last_calculated_on",
         "target_scope", "scope_value", "calculation_error"],
        as_dict=True,
    )

    # 5. No calculated target: the rule does not apply to this buyer, or the job
    # has not reached them yet. Nothing is fabricated -- a zeroed card would
    # claim a target they do not have.
    if not target:
        return None

    # A target whose last calculation failed has no trustworthy figures. Showing
    # them as zero would misreport sales the buyer actually made, so the card is
    # withheld entirely. The stored reason is internal and is never published.
    if target.calculation_error:
        return None

    if target.status not in VISIBLE_STATUSES:
        return None

    return _card(rule, target)


def _card(rule, target):
    """The presentation-ready entry. Every derived value is computed HERE."""

    period = frappe.db.get_value(
        "YOB Target Period", rule.target_period,
        ["start_date", "end_date"], as_dict=True) or {}

    is_amount = target.target_type == "Amount"

    target_value = flt(target.target_amount) if is_amount else flt(target.target_quantity)
    achieved_value = flt(target.achieved_amount) if is_amount \
        else flt(target.achieved_quantity)

    card = {
        "customer_target": target.name,
        "target_rule": rule.name,
        "title": rule.rule_name,
        "description": rule.description or None,

        "target_period_start": period.get("start_date"),
        "target_period_end": period.get("end_date"),

        "target_scope": target.target_scope,
        "scope_value": target.scope_value or None,

        "target_type": target.target_type,
        "currency": target.currency or None,
        "uom": target.target_uom or None,

        "target_amount": flt(target.target_amount) if is_amount else None,
        "achieved_amount": flt(target.achieved_amount),
        "target_quantity": flt(target.target_quantity) if not is_amount else None,
        "achieved_quantity": flt(target.achieved_quantity) if not is_amount else None,

        # NOT capped. 125% achievement publishes 125: a progress bar may stop
        # drawing at 100, but the backend must not decide that a buyer who beat
        # their target merely met it.
        "achievement_percentage": flt(target.achievement_percentage),

        # Floored at zero instead of going negative, because "you still need
        # -5,000" is not a sentence. Exceeding the target leaves 0 remaining.
        "remaining_amount": max(target_value - achieved_value, 0.0) if is_amount else None,
        "remaining_quantity": None if is_amount else max(target_value - achieved_value, 0.0),

        "status": target.status,
        "incentive_type": target.incentive_type or None,
        "eligible_incentive": flt(target.eligible_incentive),
        "final_incentive": flt(target.final_incentive),
        "current_slab": target.current_slab or None,

        "last_calculated_on": target.last_calculated_on,
    }

    card["next_milestone"] = _next_milestone(
        rule.name, target, target_value, achieved_value)

    return card


def _next_milestone(rule_name, target, target_value, achieved_value):
    """The next slab worth reaching, or None.

    Answers the question a card wants to ask -- "how much more for the next
    bracket?" -- so Angular never interprets a slab table.

    Consistent with Phase 30A's SINGLE-slab model: the next milestone is the
    lowest slab whose `from_achievement` is strictly above the current
    achievement. Nothing accumulates across slabs, because nothing accumulates in
    the calculation either.

    `None` when there is no slab configuration, or when the buyer is already past
    the highest bracket. Absence is the honest answer; inventing a milestone
    beyond the merchant's own top slab would promise a reward that does not exist.
    """

    if target.incentive_type not in ("Slab Fixed Amount", "Slab Percentage"):
        return None

    achievement = flt(target.achievement_percentage)

    slabs = frappe.get_all(
        "YOB Target Rule Incentive Slab",
        filters={"parent": rule_name, "parenttype": "YOB Target Rule"},
        fields=["from_achievement", "to_achievement", "incentive_amount",
                "incentive_percentage", "max_incentive"],
        order_by="from_achievement asc",
        limit_page_length=0,
    )

    nxt = next((s for s in slabs if flt(s.from_achievement) > achievement), None)

    if not nxt:
        return None

    # The value the buyer must REACH for that slab, expressed in the target's own
    # unit, then the gap from where they are. Floored for the same reason the
    # remaining value is.
    needed_value = target_value * flt(nxt.from_achievement) / 100.0
    gap = max(needed_value - achieved_value, 0.0)

    is_amount = target.target_type == "Amount"

    return {
        "achievement_percentage": flt(nxt.from_achievement),
        "remaining_amount": gap if is_amount else None,
        "remaining_quantity": None if is_amount else gap,
        "incentive_amount": flt(nxt.incentive_amount) or None,
        "incentive_percentage": flt(nxt.incentive_percentage) or None,
        "max_incentive": flt(nxt.max_incentive) or None,
    }
