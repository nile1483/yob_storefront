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

    return {
        "cart": cart_dict,
        "contact": contact_data,
        "billing_address": billing_address_data,
        "shipping_address": shipping_address_data,
        "cart_updated": bool(removed_items or price_updated_items),
        "removed_items": removed_items or [],
        "price_updated_items": price_updated_items or [],
    }
    
def get_available_payment_methods(customer, company, order_amount):

    customer_group = frappe.db.get_value(
        "Customer",
        customer,
        "customer_group"
    )

    assignments = frappe.get_all(
        "Payment Method Assignment",
        filters={"is_active": 1},
        fields=[
            "payment_method",
            "reference_doctype",
            "reference_name",
            "minimum_order_amount",
            "maximum_order_amount"
        ]
    )

    valid_methods = set()

    for a in assignments:

        # ---------------------------
        # Assignment target
        # ---------------------------

        if a.reference_doctype == "Customer":
            if a.reference_name != customer:
                continue

        elif a.reference_doctype == "Customer Group":
            if a.reference_name != customer_group:
                continue

        elif a.reference_doctype == "Company":
            if a.reference_name != company:
                continue

        # ---------------------------
        # Order amount rules
        # ---------------------------

        if a.minimum_order_amount and order_amount < a.minimum_order_amount:
            continue

        if a.maximum_order_amount and order_amount > a.maximum_order_amount:
            continue

        valid_methods.add(a.payment_method)

    if not valid_methods:
        return []

    methods = frappe.get_all(
        "Payment Method",
        filters={
            "name": ["in", list(valid_methods)],
            "is_active": 1
        },
        fields=[
            "name",
            "method_code",
            "payment_type",
            "display_order",
            "icon",
            "description"
        ],
        order_by="display_order asc"
    )

    return methods



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
        fields=["name", "disabled", "is_sales_item"]
    )

    item_map = {d.name: d for d in items}
    
    valid_rows = []
    
    for row in cart.items:
        
        item = item_map.get(row.item_code)

        if not item or item.disabled or not item.is_sales_item:
            removed_items.append(row.item_code)
            continue

        valid_rows.append(row) 

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
    
    sync_sales_order_to_cart(cart, so)

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