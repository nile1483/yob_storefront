# Copyright (c) 2026, YOB and Shayona
"""Public payment authorization: the token is the credential, not the session.

`/payment/<token>` is intentionally public. A payer may arrive from a shared
link, an email or WhatsApp, with no storefront login at all -- so they are
Frappe's `Guest` user, who has no ERPNext Customer, Contact, Address or Sales
Order permission and must never be granted any.

The bug these tests pin: Cart -> Sales Order commitment ran ERPNext party
resolution under the SESSION user's permissions. `SellingController.
set_missing_lead_customer_details` forwards `self.flags.ignore_permissions` into
`_get_party_details`, which calls `frappe.has_permission(party_type, ...,
throw=True)`. Without the flag a Guest payer raised PermissionError on Customer
during `set_missing_values()` -- BEFORE `insert()`, which is why
`insert(ignore_permissions=True)` alone did not fix it.

The question the system must answer is not

    "may Guest read Customer?"

but

    "does this validated token authorize payment of this exact Payment Request,
     whose trusted source authorizes exactly this Sales Order?"

These tests prove the second question is answered strictly, and that answering
it does not turn the token into a general session.
"""

import unittest
from unittest.mock import patch

import frappe

from yob_storefront.tests.test_payment_cutover import CutoverCase
from yob_storefront.tests.test_payment_lifecycle import _error_code


class GuestCommitmentCase(CutoverCase):
    """The central regression: commitment must succeed as Guest."""

    def as_guest(self):
        """Switch to the real Guest user for the duration of a test.

        Not a mock. `frappe.set_user("Guest")` is the actual public-request
        identity, and Guest's permissions are whatever the site grants -- which
        the first test below asserts is nothing relevant.
        """

        original = frappe.session.user
        frappe.set_user("Guest")
        self.addCleanup(frappe.set_user, original)

    def test_guest_has_no_erpnext_permissions(self):
        """The precondition. If this ever fails, every test below is vacuous.

        Guest must NOT be granted these to make payment work -- that would be
        exactly the wrong fix.
        """

        self.as_guest()

        self.assertEqual(frappe.session.user, "Guest")

        for doctype, ptype in (("Customer", "read"), ("Contact", "read"),
                               ("Address", "read"), ("Sales Order", "create"),
                               ("Payment Request", "read")):
            self.assertFalse(
                frappe.has_permission(doctype, ptype, user="Guest"),
                f"Guest has {ptype} on {doctype}; the public payment fix must "
                f"not depend on granting this")

    def test_commitment_succeeds_as_guest(self):
        """THE regression. Valid token + valid source + Guest -> committed."""

        from yob_storefront.services.commitment_service import (
            ensure_payment_request_committed,
        )

        cart, data = self.started()          # set up as the authenticated buyer
        so_before = frappe.db.count("Sales Order")

        self.as_guest()                      # now behave like a shared link

        result = ensure_payment_request_committed(token=data["token"])

        self.assertFalse(_error_code(result), f"unexpected error: {result}")
        self.assertTrue(result["created"])
        # Since Phase 32C the attempt already owns its Sales Order, so a
        # correct commitment adds none -- it closes the Cart against it.
        self.assertEqual(frappe.db.count("Sales Order"), so_before,
                         "commitment created a second Sales Order")
        self.assertEqual(result["sales_order"].docstatus, 0)

    def test_trusted_path_succeeds_where_guest_permissions_would_not(self):
        """Both gates cleared, without Guest holding either permission.

        Reproducing the literal pre-fix traceback after the fix is impractical,
        so this asserts the equivalent pair the spec allows: Guest genuinely
        lacks the permissions, and the authorized commitment path still
        completes. Gate 1 (Customer/party) is covered by the Sales Order's own
        flag; Gate 2 (Item, and then Account) by the trusted identity.
        """

        from yob_storefront.services.order_service import create_sales_order_from_cart

        cart, data = self.started()
        cart.reload()

        self.as_guest()

        self.assertFalse(frappe.has_permission("Customer", "read", user="Guest"))
        self.assertFalse(frappe.has_permission("Item", "read", user="Guest"))

        from yob_storefront.services.payment_request_service import trusted_execution

        with trusted_execution():
            so = create_sales_order_from_cart(cart)

        self.assertTrue(so.name)
        self.assertEqual(so.customer, cart.customer)
        # And the privileged identity did not survive the block.
        self.assertEqual(frappe.session.user, "Guest")

    def test_public_checkout_data_works_without_a_session(self):
        """The payment page itself must load for Guest."""

        cart, data = self.started()

        self.as_guest()

        response = self.checkout(data["token"])

        self.assertIsNone(_error_code(response), f"unexpected: {response}")
        self.assertEqual(response["data"]["source_doctype"], "Cart")
        self.assertTrue(response["data"]["payment_methods"])

    def test_full_public_payment_as_guest(self):
        """End to end through the real endpoint, as Guest."""

        cart, data = self.started()

        self.as_guest()

        response = self.pay(data["token"], "Razorpay")

        self.assertIsNone(_error_code(response), f"unexpected: {response}")
        self.assertEqual(
            set(response["data"]),
            {"payment_method", "razorpay_key", "order_id", "amount",
             "currency", "sales_order", "payment_request"})

    def test_pay_later_as_guest(self):
        cart, data = self.started()

        self.as_guest()

        response = self.pay(data["token"], "Pay Later")

        self.assertIsNone(_error_code(response), f"unexpected: {response}")
        self.assertEqual(response["data"]["payment_status"], "Unpaid")


