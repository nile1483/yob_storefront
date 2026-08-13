# Copyright (c) 2026, YOB and Shayona
"""Phase 2A -- eligibility authority + idempotent local commitment.

Two server primitives, proven in isolation before Phase 2B wires them into
``process_payment``:

1. ``payment_method_service`` -- the single Payment Method eligibility rule.
2. ``commitment_service``     -- immutable Payment Request -> ONE Draft Sales
   Order, idempotent on retry.

Neither is wired into a live endpoint in this phase, so these tests are the
only thing exercising them. Cart fixtures, savepoint isolation and the Proceed
helper are reused from the Phase 1 suite rather than rebuilt; the Gate 2 suite
already proves line-level net/tax/discount parity for the same conversion path,
so it is not duplicated here.
"""

import unittest
from unittest.mock import patch

import frappe

from yob_storefront.services import payment_view
from yob_storefront.services.commitment_service import (
    ensure_payment_request_committed,
)
from yob_storefront.services.payment_method_service import (
    get_eligible_payment_methods,
    is_payment_method_eligible,
)
from yob_storefront.tests.test_payment_lifecycle import (
    CUSTOMER,
    LifecycleCase,
    _error_code,
    _raw,
)

COMPANY = "Shayona Technology"
CUSTOMER_GROUP = "Commercial"


# =========================================================
# 1-5. PAYMENT METHOD ELIGIBILITY
# =========================================================

