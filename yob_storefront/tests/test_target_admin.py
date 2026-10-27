# Copyright (c) 2026, YOB and Shayona
"""The administrative Target Management views. READ ONLY, by construction.

WHAT THESE DEFEND
-----------------
Phase 31 adds two Desk reports over the Customer Targets Phase 30A calculates.
Four invariants matter more than any individual number:

* **No second engine.** `NoRecalculationCase` proves opening a report neither
  recalculates nor writes, and does not read a Sales Order.
* **Money is never summed across currencies.** `CurrencySafetyCase`. A rupee
  total that quietly included dollars is the worst kind of wrong: plausible.
* **Quantities are never summed across UOMs.** `QuantitySafetyCase`. 500 Boxes
  plus 250 Kg is not 750 of anything.
* **Target End is the business deadline, never Report End.** `DateSemanticsCase`.
  The two have been separate since Phase 30A and an admin list must not merge
  them.

`achievement_percentage` is read as stored and is never capped, so a customer at
125% reads 125 everywhere.
"""

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_days, flt, getdate, today

from yob_storefront.services import target_admin
from yob_storefront.services import target_service

SEED_ITEM = "YOB-BOLT-M10"
SEED_CUSTOMER = "YOB Demo Buyer"

#: Inside the test site's only active Fiscal Year (2026-04-01 .. 2027-03-31).
PERIOD_START = "2026-06-01"
PERIOD_END = "2026-06-25"


def _seeded():
    return bool(frappe.db.exists("Item", SEED_ITEM)
                and frappe.db.exists("Customer", SEED_CUSTOMER))


def executable_source(module):
    """A module's source with every docstring removed.

    The source scans below assert what the CODE does, so a docstring that
    EXPLAINS why a report date is deliberately absent must not fail the very
    check it describes. Same AST approach `test_variant_catalog` already uses.
    """

    import ast
    import inspect as _inspect

    tree = ast.parse(_inspect.getsource(module))

    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef))
                and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body.pop(0)

    return ast.unparse(tree)


class AdminBase(unittest.TestCase):
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
        self.baseline_group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")

        # Every assertion narrows to fixtures created here, so pre-existing rows
        # on the bench can never change a count.
        self.scope = {}

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()
        self.assertEqual(self.commits, [], "a commit escaped the test rollback")

    # ------------------------------------------------------------- fixtures

    def make_customer(self, name, group=None):
        return frappe.get_doc({
            "doctype": "Customer", "customer_name": name, "customer_type": "Company",
            "customer_group": group or self.baseline_group,
            "territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
        }).insert(ignore_permissions=True).name

    def make_group(self, name):
        return frappe.get_doc({
            "doctype": "Customer Group", "customer_group_name": name,
            "parent_customer_group": frappe.db.get_value(
                "Customer Group", {"is_group": 1}, "name"),
            "is_group": 0,
        }).insert(ignore_permissions=True).name

    def make_item(self, code, uoms=None):
        doc = {"doctype": "Item", "item_code": code, "item_name": code,
               "item_group": self.item_group, "stock_uom": self.stock_uom,
               "is_stock_item": 0, "is_sales_item": 1, "gst_hsn_code": self.hsn}
        if uoms:
            doc["uoms"] = [{"uom": u, "conversion_factor": cf} for u, cf in uoms]
        return frappe.get_doc(doc).insert(ignore_permissions=True).name

    def make_uom(self, name):
        if frappe.db.exists("UOM", name):
            return name
        return frappe.get_doc({"doctype": "UOM", "uom_name": name}).insert(
            ignore_permissions=True).name

    def make_period(self, name, start=PERIOD_START, end=PERIOD_END):
        return frappe.get_doc({
            "doctype": "YOB Target Period", "period_name": name,
            "start_date": start, "end_date": end, "enabled": 1,
        }).insert(ignore_permissions=True).name

    #: Scope -> the typed field carrying its value, mirroring the controller.
    SCOPE_FIELD = {"Item": "scope_item", "Item Group": "scope_item_group",
                   "Brand": "scope_brand", "Total Sales Order": None}

    def make_rule(self, name, *, period, customers, target_amount=1000,
                  target_type="Amount", target_quantity=0, target_uom=None,
                  incentive_type="Fixed Amount", incentive_amount=500,
                  slabs=None, report_start="2026-06-01", report_end="2027-03-01",
                  company=None, scope="Total Sales Order", scope_value=None):
        payload = {
            "doctype": "YOB Target Rule", "rule_name": name,
            "company": company or self.company, "target_period": period,
            "report_start_date": report_start, "report_end_date": report_end,
            "enabled": 1, "customer_visible": 1,
            "target_scope": scope,
            "target_type": target_type, "target_amount": target_amount,
            "target_quantity": target_quantity, "target_uom": target_uom,
            "incentive_type": incentive_type, "incentive_amount": incentive_amount,
            "incentive_percentage": 0,
            "conditions": [{"condition_type": "Customer", "condition_customer": c}
                           for c in customers],
            "incentive_slabs": slabs or [],
        }

        if scope_value is not None:
            payload[self.SCOPE_FIELD[scope]] = scope_value

        doc = frappe.get_doc(payload).insert(ignore_permissions=True)
        doc.submit()
        return doc

    def make_sales_order(self, customer, *, items, date=PERIOD_START, company=None):
        company = company or self.company
        warehouse = frappe.db.get_value(
            "Warehouse", {"company": company, "is_group": 0}, "name")
        doc = frappe.get_doc({
            "doctype": "Sales Order", "customer": customer, "company": company,
            "transaction_date": date, "delivery_date": date,
            "currency": frappe.get_cached_value("Company", company, "default_currency"),
            "items": [{"item_code": r["item_code"], "qty": r["qty"],
                       "uom": r.get("uom") or self.stock_uom,
                       "rate": r.get("rate", 100), "delivery_date": date,
                       "warehouse": warehouse} for r in items],
        })
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        doc.submit()
        return doc

    def calculate(self, on=PERIOD_END):
        return target_service.recalculate_due_targets(on)

    # ------------------------------------------------------------- helpers

    def rule_filter(self, *rules):
        """Narrow every aggregate to this test's own rules."""

        return {"target_rule": [r.name if hasattr(r, "name") else r for r in rules]}

    def customer_report(self, **filters):
        from yob_storefront.yob_storefront.report.yob_customer_target_report \
            import yob_customer_target_report as report

        return report.execute(filters)

    def dashboard(self, **filters):
        from yob_storefront.yob_storefront.report.yob_target_management_dashboard \
            import yob_target_management_dashboard as report

        filters.setdefault("view", "Overview")
        return report.execute(filters)

    def card(self, summary, label_starts):
        for entry in summary or []:
            if str(entry.get("label", "")).startswith(label_starts):
                return entry
        return None


# =========================================================================
# PHASE 31A -- THE CUSTOMER VIEW
# =========================================================================

