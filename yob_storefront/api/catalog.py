# Copyright (c) 2026, YOB and Shayona
# path: apps/yob_storefront/yob_storefront/api/catalog.py
"""
CATALOG API
Private B2B Only
Requires Login + Customer
ERPNext v16 Compatible
"""

import frappe
from yob_core.api.boundary import yob_api
from frappe.utils import get_url
from yob_storefront.api.response import (
    APPLICATION_ACCESS_DENIED,
    CATEGORY_NOT_FOUND,
    CATEGORY_NOT_LISTABLE,
    HTTP_FORBIDDEN,
    HTTP_NOT_FOUND,
    HTTP_UNPROCESSABLE,
    ITEM_NOT_FOUND,
    PAGE_SIZE_INVALID,
    UNSUPPORTED_FILTERS,
    UNSUPPORTED_SCOPE,
    UNSUPPORTED_SORT,
    VALIDATION_FAILED,
    VARIANT_ATTRIBUTES_REQUIRED,
    VARIANT_FAMILY_UNSUPPORTED,
    VARIANT_NOT_AVAILABLE,
    error_response,
    server_error,
    success_response,
)
from yob_auth.security.decorators import require_application
from yob_storefront.utils.context import STOREFRONT_APP, get_storefront_customer
from yob_storefront.services.pricing_service import (
    get_item_pricing,
    get_applicable_pricing_rules
)
 

# =========================================================
# 1️⃣ GET CATEGORIES (LOGIN REQUIRED)
# =========================================================

