# Copyright (c) 2026, YOB and Shayona
"""Provider Foundation Phase A -- the gateway seam, proven inert.

Phase A introduces an abstraction and routes the existing Razorpay
implementation through it. The whole point is that NOTHING observable changes,
so most of these tests assert absence of change; the rest pin the new
architectural rules so a later phase cannot quietly violate them:

    Payment Method -> Payment Gateway -> registry -> YOBGateway -> adapter

    Frappe Payments  = gateway configuration + credentials
    YOB              = commercial lifecycle, and provider capabilities Payments
                       does not offer
    Integration Request = provider transport/audit only, never financial truth
"""

import unittest
from unittest.mock import patch

import frappe

from yob_storefront.integrations.gateways import registry
from yob_storefront.integrations.gateways.base import (
    CAP_RECOVER,
    CAP_SERVER_VERIFY,
    Obligation,
    ProviderNotConfigured,
    UnsupportedProvider,
)
from yob_storefront.integrations.gateways.razorpay_gateway import RazorpayGateway
from yob_storefront.tests.test_payment_cutover import CutoverCase
from yob_storefront.tests.test_payment_lifecycle import _error_code


# =========================================================
# 1-4. MAPPING, REGISTRY, DISPATCH
# =========================================================

class GatewayMappingCase(unittest.TestCase):

    def test_razorpay_method_resolves_through_the_gateway_link(self):
        """1. Dispatch is by Payment Gateway, not by method_code."""

        method = frappe.get_doc("Payment Method", "Razorpay")

        self.assertEqual(method.payment_gateway, "Razorpay",
                         "migration did not map the Razorpay method")

        gateway = registry.resolve_gateway(method)

        self.assertIsInstance(gateway, RazorpayGateway)
        self.assertEqual(gateway.provider, "Razorpay")

    def test_pay_later_has_no_external_gateway(self):
        """2. Internal YOB method: no provider, and that is not a defect."""

        method = frappe.get_doc("Payment Method", "Pay Later")

        self.assertFalse(method.payment_gateway,
                         "Pay Later must not be linked to an external gateway")
        self.assertIsNone(registry.resolve_gateway(method))

    def test_registry_resolves_by_provider_name(self):
        """3."""

        self.assertIn("Razorpay", registry.registered_providers())
        self.assertIsInstance(registry.get_driver("Razorpay"), RazorpayGateway)

    def test_unknown_gateway_fails_closed(self):
        """4. Never fall through to 'internal' -- that would take no payment."""

        with self.assertRaises(UnsupportedProvider):
            registry.get_driver("NoSuchProvider")

        method = frappe.get_doc("Payment Method", "Razorpay")
        method.payment_gateway = "Pay Later"        # a Payment Method, not a gateway

        with self.assertRaises(UnsupportedProvider):
            registry.resolve_gateway(method)

    def test_dispatch_does_not_branch_on_method_code(self):
        """The chain this phase exists to prevent must not have grown back."""

        from yob_storefront.api import payment
        from yob_storefront.tests.test_payment_lifecycle import _code_only

        source = _code_only(payment.process_payment)

        self.assertNotIn("method_code", source,
                         "process_payment still dispatches on method_code")
        self.assertNotIn("razorpay", source.lower(),
                         "process_payment still names a provider")


# =========================================================
# 5-6. FRAPPE PAYMENTS AS THE CONFIGURATION FOUNDATION
# =========================================================

