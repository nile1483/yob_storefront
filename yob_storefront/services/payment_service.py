import frappe
import razorpay

from yob_storefront.api.response import (
    HTTP_CONFLICT,
    HTTP_INTERNAL_SERVER_ERROR,
    HTTP_UNPROCESSABLE,
    PAYMENT_ALREADY_PROCESSED,
    PAYMENT_AMOUNT_MISMATCH,
    PAYMENT_CURRENCY_MISMATCH,
    PAYMENT_NOT_CAPTURED,
    PAYMENT_REFERENCE_INVALID,
    PAYMENT_SIGNATURE_INVALID,
    PAYMENT_VERIFICATION_FAILED,
    error_response,
    is_error,
    success_response,
)
from yob_storefront.integrations.razorpay import client as razorpay_client
from yob_storefront.services.payment_request_service import (
    validate_sales_order_source,
)

# Provider credentials and SDK calls live in integrations/razorpay/client.py.
# The previous get_razorpay_settings() helper cached the DECRYPTED api_secret in
# Redis, which this deployment runs without authentication; it was removed
# rather than moved. `razorpay` stays imported only for the SDK exception type
# translated below.

# =========================================================
# SAVE PAYMENT LOG
# =========================================================

def save_razorpay_payment_log(
    pr,
    payment,
    razorpay_order_id,
    razorpay_payment_id,
    razorpay_signature,
    status="Received"
):

    log = frappe.new_doc("Razorpay Payment Log")

    # Razorpay
    log.razorpay_order_id = razorpay_order_id
    log.razorpay_payment_id = razorpay_payment_id
    log.razorpay_signature = razorpay_signature

    # Payment
    log.payment_status = payment.get("status")
    log.payment_method = payment.get("method")
    log.payment_amount = payment.get("amount", 0) / 100
    log.currency = payment.get("currency")
    log.gateway_response = frappe.as_json(payment)

    # Payment Request
    log.payment_request = pr.name
    log.customer = pr.party
    log.reference_doctype = pr.reference_doctype
    log.reference_name = pr.reference_name

    # Customer Details (optional)
    if pr.party_type == "Customer":
        customer = frappe.get_doc("Customer", pr.party)

        log.email = customer.email_id or ""
        log.contact = customer.mobile_no or ""

    # Log Status
    log.status = status

    log.insert(ignore_permissions=True)

    return log


# =========================================================
# VERIFY SIGNATURE
# =========================================================

def verify_razorpay_signature(
    razorpay_order_id,
    razorpay_payment_id,
    razorpay_signature
):
    try:
        razorpay_client.verify_payment_signature(
            razorpay_order_id, razorpay_payment_id, razorpay_signature
        )

        return success_response(
            notice="Payment signature verified successfully."
        )

    except razorpay.errors.SignatureVerificationError:

        return error_response(
            PAYMENT_SIGNATURE_INVALID,
            "Invalid payment signature.",
            status_code=HTTP_UNPROCESSABLE,
        )

    except Exception:

        frappe.log_error(
            frappe.get_traceback(),
            "Razorpay Signature Verification Error"
        )

        return error_response(
            PAYMENT_VERIFICATION_FAILED,
            "Unable to verify payment signature.",
            status_code=HTTP_INTERNAL_SERVER_ERROR,
        )


# =========================================================
# Fetch Razorpay order details. using Razorpay Order ID
# =========================================================
def get_razorpay_order(order_id):
    """
    Fetch Razorpay order details.

    Args:
        order_id (str): Razorpay Order ID

    Returns:
        dict: Razorpay order response
    """

    return razorpay_client.fetch_order(order_id)
    
# =========================================================
# Fetch Razorpay payment details. using Razorpay Payment ID
# =========================================================
def get_razorpay_payment(payment_id):

    return razorpay_client.fetch_payment(payment_id)
    
# =========================================================
# GET PAYMENT REQUEST
# =========================================================

