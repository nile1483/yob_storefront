"""
PAYMENT API -- intentionally guest-accessible.

These three endpoints are the only remaining ``allow_guest=True`` methods in
yob_storefront. They CANNOT use ``require_application`` because they are
reached without a Frappe session:

  * get_checkout_data / process_payment
        Authorized by the unguessable 32-byte ``custom_checkout_token`` stored
        on the Payment Request (``secrets.token_urlsafe(32)``, ~256 bits).
        Both resolve it through the single
        ``payment_request_service.resolve_checkout_token`` primitive, which
        rejects a blank token before querying, requires exactly one match, and
        enforces ``custom_checkout_expiry`` -- so the two endpoints cannot drift
        into different token semantics.

  * verify_payment
        Authorized by Razorpay's HMAC signature, verified server-side in
        ``payment_service.verify_razorpay_signature()`` before any state change.
        It does NOT accept or resolve a checkout token -- it finds the Payment
        Request by ``custom_razorpay_order_id`` -- so the token resolver does
        not apply to it and it is not a second, weaker token surface.

The Customer is never taken from the request: it is derived from
token -> Payment Request -> Cart -> Cart.customer. See
docs/yob_storefront_security_checklist.md.
"""

import frappe
from yob_core.api.boundary import yob_api
from yob_storefront.api.response import (
    HTTP_CONFLICT,
    HTTP_CREATED,
    HTTP_INTERNAL_SERVER_ERROR,
    HTTP_UNPROCESSABLE,
    PAYMENT_ALREADY_PROCESSED,
    PAYMENT_AMOUNT_MISMATCH,
    PAYMENT_METHOD_UNSUPPORTED,
    PAYMENT_PROVIDER_ERROR,
    PAYMENT_PROVIDER_NOT_CONFIGURED,
    PAYMENT_REFERENCE_INVALID,
    PAYMENT_VERIFICATION_FAILED,
    VALIDATION_FAILED,
    error_response,
    is_error,
    server_error,
    success_response,
)
from yob_storefront.integrations.gateways.base import (
    Obligation,
    ProviderAlreadyPaid,
    ProviderIntegrityError,
    ProviderNotConfigured,
    ProviderPreflightFailed,
    UnsupportedProvider,
)
from yob_storefront.integrations.gateways.registry import resolve_gateway
from yob_storefront.services import payment_view
from yob_storefront.services.cart_service import build_cart_response
from yob_storefront.services.commitment_service import (
    ensure_payment_request_committed,
)
from yob_storefront.services.payment_method_service import (
    get_eligible_payment_methods,
    is_payment_method_eligible,
)
from yob_storefront.services.payment_request_service import (
    resolve_checkout_token,
    same_money,
    validate_payment_request_source_current,
    validate_sales_order_source,
)
from yob_storefront.services.payment_service import process_success_payment


def _set_payment_request_fields(pr, values: dict):
    """Write non-financial fields on an issued Payment Request.

    Deliberately ``frappe.db.set_value`` and never ``pr.save()``. ``save()``
    persists the whole in-memory document, so any code path that saves an
    issued Payment Request is one stale attribute away from rewriting
    ``grand_total`` or ``currency`` -- exactly the mutation this phase exists to
    eliminate. Keeping every post-issuance write in this one helper also makes
    the immutability audit a single grep.

    The in-memory document is updated to match, so the caller can keep reading
    it, and the document cache is invalidated so nothing else sees the old row.
    """

    frappe.db.set_value("Payment Request", pr.name, values)

    for field, value in values.items():
        pr.set(field, value)

    frappe.clear_document_cache("Payment Request", pr.name)

@frappe.whitelist(allow_guest=True, methods=["POST"])
@yob_api
def verify_payment(
    razorpay_order_id=None, razorpay_payment_id=None, razorpay_signature=None
):
    # Guest endpoint: a missing argument must answer as a client error, not as
    # an un-enveloped TypeError carrying a traceback and this signature.
    for field, value in (
        ("razorpay_order_id", razorpay_order_id),
        ("razorpay_payment_id", razorpay_payment_id),
        ("razorpay_signature", razorpay_signature),
    ):
        if not value:
            return error_response(
                VALIDATION_FAILED,
                "Payment verification details are incomplete.",
                field=field,
                status_code=HTTP_UNPROCESSABLE,
            )

    try:

        # Already a standard success/error envelope, with its status set.
        return process_success_payment(
            razorpay_order_id,
            razorpay_payment_id,
            razorpay_signature
        )

    except frappe.ValidationError as e:

        return error_response(
            PAYMENT_VERIFICATION_FAILED,
            str(e),
            status_code=HTTP_UNPROCESSABLE,
        )

    except Exception:

        return server_error("Verify Payment Error", "Failed to verify payment")

 


