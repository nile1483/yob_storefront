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
| `customer_not_found` | payment/checkout | the customer behind the link no longer exists | not_found |

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