class CustomerViewCase(AdminBase):

    def setUp(self):
        super().setUp()
        self.period = self.make_period("_T31-CUST")
        self.alice = self.make_customer("_T31 Alice")
        self.bob = self.make_customer("_T31 Bob")
        self.item = self.make_item("_T31-CUST-ITEM")

    def test_a_customer_is_required(self):
        """Without it the report would silently be the cross-customer dashboard."""

        with self.assertRaises(frappe.ValidationError):
            self.customer_report()

    def test_one_customer_with_one_target(self):
        self.make_sales_order(self.alice, items=[
            {"item_code": self.item, "qty": 6, "rate": 100}])
        rule = self.make_rule("_T31-ONE", period=self.period, customers=[self.alice])
        self.calculate()

        columns, rows, _msg, _chart, summary = self.customer_report(
            customer=self.alice, **self.rule_filter(rule))

        self.assertTrue(columns)
        self.assertEqual(len(rows), 1)

        row = rows[0]
        self.assertEqual(row["target_rule"], rule.name)
        self.assertEqual(row["target_period"], self.period)
        self.assertEqual(getdate(row["start_date"]), getdate(PERIOD_START))
        self.assertEqual(getdate(row["end_date"]), getdate(PERIOD_END))
        self.assertEqual(flt(row["target_amount"]), 1000.0)
        self.assertEqual(flt(row["achieved_amount"]), 600.0)
        self.assertEqual(flt(row["achievement_percentage"]), 60.0)
        self.assertEqual(flt(row["remaining_amount"]), 400.0)
        self.assertEqual(row["currency"], self.currency)
        self.assertEqual(row["company"], self.company)
        self.assertEqual(row["target_scope"], "Total Sales Order")
        self.assertIsNotNone(row["last_calculated_on"])
        self.assertIsNotNone(row["customer_target"])

        self.assertEqual(self.card(summary, "Total Targets")["value"], 1)

    def test_a_customer_with_multiple_targets(self):
        self.make_sales_order(self.alice, items=[
            {"item_code": self.item, "qty": 10, "rate": 100}])

        first = self.make_rule("_T31-MULTI-A", period=self.period,
                               customers=[self.alice], target_amount=500)
        second = self.make_rule("_T31-MULTI-B", period=self.period,
                                customers=[self.alice], target_amount=2000)
        self.calculate()

        _c, rows, _m, _ch, summary = self.customer_report(
            customer=self.alice, **self.rule_filter(first, second))

        self.assertEqual(len(rows), 2)
        self.assertEqual({r["target_rule"] for r in rows}, {first.name, second.name})
        self.assertEqual(self.card(summary, "Total Targets")["value"], 2)

    def test_an_unrelated_customers_data_is_excluded(self):
        self.make_sales_order(self.alice, items=[
            {"item_code": self.item, "qty": 6, "rate": 100}])
        self.make_sales_order(self.bob, items=[
            {"item_code": self.item, "qty": 90, "rate": 100}])

        rule = self.make_rule("_T31-ISO", period=self.period,
                              customers=[self.alice, self.bob])
        self.calculate()

        _c, rows, _m, _ch, _s = self.customer_report(
            customer=self.alice, **self.rule_filter(rule))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["customer"], self.alice)
        self.assertEqual(flt(rows[0]["achieved_amount"]), 600.0)

        blob = frappe.as_json(rows)
        self.assertNotIn(self.bob, blob, "another customer leaked into the view")

    def test_amount_and_quantity_targets_side_by_side(self):
        box = self.make_uom("_T31 Box")
        item = self.make_item("_T31-MIXED-ITEM", uoms=[(self.stock_uom, 1), (box, 10)])

        self.make_sales_order(self.alice, items=[
            {"item_code": item, "qty": 4, "uom": box, "rate": 100}])

        amount = self.make_rule("_T31-MIX-AMT", period=self.period,
                                customers=[self.alice], target_amount=1000)
        quantity = self.make_rule("_T31-MIX-QTY", period=self.period,
                                  customers=[self.alice], target_type="Quantity",
                                  target_quantity=50, target_uom=self.stock_uom,
                                  target_amount=0)
        self.calculate()

        _c, rows, _m, _ch, _s = self.customer_report(
            customer=self.alice, **self.rule_filter(amount, quantity))

        by_rule = {r["target_rule"]: r for r in rows}

        money = by_rule[amount.name]
        self.assertEqual(money["target_type"], "Amount")
        self.assertIsNotNone(money["remaining_amount"])
        self.assertIsNone(money["remaining_quantity"],
                          "an Amount row carried a quantity remainder")

        units = by_rule[quantity.name]
        self.assertEqual(units["target_type"], "Quantity")
        self.assertEqual(units["target_uom"], self.stock_uom)
        # 4 Box x 10 = 40 stock units, converted by Phase 30A.
        self.assertEqual(flt(units["achieved_quantity"]), 40.0)
        self.assertEqual(flt(units["remaining_quantity"]), 10.0)
        self.assertIsNone(units["remaining_amount"],
                          "a Quantity row carried a money remainder")

    def test_achievement_above_one_hundred_is_not_capped(self):
        self.make_sales_order(self.alice, items=[
            {"item_code": self.item, "qty": 25, "rate": 100}])
        rule = self.make_rule("_T31-OVER", period=self.period,
                              customers=[self.alice], target_amount=2000)
        self.calculate()

        _c, rows, _m, _ch, _s = self.customer_report(
            customer=self.alice, **self.rule_filter(rule))

        self.assertEqual(flt(rows[0]["achievement_percentage"]), 125.0)
        self.assertEqual(flt(rows[0]["remaining_amount"]), 0.0,
                         "remaining went negative instead of flooring at zero")

    def test_mixed_statuses_are_counted(self):
        self.make_sales_order(self.alice, items=[
            {"item_code": self.item, "qty": 10, "rate": 100}])

        achieved = self.make_rule("_T31-ST-ACH", period=self.period,
                                  customers=[self.alice], target_amount=500)
        expired = self.make_rule("_T31-ST-EXP", period=self.period,
                                 customers=[self.alice], target_amount=100_000)
        # Calculated AFTER the period closed, so the short one expires.
        self.calculate(add_days(PERIOD_END, 5))

        _c, rows, _m, _ch, summary = self.customer_report(
            customer=self.alice, **self.rule_filter(achieved, expired))

        statuses = {r["target_rule"]: r["status"] for r in rows}
        self.assertEqual(statuses[achieved.name], "Achieved")
        self.assertEqual(statuses[expired.name], "Expired")

        self.assertEqual(self.card(summary, "Achieved")["value"], 1)
        self.assertEqual(self.card(summary, "Expired")["value"], 1)
        self.assertEqual(self.card(summary, "Not Started")["value"], 0,
                         "a zero status card is missing from the summary")

    def test_a_completed_target_is_surfaced(self):
        self.make_sales_order(self.alice, items=[
            {"item_code": self.item, "qty": 10, "rate": 100}])
        rule = self.make_rule("_T31-DONE", period=self.period,
                              customers=[self.alice], target_amount=500)
        self.calculate()

        name = frappe.db.get_value(
            "YOB Customer Target",
            {"customer": self.alice, "target_rule": rule.name}, "name")
        doc = frappe.get_doc("YOB Customer Target", name)
        doc.status = "Completed"
        doc.reward_status = "Completed"
        doc.save(ignore_permissions=True)

        _c, rows, _m, _ch, summary = self.customer_report(
            customer=self.alice, **self.rule_filter(rule))

        self.assertEqual(rows[0]["status"], "Completed")
        self.assertEqual(rows[0]["reward_status"], "Completed")
        self.assertEqual(self.card(summary, "Completed")["value"], 1)

    def test_incentives_match_the_stored_calculation(self):
        self.make_sales_order(self.alice, items=[
            {"item_code": self.item, "qty": 12, "rate": 100}])
        rule = self.make_rule("_T31-INC", period=self.period, customers=[self.alice],
                              target_amount=1000, incentive_amount=750)
        self.calculate()

        _c, rows, _m, _ch, _s = self.customer_report(
            customer=self.alice, **self.rule_filter(rule))

        stored = frappe.db.get_value(
            "YOB Customer Target",
            {"customer": self.alice, "target_rule": rule.name},
            ["final_incentive", "eligible_incentive", "incentive_type", "current_slab"],
            as_dict=True)

        self.assertEqual(flt(rows[0]["final_incentive"]), flt(stored.final_incentive))
        self.assertEqual(rows[0]["incentive_type"], stored.incentive_type)
        self.assertEqual(flt(rows[0]["final_incentive"]), 750.0)

    def test_filters_narrow_the_view(self):
        self.make_sales_order(self.alice, items=[
            {"item_code": self.item, "qty": 10, "rate": 100}])

        other_period = self.make_period("_T31-OTHERP", "2026-07-01", "2026-07-25")
        first = self.make_rule("_T31-F-A", period=self.period, customers=[self.alice])
        second = self.make_rule("_T31-F-B", period=other_period,
                                customers=[self.alice])
        self.calculate("2026-07-25")

        both = self.customer_report(customer=self.alice,
                                    **self.rule_filter(first, second))[1]
        self.assertEqual(len(both), 2)

        # By rule.
        only = self.customer_report(customer=self.alice, target_rule=first.name)[1]
        self.assertEqual([r["target_rule"] for r in only], [first.name])

        # By period.
        by_period = self.customer_report(
            customer=self.alice, target_period=other_period,
            **self.rule_filter(first, second))[1]
        self.assertEqual([r["target_rule"] for r in by_period], [second.name])

        # By company, type and scope.
        for key, value in (("company", self.company), ("target_type", "Amount"),
                           ("target_scope", "Total Sales Order")):
            narrowed = self.customer_report(
                customer=self.alice, **{key: value}, **self.rule_filter(first, second))[1]
            self.assertEqual(len(narrowed), 2, f"{key} wrongly excluded rows")

        # A status nothing matches.
        none = self.customer_report(
            customer=self.alice, status="Completed",
            **self.rule_filter(first, second))[1]
        self.assertEqual(none, [])

    def test_the_columns_offer_native_drill_through(self):
        """Link columns are what make Desk cells navigate -- no custom JS."""

        columns = self.customer_report(customer=self.alice)[0] \
            if frappe.db.exists("Customer", self.alice) else []

        links = {c["fieldname"]: c for c in columns if c.get("fieldtype") == "Link"}

        for fieldname, doctype in (("target_rule", "YOB Target Rule"),
                                   ("target_period", "YOB Target Period"),
                                   ("customer_target", "YOB Customer Target"),
                                   ("company", "Company")):
            self.assertIn(fieldname, links, f"{fieldname} is not a Link column")
            self.assertEqual(links[fieldname]["options"], doctype)

    def test_money_columns_follow_each_rows_own_currency(self):
        """`options: currency` points a money cell at the row's currency column,
        so a multi-company table renders each row in its own base currency."""

        columns = self.customer_report(customer=self.alice)[0]

        for money in ("target_amount", "achieved_amount", "remaining_amount",
                      "final_incentive"):
            column = next(c for c in columns if c["fieldname"] == money)
            self.assertEqual(column["fieldtype"], "Currency")
            self.assertEqual(column["options"], "currency")


