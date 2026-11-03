# Copyright (c) 2026, YOB and Shayona
"""Payment Request lifecycle: issuance, bearer-token resolution, staleness.

A Payment Request is an IMMUTABLE payment obligation. Once issued it records
what the buyer agreed to pay, and nothing may later refresh it from a changed
Cart. The Cart stays mutable; when it stops matching the obligation, the answer
is ``payment_request_stale`` -- never a silently re-priced payment link.

Three responsibilities live here, and nowhere else:

1. ``resolve_checkout_token``   -- the ONE way a public endpoint turns a bearer
                                   token into a Payment Request.
2. ``validate_payment_request_source_current`` -- compare-only staleness check.
   It never writes to the Payment Request and never persists a repricing.
3. ``issue_checkout_credential`` -- the ONE way a Cart-backed obligation is
   created, reused, rotated or superseded, always under a Cart row lock.

Immutability rule, enforced by construction: every write below either
(a) creates a NEW Payment Request, or (b) touches ONLY the two credential
fields through ``frappe.db.set_value``. There is deliberately no code path that
loads an issued Payment Request and calls ``save()`` on it, because ``save()``
rewrites ``grand_total`` and ``currency`` from whatever the in-memory document
happens to hold.
"""

import secrets
from contextlib import contextmanager
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import now_datetime

from yob_storefront.api.response import (
    CART_NOT_FOUND,
    CHECKOUT_TOKEN_EXPIRED,
    CHECKOUT_TOKEN_INVALID,
    CUSTOMER_NOT_FOUND,
    HTTP_CONFLICT,
    HTTP_INTERNAL_SERVER_ERROR,
    HTTP_NOT_FOUND,
    HTTP_UNPROCESSABLE,
    INTERNAL_SERVER_ERROR,
    ORDER_NOT_FOUND,
    PAYMENT_AMOUNT_MISMATCH,
    PAYMENT_CURRENCY_MISMATCH,
    PAYMENT_REFERENCE_INVALID,
    PAYMENT_REQUEST_STALE,
    error_response,
)
from yob_storefront.services.cart_service import reprice_cart
from yob_storefront.services.order_service import create_sales_order_from_cart
from yob_storefront.services.payment_source import cart_payment_snapshot, fingerprint

#: How long an issued checkout credential stays usable.
CHECKOUT_TOKEN_TTL = timedelta(hours=1)

#: A Payment Request in one of these states is no longer a usable obligation.
CLOSED_STATUSES = ("Paid", "Cancelled")


# =========================================================
# 1. BEARER TOKEN RESOLUTION
# =========================================================

def resolve_checkout_token(token):
    """Resolve a checkout bearer token to its Payment Request.

    Returns the Payment Request document, or a ready-to-return error envelope
    (test with ``is_error``). Every public endpoint that accepts a checkout
    token MUST come through here, so none of them can drift to weaker
    semantics.

    Order matters:

    1. A blank/None/whitespace token is rejected BEFORE any query. Frappe
       renders ``{"custom_checkout_token": None}`` as ``IS NULL``, which after
       supersession matches every historical revoked Payment Request -- an
       empty token would otherwise be the master key to all of them.
    2. Exactly one row must match. More than one means the uniqueness invariant
       has been violated; we fail closed rather than pick an arbitrary
       obligation for a payment.
    3. Expiry and basic usability are checked before the caller sees the doc.

    A superseded Payment Request has ``custom_checkout_token = NULL`` and is
    therefore unreachable through its old credential, by construction.
    """

    # 1 -- reject before querying.
    if not token or not str(token).strip():
        return _invalid_token()

    token = str(token).strip()

    # 2 -- require exactly one match. limit=2 is enough to detect duplication
    # without scanning the table.
    rows = frappe.get_all(
        "Payment Request",
        filters={"custom_checkout_token": token},
        fields=["name"],
        ignore_permissions=True,
        limit=2,
    )

    if not rows:
        return _invalid_token()

    if len(rows) > 1:
        # Fail closed. The token is not logged: it is a live bearer credential.
        frappe.log_error(
            f"{len(rows)} Payment Requests share one checkout token: "
            f"{', '.join(r.name for r in rows)}",
            "YOB Checkout Token Integrity",
        )
        return error_response(
            INTERNAL_SERVER_ERROR,
            "This checkout link cannot be processed. Please try again.",
            status_code=HTTP_INTERNAL_SERVER_ERROR,
        )

    pr = frappe.get_doc("Payment Request", rows[0].name)

    # 3 -- expiry, then basic usability.
    if pr.custom_checkout_expiry and pr.custom_checkout_expiry < now_datetime():
        return error_response(
            CHECKOUT_TOKEN_EXPIRED,
            "This payment link has expired.",
            field="token",
            status_code=HTTP_UNPROCESSABLE,
        )

    if not pr.reference_doctype or not pr.reference_name:
        return _invalid_token(status_code=HTTP_UNPROCESSABLE)

    if pr.status in CLOSED_STATUSES or pr.docstatus == 2:
        return _invalid_token(status_code=HTTP_UNPROCESSABLE)

    return pr


