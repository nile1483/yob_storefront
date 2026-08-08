# Copyright (c) 2026, YOB and Shayona
# path: apps/yob_storefront/yob_storefront/api/response.py
"""Storefront view of the shared YOB public API response envelope.

The generic helpers, HTTP constants and platform-wide error codes live in
``yob_core.api`` and are re-exported here so every storefront module keeps
importing ``yob_storefront.api.response``. Sharing is safe: ``yob_storefront``
declares ``yob_core`` in ``required_apps`` (see hooks.py).

``APPLICATION_ACCESS_DENIED`` is the one authentication-domain code storefront
genuinely needs -- ``catalog.py`` returns it when a caller lacks storefront
access -- so it is re-exported from ``yob_auth``, which owns it.

This module adds only the storefront-specific stable error codes.
"""

from yob_auth.api.response import (  # noqa: F401  (re-exported API)
    APPLICATION_ACCESS_DENIED,
)
from yob_core.api.errors import (  # noqa: F401  (re-exported API)
    INTERNAL_SERVER_ERROR,
    VALIDATION_FAILED,
)
from yob_core.api.http import (  # noqa: F401  (re-exported API)
    HTTP_BAD_REQUEST,
    HTTP_CONFLICT,
    HTTP_CREATED,
    HTTP_FORBIDDEN,
    HTTP_INTERNAL_SERVER_ERROR,
    HTTP_NOT_FOUND,
    HTTP_OK,
    HTTP_TOO_MANY_REQUESTS,
    HTTP_UNAUTHORIZED,
    HTTP_UNPROCESSABLE,
)
from yob_core.api.response import (  # noqa: F401  (re-exported API)
    build_error,
    error_response,
    errors_response,
    is_error,
    server_error,
    set_status,
    success_response,
)

# ---------------------------------------------------------
# STOREFRONT STABLE ERROR CODES
# ---------------------------------------------------------

# Catalog
CATEGORY_NOT_FOUND = "category_not_found"
ITEM_NOT_FOUND = "item_not_found"

# Cart
CART_NOT_FOUND = "cart_not_found"
CART_EMPTY = "cart_empty"
QUANTITY_INVALID = "quantity_invalid"

# Contacts & addresses
CONTACT_NOT_FOUND = "contact_not_found"
CONTACT_INVALID = "contact_invalid"
CONTACT_REQUIRED = "contact_required"
ADDRESS_NOT_FOUND = "address_not_found"
BILLING_ADDRESS_INVALID = "billing_address_invalid"
BILLING_ADDRESS_REQUIRED = "billing_address_required"
SHIPPING_ADDRESS_INVALID = "shipping_address_invalid"
SHIPPING_ADDRESS_REQUIRED = "shipping_address_required"
SHIPPING_NOT_APPLICABLE = "shipping_not_applicable"

# Coupons
COUPON_CODE_REQUIRED = "coupon_code_required"
COUPON_INVALID = "coupon_invalid"
COUPON_NOT_ACTIVE = "coupon_not_active"
COUPON_EXPIRED = "coupon_expired"
COUPON_USAGE_LIMIT_REACHED = "coupon_usage_limit_reached"
COUPON_NOT_APPLICABLE = "coupon_not_applicable"
COUPON_MINIMUM_NOT_MET = "coupon_minimum_not_met"
COUPON_MAXIMUM_EXCEEDED = "coupon_maximum_exceeded"
COUPON_NOT_APPLIED = "coupon_not_applied"

# Orders
ORDER_NOT_FOUND = "order_not_found"

# Checkout & payment
CHECKOUT_TOKEN_INVALID = "checkout_token_invalid"
CHECKOUT_TOKEN_EXPIRED = "checkout_token_expired"
CUSTOMER_NOT_FOUND = "customer_not_found"
PAYMENT_METHOD_UNSUPPORTED = "payment_method_unsupported"
PAYMENT_PROVIDER_NOT_CONFIGURED = "payment_provider_not_configured"
PAYMENT_ALREADY_PROCESSED = "payment_already_processed"
PAYMENT_SIGNATURE_INVALID = "payment_signature_invalid"
PAYMENT_VERIFICATION_FAILED = "payment_verification_failed"
PAYMENT_NOT_CAPTURED = "payment_not_captured"
PAYMENT_AMOUNT_MISMATCH = "payment_amount_mismatch"
PAYMENT_CURRENCY_MISMATCH = "payment_currency_mismatch"
PAYMENT_REFERENCE_INVALID = "payment_reference_invalid"
