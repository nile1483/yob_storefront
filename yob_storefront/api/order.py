import frappe
from yob_core.api.boundary import yob_api
from yob_auth.security.decorators import require_application
from yob_storefront.api.response import (
    HTTP_NOT_FOUND,
    HTTP_UNPROCESSABLE,
    ORDER_NOT_FOUND,
    VALIDATION_FAILED,
    error_response,
    success_response,
)
from yob_storefront.utils.context import STOREFRONT_APP, get_storefront_customer

# ---------------- ORDER LIST ----------------

@frappe.whitelist(methods=["GET"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def get_orders(auth_context=None):
    customer = get_storefront_customer(auth_context).name

    orders = frappe.get_all(
        "Sales Order",
        filters={"customer": customer},
        fields=[
            "name",
            "status",
            "grand_total",
            "transaction_date",
            "delivery_date"
        ],
        order_by="creation desc"
    )
    
    return success_response(
        data=orders,
        notice="Orders fetched successfully",
        meta={"count": len(orders)},
    )


@frappe.whitelist(methods=["GET"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def get_order_details(order_id=None, auth_context=None):

    if not order_id:
        return error_response(
            VALIDATION_FAILED,
            "Order ID is required.",
            field="order_id",
            status_code=HTTP_UNPROCESSABLE,
        )

    customer = get_storefront_customer(auth_context).name

    if not frappe.db.exists(
        "Sales Order",
        {
            "name": order_id,
            "customer": customer
        }
    ):
        # Not-owned and non-existent orders answer identically, so the response
        # cannot be used to probe for other customers' orders.
        return error_response(
            ORDER_NOT_FOUND,
            "Order not found.",
            field="order_id",
            status_code=HTTP_NOT_FOUND,
        )

    order = frappe.get_doc("Sales Order", order_id)

    # ---------------------------------------------------------
    # Calculate Discount
    # ---------------------------------------------------------
    total_item_discount = sum(
        (item.price_list_rate - item.rate) * item.qty
        for item in order.items
    )

    original_total = order.total + total_item_discount

    # ---------------------------------------------------------
    # Payment Details
    # ---------------------------------------------------------
    payment_logs = frappe.get_all(
        "Razorpay Payment Log",
        filters={
            "reference_doctype": "Sales Order",
            "reference_name": order.name
        },
        fields=[
            "name",
            "payment_status",
            "payment_method",
            "payment_amount",
            "currency",
            "razorpay_order_id",
            "razorpay_payment_id",
            "email",
            "contact",
            "creation"
        ],
        order_by="creation desc"
    )

    data = {
        "name": order.name,
        "customer": order.customer,
        "status": order.status,

        "transaction_date": order.transaction_date,
        "delivery_date": order.delivery_date,

        "currency": order.currency,

        "original_total": original_total,
        "discount": total_item_discount,
        "subtotal": order.total,
        "net_total": order.net_total,
        "tax": order.total_taxes_and_charges,
        "grand_total": order.grand_total,
        "rounded_total": order.rounded_total,

        "contact_person": order.contact_person,
        "contact_email": order.contact_email,
        "contact_mobile": order.contact_mobile,

        "billing_address_name": order.customer_address,
        "shipping_address_name": order.shipping_address_name,

        "payment_terms_template": order.payment_terms_template,
        "tc_name": order.tc_name,
        "terms": order.terms,

        "items": [
                {
                    "item_code": item.item_code,
                    "item_name": item.item_name,
                    "description": item.description,
                    "image": item.image,
                    "qty": item.qty,
                    "uom": item.uom,
                    "price_list_rate": item.price_list_rate,
                    "rate": item.rate,
                    "discount_percentage": item.discount_percentage,
                    "discount_amount": (item.price_list_rate - item.rate) * item.qty,
                    "amount": item.amount,
                    "net_amount": item.net_amount,
                }
                for item in order.items
            ],
        "taxes": order.taxes,  

        "payment_logs": payment_logs
    }

    # ---------------------------------------------------------
    # Billing Address
    # ---------------------------------------------------------
    if order.customer_address:

        data["billing_address"] = frappe.db.get_value(
            "Address",
            order.customer_address,
            [
                "address_title",
                "address_line1",
                "address_line2",
                "city",
                "state",
                "country",
                "pincode",
                "phone"
            ],
            as_dict=True
        )

    # ---------------------------------------------------------
    # Shipping Address
    # ---------------------------------------------------------
    if order.shipping_address_name:

        data["shipping_address"] = frappe.db.get_value(
            "Address",
            order.shipping_address_name,
            [
                "address_title",
                "address_line1",
                "address_line2",
                "city",
                "state",
                "country",
                "pincode",
                "phone"
            ],
            as_dict=True
        )

    return success_response(
        data=data,
        notice="Order fetched successfully"
    )