def _invalid_token(status_code=HTTP_NOT_FOUND):
    """One answer for every unusable token, so the cases stay indistinguishable."""

    return error_response(
        CHECKOUT_TOKEN_INVALID,
        "This checkout link is not valid.",
        field="token",
        status_code=status_code,
    )


# =========================================================
# 2. COMPARE-ONLY SOURCE VALIDATION
# =========================================================

def validate_payment_request_source_current(pr):
    """Is the payment source still exactly what this obligation was issued for?

    Compare-only. This function NEVER writes ``pr.grand_total``,
    ``pr.currency`` or ``pr.custom_source_fingerprint``, and never persists the
    repricing it performs.

    Returns ``{"cart": <repriced Cart doc>, "customer": <Customer doc>,
    "snapshot": dict, "fingerprint": str}`` when current, else an error
    envelope (``payment_request_stale`` when the source moved).

    The returned Cart document is the authoritative CALCULATED state -- freshly
    loaded and repriced in memory, deliberately unsaved -- so the caller can
    build its response from it without a second calculation.
    """

    # COMMITTED, NOT "Sales-Order-backed" -- the distinction changed in 32C.
    #
    # The reference is a Sales Order from birth now, so it no longer tells the
    # two phases apart; `custom_committed_at` does. A committed obligation is
    # never compared against a Cart again: its Cart is closed, and a later cart
    # must never be able to supersede an order someone may already be paying.
    # `validate_sales_order_source` is that phase's check.
    if pr.custom_committed_at:
        return error_response(
            PAYMENT_REFERENCE_INVALID,
            "This checkout link is not valid.",
            status_code=HTTP_UNPROCESSABLE,
        )

    if not pr.custom_cart or not frappe.db.exists("Cart", pr.custom_cart):
        return error_response(
            CART_NOT_FOUND,
            "The cart for this checkout link no longer exists.",
            status_code=HTTP_NOT_FOUND,
        )

    cart = frappe.get_doc("Cart", pr.custom_cart)

    if not frappe.db.exists("Customer", cart.customer):
        return error_response(
            CUSTOMER_NOT_FOUND,
            "The customer for this checkout link no longer exists.",
            status_code=HTTP_NOT_FOUND,
        )

    customer = frappe.get_doc("Customer", cart.customer)

    # Party identity: the obligation names a party, and only that party's cart
    # may satisfy it.
    if pr.party_type != "Customer" or pr.party != cart.customer:
        return _stale("The customer for this payment has changed.")

    # Authoritative recalculation, in memory only. reprice_cart mutates the
    # document object and performs no database write of its own; the Cart is
    # never saved here, so a public GET leaves no trace. Verified by
    # test_payment_lifecycle.PublicCheckoutCase.
    #
    # Runs inside the trusted boundary: ERPNext's pricing engine calls
    # get_item_details, which permission-checks its own cached Item against the
    # execution user. The public payer is Guest and holds no such permission.
    # Every authorization decision was already made above -- the token resolved
    # this exact Payment Request, and `cart` came from its trusted reference.
    with trusted_execution():
        reprice_cart(cart, customer)

    snapshot = cart_payment_snapshot(cart)
    current = fingerprint(snapshot)

    if current != (pr.custom_source_fingerprint or ""):
        return _stale()

    # Belt and braces: the fingerprint already covers both, but comparing them
    # directly means a future snapshot change cannot silently stop guarding the
    # money.
    if not same_money(pr.grand_total, cart.grand_total):
        return _stale()

    if (pr.currency or None) != (cart.currency or None):
        return _stale()

    return {
        "cart": cart,
        "customer": customer,
        "snapshot": snapshot,
        "fingerprint": current,
    }


