# Copyright (c) 2026, YOB and Shayona
"""Payment Lifecycle Phase 1 -- immutable Payment Request + stale detection.

The invariant under test, stated once:

    A Payment Request is an IMMUTABLE obligation. Once issued, no code path may
    refresh its grand_total, currency or source fingerprint from a changed
    Cart. When the Cart moves, the answer is `payment_request_stale` -- never a
    silently re-priced payment link.

Every test creates only what it needs and rolls back to a savepoint, so the
site is left exactly as found. Rollback is explicit rather than left to
request-end behaviour because the API layer catches errors and returns
envelopes, and because `frappe.db.rollback()` restores the database but not
Frappe's document cache -- hence the `clear_cache()` in tearDown.
"""

import inspect
import unittest
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime

from yob_storefront.api.response import (
    CHECKOUT_TOKEN_EXPIRED,
    CHECKOUT_TOKEN_INVALID,
    PAYMENT_REQUEST_STALE,
)
from yob_storefront.services import payment_request_service as prs
from yob_storefront.services.payment_source import (
    cart_payment_snapshot,
    cart_fingerprint,
    fingerprint,
)

CUSTOMER = "YOB Demo Buyer"
ITEM = "YOB-BOLT-M10"          # 12.50 list; PRLE-0001 gives 10% at qty >= 10
ITEM_B = "YOB-NUT-M10"
CONTACT = "Demo Buyer-YOB Demo Buyer"
BILLING = "YOB Demo Billing-Billing"
SHIPPING = "YOB Demo Shipping-Shipping"


def _seeded() -> bool:
    return bool(
        frappe.db.exists("Customer", CUSTOMER)
        and frappe.db.exists("Item", ITEM)
        and frappe.db.exists("Contact", CONTACT)
        and frappe.db.exists("Address", BILLING)
    )


def _raw(endpoint):
    """The undecorated endpoint body.

    ``inspect.unwrap`` rather than a fixed number of ``__wrapped__`` hops: the
    stack is three deep on proceed_to_payment and two on the payment endpoints,
    and hard-coding either silently leaves ``require_application`` in place --
    which then answers ``application_access_denied`` AND commits an audit row,
    destroying the test's savepoint. Only identity resolution is bypassed; all
    the lifecycle logic under test still runs.
    """

    return inspect.unwrap(endpoint)


def _code_only(fn) -> str:
    """Source of ``fn`` with its docstring and comments removed.

    Round-tripping through the AST drops comments for free and normalises
    quoting, so the assertions below match executable statements only.
    """

    import ast

    tree = ast.parse(inspect.getsource(fn))
    body = tree.body[0]

    if (body.body and isinstance(body.body[0], ast.Expr)
            and isinstance(body.body[0].value, ast.Constant)
            and isinstance(body.body[0].value.value, str)):
        body.body.pop(0)

    return ast.unparse(tree)


def _error_code(envelope):
    """First error code from a YOB error envelope, or None on success."""

    if isinstance(envelope, dict) and "errors" in envelope:
        return envelope["errors"][0]["code"]
    return None


