# Copyright (c) 2026, YOB and Shayona
"""The Target Progress block: whose targets are shown, and when.

THE THREE DATE CONCEPTS THIS FILE KEEPS APART
---------------------------------------------
    Target Period    which sales count         -- Phase 30A
    Report Window    when recalculation runs   -- Phase 30A
    Show From/Until  when a block displays it  -- Phase 30B, here

`VisibilityWindowCase` is the one that matters: a target whose sales period
closed in June is still DISPLAYED in July if the block says so, and disappears
the day after `show_until` without anything about the target changing.

SECURITY
--------
`SecurityCase` proves the shape of the thing rather than one call: the projection
takes a `customer_doc` it is GIVEN, no function on the path accepts a customer
parameter, and the block is reachable only through `cms.get_page` /
`cms.get_route_content`, which resolve the buyer from `auth_context`. A browser
has nowhere to put another party's name.
"""

import inspect
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_days, flt, getdate, today

from yob_storefront.services import target_progress
from yob_storefront.services import target_service

SEED_ITEM = "YOB-BOLT-M10"
SEED_CUSTOMER = "YOB Demo Buyer"

#: Inside the test site's only active Fiscal Year (2026-04-01 .. 2027-03-31).
PERIOD_START = "2026-06-01"
PERIOD_END = "2026-06-25"


def _seeded():
    return bool(frappe.db.exists("Item", SEED_ITEM)
                and frappe.db.exists("Customer", SEED_CUSTOMER))


class BlockBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data on the test site")

    def setUp(self):
        frappe.set_user("Administrator")

        from yob_storefront.api import cms as cms_api

        self.cms = cms_api

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

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()
        self.assertEqual(self.commits, [], "a commit escaped the test rollback")

    # ------------------------------------------------------------- fixtures

    def make_customer(self, name):
        return frappe.get_doc({
            "doctype": "Customer", "customer_name": name, "customer_type": "Company",
            "customer_group": self.baseline_group,
            "territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
        }).insert(ignore_permissions=True).name

    def make_item(self, code):
        return frappe.get_doc({
            "doctype": "Item", "item_code": code, "item_name": code,
            "item_group": self.item_group, "stock_uom": self.stock_uom,
            "is_stock_item": 0, "is_sales_item": 1, "gst_hsn_code": self.hsn,
        }).insert(ignore_permissions=True).name

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

    def make_rule(self, name, *, period, customers, visible=1, target_amount=1000,
                  target_type="Amount", target_quantity=0, target_uom=None,
                  incentive_type="Fixed Amount", incentive_amount=500,
                  incentive_percentage=0, slabs=None, description=None,
                  report_start="2026-06-01", report_end="2026-12-31"):
        doc = frappe.get_doc({
            "doctype": "YOB Target Rule", "rule_name": name,
            "company": self.company, "target_period": period,
            "report_start_date": report_start, "report_end_date": report_end,
            "enabled": 1, "customer_visible": visible, "description": description,
            "target_scope": "Total Sales Order",
            "target_type": target_type, "target_amount": target_amount,
            "target_quantity": target_quantity, "target_uom": target_uom,
            "incentive_type": incentive_type, "incentive_amount": incentive_amount,
            "incentive_percentage": incentive_percentage,
            "conditions": [{"condition_type": "Customer", "condition_customer": c}
                           for c in customers],
            "incentive_slabs": slabs or [],
        }).insert(ignore_permissions=True)
        doc.submit()
        return doc

    def make_sales_order(self, customer, *, items, date=PERIOD_START):
        warehouse = frappe.db.get_value(
            "Warehouse", {"company": self.company, "is_group": 0}, "name")
        doc = frappe.get_doc({
            "doctype": "Sales Order", "customer": customer, "company": self.company,
            "transaction_date": date, "delivery_date": date, "currency": self.currency,
            "items": [{"item_code": r["item_code"], "qty": r["qty"],
                       "uom": r.get("uom") or self.stock_uom,
                       "rate": r.get("rate", 100), "delivery_date": date,
                       "warehouse": warehouse} for r in items],
        })
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        doc.submit()
        return doc

    def make_block(self, name, rows):
        """A Target Progress block. `rows` is [(rule, show_from, show_until)]."""

        return frappe.get_doc({
            "doctype": "YOB Storefront Block", "block_name": name,
            "block_type": "Target Progress", "enabled": 1,
            "target_rows": [
                {"target_rule": r, "show_from": f, "show_until": u} for r, f, u in rows
            ],
        }).insert(ignore_permissions=True)

    # ------------------------------------------------------------- helpers

    def project(self, block, customer_name, on=None):
        """The block, resolved for one customer, optionally on a given date."""

        customer = frappe.get_doc("Customer", customer_name)
        frappe.clear_cache()

        if on is None:
            return target_progress.project_target_progress(block, customer)

        with patch.object(target_progress, "today", return_value=on):
            return target_progress.project_target_progress(block, customer)

    def titles(self, payload):
        return [t["title"] for t in payload["targets"]]


