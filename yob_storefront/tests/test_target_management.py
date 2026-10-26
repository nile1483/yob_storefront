# Copyright (c) 2026, YOB and Shayona
"""Target Management: the calculation engine, and the defects it must not have.

WHAT THIS FILE IS DEFENDING
---------------------------
Phase 30A replaces an experimental implementation whose tests were 22-line
placeholders. Four of its defects were business-visible, and each has a case
here that fails if it ever returns:

* `ReportWindowCase` -- the report window gated the JOB, but the experiment also
  OR-ed it into the achievement SQL, so a January target whose report window ran
  to 31 March counted February sales.
* `QuantityUOMCase` -- `SUM(qty)` added 5 Box to 30 Nos and answered 35.
* `CompanyIsolationCase` -- the company was stored and never filtered on, so
  every company's orders fed the same target.
* `SecuritySurfaceCase` -- a whitelisted method took a customer name and wrote
  with `ignore_permissions=True`.

FIXTURES
--------
Everything is created inside the test and rolled back. `frappe.db.commit` is
neutered so nothing can outlive a test, and Sales Orders are submitted for real
so `docstatus`, `stock_qty` and `base_net_amount` are ERPNext's own values rather
than numbers this file made up.
"""

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_days, flt, getdate, today

from yob_storefront.services import target_achievement as achievement
from yob_storefront.services import target_incentive as incentive
from yob_storefront.services import target_service
from yob_storefront.services.target_achievement import TargetCalculationError

SEED_ITEM = "YOB-BOLT-M10"
SEED_CUSTOMER = "YOB Demo Buyer"

#: Fixed dates live inside the test site's only active Fiscal Year
#: (2026-04-01 .. 2027-03-31). ERPNext refuses a Sales Order dated outside an
#: active Fiscal Year, so the date-boundary scenarios use a 1 - 25 JUNE period
#: rather than the January one the business example is written around. The shape
#: is identical; only the month moved.


def _seeded():
    return bool(frappe.db.exists("Item", SEED_ITEM)
                and frappe.db.exists("Customer", SEED_CUSTOMER))


class TargetBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data on the test site")

    def setUp(self):
        frappe.set_user("Administrator")

        self.commits = []
        cp = patch.object(frappe.db, "commit",
                          side_effect=lambda *a, **k: self.commits.append(1))
        cp.start()
        self.addCleanup(cp.stop)

        self.company = frappe.db.get_value("Company", {}, "name")
        self.currency = frappe.get_cached_value("Company", self.company, "default_currency")
        self.item_group = frappe.db.get_value("Item", SEED_ITEM, "item_group")
        self.stock_uom = frappe.db.get_value("Item", SEED_ITEM, "stock_uom")
        self.hsn = frappe.db.get_value("Item", SEED_ITEM, "gst_hsn_code")
        self.warehouse = frappe.db.get_value(
            "Warehouse", {"company": self.company, "is_group": 0}, "name")

        # Resolved ONCE, before any test creates a group of its own. Picking
        # "some non-group Customer Group" lazily inside `make_customer` put a
        # supposedly-unrelated customer into the very group the test had just
        # created, and the eligibility assertions quietly passed for the wrong
        # reason.
        self.baseline_group = frappe.db.get_value(
            "Customer Group", {"is_group": 0}, "name")

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()
        self.assertEqual(self.commits, [], "a commit escaped the test rollback")

    # ------------------------------------------------------------- fixtures

    def make_customer(self, name, group=None):
        doc = {
            "doctype": "Customer", "customer_name": name, "customer_type": "Company",
            "customer_group": group or self.baseline_group,
            "territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
        }
        return frappe.get_doc(doc).insert(ignore_permissions=True).name

    def make_company(self, name="_T30 Second Co", abbr="T3SC"):
        """A minimum valid second Company, through ERPNext's own creation path.

        No validation is weakened: ERPNext builds the chart of accounts, the
        default warehouses and the cost centres itself, which is exactly what
        makes the resulting Sales Order a real one. Rolled back with everything
        else.
        """

        existing = frappe.db.get_value("Company", {"company_name": name}, "name")
        if existing:
            return existing

        return frappe.get_doc({
            "doctype": "Company",
            "company_name": name,
            "abbr": abbr,
            "default_currency": self.currency,
            "country": frappe.db.get_value("Company", self.company, "country") or "India",
            "create_chart_of_accounts_based_on": "Standard Template",
            "chart_of_accounts": "Standard",
        }).insert(ignore_permissions=True).name

    def make_group(self, name):
        parent = frappe.db.get_value("Customer Group", {"is_group": 1}, "name")
        return frappe.get_doc({
            "doctype": "Customer Group", "customer_group_name": name,
            "parent_customer_group": parent, "is_group": 0,
        }).insert(ignore_permissions=True).name

    def make_item(self, code, *, brand=None, item_group=None, uoms=None):
        doc = {
            "doctype": "Item", "item_code": code, "item_name": code,
            "item_group": item_group or self.item_group, "stock_uom": self.stock_uom,
            "is_stock_item": 0, "is_sales_item": 1, "gst_hsn_code": self.hsn,
        }
        if brand:
            doc["brand"] = brand
        if uoms:
            doc["uoms"] = [{"uom": u, "conversion_factor": cf} for u, cf in uoms]
        return frappe.get_doc(doc).insert(ignore_permissions=True).name

    def make_brand(self, name):
        return frappe.get_doc({
            "doctype": "Brand", "brand": name}).insert(ignore_permissions=True).name

    def make_uom(self, name):
        if frappe.db.exists("UOM", name):
            return name
        return frappe.get_doc({
            "doctype": "UOM", "uom_name": name}).insert(ignore_permissions=True).name

    def make_period(self, name, start, end, *, enabled=1):
        return frappe.get_doc({
            "doctype": "YOB Target Period", "period_name": name,
            "start_date": start, "end_date": end, "enabled": enabled,
        }).insert(ignore_permissions=True).name

    #: Target Scope -> the typed field that carries its value, mirroring the
    #: controller. Not a Dynamic Link: see the note in `yob_target_rule.py`.
    SCOPE_FIELD = {"Item": "scope_item", "Item Group": "scope_item_group",
                   "Brand": "scope_brand", "Total Sales Order": None}

    CONDITION_FIELD = {"Customer": "condition_customer",
                       "Customer Group": "condition_customer_group"}

    def make_rule(self, name, *, period, conditions, scope="Total Sales Order",
                  scope_value=None, target_type="Amount", target_amount=1000,
                  target_quantity=0, target_uom=None, incentive_type="Fixed Amount",
                  incentive_amount=500, incentive_percentage=0, slabs=None,
                  report_start=None, report_end=None, company=None, submit=True,
                  enabled=1):
        payload = {
            "doctype": "YOB Target Rule", "rule_name": name,
            "company": company if company is not None else self.company,
            "target_period": period,
            "report_start_date": report_start or add_days(today(), -30),
            "report_end_date": report_end or add_days(today(), 30),
            "enabled": enabled,
            "target_scope": scope,
            "target_type": target_type, "target_amount": target_amount,
            "target_quantity": target_quantity, "target_uom": target_uom,
            "incentive_type": incentive_type, "incentive_amount": incentive_amount,
            "incentive_percentage": incentive_percentage,
            "conditions": [
                {"condition_type": t, self.CONDITION_FIELD[t]: v} for t, v in conditions
            ],
            "incentive_slabs": slabs or [],
        }

        if scope_value is not None:
            field = self.SCOPE_FIELD.get(scope)
            # A value supplied for Total Sales Order has no typed home, so it is
            # pushed onto `scope_value` directly -- which is exactly what the
            # "must be empty" test needs to prove is refused.
            payload[field or "scope_value"] = scope_value

        doc = frappe.get_doc(payload).insert(ignore_permissions=True)

        if submit:
            doc.submit()

        return doc

    def make_sales_order(self, customer, *, items, date=None, company=None,
                         submit=True):
        """A REAL submitted Sales Order, so ERPNext owns stock_qty and amounts."""

        company = company or self.company
        warehouse = frappe.db.get_value(
            "Warehouse", {"company": company, "is_group": 0}, "name")

        doc = frappe.get_doc({
            "doctype": "Sales Order", "customer": customer, "company": company,
            "transaction_date": date or today(),
            "delivery_date": date or today(),
            "currency": frappe.get_cached_value("Company", company, "default_currency"),
            "items": [{
                "item_code": row["item_code"], "qty": row["qty"],
                "uom": row.get("uom") or self.stock_uom,
                "rate": row.get("rate", 100),
                "delivery_date": date or today(),
                "warehouse": warehouse,
            } for row in items],
        })
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)

        if submit:
            doc.submit()

        return doc

    # ------------------------------------------------------------- helpers

    def run_calculation(self, calculation_date=None):
        return target_service.recalculate_due_targets(calculation_date)

    def target_for(self, customer, rule_name):
        name = frappe.db.get_value(
            "YOB Customer Target", {"customer": customer, "target_rule": rule_name}, "name")
        return frappe.get_doc("YOB Customer Target", name) if name else None


# =========================================================================
# TARGET PERIOD
# =========================================================================

