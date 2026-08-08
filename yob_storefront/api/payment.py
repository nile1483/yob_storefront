"""
PAYMENT API -- intentionally guest-accessible.

These three endpoints are the only remaining ``allow_guest=True`` methods in
yob_storefront. They CANNOT use ``require_application`` because they are
reached without a Frappe session:

  * get_checkout_data / process_payment
        Authorized by the unguessable 32-byte ``custom_checkout_token`` stored
        on the Payment Request (``secrets.token_urlsafe(32)``, ~256 bits).
        Both call ``validate_payment_request()`` first, which enforces
        ``custom_checkout_expiry`` before any mutation.

  * verify_payment
        Authorized by Razorpay's HMAC signature, verified server-side in
        ``payment_service.verify_razorpay_signature()`` before any state change.

The Customer is never taken from the request: it is derived from
token -> Payment Request -> Cart -> Cart.customer. See
docs/yob_storefront_security_checklist.md.
"""

import frappe
import razorpay
from yob_storefront.api.response import (
    CART_NOT_FOUND,
    CHECKOUT_TOKEN_EXPIRED,
    CHECKOUT_TOKEN_INVALID,
    CUSTOMER_NOT_FOUND,
    HTTP_CONFLICT,
    HTTP_CREATED,
    HTTP_INTERNAL_SERVER_ERROR,
    HTTP_NOT_FOUND,
    HTTP_UNPROCESSABLE,
    PAYMENT_ALREADY_PROCESSED,
    PAYMENT_METHOD_UNSUPPORTED,
    PAYMENT_PROVIDER_NOT_CONFIGURED,
    PAYMENT_REFERENCE_INVALID,
    PAYMENT_VERIFICATION_FAILED,
    error_response,
    is_error,
    server_error,
    success_response,
)
from yob_storefront.services.cart_service import build_cart_response
from yob_storefront.services.cart_service import get_available_payment_methods
from frappe.utils import now_datetime
from datetime import datetime 
from yob_storefront.services.cart_service import ( 
    reprice_cart
)

from yob_storefront.services.payment_service import process_success_payment
from yob_storefront.services.order_service import create_sales_order_from_cart

@frappe.whitelist(allow_guest=True)
def verify_payment(razorpay_order_id, razorpay_payment_id, razorpay_signature):

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

 


@frappe.whitelist(allow_guest=True)
def get_checkout_data(token):

    pr = frappe.get_all(
        "Payment Request",
        filters={"custom_checkout_token": token},
        fields=["name"],
        ignore_permissions=True,
        limit=1,
    )

    if not pr:
        return error_response(
            CHECKOUT_TOKEN_INVALID,
            "This checkout link is not valid.",
            field="token",
            status_code=HTTP_NOT_FOUND,
        )

    pr = frappe.get_doc("Payment Request", pr[0].name)

    validation_result = validate_payment_request(pr)

    if validation_result:
        return validation_result

    # -----------------------------------------------------
    # Sync Cart & Payment Request
    # -----------------------------------------------------
    sync_result = sync_payment_request_with_cart(pr)

    if is_error(sync_result):
        return sync_result

    cart = sync_result["cart"]  
    
    customer = sync_result["customer"] 
    
    # -----------------------------------------------------
    # Build Checkout Response
    # -----------------------------------------------------
    response_data = build_cart_response(cart)

    response_data["payment_request"] = pr.name
    response_data["amount"] = cart.grand_total
    response_data["currency"] = cart.currency

    response_data["payment_methods"] = get_available_payment_methods(
        customer.name,
        cart.company,
        cart.grand_total,
    )

    return success_response(response_data, notice="Checkout data retrieved successfully")