# =========================================================================
# THE BLOCK CONFIGURATION
# =========================================================================

class BlockConfigurationCase(BlockBase):

    def setUp(self):
        super().setUp()
        self.period = self.make_period("_T30B-CFG")
        self.customer = self.make_customer("_T30B Cfg Buyer")
        self.rule = self.make_rule("_T30B-CFG-RULE", period=self.period,
                                   customers=[self.customer])

    def test_target_progress_is_a_registered_block_type(self):
        meta = frappe.get_meta("YOB Storefront Block")
        options = meta.get_field("block_type").options or ""

        self.assertIn("Target Progress", options)

        from yob_storefront.services.content_service import BLOCK_TYPES, PROJECTORS

        self.assertEqual(BLOCK_TYPES["Target Progress"], "target_progress")
        self.assertIn("target_progress", PROJECTORS)

    def test_a_block_needs_at_least_one_target_rule(self):
        with self.assertRaises(frappe.ValidationError):
            self.make_block("_T30B Empty", [])

    def test_both_display_dates_are_required(self):
        for rows in ([(self.rule.name, None, "2026-07-01")],
                     [(self.rule.name, "2026-06-01", None)]):
            with self.assertRaises(frappe.ValidationError):
                self.make_block(f"_T30B Missing {frappe.generate_hash(length=5)}", rows)

    def test_show_from_cannot_be_after_show_until(self):
        with self.assertRaises(frappe.ValidationError):
            self.make_block("_T30B Reversed",
                            [(self.rule.name, "2026-07-10", "2026-07-01")])

    def test_a_single_day_window_is_valid(self):
        block = self.make_block("_T30B OneDay",
                                [(self.rule.name, "2026-07-01", "2026-07-01")])
        self.assertEqual(len(block.target_rows), 1)

    def test_the_same_rule_cannot_be_listed_twice(self):
        with self.assertRaises(frappe.ValidationError):
            self.make_block("_T30B Dup", [
                (self.rule.name, "2026-06-01", "2026-06-30"),
                (self.rule.name, "2026-07-01", "2026-07-30"),
            ])

    def test_a_rule_that_is_not_customer_visible_is_refused(self):
        hidden = self.make_rule("_T30B-HIDDEN", period=self.period,
                                customers=[self.customer], visible=0)

        with self.assertRaises(frappe.ValidationError):
            self.make_block("_T30B Hidden", [(hidden.name, "2026-06-01", "2026-12-31")])

    def test_a_nonexistent_rule_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self.make_block("_T30B Ghost", [("_T30B-NO-SUCH-RULE", "2026-06-01",
                                             "2026-12-31")])

    def test_configuring_a_block_does_not_touch_the_target_rule(self):
        before = frappe.db.get_value(
            "YOB Target Rule", self.rule.name,
            ["modified", "report_start_date", "report_end_date", "customer_visible"],
            as_dict=True)

        self.make_block("_T30B NoWrite", [(self.rule.name, "2026-06-01", "2026-12-31")])

        after = frappe.db.get_value(
            "YOB Target Rule", self.rule.name,
            ["modified", "report_start_date", "report_end_date", "customer_visible"],
            as_dict=True)

        self.assertEqual(dict(before), dict(after),
                         "the block wrote to the Target Rule")

    def test_no_display_dates_leaked_onto_the_target_rule(self):
        """Show From / Show Until belong to the BLOCK. Three date concepts stay
        three: a rule exposed by two blocks has two display windows and one
        Target Period."""

        meta = frappe.get_meta("YOB Target Rule")

        for forbidden in ("show_from", "show_until", "display_from", "display_until",
                          "visible_from", "visible_until"):
            self.assertIsNone(meta.get_field(forbidden))

    def test_switching_block_type_clears_the_target_rows(self):
        """The established convention: a changed type leaves no stale config."""

        block = self.make_block("_T30B Switch",
                                [(self.rule.name, "2026-06-01", "2026-12-31")])

        block.block_type = "Rich Text"
        block.content = "<p>now text</p>"
        block.save(ignore_permissions=True)

        self.assertEqual(list(block.target_rows), [])

    def test_existing_block_types_still_validate(self):
        """Phase 30B must not disturb the five that already shipped."""

        rich = frappe.get_doc({
            "doctype": "YOB Storefront Block", "block_name": "_T30B Rich",
            "block_type": "Rich Text", "enabled": 1, "content": "<p>hi</p>",
        }).insert(ignore_permissions=True)
        self.assertEqual(rich.block_type, "Rich Text")

        banner = frappe.get_doc({
            "doctype": "YOB Storefront Block", "block_name": "_T30B Banner",
            "block_type": "Image Banner", "enabled": 1,
            "desktop_image": "/files/b.png",
        }).insert(ignore_permissions=True)
        self.assertEqual(banner.block_type, "Image Banner")

        # And a Target Progress block does not acquire their fields.
        block = self.make_block("_T30B Clean",
                                [(self.rule.name, "2026-06-01", "2026-12-31")])
        self.assertFalse(block.content)
        self.assertFalse(block.desktop_image)
        self.assertFalse(block.storefront_category)


