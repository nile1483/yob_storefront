# Frontend Contract

Every endpoint the Angular storefront uses, as the backend actually implements
it today. Paths are dotted Frappe RPC paths called at
`/api/method/<dotted.path>`.

Read `README.md` first for the envelope model.

---

## 1. Authentication & session

### `POST yob_auth.api.auth.login_with_password` — **PUBLIC**

```json
{ "application": "STOREFRONT", "username": "buyer@example.com", "password": "…" }
```

All three fields are **mandatory**; omitting one is a `TypeError` → 500, not a
clean validation error.

Success returns `data.authenticated`, the profile context, and `csrf_token`.
`Set-Cookie` delivers **`sid` as HttpOnly**.

**Rate limit:** 10 attempts per 300 s keyed on username, counted *before*
authentication — successful logins consume the budget too. Exceeding it returns
429, which can look like a bad password. Budget your dev/test logins.

### `POST yob_auth.api.auth.request_otp` / `login_with_otp` — **PUBLIC**

`request_otp(application, identifier, method)` → `login_with_otp(challenge_id, otp)`.

### `GET yob_auth.api.auth.get_session_context` — authenticated

Query: `application`. Returns the current session/profile context and a
`csrf_token`. Use it to re-hydrate after a reload.

### `POST yob_auth.api.auth.logout` — authenticated

No body. Requires CSRF.

### Session rules for the frontend

- **Never read, store, or send `sid` yourself.** It is HttpOnly and browser-managed.
- Send `X-Frappe-CSRF-Token` on **every non-GET authenticated** call.
- Authenticated calls must traverse the edge proxy that sets
  `X-YOB-Original-Host`; a direct origin call answers 403
  `application_access_denied` even with a valid cookie.

---

## 2. Catalog — all authenticated

| Endpoint | Method | Params |
|---|---|---|
| `catalog.get_categories` | GET | `parent_slug` (optional) |
| `catalog.get_category` | GET | `slug`, `qty` (default 1) |
| `catalog.get_item` | GET | `slug`, `qty` (default 1) |
| `cms.get_config` | GET | — |

`qty` exists because pricing is quantity-dependent — the server returns the
price *for that quantity*, including any quantity-break pricing rule.

**Category object:** `name`, `category_name`, `slug`, `parent_category`,
`display_order`, `thumbnail`, `banner`, `meta_title`, `meta_description`.
`get_categories` also returns `meta.count`.

**`cms.get_config`** returns `store_name`, `default_currency`,
`default_price_list`, `company`, `store_domain`, `store_logo`,
`default_terms_page`, `default_privacy_page`, `allow_guest_purchase`,
`default_warehouse`, `allowed_payment_modes`.

> **Known quirk — `allowed_payment_modes` is always `[]`.** The backend reads a
> field named `allowed_payment_modes` while the DocType field is
> `allowed_payment_methods`. Documented as current truth. **Do not use it** to
> decide payment methods — use the payment-method endpoints, which are
> authoritative.

Listing and detail responses use their own field sets. They are **not**
normalised to a single shape; build separate DTOs rather than assuming a
category card and a category detail carry identical fields.

---

## 3. Cart — all authenticated

> This section documents **current** behaviour. A future redesign is described
> at the end under *DEFERRED* — do not implement that now.

### Response-shape asymmetry — read this carefully

| Endpoint | Returns |
|---|---|
| `cart.get_cart` | **envelope object**: `{ cart, contact, billing_address, shipping_address, cart_updated, removed_items, price_updated_items }` |
| `cart.add_to_cart` | **flat Cart document** (the same fields as the inner `cart` object above) |
| `cart.remove_from_cart` | **flat Cart document** |
| `cart.set_cart_*` | **small acknowledgement object** — see §5 |

These three shapes are genuinely different. Parse them separately, and treat
`get_cart` as the canonical read.

### `GET cart.get_cart`