class TokenIsNotASessionCase(CutoverCase):
    """The bypass must not leak into a general privilege."""

    def as_guest(self):
        original = frappe.session.user
        frappe.set_user("Guest")
        self.addCleanup(frappe.set_user, original)

    def test_token_does_not_grant_generic_doctype_access(self):
        """A valid token authorizes ONE payment flow, not a session.

        After a successful public payment, Guest must still be unable to read
        Customers, Addresses, Contacts or Sales Orders generally.
        """

        cart, data = self.started()

        self.as_guest()
        self.pay(data["token"], "Razorpay")

        for doctype, ptype in (("Customer", "read"), ("Address", "read"),
                               ("Contact", "read"), ("Sales Order", "read")):
            self.assertFalse(
                frappe.has_permission(doctype, ptype, user="Guest"),
                f"paying granted Guest {ptype} on {doctype}")

    def test_invalid_token_commits_nothing(self):
        """No token, no authorization, no Sales Order, no provider call."""

        self.started()
        so_before = frappe.db.count("Sales Order")

        self.as_guest()

        for bad in (None, "", "   ", "not-a-real-token"):
            response = self.pay(bad, "Razorpay")

            self.assertEqual(_error_code(response), "checkout_token_invalid",
                             f"token {bad!r} was not rejected")

        self.assertEqual(frappe.db.count("Sales Order"), so_before)
        self.assertEqual(len(self.fake.orders), 0)

    def test_caller_cannot_rebind_the_source(self):
        """process_payment accepts ONLY token and payment_method.

        The Cart is read from the Payment Request's own reference. There is no
        parameter through which a caller could name a different Cart, Sales
        Order, customer, amount, currency, address or contact -- asserted from
        the signature so a future parameter cannot be added silently.
        """

        import inspect

        from yob_storefront.api import payment
        from yob_storefront.tests.test_payment_lifecycle import _code_only, _raw

        params = set(inspect.signature(_raw(payment.process_payment)).parameters)

        self.assertEqual(params, {"token", "payment_method"})

        # And the Cart identity comes from the obligation, not from anywhere else.
        from yob_storefront.services import commitment_service

        source = _code_only(commitment_service.ensure_payment_request_committed)
        # The Cart identity is still read off the obligation, from `custom_cart`
        # since Phase 32C -- `reference_name` now names the attempt's Sales Order.
        # What must never appear is a cart taken from anywhere the caller controls.
        self.assertIn("cart_name = pr.custom_cart", source)

    def test_committed_obligation_is_reused_not_duplicated(self):
        """Refreshing a public payment link must not create a second order."""

        cart, data = self.started()

        self.as_guest()

        first = self.pay(data["token"], "Razorpay")
        so_after = frappe.db.count("Sales Order")
        second = self.pay(data["token"], "Razorpay")

        self.assertEqual(second["data"]["sales_order"],
                         first["data"]["sales_order"])
        self.assertEqual(frappe.db.count("Sales Order"), so_after)
        self.assertEqual(len(self.fake.orders), 1)

    def test_stale_source_still_refused_for_guest(self):
        """The bypass must not weaken the financial invariant."""

        cart, data = self.started()

        cart.reload()
        cart.items[0].quantity = 20
        self.reprice(cart)

        so_before = frappe.db.count("Sales Order")

        self.as_guest()

        response = self.pay(data["token"], "Razorpay")

        self.assertEqual(_error_code(response), "payment_request_stale")
        self.assertEqual(frappe.db.count("Sales Order"), so_before)

    def test_financial_parity_holds_under_guest_commitment(self):
        """ignore_permissions bypasses permissions, never validation."""

        from yob_storefront.services.commitment_service import (
            ensure_payment_request_committed,
        )

        cart, data = self.started()
        pr = self.pr_row(data["payment_request"], "grand_total", "currency")

        self.as_guest()

        result = ensure_payment_request_committed(token=data["token"])
        so = result["sales_order"]

        self.assertAlmostEqual(float(so.grand_total), float(pr.grand_total),
                               places=2)
        self.assertEqual(so.currency, pr.currency)
        self.assertEqual(so.customer, cart.customer)
        self.assertEqual(so.contact_person, cart.contact_person)
        self.assertEqual(so.customer_address, cart.billing_address)
        self.assertEqual(so.shipping_address_name, cart.shipping_address)
        self.assertEqual(len(so.items), len(cart.items))

    def test_rollback_still_clean_under_guest(self):
        """A failed commitment as Guest must leave nothing behind."""

        from yob_storefront.services.commitment_service import (
            ensure_payment_request_committed,
        )

        cart, data = self.started()
        so_before = frappe.db.count("Sales Order")

        # The trigger moved with the conversion. Before Phase 32C a disabled
        # Customer failed INSIDE the commitment, because the commitment built
        # the Sales Order; it builds none now, so that no longer fails here.
        # Destroying the attempt's snapshot is a real commitment-time failure
        # and tests the same thing: a Guest-driven failure must leave the Cart
        # open and the obligation uncommitted.
        issued = self.pr_row(data["payment_request"], "reference_name").reference_name
        frappe.delete_doc("Sales Order", issued, ignore_permissions=True, force=True)

        self.as_guest()

        with self.assertRaises(frappe.ValidationError):
            ensure_payment_request_committed(token=data["token"])

        self.assertEqual(frappe.db.count("Sales Order"), so_before - 1,
                         "the failed commitment created or removed an order of its own")
        self.assertEqual(frappe.db.get_value("Cart", cart.name, "status"), "Draft")
        self.assertIsNone(
            self.pr_row(data["payment_request"], "custom_committed_at").custom_committed_at,
            "the obligation was committed despite the refusal")


