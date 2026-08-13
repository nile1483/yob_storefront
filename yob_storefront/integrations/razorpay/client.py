# Copyright (c) 2026, YOB and Shayona
"""Razorpay provider adapter.

All Razorpay SDK translation lives here. API adapters and services call these
functions and never construct a ``razorpay.Client`` themselves, so credential
handling, error translation and the SDK surface have exactly one owner
(CHG-001 section 6.6).

Security note: the API secret is deliberately NOT cached. The previous
``get_razorpay_settings()`` helper wrote the decrypted secret into Redis, which
this bench runs without authentication (``use_redis_auth: false``), and the
platform security standard forbids storing secrets outside their owning
document. ``Razorpay Settings`` is a Single, so Frappe already caches the
document itself; only the decryption is repeated.
"""

import hashlib

import frappe
import razorpay


def get_credentials() -> dict:
    """Return the configured Razorpay credentials.

    ``api_key`` may be empty when the provider is not configured; callers are
    responsible for answering with ``payment_provider_not_configured`` rather
    than letting the SDK fail.
    """

    doc = frappe.get_single("Razorpay Settings")
    return {"api_key": doc.api_key, "api_secret": doc.get_password("api_secret")}


def is_configured() -> bool:
    """True when a Razorpay API key is present."""

    return bool(get_credentials().get("api_key"))


def get_client() -> razorpay.Client:
    """Build an authenticated Razorpay client."""

    credentials = get_credentials()
    return razorpay.Client(auth=(credentials["api_key"], credentials["api_secret"]))


def verify_payment_signature(order_id: str, payment_id: str, signature: str) -> None:
    """Verify a Razorpay payment signature.

    Raises the SDK's ``SignatureVerificationError`` on mismatch; the calling
    service translates that into the published ``payment_signature_invalid``
    error. This function deliberately does not catch it, so a failed
    verification can never be mistaken for success.
    """

    get_client().utility.verify_payment_signature(
        {
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        }
    )


def fetch_order(order_id: str) -> dict:
    """Fetch a Razorpay order. Logs and re-raises so the caller decides."""

    try:
        return get_client().order.fetch(order_id)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Razorpay Fetch Order Error")
        raise


def fetch_payment(payment_id: str) -> dict:
    """Fetch a Razorpay payment."""

    return get_client().payment.fetch(payment_id)


def receipt_for_payment_request(payment_request: str) -> str:
    """Deterministic provider identity for ONE immutable Payment Request.

    Razorpay's documented contract: ``receipt`` is max 40 characters, must be
    unique, and creating a second Order with the same receipt is rejected as a
    duplicate. Orders can be listed filtered by receipt, which is what makes
    recovery possible after a lost create response.

    Format: ``yob-<sha256(pr_name)[:32]>`` -- 36 chars, so it fits regardless of
    how Payment Request names are generated now or later. A hash rather than a
    truncated name because truncation can collide, and a collision here would
    mean two obligations sharing one provider order.

    Derived from the PAYMENT REQUEST, never the Cart: after commitment the Cart
    is no longer the obligation.
    """

    digest = hashlib.sha256(payment_request.encode("utf-8")).hexdigest()
    receipt = f"yob-{digest[:32]}"
    assert len(receipt) <= 40, "receipt exceeds Razorpay's 40-character limit"
    return receipt


def find_order_by_receipt(receipt: str) -> dict | None:
    """Recover a previously created Order using its deterministic receipt.

    Used when a create call may have succeeded at the provider but its response
    or our local persistence was lost. Returns None when nothing matches.

    The caller MUST still verify amount and currency against the immutable
    Payment Request before reusing the recovered order.
    """

    matches = find_orders_by_receipt(receipt)

    return matches[0] if matches else None


def find_orders_by_receipt(receipt: str) -> list:
    """ALL provider orders carrying this receipt, oldest first.

    Returns a list, not one order, because Razorpay does NOT enforce receipt
    uniqueness -- wire-verified in Test Mode, where two orders were created with
    the same receipt and both were accepted. Callers that must pick one are
    responsible for reconciling them; silently returning "the first item" would
    make which order a buyer pays depend on provider list order.

    Ordered by ``created_at`` (unix timestamp), with ``id`` breaking ties, so
    the sequence is deterministic across calls.

    NOTE: this listing is EVENTUALLY CONSISTENT. A just-created order may not
    appear for some seconds -- also wire-verified. An empty result therefore
    does NOT prove the order does not exist, and must never be treated as
    licence to create another one.
    """

    result = get_client().order.all({"receipt": receipt})

    matches = [o for o in (result or {}).get("items", [])
               if o.get("receipt") == receipt]

    matches.sort(key=lambda o: (o.get("created_at") or 0, o.get("id") or ""))

    return matches


def fetch_order_payments(order_id: str) -> dict:
    """List the payments recorded against a Razorpay order.

    Used to detect an already-captured/authorised payment before creating a
    replacement order, which is what makes retried checkouts idempotent.
    """

    return get_client().order.payments(order_id)


def create_order(amount_paise: int, currency: str, receipt: str | None = None,
                 notes: dict | None = None) -> dict:
    """Create a Razorpay order.

    ``amount_paise`` is the smallest currency unit, as Razorpay requires. The
    caller owns the conversion because it also owns the rounding rules.

    ``receipt`` and ``notes`` are omitted from the payload when not supplied, so
    the request Razorpay receives is byte-for-byte what the pre-refactor call
    sent: ``{"amount", "currency", "payment_capture"}``. CHG-002 forbids
    changing payment behaviour, and adding a field to a provider call is a
    behaviour change.
    """

    payload = {
        "amount": amount_paise,
        "currency": currency,
        "payment_capture": 1,
    }
    if receipt:
        payload["receipt"] = receipt
    if notes:
        payload["notes"] = notes

    return get_client().order.create(payload)