class PaymentsFoundationCase(unittest.TestCase):

    def setUp(self):
        self.gateway = RazorpayGateway()

    def test_driver_reaches_the_installed_payments_controller(self):
        """5. Configuration comes from Frappe Payments, via its own resolver."""

        from payments.utils.utils import get_payment_gateway_controller

        expected = get_payment_gateway_controller("Razorpay")
        controller = self.gateway.controller()

        self.assertEqual(controller.doctype, expected.doctype)
        self.assertEqual(controller.doctype, "Razorpay Settings")
        # The capability Payments genuinely adds beyond YOB's adapter.
        self.assertTrue(hasattr(controller, "validate_transaction_currency"))
        self.assertIn("INR", controller.supported_currencies)

    def test_credentials_come_only_from_razorpay_settings(self):
        """6. One source. YOB stores no copy of any key or secret."""

        self.assertEqual(
            self.gateway.public_key(),
            frappe.db.get_single_value("Razorpay Settings", "api_key") or "")

        # No YOB DocType may carry provider credentials.
        for doctype in ("Payment Method", "Payment Method Assignment",
                        "YOB Store Settings"):
            fields = {f.fieldname for f in frappe.get_meta(doctype).fields}
            leaked = {f for f in fields
                      if any(w in f for w in ("api_key", "api_secret",
                                              "secret_key", "publishable"))}
            self.assertEqual(leaked, set(),
                             f"{doctype} carries provider credentials: {leaked}")

    def test_driver_advertises_the_capabilities_payments_lacks(self):
        caps = self.gateway.capabilities()

        self.assertIn(CAP_RECOVER, caps)
        self.assertIn(CAP_SERVER_VERIFY, caps)

    def test_unconfigured_gateway_is_reported_not_guessed(self):
        with patch.object(RazorpayGateway, "public_key", return_value=""):
            with self.assertRaises(ProviderNotConfigured):
                self.gateway.assert_configured()

    def test_hosted_checkout_is_not_used(self):
        """Frappe's server-rendered flow is intentionally never invoked.

        YOB has its own SPA. Calling any of these would redirect the buyer away
        from it and hand payment state to Integration Request.
        """

        import inspect

        from yob_storefront.api import payment
        from yob_storefront.integrations.gateways import razorpay_gateway

        for module in (payment, razorpay_gateway):
            source = inspect.getsource(module)
            for forbidden in ("get_payment_url", "authorize_payment",
                              "create_request(", "razorpay_checkout",
                              "payment-success"):
                self.assertNotIn(
                    f".{forbidden}" if forbidden.endswith("(") else forbidden,
                    source.replace("``get_payment_url``", "")
                          .replace("``create_request``", "")
                          .replace("``authorize_payment``", "")
                          .replace("``*_checkout``", ""),
                    f"{module.__name__} references hosted checkout: {forbidden}")


# =========================================================
# 7-8. AUTHORITY RULES
# =========================================================

class AuthorityCase(CutoverCase):
    """Payment Request + Sales Order are authoritative. Nothing else is."""

    def test_integration_request_is_not_consulted_for_financial_truth(self):
        """8. Provider transport/audit only.

        Phase A created none at all. Since B2 delegated order creation to
        Frappe Payments, Integration Requests ARE produced -- so the original
        "creates none" assertion was true only of the SDK path and has been
        replaced by the one that actually matters and survives: no code
        deciding what is owed ever reads one.
        """

        cart, data = self.started()
        pr_before = self.pr_row(data["payment_request"], "grand_total", "currency")

        response = self.pay(data["token"], "Razorpay")

        self.assertIsNone(_error_code(response), f"unexpected: {response}")

        # Audit records may now exist; they change nothing authoritative.
        self.assertEqual(
            dict(pr_before),
            dict(self.pr_row(data["payment_request"], "grand_total", "currency")),
            "provider audit changed the obligation")

        from yob_storefront.api import payment
        from yob_storefront.services import commitment_service, payment_service
        from yob_storefront.tests.test_payment_lifecycle import _code_only

        for fn in (payment.process_payment, payment.process_gateway_payment,
                   commitment_service.ensure_payment_request_committed,
                   payment_service.process_success_payment):
            self.assertNotIn("Integration Request", _code_only(fn),
                             f"{fn.__name__} consults Integration Request")

    def test_obligation_amount_comes_from_the_payment_request(self):
        """7. Not from the Cart, and not from the Sales Order."""

        cart, data = self.started()
        pr = frappe.get_doc("Payment Request", data["payment_request"])
        so = frappe.get_doc("Sales Order", {"customer": pr.party}, "name") \
            if False else None

        obligation = Obligation(payment_request=pr, sales_order=so)

        self.assertEqual(obligation.amount_minor,
                         int(round(float(pr.grand_total) * 100)))
        self.assertEqual(obligation.currency, pr.currency)
        self.assertEqual(obligation.reference, pr.name)


# =========================================================
# 9-12. BEHAVIOUR PARITY
# =========================================================