if __name__ == "__main__":
    unittest.main()


class ProcessorIdentityCase(unittest.TestCase):
    """The internal identity: minimal, disabled, and not spreading.

    Fresh-install provisioning only -- there are no deployed sites yet, so there
    is deliberately no migration/upgrade patch for this identity.
    """

    ROLE = "YOB Payment Processor"
    USER = "payment-processor@yob.internal"

    def test_service_user_is_disabled(self):
        """It must not be able to authenticate interactively.

        Verified on this Frappe version: a disabled user keeps its roles when
        entered through the trusted execution boundary, so `enabled = 0` costs
        nothing and removes the login surface entirely.
        """

        self.assertTrue(frappe.db.exists("User", self.USER))
        self.assertEqual(frappe.db.get_value("User", self.USER, "enabled"), 0)

    def test_service_user_holds_only_the_processor_role(self):
        self.assertEqual(
            frappe.get_all("Has Role", {"parent": self.USER}, pluck="role"),
            [self.ROLE])

    def test_role_has_no_desk_access(self):
        self.assertEqual(frappe.db.get_value("Role", self.ROLE, "desk_access"), 0)

    def test_permissions_are_exactly_the_proven_minimum(self):
        """The proven minimum. Anything more must be justified by a test.

        Each entry was added only after a failing test proved the commitment
        path needs it:

            Item      read    `get_item_details` loads its own cached Item and
                              calls `item.check_permission()`.
            Account   read    ERPNext tax/party resolution reads the tax Account.
            Sales Taxes and Charges Template
                      SELECT  Phase 32J. ERPNext 16.52.1's
                              `require_permission(..., "select")` is reached
                              through India Compliance's regional address hook on
                              a GST-REGISTERED company, and the commitment runs
                              as this identity -- so the buyer's own Phase 32B
                              grant does nothing for it and Proceed to Payment
                              answered PermissionError for every real buyer.
                              `select` is the whole gate; `read` would hand this
                              identity the tax master for no reason, and the
                              assertion below pins that distinction.

        `select` is now collected. It previously was NOT, so a `select` grant on
        ANY DocType was invisible to this guard -- the one permission type the
        storefront actually needs to hand out was the one it could not see.
        """

        granted = {}

        for row in frappe.get_all(
            "Custom DocPerm", filters={"role": self.ROLE},
            fields=["parent", "read", "select", "write", "create", "delete",
                    "export", "report", "share", "print", "email", "permlevel"],
        ):
            granted[row.parent] = {
                k: v for k, v in row.items()
                if k not in ("parent", "name", "permlevel") and v
            }

        self.assertEqual(
            granted,
            {
                "Item": {"read": 1},
                "Account": {"read": 1},
                "Sales Taxes and Charges Template": {"select": 1},
            },
            f"processor role permissions drifted: {granted}")

    def test_the_tax_template_grant_is_select_and_not_read(self):
        """Pinned separately, because the two are easy to confuse.

        A `read` grant would satisfy the same ERPNext gate while giving this
        identity the entire tax master, so the narrowness is the point rather
        than an implementation detail.
        """

        row = frappe.db.get_value(
            "Custom DocPerm",
            {"role": self.ROLE, "parent": "Sales Taxes and Charges Template"},
            ["read", "select", "export", "write"], as_dict=True)

        self.assertEqual(row, {"read": 0, "select": 1, "export": 0, "write": 0})

    def test_party_doctypes_are_not_granted(self):
        """Covered by the Sales Order's own flag; must stay ungranted."""

        for doctype in ("Customer", "Address", "Contact", "Sales Order",
                        "Payment Request"):
            self.assertFalse(
                frappe.db.exists("Custom DocPerm",
                                 {"parent": doctype, "role": self.ROLE}),
                f"{doctype} was granted to the processor role without proof")

    def test_role_is_not_held_by_guest_or_storefront_users(self):
        """The internal identity must be the ONLY holder."""

        holders = frappe.get_all("Has Role", {"role": self.ROLE}, pluck="parent")

        self.assertEqual(holders, [self.USER])
        self.assertNotIn("Guest", holders)

    def test_existing_permissions_were_not_displaced(self):
        """setup_custom_perms first, or Frappe REPLACES the whole perm set.

        That mistake once destroyed every standard Item role on this bench, so
        the count is pinned: the processor role is additive.
        """

        self.assertGreater(frappe.db.count("Custom DocPerm", {"parent": "Item"}), 1)
        self.assertGreater(frappe.db.count("Custom DocPerm", {"parent": "Account"}), 1)

    def test_provisioning_is_idempotent(self):
        """Running fresh-install setup again must not duplicate anything."""

        from yob_storefront.install import ensure_payment_processor_identity

        before = (
            frappe.db.count("User", {"name": self.USER}),
            frappe.db.count("Role", {"name": self.ROLE}),
            frappe.db.count("Custom DocPerm", {"role": self.ROLE}),
            frappe.db.count("Has Role", {"role": self.ROLE}),
        )

        ensure_payment_processor_identity()
        ensure_payment_processor_identity()

        after = (
            frappe.db.count("User", {"name": self.USER}),
            frappe.db.count("Role", {"name": self.ROLE}),
            frappe.db.count("Custom DocPerm", {"role": self.ROLE}),
            frappe.db.count("Has Role", {"role": self.ROLE}),
        )

        self.assertEqual(before, after, "re-running setup duplicated records")