# =========================================================================
# PHASE 31B -- KPIs
# =========================================================================

class DashboardKPICase(AdminBase):

    def setUp(self):
        super().setUp()
        self.period = self.make_period("_T31-KPI")
        self.item = self.make_item("_T31-KPI-ITEM")

        self.alice = self.make_customer("_T31 KPI Alice")
        self.bob = self.make_customer("_T31 KPI Bob")

        # Alice reaches both of her targets; Bob misses his.
        self.make_sales_order(self.alice, items=[
            {"item_code": self.item, "qty": 20, "rate": 100}])
        self.make_sales_order(self.bob, items=[
            {"item_code": self.item, "qty": 2, "rate": 100}])

        self.reached = self.make_rule("_T31-KPI-LOW", period=self.period,
                                      customers=[self.alice, self.bob],
                                      target_amount=1000)
        self.second = self.make_rule("_T31-KPI-LOW2", period=self.period,
                                     customers=[self.alice], target_amount=500)
        self.calculate(add_days(PERIOD_END, 5))

        self.only = self.rule_filter(self.reached, self.second)

    def test_status_counts_are_correct(self):
        counts = target_admin.status_counts(self.only)

        # Alice: 2000 vs 1000 and vs 500 -> both Achieved.
        # Bob: 200 vs 1000, period closed -> Expired.
        self.assertEqual(counts["Achieved"], 2)
        self.assertEqual(counts["Expired"], 1)
        self.assertEqual(counts["Completed"], 0)
        self.assertEqual(sum(counts.values()), 3)

    def test_customers_are_deduplicated(self):
        """Alice holds two targets and must count as ONE customer."""

        headline = target_admin.headline_counts(self.only)

        self.assertEqual(headline["targets"], 3)
        self.assertEqual(headline["customers"], 2, "a customer was counted per target")
        self.assertEqual(headline["rules"], 2)

    def test_the_achievement_rate_formula(self):
        """(Achieved + Completed) / all selected targets."""

        rate = target_admin.achievement_rate(self.only)

        self.assertAlmostEqual(rate, 2 / 3 * 100.0, places=6)

        # A Completed target counts as reached.
        name = frappe.db.get_value(
            "YOB Customer Target",
            {"customer": self.bob, "target_rule": self.reached.name}, "name")
        doc = frappe.get_doc("YOB Customer Target", name)
        doc.status = "Completed"
        doc.save(ignore_permissions=True)

        self.assertAlmostEqual(target_admin.achievement_rate(self.only), 100.0,
                               places=6)

    def test_the_achievement_rate_of_an_empty_selection_is_zero(self):
        self.assertEqual(
            target_admin.achievement_rate({"target_rule": "_T31-NO-SUCH-RULE"}), 0.0)

    def test_the_kpi_summary_renders_every_status(self):
        _c, _r, _m, _ch, summary = self.dashboard(**self.only)

        for status in target_admin.ALL_STATUSES:
            self.assertIsNotNone(self.card(summary, status),
                                 f"{status} has no KPI card")

        self.assertEqual(self.card(summary, "Customers With Targets")["value"], 2)
        self.assertEqual(self.card(summary, "Total Targets")["value"], 3)
        self.assertIsNotNone(self.card(summary, "Achievement Rate"))

    def test_the_achievement_distribution_is_not_capped(self):
        distribution = {b["label"]: b["targets"]
                        for b in target_admin.achievement_distribution(self.only)}

        # Alice is at 200% and 400%; Bob at 20%.
        self.assertEqual(distribution["120%+"], 2)
        self.assertEqual(distribution[">0-25%"], 1)
        self.assertIn("100-<120%", distribution)

    def test_the_chart_carries_the_distribution(self):
        _c, _r, _m, chart, _s = self.dashboard(**self.only)

        self.assertEqual(chart["type"], "bar")
        self.assertEqual(len(chart["data"]["labels"]),
                         len(target_admin.ACHIEVEMENT_BUCKETS))
        self.assertEqual(sum(chart["data"]["datasets"][0]["values"]), 3)

    def test_data_freshness_is_published(self):
        newest = target_admin.last_calculated(self.only)
        self.assertIsNotNone(newest)

        _c, _r, message, _ch, _s = self.dashboard(**self.only)
        self.assertIn("Last calculated", message)

    def test_an_invalid_view_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self.dashboard(view="Everything", **self.only)

    def test_a_failed_calculation_is_counted_but_not_measured(self):
        """Inverted deliberately in the Phase 31 hardening.

        This asserted that `Total Targets` DROPPED when a calculation failed,
        which made the headline shrink with no explanation. The contract now is
        that the record is still counted, the achievement metrics exclude it, and
        `Calculation Issues` is the difference.
        """

        before = target_admin.headline_counts(self.only)["targets"]
        measured_before = sum(target_admin.status_counts(self.only).values())

        name = frappe.db.get_value(
            "YOB Customer Target",
            {"customer": self.bob, "target_rule": self.reached.name}, "name")
        frappe.db.set_value("YOB Customer Target", name, "calculation_error",
                            "internal conversion failure")

        self.assertEqual(target_admin.headline_counts(self.only)["targets"], before,
                         "Total Targets shrank with no explanation")
        self.assertEqual(target_admin.calculation_issues(self.only), 1)
        self.assertEqual(sum(target_admin.status_counts(self.only).values()),
                         measured_before - 1,
                         "the stale record is still being measured")

        rows = target_admin.target_rows(self.only)
        self.assertNotIn(name, [r["customer_target"] for r in rows])


