# YOB Storefront Context

`yob_storefront` is the optional ecommerce/B2B ordering solution currently
installed on the recorded site. It is a sibling of future solution apps and is
not required by the shared platform.

## Ownership

Storefront owns catalog/product presentation, cart, pricing, coupons,
storefront contact/address flows, checkout, order/payment orchestration,
CMS/menu/cache behavior, provider adapters, domain errors, and its Desk
navigation.

## Payment provider architecture

Three layers, with ownership fixed:

| Layer | Owner | Responsibility |
| --- | --- | --- |
| Gateway configuration | **Frappe Payments** | `Payment Gateway` records, gateway Settings DocTypes, credentials, supported-currency metadata, and any provider capability its controller already satisfies |
| `YOBGateway` driver | `yob_storefront` | Provider capability adapter: one thin driver per gateway, under `integrations/gateways/` |
| Commercial lifecycle | `yob_storefront` | Cart, immutable Payment Request, Payment Method eligibility, Cart → Sales Order commitment, Pay Later, `/payment/:token`, the durable commit before any provider call, settlement and idempotency, and every public API contract |

Dispatch is `Payment Method → Payment Gateway → registry → YOBGateway`. The
`Payment Method.payment_gateway` link is the dispatch key; `method_code` remains
for display and frontend compatibility only. An internal YOB method such as
Pay Later has **no** gateway link, and that absence is how it is recognised.

Credentials have exactly one home: the gateway's own Settings DocType
(`Razorpay Settings` today). YOB stores no copy of any key or secret, and
credential access lives behind the driver rather than in payment orchestration.

**Frappe's hosted checkout is intentionally not used.** `get_payment_url`,
`create_request`, `authorize_payment` and the `*_checkout` pages drive a
server-rendered redirect flow; YOB has its own Angular SPA, which receives a
JSON payload and opens the provider's browser SDK itself. Payments remains the
server-side configuration foundation; the buyer's experience is YOB's.

### VERIFIED: real Razorpay Test Mode lifecycle

Confirmed by manual browser payments on a migrated dev site, then audited
read-only. Not inferred from tests.

**Happy path — both entry routes work:** authenticated Checkout → redirect to
`/payment/<token>`, and the same link opened directly in an incognito window
with **no storefront session**. Razorpay Test Mode payment completed in both.

**Settled record shape** (two independent transactions, ₹8910 and ₹1350):

```
Payment Request  status=Paid, mode_of_payment=Razorpay
                 provider order + provider payment stored
                 source fingerprint retained, checkout token REVOKED
Sales Order      Draft (docstatus=0), amount and currency equal to the PR
Cart             Ordered, pointing at that Sales Order
```

**Abandoned record shape** — legitimate, not a defect. A buyer who opens
Checkout and leaves:

```
Payment Request  Draft, provider order present, NO provider payment
                 claim set, fingerprint retained, token still LIVE
Sales Order      Draft, committed, amount matches
```

The live token is correct: the obligation is unpaid and the payer must be able
to return. Reopening the link neither duplicates the Sales Order nor the
provider order.

**Uniqueness, verified by query against real data:** no Sales Order serves more
than one Payment Request; no provider order is shared by more than one Payment
Request. `Payment Entry` count is 0 — no payment accounting exists, by design.

**Signature verification.** Settlement is reachable only past the Razorpay HMAC
check, so `status=Paid` with a real provider payment id is itself proof the
production HMAC path executed with a genuine Razorpay signature.

### VERIFIED: security boundaries

Each proven by an endpoint-level automated test, not by inspection:

| Boundary | Result |
| --- | --- |
| Invalid signature | rejected, no settlement, no state change |
| Tampered signed value (order id swapped, signature kept) | rejected |
| Payment from another transaction | rejected, fails closed |
| Duplicate/replay of a valid success | idempotent; one Sales Order, one settlement |
| Already-paid request | token revoked; no second charge can be started |
| Revoked token | denies both the payment page and payment initiation |
| Ineligible payment method | rejected server-side, no provider order |
| Stale / amount / currency mismatch | rejected before any provider operation |
| Guest privilege | no ERPNext access before or after paying |

Razorpay signs `order_id|payment_id`; verification happens server-side in
`payment_service.verify_razorpay_signature` before any state transition.

### Public payment authorization

`/payment/<token>` is **public**. A payer may arrive from authenticated
checkout, a shared link, an email or an incognito window. **No storefront login
or session is required**, so the caller is normally Frappe's `Guest`.

The credential is the **payment token**, not the session. After server-side
validation it authorizes exactly one Payment Request. The question the system
answers is not *"may Guest read Customer?"* but *"does this validated token
authorize payment of this exact Payment Request, whose trusted source authorizes
exactly this Sales Order?"*

