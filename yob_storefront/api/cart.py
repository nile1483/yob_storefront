import frappe
from yob_core.api.boundary import yob_api
from yob_storefront.api.response import (
    BILLING_ADDRESS_INVALID,
    BILLING_ADDRESS_REQUIRED,
    CONTACT_INVALID,
    CONTACT_REQUIRED,
    COUPON_CODE_REQUIRED,
    HTTP_NOT_FOUND,
    HTTP_UNPROCESSABLE,
    ITEM_NOT_FOUND,
    QUANTITY_INVALID,
    SHIPPING_ADDRESS_INVALID,
    SHIPPING_ADDRESS_REQUIRED,
    SHIPPING_NOT_APPLICABLE,
    VALIDATION_FAILED,
    error_response,
    is_error,
    server_error,
    success_response,
)
from yob_auth.security.decorators import require_application
from yob_storefront.utils.context import STOREFRONT_APP, get_storefront_customer
from erpnext.setup.doctype.company.company import get_default_company_address
from yob_storefront.services.cart_service import ( 
    reprice_cart,
    validate_cart_expiry
) 
from yob_storefront.services.cart_service import build_cart_response
from yob_storefront.services.coupon_service import CouponService
from yob_storefront.utils.store import get_store_settings
from frappe.utils import now_datetime 
from pprint import pprint
 
# =========================================================
# HELPERS
# =========================================================
 

def get_or_create_cart(customer):
    cart_name = frappe.db.get_value(
        "Cart", {"customer": customer.name, "status": "Draft"}, "name"
    )

    if cart_name:
        return frappe.get_doc("Cart", cart_name)

    settings = get_store_settings()

    company_address = get_default_company_address(settings.company)

    cart = frappe.get_doc(
        {
            "doctype": "Cart",
            "customer": customer.name,
            "user": frappe.session.user,
            "company": settings.company,
            "currency": settings.default_currency,
            "selling_price_list": settings.default_price_list,
            "company_address": company_address,
            "status": "Draft",
        }
    )

    cart.insert(ignore_permissions=True)
    return cart 


# =========================================================
# GET CART
# =========================================================


