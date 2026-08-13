# Copyright (c) 2026, YOB and Shayona
"""Razorpay driver.

Order creation is delegated to the installed Frappe Payments Razorpay
controller. YOB retains the capabilities Payments does not provide: deterministic
receipt correlation, recovery, order fetch, order-payments listing and
server-side HMAC verification (its own `verify_signature` has zero callers in
the Orders flow).

WIRE-VERIFIED PROVIDER LIMITATIONS -- both contradict Razorpay's documentation
and were proven against real Test Mode:

* `receipt` is NOT unique. Creating a second order with the same receipt
  SUCCEEDS and returns a different order id.
* the receipt listing is EVENTUALLY CONSISTENT. A just-created order can be
  invisible to `order.all({"receipt": ...})` for several seconds.

So Razorpay's Orders API gives us no idempotent-create primitive, and the
receipt is a CORRELATION key, never an idempotency key. What YOB guarantees is
therefore not "one physical Razorpay order per Payment Request" -- we cannot
enforce that -- but:

    one Payment Request -> one CANONICAL, exposed Razorpay order

delivered by:

* a durable creation claim written and committed BEFORE the network call, so
  only one request may ever issue a create;
* exactly one outbound create per unresolved claim;
* recovery-only handling of every ambiguous outcome, with bounded polling for
  the propagation delay;
* deterministic receipt correlation for that recovery;
* canonical provider-reference persistence, which always wins afterwards;
* settlement against that exact canonical order and no other.

An empty receipt listing NEVER licenses another create. That inference is
precisely how duplicate orders were produced during wire verification.

Frappe Payments' hosted checkout is deliberately not used: no
``get_payment_url``, no ``create_request``, no ``authorize_payment``, no
redirect pages. The SPA opens Checkout.js with the payload this driver returns.
"""

import time
from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe.utils import now_datetime

from yob_storefront.integrations.gateways.base import (
    CAP_RECOVER,
    CAP_SERVER_VERIFY,
    Obligation,
    ProviderAlreadyPaid,
    ProviderError,
    ProviderIntegrityError,
    ProviderIntent,
    ProviderNotConfigured,
    ProviderPreflightFailed,
    ProviderResult,
    YOBGateway,
)
from yob_storefront.integrations.gateways.registry import register
from yob_storefront.integrations.razorpay import client as razorpay_client

#: Name of the Frappe `Payment Gateway` this driver serves.
PROVIDER = "Razorpay"

#: Bounded recovery polling. The receipt listing is eventually consistent, so a
#: just-created order may be invisible for a few seconds. Kept small: a public
#: request must not hang, and failing to find the order is SAFE -- the next
#: retry recovers again, whereas creating another order is not recoverable.
RECOVERY_ATTEMPTS = 3
RECOVERY_DELAY_SECONDS = 1.0


def _sleep(seconds: float) -> None:
    """Indirection so tests exercise the backoff branching without waiting."""

    time.sleep(seconds)


def _is_attempted(order: dict) -> bool:
    """Has money been moved, or tried, against this provider order?"""

    return bool(
        int(order.get("attempts") or 0) > 0
        or order.get("amount_paid")
        or order.get("status") in ("attempted", "paid")
    )


