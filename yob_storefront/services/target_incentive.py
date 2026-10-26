# Copyright (c) 2026, YOB and Shayona
"""Incentive and status, decided from an already-calculated achievement.

A SINGLE SLAB, NOT A COMMISSION LADDER
--------------------------------------
One slab applies: the one whose inclusive bounds contain the achievement. There
is deliberately no progressive accumulation across slabs -- 0-50 paying on the
first 50% and 50-100 paying on the next is a different product, and the merchant
configuring these rows is not describing one.

STATUS IS A FACT ABOUT THE TARGET PERIOD, NOT THE REPORT WINDOW
---------------------------------------------------------------
`Expired` means the period ended short, never "the report window closed". Those
are separate dates with separate jobs, and the reference conflated them.

The consequence is deliberate and worth stating: an `Expired` target can become
`Achieved` again. A backdated in-period Sales Order entered while the rule is
still inside its report window is a real correction, and the status must follow
the sales rather than defend an earlier verdict. Once the report window closes,
nothing recalculates and the last verdict stands.
"""

from frappe.utils import flt, getdate

#: Achievement required before a non-slab incentive is payable, as a percentage.
FULL_ACHIEVEMENT = 100.0

STATUS_NOT_STARTED = "Not Started"
STATUS_IN_PROGRESS = "In Progress"
STATUS_ACHIEVED = "Achieved"
STATUS_EXPIRED = "Expired"
#: Manual reward state. Recalculation never assigns it and never clears it.
STATUS_COMPLETED = "Completed"


def achievement_percentage(*, target_type, target_amount, target_quantity,
                           achieved_amount, achieved_quantity):
    """Achievement as a percentage, never negative.

    Measured against whichever target the rule actually set. A zero target would
    divide by zero, so it answers 0 -- but the rule validation refuses a
    non-positive target, so this is a floor rather than a real case.
    """

    if target_type == "Amount":
        target = flt(target_amount)
        achieved = flt(achieved_amount)
    else:
        target = flt(target_quantity)
        achieved = flt(achieved_quantity)

    if target <= 0:
        return 0.0

    return max(0.0, achieved / target * 100.0)


# =========================================================
# SLABS
# =========================================================

def current_slab(slabs, achievement):
    """The one applicable slab, or None.

    Bounds are INCLUSIVE at both ends, and the rule validation refuses
    overlapping rows, so at most one slab can match.

    Above the highest configured slab the HIGHEST slab applies. That is the
    reference's behaviour and it is the right one: a merchant who wrote
    "90-100% pays 5000" means 120% pays at least that, not nothing. Below the
    lowest slab there is deliberately no match -- nothing was earned yet.
    """

    achievement = flt(achievement)
    ordered = sorted(slabs, key=lambda row: flt(row.from_achievement))

    for row in ordered:
        if flt(row.from_achievement) <= achievement <= flt(row.to_achievement):
            return row

    if ordered:
        highest = ordered[-1]
        if achievement > flt(highest.to_achievement):
            return highest

    return None


def slab_label(slab):
    """How a slab is shown on a Customer Target. Display only."""

    if not slab:
        return ""

    return f"{flt(slab.from_achievement):g}% - {flt(slab.to_achievement):g}%"


# =========================================================
# INCENTIVE
# =========================================================

def calculate_incentive(*, incentive_type, slabs, achievement, achieved_amount,
                        incentive_amount=0, incentive_percentage=0):
    """The incentive for this achievement.

    Returns the display/amount fields a Customer Target carries. `eligible` and
    `final` are the same figure in this phase: they diverge only once a
    settlement workflow exists to reduce one against the other, and inventing
    that difference now would mean inventing the workflow.

    A percentage incentive always applies to the ACHIEVED AMOUNT, including for a
    Quantity target -- which is why the engine computes a qualifying amount even
    when the target is measured in units. A percentage of a quantity would be a
    quantity, not money.
    """

    blank = {
        "eligible_incentive": 0.0,
        "final_incentive": 0.0,
        "applied_incentive_amount": 0.0,
        "applied_incentive_percentage": 0.0,
        "current_slab": "",
    }

    achievement = flt(achievement)
    achieved_amount = flt(achieved_amount)

    if incentive_type == "Fixed Amount":
        if achievement < FULL_ACHIEVEMENT:
            return blank
        amount = flt(incentive_amount)
        return {**blank, "applied_incentive_amount": amount,
                "eligible_incentive": amount, "final_incentive": amount}

    if incentive_type == "Percentage":
        if achievement < FULL_ACHIEVEMENT:
            return blank
        percentage = flt(incentive_percentage)
        amount = achieved_amount * percentage / 100.0
        return {**blank, "applied_incentive_percentage": percentage,
                "eligible_incentive": amount, "final_incentive": amount}

    if incentive_type in ("Slab Fixed Amount", "Slab Percentage"):
        slab = current_slab(slabs or [], achievement)

        if not slab:
            # Below the lowest slab. Nothing earned, and the label stays empty
            # rather than naming a slab that did not apply.
            return blank

        label = slab_label(slab)

        if incentive_type == "Slab Fixed Amount":
            amount = flt(slab.incentive_amount)
            return {**blank, "current_slab": label,
                    "applied_incentive_amount": amount,
                    "eligible_incentive": amount, "final_incentive": amount}

        percentage = flt(slab.incentive_percentage)
        amount = achieved_amount * percentage / 100.0
        cap = flt(slab.max_incentive)

        # 0 means uncapped, so only a positive cap bites.
        if cap > 0:
            amount = min(amount, cap)

        return {**blank, "current_slab": label,
                "applied_incentive_percentage": percentage,
                "eligible_incentive": amount, "final_incentive": amount}

    return blank


# =========================================================
# STATUS
# =========================================================

def calculate_status(*, achievement, period_start, period_end, calculation_date,
                     previous_status=None):
    """The target's status, from the TARGET PERIOD and the achievement.

    `Completed` is a manual reward state and is returned untouched: a background
    job must not undo a settlement somebody recorded by hand. Nothing in this
    phase ever assigns it.
    """

    if previous_status == STATUS_COMPLETED:
        return STATUS_COMPLETED

    achievement = flt(achievement)
    start = getdate(period_start)
    end = getdate(period_end)
    today_ = getdate(calculation_date)

    if achievement >= FULL_ACHIEVEMENT:
        # Reaching the target is reaching it, whenever that is noticed -- so a
        # late correction inside the report window can lift an Expired target
        # back to Achieved.
        return STATUS_ACHIEVED

    if today_ < start:
        return STATUS_NOT_STARTED

    if today_ > end:
        # The period closed short. Still recalculable while the report window is
        # open; `Expired` is a verdict on the sales, not a lock.
        return STATUS_EXPIRED

    if achievement <= 0:
        return STATUS_NOT_STARTED

    return STATUS_IN_PROGRESS