class TargetPeriodCase(TargetBase):

    def test_dates_are_required(self):
        for payload in ({"start_date": today()}, {"end_date": today()}, {}):
            with self.assertRaises(frappe.ValidationError):
                frappe.get_doc({
                    "doctype": "YOB Target Period",
                    "period_name": f"_T30-BAD-{len(payload)}-{frappe.generate_hash(length=6)}",
                    **payload,
                }).insert(ignore_permissions=True)

    def test_start_cannot_be_after_end(self):
        with self.assertRaises(frappe.ValidationError):
            self.make_period("_T30-REVERSED", today(), add_days(today(), -1))

    def test_a_single_day_period_is_valid(self):
        """start == end is a legitimate one-day target."""

        name = self.make_period("_T30-ONEDAY", today(), today())
        self.assertTrue(frappe.db.exists("YOB Target Period", name))


# =========================================================================
# TARGET RULE VALIDATION
# =========================================================================

class TargetRuleValidationCase(TargetBase):

    def setUp(self):
        super().setUp()
        self.period = self.make_period("_T30-VAL", add_days(today(), -10), today())

    def test_company_is_mandatory(self):
        with self.assertRaises(frappe.ValidationError):
            self.make_rule("_T30-NOCOMPANY", period=self.period,
                           conditions=[("Customer", SEED_CUSTOMER)], company="")

    def test_currency_is_derived_from_company_and_read_only(self):
        """An arbitrary currency against a base-currency sum is nonsense."""

        rule = self.make_rule("_T30-CUR", period=self.period,
                              conditions=[("Customer", SEED_CUSTOMER)], submit=False)

        self.assertEqual(rule.currency, self.currency)

        other = frappe.db.get_value(
            "Currency", {"name": ["!=", self.currency], "enabled": 1}, "name")
        if not other:
            self.skipTest("this bench has only one enabled Currency")

        # A real, existing currency -- so this proves the SERVER overwrites it,
        # not that Frappe rejected a nonsense link.
        rule.currency = other
        rule.save(ignore_permissions=True)

        self.assertEqual(rule.currency, self.currency,
                         "an arbitrary currency survived against a base-currency sum")

    def bare_rule_payload(self, name, **overrides):
        """A rule payload with no fixture defaults, for 'field is required' tests.

        `make_rule` fills sensible report dates, which is right for every other
        test and useless for proving they are mandatory.
        """

        payload = {
            "doctype": "YOB Target Rule", "rule_name": name,
            "company": self.company, "target_period": self.period,
            "target_scope": "Total Sales Order", "target_type": "Amount",
            "target_amount": 100, "incentive_type": "Fixed Amount",
            "incentive_amount": 10,
            "conditions": [{"condition_type": "Customer",
                            "condition_customer": SEED_CUSTOMER}],
        }
        payload.update(overrides)
        return payload

    def test_report_dates_are_required_and_ordered(self):
        for missing in ({"report_end_date": today()}, {"report_start_date": today()}, {}):
            with self.assertRaises(frappe.ValidationError):
                frappe.get_doc(self.bare_rule_payload(
                    f"_T30-NOREPORT-{len(missing)}-{frappe.generate_hash(length=6)}",
                    **missing)).insert(ignore_permissions=True)

        with self.assertRaises(frappe.ValidationError):
            self.make_rule("_T30-BADREPORT", period=self.period,
                           conditions=[("Customer", SEED_CUSTOMER)],
                           report_start=today(), report_end=add_days(today(), -1))

    def test_scope_value_required_except_total_sales_order(self):
        for scope in ("Item", "Item Group", "Brand"):
            with self.assertRaises(frappe.ValidationError):
                self.make_rule(f"_T30-NOSCOPE-{scope}", period=self.period,
                               conditions=[("Customer", SEED_CUSTOMER)],
                               scope=scope, scope_value=None)

    def test_total_sales_order_clears_any_supplied_scope_value(self):
        """A document-level target names nothing, so a value cannot survive."""

        rule = self.make_rule("_T30-TOTALSCOPE", period=self.period,
                              conditions=[("Customer", SEED_CUSTOMER)],
                              scope="Total Sales Order", scope_value=SEED_ITEM)

        self.assertFalse(rule.scope_value)
        for field in ("scope_item", "scope_item_group", "scope_brand"):
            self.assertFalse(rule.get(field))

    def test_total_sales_order_uses_no_dynamic_link(self):
        """The reference's bug: 'Total Sales Order' was a Dynamic Link TARGET.

        There is no Dynamic Link on this DocType at all, so a link can never be
        resolved against a DocType that does not exist.
        """

        meta = frappe.get_meta("YOB Target Rule")

        self.assertEqual(
            [f.fieldname for f in meta.fields if f.fieldtype == "Dynamic Link"], [])

        rule = self.make_rule("_T30-TOTALCLEAN", period=self.period,
                              conditions=[("Customer", SEED_CUSTOMER)],
                              scope="Total Sales Order")
        self.assertFalse(rule.scope_value)

    def test_scope_value_mirrors_the_typed_field(self):
        item = self.make_item("_T30-SCOPEITEM")
        rule = self.make_rule("_T30-SCOPED", period=self.period,
                              conditions=[("Customer", SEED_CUSTOMER)],
                              scope="Item", scope_value=item)

        self.assertEqual(rule.scope_item, item)
        self.assertEqual(rule.scope_value, item)

    def test_switching_scope_clears_the_other_typed_fields(self):
        item = self.make_item("_T30-STALE")
        brand = self.make_brand("_T30 Stale Brand")

        rule = self.make_rule("_T30-SWITCH", period=self.period,
                              conditions=[("Customer", SEED_CUSTOMER)],
                              scope="Item", scope_value=item, submit=False)

        rule.target_scope = "Brand"
        rule.scope_brand = brand
        rule.save(ignore_permissions=True)

        self.assertIsNone(rule.scope_item, "a stale item survived a scope change")
        self.assertEqual(rule.scope_value, brand)

        rule.target_scope = "Total Sales Order"
        rule.save(ignore_permissions=True)

        self.assertIsNone(rule.scope_brand)
        self.assertIsNone(rule.scope_value)

    def test_a_nonexistent_scope_value_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self.make_rule("_T30-GHOSTSCOPE", period=self.period,
                           conditions=[("Customer", SEED_CUSTOMER)],
                           scope="Item", scope_value="_T30-NO-SUCH-ITEM")

    def test_at_least_one_eligibility_condition_is_required(self):
        with self.assertRaises(frappe.ValidationError):
            self.make_rule("_T30-NOCOND", period=self.period, conditions=[])

    def test_a_condition_requires_its_value(self):
        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc({
                "doctype": "YOB Target Rule", "rule_name": "_T30-EMPTYCOND",
                "company": self.company, "target_period": self.period,
                "report_start_date": today(), "report_end_date": today(),
                "target_scope": "Total Sales Order", "target_type": "Amount",
                "target_amount": 100, "incentive_type": "Fixed Amount",
                "incentive_amount": 10,
                "conditions": [{"condition_type": "Customer"}],
            }).insert(ignore_permissions=True)

    def test_condition_value_mirrors_the_typed_field(self):
        group = self.make_group("_T30 Cond Group")
        rule = self.make_rule("_T30-CONDDT", period=self.period,
                              conditions=[("Customer", SEED_CUSTOMER),
                                          ("Customer Group", group)])

        self.assertEqual(rule.conditions[0].condition_value, SEED_CUSTOMER)
        self.assertEqual(rule.conditions[1].condition_value, group)

    def test_switching_a_condition_type_clears_the_other_typed_field(self):
        group = self.make_group("_T30 Switch Group")
        rule = self.make_rule("_T30-CONDSWITCH", period=self.period,
                              conditions=[("Customer", SEED_CUSTOMER)], submit=False)

        row = rule.conditions[0]
        row.condition_type = "Customer Group"
        row.condition_customer_group = group
        rule.save(ignore_permissions=True)

        self.assertIsNone(rule.conditions[0].condition_customer)
        self.assertEqual(rule.conditions[0].condition_value, group)

    def test_amount_target_requires_a_positive_amount(self):
        for bad in (0, -1):
            with self.assertRaises(frappe.ValidationError):
                self.make_rule(f"_T30-AMT{bad}", period=self.period,
                               conditions=[("Customer", SEED_CUSTOMER)],
                               target_type="Amount", target_amount=bad)

    def test_quantity_target_requires_quantity_and_uom(self):
        with self.assertRaises(frappe.ValidationError):
            self.make_rule("_T30-QTYZERO", period=self.period,
                           conditions=[("Customer", SEED_CUSTOMER)],
                           target_type="Quantity", target_quantity=0,
                           target_uom=self.stock_uom, target_amount=0)

        with self.assertRaises(frappe.ValidationError):
            self.make_rule("_T30-QTYNOUOM", period=self.period,
                           conditions=[("Customer", SEED_CUSTOMER)],
                           target_type="Quantity", target_quantity=10,
                           target_uom=None, target_amount=0)

    def test_incentive_values_are_required_for_their_type(self):
        with self.assertRaises(frappe.ValidationError):
            self.make_rule("_T30-FIXEDZERO", period=self.period,
                           conditions=[("Customer", SEED_CUSTOMER)],
                           incentive_type="Fixed Amount", incentive_amount=0)

        with self.assertRaises(frappe.ValidationError):
            self.make_rule("_T30-PCTZERO", period=self.period,
                           conditions=[("Customer", SEED_CUSTOMER)],
                           incentive_type="Percentage", incentive_percentage=0,
                           incentive_amount=0)

    def test_slab_incentive_requires_slabs(self):
        with self.assertRaises(frappe.ValidationError):
            self.make_rule("_T30-NOSLABS", period=self.period,
                           conditions=[("Customer", SEED_CUSTOMER)],
                           incentive_type="Slab Fixed Amount", incentive_amount=0,
                           slabs=[])

    def test_malformed_slabs_are_refused(self):
        base = dict(period=self.period, conditions=[("Customer", SEED_CUSTOMER)],
                    incentive_type="Slab Fixed Amount", incentive_amount=0)

        # Inverted bounds.
        with self.assertRaises(frappe.ValidationError):
            self.make_rule("_T30-SLABINV", slabs=[
                {"from_achievement": 100, "to_achievement": 50, "incentive_amount": 10}],
                **base)

        # Negative bound.
        with self.assertRaises(frappe.ValidationError):
            self.make_rule("_T30-SLABNEG", slabs=[
                {"from_achievement": -10, "to_achievement": 50, "incentive_amount": 10}],
                **base)

        # No payout configured for the chosen slab type.
        with self.assertRaises(frappe.ValidationError):
            self.make_rule("_T30-SLABNOPAY", slabs=[
                {"from_achievement": 0, "to_achievement": 50, "incentive_amount": 0}],
                **base)

    def test_overlapping_slabs_are_refused(self):
        """A single slab applies, so an overlap makes the answer order-dependent."""

        with self.assertRaises(frappe.ValidationError):
            self.make_rule("_T30-SLABOVERLAP", period=self.period,
                           conditions=[("Customer", SEED_CUSTOMER)],
                           incentive_type="Slab Fixed Amount", incentive_amount=0,
                           slabs=[
                               {"from_achievement": 0, "to_achievement": 50,
                                "incentive_amount": 10},
                               {"from_achievement": 50, "to_achievement": 100,
                                "incentive_amount": 20},
                           ])

    def test_adjacent_slabs_are_accepted(self):
        rule = self.make_rule("_T30-SLABADJ", period=self.period,
                              conditions=[("Customer", SEED_CUSTOMER)],
                              incentive_type="Slab Fixed Amount", incentive_amount=0,
                              slabs=[
                                  {"from_achievement": 0, "to_achievement": 49.99,
                                   "incentive_amount": 10},
                                  {"from_achievement": 50, "to_achievement": 100,
                                   "incentive_amount": 20},
                              ])

        self.assertEqual(len(rule.incentive_slabs), 2)

    def test_a_disabled_period_cannot_be_submitted_against(self):
        disabled = self.make_period("_T30-OFFPERIOD", add_days(today(), -5), today(),
                                    enabled=0)

        with self.assertRaises(frappe.ValidationError):
            self.make_rule("_T30-OFFRULE", period=disabled,
                           conditions=[("Customer", SEED_CUSTOMER)])


