import frappe
from yob_core.api.boundary import yob_api
from yob_auth.security.decorators import require_application
from yob_storefront.api.response import server_error, success_response
from yob_storefront.services.payment_method_service import get_eligible_payment_methods
from yob_storefront.utils.context import (
    STOREFRONT_APP,
    assert_customer_matches,
    get_storefront_customer,
)


@frappe.whitelist(methods=["GET"])
@yob_api
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
        # The eligibility rule itself lives in the service, which is the single
        # authority shared with the public checkout payload and (later)
        # process_payment. This adapter only shapes the envelope.
        methods = get_eligible_payment_methods(
            customer, company, float(order_amount)
        )

        if not methods:
            return success_response(
                [],
                notice="No payment methods available",
                meta={"count": 0},
            )

        return success_response(
            methods,
            notice="Payment methods loaded",
            meta={"count": len(methods)},
        )

    except Exception:
        return server_error("Get Payment Methods", "Failed to load payment methods")