def get_payment_request_by_razorpay_order_id(razorpay_order_id):
    """Provider order id -> Payment Request, or a safe error envelope.

    Returns an envelope rather than throwing so an unknown provider order gives
    a stable machine-readable code instead of a raw exception message reaching
    the client through ``verify_payment``'s ValidationError handler.
    """

    if not razorpay_order_id:
        return error_response(
            PAYMENT_REFERENCE_INVALID,
            "This payment could not be matched to an order.",
            status_code=HTTP_UNPROCESSABLE,
        )

    pr_name = frappe.db.get_value(
        "Payment Request",
        {"custom_razorpay_order_id": razorpay_order_id},
        "name"
    )

    if not pr_name:
        return error_response(
            PAYMENT_REFERENCE_INVALID,
            "This payment could not be matched to an order.",
            status_code=HTTP_UNPROCESSABLE,
        )

    return frappe.get_doc("Payment Request", pr_name)


# # =========================================================
# # CREATE SALES ORDER FROM CART
# # =========================================================

# def create_sales_order_from_cart_v1(cart):

#     so = frappe.new_doc("Sales Order")

#     so.customer = cart.customer
#     so.company = cart.company
#     so.currency = cart.currency
#     so.selling_price_list = cart.price_list
#     so.delivery_date = today()

#     # coupon support
#     if getattr(cart, "coupon_code", None):
#         so.coupon_code = cart.coupon_code

#     # addresses
#     so.customer_address = cart.billing_address
#     so.shipping_address_name = cart.shipping_address

#     # taxes
#     if getattr(cart, "taxes_and_charges", None):
#         so.taxes_and_charges = cart.taxes_and_charges

#     for row in cart.items:
#         so.append("items", {
#             "item_code": row.item_code,
#             "qty": row.quantity,
#             "uom": row.uom,
#             "rate": row.rate,
#             "delivery_date": today()
#         })

#     # allow ERPNext pricing engine
#     so.set_missing_values()
#     so.calculate_taxes_and_totals()

#     so.insert(ignore_permissions=True)
#     # so.submit()

#     return so


# =========================================================
# PROCESS SUCCESS PAYMENT
# =========================================================