# =========================================================================
# THE DATE BOUNDARY  -- the experiment's primary defect
# =========================================================================

class ReportWindowCase(TargetBase):
    """Report dates gate the JOB. They never change which sales qualify."""

    def test_report_window_is_absent_from_the_qualifying_range(self):
        """The range function takes no report dates AT ALL.

        Structural, deliberately: a report date cannot leak into a transaction
        filter if it is not a parameter of the only function that builds one.
        """

        import inspect as _inspect

        params = _inspect.signature(achievement.qualifying_date_range).parameters

        self.assertEqual(list(params), ["period_start", "period_end", "calculation_date"])

        for name in dir(achievement):
            if name.startswith("_"):
                continue
            obj = getattr(achievement, name)
            if callable(obj) and hasattr(obj, "__code__"):
                self.assertNotIn(
                    "report_start_date", obj.__code__.co_varnames,
                    f"{name}() accepts a report date")
                self.assertNotIn(
                    "report_end_date", obj.__code__.co_varnames,
                    f"{name}() accepts a report date")

    def test_the_range_is_the_period_clipped_to_the_calculation_date(self):
        start, end = getdate("2026-06-01"), getdate("2026-06-25")

        # Mid-period: clipped to today, not reaching into the future.
        self.assertEqual(
            achievement.qualifying_date_range(start, end, "2026-06-10"),
            (start, getdate("2026-06-10")))

        # After the period: the whole period, never beyond it.
        self.assertEqual(
            achievement.qualifying_date_range(start, end, "2026-08-31"),
            (start, end))

        # Exactly the boundaries, inclusive.
        self.assertEqual(
            achievement.qualifying_date_range(start, end, "2026-06-01"), (start, start))
        self.assertEqual(
            achievement.qualifying_date_range(start, end, "2026-06-25"), (start, end))

        # Before the period: nothing to count.
        self.assertIsNone(
            achievement.qualifying_date_range(start, end, "2026-05-31"))

    def scenario(self, *, report_start, report_end):
        """A 1-25 Jan target, with sales inside and outside that window."""

        customer = self.make_customer("_T30 Window Buyer")
        period = self.make_period("_T30-JAN", "2026-06-01", "2026-06-25")
        item = self.make_item("_T30-WINDOW-ITEM")

        # Inside the target period.
        self.make_sales_order(customer, date="2026-06-10",
                              items=[{"item_code": item, "qty": 1, "rate": 100}])
        # Target Start exactly.
        self.make_sales_order(customer, date="2026-06-01",
                              items=[{"item_code": item, "qty": 1, "rate": 100}])
        # Target End exactly.
        self.make_sales_order(customer, date="2026-06-25",
                              items=[{"item_code": item, "qty": 1, "rate": 100}])
        # One day BEFORE the period.
        self.make_sales_order(customer, date="2026-05-31",
                              items=[{"item_code": item, "qty": 1, "rate": 1000}])
        # One day AFTER the period, but INSIDE the report window. The defect.
        self.make_sales_order(customer, date="2026-06-26",
                              items=[{"item_code": item, "qty": 1, "rate": 1000}])
        # July, comfortably inside the report window.
        self.make_sales_order(customer, date="2026-07-15",
                              items=[{"item_code": item, "qty": 1, "rate": 1000}])

        rule = self.make_rule("_T30-WINDOW", period=period,
                              conditions=[("Customer", customer)],
                              target_amount=1000,
                              report_start=report_start, report_end=report_end)

        return customer, rule

    def test_sales_outside_the_period_never_count(self):
        """THE regression. 300 is the only honest answer: 3 x 100 inside 1-25 June.

        The experiment answered 3300 because its SQL was
        `(target window) OR (report window)`.
        """

        customer, rule = self.scenario(report_start="2026-06-15", report_end="2026-08-31")

        self.run_calculation("2026-08-31")
        target = self.target_for(customer, rule.name)

        self.assertIsNotNone(target, "no target was produced")
        self.assertEqual(flt(target.achieved_amount), 300.0,
                         "a transaction outside the Target Period was counted")

    def test_before_report_start_nothing_is_calculated(self):
        customer, rule = self.scenario(report_start="2026-06-15", report_end="2026-08-31")

        summary = self.run_calculation("2026-06-14")

        self.assertEqual(summary["rules"], 0, "a rule ran before its report window")
        self.assertIsNone(self.target_for(customer, rule.name))

    def test_exactly_report_start_calculates(self):
        customer, rule = self.scenario(report_start="2026-06-15", report_end="2026-08-31")

        self.run_calculation("2026-06-15")
        target = self.target_for(customer, rule.name)

        self.assertIsNotNone(target)
        # Only 1 and 10 June exist by then: the 25th has not happened yet.
        self.assertEqual(flt(target.achieved_amount), 200.0)

    def test_exactly_report_end_calculates(self):
        customer, rule = self.scenario(report_start="2026-06-15", report_end="2026-08-31")

        self.run_calculation("2026-08-31")

        self.assertIsNotNone(self.target_for(customer, rule.name))

    def test_after_report_end_the_previous_result_stands(self):
        customer, rule = self.scenario(report_start="2026-06-15", report_end="2026-06-20")

        self.run_calculation("2026-06-20")
        first = self.target_for(customer, rule.name)
        amount_at_close = flt(first.achieved_amount)
        calculated_at = first.last_calculated_on

        # Well past the report end, with the 25 June sale now in existence.
        summary = self.run_calculation("2026-09-30")

        self.assertEqual(summary["rules"], 0, "a rule ran after its report window")

        again = self.target_for(customer, rule.name)
        self.assertEqual(flt(again.achieved_amount), amount_at_close,
                         "a closed rule was recalculated")
        self.assertEqual(again.last_calculated_on, calculated_at)

    def test_a_partial_run_uses_only_the_sales_that_exist_so_far(self):
        customer, rule = self.scenario(report_start="2026-06-01", report_end="2026-08-31")

        self.run_calculation("2026-06-10")

        self.assertEqual(flt(self.target_for(customer, rule.name).achieved_amount), 200.0)

    def test_a_disabled_rule_is_not_due(self):
        customer, rule = self.scenario(report_start="2026-06-01", report_end="2026-08-31")
        rule.db_set("enabled", 0)

        self.assertEqual(self.run_calculation("2026-07-01")["rules"], 0)

    def test_a_draft_rule_is_not_due(self):
        period = self.make_period("_T30-DRAFT", "2026-06-01", "2026-06-25")
        self.make_rule("_T30-DRAFTRULE", period=period,
                       conditions=[("Customer", SEED_CUSTOMER)], submit=False)

        self.assertEqual(self.run_calculation("2026-06-10")["rules"], 0)

    def test_a_disabled_period_stops_processing(self):
        customer, rule = self.scenario(report_start="2026-06-01", report_end="2026-08-31")
        frappe.db.set_value("YOB Target Period", rule.target_period, "enabled", 0)

        self.assertEqual(self.run_calculation("2026-07-01")["rules"], 0)


