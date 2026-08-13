# Copyright (c) 2026, YOB and Shayona
"""Phase 2B -- the live payment cutover, end to end.

Initiation and settlement now share one authoritative chain:

    token -> immutable Payment Request -> ONE committed Sales Order

and settlement no longer creates anything. These tests exercise the real public
endpoints; only two things are stubbed:

* ``get_storefront_customer`` in the authenticated Proceed helper (identity);
* the Razorpay SDK, via a deterministic fake that enforces the provider's
  documented receipt rules. No real-money calls, no test-mode keys.

``frappe.db.commit`` is patched to a RECORDER rather than executed. That is not
a convenience: a real commit would destroy the savepoint that keeps these tests
isolated, and recording it is also how the durability-boundary ordering is
proven.
"""

import unittest
from unittest.mock import patch

import frappe

from yob_storefront.services.commitment_service import (
    ensure_payment_request_committed,
)
from yob_storefront.tests.test_payment_lifecycle import (
    CUSTOMER,
    LifecycleCase,
    _error_code,
    _raw,
)

COMPANY = "Shayona Technology"


class FakeRazorpay:
    """Deterministic Razorpay, modelling the REAL wire-verified behaviour.

    Corrected after Test-Mode verification falsified the documented contract.
    This fake now reproduces what Razorpay actually does:

    * ``receipt`` is NOT unique -- creating twice with the same receipt SUCCEEDS
      and yields two different order ids;
    * the receipt listing is EVENTUALLY CONSISTENT -- ``lookup_delay_calls``
      makes a just-created order invisible for the first N lookups.

    A fake that protects us from our own bug is worthless. This one permits the
    duplicate, so the tests must prove YOB's durable creation claim is what
    stops it.
    """

    class DuplicateReceipt(Exception):
        """Retained only for legacy assertions; the real API does not raise."""

    class Unreachable(Exception):
        pass

    def __init__(self):
        self.orders = {}
        self.payments = {}
        self.create_calls = 0
        self.fail_create = False
        self.lose_create_response = False
        #: Raw payloads the Frappe Payments controller sent, for units checks.
        self.controller_payloads = []
        #: Number of receipt lookups that return nothing before orders appear,
        #: reproducing the wire-verified propagation delay.
        self.lookup_delay_calls = 0
        self.lookup_calls = 0

    # --- provider surface -------------------------------------------------

    def create(self, payload):
        self.create_calls += 1

        if self.fail_create:
            raise self.Unreachable("network down")

        receipt = payload.get("receipt")
        if receipt and any(o["receipt"] == receipt for o in self.orders.values()):
            raise self.DuplicateReceipt("Order with this receipt already exists")

        oid = f"order_{len(self.orders) + 1:04d}"
        self.orders[oid] = {
            "id": oid, "receipt": receipt, "amount": payload["amount"],
            "currency": payload["currency"], "status": "created",
        }

        if self.lose_create_response:
            # Provider succeeded; our side never saw the answer.
            raise self.Unreachable("connection reset after create")

        return self.orders[oid]

    def all(self, data=None):
        self.lookup_calls += 1

        # Eventual consistency, as observed on the wire: a just-created order
        # is not immediately listable.
        if self.lookup_calls <= self.lookup_delay_calls:
            return {"count": 0, "items": []}

        receipt = (data or {}).get("receipt")
        items = [o for o in self.orders.values()
                 if not receipt or o["receipt"] == receipt]
        return {"count": len(items), "items": items}

    def fetch(self, oid):
        return self.orders[oid]

    def payments_for(self, oid):
        return {"items": [p for p in self.payments.values() if p["order_id"] == oid]}

    def pay(self, oid, status="captured"):
        """Simulate a real captured payment against an order."""

        order = self.orders[oid]
        order["status"] = "paid"
        pid = f"pay_{len(self.payments) + 1:04d}"
        self.payments[pid] = {
            "id": pid, "order_id": oid, "status": status,
            "amount": order["amount"], "currency": order["currency"],
            "method": "card",
        }
        return pid

    def create_via_http(self, data):
        """Order creation as the Frappe Payments controller performs it.

        Since Phase B2 the create leg goes through the Payments controller,
        which calls ``make_post_request`` directly instead of the Razorpay SDK.
        The amount arriving here is ALREADY in minor units, because the
        controller multiplied the business amount by 100 -- so this method also
        witnesses the units contract.
        """

        self.create_calls += 1

        if self.fail_create:
            raise self.Unreachable("network down")

        # NO uniqueness check: the real API accepts a duplicate receipt and
        # returns a NEW order. If YOB calls create twice, two orders exist --
        # which is exactly what the creation claim must prevent.
        receipt = data.get("receipt")

        oid = f"order_{len(self.orders) + 1:04d}"
        self.orders[oid] = {
            "id": oid, "receipt": receipt, "amount": data["amount"],
            "currency": data["currency"], "status": "created",
            "attempts": 0, "amount_paid": None,
            "created_at": 1_700_000_000 + len(self.orders),
        }
        self.controller_payloads.append(dict(data))

        if self.lose_create_response:
            raise self.Unreachable("connection reset after create")

        return self.orders[oid]

    def install(self):
        """Patch the SDK client factory AND the Payments controller's HTTP call.

        Two seams, because Phase B2 split the provider surface: creation goes
        through Frappe Payments (`make_post_request`), while fetch, receipt
        lookup, order-payments and signature verification remain YOB extensions
        on the SDK.
        """

        fake = self

        class _Payment:
            def fetch(self, pid):
                return fake.payments[pid]

        class _Order:
            def create(self, payload):
                return fake.create(payload)

            def all(self, data=None):
                return fake.all(data)

            def fetch(self, oid):
                return fake.fetch(oid)

            def payments(self, oid):
                return fake.payments_for(oid)

        class _Utility:
            def verify_payment_signature(self, params):
                if params.get("razorpay_signature") != "good-signature":
                    import razorpay
                    raise razorpay.errors.SignatureVerificationError("bad signature")

        class _Client:
            order = _Order()
            payment = _Payment()
            utility = _Utility()

        import contextlib

        from yob_storefront.integrations.razorpay import client as rz

        @contextlib.contextmanager
        def _both():
            with patch.object(rz, "get_client", return_value=_Client()), \
                    patch(
                        "payments.payment_gateways.doctype.razorpay_settings."
                        "razorpay_settings.make_post_request",
                        side_effect=lambda url, auth=None, data=None, **kw:
                            fake.create_via_http(data),
                    ):
                yield

        return _both()


