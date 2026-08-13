# YOB API Method Matrix

Status: `Final backend contract` — CHG-002 F-04. Date: `2026-08-09`

This is the contract the future Angular application must follow. It was
established while no live client existed, so no backward-compatibility shim is
provided and none should be added.

## Transport rules (apply to every row)

- **Base:** `POST|GET /api/method/<dotted path>`; envelope is Frappe's outer
  `message` wrapping the YOB inner body (`data`/`notice`/`meta`, or `errors[]`).
- **CSRF is Frappe's own** — no custom system. `frappe/auth.py` validates only
  `POST/PUT/DELETE/PATCH`, and only when the session already holds a token.
- **Obtaining the token:** `login_with_password` / `login_with_otp` /
  `get_session_context` all return `data.csrf_token`. Send it as
  `X-Frappe-CSRF-Token` (a `csrf_token` form field is also accepted).
- **First login needs no token** (Guest session holds none). **A repeat login on
  an existing session does** — clear cookies or send the header.
- **Session:** the `sid` cookie, scoped to the SPA domain.
- **Domain:** every authenticated call must traverse the edge proxy that sets
  `X-YOB-Original-Host`; direct origin access fails closed with
  `application_access_denied`.
- Only `GET` and `POST` are accepted anywhere. `PUT`/`DELETE`/`PATCH`/`HEAD` are
  rejected by Frappe before the endpoint runs.

## Authentication endpoints — `yob_auth`

| Endpoint | Purpose | Method | Access | CSRF | Request | Response |
| --- | --- | --- | --- | --- | --- | --- |
| `yob_auth.api.auth.login_with_password` | Password login | POST | Guest | Not on first login; **required** on a session that already has a token | `application`, `username`, `password` | `data.{authenticated, authentication_method, user, context, csrf_token}` |
| `yob_auth.api.auth.request_otp` | Issue OTP challenge | POST | Guest | as above | `application`, `identifier`, `method` | `data.{challenge_id, expires_in}` |
| `yob_auth.api.auth.login_with_otp` | OTP login | POST | Guest | as above | `challenge_id`, `otp` | same as password login |
| `yob_auth.api.auth.get_session_context` | Re-read context / **refresh CSRF token** | GET | Authenticated | No (GET) | `application` | same shape, incl. `csrf_token` |
| `yob_auth.api.auth.logout` | End session | POST | Authenticated | **Required** | — | `data.{authenticated:false}` |

> **Angular:** call `get_session_context` to refresh the token rather than
> re-logging in — a repeat login consumes the rate-limit budget (10 per 300 s
> per username, counted before authentication).

## Read-only storefront endpoints — `GET`, no CSRF

| Endpoint | Purpose | Access | Request | Response |
| --- | --- | --- | --- | --- |
| `catalog.get_categories` | Category list | Auth + Customer | `parent_slug?` | `data[]`, `meta.count` |
| `catalog.get_category` | Category + items | Auth + Customer | `slug`, `qty?` | `data.{category, subcategories, items}` |
| `catalog.get_item` | Item detail + pricing | Auth + Customer | `slug`, `qty?` | `data.{item, pricing, offers}` |
| `cart.get_cart` | Current cart | Auth + Customer | — | `data.{cart, contact, addresses, …}` |
| `address.get_contacts` | Customer contacts | Auth + Customer | — | `data[]` |
| `address.get_addresses` | Customer addresses | Auth + Customer | — | `data[]` |
| `address.get_contact_for_customer` | **Desk-internal**, not for the SPA | Desk session + DocType permission | `customer` | plain value, **not** an envelope |
| `order.get_orders` | Order history | Auth + Customer | — | `data[]`, `meta.count` |
| `order.get_order_details` | One order | Auth + Customer | `order_id` | `data.{order}` |
| `cms.get_config` | Store config | Auth | — | `data.{company, store_name, …}` |
| `payment_method.get_payment_methods` | Methods for the customer | Auth + Customer | `customer?`, `company?`, `order_amount?` | `data[]` |
| `payment.get_checkout_data` | Checkout page data | **Guest + token** | `token` | `data.{cart, payment_request, amount, currency, payment_methods}` |

> `payment_method.get_payment_methods` accepts `customer`/`company` for
> compatibility only. They are compared to the authenticated Customer and then
> discarded — never authorization.
>
> **F-12 blocked:** `cms.get_config` returns `allowed_payment_modes: []`
> permanently. Do not build against that key.

## Mutating storefront endpoints — `POST`, CSRF required

| Endpoint | Purpose | Access | Request |
| --- | --- | --- | --- |
| `cart.add_to_cart` | Add line, or **increment** an existing one | Auth + Customer | `item_code`, `qty?` (a **delta**, not a total) |
| `cart.remove_from_cart` | Remove line | Auth + Customer | `item_code` |
| `cart.clear_cart` | Empty cart | Auth + Customer | — |
| `cart.set_cart_contact` | Set contact | Auth + Customer | `contact_person` |
| `cart.set_cart_billing_address` | Set billing | Auth + Customer | `billing_address` |
| `cart.set_cart_shipping_address` | Set shipping | Auth + Customer | `shipping_address` |
| `cart.apply_coupon` | Apply coupon | Auth + Customer | `code` |
| `cart.remove_coupon` | Remove coupon | Auth + Customer | — |
| `address.add_address` | Create address | Auth + Customer | form fields incl. GST |
| `address.update_address` | Update address | Auth + Customer | `name` + fields |
| `address.delete_address` | Delete address | Auth + Customer | `name` |
| `address.update_contact` | Update contact | Auth + Customer | `name` + fields |
| `address.delete_contact` | Delete contact | Auth + Customer | `name` |
| `checkout.proceed_to_payment` | Create Payment Request + token | Auth + Customer | — |
| `payment.process_payment` | Start payment | **Guest + token** | `token`, `payment_method` |
| `payment.verify_payment` | Verify Razorpay signature | **Guest + HMAC** | `razorpay_order_id`, `razorpay_payment_id`, `razorpay_signature` |

### Guest endpoints — CSRF requirement

The three guest endpoints run without a Frappe session, so the session has no
CSRF token and Frappe's check short-circuits. **They are not CSRF-exempt by
design** — they are authorized by their own credential instead:

| Endpoint | Authorized by | Validated before any lookup or mutation |
| --- | --- | --- |
| `payment.get_checkout_data` | 32-byte `custom_checkout_token` (~256-bit) | token presence, then existence, then `custom_checkout_expiry` |
| `payment.process_payment` | same token | same, then payment-method dispatch |
| `payment.verify_payment` | Razorpay HMAC signature | signature verified server-side before state change |

Angular must treat the checkout token as a bearer credential: never log it,
never place it in a shareable URL beyond the checkout route.

## Error contract

Branch on HTTP status, then `errors[0].code`. There is no `success` boolean.
36 published storefront codes plus the platform codes; see
[`error-catalog.md`](error-catalog.md).

Frappe's classified exceptions keep their intentional status (CHG-002 §7):
`ValidationError` → `validation_failed` at **417**; `PermissionError` (403),
`DoesNotExistError` (404) and `DuplicateEntryError` (409) are **not** mapped and
surface as Frappe's own body — Angular must handle both shapes until those
mappings are approved.
