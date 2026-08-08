# copyright (c) 2026, YOB
# path: apps/yob_storefront/yob_storefront/services/order_service.py

import frappe
from frappe.utils import today

from erpnext.accounts.doctype.pricing_rule.utils import (
    apply_pricing_rule_on_transaction,
)


# =========================================================
# CREATE SALES ORDER FROM CART
# =========================================================

def create_sales_order_from_cart(cart):
    """
    Create a Sales Order from Cart.

    Args:
        cart (Cart): Cart document.

    Returns:
        Sales Order: Inserted Sales Order document.
    """

    so = frappe.new_doc("Sales Order")

    # -----------------------------------------------------
    # Basic Details
    # -----------------------------------------------------
    so.customer = cart.customer
    so.company = cart.company
    so.currency = cart.currency
    so.selling_price_list = cart.selling_price_list
    so.transaction_date = today()
    so.delivery_date = today()

    # -----------------------------------------------------
    # Coupon
    # -----------------------------------------------------
    if cart.coupon_code:
        coupon_name = frappe.db.get_value(
            "Coupon Code",
            {"coupon_code": cart.coupon_code},
            "name"
        )

        if coupon_name:
            so.coupon_code = coupon_name

    # -----------------------------------------------------
    # Customer Details
    # -----------------------------------------------------
    so.contact_person = cart.contact_person
    so.customer_address = cart.billing_address
    so.shipping_address_name = cart.shipping_address

    # -----------------------------------------------------
    # Taxes Template
    # -----------------------------------------------------
    if cart.taxes_and_charges:
        so.taxes_and_charges = cart.taxes_and_charges

    # -----------------------------------------------------
    # Items
    # -----------------------------------------------------
    for row in cart.items:
        so.append("items", {
            "item_code": row.item_code,
            "qty": row.quantity,
            "uom": row.uom or row.stock_uom,
            "stock_uom": row.stock_uom or row.uom,
            "rate": row.rate,
            "delivery_date": today(),
        })

    # -----------------------------------------------------
    # ERPNext Pricing Engine
    # -----------------------------------------------------
    so.set_missing_values()

    apply_pricing_rule_on_transaction(so)

    so.calculate_taxes_and_totals()

    # -----------------------------------------------------
    # Save
    # -----------------------------------------------------
    so.insert(ignore_permissions=True)

    # Uncomment if you want immediate submission
    # so.submit()

    return so


# =========================================================
# SUBMIT SALES ORDER
# =========================================================

def submit_sales_order(so):
    """
    Submit Sales Order if not already submitted.
    """

    if isinstance(so, str):
        so = frappe.get_doc("Sales Order", so)

    if so.docstatus == 0:
        so.submit()

    return so


# =========================================================
# CANCEL SALES ORDER
# =========================================================

def cancel_sales_order(so):
    """
    Cancel Sales Order.
    """

    if isinstance(so, str):
        so = frappe.get_doc("Sales Order", so)

    if so.docstatus == 1:
        so.cancel()

    return so