`data.cart` fields: `name`, `customer`, `company`, `company_address`, `user`,
`order_type`, `currency`, `selling_price_list`, `contact_person`,
`billing_address`, `shipping_address`, `is_shippable`, `total_quantity`,
`net_total`, `tax_total`, `total_discount`, `coupon_code`, `coupon_discount`,
`grand_total`, `status`, `sales_order`, `ordered_on`, `checkout_by`, `items[]`.

`items[]` fields: `item_code`, `item_name`, `item_slug`, `image`, `quantity`,
`uom`, `stock_uom`, `conversion_factor`, `base_price`, `rate`, `amount`,
`discount_percentage`, `discount_amount`, `line_discount`, `tax_amount`,
`total_amount`, `pricing_rules`, `pricing_rule_apply_on`, `pricing_rule_label`,
`item_expiry_date`, plus Frappe's `parent`/`parentfield`/`parenttype`.

`data.contact`, `data.billing_address`, `data.shipping_address` are **resolved
objects** (display strings, city, state, country, pincode) — not just ids.

**Reconciliation fields** — `cart_updated` (boolean), `removed_items[]`,
`price_updated_items[]`. The server silently removes items that became
unavailable and re-prices the rest on every read. If `cart_updated` is true,
tell the user what changed.

### `POST cart.add_to_cart`

```json
{ "item_code": "ITEM-001", "qty": 2 }
```

> ### ⚠️ `qty` is a DELTA, not the new quantity
>
> ```
> line has qty 2  →  add_to_cart(item, qty=5)  →  line becomes 7   (one row)
> ```
>
> The supplied `qty` is **added** to any existing line for that `item_code`. It
> does **not** replace it, and it does **not** create a second row.
>
> **A quantity stepper must send the difference.** Moving 2 → 5 means sending
> `qty=3`. Sending `qty=5` gives you 7.
>
> **This call is NOT idempotent.** A retry, double-submit or double-click
> **adds twice**. Disable the control while in flight, and never auto-retry
> this call on a network error — the first attempt may already have applied.
>
> There is **no absolute set-quantity endpoint**. To set a value outright,
> either compute the delta client-side, or `remove_from_cart` then
> `add_to_cart` with the target quantity.

Other rules:

- **One row per `item_code`.** The backend keeps a single row deliberately:
  ERPNext evaluates a Pricing Rule's `min_qty`/`max_qty` against the **row**
  quantity, so two rows of 5 would miss a `min_qty=10` rule that one row of 10
  satisfies.
- **Fractional quantities are accepted** (e.g. `2.5`).
- `qty` must be **> 0** — `0` or negative answers 422 `quantity_invalid`
  with `field: "qty"`.

### `POST cart.remove_from_cart`

```json
{ "item_code": "ITEM-001" }
```

Removes the **whole line**, regardless of quantity. There is no partial
decrement endpoint.

### `POST cart.clear_cart` / `apply_coupon` / `remove_coupon`

`apply_coupon` takes `{ "code": "…" }`. Coupon errors are a family of their own
— see `ERROR-CODES.md`.

### Pricing and currency

**All money is server-authoritative.** `rate`, `amount`, `discount_*`,
`tax_amount`, `net_total`, `tax_total`, `grand_total` and `currency` are
calculated by ERPNext's pricing engine on the server. The frontend displays
them and never recomputes them. `cart.currency` is the authority for
formatting — do not use a locale default.

### Cart expiry

Expiry is **whole-cart**, driven by `YOB Store Settings.cart_expiry` (hours)
against the cart's `modified` timestamp. When it lapses, the server clears
**all** items and totals on the next read. There is no per-line expiry today;
`item_expiry_date` exists on the row but is not the mechanism.

### DEFERRED / NOT CURRENT CONTRACT

The backend project state also describes a **future** Cart redesign. **None of
it is implemented. Do not build against it:**

- independent duplicate rows for the same item;
- stable per-line identity;
- immutable line quantity;
- per-line expiry.

