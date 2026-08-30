# Error Codes

Every code below is extracted from backend source. Nothing here is invented.

**Branch on `errors[0].code`.** Never parse `detail` — it is human-readable and
may change. `field` is present when the error belongs to a specific input.

```json
{ "message": { "errors": [
  { "code": "quantity_invalid", "detail": "Quantity must be greater than zero.", "field": "qty" }
] } }
```

## Handling categories

| Category | Suggested frontend behaviour |
|---|---|
| `validation` | show inline against `field`; keep the user where they are |
| `not_found` | the referenced thing is gone; refresh the list |
| `access` | permission/business access refused; not retryable by repeating |
| `stale` | server state moved on; **refetch and restart the step** |
| `retryable_provider` | transient provider fault; offer "try again" |
| `terminal_payment` | payment is finished or closed; stop, do not retry |
| `session` | session unusable; re-authenticate |
| `internal` | generic failure; show a neutral message, log the correlation id |

---

## Auth & session

| Code | Endpoints | Meaning | Category |
|---|---|---|---|
| `invalid_credentials` | `login_with_password` | wrong username/password | validation |
| `login_method_disabled` | `login_with_password`, `request_otp` | that method is off for this application | access |
| `otp_invalid` | `login_with_otp` | wrong or expired OTP | validation |
| `application_access_denied` | any authenticated storefront endpoint | the user has no access grant for `STOREFRONT`, or the request did not traverse the edge proxy | access |
| `authentication_required` | platform-level | no usable session | session |
| `rate_limit_exceeded` | `login_with_password` | 10 attempts / 300 s per username, counted before authentication | validation |

> `application_access_denied` is deliberately **indistinguishable** between
> "unknown application" and "application disabled" — do not try to tell them
> apart.

## Catalog

| Code | Endpoints | Meaning | Category |
|---|---|---|---|
| `category_not_found` | `get_category` | unknown slug | not_found |
| `item_not_found` | `get_item` | unknown slug | not_found |

## Cart

| Code | Endpoints | Meaning | Category |
|---|---|---|---|
| `cart_not_found` | `proceed_to_payment` | no open Draft cart (**404**) | not_found |
| `cart_empty` | `proceed_to_payment` | cart has no items | validation |
| `quantity_invalid` | `add_to_cart` | `qty` must be > 0; `field: "qty"` | validation |
| `item_is_template` | `add_to_cart` | the code is a variant FAMILY, not a product (**422**); choose attributes on the family page and add the resolved variant | validation |
| `item_not_purchasable` | `add_to_cart` | the SKU exists but cannot be sold now — disabled, not a sales item, past end of life, or an orphaned variant (**422**) | validation |
| `cart_item_uom_changed` | `add_to_cart` | the merchant changed the item's selling unit after this cart line was priced, so the quantity just entered and the quantity on the line do not mean the same thing (**409**); `field: "item_code"`, `details: {item_code, existing_uom, current_uom}`. Tell the buyer the unit changed and offer to remove the line so it can be added again. The backend never converts and never merges across units. | conflict |

## Catalog — listing (`get_items`, `get_category`)

Every one is a client-fixable request problem, answered **422** with the offending
field named. None of them is a server fault.

| Code | Endpoints | Meaning | Category |
|---|---|---|---|
| `unsupported_scope` | `get_items` | `scope_type` other than `category`; `collection`/`all` are reserved, not broken; `field: "scope_type"` | validation |
| `unsupported_filters` | `get_items` | `filters` was non-empty (or unparseable JSON); filters are not implemented and are never silently dropped; `field: "filters"` | validation |
| `unsupported_sort` | `get_items` | `sort` other than `name_asc` / `name_desc` / `newest`; `field: "sort"` | validation |
| `page_size_invalid` | `get_items` | `page_size` outside 1..24 — **refused, never clamped**, so a client bug is visible; `field: "page_size"`. The ceiling was 48 before OpenAPI 3.10.0 | validation |
| `cursor_invalid` | `get_items` | the cursor is malformed, or belongs to a different scope/search/sort/customer/price list; request the first page again; `field: "cursor"` | validation |
| `search_too_long` | `get_items` | the search string exceeds the allowed length; `field: "search"` | validation |
| `category_not_listable` | `get_items` | the slug names a GROUP category, which holds sub-categories rather than products; call `get_categories` for its children; `field: "scope_value"` | validation |

