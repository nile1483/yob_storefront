# Copyright (c) 2026, YOB and Shayona
"""Local commercial commitment: Payment Request -> ONE Draft Sales Order.

    immutable Payment Request
             |
        current Cart
             |
      ONE Draft Sales Order
             |
    PR reference becomes Sales Order

This service owns exactly that transition and nothing else. It contains no
Razorpay, no Pay Later, no provider dispatch and no settlement: those decide
HOW money arrives, while this decides WHAT was committed to. Keeping them apart
is what lets a retry after a lost provider response be safe -- the durable local
obligation already exists and is found again rather than duplicated.

NOT WIRED IN PHASE 2A. ``process_payment`` still runs the old lifecycle. See
``ensure_payment_request_committed`` for the caller contract Phase 2B must
honour, and the transaction note at the bottom of this docstring.

Lock ordering (non-negotiable)
------------------------------
``proceed_to_payment`` locks the Cart and then updates/revokes Cart-backed
Payment Requests. This service must therefore take the SAME direction:

    resolve PR (no lock) -> find its Cart -> FOR UPDATE Cart
    -> FOR UPDATE Payment Request -> reload both -> revalidate everything

Locking the Payment Request first and then waiting for the Cart would invert
the order against Proceed and deadlock for no benefit. The initial unlocked
read exists only to discover which Cart to lock; nothing is decided from it,
because anything read before waiting on a lock may be stale by the time the
lock is granted.

Transaction boundary
--------------------
This service does NOT call ``frappe.db.commit()``. The commitment it performs
is atomic within the caller's transaction, and the row locks it holds stay held
until that transaction ends. That is deliberate: the locks are what stop a
competing request from committing the same Cart twice, so releasing them early
would destroy the guarantee. Phase 2B's ``process_payment`` must commit
explicitly AFTER this returns and BEFORE any provider call, so the local
obligation is durable before an external side effect can reference it.
"""

import frappe
from frappe.utils import now_datetime

from yob_storefront.api.response import (
    CART_NOT_FOUND,
    HTTP_NOT_FOUND,
    HTTP_UNPROCESSABLE,
    PAYMENT_REFERENCE_INVALID,
    error_response,
    is_error,
)
from yob_storefront.services.order_service import create_sales_order_from_cart
from yob_storefront.services.payment_request_service import (
    resolve_checkout_token,
    same_money,
    trusted_execution,
    validate_payment_request_source_current,
    validate_sales_order_source,
)


