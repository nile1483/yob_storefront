# Copyright (c) 2026, YOB and Shayona
"""`add_to_cart.qty` is a NON-IDEMPOTENT DELTA. Pinned here permanently.

    cart has qty 2  ->  add_to_cart(item, qty=5)  ->  qty 7, on ONE row

The supplied `qty` is ADDED to any existing line for that `item_code`. It does
NOT replace it, and it does NOT append a second row.

WHY THIS FILE EXISTS
--------------------
"Set the quantity to N" is the intuitive reading of an add-to-cart call, and
this contract is the opposite. Nothing else in the suite asserted it, so a
refactor toward SET semantics would have gone green while silently changing
every buyer's order quantity -- a wrong-quantity order, not a visible failure.

The behaviour was queried and corrected during the frontend handoff precisely
because the frontend had assumed SET. These tests exist so that assumption
cannot quietly become true again.

IF YOU ARE HERE BECAUSE THESE TESTS FAIL
----------------------------------------
Do not "fix" them by changing the expected numbers. Either the delta contract
was deliberately replaced -- in which case the Angular quantity stepper, the
published contract in `frontend-api-handoff/`, and every mock built on it all
have to change together -- or a regression has just been caught.

NON-IDEMPOTENCE IS INTENTIONAL, NOT AN OVERSIGHT
------------------------------------------------
Repeating the same request applies the delta again. A retried, double-submitted
or double-clicked call adds twice. Callers must guard the control and must
never auto-retry this endpoint on a network error, because the first attempt may
already have applied. There is deliberately no absolute set-quantity endpoint.

ONE ROW PER ITEM IS ALSO INTENTIONAL
------------------------------------
ERPNext evaluates a Pricing Rule's min_qty/max_qty against the ROW quantity, so
two rows of 5 would silently miss a min_qty=10 rule that one row of 10
satisfies. One row also prices identically to a Desk-entered Sales Order.
"""

import inspect
import unittest
from unittest.mock import patch

import frappe

CUSTOMER = "YOB Demo Buyer"
ITEM = "YOB-BOLT-M10"


def _seeded() -> bool:
    return bool(frappe.db.exists("Customer", CUSTOMER)
                and frappe.db.exists("Item", ITEM))


class CartQuantitySemanticsCase(unittest.TestCase):
    """Delta semantics, proven through the real endpoint."""

    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data on the test site")

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.db.savepoint("cart_qty")
        self.customer = frappe.get_doc("Customer", CUSTOMER)

        from yob_storefront.api import cart as cart_api

        self.cart_api = cart_api
        self._identity = patch.object(cart_api, "get_storefront_customer",
                                      return_value=self.customer)
        self._identity.start()
        self.addCleanup(self._identity.stop)

        # Start from empty so quantities are unambiguous.
        self.call(cart_api.clear_cart)

    def tearDown(self):
        frappe.db.rollback(save_point="cart_qty")
        frappe.clear_cache()

    # ----------------------------------------------------------- helpers

    def call(self, endpoint, **kwargs):
        """Invoke the real endpoint body; only identity resolution is stubbed."""

        return inspect.unwrap(endpoint)(auth_context={}, **kwargs)

    def add(self, qty, item=ITEM):
        response = self.call(self.cart_api.add_to_cart, item_code=item, qty=qty)

        self.assertNotIn("errors", response, f"add_to_cart failed: {response}")

        return response["data"]

    def rows_for(self, cart_data, item=ITEM):
        return [r for r in cart_data["items"] if r["item_code"] == item]

    def quantity_of(self, cart_data, item=ITEM):
        rows = self.rows_for(cart_data, item)

        self.assertEqual(len(rows), 1,
                         f"expected exactly ONE row for {item}, got {len(rows)}")

        return float(rows[0]["quantity"])

    # ----------------------------------------------------------- the contract

    def test_qty_is_a_delta_not_an_absolute_quantity(self):
        """2 then 5 gives 7 on one row -- NOT 5, and NOT two rows.

        This single assertion is the whole contract. If `add_to_cart` ever SET
        the quantity, step 2 would read 5 and this fails.
        """

        first = self.add(2)
        self.assertEqual(self.quantity_of(first), 2.0,
                         "a first add must set the starting quantity")

        second = self.add(5)

        self.assertEqual(
            self.quantity_of(second), 7.0,
            "add_to_cart(qty=5) on a line of 2 must give 7 (DELTA). "
            "Reading 5 means someone converted this to SET semantics -- see the "
            "module docstring before changing this number.")

        self.assertEqual(len(self.rows_for(second)), 1,
                         "the delta must stay on ONE row, not append a second")

    def test_repeating_the_same_request_applies_the_delta_again(self):
        """NON-IDEMPOTENT by design: 2 + 5 + 5 = 12.

        This is why the caller must guard the control and must never auto-retry
        this endpoint after a timeout.
        """

        self.add(2)
        self.add(5)
        third = self.add(5)

        self.assertEqual(
            self.quantity_of(third), 12.0,
            "repeating add_to_cart(qty=5) must apply the delta again (7 -> 12). "
            "Reading 7 would mean the call had become idempotent, and reading 5 "
            "would mean SET semantics.")

        self.assertEqual(len(self.rows_for(third)), 1)

    def test_fractional_deltas_accumulate(self):
        """Fractional quantities are supported and still accumulate."""

        self.add(2.5)
        second = self.add(0.25)

        self.assertAlmostEqual(self.quantity_of(second), 2.75, places=6,
                               msg="fractional deltas must accumulate exactly")
        self.assertEqual(len(self.rows_for(second)), 1)

    def test_a_second_item_gets_its_own_row(self):
        """One row PER ITEM -- the single-row rule is per item_code, not global."""

        other = frappe.db.get_value(
            "Item", {"name": ["!=", ITEM], "disabled": 0, "is_sales_item": 1}, "name")

        if not other:
            self.skipTest("needs a second sellable Item")

        self.add(2)
        data = self.add(3, item=other)

        self.assertEqual(self.quantity_of(data, ITEM), 2.0)
        self.assertEqual(self.quantity_of(data, other), 3.0)

    def test_zero_and_negative_are_refused(self):
        """A delta of zero or less is rejected rather than treated as a set."""

        self.add(2)

        for bad in (0, -1):
            response = self.call(self.cart_api.add_to_cart,
                                 item_code=ITEM, qty=bad)

            self.assertIn("errors", response, f"qty={bad} was accepted")
            self.assertEqual(response["errors"][0]["code"], "quantity_invalid")
            self.assertEqual(response["errors"][0]["field"], "qty")

        # And the refusal left the existing line untouched.
        current = self.call(self.cart_api.get_cart)["data"]["cart"]
        self.assertEqual(self.quantity_of(current), 2.0)

    def test_implementation_still_adds_rather_than_assigns(self):
        """Structural guard: the source must ACCUMULATE, not assign.

        Cheap insurance against a refactor that keeps the tests above passing
        by coincidence -- e.g. one that recomputes the row from scratch.
        """

        from yob_storefront.tests.test_payment_lifecycle import _code_only

        source = _code_only(inspect.unwrap(self.cart_api.add_to_cart))

        self.assertIn("existing.quantity", source)
        self.assertRegex(
            source, r"existing\.quantity\s*=\s*\(?existing\.quantity[^=]*\+",
            "add_to_cart no longer accumulates into the existing row")


if __name__ == "__main__":
    unittest.main()