# =========================================================================
# CURRENCY SAFETY
# =========================================================================

class CurrencySafetyCase(AdminBase):

    def make_company(self, name="_T31 Second Co", abbr="T31S", currency=None):
        existing = frappe.db.get_value("Company", {"company_name": name}, "name")
        if existing:
            return existing
        return frappe.get_doc({
            "doctype": "Company", "company_name": name, "abbr": abbr,
            "default_currency": currency or self.currency,
            "country": frappe.db.get_value("Company", self.company, "country") or "India",
            "create_chart_of_accounts_based_on": "Standard Template",
            "chart_of_accounts": "Standard",
        }).insert(ignore_permissions=True).name

    def setUp(self):
        super().setUp()
        self.period = self.make_period("_T31-CUR")
        self.item = self.make_item("_T31-CUR-ITEM")
        self.alice = self.make_customer("_T31 Cur Alice")

    def test_one_currency_sums_correctly(self):
        self.make_sales_order(self.alice, items=[
            {"item_code": self.item, "qty": 20, "rate": 100}])

        first = self.make_rule("_T31-CUR-A", period=self.period,
                               customers=[self.alice], target_amount=1000,
                               incentive_amount=300)
        second = self.make_rule("_T31-CUR-B", period=self.period,
                                customers=[self.alice], target_amount=1000,
                                incentive_amount=700)
        self.calculate()

        only = self.rule_filter(first, second)
        exposure = target_admin.incentive_exposure(only)

        self.assertEqual(len(exposure), 1, "one currency should give one group")
        self.assertEqual(exposure[0]["currency"], self.currency)
        self.assertEqual(flt(exposure[0]["final_incentive"]), 1000.0)
        self.assertEqual(exposure[0]["targets"], 2)

    def test_mixed_currencies_are_never_summed_into_one_total(self):
        """THE guard. A single number here would be a lie in whichever symbol
        happened to render."""

        other_currency = frappe.db.get_value(
            "Currency", {"name": ["!=", self.currency], "enabled": 1}, "name")
        if not other_currency:
            self.skipTest("this bench has only one enabled Currency")

        other_company = self.make_company(currency=other_currency)
        other_customer = self.make_customer("_T31 Cur Bob")

        self.make_sales_order(self.alice, items=[
            {"item_code": self.item, "qty": 20, "rate": 100}])
        self.make_sales_order(other_customer, company=other_company, items=[
            {"item_code": self.item, "qty": 20, "rate": 100}])

        mine = self.make_rule("_T31-CUR-HOME", period=self.period,
                              customers=[self.alice], target_amount=1000,
                              incentive_amount=300)
        theirs = self.make_rule("_T31-CUR-AWAY", period=self.period,
                                customers=[other_customer], target_amount=1000,
                                incentive_amount=700, company=other_company)
        self.calculate()

        only = self.rule_filter(mine, theirs)
        exposure = target_admin.incentive_exposure(only)

        currencies = {row["currency"] for row in exposure}
        self.assertEqual(len(exposure), 2,
                         f"two currencies collapsed into {len(exposure)} total(s)")
        self.assertEqual(currencies, {self.currency, other_currency})

        # And the dashboard surfaces one card per currency, never a merged one.
        _c, _r, message, _ch, summary = self.dashboard(**only)
        money_cards = [c for c in summary
                       if str(c.get("label", "")).startswith("Incentive Exposure")]

        self.assertEqual(len(money_cards), 2)
        self.assertEqual({c["currency"] for c in money_cards},
                         {self.currency, other_currency})
        self.assertIn("per currency", message,
                      "the mixed-currency caveat is not shown to the reader")

    def test_narrowing_to_one_company_gives_one_monetary_total(self):
        other_currency = frappe.db.get_value(
            "Currency", {"name": ["!=", self.currency], "enabled": 1}, "name")
        if not other_currency:
            self.skipTest("this bench has only one enabled Currency")

        other_company = self.make_company(currency=other_currency)
        other_customer = self.make_customer("_T31 Cur Carol")

        self.make_sales_order(self.alice, items=[
            {"item_code": self.item, "qty": 20, "rate": 100}])
        self.make_sales_order(other_customer, company=other_company, items=[
            {"item_code": self.item, "qty": 20, "rate": 100}])

        mine = self.make_rule("_T31-CUR-ONE", period=self.period,
                              customers=[self.alice], target_amount=1000,
                              incentive_amount=300)
        theirs = self.make_rule("_T31-CUR-TWO", period=self.period,
                                customers=[other_customer], target_amount=1000,
                                incentive_amount=700, company=other_company)
        self.calculate()

        narrowed = dict(self.rule_filter(mine, theirs))
        narrowed["company"] = self.company

        exposure = target_admin.incentive_exposure(narrowed)

        self.assertEqual(len(exposure), 1)
        self.assertEqual(exposure[0]["currency"], self.currency)
        self.assertEqual(flt(exposure[0]["final_incentive"]), 300.0)

    def test_no_exchange_rate_is_ever_applied(self):
        source = executable_source(target_admin)

        for forbidden in ("conversion_rate", "exchange_rate", "get_exchange_rate",
                          "Currency Exchange"):
            self.assertNotIn(forbidden, source,
                             f"{forbidden} appears in an aggregation that must not convert")


# =========================================================================
# QUANTITY SAFETY
# =========================================================================

class QuantitySafetyCase(AdminBase):

    def setUp(self):
        super().setUp()
        self.period = self.make_period("_T31-QTY")
        self.alice = self.make_customer("_T31 Qty Alice")

        self.box = self.make_uom("_T31 Qty Box")
        self.kg = self.make_uom("_T31 Qty Kg")

        self.boxes = self.make_item("_T31-QTY-BOXES",
                                    uoms=[(self.stock_uom, 1), (self.box, 10)])
        self.weighed = self.make_item("_T31-QTY-WEIGHED",
                                      uoms=[(self.stock_uom, 1), (self.kg, 5)])

        self.make_sales_order(self.alice, items=[
            {"item_code": self.boxes, "qty": 50, "uom": self.stock_uom, "rate": 10}])
        self.make_sales_order(self.alice, items=[
            {"item_code": self.weighed, "qty": 25, "uom": self.stock_uom, "rate": 10}])

        # SCOPED TO ITS OWN ITEM on purpose. With `Total Sales Order` both items
        # feed both rules, and the weighed item has no Box conversion -- so Phase
        # 30A would (correctly) refuse to produce a total and set
        # `calculation_error`. Scoping each rule to the product it is about is
        # both the realistic configuration and the only one with compatible
        # units, which is exactly the point this case is making.
        self.in_boxes = self.make_rule(
            "_T31-QTY-BOX", period=self.period, customers=[self.alice],
            target_type="Quantity", target_quantity=500, target_uom=self.box,
            target_amount=0, scope="Item", scope_value=self.boxes)
        self.in_kg = self.make_rule(
            "_T31-QTY-KG", period=self.period, customers=[self.alice],
            target_type="Quantity", target_quantity=250, target_uom=self.kg,
            target_amount=0, scope="Item", scope_value=self.weighed)
        self.calculate()

        self.only = self.rule_filter(self.in_boxes, self.in_kg)

    def test_incompatible_uoms_are_never_summed(self):
        """THE guard: `500 Boxes + 250 Kg` must not become `750` of anything."""

        summary = target_admin.quantity_summary(self.only)

        self.assertEqual(summary["targets"], 2)
        self.assertEqual(summary["distinct_uoms"], 2)

        # Counts and statistics only -- no summed quantity key exists at all.
        for forbidden in ("target_quantity", "achieved_quantity", "total_quantity",
                          "remaining_quantity"):
            self.assertNotIn(forbidden, summary,
                             f"{forbidden} is a cross-UOM total")

    def test_the_dashboard_publishes_no_summed_quantity_card(self):
        _c, _r, _m, _ch, summary = self.dashboard(**self.only)

        labels = [str(c.get("label", "")) for c in summary]

        self.assertIn("Quantity Targets", labels)

        for forbidden in ("Total Quantity", "Target Quantity", "Achieved Quantity"):
            self.assertNotIn(forbidden, labels,
                             f"a summed quantity card ({forbidden}) was published")

    def test_amount_totals_exclude_quantity_targets(self):
        """Otherwise a 'total target' would be part money, part unitless."""

        item = self.make_item("_T31-QTY-AMTITEM")
        self.make_sales_order(self.alice, items=[
            {"item_code": item, "qty": 5, "rate": 100}])
        # An Amount target has no UOM to be incompatible with, but scoping keeps
        # the fixture's three rules independent of one another.
        money = self.make_rule("_T31-QTY-AMT", period=self.period,
                               customers=[self.alice], target_amount=1000,
                               scope="Item", scope_value=item)
        self.calculate()

        every = self.rule_filter(self.in_boxes, self.in_kg, money)
        totals = target_admin.amount_totals(every)

        self.assertEqual(len(totals), 1)
        # 1000 from the Amount rule only; the two quantity targets contribute 0.
        self.assertEqual(flt(totals[0]["target_amount"]), 1000.0)
        self.assertEqual(totals[0]["targets"], 1)

    def test_individual_rows_keep_their_own_quantity_and_uom(self):
        rows = target_admin.target_rows(self.only)
        by_rule = {r["target_rule"]: r for r in rows}

        boxes = by_rule[self.in_boxes.name]
        self.assertEqual(boxes["target_uom"], self.box)
        self.assertEqual(flt(boxes["target_quantity"]), 500.0)

        kilos = by_rule[self.in_kg.name]
        self.assertEqual(kilos["target_uom"], self.kg)
        self.assertEqual(flt(kilos["target_quantity"]), 250.0)