## Storefront filters (Phase 25C)

| Code | Endpoints | Meaning | Category |
|---|---|---|---|
| `storefront_filter_invalid` | `get_items` | `storefront_filters` was not a JSON object of `key -> [value keys]` (**422**) — a client bug | validation |
| `storefront_filter_unknown` | `get_items` | the filter key is not exposed by this category, or is disabled (**422**) — your cached facet list is stale; re-fetch `get_category_filters` | validation |
| `storefront_filter_value_unknown` | `get_items` | the value is not one of that filter's enabled values (**422**) — same remedy | validation |
| `storefront_filter_context_required` | `get_items` | filters were sent without a category scope (**422**); merchandising facets only exist inside a category. Reachable since OpenAPI 3.10.0, when `scope_value` became optional | validation |

Selections are never interpreted as database fields: an unknown key is refused,
not queried.

## Navigation and content (Phase 25C)

| Code | Endpoints | Meaning | Category |
|---|---|---|---|
| `menu_not_found` | `cms.get_menu` | no such menu key, **or** the menu is disabled (**404**) — deliberately indistinguishable | not_found |
| `page_not_found` | `cms.get_page` | no such page slug, **or** the page is unpublished (**404**) | not_found |
| `content_route_unknown` | `cms.get_route_content` | the `route_key` is not an application route that can hold content, or is a deliberately excluded one (login, checkout, payment) (**422**); it is never mapped to a neighbouring route | validation |

## Catalog — variants

| Code | Endpoints | Meaning | Category |
|---|---|---|---|
| `variant_attributes_required` | `resolve_variant` | not every attribute of the family was chosen (**422**); `field: "attributes"` | validation |
| `variant_not_available` | `resolve_variant` | the chosen combination has no salable variant — never resolved to a neighbour (**422**) | validation |
| `variant_family_unsupported` | `get_item` | a `Manufacturer`-based family, which has no attribute selector (**422**) | validation |

## Coupons

All from `apply_coupon` / `remove_coupon`; all `validation`.

| Code | Meaning |
|---|---|
| `coupon_code_required` | no code supplied |
| `coupon_invalid` | unknown code |
| `coupon_not_active` | exists but not enabled |
| `coupon_expired` | outside its validity window |
| `coupon_usage_limit_reached` | usage cap hit |
| `coupon_not_applicable` | not valid for this cart/customer |
| `coupon_minimum_not_met` | cart below the coupon minimum |
| `coupon_maximum_exceeded` | cart above the coupon maximum |
| `coupon_not_applied` | `remove_coupon` with no coupon applied |

## Addresses & contacts

| Code | Endpoints | Meaning | Category |
|---|---|---|---|
| `contact_not_found` | contact endpoints | unknown Contact | not_found |
| `contact_invalid` | `set_cart_contact` | the Contact does not belong to this customer | access |
| `address_not_found` | address endpoints | unknown Address | not_found |
| `billing_address_invalid` | `set_cart_billing_address` | Address does not belong to this customer | access |
| `shipping_address_invalid` | `set_cart_shipping_address` | Address does not belong to this customer | access |
| `shipping_not_applicable` | `set_cart_shipping_address` | cart is not shippable — re-read `is_shippable` | validation |
| `address_in_use` | `delete_address` | **409** — still referenced by a Cart, a historical Sales Order, or the Customer default | conflict |
| `contact_in_use` | `delete_contact` | **409** — still referenced by a Cart or a historical Sales Order | conflict |
| `validation_failed` | `add_/update_address`, `add_/update_contact` | **422** — a required field was cleared, or a value was rejected by ERPNext / India Compliance. Carries `field` where the failing field can be identified | validation |
| `customer_not_found` | payment/checkout | the customer behind the link no longer exists | not_found |

> **`*_in_use` is not a failure to retry, and not the same as 404.** The record
> still exists and is still valid; only the delete was refused. Leave it in the
> list and tell the user it is in use. Nothing is detached automatically — the
> selection has to change first.
>
> The response carries **only** the code and a plain sentence. It does not name
> the referring document: Frappe's own message for this case embeds an absolute
> Desk URL and the referring docname, and that is stripped before the storefront
> ever sees it. There is nothing in the body to parse.

## Checkout preconditions

All from `proceed_to_payment`, all **422**, all `validation`. Each names the
field the user must fix.