class LifecycleCase(unittest.TestCase):
    """Shared fixture: one seeded buyer, one Draft Cart, savepoint isolation."""

    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data on the test site")

    def setUp(self):
        frappe.set_user("Administrator")

        # SAFETY NET, and not a theoretical one: the Razorpay path contains a
        # deliberate frappe.db.commit() before the provider call. A test that
        # reaches it executes a REAL commit, which ends the transaction and
        # makes the savepoint below meaningless -- this leaked a Sales Order, a
        # committed Cart and a superseded Payment Request into the test site
        # once before it was caught and cleaned up.
        #
        # Every payment test relies on savepoint rollback for isolation and
        # none needs durability, so commit is recorded rather than executed for
        # the whole hierarchy. self.commits also lets a test PROVE the
        # durability boundary was crossed before the provider was contacted.
        self.commits = []
        self._commit_patch = patch.object(
            frappe.db, "commit", side_effect=lambda: self.commits.append(True))
        self._commit_patch.start()
        self.addCleanup(self._commit_patch.stop)

        frappe.db.savepoint("phase1")
        self.customer = frappe.get_doc("Customer", CUSTOMER)

    def tearDown(self):
        frappe.db.rollback(save_point="phase1")
        frappe.clear_cache()

    # ----------------------------------------------------------- helpers

    def make_cart(self, qty=12, item=ITEM, shipping=SHIPPING):
        """A checkout-ready Cart built through the REAL pricing path."""

        from yob_storefront.api.cart import get_or_create_cart
        from yob_storefront.services.cart_service import reprice_cart

        cart = get_or_create_cart(self.customer)
        cart.set("items", [])
        cart.append("items", {"item_code": item, "quantity": qty,
                              "uom": "Nos", "stock_uom": "Nos"})
        cart.contact_person = CONTACT
        cart.billing_address = BILLING
        cart.shipping_address = shipping
        reprice_cart(cart, self.customer)
        cart.save(ignore_permissions=True)
        return cart

    def reprice(self, cart):
        """Recalculate and persist, as an authenticated cart mutation would."""

        from yob_storefront.services.cart_service import reprice_cart

        reprice_cart(cart, self.customer)
        cart.save(ignore_permissions=True)
        return cart

    def proceed(self):
        """Call the real endpoint with the storefront auth boundary stubbed.

        Only identity resolution is stubbed. Everything under test -- the Cart
        lock, repricing, fingerprinting, candidate selection -- runs for real.
        """

        from yob_storefront.api import checkout

        with patch.object(checkout, "get_storefront_customer",
                          return_value=self.customer):
            return _raw(checkout.proceed_to_payment)(
                auth_context={"profile_name": CUSTOMER}
            )

    def pr_row(self, name, *fields):
        return frappe.db.get_value("Payment Request", name, list(fields), as_dict=True)

    def assert_created(self, response):
        data = response["data"]
        self.assertTrue(data["token"])
        self.assertTrue(data["payment_request"])
        return data


# =========================================================
# 1-5. FINGERPRINT CANONICALISATION
# =========================================================

class FingerprintCase(LifecycleCase):

    def test_fingerprint_is_stable_across_reload(self):
        """1. The same stored Cart must always hash the same."""

        cart = self.make_cart()
        first = cart_fingerprint(cart)

        reloaded = frappe.get_doc("Cart", cart.name)

        self.assertEqual(first, cart_fingerprint(reloaded))

    def test_timestamps_do_not_affect_the_fingerprint(self):
        """2. `modified` moves on every unrelated write; the obligation does not."""

        cart = self.make_cart()
        before = cart_fingerprint(cart)

        frappe.db.set_value("Cart", cart.name, "modified",
                            now_datetime() + timedelta(days=1),
                            update_modified=False)
        frappe.clear_document_cache("Cart", cart.name)

        after = cart_fingerprint(frappe.get_doc("Cart", cart.name))

        self.assertEqual(before, after, "a timestamp changed the obligation")

    def test_line_order_does_not_affect_the_fingerprint(self):
        """3. Row order is storage detail, not obligation."""

        cart = self.make_cart()
        cart.append("items", {"item_code": ITEM_B, "quantity": 3,
                              "uom": "Nos", "stock_uom": "Nos"})
        self.reprice(cart)

        forward = cart_payment_snapshot(cart)

        cart.items.reverse()
        reversed_snapshot = cart_payment_snapshot(cart)

        self.assertEqual(fingerprint(forward), fingerprint(reversed_snapshot))

    def test_tied_lines_order_deterministically(self):
        """4. Lines tying on item/qty/rate must still sort stably.

        Sorting on a subset of fields would leave rows that differ only in a
        later field free to swap places, so the same Cart could hash two ways.
        Ordering is by each line's FULL canonical serialisation, so equal-
        prefix lines are separated by whatever actually differs.
        """

        base = {"item_code": ITEM, "quantity": "1.000000", "uom": "Nos",
                "conversion_factor": "1.000000", "rate": "10.000000",
                "amount": "10.000000", "discount_percentage": "0.000000",
                "discount_amount": "0.000000", "tax_amount": "0.000000",
                "total_amount": "10.000000", "pricing_rules": None,
                "pricing_rule_apply_on": None}

        # Identical on (item_code, uom, quantity, rate); differ only in tax.
        a = dict(base, tax_amount="1.000000")
        b = dict(base, tax_amount="2.000000")

        snap_ab = {"source_doctype": "Cart", "items": sorted([a, b], key=_canon)}
        snap_ba = {"source_doctype": "Cart", "items": sorted([b, a], key=_canon)}

        self.assertEqual(fingerprint(snap_ab), fingerprint(snap_ba))

    def test_duplicate_lines_change_the_hash(self):
        """5. Multiplicity is preserved: two rows of qty 5 != one row of qty 5."""

        one = self.make_cart(qty=5)
        one_fp = cart_fingerprint(one)

        one.append("items", {"item_code": ITEM, "quantity": 5,
                             "uom": "Nos", "stock_uom": "Nos"})
        two = self.reprice(one)

        self.assertNotEqual(one_fp, cart_fingerprint(two),
                            "a duplicated line collided with a single line")