# =========================================================================
# NEAR TARGET
# =========================================================================

class NearTargetCase(AdminBase):

    def setUp(self):
        super().setUp()
        # A live period, so these targets are In Progress rather than Expired.
        self.period = self.make_period("_T31-NEAR", add_days(today(), -3),
                                       add_days(today(), 10))
        self.item = self.make_item("_T31-NEAR-ITEM")
        self.rules = []

        self.cases = {}
        for label, qty, target in (("eighty", 8, 1000), ("ninetynine", 999, 100_000),
                                   ("exactly100", 10, 1000), ("over", 25, 1000),
                                   ("low", 1, 1000)):
            customer = self.make_customer(f"_T31 Near {label}")
            self.make_sales_order(customer, date=today(),
                                  items=[{"item_code": self.item, "qty": qty,
                                          "rate": 100}])
            rule = self.make_rule(f"_T31-NEAR-{label}", period=self.period,
                                  customers=[customer], target_amount=target,
                                  report_start=add_days(today(), -3),
                                  report_end=add_days(today(), 30))
            self.rules.append(rule)
            self.cases[label] = (customer, rule)

        self.calculate(today())
        self.only = self.rule_filter(*self.rules)

    def test_eighty_to_just_under_one_hundred_qualifies(self):
        rows = target_admin.near_target(self.only)
        rules = {r["target_rule"] for r in rows}

        self.assertIn(self.cases["eighty"][1].name, rules, "80% was excluded")
        self.assertIn(self.cases["ninetynine"][1].name, rules, "99.9% was excluded")

    def test_one_hundred_and_above_are_excluded(self):
        rows = target_admin.near_target(self.only)
        rules = {r["target_rule"] for r in rows}

        self.assertNotIn(self.cases["exactly100"][1].name, rules,
                         "exactly 100% is achieved, not near")
        self.assertNotIn(self.cases["over"][1].name, rules)

    def test_low_achievement_is_excluded(self):
        rows = target_admin.near_target(self.only)

        self.assertNotIn(self.cases["low"][1].name,
                         {r["target_rule"] for r in rows})

    def test_the_floor_is_configurable(self):
        """Where "close enough to chase" starts is a sales decision."""

        rows = target_admin.near_target(self.only, floor=10)

        self.assertIn(self.cases["low"][1].name, {r["target_rule"] for r in rows},
                      "lowering the floor did not widen the list")

    def test_sorted_closest_to_achievement_first(self):
        rows = target_admin.near_target(self.only, floor=0)
        percentages = [flt(r["achievement_percentage"]) for r in rows]

        self.assertEqual(percentages, sorted(percentages, reverse=True),
                         "the call list is not ordered closest-first")

    def test_rows_carry_what_a_salesperson_needs(self):
        rows = target_admin.near_target(self.only)
        row = next(r for r in rows
                   if r["target_rule"] == self.cases["eighty"][1].name)

        self.assertEqual(flt(row["achievement_percentage"]), 80.0)
        self.assertEqual(flt(row["remaining_amount"]), 200.0)
        self.assertEqual(getdate(row["end_date"]), getdate(add_days(today(), 10)))
        self.assertEqual(row["days_remaining"], 10)
        self.assertIsNotNone(row["customer"])


# =========================================================================
# AT RISK AND ENDING SOON
# =========================================================================

class AtRiskCase(AdminBase):

    def setUp(self):
        super().setUp()
        self.item = self.make_item("_T31-RISK-ITEM")
        self.rules = []
        self.cases = {}

        # label -> (days until period end, qty, target)
        plan = {
            "risky": (3, 1, 1000),          # ends soon, 10% -> AT RISK
            "healthy": (3, 10, 1000),       # ends soon, 100% -> achieved
            "faraway": (60, 1, 1000),       # low but months away
            "already_over": (-5, 1, 1000),  # period closed -> Expired
        }

        for label, (days, qty, target) in plan.items():
            period = self.make_period(
                f"_T31-RISK-{label}", add_days(today(), -10), add_days(today(), days))
            customer = self.make_customer(f"_T31 Risk {label}")
            self.make_sales_order(customer, date=add_days(today(), -5),
                                  items=[{"item_code": self.item, "qty": qty,
                                          "rate": 100}])
            rule = self.make_rule(f"_T31-RISKR-{label}", period=period,
                                  customers=[customer], target_amount=target,
                                  report_start=add_days(today(), -10),
                                  report_end=add_days(today(), 90))
            self.rules.append(rule)
            self.cases[label] = rule

        self.calculate(today())
        self.only = self.rule_filter(*self.rules)

    def rules_in(self, rows):
        return {r["target_rule"] for r in rows}

    def test_ending_soon_and_behind_is_at_risk(self):
        rows = target_admin.at_risk(self.only)

        self.assertIn(self.cases["risky"].name, self.rules_in(rows))

    def test_an_achieved_target_is_not_at_risk(self):
        rows = target_admin.at_risk(self.only)

        self.assertNotIn(self.cases["healthy"].name, self.rules_in(rows))

    def test_an_expired_target_is_not_at_risk(self):
        """Its period already closed -- that is Expired, a separate outcome."""

        expired_name = frappe.db.get_value(
            "YOB Customer Target", {"target_rule": self.cases["already_over"].name},
            "status")
        self.assertEqual(expired_name, "Expired")

        rows = target_admin.at_risk(self.only)
        self.assertNotIn(self.cases["already_over"].name, self.rules_in(rows))

    def test_a_far_future_target_is_not_at_risk(self):
        rows = target_admin.at_risk(self.only)

        self.assertNotIn(self.cases["faraway"].name, self.rules_in(rows))

    def test_the_horizon_and_ceiling_are_configurable(self):
        wide = target_admin.at_risk(self.only, days=90, ceiling=75)

        self.assertIn(self.cases["faraway"].name, self.rules_in(wide),
                      "widening the horizon did not include the distant target")

    def test_at_risk_rows_carry_the_deadline(self):
        row = next(r for r in target_admin.at_risk(self.only)
                   if r["target_rule"] == self.cases["risky"].name)

        self.assertEqual(getdate(row["end_date"]), getdate(add_days(today(), 3)))
        self.assertEqual(row["days_remaining"], 3)
        self.assertEqual(flt(row["achievement_percentage"]), 10.0)
        self.assertEqual(flt(row["remaining_amount"]), 900.0)

    def test_ending_soon_lists_active_targets_regardless_of_achievement(self):
        """Unlike At Risk, no achievement bound: a target at 95% with two days
        left is still ending soon, and that is useful."""

        rows = target_admin.ending_soon(self.only)
        rules = self.rules_in(rows)

        self.assertIn(self.cases["risky"].name, rules)
        self.assertNotIn(self.cases["already_over"].name, rules,
                         "a closed period is not 'ending soon'")
        self.assertNotIn(self.cases["faraway"].name, rules)

    def test_ending_soon_excludes_a_reached_target(self):
        rows = target_admin.ending_soon(self.only)

        self.assertNotIn(self.cases["healthy"].name, self.rules_in(rows))


