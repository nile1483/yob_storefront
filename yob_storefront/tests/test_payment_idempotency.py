# Copyright (c) 2026, YOB and Shayona
"""Gate 3 — provider idempotency and rollback mechanics.

Proves the mechanisms the payment commitment boundary will rely on, using a
deterministic stub for Razorpay. No live-money calls, no test-mode keys needed.

Razorpay's documented contract, which these tests encode:
  * receipt is max 40 characters
  * receipt must be unique
  * creating a second Order with the same receipt is rejected as a duplicate
  * Orders can be listed filtered by receipt

Real Test-Mode wire verification remains a separate pre-release item.
"""

import unittest
from unittest.mock import patch

import frappe

from yob_storefront.integrations.razorpay import client as rz

PR_A = "ACC-PRQ-2026-00001"
PR_B = "ACC-PRQ-2026-00002"


class FakeRazorpay:
    """Deterministic stand-in enforcing Razorpay's documented receipt rules."""

    class DuplicateReceipt(Exception):
        pass

    def __init__(self):
        self.orders = {}
        self.create_calls = 0

    # --- surface used by the adapter -------------------------------------

    def create(self, payload):
        self.create_calls += 1
        receipt = payload.get("receipt")
        if receipt and any(o["receipt"] == receipt for o in self.orders.values()):
            raise self.DuplicateReceipt("Order with this receipt already exists")
        oid = f"order_{len(self.orders) + 1:04d}"
        self.orders[oid] = {"id": oid, "receipt": receipt, "amount": payload["amount"],
                            "currency": payload["currency"], "status": "created"}
        return self.orders[oid]

    def all(self, data=None):
        receipt = (data or {}).get("receipt")
        items = [o for o in self.orders.values() if not receipt or o["receipt"] == receipt]
        return {"count": len(items), "items": items}

    def fetch(self, oid):
        return self.orders[oid]

    def _install(self):
        class _Client:
            order = self
        return patch.object(rz, "get_client", return_value=_Client())


class ReceiptIdentityCase(unittest.TestCase):
    def test_receipt_is_deterministic_bounded_and_distinct(self):
        a1, a2, b = rz.receipt_for_payment_request(PR_A), \
                    rz.receipt_for_payment_request(PR_A), \
                    rz.receipt_for_payment_request(PR_B)

        self.assertEqual(a1, a2, "receipt must be stable across retries")
        self.assertNotEqual(a1, b, "different Payment Requests must differ")
        self.assertLessEqual(len(a1), 40, "Razorpay limit")
        self.assertTrue(a1.startswith("yob-"))

    def test_receipt_fits_for_any_payment_request_name(self):
        """Bounded by hashing, so future naming series cannot overflow."""

        for name in ("X", "ACC-PRQ-2026-00001", "P" * 200):
            self.assertLessEqual(len(rz.receipt_for_payment_request(name)), 40)


class ProviderRecoveryCase(unittest.TestCase):
    """The distributed failure: provider succeeded, local save did not."""

    def setUp(self):
        self.fake = FakeRazorpay()

    def test_lost_response_recovers_the_same_order(self):
        receipt = rz.receipt_for_payment_request(PR_A)

        with self.fake._install():
            created = rz.create_order(15930, "INR", receipt=receipt)
            # Simulate: response received by Razorpay but our local save failed,
            # so custom_razorpay_order_id was never persisted.
            recovered = rz.find_order_by_receipt(receipt)

        self.assertIsNotNone(recovered, "receipt lookup must recover the order")
        self.assertEqual(recovered["id"], created["id"])
        self.assertEqual(recovered["amount"], 15930)
        self.assertEqual(recovered["currency"], "INR")
        self.assertEqual(len(self.fake.orders), 1, "recovery must not create a second order")

    def test_duplicate_receipt_is_rejected_not_silently_duplicated(self):
        receipt = rz.receipt_for_payment_request(PR_A)

        with self.fake._install():
            rz.create_order(15930, "INR", receipt=receipt)
            with self.assertRaises(FakeRazorpay.DuplicateReceipt):
                rz.create_order(15930, "INR", receipt=receipt)

        self.assertEqual(len(self.fake.orders), 1)

    def test_concurrent_attempts_converge_on_one_provider_order(self):
        """Both requests reach the provider; only one obligation results.

        This is why no DB lock is held across the network call -- the
        deterministic receipt, not a lock, provides convergence.
        """

        receipt = rz.receipt_for_payment_request(PR_A)
        results = []

        with self.fake._install():
            for _ in range(2):                       # two concurrent callers
                try:
                    results.append(rz.create_order(15930, "INR", receipt=receipt)["id"])
                except FakeRazorpay.DuplicateReceipt:
                    results.append(rz.find_order_by_receipt(receipt)["id"])

        self.assertEqual(len(set(results)), 1, "callers must converge on one order")
        self.assertEqual(len(self.fake.orders), 1)
        self.assertEqual(self.fake.create_calls, 2, "both did attempt creation")

    def test_different_payment_requests_get_different_orders(self):
        with self.fake._install():
            a = rz.create_order(15930, "INR", receipt=rz.receipt_for_payment_request(PR_A))
            b = rz.create_order(20000, "INR", receipt=rz.receipt_for_payment_request(PR_B))

        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(len(self.fake.orders), 2)


class RollbackCacheInvalidationCase(unittest.TestCase):
    """Gate 2 discovery, pinned as a Gate 3 design rule.

    frappe.db.rollback() restores the database but NOT the document cache.
    process_payment catches validation errors and keeps serving in the same
    request, so it must invalidate what it touched or it acts on stale docs.
    """

    CUSTOMER = "YOB Demo Buyer"

    @classmethod
    def setUpClass(cls):
        if not frappe.db.exists("Customer", cls.CUSTOMER):
            raise unittest.SkipTest("requires seed_demo_data")

    def test_rollback_restores_the_database(self):
        """Rollback restores the DB. Whether the CACHE is also stale is not
        asserted here: staleness proved non-deterministic across runs -- it
        depends on prior cache state and test ordering. It is real (it leaked a
        disabled Customer between Gate 2 tests) but not reliably reproducible,
        so asserting its presence makes a brittle test. The invariant worth
        pinning is the remedy, in the test below.
        """

        frappe.db.savepoint("stale")
        try:
            frappe.db.set_value("Customer", self.CUSTOMER, "disabled", 1,
                                update_modified=False)
            frappe.db.rollback(save_point="stale")
            self.assertEqual(frappe.db.get_value("Customer", self.CUSTOMER, "disabled"), 0,
                             "database must be restored by rollback")
        finally:
            frappe.clear_document_cache("Customer", self.CUSTOMER)

    def test_clear_document_cache_restores_database_truth(self):
        frappe.db.savepoint("fixed")
        try:
            frappe.db.set_value("Customer", self.CUSTOMER, "disabled", 1,
                                update_modified=False)
            frappe.db.rollback(save_point="fixed")
            frappe.clear_document_cache("Customer", self.CUSTOMER)   # the rule

            self.assertEqual(frappe.get_cached_doc("Customer", self.CUSTOMER).disabled, 0)
        finally:
            frappe.clear_document_cache("Customer", self.CUSTOMER)