# =========================================================================
# COMPANY
# =========================================================================

class CompanyIsolationCase(TargetBase):

    def test_another_companys_sales_order_never_counts(self):
        """The experiment stored a company and never filtered on it.

        Self-contained: a second Company is created here rather than borrowed
        from the bench, so this runs everywhere instead of skipping on a
        single-company site.
        """

        other = self.make_company()
        customer = self.make_customer("_T30 Company Buyer")
        item = self.make_item("_T30-COMPANY-ITEM")
        period = self.make_period("_T30-CO", add_days(today(), -5), today())

        self.make_sales_order(customer, date=today(), company=self.company,
                              items=[{"item_code": item, "qty": 1, "rate": 100}])
        self.make_sales_order(customer, date=today(), company=other,
                              items=[{"item_code": item, "qty": 1, "rate": 5000}])

        rule = self.make_rule("_T30-CORULE", period=period,
                              conditions=[("Customer", customer)], target_amount=100)

        self.run_calculation(today())

        self.assertEqual(flt(self.target_for(customer, rule.name).achieved_amount), 100.0,
                         "another company's order inflated the achievement")

    def test_the_company_filter_actually_binds_in_sql(self):
        """End-to-end proof that does not need a second Company on the bench.

        `test_another_companys_sales_order_never_counts` is the fuller version and
        skips on a single-company site, which would otherwise leave the
        experiment's missing-filter defect with no executable coverage here.
        """

        customer = self.make_customer("_T30 Filter Buyer")
        item = self.make_item("_T30-FILTER-ITEM")

        self.make_sales_order(customer, date=today(),
                              items=[{"item_code": item, "qty": 1, "rate": 100}])

        window = (add_days(today(), -5), today())

        mine = achievement.achieved_amount(
            customer=customer, company=self.company, scope="Total Sales Order",
            scope_value=None, from_date=window[0], to_date=window[1])

        self.assertEqual(flt(mine), 100.0, "the order was not found at all")

        other = achievement.achieved_amount(
            customer=customer, company="_T30 Nonexistent Company",
            scope="Total Sales Order", scope_value=None,
            from_date=window[0], to_date=window[1])

        self.assertEqual(flt(other), 0.0,
                         "the company filter did not bind: another company's target "
                         "would count these sales")

    def test_the_company_filter_is_in_the_shared_predicate(self):
        conditions, values = achievement._base_conditions(
            customer="C", company="CO", scope="Total Sales Order", scope_value=None,
            from_date=today(), to_date=today())

        self.assertIn("so.company = %(company)s", conditions)
        self.assertEqual(values["company"], "CO")


# =========================================================================
# CUSTOMER ELIGIBILITY
# =========================================================================

class EligibilityCase(TargetBase):

    def setUp(self):
        super().setUp()
        self.period = self.make_period("_T30-ELIG", add_days(today(), -5), today())

    def test_a_direct_customer_qualifies(self):
        customer = self.make_customer("_T30 Direct")
        rule = self.make_rule("_T30-DIRECT", period=self.period,
                              conditions=[("Customer", customer)])

        self.assertEqual(target_service.eligible_customers(rule.name), [customer])

    def test_a_customer_group_qualifies_its_members(self):
        group = self.make_group("_T30 Dealers")
        member = self.make_customer("_T30 Dealer One", group=group)
        self.make_customer("_T30 Outsider")

        rule = self.make_rule("_T30-GROUP", period=self.period,
                              conditions=[("Customer Group", group)])

        self.assertEqual(target_service.eligible_customers(rule.name), [member])

    def test_conditions_are_ORed(self):
        group = self.make_group("_T30 OR Group")
        in_group = self.make_customer("_T30 OR Member", group=group)
        direct = self.make_customer("_T30 OR Direct")
        self.make_customer("_T30 OR Neither")

        rule = self.make_rule("_T30-OR", period=self.period,
                              conditions=[("Customer", direct),
                                          ("Customer Group", group)])

        self.assertEqual(target_service.eligible_customers(rule.name),
                         sorted([direct, in_group]))

    def test_a_customer_matching_twice_appears_once(self):
        """Named directly AND in a listed group is still one customer."""

        group = self.make_group("_T30 Dup Group")
        customer = self.make_customer("_T30 Dup Member", group=group)

        rule = self.make_rule("_T30-DUP", period=self.period,
                              conditions=[("Customer", customer),
                                          ("Customer Group", group)])

        self.assertEqual(target_service.eligible_customers(rule.name), [customer])

    def test_an_unrelated_customer_is_excluded(self):
        customer = self.make_customer("_T30 Excluded")
        rule = self.make_rule("_T30-EXCL", period=self.period,
                              conditions=[("Customer", SEED_CUSTOMER)])

        self.assertNotIn(customer, target_service.eligible_customers(rule.name))

    def test_multiple_rules_create_independent_targets_for_one_customer(self):
        """Required: the storefront will later show several targets side by side.

        There is deliberately no 'best rule wins' and no cross-rule
        deduplication.
        """

        customer = self.make_customer("_T30 Multi")
        item = self.make_item("_T30-MULTI-ITEM")

        self.make_sales_order(customer, date=today(),
                              items=[{"item_code": item, "qty": 2, "rate": 100}])

        first = self.make_rule("_T30-MULTI-A", period=self.period,
                               conditions=[("Customer", customer)], target_amount=100)
        second = self.make_rule("_T30-MULTI-B", period=self.period,
                                conditions=[("Customer", customer)], target_amount=400)

        self.run_calculation(today())

        a = self.target_for(customer, first.name)
        b = self.target_for(customer, second.name)

        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        # Same order feeds both, independently.
        self.assertEqual(flt(a.achieved_amount), 200.0)
        self.assertEqual(flt(b.achieved_amount), 200.0)
        self.assertEqual(flt(a.achievement_percentage), 200.0)
        self.assertEqual(flt(b.achievement_percentage), 50.0)


# =========================================================================
# SCOPE
# =========================================================================

class ScopeCase(TargetBase):

    def setUp(self):
        super().setUp()
        self.period = self.make_period("_T30-SCOPE", add_days(today(), -5), today())
        self.customer = self.make_customer("_T30 Scope Buyer")

    def test_item_scope_counts_only_that_item(self):
        wanted = self.make_item("_T30-SCOPE-A")
        other = self.make_item("_T30-SCOPE-B")

        self.make_sales_order(self.customer, date=today(), items=[
            {"item_code": wanted, "qty": 1, "rate": 100},
            {"item_code": other, "qty": 1, "rate": 900},
        ])

        rule = self.make_rule("_T30-ITEMSCOPE", period=self.period,
                              conditions=[("Customer", self.customer)],
                              scope="Item", scope_value=wanted, target_amount=100)

        self.run_calculation(today())

        self.assertEqual(flt(self.target_for(self.customer, rule.name).achieved_amount),
                         100.0)

    def test_item_group_scope(self):
        group = frappe.get_doc({
            "doctype": "Item Group", "item_group_name": "_T30 Scope Group",
            "parent_item_group": frappe.db.get_value("Item Group", {"is_group": 1}, "name"),
            "is_group": 0,
        }).insert(ignore_permissions=True).name

        inside = self.make_item("_T30-GRP-IN", item_group=group)
        outside = self.make_item("_T30-GRP-OUT")

        self.make_sales_order(self.customer, date=today(), items=[
            {"item_code": inside, "qty": 1, "rate": 100},
            {"item_code": outside, "qty": 1, "rate": 900},
        ])

        rule = self.make_rule("_T30-GRPSCOPE", period=self.period,
                              conditions=[("Customer", self.customer)],
                              scope="Item Group", scope_value=group, target_amount=100)

        self.run_calculation(today())

        self.assertEqual(flt(self.target_for(self.customer, rule.name).achieved_amount),
                         100.0)

    def test_brand_scope(self):
        brand = self.make_brand("_T30 Scope Brand")
        inside = self.make_item("_T30-BRAND-IN", brand=brand)
        outside = self.make_item("_T30-BRAND-OUT")

        self.make_sales_order(self.customer, date=today(), items=[
            {"item_code": inside, "qty": 1, "rate": 100},
            {"item_code": outside, "qty": 1, "rate": 900},
        ])

        rule = self.make_rule("_T30-BRANDSCOPE", period=self.period,
                              conditions=[("Customer", self.customer)],
                              scope="Brand", scope_value=brand, target_amount=100)

        self.run_calculation(today())

        self.assertEqual(flt(self.target_for(self.customer, rule.name).achieved_amount),
                         100.0)

    def test_total_sales_order_scope_counts_every_row(self):
        first = self.make_item("_T30-TOTAL-A")
        second = self.make_item("_T30-TOTAL-B")

        self.make_sales_order(self.customer, date=today(), items=[
            {"item_code": first, "qty": 1, "rate": 100},
            {"item_code": second, "qty": 1, "rate": 400},
        ])

        rule = self.make_rule("_T30-TOTALSCOPE2", period=self.period,
                              conditions=[("Customer", self.customer)],
                              scope="Total Sales Order", target_amount=500)

        self.run_calculation(today())

        self.assertEqual(flt(self.target_for(self.customer, rule.name).achieved_amount),
                         500.0)


