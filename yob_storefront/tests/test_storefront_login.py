# Copyright (c) 2026, YOB and Shayona
"""Logging in to the STOREFRONT application, end to end.

WHY THIS LIVES IN yob_storefront
--------------------------------
`yob_auth` owns the login endpoints and `yob_auth.tests.test_login_endpoint`
covers their mechanics against a throwaway application. It cannot cover THIS,
because the claim being tested is storefront-shaped: that a real password login
to `STOREFRONT` resolves to the right **Customer**, and that what the buyer gets
is a Website User with no ERPNext access. `Customer` is an ERPNext DocType and
`STOREFRONT` is this app's registration, so a test asserting either belongs to
the app that declares both -- `yob_auth` must never require a business app.

The request scaffolding is imported from `yob_auth.tests.auth_harness` rather
than copied: `yob_storefront` already declares `yob_auth`, and two divergent
copies of a login harness would be worse than one shared one. See that module
for why these fixtures are committed instead of transaction-scoped.
"""

import unittest

import frappe

from yob_auth.api import auth
from yob_auth.security.access import get_current_context
from yob_auth.tests import auth_harness as harness
from yob_storefront.permissions.storefront_role import STOREFRONT_BUYER_ROLE
from yob_storefront.utils.context import STOREFRONT_APP, get_storefront_customer

CUSTOMER = "_Test Storefront Login Customer"
BUYER = "_test-sf-login@yob.invalid"
OFF_BUYER = "_test-sf-login-off@yob.invalid"
ALT_APP = "_TESTSFALT"

PASSWORD = "Yob-Test-Storefront-9!x"

ALL_USERS = (BUYER, OFF_BUYER)
ACCESS = "YOB User Application Access"


def _user(email):
    if frappe.db.exists("User", email):
        return
    doc = frappe.get_doc({
        "doctype": "User", "email": email, "first_name": "Storefront",
        "last_name": "Login", "send_welcome_email": 0, "new_password": PASSWORD,
    })
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)


def _grant(user, enabled):
    if frappe.db.exists(ACCESS, {"user": user, "application": STOREFRONT_APP}):
        return
    frappe.get_doc({
        "doctype": ACCESS, "user": user, "application": STOREFRONT_APP,
        "enabled": enabled, "profile_doctype": "Customer",
        "profile_name": CUSTOMER,
    }).insert(ignore_permissions=True)


def _ensure_fixtures():
    """Create whatever is missing. Called from `setUp`, never committed.

    WHY NOT `setUpClass` + commit -- THE BUG THIS REPLACES
    ------------------------------------------------------
    The first version built this once per class and committed it, following the
    convention in `yob_auth.tests.test_domain_enforcement`. In a FULL-APP run
    that commit was not durable, so the first test's `tearDown` rollback took
    the whole fixture with it. The module then did not fail cleanly: the first
    login passed, six tests failed -- and four passed for the WRONG REASON.
    "Logging in to another application is refused" passed because the other
    application had vanished too (a missing application is refused with the
    same code), and "the grant made them a buyer" passed off a stale role
    cache.

    So the fixture is rebuilt per test and never relies on a commit surviving:
    a rollback simply means the next `setUp` creates it again. `setUp` then
    ASSERTS the fixture is really present, which is what turns a vanished
    fixture into a loud failure instead of a vacuous pass.
    """

    if not frappe.db.exists("Customer", CUSTOMER):
        frappe.get_doc({"doctype": "Customer", "customer_name": CUSTOMER}).insert(
            ignore_permissions=True)

    if not frappe.db.exists("YOB Application", ALT_APP):
        frappe.get_doc({
            "doctype": "YOB Application", "application_code": ALT_APP,
            "application_name": "Storefront Login Alt", "enabled": 1,
            "domains": "", "allow_password_login": 1, "allow_email_otp": 0,
            "allow_mobile_otp": 0,
        }).insert(ignore_permissions=True)

    for email in ALL_USERS:
        _user(email)

    # The buyer role is NOT assigned here: the `YOB User Application Access`
    # hooks grant it, and one of the tests below asserts exactly that.
    _grant(BUYER, enabled=1)
    _grant(OFF_BUYER, enabled=0)