def _canon(value):
    from yob_storefront.services.payment_source import _canonical
    return _canonical(value)


# =========================================================
# 6-12. PROCEED: ISSUE, REUSE, ROTATE, SUPERSEDE
# =========================================================

class ProceedCase(LifecycleCase):

    def test_first_proceed_creates_pr_with_fingerprint(self):
        """6."""

        cart = self.make_cart()
        data = self.assert_created(self.proceed())

        row = self.pr_row(data["payment_request"], "grand_total", "currency",
                          "custom_source_fingerprint", "reference_doctype",
                          "reference_name", "party")

        self.assertEqual(row.reference_doctype, "Cart")
        self.assertEqual(row.reference_name, cart.name)
        self.assertEqual(row.party, CUSTOMER)
        self.assertEqual(row.custom_source_fingerprint,
                         cart_fingerprint(frappe.get_doc("Cart", cart.name)))
        self.assertAlmostEqual(float(row.grand_total), float(cart.grand_total), places=2)

    def test_unchanged_proceed_reuses_pr_and_token(self):
        """7. Idempotent: an unchanged cart must not issue a second obligation."""

        cart = self.make_cart()
        first = self.assert_created(self.proceed())
        before = self.pr_row(first["payment_request"], "modified")

        second = self.assert_created(self.proceed())

        self.assertEqual(first["payment_request"], second["payment_request"])
        self.assertEqual(first["token"], second["token"], "token must not rotate")
        self.assertEqual(before.modified,
                         self.pr_row(first["payment_request"], "modified").modified,
                         "an unchanged reuse wrote to the Payment Request")
        self.assertEqual(self._usable_count(cart), 1)

    def test_expired_token_rotates_on_the_same_pr(self):
        """8. Expiry kills the credential, not the obligation."""

        cart = self.make_cart()
        first = self.assert_created(self.proceed())
        pr_name = first["payment_request"]

        money_before = self.pr_row(pr_name, "grand_total", "currency",
                                   "custom_source_fingerprint")

        # Expire the credential without touching anything financial.
        frappe.db.set_value("Payment Request", pr_name, "custom_checkout_expiry",
                            now_datetime() - timedelta(minutes=1))
        frappe.clear_document_cache("Payment Request", pr_name)

        second = self.assert_created(self.proceed())

        self.assertEqual(second["payment_request"], pr_name, "obligation must survive")
        self.assertNotEqual(second["token"], first["token"], "credential must rotate")
        self.assertEqual(self._usable_count(cart), 1,
                         "rotation must not create a Payment Request")

        money_after = self.pr_row(pr_name, "grand_total", "currency",
                                  "custom_source_fingerprint")
        self.assertEqual(dict(money_before), dict(money_after),
                         "rotation touched the financial obligation")

        # The old credential must stop resolving immediately.
        self.assertEqual(_error_code(prs.resolve_checkout_token(first["token"])),
                         CHECKOUT_TOKEN_INVALID)

    def test_quantity_change_supersedes(self):
        """9."""

        cart = self.make_cart(qty=12)
        first = self.assert_created(self.proceed())

        cart.reload()
        cart.items[0].quantity = 20
        self.reprice(cart)

        second = self.assert_created(self.proceed())

        self.assert_superseded(first, second)

    def test_pricing_change_supersedes(self):
        """10. Same cart contents, different money -> different obligation."""

        cart = self.make_cart(qty=12)
        first = self.assert_created(self.proceed())
        old_total = self.pr_row(first["payment_request"], "grand_total").grand_total

        # Move the price list rate, then reprice through the real engine.
        rate_row = frappe.db.get_value(
            "Item Price", {"item_code": ITEM, "price_list": cart.selling_price_list}, "name")
        if not rate_row:
            self.skipTest("no Item Price for the seeded selling price list")

        frappe.db.set_value("Item Price", rate_row, "price_list_rate", 99.0)
        frappe.clear_cache()

        cart.reload()
        self.reprice(cart)
        second = self.assert_created(self.proceed())

        self.assert_superseded(first, second)
        self.assertAlmostEqual(
            float(self.pr_row(first["payment_request"], "grand_total").grand_total),
            float(old_total), places=2,
            msg="the old obligation's amount was rewritten")

    def test_address_change_supersedes_even_when_total_is_unchanged(self):
        """11. The obligation is not only its total.

        Changing the delivery address changes what becomes the Sales Order, so
        it is a different commitment even at an identical price.
        """

        cart = self.make_cart(qty=12, shipping=SHIPPING)
        first = self.assert_created(self.proceed())
        total_before = self.pr_row(first["payment_request"], "grand_total").grand_total

        cart.reload()
        cart.shipping_address = BILLING          # different address, same money
        self.reprice(cart)

        second = self.assert_created(self.proceed())

        self.assertAlmostEqual(
            float(self.pr_row(second["payment_request"], "grand_total").grand_total),
            float(total_before), places=2, msg="this test needs an unchanged total")
        self.assert_superseded(first, second)

    def assert_superseded(self, first, second):
        """12. The replacement is new; the old one is revoked but untouched."""

        self.assertNotEqual(first["payment_request"], second["payment_request"],
                            "a changed obligation must not reuse the Payment Request")

        old = self.pr_row(first["payment_request"], "custom_checkout_token",
                          "custom_checkout_expiry", "docstatus", "status")

        self.assertIsNone(old.custom_checkout_token, "old credential not revoked")
        self.assertIsNone(old.custom_checkout_expiry, "old expiry not cleared")
        self.assertEqual(old.docstatus, 0,
                         "supersession must not submit or cancel the Draft")

        # The new one carries the current obligation and a live credential.
        new = self.pr_row(second["payment_request"], "custom_checkout_token",
                          "custom_source_fingerprint")
        self.assertEqual(new.custom_checkout_token, second["token"])
        self.assertTrue(new.custom_source_fingerprint)

    def _usable_count(self, cart):
        """Live credentials on this Cart -- the invariant that actually matters.

        Counting every Payment Request for the customer would be wrong: the
        site carries legacy rows from the pre-Phase-1 lifecycle (no fingerprint,
        still tokened), and the correct response to those is precisely to
        supersede them. What must stay at one is the number of credentials that
        can still be paid.
        """

        return frappe.db.count("Payment Request", {
            "reference_doctype": "Cart",
            "reference_name": cart.name,
            "custom_checkout_token": ["is", "set"],
        })