**Guest receives no ERPNext roles or DocType permissions, ever.** Ordinary Guest
access to Item, Customer, Address, Contact and Sales Order remains denied, and
is asserted by test both before and after a successful public payment.

### Trusted internal execution

ERPNext's controllers permission-check documents YOB never constructs against
the *current execution user* — `get_item_details` loads its own cached Item and
calls `item.check_permission()`, and tax resolution reads the tax Account.
No document flag reaches those. Frappe 16.30.0 has no request-local
permission-bypass context (its only user-level short-circuit is
`user == "Administrator"`), and a controller `has_permission` hook can deny but
never grant.

So YOB briefly switches execution identity, **only after** token resolution,
source binding, financial invariants, party identity, payment state and method
eligibility have all passed:

```
Guest + token -> validate everything -> trusted_execution() -> ERPNext work
              -> finally: restore -> continue public response
```

| | |
| --- | --- |
| Identity | `payment-processor@yob.internal`, **`enabled = 0`** (cannot authenticate), `desk_access = 0` |
| Role | `YOB Payment Processor` — never `YOB Storefront Buyer`, never Administrator |
| Permissions | **`read` on Item and Account only.** Each was added only after a test proved the commitment path needs it |
| Not granted | Customer, Address, Contact, Sales Order — the Sales Order's own `flags.ignore_permissions` covers the party path |
| Scope | Wraps only the reprice and the Cart → Sales Order commitment. Never token lookup, general API handling or provider callbacks |

`frappe.set_user` is **banned project-wide except in this one boundary**
(`services/payment_request_service.py`), enforced by a guard test that also
asserts the exemption covers exactly one file.

**Restoration.** `set_user` clobbers nine request-local values, so the boundary
calls `set_user(original)` **first** — letting Frappe clear `cache`,
`role_permissions`, `user_perms`, `new_doc_templates` and the Jinja
environments, which is what stops privileged permission state leaking — then
restores the three it cannot rebuild: `session.sid` (overwritten with the
username), `session.data` and `form_dict`. Always in `finally`.

The bypass is DocType-permission only. It does **not** skip validation: ERPNext
party checks, required fields, pricing, taxes and India Compliance all still
run, and the three-way Cart == Payment Request == Sales Order invariant is
asserted afterwards.

Provisioning is **fresh-install only**. There are no deployed sites yet, so no
migration or upgrade patch exists for this identity.

### Preflight boundary

Static provider prerequisites are validated **before** the Cart is committed to
a Sales Order:

```
resolve token -> method eligibility -> resolve gateway -> gateway.preflight()
   -> ONLY IF PREFLIGHT PASSES: commitment -> provider dispatch
```

`preflight()` is non-network and side-effect free (credentials present,
currency supported). This keeps two failure classes distinguishable:

| | Committed? | `details.retryable` | `details.sales_order` |
| --- | --- | --- | --- |
| Preflight failure — the gateway could never take this payment | no | `false` | absent |
| Provider/network failure — a real obligation exists | yes | `true` | present |

Because preflight precedes commitment, and cart-staleness is detected *inside*
commitment, a misconfigured gateway is reported **before** a stale cart. That
ordering is deliberate: an unusable payment method is actionable regardless of
cart state.

Internal methods (Pay Later) have no gateway and skip preflight entirely.

### Amount units

`Payment Request.grand_total` is the **business** amount (₹135.00).
`Obligation.amount_minor` is provider minor units (13500 paise). The Frappe
Payments Razorpay controller takes the **business** amount and multiplies by 100
itself — verified against the installed code — so `amount_minor` must never be
passed to `controller.create_order`.

`Integration Request` is **provider transport/audit state only**. It never
determines the amount owed, cart freshness, the authoritative Sales Order,
whether a Payment Request is immutable, or retry idempotency —
`Payment Request + Sales Order` are the authoritative YOB payment state.
Verified against the installed Payments code: creating one changes no YOB
document, and `create_order` never reaches `authorize_payment`,
`on_payment_authorized` or the redirect flow.

Razorpay is the only implemented driver. It delegates configuration and
credentials to Frappe Payments, and retains YOB extensions for capabilities
Payments does not provide: deterministic receipt identity, recovery by receipt,
provider order fetch, and server-side HMAC verification.

## Catalog listing (Phase 22B-1, retired legacy path in 22B-3)

Responsibilities are now split cleanly, and there is only one product path.

| | `catalog.get_category` | `catalog.get_items` |
| --- | --- | --- |
| Returns | category metadata + child categories | products |
| Products | **none** | one bounded page |
| Pricing calls | **zero, always** | ~page size |
| Parameters | `slug` | scope, search, sort, page_size, cursor, qty |

```text
get_category  -> category metadata and children
get_items     -> ALL storefront product listing
```

### The retired path

