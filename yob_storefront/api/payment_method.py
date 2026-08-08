import frappe
from yob_auth.security.decorators import require_application
from yob_storefront.api.response import server_error, success_response
from yob_storefront.utils.context import (
    STOREFRONT_APP,
    assert_customer_matches,
    get_storefront_customer,
)


@frappe.whitelist()
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def get_payment_methods(customer=None, company=None, order_amount=0, auth_context=None):
    """Payment methods available to the AUTHENTICATED customer.

    ``customer`` and ``company`` are retained for frontend compatibility only.
    ``customer`` is never used as authorization truth: it is overwritten with
    the authenticated Customer, and a mismatching value is rejected outright.
    """

    # Authorization runs OUTSIDE the try block: a rejected caller must surface
    # as 403, never be swallowed into a generic "failed to load" 400.
    assert_customer_matches(auth_context, customer)
    customer = get_storefront_customer(auth_context).name

    try:
        order_amount = float(order_amount)

        customer_group = None

        if customer:
            customer_group = frappe.db.get_value("Customer", customer, "customer_group")

        assignments = frappe.get_all(
            "Payment Method Assignment",
            filters={"is_active": 1},
            fields=[
                "payment_method",
                "reference_doctype",
                "reference_name",
                "min_order_amount",
                "max_order_amount",
            ],
        )

        valid_methods = set()

        for a in assignments:

            # -----------------------------
            # Assignment Target Check
            # -----------------------------

            if a.reference_doctype == "Customer":
                if not customer or a.reference_name != customer:
                    continue

            elif a.reference_doctype == "Customer Group":
                if not customer_group or a.reference_name != customer_group:
                    continue

            elif a.reference_doctype == "Company":
                if a.reference_name != company:
                    continue

            # -----------------------------
            # Order Amount Check
            # -----------------------------

            if a.min_order_amount and order_amount < a.min_order_amount:
                continue

            if a.max_order_amount and order_amount > a.max_order_amount:
                continue

            valid_methods.add(a.payment_method)

        if not valid_methods:
            return success_response(
                [],
                notice="No payment methods available",
                meta={"count": 0},
            )

        methods = frappe.get_all(
            "Payment Method",
            filters={"name": ["in", list(valid_methods)], "is_active": 1},
            fields=[
                "name",
                "method_code",
                "payment_type",
                "display_order",
                "icon",
                "description",
            ],
            order_by="display_order asc",
        )

        return success_response(
            methods,
            notice="Payment methods loaded",
            meta={"count": len(methods)},
        )

    except Exception:
        return server_error("Get Payment Methods", "Failed to load payment methods")
