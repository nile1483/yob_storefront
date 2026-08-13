import frappe
from yob_core.api.boundary import yob_api
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
from yob_storefront.services.cart_service import reprice_cart
from yob_storefront.services.payment_request_service import issue_checkout_credential
from yob_storefront.utils.context import STOREFRONT_APP, get_storefront_customer


@frappe.whitelist(methods=["POST"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def proceed_to_payment(auth_context=None):
    """Issue (or re-issue) the checkout credential for the buyer's open Cart.

    This is the ONLY path that creates or replaces a Cart-backed Payment
    Request, which is what lets the ordering below be the whole concurrency
    story:

        locate Cart -> FOR UPDATE -> reload -> reprice -> save -> fingerprint
        -> ONLY THEN look for existing Payment Requests

    Candidate lookup after the lock is the point. Two competing Proceed calls
    serialise on the Cart row, so the second one reloads and SEES what the first
    one created, and reuses it. Looking first and locking later is what allows
    two live payment obligations for one cart.

    No explicit commit: there is no provider call here, so the normal
    request-end transaction boundary is the right one, and committing early
    would only publish a half-finished supersession.
    """

    customer = get_storefront_customer(auth_context)

    # ------------------------------------------------
    # Locate the Draft Cart -- identity only, no data read yet
    # ------------------------------------------------
    cart_name = frappe.db.get_value(
        "Cart",
        {"customer": customer.name, "status": "Draft"},
        "name",
    )

    if not cart_name:
        return error_response(
            CART_NOT_FOUND,
            "No open cart was found.",
            status_code=HTTP_NOT_FOUND,
        )

    # ------------------------------------------------
    # Lock the Cart row, THEN read it
    # ------------------------------------------------
    # A competing request blocks here until this transaction ends. The reload
    # afterwards is what makes the lock worth taking: it is how this request
    # observes whatever the winner committed.
    frappe.db.get_value("Cart", cart_name, "name", for_update=True)

    cart = frappe.get_doc("Cart", cart_name)

    if not cart.items:
        return error_response(
            CART_EMPTY,
            "The cart is empty.",
            status_code=HTTP_UNPROCESSABLE,
        )

    # ------------------------------------------------
    # Validate required fields (against the locked state)
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
    # Authoritative pricing, persisted
    # ------------------------------------------------
    # Unlike the public GET, this is an authenticated POST by the cart's owner:
    # the priced state it is about to be billed for is worth storing, and the
    # Payment Request is issued against exactly this saved state.
    reprice_cart(cart, customer)
    cart.save(ignore_permissions=True)

    # ------------------------------------------------
    # Issue / reuse / rotate / supersede -- still under the lock
    # ------------------------------------------------
    result = issue_checkout_credential(cart, customer)

    token = result["token"]

    return success_response(
        {
            "payment_url": f"/payment/{token}",
            "payment_request": result["payment_request"],
            "token": token,
        },
        notice="Proceed to payment",
        # 201 only when a Payment Request was actually created. Reusing an open
        # obligation -- with or without a rotated credential -- is not creation.
        status_code=HTTP_CREATED if result["created"] else HTTP_OK,
    )