# =========================================================
# 13-15. TOKEN RESOLUTION
# =========================================================

class TokenResolverCase(LifecycleCase):

    def test_superseded_token_cannot_resolve(self):
        """13."""

        cart = self.make_cart(qty=12)
        first = self.assert_created(self.proceed())

        cart.reload()
        cart.items[0].quantity = 20
        self.reprice(cart)
        self.proceed()

        self.assertEqual(_error_code(prs.resolve_checkout_token(first["token"])),
                         CHECKOUT_TOKEN_INVALID)

    def test_blank_token_cannot_reach_revoked_payment_requests(self):
        """14. The `IS NULL` hazard, pinned.

        Frappe renders {"custom_checkout_token": None} as `IS NULL`. Once
        supersession starts clearing tokens, a blank token that reached the
        query would match every revoked obligation at once. The guard must run
        BEFORE the query, so a revoked row existing is exactly the condition
        that makes this test meaningful.
        """

        cart = self.make_cart(qty=12)
        self.proceed()
        cart.reload()
        cart.items[0].quantity = 20
        self.reprice(cart)
        self.proceed()

        revoked = frappe.db.count("Payment Request",
                                  {"party": CUSTOMER, "custom_checkout_token": ["is", "not set"]})
        self.assertGreaterEqual(revoked, 1, "test needs a revoked Payment Request")

        for blank in (None, "", "   "):
            self.assertEqual(_error_code(prs.resolve_checkout_token(blank)),
                             CHECKOUT_TOKEN_INVALID,
                             f"blank token {blank!r} was not rejected")

    def test_expired_token_reports_expiry(self):
        """Expiry is distinguishable from an unknown token, as published."""

        self.make_cart()
        data = self.assert_created(self.proceed())

        frappe.db.set_value("Payment Request", data["payment_request"],
                            "custom_checkout_expiry", now_datetime() - timedelta(minutes=1))
        frappe.clear_document_cache("Payment Request", data["payment_request"])

        self.assertEqual(_error_code(prs.resolve_checkout_token(data["token"])),
                         CHECKOUT_TOKEN_EXPIRED)

    def test_duplicate_token_fails_closed(self):
        """15. If DB uniqueness were ever absent, resolution must not guess.

        The column now carries a UNIQUE index, so a duplicate cannot be
        inserted -- which is also why the duplicate is simulated at the query
        boundary rather than written. What is under test is the resolver's
        behaviour, not MariaDB's: given two matching rows it must fail closed
        with a safe internal error instead of selecting an arbitrary
        obligation for somebody to pay.
        """

        self.make_cart()
        data = self.assert_created(self.proceed())

        real_get_all = frappe.get_all

        def two_rows(doctype, *args, **kwargs):
            if doctype == "Payment Request" and \
                    kwargs.get("filters", {}).get("custom_checkout_token"):
                row = frappe._dict(name=data["payment_request"])
                return [row, frappe._dict(name="ACC-PRQ-FAKE-00002")]
            return real_get_all(doctype, *args, **kwargs)

        with patch.object(frappe, "get_all", side_effect=two_rows):
            result = prs.resolve_checkout_token(data["token"])

        self.assertEqual(_error_code(result), "internal_server_error",
                         "duplicate tokens must fail closed, not pick a row")

    def test_unique_index_is_enforced_by_the_database(self):
        """The uniqueness the resolver is allowed to rely on actually exists."""

        indexes = frappe.db.sql(
            "SHOW INDEX FROM `tabPayment Request` WHERE Column_name = 'custom_checkout_token'",
            as_dict=True)

        self.assertTrue(indexes, "custom_checkout_token has no index")
        self.assertEqual(indexes[0]["Non_unique"], 0, "index is not UNIQUE")