def _destroy_fixtures():
    frappe.set_user("Administrator")
    harness.purge_auth_traces(*ALL_USERS)
    for email in ALL_USERS:
        for name in frappe.get_all(ACCESS, filters={"user": email}, pluck="name"):
            frappe.delete_doc(ACCESS, name, force=True, ignore_permissions=True)
        if frappe.db.exists("User", email):
            frappe.delete_doc("User", email, force=True, ignore_permissions=True)
    if frappe.db.exists("YOB Application", ALT_APP):
        frappe.delete_doc("YOB Application", ALT_APP, force=True,
                          ignore_permissions=True)
    if frappe.db.exists("Customer", CUSTOMER):
        frappe.delete_doc("Customer", CUSTOMER, force=True, ignore_permissions=True)


def _trusted_headers() -> dict:
    """The proxy header this site's STOREFRONT configuration requires, if any.

    `install._ensure_storefront_application` ships `domains` EMPTY, because the
    allowed host is site-specific -- but a configured site (like this bench) has
    one, and then `get_application()` fails closed without the header. Reading
    the stored value keeps this module correct on both, instead of writing site
    configuration from a test.
    """

    hosts = [line.strip() for line
             in (frappe.db.get_value("YOB Application", STOREFRONT_APP, "domains") or "").splitlines()
             if line.strip()]
    return {"X-YOB-Original-Host": hosts[0]} if hosts else {}


