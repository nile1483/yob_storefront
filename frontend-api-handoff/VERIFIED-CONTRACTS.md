# Verified Contracts — evidence ledger

What is proven, and by what kind of evidence. Nothing is upgraded beyond the
strongest *truthful* evidence.

## Evidence levels

| Level | Means |
|---|---|
| **REAL PROVIDER VERIFIED** | executed against the real Razorpay Test Mode API over the network |
| **REAL BROWSER VERIFIED** | a human completed the flow in an actual browser against the running stack |
| **WIRE VERIFIED** | observed over HTTP through the edge proxy |
| **TEST VERIFIED** | the real endpoint executed in-process against seeded data, provider faked, savepoint-isolated. No HTTP round trip |
| **SOURCE VERIFIED** | read from the implementation; not executed |
| **NOT VERIFIED / OPEN** | no evidence yet |

---

## Auth & session

| Fact | Evidence |
|---|---|
| `login_with_password` returns session + `csrf_token`, sets HttpOnly `sid` | **WIRE VERIFIED** |
| `get_session_context` shape | **WIRE VERIFIED** |
| `logout` requires CSRF | **WIRE VERIFIED** |
| Login rate limit 10/300 s keyed on username, counted pre-auth | **SOURCE VERIFIED** |
| Unauthenticated call → 403 with raw traceback, not the YOB envelope | **WIRE VERIFIED** |
| OTP endpoints | **SOURCE VERIFIED** |

## Catalog & CMS

| Fact | Evidence |
|---|---|
| `get_categories` / `get_category` / `get_item` shapes | **WIRE VERIFIED** |
| `cms.get_config` shape | **WIRE VERIFIED** |
| `allowed_payment_modes` always `[]` (field-name defect) | **SOURCE VERIFIED** |

## Cart

| Fact | Evidence |
|---|---|
| `get_cart` envelope: `cart`, `contact`, `billing_address`, `shipping_address`, reconciliation fields | **TEST VERIFIED** (executed) |
| `add_to_cart` / `remove_from_cart` return a **flat Cart document**, not the `get_cart` envelope | **TEST VERIFIED** (executed) |
| **`add_to_cart` `qty` is a DELTA** — one row per `item_code`; 2 then 5 gives **7**, not 5 | **TEST VERIFIED** — `tests/test_cart_quantity_semantics.py` |
| `add_to_cart` is **not idempotent** — repeating adds again (2+5+5 = 12) | **TEST VERIFIED** — same file |
| Fractional deltas accumulate (2.5 + 0.25 = 2.75) | **TEST VERIFIED** |
| One row **per item**; a second item gets its own row | **TEST VERIFIED** |
| `qty <= 0` refused and leaves the existing line untouched | **TEST VERIFIED** |
| No absolute set-quantity endpoint exists | **SOURCE VERIFIED** (endpoint registry) |
| Whole-cart expiry from `YOB Store Settings.cart_expiry` | **SOURCE VERIFIED** |
| Server-authoritative pricing/tax/discount; `cart.currency` authoritative | **TEST VERIFIED** |
| Cart → Sales Order financial parity (net, tax, discount, grand total) | **TEST VERIFIED** |

> **The delta tests were mutation-checked.** With `add_to_cart` temporarily
> switched to SET semantics, **4 of the 6 failed**. They genuinely pin the
> behaviour rather than passing incidentally. The mutation was reverted and the
> file verified byte-identical by checksum.

## Checkout selections

| Fact | Evidence |
|---|---|
| Setters return an **acknowledgement**, not a Cart | **TEST VERIFIED** (executed) |
| `set_cart_billing_address` returns **both** billing and shipping (auto-fill) | **TEST VERIFIED** (executed) |
| `is_shippable` server-derived and may change on any Cart response | **TEST VERIFIED** |
| Contact/address ownership validated against the customer | **TEST VERIFIED** |

## Addresses & contacts

| Fact | Evidence |
|---|---|
| `get_addresses` / `get_contacts` shapes; `name` is the identifier | **TEST VERIFIED** (executed) |
| `display` is server-rendered HTML | **TEST VERIFIED** (executed) |
| Customer link attached server-side | **SOURCE VERIFIED** |
| Billing and shipping share one Address list | **SOURCE VERIFIED** |

## Account CRUD (Phase 15B)

All executed against the real endpoints, savepoint/rollback isolated, in
`tests/test_account_crud_audit.py`.

