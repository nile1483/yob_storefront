# Copyright (c) 2026, YOB and Shayona
"""Gateway Phase B1 -- preflight boundary + Integration Request authority.

Two distinct failure classes must stay distinguishable, in the data and in the
API:

    preflight failure    the gateway could never have taken this payment.
                         Nothing is committed: Cart stays Draft, the Payment
                         Request stays Cart-backed, no Sales Order exists, and
                         no provider call is made. `details.retryable = false`.

    provider failure     a real obligation exists and the network or provider
                         failed. The Draft/Unpaid Sales Order STANDS and the
                         attempt is retryable. `details.retryable = true` plus
                         `details.sales_order`.

Phase A committed the Cart before checking gateway configuration, so an
unconfigured gateway produced a real order for a payment that could never
start. B1 fixes that ordering; these tests pin it.

The second half pins Integration Request as provider transport/audit only,
BEFORE Phase B2 starts producing them via the Payments controller.
"""

import unittest
from unittest.mock import patch

import frappe

from yob_storefront.integrations.gateways.base import (
    Obligation,
    ProviderNotConfigured,
    ProviderPreflightFailed,
)
from yob_storefront.integrations.gateways.razorpay_gateway import RazorpayGateway
from yob_storefront.tests.test_payment_cutover import CutoverCase, FakeRazorpay
from yob_storefront.tests.test_payment_lifecycle import _error_code


def _details(response) -> dict:
    return response["errors"][0].get("details") or {}


# =========================================================
# PREFLIGHT: DRIVER LEVEL
# =========================================================

class DriverPreflightCase(unittest.TestCase):

    def setUp(self):
        self.gateway = RazorpayGateway()

    def test_configured_gateway_passes_preflight(self):
        """3. INR is supported and credentials are present."""

        pr = frappe._dict(name="PR-TEST", grand_total=135.0, currency="INR")

        with patch.object(RazorpayGateway, "public_key", return_value="rzp_test_x"):
            self.gateway.preflight(Obligation.pending(pr))   # must not raise

    def test_missing_credentials_fail_preflight(self):
        pr = frappe._dict(name="PR-TEST", grand_total=135.0, currency="INR")

        with patch.object(RazorpayGateway, "public_key", return_value=""):
            with self.assertRaises(ProviderNotConfigured):
                self.gateway.preflight(Obligation.pending(pr))

    def test_unsupported_currency_fails_preflight(self):
        """Currency comes from the Payments controller's own supported list."""

        pr = frappe._dict(name="PR-TEST", grand_total=135.0, currency="XYZ")

        with patch.object(RazorpayGateway, "public_key", return_value="rzp_test_x"):
            with self.assertRaises(ProviderPreflightFailed):
                self.gateway.preflight(Obligation.pending(pr))

    def test_currency_list_is_the_installed_payments_list(self):
        """YOB keeps no copy of provider currency metadata."""

        controller = self.gateway.controller()

        self.assertIn("INR", controller.supported_currencies)
        self.assertNotIn("XYZ", controller.supported_currencies)

    def test_preflight_makes_no_provider_call(self):
        """9. Static checks only -- nothing irreversible, nothing on the wire."""

        pr = frappe._dict(name="PR-TEST", grand_total=135.0, currency="INR")

        from yob_storefront.integrations.razorpay import client as rz

        with patch.object(RazorpayGateway, "public_key", return_value="rzp_test_x"), \
                patch.object(rz, "get_client") as spy:
            self.gateway.preflight(Obligation.pending(pr))

        spy.assert_not_called()

    def test_preflight_does_not_read_the_sales_order(self):
        """There is no Sales Order yet; a driver reading one would be a bug."""

        pr = frappe._dict(name="PR-TEST", grand_total=135.0, currency="INR")
        obligation = Obligation.pending(pr)

        self.assertIsNone(obligation.sales_order)

        with patch.object(RazorpayGateway, "public_key", return_value="rzp_test_x"):
            self.gateway.preflight(obligation)