@frappe.whitelist(allow_guest=True, methods=["GET"])
@yob_api
def get_checkout_data(token=None):
    """Render the guest payment page for one immutable obligation.

    Read-only in every sense: it mutates neither the Payment Request nor its
    source. The blank-token guard, the exactly-one-match rule and the expiry
    check all live in ``resolve_checkout_token`` so this endpoint cannot drift
    away from ``process_payment``.

    Serves BOTH payment sources, which is what makes a browser refresh work
    after ``process_payment`` has committed Cart -> Sales Order:

        PR -> Cart          pre-commitment; stale Cart answers
                            payment_request_stale
        PR -> Sales Order   post-commitment; the Cart is finished and is never
                            consulted, compared or revalidated

    The payable money always comes from the immutable Payment Request. The
    referenced document only supplies display and order information.
    """

    pr = resolve_checkout_token(token)

    if is_error(pr):
        return pr

    if pr.reference_doctype == "Sales Order":
        return _sales_order_checkout(pr)

    return _cart_checkout(pr)


def _cart_checkout(pr):
    """Pre-commitment payment page, from the Cart the obligation was issued for."""

    # Compare-only. Returns the authoritative CALCULATED cart (repriced in
    # memory, deliberately unsaved) when the source still matches.
    source = validate_payment_request_source_current(pr)

    if is_error(source):
        return source

    cart = source["cart"]
    customer = source["customer"]

    # build_cart_response is kept verbatim for this branch: it is the published
    # shape the SPA already consumes, and Phase 2B must not break it.
    response_data = build_cart_response(cart)

    response_data["source_doctype"] = "Cart"
    response_data["source_name"] = cart.name
    response_data["payment_request"] = pr.name
    # Read from the Payment Request, not the Cart. They are equal here -- the
    # validation above just proved it -- but the obligation is what the buyer
    # is being asked to pay, and saying so in code keeps it that way.
    response_data["amount"] = pr.grand_total
    response_data["currency"] = pr.currency
    response_data["payment_methods"] = get_eligible_payment_methods(
        customer.name, cart.company, pr.grand_total,
    )

    return success_response(response_data, notice="Checkout data retrieved successfully")


def _sales_order_checkout(pr):
    """Post-commitment payment page, from the committed Sales Order.

    No Cart is loaded, compared or revalidated here. After commitment the Cart
    is Ordered and irrelevant to what is owed; re-deriving anything from it
    would reintroduce exactly the mutable-source problem Phase 1 removed.
    """

    so = validate_sales_order_source(pr)

    if is_error(so):
        return so

    response_data = payment_view.payment_summary(pr)

    if is_error(response_data):
        return response_data

    response_data["payment_request"] = pr.name
    response_data["payment_methods"] = get_eligible_payment_methods(
        so.customer, so.company, pr.grand_total,
    )

    return success_response(response_data, notice="Checkout data retrieved successfully")