| Fact | Evidence |
|---|---|
| `update_address` is a **partial** update — omitted fields keep their stored value | **TEST VERIFIED** |
| Omission vs explicit empty are distinct: omitted preserves, `""` clears | **TEST VERIFIED** |
| An explicit `0` on a flag is applied (presence, not truthiness) | **TEST VERIFIED** |
| The same payload applied twice converges (idempotent) | **TEST VERIFIED** |
| Clearing a required field → `validation_failed` **with the field named** | **TEST VERIFIED** |
| An invalid value → `validation_failed`, **not** a generic 500 | **TEST VERIFIED** |
| A minimal `{name, address_line1}` payload succeeds (no mandatory-`gst_category` 500) | **TEST VERIFIED** |
| Validation detail carries no traceback, Desk URL or exception class | **TEST VERIFIED** |
| `update_address` / `update_contact` never rename the document | **TEST VERIFIED** |
| Dynamic Links (Customer ownership) survive an edit | **TEST VERIFIED** |
| `update_contact` preserves omitted fields; explicit empty `first_name` → `validation_failed` | **TEST VERIFIED** |
| `update_contact` response reflects the **saved** record, not the request | **TEST VERIFIED** |
| Another customer's address/contact: not editable, not deletable | **TEST VERIFIED** |
| Unknown and foreign records answer **identically** (no existence probe) | **TEST VERIFIED** |
| Unreferenced address/contact deletes | **TEST VERIFIED** |
| Cart-selected → **409 `address_in_use` / `contact_in_use`** | **TEST VERIFIED** |
| Historical Sales-Order-linked → **409** | **TEST VERIFIED** |
| Customer-default-linked address → **409** | **TEST VERIFIED** |
| A refused delete **detaches nothing** — the referring order is byte-identical | **TEST VERIFIED** |
| A link conflict raises **no exception**; it returns an envelope | **TEST VERIFIED** |
| Conflict response carries no `_server_messages`, Desk URL, HTML, or referring docname | **TEST VERIFIED** — and `frappe.local.message_log` is asserted empty, which is what the framework would have serialised |
| `in_use` (409), `not_found` (404) and 500 are three distinct outcomes | **TEST VERIFIED** |
| List caches invalidate on add / update / delete, for both addresses and contacts | **TEST VERIFIED** — read-after-write, no TTL wait |
| A **refused** delete leaves the list intact | **TEST VERIFIED** |
| A Cart reflects an edited Address master (live, not snapshotted) | **TEST VERIFIED** |
| Editing an Address still does not change a past order | **TEST VERIFIED** — the Phase 14.5 guarantee, re-asserted from the CRUD side |

> **Mutation-checked.** Each fix was reverted in turn and the suite re-run:
> restoring the full-replace killed **12** tests, restoring the cache bug killed
> **6**, and removing the `message_log` truncation killed **5**. The file was
> restored byte-identical (checksum) after each. These tests pin the behaviour
> rather than passing incidentally.
>
> **The raw leak was captured, not inferred.** Executed against the running
> framework, a refused Contact delete queues:
> `Cannot delete or cancel because Contact <a href="http://<host>/desk/contact/…">…</a> is linked with Cart <a href="http://<host>/desk/cart/CART-…">…</a>`
> — absolute Desk URL, host, referring docname and HTML, at HTTP 417.

## Checkout hand-off

| Fact | Evidence |
|---|---|
| `proceed_to_payment` returns `payment_request`, `token`, `payment_url` | **REAL BROWSER VERIFIED** |
| **201 created / 200 reused**, same token on reuse | **TEST VERIFIED** (executed, both observed) |
| Unchanged cart reuses the obligation; changed cart supersedes and revokes | **TEST VERIFIED** |
| Token expires after 1 hour | **SOURCE VERIFIED** |

## Public payment

| Fact | Evidence |
|---|---|
| **`/payment/<token>` works with NO storefront session** | **REAL BROWSER VERIFIED** (incognito, direct link) |
| Checkout redirect → payment page | **REAL BROWSER VERIFIED** |
| `get_checkout_data` Cart-backed shape | **TEST VERIFIED** |
| `get_checkout_data` Sales Order-backed shape (name strings, flat items) | **TEST VERIFIED** |
| SO-backed checkout never consults a Cart | **TEST VERIFIED** |
| Token survives the Cart → Sales Order transition | **TEST VERIFIED** |
| `process_payment` accepts **only** `token` + `payment_method` | **SOURCE VERIFIED** (signature asserted by test) |
| `payment_method` is the Payment Method **`name`** | **TEST VERIFIED** |
| Payment-method eligibility computed server-side | **TEST VERIFIED** |
| `get_payment_methods` needs `company` or returns `[]` | **TEST VERIFIED** (executed) |

## Razorpay