class CutoverCase(LifecycleCase):
    """Shared harness: seeded buyer, configured provider, recorded commits."""

    def setUp(self):
        super().setUp()
        self.fake = FakeRazorpay()

        # Configure the provider inside the savepoint. A placeholder value,
        # rolled back in tearDown; no live call is ever made.
        self.configure_gateway()

        # The base class already replaced frappe.db.commit with a recorder.
        # Re-point it so each entry records how many provider orders existed at
        # commit time -- that is what proves the durability boundary came first.
        self._commit_patch.stop()
        self.commits = []
        self._commit_patch = patch.object(
            frappe.db, "commit",
            side_effect=lambda: self.commits.append(len(self.fake.orders)))
        self._commit_patch.start()

    @staticmethod
    def configure_gateway(api_key="rzp_test_placeholder", secret="placeholder-secret"):
        """Configure Razorpay exactly the way a real site stores it.

        Two writes, because Frappe stores a Password field in two places and the
        Payments controller reads BOTH:

        * ``tabSingles.api_secret`` -- a masked placeholder. The controller
          guards order creation with ``if self.api_key and self.api_secret``,
          reading the document field, so an empty one silently returns None
          instead of raising.
        * ``__Auth`` -- the real secret, which ``get_password()`` decrypts.

        Verified against the configured site before being written this way.

        Deliberately NOT ``doc.save()``: ``RazorpaySettings.validate()`` calls
        ``validate_razorpay_credentails()``, which makes a REAL HTTP request to
        api.razorpay.com. Saving placeholder credentials in a unit test would
        hit the network.

        Everything here is inside the test savepoint and rolls back.
        """

        frappe.db.set_single_value("Razorpay Settings", "api_key", api_key)
        frappe.db.set_single_value("Razorpay Settings", "api_secret",
                                   "*" * len(secret))
        frappe.utils.password.set_encrypted_password(
            "Razorpay Settings", "Razorpay Settings", secret, "api_secret")
        frappe.clear_document_cache("Razorpay Settings", "Razorpay Settings")

    @staticmethod
    def unconfigure_gateway():
        """Remove credentials the way an unconfigured site looks."""

        frappe.db.set_single_value("Razorpay Settings", "api_key", "")
        frappe.db.set_single_value("Razorpay Settings", "api_secret", "")
        frappe.db.delete("__Auth", {"doctype": "Razorpay Settings"})
        frappe.clear_document_cache("Razorpay Settings", "Razorpay Settings")

    # ----------------------------------------------------------- helpers

    def checkout(self, token):
        from yob_storefront.api import payment
        return _raw(payment.get_checkout_data)(token=token)

    def pay(self, token, method):
        from yob_storefront.api import payment
        with self.fake.install():
            return _raw(payment.process_payment)(token=token, payment_method=method)

    def verify(self, order_id, payment_id, signature="good-signature"):
        from yob_storefront.api import payment
        with self.fake.install():
            return _raw(payment.verify_payment)(
                razorpay_order_id=order_id,
                razorpay_payment_id=payment_id,
                razorpay_signature=signature,
            )

    def started(self, qty=12):
        """A Cart-backed obligation with a live checkout credential."""

        cart = self.make_cart(qty=qty)
        return cart, self.assert_created(self.proceed())