# =========================================================
# PREFLIGHT: ENDPOINT LEVEL
# =========================================================

class EndpointPreflightCase(CutoverCase):

    def assert_nothing_committed(self, cart, data, so_before):
        """6, 7, 8. The whole point: a refused start leaves no trace."""

        self.assertEqual(frappe.db.count("Sales Order"), so_before,
                         "preflight failure created a Sales Order")
        self.assertEqual(frappe.db.get_value("Cart", cart.name, "status"), "Draft",
                         "preflight failure left the Cart Ordered")
        self.assertIsNone(frappe.db.get_value("Cart", cart.name, "sales_order"))

        pr = self.pr_row(data["payment_request"],
                         "reference_doctype", "reference_name", "status")
        self.assertEqual(pr.reference_doctype, "Cart",
                         "preflight failure moved the obligation to a Sales Order")
        self.assertEqual(pr.reference_name, cart.name)
        self.assertNotEqual(pr.status, "Paid")

        self.assertEqual(self.commits, [], "preflight failure committed durably")
        self.assertEqual(len(self.fake.orders), 0,
                         "preflight failure still contacted the provider")

    def test_missing_credentials_fail_before_commitment(self):
        """4, 6, 7, 8, 9. The Phase A behaviour this phase deliberately changes."""

        cart, data = self.started()
        so_before = frappe.db.count("Sales Order")

        self.unconfigure_gateway()

        response = self.pay(data["token"], "Razorpay")

        self.assertEqual(_error_code(response), "payment_provider_not_configured")
        self.assertIs(_details(response)["retryable"], False)
        self.assertNotIn("sales_order", _details(response),
                         "a preflight failure must not name an order")

        self.assert_nothing_committed(cart, data, so_before)

    def test_unsupported_currency_fails_before_commitment(self):
        """5, 6, 7, 8, 9.

        The gateway's supported list is narrowed rather than the Payment
        Request's currency being edited. Editing the obligation would ALSO make
        it disagree with its Cart, so the request would answer
        `payment_request_stale` and this test would never reach the currency
        rule it exists to prove -- which is exactly what happened when the B2
        ordering change put the staleness check first.

        Narrowing the controller's own list also keeps the delegation under
        test: the value consulted is Frappe Payments' `supported_currencies`.
        """

        cart, data = self.started()
        so_before = frappe.db.count("Sales Order")

        from payments.payment_gateways.doctype.razorpay_settings.razorpay_settings import (
            RazorpaySettings,
        )

        with patch.object(RazorpaySettings, "supported_currencies", ("USD",)):
            response = self.pay(data["token"], "Razorpay")

        self.assertEqual(_error_code(response), "payment_provider_error")
        self.assertIs(_details(response)["retryable"], False)
        self.assertNotIn("sales_order", _details(response))

        self.assert_nothing_committed(cart, data, so_before)

    def test_configured_gateway_still_completes_the_lifecycle(self):
        """10. Preflight must not block the good path."""

        cart, data = self.started()

        response = self.pay(data["token"], "Razorpay")

        self.assertIsNone(_error_code(response), f"unexpected: {response}")
        self.assertEqual(
            set(response["data"]),
            {"payment_method", "razorpay_key", "order_id", "amount",
             "currency", "sales_order", "payment_request"})
        self.assertEqual(frappe.db.get_value("Cart", cart.name, "status"), "Ordered")

    def test_provider_failure_after_commitment_still_keeps_the_order(self):
        """11. The OTHER failure class must be unaffected -- and distinguishable."""

        cart, data = self.started()
        self.fake.fail_create = True

        response = self.pay(data["token"], "Razorpay")

        self.assertEqual(_error_code(response), "payment_provider_error")

        details = _details(response)
        self.assertIs(details["retryable"], True)
        self.assertTrue(details["sales_order"], "a real failure must name its order")

        self.assertEqual(frappe.db.get_value("Sales Order", details["sales_order"],
                                             "docstatus"), 0)
        self.assertEqual(frappe.db.get_value("Cart", cart.name, "status"), "Ordered")

    def test_pay_later_skips_gateway_preflight_entirely(self):
        """2, 8. An internal method has no provider to preflight.

        Proven by breaking the gateway: with no credentials at all, Pay Later
        must still commit normally, because it never touches a driver.
        """

        cart, data = self.started()
        so_before = frappe.db.count("Sales Order")

        self.unconfigure_gateway()

        with patch.object(RazorpayGateway, "preflight",
                          side_effect=AssertionError("Pay Later ran gateway preflight")):
            response = self.pay(data["token"], "Pay Later")

        self.assertIsNone(_error_code(response), f"unexpected: {response}")
        self.assertEqual(frappe.db.count("Sales Order"), so_before + 1)
        self.assertEqual(response["data"]["payment_status"], "Unpaid")

    def test_process_payment_has_no_razorpay_settings_lookup(self):
        """14. Provider configuration lives behind the driver, not in the API."""

        from yob_storefront.api import payment
        from yob_storefront.tests.test_payment_lifecycle import _code_only

        for fn in (payment.process_payment, payment.process_gateway_payment,
                   payment._preflight):
            source = _code_only(fn)
            self.assertNotIn("Razorpay Settings", source)
            self.assertNotIn("api_key", source)