# =========================================================================
# AMOUNT
# =========================================================================

class AmountCase(TargetBase):

    def setUp(self):
        super().setUp()
        self.period = self.make_period("_T30-AMT", add_days(today(), -5), today())
        self.customer = self.make_customer("_T30 Amount Buyer")
        self.item = self.make_item("_T30-AMOUNT-ITEM")

    def test_amount_is_the_qualifying_base_net_amount(self):
        order = self.make_sales_order(self.customer, date=today(), items=[
            {"item_code": self.item, "qty": 3, "rate": 250}])

        expected = sum(flt(row.base_net_amount) for row in order.items)

        rule = self.make_rule("_T30-AMOUNT", period=self.period,
                              conditions=[("Customer", self.customer)],
                              target_amount=750)

        self.run_calculation(today())
        target = self.target_for(self.customer, rule.name)

        self.assertEqual(flt(target.achieved_amount), expected)
        self.assertEqual(flt(target.achieved_amount), 750.0)

    def test_draft_and_cancelled_orders_are_excluded(self):
        self.make_sales_order(self.customer, date=today(), submit=False, items=[
            {"item_code": self.item, "qty": 1, "rate": 1000}])

        cancelled = self.make_sales_order(self.customer, date=today(), items=[
            {"item_code": self.item, "qty": 1, "rate": 2000}])
        cancelled.cancel()

        self.make_sales_order(self.customer, date=today(), items=[
            {"item_code": self.item, "qty": 1, "rate": 100}])

        rule = self.make_rule("_T30-DOCSTATUS", period=self.period,
                              conditions=[("Customer", self.customer)],
                              target_amount=100)

        self.run_calculation(today())

        self.assertEqual(flt(self.target_for(self.customer, rule.name).achieved_amount),
                         100.0)

    def test_achievement_percentage_is_amount_over_target(self):
        self.make_sales_order(self.customer, date=today(), items=[
            {"item_code": self.item, "qty": 1, "rate": 500}])

        rule = self.make_rule("_T30-AMTPCT", period=self.period,
                              conditions=[("Customer", self.customer)],
                              target_amount=1000)

        self.run_calculation(today())

        self.assertEqual(flt(self.target_for(self.customer, rule.name).achievement_percentage),
                         50.0)


# =========================================================================
# QUANTITY AND UOM  -- the experiment's second defect
# =========================================================================

class QuantityUOMCase(TargetBase):
    """`SUM(qty)` adds 5 Box to 30 Nos and answers 35. The honest answer is 80."""

    def setUp(self):
        super().setUp()
        self.period = self.make_period("_T30-QTY", add_days(today(), -5), today())
        self.customer = self.make_customer("_T30 Qty Buyer")
        self.box = self.make_uom("_T30 Box")

    def mixed_item(self, code="_T30-QTY-ITEM"):
        """One item sellable in its stock UOM and in Box, 1 Box = 10 stock units."""

        return self.make_item(code, uoms=[(self.stock_uom, 1), (self.box, 10)])

    def test_same_uom_adds_normally(self):
        item = self.mixed_item("_T30-QTY-SAME")

        self.make_sales_order(self.customer, date=today(), items=[
            {"item_code": item, "qty": 12, "uom": self.stock_uom, "rate": 10}])

        rule = self.make_rule("_T30-QTYSAME", period=self.period,
                              conditions=[("Customer", self.customer)],
                              target_type="Quantity", target_quantity=12,
                              target_uom=self.stock_uom, target_amount=0)

        self.run_calculation(today())

        self.assertEqual(flt(self.target_for(self.customer, rule.name).achieved_quantity),
                         12.0)

    def test_a_different_selling_uom_is_converted(self):
        item = self.mixed_item("_T30-QTY-CONV")

        self.make_sales_order(self.customer, date=today(), items=[
            {"item_code": item, "qty": 5, "uom": self.box, "rate": 100}])

        rule = self.make_rule("_T30-QTYCONV", period=self.period,
                              conditions=[("Customer", self.customer)],
                              target_type="Quantity", target_quantity=50,
                              target_uom=self.stock_uom, target_amount=0)

        self.run_calculation(today())

        self.assertEqual(flt(self.target_for(self.customer, rule.name).achieved_quantity),
                         50.0)

    def test_the_mixed_box_and_nos_example(self):
        """THE regression. 5 Box + 30 Nos with 1 Box = 10 Nos is 80 Nos, not 35."""

        item = self.mixed_item("_T30-QTY-MIX")

        self.make_sales_order(self.customer, date=today(), items=[
            {"item_code": item, "qty": 5, "uom": self.box, "rate": 100}])
        self.make_sales_order(self.customer, date=today(), items=[
            {"item_code": item, "qty": 30, "uom": self.stock_uom, "rate": 10}])

        rule = self.make_rule("_T30-QTYMIX", period=self.period,
                              conditions=[("Customer", self.customer)],
                              target_type="Quantity", target_quantity=80,
                              target_uom=self.stock_uom, target_amount=0)

        self.run_calculation(today())
        target = self.target_for(self.customer, rule.name)

        self.assertEqual(flt(target.achieved_quantity), 80.0,
                         "quantities in different units were added raw")
        self.assertEqual(flt(target.achievement_percentage), 100.0)

    def test_the_target_uom_may_be_the_larger_unit(self):
        """Target in Box: 5 Box + 30 Nos = 8 Box."""

        item = self.mixed_item("_T30-QTY-BOXTGT")

        self.make_sales_order(self.customer, date=today(), items=[
            {"item_code": item, "qty": 5, "uom": self.box, "rate": 100}])
        self.make_sales_order(self.customer, date=today(), items=[
            {"item_code": item, "qty": 30, "uom": self.stock_uom, "rate": 10}])

        rule = self.make_rule("_T30-QTYBOX", period=self.period,
                              conditions=[("Customer", self.customer)],
                              target_type="Quantity", target_quantity=8,
                              target_uom=self.box, target_amount=0)

        self.run_calculation(today())

        self.assertEqual(flt(self.target_for(self.customer, rule.name).achieved_quantity),
                         8.0)

    def test_an_unconvertible_row_fails_the_target_safely(self):
        """No false total, and the previous figures survive."""

        alien = self.make_uom("_T30 Alien Unit")
        item = self.make_item("_T30-QTY-ALIEN")

        self.make_sales_order(self.customer, date=today(), items=[
            {"item_code": item, "qty": 10, "uom": self.stock_uom, "rate": 10}])

        rule = self.make_rule("_T30-QTYALIEN", period=self.period,
                              conditions=[("Customer", self.customer)],
                              target_type="Quantity", target_quantity=10,
                              target_uom=alien, target_amount=0)

        self.run_calculation(today())
        target = self.target_for(self.customer, rule.name)

        self.assertIsNotNone(target, "the failure lost the record entirely")
        self.assertTrue(target.calculation_error, "no diagnostic was recorded")
        self.assertEqual(flt(target.achieved_quantity), 0.0,
                         "an unconvertible quantity produced a total")

    def test_the_unconvertible_case_raises_rather_than_guessing(self):
        alien = self.make_uom("_T30 Alien Two")
        item = self.make_item("_T30-QTY-ALIEN2")

        self.make_sales_order(self.customer, date=today(), items=[
            {"item_code": item, "qty": 10, "uom": self.stock_uom, "rate": 10}])

        with self.assertRaises(TargetCalculationError):
            achievement.achieved_quantity(
                customer=self.customer, company=self.company,
                scope="Total Sales Order", scope_value=None,
                from_date=add_days(today(), -5), to_date=today(),
                target_uom=alien)

    def test_a_quantity_target_also_records_the_qualifying_amount(self):
        """A percentage incentive needs money, so the amount is calculated too."""

        item = self.mixed_item("_T30-QTY-AMT")

        self.make_sales_order(self.customer, date=today(), items=[
            {"item_code": item, "qty": 10, "uom": self.stock_uom, "rate": 25}])

        rule = self.make_rule("_T30-QTYAMT", period=self.period,
                              conditions=[("Customer", self.customer)],
                              target_type="Quantity", target_quantity=10,
                              target_uom=self.stock_uom, target_amount=0,
                              incentive_type="Percentage", incentive_percentage=10,
                              incentive_amount=0)

        self.run_calculation(today())
        target = self.target_for(self.customer, rule.name)

        self.assertEqual(flt(target.achieved_quantity), 10.0)
        self.assertEqual(flt(target.achieved_amount), 250.0,
                         "a quantity target recorded no qualifying amount")
        # 10% of 250, because a percentage of a quantity would not be money.
        self.assertEqual(flt(target.final_incentive), 25.0)


# =========================================================================
# INCENTIVES
# =========================================================================

