# API Changelog — what changed vs earlier frontend assumptions

Not a git log. This lists **contract differences** between what the frontend
previously assumed (the earlier `reference/api` package and pre-payment specs)
and what the backend does today.

Each entry: **OLD** → **CURRENT** → **FRONTEND ACTION**.

Where nothing changed, nothing is listed.

---

## 1. Cart setters return an acknowledgement, not a Cart

**OLD** — earlier docs did not pin the setter response shape; it was natural to
assume a setter returned the updated Cart.

**CURRENT** — verified by execution:

```
set_cart_contact          → { "contact_person": "…" }
set_cart_billing_address  → { "billing_address": "…", "shipping_address": "…" }
set_cart_shipping_address → { "shipping_address": "…" }
```

No totals, no `is_shippable`, no items, no reconciliation flags.

**FRONTEND ACTION** — after every setter, call `get_cart` and ingest the
canonical Cart. Do not merge the setter response into cart state as if it were
one.

## 2. Billing auto-fills shipping

**OLD** — not documented.

**CURRENT** — `set_cart_billing_address` returns **both** fields; when shipping
is unset, setting billing also sets shipping.

**FRONTEND ACTION** — reflect both values from that response, then refresh with
`get_cart`. Do not assume shipping is untouched.

## 3. `is_shippable` is server-owned and volatile

**OLD** — treated as a stable cart property.

**CURRENT** — derived on **every** reprice from whether any line is a stock
item. It can flip on any Cart response.

**FRONTEND ACTION** — re-read it from each Cart response. Never cache across a
mutation; never compute it client-side. `shipping_not_applicable` is the error
when you set a shipping address on a non-shippable cart.

## 4. Cart mutation responses differ in shape from `get_cart`

**OLD** — assumed one Cart shape.

**CURRENT** — `add_to_cart` and `remove_from_cart` return the **flat Cart
document**; `get_cart` returns an **envelope** (`cart`, `contact`,
`billing_address`, `shipping_address`, `cart_updated`, `removed_items`,
`price_updated_items`).

**FRONTEND ACTION** — two parsers, or normalise deliberately in one place. Treat
`get_cart` as canonical.

## 5. Public payment is token-authorized, not session-authorized

**OLD** — the payment page was reachable only after checkout, so a session was
implicitly assumed.

**CURRENT** — `/payment/<token>` is `allow_guest`. Verified in a real incognito
browser with no `sid`: full Razorpay payment completed.

**FRONTEND ACTION** — put the payment route **outside** the session guard. Do
not redirect to login. See `AUTHORIZATION-MATRIX.md`.

## 6. `get_checkout_data` now serves TWO source shapes

**OLD** — a single Cart-shaped payload.

**CURRENT** — branch on `data.source_doctype`:
`"Cart"` (before initiation) or `"Sales Order"` (after commitment). In the Sales
Order shape, `billing_address`/`shipping_address` are **name strings**, not
objects, and `items[]` is flat.

**FRONTEND ACTION** — handle both. A browser refresh after paying lands on the
Sales Order shape; assuming Cart-backed will crash the page.

## 7. `process_payment` request fields

**OLD** — assumed a richer payload might be needed.

**CURRENT** — the signature is exactly `token` and `payment_method`. Nothing
else is accepted.

**FRONTEND ACTION** — send only those two. Remove any customer/cart/order/
amount/currency/address/contact fields.

## 8. `payment_method` is `name`, not `method_code`

**OLD** — `method_code` (`"razorpay"`, `"paylater"`) looked like the identifier.

**CURRENT** — the backend expects the Payment Method record **`name`**
(e.g. `"Razorpay"`, `"Pay Later"`). Sending `method_code` answers
`payment_method_unsupported`.

**FRONTEND ACTION** — send `method.name` from `payment_methods[]`. `method_code`
remains available for display/branding only.

> Note: the **response** still echoes `payment_method` as the lowercase code
> (`"razorpay"`, `"paylater"`). Request and response use different values —
> intentional, for backward compatibility.

## 9. `proceed_to_payment` returns 200 **or** 201

**OLD** — a single success status assumed.

**CURRENT** — **201** when a Payment Request was created; **200** when an
existing open obligation was reused (same token).

**FRONTEND ACTION** — treat both as success. Do not branch on 201 alone.

## 10. Token is revoked after settlement

**OLD** — not specified.

**CURRENT** — on successful payment the token is cleared. Reusing that URL
answers `checkout_token_invalid` (404).