Current behaviour is: **one row per `item_code`, `qty` added as a delta**, whole-line
removal, and whole-cart expiry — as documented above. ("Upsert" is deliberately
avoided as a description here: it is ambiguous about whether the supplied
quantity replaces or accumulates. It accumulates.)

---

## 4. Addresses & Contacts — all authenticated

| Endpoint | Method | Params |
|---|---|---|
| `address.get_addresses` | GET | — |
| `address.get_contacts` | GET | — |
| `address.add_address` | POST | body fields (see below) |
| `address.add_contact` | POST | body fields |
| `address.update_address` / `update_contact` | POST | body fields |
| `address.delete_address` / `delete_contact` | POST | `name` |

**Address object:** `name` ← **the identifier you send back to setters**,
`address_title`, `address_type`, `address_line1`, `address_line2`, `city`,
`state`, `country`, `pincode`, `display` (pre-rendered HTML block),
`is_primary_address`, `is_shipping_address`.

**Contact object:** `name` ← identifier, `first_name`, `last_name`, `full_name`,
`email`, `phone`, `salutation`, `designation`, `gender`, `company_name`.

Notes:

- `name` is a **Frappe document name**, e.g. `"Example Contact-Example Buyer Ltd"` — it
  contains spaces and hyphens. Treat it as an opaque string; never parse it.
- `display` is server-rendered HTML with `<br>`. Render as HTML or strip tags —
  do not assume plain text.
- The Customer link is attached **server-side** from the session. A caller
  cannot attach an address or contact to another Customer.
- **Billing and shipping share one Address list.** There is no separate
  shipping address book — `address_type` is informational, and either address
  may be selected for either role.
- India-specific fields (GSTIN, state code) appear inside `display` when
  present; they are not separate response fields.
- **Create responses are minimal.** After `add_address` / `add_contact`, re-read
  the list rather than assuming the response contains a fully resolved object.

There are no other address/contact endpoints in the storefront contract.

### 4.1 `update_address` / `update_contact` are PARTIAL updates

**Send the fields your form edits. Nothing else.** Both endpoints key on whether
a field was **present in the request**, not on whether its value is truthy:

| You send | Result |
|---|---|
| field omitted | **unchanged** — the stored value is preserved |
| field with a value | validated and applied |
| field with an explicit empty value (`""`) | **cleared**, where the field is optional |
| field with an invalid value | `validation_failed`, and the record is untouched |

```jsonc
// The address has address_line2, phone, email_id and both default flags set.
{ "name": "Example Billing-Billing", "address_line1": "2 New Road" }
// -> address_line1 changes. Everything else is exactly as it was.
```

**Do not resend untouched optional fields to protect them.** That was a real
workaround for a real bug — `update_address` used to assign every field
unconditionally, so an omitted field was written as blank and silently
destroyed. It no longer behaves that way, and padding the payload now risks the
opposite mistake: sending `""` for a field the user never touched will **clear**
it, because an explicit empty value is a deliberate instruction.

`name` is required and identifies the record. It is **not** editable — changing
`address_title` renames nothing, so document names and every link pointing at
them stay stable.

Both are **idempotent**: sending the same payload twice converges on the same
record.

Errors: `validation_failed` (422, with `field` where the failing field can be
identified), `address_not_found` / `contact_not_found` (404).

### 4.2 Deleting may legitimately be refused

`delete_address` / `delete_contact` take `name` and return an empty `data` on
success. Deletion is **conditional**: Frappe refuses to delete a record another
document still points at.

| Outcome | Status | Code |
|---|---|---|
| deleted | 200 | — |
| unknown, or not yours | 404 | `address_not_found` / `contact_not_found` |
| still referenced | **409** | **`address_in_use` / `contact_in_use`** |

An address or contact is "in use" when a **Cart** has it selected, a
**historical Sales Order** references it, or it is the **Customer's default**.

**Nothing is detached automatically.** The backend will not clear a Cart's
selection or rewrite an order so a delete can proceed — that would either break
a live checkout or alter history. The customer must change the selection
themselves first.