| Code | `field` |
|---|---|
| `contact_required` | `contact_person` |
| `billing_address_required` | `billing_address` |
| `shipping_address_required` | `shipping_address` (only when the cart is shippable) |

## Payment — token & state

| Code | Status | Meaning | Category |
|---|---|---|---|
| `checkout_token_invalid` | **404** | blank, unknown, or **revoked** token | terminal_payment |
| `checkout_token_expired` | 422 | recognised but past its 1-hour expiry | terminal_payment |
| `payment_request_stale` | **409** | the cart changed since the obligation was issued — send the buyer back to the cart and restart | stale |
| `payment_reference_invalid` | 422 | the payment could not be matched to a usable source/order | not_found |
| `order_not_found` | 404 | the referenced Sales Order no longer exists | not_found |
| `payment_already_processed` | 409 | a *different* payment arrived for an already-settled obligation | terminal_payment |

> After a successful payment the token is **revoked**, so reusing that URL
> answers `checkout_token_invalid`. Treat it as *terminal*, not as an error to
> retry — show a "payment complete" state.

## Payment — method & provider

| Code | Status | Meaning | Category |
|---|---|---|---|
| `payment_method_unsupported` | 422 | unknown method, **or** a known method that is not eligible for this order right now. Also what you get for sending `method_code` instead of `name` | validation |
| `payment_provider_not_configured` | 500 | no provider credentials on the server; `details.retryable: false` | internal |
| `payment_provider_error` | 500 **or 422** | see below | see below |
| `validation_failed` | 422 | a required request field was missing; `field` names it | validation |

### `payment_provider_error` — read `details`

This one code covers two very different situations. **Branch on `details.retryable`:**

```json
{ "code": "payment_provider_error",
  "detail": "…",
  "details": { "retryable": true, "sales_order": "SAL-ORD-…" } }
```

| `details.retryable` | `details.sales_order` | Meaning | What to do |
|---|---|---|---|
| `true` | present | provider failed **after** the order was committed. **The order was NOT rolled back.** | offer "try again" — do **not** say the order failed |
| `false` | absent | the gateway could never take this payment (misconfigured, or unsupported currency — 422). Nothing was committed. | offer another payment method |
| `false` | present | provider state needs manual review (409) | tell the user to contact support |

## Payment — verification & settlement

All from `verify_payment`.

| Code | Status | Meaning | Category |
|---|---|---|---|
| `payment_signature_invalid` | 422 | Razorpay HMAC verification failed | terminal_payment |
| `payment_verification_failed` | 500 | verification could not be completed | retryable_provider |
| `payment_not_captured` | 422 | provider order/payment is not in a paid/captured state | terminal_payment |
| `payment_amount_mismatch` | 409 | provider amount ≠ the immutable obligation | terminal_payment |
| `payment_currency_mismatch` | 409 | provider currency ≠ the obligation | terminal_payment |

## Orders

| Code | Endpoints | Meaning | Category |
|---|---|---|---|
| `order_not_found` | `get_order_details` | unknown or not this customer's order | not_found |
| `validation_failed` | `get_order_details` | `order_id` was not sent (`field: "order_id"`, **422**) | validation |

`get_orders` takes no parameters and has no endpoint-specific error code.

> **`order_not_found` is deliberately ambiguous.** Another customer's order and
> a non-existent order return the identical 404, so the response cannot be used
> to probe for orders that are not yours. Do not present it as "access denied" —
> "order not found" is the correct user-facing message for both.

## Platform

| Code | Meaning | Category |
|---|---|---|
| `validation_failed` | a required field was missing or malformed | validation |
| `internal_server_error` | unexpected backend fault. Generic by design — never carries a traceback | internal |

---

## Raw Frappe shapes you must tolerate

Not every failure reaches the YOB envelope.

**Unauthenticated call to an authenticated endpoint** → **403** with Frappe's
own body and a **raw traceback in `exc`**. `@frappe.whitelist()` rejects Guest
before YOB error handling runs, so there is no `errors[]` array. Normalise this
in your HTTP layer to something like a synthetic `authentication_required`.

**Missing CSRF on a non-GET authenticated call** → Frappe `CSRFTokenError`,
again outside the YOB envelope.

**Edge proxy rejection** → **421 Misdirected Request** from nginx, never
reaching Frappe at all.

In all three cases: **never render `exc` or `_server_messages`.** Show a neutral
message and log the detail.