@frappe.whitelist(allow_guest=True, methods=["POST"])
@yob_api
def process_payment(token=None, payment_method=None):
    """Commit the obligation locally, then dispatch to the chosen method.

    The browser selects ONLY the Payment Method. Amount, currency, source,
    customer and provider order id are all derived server-side from the
    immutable Payment Request and its committed Sales Order.

    Sequence:

        resolve token -> load method -> derive source context
        -> re-check eligibility authoritatively -> commit (Cart -> ONE Draft SO)
        -> dispatch by method_code

    Eligibility is re-checked BEFORE the commitment, so an ineligible method
    cannot cause a Sales Order to be created.
    """

    if not payment_method:
        return error_response(
            VALIDATION_FAILED,
            "Payment method is required.",
            field="payment_method",
            status_code=HTTP_UNPROCESSABLE,
        )

    try:
        # Identical token semantics to get_checkout_data -- same primitive,
        # including the blank-token guard that must run before any query.
        pr = resolve_checkout_token(token)

        if is_error(pr):
            return pr

        method = frappe.get_doc("Payment Method", payment_method)

        # ---------------------------------------------------------------
        # Authoritative eligibility re-check
        # ---------------------------------------------------------------
        # Never "it was offered earlier, so it is allowed". The rule is
        # re-evaluated now, against the immutable obligation's amount and the
        # party derived from the Payment Request -- for a Cart-backed PR and an
        # already-committed one alike.
        context = _source_context(pr)

        if is_error(context):
            return context

        if not is_payment_method_eligible(
            method.name, context["customer"], context["company"], pr.grand_total
        ):
            return error_response(
                PAYMENT_METHOD_UNSUPPORTED,
                "This payment method is not available for this order.",
                field="payment_method",
                status_code=HTTP_UNPROCESSABLE,
            )

        # ---------------------------------------------------------------
        # Advisory source-current check, BEFORE gateway preflight
        # ---------------------------------------------------------------
        # A buyer whose cart moved on should be told THAT, not that the
        # merchant's gateway is misconfigured -- their problem is actionable and
        # the gateway's is not theirs to fix. So staleness is reported first.
        #
        # Advisory only. It is a compare-only read with no lock, so the cart can
        # still change between here and commitment; ensure_payment_request_
        # committed revalidates under Cart FOR UPDATE -> Payment Request FOR
        # UPDATE and remains the authority. This check exists for error
        # ORDERING, never as the safety guarantee.
        #
        # Skipped for an already Sales-Order-backed obligation: its cart is
        # finished and must never be consulted again.
        if pr.reference_doctype == "Cart":
            current = validate_payment_request_source_current(pr)

            if is_error(current):
                return current

        # ---------------------------------------------------------------
        # Resolve the gateway
        # ---------------------------------------------------------------
        # By Payment Gateway link, never by method_code. method_code stays for
        # display and frontend compatibility, but using it to choose a provider
        # is what grows `if razorpay / if stripe / if paypal` chains through the
        # payment code.
        #
        # No gateway means an INTERNAL YOB method (Pay Later): it has no
        # external provider, so there is nothing to dispatch to. That single
        # internal branch is deliberate; per-provider branches are not.
        try:
            gateway = resolve_gateway(method)
        except UnsupportedProvider:
            # Fails closed: a method wired to a gateway with no driver must not
            # silently fall through to "internal", which would fulfil an online
            # order with no payment at all.
            frappe.log_error(
                f"Payment Method '{method.name}' names an unsupported gateway",
                "YOB Gateway Registry",
            )
            return error_response(
                PAYMENT_METHOD_UNSUPPORTED,
                "This payment method is not supported.",
                field="payment_method",
                status_code=HTTP_UNPROCESSABLE,
            )

        # ---------------------------------------------------------------
        # Provider preflight -- BEFORE any commercial commitment
        # ---------------------------------------------------------------
        # Static prerequisites only: credentials present, currency supported.
        # No network call, no side effect.
        #
        # This ordering is the point of the check. A gateway that could never
        # have taken this payment must not leave a real Draft Sales Order and an
        # Ordered Cart behind -- "we cannot start" and "we started and the
        # provider failed" are different events and the buyer's data should show
        # which one happened.
        #
        # Internal methods skip it entirely: they have no provider to preflight.
        if gateway is not None:
            failure = _preflight(gateway, pr)

            if failure:
                return failure

        # ---------------------------------------------------------------
        # ONE local commitment, shared by every method
        # ---------------------------------------------------------------
        # Cart lock -> PR lock -> reload -> revalidate -> ONE Draft Sales Order.
        # Idempotent: an already Sales-Order-backed Payment Request returns the
        # same order and never looks for a Cart.
        result = ensure_payment_request_committed(token=token)

        if is_error(result):
            return result

        so = result["sales_order"]

        # The reference moved during commitment; re-read so the dispatchers see
        # database truth rather than the pre-commitment document.
        pr = frappe.get_doc("Payment Request", result["payment_request"])

        if gateway is None:
            return process_pay_later(pr, so)

        return process_gateway_payment(gateway, pr, so)

    except frappe.DoesNotExistError:
        return error_response(
            PAYMENT_METHOD_UNSUPPORTED,
            "This payment method is not supported.",
            field="payment_method",
            status_code=HTTP_UNPROCESSABLE,
        )

    except Exception:
        return server_error("Process Payment Error", "Failed to process payment")