The 409 carries **only the code and a plain sentence**. It deliberately does not
name the referring documents: identifying which order or cart blocks the delete
is internal information, and Frappe's own message for this case embeds an
absolute Desk URL. Show your own message from the code; there is nothing to
parse.

> **404 and 409 mean different things.** 404 is terminal — remove the row.
> 409 means the record still exists and is still valid; leave it in the list.

### 4.3 After a mutation: re-read, never auto-retry

**Re-read the list after every successful mutation.** The server invalidates its
own list cache on write, so a `get_addresses` / `get_contacts` immediately after
a write returns the new state — you do not need to wait, poll, or bust a cache.

**Do not automatically retry a delete whose response you did not receive.**
Update is safe to repeat; delete is not, and the danger is not a double
deletion:

```
delete succeeds -> response lost -> client retries -> 404 not_found
```

That 404 is indistinguishable from "this never existed", so an automatic retry
turns a **success** into what looks like an error. On an uncertain delete,
**re-read the list** and see whether the record is gone. The same applies to any
generic mutation-retry layer: there is no server-side request-deduplication for
these endpoints, so do not add a blanket client-side retry either.

---

## 5. Checkout cart selections — authenticated

| Endpoint | Body | Returns |
|---|---|---|
| `cart.set_cart_contact` | `{ "contact_person": "<Contact.name>" }` | `{ "contact_person": "…" }` |
| `cart.set_cart_billing_address` | `{ "billing_address": "<Address.name>" }` | `{ "billing_address": "…", "shipping_address": "…" }` |
| `cart.set_cart_shipping_address` | `{ "shipping_address": "<Address.name>" }` | `{ "shipping_address": "…" }` |

### The rule that matters

**A setter returns an acknowledgement, NOT the Cart.**

```
setter  →  { "contact_person": "…" }   ← selection echo only
        →  frontend MUST call get_cart
        →  ingest the canonical Cart
```

Totals, `is_shippable`, taxes and reconciliation flags are **not** in the setter
response. Treating the setter response as a Cart will leave your UI stale.

### Billing auto-fills shipping

`set_cart_billing_address` returns **both** `billing_address` and
`shipping_address`. When shipping is unset, setting billing also populates
shipping. That is why the response carries both — reflect both in your state,
then refresh with `get_cart`.

### `is_shippable` is server-owned

Derived on every reprice from whether any line is a stock item. It can **change
on any Cart response** — adding a digital-only item can turn it off, adding a
physical item turns it on. Re-read it from each Cart response; never cache it
across a mutation, and never compute it client-side.

Setting a shipping address on a non-shippable cart answers
`shipping_not_applicable`. Ownership violations answer `contact_invalid`,
`billing_address_invalid`, or `shipping_address_invalid`.

---

## 6. `POST checkout.proceed_to_payment` — authenticated

**No request body.** Requires `sid` + `X-Frappe-CSRF-Token`. Everything is
derived from the session and the buyer's open cart.

```json
{ "message": { "data": {
    "payment_request": "ACC-PRQ-2026-00002",
    "payment_url": "/payment/<token>",
    "token": "<token>"
}, "notice": "Proceed to payment" } }
```

### Status semantics — handle **both** as success

| Status | Meaning |
|---|---|
| **201** | a Payment Request was **created** (first checkout, or a replacement because the cart changed) |
| **200** | an existing open obligation was **reused** — same request, same token |

An unchanged cart reuses the same obligation and token. If the cart changed, the
server issues a replacement and **immediately revokes the old token**.

`payment_url` is a **relative SPA path** — your app must own the `/payment/:token`
route. The token expires **1 hour** after issue.

Precondition failures (422 unless noted): `cart_not_found` (404), `cart_empty`,
`contact_required`, `billing_address_required`, `shipping_address_required`
(only when the cart is shippable).

**This is the authenticated hand-off.** Everything after it is public.

---