@frappe.whitelist(allow_guest=True)
def process_payment(token, payment_method):
    try:

        # Get payment request
        pr = get_payment_request(token)

        if not pr:
            return error_response(
                CHECKOUT_TOKEN_INVALID,
                "This checkout link is not valid.",
                field="token",
                status_code=HTTP_NOT_FOUND,
            )

        validation_result = validate_payment_request(pr)

        if validation_result:
            return validation_result

        method = frappe.get_doc("Payment Method", payment_method)

        # -------------------------
        # PAY LATER
        # -------------------------
        if method.method_code == "paylater":
            return process_pay_later(pr)

        # -------------------------
        # RAZORPAY
        # -------------------------
        if method.method_code == "razorpay":
            return process_razorpay_payment(pr)

        return error_response(
            PAYMENT_METHOD_UNSUPPORTED,
            "This payment method is not supported.",
            field="payment_method",
            status_code=HTTP_UNPROCESSABLE,
        )

    except frappe.DoesNotExistError:
        return error_response(
            PAYMENT_METHOD_UNSUPPORTED,
            "This payment method is not supported.",
            field="payment_method",
            status_code=HTTP_UNPROCESSABLE,
        )

    except Exception:
        return server_error("Process Payment Error", "Failed to process payment")


# -------------------------------------------------------
# PAYMENT REQUEST
# -------------------------------------------------------

def get_payment_request(token):

    pr = frappe.get_all(
        "Payment Request",
        filters={"custom_checkout_token": token},
        fields=["name"],
        ignore_permissions=True,
        limit=1
    )

    if not pr:
        return None

    return frappe.get_doc("Payment Request", pr[0].name)


def sync_payment_request_with_cart(pr):
    """
    Reprice the linked cart and keep the Payment Request in sync.

    Returns:
        On failure: a standard error envelope (see ``is_error``), ready to be
            returned to the client unchanged.
        On success: {"cart": Cart document, "customer": Customer document}.
    """

    if pr.reference_doctype != "Cart":
        return error_response(
            PAYMENT_REFERENCE_INVALID,
            "This checkout link is not valid.",
            status_code=HTTP_UNPROCESSABLE,
        )

    if not frappe.db.exists("Cart", pr.reference_name):
        return error_response(
            CART_NOT_FOUND,
            "The cart for this checkout link no longer exists.",
            status_code=HTTP_NOT_FOUND,
        )

    cart = frappe.get_doc("Cart", pr.reference_name)


    if not frappe.db.exists("Customer", cart.customer):
        return error_response(
            CUSTOMER_NOT_FOUND,
            "The customer for this checkout link no longer exists.",
            status_code=HTTP_NOT_FOUND,
        )

    customer = frappe.get_doc("Customer", cart.customer)

    reprice_cart(cart, customer)

    cart.save(ignore_permissions=True)

    if (
        pr.grand_total != cart.grand_total
        or pr.currency != cart.currency
    ):
        pr.grand_total = cart.grand_total
        pr.currency = cart.currency

        # If amount changed, create a new Razorpay order later
        pr.custom_razorpay_order_id = None
        pr.custom_razorpay_status = None

        pr.save(ignore_permissions=True)

    return {
        "cart": cart,
        "customer": customer,
    }

def validate_payment_request(pr):
    """Return a standard error envelope when the checkout link is unusable, else None."""

    if pr.custom_checkout_expiry and pr.custom_checkout_expiry < datetime.now():
        return error_response(
            CHECKOUT_TOKEN_EXPIRED,
            "This payment link has expired.",
            field="token",
            status_code=HTTP_UNPROCESSABLE,
        )

    if not pr.reference_name:
        return error_response(
            CHECKOUT_TOKEN_INVALID,
            "This checkout link is not valid.",
            field="token",
            status_code=HTTP_UNPROCESSABLE,
        )

    return None


# -------------------------------------------------------
# RAZORPAY PAYMENT
# -------------------------------------------------------