class StorefrontLoginCase(unittest.TestCase):
    """A real Website User, a real password, the real STOREFRONT application."""

    @classmethod
    def setUpClass(cls):
        frappe.set_user("Administrator")
        if not frappe.db.exists("YOB Application", STOREFRONT_APP):
            raise unittest.SkipTest(
                "the STOREFRONT application is not installed on this site")
        # A clean slate: an interrupted earlier run can have committed these.
        _destroy_fixtures()

    @classmethod
    def tearDownClass(cls):
        # Best effort: a successful login commits a Frappe session, which can
        # carry the fixture with it, so removal cannot rely on a rollback.
        _destroy_fixtures()
        frappe.db.commit()
        frappe.clear_cache()

    def setUp(self):
        frappe.set_user("Administrator")
        # clear_cache BEFORE the fixture as well as after: a stale `get_roles`
        # entry from an earlier test is exactly what let a vanished fixture
        # produce a passing assertion.
        frappe.clear_cache()
        _ensure_fixtures()
        self.assertTrue(
            frappe.db.exists("User", BUYER),
            "the fixture user is missing, so the assertions in this module "
            "would pass vacuously")
        self.assertTrue(frappe.db.exists("YOB Application", ALT_APP))
        self.assertTrue(frappe.db.exists(
            ACCESS, {"user": BUYER, "application": STOREFRONT_APP, "enabled": 1}))

        self.locals = harness.snapshot_locals()
        self.addCleanup(harness.restore_locals, self.locals)
        self.addCleanup(frappe.clear_cache)
        self.addCleanup(frappe.db.rollback)
        harness.install_guest_request(_trusted_headers())
        harness.clear_password_limits(*ALL_USERS)

    def login(self, username=BUYER, password=PASSWORD, application=STOREFRONT_APP):
        return auth.login_with_password(
            application=application, username=username, password=password)

    # ------------------------------------------------------------------ success

    def test_a_buyer_can_log_in_to_the_storefront(self):
        response = self.login()

        self.assertNotIn("errors", response, f"login failed: {response}")
        self.assertEqual(response["data"]["context"]["application"], STOREFRONT_APP)
        self.assertEqual(frappe.session.user, BUYER)

    def test_the_context_resolves_the_buyers_own_customer(self):
        context = self.login()["data"]["context"]

        self.assertEqual(context["profile_doctype"], "Customer")
        self.assertEqual(context["profile_name"], CUSTOMER)

    def test_the_live_session_resolves_the_same_customer_the_storefront_serves(self):
        """From the session the login created, through the storefront adapter.

        This is the join the login response alone cannot prove: that a later
        request in that session re-derives the same Customer from server state.
        """

        self.login()

        customer = get_storefront_customer(get_current_context(STOREFRONT_APP))

        self.assertEqual(customer.name, CUSTOMER)
        self.assertEqual(customer.doctype, "Customer")

    def test_the_session_endpoint_agrees_with_the_login_response(self):
        login_context = self.login()["data"]["context"]

        session_response = auth.get_session_context(application=STOREFRONT_APP)

        self.assertNotIn("errors", session_response, f"{session_response}")
        self.assertEqual(session_response["data"]["context"], login_context)
        self.assertEqual(session_response["data"]["authentication_method"], "session")

    # --------------------------------------------------------- what a buyer is

    def test_the_grant_alone_made_the_buyer_a_storefront_buyer(self):
        """The role comes from the access hooks, never from the login path."""

        self.assertTrue(
            frappe.db.exists("Has Role", {"parent": BUYER,
                                          "role": STOREFRONT_BUYER_ROLE}),
            "the enabled grant did not attach the buyer role")
        self.assertIn(STOREFRONT_BUYER_ROLE, frappe.get_roles(BUYER))

    def test_logging_in_grants_no_erpnext_access(self):
        self.login()

        self.assertEqual(frappe.db.get_value("User", BUYER, "user_type"),
                         "Website User")
        # The same list `test_proceed_to_payment_buyer` asserts after a real
        # Proceed. `Address` and `Contact` are deliberately NOT here: Frappe
        # grants every logged-in user `read` on both and restricts them per
        # record instead, so asserting otherwise would be asserting a falsehood
        # rather than a boundary.
        for doctype, ptype in (("Sales Order", "read"), ("Sales Order", "write"),
                               ("Payment Request", "read"),
                               ("Payment Request", "write"),
                               ("Payment Request", "create"),
                               ("Customer", "read"), ("Customer", "select"),
                               ("Company", "read"), ("Company", "select"),
                               ("Account", "read")):
            with self.subTest(permission=f"{doctype}.{ptype}"):
                self.assertFalse(frappe.has_permission(doctype, ptype),
                                 f"the buyer gained {doctype} {ptype} by logging in")

    def test_the_buyer_keeps_the_catalogue_read_the_role_exists_for(self):
        """The counterpart: the role must still grant what pricing needs."""

        self.login()

        self.assertTrue(frappe.has_permission("Item", "read"),
                        "the buyer cannot read Item, so pricing will fail")

    # ----------------------------------------------------------------- refusals

    def test_logging_in_to_another_application_is_refused(self):
        # Guard: a MISSING application is refused with this same code, so the
        # application has to exist for the refusal to mean "no grant for it".
        self.assertEqual(
            frappe.db.get_value("YOB Application", ALT_APP, "enabled"), 1)
        self.assertFalse(frappe.db.exists(ACCESS, {"user": BUYER,
                                                   "application": ALT_APP}))

        response = self.login(application=ALT_APP)

        self.assertEqual(response["errors"][0]["code"], "application_access_denied")
        self.assertEqual(harness.status(), 403)
        self.assertEqual(frappe.session.user, "Guest")

    def test_a_disabled_grant_cannot_log_in(self):
        response = self.login(username=OFF_BUYER)

        self.assertEqual(response["errors"][0]["code"], "application_access_denied")
        self.assertEqual(harness.status(), 403)
        self.assertEqual(frappe.session.user, "Guest")

    def test_a_disabled_grant_also_holds_no_buyer_role(self):
        # Guard: `get_roles` of a user who does not exist answers Guest's roles,
        # which would satisfy the assertion below for the wrong reason.
        self.assertTrue(frappe.db.exists("User", OFF_BUYER))
        self.assertEqual(
            frappe.db.get_value(ACCESS, {"user": OFF_BUYER,
                                         "application": STOREFRONT_APP},
                                "enabled"),
            0)

        self.assertNotIn(STOREFRONT_BUYER_ROLE, frappe.get_roles(OFF_BUYER))

    # -------------------------------------------------- the shipped OTP posture

    def test_the_storefront_requires_a_customer_profile(self):
        """Every grant must resolve to a Customer; the login test relies on it."""

        self.assertEqual(
            frappe.db.get_value("YOB Application", STOREFRONT_APP,
                                "required_profile_doctype"),
            "Customer")

    def test_the_storefront_refuses_otp_while_the_method_is_disabled(self):
        """The shipped posture is password-only. If a site turns OTP on, this
        test is expected to be revisited deliberately rather than silently."""

        allowed = frappe.db.get_value("YOB Application", STOREFRONT_APP,
                                      "allow_email_otp")
        if allowed:
            self.skipTest("this site has deliberately enabled email OTP for "
                          "STOREFRONT, so refusing it is no longer the posture")

        response = auth.request_otp(application=STOREFRONT_APP, identifier=BUYER,
                                    method="email_otp")

        self.assertEqual(response["errors"][0]["code"], "application_access_denied")
        self.assertEqual(harness.status(), 403)


if __name__ == "__main__":
    unittest.main()