# =========================================================================
# VISIBILITY WINDOW  -- the third date concept
# =========================================================================

class VisibilityWindowCase(BlockBase):

    def setUp(self):
        super().setUp()
        self.customer = self.make_customer("_T30B Window Buyer")
        self.item = self.make_item("_T30B-WIN-ITEM")
        self.period = self.make_period("_T30B-WIN")

        self.make_sales_order(self.customer, date="2026-06-10",
                              items=[{"item_code": self.item, "qty": 5, "rate": 100}])

        self.rule = self.make_rule("_T30B-WIN-RULE", period=self.period,
                                   customers=[self.customer], target_amount=1000)

        target_service.recalculate_due_targets("2026-06-25")

        self.block = self.make_block("_T30B Window",
                                     [(self.rule.name, "2026-07-01", "2026-07-15")])

    def test_before_show_from_it_is_hidden(self):
        self.assertEqual(self.titles(self.project(self.block, self.customer,
                                                  on="2026-06-30")), [])

    def test_exactly_show_from_it_is_visible(self):
        self.assertEqual(self.titles(self.project(self.block, self.customer,
                                                  on="2026-07-01")),
                         [self.rule.name])

    def test_between_the_dates_it_is_visible(self):
        self.assertEqual(self.titles(self.project(self.block, self.customer,
                                                  on="2026-07-08")),
                         [self.rule.name])

    def test_exactly_show_until_it_is_visible(self):
        self.assertEqual(self.titles(self.project(self.block, self.customer,
                                                  on="2026-07-15")),
                         [self.rule.name])

    def test_after_show_until_it_is_hidden(self):
        self.assertEqual(self.titles(self.project(self.block, self.customer,
                                                  on="2026-07-16")), [])

    def test_a_closed_sales_period_is_still_displayed(self):
        """THE business case. Sales ended 25 June; the card shows on 8 July.

        The display window is a third concept: it neither extends what was
        earned nor reopens recalculation.
        """

        payload = self.project(self.block, self.customer, on="2026-07-08")

        self.assertEqual(len(payload["targets"]), 1)
        card = payload["targets"][0]

        self.assertEqual(getdate(card["target_period_end"]), getdate("2026-06-25"))
        # Only the 10 June order counted -- the display date changed nothing.
        self.assertEqual(flt(card["achieved_amount"]), 500.0)

    def test_two_rules_can_overlap_in_one_block(self):
        """January and February both visible during the overlap, then one drops."""

        second_period = self.make_period("_T30B-WIN2", "2026-07-01", "2026-07-25")
        self.make_sales_order(self.customer, date="2026-07-10",
                              items=[{"item_code": self.item, "qty": 2, "rate": 100}])
        second = self.make_rule("_T30B-WIN-RULE2", period=second_period,
                                customers=[self.customer], target_amount=1000)
        target_service.recalculate_due_targets("2026-07-25")

        block = self.make_block("_T30B Overlap", [
            (self.rule.name, "2026-06-01", "2026-07-15"),
            (second.name, "2026-07-01", "2026-08-15"),
        ])

        both = self.titles(self.project(block, self.customer, on="2026-07-10"))
        self.assertEqual(both, [self.rule.name, second.name])

        after = self.titles(self.project(block, self.customer, on="2026-07-20"))
        self.assertEqual(after, [second.name], "the closed window still displayed")

    def test_row_order_is_the_display_order(self):
        second_period = self.make_period("_T30B-ORD2", "2026-06-01", "2026-06-25")
        second = self.make_rule("_T30B-ORD-RULE2", period=second_period,
                                customers=[self.customer], target_amount=1000)
        target_service.recalculate_due_targets("2026-06-25")

        window = ("2026-07-01", "2026-07-31")

        forward = self.make_block("_T30B Fwd", [
            (self.rule.name, *window), (second.name, *window)])
        reverse = self.make_block("_T30B Rev", [
            (second.name, *window), (self.rule.name, *window)])

        self.assertEqual(self.titles(self.project(forward, self.customer, on="2026-07-10")),
                         [self.rule.name, second.name])
        self.assertEqual(self.titles(self.project(reverse, self.customer, on="2026-07-10")),
                         [second.name, self.rule.name])

    def test_only_configured_rules_appear(self):
        """A second target the customer genuinely has is NOT shown by this block."""

        other_period = self.make_period("_T30B-UNCONF", "2026-06-01", "2026-06-25")
        unconfigured = self.make_rule("_T30B-UNCONF-RULE", period=other_period,
                                      customers=[self.customer], target_amount=1000)
        target_service.recalculate_due_targets("2026-06-25")

        self.assertIsNotNone(frappe.db.get_value(
            "YOB Customer Target",
            {"customer": self.customer, "target_rule": unconfigured.name}, "name"),
            "the fixture did not produce the unconfigured target")

        titles = self.titles(self.project(self.block, self.customer, on="2026-07-08"))

        self.assertEqual(titles, [self.rule.name])
        self.assertNotIn(unconfigured.name, titles)

    def test_a_rule_unpublished_after_configuration_disappears(self):
        """Re-checked at projection, not merely at save."""

        frappe.db.set_value("YOB Target Rule", self.rule.name, "customer_visible", 0)

        self.assertEqual(self.titles(self.project(self.block, self.customer,
                                                  on="2026-07-08")), [])