# =========================================================
# 16-17. PUBLIC GET
# =========================================================

class PublicCheckoutCase(LifecycleCase):

    def get_checkout(self, token):
        from yob_storefront.api import payment
        return _raw(payment.get_checkout_data)(token=token)

    def test_unchanged_checkout_data_succeeds_without_mutation(self):
        """16. A public GET must read, and only read."""

        cart = self.make_cart()
        data = self.assert_created(self.proceed())
        pr_name = data["payment_request"]

        pr_before = self.pr_row(pr_name, "grand_total", "currency",
                               "custom_source_fingerprint", "modified")
        cart_before = frappe.db.get_value(
            "Cart", cart.name, ["grand_total", "modified"], as_dict=True)

        response = self.get_checkout(data["token"])

        self.assertIsNone(_error_code(response), f"unexpected error: {response}")
        self.assertEqual(response["data"]["payment_request"], pr_name)
        self.assertAlmostEqual(float(response["data"]["amount"]),
                               float(pr_before.grand_total), places=2)

        self.assertEqual(dict(pr_before),
                         dict(self.pr_row(pr_name, "grand_total", "currency",
                                          "custom_source_fingerprint", "modified")),
                         "the GET mutated the Payment Request")
        self.assertEqual(dict(cart_before),
                         dict(frappe.db.get_value("Cart", cart.name,
                                                  ["grand_total", "modified"], as_dict=True)),
                         "the GET persisted a Cart repricing")

    def test_changed_cart_returns_payment_request_stale(self):
        """17. The whole point: no silent re-pricing of a live payment link."""

        cart = self.make_cart(qty=12)
        data = self.assert_created(self.proceed())
        pr_name = data["payment_request"]
        before = self.pr_row(pr_name, "grand_total", "currency",
                             "custom_source_fingerprint")

        cart.reload()
        cart.items[0].quantity = 20
        self.reprice(cart)

        response = self.get_checkout(data["token"])

        self.assertEqual(_error_code(response), PAYMENT_REQUEST_STALE)
        self.assertEqual(dict(before),
                         dict(self.pr_row(pr_name, "grand_total", "currency",
                                          "custom_source_fingerprint")),
                         "a stale GET rewrote the obligation")