@contextmanager
def trusted_execution():
    """Run internal ERPNext work as the dedicated payment-processor identity.

    THE PUBLIC-PAYMENT AUTHORIZATION BOUNDARY. ``/payment/<token>`` is public,
    so the caller is Guest. Guest holds no roles and no DocType permissions, and
    must keep none. But ERPNext's controllers check permissions against the
    CURRENT EXECUTION USER on documents YOB never constructs -- notably
    ``get_item_details``, which loads its own cached Item and calls
    ``item.check_permission()``. No document flag reaches that, Frappe 16.30.0
    has no request-local bypass context (``permissions.py`` short-circuits only
    on Administrator), and a ``has_permission`` hook can deny but never grant
    (``permissions.py:495`` honours only a falsy return).

    So the execution identity is switched, briefly and narrowly.

    ENTER ONLY AFTER AUTHORIZATION. The token must already be resolved to one
    exact Payment Request, with source binding, financial invariants, party
    identity, payment state and method eligibility all validated. This context
    grants no authority over WHICH documents are touched; that was decided
    before it was entered, from token-bound server state.

    RESTORATION. ``frappe.set_user`` clobbers NINE request-local values, so a
    naive ``set_user(original)`` is not enough:

        session.user, session.sid, cache, form_dict, jenv_restricted,
        jenv_unrestricted, session.data, role_permissions,
        new_doc_templates, user_perms

    ``set_user(original)`` is therefore called FIRST, so Frappe performs its own
    reset of the caches, role permissions, user perms and Jinja environments --
    that is what stops the processor's privileged permission state leaking into
    the rest of the request. Only then are the three values Frappe cannot
    reconstruct put back: ``sid`` (which set_user overwrites with the username),
    ``session.data`` and ``form_dict`` (both blanked, and nothing repopulates
    them mid-request).

    Always restores, including on exception.
    """

    from yob_storefront.install import PAYMENT_PROCESSOR_USER

    original_user = frappe.session.user
    original_sid = frappe.session.sid
    original_data = frappe.session.data
    original_form_dict = frappe.local.form_dict

    try:
        frappe.set_user(PAYMENT_PROCESSOR_USER)
        yield
    finally:
        # First: let Frappe clear every privileged cache it owns.
        frappe.set_user(original_user)

        # Then: restore what set_user destroys and cannot rebuild.
        frappe.session.sid = original_sid
        frappe.session.data = original_data
        frappe.local.form_dict = original_form_dict


def validate_sales_order_source(pr):
    """Is the committed Sales Order still this obligation's order?

    The post-commitment counterpart of
    ``validate_payment_request_source_current``. Returns the Sales Order
    document or an error envelope.

    Deliberately NOT a fingerprint comparison. The Cart fingerprint on the
    Payment Request is historical evidence of the obligation it was ISSUED for;
    once the reference is a Sales Order that hash describes a document the
    payment no longer points at. What must hold after commitment is identity:
    same party, same money, same currency, and an order that can still be paid.

    Used by the public checkout page, the commitment service's
    already-committed branch, and payment settlement -- one authority, so those
    three cannot disagree about whether an order is still payable.
    """

    if pr.reference_doctype != "Sales Order":
        return error_response(
            PAYMENT_REFERENCE_INVALID,
            "This checkout link is not valid.",
            status_code=HTTP_UNPROCESSABLE,
        )

    if not pr.reference_name or not frappe.db.exists("Sales Order", pr.reference_name):
        return error_response(
            ORDER_NOT_FOUND,
            "The order for this payment no longer exists.",
            status_code=HTTP_NOT_FOUND,
        )

    so = frappe.get_doc("Sales Order", pr.reference_name)

    # 2 = Cancelled. A cancelled order can never back a payment.
    if so.docstatus == 2:
        return error_response(
            PAYMENT_REFERENCE_INVALID,
            "This order can no longer be paid.",
            status_code=HTTP_UNPROCESSABLE,
        )

    if pr.party_type != "Customer" or pr.party != so.customer:
        return error_response(
            PAYMENT_REFERENCE_INVALID,
            "This payment does not match its order.",
            status_code=HTTP_UNPROCESSABLE,
        )

    if not same_money(pr.grand_total, so.grand_total):
        return error_response(
            PAYMENT_AMOUNT_MISMATCH,
            "The order amount no longer matches this payment.",
            status_code=HTTP_CONFLICT,
        )

    if (pr.currency or None) != (so.currency or None):
        return error_response(
            PAYMENT_CURRENCY_MISMATCH,
            "The order currency no longer matches this payment.",
            status_code=HTTP_CONFLICT,
        )

    return so