@register
class RazorpayGateway(YOBGateway):

    provider = PROVIDER
    client_sdk = "razorpay-checkout-v1"

    # ------------------------------------------------------------------
    # Frappe Payments configuration
    # ------------------------------------------------------------------

    def controller(self):
        """The Frappe Payments gateway controller for Razorpay.

        Resolved through the Payments app rather than by loading
        ``Razorpay Settings`` directly, so the Payment Gateway record stays the
        single point of configuration and a site that repoints
        ``gateway_controller`` is honoured.

        Falls back to the Settings Single when no Payment Gateway record exists
        yet -- ``get_payment_gateway_controller`` itself does the same when
        ``gateway_controller`` is unset, and a site mid-migration must not lose
        the ability to take payments.
        """

        try:
            from payments.utils.utils import get_payment_gateway_controller

            return get_payment_gateway_controller(PROVIDER)
        except Exception:
            return frappe.get_single("Razorpay Settings")

    def capabilities(self) -> frozenset:
        return frozenset({CAP_RECOVER, CAP_SERVER_VERIFY})

    def public_key(self) -> str:
        """The PUBLISHABLE key. The secret never leaves the credential layer."""

        return self.controller().api_key

    def assert_configured(self) -> None:
        """Both credentials must exist, because the controller requires both.

        The Payments controller guards order creation with
        ``if self.api_key and self.api_secret:`` and has no else branch -- a
        missing secret makes it return None rather than raise. Checking both
        here turns that silent nothing into an explicit preflight failure,
        before anything is committed.

        Only PRESENCE is tested. The secret is never read into a variable that
        could be logged, and never returned.
        """

        controller = self.controller()

        has_secret = bool(
            controller.get_password("api_secret", raise_exception=False))

        if not self.public_key() or not has_secret:
            # Server-side misconfiguration, not something the caller can fix.
            frappe.log_error("Razorpay credentials are incomplete",
                             "Razorpay Not Configured")
            raise ProviderNotConfigured(PROVIDER)

    def validate_currency(self, currency: str) -> None:
        """Delegate to the Payments controller's own currency rules.

        ``validate_transaction_currency`` checks the currency against the
        controller's ``supported_currencies`` tuple and ``frappe.throw``s
        otherwise. Frappe Payments maintains that list per gateway, which is
        exactly the kind of provider metadata YOB should not be keeping its own
        copy of.

        Its message is translated and provider-worded ("Please select another
        payment method..."), so it is caught and re-raised as a preflight
        failure: the API layer owns what reaches the client.
        """

        if not currency:
            raise ProviderPreflightFailed("No currency on the payment obligation.")

        try:
            self.controller().validate_transaction_currency(currency)
        except Exception as exc:
            raise ProviderPreflightFailed(
                f"{PROVIDER} does not support transactions in {currency}."
            ) from exc

    # ------------------------------------------------------------------
    # Provider operations
    # ------------------------------------------------------------------

    def prepare_payment(self, obligation: Obligation) -> ProviderIntent:
        """Return THE Razorpay order for this obligation, creating only if needed.

        One immutable Payment Request maps to at most one effective provider
        order. Retrying is never solved by creating a new Payment Request, Sales
        Order or provider order -- it is solved by the deterministic receipt,
        derived from the Payment Request name, which lets a lost or ambiguous
        create be recovered instead of duplicated.

        Order of preference:

        1. a locally stored provider order that still matches this obligation;
        2. creation through the Frappe Payments controller;
        3. recovery by deterministic receipt when creation failed OR returned an
           uncertain result.

        Step 3 is driven by IDENTITY, never by parsing an exception. The
        installed Payments ``create_order`` catches everything and re-raises one
        generic "Could not create razorpay order", so Razorpay's duplicate
        receipt error never reaches us intact. A receipt lookup answers both
        "it already existed" and "our response was lost" without depending on
        any message text.
        """

        pr = obligation.payment_request
        receipt = razorpay_client.receipt_for_payment_request(pr.name)

        # 1 -- canonical order already known. Always wins.
        if pr.custom_razorpay_order_id:
            order = self._reuse_stored_order(obligation, receipt)

            if order:
                return self._intent(obligation, order, reused=True)

        # 2 -- durable claim, taken BEFORE any network call.
        claimed = self._claim_creation(pr)

        if not claimed:
            # Another request already issued a create for this obligation, or
            # a previous attempt ended without a trustworthy order id. Either
            # way the provider may already hold an order, so this request is
            # RECOVERY ONLY -- it must never create.
            return self._recover_only(obligation, receipt)

        # 3 -- we hold the claim, so exactly one create is issued for it.
        order = self._create_via_payments(obligation, receipt)

        if order is None:
            # Ambiguous: the create may have reached Razorpay. The claim stays
            # set, so no future request will create either. Recover instead.
            return self._recover_only(obligation, receipt)

        self._assert_order_matches(order, obligation, receipt)

        self._persist(pr, {
            "custom_razorpay_order_id": order["id"],
            "custom_razorpay_status": order.get("status"),
        })

        return self._intent(obligation, order)

    # ------------------------------------------------------------------
    # Durable creation claim
    # ------------------------------------------------------------------

    def _claim_creation(self, pr) -> bool:
        """Take the exclusive right to issue ONE provider create. Durable.

        Returns True when this request may create, False when it must recover.

        Why a durable claim rather than provider idempotency: Razorpay's Orders
        API gives us none. Wire verification proved receipts are NOT unique --
        the same receipt created two orders -- and that receipt lookup is
        eventually consistent, so "look before you create" has a window in which
        a just-created order is invisible. The only place we can serialise
        reliably is our own database, before the network.

        Sequence, and every part of it matters:

            FOR UPDATE the Payment Request row   <- serialise competing requests
            re-read claim + canonical order id   <- decide on durable truth
            write the claim
            COMMIT                               <- durable, AND releases the lock
            (only now) call the provider

        The commit is what makes the claim survive a crash, a worker death or an
        HTTP timeout -- an in-memory flag would not. It also releases the row
        lock BEFORE the network call, so no lock is ever held across provider
        I/O.
        """

        frappe.db.get_value("Payment Request", pr.name, "name", for_update=True)

        row = frappe.db.get_value(
            "Payment Request", pr.name,
            ["custom_razorpay_order_id", "custom_provider_claim_at"],
            as_dict=True) or frappe._dict()

        # Re-checked under the lock: a competitor may have finished since step 1.
        if row.custom_razorpay_order_id or row.custom_provider_claim_at:
            frappe.db.commit()          # release the lock; we will only recover
            return False

        frappe.db.set_value("Payment Request", pr.name,
                            "custom_provider_claim_at", now_datetime())
        frappe.clear_document_cache("Payment Request", pr.name)

        # Durable BEFORE the network, and the lock is released here.
        frappe.db.commit()

        return True

    def _recover_only(self, obligation: Obligation, receipt: str) -> ProviderIntent:
        """Find the order a previous attempt may have created. Never create.

        Bounded polling, because the receipt listing is eventually consistent:
        a create that succeeded seconds ago may not be visible yet. Timing is
        module-level and injectable so tests exercise the real branching without
        real sleeps.

        If nothing becomes visible we deliberately STOP rather than create
        again. That is the whole point of this design: an empty listing does not
        prove absence, and creating on that basis is exactly how duplicate
        orders were produced during wire verification. The obligation stays in a
        recoverable provider-pending state and the next retry recovers again.
        """

        pr = obligation.payment_request

        for attempt in range(RECOVERY_ATTEMPTS):
            order = self._resolve_receipt_matches(obligation, receipt)

            if order:
                self._assert_order_matches(order, obligation, receipt)
                self._persist(pr, {
                    "custom_razorpay_order_id": order["id"],
                    "custom_razorpay_status": order.get("status"),
                })
                return self._intent(obligation, order, reused=True)

            if attempt < RECOVERY_ATTEMPTS - 1:
                _sleep(RECOVERY_DELAY_SECONDS * (2 ** attempt))

        raise ProviderError(
            f"{PROVIDER} order for {pr.name} is not yet visible; recovery "
            f"will be retried"
        )

    def _resolve_receipt_matches(self, obligation: Obligation, receipt: str):
        """Pick THE canonical order among everything sharing this receipt.

        Duplicates are possible (wire-verified), so this cannot assume one
        match. Rules, in order:

        * ignore anything whose amount/currency is not this obligation -- a
          shared receipt does not make an order ours;
        * if more than one match has been ATTEMPTED or PAID, fail closed. Money
          may have moved against more than one order and picking either could
          settle the wrong one; that needs a human, not a heuristic;
        * if exactly one has been attempted, that is the order the buyer
          actually used;
        * otherwise every match is an untouched `created` order, so the OLDEST
          is canonical -- deterministic, and stable across retries.
        """

        candidates = [
            order for order in razorpay_client.find_orders_by_receipt(receipt)
            if int(order.get("amount") or 0) == obligation.amount_minor
            and order.get("currency") == obligation.currency
        ]

        if not candidates:
            return None

        attempted = [o for o in candidates if _is_attempted(o)]

        if len(attempted) > 1:
            # Never settle the wrong paid order merely because it was oldest.
            frappe.log_error(
                f"{len(attempted)} attempted {PROVIDER} orders share receipt "
                f"{receipt}: {', '.join(o.get('id', '?') for o in attempted)}",
                "YOB Provider Integrity",
            )
            raise ProviderIntegrityError(
                f"multiple attempted {PROVIDER} orders share one receipt")

        if attempted:
            return attempted[0]

        return candidates[0]          # oldest; find_orders_by_receipt sorts

    def _create_via_payments(self, obligation: Obligation, receipt: str):
        """Create the order through the Frappe Payments controller.

        Returns the order, or None when the outcome is UNCERTAIN -- the caller
        then recovers by receipt rather than creating again.

        AMOUNT UNITS, and why this is the most dangerous line in the file: the
        controller takes the amount in BUSINESS units and multiplies by 100
        itself (verified against the installed source, and pinned by a
        regression test). Passing ``obligation.amount_minor`` here would bill a
        hundred times the obligation.

        Decimal rather than float, because ``135.0 * 100`` is not reliably
        13500 in binary floating point and this value becomes money.
        """

        controller = self.controller()

        try:
            order = controller.create_order(
                amount=self._business_amount(obligation),   # MAJOR units
                currency=obligation.currency,
                receipt=receipt,
                payment_capture=1,
            )
        except Exception:
            # The controller wraps every provider failure in one generic error,
            # so this tells us nothing about whether the order now exists at
            # Razorpay. Treat it as uncertain and let identity decide.
            frappe.log_error(frappe.get_traceback(),
                             "Razorpay Create Order - Recovering by receipt")
            return None

        if not order or not order.get("id"):
            # `create_order` returns None when credentials are absent; preflight
            # should have caught that, so this is belt and braces.
            return None

        return order

    @staticmethod
    def _business_amount(obligation: Obligation) -> Decimal:
        """The obligation in MAJOR units, for the Payments controller."""

        pr = obligation.payment_request

        return Decimal(str(pr.grand_total)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)

    def _reuse_stored_order(self, obligation: Obligation, receipt: str):
        """Reuse the stored provider order when still valid, else None.

        A provider failure here is not fatal: falling through to create/recover
        is correct, and the deterministic receipt guarantees that path converges
        on one order rather than making a second.
        """

        pr = obligation.payment_request

        try:
            order = razorpay_client.fetch_order(pr.custom_razorpay_order_id)

            self._persist(pr, {"custom_razorpay_status": order.get("status")})

            # An already captured/authorised payment must never be re-collected.
            payments = razorpay_client.fetch_order_payments(
                pr.custom_razorpay_order_id)

            for payment in payments.get("items", []):
                if payment.get("status") in ("captured", "authorized"):
                    raise ProviderAlreadyPaid(pr.name)

            # Reuse only an order that is still open AND still describes this
            # obligation.
            if (
                order.get("status") == "created"
                and int(order.get("amount") or 0) == obligation.amount_minor
                and order.get("currency") == obligation.currency
                and order.get("receipt") == receipt
            ):
                return order

        except ProviderAlreadyPaid:
            raise

        except Exception:
            frappe.log_error(frappe.get_traceback(),
                             "Invalid Razorpay Order - Recovering")

        return None

    def _assert_order_matches(self, order: dict, obligation: Obligation,
                              receipt: str) -> None:
        """A provider order may back this obligation only if it IS this obligation.

        Applied to created AND recovered orders alike. The amount comparison is
        the one that catches a units mistake: ``order["amount"]`` is what
        Razorpay actually recorded, and it must equal the obligation's minor
        amount exactly.
        """

        if not order or not order.get("id"):
            raise ProviderError(f"{PROVIDER} did not return an order")

        if order.get("receipt") != receipt:
            raise ProviderError(f"{PROVIDER} order receipt does not match")

        if int(order.get("amount") or 0) != obligation.amount_minor:
            raise ProviderError(
                f"{PROVIDER} order amount {order.get('amount')} does not match "
                f"obligation {obligation.amount_minor}")

        if order.get("currency") != obligation.currency:
            raise ProviderError(f"{PROVIDER} order currency does not match")

    @staticmethod
    def _persist(pr, values: dict) -> None:
        """Narrow write of provider metadata onto an issued Payment Request.

        ``db.set_value`` and never ``pr.save()``: a whole-document save would
        rewrite ``grand_total`` and ``currency`` from the in-memory document,
        which is exactly the mutation the immutable-obligation model forbids.
        """

        frappe.db.set_value("Payment Request", pr.name, values)

        for field, value in values.items():
            pr.set(field, value)

        frappe.clear_document_cache("Payment Request", pr.name)

    def recover_payment(self, obligation: Obligation) -> ProviderIntent | None:
        """Find this obligation's existing Razorpay order, or None.

        Identity comes from the deterministic receipt derived from the
        immutable Payment Request name, so recovery works even when the local
        order id was never persisted.
        """

        receipt = razorpay_client.receipt_for_payment_request(
            obligation.reference)

        order = razorpay_client.find_order_by_receipt(receipt)

        if not order:
            return None

        return self._intent(obligation, order, reused=True)

    def verify_payment(self, obligation: Obligation,
                       provider_payload: dict) -> ProviderResult:
        """Verify a Checkout.js callback server-side.

        Signature verification is Razorpay's HMAC over ``order_id|payment_id``,
        performed by the SDK. Frappe Payments has an equivalent helper that
        nothing in its Orders flow calls, so this stays a YOB responsibility.

        Not wired into the live settlement path in Phase A -- ``verify_payment``
        resolves its Payment Request from the provider order id and does not
        know the Payment Method. See the Phase B plan.
        """

        order_id = provider_payload["razorpay_order_id"]
        payment_id = provider_payload["razorpay_payment_id"]

        razorpay_client.verify_payment_signature(
            order_id, payment_id, provider_payload["razorpay_signature"])

        payment = razorpay_client.fetch_payment(payment_id)

        return ProviderResult(
            provider=PROVIDER,
            settled=payment.get("status") == "captured",
            provider_reference=order_id,
            provider_payment_reference=payment_id,
            amount_minor=payment.get("amount"),
            currency=payment.get("currency"),
            raw=payment,
        )

    # ------------------------------------------------------------------

    def _intent(self, obligation: Obligation, order: dict,
                reused: bool = False) -> ProviderIntent:
        return ProviderIntent(
            provider=PROVIDER,
            client_sdk=self.client_sdk,
            provider_reference=order["id"],
            # Opaque to YOB core; the SPA hands it to Checkout.js.
            client_payload={"key": self.public_key(), "order_id": order["id"]},
            amount_minor=obligation.amount_minor,
            currency=obligation.currency,
            reused=reused,
        )
