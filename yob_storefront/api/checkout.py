import frappe
import secrets
from datetime import timedelta
from frappe.utils import now_datetime
from yob_storefront.api.response import (
    BILLING_ADDRESS_REQUIRED,
    CART_EMPTY,
    CART_NOT_FOUND,
    CONTACT_REQUIRED,
    HTTP_CREATED,
    HTTP_NOT_FOUND,
    HTTP_OK,
    HTTP_UNPROCESSABLE,
    SHIPPING_ADDRESS_REQUIRED,
    error_response,
    success_response,
)
from yob_auth.security.decorators import require_application
from yob_storefront.utils.context import STOREFRONT_APP, get_storefront_customer
from pprint import pprint

@frappe.whitelist()
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def proceed_to_payment(auth_context=None):
    

    # ------------------------------------------------
    # Validate Customer
    # ------------------------------------------------
    customer = get_storefront_customer(auth_context)

    # ------------------------------------------------
    # Get Draft Cart
    # ------------------------------------------------
    cart_name = frappe.db.get_value(
        "Cart",
        {"customer": customer.name, "status": "Draft"}
    )

    if not cart_name:
        return error_response(
            CART_NOT_FOUND,
            "No open cart was found.",
            status_code=HTTP_NOT_FOUND,
        )

    cart = frappe.get_doc("Cart", cart_name)

    if not cart.items:
        return error_response(
            CART_EMPTY,
            "The cart is empty.",
            status_code=HTTP_UNPROCESSABLE,
        )

    # ------------------------------------------------
    # Validate Required Fields
    # ------------------------------------------------
    if not cart.contact_person:
        return error_response(
            CONTACT_REQUIRED,
            "Please select a contact person.",
            field="contact_person",
            status_code=HTTP_UNPROCESSABLE,
        )

    if not cart.billing_address:
        return error_response(
            BILLING_ADDRESS_REQUIRED,
            "Please select a billing address.",
            field="billing_address",
            status_code=HTTP_UNPROCESSABLE,
        )

    if cart.is_shippable and not cart.shipping_address:
        return error_response(
            SHIPPING_ADDRESS_REQUIRED,
            "Please select a shipping address.",
            field="shipping_address",
            status_code=HTTP_UNPROCESSABLE,
        )

    # ------------------------------------------------
    # Find Existing Payment Request
    # ------------------------------------------------
    pr_name = frappe.db.get_value(
            "Payment Request",
            {
                "reference_doctype": "Cart",
                "reference_name": cart.name,
                "party": cart.customer,
                "status": ["not in", ["Paid", "Cancelled"]]
            },
            "name"
        )
   
    # ------------------------------------------------
    # Create Payment Request if not exists
    # ------------------------------------------------
    if pr_name:

        pr = frappe.get_doc("Payment Request", pr_name)

        pr.grand_total = cart.grand_total
        pr.currency = cart.currency

        created = False

    else:

        created = True

        pr = frappe.get_doc({
            "doctype": "Payment Request",
            "payment_request_type": "Inward",
            "party_type": "Customer",
            "party": cart.customer,
            "reference_doctype": "Cart",
            "reference_name": cart.name,
            "grand_total": cart.grand_total,
            "currency": cart.currency,
            "email_to": frappe.session.user,
            "subject": f"Payment for Cart {cart.name}"
        })
        pprint("Payment Request: ")
        pr.insert(ignore_permissions=True)
        
        pprint(pr.as_dict())
        
        

    # ------------------------------------------------
    # Generate Secure Checkout Token
    # ------------------------------------------------
    token = secrets.token_urlsafe(32)

    pr.custom_checkout_token  = token
    pr.custom_checkout_expiry = now_datetime() + timedelta(hours=1)

    pr.save(ignore_permissions=True)

    # ------------------------------------------------
    # Return Payment Page URL
    # ------------------------------------------------
    # 201 only when a Payment Request was actually created; reusing an open one
    # is an update, not a creation.
    return success_response(
        {
            "payment_url": f"/payment/{token}",
            "payment_request": pr.name,
            "token": token
        },
        notice="Proceed to payment",
        status_code=HTTP_CREATED if created else HTTP_OK,
    )

    # except Exception:
    #     # Never return frappe.get_traceback() to a client.
    #     return server_error("Checkout Error", "Failed to start checkout")