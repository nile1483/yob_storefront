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
# An Item Template is a family, not a product: ERPNext refuses to price it and it
# can never be bought. The buyer picks attributes on the family page and the
# server resolves an actual variant SKU. 422 -- a fixable request, never a fault.
ITEM_IS_TEMPLATE = "item_is_template"
# The exact code exists but cannot be sold right now: disabled, not a sales item,
# past end of life, or an orphaned variant.
ITEM_NOT_PURCHASABLE = "item_not_purchasable"
# The chosen attribute combination has no variant. Never invented, never
# silently resolved to a neighbour.
VARIANT_NOT_AVAILABLE = "variant_not_available"
# Fewer attributes than the family defines. A partial selection is a different
# problem from an impossible one, and the buyer fixes it differently.
VARIANT_ATTRIBUTES_REQUIRED = "variant_attributes_required"
# `variant_based_on = "Manufacturer"`: a real ERPNext mode with no attribute
# selector to render. YOB fails closed rather than inventing semantics for it.
VARIANT_FAMILY_UNSUPPORTED = "variant_family_unsupported"

# Catalog listing (get_items). Every one is a client-fixable request problem, so
# each is 422 with the offending field named -- none of them is a server fault.
# `scope_type` values other than `category` are reserved, not broken: they answer
# `unsupported_scope` so a client cannot reach an unfinished feature by guessing.
UNSUPPORTED_SCOPE = "unsupported_scope"
UNSUPPORTED_FILTERS = "unsupported_filters"
UNSUPPORTED_SORT = "unsupported_sort"
PAGE_SIZE_INVALID = "page_size_invalid"
CURSOR_INVALID = "cursor_invalid"
SEARCH_TOO_LONG = "search_too_long"
CATEGORY_NOT_LISTABLE = "category_not_listable"

# Storefront navigation and content (Phase 25C)
MENU_NOT_FOUND = "menu_not_found"
PAGE_NOT_FOUND = "page_not_found"

# System route content placements (Phase 25G). A route is application structure,
# not merchant data, so an unknown one is a CLIENT bug rather than a missing
# record -- it is refused as validation and never mapped to a neighbouring route.
CONTENT_ROUTE_UNKNOWN = "content_route_unknown"

# Merchandising filter selection. Four codes rather than one because a buyer's
# client fixes each differently: a malformed payload is a bug, an unknown filter
# means the page is stale, a bad value means the chip is stale, and a missing
# category means the request had no browsing context to filter within.
STOREFRONT_FILTER_INVALID = "storefront_filter_invalid"
STOREFRONT_FILTER_UNKNOWN = "storefront_filter_unknown"
STOREFRONT_FILTER_VALUE_UNKNOWN = "storefront_filter_value_unknown"
STOREFRONT_FILTER_CONTEXT_REQUIRED = "storefront_filter_context_required"

# Cart
CART_NOT_FOUND = "cart_not_found"
CART_EMPTY = "cart_empty"
QUANTITY_INVALID = "quantity_invalid"
# The merchant changed the item's authoritative selling UOM after this Cart line
# was priced, so the quantity the buyer just entered (counted in today's unit)
# and the quantity already on the line (counted in the line's own) do not mean
# the same thing. YOB never converts between them and never reinterprets stored
# intent, so the add is refused until the line is removed and re-added. 409: the
# request is valid, the stored state conflicts with it.
CART_ITEM_UOM_CHANGED = "cart_item_uom_changed"

# Contacts & addresses
CONTACT_NOT_FOUND = "contact_not_found"
CONTACT_INVALID = "contact_invalid"
CONTACT_REQUIRED = "contact_required"
ADDRESS_NOT_FOUND = "address_not_found"
# Frappe refused the delete because the record is still linked -- to a Cart, a
# historical Sales Order, or the Customer's own default. That is link integrity
# working, not a fault: deleting would strand the referring document. The
# storefront answers 409 with a code the client can act on, and deliberately
# does NOT name the referring documents (Frappe's own message embeds a Desk
# anchor, which never reaches a storefront caller).
ADDRESS_IN_USE = "address_in_use"
CONTACT_IN_USE = "contact_in_use"
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
# The Cart no longer matches the obligation the Payment Request was issued for.
# A Payment Request is immutable, so the answer is never a re-priced payment
# link: the buyer returns to the cart and starts checkout again.
PAYMENT_REQUEST_STALE = "payment_request_stale"
# The provider failed AFTER the local obligation was durably committed. The
# Sales Order and Payment Request still exist and the attempt is retryable --
# this code must never be read as "nothing happened".
PAYMENT_PROVIDER_ERROR = "payment_provider_error"