# =========================================================================
# SECURITY
# =========================================================================

class SecurityCase(BlockBase):

    def setUp(self):
        super().setUp()
        self.item = self.make_item("_T30B-SEC-ITEM")
        self.period = self.make_period("_T30B-SEC")

        self.alice = self.make_customer("_T30B Alice")
        self.bob = self.make_customer("_T30B Bob")

        self.make_sales_order(self.alice, date="2026-06-10",
                              items=[{"item_code": self.item, "qty": 5, "rate": 100}])
        self.make_sales_order(self.bob, date="2026-06-10",
                              items=[{"item_code": self.item, "qty": 90, "rate": 100}])

        # One rule, both customers eligible -- so each has a real target and the
        # only thing separating them is identity.
        self.rule = self.make_rule("_T30B-SEC-RULE", period=self.period,
                                   customers=[self.alice, self.bob],
                                   target_amount=1000)
        target_service.recalculate_due_targets("2026-06-25")

        self.block = self.make_block("_T30B Security",
                                     [(self.rule.name, "2026-06-01", "2026-12-31")])

    def test_each_customer_sees_only_their_own_figures(self):
        alice = self.project(self.block, self.alice, on="2026-07-01")["targets"][0]
        bob = self.project(self.block, self.bob, on="2026-07-01")["targets"][0]

        self.assertEqual(flt(alice["achieved_amount"]), 500.0)
        self.assertEqual(flt(bob["achieved_amount"]), 9000.0)
        self.assertNotEqual(alice["customer_target"], bob["customer_target"])

    def test_no_function_on_the_path_accepts_a_customer_parameter(self):
        """Structural: there is nowhere to put another party's name.

        A browser cannot request `customer=CUST-X` because no signature from the
        endpoint down to the projection has such an argument.
        """

        # The PUBLIC boundary. Internal helpers must of course receive the
        # resolved identity -- what matters is that nothing on the way in can be
        # told which identity to use.
        entry = list(inspect.signature(target_progress.project_target_progress)
                     .parameters)

        self.assertEqual(entry, ["block", "customer_doc"])

        for forbidden in ("customer", "customer_name", "customer_id", "party"):
            self.assertNotIn(forbidden, entry)

        # The two endpoints that can reach a block take a slug / route key only.
        for endpoint in (self.cms.get_page, self.cms.get_route_content):
            params = list(inspect.signature(inspect.unwrap(endpoint)).parameters)
            for forbidden in ("customer", "customer_name", "customer_id", "party",
                              "target_rule", "block"):
                self.assertNotIn(forbidden, params,
                                 f"{endpoint.__name__} accepts {forbidden}")

    def test_the_projection_never_reads_the_request(self):
        """The resolved customer arrives as an argument, never from the wire.

        This is the other half of the signature check: a function that took no
        customer parameter could still reach into `form_dict` for one.
        """

        import pathlib as _pathlib

        source = _pathlib.Path(target_progress.__file__).read_text()

        for forbidden in ("form_dict", "frappe.request", "frappe.local.request",
                          "get_request_header"):
            self.assertNotIn(forbidden, source,
                             f"the projection reads {forbidden}")

    def test_the_target_module_exposes_no_whitelisted_method(self):
        """No second endpoint. A block is the only way in."""

        for name, obj in vars(target_progress).items():
            if not callable(obj) or getattr(obj, "__module__", None) != \
                    "yob_storefront.services.target_progress":
                continue
            self.assertNotIn(obj, frappe.whitelisted,
                             f"target_progress.{name} is whitelisted")

    def test_the_customer_comes_from_auth_context_through_the_endpoint(self):
        """End to end: `cms.get_page` resolves the buyer, never the request."""

        page = frappe.get_doc({
            "doctype": "YOB Storefront Page", "slug": "t30b-sec-page",
            "title": "Targets", "enabled": 1,
            "blocks": [{"block": self.block.name}],
        }).insert(ignore_permissions=True)

        for customer, expected in ((self.alice, 500.0), (self.bob, 9000.0)):
            doc = frappe.get_doc("Customer", customer)
            frappe.clear_cache()
            with patch.object(self.cms, "get_storefront_customer", return_value=doc):
                response = inspect.unwrap(self.cms.get_page)(
                    slug=page.slug, auth_context={})

            self.assertNotIn("errors", response, response)
            block = response["data"]["blocks"][0]
            self.assertEqual(block["type"], "target_progress")
            self.assertEqual(flt(block["targets"][0]["achieved_amount"]), expected)

    def test_an_unconfigured_rule_cannot_be_reached_through_another_block(self):
        """A hostile block identifier exposes only what THAT block configured."""

        secret_period = self.make_period("_T30B-SEC-OTHER", "2026-06-01", "2026-06-25")
        secret = self.make_rule("_T30B-SEC-SECRET", period=secret_period,
                                customers=[self.alice], target_amount=1000)
        target_service.recalculate_due_targets("2026-06-25")

        titles = self.titles(self.project(self.block, self.alice, on="2026-07-01"))

        self.assertEqual(titles, [self.rule.name])
        self.assertNotIn(secret.name, titles)

    def test_a_customer_with_no_target_gets_nothing_fabricated(self):
        carol = self.make_customer("_T30B Carol")

        payload = self.project(self.block, carol, on="2026-07-01")

        self.assertEqual(payload["targets"], [],
                         "a card was invented for an ineligible customer")

    def test_no_reason_for_exclusion_is_leaked(self):
        """Absence is the whole answer: no 'not eligible' explanation."""

        carol = self.make_customer("_T30B Carol Two")
        blob = frappe.as_json(self.project(self.block, carol, on="2026-07-01"))

        for leak in ("not eligible", "Dealers", "condition", "customer_group",
                     self.alice, self.bob):
            self.assertNotIn(leak, blob)

    def test_the_projection_never_switches_user(self):
        import pathlib

        source = pathlib.Path(target_progress.__file__).read_text()

        self.assertNotIn("set_user", source)
        self.assertNotIn("frappe.flags.ignore_permissions", source)