class ParityCase(CutoverCase):
    """The refactor must be invisible from outside."""

    RAZORPAY_KEYS = {"payment_method", "razorpay_key", "order_id", "amount",
                     "currency", "sales_order", "payment_request"}

    def test_razorpay_process_payment_response_is_unchanged(self):
        """9. The exact published Phase 2B / Angular Phase 13 contract."""

        cart, data = self.started()
        pr = self.pr_row(data["payment_request"], "grand_total", "currency")

        response = self.pay(data["token"], "Razorpay")

        self.assertIsNone(_error_code(response), f"unexpected: {response}")
        body = response["data"]

        self.assertEqual(set(body), self.RAZORPAY_KEYS)
        self.assertEqual(body["payment_method"], "razorpay")
        self.assertEqual(body["razorpay_key"], "rzp_test_placeholder")
        self.assertEqual(body["amount"], int(round(float(pr.grand_total) * 100)))
        self.assertEqual(body["currency"], pr.currency)
        self.assertTrue(body["order_id"])
        self.assertTrue(body["sales_order"])

    def test_razorpay_verification_response_is_unchanged(self):
        """10. Settlement is untouched by this phase."""

        cart, data = self.started()
        initiation = self.pay(data["token"], "Razorpay")
        order_id = initiation["data"]["order_id"]
        payment_id = self.fake.pay(order_id)

        response = self.verify(order_id, payment_id)

        self.assertIsNone(_error_code(response), f"unexpected: {response}")
        self.assertEqual(set(response["data"]),
                         {"sales_order", "payment_request", "payment_id"})

    def test_pay_later_is_unchanged(self):
        """12. Routed by the absence of a gateway, behaving exactly as before."""

        cart, data = self.started()

        response = self.pay(data["token"], "Pay Later")

        self.assertIsNone(_error_code(response), f"unexpected: {response}")
        self.assertEqual(
            set(response["data"]),
            {"payment_method", "sales_order", "payment_request", "amount",
             "currency", "payment_status"})
        self.assertEqual(response["data"]["payment_method"], "paylater")
        self.assertEqual(response["data"]["payment_status"], "Unpaid")

    def test_provider_not_configured_answers_before_any_provider_call(self):
        """An unconfigured gateway is refused, and nothing is committed.

        This assertion was INVERTED by Phase B1. In Phase A the commitment ran
        before the configuration check, so an unconfigured gateway still left a
        Draft Sales Order; that was flagged as a defect and B1 moved the check
        into preflight, ahead of commitment. The obligation therefore stays
        Cart-backed here.

        The detailed preflight coverage lives in test_gateway_preflight; this
        keeps the Phase A parity suite honest about current behaviour.
        """

        cart, data = self.started()
        so_before = frappe.db.count("Sales Order")

        self.unconfigure_gateway()

        response = self.pay(data["token"], "Razorpay")

        self.assertEqual(_error_code(response), "payment_provider_not_configured")
        self.assertEqual(self.commits, [],
                         "committed durably before the configuration check")
        self.assertEqual(len(self.fake.orders), 0,
                         "contacted the provider despite missing credentials")
        self.assertEqual(frappe.db.count("Sales Order"), so_before,
                         "an unstartable payment committed an order")
        self.assertEqual(
            self.pr_row(data["payment_request"], "reference_doctype").reference_doctype,
            "Cart")

    def test_recovery_still_converges_through_the_driver(self):
        """11. The guarantee Frappe Payments lacks, still held after the seam."""

        cart, data = self.started()

        first = self.pay(data["token"], "Razorpay")

        frappe.db.set_value("Payment Request", data["payment_request"],
                            "custom_razorpay_order_id", None)
        frappe.clear_document_cache("Payment Request", data["payment_request"])

        second = self.pay(data["token"], "Razorpay")

        self.assertEqual(second["data"]["order_id"], first["data"]["order_id"])
        self.assertEqual(len(self.fake.orders), 1,
                         "recovery created a second provider order")

    def test_driver_recover_payment_finds_the_existing_order(self):
        """The recovery capability, exercised directly on the driver."""

        cart, data = self.started()
        initiation = self.pay(data["token"], "Razorpay")

        pr = frappe.get_doc("Payment Request", data["payment_request"])
        so = frappe.get_doc("Sales Order", initiation["data"]["sales_order"])
        obligation = Obligation(payment_request=pr, sales_order=so)

        with self.fake.install():
            intent = RazorpayGateway().recover_payment(obligation)

        self.assertIsNotNone(intent, "deterministic receipt did not recover")
        self.assertEqual(intent.provider_reference, initiation["data"]["order_id"])
        self.assertTrue(intent.reused)
        self.assertEqual(intent.client_sdk, "razorpay-checkout-v1")
        self.assertEqual(set(intent.client_payload), {"key", "order_id"})


if __name__ == "__main__":
    unittest.main()
