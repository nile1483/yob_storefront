# Copyright (c) 2026, YOB and Shayona
"""A customer's calculated target. SYSTEM GENERATED -- never hand-authored.

Every figure on this document is produced by
`services/target_service.recalculate_due_targets()`. The DocType grants no
`create` and no `delete` even to System Manager, and every calculated field is
`read_only`, so the only way a number changes is a calculation.

That matters because an achievement figure a human typed is indistinguishable
from one the engine produced, and the next scheduled run would overwrite it
without anyone noticing which was which.

TWO FIELDS ARE DELIBERATELY NOT LOCKED
--------------------------------------
`status` and `reward_status`. `Completed` is a MANUAL administrative state that
recalculation must never assign and never clear, so an administrator has to be
able to set it -- and a document on which literally nothing is writable makes
that impossible.

The permission is as narrow as that sentence: the only status a human may assign
is `Completed`. `Not Started`, `In Progress`, `Achieved` and `Expired` remain the
engine's, and moving a target back out of `Completed` is refused because
reopening is not a designed workflow here.

Both checks compare against the STORED row rather than relying on `read_only`,
which only constrains Desk and is ignored by an API call, a patch or a bulk
update.

ONE ROW PER customer + target_rule, enforced by a UNIQUE INDEX rather than by a
lookup-then-insert. Two scheduler workers racing on the same pair would both
pass an existence check and both insert; the index makes the loser fail, and
`upsert_customer_target()` turns that failure into the update it meant to be.
"""

import frappe
from frappe import _
from frappe.model.document import Document


#: The only fields a human may change on a calculated target.
#:
#: `status` is here because `Completed` is a MANUAL administrative state by
#: design -- somebody records that a target was settled, and recalculation then
#: leaves it alone (`target_incentive.calculate_status()`). `reward_status` is
#: its companion and is freely administrative: it describes a settlement, not an
#: achievement, so no calculation competes for it.
#:
#: Everything else is a calculated figure. An achievement a human typed is
#: indistinguishable from one the engine produced, and the next scheduled run
#: would silently overwrite it.
ADMIN_EDITABLE_FIELDS = ("status", "reward_status")

#: The ONLY status a human may assign.
#:
#: `status` is otherwise calculation-owned: `Not Started`, `In Progress`,
#: `Achieved` and `Expired` are verdicts on sales, derived from the Target Period
#: and the achievement. Typing one of them by hand asserts a business fact that
#: the Sales Orders do not support -- and because recalculation recomputes them,
#: a forged `Achieved` would also silently revert at the next run, so the forgery
#: is both wrong and invisible.
#:
#: `Completed` is the exception precisely because nothing calculates it.
MANUALLY_ASSIGNABLE_STATUS = "Completed"


class YOBCustomerTarget(Document):
    def validate(self):
        if self.flags.from_target_calculation:
            return

        if self.is_new():
            # Only the engine creates these. The DocType also grants no `create`
            # permission, so this is the second lock rather than the only one.
            frappe.throw(
                _("Customer Target records are created by the system and cannot be "
                  "added manually."),
                frappe.PermissionError,
            )

        self.reject_manual_calculation_edits()

    def reject_manual_calculation_edits(self):
        """A manual save may touch `status`/`reward_status` and nothing else.

        Compared against the STORED row rather than trusting `read_only`, which
        is a Desk affordance: an API call, a patch or a bulk update ignores it
        entirely.
        """

        before = frappe.db.get_value(
            self.doctype, self.name, "*", as_dict=True) or {}

        changed = [
            field.fieldname
            for field in self.meta.fields
            if field.fieldname in before
            and field.fieldname not in ADMIN_EDITABLE_FIELDS
            and (self.get(field.fieldname) or None) != (before.get(field.fieldname) or None)
        ]

        if changed:
            frappe.throw(
                _("{0} is calculated by the system and cannot be edited. Only Status and "
                  "Reward Status may be set manually.").format(
                    ", ".join(_(self.meta.get_label(f)) for f in sorted(changed))),
                frappe.PermissionError,
            )

        self.reject_forged_status(before)

    def reject_forged_status(self, before):
        """A manual status change may only ever be to `Completed`.

        Being allowed to EDIT the field is not the same as being allowed to write
        any value into it. The calculated verdicts stay the engine's: marking a
        target `Achieved` by hand claims sales that do not exist, and marking one
        `Expired` writes off a period the engine may still be counting.

        Moving a target OUT of `Completed` is refused too. Reopening a settled
        target is not a designed workflow in this phase, and the honest way to
        add one later is a deliberate decision about what it means -- not a side
        effect of the field being writable.
        """

        previous = before.get("status") or None
        current = self.status or None

        if current == previous:
            return

        if current != MANUALLY_ASSIGNABLE_STATUS:
            frappe.throw(
                _("Status {0} is calculated from the Target Period and the achievement. "
                  "An administrator may only set {1}.").format(
                    current or _("(blank)"), MANUALLY_ASSIGNABLE_STATUS),
                frappe.PermissionError,
            )
