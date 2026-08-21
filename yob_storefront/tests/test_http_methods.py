# Copyright (c) 2026, YOB and Shayona
"""HTTP-method contract tests (CHG-002 F-04).

Frappe enforces `methods=` on `frappe.whitelist` before the endpoint body runs,
and CSRF-checks only unsafe verbs. Declaring the verb is therefore what closes
the mutating-GET hole: a state-changing endpoint reachable by GET is reachable
with no CSRF token at all.

These assert the DECLARED contract, which is what the future Angular client must
follow. They read Frappe's own registry rather than re-parsing source, so a
declaration that Frappe does not actually honour would fail here.
"""

import importlib
import pkgutil
import unittest

import frappe

READ_ONLY = {
    "address.get_contacts", "address.get_addresses", "address.get_contact_for_customer",
    "cart.get_cart", "catalog.get_categories", "catalog.get_category", "catalog.get_item",
    # Bounded catalog listing (Phase 22B-1). A read: it prices into a throwaway
    # in-memory Sales Order that is never inserted, exactly as get_category does.
    "catalog.get_items",
    # Variant resolution (Phase 24B). A read: it resolves a selection through
    # ERPNext and prices into a throwaway in-memory Sales Order, exactly as
    # get_item does. It stores nothing.
    "catalog.resolve_variant",
    "cms.get_config", "order.get_orders", "order.get_order_details",
    # Storefront runtime reads (Phase 25C). `get_page` prices any Product Grid
    # through the existing catalogue service into throwaway in-memory Sales
    # Orders, exactly as `get_items` does; nothing is stored.
    "catalog.get_category_filters", "cms.get_menu", "cms.get_page",
    "payment.get_checkout_data", "payment_method.get_payment_methods",
}

MUTATING = {
    "address.add_contact", "address.update_contact", "address.delete_contact",
    "address.add_address",
    "address.update_address", "address.delete_address",
    "cart.add_to_cart", "cart.remove_from_cart", "cart.clear_cart",
    "cart.set_cart_contact", "cart.set_cart_billing_address",
    "cart.set_cart_shipping_address", "cart.apply_coupon", "cart.remove_coupon",
    "checkout.proceed_to_payment", "payment.verify_payment", "payment.process_payment",
}

GUEST = {"payment.get_checkout_data", "payment.verify_payment", "payment.process_payment"}


def _declared_methods():
    """Map ``module.function`` -> allowed HTTP methods, from Frappe's registry."""

    found = {}
    pkg = importlib.import_module("yob_storefront.api")
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        mod = importlib.import_module(f"yob_storefront.api.{mod_info.name}")
        for name, fn in vars(mod).items():
            if not callable(fn) or not getattr(fn, "__module__", "").startswith("yob_storefront"):
                continue
            if fn not in frappe.whitelisted:
                continue
            found[f"{mod_info.name}.{name}"] = frappe.allowed_http_methods_for_whitelisted_func.get(fn)
    return found


class TestDeclaredHTTPMethods(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.declared = _declared_methods()

    def test_every_endpoint_declares_a_method(self):
        """An undeclared endpoint accepts every verb, including mutating GET."""

        undeclared = sorted(k for k, v in self.declared.items() if not v)
        self.assertEqual(undeclared, [], f"endpoints without methods=: {undeclared}")

    def test_read_only_endpoints_accept_get(self):
        for key in sorted(READ_ONLY):
            with self.subTest(endpoint=key):
                self.assertIn(key, self.declared, f"{key} is not whitelisted")
                self.assertIn("GET", self.declared[key])

    def test_mutating_endpoints_accept_post(self):
        for key in sorted(MUTATING):
            with self.subTest(endpoint=key):
                self.assertIn(key, self.declared, f"{key} is not whitelisted")
                self.assertIn("POST", self.declared[key])

    def test_mutating_endpoints_reject_get(self):
        """The whole point of F-04: no state change without CSRF."""

        for key in sorted(MUTATING):
            with self.subTest(endpoint=key):
                self.assertNotIn(
                    "GET", self.declared[key],
                    f"{key} still accepts GET, so it is reachable without a CSRF token",
                )

    def test_unsupported_methods_are_rejected(self):
        """No endpoint may accept PUT/DELETE/PATCH/HEAD."""

        for key, methods in sorted(self.declared.items()):
            with self.subTest(endpoint=key):
                for verb in ("PUT", "DELETE", "PATCH", "HEAD"):
                    self.assertNotIn(verb, methods, f"{key} accepts {verb}")

    def test_read_only_endpoints_do_not_accept_post(self):
        for key in sorted(READ_ONLY):
            with self.subTest(endpoint=key):
                self.assertNotIn("POST", self.declared[key])

    def test_every_endpoint_is_classified(self):
        """Guard against a new endpoint silently escaping this contract."""

        self.assertEqual(
            sorted(self.declared) , sorted(READ_ONLY | MUTATING),
            "an endpoint exists that is in neither READ_ONLY nor MUTATING",
        )


class TestGuestSurfaceUnchanged(unittest.TestCase):
    """The guest surface must stay exactly three payment endpoints."""

    def test_guest_endpoints_are_exactly_the_inventoried_three(self):
        guest = set()
        pkg = importlib.import_module("yob_storefront.api")
        for mod_info in pkgutil.iter_modules(pkg.__path__):
            mod = importlib.import_module(f"yob_storefront.api.{mod_info.name}")
            for name, fn in vars(mod).items():
                if callable(fn) and fn in frappe.whitelisted:
                    if getattr(fn, "__module__", "").startswith("yob_storefront"):
                        if fn in frappe.guest_methods:
                            guest.add(f"{mod_info.name}.{name}")
        self.assertEqual(guest, GUEST)

    def test_guest_mutating_endpoints_are_post_only(self):
        declared = _declared_methods()
        for key in ("payment.verify_payment", "payment.process_payment"):
            with self.subTest(endpoint=key):
                self.assertEqual(list(declared[key]), ["POST"])