def _stale(detail=None):
    return error_response(
        PAYMENT_REQUEST_STALE,
        detail or "Your cart has changed since this payment was started. "
                  "Please return to the cart and continue again.",
        status_code=HTTP_CONFLICT,
    )


def same_money(a, b) -> bool:
    """Currency equality at Frappe's practical precision."""

    return abs(float(a or 0) - float(b or 0)) < 0.005


# =========================================================
# 3. ISSUANCE / REUSE / ROTATION / SUPERSESSION
# =========================================================

def issue_checkout_credential(cart, customer):
    """Return the ONE currently usable Cart-backed obligation for this Cart.

    The caller MUST already hold a ``FOR UPDATE`` lock on the Cart row and must
    have reloaded and repriced the Cart under that lock. Candidate lookup
    happens here, i.e. strictly after the lock, which is what makes two
    competing Proceed requests converge instead of both creating a Payment
    Request.

    Returns ``{"payment_request": doc-or-name, "token": str, "created": bool}``.

    Three outcomes:

    * obligation unchanged, token live      -> reuse both, write nothing
    * obligation unchanged, token expired   -> rotate ONLY the credential
    * obligation changed, or none exists    -> issue a replacement, then revoke
                                               the old credential(s)
    """

    current_fingerprint = fingerprint(cart_payment_snapshot(cart))
    candidates = _usable_candidates(cart)

    match = _select_matching(candidates, cart, current_fingerprint)

    if match:
        # Any other usable credential for this Cart is redundant; one Cart may
        # only have one live credential. Safe to revoke here: we hold the lock.
        for row in candidates:
            if row.name != match.name:
                _revoke_credential(row.name)

        if match.custom_checkout_expiry and match.custom_checkout_expiry > now_datetime():
            return {"payment_request": match.name, "token": match.custom_checkout_token,
                    "created": False}

        # Same financial obligation, dead credential: rotate the credential
        # only. grand_total, currency and the fingerprint are not touched.
        token = _rotate_credential(match.name)
        return {"payment_request": match.name, "token": token, "created": False}

    # Changed obligation (or first ever). Create the replacement FIRST, and only
    # revoke the old credentials once it is safely stored -- so a failure can
    # never leave the buyer with no way to pay.
    savepoint = "yob_pr_supersede"
    frappe.db.savepoint(savepoint)
    touched = [row.name for row in candidates]

    try:
        pr, token = _create_payment_request(cart, current_fingerprint)

        for row in candidates:
            _revoke_credential(row.name)

    except Exception:
        frappe.db.rollback(save_point=savepoint)
        # Rollback restores the database but NOT Frappe's document cache, and
        # the API boundary may turn this into an envelope inside the same
        # request. Anything we may have written must be re-read from the DB.
        for name in touched:
            frappe.clear_document_cache("Payment Request", name)
        raise

    return {"payment_request": pr.name, "token": token, "created": True}