class EligibilityCase(LifecycleCase):
    """The rule that decides which methods a buyer is offered.

    The site seeds two Company-scoped assignments (Razorpay, Pay Later). Each
    test below clears them inside its savepoint so the case under test is the
    only thing the rule can see -- otherwise a seeded Company assignment would
    satisfy every lookup and the tests would pass without testing anything.
    """

    def setUp(self):
        super().setUp()
        frappe.db.delete("Payment Method Assignment")
        frappe.clear_cache()

    def assign(self, method="Pay Later", reference_doctype="Customer",
               reference_name=CUSTOMER, minimum=0, maximum=0, is_active=1):
        return frappe.get_doc({
            "doctype": "Payment Method Assignment",
            "payment_method": method,
            "reference_doctype": reference_doctype,
            "reference_name": reference_name,
            "minimum_order_amount": minimum,
            "maximum_order_amount": maximum,
            "is_active": is_active,
        }).insert(ignore_permissions=True)

    def eligible(self, amount=100.0):
        return [m["name"] for m in
                get_eligible_payment_methods(CUSTOMER, COMPANY, amount)]

    # ------------------------------------------------------------ 1

    def test_customer_assignment(self):
        self.assign(reference_doctype="Customer", reference_name=CUSTOMER)
        self.assertEqual(self.eligible(), ["Pay Later"])

    def test_customer_assignment_for_another_customer_does_not_apply(self):
        other = frappe.get_all("Customer", filters={"name": ["!=", CUSTOMER]},
                               pluck="name", limit=1)
        if not other:
            self.skipTest("needs a second Customer")

        self.assign(reference_doctype="Customer", reference_name=other[0])
        self.assertEqual(self.eligible(), [])

    # ------------------------------------------------------------ 2

    def test_customer_group_assignment(self):
        self.assign(reference_doctype="Customer Group", reference_name=CUSTOMER_GROUP)
        self.assertEqual(self.eligible(), ["Pay Later"])

    def test_customer_group_mismatch_does_not_apply(self):
        other = frappe.get_all("Customer Group",
                               filters={"name": ["!=", CUSTOMER_GROUP]},
                               pluck="name", limit=1)
        if not other:
            self.skipTest("needs a second Customer Group")

        self.assign(reference_doctype="Customer Group", reference_name=other[0])
        self.assertEqual(self.eligible(), [])

    # ------------------------------------------------------------ 3

    def test_company_assignment(self):
        self.assign(reference_doctype="Company", reference_name=COMPANY)
        self.assertEqual(self.eligible(), ["Pay Later"])

    def test_company_mismatch_does_not_apply(self):
        self.assign(reference_doctype="Company", reference_name=COMPANY)
        self.assertEqual(
            [m["name"] for m in get_eligible_payment_methods(CUSTOMER, "Nope", 100.0)],
            [])

    # ------------------------------------------------------------ 4

    def test_minimum_order_amount_boundary(self):
        self.assign(minimum=500)

        self.assertEqual(self.eligible(amount=499.99), [], "below minimum")
        self.assertEqual(self.eligible(amount=500.0), ["Pay Later"], "at minimum")
        self.assertEqual(self.eligible(amount=500.01), ["Pay Later"], "above minimum")

    def test_maximum_order_amount_boundary(self):
        self.assign(maximum=500)

        self.assertEqual(self.eligible(amount=499.99), ["Pay Later"], "below maximum")
        self.assertEqual(self.eligible(amount=500.0), ["Pay Later"], "at maximum")
        self.assertEqual(self.eligible(amount=500.01), [], "above maximum")

    def test_zero_bounds_are_not_constraints(self):
        """A 0 min/max means 'unset', which is how the seeded data reads."""

        self.assign(minimum=0, maximum=0)
        self.assertEqual(self.eligible(amount=0.01), ["Pay Later"])

    # ------------------------------------------------------------ 5

    def test_inactive_assignment_is_ignored(self):
        self.assign(is_active=0)
        self.assertEqual(self.eligible(), [])

    def test_inactive_payment_method_is_ignored(self):
        """An active assignment cannot resurrect a deactivated method."""

        self.assign()
        frappe.db.set_value("Payment Method", "Pay Later", "is_active", 0)
        frappe.clear_cache()

        self.assertEqual(self.eligible(), [])

    def test_method_with_no_applicable_assignment(self):
        self.assertEqual(self.eligible(), [], "no assignment must offer nothing")

    # ------------------------------------------------------------ shape

    def test_display_fields_and_order_are_server_owned(self):
        """The browser shows exactly what the server says, in this order."""

        self.assign(method="Pay Later")      # display_order 1
        self.assign(method="Razorpay")       # display_order 2

        methods = get_eligible_payment_methods(CUSTOMER, COMPANY, 100.0)

        self.assertEqual([m["name"] for m in methods], ["Pay Later", "Razorpay"])
        for method in methods:
            self.assertEqual(
                set(method),
                {"name", "method_code", "payment_type", "display_order",
                 "icon", "description"})

    def test_single_method_recheck_matches_the_offered_list(self):
        """process_payment will re-check one method; it must not drift."""

        self.assign(method="Pay Later", minimum=500)

        self.assertTrue(is_payment_method_eligible("Pay Later", CUSTOMER, COMPANY, 600))
        self.assertFalse(is_payment_method_eligible("Pay Later", CUSTOMER, COMPANY, 400))
        self.assertFalse(is_payment_method_eligible("Razorpay", CUSTOMER, COMPANY, 600))
        self.assertFalse(is_payment_method_eligible(None, CUSTOMER, COMPANY, 600))

    def test_endpoint_returns_exactly_what_the_service_decides(self):
        """The migrated API must reproduce the service, not a second copy."""

        self.assign(method="Pay Later")
        self.assign(method="Razorpay", minimum=1000)

        from yob_storefront.api import payment_method as api

        with patch.object(api, "assert_customer_matches"), \
                patch.object(api, "get_storefront_customer",
                             return_value=self.customer):
            response = _raw(api.get_payment_methods)(
                customer=CUSTOMER, company=COMPANY, order_amount=100,
                auth_context={"profile_name": CUSTOMER})

        self.assertEqual(response["data"],
                         get_eligible_payment_methods(CUSTOMER, COMPANY, 100.0))
        self.assertEqual(response["meta"]["count"], len(response["data"]))


# =========================================================
# 6-11, 14. LOCAL COMMITMENT
# =========================================================

