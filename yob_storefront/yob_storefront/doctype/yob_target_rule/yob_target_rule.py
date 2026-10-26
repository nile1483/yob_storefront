# Copyright (c) 2026, YOB and Shayona
"""The target configuration, and the server-side rules that keep it coherent.

TWO DATE RANGES THAT MEAN DIFFERENT THINGS
------------------------------------------
    Target Period   ->  WHICH TRANSACTION DATES COUNT
    Report Window   ->  WHEN RECALCULATION MAY RUN

They are never combined. The reference implementation this replaces emitted
`(target window) OR (report window)` into the achievement SQL, so a rule whose
report window ran to 31 March silently counted February sales toward a January
target. Nothing here may reintroduce that: see
`services/target_achievement.qualifying_date_range()`, which is the single place
a transaction date range is decided.

Because the report window only gates the job, both of its dates stay editable
after submission -- that is operational control, not a change to what was
earned. `enabled` and `customer_visible` are editable for the same reason.

VALIDATION IS SERVER-SIDE
-------------------------
Every rule below is enforced here, not by Desk `depends_on`. `depends_on` hides
a field; it does not stop an import, a patch, a test or an API call from writing
a rule that the calculation engine would then have to guess about.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate

#: Target Scope -> the typed Link field that carries its value.
#:
#: NOT a Dynamic Link. The reference used one and made `Total Sales Order` its
#: target DocType, asking Frappe to resolve a link against a table that does not
#: exist. A Dynamic Link also cannot work here at all: Frappe runs
#: `_validate_links()` BEFORE `validate()` (`document.py:477` vs `:484`), so a
#: server-derived options field is always still empty when the link is checked.
#:
#: Typed fields plus "clear every other type" is the pattern this app already
#: uses for a type-plus-record destination (`utils.storefront_content
#: .apply_destination()`), and it makes "Total Sales Order has no scope value"
#: structurally true rather than defended.
SCOPE_FIELDS = {
    "Item": "scope_item",
    "Item Group": "scope_item_group",
    "Brand": "scope_brand",
    "Total Sales Order": None,
}

#: Which DocType each typed scope field links to, for the existence check.
SCOPE_LINK_DOCTYPES = {
    "scope_item": "Item",
    "scope_item_group": "Item Group",
    "scope_brand": "Brand",
}

#: Eligibility condition type -> the typed Link field that carries its value.
CONDITION_FIELDS = {
    "Customer": "condition_customer",
    "Customer Group": "condition_customer_group",
}

TARGET_TYPES = ("Amount", "Quantity")

INCENTIVE_TYPES = ("Fixed Amount", "Percentage", "Slab Fixed Amount", "Slab Percentage")

SLAB_INCENTIVE_TYPES = ("Slab Fixed Amount", "Slab Percentage")

#: Achievement at which a non-slab incentive becomes payable, as a percentage.
#: Carried over from the reference's business model deliberately.
FULL_ACHIEVEMENT = 100.0


class YOBTargetRule(Document):
    def on_update_after_submit(self):
        """Re-assert the report-window check on a post-submit edit.

        `allow_on_submit` fields bypass `validate()` completely: Frappe calls
        `on_update_after_submit` instead. Without this, a submitted rule could be
        given `report_start_date > report_end_date`, and an inverted window makes
        the rule silently never due -- the job simply stops finding it, with
        nothing to show why.

        Only the window is re-checked, because only `enabled`,
        `customer_visible` and the two report dates are editable here, and the
        first two need no cross-field rule.
        """

        self.validate_report_window()

    def validate(self):
        self.validate_company()
        self.set_currency_from_company()
        self.validate_target_period()
        self.validate_report_window()
        self.validate_scope()
        self.validate_conditions()
        self.validate_target()
        self.validate_incentive()


    # ------------------------------------------------------------- company

    def validate_company(self):
        """Mandatory, because achievement is filtered by it.

        Without a company a rule would count every company's Sales Orders on the
        site. The reference omitted the filter entirely, which is the same defect
        with a friendlier failure mode.
        """

        if not self.company:
            frappe.throw(_("Company is required."), frappe.ValidationError)

    def set_currency_from_company(self):
        """Derived, never chosen.

        Achievement is summed from `Sales Order Item.base_net_amount`, which
        ERPNext stores in the COMPANY'S base currency. A freely chosen currency
        here would label those rupees as dollars and the comparison against
        `target_amount` would be arithmetic nonsense. No conversion exists in
        this phase, so the only honest currency is the company's own.
        """

        if not self.company:
            return

        base = frappe.get_cached_value("Company", self.company, "default_currency")

        if not base:
            frappe.throw(
                _("Company {0} has no default currency.").format(self.company),
                frappe.ValidationError)

        self.currency = base


    # ------------------------------------------------------------- periods

    def validate_target_period(self):
        if not self.target_period:
            frappe.throw(_("Target Period is required."), frappe.ValidationError)

        period = frappe.db.get_value(
            "YOB Target Period", self.target_period,
            ["enabled", "start_date", "end_date"], as_dict=True)

        if not period:
            frappe.throw(
                _("Target Period {0} does not exist.").format(self.target_period),
                frappe.ValidationError)

        if not period.start_date or not period.end_date:
            frappe.throw(
                _("Target Period {0} has no date range.").format(self.target_period),
                frappe.ValidationError)

        # A disabled period may be LINKED but is never processed (see
        # `due_target_rules()`). Refusing it at submit time would block the
        # ordinary workflow of preparing next month's rule in advance, so this
        # is a submit-time check only.
        if self.docstatus == 1 and not period.enabled:
            frappe.throw(
                _("Target Period {0} is disabled and cannot be submitted against.")
                .format(self.target_period),
                frappe.ValidationError)

    def validate_report_window(self):
        if not self.report_start_date:
            frappe.throw(_("Report Start Date is required."), frappe.ValidationError)

        if not self.report_end_date:
            frappe.throw(_("Report End Date is required."), frappe.ValidationError)

        if getdate(self.report_start_date) > getdate(self.report_end_date):
            frappe.throw(
                _("Report Start Date {0} cannot be after Report End Date {1}.").format(
                    self.report_start_date, self.report_end_date),
                frappe.ValidationError)


    # --------------------------------------------------------------- scope

    def validate_scope(self):
        """Exactly one typed scope field survives, and `scope_value` mirrors it.

        Every other type's field is CLEARED rather than merely ignored, so a rule
        switched from Item to Brand cannot leave a stale item behind for the
        achievement query to read later.
        """

        if self.target_scope not in SCOPE_FIELDS:
            frappe.throw(
                _("{0} is not a valid Target Scope.").format(self.target_scope or ""),
                frappe.ValidationError)

        keep = SCOPE_FIELDS[self.target_scope]

        for fieldname in SCOPE_LINK_DOCTYPES:
            if fieldname != keep:
                self.set(fieldname, None)

        if keep is None:
            # Total Sales Order is a document-level target: there is nothing to
            # name, and naming something would be a contradiction.
            self.scope_value = None
            return

        value = self.get(keep)

        if not value:
            frappe.throw(
                _("{0} is required when Target Scope is {1}.").format(
                    _(self.meta.get_label(keep)), self.target_scope),
                frappe.ValidationError)

        linked = SCOPE_LINK_DOCTYPES[keep]

        if not frappe.db.exists(linked, value):
            frappe.throw(
                _("{0} {1} does not exist.").format(linked, value),
                frappe.ValidationError)

        self.scope_value = value


    # --------------------------------------------------------- eligibility

    def validate_conditions(self):
        """At least one condition, every one resolvable.

        OR semantics across rows: a customer qualifies by matching ANY row. A
        rule with no rows would therefore qualify nobody, which is not a
        configuration anybody wants -- it is an unfinished one.
        """

        if not self.conditions:
            frappe.throw(
                _("At least one Customer Eligibility condition is required."),
                frappe.ValidationError)

        for row in self.conditions:
            if row.condition_type not in CONDITION_FIELDS:
                frappe.throw(
                    _("Row {0}: {1} is not a valid eligibility type.").format(
                        row.idx, row.condition_type or ""),
                    frappe.ValidationError)

            keep = CONDITION_FIELDS[row.condition_type]

            for fieldname in CONDITION_FIELDS.values():
                if fieldname != keep:
                    row.set(fieldname, None)

            value = row.get(keep)

            if not value:
                frappe.throw(
                    _("Row {0}: a {1} is required.").format(row.idx, row.condition_type),
                    frappe.ValidationError)

            if not frappe.db.exists(row.condition_type, value):
                frappe.throw(
                    _("Row {0}: {1} {2} does not exist.").format(
                        row.idx, row.condition_type, value),
                    frappe.ValidationError)

            # Mirrored so the eligibility resolver reads one column whatever the
            # condition type is.
            row.condition_value = value


    # -------------------------------------------------------------- target

    def validate_target(self):
        if self.target_type not in TARGET_TYPES:
            frappe.throw(
                _("{0} is not a valid Target Type.").format(self.target_type or ""),
                frappe.ValidationError)

        if self.target_type == "Amount":
            if flt(self.target_amount) <= 0:
                frappe.throw(
                    _("Target Amount must be greater than zero."), frappe.ValidationError)

            # Quantity fields cleared so the snapshot on a Customer Target cannot
            # advertise a quantity target that is not being measured.
            self.target_quantity = 0
            self.target_uom = None
            return

        if flt(self.target_quantity) <= 0:
            frappe.throw(
                _("Target Quantity must be greater than zero."), frappe.ValidationError)

        if not self.target_uom:
            frappe.throw(
                _("Target UOM is required for a Quantity target."), frappe.ValidationError)

        if not frappe.db.exists("UOM", self.target_uom):
            frappe.throw(
                _("UOM {0} does not exist.").format(self.target_uom), frappe.ValidationError)

        self.target_amount = 0


    # ----------------------------------------------------------- incentive

    def validate_incentive(self):
        if self.incentive_type not in INCENTIVE_TYPES:
            frappe.throw(
                _("{0} is not a valid Incentive Type.").format(self.incentive_type or ""),
                frappe.ValidationError)

        if self.incentive_type == "Fixed Amount":
            if flt(self.incentive_amount) <= 0:
                frappe.throw(
                    _("Incentive Amount must be greater than zero for a Fixed Amount "
                      "incentive."),
                    frappe.ValidationError)
            self.incentive_percentage = 0
            self.incentive_slabs = []
            return

        if self.incentive_type == "Percentage":
            if flt(self.incentive_percentage) <= 0:
                frappe.throw(
                    _("Incentive % must be greater than zero for a Percentage incentive."),
                    frappe.ValidationError)
            self.incentive_amount = 0
            self.incentive_slabs = []
            return

        self.validate_slabs()
        self.incentive_amount = 0
        self.incentive_percentage = 0

    def validate_slabs(self):
        """Slabs must be individually sane and must not overlap.

        A single slab applies (the model is NOT progressive), so two slabs
        covering the same achievement would make the answer depend on row order.
        `get_current_slab()` resolves by `from_achievement DESC` and would quietly
        pick one; rejecting the overlap here means the merchant finds out instead.
        """

        if not self.incentive_slabs:
            frappe.throw(
                _("At least one Incentive Slab is required for {0}.")
                .format(self.incentive_type),
                frappe.ValidationError)

        seen = []

        for row in self.incentive_slabs:
            low = flt(row.from_achievement)
            high = flt(row.to_achievement)

            if low < 0 or high < 0:
                frappe.throw(
                    _("Slab row {0}: achievement bounds cannot be negative.").format(row.idx),
                    frappe.ValidationError)

            if low > high:
                frappe.throw(
                    _("Slab row {0}: From Achievement {1}% cannot exceed To Achievement {2}%.")
                    .format(row.idx, low, high),
                    frappe.ValidationError)

            if self.incentive_type == "Slab Fixed Amount" and flt(row.incentive_amount) <= 0:
                frappe.throw(
                    _("Slab row {0}: Incentive Amount must be greater than zero.")
                    .format(row.idx),
                    frappe.ValidationError)

            if self.incentive_type == "Slab Percentage" and flt(row.incentive_percentage) <= 0:
                frappe.throw(
                    _("Slab row {0}: Incentive % must be greater than zero.").format(row.idx),
                    frappe.ValidationError)

            if flt(row.max_incentive) < 0:
                frappe.throw(
                    _("Slab row {0}: Max Incentive cannot be negative.").format(row.idx),
                    frappe.ValidationError)

            # Bounds are INCLUSIVE at both ends, so 0-50 and 50-100 genuinely
            # overlap at exactly 50 and that is a real ambiguity, not a rounding
            # detail.
            for other_idx, other_low, other_high in seen:
                if low <= other_high and other_low <= high:
                    frappe.throw(
                        _("Slab rows {0} and {1} overlap: {2}%-{3}% and {4}%-{5}% both "
                          "cover the same achievement.").format(
                            other_idx, row.idx, other_low, other_high, low, high),
                        frappe.ValidationError)

            seen.append((row.idx, low, high))