def _usable_candidates(cart):
    """Cart-backed Payment Requests that still hold a live checkout credential.

    Scope rules:

    * Cart-backed only. A Sales-Order-backed Payment Request is a committed
      obligation and is never a candidate, so changing a Cart cannot supersede
      one.
    * Same party as the Cart.
    * Not closed, not cancelled.
    * ``custom_checkout_token`` actually set. A revoked (superseded, or paid)
      Payment Request is history: it must never be selected as the current
      obligation. ``["is", "set"]`` renders as ``!= ''`` without COALESCE, so
      both NULL and empty-string tokens are excluded.

    An EXPIRED credential is still a candidate: expiry kills the credential,
    not the obligation, so it stays eligible for rotation.
    """

    return frappe.get_all(
        "Payment Request",
        filters={
            # Selected by `custom_cart`, not by the reference: since Phase 32C
            # the reference is this attempt's own Sales Order, so it identifies
            # the SNAPSHOT rather than the source. The Cart link is what groups
            # the attempts made for one cart.
            "custom_cart": cart.name,
            # An uncommitted attempt only. Once the Cart has been closed against
            # its order there is nothing left to supersede, and reusing that
            # attempt for a new cart is exactly what must never happen.
            "custom_committed_at": ["is", "not set"],
            "party_type": "Customer",
            "party": cart.customer,
            "docstatus": ["<", 2],
            "status": ["not in", list(CLOSED_STATUSES)],
            "custom_checkout_token": ["is", "set"],
        },
        fields=[
            "name", "grand_total", "currency", "custom_source_fingerprint",
            "custom_checkout_token", "custom_checkout_expiry", "creation",
            "reference_name",
        ],
        order_by="creation asc, name asc",
        ignore_permissions=True,
    )


def _select_matching(candidates, cart, current_fingerprint):
    """Deterministically pick the candidate that IS the current obligation.

    Legacy data may hold more than one usable Cart-backed Payment Request --
    the pre-Phase-1 code could create them. Returning "the first row" would make
    which obligation a buyer pays depend on row order, so selection is explicit:
    only a full financial match qualifies, and among matches the OLDEST wins
    (the original issuance, ordered by creation then name). The rest are revoked
    by the caller under the Cart lock.
    """

    for row in candidates:               # already ordered creation asc, name asc
        if (row.custom_source_fingerprint or "") != current_fingerprint:
            continue
        if not same_money(row.grand_total, cart.grand_total):
            continue
        if (row.currency or None) != (cart.currency or None):
            continue
        return row

    return None


def _create_payment_request(cart, source_fingerprint):
    """Issue ONE payment attempt: an immutable Draft Sales Order, then its
    Payment Request.

    ONE ATTEMPT = ONE SNAPSHOT
    --------------------------
    The Sales Order built here is this attempt's immutable record of what was
    offered. It is never re-costed, never re-pointed at another obligation, and
    never reused by a different attempt: a changed Cart supersedes the whole
    attempt and the replacement builds its own Sales Order. That is what keeps
    "which order did this payment pay for?" answerable from the Payment Request
    alone, including for a payment that arrives after supersession.

    WHY THE REFERENCE IS A SALES ORDER
    ----------------------------------
    ERPNext 16.52.1 enforces `ALLOWED_DOCTYPES_FOR_PAYMENT_REQUEST` inside
    `validate_reference_document()`, which runs on every insert, and `Cart` is
    not in that list. The reference is therefore a Sales Order from birth --
    which is also where it already ended up, because commitment used to re-point
    it. This shortens the Cart-backed window to zero rather than inventing a new
    model. The Cart link moves to `custom_cart`.

    TRUSTED EXECUTION IS REQUIRED HERE
    ----------------------------------
    16.52.1's `Payment Request.validate()` also calls
    `ref_doc.check_permission("read")`, and a storefront buyer deliberately holds
    no Sales Order read (`permissions/storefront_role`). The order build and the
    insert therefore share the SAME trusted boundary the commitment already used,
    entered only after `proceed_to_payment` has authenticated the caller and
    locked and repriced the Cart.
    """

    token = _new_token()

    with trusted_execution():
        so = create_sales_order_from_cart(cart)

        # The obligation must equal its own snapshot, checked before the Payment
        # Request exists rather than after. ERPNext compares `grand_total`
        # against the reference during validate(), so a mismatch would otherwise
        # surface as an opaque ERPNext refusal instead of a YOB invariant.
        if not same_money(so.grand_total, cart.grand_total) or so.currency != cart.currency:
            frappe.throw(
                _("The order snapshot does not match the cart it was built from."),
                frappe.ValidationError,
            )

        _grant_processor_read(so.name)

        pr = frappe.get_doc({
            "doctype": "Payment Request",
            "payment_request_type": "Inward",
            "party_type": "Customer",
            "party": cart.customer,
            "reference_doctype": "Sales Order",
            "reference_name": so.name,
            "grand_total": so.grand_total,
            "currency": so.currency,
            "email_to": frappe.session.user,
            "subject": f"Payment for Order {so.name}",
            "custom_cart": cart.name,
            "custom_source_fingerprint": source_fingerprint,
            "custom_checkout_token": token,
            "custom_checkout_expiry": now_datetime() + CHECKOUT_TOKEN_TTL,
        })
        pr.insert(ignore_permissions=True)

    return pr, token


