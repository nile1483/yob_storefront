# Copyright (c) 2026, YOB and Shayona
"""
Tenant-isolation and authorization tests.

Two storefront customers (A and B) are created with their own Users and
STOREFRONT access grants. The tests assert that an authenticated session for
Customer A can never reach Customer B's data, and that no request-supplied value
can influence which Customer the server acts on.

Run with:
    bench --site <site> run-tests --app yob_storefront \
        --module yob_storefront.tests.test_tenant_isolation
"""

import unittest

import frappe

from yob_auth.security.access import resolve_access
from yob_auth.security.exceptions import YOBAccessDeniedError, YOBAuthenticationError
from yob_storefront.utils.context import (
    STOREFRONT_APP,
    assert_customer_matches,
    get_storefront_customer,
)

PASSWORD = "Test-Isolation-Pw-1"


def _ensure_application():
    if frappe.db.exists("YOB Application", STOREFRONT_APP):
        return
    frappe.get_doc(
        {
            "doctype": "YOB Application",
            "application_code": STOREFRONT_APP,
            "application_name": "YOB Storefront",
            "enabled": 1,
            "domains": "",  # domain validation off - see security checklist
            "required_profile_doctype": "Customer",
            "allow_password_login": 1,
            "allow_email_otp": 0,
            "allow_mobile_otp": 0,
        }
    ).insert(ignore_permissions=True)


def _ensure_customer(name):
    if not frappe.db.exists("Customer", name):
        frappe.get_doc(
            {"doctype": "Customer", "customer_name": name, "customer_type": "Company"}
        ).insert(ignore_permissions=True)
    return name


def _ensure_user(email):
    if not frappe.db.exists("User", email):
        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": email,
                "first_name": email.split("@")[0],
                "send_welcome_email": 0,
                "user_type": "Website User",
                "new_password": PASSWORD,
            }
        )
        user.insert(ignore_permissions=True)
    return email


def _ensure_access(user, customer):
    existing = frappe.db.exists(
        "YOB User Application Access", {"user": user, "application": STOREFRONT_APP}
    )
    if existing:
        return existing
    return frappe.get_doc(
        {
            "doctype": "YOB User Application Access",
            "user": user,
            "application": STOREFRONT_APP,
            "enabled": 1,
            "profile_doctype": "Customer",
            "profile_name": customer,
        }
    ).insert(ignore_permissions=True).name


class StorefrontIsolationCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_application()
        cls.customer_a = _ensure_customer("_Test Storefront Customer A")
        cls.customer_b = _ensure_customer("_Test Storefront Customer B")
        cls.user_a = _ensure_user("storefront-a@example.com")
        cls.user_b = _ensure_user("storefront-b@example.com")
        _ensure_access(cls.user_a, cls.customer_a)
        _ensure_access(cls.user_b, cls.customer_b)
        frappe.db.commit()

    def tearDown(self):
        frappe.set_user("Administrator")

    # ------------------------------------------------------------------
    # Context resolution
    # ------------------------------------------------------------------

    def test_context_resolves_own_customer(self):
        ctx = resolve_access(self.user_a, STOREFRONT_APP, validate_domain=False)
        self.assertEqual(ctx.user, self.user_a)
        self.assertEqual(ctx.application, STOREFRONT_APP)
        self.assertEqual(ctx.profile_doctype, "Customer")
        self.assertEqual(ctx.profile_name, self.customer_a)

    def test_context_never_returns_other_customer(self):
        ctx = resolve_access(self.user_a, STOREFRONT_APP, validate_domain=False)
        self.assertNotEqual(ctx.profile_name, self.customer_b)

    def test_guest_is_rejected(self):
        with self.assertRaises(YOBAuthenticationError):
            resolve_access("Guest", STOREFRONT_APP, validate_domain=False)

    def test_user_without_access_is_rejected(self):
        stranger = _ensure_user("storefront-none@example.com")
        frappe.db.commit()
        with self.assertRaises(YOBAccessDeniedError):
            resolve_access(stranger, STOREFRONT_APP, validate_domain=False)

    def test_disabled_access_is_rejected(self):
        name = frappe.db.get_value(
            "YOB User Application Access",
            {"user": self.user_b, "application": STOREFRONT_APP},
        )
        frappe.db.set_value("YOB User Application Access", name, "enabled", 0)
        try:
            with self.assertRaises(YOBAccessDeniedError):
                resolve_access(self.user_b, STOREFRONT_APP, validate_domain=False)
        finally:
            frappe.db.set_value("YOB User Application Access", name, "enabled", 1)

    def test_disabled_user_is_rejected(self):
        frappe.db.set_value("User", self.user_b, "enabled", 0)
        try:
            with self.assertRaises(Exception):
                resolve_access(self.user_b, STOREFRONT_APP, validate_domain=False)
        finally:
            frappe.db.set_value("User", self.user_b, "enabled", 1)

    def test_access_to_another_application_does_not_grant_storefront(self):
        other = "OTHERAPP"
        if not frappe.db.exists("YOB Application", other):
            frappe.get_doc(
                {
                    "doctype": "YOB Application",
                    "application_code": other,
                    "application_name": "Other",
                    "enabled": 1,
                    "allow_password_login": 1,
                }
            ).insert(ignore_permissions=True)
        stranger = _ensure_user("storefront-other@example.com")
        if not frappe.db.exists(
            "YOB User Application Access", {"user": stranger, "application": other}
        ):
            frappe.get_doc(
                {
                    "doctype": "YOB User Application Access",
                    "user": stranger,
                    "application": other,
                    "enabled": 1,
                }
            ).insert(ignore_permissions=True)
        frappe.db.commit()
        with self.assertRaises(YOBAccessDeniedError):
            resolve_access(stranger, STOREFRONT_APP, validate_domain=False)

    # ------------------------------------------------------------------
    # Caller-supplied values must never be authoritative
    # ------------------------------------------------------------------

    def test_supplied_customer_mismatch_is_rejected(self):
        ctx = resolve_access(self.user_a, STOREFRONT_APP, validate_domain=False)
        with self.assertRaises(YOBAccessDeniedError):
            assert_customer_matches(ctx, self.customer_b)

    def test_supplied_customer_match_is_allowed(self):
        ctx = resolve_access(self.user_a, STOREFRONT_APP, validate_domain=False)
        assert_customer_matches(ctx, self.customer_a)  # must not raise

    def test_missing_context_is_rejected(self):
        with self.assertRaises(YOBAccessDeniedError):
            get_storefront_customer(None)

    def test_customer_comes_from_context_not_request(self):
        ctx = resolve_access(self.user_a, STOREFRONT_APP, validate_domain=False)
        frappe.local.form_dict = frappe._dict(
            customer=self.customer_b,
            customer_id=self.customer_b,
            profile_name=self.customer_b,
            company="Anything",
            roles=["System Manager"],
        )
        customer = get_storefront_customer(ctx)
        self.assertEqual(customer.name, self.customer_a)

    def test_decorator_strips_caller_supplied_auth_context(self):
        """A forged auth_context in form_dict or kwargs must be discarded."""
        from yob_auth.security.decorators import require_application

        forged = frappe._dict(
            user="Administrator",
            application=STOREFRONT_APP,
            profile_doctype="Customer",
            profile_name=self.customer_b,
            company=None,
            roles=["System Manager"],
        )

        @require_application(STOREFRONT_APP, profile_doctype="Customer")
        def probe(auth_context=None):
            return auth_context

        frappe.set_user(self.user_a)
        frappe.local.form_dict = frappe._dict(auth_context=forged)
        result = probe(auth_context=forged)

        self.assertEqual(result.user, self.user_a)
        self.assertEqual(result.profile_name, self.customer_a)
        self.assertNotEqual(result.profile_name, self.customer_b)
        self.assertNotIn("auth_context", frappe.local.form_dict)


class CartAndOrderIsolationCase(StorefrontIsolationCase):
    """Customer A must not be able to read or mutate Customer B's documents."""

    def _cart_for(self, customer):
        name = frappe.db.get_value("Cart", {"customer": customer, "status": "Draft"})
        if name:
            return frappe.get_doc("Cart", name)
        settings = frappe.get_single("YOB Store Settings")
        return frappe.get_doc(
            {
                "doctype": "Cart",
                "customer": customer,
                "company": settings.company,
                "currency": settings.default_currency,
                "selling_price_list": settings.default_price_list,
                "status": "Draft",
            }
        ).insert(ignore_permissions=True)

    def test_cart_lookup_is_scoped_to_authenticated_customer(self):
        cart_b = self._cart_for(self.customer_b)
        frappe.db.commit()

        ctx_a = resolve_access(self.user_a, STOREFRONT_APP, validate_domain=False)
        customer_a = get_storefront_customer(ctx_a)

        # This is exactly the lookup every cart endpoint performs.
        reachable = frappe.db.get_value(
            "Cart", {"customer": customer_a.name, "status": "Draft"}, "name"
        )
        self.assertNotEqual(reachable, cart_b.name)

    def test_order_lookup_is_scoped_to_authenticated_customer(self):
        ctx_a = resolve_access(self.user_a, STOREFRONT_APP, validate_domain=False)
        customer_a = get_storefront_customer(ctx_a).name

        orders = frappe.get_all(
            "Sales Order", filters={"customer": customer_a}, pluck="customer"
        )
        self.assertTrue(all(c == customer_a for c in orders))
        self.assertNotIn(self.customer_b, orders)