def ensure_payment_request_committed(payment_request=None, token=None):
    """Return the ONE Draft Sales Order committing this Payment Request.

    Idempotent. Creates the Sales Order on first call; every later call for the
    same Payment Request returns the same one.

    Pass ``token`` (public bearer flow) or ``payment_request`` (a name or doc,
    for trusted internal callers). When ``token`` is supplied it is revalidated
    AFTER the locks are taken, so a credential superseded while this request
    waited cannot commit.

    Returns ``{"payment_request": name, "sales_order": doc, "created": bool}``
    or a ready-to-return error envelope (test with ``is_error``).

    CALLER CONTRACT -- Phase 2B must honour all four:

    1. Row locks on the Cart and the Payment Request are held on return and
       released only when the caller's transaction ends. Do not release them
       before the PR->SO transition is durable.
    2. This service does not commit. Commit explicitly after a successful
       return and before any provider call.
    3. On an error envelope the caller's transaction has already been restored
       to the pre-commitment state by this service; the caller must not treat a
       returned envelope as "nothing happened elsewhere" and must not commit.
    4. Payment Method eligibility is NOT checked here. The caller re-checks it
       authoritatively via ``payment_method_service.is_payment_method_eligible``
       against the immutable Payment Request amount.
    """

    pr = _resolve(payment_request, token)

    if is_error(pr):
        return pr

    # An already-committed obligation never looks for a Cart. Checked before
    # locking, and again after, because the answer can change while waiting.
    if pr.reference_doctype == "Sales Order":
        return _existing_commitment(pr)

    if pr.reference_doctype != "Cart":
        return error_response(
            PAYMENT_REFERENCE_INVALID,
            "This checkout link is not valid.",
            status_code=HTTP_UNPROCESSABLE,
        )

    cart_name = pr.reference_name

    if not frappe.db.exists("Cart", cart_name):
        return error_response(
            CART_NOT_FOUND,
            "The cart for this checkout link no longer exists.",
            status_code=HTTP_NOT_FOUND,
        )

    # ---------------------------------------------------------------
    # Locks, in Proceed's order: Cart first, then Payment Request.
    # ---------------------------------------------------------------
    frappe.db.get_value("Cart", cart_name, "name", for_update=True)
    frappe.db.get_value("Payment Request", pr.name, "name", for_update=True)

    # Everything read before the locks is now suspect. Re-read both rows from
    # the database and re-decide from scratch.
    frappe.clear_document_cache("Payment Request", pr.name)
    frappe.clear_document_cache("Cart", cart_name)

    pr = frappe.get_doc("Payment Request", pr.name)

    # A competing request may have committed this very Payment Request while we
    # waited -- this is the branch that makes two racing commitments converge on
    # one Sales Order instead of creating two.
    if pr.reference_doctype == "Sales Order":
        return _existing_commitment(pr)

    if pr.reference_doctype != "Cart" or pr.reference_name != cart_name:
        return error_response(
            PAYMENT_REFERENCE_INVALID,
            "This checkout link is not valid.",
            status_code=HTTP_UNPROCESSABLE,
        )

    # Proceed may have superseded this credential while we waited on the lock.
    revalidated = _revalidate_token(pr, token)

    if is_error(revalidated):
        return revalidated

    # Phase 1's compare-only check: fingerprint, grand_total, currency, party.
    # It reprices the Cart in memory and persists nothing.
    source = validate_payment_request_source_current(pr)

    if is_error(source):
        return source

    return _commit_cart(pr, source["cart"])


# =========================================================
# RESOLUTION
# =========================================================

def _resolve(payment_request, token):
    """Discover the Payment Request. Deliberately unlocked -- see module docs."""

    if token:
        return resolve_checkout_token(token)

    if not payment_request:
        return error_response(
            PAYMENT_REFERENCE_INVALID,
            "This checkout link is not valid.",
            status_code=HTTP_UNPROCESSABLE,
        )

    if isinstance(payment_request, str):
        if not frappe.db.exists("Payment Request", payment_request):
            return error_response(
                PAYMENT_REFERENCE_INVALID,
                "This checkout link is not valid.",
                status_code=HTTP_UNPROCESSABLE,
            )
        return frappe.get_doc("Payment Request", payment_request)

    return payment_request


def _revalidate_token(pr, token):
    """After the locks: does the bearer credential still belong to this PR?

    Re-resolving the token would find whatever Payment Request holds it NOW,
    which after supersession is a different row. Comparing identity is the
    point: a token that has moved on no longer authorises this obligation.
    """

    if not token:
        return None

    if not pr.custom_checkout_token or pr.custom_checkout_token != token:
        return error_response(
            PAYMENT_REFERENCE_INVALID,
            "This checkout link is not valid.",
            status_code=HTTP_UNPROCESSABLE,
        )

    if pr.custom_checkout_expiry and pr.custom_checkout_expiry < now_datetime():
        return error_response(
            PAYMENT_REFERENCE_INVALID,
            "This payment link has expired.",
            status_code=HTTP_UNPROCESSABLE,
        )

    return None


# =========================================================
# ALREADY COMMITTED
# =========================================================

def _existing_commitment(pr):
    """Return the Sales Order this Payment Request already commits to.

    Never creates a second one. This is what makes a retry safe after the local
    commitment succeeded but the response, or a later provider step, was lost.

    Identity validation is delegated to ``validate_sales_order_source`` so that
    this branch, the public checkout page and payment settlement all apply the
    same definition of "still payable".
    """

    so = validate_sales_order_source(pr)

    if is_error(so):
        return so

    return {"payment_request": pr.name, "sales_order": so, "created": False}


# =========================================================
# CART -> SALES ORDER
# =========================================================