def _grant_processor_read(sales_order):
    """Let the trusted processor read THIS attempt's order, and no other.

    ERPNext 16.52.1's `Payment Request.validate()` calls
    `ref_doc.check_permission("read")` on the referenced Sales Order. The
    processor identity deliberately holds no Sales Order role permission, so the
    insert would be refused. A document-specific `DocShare` satisfies exactly
    that check -- Frappe's `has_permission` falls through to
    `false_if_not_shared()`, which matches on `share_name` (frappe/permissions.py
    :179-199) -- WITHOUT making every other Sales Order readable. Verified: an
    unshared order is still refused, and a Payment Request against one still
    raises PermissionError.

    READ AND NOTHING ELSE. `write`, `share`, `submit` and `everyone` are passed
    explicitly rather than left to defaults, for the same reason the role's
    Custom DocPerm spells out its zeros: a default that changes upstream must
    not silently widen this grant.

    `ignore_share_permission` controls who may CREATE the share -- neither the
    buyer nor the processor holds `share` on Sales Order. It does not bypass the
    permission check itself, which still runs and still refuses unshared orders.
    It is NOT `ignore_permissions`.

    Idempotent, and transactional: the share is written inside the caller's
    transaction alongside the Sales Order and the Payment Request, so a rollback
    takes it with them and can never leave an orphaned grant behind.
    """

    from yob_storefront.install import PAYMENT_PROCESSOR_USER

    if frappe.db.exists("DocShare", {
        "share_doctype": "Sales Order",
        "share_name": sales_order,
        "user": PAYMENT_PROCESSOR_USER,
    }):
        return

    frappe.share.add(
        "Sales Order",
        sales_order,
        user=PAYMENT_PROCESSOR_USER,
        read=1,
        write=0,
        share=0,
        submit=0,
        everyone=0,
        flags={"ignore_share_permission": True},
    )


def _rotate_credential(pr_name):
    """Replace ONLY the checkout credential on an existing obligation.

    ``db.set_value`` rather than ``doc.save()`` on purpose: ``save()`` rewrites
    every field from the in-memory document, which is exactly the class of
    accident this phase exists to make impossible. The old token stops resolving
    the moment this row is updated.
    """

    token = _new_token()

    frappe.db.set_value("Payment Request", pr_name, {
        "custom_checkout_token": token,
        "custom_checkout_expiry": now_datetime() + CHECKOUT_TOKEN_TTL,
    })
    frappe.clear_document_cache("Payment Request", pr_name)

    return token


def _revoke_credential(pr_name):
    """Supersede a Payment Request by destroying its bearer credential.

    The Draft Payment Request itself is deliberately left alone -- not
    submitted, not cancelled, financial fields untouched. It remains an accurate
    historical record of an obligation that was offered; only the ability to pay
    against it is withdrawn.
    """

    frappe.db.set_value("Payment Request", pr_name, {
        "custom_checkout_token": None,
        "custom_checkout_expiry": None,
    })
    frappe.clear_document_cache("Payment Request", pr_name)


def _new_token() -> str:
    """256 bits of URL-safe randomness. Never logged."""

    return secrets.token_urlsafe(32)
