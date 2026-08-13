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


# Error codes are NOT defined here. The published storefront codes live in
# `yob_storefront/api/response.py` as lowercase snake_case wire values, and the
# platform codes in `yob_core.api.errors`. A legacy uppercase `ErrorCode` class
# used to sit here with zero importers; it was removed under CHG-001 (F-09)
# because a second, divergent code list is exactly how a published contract
# drifts. Do not reintroduce one.

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