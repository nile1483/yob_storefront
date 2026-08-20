# Copyright (c) 2026, YOB and Shayona
"""Public API response-contract tests for yob_storefront.

Pure static checks: no site data, no fixtures. The envelope helpers themselves
are tested once, in ``yob_core.tests.test_response_envelope`` -- this app
re-exports them rather than owning a second copy.

The scans come from ``yob_core.testing.api_contract``; everything
storefront-specific is configuration, declared below.
"""

import pathlib
import json
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
    # get_item dispatches on what the slug addresses: a simple Item is priced
    # inline, a variant FAMILY answers with its matrix. The family branch builds
    # and returns a full envelope of its own (Phase 24B).
    "_family_response",
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


class TestPublishedApiReference(unittest.TestCase):
    """The reference package must describe the API that actually exists.

    The smallest guard that would have caught the real drift: `catalog.get_items`
    shipped in Phase 22B and `catalog.resolve_variant` in Phase 24B, and neither
    appeared in `openapi.json` until Phase 24D-1 went looking. A published
    endpoint nobody documented is a contract a client has to reverse-engineer.

    Deliberately shallow -- presence, not shape. Validating every field against a
    schema would be a documentation framework; this is a checklist that fails the
    moment a new endpoint or error code is added without publishing it.
    """

    HANDOFF = APP_ROOT.parent / "frontend-api-handoff"

    def openapi(self):
        path = self.HANDOFF / "openapi.json"
        if not path.exists():
            self.skipTest("no published OpenAPI document in this checkout")
        return json.loads(path.read_text())

    def whitelisted_endpoints(self):
        import importlib
        import pkgutil

        import frappe

        import yob_storefront.api as api_pkg

        found = set()
        for module_info in pkgutil.iter_modules(api_pkg.__path__):
            module = importlib.import_module(f"yob_storefront.api.{module_info.name}")
            for name, obj in vars(module).items():
                if not callable(obj) or getattr(obj, "__module__", None) != module.__name__:
                    continue
                if obj in frappe.whitelisted:
                    found.add(f"yob_storefront.api.{module_info.name}.{name}")
        return found

    def test_every_whitelisted_endpoint_is_published(self):
        documented = {path.rsplit("/", 1)[-1] for path in self.openapi()["paths"]}
        missing = sorted(self.whitelisted_endpoints() - documented)

        self.assertEqual(
            missing, [],
            "endpoints exist in production but not in frontend-api-handoff/openapi.json:\n"
            + "\n".join(missing))

    def test_the_reference_documents_no_endpoint_that_vanished(self):
        documented = {path.rsplit("/", 1)[-1] for path in self.openapi()["paths"]
                      if ".api.yob_storefront" in path or "yob_storefront.api." in path}
        stale = sorted(documented - self.whitelisted_endpoints())

        self.assertEqual(stale, [],
                         "the reference still documents endpoints that no longer exist:\n"
                         + "\n".join(stale))

    def test_every_storefront_error_code_is_published(self):
        path = self.HANDOFF / "ERROR-CODES.md"
        if not path.exists():
            self.skipTest("no published error-code reference in this checkout")

        published = path.read_text()
        codes = {value for name, value in vars(storefront_response).items()
                 if name.isupper() and isinstance(value, str) and value.islower()}

        missing = sorted(code for code in codes if f"`{code}`" not in published)

        self.assertEqual(missing, [],
                         "error codes returned by production but absent from "
                         "frontend-api-handoff/ERROR-CODES.md:\n" + "\n".join(missing))


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
