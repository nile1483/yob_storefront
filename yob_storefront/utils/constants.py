# =========================================================
# CART STATUS
# =========================================================

class CartStatus:
    DRAFT = "Draft"
    ORDERED = "Ordered"
    EXPIRED = "Expired"
    CANCELLED = "Cancelled"


# =========================================================
# PAYMENT REQUEST STATUS
# =========================================================

class PaymentRequestStatus:
    DRAFT = "Draft"
    INITIATED = "Initiated"
    PAID = "Paid"
    FAILED = "Failed"
    EXPIRED = "Expired"
    CANCELLED = "Cancelled"


# =========================================================
# PAYMENT METHODS
# =========================================================

class PaymentMethodCode:
    RAZORPAY = "razorpay"
    PAY_LATER = "paylater"


# =========================================================
# ERROR CODES
# =========================================================

class ErrorCode:
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    UNAUTHORIZED = "UNAUTHORIZED"
    SERVER_ERROR = "SERVER_ERROR"

    CART_NOT_FOUND = "CART_NOT_FOUND"
    CART_EMPTY = "CART_EMPTY"

    COUPON_INVALID = "COUPON_INVALID"
    COUPON_EXPIRED = "COUPON_EXPIRED"

    PAYMENT_FAILED = "PAYMENT_FAILED"
    PAYMENT_EXPIRED = "PAYMENT_EXPIRED"
    PAYMENT_INVALID = "PAYMENT_INVALID"


# =========================================================
# CACHE KEYS
# =========================================================

class CacheKey:
    STORE_CONFIG = "yob:store_config"
    CATEGORY_TREE = "yob:category_tree"
    
    @staticmethod
    def customer_address(customer):
        return f"yob:customer:{customer}:addresses"

    @staticmethod
    def customer_contact(customer):
        return f"yob:customer:{customer}:contacts"


# =========================================================
# TOKEN EXPIRY
# =========================================================

CHECKOUT_TOKEN_EXPIRY_HOURS = 1
STORE_CONFIG_CACHE_SECONDS = 3600


# =========================================================
# REFERENCE DOCTYPES
# =========================================================

class ReferenceDoctype:
    CART = "Cart"
    SALES_ORDER = "Sales Order"


# =========================================================
# PAYMENT GATEWAYS
# =========================================================

class PaymentGateway:
    RAZORPAY = "Razorpay"