## 7. Public payment — `/payment/<token>`

> ### `/payment/<token>` IS PUBLIC
>
> **A storefront session is NOT required.** The token is the credential for one
> exact Payment Request. A payer may arrive from a Checkout redirect, a shared
> link, an email, or an incognito window with no `sid` at all.
>
> **Do not put this route behind your session guard.**

### `GET payment.get_checkout_data` — **PUBLIC**

Query: `token`.

Serves **two source shapes**. Branch on `data.source_doctype`:

**`"Cart"`** (before payment initiation) — the full cart envelope
(`cart`, `contact`, `billing_address`, `shipping_address`, `cart_updated`,
`removed_items`, `price_updated_items`) **plus** `source_doctype`,
`source_name`, `payment_request`, `amount`, `currency`, `payment_methods[]`.

**`"Sales Order"`** (after payment initiation committed the order) —
`source_doctype`, `source_name`, `customer`, `company`, `amount`, `currency`,
`items[]`, `billing_address`, `shipping_address`, `contact_person`,
`order_status`, `docstatus`, `payment_request`, `payment_methods[]`.

**Two traps in the Sales Order shape:**
1. `billing_address` / `shipping_address` are **name strings**, not objects.
2. `items[]` is **flat** (`item_code`, `item_name`, `quantity`, `uom`, `rate`,
   `amount`) — not nested under `cart`.

After the source becomes Sales Order, **the Cart is no longer payment truth**.
It is not read, compared or revalidated, and a later cart edit cannot invalidate
the committed payment.

`amount` here is the **normal business amount** (e.g. `135.0`), always taken
from the immutable Payment Request.

Errors: `checkout_token_invalid` (404 — blank, unknown, or revoked),
`checkout_token_expired` (422), `payment_request_stale` (409 — the cart moved
since the obligation was issued; send the buyer back to the cart).

### `POST payment.process_payment` — **PUBLIC**

```json
{ "token": "<token>", "payment_method": "Razorpay" }
```

**These are the only two fields the backend accepts.**

`payment_method` is the Payment Method record **`name`** (the `name` field from
`payment_methods[]`) — **not** `method_code`. Sending `method_code` answers
`payment_method_unsupported`.

**The frontend must NOT send** `customer`, `cart`, `sales_order`, `amount`,
`currency`, `billing_address`, `shipping_address` or `contact` — all of these
come from token-bound trusted server state, and the endpoint has no parameters
for them.

**Pay Later** → `201`:

```json
{ "payment_method": "paylater", "sales_order": "SAL-ORD-…",
  "payment_request": "ACC-PRQ-…", "amount": 135.0,
  "currency": "INR", "payment_status": "Unpaid" }
```

**Razorpay** → `201`:

```json
{ "payment_method": "razorpay", "razorpay_key": "rzp_test_…",
  "order_id": "order_…", "amount": 13500, "currency": "INR",
  "sales_order": "SAL-ORD-…", "payment_request": "ACC-PRQ-…" }
```

> **`amount` here is in PROVIDER MINOR UNITS — paise.** `13500` = ₹135.00.
> Everywhere else `amount` is the business amount. This value is meant to be
> handed to Razorpay Checkout **unchanged**. Do not multiply or divide it.

The response shape is identical whether the provider order was newly created,
reused, or recovered.

### `POST payment.verify_payment` — **PUBLIC**

```json
{ "razorpay_order_id": "order_…",
  "razorpay_payment_id": "pay_…",
  "razorpay_signature": "…" }
```

**No token.** The obligation is resolved through the provider order id. All
three fields are required; a missing one answers `validation_failed` with
`field` naming it.

Success → `200`:

```json
{ "sales_order": "SAL-ORD-…", "payment_request": "ACC-PRQ-…", "payment_id": "pay_…" }
```

**Idempotent.** Re-verifying the *same* payment returns the same body with
notice `"Payment already processed."`. A *different* payment against a settled
obligation answers `payment_already_processed` (409).