class CommitmentCase(LifecycleCase):

    def committed_pr(self, qty=12):
        """A Cart-backed obligation ready to commit."""

        cart = self.make_cart(qty=qty)
        data = self.assert_created(self.proceed())
        return cart, data

    # ------------------------------------------------------------ 6, 7, 8, 9

    def test_cart_backed_pr_commits_to_exactly_one_draft_sales_order(self):
        cart, data = self.committed_pr()
        so_before = frappe.db.count("Sales Order")

        result = ensure_payment_request_committed(token=data["token"])

        self.assertFalse(_error_code(result), f"unexpected error: {result}")
        self.assertTrue(result["created"])
        self.assertEqual(frappe.db.count("Sales Order"), so_before + 1)
        self.assertEqual(result["sales_order"].docstatus, 0, "must stay Draft")

    def test_financial_invariant_pr_equals_cart_equals_sales_order(self):
        """7. One number, three documents."""

        cart, data = self.committed_pr()
        pr_before = self.pr_row(data["payment_request"], "grand_total", "currency")

        result = ensure_payment_request_committed(token=data["token"])
        so = result["sales_order"]

        cart_now = frappe.get_doc("Cart", cart.name)

        self.assertAlmostEqual(float(pr_before.grand_total),
                               float(cart_now.grand_total), places=2)
        self.assertAlmostEqual(float(pr_before.grand_total),
                               float(so.grand_total), places=2)
        self.assertEqual(pr_before.currency, cart_now.currency)
        self.assertEqual(pr_before.currency, so.currency)

    def test_cart_becomes_ordered_with_the_sales_order_reference(self):
        """8."""

        cart, data = self.committed_pr()

        result = ensure_payment_request_committed(token=data["token"])

        row = frappe.db.get_value(
            "Cart", cart.name, ["status", "sales_order", "ordered_on"], as_dict=True)

        self.assertEqual(row.status, "Ordered")
        self.assertEqual(row.sales_order, result["sales_order"].name)
        self.assertTrue(row.ordered_on)

    def test_pr_becomes_sales_order_backed_without_financial_mutation(self):
        """9. The reference moves; the obligation does not."""

        cart, data = self.committed_pr()
        pr_name = data["payment_request"]

        before = self.pr_row(pr_name, "grand_total", "currency",
                             "custom_source_fingerprint", "custom_checkout_token",
                             "custom_checkout_expiry")

        result = ensure_payment_request_committed(token=data["token"])

        after = self.pr_row(pr_name, "grand_total", "currency",
                            "custom_source_fingerprint", "custom_checkout_token",
                            "custom_checkout_expiry", "reference_doctype",
                            "reference_name")

        self.assertEqual(after.reference_doctype, "Sales Order")
        self.assertEqual(after.reference_name, result["sales_order"].name)

        for field in before:
            self.assertEqual(before[field], after[field],
                             f"commitment mutated {field}")

    def test_committed_pr_is_never_recompared_against_a_cart(self):
        """The retained fingerprint is history, not a Sales Order fingerprint."""

        from yob_storefront.services.payment_request_service import (
            validate_payment_request_source_current,
        )

        cart, data = self.committed_pr()
        ensure_payment_request_committed(token=data["token"])

        pr = frappe.get_doc("Payment Request", data["payment_request"])

        self.assertTrue(pr.custom_source_fingerprint, "history must be retained")
        self.assertEqual(
            _error_code(validate_payment_request_source_current(pr)),
            "payment_reference_invalid",
            "a committed obligation must not be compared to a Cart")

    # ------------------------------------------------------------ 10

    def test_conversion_failure_rolls_back_completely(self):
        """10. No partial Sales Order, Cart stays Draft, PR stays Cart-backed.

        A disabled Customer is a hard transaction-time ERPNext validation, and
        it is disabled below the ORM so the failure lands inside the conversion
        rather than in fixture setup -- the same trigger the Gate 2 rollback
        test uses.
        """

        cart, data = self.committed_pr()
        so_before = frappe.db.count("Sales Order")

        frappe.db.set_value("Customer", CUSTOMER, "disabled", 1, update_modified=False)
        frappe.clear_cache()

        with self.assertRaises(frappe.ValidationError):
            ensure_payment_request_committed(token=data["token"])

        self.assertEqual(frappe.db.count("Sales Order"), so_before,
                         "a Sales Order survived a failed commitment")

        cart_row = frappe.db.get_value("Cart", cart.name,
                                       ["status", "sales_order"], as_dict=True)
        self.assertEqual(cart_row.status, "Draft")
        self.assertFalse(cart_row.sales_order)

        pr_row = self.pr_row(data["payment_request"],
                             "reference_doctype", "reference_name")
        self.assertEqual(pr_row.reference_doctype, "Cart")
        self.assertEqual(pr_row.reference_name, cart.name)

    def test_rollback_leaves_no_stale_document_cache(self):
        """The Gate 3 rule, applied to the commitment block.

        Rollback restores the database but not Frappe's cache. After the failed
        attempt above, a cached read must still show database truth.
        """

        cart, data = self.committed_pr()

        frappe.db.set_value("Customer", CUSTOMER, "disabled", 1, update_modified=False)
        frappe.clear_cache()

        with self.assertRaises(frappe.ValidationError):
            ensure_payment_request_committed(token=data["token"])

        self.assertEqual(frappe.get_cached_doc("Cart", cart.name).status, "Draft")
        self.assertEqual(
            frappe.get_cached_doc("Payment Request",
                                  data["payment_request"]).reference_doctype,
            "Cart")

    # ------------------------------------------------------------ 11, 14

    def test_retry_on_committed_pr_returns_the_same_sales_order(self):
        """11. What makes a retry after a lost response safe."""

        cart, data = self.committed_pr()

        first = ensure_payment_request_committed(token=data["token"])
        so_after_first = frappe.db.count("Sales Order")

        second = ensure_payment_request_committed(token=data["token"])

        self.assertFalse(_error_code(second), f"unexpected error: {second}")
        self.assertFalse(second["created"], "retry must not create")
        self.assertEqual(second["sales_order"].name, first["sales_order"].name)
        self.assertEqual(frappe.db.count("Sales Order"), so_after_first,
                         "retry created a second Sales Order")

    def test_repeated_commitment_converges_on_one_sales_order(self):
        """14. Competing commitment, replayed as the locks would serialise it.

        Two requests contending for the same Cart-backed Payment Request are
        serialised by the Cart lock, so the loser proceeds only after the winner
        committed -- and then reloads the Payment Request under its own lock and
        finds it already Sales-Order-backed. Replaying that sequence tests the
        branch the lock produces. A genuine parallel race needs two database
        connections blocking on each other, which this runner cannot do inside
        its shared savepoint without self-deadlocking; the ordering that makes
        it safe is asserted separately below.
        """

        cart, data = self.committed_pr()
        so_before = frappe.db.count("Sales Order")

        results = [ensure_payment_request_committed(token=data["token"])
                   for _ in range(3)]

        names = {r["sales_order"].name for r in results}

        self.assertEqual(len(names), 1, f"commitments diverged: {names}")
        self.assertEqual([r["created"] for r in results], [True, False, False])
        self.assertEqual(frappe.db.count("Sales Order"), so_before + 1)

    def test_commitment_locks_cart_before_payment_request(self):
        """Lock direction must match proceed_to_payment, or the two deadlock.

        Proceed locks Cart then touches Payment Requests. Commitment must take
        the same direction; PR-then-Cart would be a textbook lock-order
        inversion.
        """

        from yob_storefront.services import commitment_service
        from yob_storefront.tests.test_payment_lifecycle import _code_only

        source = _code_only(commitment_service.ensure_payment_request_committed)

        cart_lock = source.find("frappe.db.get_value('Cart', cart_name, 'name', for_update=True)")
        pr_lock = source.find("frappe.db.get_value('Payment Request', pr.name, 'name', for_update=True)")

        self.assertGreater(cart_lock, 0, "the Cart row is never locked")
        self.assertGreater(pr_lock, cart_lock,
                           "Payment Request is locked before the Cart -- deadlock ordering")

    def test_service_does_not_commit_the_transaction(self):
        """The caller owns the transaction boundary. Pinned, because a stray
        commit here would release the locks early and silently break the
        concurrency guarantee this service exists to provide."""

        from yob_storefront.services import commitment_service
        from yob_storefront.tests.test_payment_lifecycle import _code_only

        for fn in (commitment_service.ensure_payment_request_committed,
                   commitment_service._commit_cart):
            self.assertNotIn("frappe.db.commit", _code_only(fn),
                             f"{fn.__name__} commits; the caller must")

    # ------------------------------------------------------------ guards

    def test_stale_cart_cannot_commit(self):
        """Phase 1's staleness rule still gates the commitment."""

        cart, data = self.committed_pr()

        cart.reload()
        cart.items[0].quantity = 20
        self.reprice(cart)

        result = ensure_payment_request_committed(token=data["token"])

        self.assertEqual(_error_code(result), "payment_request_stale")
        self.assertEqual(
            self.pr_row(data["payment_request"], "reference_doctype").reference_doctype,
            "Cart")

    def test_superseded_token_cannot_commit(self):
        """A credential revoked while this request waited must not commit."""

        cart, data = self.committed_pr()

        cart.reload()
        cart.items[0].quantity = 20
        self.reprice(cart)
        self.proceed()                      # supersedes the first credential

        result = ensure_payment_request_committed(token=data["token"])

        self.assertTrue(_error_code(result), "a revoked token committed an order")