# =========================================================
# INTEGRATION REQUEST AUTHORITY
# =========================================================

class IntegrationRequestAuthorityCase(CutoverCase):
    """Pinned BEFORE B2 starts producing Integration Requests.

    Authority hierarchy:

        commercial/payment   Payment Request, Sales Order
        transport/audit      Integration Request

    An Integration Request must never redefine the payable amount, decide the
    source, mutate immutable Payment Request fields, become the idempotency
    authority, or cause a Cart -> Sales Order conversion.
    """

    def test_payments_order_creation_is_audit_only(self):
        """10 (spike). Exercise the REAL Payments path B2 will adopt.

        ``controller.create_order`` is called with only the network stubbed, so
        this proves what the installed code actually does to YOB state -- not
        what its docstring implies. It is the de-risking evidence for B2.
        """

        cart, data = self.started()

        pr_before = self.pr_row(data["payment_request"], "grand_total", "currency",
                                "custom_source_fingerprint", "reference_doctype",
                                "reference_name", "status")
        cart_before = frappe.db.get_value("Cart", cart.name,
                                          ["status", "sales_order"], as_dict=True)
        so_before = frappe.db.count("Sales Order")
        ir_before = frappe.db.count("Integration Request")

        controller = RazorpayGateway().controller()

        # Credentials must exist for create_order to attempt the request at all.
        frappe.db.set_single_value("Razorpay Settings", "api_key", "rzp_test_x")
        frappe.db.set_single_value("Razorpay Settings", "api_secret", "secret_x")
        frappe.clear_document_cache("Razorpay Settings", "Razorpay Settings")
        controller = RazorpayGateway().controller()

        captured = {}

        def fake_post(url, auth=None, data=None, **kwargs):
            captured["url"] = url
            captured["data"] = data
            return {"id": "order_SPIKE0001", "status": "created",
                    "amount": data["amount"], "currency": data["currency"],
                    "receipt": data.get("receipt")}

        with patch(
            "payments.payment_gateways.doctype.razorpay_settings."
            "razorpay_settings.make_post_request",
            side_effect=fake_post,
        ):
            order = controller.create_order(
                amount=135.0,                # BUSINESS units -- see B2 note
                currency="INR",
                receipt="yob-spike",
                payment_capture=1,
            )

        # It did create an Integration Request...
        self.assertEqual(frappe.db.count("Integration Request"), ir_before + 1)
        self.assertTrue(order.get("integration_request"))

        # ...and it changed NOTHING that YOB treats as authoritative.
        self.assertEqual(
            dict(pr_before),
            dict(self.pr_row(data["payment_request"], "grand_total", "currency",
                             "custom_source_fingerprint", "reference_doctype",
                             "reference_name", "status")),
            "Integration Request creation mutated the Payment Request")

        self.assertEqual(
            dict(cart_before),
            dict(frappe.db.get_value("Cart", cart.name,
                                     ["status", "sales_order"], as_dict=True)),
            "Integration Request creation converted the Cart")

        self.assertEqual(frappe.db.count("Sales Order"), so_before,
                         "Integration Request creation created a Sales Order")

        # And it confirms the B2 unit hazard: rupees in, paise on the wire.
        self.assertEqual(captured["data"]["amount"], 13500,
                         "controller.create_order multiplies by 100 internally")

    def test_settlement_hooks_are_not_reachable_from_order_creation(self):
        """The redirect/accounting lifecycle stays unentered.

        ``authorize_payment`` is the function that runs ``on_payment_authorized``
        and drives Frappe's redirect flow. Order creation must not call it, and
        neither must any YOB code.
        """

        import inspect

        from payments.payment_gateways.doctype.razorpay_settings import (
            razorpay_settings as payments_razorpay,
        )

        source = inspect.getsource(payments_razorpay.RazorpaySettings.create_order)

        for forbidden in ("authorize_payment", "on_payment_authorized",
                          "finalize_request", "redirect_to"):
            self.assertNotIn(forbidden, source,
                             f"Payments create_order reaches {forbidden}")

    def test_integration_request_is_not_yob_idempotency_authority(self):
        """12. Convergence comes from the deterministic receipt, not from audit.

        Since B2 the Payments controller DOES create Integration Requests, so
        the earlier "creates none" assertion no longer holds and would be the
        wrong thing to assert. What must remain true is stronger: the second
        initiation reuses the locally stored provider order WITHOUT creating
        another Integration Request or another provider order -- so the audit
        trail is demonstrably not what makes the retry safe.
        """

        cart, data = self.started()

        first = self.pay(data["token"], "Razorpay")
        ir_after_first = frappe.db.count("Integration Request")

        second = self.pay(data["token"], "Razorpay")

        self.assertEqual(first["data"]["order_id"], second["data"]["order_id"])
        self.assertEqual(len(self.fake.orders), 1,
                         "a retry created a second provider order")
        self.assertEqual(frappe.db.count("Integration Request"), ir_after_first,
                         "a reused order still produced a new audit record")