# =========================================================
# 1-3. PUBLIC CHECKOUT, BOTH SOURCES
# =========================================================

class CheckoutSourceCase(CutoverCase):

    def test_cart_backed_checkout(self):
        """1. Pre-commitment shape is unchanged for the existing SPA."""

        cart, data = self.started()

        response = self.checkout(data["token"])

        self.assertIsNone(_error_code(response), f"unexpected: {response}")
        body = response["data"]

        self.assertEqual(body["source_doctype"], "Cart")
        self.assertEqual(body["source_name"], cart.name)
        self.assertEqual(body["payment_request"], data["payment_request"])
        # The published cart keys must survive the cutover. build_cart_response
        # nests the lines under "cart", which is the shape the SPA already
        # consumes -- asserting a flat "items" here would be asserting a
        # contract that never existed.
        self.assertIn("cart", body)
        self.assertTrue(body["cart"]["items"])
        self.assertIn("contact", body)
        self.assertIn("payment_methods", body)

    def test_sales_order_backed_checkout(self):
        """2. A refresh of /payment/:token after commitment must still work."""

        cart, data = self.started()
        result = ensure_payment_request_committed(token=data["token"])

        response = self.checkout(data["token"])

        self.assertIsNone(_error_code(response), f"unexpected: {response}")
        body = response["data"]

        self.assertEqual(body["source_doctype"], "Sales Order")
        self.assertEqual(body["source_name"], result["sales_order"].name)
        self.assertEqual(body["customer"], CUSTOMER)
        self.assertTrue(body["items"])
        self.assertIn("payment_methods", body)

        # Money still comes from the immutable obligation.
        pr = self.pr_row(data["payment_request"], "grand_total", "currency")
        self.assertAlmostEqual(float(body["amount"]), float(pr.grand_total), places=2)
        self.assertEqual(body["currency"], pr.currency)

    def test_so_backed_checkout_never_consults_a_cart(self):
        """3. The committed order is the source; the Cart is finished.

        A changed Cart -- even one that would be 'stale' pre-commitment -- must
        be irrelevant, and no Cart may be loaded at all.
        """

        cart, data = self.started()
        ensure_payment_request_committed(token=data["token"])

        real_get_doc = frappe.get_doc
        loaded = []

        def spy(doctype, *args, **kwargs):
            if isinstance(doctype, str):
                loaded.append(doctype)
            return real_get_doc(doctype, *args, **kwargs)

        with patch.object(frappe, "get_doc", side_effect=spy):
            response = self.checkout(data["token"])

        self.assertIsNone(_error_code(response))
        self.assertNotIn("Cart", loaded, "an SO-backed checkout loaded a Cart")

    def test_token_survives_the_cart_to_sales_order_transition(self):
        """The credential is NOT revoked merely because the reference moved."""

        cart, data = self.started()
        ensure_payment_request_committed(token=data["token"])

        row = self.pr_row(data["payment_request"],
                          "custom_checkout_token", "custom_checkout_expiry")

        self.assertEqual(row.custom_checkout_token, data["token"])
        self.assertTrue(row.custom_checkout_expiry)