class IncentiveCase(TargetBase):
    """Pure calculation, exercised directly: no fixtures to obscure the numbers."""

    def slab(self, low, high, *, amount=0, percentage=0, cap=0):
        return frappe._dict({
            "from_achievement": low, "to_achievement": high,
            "incentive_amount": amount, "incentive_percentage": percentage,
            "max_incentive": cap,
        })

    def test_fixed_amount_below_at_and_above_the_threshold(self):
        for achieved, expected in ((99.99, 0.0), (100.0, 500.0), (150.0, 500.0)):
            result = incentive.calculate_incentive(
                incentive_type="Fixed Amount", slabs=[], achievement=achieved,
                achieved_amount=10_000, incentive_amount=500)

            self.assertEqual(result["final_incentive"], expected,
                             f"achievement {achieved} paid {result['final_incentive']}")

    def test_percentage_applies_to_the_achieved_amount(self):
        below = incentive.calculate_incentive(
            incentive_type="Percentage", slabs=[], achievement=99,
            achieved_amount=10_000, incentive_percentage=5)
        self.assertEqual(below["final_incentive"], 0.0)

        at = incentive.calculate_incentive(
            incentive_type="Percentage", slabs=[], achievement=100,
            achieved_amount=10_000, incentive_percentage=5)
        self.assertEqual(at["final_incentive"], 500.0)
        self.assertEqual(at["applied_incentive_percentage"], 5)

    def test_slab_fixed_amount_uses_the_matching_slab(self):
        slabs = [self.slab(0, 49.99, amount=100),
                 self.slab(50, 99.99, amount=400),
                 self.slab(100, 200, amount=900)]

        for achieved, expected in ((10, 100.0), (75, 400.0), (100, 900.0)):
            result = incentive.calculate_incentive(
                incentive_type="Slab Fixed Amount", slabs=slabs,
                achievement=achieved, achieved_amount=10_000)
            self.assertEqual(result["final_incentive"], expected)

    def test_slab_percentage_applies_to_the_achieved_amount(self):
        slabs = [self.slab(50, 100, percentage=2)]

        result = incentive.calculate_incentive(
            incentive_type="Slab Percentage", slabs=slabs, achievement=80,
            achieved_amount=10_000)

        self.assertEqual(result["final_incentive"], 200.0)
        self.assertEqual(result["applied_incentive_percentage"], 2)

    def test_max_incentive_caps_a_percentage_slab(self):
        slabs = [self.slab(50, 100, percentage=10, cap=250)]

        result = incentive.calculate_incentive(
            incentive_type="Slab Percentage", slabs=slabs, achievement=80,
            achieved_amount=10_000)

        self.assertEqual(result["final_incentive"], 250.0, "Max Incentive was ignored")

    def test_a_zero_max_incentive_means_uncapped(self):
        slabs = [self.slab(50, 100, percentage=10, cap=0)]

        result = incentive.calculate_incentive(
            incentive_type="Slab Percentage", slabs=slabs, achievement=80,
            achieved_amount=10_000)

        self.assertEqual(result["final_incentive"], 1000.0)

    def test_slab_boundaries_are_inclusive_at_both_ends(self):
        slabs = [self.slab(50, 75, amount=400)]

        for achieved in (50, 60, 75):
            self.assertEqual(
                incentive.calculate_incentive(
                    incentive_type="Slab Fixed Amount", slabs=slabs,
                    achievement=achieved, achieved_amount=100)["final_incentive"],
                400.0, f"{achieved}% should be inside 50-75")

        self.assertEqual(
            incentive.calculate_incentive(
                incentive_type="Slab Fixed Amount", slabs=slabs,
                achievement=49.99, achieved_amount=100)["final_incentive"],
            0.0, "below the lowest slab must pay nothing")

    def test_above_the_highest_slab_the_highest_slab_applies(self):
        """A merchant writing '90-100 pays 5000' means 150% pays at least that."""

        slabs = [self.slab(0, 89.99, amount=100), self.slab(90, 100, amount=5000)]

        result = incentive.calculate_incentive(
            incentive_type="Slab Fixed Amount", slabs=slabs, achievement=150,
            achieved_amount=100)

        self.assertEqual(result["final_incentive"], 5000.0)
        self.assertEqual(result["current_slab"], "90% - 100%")

    def test_below_the_lowest_slab_names_no_slab(self):
        slabs = [self.slab(50, 100, amount=400)]

        result = incentive.calculate_incentive(
            incentive_type="Slab Fixed Amount", slabs=slabs, achievement=10,
            achieved_amount=100)

        self.assertEqual(result["current_slab"], "")
        self.assertEqual(result["final_incentive"], 0.0)

    def test_the_model_is_not_progressive(self):
        """One slab pays. Two slabs are not summed."""

        slabs = [self.slab(0, 50, amount=100), self.slab(51, 100, amount=200)]

        result = incentive.calculate_incentive(
            incentive_type="Slab Fixed Amount", slabs=slabs, achievement=100,
            achieved_amount=100)

        self.assertEqual(result["final_incentive"], 200.0,
                         "slabs were accumulated progressively")


# =========================================================================
# STATUS
# =========================================================================

class StatusCase(TargetBase):

    def status(self, *, achievement_pct, start, end, on, previous=None):
        return incentive.calculate_status(
            achievement=achievement_pct, period_start=start, period_end=end,
            calculation_date=on, previous_status=previous)

    def test_before_the_period_starts(self):
        self.assertEqual(
            self.status(achievement_pct=0, start="2026-07-01", end="2026-07-28",
                        on="2026-06-15"),
            "Not Started")

    def test_during_the_period(self):
        args = dict(start="2026-07-01", end="2026-07-28", on="2026-07-10")

        self.assertEqual(self.status(achievement_pct=0, **args), "Not Started")
        self.assertEqual(self.status(achievement_pct=40, **args), "In Progress")
        self.assertEqual(self.status(achievement_pct=100, **args), "Achieved")
        self.assertEqual(self.status(achievement_pct=130, **args), "Achieved")

    def test_after_the_period(self):
        args = dict(start="2026-07-01", end="2026-07-28", on="2026-08-10")

        self.assertEqual(self.status(achievement_pct=80, **args), "Expired")
        self.assertEqual(self.status(achievement_pct=100, **args), "Achieved")

    def test_report_end_does_not_decide_expiry(self):
        """Status reads the TARGET PERIOD. The report window is not consulted.

        `calculate_status` takes no report date, which is the structural
        guarantee.
        """

        import inspect as _inspect

        params = _inspect.signature(incentive.calculate_status).parameters

        for forbidden in ("report_start_date", "report_end_date", "report_end"):
            self.assertNotIn(forbidden, params)

    def test_completed_is_never_overwritten_by_recalculation(self):
        """A manual settlement state survives the background job."""

        self.assertEqual(
            self.status(achievement_pct=0, start="2026-07-01", end="2026-07-28",
                        on="2026-08-10", previous="Completed"),
            "Completed")

    def test_an_expired_target_becomes_achieved_after_a_backdated_sale(self):
        """The business case, end to end.

        A 1-25 June target sits at 80% and is Expired. A valid backdated 20
        January order arrives while the rule is still inside its report window,
        and recalculation lifts it to Achieved.
        """

        customer = self.make_customer("_T30 Backdated Buyer")
        item = self.make_item("_T30-BACKDATED")
        period = self.make_period("_T30-BACK", "2026-06-01", "2026-06-25")

        self.make_sales_order(customer, date="2026-06-10",
                              items=[{"item_code": item, "qty": 8, "rate": 100}])

        rule = self.make_rule("_T30-BACKRULE", period=period,
                              conditions=[("Customer", customer)], target_amount=1000,
                              report_start="2026-06-01", report_end="2026-08-31")

        self.run_calculation("2026-07-01")
        first = self.target_for(customer, rule.name)

        self.assertEqual(flt(first.achievement_percentage), 80.0)
        self.assertEqual(first.status, "Expired")

        # A real correction: dated INSIDE the target period, entered later.
        self.make_sales_order(customer, date="2026-06-20",
                              items=[{"item_code": item, "qty": 3, "rate": 100}])

        self.run_calculation("2026-07-15")
        again = self.target_for(customer, rule.name)

        self.assertEqual(flt(again.achieved_amount), 1100.0)
        self.assertEqual(flt(again.achievement_percentage), 110.0)
        self.assertEqual(again.status, "Achieved",
                         "a valid backdated in-period sale did not revive the target")


# =========================================================================
# IDEMPOTENCY
# =========================================================================