@frappe.whitelist(methods=["GET"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def get_cart(auth_context=None):
    try:
        customer = get_storefront_customer(auth_context)
        
        cart = get_or_create_cart(customer)

        validate_cart_expiry(cart)
        
        removed_items, price_updated_items = reprice_cart(cart, customer)

        # cart.ordered_on = now_datetime()
        # cart.checkout_by = frappe.session.user 
        # print("Before save:", cart.get_valid_dict())
        cart.save(ignore_permissions=True)
        # print("After save:", cart.get_valid_dict())

        cart.db_set("ordered_on", now_datetime(), update_modified=False)
        cart.db_set("checkout_by", frappe.session.user, update_modified=False)

        response_data = build_cart_response(cart, removed_items, price_updated_items)

        notice = "Cart loaded"

        if removed_items:
            notice = "Some items were removed because they are no longer available."

        elif price_updated_items:
            notice = "Some items were updated due to pricing changes."

        return success_response(response_data, notice=notice)

    except Exception:
        return server_error("Get Cart Error", "Failed to load cart")


# =========================================================
# ADD / UPDATE ITEM
# =========================================================


@frappe.whitelist(methods=["POST"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def add_to_cart(item_code=None, qty=1, auth_context=None):
    try:
        if not item_code:
            return error_response(
                VALIDATION_FAILED,
                "Item code is required.",
                field="item_code",
                status_code=HTTP_UNPROCESSABLE,
            )

        qty = float(qty)
        if qty <= 0:
            return error_response(
                QUANTITY_INVALID,
                "Quantity must be greater than zero.",
                field="qty",
                status_code=HTTP_UNPROCESSABLE,
            )

        customer = get_storefront_customer(auth_context)
        cart = get_or_create_cart(customer)

        existing = next((row for row in cart.items if row.item_code == item_code), None)

        if existing:
            # INCREMENT, not replace: `qty` is a delta. Kept on ONE row per item
            # rather than appending a second row, because ERPNext evaluates a
            # Pricing Rule's min_qty/max_qty against the ROW's quantity -- two
            # rows of 5 would silently miss a min_qty=10 rule that one row of 10
            # satisfies. One row also prices identically to a Desk-entered
            # Sales Order.
            #
            # Callers send a delta, so this is NOT idempotent: a retried or
            # double-submitted request adds twice. Use set_cart_item_qty-style
            # absolute updates from a cart stepper if that matters.
            existing.quantity = (existing.quantity or 0) + qty
        else:
            item = frappe.get_doc("Item", item_code)
            cart.append("items", {
                            "item_code": item.item_code,
                            "item_name": item.item_name,
                            "quantity": qty,
                            "uom": item.stock_uom,
                            "stock_uom": item.stock_uom,
                            "conversion_factor": 1,
                            "image": item.image, 
                            "item_slug": item.custom_slug, 
                        })

        
        validate_cart_expiry(cart)
        
        removed_items, price_updated_items = reprice_cart(cart, customer)


        cart.save(ignore_permissions=True)

        return success_response(cart.as_dict(no_default_fields=True), notice="Item added")

    except frappe.DoesNotExistError:
        return error_response(
            ITEM_NOT_FOUND,
            "Item not found.",
            field="item_code",
            status_code=HTTP_NOT_FOUND,
        )

    except Exception:
        return server_error("Add To Cart Error", "Failed to add item")


# =========================================================
# REMOVE ITEM
# =========================================================


@frappe.whitelist(methods=["POST"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def remove_from_cart(item_code=None, auth_context=None):
    try:
        # Without this, a blank item_code matched no row, so the cart was
        # repriced and saved for a removal that never happened.
        if not item_code:
            return error_response(
                VALIDATION_FAILED,
                "Item code is required.",
                field="item_code",
                status_code=HTTP_UNPROCESSABLE,
            )

        customer = get_storefront_customer(auth_context)
        cart = get_or_create_cart(customer)

        cart.set("items", [row for row in cart.items if row.item_code != item_code])

        validate_cart_expiry(cart)
        
        reprice_cart(cart, customer)
        
        cart.save(ignore_permissions=True)

        return success_response(cart.as_dict(no_default_fields=True), notice="Item removed")

    except Exception:
        return server_error("Remove Cart Error", "Failed to remove item")


# =========================================================
# CLEAR CART
# =========================================================


@frappe.whitelist(methods=["POST"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def clear_cart(auth_context=None):
    try:
        customer = get_storefront_customer(auth_context)
        cart = get_or_create_cart(customer)

        cart.set("items", [])
        reprice_cart(cart, customer)
        cart.save(ignore_permissions=True)

        return success_response(cart.as_dict(no_default_fields=True), notice="Cart cleared")

    except Exception:
        return server_error("Clear Cart Error", "Failed to clear cart")


# =========================================================
# APPLY COUPON
# =========================================================


# @frappe.whitelist()
# def apply_coupon(coupon_code):
#     try:
#         customer = get_storefront_customer(auth_context)
#         cart = get_or_create_cart(customer)

#         if not frappe.db.exists("Coupon Code", coupon_code):
#             return error("Invalid coupon code")

#         cart.coupon_code = coupon_code

#         removed_items, price_updated_items = reprice_cart(cart, customer)
#         cart.save(ignore_permissions=True)

#         return success(cart.as_dict(no_default_fields=True), "Coupon applied")

#     except Exception:
#         frappe.log_error(frappe.get_traceback(), "Coupon Apply Error")
#         return error("Failed to apply coupon")


# =========================================================
# REMOVE COUPON
# =========================================================


# @frappe.whitelist()
# def remove_coupon():
#     try:
#         customer = get_storefront_customer(auth_context)
#         cart = get_or_create_cart(customer)

#         cart.coupon_code = None

#         reprice_cart(cart, customer)
#         cart.save(ignore_permissions=True)

#         return success(cart.as_dict(no_default_fields=True), "Coupon removed")

#     except Exception:
#         frappe.log_error(frappe.get_traceback(), "Coupon Remove Error")
#         return error("Failed to remove coupon")


@frappe.whitelist(methods=["POST"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def set_cart_contact(contact_person=None, auth_context=None):
    try:
        if not contact_person:
            return error_response(
                CONTACT_REQUIRED,
                "Contact is required.",
                field="contact_person",
                status_code=HTTP_UNPROCESSABLE,
            )

        customer = get_storefront_customer(auth_context)
        cart = get_or_create_cart(customer)

        # Validate contact belongs to customer
        if not frappe.db.exists(
            "Contact",
            {
                "name": contact_person,
                "link_doctype": "Customer",
                "link_name": customer.name,
            },
        ):
            return error_response(
                CONTACT_INVALID,
                "The selected contact does not belong to this customer.",
                field="contact_person",
                status_code=HTTP_UNPROCESSABLE,
            )

        cart.contact_person = contact_person
        cart.save(ignore_permissions=True)

        return success_response(
            {"contact_person": cart.contact_person}, notice="Contact updated"
        )

    except Exception:
        return server_error("Set Cart Contact Error", "Failed to update contact")


@frappe.whitelist(methods=["POST"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def set_cart_billing_address(billing_address=None, auth_context=None):
    try:
        if not billing_address:
            return error_response(
                BILLING_ADDRESS_REQUIRED,
                "Billing address is required.",
                field="billing_address",
                status_code=HTTP_UNPROCESSABLE,
            )

        customer = get_storefront_customer(auth_context)
        cart = get_or_create_cart(customer)

        # Validate address belongs to customer
        if not frappe.db.exists(
            "Dynamic Link",
            {
                "parent": billing_address,
                "link_doctype": "Customer",
                "link_name": customer.name,
            },
        ):
            return error_response(
                BILLING_ADDRESS_INVALID,
                "The selected billing address does not belong to this customer.",
                field="billing_address",
                status_code=HTTP_UNPROCESSABLE,
            )

        cart.billing_address = billing_address

        # Optional: Auto-assign shipping if not shippable
        if not cart.shipping_address:
            cart.shipping_address = billing_address

        cart.save(ignore_permissions=True)

        return success_response(
            {
                "billing_address": cart.billing_address,
                "shipping_address": cart.shipping_address,
            },
            notice="Billing address updated",
        )

    except Exception:
        return server_error("Set Billing Error", "Failed to update billing address")


@frappe.whitelist(methods=["POST"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def set_cart_shipping_address(shipping_address=None, auth_context=None):
    try:
        if not shipping_address:
            return error_response(
                SHIPPING_ADDRESS_REQUIRED,
                "Shipping address is required.",
                field="shipping_address",
                status_code=HTTP_UNPROCESSABLE,
            )

        customer = get_storefront_customer(auth_context)
        cart = get_or_create_cart(customer)

        if not cart.is_shippable:
            return error_response(
                SHIPPING_NOT_APPLICABLE,
                "Shipping is not required for this cart.",
                status_code=HTTP_UNPROCESSABLE,
            )

        # Validate address belongs to customer
        if not frappe.db.exists(
            "Dynamic Link",
            {
                "parent": shipping_address,
                "link_doctype": "Customer",
                "link_name": customer.name,
            },
        ):
            return error_response(
                SHIPPING_ADDRESS_INVALID,
                "The selected shipping address does not belong to this customer.",
                field="shipping_address",
                status_code=HTTP_UNPROCESSABLE,
            )

        cart.shipping_address = shipping_address
        cart.save(ignore_permissions=True)

        return success_response(
            {"shipping_address": cart.shipping_address}, notice="Shipping address updated"
        )

    except Exception:
        return server_error("Set Shipping Error", "Failed to update shipping address")


# =========================================================
# APPLY COUPON
# =========================================================


@frappe.whitelist(methods=["POST"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def apply_coupon(code=None, auth_context=None):

    # try:
    # CouponService raises the same code for a blank value, but guarding here
    # keeps the answer identical without first creating or loading a cart.
    if not code:
        return error_response(
            COUPON_CODE_REQUIRED,
            "Coupon code is required.",
            field="code",
            status_code=HTTP_UNPROCESSABLE,
        )

    customer = get_storefront_customer(auth_context)
    
    cart = get_or_create_cart(customer)
    
    validate_cart_expiry(cart)
    
    service = CouponService(cart, customer)

    result = service.apply(code)

    if is_error(result):
        return result

    removed_items, price_updated_items = reprice_cart(cart, customer)

    cart.save(ignore_permissions=True)

    return success_response(
        {
            "cart": cart.as_dict(no_default_fields=True),
            "coupon_code": cart.coupon_code,
            "removed_items": removed_items,
            "price_updated_items": price_updated_items,
        },
        notice="Coupon applied successfully",
    )

    # except Exception:
    #     return server_error("Apply Coupon")


# =========================================================
# REMOVE COUPON
# =========================================================


@frappe.whitelist(methods=["POST"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def remove_coupon(auth_context=None):
    try:
        customer = get_storefront_customer(auth_context)
        cart = get_or_create_cart(customer)

        service = CouponService(cart, customer)

        result = service.remove()

        if is_error(result):
            return result

        removed_items, price_updated_items = reprice_cart(cart, customer)

        cart.save(ignore_permissions=True)

        return success_response(
            {
                "cart": cart.as_dict(no_default_fields=True),
                "removed_items": removed_items,
                "price_updated_items": price_updated_items,
            },
            notice="Coupon removed successfully",
        )

    except Exception:
        return server_error("Remove Coupon", "Failed to remove coupon")
