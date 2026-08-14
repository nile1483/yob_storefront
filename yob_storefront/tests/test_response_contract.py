# Copyright (c) 2026, YOB and Shayona
"""Public API response-contract tests for yob_storefront.

Pure static checks: no site data, no fixtures. The envelope helpers themselves
are tested once, in ``yob_core.tests.test_response_envelope`` -- this app
re-exports them rather than owning a second copy.

The scans come from ``yob_core.testing.api_contract``; everything
storefront-specific is configuration, declared below.
"""

import pathlib
import unittest

from yob_core.testing.api_contract import APIContractChecker

from yob_storefront.api import response as storefront_response

APP_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Internal functions that build and return a standard envelope themselves, so an
# endpoint may hand their result straight back. Their own return statements are
# covered by the legacy-shape and traceback scans below.
DELEGATING_HELPERS = {
    "process_success_payment",
    "process_pay_later",
    # Provider dispatch is now gateway-neutral: process_razorpay_payment was
    # replaced by one helper that serves every external gateway.
    "process_gateway_payment",
    # get_checkout_data dispatches on the payment source; each branch builds
    # and returns a full envelope of its own.
    "_cart_checkout",
    "_sales_order_checkout",
    # Account CRUD: turns a Frappe/ERPNext/India-Compliance validation refusal
    # into the standard `validation_failed` envelope, with the framework's
    # message sanitised. Shared by add/update of both Address and Contact so
    # the four endpoints cannot drift apart.
    "validation_error_response",
}

# Whitelisted methods deliberately outside the public storefront contract.
# ``get_contact_for_customer`` is an INTERNAL DESK api called by the Cart form
# Client Script, which consumes the bare contact name; it is authorized with
# standard DocType permissions, not with storefront application access.
CONTRACT_EXEMPT = {"get_contact_for_customer"}

CHECKER = APIContractChecker(
    app_root=APP_ROOT,
    response_module=storefront_response,
    delegating_helpers=DELEGATING_HELPERS,
    contract_exempt=CONTRACT_EXEMPT,
    extra_scan_dirs=("services",),
)


class TestErrorCodes(unittest.TestCase):
    def test_every_visible_code_is_lowercase_snake_case(self):
        offenders = CHECKER.error_code_offenders()
        self.assertEqual(
            offenders,
            [],
            "error codes must be lowercase snake_case:\n" + "\n".join(offenders),
        )

    def test_error_codes_are_declared_constants_not_inline_literals(self):
        """``error_response("...")`` must name a constant, so codes stay stable."""

        offenders = CHECKER.inline_error_code_offenders()
        self.assertEqual(
            offenders, [], "inline error-code literals:\n" + "\n".join(offenders)
        )

    def test_storefront_codes_keep_their_published_values(self):
        """A spot-check that the rename-free guarantee actually held."""

        self.assertEqual(storefront_response.CART_EMPTY, "cart_empty")
        self.assertEqual(storefront_response.COUPON_INVALID, "coupon_invalid")
        self.assertEqual(storefront_response.ORDER_NOT_FOUND, "order_not_found")
        self.assertEqual(
            storefront_response.PAYMENT_AMOUNT_MISMATCH, "payment_amount_mismatch"
        )

    def test_shared_codes_remain_importable_from_here(self):
        self.assertEqual(storefront_response.VALIDATION_FAILED, "validation_failed")
        self.assertEqual(storefront_response.INTERNAL_SERVER_ERROR, "internal_server_error")
        self.assertEqual(
            storefront_response.APPLICATION_ACCESS_DENIED, "application_access_denied"
        )
        self.assertEqual(storefront_response.HTTP_NOT_FOUND, 404)


class TestEndpointsUseTheContract(unittest.TestCase):
    def test_every_whitelisted_endpoint_returns_a_standard_envelope(self):
        offenders = CHECKER.envelope_offenders()
        self.assertEqual(
            offenders,
            [],
            "endpoints must answer through the response helpers:\n" + "\n".join(offenders),
        )

    def test_no_module_returns_the_legacy_shape(self):
        offenders = CHECKER.legacy_shape_offenders()
        self.assertEqual(
            offenders, [], "legacy response shape survives:\n" + "\n".join(offenders)
        )

    def test_no_module_can_leak_a_traceback(self):
        offenders = CHECKER.traceback_leak_offenders()
        self.assertEqual(
            offenders, [], "tracebacks must never be returned:\n" + "\n".join(offenders)
        )

    def test_unexpected_failures_are_not_disguised_as_business_errors(self):
        """A bare ``except Exception`` must answer 500, i.e. go through server_error."""

        offenders = CHECKER.disguised_failure_offenders()
        self.assertEqual(
            offenders,
            [],
            "generic exception handlers must return server_error (500):\n" + "\n".join(offenders),
        )