if __name__ == "__main__":
    unittest.main()


# =========================================================
# B2: DELEGATED ORDER CREATION
# =========================================================

class DelegatedOrderCreationCase(CutoverCase):
    """Order creation now goes through the Frappe Payments controller.

    Fetch, receipt lookup, order-payments and signature verification remain YOB
    extensions, because Payments provides none of them.
    """

    def test_controller_receives_business_amount_not_minor(self):
        """3. The single most dangerous unit in the system.

        The Payments controller takes MAJOR units and multiplies by 100 itself.
        Passing `amount_minor` would bill a hundred times the obligation. This
        asserts on the payload that reached the wire, so it fails catastrophically
        and visibly if the wrong unit is ever passed.
        """

        cart, data = self.started()
        pr = self.pr_row(data["payment_request"], "grand_total")
        expected_minor = int(round(float(pr.grand_total) * 100))

        response = self.pay(data["token"], "Razorpay")

        self.assertIsNone(_error_code(response), f"unexpected: {response}")

        payload = self.fake.controller_payloads[0]

        self.assertEqual(payload["amount"], expected_minor,
                         "controller did not produce the expected minor amount")
        self.assertEqual(response["data"]["amount"], expected_minor)

        # The catastrophic case, stated explicitly: had amount_minor been passed
        # as the business amount, the wire would have carried 100x.
        self.assertNotEqual(payload["amount"], expected_minor * 100,
                            "amount_minor was passed as the business amount")

    def test_controller_receives_the_deterministic_receipt(self):
        """4. Identity survives delegation."""

        from yob_storefront.integrations.razorpay import client as rz

        cart, data = self.started()

        self.pay(data["token"], "Razorpay")

        payload = self.fake.controller_payloads[0]

        self.assertEqual(
            payload["receipt"],
            rz.receipt_for_payment_request(data["payment_request"]),
            "receipt must be derived from the immutable Payment Request")
        self.assertLessEqual(len(payload["receipt"]), 40, "Razorpay limit")

    def test_generic_controller_failure_recovers_by_receipt(self):
        """7. Recovery is driven by identity, never by parsing an exception.

        The Payments controller collapses every provider failure into one
        generic error, so the duplicate-receipt signal never survives. Here the
        order DOES exist at the provider and the create still 'fails'; recovery
        must find it.
        """

        cart, data = self.started()

        # Seed the provider with the order this obligation would create, then
        # make every create attempt fail generically.
        from yob_storefront.integrations.razorpay import client as rz

        receipt = rz.receipt_for_payment_request(data["payment_request"])
        self.fake.orders["order_PRE0001"] = {
            "id": "order_PRE0001", "receipt": receipt,
            "amount": int(round(float(
                self.pr_row(data["payment_request"], "grand_total").grand_total) * 100)),
            "currency": "INR", "status": "created",
        }
        self.fake.fail_create = True

        response = self.pay(data["token"], "Razorpay")

        self.assertIsNone(_error_code(response), f"unexpected: {response}")
        self.assertEqual(response["data"]["order_id"], "order_PRE0001",
                         "recovery did not find the existing order")
        self.assertEqual(len(self.fake.orders), 1,
                         "recovery created a second provider order")

    def test_generic_controller_failure_without_order_is_a_safe_error(self):
        """8. Nothing to recover -> retryable provider error, order intact."""

        cart, data = self.started()
        self.fake.fail_create = True

        response = self.pay(data["token"], "Razorpay")

        self.assertEqual(_error_code(response), "payment_provider_error")
        self.assertIs(_details(response)["retryable"], True)
        self.assertTrue(_details(response)["sales_order"])
        self.assertEqual(len(self.fake.orders), 0)

    def test_lost_local_persistence_recovers_on_retry(self):
        """9. Provider has the order; our local id write was lost."""

        cart, data = self.started()

        first = self.pay(data["token"], "Razorpay")
        order_id = first["data"]["order_id"]

        frappe.db.set_value("Payment Request", data["payment_request"],
                            "custom_razorpay_order_id", None)
        frappe.clear_document_cache("Payment Request", data["payment_request"])

        second = self.pay(data["token"], "Razorpay")

        self.assertEqual(second["data"]["order_id"], order_id)
        self.assertEqual(len(self.fake.orders), 1,
                         "retry created a second provider order")
        self.assertEqual(
            self.pr_row(data["payment_request"],
                        "custom_razorpay_order_id").custom_razorpay_order_id,
            order_id, "recovered order id was not persisted")

    def test_existing_local_order_is_reused_without_creating(self):
        """10. No create call at all when a valid local order exists."""

        cart, data = self.started()

        first = self.pay(data["token"], "Razorpay")
        calls_after_first = self.fake.create_calls

        second = self.pay(data["token"], "Razorpay")

        self.assertEqual(second["data"]["order_id"], first["data"]["order_id"])
        self.assertEqual(self.fake.create_calls, calls_after_first,
                         "a valid stored order still triggered a create")

    def test_recovery_verifies_amount_receipt_and_currency(self):
        """11, 12, 13. A recovered order must BE this obligation.

        Each field is corrupted in turn on the provider's copy; every one must
        be refused rather than reused.
        """

        from yob_storefront.integrations.razorpay import client as rz

        for field, bad in (("amount", 999), ("currency", "USD"),
                           ("receipt", "someone-elses-receipt")):
            with self.subTest(field=field):
                frappe.db.rollback(save_point="phase1")
                frappe.db.savepoint("phase1")
                self.configure_gateway()
                self.fake = FakeRazorpay()

                cart, data = self.started()
                receipt = rz.receipt_for_payment_request(data["payment_request"])
                total = float(self.pr_row(data["payment_request"],
                                          "grand_total").grand_total)

                order = {"id": "order_BAD0001", "receipt": receipt,
                         "amount": int(round(total * 100)), "currency": "INR",
                         "status": "created"}
                order[field] = bad
                self.fake.orders["order_BAD0001"] = order
                self.fake.fail_create = True

                response = self.pay(data["token"], "Razorpay")

                self.assertEqual(_error_code(response), "payment_provider_error",
                                 f"a mismatched {field} was accepted")
                self.assertIsNone(
                    self.pr_row(data["payment_request"],
                                "custom_razorpay_order_id").custom_razorpay_order_id,
                    f"a mismatched {field} was persisted")

    def test_published_spa_contract_is_unchanged(self):
        """6. Delegation must be invisible to Angular."""

        cart, data = self.started()

        response = self.pay(data["token"], "Razorpay")

        self.assertEqual(
            set(response["data"]),
            {"payment_method", "razorpay_key", "order_id", "amount",
             "currency", "sales_order", "payment_request"})
        # No Integration Request identifier may leak into the SPA contract.
        self.assertNotIn("integration_request", str(response["data"]).lower())