# =========================================================
# 12-13. SOURCE DISPATCHER
# =========================================================

class SourceDispatcherCase(LifecycleCase):

    def test_cart_backed_summary(self):
        """12."""

        cart = self.make_cart(qty=12)
        data = self.assert_created(self.proceed())
        pr = frappe.get_doc("Payment Request", data["payment_request"])

        summary = payment_view.payment_summary(pr)

        self.assertEqual(summary["source_doctype"], "Cart")
        self.assertEqual(summary["source_name"], cart.name)
        self.assertEqual(summary["customer"], CUSTOMER)
        self.assertEqual(len(summary["items"]), len(cart.items))

        # The money comes from the obligation, never from the source document.
        self.assertAlmostEqual(float(summary["amount"]), float(pr.grand_total), places=2)
        self.assertEqual(summary["currency"], pr.currency)

    def test_sales_order_backed_summary(self):
        """13. After commitment the same dispatcher reads the Sales Order."""

        cart = self.make_cart(qty=12)
        data = self.assert_created(self.proceed())

        result = ensure_payment_request_committed(token=data["token"])
        so = result["sales_order"]

        frappe.clear_document_cache("Payment Request", data["payment_request"])
        pr = frappe.get_doc("Payment Request", data["payment_request"])

        summary = payment_view.payment_summary(pr)

        self.assertEqual(summary["source_doctype"], "Sales Order")
        self.assertEqual(summary["source_name"], so.name)
        self.assertEqual(summary["customer"], CUSTOMER)
        self.assertEqual(len(summary["items"]), len(so.items))
        self.assertAlmostEqual(float(summary["amount"]), float(pr.grand_total), places=2)
        self.assertEqual(summary["docstatus"], 0)

        # And it must NOT have been derived from a Cart.
        self.assertNotIn("is_shippable", summary)

    def test_unsupported_source_is_refused(self):
        cart = self.make_cart(qty=12)
        data = self.assert_created(self.proceed())

        frappe.db.set_value("Payment Request", data["payment_request"], {
            "reference_doctype": "Sales Invoice", "reference_name": "whatever"})
        frappe.clear_document_cache("Payment Request", data["payment_request"])

        pr = frappe.get_doc("Payment Request", data["payment_request"])

        self.assertEqual(_error_code(payment_view.payment_summary(pr)),
                         "payment_reference_invalid")

    def test_dispatcher_is_wired_into_get_checkout_data(self):
        """Phase 2B wires it: a refresh after commitment must still work.

        This test previously asserted the OPPOSITE -- that Phase 2A stayed
        inert at the public boundary. The cutover is exactly what changed it,
        and the same token must now resolve to the committed order rather than
        payment_reference_invalid.
        """

        cart = self.make_cart(qty=12)
        data = self.assert_created(self.proceed())
        result = ensure_payment_request_committed(token=data["token"])

        from yob_storefront.api import payment

        response = _raw(payment.get_checkout_data)(token=data["token"])

        self.assertIsNone(_error_code(response), f"unexpected error: {response}")
        self.assertEqual(response["data"]["source_doctype"], "Sales Order")
        self.assertEqual(response["data"]["source_name"], result["sales_order"].name)


if __name__ == "__main__":
    unittest.main()