# =========================================================================
# DATE SEMANTICS
# =========================================================================

class DateSemanticsCase(AdminBase):
    """The business deadline is Target Period end. Never Report End."""

    def test_no_admin_query_reads_a_report_date(self):
        """Structural: the aggregation never selects or filters a report date.

        Phase 30A kept the two apart deliberately -- the report window controls
        only when recalculation runs. An admin list that used it would tell
        management a target was closing when only the reporting window was.
        """

        source = executable_source(target_admin)

        for forbidden in ("report_start_date", "report_end_date"):
            self.assertNotIn(forbidden, source,
                             f"{forbidden} reached the admin aggregation")

    def test_ending_soon_follows_target_end_not_report_end(self):
        """A rule whose REPORT window closes tomorrow but whose TARGET period runs
        for months must NOT appear in Ending Soon."""

        period = self.make_period("_T31-DATES", add_days(today(), -5),
                                  add_days(today(), 120))
        customer = self.make_customer("_T31 Dates Buyer")
        item = self.make_item("_T31-DATES-ITEM")

        self.make_sales_order(customer, date=add_days(today(), -2),
                              items=[{"item_code": item, "qty": 1, "rate": 100}])

        rule = self.make_rule("_T31-DATES-RULE", period=period, customers=[customer],
                              target_amount=1000,
                              report_start=add_days(today(), -5),
                              # Report window closes tomorrow.
                              report_end=add_days(today(), 1))
        self.calculate(today())

        only = self.rule_filter(rule)

        self.assertEqual(target_admin.ending_soon(only), [],
                         "a closing REPORT window was reported as a closing target")
        self.assertEqual(target_admin.at_risk(only), [],
                         "a closing REPORT window put a target At Risk")

        # And with the real deadline inside the horizon it does appear.
        frappe.db.set_value("YOB Target Period", period, "end_date",
                            add_days(today(), 3))
        self.assertEqual(
            {r["target_rule"] for r in target_admin.ending_soon(only)}, {rule.name})

    def test_the_row_end_date_is_the_target_period_end(self):
        period = self.make_period("_T31-ENDDATE", "2026-06-01", "2026-06-25")
        customer = self.make_customer("_T31 EndDate Buyer")
        item = self.make_item("_T31-ENDDATE-ITEM")

        self.make_sales_order(customer, items=[
            {"item_code": item, "qty": 1, "rate": 100}])
        rule = self.make_rule("_T31-ENDDATE-RULE", period=period,
                              customers=[customer], target_amount=1000,
                              report_start="2026-06-01", report_end="2027-03-01")
        self.calculate()

        row = target_admin.target_rows(self.rule_filter(rule))[0]

        self.assertEqual(getdate(row["end_date"]), getdate("2026-06-25"))


# =========================================================================
# RULE PERFORMANCE
# =========================================================================

class RulePerformanceCase(AdminBase):

    def setUp(self):
        super().setUp()
        self.period = self.make_period("_T31-PERF")
        self.item = self.make_item("_T31-PERF-ITEM")

        self.alice = self.make_customer("_T31 Perf Alice")
        self.bob = self.make_customer("_T31 Perf Bob")

        self.make_sales_order(self.alice, items=[
            {"item_code": self.item, "qty": 20, "rate": 100}])
        self.make_sales_order(self.bob, items=[
            {"item_code": self.item, "qty": 2, "rate": 100}])

        self.rule = self.make_rule("_T31-PERF-RULE", period=self.period,
                                   customers=[self.alice, self.bob],
                                   target_amount=1000, incentive_amount=400)
        self.calculate(add_days(PERIOD_END, 5))
        self.only = self.rule_filter(self.rule)

    def test_rule_level_aggregation(self):
        rows = target_admin.rule_performance(self.only)

        self.assertEqual(len(rows), 1)
        row = rows[0]

        self.assertEqual(row["target_rule"], self.rule.name)
        self.assertEqual(row["targets"], 2)
        self.assertEqual(row["customers"], 2)
        self.assertEqual(row["reached"], 1)
        self.assertEqual(row["expired"], 1)
        self.assertAlmostEqual(row["achievement_rate"], 50.0, places=6)
        # Alice 200%, Bob 20% -> average 110%, uncapped.
        self.assertAlmostEqual(row["average_achievement"], 110.0, places=6)
        self.assertEqual(flt(row["final_incentive"]), 400.0)
        self.assertEqual(row["currency"], self.currency)
        self.assertEqual(getdate(row["end_date"]), getdate(PERIOD_END))

    def test_eligibility_is_not_recalculated(self):
        """A rule's population is the Customer Targets the job already made."""

        source = executable_source(target_admin)

        for forbidden in ("eligible_customers", "recalculate_due_targets",
                          "calculate_customer_target", "tabYOB Target Rule Condition"):
            self.assertNotIn(forbidden, source,
                             f"{forbidden} means eligibility is being re-derived")

    def test_the_dashboard_rule_view_uses_its_own_columns(self):
        columns, rows, _m, _ch, _s = self.dashboard(view="Rule Performance",
                                                     **self.only)

        fieldnames = {c["fieldname"] for c in columns}

        self.assertIn("achievement_rate", fieldnames)
        self.assertIn("average_achievement", fieldnames)
        self.assertIn("customers", fieldnames)
        self.assertEqual(len(rows), 1)


# =========================================================================
# NO RECALCULATION
# =========================================================================