# =========================================================================
# DATA CORRECTNESS
# =========================================================================

class CardDataCase(BlockBase):

    def setUp(self):
        super().setUp()
        self.customer = self.make_customer("_T30B Data Buyer")
        self.item = self.make_item("_T30B-DATA-ITEM")
        self.period = self.make_period("_T30B-DATA")

    def card(self, rule, *, on="2026-07-01"):
        block = self.make_block(f"_T30B Card {frappe.generate_hash(length=5)}",
                                [(rule.name, "2026-06-01", "2026-12-31")])
        payload = self.project(block, self.customer, on=on)
        self.assertEqual(len(payload["targets"]), 1, payload)
        return payload["targets"][0]

    def test_an_amount_target_card(self):
        self.make_sales_order(self.customer, date="2026-06-10",
                              items=[{"item_code": self.item, "qty": 6, "rate": 100}])
        rule = self.make_rule("_T30B-AMT", period=self.period,
                              customers=[self.customer], target_amount=1000,
                              description="Mid-year push")
        target_service.recalculate_due_targets("2026-06-25")

        card = self.card(rule)

        self.assertEqual(card["target_type"], "Amount")
        self.assertEqual(card["currency"], self.currency)
        self.assertIsNone(card["uom"])
        self.assertEqual(flt(card["target_amount"]), 1000.0)
        self.assertEqual(flt(card["achieved_amount"]), 600.0)
        self.assertEqual(flt(card["achievement_percentage"]), 60.0)
        self.assertEqual(flt(card["remaining_amount"]), 400.0)
        self.assertIsNone(card["target_quantity"])
        self.assertIsNone(card["remaining_quantity"])
        # Calculated ON the period's last day, so the period was still open:
        # Phase 30A reports In Progress, and only a run AFTER 25 June expires it.
        self.assertEqual(card["status"], "In Progress")
        self.assertEqual(card["description"], "Mid-year push")
        self.assertEqual(card["target_rule"], rule.name)
        self.assertIsNotNone(card["last_calculated_on"])
        self.assertEqual(getdate(card["target_period_start"]), getdate(PERIOD_START))
        self.assertEqual(getdate(card["target_period_end"]), getdate(PERIOD_END))

    def test_a_quantity_target_card_carries_the_uom(self):
        box = self.make_uom("_T30B Box")
        item = frappe.get_doc({
            "doctype": "Item", "item_code": "_T30B-QTY-ITEM",
            "item_name": "_T30B-QTY-ITEM", "item_group": self.item_group,
            "stock_uom": self.stock_uom, "is_stock_item": 0, "is_sales_item": 1,
            "gst_hsn_code": self.hsn,
            "uoms": [{"uom": self.stock_uom, "conversion_factor": 1},
                     {"uom": box, "conversion_factor": 10}],
        }).insert(ignore_permissions=True).name

        self.make_sales_order(self.customer, date="2026-06-10",
                              items=[{"item_code": item, "qty": 3, "uom": box,
                                      "rate": 100}])

        rule = self.make_rule("_T30B-QTY", period=self.period,
                              customers=[self.customer], target_type="Quantity",
                              target_quantity=50, target_uom=self.stock_uom,
                              target_amount=0)
        target_service.recalculate_due_targets("2026-06-25")

        card = self.card(rule)

        self.assertEqual(card["target_type"], "Quantity")
        self.assertEqual(card["uom"], self.stock_uom)
        self.assertEqual(flt(card["target_quantity"]), 50.0)
        # 3 Box x 10 = 30, converted by Phase 30A, not here.
        self.assertEqual(flt(card["achieved_quantity"]), 30.0)
        self.assertEqual(flt(card["remaining_quantity"]), 20.0)
        self.assertIsNone(card["target_amount"])
        self.assertIsNone(card["remaining_amount"])

    def test_achievement_above_one_hundred_is_not_capped(self):
        """125% publishes 125. A progress bar may cap; the backend must not."""

        self.make_sales_order(self.customer, date="2026-06-10",
                              items=[{"item_code": self.item, "qty": 25, "rate": 100}])
        rule = self.make_rule("_T30B-OVER", period=self.period,
                              customers=[self.customer], target_amount=2000)
        target_service.recalculate_due_targets("2026-06-25")

        card = self.card(rule)

        self.assertEqual(flt(card["achievement_percentage"]), 125.0)
        self.assertEqual(flt(card["remaining_amount"]), 0.0,
                         "remaining went negative instead of flooring at zero")
        self.assertEqual(card["status"], "Achieved")

    def test_statuses_are_surfaced(self):
        for label, target_amount, expected in (("INPROG", 10_000, "In Progress"),
                                               ("ACHIEVED", 100, "Achieved")):
            self.make_sales_order(self.customer, date=today(),
                                  items=[{"item_code": self.item, "qty": 5, "rate": 100}])
            live = self.make_period(f"_T30B-ST-{label}", add_days(today(), -3),
                                    add_days(today(), 3))
            rule = self.make_rule(f"_T30B-ST-{label}", period=live,
                                  customers=[self.customer],
                                  target_amount=target_amount,
                                  report_start=add_days(today(), -3),
                                  report_end=add_days(today(), 30))
            target_service.recalculate_due_targets(today())

            block = self.make_block(f"_T30B ST {label}",
                                    [(rule.name, add_days(today(), -3),
                                      add_days(today(), 30))])
            payload = self.project(block, self.customer)
            self.assertEqual(payload["targets"][0]["status"], expected)

    def test_a_completed_target_is_surfaced(self):
        self.make_sales_order(self.customer, date="2026-06-10",
                              items=[{"item_code": self.item, "qty": 20, "rate": 100}])
        rule = self.make_rule("_T30B-DONE", period=self.period,
                              customers=[self.customer], target_amount=1000)
        target_service.recalculate_due_targets("2026-06-25")

        name = frappe.db.get_value(
            "YOB Customer Target",
            {"customer": self.customer, "target_rule": rule.name}, "name")
        doc = frappe.get_doc("YOB Customer Target", name)
        doc.status = "Completed"
        doc.save(ignore_permissions=True)

        self.assertEqual(self.card(rule)["status"], "Completed")

    def test_incentive_values_agree_with_phase_30a(self):
        self.make_sales_order(self.customer, date="2026-06-10",
                              items=[{"item_code": self.item, "qty": 12, "rate": 100}])
        rule = self.make_rule("_T30B-INC", period=self.period,
                              customers=[self.customer], target_amount=1000,
                              incentive_type="Fixed Amount", incentive_amount=750)
        target_service.recalculate_due_targets("2026-06-25")

        card = self.card(rule)
        stored = frappe.db.get_value(
            "YOB Customer Target",
            {"customer": self.customer, "target_rule": rule.name},
            ["eligible_incentive", "final_incentive", "incentive_type", "current_slab"],
            as_dict=True)

        self.assertEqual(flt(card["eligible_incentive"]), flt(stored.eligible_incentive))
        self.assertEqual(flt(card["final_incentive"]), flt(stored.final_incentive))
        self.assertEqual(card["incentive_type"], stored.incentive_type)
        self.assertEqual(flt(card["final_incentive"]), 750.0)

    def test_the_current_slab_is_surfaced(self):
        self.make_sales_order(self.customer, date="2026-06-10",
                              items=[{"item_code": self.item, "qty": 6, "rate": 100}])
        rule = self.make_rule(
            "_T30B-SLAB", period=self.period, customers=[self.customer],
            target_amount=1000, incentive_type="Slab Fixed Amount",
            incentive_amount=0,
            slabs=[{"from_achievement": 50, "to_achievement": 74.99,
                    "incentive_amount": 200},
                   {"from_achievement": 75, "to_achievement": 100,
                    "incentive_amount": 500}])
        target_service.recalculate_due_targets("2026-06-25")

        card = self.card(rule)

        self.assertEqual(card["current_slab"], "50% - 74.99%")
        self.assertEqual(flt(card["final_incentive"]), 200.0)

    def test_the_next_milestone_is_derived(self):
        """Angular never interprets a slab table."""

        self.make_sales_order(self.customer, date="2026-06-10",
                              items=[{"item_code": self.item, "qty": 6, "rate": 100}])
        rule = self.make_rule(
            "_T30B-NEXT", period=self.period, customers=[self.customer],
            target_amount=1000, incentive_type="Slab Fixed Amount",
            incentive_amount=0,
            slabs=[{"from_achievement": 50, "to_achievement": 74.99,
                    "incentive_amount": 200},
                   {"from_achievement": 80, "to_achievement": 100,
                    "incentive_amount": 500}])
        target_service.recalculate_due_targets("2026-06-25")

        card = self.card(rule)
        nxt = card["next_milestone"]

        self.assertIsNotNone(nxt)
        self.assertEqual(flt(nxt["achievement_percentage"]), 80.0)
        # 80% of 1000 is 800; achieved 600; so 200 more.
        self.assertEqual(flt(nxt["remaining_amount"]), 200.0)
        self.assertEqual(flt(nxt["incentive_amount"]), 500.0)
        self.assertIsNone(nxt["remaining_quantity"])

    def test_no_next_milestone_above_the_highest_slab(self):
        self.make_sales_order(self.customer, date="2026-06-10",
                              items=[{"item_code": self.item, "qty": 20, "rate": 100}])
        rule = self.make_rule(
            "_T30B-TOPSLAB", period=self.period, customers=[self.customer],
            target_amount=1000, incentive_type="Slab Fixed Amount",
            incentive_amount=0,
            slabs=[{"from_achievement": 50, "to_achievement": 100,
                    "incentive_amount": 500}])
        target_service.recalculate_due_targets("2026-06-25")

        self.assertIsNone(self.card(rule)["next_milestone"],
                          "a milestone beyond the merchant's top slab was invented")

    def test_a_non_slab_incentive_has_no_next_milestone(self):
        self.make_sales_order(self.customer, date="2026-06-10",
                              items=[{"item_code": self.item, "qty": 6, "rate": 100}])
        rule = self.make_rule("_T30B-NOSLAB", period=self.period,
                              customers=[self.customer], target_amount=1000)
        target_service.recalculate_due_targets("2026-06-25")

        self.assertIsNone(self.card(rule)["next_milestone"])

    def test_no_admin_only_field_is_published(self):
        self.make_sales_order(self.customer, date="2026-06-10",
                              items=[{"item_code": self.item, "qty": 6, "rate": 100}])
        rule = self.make_rule("_T30B-SAFE", period=self.period,
                              customers=[self.customer], target_amount=1000)
        target_service.recalculate_due_targets("2026-06-25")

        card = self.card(rule)

        for forbidden in ("calculation_error", "approval_remarks", "approved_by",
                          "approved_on", "customer", "company", "reward_status",
                          "report_start_date", "report_end_date"):
            self.assertNotIn(forbidden, card, f"{forbidden} reached the customer")