class IdempotencyCase(TargetBase):

    def test_repeated_runs_do_not_duplicate_or_change_totals(self):
        customer = self.make_customer("_T30 Idempotent")
        item = self.make_item("_T30-IDEM-ITEM")
        period = self.make_period("_T30-IDEM", add_days(today(), -5), today())

        self.make_sales_order(customer, date=today(),
                              items=[{"item_code": item, "qty": 2, "rate": 300}])

        rule = self.make_rule("_T30-IDEMRULE", period=period,
                              conditions=[("Customer", customer)], target_amount=600)

        self.run_calculation(today())
        first = self.target_for(customer, rule.name)
        first_amount = flt(first.achieved_amount)
        first_name = first.name

        self.run_calculation(today())
        self.run_calculation(today())

        rows = frappe.get_all("YOB Customer Target",
                              filters={"customer": customer, "target_rule": rule.name},
                              pluck="name")

        self.assertEqual(len(rows), 1, f"repeated runs produced {len(rows)} rows")
        self.assertEqual(rows[0], first_name, "a second row replaced the first")

        again = self.target_for(customer, rule.name)
        self.assertEqual(flt(again.achieved_amount), first_amount)
        self.assertEqual(flt(again.achievement_percentage), 100.0)

    def test_the_unique_index_exists(self):
        """Race safety is a database constraint, not a lookup-then-insert."""

        rows = frappe.db.sql(
            """
            SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
            WHERE table_name = 'tabYOB Customer Target'
              AND constraint_type = 'UNIQUE'
              AND CONSTRAINT_NAME = 'unique_customer_target_rule'
            """)

        self.assertEqual(rows[0][0], 1, "the composite unique index is missing")

    def test_one_failing_target_does_not_stop_the_run(self):
        alien = self.make_uom("_T30 Isolation Unit")
        good_customer = self.make_customer("_T30 Good Buyer")
        bad_customer = self.make_customer("_T30 Bad Buyer")
        item = self.make_item("_T30-ISO-ITEM")
        period = self.make_period("_T30-ISO", add_days(today(), -5), today())

        for customer in (good_customer, bad_customer):
            self.make_sales_order(customer, date=today(),
                                  items=[{"item_code": item, "qty": 5, "rate": 100}])

        good = self.make_rule("_T30-ISO-GOOD", period=period,
                              conditions=[("Customer", good_customer)],
                              target_amount=500)
        bad = self.make_rule("_T30-ISO-BAD", period=period,
                             conditions=[("Customer", bad_customer)],
                             target_type="Quantity", target_quantity=5,
                             target_uom=alien, target_amount=0)

        self.run_calculation(today())

        healthy = self.target_for(good_customer, good.name)
        self.assertIsNotNone(healthy, "a bad rule took down a good one")
        self.assertEqual(flt(healthy.achieved_amount), 500.0)

        broken = self.target_for(bad_customer, bad.name)
        self.assertTrue(broken.calculation_error)


# =========================================================================
# SECURITY AND PERMISSIONS
# =========================================================================

class SecuritySurfaceCase(TargetBase):
    """No public method takes a customer name. The experiment's did."""

    def test_no_target_module_exposes_a_whitelisted_method(self):
        import importlib

        for module_name in ("yob_storefront.services.target_service",
                            "yob_storefront.services.target_achievement",
                            "yob_storefront.services.target_incentive"):
            module = importlib.import_module(module_name)

            for name, obj in vars(module).items():
                if not callable(obj) or getattr(obj, "__module__", None) != module_name:
                    continue
                self.assertNotIn(
                    obj, frappe.whitelisted,
                    f"{module_name}.{name} is whitelisted; Phase 30A has no public API")

    def test_the_calculation_service_is_not_reachable_as_a_dotted_api(self):
        """Even the orchestrator: a storefront page must never trigger a run."""

        from yob_storefront.services.target_service import recalculate_due_targets

        self.assertNotIn(recalculate_due_targets, frappe.whitelisted)

    def test_calculated_fields_are_read_only(self):
        meta = frappe.get_meta("YOB Customer Target")

        # `status` and `reward_status` are deliberately absent: they are the
        # administrative pair, and `ManualCompletionCase` proves they are open
        # while every figure beside them stays shut.
        for fieldname in ("achieved_amount", "achieved_quantity",
                          "achievement_percentage", "eligible_incentive",
                          "final_incentive", "current_slab",
                          "last_calculated_on"):
            field = meta.get_field(fieldname)
            self.assertIsNotNone(field, f"{fieldname} is missing")
            self.assertTrue(field.read_only, f"{fieldname} is editable in Desk")

    def test_customer_target_cannot_be_created_or_deleted_from_desk(self):
        """Not even by System Manager: these records belong to the engine."""

        meta = frappe.get_meta("YOB Customer Target")
        perms = [p for p in meta.permissions if p.role == "System Manager"]

        self.assertTrue(perms, "System Manager has no permission row")
        self.assertFalse(perms[0].create, "Desk can create a calculated target")
        self.assertFalse(perms[0].delete, "Desk can delete a calculated target")

    def test_customer_target_cannot_be_renamed(self):
        self.assertFalse(frappe.get_meta("YOB Customer Target").allow_rename)

    def test_system_manager_can_create_and_submit_a_target_rule(self):
        """The submit permission is real, not an Administrator-only accident."""

        meta = frappe.get_meta("YOB Target Rule")
        perms = [p for p in meta.permissions if p.role == "System Manager"]

        self.assertTrue(perms, "System Manager has no permission row")
        self.assertTrue(perms[0].create)
        self.assertTrue(perms[0].submit)
        self.assertTrue(perms[0].cancel)
        self.assertTrue(perms[0].amend)

    def test_the_service_does_not_switch_users(self):
        """No `frappe.set_user`, no global permission flag."""

        import pathlib

        root = pathlib.Path(target_service.__file__).parent

        for name in ("target_service.py", "target_achievement.py", "target_incentive.py"):
            source = (root / name).read_text()
            self.assertNotIn("set_user", source, f"{name} switches the session user")
            self.assertNotIn("frappe.flags.ignore_permissions", source,
                             f"{name} sets the GLOBAL permission flag")


# =========================================================================
# THE MANUAL `Completed` STATE
# =========================================================================

class ManualCompletionCase(TargetBase):
    """`Completed` is settled by a person, and recalculation must respect it.

    The pairing matters: a document on which NOTHING is writable cannot be
    completed at all, and a document on which everything is writable lets an
    achievement figure be typed. Only `status` and `reward_status` are open.
    """

    def setUp(self):
        super().setUp()
        self.period = self.make_period("_T30-DONE", add_days(today(), -5), today())
        self.customer = self.make_customer("_T30 Completion Buyer")
        self.item = self.make_item("_T30-DONE-ITEM")

        self.make_sales_order(self.customer, date=today(),
                              items=[{"item_code": self.item, "qty": 1, "rate": 100}])

        self.rule = self.make_rule("_T30-DONERULE", period=self.period,
                                   conditions=[("Customer", self.customer)],
                                   target_amount=1000)

        self.run_calculation(today())
        self.target = self.target_for(self.customer, self.rule.name)

    def test_an_administrator_can_set_completed(self):
        """THE check: manual completion must actually be possible."""

        self.assertEqual(self.target.status, "In Progress")

        self.target.status = "Completed"
        self.target.reward_status = "Completed"
        self.target.save(ignore_permissions=True)

        again = self.target_for(self.customer, self.rule.name)
        self.assertEqual(again.status, "Completed")
        self.assertEqual(again.reward_status, "Completed")

    def test_status_and_reward_status_are_editable_in_desk(self):
        """Not `read_only`, or the Desk form offers no way to complete."""

        meta = frappe.get_meta("YOB Customer Target")

        for fieldname in ("status", "reward_status"):
            self.assertFalse(meta.get_field(fieldname).read_only,
                             f"{fieldname} cannot be set by an administrator")

    def test_recalculation_never_overwrites_completed(self):
        self.target.status = "Completed"
        self.target.save(ignore_permissions=True)

        # More sales arrive and the job runs again.
        self.make_sales_order(self.customer, date=today(),
                              items=[{"item_code": self.item, "qty": 20, "rate": 100}])
        self.run_calculation(today())

        again = self.target_for(self.customer, self.rule.name)

        self.assertEqual(again.status, "Completed",
                         "recalculation destroyed a settled target")
        # The figures still refresh -- only the verdict is frozen.
        self.assertEqual(flt(again.achieved_amount), 2100.0)

    def test_recalculation_does_not_clear_a_manual_reward_status(self):
        self.target.reward_status = "Completed"
        self.target.save(ignore_permissions=True)

        self.run_calculation(today())

        self.assertEqual(self.target_for(self.customer, self.rule.name).reward_status,
                         "Completed")

    def test_a_calculated_status_cannot_be_forged(self):
        """Editing the FIELD is not writing any VALUE into it.

        These four are verdicts on sales. Typing one asserts a business fact the
        Sales Orders do not support -- and since recalculation recomputes them,
        a forged `Achieved` would also revert silently at the next run.
        """

        # This target is calculated as "In Progress", so those three are the
        # genuine changes to attempt on it. Assigning the value it already holds
        # is a no-op, not a forgery, and proves nothing.
        for forged in ("Achieved", "Expired", "Not Started"):
            target = self.target_for(self.customer, self.rule.name)
            target.status = forged

            with self.assertRaises(frappe.PermissionError,
                                   msg=f"{forged} was hand-assignable"):
                target.save(ignore_permissions=True)

        self.assertEqual(self.target_for(self.customer, self.rule.name).status,
                         "In Progress", "a forged status stuck")

        # "In Progress" is the fourth, and needs a target the engine put
        # somewhere else -- an already-closed period reports Expired.
        past = self.make_period("_T30-FORGE-PAST", add_days(today(), -20),
                                add_days(today(), -10))
        closed = self.make_rule("_T30-FORGE-SHORT", period=past,
                                conditions=[("Customer", self.customer)],
                                target_amount=1_000_000)
        self.run_calculation(today())

        expired = self.target_for(self.customer, closed.name)
        self.assertEqual(expired.status, "Expired")

        expired.status = "In Progress"
        with self.assertRaises(frappe.PermissionError,
                               msg="In Progress was hand-assignable"):
            expired.save(ignore_permissions=True)

        self.assertEqual(self.target_for(self.customer, closed.name).status, "Expired")

    def test_the_calculation_still_owns_every_non_completed_status(self):
        """The engine assigns the four freely; only a human is restricted."""

        self.assertEqual(self.target_for(self.customer, self.rule.name).status,
                         "In Progress")

        # Enough sales to reach the target, applied by the engine.
        self.make_sales_order(self.customer, date=today(),
                              items=[{"item_code": self.item, "qty": 20, "rate": 100}])
        self.run_calculation(today())

        self.assertEqual(self.target_for(self.customer, self.rule.name).status,
                         "Achieved", "the engine could not set a calculated status")

        # And a period that has closed short still expires, by calculation.
        past = self.make_period("_T30-DONE-PAST", add_days(today(), -20),
                                add_days(today(), -10))
        short = self.make_rule("_T30-DONE-SHORT", period=past,
                               conditions=[("Customer", self.customer)],
                               target_amount=1_000_000)
        self.run_calculation(today())

        self.assertEqual(self.target_for(self.customer, short.name).status, "Expired")

    def test_a_completed_target_cannot_be_reopened_by_hand(self):
        """Reopening is not a designed workflow in this phase.

        Refused deliberately rather than left to fall out of the field being
        writable: what reopening MEANS is a decision, not a side effect.
        """

        target = self.target_for(self.customer, self.rule.name)
        target.status = "Completed"
        target.save(ignore_permissions=True)

        for attempt in ("Achieved", "In Progress", "Expired", "Not Started"):
            reopened = self.target_for(self.customer, self.rule.name)
            reopened.status = attempt

            with self.assertRaises(frappe.PermissionError,
                                   msg=f"Completed was reopened as {attempt}"):
                reopened.save(ignore_permissions=True)

        self.assertEqual(self.target_for(self.customer, self.rule.name).status,
                         "Completed")

    def test_reward_status_remains_independently_editable(self):
        """It describes a settlement, not an achievement, so nothing competes."""

        for value in ("Completed", "Pending"):
            target = self.target_for(self.customer, self.rule.name)
            target.reward_status = value
            target.save(ignore_permissions=True)

            self.assertEqual(self.target_for(self.customer, self.rule.name).reward_status,
                             value)

        # And it did not drag the calculated status along with it.
        self.assertEqual(self.target_for(self.customer, self.rule.name).status,
                         "In Progress")

    def test_marking_completed_alongside_reward_status_is_one_valid_save(self):
        """The ordinary administrative action: settle and record it together."""

        target = self.target_for(self.customer, self.rule.name)
        target.status = "Completed"
        target.reward_status = "Completed"
        target.save(ignore_permissions=True)

        again = self.target_for(self.customer, self.rule.name)
        self.assertEqual(again.status, "Completed")
        self.assertEqual(again.reward_status, "Completed")

    def test_a_calculated_figure_still_cannot_be_edited_by_hand(self):
        for fieldname, value in (("achieved_amount", 999_999),
                                 ("achievement_percentage", 500),
                                 ("final_incentive", 12_345),
                                 ("target_amount", 1),
                                 ("last_calculated_on", "2026-01-01 00:00:00")):
            target = self.target_for(self.customer, self.rule.name)
            target.set(fieldname, value)

            with self.assertRaises(frappe.PermissionError,
                                   msg=f"{fieldname} was hand-editable"):
                target.save(ignore_permissions=True)

    def test_a_customer_target_cannot_be_created_by_hand(self):
        doc = frappe.new_doc("YOB Customer Target")
        doc.customer = self.customer
        doc.target_rule = self.rule.name

        with self.assertRaises(frappe.PermissionError):
            doc.insert(ignore_permissions=True)

    def test_completing_one_target_leaves_another_alone(self):
        second = self.make_rule("_T30-DONERULE2", period=self.period,
                                conditions=[("Customer", self.customer)],
                                target_amount=50)
        self.run_calculation(today())

        # Re-read: the run above rewrote the row held from setUp.
        target = self.target_for(self.customer, self.rule.name)
        target.status = "Completed"
        target.save(ignore_permissions=True)

        self.run_calculation(today())

        self.assertEqual(self.target_for(self.customer, self.rule.name).status,
                         "Completed")
        self.assertEqual(self.target_for(self.customer, second.name).status, "Achieved")


