import frappe
import razorpay
from frappe.utils import today
from yob_storefront.api.response import (
    HTTP_CONFLICT,
    HTTP_INTERNAL_SERVER_ERROR,
    HTTP_UNPROCESSABLE,
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
from yob_storefront.services.order_service import create_sales_order_from_cart

# =========================================================
# CACHE HELPERS
# =========================================================

def get_razorpay_settings():
    cache_key = "yob:razorpay:settings"

    settings = frappe.cache().get_value(cache_key)

    if not settings:
        doc = frappe.get_single("Razorpay Settings")

        settings = {
            "api_key": doc.api_key,
            "api_secret": doc.get_password("api_secret")
        }

        frappe.cache().set_value(cache_key, settings)

    return settings


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
    settings = get_razorpay_settings()

    client = razorpay.Client(
        auth=(settings["api_key"], settings["api_secret"])
    )

    params = {
        "razorpay_order_id": razorpay_order_id,
        "razorpay_payment_id": razorpay_payment_id,
        "razorpay_signature": razorpay_signature
    }

    try:
        client.utility.verify_payment_signature(params)

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

    settings = get_razorpay_settings()

    client = razorpay.Client(
        auth=(
            settings["api_key"],
            settings["api_secret"]
        )
    )

    try:
        return client.order.fetch(order_id)

    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "Razorpay Fetch Order Error"
        )
        raise
    
# =========================================================
# Fetch Razorpay payment details. using Razorpay Payment ID
# =========================================================
def get_razorpay_payment(payment_id):

    settings = get_razorpay_settings()

    client = razorpay.Client(
        auth=(
            settings["api_key"],
            settings["api_secret"]
        )
    )

    return client.payment.fetch(payment_id)
    
# =========================================================
# GET PAYMENT REQUEST
# =========================================================

def get_payment_request_by_razorpay_order_id(razorpay_order_id):

    pr_name = frappe.db.get_value(
        "Payment Request",
        {"custom_razorpay_order_id": razorpay_order_id},
        "name"
    )

    if not pr_name:
        frappe.throw("Payment Request not found")

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

    log = None

    # try:

    # -------------------------------------------------
    # Verify Razorpay Signature
    # -------------------------------------------------
    result = verify_razorpay_signature(
        razorpay_order_id,
        razorpay_payment_id,
        razorpay_signature
    )

    if is_error(result):
        return result

    # -------------------------------------------------
    # Get Payment Request
    # -------------------------------------------------
    pr = get_payment_request_by_razorpay_order_id(
        razorpay_order_id
    )

    # -------------------------------------------------
    # Prevent Duplicate Processing
    # -------------------------------------------------
    if pr.status == "Paid":
        return success_response(
            {
                "payment_request": pr.name
            },
            notice="Payment already processed."
        )

    # -------------------------------------------------
    # Fetch Razorpay Order & Payment
    # -------------------------------------------------
    order = get_razorpay_order(razorpay_order_id)
    payment = get_razorpay_payment(razorpay_payment_id)

    # -------------------------------------------------
    # Validate Order
    # -------------------------------------------------
    if order.get("status") != "paid":
        return error_response(
            PAYMENT_NOT_CAPTURED,
            "The payment has not been completed.",
            status_code=HTTP_UNPROCESSABLE,
        )

    # -------------------------------------------------
    # Validate Payment
    # -------------------------------------------------
    if payment.get("status") != "captured":
        return error_response(
            PAYMENT_NOT_CAPTURED,
            "The payment has not been captured.",
            status_code=HTTP_UNPROCESSABLE,
        )

    if payment.get("currency") != pr.currency:
        return error_response(
            PAYMENT_CURRENCY_MISMATCH,
            "The payment currency does not match the payment request.",
            status_code=HTTP_CONFLICT,
        )

    expected_amount = int(pr.grand_total * 100)

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
    # Save Payment Log
    # -------------------------------------------------
    log = save_razorpay_payment_log(
        pr=pr,
        payment=payment,
        razorpay_order_id=razorpay_order_id,
        razorpay_payment_id=razorpay_payment_id,
        razorpay_signature=razorpay_signature,
        status="Received"
    )

    # -------------------------------------------------
    # Validate Reference
    # -------------------------------------------------
    if pr.reference_doctype != "Cart":
        return error_response(
            PAYMENT_REFERENCE_INVALID,
            "This payment cannot be matched to a cart.",
            status_code=HTTP_UNPROCESSABLE,
        )

    # -------------------------------------------------
    # Get Cart
    # -------------------------------------------------
    cart = frappe.get_doc(
        "Cart",
        pr.reference_name
    )

    # -------------------------------------------------
    # Create Sales Order
    # -------------------------------------------------
    so = create_sales_order_from_cart(cart)

    # -------------------------------------------------
    # Update Cart
    # -------------------------------------------------
    cart.status = "Ordered"
    cart.sales_order = so.name
    cart.save(ignore_permissions=True)

    # -------------------------------------------------
    # Update Payment Request
    # -------------------------------------------------
     
    pr.transaction_date = frappe.utils.today()
    pr.mode_of_payment = "Razorpay"
    
    pr.custom_razorpay_payment_id   = razorpay_payment_id    
    pr.custom_razorpay_signature    = razorpay_signature
    pr.custom_razorpay_status       = payment["status"]
    pr.custom_razorpay_response     = frappe.as_json(payment)
    
    pr.reference_doctype = "Sales Order"
    pr.reference_name = so.name
    
    pr.custom_checkout_token = None
    pr.custom_checkout_expiry = None

    pr.save(ignore_permissions=True)  
    
    # -------------------------------------------------
    # Update Payment Log
    # -------------------------------------------------
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

    # except Exception:

    #     if log and log.docstatus == 0:
    #         log.status = "Failed"
    #         log.save(ignore_permissions=True)

    #     return server_error("Razorpay Payment Processing Error")