# =========================================================
# 4-5. AUTHORITATIVE ELIGIBILITY
# =========================================================

class EligibilityGateCase(CutoverCase):

    def test_eligible_method_is_accepted(self):
        """4."""

        cart, data = self.started()

        response = self.pay(data["token"], "Pay Later")

        self.assertIsNone(_error_code(response), f"unexpected: {response}")

    def test_ineligible_method_is_rejected_authoritatively(self):
        """5. Not 'it was offered earlier, so it is allowed'."""

        cart, data = self.started()
        so_before = frappe.db.count("Sales Order")

        # Make Pay Later ineligible AFTER the checkout page offered it.
        frappe.db.set_value("Payment Method Assignment", "Pay Later", "is_active", 0)
        frappe.clear_cache()

        response = self.pay(data["token"], "Pay Later")

        self.assertEqual(_error_code(response), "payment_method_unsupported")
        self.assertEqual(frappe.db.count("Sales Order"), so_before,
                         "an ineligible method still committed an order")
        self.assertEqual(
            self.pr_row(data["payment_request"], "reference_doctype").reference_doctype,
            "Cart", "an ineligible method still moved the obligation")

    def test_amount_bound_rejection_uses_the_immutable_obligation(self):
        response_amount = None

        cart, data = self.started()
        pr_total = float(self.pr_row(data["payment_request"], "grand_total").grand_total)

        # A minimum just above the obligation makes the method ineligible.
        frappe.db.set_value("Payment Method Assignment", "Pay Later",
                            "minimum_order_amount", pr_total + 1)
        frappe.clear_cache()

        response = self.pay(data["token"], "Pay Later")

        self.assertEqual(_error_code(response), "payment_method_unsupported")


# =========================================================
# 6-8. PAY LATER
# =========================================================

class PayLaterCase(CutoverCase):

    def test_pay_later_commits_exactly_one_sales_order(self):
        """6."""

        cart, data = self.started()
        so_before = frappe.db.count("Sales Order")

        response = self.pay(data["token"], "Pay Later")

        self.assertIsNone(_error_code(response), f"unexpected: {response}")
        self.assertEqual(frappe.db.count("Sales Order"), so_before + 1)
        self.assertEqual(response["data"]["payment_status"], "Unpaid")

        so = frappe.get_doc("Sales Order", response["data"]["sales_order"])
        self.assertEqual(so.docstatus, 0, "must stay Draft")

    def test_repeated_pay_later_returns_the_same_sales_order(self):
        """7."""

        cart, data = self.started()

        first = self.pay(data["token"], "Pay Later")
        so_after_first = frappe.db.count("Sales Order")
        second = self.pay(data["token"], "Pay Later")

        self.assertIsNone(_error_code(second), f"unexpected: {second}")
        self.assertEqual(second["data"]["sales_order"], first["data"]["sales_order"])
        self.assertEqual(frappe.db.count("Sales Order"), so_after_first,
                         "a repeat created a second Sales Order")

    def test_pay_later_leaves_the_obligation_outstanding(self):
        """8. Choosing 'pay later' is about timing, not payment."""

        cart, data = self.started()

        self.pay(data["token"], "Pay Later")

        row = self.pr_row(data["payment_request"], "status", "docstatus",
                          "custom_checkout_token", "reference_doctype")

        self.assertNotEqual(row.status, "Paid", "Pay Later must not mark Paid")
        self.assertNotEqual(row.status, "Cancelled", "Pay Later must not cancel")
        self.assertEqual(row.reference_doctype, "Sales Order")
        # Credential stays usable so a future "Pay Now" can use the same link.
        self.assertEqual(row.custom_checkout_token, data["token"])

    def test_pay_later_contains_no_cart_to_sales_order_conversion(self):
        """27. The duplicate conversion is gone, not merely bypassed."""

        import inspect
        from yob_storefront.api import payment
        from yob_storefront.tests.test_payment_lifecycle import _code_only

        source = _code_only(payment.process_pay_later)

        self.assertNotIn("create_sales_order_from_cart", source)
        self.assertNotIn("Cart", source, "Pay Later still references a Cart")

        # And the module no longer imports the conversion at all.
        self.assertNotIn("create_sales_order_from_cart",
                         inspect.getsource(payment).split("def process_payment")[0])