class ContextRestorationCase(CutoverCase):
    """`set_user` clobbers nine request-local values; all must come back."""

    def snapshot(self):
        return (frappe.session.user, frappe.session.sid,
                dict(frappe.session.data or {}), dict(frappe.local.form_dict or {}))

    def test_guest_state_fully_restored_and_no_permission_leak(self):
        """Guest -> processor -> Guest, with Item denied again afterwards."""

        from yob_storefront.services.payment_request_service import trusted_execution

        frappe.set_user("Guest")
        self.addCleanup(frappe.set_user, "Administrator")

        frappe.local.form_dict = frappe._dict({"token": "sentinel"})
        frappe.session.data = frappe._dict({"marker": "guest-data"})
        before = self.snapshot()

        self.assertFalse(frappe.has_permission("Item", "read"),
                         "Guest already had Item read; test is vacuous")

        with trusted_execution():
            self.assertTrue(frappe.has_permission("Item", "read"),
                            "processor cannot read Item inside the boundary")
            self.assertTrue(frappe.has_permission("Account", "read"))

        self.assertEqual(self.snapshot(), before, "request state not restored")
        # The decisive one: no privileged permission cache survived.
        self.assertFalse(frappe.has_permission("Item", "read"),
                         "processor permissions leaked past the boundary")

    def test_authenticated_user_and_exact_sid_restored(self):
        """A real SID must survive -- set_user overwrites it with the username."""

        from yob_storefront.services.payment_request_service import trusted_execution

        buyer = frappe.db.get_value("User", {"name": ["!=", "Administrator"],
                                             "enabled": 1}, "name")
        if not buyer:
            self.skipTest("no enabled non-admin user on this site")

        frappe.set_user(buyer)
        self.addCleanup(frappe.set_user, "Administrator")

        frappe.session.sid = "a-real-looking-sid-value"
        frappe.session.data = frappe._dict({"marker": "authenticated"})
        frappe.local.form_dict = frappe._dict({"payment_method": "Razorpay"})
        before = self.snapshot()

        with trusted_execution():
            pass

        self.assertEqual(frappe.session.user, buyer)
        self.assertEqual(frappe.session.sid, "a-real-looking-sid-value",
                         "SID was replaced by the username")
        self.assertEqual(self.snapshot(), before)

    def test_state_restored_after_an_exception(self):
        """`finally` must run even when the internal operation throws."""

        from yob_storefront.services.payment_request_service import trusted_execution

        frappe.set_user("Guest")
        self.addCleanup(frappe.set_user, "Administrator")

        frappe.local.form_dict = frappe._dict({"token": "sentinel"})
        before = self.snapshot()

        with self.assertRaises(RuntimeError):
            with trusted_execution():
                raise RuntimeError("internal failure")

        self.assertEqual(self.snapshot(), before,
                         "state not restored after an exception")
        self.assertFalse(frappe.has_permission("Item", "read"))