def _commit_cart(pr, cart):
    """The atomic Cart -> Draft Sales Order transition.

    Everything here is inside one savepoint. A conversion-time validation
    failure -- India Compliance, a disabled Customer, a stock rule -- must leave
    NO Sales Order behind, the Cart still Draft and the Payment Request still
    Cart-backed. Request-end rollback is not sufficient, because the API
    boundary catches errors and returns envelopes within the same request.
    """

    savepoint = "yob_commit_cart"
    frappe.db.savepoint(savepoint)

    try:
        # Trusted boundary: ERPNext's Cart -> Sales Order work reaches documents
        # YOB never constructs (get_item_details' cached Item), which are
        # permission-checked against the execution user. The public payer is
        # Guest. Entered only here, after the token, source, financial, party,
        # state and eligibility checks have all passed and the locks are held --
        # the caller cannot influence WHICH documents this touches.
        with trusted_execution():
            so = create_sales_order_from_cart(cart)

        _assert_financial_identity(pr, cart, so)

        # Cart lifecycle. A full save: the Cart is the mutable source document
        # and this is its legitimate terminal transition.
        cart.status = "Ordered"
        cart.sales_order = so.name
        cart.ordered_on = now_datetime()
        cart.checkout_by = frappe.session.user
        cart.save(ignore_permissions=True)

        # The obligation now points at the committed Sales Order. Narrow field
        # updates, never pr.save(): save() would rewrite grand_total, currency
        # and the fingerprint from the in-memory document. Those three, and the
        # checkout credential, are preserved exactly.
        #
        # custom_source_fingerprint deliberately stays as issued. It is
        # historical evidence of the CART obligation this payment was created
        # for. Once reference_doctype is Sales Order it must never be
        # reinterpreted as a Sales Order fingerprint -- which is why
        # validate_payment_request_source_current refuses non-Cart sources
        # rather than comparing them.
        frappe.db.set_value("Payment Request", pr.name, {
            "reference_doctype": "Sales Order",
            "reference_name": so.name,
        })
        frappe.clear_document_cache("Payment Request", pr.name)

    except Exception:
        frappe.db.rollback(save_point=savepoint)
        _invalidate(pr, cart)
        raise

    return {"payment_request": pr.name, "sales_order": so, "created": True}


def _assert_financial_identity(pr, cart, so):
    """The hard invariant: PR == Cart == Sales Order.

    The Sales Order RECALCULATES its own totals through ERPNext rather than
    copying the Cart's, which is the only way the commitment is trustworthy --
    and also the only reason this assertion can fail. It raising here is
    correct: the savepoint rollback above then removes the Sales Order, so a
    disagreement refuses the commitment instead of billing one number and
    ordering another.

    Line-level net/tax/discount parity is proven by the Gate 2 suite against
    this same conversion path and is not re-proven per commitment; what must
    hold for every payment is that the three payable totals are one number.
    """

    for label, value in (("Cart", cart.grand_total), ("Sales Order", so.grand_total)):
        if not same_money(pr.grand_total, value):
            frappe.throw(
                f"Commitment total mismatch: Payment Request {pr.grand_total} "
                f"!= {label} {value}",
                frappe.ValidationError,
            )

    for label, value in (("Cart", cart.currency), ("Sales Order", so.currency)):
        if (pr.currency or None) != (value or None):
            frappe.throw(
                f"Commitment currency mismatch: Payment Request {pr.currency} "
                f"!= {label} {value}",
                frappe.ValidationError,
            )

    if so.docstatus != 0:
        frappe.throw(
            "Commitment must leave the Sales Order in Draft",
            frappe.ValidationError,
        )


def _invalidate(pr, cart):
    """Gate 3 rule: rollback restores the database, not the document cache.

    Every document the rolled-back block may have written or read-through must
    be dropped, or the caller keeps serving pre-rollback values inside the same
    request. The Sales Order is not cleared by name because a failed insert may
    not have produced one; clearing the doctype covers the attempt either way.
    """

    frappe.clear_document_cache("Payment Request", pr.name)
    frappe.clear_document_cache("Cart", cart.name)

    if cart.customer:
        frappe.clear_document_cache("Customer", cart.customer)

    if getattr(cart, "sales_order", None):
        frappe.clear_document_cache("Sales Order", cart.sales_order)