### `GET payment_method.get_payment_methods` — authenticated

Query: `customer`, `company`, `order_amount`.

> **`company` is required in practice.** Payment Method Assignments are usually
> Company-scoped, and omitting `company` yields an **empty list**. Pass the
> `company` from the cart or `cms.get_config`.

`customer` is accepted for backward compatibility only — it is overwritten with
the authenticated Customer and a mismatch is rejected.

Each method: `name` ← **use this as `payment_method`**, `method_code`,
`payment_type` (`Online`/`Offline`), `display_order`, `icon`, `description`.
Ordered by `display_order`. Render exactly this list.

For the public payment page use `payment_methods[]` from `get_checkout_data`
instead — same shape, no session needed.

---

## 8. Orders — all authenticated

| Endpoint | Method | Params |
|---|---|---|
| `order.get_orders` | GET | **none** |
| `order.get_order_details` | GET | `order_id` (**required**) |

Both are scoped to the authenticated Customer server-side. A non-existent order
and someone else's order answer **identically** (`order_not_found`, 404), so the
response cannot be used to probe for other customers' orders.

### `GET order.get_orders`

**Takes no parameters.** There is no filter, search, date range, page or limit —
sending any of them changes nothing. The whole list is returned every time.

Array of rows plus `meta.count`. A row has **exactly** these six fields:

```json
{ "name": "SAL-ORD-2026-00001", "status": "Draft", "grand_total": 1350.0,
  "currency": "INR", "transaction_date": "2026-08-13", "delivery_date": "2026-08-13" }
```

**Ordering: `creation` descending — newest order first.** The server sorts;
do not re-sort client-side.

> The sort key is the record's **creation timestamp**, not `transaction_date`.
> These differ: several orders placed on the same day share one
> `transaction_date` but still come back newest-first. Sorting the array by
> `transaction_date` in the client would scramble that order, and `creation`
> is **not** in the response, so the server order is the only way to know it.

> **`currency` is per row.** Each order carries its own stored currency. Never
> format an order-list amount using a store default or environment config — an
> order placed in another currency would render as the wrong money.

### `GET order.get_order_details`

`order_id` is **required**. Omitting it answers **422 `validation_failed`** with
`field: "order_id"` — it is not treated as "list all".

The response object has **exactly these 26 keys**:

| Group | Fields |
|---|---|
| Identity | `name`, `customer`, `status` |
| Dates | `transaction_date`, `delivery_date` (`"YYYY-MM-DD"`) |
| Money | `currency`, `original_total`, `discount`, `subtotal`, `net_total`, `tax`, `grand_total`, `rounded_total` |
| Contact | `contact_person`, `contact_email`, `contact_mobile` |
| Address | `billing_address_name`, `shipping_address_name`, `billing_address_display`, `shipping_address_display` |
| Terms | `payment_terms_template`, `tc_name`, `terms` |
| Collections | `items[]`, `taxes[]`, `payment_logs[]` |

Every scalar above may be `null` except `name`, `customer`, `status` and
`currency`. The three collections are always arrays, possibly empty.

`items[]` rows:

```
item_code  item_name  description  image  qty  uom
price_list_rate  rate  discount_percentage  discount_amount  amount  net_amount
```

`payment_logs[]` rows (Razorpay Payment Log, newest first; empty for an unpaid
or Pay Later order):

```
name  payment_status  payment_method  payment_amount  currency
razorpay_order_id  razorpay_payment_id  email  contact  creation
```

> `payment_method` here is the **provider's** method string (`"netbanking"`,
> `"card"`, …) — Razorpay's value for how the payer actually paid. It is not
> the YOB Payment Method `name` you send to `process_payment`.

> **`taxes[]` is passed through raw** from ERPNext's `Sales Taxes and Charges`
> child table, not projected into a YOB shape. Its columns are ERPNext's and are
> **not pinned by this contract** — treat them as display-only and do not build
> required DTO fields on them. Every real order observed on the backend so far
> has `taxes: []`, so no row shape has been verified against live data.

