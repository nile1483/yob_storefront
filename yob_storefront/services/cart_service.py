import frappe
from frappe.utils import get_url
from frappe.utils import now_datetime, get_datetime
from yob_storefront.utils.store import get_store_settings

from yob_storefront.services.pricing_service import (
    calculate_cart_using_sales_order,
    sync_sales_order_to_cart,
)

def build_cart_response(cart, removed_items=None, price_updated_items=None):

    cart_dict = cart.as_dict(no_default_fields=True)
    cart_dict["name"] = cart.name

    # ADDITIVE: the authoritative pricing result, paid rows and ERPNext-generated
    # promotion rows alike. `items` keeps its existing shape and meaning so the
    # deployed Angular cart is unaffected; a later frontend gate renders this.
    #
    # `items` cannot express a promotion: a same-SKU free row shares its paid
    # row's item_code, and a different-SKU gift has no Cart row at all.
    cart_dict["pricing_rows"] = cart.flags.get("pricing_projection") or []

    for item in cart_dict.get("items", []):
        item["image"] = get_url(item["image"]) if item.get("image") else None
    # ===============================
    # CONTACT
    # ===============================

    contact_data = None

    if cart.contact_person:
        contact = frappe.get_doc("Contact", cart.contact_person)

        contact_data = {
            "name": contact.name,
            "full_name": contact.full_name,
            "email": contact.email_ids[0].email_id if contact.email_ids else None,
            "phone": contact.phone_nos[0].phone if contact.phone_nos else None,
        }

    # ===============================
    # BILLING ADDRESS
    # ===============================

    billing_address_data = None

    if cart.billing_address:
        addr = frappe.get_doc("Address", cart.billing_address)

        billing_address_data = {
            "name": addr.name,
            "display": addr.get_display(),
            "city": addr.city,
            "state": addr.state,
            "country": addr.country,
            "pincode": addr.pincode,
        }

    # ===============================
    # SHIPPING ADDRESS
    # ===============================

    shipping_address_data = None

    if cart.shipping_address:
        addr = frappe.get_doc("Address", cart.shipping_address)

        shipping_address_data = {
            "name": addr.name,
            "display": addr.get_display(),
            "city": addr.city,
            "state": addr.state,
            "country": addr.country,
            "pincode": addr.pincode,
        }

    # ADDITIVE reconciliation list. A line whose UNIT meaning moved -- the
    # merchant edited the Item's conversion factor, or dropped the UOM the row was
    # priced in -- must never change quietly: "2" was chosen as 2 Boxes and the
    # buyer has to be told if it is now worth something else. Empty in normal
    # operation.
    uom_changed_items = cart.flags.get("uom_changed_items") or []

    return {
        "cart": cart_dict,
        "contact": contact_data,
        "billing_address": billing_address_data,
        "shipping_address": shipping_address_data,
        "cart_updated": bool(removed_items or price_updated_items or uom_changed_items),
        "removed_items": removed_items or [],
        "price_updated_items": price_updated_items or [],
        "uom_changed_items": uom_changed_items,
    }
    
def get_available_payment_methods(customer, company, order_amount):
    """Compatibility shim. The rule lives in payment_method_service.

    This was the SECOND copy of the eligibility rule; it had already drifted
    from the API copy (it lacked the missing-customer guard). Kept as a
    forwarding function so existing callers and any external import keep
    working, but it owns nothing.
    """

    from yob_storefront.services.payment_method_service import (
        get_eligible_payment_methods,
    )

    return get_eligible_payment_methods(customer, company, order_amount)



# =========================================================
# CART REPRICE + CLEAN
# =========================================================


def reprice_cart(cart, customer):
    """
    Recalculate cart prices based on the customer's pricing rules.

    Args:
        cart (Cart): The shopping cart to reprice.
        customer (Customer): The customer whose pricing rules are applied.

    Returns:
        tuple[list[CartItem], list[CartItem]]:
            A tuple containing:
            - removed_items: Items removed from the cart because they are
            no longer available or eligible.
            - updated_items: Items whose prices were updated.
    """

    removed_items = []
    updated_items = []

    # Snapshot before recalculation
    old_snapshot = {
        row.name: {
                        "item_code": row.item_code,
                        "quantity": row.quantity,
                        "rate": row.rate,
                        "total": row.total_amount,
        }
        for row in cart.items
    }

    # ----------------------------
    # Remove disabled / invalid items
    # ----------------------------
    
    item_codes = list({row.item_code for row in cart.items})
    
    items = frappe.get_all(
        "Item",
        filters={"name": ["in", item_codes]},
        fields=["name", "disabled", "is_sales_item", "is_stock_item"]
    )

    item_map = {d.name: d for d in items}

    valid_rows = []

    for row in cart.items:

        item = item_map.get(row.item_code)

        if not item or item.disabled or not item.is_sales_item:
            removed_items.append(row.item_code)
            continue

        valid_rows.append(row)

    # ------------------------------------------------------------------
    # Shipping applicability, derived -- never client-supplied.
    #
    # A cart needs a shipping address as soon as ONE line is a physical good.
    # ERPNext already models this as `is_stock_item` ("Maintain Stock"): stock
    # items move through a warehouse, non-stock items (services, digital) do
    # not. Using it avoids inventing a parallel virtual/physical field.
    #
    # Recomputed on every reprice, so removing the last physical line clears
    # the requirement again. If a dedicated virtual/physical field is added
    # later, only this predicate changes.
    # ------------------------------------------------------------------
    cart.is_shippable = 1 if any(
        (item_map.get(row.item_code) or {}).get("is_stock_item")
        for row in valid_rows
    ) else 0 

    cart.set("items", valid_rows)

    # ------------------------------------------------
    # Cart became empty
    # ------------------------------------------------
    
    if not cart.items:
        cart.total_quantity = 0
        cart.net_total = 0
        cart.tax_total = 0
        cart.grand_total = 0
        cart.total_discount = 0
        cart.coupon_discount = 0
        cart.coupon_code = None
        return removed_items, updated_items

    # --------------------------------------------
    # Recalculate using ERPNext pricing engine
    # --------------------------------------------
    
    so = calculate_cart_using_sales_order(cart, customer)  

    # --------------------------------------------
    # Sync sales order to cart
    # --------------------------------------------
    
    # The projection is the authoritative pricing RESULT, including the
    # promotion rows ERPNext generated. It is stashed on the document rather
    # than persisted: Cart Items stay customer intent only.
    cart.flags.pricing_projection = sync_sales_order_to_cart(cart, so)

    # ------------------------------------------------
    # Detect updated items
    # ------------------------------------------------     
     
    for row in cart.items:
        old = old_snapshot.get(row.name)
        
        if not old:
            continue

        if (
            old["quantity"] != row.quantity 
            or old["rate"] != row.rate 
            or old["total"] != row.total_amount
            ):
               updated_items.append(row.item_code)

    return removed_items, updated_items


def validate_cart_expiry(cart):
    settings = get_store_settings() 
    
    if not settings.cart_expiry:
        return 0

    expiry_seconds = settings.cart_expiry * 3600

    modified = get_datetime(cart.modified)
    
    if (now_datetime() - modified).total_seconds() > expiry_seconds:

        # Remove all items
        cart.set("items", [])

        # Clear totals
        cart.total_quantity = 0
        cart.net_total = 0
        cart.tax_total = 0
        cart.grand_total = 0
        cart.total_discount = 0
        cart.coupon_discount = 0
        cart.coupon_code = None

        # Clear taxes if applicable
        if hasattr(cart, "taxes"):
            cart.set("taxes", [])
 

        cart.save(ignore_permissions=True)

        return 1