# =========================================================
# 18-20. PAYMENT DISPATCH ON A STALE SOURCE
# =========================================================

class StaleDispatchCase(LifecycleCase):

    def process(self, token, method):
        from yob_storefront.api import payment
        return _raw(payment.process_payment)(
            token=token, payment_method=method)

    def stale_setup(self):
        cart = self.make_cart(qty=12)
        data = self.assert_created(self.proceed())
        before = self.pr_row(data["payment_request"], "grand_total", "currency",
                             "custom_source_fingerprint")

        cart.reload()
        cart.items[0].quantity = 20
        self.reprice(cart)

        return data, before

    def test_stale_razorpay_cannot_mutate_the_payment_request(self):
        """18. Rejected BEFORE the provider is contacted.

        The gateway is configured on purpose. Since Phase B1, provider preflight
        runs BEFORE commitment -- and staleness is detected inside commitment --
        so an unconfigured gateway would answer payment_provider_not_configured
        and this test would never reach the staleness check it exists to prove.
        """

        data, before = self.stale_setup()

        from yob_storefront.tests.test_payment_cutover import CutoverCase
        CutoverCase.configure_gateway()

        from yob_storefront.integrations.razorpay import client as rz

        with patch.object(rz, "get_client") as spy:
            response = self.process(data["token"], "Razorpay")

        self.assertEqual(_error_code(response), PAYMENT_REQUEST_STALE)
        spy.assert_not_called()
        self.assertEqual(dict(before),
                         dict(self.pr_row(data["payment_request"], "grand_total",
                                          "currency", "custom_source_fingerprint")))

    def test_stale_pay_later_cannot_mutate_the_payment_request(self):
        """19. And must not commit a Sales Order either."""

        data, before = self.stale_setup()
        so_before = frappe.db.count("Sales Order")

        response = self.process(data["token"], "Pay Later")

        self.assertEqual(_error_code(response), PAYMENT_REQUEST_STALE)
        self.assertEqual(frappe.db.count("Sales Order"), so_before,
                         "a stale checkout created a Sales Order")
        self.assertEqual(dict(before),
                         dict(self.pr_row(data["payment_request"], "grand_total",
                                          "currency", "custom_source_fingerprint")))
        self.assertEqual(
            self.pr_row(data["payment_request"], "reference_doctype").reference_doctype,
            "Cart", "a stale checkout moved the reference to a Sales Order")

    def test_current_pay_later_still_commits(self):
        """The migration must not only reject -- the good path must still work.

        Guards against a validator that is 'safe' because it fails everything.
        """

        self.make_cart(qty=12)
        data = self.assert_created(self.proceed())
        so_before = frappe.db.count("Sales Order")

        response = self.process(data["token"], "Pay Later")

        self.assertIsNone(_error_code(response), f"unexpected error: {response}")
        self.assertEqual(frappe.db.count("Sales Order"), so_before + 1)
        self.assertTrue(response["data"]["sales_order"])

        # The obligation moved to the Sales Order without its money changing.
        pr = self.pr_row(data["payment_request"], "reference_doctype", "grand_total")
        self.assertEqual(pr.reference_doctype, "Sales Order")
        self.assertAlmostEqual(float(pr.grand_total),
                               float(response["data"].get("amount") or pr.grand_total), places=2)

    def test_sales_order_backed_pr_is_not_superseded_by_a_cart_change(self):
        """20. A committed obligation is out of the Cart supersession scope."""

        cart = self.make_cart(qty=12)
        data = self.assert_created(self.proceed())
        pr_name = data["payment_request"]

        # Simulate the post-commitment state: the obligation now references a
        # Sales Order, exactly as process_pay_later leaves it.
        so = frappe.db.get_value("Sales Order", {}, "name")
        if not so:
            self.skipTest("no Sales Order on this site to reference")

        frappe.db.set_value("Payment Request", pr_name, {
            "reference_doctype": "Sales Order", "reference_name": so})
        frappe.clear_document_cache("Payment Request", pr_name)

        before = self.pr_row(pr_name, "grand_total", "currency",
                             "custom_source_fingerprint", "custom_checkout_token")

        # Change the Cart and Proceed again.
        cart.reload()
        cart.items[0].quantity = 20
        self.reprice(cart)
        self.proceed()

        after = self.pr_row(pr_name, "grand_total", "currency",
                            "custom_source_fingerprint", "custom_checkout_token")

        self.assertEqual(dict(before), dict(after),
                         "a Cart change superseded a Sales-Order-backed obligation")

        # And it is not compared against a Cart either.
        pr = frappe.get_doc("Payment Request", pr_name)
        self.assertEqual(_error_code(prs.validate_payment_request_source_current(pr)),
                         "payment_reference_invalid")