Until Phase 22B-3, `get_category` also returned an embedded `items` array: every
Item in a leaf category, each priced through a throwaway Sales Order. Phase 22A
measured it at ~51 ms per Item, growing linearly (100 items took 5.1 s), and a
single end-of-life Item returned a 500 for the whole category. It was unbounded by
construction -- no limit, no cursor, no way for a caller to ask for less.

Phase 22B-2 migrated Angular to `get_items`; Phase 22B-3 deleted the item loading,
the per-item pricing, the `items` response field, `meta.item_count`, and the `qty`
parameter that existed only to price those products.

`tests/test_catalog_category.py` asserts **zero pricing calls** for leaf categories
holding 0, 1, 12 and 30 Items, and that no Item query runs at all. That assertion --
not the absence of a response field -- is what stops the unbounded path returning:
deleting a key while keeping the loop would still pass a shape check.

### Catalog visibility rule

An Item is listable only when an applicable **base Item Price > 0** resolves for the
customer, judged **before** Pricing Rules:

```text
no Item Price + fixed-rate Pricing Rule   -> NOT listable
Item Price 0  + Pricing Rule              -> NOT listable
Item Price 100 + 100% discount rule       -> listable (final rate may be 0)
```

Phase 22A proved a fixed-rate rule alone yields `price_list_rate = 999` with no Item
Price at all. Eligibility is a statement about the base price, never the final rate.

### Three stages

```text
Stage 1  bounded SQL candidates -- category, disabled, is_sales_item, has_variants,
         end_of_life, item_name search, keyset cursor, plus a BROAD `EXISTS` test for
         any possibly-applicable positive Item Price.
         A deliberate SUPERSET: false positives are fine, false negatives are lost
         products. It never decides WHICH price wins.
Stage 2  exact base price via ERPNext's own `get_price_list_rate_for`, plus
         variant->template and default-price-list fallbacks. Eligible iff > 0.
Stage 3  the existing Sales Order pricing engine, unchanged and still authoritative.
```

Stage 2 runs before Stage 3, so no Sales Order is ever built for an Item that has no
valid base price.

### Candidate scanning and continuation

Because Stage 1 is a superset, a page of 24 products is not 24 candidate rows: a run
of candidates can pass the cheap query and then fail exact Stage-2 eligibility. The
listing therefore scans in bounded batches until the page plus its lookahead is
filled, or the candidate space runs out.

Two limits, and the difference between them matters:

| Constant | Meaning |
| --- | --- |
| `MAX_CANDIDATE_BATCH` (96) | ceiling on rows returned by **one** candidate query; the batch itself is derived from `page_size` |
| `MAX_CANDIDATE_SCAN` (2000) | rows one **request** will examine before handing continuation back to the client |

`MAX_CANDIDATE_SCAN` is a **work budget, not a correctness boundary**. It exists only
so a single HTTP request terminates -- a category of 100k ineligible rows must not pin
a worker. Spending it never ends pagination.

There are exactly three outcomes, and each reports itself honestly:

| Outcome | `has_more` | `next_cursor` | Page |
| --- | --- | --- | --- |
| Page filled | `true` | last **returned** Item | full |
| Scan budget spent | `true` | last **examined** candidate | **may be short or even empty** |
| Candidates exhausted | `false` | `null` | short or full |

**Only genuine exhaustion is terminal.** A short or empty page with `has_more=true` is
a valid, expected answer meaning "no more products found *yet*" -- the client calls
Load More again and the scan resumes past the candidates already rejected, so progress
is guaranteed and no candidate is examined twice.

> Phase 22B-1 originally capped the scan at a fixed number of batches and treated
> hitting that cap as exhaustion. A run of Stage-1 false positives longer than the cap
> then answered `has_more=false`, stranding every product behind it with no way for a
> client to ask again. Fixed in Phase 22B-1A; there is no fixed batch-count cap, and
> `CandidateScanContinuationCase` regression-tests all three outcomes above.

### Price list and fallback

Customer default -> Customer Group default -> `Selling Settings.selling_price_list`.

Two ERPNext guards differ and both are mirrored rather than tidied:

* **variant -> template** falls back only when the rate `is None`; a stored 0 is a
  real answer and stops there (`get_item_details.py:1043`);
* **selected list -> default list** falls back when the rate is falsy, so a stored 0
  DOES fall through when `fallback_to_default_price_list` is on
  (`get_item_details.py:125`). Verified by test.

### Contract summary

* scope: `scope_type=category` only; `collection`/`all` answer `unsupported_scope`.
  A group category answers `category_not_listable` -- no descendant recursion.
* search: `item_name` only, whitespace-split, **AND** across words, `%`/`_` escaped.
* sort: `name_asc` (default) | `name_desc` | `newest`, each with the Item `name` as
  tiebreak. Never `modified` (it reshuffles on edit and breaks the cursor), never price.