def _preflight(gateway, pr):
    """Run a gateway's static prerequisites. Returns an error envelope or None.

    Public error contract, chosen to add NO new error codes:

        preflight failure       existing code + details.retryable = false
                                and NO `sales_order` -- nothing was committed
        post-commitment failure payment_provider_error, details.retryable = true
                                and details.sales_order -- the order stands

    ``details.retryable`` is the discriminator the SPA needs, and it works
    across both codes, so no new public error family is invented:

    * missing credentials keeps its published ``payment_provider_not_configured``
      rather than being flattened into a generic provider error -- it is a
      server misconfiguration and losing that specificity would help nobody;
    * an unsupported currency reuses ``payment_provider_error``, since it is a
      provider capability limit and there is no better published code.

    The additions are backward compatible: ``details`` is a new optional key on
    responses that previously carried none.
    """

    try:
        gateway.preflight(Obligation.pending(pr))
        return None

    except ProviderNotConfigured:
        # Already logged by the driver; the caller cannot fix this.
        return error_response(
            PAYMENT_PROVIDER_NOT_CONFIGURED,
            "Online payment is temporarily unavailable.",
            details={"retryable": False},
            status_code=HTTP_INTERNAL_SERVER_ERROR,
        )

    except ProviderPreflightFailed as exc:
        # A capability limit, not a fault: safe to tell the buyer, and the
        # driver has already phrased it without provider internals.
        return error_response(
            PAYMENT_PROVIDER_ERROR,
            str(exc.reason),
            details={"retryable": False},
            status_code=HTTP_UNPROCESSABLE,
        )

    except Exception:
        # A driver bug must not be reported as a provider outage, and must not
        # let commitment proceed.
        frappe.log_error(frappe.get_traceback(),
                         f"{gateway.provider} Preflight Error")
        return error_response(
            PAYMENT_PROVIDER_ERROR,
            "Online payment is temporarily unavailable.",
            details={"retryable": False},
            status_code=HTTP_INTERNAL_SERVER_ERROR,
        )


def _source_context(pr):
    """Customer and company for the eligibility check, derived server-side.

    ``pr.party`` is the authoritative customer -- it is on the immutable
    obligation. Only ``company`` has to come from the source document, because
    the Payment Request does not carry one.
    """

    if pr.reference_doctype == "Cart":
        company = frappe.db.get_value("Cart", pr.reference_name, "company")
    elif pr.reference_doctype == "Sales Order":
        company = frappe.db.get_value("Sales Order", pr.reference_name, "company")
    else:
        return error_response(
            PAYMENT_REFERENCE_INVALID,
            "This checkout link is not valid.",
            status_code=HTTP_UNPROCESSABLE,
        )

    if not company:
        return error_response(
            PAYMENT_REFERENCE_INVALID,
            "This checkout link is not valid.",
            status_code=HTTP_UNPROCESSABLE,
        )

    return {"customer": pr.party, "company": company}


# -------------------------------------------------------
# PAY LATER
# -------------------------------------------------------

def process_pay_later(pr, so):
    """Offline method: the local commitment IS the whole transaction.

    A peer dispatch, not a special path. Its own Cart -> Sales Order conversion
    is gone: creating an order here after the common commitment would produce a
    SECOND Sales Order for one Payment Request.

    The Payment Request stays OUTSTANDING -- deliberately not marked Paid, not
    cancelled, and its checkout credential is left usable. Choosing "pay later"
    is a statement about timing, not a payment, so the obligation must survive
    to support a future "Pay Now" against the same order.

    No provider call, so no explicit commit: the normal request-end transaction
    boundary is correct here.
    """

    return success_response(
        {
            "payment_method": "paylater",
            "sales_order": so.name,
            "payment_request": pr.name,
            "amount": pr.grand_total,
            "currency": pr.currency,
            "payment_status": "Unpaid",
        },
        notice="Order created with Pay Later",
        status_code=HTTP_CREATED,
    )