def process_razorpay_payment(pr):

    sync_result = sync_payment_request_with_cart(pr)

    if is_error(sync_result):
        return sync_result

    settings = frappe.get_single("Razorpay Settings")

    if not settings.api_key:
        # Server-side misconfiguration, not something the caller can fix.
        frappe.log_error("Razorpay API key is not configured", "Razorpay Not Configured")
        return error_response(
            PAYMENT_PROVIDER_NOT_CONFIGURED,
            "Online payment is temporarily unavailable.",
            status_code=HTTP_INTERNAL_SERVER_ERROR,
        )

    amount_paise = int(pr.grand_total * 100)

    client = razorpay.Client(
        auth=(
            settings.api_key,
            settings.get_password("api_secret")
        )
    )

    # -------------------------------------------------------
    # Reuse existing Razorpay Order if still valid
    # -------------------------------------------------------
    if pr.custom_razorpay_order_id:

        try:

            order = client.order.fetch(pr.custom_razorpay_order_id)

            # Save latest status
            pr.custom_razorpay_status = order.get("status")
            pr.save(ignore_permissions=True)

            # Check if payment already completed
            payments = client.order.payments(pr.custom_razorpay_order_id)

            for payment in payments.get("items", []):

                if payment["status"] in ("captured", "authorized"):
                    return error_response(
                        PAYMENT_ALREADY_PROCESSED,
                        "Payment has already been completed.",
                        status_code=HTTP_CONFLICT,
                    )

            # Reuse only if order is still active
            if order.get("status") == "created":

                return success_response({
                    "payment_method": "razorpay",
                    "razorpay_key": settings.api_key,
                    "order_id": pr.custom_razorpay_order_id,
                    "amount": amount_paise,
                    "currency": pr.currency
                })

        except Exception:
            # Existing order is invalid/not found.
            # A new order will be created below.
            frappe.log_error(
                frappe.get_traceback(),
                "Invalid Razorpay Order - Creating New"
            )

    # -------------------------------------------------------
    # Create New Razorpay Order
    # -------------------------------------------------------
    order = client.order.create({
        "amount": amount_paise,
        "currency": pr.currency,
        "payment_capture": 1
    })

    pr.custom_razorpay_order_id = order["id"]
    pr.custom_razorpay_status = order.get("status")

    pr.save(ignore_permissions=True)

    return success_response({
        "payment_method": "razorpay",
        "razorpay_key": settings.api_key,
        "order_id": order["id"],
        "amount": amount_paise,
        "currency": pr.currency
    }, status_code=HTTP_CREATED)


# -------------------------------------------------------
# OFFLINE PAYMENT
# -------------------------------------------------------

def process_pay_later(pr):

     
    # -----------------------------------------------------
    # Get Cart and customer
    # -----------------------------------------------------       
    sync_result = sync_payment_request_with_cart(pr)

    if is_error(sync_result):
        return sync_result

    cart = sync_result.get("cart")
    # -----------------------------------------------------
    # Prevent Duplicate Order
    # -----------------------------------------------------
    if cart.status == "Ordered" and cart.sales_order:
        return success_response({
            "sales_order": cart.sales_order,
            "payment_status": "Unpaid"
        }, notice="Sales Order already exists.")
    
    
    # -----------------------------------------------------
    # Create Sales Order
    # -----------------------------------------------------
    so = create_sales_order_from_cart(cart)
    
    # -----------------------------------------------------
    # Update Cart
    # -----------------------------------------------------
     
    cart.sales_order = so.name
    cart.status = "Ordered"
    cart.ordered_on = now_datetime()
    cart.checkout_by = frappe.session.user
    cart.save(ignore_permissions=True)
    
    # -----------------------------------------------------
    # Update Payment Request
    # -----------------------------------------------------
    pr.reference_doctype = "Sales Order"
    pr.reference_name = so.name
    pr.save(ignore_permissions=True)

    return success_response(
        {
            "sales_order": so.name,
            "payment_status": "Unpaid"
        },
        notice="Order created with Pay Later",
        status_code=HTTP_CREATED,
    )