# =========================================================
# 9-18. RAZORPAY INITIATION
# =========================================================

class RazorpayInitiationCase(CutoverCase):

    def test_razorpay_commits_exactly_one_sales_order(self):
        """9."""

        cart, data = self.started()
        so_before = frappe.db.count("Sales Order")

        response = self.pay(data["token"], "Razorpay")

        self.assertIsNone(_error_code(response), f"unexpected: {response}")
        self.assertEqual(frappe.db.count("Sales Order"), so_before + 1)
        self.assertEqual(len(self.fake.orders), 1)

    def test_commit_happens_before_the_provider_is_contacted(self):
        """10 + 11. The durability boundary, and no lock held across the network.

        ``self.commits`` records the number of provider orders that existed at
        each commit. A commit recorded while that count is 0 proves the local
        obligation was made durable BEFORE the first provider call -- which is
        also the point at which the Cart and Payment Request row locks are
        released.
        """

        cart, data = self.started()

        response = self.pay(data["token"], "Razorpay")

        self.assertIsNone(_error_code(response))
        self.assertTrue(self.commits, "no explicit commit before the provider call")
        self.assertEqual(self.commits[0], 0,
                         "the provider was contacted before the local commit")

    def test_provider_amount_comes_from_the_immutable_obligation(self):
        """12."""

        cart, data = self.started()
        pr = self.pr_row(data["payment_request"], "grand_total", "currency")

        response = self.pay(data["token"], "Razorpay")
        order = list(self.fake.orders.values())[0]

        expected_paise = int(round(float(pr.grand_total) * 100))

        self.assertEqual(order["amount"], expected_paise)
        self.assertEqual(order["currency"], pr.currency)
        self.assertEqual(response["data"]["amount"], expected_paise)

        # ... and it equals the committed Sales Order.
        so = frappe.get_doc("Sales Order", response["data"]["sales_order"])
        self.assertAlmostEqual(float(so.grand_total), float(pr.grand_total), places=2)
        self.assertEqual(so.currency, pr.currency)

    def test_deterministic_receipt_is_used(self):
        """13."""

        from yob_storefront.integrations.razorpay import client as rz

        cart, data = self.started()
        self.pay(data["token"], "Razorpay")

        order = list(self.fake.orders.values())[0]

        self.assertEqual(
            order["receipt"],
            rz.receipt_for_payment_request(data["payment_request"]),
            "receipt must be derived from the immutable Payment Request")

    def test_lost_create_response_recovers_within_the_same_request(self):
        """14. Provider succeeded; our side never saw the answer.

        The order already exists at Razorpay, so the receipt lookup finds it
        immediately and the SAME request recovers -- the caller never sees an
        error. That is stronger than the cross-request recovery I first
        expected, and it is the behaviour worth pinning.
        """

        cart, data = self.started()
        self.fake.lose_create_response = True

        response = self.pay(data["token"], "Razorpay")

        self.assertIsNone(_error_code(response), f"unexpected: {response}")
        self.assertEqual(len(self.fake.orders), 1,
                         "a lost response produced a second provider order")
        self.assertEqual(response["data"]["order_id"],
                         list(self.fake.orders.values())[0]["id"])

        # The recovered order id was persisted, so a later retry reuses it.
        self.assertEqual(
            self.pr_row(data["payment_request"], "custom_razorpay_order_id")
                .custom_razorpay_order_id,
            response["data"]["order_id"])

    def test_lost_response_and_failed_recovery_is_retryable(self):
        """14b. Both the create response AND the recovery lookup are lost.

        Now the caller does see an error -- and it must be the retryable one,
        with the Sales Order intact. The next attempt recovers by receipt.
        """

        cart, data = self.started()
        self.fake.lose_create_response = True

        from yob_storefront.integrations.razorpay import client as rz

        with patch.object(rz, "find_orders_by_receipt", return_value=[]):
            first = self.pay(data["token"], "Razorpay")

        self.assertEqual(_error_code(first), "payment_provider_error")
        self.assertTrue(first["errors"][0]["details"]["retryable"])
        self.assertEqual(len(self.fake.orders), 1, "the provider DID create one")

        # Retry: create is refused as a duplicate receipt, recovery succeeds.
        self.fake.lose_create_response = False
        second = self.pay(data["token"], "Razorpay")

        self.assertIsNone(_error_code(second), f"unexpected: {second}")
        self.assertEqual(len(self.fake.orders), 1,
                         "retry created a second provider order")

    def test_lost_canonical_id_recovers_without_a_second_create(self):
        """15. Rewritten after wire verification.

        This used to assert that BOTH attempts called create, relying on
        Razorpay rejecting the duplicate receipt. Test Mode proved it does not
        reject -- a second create simply produces a second order. So the
        guarantee moved into YOB: the durable creation claim means the retry
        never calls create at all and recovers by receipt instead.
        """

        cart, data = self.started()

        first = self.pay(data["token"], "Razorpay")
        order_id = first["data"]["order_id"]
        creates_after_first = self.fake.create_calls

        # Forget the canonical order id. The CLAIM remains, and that is what
        # stops a second create.
        frappe.db.set_value("Payment Request", data["payment_request"],
                            "custom_razorpay_order_id", None)
        frappe.clear_document_cache("Payment Request", data["payment_request"])

        second = self.pay(data["token"], "Razorpay")

        self.assertIsNone(_error_code(second), f"unexpected: {second}")
        self.assertEqual(second["data"]["order_id"], order_id)
        self.assertEqual(len(self.fake.orders), 1)
        self.assertEqual(self.fake.create_calls, creates_after_first,
                         "the retry issued a second create")

    def test_repeated_initiation_converges_on_one_provider_order(self):
        """16."""

        cart, data = self.started()
        # A delta, not an absolute: the site carries pre-existing Sales Orders
        # for this customer from earlier seeding.
        so_before = frappe.db.count("Sales Order")

        responses = [self.pay(data["token"], "Razorpay") for _ in range(3)]

        order_ids = {r["data"]["order_id"] for r in responses}
        sales_orders = {r["data"]["sales_order"] for r in responses}

        self.assertEqual(len(order_ids), 1, f"provider orders diverged: {order_ids}")
        self.assertEqual(len(sales_orders), 1, f"sales orders diverged: {sales_orders}")
        self.assertEqual(len(self.fake.orders), 1)
        self.assertEqual(frappe.db.count("Sales Order"), so_before + 1)

    def test_provider_failure_after_commitment_keeps_the_order(self):
        """17. The Sales Order is NOT rolled back."""

        cart, data = self.started()
        self.fake.fail_create = True

        response = self.pay(data["token"], "Razorpay")

        self.assertEqual(_error_code(response), "payment_provider_error")

        details = response["errors"][0]["details"]
        self.assertTrue(details["retryable"])

        so = frappe.get_doc("Sales Order", details["sales_order"])
        self.assertEqual(so.docstatus, 0, "Draft order must survive")

        row = self.pr_row(data["payment_request"],
                          "reference_doctype", "reference_name", "status")
        self.assertEqual(row.reference_doctype, "Sales Order")
        self.assertEqual(row.reference_name, so.name)
        self.assertNotEqual(row.status, "Paid")

        self.assertEqual(frappe.db.get_value("Cart", cart.name, "status"), "Ordered")

    def test_retry_after_provider_failure_reuses_the_sales_order(self):
        """18. Rewritten after wire verification -- and this one is a trade-off.

        A failed create is AMBIGUOUS: the request may have reached Razorpay and
        been lost on the way back. Since the wire proved a second create would
        silently produce a second order, the claim blocks it and the retry is
        recovery-only. If the create genuinely never reached Razorpay there is
        nothing to recover, so the payment stays provider-pending rather than
        completing.

        That is the accepted cost of never double-creating: correctness over
        instant recovery from a rare failure. Resolving it needs an
        operator-controlled reset, designed separately.

        What must hold regardless, and is asserted here: the Sales Order is not
        rolled back, it is reused, and no provider order ever appears.
        """

        cart, data = self.started()
        self.fake.fail_create = True

        failed = self.pay(data["token"], "Razorpay")
        so_name = failed["errors"][0]["details"]["sales_order"]
        so_count = frappe.db.count("Sales Order")

        self.fake.fail_create = False
        retry = self.pay(data["token"], "Razorpay")

        self.assertEqual(_error_code(retry), "payment_provider_error")
        self.assertIs(retry["errors"][0]["details"]["retryable"], True)
        self.assertEqual(retry["errors"][0]["details"]["sales_order"], so_name,
                         "the committed order was not reused")
        self.assertEqual(frappe.db.count("Sales Order"), so_count)
        self.assertEqual(len(self.fake.orders), 0,
                         "an ambiguous failure produced a provider order")

    def test_stale_cart_is_irrelevant_after_commitment(self):
        """25."""

        cart, data = self.started()
        self.pay(data["token"], "Razorpay")

        # Mutate the Cart in a way that would be 'stale' pre-commitment.
        cart.reload()
        cart.status = "Draft"
        cart.items[0].quantity = 99
        cart.save(ignore_permissions=True)

        retry = self.pay(data["token"], "Razorpay")

        self.assertIsNone(_error_code(retry),
                          f"a changed Cart broke a committed payment: {retry}")

    def test_response_shape_is_identical_for_new_and_reused_orders(self):
        """12 (SPA contract). Created, reused and recovered look the same."""

        cart, data = self.started()

        created = self.pay(data["token"], "Razorpay")
        reused = self.pay(data["token"], "Razorpay")

        expected = {"payment_method", "razorpay_key", "order_id", "amount",
                    "currency", "sales_order", "payment_request"}

        self.assertEqual(set(created["data"]), expected)
        self.assertEqual(set(reused["data"]), expected)
        self.assertEqual(created["data"]["order_id"], reused["data"]["order_id"])

        # Only the publishable key may leave the server.
        self.assertEqual(created["data"]["razorpay_key"], "rzp_test_placeholder")
        self.assertNotIn("secret", str(created["data"]).lower())