# =========================================================
# B2 HARDENING: DURABLE CREATION CLAIM
# =========================================================

class CreationClaimCase(CutoverCase):
    """The wire disproved receipt idempotency, so YOB provides its own.

    Razorpay accepts duplicate receipts and its receipt listing is eventually
    consistent, so "look before you create" has a window. The durable claim,
    committed before the network, is what closes it.
    """

    def setUp(self):
        super().setUp()
        # Backoff must never actually sleep in tests; the branching is what
        # matters, not wall-clock time.
        self._sleeps = []
        patcher = patch(
            "yob_storefront.integrations.gateways.razorpay_gateway._sleep",
            side_effect=self._sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def claim_at(self, pr_name):
        return frappe.db.get_value("Payment Request", pr_name,
                                   "custom_provider_claim_at")

    def test_fake_permits_duplicate_receipts(self):
        """1. The fake models reality, not the documentation.

        If this ever fails, the fake has drifted back to protecting us from our
        own bug and every convergence test below becomes worthless.
        """

        self.fake.create_via_http({"amount": 100, "currency": "INR",
                                   "receipt": "same"})
        self.fake.create_via_http({"amount": 100, "currency": "INR",
                                   "receipt": "same"})

        self.assertEqual(len(self.fake.orders), 2,
                         "fake still enforces receipt uniqueness")

    def test_receipt_lookup_may_initially_return_nothing(self):
        """2. Eventual consistency, as observed on the wire."""

        from yob_storefront.integrations.razorpay import client as rz

        self.fake.lookup_delay_calls = 1
        self.fake.create_via_http({"amount": 100, "currency": "INR",
                                   "receipt": "r1"})

        with self.fake.install():
            self.assertEqual(rz.find_orders_by_receipt("r1"), [])
            self.assertEqual(len(rz.find_orders_by_receipt("r1")), 1)

    def test_claim_is_durable_before_the_network(self):
        """5. Claim committed, then canonical id persisted."""

        cart, data = self.started()

        response = self.pay(data["token"], "Razorpay")

        self.assertIsNone(_error_code(response), f"unexpected: {response}")
        self.assertTrue(self.claim_at(data["payment_request"]),
                        "no durable creation claim was recorded")
        self.assertEqual(
            self.pr_row(data["payment_request"],
                        "custom_razorpay_order_id").custom_razorpay_order_id,
            response["data"]["order_id"])

    def test_second_caller_observes_the_claim_and_does_not_create(self):
        """3, 4. Only ONE outbound create despite a permissive provider."""

        cart, data = self.started()

        # First request takes the claim but its response is lost, so no
        # canonical id is stored -- the state a competing request would see.
        self.fake.lose_create_response = True
        self.fake.lookup_delay_calls = 99          # never becomes visible
        first = self.pay(data["token"], "Razorpay")

        self.assertEqual(_error_code(first), "payment_provider_error")
        self.assertEqual(self.fake.create_calls, 1)
        self.assertTrue(self.claim_at(data["payment_request"]))

        # Second request: claim exists, so it must RECOVER, never create.
        second = self.pay(data["token"], "Razorpay")

        self.assertEqual(self.fake.create_calls, 1,
                         "a second create was issued despite the claim")
        self.assertEqual(len(self.fake.orders), 1,
                         "a second provider order was created")
        self.assertEqual(_error_code(second), "payment_provider_error")

    def test_crash_after_create_recovers_without_a_second_create(self):
        """6, 7, 8, 9, 10. The exact sequence that produced the wire duplicates.

        create succeeds -> response lost -> first lookup returns zero ->
        bounded re-check -> order appears -> persisted and returned.
        """

        cart, data = self.started()

        self.fake.lose_create_response = True
        self.fake.lookup_delay_calls = 1          # invisible on the first look

        response = self.pay(data["token"], "Razorpay")

        self.assertIsNone(_error_code(response),
                          f"recovery did not converge: {response}")
        self.assertEqual(self.fake.create_calls, 1,
                         "recovery issued another create")
        self.assertEqual(len(self.fake.orders), 1,
                         "a second provider order was created")
        self.assertTrue(self._sleeps, "no backoff between recovery attempts")

        created_id = next(iter(self.fake.orders))
        self.assertEqual(response["data"]["order_id"], created_id)
        self.assertEqual(
            self.pr_row(data["payment_request"],
                        "custom_razorpay_order_id").custom_razorpay_order_id,
            created_id, "recovered order id was not persisted")

    def test_invisible_order_never_triggers_another_create(self):
        """6 (the rule). An empty listing is not proof of absence."""

        cart, data = self.started()

        self.fake.lose_create_response = True
        self.fake.lookup_delay_calls = 99

        first = self.pay(data["token"], "Razorpay")
        second = self.pay(data["token"], "Razorpay")
        third = self.pay(data["token"], "Razorpay")

        for response in (first, second, third):
            self.assertEqual(_error_code(response), "payment_provider_error")
            self.assertIs(_details(response)["retryable"], True)
            self.assertTrue(_details(response)["sales_order"])

        self.assertEqual(self.fake.create_calls, 1,
                         "repeated retries created more provider orders")
        self.assertEqual(len(self.fake.orders), 1)

        # Once it becomes visible, the same retry recovers it.
        self.fake.lookup_delay_calls = 0
        recovered = self.pay(data["token"], "Razorpay")

        self.assertIsNone(_error_code(recovered), f"unexpected: {recovered}")
        self.assertEqual(self.fake.create_calls, 1)

    def test_canonical_id_always_wins(self):
        """11. Once stored, the canonical order is used and never re-derived."""

        cart, data = self.started()
        first = self.pay(data["token"], "Razorpay")
        canonical = first["data"]["order_id"]

        # A duplicate appears at the provider sharing the same receipt.
        receipt = self.fake.orders[canonical]["receipt"]
        self.fake.orders["order_DUP01"] = {
            "id": "order_DUP01", "receipt": receipt,
            "amount": self.fake.orders[canonical]["amount"],
            "currency": "INR", "status": "created", "attempts": 0,
            "amount_paid": None, "created_at": 1_600_000_000,   # OLDER
        }

        second = self.pay(data["token"], "Razorpay")

        self.assertEqual(second["data"]["order_id"], canonical,
                         "an older duplicate displaced the canonical order")

    def test_multiple_unattempted_duplicates_resolve_deterministically(self):
        """12. All untouched -> oldest is canonical, stably."""

        from yob_storefront.integrations.gateways.razorpay_gateway import (
            RazorpayGateway,
        )
        from yob_storefront.integrations.razorpay import client as rz

        cart, data = self.started()
        pr = frappe.get_doc("Payment Request", data["payment_request"])
        receipt = rz.receipt_for_payment_request(pr.name)
        minor = int(round(float(pr.grand_total) * 100))

        for oid, created in (("order_NEW", 200), ("order_OLD", 100)):
            self.fake.orders[oid] = {
                "id": oid, "receipt": receipt, "amount": minor,
                "currency": "INR", "status": "created", "attempts": 0,
                "amount_paid": None, "created_at": created,
            }

        obligation = Obligation(payment_request=pr, sales_order=None)

        with self.fake.install():
            chosen = RazorpayGateway()._resolve_receipt_matches(obligation, receipt)

        self.assertEqual(chosen["id"], "order_OLD")

    def test_multiple_attempted_duplicates_fail_closed(self):
        """13. Never settle the wrong paid order because it happened to be older."""

        from yob_storefront.integrations.gateways.base import ProviderIntegrityError
        from yob_storefront.integrations.gateways.razorpay_gateway import (
            RazorpayGateway,
        )
        from yob_storefront.integrations.razorpay import client as rz

        cart, data = self.started()
        pr = frappe.get_doc("Payment Request", data["payment_request"])
        receipt = rz.receipt_for_payment_request(pr.name)
        minor = int(round(float(pr.grand_total) * 100))

        for oid, created in (("order_PAID_A", 100), ("order_PAID_B", 200)):
            self.fake.orders[oid] = {
                "id": oid, "receipt": receipt, "amount": minor,
                "currency": "INR", "status": "paid", "attempts": 1,
                "amount_paid": minor, "created_at": created,
            }

        obligation = Obligation(payment_request=pr, sales_order=None)

        with self.fake.install():
            with self.assertRaises(ProviderIntegrityError):
                RazorpayGateway()._resolve_receipt_matches(obligation, receipt)

    def test_settlement_only_accepts_the_canonical_order(self):
        """14. A payment against a duplicate must not settle this obligation."""

        cart, data = self.started()
        initiation = self.pay(data["token"], "Razorpay")
        canonical = initiation["data"]["order_id"]

        # A duplicate order, paid. It is NOT this Payment Request's canonical
        # order, so settlement must not resolve to this obligation.
        receipt = self.fake.orders[canonical]["receipt"]
        self.fake.orders["order_DUP02"] = {
            "id": "order_DUP02", "receipt": receipt,
            "amount": self.fake.orders[canonical]["amount"],
            "currency": "INR", "status": "created", "attempts": 0,
            "amount_paid": None, "created_at": 1_900_000_000,
        }
        rogue_payment = self.fake.pay("order_DUP02")

        response = self.verify("order_DUP02", rogue_payment)

        self.assertEqual(_error_code(response), "payment_reference_invalid",
                         "a duplicate order settled the obligation")
        self.assertNotEqual(
            self.pr_row(data["payment_request"], "status").status, "Paid")

    def test_pay_later_unaffected_by_the_claim(self):
        """15. Internal methods never touch provider claim state."""

        cart, data = self.started()

        response = self.pay(data["token"], "Pay Later")

        self.assertIsNone(_error_code(response), f"unexpected: {response}")
        self.assertIsNone(self.claim_at(data["payment_request"]),
                          "Pay Later recorded a provider creation claim")

    def test_published_contract_unchanged_by_hardening(self):
        """16."""

        cart, data = self.started()

        response = self.pay(data["token"], "Razorpay")

        self.assertEqual(
            set(response["data"]),
            {"payment_method", "razorpay_key", "order_id", "amount",
             "currency", "sales_order", "payment_request"})