# =========================================================================
# PHASE 32C -- document-specific Sales Order authorization (DocShare)
# =========================================================================

class AttemptDocShareCase(CutoverCase):
    """The grant that lets ERPNext validate, and nothing wider.

    ERPNext 16.52.1's `Payment Request.validate()` calls
    `ref_doc.check_permission("read")` on the referenced Sales Order. The
    processor identity deliberately holds NO Sales Order role permission, so the
    insert would be refused outright. A document-specific DocShare satisfies
    exactly that check for exactly that order.

    The alternative -- granting `Sales Order: read` to the role -- would let the
    processor read every order on the site, including non-storefront ERP orders.
    These tests are what keep the narrow option honest: if the grant ever widens,
    `test_processor_cannot_read_an_unrelated_sales_order` fails first.
    """

    PROCESSOR = "payment-processor@yob.internal"

    def _attempt(self):
        """Issue one attempt and return (cart, payment request row)."""

        cart = self.make_cart(qty=12)
        data = self.assert_created(self.proceed())
        row = self.pr_row(data["payment_request"], "reference_name", "custom_cart")
        return cart, data, row

    def _shares(self, sales_order):
        return frappe.get_all(
            "DocShare",
            filters={"share_doctype": "Sales Order", "share_name": sales_order,
                     "user": self.PROCESSOR},
            fields=["name", "read", "write", "share", "submit", "everyone"])

    # ------------------------------------------------------------- the grant

    def test_issuing_an_attempt_shares_exactly_its_own_order(self):
        _, _, row = self._attempt()

        shares = self._shares(row.reference_name)

        self.assertEqual(len(shares), 1,
                         "an attempt must create exactly one share for its order")

    def test_the_share_is_read_only(self):
        """Every right spelled out: a default that widens upstream must fail here."""

        _, _, row = self._attempt()
        share = self._shares(row.reference_name)[0]

        self.assertEqual(share.read, 1)
        for right in ("write", "share", "submit", "everyone"):
            with self.subTest(right=right):
                self.assertEqual(share[right], 0,
                                 f"the processor was granted `{right}` on a Sales Order")

    def test_processor_can_read_the_attempt_order(self):
        _, _, row = self._attempt()

        original = frappe.session.user
        try:
            frappe.set_user(self.PROCESSOR)
            self.assertTrue(
                frappe.has_permission("Sales Order", "read", doc=row.reference_name),
                "ERPNext could not validate the Payment Request against its own order")
        finally:
            frappe.set_user(original)

    def test_processor_cannot_read_an_unrelated_sales_order(self):
        """THE test that keeps this narrower than a role grant."""

        _, _, first = self._attempt()

        # A second, independent attempt. Its order is unrelated to the first.
        self.make_cart(qty=10)
        second_data = self.assert_created(self.proceed())
        second = self.pr_row(second_data["payment_request"], "reference_name")

        self.assertNotEqual(first.reference_name, second.reference_name,
                            "fixture failed: both attempts share one order")

        # Remove the second order's share so it stands for "an order this
        # processor was never authorised for" -- an ERP order, or another
        # customer's. Role-level read would still expose it; a share does not.
        for share in self._shares(second.reference_name):
            frappe.delete_doc("DocShare", share.name, ignore_permissions=True, force=True)

        original = frappe.session.user
        try:
            frappe.set_user(self.PROCESSOR)
            self.assertFalse(
                frappe.has_permission("Sales Order", "read", doc=second.reference_name),
                "the processor read a Sales Order it was never shared")
            with self.assertRaises(frappe.PermissionError):
                frappe.get_doc("Sales Order", second.reference_name).check_permission("read")
        finally:
            frappe.set_user(original)

    # --------------------------------------------------------- who does NOT

    def test_the_buyer_gains_no_sales_order_access(self):
        """The share authorises the internal processor, never the payer."""

        _, _, row = self._attempt()

        self.assertEqual(
            frappe.get_all("DocShare",
                           filters={"share_doctype": "Sales Order",
                                    "share_name": row.reference_name},
                           pluck="user"),
            [self.PROCESSOR],
            "the attempt order was shared with somebody other than the processor")

    def test_guest_gains_no_sales_order_access(self):
        _, _, row = self._attempt()

        original = frappe.session.user
        try:
            frappe.set_user("Guest")
            self.assertFalse(
                frappe.has_permission("Sales Order", "read", doc=row.reference_name),
                "holding the public token granted Guest read on the order")
        finally:
            frappe.set_user(original)

    def test_no_role_level_sales_order_permission_was_added(self):
        """Option E, not Option A. A role grant would make the share pointless."""

        self.assertFalse(
            frappe.db.exists("Custom DocPerm",
                             {"role": "YOB Payment Processor", "parent": "Sales Order"}),
            "a role-level Sales Order permission appeared; the document-specific "
            "share is no longer the boundary")

    # ------------------------------------------------------------ idempotent

    def test_repeated_proceed_on_an_unchanged_cart_adds_no_second_share(self):
        """An unchanged cart reuses the attempt, so it reuses the one share."""

        _, _, row = self._attempt()

        self.proceed()
        self.proceed()

        self.assertEqual(len(self._shares(row.reference_name)), 1,
                         "repeated Proceed duplicated the DocShare")