#### Contact fields come from the order, not the Contact master

`contact_person` / `contact_email` / `contact_mobile` are read from the Sales
Order's own stored fields, so editing a Contact does not rewrite past orders —
the same guarantee the addresses have below.

They are, however, **frequently `null`** on orders placed through storefront
checkout: only `contact_person` is populated at commitment today. Render them
defensively and do not require them.

#### Addresses — historical snapshot, stable types

```json
{
  "billing_address_name":     "Example Billing-Billing",
  "shipping_address_name":    "Example Shipping-Shipping",
  "billing_address_display":  "A401 Example House\nExampleton\nGujarat...",
  "shipping_address_display": null
}
```

Two clearly separated concerns:

| Field | Type | Meaning |
|---|---|---|
| `*_address_name` | `string \| null` | **identifier** — the linked Address master. For "use this again" style actions. Points at the CURRENT master, which may since have been edited or deleted. **Never render this as the order's address.** |
| `*_address_display` | `string \| null` | **historical display** — plain text, from the order's own immutable order-time snapshot. This is what the order was placed against, and what an invoice must show. |

**`*_address_display` is plain text with real newlines — never HTML.** Render it
as text (e.g. `white-space: pre-line`, or split on `\n`). You must not need
`[innerHTML]`, and you should not use it: the value is data, not markup.

**The type never changes.** It is `string | null` in every case — normal,
legacy-fallback, and missing. It is never an object.

Where the value comes from:

- **normal orders** — the immutable Sales Order snapshot. The Address master is
  **not read at all**. Editing or deleting the address afterwards does not
  change the order.
- **legacy orders with a blank snapshot** — best-effort projection of the
  currently linked Address, as the same plain-text string.
- **legacy with the linked Address also gone** — `null`, and the rest of the
  order still renders.

## 9. Razorpay — the frontend's part

```
process_payment (razorpay)
  → { razorpay_key, order_id, amount (paise), currency }
  → load https://checkout.razorpay.com/v1/checkout.js
  → new Razorpay({ key, order_id, amount, currency, handler })
  → user pays in Razorpay's hosted modal
  → handler receives { razorpay_order_id, razorpay_payment_id, razorpay_signature }
  → POST verify_payment with exactly those three
  → show success only after verify_payment succeeds
```

Rules:

- **The frontend never verifies the HMAC.** The backend does, server-side,
  before any state change. Signature material is never exposed to the browser.
- **Never send an authoritative amount or currency** — the endpoint does not
  accept them.
- `razorpay_key` is the **publishable** key. The API secret never leaves the
  server and appears nowhere in any response.
- Pass `amount` through unchanged; it is already in paise.
- **Do not persist the token** in `localStorage` or `sessionStorage`. It lives in
  the URL for the life of the page.
- Do not redirect to any Frappe-hosted checkout page. YOB deliberately does not
  use Frappe's hosted flow.

A provider failure **after** local commitment returns `payment_provider_error`
with `details.retryable: true` and `details.sales_order`. **The order was not
rolled back** — offer a retry, do not report "order failed".

---

## 10. Security contract

**The frontend must NEVER:**

- store or read `sid`;
- use the payment token as a substitute session credential;
- require login for `/payment/<token>`;
- send customer, cart, order, amount, currency, address or contact into a
  token-authorized endpoint;
- calculate an authoritative payment amount;
- verify a Razorpay signature client-side;
- display a raw Frappe traceback (`exc`) or `_server_messages`;
- persist the payment token in web storage;
- reference the backend's internal service identity in any way.

**The frontend SHOULD:**

- display server-returned totals verbatim;
- follow the returned `payment_url` exactly;
- treat the token as sensitive (URL only, never logged);
- handle already-paid and revoked-token states as terminal;
- accept the server's cart reconciliation (`cart_updated`, `removed_items`,
  `price_updated_items`) and surface it;
- keep the public payment route outside the session guard.