# =========================================================
# 19-24, 26. SETTLEMENT
# =========================================================

class SettlementCase(CutoverCase):

    def paid(self, qty=12):
        """Drive a real initiation, then simulate a captured provider payment."""

        cart, data = self.started(qty=qty)
        initiation = self.pay(data["token"], "Razorpay")
        order_id = initiation["data"]["order_id"]
        payment_id = self.fake.pay(order_id)
        return cart, data, initiation, order_id, payment_id

    def test_settlement_uses_the_exact_committed_sales_order(self):
        """19."""

        cart, data, initiation, order_id, payment_id = self.paid()

        response = self.verify(order_id, payment_id)

        self.assertIsNone(_error_code(response), f"unexpected: {response}")
        self.assertEqual(response["data"]["sales_order"],
                         initiation["data"]["sales_order"])
        self.assertEqual(response["data"]["payment_request"],
                         data["payment_request"])

    def test_settlement_never_creates_another_sales_order(self):
        """20."""

        cart, data, initiation, order_id, payment_id = self.paid()
        so_before = frappe.db.count("Sales Order")

        self.verify(order_id, payment_id)

        self.assertEqual(frappe.db.count("Sales Order"), so_before)

    def test_settlement_retry_is_idempotent(self):
        """21. Callback retries, refreshes and duplicate webhooks converge."""

        cart, data, initiation, order_id, payment_id = self.paid()

        first = self.verify(order_id, payment_id)
        so_count = frappe.db.count("Sales Order")
        log_count = frappe.db.count("Razorpay Payment Log")

        second = self.verify(order_id, payment_id)

        self.assertIsNone(_error_code(second), f"unexpected: {second}")
        self.assertEqual(second["data"]["sales_order"], first["data"]["sales_order"])
        self.assertEqual(frappe.db.count("Sales Order"), so_count,
                         "a settlement retry created another Sales Order")
        self.assertEqual(frappe.db.count("Razorpay Payment Log"), log_count,
                         "a settlement retry logged the payment twice")

    def test_a_different_payment_against_a_settled_obligation_is_refused(self):
        """Paying twice must never be silently absorbed."""

        cart, data, initiation, order_id, payment_id = self.paid()
        self.verify(order_id, payment_id)

        second_payment = self.fake.pay(order_id)
        response = self.verify(order_id, second_payment)

        self.assertEqual(_error_code(response), "payment_already_processed")

    def test_amount_mismatch_refuses_settlement(self):
        """22."""

        cart, data, initiation, order_id, payment_id = self.paid()
        self.fake.payments[payment_id]["amount"] += 100

        response = self.verify(order_id, payment_id)

        self.assertEqual(_error_code(response), "payment_amount_mismatch")
        self.assertNotEqual(
            self.pr_row(data["payment_request"], "status").status, "Paid")

    def test_currency_mismatch_refuses_settlement(self):
        """23."""

        cart, data, initiation, order_id, payment_id = self.paid()
        self.fake.payments[payment_id]["currency"] = "USD"

        response = self.verify(order_id, payment_id)

        self.assertEqual(_error_code(response), "payment_currency_mismatch")

    def test_unknown_provider_order_refuses_settlement(self):
        """24."""

        response = self.verify("order_does_not_exist", "pay_x")

        self.assertEqual(_error_code(response), "payment_reference_invalid")

    def test_bad_signature_refuses_settlement(self):
        """The frontend's claim of success is never trusted."""

        cart, data, initiation, order_id, payment_id = self.paid()

        response = self.verify(order_id, payment_id, signature="forged")

        self.assertEqual(_error_code(response), "payment_signature_invalid")
        self.assertNotEqual(
            self.pr_row(data["payment_request"], "status").status, "Paid")

    def test_settlement_does_not_mutate_the_obligation_amount(self):
        """26. No post-issuance financial mutation on the successful path."""

        cart, data, initiation, order_id, payment_id = self.paid()

        before = self.pr_row(data["payment_request"], "grand_total", "currency",
                             "custom_source_fingerprint")

        self.verify(order_id, payment_id)

        after = self.pr_row(data["payment_request"], "grand_total", "currency",
                            "custom_source_fingerprint")

        self.assertEqual(dict(before), dict(after))

    def test_settlement_marks_paid_and_revokes_the_credential(self):
        """The settled obligation is closed and its link stops working."""

        cart, data, initiation, order_id, payment_id = self.paid()

        self.verify(order_id, payment_id)

        row = self.pr_row(data["payment_request"], "status",
                          "custom_checkout_token", "custom_razorpay_payment_id")

        self.assertEqual(row.status, "Paid")
        self.assertIsNone(row.custom_checkout_token,
                          "a settled obligation kept a usable payment link")
        self.assertEqual(row.custom_razorpay_payment_id, payment_id)

        # And the old link no longer resolves.
        self.assertEqual(_error_code(self.checkout(data["token"])),
                         "checkout_token_invalid")

    def test_settlement_never_consults_a_cart(self):
        """Financial truth comes from PR -> Sales Order, never from a Cart."""

        cart, data, initiation, order_id, payment_id = self.paid()

        real_get_doc = frappe.get_doc
        loaded = []

        def spy(doctype, *args, **kwargs):
            if isinstance(doctype, str):
                loaded.append(doctype)
            return real_get_doc(doctype, *args, **kwargs)

        with patch.object(frappe, "get_doc", side_effect=spy):
            response = self.verify(order_id, payment_id)

        self.assertIsNone(_error_code(response))
        self.assertNotIn("Cart", loaded, "settlement loaded a Cart")

    def test_settlement_contains_no_sales_order_creation(self):
        """20 (structural). The capability is gone from the module."""

        from yob_storefront.services import payment_service
        from yob_storefront.tests.test_payment_lifecycle import _code_only

        source = _code_only(payment_service.process_success_payment)

        self.assertNotIn("create_sales_order_from_cart", source)
        self.assertNotIn("pr.save(", source, "issued-PR whole-document save remains")


if __name__ == "__main__":
    unittest.main()
