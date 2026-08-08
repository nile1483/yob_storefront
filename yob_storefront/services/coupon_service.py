import frappe
from frappe.utils import today
from yob_storefront.api.response import (
    COUPON_CODE_REQUIRED,
    COUPON_EXPIRED,
    COUPON_INVALID,
    COUPON_MAXIMUM_EXCEEDED,
    COUPON_MINIMUM_NOT_MET,
    COUPON_NOT_ACTIVE,
    COUPON_NOT_APPLICABLE,
    COUPON_NOT_APPLIED,
    COUPON_USAGE_LIMIT_REACHED,
    HTTP_UNPROCESSABLE,
    error_response,
    is_error,
    success_response,
)

# The coupon code arrives as the ``code`` parameter of
# yob_storefront.api.cart.apply_coupon, so that is the ``field`` reported back.
COUPON_FIELD = "code"


def _rejected(code, detail):
    """A coupon the client may not use: a valid request with an unacceptable value."""

    return error_response(
        code,
        detail,
        field=COUPON_FIELD,
        status_code=HTTP_UNPROCESSABLE,
    )


class CouponService:

    def __init__(self, cart, customer):
        self.cart = cart
        self.customer = customer

    def apply(self, code):

        code = (code or "").strip().upper()

        if not code:
            return _rejected(COUPON_CODE_REQUIRED, "Coupon code is required.")

        result = self.validate(code)

        if is_error(result):
            return result

        self.cart.coupon_code = code

        return success_response(
            {
                "coupon": result["data"]["coupon"],
                "pricing_rule": result["data"]["pricing_rule"]
            },
            notice="Coupon applied successfully"
        )

    def remove(self):

        if not self.cart.coupon_code:
            return _rejected(COUPON_NOT_APPLIED, "No coupon is applied to this cart.")

        self.cart.coupon_code = None

        return success_response(
            None,
            notice="Coupon removed successfully"
        )

    def validate(self, code):

        coupon = frappe.db.get_value(
            "Coupon Code",
            {"coupon_code": code},
            [
                "name",
                "coupon_name",
                "coupon_code",
                "pricing_rule",
                "customer",
                "valid_from",
                "valid_upto",
                "maximum_use",
                "used"
            ],
            as_dict=True
        )

        if not coupon:
            return _rejected(COUPON_INVALID, "The coupon is invalid or expired.")

        current_date = today()

        if coupon.valid_from and current_date < str(coupon.valid_from):
            return _rejected(COUPON_NOT_ACTIVE, "This coupon is not active yet.")

        if coupon.valid_upto and current_date > str(coupon.valid_upto):
            return _rejected(COUPON_EXPIRED, "This coupon has expired.")

        if coupon.maximum_use and coupon.used >= coupon.maximum_use:
            return _rejected(
                COUPON_USAGE_LIMIT_REACHED,
                "This coupon has reached its usage limit."
            )

        if coupon.customer and coupon.customer != self.customer.name:
            return _rejected(
                COUPON_NOT_APPLICABLE,
                "This coupon is not applicable for this customer."
            )

        # A coupon whose pricing rule is missing, disabled or not usable for
        # selling is a configuration problem. The client is told only that the
        # coupon cannot be applied -- never the internal reason.
        if not coupon.pricing_rule:
            return _rejected(COUPON_INVALID, "The coupon is invalid or expired.")

        pricing_rule = frappe.db.get_value(
            "Pricing Rule",
            coupon.pricing_rule,
            [
                "name",
                "disable",
                "selling",
                "company",
                "currency",
                "valid_from",
                "valid_upto",
                "min_amt",
                "max_amt"
            ],
            as_dict=True
        )

        if not pricing_rule:
            return _rejected(COUPON_INVALID, "The coupon is invalid or expired.")

        if pricing_rule.disable:
            return _rejected(COUPON_NOT_APPLICABLE, "This coupon cannot be applied.")

        if not pricing_rule.selling:
            return _rejected(COUPON_NOT_APPLICABLE, "This coupon cannot be applied.")

        if pricing_rule.company and pricing_rule.company != self.cart.company:
            return _rejected(COUPON_NOT_APPLICABLE, "This coupon cannot be applied.")

        if pricing_rule.currency and pricing_rule.currency != self.cart.currency:
            return _rejected(COUPON_NOT_APPLICABLE, "This coupon cannot be applied.")

        if pricing_rule.min_amt and self.cart.net_total < pricing_rule.min_amt:
            return error_response(
                COUPON_MINIMUM_NOT_MET,
                f"Minimum order amount should be {pricing_rule.min_amt}",
                field=COUPON_FIELD,
                details={"minimum_amount": pricing_rule.min_amt},
                status_code=HTTP_UNPROCESSABLE,
            )

        if pricing_rule.max_amt and self.cart.net_total > pricing_rule.max_amt:
            return error_response(
                COUPON_MAXIMUM_EXCEEDED,
                f"Maximum order amount is {pricing_rule.max_amt}",
                field=COUPON_FIELD,
                details={"maximum_amount": pricing_rule.max_amt},
                status_code=HTTP_UNPROCESSABLE,
            )

        return success_response(
            {
                "coupon": coupon,
                "pricing_rule": pricing_rule
            }
        )
