# API Changelog — what changed vs earlier frontend assumptions

Not a git log. This lists **contract differences** between what the frontend
previously assumed (the earlier `reference/api` package and pre-payment specs)
and what the backend does today.

Each entry: **OLD** → **CURRENT** → **FRONTEND ACTION**.

Where nothing changed, nothing is listed.

---

## 0. Content block schemas are now typed (OpenAPI 3.4.1)

**Documentation only — no runtime change.** `cms.get_page` returns exactly what
it returned in 3.4.0.

**OLD** — `slides` and `cards` were published as `array<object>`: an array of
something. `desktop_height_px` / `mobile_height_px` were listed once, under the
image-banner group, without saying which other block types return them.

**CURRENT** — two real schemas, `BannerCarouselSlide` and `PromoCard`, both
`{desktop_image, mobile_image, title, alt_text, destination}` with every key
always present, referenced from `slides` and `cards`. Every `ContentBlock`
property now names the block types that carry it, and the height fields are
documented as belonging to `image_banner`, `banner_carousel` and `promo_grid`
only — never `rich_text` or `product_grid`. `MenuItem.children` is likewise a
real self-reference instead of `array<object>`.

**FRONTEND ACTION** — sync the 3.4.1 reference and confirm your existing DTOs
match; there is nothing to re-implement. If you hand-wrote a slide or card type,
regenerate it from the schema now that one exists. A guard test asserts the
published schemas against blocks the runtime actually projected, so this cannot
drift again.

## 0. Storefront navigation, filters and content pages (Phase 25C)

**OLD** — navigation was hard-coded in the SPA, there were no merchandising
filters, and there was no dynamic content page.

**CURRENT** — three new read endpoints plus one additive parameter:

```
cms.get_menu(menu_key)                     published navigation tree
cms.get_page(slug)                         ordered, discriminated content blocks
catalog.get_category_filters(scope_value)  facets for a category (no counts)
catalog.get_items(..., storefront_filters) OR within a filter, AND across filters
```

**FRONTEND ACTION** — drive header and drawer from `get_menu` instead of a
hard-coded tree; build facet UI from `get_category_filters` and send the keys back
in `storefront_filters`; **restart pagination whenever the selection changes**
(the cursor is bound to it). Render blocks by `type`. Treat any response
containing a `product_grid` as customer-specific and never cache it across users.
`storefront_page` destinations carry a `null` href by design — build `/pages/${target}` on the client.

## 0a. Variant families: one page, one card, server-side resolution

**OLD** — every ERPNext variant was listed as its own product card, and every
variant carried its TEMPLATE's slug, so a product URL resolved to an arbitrary
sibling. There was no attribute data anywhere.

**CURRENT** —

```
catalog.get_items          one card per simple Item and one per FAMILY,
                           never one per variant. New on every card:
                           has_variants, price_state ("priced" | "select_options").
                           A family card's money fields are all null.
catalog.get_item           on a family slug returns is_template/is_purchasable,
                           attributes[] and variants[] and NO price.
catalog.resolve_variant    NEW. (template, attributes, qty) -> the full resolved
                           product payload, same shape as a simple product page.
cart.add_to_cart           unchanged signature; a template answers item_is_template
                           (422) and an unsalable SKU item_not_purchasable (422).
```