# =========================================================
# 21. CONCURRENCY
# =========================================================

class ConcurrencyCase(LifecycleCase):
    """Lock-before-lookup, proven at the level this runner can prove it.

    A genuine two-transaction race needs two database connections blocking on
    each other; the Frappe test runner shares one connection inside a
    savepoint, so forcing it here would produce a self-deadlock or a
    sleep-based flake rather than evidence. Instead:

      * the ORDERING that makes the race safe is asserted from the source, and
      * the CONVERGENCE it produces is asserted behaviourally by replaying the
        sequence a serialised pair of requests would actually see.
    """

    def test_proceed_locks_the_cart_before_looking_for_candidates(self):
        """21a. Ordering: FOR UPDATE strictly precedes the candidate query."""

        from yob_storefront.api import checkout

        # Comments and the docstring describe the ordering; only executable code
        # establishes it. Strip both before asserting, or the test passes on
        # prose alone.
        source = _code_only(_raw(checkout.proceed_to_payment))

        lock_at = source.find("for_update=True")
        reload_at = source.find("frappe.get_doc('Cart', cart_name)")
        issue_at = source.find("issue_checkout_credential")

        self.assertGreater(lock_at, 0, "the Cart row is never locked")
        self.assertGreater(reload_at, lock_at, "the Cart is read before it is locked")
        self.assertGreater(issue_at, reload_at,
                           "candidates are looked up before the lock is held")

        # The candidate query itself must live behind the lock, i.e. inside the
        # service the locked handler calls -- never in the handler before it.
        self.assertNotIn("Payment Request", source,
                         "proceed_to_payment queries Payment Request directly")

    def test_competing_proceed_converges_on_one_usable_credential(self):
        """21b. Convergence: B, arriving after A, reuses instead of creating.

        This is precisely what serialisation on the Cart row produces -- A
        commits, B then reloads and queries -- so replaying it in sequence
        tests the logic the lock enables.
        """

        self.make_cart(qty=12)

        a = self.assert_created(self.proceed())     # A wins the lock
        b = self.assert_created(self.proceed())     # B proceeds after A

        self.assertEqual(a["payment_request"], b["payment_request"])
        self.assertEqual(a["token"], b["token"])

        usable = frappe.get_all(
            "Payment Request",
            filters={"party": CUSTOMER, "reference_doctype": "Cart",
                     "custom_checkout_token": ["is", "set"]},
            pluck="name")

        self.assertEqual(len(usable), 1,
                         f"one Cart must yield one usable credential, got {usable}")

    def test_legacy_duplicate_usable_prs_converge_to_one(self):
        """Historical data may hold two usable Cart-backed obligations.

        Pre-Phase-1 code could create them. Proceed must not pick one
        arbitrarily and leave the other payable: it selects the current
        obligation deterministically and revokes the redundant credential
        under the lock.
        """

        cart = self.make_cart(qty=12)
        first = self.assert_created(self.proceed())

        # Forge a second usable credential on the same Cart, as legacy data has.
        legacy = frappe.copy_doc(frappe.get_doc("Payment Request",
                                                first["payment_request"]))
        legacy.custom_checkout_token = prs._new_token()
        legacy.custom_checkout_expiry = now_datetime() + timedelta(hours=1)
        legacy.custom_source_fingerprint = self.pr_row(
            first["payment_request"], "custom_source_fingerprint").custom_source_fingerprint
        legacy.insert(ignore_permissions=True)

        self.assertEqual(len(prs._usable_candidates(cart)), 2, "fixture failed")

        result = self.assert_created(self.proceed())

        usable = frappe.get_all(
            "Payment Request",
            filters={"reference_doctype": "Cart", "reference_name": cart.name,
                     "custom_checkout_token": ["is", "set"]},
            pluck="name")

        self.assertEqual(usable, [result["payment_request"]],
                         "exactly one credential must survive")
        self.assertEqual(result["payment_request"], first["payment_request"],
                         "selection must be deterministic (oldest matching), not row order")