@frappe.whitelist(methods=["GET"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def get_categories(parent_slug=None, auth_context=None):

    try:
        # 🔐 Enforce Login + Customer
        get_storefront_customer(auth_context)

        filters = {"is_active": 1}

        if parent_slug:
            parent = frappe.get_value(
                "Category",
                {"slug": parent_slug},
                "name"
            )

            if not parent:
                return success_response([], notice="No categories found", meta={"count": 0})

            filters["parent_category"] = parent
        else:
            filters["parent_category"] = None

        categories = frappe.get_all(
            "Category",
            filters=filters,
            fields=[
                "name",
                "category_name",
                "slug",
                "thumbnail",
                "banner",
                "display_order",
                "meta_title",
                "meta_description",
                "parent_category"
            ],
            order_by="display_order asc"
        )

        for c in categories:
            if c.get("thumbnail"):
                # c["thumbnail"] = get_url(c["thumbnail"])
                c["thumbnail"] = c["thumbnail"]
            if c.get("banner"):
                # c["banner"] = get_url(c["banner"])
                c["banner"] = c["banner"]

        return success_response(
            categories,
            notice="Categories loaded",
            meta={"count": len(categories)},
        )

    except frappe.PermissionError as exc:
        return error_response(
            APPLICATION_ACCESS_DENIED,
            str(exc) or "You are not authorized to access the storefront.",
            status_code=HTTP_FORBIDDEN,
        )

    except Exception:
        return server_error("Get Categories Error", "Failed to load categories")


# =========================================================
# 2️⃣ GET CATEGORY WITH ITEMS (LOGIN REQUIRED)
# =========================================================

@frappe.whitelist(methods=["GET"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def get_category(slug=None, auth_context=None):
    """Category metadata and its child categories. **No products.**

    ## The embedded product payload was retired in Phase 22B-3

    This endpoint used to load every Item in a leaf category and price each one
    through a throwaway Sales Order. Phase 22A measured that: one pricing call and
    one temporary Sales Order per Item at ~51 ms each, growing linearly (100 items
    took 5.1 s), and a single end-of-life Item returned a 500 for the whole
    category. It was unbounded by construction -- no limit, no cursor, no way for a
    caller to ask for less.

    All product listing now belongs to `get_items`, which is bounded, sorted,
    cursor-paginated and isolates per-item failures. Nothing here queries Item,
    prices anything, or paginates -- and `tests/test_catalog_category.py` asserts
    zero pricing calls so the old behaviour cannot creep back.

    `items` and `meta.item_count` are gone from the response, and the `qty`
    parameter with them: it existed only to price the embedded products. Frappe
    filters keyword arguments to a function's signature, so a stale client still
    sending `qty` is unaffected.
    """

    if not slug:
        return error_response(
            VALIDATION_FAILED,
            "Category slug is required.",
            field="slug",
            status_code=HTTP_UNPROCESSABLE,
        )

    try:
        # 🔐 Enforce Login + Customer. The identity is not used for pricing any
        # more, but the authorization boundary is unchanged on purpose: category
        # metadata stays behind the same storefront application check it always had.
        get_storefront_customer(auth_context)

        category = frappe.get_value(
            "Category",
            {"slug": slug, "is_active": 1},
            [
                "name",
                "category_name",
                "slug",
                "thumbnail",
                "banner",
                "meta_title",
                "meta_description",
                "description",
                "is_group",
                "parent_category"
            ],
            as_dict=True
        )

        if not category:
            return error_response(
                CATEGORY_NOT_FOUND,
                "Category not found.",
                field="slug",
                status_code=HTTP_NOT_FOUND,
            )

        if category.get("thumbnail"):
            # category["thumbnail"] = get_url(category["thumbnail"])
            category["thumbnail"] = category["thumbnail"]

        if category.get("banner"):
            # category["banner"] = get_url(category["banner"])
            category["banner"] = category["banner"]

        subcategories = []

        # ---------------- GROUP CATEGORY ----------------
        if category["is_group"]:

            subcategories = frappe.get_all(
                "Category",
                filters={
                    "parent_category": category["name"],
                    "is_active": 1
                },
                fields=[
                    "name",
                    "category_name",
                    "slug",
                    "thumbnail",
                    "display_order",
                    "is_group"
                ],
                order_by="display_order asc"
            )

            for child in subcategories:
                if child.get("thumbnail"):
                    # child["thumbnail"] = get_url(child["thumbnail"])
                    child["thumbnail"] = child["thumbnail"]

        # ---------------- LEAF CATEGORY ----------------
        # Nothing to do. A leaf category carries no product payload: products are
        # served exclusively by `get_items`, which is bounded, sorted and
        # cursor-paginated. See the retirement note above.

        return success_response(
            {
                "category": category,
                "subcategories": subcategories,
            },
            notice="Category loaded",
            meta={
                "subcategory_count": len(subcategories),
            },
        )

    except frappe.PermissionError as exc:
        return error_response(
            APPLICATION_ACCESS_DENIED,
            str(exc) or "You are not authorized to access the storefront.",
            status_code=HTTP_FORBIDDEN,
        )

    except Exception:
        return server_error("Get Category Error", "Failed to load category")


# =========================================================
# 3️⃣ GET SINGLE ITEM (LOGIN REQUIRED)
# =========================================================

@frappe.whitelist(methods=["GET"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def get_item(slug=None, qty=1, auth_context=None):
    """One PUBLIC product page: a simple Item, or a variant FAMILY.

    A slug addresses a product a buyer can navigate to. Variants are not
    navigable: they are reached by choosing attributes on their family's page and
    resolving through `resolve_variant`, which is why they carry no public slug
    (Phase 24B, Decision 3).

    A family page carries NO price. ERPNext refuses an Item Price on a template
    and there is no honest family rate to quote before a selection is made -- so
    the response carries the matrix and the client asks for a price once the
    buyer has chosen.
    """

    if not slug:
        return error_response(
            VALIDATION_FAILED,
            "Item slug is required.",
            field="slug",
            status_code=HTTP_UNPROCESSABLE,
        )

    customer = get_storefront_customer(auth_context)

    item = frappe.get_value(
        "Item",
        {"custom_slug": slug, "disabled": 0},
        ["name", "item_name", "item_group", "image", "has_variants", "variant_based_on"],
        as_dict=True,
    )

    if not item:
        return error_response(
            ITEM_NOT_FOUND,
            "Item not found.",
            field="slug",
            status_code=HTTP_NOT_FOUND,
        )

    if item["has_variants"]:
        return _family_response(item, slug)

    return success_response(build_item_detail(customer, item["name"], qty, slug=slug),
                            notice="Item loaded")


def _family_response(item, slug):
    """A variant family: identity, selectable attributes, real combinations."""

    from yob_storefront.services.variant_service import ATTRIBUTE_BASED, variant_matrix

    if item.get("variant_based_on") != ATTRIBUTE_BASED:
        # Manufacturer-based families have no attribute selector to render. Fail
        # closed rather than invent one; see services/variant_service.py.
        return error_response(
            VARIANT_FAMILY_UNSUPPORTED,
            "This product cannot be configured online.",
            field="slug",
            status_code=HTTP_UNPROCESSABLE,
        )

    matrix = variant_matrix(item["name"])

    return success_response({
        "name": item["name"],
        "item_name": item["item_name"],
        "item_group": item["item_group"],
        "image": item["image"] or None,
        "custom_slug": slug,

        # The family itself is never priced and never added to a cart. Both flags
        # are explicit so a client cannot infer "product" from the absence of a
        # price and try to buy it.
        "is_template": 1,
        "is_purchasable": 0,

        "variant_of": None,
        "attributes": matrix["attributes"],
        "variants": matrix["variants"],
    }, notice="Product options loaded")


def build_item_detail(customer, item_code, qty=1, slug=None, selected=None):
    """The priced product payload, for a simple Item or a resolved variant.

    ONE serializer for both, so a variant page and a simple product page cannot
    drift apart, and `resolve_variant` needs no shape of its own. Everything
    monetary comes from the ordinary Phase 23 preview -- a temporary Sales Order
    built from the trusted SellingContext -- and nothing here decides a UOM, a
    warehouse or a rate.
    """

    from yob_storefront.services.variant_service import attributes_of, family_of

    item = frappe.get_cached_value(
        "Item", item_code, ["name", "item_name", "item_group", "image", "custom_slug"],
        as_dict=True)

    settings = frappe.get_single("YOB Store Settings")

    pricing = get_item_pricing(
        customer=customer,
        item_code=item_code,
        qty=qty,
        company=settings.company,
        currency=settings.default_currency,
    )

    stock_info = resolve_stock_availability(customer, item_code)

    rules = get_applicable_pricing_rules(
        # The helper does frappe.db.get_value("Customer", customer, ...), so it
        # needs the Customer NAME. Passing the document made Frappe treat it as a
        # filters object -> "Unsupported filters type: Customer".
        customer=customer.name,
        item_code=item_code,
        item_group=item["item_group"],
    )

    family = family_of(item_code)

    return {
        "name": item["name"],
        "item_name": item["item_name"],
        "image": item["image"] or None,
        "item_group": item["item_group"],
        "custom_slug": slug or item.get("custom_slug") or None,
        "qty": float(qty),

        "is_template": 0,
        "is_purchasable": 1,

        # Variant identity. `variant_of` is the family this SKU belongs to and
        # `selected` is its own stored attribute map -- read from ERPNext, never
        # echoed back from the request.
        "variant_of": family.get("variant_of") or None,
        "selected": selected if selected is not None else (
            attributes_of(item_code) if family.get("variant_of") else None),

        "base_price": pricing["base_price"],
        "rate": pricing["rate"],
        "discount_percentage": pricing["discount_percentage"],
        "discount_amount": pricing["discount_amount"],

        "net_amount": pricing["net_amount"],
        "tax_amount": pricing["tax_amount"],
        "tax_label":  pricing["tax_label"],
        "total_amount": pricing["total_amount"],

        # The UOM the transaction actually resolved -- read off the priced
        # Sales Order row, not guessed from the Item. No selectable UOM here;
        # this is metadata so the frontend can display the right unit rather
        # than inferring it from text.
        "uom": pricing["uom"],
        "stock_uom": stock_info["stock_uom"],

        # So a client can render "2 Strips (20 Nos)" without doing UOM
        # arithmetic of its own. Both come off the priced Sales Order row.
        "conversion_factor": pricing["conversion_factor"],
        "stock_qty": pricing["stock_qty"],

        # Availability for the ACTUAL SKU (a variant reports its own stock,
        # never its template's) in the warehouse this transaction would use.
        # `None` for a non-stock item, and `None` -- never 0 -- when no
        # warehouse resolves: absent stock and zero stock are different facts,
        # and returning 0 would read as "out of stock".
        "is_stock_item": stock_info["is_stock_item"],
        "warehouse": stock_info["warehouse"],
        "actual_qty": stock_info["actual_qty"],

        "pricing": pricing,

        "pricing_rule_label": pricing["pricing_rule_label"],
        "pricing_rule_apply_on": pricing["pricing_rule_apply_on"],

        "available_rules": rules["offers"],
    }


# =========================================================
# VARIANT RESOLUTION  (Phase 24B)
# =========================================================

@frappe.whitelist(methods=["GET"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def resolve_variant(template=None, attributes=None, qty=1, auth_context=None):
    """A completed attribute selection -> the actual variant, fully priced.

    THE SERVER RESOLVES THE SKU. A browser must never build one: ERPNext's
    `make_variant_item_code` is its own naming algorithm, and a second
    implementation of it would be a second source of identity. `attributes` is a
    selection, never authority -- the response is built from what ERPNext stored
    against the resolved variant, not from what was sent.

    Answers the same payload as a simple product page, so a client renders one
    shape either way.
    """

    if not template:
        return error_response(
            VALIDATION_FAILED,
            "A product is required.",
            field="template",
            status_code=HTTP_UNPROCESSABLE,
        )

    selection = attributes

    if isinstance(selection, str):
        try:
            selection = frappe.parse_json(selection)
        except (ValueError, TypeError):
            selection = None

    if not isinstance(selection, dict) or not selection:
        return error_response(
            VARIANT_ATTRIBUTES_REQUIRED,
            "Please choose all options.",
            field="attributes",
            status_code=HTTP_UNPROCESSABLE,
        )

    customer = get_storefront_customer(auth_context)

    from yob_storefront.services.variant_service import is_attribute_family, resolve

    if not frappe.db.exists("Item", template) or not is_attribute_family(template):
        # Covers a bad code, a simple Item, and a Manufacturer-based family --
        # none of which has an attribute selection to resolve.
        return error_response(
            VARIANT_NOT_AVAILABLE,
            "This combination is not available.",
            field="attributes",
            status_code=HTTP_UNPROCESSABLE,
        )

    item_code, reason = resolve(template, selection)

    if reason == "incomplete":
        return error_response(
            VARIANT_ATTRIBUTES_REQUIRED,
            "Please choose all options.",
            field="attributes",
            status_code=HTTP_UNPROCESSABLE,
        )

    if not item_code:
        return error_response(
            VARIANT_NOT_AVAILABLE,
            "This combination is not available.",
            field="attributes",
            status_code=HTTP_UNPROCESSABLE,
        )

    return success_response(build_item_detail(customer, item_code, qty),
                            notice="Item loaded")


# =========================================================
# HEADER PRODUCT SUGGESTIONS  (Phase 26A)
# =========================================================

@frappe.whitelist(methods=["GET"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def get_product_suggestions(search=None, auth_context=None):
    """A few public products for the header typeahead. Navigation only.

    Answers at most eight products matching every word typed, for a dropdown that
    opens a product page. It is NOT a listing: no pagination, no cursor, no
    facets, no category scope, and **no money** -- no rate, discount, tax, UOM,
    stock or warehouse. The product page stays authoritative once clicked.

    * `search` -- trimmed; **fewer than 3 characters answers an empty list and
      queries nothing at all**, which is the same floor the SPA applies. A short
      string is not an error: a buyer mid-word has done nothing wrong.

    Eligibility is the catalogue's own, not a cheaper lookalike: the same Stage-1
    candidate SQL (public slug, family collapse, manufacturer fail-closed,
    end-of-life, the same AND-across-words `item_name` match) and the same Stage-2
    base-price rule. What it skips is Stage 3, the throwaway Sales Order, because
    nothing monetary is returned -- a saving in work, never a weakening of the
    rule.

    A generated variant never appears on its own; a family appears once, as the
    family. Scope is global by design: the same products answer from the cart,
    the account page or anywhere else.

    The Customer comes from `auth_context` only, so a buyer can never see a
    product their own catalogue would not list.
    """

    from yob_storefront.services.catalog_listing_service import (
        ListingError,
        PricingContext,
        normalize_search,
    )

    try:
        from yob_storefront.services.product_suggestion_service import (
            MIN_SEARCH_LENGTH,
            suggest_products,
        )

        # Normalised BEFORE the length test so "  ab  " is two characters, not
        # six, and before any customer or catalogue work is done at all.
        text = " ".join(str(search or "").split())

        if len(text) < MIN_SEARCH_LENGTH:
            return success_response({"items": []}, notice="Suggestions loaded")

        try:
            terms = normalize_search(text)
        except ListingError as exc:
            return error_response(exc.code, exc.message, field=exc.field,
                                  status_code=HTTP_UNPROCESSABLE)

        customer = get_storefront_customer(auth_context)
        ctx = PricingContext(customer)

        return success_response({"items": suggest_products(ctx, terms)},
                                notice="Suggestions loaded")

    except Exception:
        return server_error("Get Product Suggestions Error",
                            "Failed to load suggestions")


# =========================================================
# CATEGORY FILTER DEFINITIONS  (Phase 25C)
# =========================================================

@frappe.whitelist(methods=["GET"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def get_category_filters(scope_value=None, auth_context=None):
    """Which merchandising filters a category page should display.

    Decided by `Category.storefront_filter_set` and NOTHING else: no walk up the
    category tree, no fallback to an Item's own Filter Set (that is an admin
    scope), no fallback to every global Filter. A category with no Filter Set
    answers an empty list, which is a merchant's explicit choice.

    Values are the ones actually assigned to a listing entity in that category, so
    a page never offers a facet that would return nothing. Determined from stored
    assignments by one indexed query -- **no pricing**, and therefore no counts:
    `Red (17)` would need the full eligibility pipeline per value.
    """

    if not scope_value:
        return error_response(
            VALIDATION_FAILED, "Category is required.", field="scope_value",
            status_code=HTTP_UNPROCESSABLE)

    try:
        get_storefront_customer(auth_context)

        category = frappe.get_value(
            "Category", {"slug": scope_value, "is_active": 1}, ["name", "is_group"],
            as_dict=True)

        if not category:
            return error_response(
                CATEGORY_NOT_FOUND, "Category not found.", field="scope_value",
                status_code=HTTP_NOT_FOUND)

        if category.is_group:
            return error_response(
                CATEGORY_NOT_LISTABLE,
                "This category holds sub-categories rather than products.",
                field="scope_value", status_code=HTTP_UNPROCESSABLE)

        from yob_storefront.services.storefront_filter_service import category_filters

        return success_response({"filters": category_filters(category.name)},
                                notice="Filters loaded")

    except Exception:
        return server_error("Get Category Filters Error", "Failed to load filters")


# =========================================================
# BOUNDED CATALOG LISTING  (Phase 22B-1)
# =========================================================

@frappe.whitelist(methods=["GET"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def get_items(
    scope_type="category",
    scope_value=None,
    search=None,
    filters=None,
    sort=None,
    page_size=None,
    cursor=None,
    qty=1,
    storefront_filters=None,
    auth_context=None,
):
    """Bounded, cursor-paginated catalog listing.

    Replaces the unbounded item payload of `get_category()`. That path loads every
    Item in a category and runs one temporary Sales Order per Item -- Phase 22A
    measured 100 items at 5.1 s, growing linearly, with one bad Item aborting the
    whole response. This endpoint keeps the expensive pricing proportional to the
    PAGE and isolates per-item failures.

    Parameters are flat, matching the rest of this API rather than introducing a
    nested query object.

    * `scope_type`  -- only `category` is implemented. `collection` and `all` are
      reserved and answer `unsupported_scope` so they cannot be reached by guessing.
    * `scope_value` -- the category slug, validated exactly as `get_category` does.
      Group categories are refused rather than silently recursing into children.
    * `search`      -- matched against `item_name` ONLY. Multiple words are ANDed.
    * `filters`     -- must be absent or empty; a non-empty set answers
      `unsupported_filters` rather than being silently dropped.
    * `sort`        -- `name_asc` (default) | `name_desc` | `newest`.
    * `page_size`   -- 1..48, default 24.
    * `cursor`      -- opaque; from a previous response.
    * `storefront_filters` -- merchandising selection as JSON, keyed by filter and
      value KEYS from `get_category_filters`, e.g.
      `{"material":["steel","aluminium"],"finish":["black"]}`. Values within one
      filter are OR-ed, different filters are AND-ed. It is a selection, never
      query grammar: an unknown key is refused, not passed to the database. It
      applies inside a category, so it requires one.

    `filters` remains reserved and still answers `unsupported_filters`; the
    merchandising parameter is separate so the Phase 22B contract is untouched.

    The Customer comes from `auth_context` only. There is no parameter through which
    a browser can list or price as somebody else.
    """

    from yob_storefront.services.catalog_listing_service import (
        DEFAULT_PAGE_SIZE,
        DEFAULT_SORT,
        MAX_PAGE_SIZE,
        MIN_PAGE_SIZE,
        SORT_MODES,
        SUPPORTED_SCOPE_TYPES,
        ListingError,
        PricingContext,
        decode_cursor,
        list_items,
        normalize_search,
    )

    try:
        # ---------------- scope ----------------
        if scope_type not in SUPPORTED_SCOPE_TYPES:
            return error_response(
                UNSUPPORTED_SCOPE,
                "That listing scope is not supported.",
                field="scope_type",
                status_code=HTTP_UNPROCESSABLE,
            )

        if not scope_value:
            # Absent scope is NOT "everything" -- that would be the `all` scope,
            # which is deliberately not implemented.
            return error_response(
                VALIDATION_FAILED,
                "Category is required.",
                field="scope_value",
                status_code=HTTP_UNPROCESSABLE,
            )

        # ---------------- filters ----------------
        parsed_filters = filters
        if isinstance(parsed_filters, str):
            try:
                parsed_filters = frappe.parse_json(parsed_filters)
            # Narrow on purpose: a bare `except Exception` here would report a
            # genuine backend fault as "filters not supported". These two are the
            # real failure modes of parsing caller-supplied JSON.
            except (ValueError, TypeError):
                return error_response(
                    UNSUPPORTED_FILTERS,
                    "Filters are not supported yet.",
                    field="filters",
                    status_code=HTTP_UNPROCESSABLE,
                )

        if parsed_filters:
            return error_response(
                UNSUPPORTED_FILTERS,
                "Filters are not supported yet.",
                field="filters",
                status_code=HTTP_UNPROCESSABLE,
            )

        # ---------------- sort ----------------
        sort = sort or DEFAULT_SORT
        if sort not in SORT_MODES:
            return error_response(
                UNSUPPORTED_SORT,
                "That sort option is not supported.",
                field="sort",
                status_code=HTTP_UNPROCESSABLE,
            )

        # ---------------- page size ----------------
        if page_size in (None, ""):
            page_size = DEFAULT_PAGE_SIZE
        try:
            page_size = int(page_size)
        except (TypeError, ValueError):
            page_size = -1

        if page_size < MIN_PAGE_SIZE or page_size > MAX_PAGE_SIZE:
            # Explicitly refused, never clamped: silently serving 48 for a request
            # of 5000 would hide the client bug that produced it.
            return error_response(
                PAGE_SIZE_INVALID,
                f"page_size must be between {MIN_PAGE_SIZE} and {MAX_PAGE_SIZE}.",
                field="page_size",
                status_code=HTTP_UNPROCESSABLE,
            )

        # ---------------- category ----------------
        customer = get_storefront_customer(auth_context)

        category = frappe.get_value(
            "Category",
            {"slug": scope_value, "is_active": 1},
            ["name", "is_group"],
            as_dict=True,
        )
        if not category:
            return error_response(
                CATEGORY_NOT_FOUND,
                "Category not found.",
                field="scope_value",
                status_code=HTTP_NOT_FOUND,
            )

        if category.is_group:
            # A category scope is exactly one category. Recursing into descendants
            # would make the page size and the cursor meaningless.
            return error_response(
                CATEGORY_NOT_LISTABLE,
                "This category holds sub-categories rather than products.",
                field="scope_value",
                status_code=HTTP_UNPROCESSABLE,
            )

        # ---------------- merchandising selection ----------------
        # Parsed and validated BEFORE the cursor is decoded, because the selection
        # is part of the cursor's binding: a cursor issued for one selection must
        # not resume inside a page produced by another.
        from yob_storefront.services.storefront_filter_service import (
            FilterSelectionError,
            fingerprint_payload,
            parse_selection,
        )

        try:
            selection = parse_selection(storefront_filters, category.name)
        except FilterSelectionError as exc:
            return error_response(exc.code, exc.message, field=exc.field,
                                  status_code=HTTP_UNPROCESSABLE)

        binding = fingerprint_payload(selection)

        # ---------------- run ----------------
        terms = normalize_search(search)
        ctx = PricingContext(customer, qty=qty)
        after_keys = decode_cursor(cursor, ctx, scope_type, scope_value, terms, sort,
                                   binding)

        items, has_more, next_cursor, _batches = list_items(
            ctx, category.name, terms, sort, page_size, after_keys, scope_type,
            scope_value, selection
        )

        return success_response(
            {
                "items": items,
                "pagination": {
                    "returned_count": len(items),
                    "page_size": page_size,
                    "has_more": has_more,
                    "next_cursor": next_cursor,
                },
                "query": {
                    "scope_type": scope_type,
                    "scope_value": scope_value,
                    "search": " ".join(terms),
                    "sort": sort,
                },
            },
            notice="Items loaded",
        )

    except ListingError as exc:
        # Expected, client-fixable: a bad cursor or an over-long search.
        return error_response(
            exc.code, exc.message, field=exc.field, status_code=HTTP_UNPROCESSABLE
        )

    except Exception:
        return server_error("Get Items Error", "Failed to load items")


# =========================================================
# PRODUCT AVAILABILITY  (Phase 23B-1)
# =========================================================

def resolve_stock_availability(customer_doc, item_code):
    """Stock for one SKU in the warehouse this customer's order would use.

    Three rules, each deliberate:

    * **the actual SKU** -- a variant reports its own stock. Its template is not a
      transactable item and its balance would be meaningless here.
    * **one warehouse**, the one ERPNext resolves for the Sales Order line. Summing
      every warehouse would promise stock the order cannot draw on. The quantity
      itself is read with ERPNext's own `get_bin_details`, using the arguments its
      Sales Order line uses, so a GROUP warehouse aggregates its children exactly
      as the order does instead of reporting 0.
    * **`None`, never `0`**, for a non-stock item or an unresolved warehouse.
      Zero means "we have none"; absent means "quantity does not apply". Collapsing
      them would show every service item as out of stock.

    Never raises: availability is decoration on a catalogue read, and a stock
    lookup must not be able to fail the product page.
    """

    info = frappe.db.get_value(
        "Item", item_code, ["is_stock_item", "stock_uom"], as_dict=True) or {}

    result = {
        "is_stock_item": frappe.utils.cint(info.get("is_stock_item")),
        "stock_uom": info.get("stock_uom"),
        "warehouse": None,
        "actual_qty": None,
    }

    if not result["is_stock_item"]:
        return result

    from yob_storefront.services.pricing_context import context_for

    warehouse = context_for(customer_doc).resolved_warehouse(item_code)
    if not warehouse:
        return result

    from erpnext.stock.get_item_details import get_bin_details

    result["warehouse"] = warehouse

    # ERPNext's OWN bin reader, called the way its Sales Order line calls it
    # (`update_bin_details` -> `get_bin_details(item, warehouse,
    # include_child_warehouses=True)`). `company` is omitted deliberately: it only
    # adds a company-wide total the storefront must never display, because this
    # order can draw on one warehouse.
    #
    # A raw `Bin` read was wrong whenever ERPNext resolved a GROUP warehouse: the
    # order line reported the aggregate of that group's children while the product
    # page showed 0 for the very same warehouse. Phase 23B-5W reproduced it --
    # ERPNext row 9, storefront 0.
    #
    # For a leaf warehouse `get_child_warehouses()` returns just that warehouse, so
    # the ordinary case is unchanged. Nothing about warehouse PRECEDENCE is decided
    # here; only which quantity belongs to the warehouse ERPNext already chose.
    result["actual_qty"] = frappe.utils.flt(
        get_bin_details(item_code, warehouse, include_child_warehouses=True).get("actual_qty")
    )
    return result