# =========================================================================
# CALCULATION ERRORS AND RECALCULATION
# =========================================================================

class SafetyCase(BlockBase):

    def setUp(self):
        super().setUp()
        self.customer = self.make_customer("_T30B Safety Buyer")
        self.item = self.make_item("_T30B-SAFE-ITEM")
        self.period = self.make_period("_T30B-SAFE")

        self.make_sales_order(self.customer, date="2026-06-10",
                              items=[{"item_code": self.item, "qty": 6, "rate": 100}])
        self.rule = self.make_rule("_T30B-SAFE-RULE", period=self.period,
                                   customers=[self.customer], target_amount=1000)
        target_service.recalculate_due_targets("2026-06-25")

        self.block = self.make_block("_T30B Safety",
                                     [(self.rule.name, "2026-06-01", "2026-12-31")])

    def test_resolving_a_block_does_not_recalculate(self):
        """Opening a page must never aggregate a period's sales."""

        with patch.object(target_service, "recalculate_due_targets",
                          side_effect=AssertionError("the block recalculated")), \
             patch.object(target_service, "calculate_customer_target",
                          side_effect=AssertionError("the block recalculated")):
            payload = self.project(self.block, self.customer, on="2026-07-01")

        self.assertEqual(len(payload["targets"]), 1)

    def test_resolving_a_block_does_not_modify_the_customer_target(self):
        name = frappe.db.get_value(
            "YOB Customer Target",
            {"customer": self.customer, "target_rule": self.rule.name}, "name")
        before = frappe.db.get_value(
            "YOB Customer Target", name,
            ["modified", "achieved_amount", "last_calculated_on", "status"],
            as_dict=True)

        self.project(self.block, self.customer, on="2026-07-01")
        self.project(self.block, self.customer, on="2026-07-02")

        after = frappe.db.get_value(
            "YOB Customer Target", name,
            ["modified", "achieved_amount", "last_calculated_on", "status"],
            as_dict=True)

        self.assertEqual(dict(before), dict(after), "the projection wrote to the target")

    def test_resolving_a_block_queries_no_sales_order(self):
        seen = []
        real = frappe.db.sql

        def spy(query, *args, **kwargs):
            seen.append(str(query))
            return real(query, *args, **kwargs)

        with patch.object(frappe.db, "sql", side_effect=spy):
            self.project(self.block, self.customer, on="2026-07-01")

        self.assertEqual([q for q in seen if "tabSales Order" in q], [],
                         "the projection read Sales Orders")

    def test_a_failed_calculation_is_withheld_not_shown_as_zero(self):
        """Presenting a broken figure as 0 would misreport real sales."""

        name = frappe.db.get_value(
            "YOB Customer Target",
            {"customer": self.customer, "target_rule": self.rule.name}, "name")
        frappe.db.set_value("YOB Customer Target", name, "calculation_error",
                            "internal UOM conversion failed for ITEM-X")

        payload = self.project(self.block, self.customer, on="2026-07-01")

        self.assertEqual(payload["targets"], [],
                         "a target with no trustworthy figures was shown")

    def test_the_internal_calculation_error_is_never_leaked(self):
        name = frappe.db.get_value(
            "YOB Customer Target",
            {"customer": self.customer, "target_rule": self.rule.name}, "name")
        frappe.db.set_value("YOB Customer Target", name, "calculation_error",
                            "internal UOM conversion failed for ITEM-X")

        blob = frappe.as_json(self.project(self.block, self.customer, on="2026-07-01"))

        for leak in ("calculation_error", "UOM conversion failed", "ITEM-X",
                     "Traceback"):
            self.assertNotIn(leak, blob)


if __name__ == "__main__":
    unittest.main()