# =========================================================================
# REPORT DATES AFTER SUBMIT
# =========================================================================

class PostSubmitReportWindowCase(TargetBase):
    """The report window is operational control, so it stays editable.

    What it must NOT do is change what was earned. Moving the window is how an
    administrator reopens or closes recalculation; the Target Period alone
    decides which transactions qualify, and no edit here can widen it.
    """

    def setUp(self):
        super().setUp()
        # A 1 - 25 June period with sales inside it and well after it.
        self.period = self.make_period("_T30-POST", "2026-06-01", "2026-06-25")
        self.customer = self.make_customer("_T30 Post Buyer")
        self.item = self.make_item("_T30-POST-ITEM")

        self.make_sales_order(self.customer, date="2026-06-10",
                              items=[{"item_code": self.item, "qty": 1, "rate": 100}])
        self.make_sales_order(self.customer, date="2026-07-15",
                              items=[{"item_code": self.item, "qty": 1, "rate": 5000}])

        self.rule = self.make_rule("_T30-POSTRULE", period=self.period,
                                   conditions=[("Customer", self.customer)],
                                   target_amount=100,
                                   report_start="2026-06-15", report_end="2026-06-20")

    def test_report_dates_are_editable_after_submission(self):
        self.assertEqual(self.rule.docstatus, 1)

        meta = frappe.get_meta("YOB Target Rule")
        for fieldname in ("report_start_date", "report_end_date"):
            self.assertTrue(meta.get_field(fieldname).allow_on_submit,
                            f"{fieldname} is frozen after submit")

        # A real post-submit edit through the ordinary document API.
        self.rule.report_end_date = "2026-08-31"
        self.rule.save(ignore_permissions=True)

        self.assertEqual(getdate(
            frappe.db.get_value("YOB Target Rule", self.rule.name, "report_end_date")),
            getdate("2026-08-31"))

    def test_extending_the_window_reopens_recalculation_only(self):
        """The window moves; the qualifying sales do not.

        THE assertion: after extending the report window to 31 August, the 15
        July order must STILL be excluded. The experiment would have added it.
        """

        self.run_calculation("2026-06-20")
        first = self.target_for(self.customer, self.rule.name)
        self.assertEqual(flt(first.achieved_amount), 100.0)

        # Closed: 1 July is past the original 20 June end.
        self.assertEqual(self.run_calculation("2026-07-01")["rules"], 0)

        self.rule.report_end_date = "2026-08-31"
        self.rule.save(ignore_permissions=True)

        # Reopened.
        summary = self.run_calculation("2026-08-01")
        self.assertEqual(summary["rules"], 1, "extending the window did not reopen the rule")

        again = self.target_for(self.customer, self.rule.name)
        self.assertEqual(flt(again.achieved_amount), 100.0,
                         "extending the Report Window pulled a post-period sale into "
                         "the target")

    def test_shrinking_the_window_stops_recalculation_without_changing_figures(self):
        self.run_calculation("2026-06-20")
        before = flt(self.target_for(self.customer, self.rule.name).achieved_amount)

        self.rule.report_start_date = "2026-06-18"
        self.rule.report_end_date = "2026-06-19"
        self.rule.save(ignore_permissions=True)

        self.assertEqual(self.run_calculation("2026-06-20")["rules"], 0)

        self.assertEqual(flt(self.target_for(self.customer, self.rule.name).achieved_amount),
                         before, "closing the window altered a stored figure")

    def test_moving_the_window_cannot_change_the_target_period(self):
        self.rule.report_start_date = "2026-06-01"
        self.rule.report_end_date = "2026-12-31"
        self.rule.save(ignore_permissions=True)

        period = frappe.db.get_value(
            "YOB Target Period", self.rule.target_period,
            ["start_date", "end_date"], as_dict=True)

        self.assertEqual(getdate(period.start_date), getdate("2026-06-01"))
        self.assertEqual(getdate(period.end_date), getdate("2026-06-25"))

        # And the window the engine actually queries is still the period's.
        self.assertEqual(
            achievement.qualifying_date_range(
                period.start_date, period.end_date, "2026-12-31"),
            (getdate("2026-06-01"), getdate("2026-06-25")))

    def test_report_dates_stay_ordered_after_submit(self):
        """Editable is not unvalidated."""

        self.rule.report_end_date = "2026-06-01"
        self.rule.report_start_date = "2026-06-20"

        with self.assertRaises(frappe.ValidationError):
            self.rule.save(ignore_permissions=True)


# =========================================================================
# CUSTOMER VISIBILITY GROUNDWORK
# =========================================================================

class CustomerVisibilityCase(TargetBase):

    def test_customer_visible_defaults_to_off(self):
        """A master safety flag must not opt products in by accident."""

        period = self.make_period("_T30-VIS", add_days(today(), -5), today())
        rule = self.make_rule("_T30-VISRULE", period=period,
                              conditions=[("Customer", SEED_CUSTOMER)])

        self.assertFalse(rule.customer_visible)

    def test_no_storefront_display_dates_live_on_the_rule(self):
        """Show From / Show Until belong to a future Storefront Block.

        Three different date ranges exist in this design and only two of them are
        the rule's: which sales count, and when recalculation runs. When a
        customer SEES a target is the block's business.
        """

        meta = frappe.get_meta("YOB Target Rule")

        for forbidden in ("show_from", "show_until", "display_from", "display_until",
                          "visible_from", "visible_until"):
            self.assertIsNone(meta.get_field(forbidden),
                              f"{forbidden} belongs to the Storefront Block, not the rule")


if __name__ == "__main__":
    unittest.main()