class NoRecalculationCase(AdminBase):

    def setUp(self):
        super().setUp()
        self.period = self.make_period("_T31-FRESH")
        self.customer = self.make_customer("_T31 Fresh Buyer")
        self.item = self.make_item("_T31-FRESH-ITEM")

        self.make_sales_order(self.customer, items=[
            {"item_code": self.item, "qty": 6, "rate": 100}])
        self.rule = self.make_rule("_T31-FRESH-RULE", period=self.period,
                                   customers=[self.customer], target_amount=1000)
        self.calculate()
        self.only = self.rule_filter(self.rule)

    def test_opening_a_report_does_not_recalculate(self):
        with patch.object(target_service, "recalculate_due_targets",
                          side_effect=AssertionError("the report recalculated")), \
             patch.object(target_service, "calculate_customer_target",
                          side_effect=AssertionError("the report recalculated")):
            self.customer_report(customer=self.customer, **self.only)
            self.dashboard(**self.only)
            self.dashboard(view="Near Target", **self.only)
            self.dashboard(view="At Risk", **self.only)
            self.dashboard(view="Ending Soon", **self.only)
            self.dashboard(view="Rule Performance", **self.only)

    def test_opening_a_report_modifies_no_customer_target(self):
        name = frappe.db.get_value(
            "YOB Customer Target",
            {"customer": self.customer, "target_rule": self.rule.name}, "name")
        before = frappe.db.get_value(
            "YOB Customer Target", name,
            ["modified", "achieved_amount", "achievement_percentage", "status",
             "last_calculated_on"], as_dict=True)

        self.customer_report(customer=self.customer, **self.only)
        self.dashboard(**self.only)

        after = frappe.db.get_value(
            "YOB Customer Target", name,
            ["modified", "achieved_amount", "achievement_percentage", "status",
             "last_calculated_on"], as_dict=True)

        self.assertEqual(dict(before), dict(after), "a report wrote to a target")

    def test_reports_read_no_sales_order(self):
        """The achievement is a stored snapshot; re-reading sales would be a
        second engine."""

        seen = []
        real = frappe.db.sql

        def spy(query, *args, **kwargs):
            seen.append(str(query))
            return real(query, *args, **kwargs)

        with patch.object(frappe.db, "sql", side_effect=spy):
            self.customer_report(customer=self.customer, **self.only)
            self.dashboard(**self.only)

        self.assertEqual([q for q in seen if "tabSales Order" in q], [],
                         "an admin report queried Sales Orders")

    def test_the_admin_service_performs_no_write(self):
        source = executable_source(target_admin)

        for forbidden in ("db_set(", ".save(", ".insert(", "set_value(", ".delete(",
                          "db_insert", "db_update"):
            self.assertNotIn(forbidden, source,
                             f"{forbidden} appears in a read-only aggregation")


# =========================================================================
# STALE FIGURES  (Phase 31 hardening)
# =========================================================================

class CalculationErrorCase(AdminBase):
    """A target whose last calculation failed keeps STALE figures (Phase 30A).

    The contract this pins:

        counted everywhere     Total Targets, Customers, Calculation Issues
        valid records only     status counts, achievement rate, buckets,
                               incentive exposure, amount totals, quantity stats,
                               Near Target, At Risk, Rule Performance
        included but FLAGGED   the customer report, and Ending Soon

    The scenario is deliberately nasty: the errored target is stored at 90%, which
    is squarely inside the Near Target band. If the exclusion were missing it
    would not merely skew a number -- it would put a customer on a call list on
    the strength of a figure nobody can date.
    """

    def setUp(self):
        super().setUp()
        # A live period, so an un-errored target here would be In Progress and a
        # genuine Near Target candidate.
        self.period = self.make_period("_T31-ERR", add_days(today(), -3),
                                       add_days(today(), 5))
        self.item = self.make_item("_T31-ERR-ITEM")

        self.healthy_customer = self.make_customer("_T31 Err Healthy")
        self.broken_customer = self.make_customer("_T31 Err Broken")

        # Healthy: 850 of 1000 -> 85%, a real Near Target row.
        self.make_sales_order(self.healthy_customer, date=today(), items=[
            {"item_code": self.item, "qty": 1, "rate": 850}])
        # Broken: will also land near 90% and then be marked errored.
        self.make_sales_order(self.broken_customer, date=today(), items=[
            {"item_code": self.item, "qty": 1, "rate": 900}])

        self.rule = self.make_rule(
            "_T31-ERR-RULE", period=self.period,
            customers=[self.healthy_customer, self.broken_customer],
            target_amount=1000, incentive_amount=400,
            report_start=add_days(today(), -3), report_end=add_days(today(), 60))
        self.calculate(today())

        self.broken = frappe.db.get_value(
            "YOB Customer Target",
            {"customer": self.broken_customer, "target_rule": self.rule.name}, "name")

        # Stored at 90% and THEN marked unreliable -- exactly Phase 30A's
        # behaviour of keeping the previous figures.
        self.assertEqual(
            flt(frappe.db.get_value("YOB Customer Target", self.broken,
                                    "achievement_percentage")), 90.0)
        frappe.db.set_value("YOB Customer Target", self.broken, "calculation_error",
                            "internal UOM conversion failed for ITEM-X")

        self.only = self.rule_filter(self.rule)

    # ------------------------------------------------- counted everywhere

    def test_total_targets_still_counts_the_errored_record(self):
        """Otherwise the headline would shrink with no explanation."""

        headline = target_admin.headline_counts(self.only)

        self.assertEqual(headline["targets"], 2)
        self.assertEqual(headline["customers"], 2)

    def test_the_issue_count_reports_it(self):
        self.assertEqual(target_admin.calculation_issues(self.only), 1)

    def test_the_difference_between_headline_and_metrics_is_explainable(self):
        """Total Targets - Calculation Issues == the measured population."""

        headline = target_admin.headline_counts(self.only)
        issues = target_admin.calculation_issues(self.only)
        measured = sum(target_admin.status_counts(self.only).values())

        self.assertEqual(headline["targets"] - issues, measured)

    # ------------------------------------------------- valid records only

    def test_it_cannot_appear_as_an_ordinary_near_target(self):
        """THE guard. 90% is inside the band; a stale 90% must not be chased."""

        rows = target_admin.near_target(self.only)

        self.assertEqual([r["customer"] for r in rows], [self.healthy_customer])
        self.assertNotIn(self.broken, [r["customer_target"] for r in rows])

    def test_it_cannot_appear_at_risk(self):
        """Ends in 5 days and reads 90% -- but 90% is not a number we have."""

        rows = target_admin.at_risk(self.only, days=10, ceiling=95)

        self.assertNotIn(self.broken, [r["customer_target"] for r in rows])
        self.assertIn(self.healthy_customer, [r["customer"] for r in rows])

    def test_the_errored_target_is_outside_the_achievement_rate_population(self):
        """It takes no part in EITHER side of the fraction.

        Asserted through the denominator rather than the ratio: both rows here
        are In Progress, so the rate is 0% with or without the stale one, and a
        ratio comparison would pass for the wrong reason. What must be true is
        that the measured population is one record, not two -- and that clearing
        the error puts it back, which is what makes the exclusion real.
        """

        self.assertEqual(sum(target_admin.status_counts(self.only).values()), 1,
                         "the stale record was measured")
        self.assertEqual(target_admin.achievement_rate(self.only), 0.0)

        frappe.db.set_value("YOB Customer Target", self.broken, "calculation_error", "")

        self.assertEqual(sum(target_admin.status_counts(self.only).values()), 2,
                         "clearing the error did not restore the record, so the "
                         "exclusion is not what this test thinks it is")

        frappe.db.set_value("YOB Customer Target", self.broken, "calculation_error",
                            "internal failure")

        self.assertEqual(sum(target_admin.status_counts(self.only).values()), 1)

    def test_a_stale_reached_target_cannot_inflate_the_rate(self):
        """The case that actually matters: a stale row stored as Achieved.

        Without the exclusion it would raise the achievement rate on the strength
        of a figure nobody can date.
        """

        frappe.db.set_value("YOB Customer Target", self.broken, "status", "Achieved")

        # Still excluded, so the healthy In Progress row alone is measured.
        self.assertEqual(target_admin.achievement_rate(self.only), 0.0,
                         "a stale Achieved record inflated the achievement rate")

        frappe.db.set_value("YOB Customer Target", self.broken, "calculation_error", "")

        self.assertEqual(target_admin.achievement_rate(self.only), 50.0,
                         "the fixture does not exercise the exclusion")

    def test_it_does_not_appear_in_any_achievement_bucket(self):
        distribution = {b["label"]: b["targets"]
                        for b in target_admin.achievement_distribution(self.only)}

        # Only the healthy 85% row is bucketed.
        self.assertEqual(sum(distribution.values()), 1)
        self.assertEqual(distribution["75-<100%"], 1)

    def test_it_does_not_contribute_to_incentive_exposure(self):
        exposure = target_admin.incentive_exposure(self.only)

        self.assertEqual(len(exposure), 1)
        self.assertEqual(exposure[0]["targets"], 1,
                         "an unreliable target was counted in the exposure")

    def test_it_does_not_contribute_to_monetary_totals(self):
        totals = target_admin.amount_totals(self.only)

        self.assertEqual(len(totals), 1)
        self.assertEqual(totals[0]["targets"], 1)
        self.assertEqual(flt(totals[0]["target_amount"]), 1000.0,
                         "a stale target amount reached the monetary total")
        self.assertEqual(flt(totals[0]["achieved_amount"]), 850.0,
                         "a stale achieved amount reached the monetary total")

    def test_it_does_not_affect_rule_performance(self):
        rows = target_admin.rule_performance(self.only)

        self.assertEqual(len(rows), 1)
        row = rows[0]

        self.assertEqual(row["targets"], 1, "rule performance counted a stale target")
        self.assertEqual(row["customers"], 1)
        self.assertAlmostEqual(row["average_achievement"], 85.0, places=6)
        self.assertEqual(flt(row["final_incentive"]), 0.0)

    def test_it_does_not_appear_in_the_default_row_list(self):
        rows = target_admin.target_rows(self.only)

        self.assertNotIn(self.broken, [r["customer_target"] for r in rows])

    # ------------------------------------------------- included but flagged

    def test_the_customer_report_still_shows_it(self):
        """The administrator is the person who has to act on it."""

        _c, rows, _m, _ch, summary = self.customer_report(
            customer=self.broken_customer, **self.only)

        self.assertEqual(len(rows), 1)
        row = rows[0]

        self.assertEqual(row["customer_target"], self.broken)
        self.assertEqual(row["calculation_state"], "Needs Attention")
        # The stored figures are retained, but unmistakably marked.
        self.assertEqual(flt(row["achievement_percentage"]), 90.0)

        self.assertEqual(self.card(summary, "Total Targets")["value"], 1)
        self.assertEqual(self.card(summary, "Calculation Issues")["value"], 1)

    def test_a_healthy_row_reads_ok(self):
        _c, rows, _m, _ch, summary = self.customer_report(
            customer=self.healthy_customer, **self.only)

        self.assertEqual(rows[0]["calculation_state"], "OK")
        self.assertIsNone(self.card(summary, "Calculation Issues"),
                          "a clean selection should show no issue card")

    def test_the_customer_report_publishes_a_calculation_state_column(self):
        columns = self.customer_report(customer=self.broken_customer)[0]
        fieldnames = [c["fieldname"] for c in columns]

        self.assertIn("calculation_state", fieldnames)
        # Beside the achievement, so the percentage cannot be read without it.
        self.assertLess(fieldnames.index("calculation_state"),
                        fieldnames.index("target_amount"))

    def test_ending_soon_includes_it_flagged(self):
        """The chosen behaviour: the DEADLINE is authoritative even when the
        achievement is not, so hiding an approaching deadline would be worse."""

        rows = target_admin.ending_soon(self.only, days=10)
        by_target = {r["customer_target"]: r for r in rows}

        self.assertIn(self.broken, by_target, "an authoritative deadline was hidden")
        self.assertEqual(by_target[self.broken]["calculation_state"],
                         "Needs Attention")

        healthy = next(r for r in rows if r["customer"] == self.healthy_customer)
        self.assertEqual(healthy["calculation_state"], "OK")

    def test_ending_soon_puts_the_trustworthy_rows_first(self):
        rows = target_admin.ending_soon(self.only, days=10)
        states = [r["calculation_state"] for r in rows]

        self.assertEqual(states, sorted(states, key=lambda s: s != "OK"),
                         "a flagged row outranked a trustworthy one for the same date")

    def test_the_dashboard_reports_the_issue_count(self):
        _c, _r, message, _ch, summary = self.dashboard(**self.only)

        card = self.card(summary, "Calculation Issues")
        self.assertIsNotNone(card)
        self.assertEqual(card["value"], 1)

        self.assertEqual(self.card(summary, "Total Targets")["value"], 2)
        self.assertIn("excluded", message,
                      "management is not told that records were excluded")

    # ------------------------------------------------- never leaked

    def test_the_internal_error_text_is_never_published(self):
        for payload in (self.customer_report(customer=self.broken_customer,
                                             **self.only),
                        self.dashboard(**self.only),
                        self.dashboard(view="Ending Soon", **self.only)):
            blob = frappe.as_json(payload)

            for leak in ("calculation_error", "UOM conversion failed", "ITEM-X",
                         "Traceback", "internal failure"):
                self.assertNotIn(leak, blob, f"{leak} reached an admin view")

    def test_no_row_carries_the_error_column_at_all(self):
        rows = target_admin.target_rows(self.only, valid_only=False)

        self.assertEqual(len(rows), 2)

        for row in rows:
            self.assertNotIn("calculation_error", row)
            self.assertIn("calculation_state", row)