def process_success_payment(
    razorpay_order_id,
    razorpay_payment_id,
    razorpay_signature
):
    """Settle a verified Razorpay payment against the ALREADY-COMMITTED order.

    By settlement time the Sales Order must already exist: ``process_payment``
    committed it before the provider was ever contacted. This function
    therefore never creates one, and never returns to a Cart for financial
    truth -- the authoritative chain is

        provider order id -> Payment Request -> exact Sales Order

    Settlement effects are the ones this repository already intended, and no
    others. Inspection of the pre-2B code found NO Payment Entry creation and NO
    Sales Order submission anywhere in the app (``submit_sales_order`` exists in
    order_service but has zero callers, and ``so.submit()`` is commented out).
    Adding payment accounting here would be inventing behaviour, not preserving
    it, so it is deliberately not added and is reported as a gap instead.

    What settlement does: record the Razorpay Payment Log, stamp the provider
    fields and mode of payment on the Payment Request, mark it Paid, and revoke
    its checkout credential.
    """

    log = None

    # -------------------------------------------------
    # Verify Razorpay Signature -- never trust the caller's claim of success
    # -------------------------------------------------
    result = verify_razorpay_signature(
        razorpay_order_id,
        razorpay_payment_id,
        razorpay_signature
    )

    if is_error(result):
        return result

    # -------------------------------------------------
    # Provider order -> Payment Request
    # -------------------------------------------------
    pr = get_payment_request_by_razorpay_order_id(razorpay_order_id)

    if is_error(pr):
        return pr

    # -------------------------------------------------
    # Idempotency, BEFORE any state change
    # -------------------------------------------------
    # Pre-2B this guard read `pr.status == "Paid"` -- but nothing in the app
    # ever set that status, so it never fired and a repeated verification
    # created a SECOND Sales Order. Settlement now marks the obligation Paid,
    # which makes the guard real, and the provider payment id distinguishes a
    # replay of the same payment from a different payment against an obligation
    # that is already settled.
    settled = _already_settled(pr, razorpay_payment_id)

    if settled is not None:
        return settled

    # -------------------------------------------------
    # The exact committed Sales Order
    # -------------------------------------------------
    so = validate_sales_order_source(pr)

    if is_error(so):
        return so

    # -------------------------------------------------
    # Provider truth
    # -------------------------------------------------
    order = get_razorpay_order(razorpay_order_id)
    payment = get_razorpay_payment(razorpay_payment_id)

    if order.get("status") != "paid":
        return error_response(
            PAYMENT_NOT_CAPTURED,
            "The payment has not been completed.",
            status_code=HTTP_UNPROCESSABLE,
        )

    if payment.get("status") != "captured":
        return error_response(
            PAYMENT_NOT_CAPTURED,
            "The payment has not been captured.",
            status_code=HTTP_UNPROCESSABLE,
        )

    # The payment must belong to the order we resolved the obligation from.
    if payment.get("order_id") and payment.get("order_id") != razorpay_order_id:
        return error_response(
            PAYMENT_REFERENCE_INVALID,
            "This payment does not belong to this order.",
            status_code=HTTP_CONFLICT,
        )

    if payment.get("currency") != pr.currency:
        return error_response(
            PAYMENT_CURRENCY_MISMATCH,
            "The payment currency does not match the payment request.",
            status_code=HTTP_CONFLICT,
        )

    # Amounts are compared against the IMMUTABLE obligation, which
    # validate_sales_order_source has just proven equals the Sales Order.
    expected_amount = int(round(float(pr.grand_total) * 100))

    if payment.get("amount") != expected_amount:
        return error_response(
            PAYMENT_AMOUNT_MISMATCH,
            "The paid amount does not match the payment request.",
            status_code=HTTP_CONFLICT,
        )

    if order.get("amount") != expected_amount:
        return error_response(
            PAYMENT_AMOUNT_MISMATCH,
            "The order amount does not match the payment request.",
            status_code=HTTP_CONFLICT,
        )

    # -------------------------------------------------
    # Settlement
    # -------------------------------------------------
    log = save_razorpay_payment_log(
        pr=pr,
        payment=payment,
        razorpay_order_id=razorpay_order_id,
        razorpay_payment_id=razorpay_payment_id,
        razorpay_signature=razorpay_signature,
        status="Received"
    )

    # Narrow field updates, never pr.save(): a whole-document save on an issued
    # Payment Request rewrites grand_total and currency from whatever the
    # in-memory document holds. This was the last such save in the payment path.
    #
    # `status = "Paid"` uses ERPNext's own Payment Request status vocabulary
    # rather than a YOB state machine, and is what makes the idempotency guard
    # above real. It also stops the settled obligation resolving from its
    # checkout token (resolve_checkout_token treats Paid as closed) and stops it
    # being reused as a Proceed candidate.
    frappe.db.set_value("Payment Request", pr.name, {
        "transaction_date": frappe.utils.today(),
        "mode_of_payment": "Razorpay",
        "custom_razorpay_payment_id": razorpay_payment_id,
        "custom_razorpay_signature": razorpay_signature,
        "custom_razorpay_status": payment["status"],
        "custom_razorpay_response": frappe.as_json(payment),
        "status": "Paid",
        # The obligation is settled, so the bearer credential is withdrawn.
        # This is a specific lifecycle reason to revoke -- unlike the Cart -> SO
        # transition, which deliberately keeps the token usable.
        "custom_checkout_token": None,
        "custom_checkout_expiry": None,
    })
    frappe.clear_document_cache("Payment Request", pr.name)

    log.status = "Completed"
    log.reference_doctype = "Sales Order"
    log.reference_name = so.name
    log.save(ignore_permissions=True)

    return success_response(
        {
            "sales_order": so.name,
            "payment_request": pr.name,
            "payment_id": razorpay_payment_id
        },
        notice="Payment verified and order created successfully."
    )


def _already_settled(pr, razorpay_payment_id):
    """Converge on the settled result, or refuse a second payment. Else None.

    Two distinct cases, which must not be conflated:

    * the SAME provider payment is being verified again (callback retry, user
      refresh, duplicate webhook) -- return the settled result unchanged;
    * a DIFFERENT payment arrives for an obligation already settled -- refuse,
      because paying twice must never be silently absorbed.
    """

    if pr.status != "Paid" and not pr.custom_razorpay_payment_id:
        return None

    if pr.custom_razorpay_payment_id == razorpay_payment_id:
        return success_response(
            {
                "sales_order": pr.reference_name if pr.reference_doctype == "Sales Order" else None,
                "payment_request": pr.name,
                "payment_id": razorpay_payment_id,
            },
            notice="Payment already processed."
        )

    return error_response(
        PAYMENT_ALREADY_PROCESSED,
        "This payment request has already been paid.",
        status_code=HTTP_CONFLICT,
    )
