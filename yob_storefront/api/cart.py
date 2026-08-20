import frappe
from yob_core.api.boundary import yob_api
from yob_storefront.api.response import (
    BILLING_ADDRESS_INVALID,
    BILLING_ADDRESS_REQUIRED,
    CART_ITEM_UOM_CHANGED,
    CONTACT_INVALID,
    ITEM_IS_TEMPLATE,
    ITEM_NOT_PURCHASABLE,
    CONTACT_REQUIRED,
    COUPON_CODE_REQUIRED,
    HTTP_CONFLICT,
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

        # ------------------------------------------------------------------
        # THE SKU GATE -- before anything is read or written.
        #
        # Attributes are a SELECTION, resolved server-side; by the time a code
        # reaches here it is a claim about what to buy, and the server checks it
        # again rather than trusting the page that produced it. A template is the
        # loud case: ERPNext refuses to price one, and without this the failure
        # surfaced as a 500 with a logged traceback for what is really a bad
        # request (Phase 24A).
        # ------------------------------------------------------------------
        refusal = _refuse_unpurchasable(item_code)

        if refusal:
            return refusal

        cart = get_or_create_cart(customer)

        # Expiry BEFORE the row is added, never after. Appending first let
        # `validate_cart_expiry` empty the cart -- including the row just added --
        # and the endpoint still answered "Item added" over an empty cart, so the
        # response and the stored state disagreed (Phase 24A).
        validate_cart_expiry(cart)

        existing = next((row for row in cart.items if row.item_code == item_code), None)

        if existing:
            # ------------------------------------------------------------------
            # MERGE GUARD -- the unit the buyer just typed into vs the unit this
            # line is counted in.
            #
            # A Cart line keeps the selling UOM ERPNext resolved when that intent
            # was first priced (Phase 23B-5U). If the merchant has since changed
            # the item's selling UOM, the product page now shows Boxes while this
            # line still holds Nos -- and adding the buyer's "2" to it would file
            # 2 Boxes as 2 Nos. There is no safe silent answer: converting would
            # rewrite intent the buyer already gave, and a second row would need
            # duplicate-SKU carts, which YOB does not have.
            #
            # So the add is refused and the client is told which two units are in
            # play. The buyer removes the line and adds it again; they still never
            # CHOOSE a unit -- ERPNext decides it on the fresh line.
            #
            # A line with no recorded unit has not been priced yet and has no
            # meaning to protect. `resolved_selling_uom` returning None means
            # ERPNext declined to describe the item, which is not a mismatch --
            # the reprice below is what surfaces a genuinely unsellable item.
            # ------------------------------------------------------------------
            from yob_storefront.services.pricing_context import context_for

            current_uom = context_for(customer).resolved_selling_uom(item_code)

            if existing.uom and current_uom and existing.uom != current_uom:
                return error_response(
                    CART_ITEM_UOM_CHANGED,
                    "This item is now sold in a different unit. "
                    "Remove it from your cart and add it again.",
                    field="item_code",
                    details={
                        "item_code": item_code,
                        "existing_uom": existing.uom,
                        "current_uom": current_uom,
                    },
                    status_code=HTTP_CONFLICT,
                )

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

            # NO `uom` and NO `conversion_factor` here -- deliberately.
            #
            # This row used to be created with `uom = stock_uom` and
            # `conversion_factor = 1`, and every later pricing call passed that
            # value on. ERPNext then had no decision left to make: for an Item
            # with `sales_uom = Box` (factor 10) the product page priced 1000 per
            # Box while the Cart charged 100 per Nos for the same buyer input
            # (Phase 23B-5W). The buyer's quantity meant two different things.
            #
            # ERPNext already answers this: with no uom in context and a selling
            # doctype, `get_basic_details` uses `item.sales_uom or item.stock_uom`
            # and derives the conversion factor from the Item's own UOM table.
            # The reprice below therefore resolves both, and
            # `sync_sales_order_to_cart` writes them onto this row -- so the unit
            # a buyer's quantity is counted in is ERPNext's answer, recorded, and
            # then held steady for the life of the row.
            #
            # `stock_uom` is a plain fact about the Item, not a resolution, and it
            # is kept so the row can be labelled before it is first priced.
            cart.append("items", {
                            "item_code": item.item_code,
                            "item_name": item.item_name,
                            "quantity": qty,
                            "stock_uom": item.stock_uom,
                            "image": item.image, 
                            "item_slug": item.custom_slug, 
                        })

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


def _refuse_unpurchasable(item_code):
    """An error envelope when this exact code may not be bought, else None.

    Deliberately code-only. Attributes, UOM, warehouse, price list and rate are
    not accepted here and never were; what this adds is that the code itself is
    re-checked against ERPNext rather than assumed valid because a product page
    offered it.
    """

    from yob_storefront.services.variant_service import is_salable_sku, is_template

    if not frappe.db.exists("Item", item_code):
        return error_response(
            ITEM_NOT_FOUND,
            "Item not found.",
            field="item_code",
            status_code=HTTP_NOT_FOUND,
        )

    if is_template(item_code):
        return error_response(
            ITEM_IS_TEMPLATE,
            "Please choose the available options for this product.",
            field="item_code",
            status_code=HTTP_UNPROCESSABLE,
        )

    if not is_salable_sku(item_code):
        return error_response(
            ITEM_NOT_PURCHASABLE,
            "This item is no longer available.",
            field="item_code",
            status_code=HTTP_UNPROCESSABLE,
        )

    return None


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