* filters: must be absent or empty; anything else answers `unsupported_filters`.
* page_size: 1..48, default 24; out-of-range is refused, not clamped.
* pagination: opaque keyset cursor bound to scope + search + sort + customer +
  price list. It is never an authorization mechanism -- category and customer are
  re-authorised on every request.
* response: `{items[], pagination{returned_count, page_size, has_more, next_cursor}}`.
  `has_more=true` means either another Item survived the FULL pipeline, or the scan
  budget was spent with candidates still unexamined -- never merely that another raw
  row exists. `returned_count` may be less than `page_size`, or 0, while `has_more`
  is still true; only `has_more=false` with `next_cursor=null` means the end. See
  "Candidate scanning and continuation" above.

ERPNext remains the final pricing authority; YOB only decides which items to price.

## Row-level tax on `pricing_rows` (Phase 23B-3)

`cart.pricing_rows` is authoritative for row **price and row tax**. Each row gains:

```text
tax_amount        net tax attributable to that Sales Order row
total_amount      net_amount + tax_amount
tax_components[]  tax_type, label, rate, amount, taxable_amount,
                  included_in_print_rate, charge_type
```

### Where the numbers come from

`calculate_taxes_and_totals` leaves `doc._item_wise_tax_details` on the priced
Sales Order -- one entry per (item row, tax row) pair. YOB reads that and nothing
else. **YOB never calculates GST**: it applies no percentage, infers no
CGST/SGST/IGST from addresses, and decides no jurisdiction. ERPNext and India
Compliance own all of it.

### Row identity, not `item_code`

Entries carry the actual item ROW OBJECT, so extraction groups on `id(item_row)`.
`item_code` would merge a paid row's tax onto its same-SKU promotion row.
ERPNext's own `get_itemised_tax()` keys by `item_code`, which is exactly why it
cannot be reused here.

### Currency

`_item_wise_tax_details` amounts are **base/company currency** -- built at
`base_tax_amount` precision, and `adjust_rounding_in_item_wise_tax_details`
reconciles them against `tax.base_tax_amount_after_discount_amount`. They are
divided by `conversion_rate` and rounded once at the transaction precision.
Returning them raw would put company-currency tax beside a transaction-currency
rate. Storefront transactions are single-currency today (company currency ==
cart currency), so multi-currency remains **untested and unsupported**.

### Inclusive tax

`total_amount = net_amount + tax_amount`, **never `amount + tax_amount`**. For an
inclusive 18% on a rate of 100 the row total is 100, not 136 -- `amount` already
contains the tax while `net_amount` is the taxable base under both treatments.

### GST classification

`gst_tax_type` is read from India Compliance, never from account-name matching.
IC sets it during document **validate**, which the pricing Sales Order never runs
(it is in-memory and never inserted), so the projection calls IC's own
`set_gst_tax_type()` first. That is metadata population, not classification.

A charge IC does not classify keeps `tax_type: null` and its description as
label -- numerically correct and never mislabelled as GST.

> On a **GST-unregistered** company IC returns no classification at all
> (`ignore_gst_validations` short-circuits to an empty account map). The current
> dev company is `gst_category: Unregistered` with no GSTIN, so component types
> are legitimately `null` there. Numeric tax parity is asserted regardless; the
> CGST/SGST vs IGST split assertions skip with that reason rather than passing by
> luck.

### What stays where

* Promotion rows carry their **own** ERPNext-derived tax -- never copied from the
  paid row, never assumed zero.
* Cart Item tax fields remain a **non-authoritative snapshot**.
* No tax is persisted as customer intent; the projection is rebuilt every reprice.
* **Cart summary remains the document total**, not a sum of row totals: documents
  carry rounding, additional discount and document-level charges.

## Owned DocTypes

Known Storefront-owned DocTypes from the reviewed archive:

- `YOB Store Settings`
- `Category`
- `Cart`
- `Cart Item` — Child DocType; no standalone navigation
- `Payment Method`
- `Payment Method Assignment`
- `Razorpay Payment Log`

Inspect current JSON flags, fields, indexes, permissions, row counts, and module
ownership before implementation. ERPNext `Sales Order` and Payments `Payment
Request` are dependency-owned transactions, not Storefront DocTypes.

## Navigation

- The `YOB Storefront` Apps Page icon opens the primary Storefront Workspace.
- Every additional Desk-visible Storefront module owns its own Workspace and
  sidebar context.
- Child/internal DocTypes are not independently linked unless direct admin
  access is approved.
- The historical Workspace label `YOB` may require a compatibility migration;
  do not rename its stored identity only for cosmetic consistency.

## Compatibility

Existing Storefront dotted API paths, parameters, methods, envelopes, statuses,
and published error-code values remain compatible unless an approved versioned
breaking change supplies a transition plan. See `contracts/`.