| Fact | Evidence |
|---|---|
| **Real Test Mode payment completes end to end** | **REAL PROVIDER VERIFIED + REAL BROWSER VERIFIED** |
| Real HMAC signature verification executed on the success path | **REAL PROVIDER VERIFIED** |
| Payment Request → **Paid**, `mode_of_payment = Razorpay` | **REAL PROVIDER VERIFIED** |
| Token revoked after settlement | **REAL PROVIDER VERIFIED** |
| Cart → `Ordered`, points at the Sales Order | **REAL PROVIDER VERIFIED** |
| **PR ↔ Sales Order ↔ provider order is 1:1** | **REAL PROVIDER VERIFIED** (queried across real records) |
| Provider order creation, fetch, receipt lookup | **REAL PROVIDER VERIFIED** |
| Deterministic receipt (≤40 chars) accepted | **REAL PROVIDER VERIFIED** |
| Business→minor amount conversion (₹1 → 100 paise) | **REAL PROVIDER VERIFIED** |
| Razorpay does **not** enforce receipt uniqueness | **REAL PROVIDER VERIFIED** |
| Receipt lookup eventually consistent (~10 s) | **REAL PROVIDER VERIFIED** |
| One canonical provider order per PR across retries | **REAL PROVIDER VERIFIED + TEST VERIFIED** |
| Abandoned state: provider order, no payment, live token | **REAL PROVIDER VERIFIED** (observed) |

## Orders

| Fact | Evidence |
|---|---|
| `get_orders` rows carry the order's own `currency` | **TEST VERIFIED** + confirmed read-only against real orders (all INR) |
| `get_orders` takes no parameters; row is exactly 6 fields | **SOURCE VERIFIED** + observed against real orders |
| `get_orders` is ordered **`creation` desc** (newest first) | **REAL DATA VERIFIED** — read-only: returned order matches descending `creation` while all three real orders share one `transaction_date`, so the sort key is distinguishable |
| Order detail returns exactly **26 keys** | **REAL DATA VERIFIED** (read-only, enumerated) |
| `get_order_details` without `order_id` → 422 `validation_failed`, `field: "order_id"` | **TEST VERIFIED** (executed) |
| Order detail exposes `billing_address_display` / `shipping_address_display` as plain text | **TEST VERIFIED** — `tests/test_order_address_history.py` |
| **Editing the Address master does NOT change a past order (billing)** | **TEST VERIFIED** — historical regression |
| **Editing the Address master does NOT change a past order (shipping)** | **TEST VERIFIED** — historical regression |
| A snapshot-backed order never reads the Address master (proven by deleting it) | **TEST VERIFIED** |
| Display fields are `string \| null` in every branch, never objects | **TEST VERIFIED** |
| Legacy blank snapshot falls back to the current master, same type | **TEST VERIFIED** |
| Legacy blank snapshot + deleted Address returns `null` safely | **TEST VERIFIED** |
| Real orders carry both snapshots and render from them | **REAL DATA VERIFIED** (read-only) |
| Order detail does **not** query the Contact master; contact fields come from stored Sales Order fields | **SOURCE VERIFIED** |
| `items[]` row shape (12 fields) | **REAL DATA VERIFIED** (read-only) |
| `payment_logs[]` row shape (10 fields); `payment_method` is the PROVIDER's method string, not the YOB Payment Method name | **REAL DATA VERIFIED** — observed `"netbanking"` on real settled orders |
| `taxes[]` row shape | **NOT VERIFIED** — raw ERPNext child table, passed through unprojected. Every real order observed has `taxes: []`, so no row has ever been seen |

## Security

| Fact | Evidence |
|---|---|
| Tampered signature rejected | **TEST VERIFIED** (endpoint level) |
| Tampered signed value rejected | **TEST VERIFIED** (endpoint level) |
| Cross-transaction identifiers rejected | **TEST VERIFIED** (endpoint level) |
| Replay idempotent — one SO, one settlement | **TEST VERIFIED** |
| Already-paid cannot start another charge | **TEST VERIFIED** |
| Revoked token denies page **and** payment | **TEST VERIFIED** |
| Abandoned reopen does not duplicate | **TEST VERIFIED** |
| Ineligible method rejected; no provider order | **TEST VERIFIED** |
| Stale / amount / currency mismatch rejected pre-provider | **TEST VERIFIED** |
| Guest has no ERPNext access before **or after** paying | **TEST VERIFIED** |
| Guest commitment succeeds without Customer/Item permission | **TEST VERIFIED** |
| Caller cannot rebind the source (signature is `token` + `payment_method` only) | **TEST VERIFIED** |
| No `Payment Entry` created; Sales Orders stay Draft | **REAL PROVIDER VERIFIED** (queried) |

## Open / not verified

| Fact | Status |
|---|---|
| True parallel two-connection race on commitment | **NOT VERIFIED** — proven by serialised replay + source-asserted lock ordering only. The test runner cannot open two blocking connections without self-deadlocking |
| Orphaned provider order after a total network partition | **OPEN** — possible in principle; inert and unpaid. Reconciliation tooling not built |
| Razorpay webhook path | **NOT VERIFIED** — the frontend callback is the only settlement path in use |
| Order detail `taxes[]` row columns | **NOT VERIFIED** — no real order has a tax row yet. Do not build required DTO fields on it |
| Refunds, partial payments, multi-currency | **NOT IMPLEMENTED** |

---

## Test suite totals at generation

```
yob_storefront   325 tests   OK
yob_core          32 tests   OK
yob_auth          21 tests   OK
                 ───────────
                 378 tests   OK
```