# =========================================================================
# SECURITY
# =========================================================================

class AdminSecurityCase(AdminBase):

    def test_both_reports_are_restricted_to_system_manager(self):
        for name in ("YOB Customer Target Report", "YOB Target Management Dashboard"):
            roles = frappe.get_all("Has Role", filters={"parent": name,
                                                        "parenttype": "Report"},
                                   pluck="role")

            self.assertEqual(set(roles), {"System Manager"},
                             f"{name} is exposed to {sorted(roles)}")

    def test_no_storefront_role_can_reach_the_reports(self):
        """The buyer role must not appear on an administrative view."""

        for name in ("YOB Customer Target Report", "YOB Target Management Dashboard"):
            roles = frappe.get_all("Has Role", filters={"parent": name,
                                                        "parenttype": "Report"},
                                   pluck="role")

            for storefront in ("YOB Storefront Buyer", "Customer", "All",
                               "Guest", "YOB Payment Processor"):
                self.assertNotIn(storefront, roles)

    def test_the_reports_reference_the_calculated_doctype(self):
        """`ref_doctype` is what makes Frappe apply that DocType's permissions
        on top of the report's own roles."""

        for name in ("YOB Customer Target Report", "YOB Target Management Dashboard"):
            self.assertEqual(
                frappe.db.get_value("Report", name, "ref_doctype"),
                "YOB Customer Target")

    def test_the_admin_modules_expose_no_whitelisted_method(self):
        """No new public surface. A Desk report runs through Frappe's own
        `query_report` entry point, which enforces the roles above; an extra
        whitelisted helper would be a second door with no such check."""

        import importlib

        for module_name in (
                "yob_storefront.services.target_admin",
                "yob_storefront.yob_storefront.report.yob_customer_target_report"
                ".yob_customer_target_report",
                "yob_storefront.yob_storefront.report"
                ".yob_target_management_dashboard.yob_target_management_dashboard"):
            module = importlib.import_module(module_name)

            for name, obj in vars(module).items():
                if not callable(obj) or getattr(obj, "__module__", None) != module_name:
                    continue
                self.assertNotIn(obj, frappe.whitelisted,
                                 f"{module_name}.{name} is whitelisted")

    def test_a_filter_fieldname_can_never_come_from_the_caller(self):
        """Report filters are browser input even inside Desk.

        An unknown key is DROPPED rather than interpolated, so no filter name can
        reach SQL.
        """

        sql, values = target_admin.build_conditions({
            "customer": "CUST-1",
            "1=1 OR status": "x",
            "ct.company; DROP TABLE tabItem": "y",
            "status": "Achieved",
        })

        self.assertIn("ct.customer = %(customer)s", sql)
        self.assertIn("ct.status = %(status)s", sql)
        self.assertNotIn("DROP", sql)
        self.assertNotIn("1=1 OR", sql)
        self.assertEqual(set(values), {"customer", "status"})

    def test_the_service_does_not_switch_users(self):
        source = executable_source(target_admin)

        self.assertNotIn("set_user", source)
        self.assertNotIn("ignore_permissions", source)


if __name__ == "__main__":
    unittest.main()