# -------------------------------------------------------
# EXTERNAL GATEWAY PAYMENT
# -------------------------------------------------------

def process_gateway_payment(gateway, pr, so):
    """Any external provider: durable local commitment, THEN the network.

    Provider-neutral. The driver owns the conversation with the provider; this
    function owns the ordering that makes it safe, which is identical for every
    gateway:

        [ caller's transaction ]  token + eligibility + Cart -> SO commitment
        COMMIT                    <- durability boundary, releases row locks
        [ network ]               provider payment create or recover
        [ new transaction ]       persist provider metadata

    The commit is mandatory and must happen BEFORE the first provider call.
    After it the database durably holds one Draft Sales Order, an Ordered Cart
    pointing at it, and a Payment Request referencing it -- so a provider
    failure leaves a real, retryable obligation instead of a provider payment
    pointing at a Sales Order that was rolled back.

    It also releases the Cart and Payment Request row locks, which must never
    be held across a network call.
    """

    # Configuration and currency were already proven by preflight, BEFORE the
    # Cart was committed -- see process_payment. By the time control reaches
    # here the gateway is known to be usable, so every failure below is a real
    # provider/network failure against a real obligation, and therefore
    # retryable with the Sales Order intact.

    # The obligation and its order must agree before money is requested. The
    # commitment service already asserted this; re-checking here means the
    # provider amount can never be derived from an unverified pair.
    if not same_money(pr.grand_total, so.grand_total) or pr.currency != so.currency:
        frappe.log_error(
            f"Payment Request {pr.name} and Sales Order {so.name} disagree",
            "YOB Commitment Invariant",
        )
        return error_response(
            PAYMENT_AMOUNT_MISMATCH,
            "This order cannot be paid right now.",
            status_code=HTTP_CONFLICT,
        )

    obligation = Obligation(payment_request=pr, sales_order=so)

    # ---------------- DURABILITY BOUNDARY ----------------
    frappe.db.commit()
    # -----------------------------------------------------

    try:
        intent = gateway.prepare_payment(obligation)

    except ProviderAlreadyPaid:
        return error_response(
            PAYMENT_ALREADY_PROCESSED,
            "Payment has already been completed.",
            status_code=HTTP_CONFLICT,
        )

    except ProviderIntegrityError:
        # Provider state a human must resolve -- e.g. two attempted orders
        # sharing one receipt. NOT retryable: repeating the request cannot
        # disambiguate them, and guessing could settle the wrong payment.
        # Already logged with the order ids by the driver.
        return error_response(
            PAYMENT_PROVIDER_ERROR,
            "This payment needs manual review. Please contact support.",
            details={"sales_order": so.name, "retryable": False},
            status_code=HTTP_CONFLICT,
        )

    except Exception:
        # The local obligation is already durable. Say so: this is retryable,
        # and the Sales Order was NOT rolled back. No provider exception text
        # reaches the client.
        frappe.log_error(frappe.get_traceback(),
                         f"{gateway.provider} Provider Error")
        return error_response(
            PAYMENT_PROVIDER_ERROR,
            "We could not reach the payment provider. Your order is saved -- "
            "please try paying again.",
            details={"sales_order": so.name, "retryable": True},
            status_code=HTTP_INTERNAL_SERVER_ERROR,
        )

    return success_response(
        _client_response(intent, pr, so),
        status_code=HTTP_CREATED,
    )


def _client_response(intent, pr, so) -> dict:
    """Adapt a ProviderIntent to the PUBLISHED per-provider SPA contract.

    The generic ProviderIntent is deliberately NOT exposed yet: Angular already
    consumes the Razorpay shape below, and Phase A must not require a frontend
    change. New providers get their own case here when they get a driver, which
    keeps the public contract explicit per provider rather than leaking an
    internal structure the SPA would then be coupled to.
    """

    if intent.provider == "Razorpay":
        return {
            "payment_method": "razorpay",
            "razorpay_key": intent.client_payload["key"],
            "order_id": intent.client_payload["order_id"],
            "amount": intent.amount_minor,
            "currency": intent.currency,
            "sales_order": so.name,
            "payment_request": pr.name,
        }

    # Unreachable while Razorpay is the only registered driver; a new driver
    # without a published contract must fail loudly rather than invent one.
    raise UnsupportedProvider(intent.provider)