**FRONTEND ACTION** — render a family page from `attributes[]`, disable any pair
missing from `variants[]`, call `resolve_variant` for the chosen combination, then
`add_to_cart(data.name, qty)`. Never build an item code, never cross attribute
values, never sort `values` (they arrive in the merchant's order), and never show a
price on a family card — use `price_state`.

## 0. Prices and quantities are in the item's SELLING UOM

**OLD** — the product page priced in the item's selling UOM while the Cart and
the Sales Order used the stock UOM. For an item sold in Boxes of 10 at ₹100/Nos
the page said **₹1000 per Box** and the cart charged **₹100 per Nos** for the same
input. Frontends that read `stock_uom` as "the unit" were reading the cart's side
of that disagreement.

**CURRENT** — one unit end to end, resolved by ERPNext (`sales_uom`, else
`stock_uom`) and recorded on the cart line. `quantity` is counted in the line's
`uom`; `rate` is per that unit. New response fields, all additive:

```
catalog.get_item     conversion_factor, stock_qty      (uom, stock_uom already existed)
catalog.get_items    uom, conversion_factor            (stock_uom already existed)
cart.get_cart        uom_changed_items[]               (reconciliation list)
```

**FRONTEND ACTION** — display `uom` beside every quantity ("2 Strips"), treat
`rate`/`base_price` as per-`uom`, and never convert units yourself: use
`stock_qty` when you need stock units and `actual_qty` (already in `stock_uom`)
for availability. Surface `uom_changed_items[]` like `removed_items[]` — it means
a stored quantity is now worth something different because the merchant changed
the item's conversion factor.

## 0b. `add_to_cart` can answer `cart_item_uom_changed` (409)

**OLD** — `add_to_cart` always merged a repeat add into the existing line for that
SKU.

**CURRENT** — it still does, unless the merchant changed the item's selling unit
after that line was priced. Then the quantity being sent and the quantity already
stored are counted in different units, so the call is refused with
`cart_item_uom_changed` and the cart is left exactly as it was. `details` carries
`item_code`, `existing_uom`, `current_uom`.

**FRONTEND ACTION** — surface it as "this item is now sold in <current_uom>",
offer `remove_from_cart` followed by a fresh `add_to_cart`, and do not convert
quantities or retry blindly.

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

## 20. `get_orders` ordering is server-owned: `creation` desc

**OLD** — the ordering was never documented, so a client could reasonably sort
the array itself, most plausibly by `transaction_date`.

**CURRENT** — the server returns rows **newest first, by `creation`**. Confirmed
by observation against real orders.

The distinction matters: `creation` is a timestamp, `transaction_date` is a
date. Orders placed on the same day share a `transaction_date` but still have a
correct newest-first order — and **`creation` is not in the response**, so a
client sorting by `transaction_date` cannot reproduce it and will scramble
same-day orders.

**FRONTEND ACTION** — render the array in the order received. Remove any
client-side sort of the order list. There is still no paging, filter or search
parameter; `get_orders` takes none.

## 21. `update_address` is a partial update — stop padding the payload ⚠️

**OLD** — `update_address` assigned **every** field unconditionally from the
request. A field you did not send was written as blank, so an edit form that
posted only the inputs it rendered destroyed `address_line2`, `phone`,
`email_id` and the `is_primary_address` / `is_shipping_address` flags. The call
returned **success**, so nothing indicated data had been lost.

The only defence was to resend a complete Address object on every edit.

**CURRENT** — a genuine partial update, keyed on request **presence**:

| You send | Result |
|---|---|
| field omitted | unchanged |
| field with a value | validated and applied |
| explicit `""` (optional field) | cleared |
| invalid value | `validation_failed`, record untouched |

**FRONTEND ACTION — and this reverses previous advice:**

1. **Send only the fields your form edits.** Do not resend untouched optional
   fields to preserve them; that workaround is obsolete.
2. **Do not pad the payload with `""`.** An explicit empty value is now a
   deliberate *clear*. Padding would destroy exactly the data the old workaround
   existed to protect.
3. A partial payload no longer fails: `{name, address_line1}` used to answer
   `internal_server_error` (blanking the India-Compliance-mandatory
   `gst_category`), and now succeeds.

`update_contact` already behaved this way and still does — the two are now
consistent, and both are idempotent.

## 22. Deleting an address or contact can be refused — 409, not 500 ⚠️

**OLD** — a delete blocked by link integrity produced:

- `delete_address` → generic **500 `internal_server_error`** — indistinguishable
  from a backend crash;
- `delete_contact` → **no envelope at all**: a raw Frappe `LinkExistsError` at
  **HTTP 417**, with `_server_messages` carrying HTML, the referring document's
  name, and an absolute Desk URL (`http://<host>/desk/cart/CART-…`).

**CURRENT** — one business error on both:

```json
{ "message": { "errors": [
  { "code": "address_in_use", "field": "name",
    "detail": "This address is currently in use and can't be deleted." } ] } }
```

**409**, `address_in_use` / `contact_in_use`. Refused when a Cart has the record
selected, a historical Sales Order references it, or it is the Customer default.
No Desk HTML, no `_server_messages`, no referring docname.

**FRONTEND ACTION** — handle 409 as its own outcome:

1. It is **not** 404. The record still exists and stays in the list.
2. It is **not** retryable — nothing is detached automatically. The user must
   change the Cart selection first.
3. Render your own copy from the code; the body has nothing to parse.

## 23. Do not auto-retry a delete

**OLD** — not specified, so a generic mutation-retry layer would cover deletes.

**CURRENT** — unchanged behaviour, now stated: these endpoints have no
request-deduplication.

```
delete succeeds -> response lost -> retry -> 404 not_found
```

A retry turns a **success** into what looks like an error.

**FRONTEND ACTION** — on an uncertain delete, **re-read the list** and check
whether the record is gone. Updates are safe to repeat; deletes are not. Exclude
these endpoints from any blanket retry policy.

## 24. Account list caches now invalidate on write

**OLD** — `get_addresses` / `get_contacts` are cached for 30 minutes, and the
invalidation was broken: the cache-clear helper received the Customer *document*
where the key is built from the customer *name*, so the key never matched. A
list read after a write returned **stale data until the TTL expired**.

**CURRENT** — every mutation invalidates the list. A read immediately after a
write returns the new state.

**FRONTEND ACTION** — re-read the list after a successful mutation and trust the
result. Remove any cache-busting parameter, forced delay or optimistic-state
workaround added to compensate.

---

## Explicitly unchanged

- Frappe outer `message` envelope, and the YOB `data`/`notice`/`meta` vs
  `errors[]` inner model.
- CSRF on non-GET authenticated calls.
- HttpOnly `sid`, browser-managed.
- Catalog and auth request/response shapes.
- All pre-existing error codes keep their published values.

> **Orders are NOT in this list.** An earlier revision of this file listed
> "catalog, auth, and orders" as unchanged, which contradicted items 18–19 on
> the same page. Order detail and the order list both changed — see those two
> entries.