**FRONTEND ACTION** — treat it as a **terminal** state (show "payment
complete"), not a retryable error.

## 11. Razorpay `amount` is in paise

**OLD** — ambiguous.

**CURRENT** — `process_payment` (Razorpay) returns `amount` in **provider minor
units**: `13500` = ₹135.00. Everywhere else `amount` is the business amount.

**FRONTEND ACTION** — pass it to Razorpay Checkout **unchanged**. Do not
multiply or divide. Do not display it as a currency figure without converting.

## 12. New payment error codes

**OLD** — the earlier `checkout_payment` code list predated the payment work.

**CURRENT** — added: `payment_request_stale` (409),
`payment_provider_error` (500 **or 422**).

Also: `payment_provider_error` and `payment_provider_not_configured` now carry a
`details` object with `retryable` (bool) and, when relevant, `sales_order`.

**FRONTEND ACTION** — add both codes. **Branch on `details.retryable`** for
provider errors: `true` means the order exists and a retry is correct; `false`
means offer another method. See `ERROR-CODES.md`.

## 13. Provider failure after commitment does not mean "order failed"

**OLD** — a provider error would naturally be shown as a failed order.

**CURRENT** — `payment_provider_error` with `details.retryable: true` and
`details.sales_order` means the Sales Order **was committed and still exists**.

**FRONTEND ACTION** — offer a retry against the same payment link. Do not tell
the user their order failed.

## 14. `get_payment_methods` needs `company`

**OLD** — treated as optional.

**CURRENT** — Payment Method Assignments are typically Company-scoped; omitting
`company` returns an **empty list** with no error.

**FRONTEND ACTION** — always pass `company` (from the cart or `cms.get_config`).
On the public payment page, prefer `payment_methods[]` from `get_checkout_data`.

## 15. `address.add_contact` exists

**OLD** — missing from the earlier OpenAPI (only `add_address` was listed).

**CURRENT** — `POST address.add_contact`, mirroring `add_address`.

**FRONTEND ACTION** — you may create contacts inline at checkout.

## 16. `add_to_cart` `qty` is a DELTA, not an absolute quantity ⚠️

**This is the highest-risk item in this changelog.** It was raised as a
suspected error in the handoff and re-verified by execution; the handoff was
right and the earlier assumption was wrong.

**OLD / EARLIER ASSUMPTION** — `qty` **sets** the line quantity:

```
line has qty 2  →  add_to_cart(item, qty=5)  →  line becomes 5
```

**CURRENT BACKEND TRUTH** — `qty` is **added** to the existing line:

```
line has qty 2  →  add_to_cart(item, qty=5)  →  line becomes 7   (one row)
```

Proven by execution against the running backend, and stated explicitly in
`api/cart.py`:

```python
existing.quantity = (existing.quantity or 0) + qty
```

**FRONTEND ACTION — two things, both important:**

1. **A quantity stepper must send the delta, not the new total.** Moving 2 → 5
   means sending `qty=3`. Sending `qty=5` produces 7.
2. **`add_to_cart` is NOT idempotent.** A retried, double-submitted or
   double-clicked request **adds twice**. Guard the button, and do not retry
   this call automatically on network failure — a timeout may already have
   applied.

There is **no absolute "set quantity" endpoint** today. To set an absolute
value you must either compute the delta client-side, or
`remove_from_cart` followed by `add_to_cart` with the target quantity.

> The backend comment explains why one row is used rather than appending a
> second: ERPNext evaluates a Pricing Rule's `min_qty`/`max_qty` against the
> **row** quantity, so two rows of 5 would silently miss a `min_qty=10` rule
> that one row of 10 satisfies.

## 17. Other Cart semantics unchanged — and the redesign is still deferred

**OLD/CURRENT (no change)** — one row per `item_code`, fractional quantities,
whole-line removal, whole-cart expiry.

**Still DEFERRED, do not implement:** independent duplicate rows, stable line
identity, immutable line quantity, per-line expiry.

**FRONTEND ACTION** — none. Listed so the deferred design is not mistaken for
current contract.

## 18. Order detail: live Address objects replaced by immutable snapshots ⚠️

**OLD** — `get_order_details` returned resolved objects built by reading the
linked Address **master** live:

```json
{ "billing_address":  { "address_title": "...", "address_line1": "...", ... },
  "shipping_address": { ... } }
```

That made order history **mutable**: editing an address rewrote the address on
every past order. For an invoice-grade record that is silent data corruption —
nothing errors and the totals still agree.

**CURRENT** — explicit historical display fields, from the order's own
order-time snapshot (`Sales Order.address_display` and
`Sales Order.shipping_address`):

```json
{ "billing_address_name":     "Example Billing-Billing",
  "shipping_address_name":    "Example Shipping-Shipping",
  "billing_address_display":  "A401 Example House\nExampleton\n...",
  "shipping_address_display": null }
```

`billing_address` and `shipping_address` **are removed**. This is a deliberate
pre-deployment contract correction, not a compatibility break to work around: we
are not carrying historically mutable data forward for a frontend we control.

**FRONTEND ACTION:**

1. Stop binding `billing_address.*` / `shipping_address.*` object fields.
2. Render `*_address_display` as **plain text** — it contains real newlines
   (`white-space: pre-line`, or split on `\n`). It is **not** HTML; do not use
   `[innerHTML]`.
3. Keep using `*_address_name` only as an identifier (e.g. "reuse this
   address"), never as the order's rendered address.
4. Both display fields are `string | null` in **every** case — normal, legacy
   fallback, and missing. They never become objects, so no union type is needed.

> Order-time snapshots exist on all current real orders, so normal orders need
> no Address read at all. A legacy order with a blank snapshot falls back to the
> current master as best effort — still as the same string type. If that Address
> is gone too, the field is `null` and the rest of the order still renders.

## 19. `get_orders` rows now carry `currency`

**OLD** — order-list rows had no currency, so the client had to infer it from
environment configuration.

**CURRENT** — every row includes the Sales Order's own stored `currency`.

**FRONTEND ACTION** — format each row with `row.currency`. Remove any
environment-derived currency fallback in the order list: an order placed in a
different currency would otherwise render as the wrong money.

---

## Explicitly unchanged

- Frappe outer `message` envelope, and the YOB `data`/`notice`/`meta` vs
  `errors[]` inner model.
- CSRF on non-GET authenticated calls.
- HttpOnly `sid`, browser-managed.
- Catalog, auth, and orders request/response shapes.
- All pre-existing error codes keep their published values.
