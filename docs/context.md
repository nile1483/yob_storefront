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
         end_of_life, item_name/item_code search, keyset cursor, plus a BROAD `EXISTS` for
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
  `scope_value` is OPTIONAL since Phase 28A: omitted, the listing browses the whole
  public catalogue (the `/products` scope). It is the ABSENCE of a category, not
  the reserved `all` scope type, and it changes nothing but the scope -- one
  pipeline, so a product cannot be public catalogue-wide and invisible in its own
  category.
* search: `item_name` **OR** `item_code`, whitespace-split, **AND** across words,
  `%`/`_` escaped. One shared predicate, so the listing and the header typeahead
  can never describe different product universes (Phase 26A-1).
* sort: `name_asc` (default) | `name_desc` | `newest`, each with the Item `name` as
  tiebreak. Never `modified` (it reshuffles on edit and breaks the cursor), never price.
* filters: must be absent or empty; anything else answers `unsupported_filters`.
  `storefront_filters` requires a category and answers
  `storefront_filter_context_required` without one -- which facets exist is a
  property of a category (Phase 25C), and a global facet set would be a second
  filtering system.
* page_size: 1..24, default 24; out-of-range is refused, not clamped. Default and
  maximum are the same number since Phase 28A -- a page is a fixed unit of work and
  more products come from the cursor, never from a bigger page. It was 1..48.
* pagination: opaque keyset cursor bound to scope + search + sort + customer +
  price list. It is never an authorization mechanism -- category and customer are
  re-authorised on every request. The scope is part of that binding, so a
  catalogue-wide cursor replayed against a category answers `cursor_invalid`.
* response: `{items[], pagination{returned_count, page_size, has_more, next_cursor}}`.
  `has_more=true` means either another Item survived the FULL pipeline, or the scan
  budget was spent with candidates still unexamined -- never merely that another raw
  row exists. `returned_count` may be less than `page_size`, or 0, while `has_more`
  is still true; only `has_more=false` with `next_cursor=null` means the end. See
  "Candidate scanning and continuation" above.

ERPNext remains the final pricing authority; YOB only decides which items to price.

### Browse category chips (Phase 28A)

`catalog.get_browse_categories()` answers every enabled Storefront Category, FLAT,
at every depth: `name`, `category_name`, `slug`, `parent_category`, `is_group`,
`display_order`, `level`.

It exists because `get_categories` answers ONE level at a time -- roots, or the
children of one `parent_slug` -- which serves tree navigation but would take one
request per node to draw a chip row. Neither shape fits the other, so the two sit
beside each other rather than one changing.

Metadata ONLY: no Item query, no price, no stock, no SellingContext, no listing
pipeline. Two indexed reads over `tabCategory`.

**Every row is a valid `get_items` target.** That is the contract: a chip a buyer
can click must be a category the listing will answer for, so the three conditions
`get_items` itself applies are applied here.

* **`is_active = 1`.** Decided per category: a disabled PARENT does not
  additionally hide an enabled child, because `get_categories`, `get_category`
  and `get_items` all read the category's own flag and none of them cascades.
* **a slug.** The public identity `get_items` resolves -- the same rule that keeps
  unrouted Items out of the listing and unrouted categories out of a menu
  destination.
* **`is_group = 0`.** A group holds sub-categories, and `get_items` refuses one
  with `category_not_listable`. Publishing it would hand a client a chip that
  fails when clicked.

Excluding groups does NOT narrow the answer to one level: a listable category at
any depth is published whatever its ancestors are, and only the non-listable nodes
drop out. **No aggregation is implied** -- a group is never republished as a chip
that lists its descendants, because a category scope is exactly one category and
`get_items` has no descendant recursion.

`is_group` is deliberately NOT in the payload: every row is listable by
construction, so the flag could only be 0, and publishing a constant invites a
client to branch on a case that cannot occur. Adding it back if group chips ever
gain a meaning is additive; removing it later would not be.

`parent_category` may name a category that is not itself published -- a listable
child of a group or of a disabled parent keeps its real parent. It is a grouping
key, not a chip reference.

`level` is computed over the FULL tree, groups and disabled ancestors included, so
a depth is a fact about the taxonomy rather than about which nodes are listable
today.

**No synthetic `All`.** Catalogue-wide browsing is the absence of `scope_value`,
not a category the merchant owns.

## Item Price storefront metadata (Phase 29A)

Three optional custom fields on ERPNext **Item Price**, installed through
`install.ensure_custom_fields()` like every other YOB field on a dependency
DocType:

| Field | Type | Purpose |
| --- | --- | --- |
| `custom_moq` | Float | starting/minimum storefront quantity |
| `custom_quantity_multiplier` | Float | storefront quantity STEP from that start |
| `custom_mrp` | Currency (`options: currency`) | Maximum Retail Price, display only |

On **Item Price** rather than Item because all three are properties of a PRICE: a
customer-specific price list may carry a different minimum, step and MRP for the
same SKU. On Item they would force one answer for every customer.

`custom_mrp` reuses the Item Price's own `currency` field. A second currency
field could disagree with the first, and this value is never converted.

### Two purposes, deliberately not one feature

    MOQ + Quantity Multiplier  ->  storefront quantity-input GUIDANCE
    MRP                        ->  informational display ONLY

The multiplier is an INCREMENT FROM THE START, not a divisor: `moq 10` with
`multiplier 6` means 10, 16, 22 -- not 12, 18, 24, and not "divisible by 6".

None of the three is UOM, conversion-factor or pack-size metadata, and none
touches stock, warehouse or reservation.

### Which Item Price

The row ERPNext actually priced against -- never "any row for this SKU".

ERPNext discards that identity: `get_price_list_rate_for()` reads
`get_item_price()[0]` and returns only `price_list_rate`, and the Sales Order
Item it fills has no field naming the Item Price. So the temporary Sales Order is
authoritative for the RATE and silent about the SOURCE.

`pricing_service.resolve_item_price_source()` recovers it by calling ERPNext's
own `get_item_price()` -- the same function, so the ranked pick
(customer-specific before generic, latest `valid_from`, batch, then UOM,
`LIMIT 1`) stays ERPNext's and is not reimplemented. Only the two-step ladder
around it is mirrored, and each step mirrors a specific ERPNext line: retry in
`stock_uom` (`get_item_details.py:1280`) and fall back variant -> template
(`get_item_details.py:1043`).

> The variant -> template fallback is near-unreachable in practice: ERPNext's own
> `ItemPrice.validate` refuses a price on an item with `has_variants`, the same
> constraint that stops a family card carrying a price. It is mirrored anyway,
> because a template that acquired a price BEFORE it became one is a real stored
> state. `test_erpnext_refuses_an_item_price_on_a_template` pins the constraint.

### `quantity_control.allowed`

`False` exactly when the authoritative pricing preview attached **at least one
Pricing Rule** to the row -- read from the same `pricing_rules` that already
produces `pricing_rule_label`. ERPNext funnels every promotional mechanism
through that field (rate/discount rule, promotional scheme, Product Discount,
free-item rule), so one check covers them all without this module knowing what
any of them are.

The reason is quantity, not price: a rule that changes behaviour at a threshold
makes "start at 10, step by 6" a claim the storefront cannot honour. Deliberately
NOT a prediction engine -- answering "would a rule apply at 16?" means evaluating
hypothetical quantities through ERPNext's rule stack, which is the unbounded work
Phase 22B removed.

MOQ and the multiplier are a PAIR under one flag; both are still published when
`allowed` is false, for transparency. MRP is INDEPENDENT: informational, no
quantity behaviour, therefore no conflict.

### The architectural boundary

**No backend enforcement, anywhere.** Nothing in `get_item`, `resolve_variant`,
`add_to_cart`, cart update, checkout or Sales Order consults MOQ or the
multiplier. A quantity below MOQ or off the step sequence behaves exactly as it
did before these fields existed, and no `minimum_order_qty` /
`invalid_quantity_step` error code exists.

**MRP never reaches pricing.** Changing it alone leaves base price, rate,
discount, tax and total identical, and no saving or percentage is derived from
it.

`NoBackendEnforcementCase` and `MRPIsInformationalCase` in
`tests/test_item_price_guidance.py` exist to keep both true.

### Where it is published

`ProductDetail` -- so `catalog.get_item` (simple) and `catalog.resolve_variant`
both carry it, and selecting a variant re-resolves it with the price. NOT
merchandising, so `resolve_variant` still carries no gallery or sections.

`get_item_pricing(..., with_price_metadata=True)` is opt-in: the product-detail
serializer asks for it, the catalogue listing does not. It costs one ranked
lookup plus one row read per item -- nothing on a product page, 48 extra queries
on a 24-card listing. Listing cards, suggestions and browse chips are unchanged.

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

## Warehouse and transaction context (Phase 23B-5W)

### The rule

> **Warehouse is trusted server/ERPNext-derived transaction context. The
> storefront buyer cannot select or control warehouse. YOB supplies a
> server-side default only if ERPNext requires one and cannot derive one.**

Buyer warehouse selection is **not** a deferred YOB feature and is not planned.
It is not on the deferred list beside selectable UOM, per-line duplicate-SKU
intent, per-line expiry and multi-currency; there is nothing here to defer.

Today ERPNext derives a warehouse on every bench we run, so YOB supplies none at
all -- the "server-side default" clause above is a contingency, not current
behaviour.

### The one resolution path

```text
get_item_details -> get_basic_details -> get_item_warehouse_

  Sales Order `set_warehouse`
    -> Item Default (per company)
      -> Item Group default
        -> Brand default
          -> the row's own warehouse
            -> Stock Settings default (only if it belongs to the same company)
```

Every storefront surface goes through it, and none of them reimplements it:

| Surface | How it gets the warehouse |
| --- | --- |
| Product preview | the temporary Sales Order's own row, after `set_missing_values()` |
| Cart pricing | the full-cart temporary Sales Order's row, same call |
| Draft Sales Order | the committed order's row, same call |
| Displayed stock | `SellingContext.resolved_warehouse()`, which ASKS `get_item_details` |

The rows YOB builds carry no `warehouse` and no `set_warehouse`; ERPNext fills
them in. `services/pricing_service.py` and `services/order_service.py` contain
neither string, asserted by test.

**Verified on `test.localhost`:** all four resolve `Stores - ST` for the seeded
stock item, and an item pointed at a different warehouse moves all four together.
Quantity does not affect it (preview prices qty 1, the cart prices the buyer's
quantity).

### `YOB Store Settings.default_warehouse` is inert

The field exists and `cms.get_config` publishes it, but **no pricing, cart, order
or availability path reads it**. Pointing it elsewhere changes nothing, which is
pinned behaviourally rather than by a source scan. It is a published response
field, so it stays for compatibility; it must never become a warehouse authority
beside ERPNext's own.

### Availability is three-valued

| Value | Meaning |
| --- | --- |
| `None` | does not apply (non-stock) or unknown (ERPNext resolved no warehouse) |
| `0.0` | a real answer: we have none |
| `n` | the quantity in the warehouse this transaction would draw on |

`None` must never be rendered as "out of stock". A variant reports its **own**
SKU (never its template's), and a template with variants is not transactable, so
ERPNext declines to describe it and quantity is `None`.

> **Fixed in 23B-5W.** The quantity is now read with ERPNext's own
> `get_bin_details(item_code, warehouse, include_child_warehouses=True)` -- the
> call its Sales Order line makes. A raw `Bin` read was wrong whenever ERPNext
> resolved a **group** warehouse: the order line reported the aggregate of the
> group's children while the product page showed 0 for the same warehouse
> (reproduced: ERPNext 9, storefront 0). For a leaf warehouse
> `get_child_warehouses()` returns just that warehouse, so the ordinary case is
> unchanged. This decides no precedence; it only reads the warehouse ERPNext
> already chose.

### When ERPNext genuinely requires one

A stock line with no resolvable warehouse fails at Sales Order **validate**
(`SalesOrder.validate_warehouse` -> `WarehouseRequired`). Preview and cart
pricing are in-memory and never validated, so browsing and pricing still work and
availability reports `None`; the **commitment refuses**, leaving no Sales Order
and the Cart still `Draft`.

YOB deliberately does not invent a warehouse to fill that gap. Any value it chose
would be a second precedence chain, and shipping from a warehouse the merchant
never nominated is worse than refusing. The fix is merchant configuration -- an
Item, Item Group, Brand or Stock Settings default.

### Preview vs Cart: one transaction, two builders

The preview and the cart do not share one `SellingContext` INSTANCE -- the
preview builds from `YOB Store Settings` plus `get_price_list_for_customer()`,
the cart goes through `context_for()`. What matters is whether they can ANSWER
differently, so each dimension is compared on the finished orders:

| Dimension | Can they diverge? |
| --- | --- |
| customer | No -- the cart is looked up by the same authenticated Customer |
| company / currency | Not today. Both come from `YOB Store Settings`; the cart prefers its own stored value, which `get_or_create_cart` wrote from the same settings. Nothing re-resolves them later, so a store that changed company or currency would leave old carts behind -- bounded by the single-company, single-currency storefront and the deferred multi-currency boundary |
| transaction date | No -- both `today()` |
| price list | No -- one resolver, re-resolved and written back on every reprice (23B-1) |
| fallback price list | No -- both leave it to ERPNext and Selling Settings |
| warehouse | No -- ERPNext resolves it in both, asserted above |
| UOM | No -- ERPNext resolves the selling UOM in both, and the Cart records rather than dictates it (23B-5U, below) |

### Selling UOM (fixed in Phase 23B-5U)

23B-5W reproduced this: Item `sales_uom = Box`, factor 10, Item Price 100/Nos --
the product page quoted **1000 per Box** while the Cart and Draft Sales Order
charged **100 per Nos** for the same buyer input.

**Root cause.** `add_to_cart` wrote `uom = stock_uom` (and
`conversion_factor = 1`) onto the Cart row, and both
`calculate_cart_using_sales_order` and `create_sales_order_from_cart` passed
`row.uom or row.stock_uom` back to ERPNext. With a UOM already in context,
`get_basic_details` has no decision left to make -- YOB had overridden it.

**What replaces it.** Nothing in YOB derives a unit:

```text
add_to_cart        appends a row with NO uom and NO conversion_factor
  -> reprice       ERPNext resolves `sales_uom or stock_uom` itself
  -> sync          the resolved uom / conversion_factor / stock_uom are RECORDED
  -> later prices  that recorded unit is sent back, so the meaning holds
```

`cart_row_to_order_item()` builds the row for **both** the pricing order and the
committed order, so they cannot be built on different units. It sends the unit
only when the row already has one, and **never sends a conversion factor** --
ERPNext re-derives that from the Item's UOM table on every calculation, and
`stock_qty = qty * conversion_factor` with it.

| Fact | Who decides |
| --- | --- |
| selling UOM of a new line | ERPNext (`sales_uom or stock_uom`) |
| selling UOM of an existing line | recorded ERPNext answer, held steady |
| conversion factor | ERPNext, re-derived every reprice |
| `stock_qty` | ERPNext (`qty * conversion_factor`) |
| Pricing Rule min/max qty | ERPNext, against `stock_qty` |
| free-item unit | ERPNext (`pricing_rule.free_item_uom or stock_uom`) |
| quantity | **the buyer** |

The buyer sends quantity and nothing else. No storefront endpoint accepts `uom`,
`conversion_factor`, `stock_qty`, `warehouse`, `price_list` or `rate`, asserted
by an endpoint scan.

### Verified behaviour

* **Box, factor 10, 100/Nos** -- preview, Cart intent, cart pricing order and
  Draft Sales Order all read `Box`, factor 10, `stock_qty = qty * 10`, 1000 per
  Box. Two Boxes = 2000, Cart == Draft Sales Order.
* **No `sales_uom`** -- everything resolves to the stock UOM, factor 1.
* **Item Price in the stock UOM** -- ERPNext multiplies by the factor (100/Nos
  becomes 1000/Box).
* **Item Price in the selling UOM** -- the exact-UOM price wins as-is (900/Box
  beats the converted 1000).
* **Pricing Rules** -- a `min_qty = 10` rule fires on ONE Box, because ERPNext
  compares `stock_qty`; a `min_qty = 11` rule does not.
* **Variants** -- a variant sells in its own `sales_uom`, priced on its own SKU.
* **Promotions** -- the free row uses `free_item_uom or stock_uom`, which is
  ERPNext's rule, so a same-SKU promotion on a Box item arrives in **Nos** unless
  the Pricing Rule says `free_item_uom = Box`. YOB does not override it: forcing
  the paid row's unit onto a free row would invent a quantity ERPNext never
  granted. Merchants who intend "buy 2 Boxes get 1 Box" must set
  `free_item_uom` on the rule.
* **Stock availability is NOT converted.** Price per Box, availability in Nos:
  `actual_qty` stays in stock units and is labelled with `stock_uom`. Responses
  carry `uom`, `stock_uom`, `conversion_factor` and `stock_qty` so a client
  renders "2 Strips" and "125 Nos available" without arithmetic of its own.

### An existing Cart when the merchant changes the unit

A stored quantity must never quietly come to mean something else, so the
recorded unit is what the row keeps:

| Merchant action | Existing Cart line | Reported |
| --- | --- | --- |
| changes `sales_uom` Box -> Nos | stays **2 Box**, still 2000 | nothing changed |
| edits the Box factor 10 -> 12 | stays 2 Box, ERPNext reprices to 1200/Box | `uom_changed_items` |
| deletes the Box conversion | ERPNext values a Box at 1 stock unit, as it does for any Desk-entered draft in that state | `uom_changed_items` |

New shoppers immediately get the merchant's new unit; only the already-chosen
line holds its own. `uom_changed_items` is an additive Cart-response list and is
empty in normal operation; `cart_updated` becomes true when it is not.

Because `uom` and `conversion_factor` are part of the payment fingerprint, a unit
change under a live checkout link answers `payment_request_stale` and refuses the
commitment rather than billing a different quantity meaning.

### The Add-to-Cart merge guard (Phase 23B-5U-1)

> **A Cart line keeps the ERPNext selling UOM established when that buyer intent
> was first priced. If the merchant later changes the item's authoritative
> selling UOM, YOB does not reinterpret or convert the existing quantity. A
> subsequent Add-to-Cart for that SKU is rejected until the existing line is
> removed and re-added.**

This is an INTEGRITY rule, not a UOM-selection feature. The buyer still never
chooses a unit; ERPNext decides it on the fresh line.

The case it closes: a line holding `2 Nos`, a merchant who switches the item to
`Box` (1 Box = 10 Nos), and a buyer who then types `2` on a product page that now
reads Boxes. Merging would file 2 Boxes as 2 Nos.

```text
add_to_cart finds an existing line for this SKU
  -> SellingContext.resolved_selling_uom(item)   <- ERPNext, same call the order uses
  -> same as the line's recorded uom ?  merge the quantity
  -> different ?                        409 cart_item_uom_changed, NOTHING mutated
```

Every other answer was rejected on purpose: converting rewrites intent the buyer
already gave, a second row would need duplicate-SKU carts (not in this
architecture), and silently merging is the defect itself.

`details` carries `item_code`, `existing_uom` and `current_uom` -- display
metadata only, no server internals. A line with no recorded unit has not been
priced yet and has nothing to protect; ERPNext declining to describe the item
means "no comparison possible", never a mismatch.

Rows created before 23B-5U already carry `uom = stock_uom`, so they keep exactly
the meaning they were created with. No patch reinterprets them, by design.

## Address changes and pricing freshness (Phase 23B-5W)

`set_cart_billing_address` and `set_cart_shipping_address` save the link and do
**not** reprice. Jurisdiction can decide the tax template, so the guarantee that
matters is that no stale financial state reaches a commitment. It holds by two
independent mechanisms, neither of them the setter:

1. **Issuance.** `proceed_to_payment` reprices under the Cart row lock and issues
   the Payment Request against that recalculated state. A deliberately corrupted
   stored total does not survive it.
2. **Commitment.** `ensure_payment_request_committed` re-reads and re-prices the
   Cart in memory (`validate_payment_request_source_current`) and compares a
   fingerprint that includes `billing_address`, `shipping_address` and
   `contact_person` as well as the money. An address changed after issuance
   answers `payment_request_stale`; no Sales Order is created and the Cart stays
   `Draft`. Re-running Proceed re-prices, re-issues and then commits the new
   obligation.

The buyer's own view refreshes on the next `get_cart`, which reprices and saves.
An address change with no money change is still stale: a different delivery
address is a different order.

## Variant products (Phase 24A audit, Phase 24B build)

Buyers choose **attributes and quantity**. Never a UOM, warehouse, conversion
factor, price list, rate or SKU string. Once attributes resolve to an actual
variant SKU, every Phase 23 guarantee applies unchanged — the same
`SellingContext`, the same temporary Sales Order, the same Cart and Draft Sales
Order paths. **There is no variant-pricing engine.**

```text
family slug -> catalog.get_item        -> attributes[] + variants[]   (NO price)
buyer picks -> catalog.resolve_variant -> find_variant -> revalidate -> full detail
            -> cart.add_to_cart(item_code, qty)        -> revalidated again
```

### What ERPNext provides (verified, `test_variant_catalog.py`)

| Concept | Where it lives |
| --- | --- |
| family | `Item.has_variants = 1`, `variant_based_on` = `Item Attribute` \| `Manufacturer` |
| selectable attributes | `Item Variant Attribute` rows on the TEMPLATE, in `idx` order, each possibly `numeric_values` |
| attribute values | `Item Attribute Value` — **global to the attribute**, and an ORDERED child table |
| an actual variant | `Item.variant_of = <template>` plus its own `Item Variant Attribute` rows |
| attributes → SKU | `erpnext.controllers.item_variant.find_variant` |
| SKU naming | `make_variant_item_code` — **never reproduced by YOB or a client** |

* `find_variant` needs the COMPLETE attribute set; a partial set and a
  never-generated combination both answer `None`.
* **Attribute values are global**, so a cross-product lies: Red/M and Blue/L
  existing does not make Red/L real. The matrix is built from actual variant rows.
* A template **cannot carry an Item Price** and cannot be priced. There is no
  family price to show before a selection.
* A variant keeps the template's **stock** UOM (`allow_different_uom` off) but may
  have its own `sales_uom`; price, stock and availability are per SKU.
* Pricing Rules reach a variant four ways — own code, TEMPLATE code (ERPNext
  matches through `variant_of`), Item Group, Brand — and a Product Discount may
  grant a variant, in `free_item_uom or stock_uom`.

### The contract

**Family page** (`get_item` on a template slug) carries `is_template: 1`,
`is_purchasable: 0`, `attributes[]` in template order with values **restricted to
those occurring in salable variants**, and `variants[]` — one row per actual
salable variant with its exact attribute map. No money fields exist on it.
Values keep `Item Attribute Value` order, so sizes read S, M, L rather than
alphabetically.

**Resolution** (`resolve_variant(template, attributes, qty)`) answers the SAME
payload as a simple product page plus `variant_of` and `selected`, so one
serializer (`api/catalog.build_item_detail`) serves both and they cannot drift.
`selected` is read from the resolved variant's stored rows, never echoed from the
request. Errors: `variant_attributes_required` (incomplete),
`variant_not_available` (no such salable combination — never resolved to a
neighbour), `variant_family_unsupported` (Manufacturer-based).

**Listing**: one card per simple Item and **one per family**, never one per
variant. Every card declares `has_variants` and `price_state`
(`priced` | `select_options`); a family card's money fields are all `null`. A
family is listable when at least one of its variants is catalogue-eligible, tested
with an early exit — no family card ever runs the pricing engine, and Phase 22B's
cursor and page size are untouched.

**Add to Cart** still takes `item_code` + `qty`. The code is revalidated
server-side before anything is read or written: exists, `has_variants = 0`,
enabled, `is_sales_item`, in life, and `variant_of` still pointing at a real
family. A template answers `item_is_template` (422); an unsalable SKU answers
`item_not_purchasable` (422). Neither ever becomes Cart intent.

### Slugs

A public slug addresses a **simple Item or a family**. Variants have none:
`custom_slug` was `reqd` and listed in `Item Variant Settings`, so ERPNext copied
the template's slug onto every variant and `get_item(slug)` answered with an
arbitrary sibling (Phase 24A). Patch
`v1_0.stop_copying_item_slug_to_variants` removes it from that list, drops `reqd`
and clears any inherited copy; an `Item.validate` hook refuses a duplicate
non-empty slug (a unique index cannot be used — unslugged Items all store the
same empty string). The catalogue only lists rows that have a slug, so nothing
unroutable is offered.

### The published contract (Phase 24D-1)

The canonical reference is `frontend-api-handoff/` — markdown, `openapi.json`
(**3.4.1** today; **3.3.0** when Phase 24D-1 published it),
`postman_collection.json` and examples — mirrored into the Angular
repo at `docs/api-handoff/` and `reference/api/`. Phase 24D-1 published what
production actually does: `catalog.resolve_variant` (new), `catalog.get_item`'s
two discriminated modes, `catalog.get_items` (which had shipped in Phase 22B
without ever being documented), the Add-to-Cart SKU refusals, and the seven
Phase 22B listing error codes that had never been published either.

The OpenAPI examples are **captured from a real run**, not hand-written.

`test_response_contract.TestPublishedApiReference` is the guard: every
whitelisted endpoint must appear in `openapi.json`, the reference may not
describe an endpoint that no longer exists, and every storefront error-code
constant must appear in `ERROR-CODES.md`. It found the two gaps above on its
first run.

The wire, verified against production rather than assumed:

```
GET /api/method/yob_storefront.api.catalog.resolve_variant
      ?template=TEE&attributes=<URL-encoded JSON>&qty=1
```

`attributes` arrives as a STRING and is parsed server-side; a mapping is accepted
too. Malformed JSON answers `variant_attributes_required`, never a 500.

### Re-anchoring needs no server state

A client that clears an attribute made incompatible by another choice needs
nothing from the server: the matrix is presentation guidance, the resolver only
answers a COMPLETE selection, and Add to Cart revalidates the resolved SKU again.
`variant_service` performs no write on any read path (asserted), and repeated
partial selections leave nothing behind (asserted by record counts).

### Manufacturer-based families are unsupported, by decision

`variant_based_on = "Manufacturer"` distinguishes variants by manufacturer part
number and has no attribute selector to render. YOB **fails closed**: such
templates are excluded from the catalogue, their family page answers
`variant_family_unsupported`, and `resolve_variant` refuses them. Nothing about
Item Attribute families is weakened, and no semantics are invented for a mode the
storefront cannot present.

### Numeric attributes

Offered exactly like any other: the values that OCCUR in generated variants. YOB
never expands `from_range`/`to_range`/`increment` into combinations — ERPNext
generates variants, YOB reports them.

## Storefront administration: navigation, filters, content (Phase 25B)

Admin and data model only. The facet projection, `get_menu` and the page/block
projection are Phase 25C, below.

### Twelve app-owned DocTypes

| Group | DocTypes |
| --- | --- |
| Filters | `YOB Storefront Filter`, `YOB Storefront Filter Value`, `YOB Storefront Filter Set`, `YOB Storefront Filter Set Filter` (child), `YOB Storefront Item Filter` (child) |
| Navigation | `YOB Storefront Menu`, `YOB Storefront Menu Item` (tree) |
| Content | `YOB Storefront Page`, `YOB Storefront Page Block` (child), `YOB Storefront Block`, `YOB Storefront Block Slide` (child), `YOB Storefront Block Promo Card` (child) |

All prefixed, all module `yob_storefront`, all synced by `bench migrate`.
`Filter Value` is a **master**, not a child: the prototype's child-row Link had no
referential protection and stored display text, so renaming a value broke it.

### Two Filter Sets, two jobs

`Item.custom_storefront_filter_set` is an ADMIN SCOPE — the Filters an
administrator may attach to that product. `Category.storefront_filter_set` is
DISPLAY — the Filters that category's listing will expose. **They are never
required to match**, and a narrow category set never erases richer item metadata.

### Integrity, enforced on the server

`Item.validate` (never a Client Script — Data Import, the REST API and
`bench execute` all bypass one): every row's Filter must be in the Item's set;
every Value must belong to its Filter; a disabled Filter or Value cannot be
*newly* assigned while existing rows survive untouched; the exact (Filter, Value)
pair may not repeat; several different values under one Filter are fine.
Uniqueness on Filter Value is `(filter, value)` and `(filter, value_key)` —
`Colour/Red` and `Paint Finish/Red` coexist.

### Where filters live

Simple Item or variant **template** — whatever the catalogue lists. A generated
variant is refused with a message naming its template, so merchants never
duplicate facets onto every SKU and a silent no-op is impossible. ERPNext variant
attributes remain a separate system.

### ERPNext Item Group is not storefront taxonomy

Item Group stays internal ERP and pricing structure (Pricing Rules keep using it,
Phase 23 unchanged). It is never a storefront category, navigation destination,
filter taxonomy or Product Grid source. Storefront `Category` is authoritative.

### Content blocks

Five types — `Image Banner`, `Rich Text`, `Banner Carousel`, `Product Grid`,
`Promo Grid`. Not `Offer Grid`: in YOB an offer is an ERPNext Pricing Rule.
Fields of the other four types are **cleared on save**, so a block that changed
type cannot keep a stale image or category that a projection might read. Rich
Text is sanitised on save with Frappe's own cleaner — Angular's sanitizer is the
last line of defence, not the only one.

**Destinations are typed and shared with navigation.** A merchant picks a type
(Catalog · Storefront Category · Storefront Page · Product · External URL for a
content block; Home · Catalog · All Products · Storefront Category ·
Storefront Page · External URL for a menu item) and a record; nobody types an
Angular route, and route construction belongs to the Phase 25C projection, never
to Desk JavaScript. A Product destination accepts a
simple Item or a variant FAMILY with a slug and refuses a generated variant —
Phase 24 family routing stays authoritative. An External URL is either an
absolute http(s) URL or a single-leading-slash INTERNAL route -- `validate_destination()`
has accepted both since Phase 25B, and `//host` is refused because a browser reads
it as scheme-relative and would leave the storefront.
`utils.storefront_content.apply_destination()` is the single validator, used by
menu items, banners, carousel slides and promo cards alike. Slide and card order
is the Frappe child-row `idx`; there is no second ordering field.

A Product Grid stores a **bounded query**: one storefront Category, 1–12 items,
and only sorts the catalogue can do without pricing every candidate (price
sorting is deliberately absent). A Page holds at most three of them, enforced
when the page is saved rather than when a buyer opens it. Phase 25C runs them
through the existing `list_items()`, so grids inherit Phase 22–24 behaviour whole.

### Installation

`install.ensure_custom_fields()` now owns every Item field — `custom_slug`,
`custom_category`, the Storefront tab, `custom_storefront_filter_set` and
`custom_storefront_filters` — and runs on `after_install` **and** `after_migrate`.

> The first two were previously created BY HAND. The Custom Field fixture in
> `hooks.py` is commented out and nothing else installed them, so a fresh install
> had no slug and no category field and the whole Phase 22–24 catalog silently
> could not work. Found in Phase 25A, fixed here, proved by test.

`custom_slug` is installed **not required** (Phase 24B: generated variants carry
no slug). Desk behaviour ships as app-owned files through `doctype_js` /
`doctype_tree_js`, not Client Script records. The existing Workspace gains
`Catalog Filters`, `Navigation` and `Content` sections — no second workspace.

## Storefront runtime APIs (Phase 25C)

Three read endpoints plus one additive parameter, all on the existing YOB API
boundary. Nothing here prices, queries or resolves anything itself.

| Endpoint | Answers |
| --- | --- |
| `cms.get_menu(menu_key)` | a published navigation tree, at most two levels |
| `catalog.get_category_filters(scope_value)` | the facets a category page should display |
| `catalog.get_items(..., storefront_filters)` | the same listing, narrowed |
| `cms.get_page(slug)` | a published page as ordered, discriminated blocks |

### One destination projection

`services/storefront_destination.project_destination()` turns a stored type plus
a record link into `{type, target, href, external, open_in_new_tab}` — used by
menu items, banners, carousel slides and promo cards alike, so the four cannot
drift. `target` is a **public slug**; `link_category`/`link_page`/`link_item` are
database identity and never leave the server.

`None` means "not clickable" **and** "the target is no longer publishable" — a
category disabled after linking, an unpublished page, a product that lost its
slug, or a stored URL that is neither a safe internal route nor http(s). A dead
link is never shipped.

### Fixed-route destinations (Phase 28C)

`Home`, `Catalog` and `All Products` carry NO target field: the route is fixed by
the type, so `TYPE_MAP` pairs them with a `None` field and `IMPLIED_ROUTES` holds
the answer -- `/`, `/catalog`, `/products`. They project a null `target`, a
backend-owned `href` and `external: false`, and they are the only destinations
that can never answer `None`, because there is no record underneath them to be
disabled or deleted.

`All Products` exists because a merchant wanting `/products` previously had to
pick `External URL` and type the route. That works and still works, but the admin
label said "External URL" for a page that is not external, and the route was
merchant input rather than a contract. The two are deliberately DIFFERENT stored
destinations projecting different machine types onto the same `href`: one is a
fixed contract, the other is input that must be re-validated on every projection.

It is registered in the SHARED `TYPE_MAP`, so the projector answers it on every
surface. Only the MENU Select offers it, exactly as `Home` has always been
menu-only -- `TYPE_MAP` is the superset and each admin surface curates its own
subset. Offering it on content blocks later is a Select change with no projector
work.

The visible menu label stays entirely the merchant's: the type fixes the
destination, never the wording.

`ROUTE_TYPES` in the Menu Item controller and `IMPLIED_ROUTE_TYPES` in
`storefront_content` both name these types; `apply_destination()` clears every
other type's target field before returning, so switching a configured item to
`All Products` leaves no stale target behind. That is the existing convention for
`Home` and `Catalog`, not a new one.

**An `external_url` destination is not necessarily external (Phase 28A).** The
field accepts an internal route as well as an absolute URL, so the type says what
was STORED and `external` says where it GOES: `false` for an in-app route the SPA
router owns, `true` for a link that leaves the storefront. Clients switch on
`external`, never on the type.

> The projector previously demanded a scheme AND a netloc, so a route a merchant
> had legitimately saved projected as `None` and the menu item silently
> disappeared. Save and runtime disagreed; Phase 28A made them agree. Every
> save-time rule is still re-applied at projection rather than trusted, `//host`
> included.

A `storefront_page` destination carries a **null `href`**. The dynamic page route
is `/pages/:slug` (decided in Phase 25C) and Angular builds it from `target`; the
backend deliberately stores no route, so changing it stays an SPA change rather
than a data migration.

### Menu publishing

Menu enabled AND item enabled AND parent enabled AND destination resolves. A
Group with no surviving children is dropped too — an expandable entry that opens
onto nothing is worse than absence. Order is `sequence, lft, name`, the same the
Desk tree shows. A disabled menu answers `menu_not_found`, exactly like a missing
one: the storefront has nothing to render either way.

### Filters

Definitions come from `Category.storefront_filter_set` and nothing else — no
parent inheritance, no fallback to the Item's admin scope, no global list. Values
are those actually assigned to a listing entity in that category (simple Items and
variant templates), found by one indexed query. **No pricing, therefore no
counts**: `Red (17)` needs the full eligibility pipeline per value.

Selection matching is OR within a filter, AND across filters, implemented as one
correlated `EXISTS` per selected filter appended to the **Stage-1** candidate SQL:

```sql
AND EXISTS (SELECT 1 FROM `tabYOB Storefront Item Filter` sf_0
            WHERE sf_0.parent = i.name AND sf_0.parenttype = 'Item'
              AND sf_0.filter = %(filter_0)s
              AND sf_0.filter_value IN %(values_0)s)
```

A JOIN would multiply a row by its matching assignments — an item with Red AND
Blue would appear twice, inflating the page and corrupting the keyset cursor.
Because it runs in Stage 1, a narrower selection costs **fewer** pricing calls,
never more; asserted by a spy on `price_candidate`.

The normalised selection (filters sorted, values sorted and de-duplicated) joins
`_binding_fingerprint`, so a cursor cannot be replayed against a different
selection or category, while `["red","blue"]` and `["blue","red"]` remain one
logical query sharing one cursor.

A selection is never interpreted as a database field: an unknown key answers
`storefront_filter_unknown`, and facets require a category context
(`storefront_filter_context_required`).

### Pages and blocks

Discriminated by `type` — `image_banner`, `rich_text`, `banner_carousel`,
`product_grid`, `promo_grid` — each carrying only its own type's fields. Slides
and cards keep their child-row order.

A **Product Grid** is answered by `list_items()`, the same service the catalogue
uses, so grid cards ARE `ListingCard` rows: simple items priced normally, variant
families `price_state: select_options` with no borrowed child price.
`content_service` contains no Item query, no Item Price lookup, no Pricing Rule
evaluation, no UOM, warehouse or stock arithmetic — asserted by an executable-code
scan. The published block schemas are asserted against blocks the
runtime actually **projected** (Phase 25C-1), so `slides`/`cards` are typed rows
(`BannerCarouselSlide` / `PromoCard`) and `x-block-fields` records which fields
each type carries — heights on `image_banner`, `banner_carousel` and `promo_grid`
only. A grid whose category was disabled or turned into a group answers
`category: null` with an empty `items`, and **never** falls back to other products.

### Caching

Menus and page structure are customer-independent. A page containing a Product
Grid is **customer-priced** through `SellingContext`, so the hydrated response is
never cached or shared across customers — proved by a test in which two customers
on different price lists receive different rates from the same page. The first cut
adds **no caching at all**: correctness first, and a structural cache would need
invalidation on every Menu, Page, Block, Category and Filter save.

> Unrelated defect still open (found in Phase 25A): `cms.get_config` calls
> `frappe.cache().delete_value(cache_key)` immediately before reading the cache,
> so its one-hour cache never serves anything. Left alone deliberately — it is a
> separate contract with its own tests.

## System route content placements (Phase 25G)

The same reusable Blocks, at fixed positions inside EXISTING application pages —
above the cart, below a product — without those pages knowing what a block is.

**Not a page builder.** Angular owns which routes exist and where a
`<yob-content-slot>` sits in each; a merchant owns what goes in a slot and in
what order. Nobody can create a route or a position from Desk: that is a code
change in both repositories.

```text
                    YOB Storefront Block
              +------------+------------+
      Storefront Page Block      Content Placement
       /pages/:slug              /cart, /orders, ...
```

`utils/system_slots.py` is the one registry — DocType validation, Desk pickers,
runtime projection, OpenAPI enums and tests all read it. Eight routes: `home`
(reserved; `/` still redirects to `/catalog`), `catalog`, `category`, `product`,
`cart`, `account`, `orders`, `order_detail`. `login`, `checkout`, `payment` and
`payment_callback` are excluded **by decision**, with reasons recorded in
`EXCLUDED_ROUTES` rather than silently omitted.

Validation is on the **(route, slot) pair**: `cart` is real and `above_listing`
is real, but `cart.above_listing` is rendered nowhere, so content stored there
would never appear. An exact `(route, slot, block)` duplicate is refused; the
same Block in another slot, on another route, or on a Page as well is the point
of the design and stays allowed.

`cms.get_route_content(route_key)` returns EVERY declared slot, empty ones
included, in one request — never one request per slot. `blocks` is the identical
`ContentBlock` union `cms.get_page` returns, because both go through the same
`project_block()`; a test asserts each of the five types is byte-identical
through both mechanisms, and another asserts `route_content` never names a block
type. An unknown route answers `content_route_unknown` and is never mapped to a
neighbour.

`MAX_PRODUCT_GRIDS = 3` now lives in `utils/storefront_content.py` and is shared
by both placement mechanisms. For a route it counts **across the whole route**,
not per slot, because one response carries every slot. No caching, for the same
reason as `get_page`: a grid is priced for the buyer looking at it.

## Section styles on a placement (Phase 25I)

Every projected block carries `section_style` — one of `default`, `muted`,
`brand_soft`, `accent`, `dark` — naming the full-width band it sits in. Angular
wraps the unchanged block renderer in a section of that style around the existing
fixed-width container.

**Semantic only.** The backend defines no colour, padding, breakpoint, text
colour or width; it stores one approved word and refuses everything else — a
Tailwind class, a CSS declaration and arbitrary text are all rejected at save.
That closed vocabulary is what stops presentation becoming merchant-configurable.

**It belongs to the PLACEMENT.** The field is on `YOB Storefront Page Block` and
`YOB Storefront Content Placement`, never on `YOB Storefront Block`, because a
Block is authored once and placed many times: the same `Welcome Text` may be
muted on a page and dark on the home route. Storing it on the Block would force
the two to agree and push merchants into duplicating content.

`project_block(block, customer_doc, section_style)` applies it once beside `type`
and `block_name`. No block-type projector sees it — a projector that knew `dark`
meant white text would be Angular's job migrating into the backend, and a test
scans for that. Page and route still share the one projector.

A blank value normalises to `default` on the way out; nothing is rewritten, so
rows predating the field render exactly as before with no data patch.

## Content width on a placement (Phase 25K)

A second, independent placement key: `content_width`, either `contained` or
`full_width`. `section_style` paints the full-width band; `content_width` says
whether the block spans that band or stays inside the fixed
`yob-content-container`. Hero banners and carousels need the latter; most content
does not.

Neither key is derived from the other and every combination is valid — a test
stores and projects all ten. `full_width` is deliberately available to **all
five** block types, not just banners: it is a generic placement primitive, and
Angular still owns each component's internal layout.

**Horizontal containment only.** Not the background, not vertical spacing, not
block or image height, not responsive image choice, no margin or padding. The
backend implements no CSS meaning; it stores one of exactly two words and refuses
`100%`, `100vw`, `max-w-none` and arbitrary text at save. There is no narrow,
wide, boxed or fluid variant — a third width gets added deliberately if a real
case arrives.

Like `section_style` it lives on `YOB Storefront Page Block` and
`YOB Storefront Content Placement`, never on `YOB Storefront Block`, so the same
hero Banner runs full width on the home route and contained on a page without
being duplicated. `project_block(block, customer_doc, section_style, content_width)`
applies both once; no block-type projector sees either, guarded by a source scan.

Blank normalises to `contained` on the way out, so every pre-25K placement looks
exactly as it did with no data patch.

## Header product suggestions (Phase 26A)

`catalog.get_product_suggestions(search)` — at most **8** public products for the
header typeahead. Navigation only: no pagination, no cursor, no facets, no
category scope, no results page, and **no money at all**.

**Under 3 characters it answers `{"items": []}` and does nothing** — no candidate
query, no customer resolution. The SPA applies the same floor; the server
enforces rather than trusts it. A short search is not an error.

### One product universe, not a cheaper lookalike

The endpoint owns no eligibility rules. It reuses the listing's own:

| Stage | Reused | Cost |
| --- | --- | --- |
| 1 | `fetch_candidates(category=None, ...)` — the identical SQL: disabled, `is_sales_item`, public slug, family collapse, manufacturer fail-closed, end-of-life, same AND-across-words `item_name`/`item_code` match, same broad Item Price `EXISTS` | one query |
| 2 | `is_catalog_eligible()` / `family_has_sellable_variant()` — the authoritative base-price rule | one `get_price_list_rate_for` per candidate |
| 3 | **skipped** | zero Sales Orders |

Stage 3 exists only to produce rates, taxes and UOM, and a dropdown shows none of
them — so skipping it saves WORK without weakening the RULE: eligibility is a
statement about the base price, and Stage 3 can neither add nor remove a product.
A parity test asserts the suggestion set equals the listing set for the same
search, which is what stops a second "searchable" universe forming.

`fetch_candidates` gained an optional `category=None` for this (global search).
Absent means the predicate is simply not applied — not a wildcard — and every
other rule is unchanged, so `get_items` behaves exactly as before.

### Searchable identity (Phase 26A-1)

A search word matches the product's **display name OR its item code**, so a buyer
can type a code fragment read off a quote (`STO-ITEM-2026`) and find the product.
AND across words is unchanged, and a single word may be satisfied by either
column — `hex 10` can take `hex` from the name and `10` from the code.

The change was made in the **shared** predicate inside `fetch_candidates`, not in
the suggestion layer, so `get_items` and `get_product_suggestions` still describe
one product universe. `test_search_does_not_match_item_code_only_text` asserted
the opposite behaviour and was deliberately inverted.

Still not searchable: description, category, Item Group, Brand — matching on any
of those makes a result unexplainable to the buyer looking at the row it
produced. No fuzzy matching, and no relevance ranking.

### Contract

`{item_code, item_name, slug, image, is_template}` and nothing else — asserted by
a test listing every forbidden monetary and transaction-context key. `image` is
the stored relative path or null, the catalogue's own convention. A family
appears once with `is_template: true`; generated variants never appear alone.

Ordering is the catalogue's `name_asc`, deterministic — there is no relevance
ranking in the catalogue and none was invented, because suggestion order must not
disagree with the listing one click away.

Authenticated like every catalogue endpoint; the Customer comes from
`auth_context` only, and a test proves a product priced solely for another
customer is never suggested.

## Product Detail merchandising (Phase 27A)

Administration and data model only — there is **no runtime API** for this yet
(27B), and the published OpenAPI is untouched at 3.8.0.

### Item > Storefront

The Item tab formerly labelled `Storefront Filters` is now `Storefront` and holds
three groups. Only the LABEL changed; `custom_storefront_tab` keeps its fieldname,
so no existing site's data or layout was rewritten.

```
Item > Storefront
├── Filters          custom_storefront_filter_set + custom_storefront_filters (unchanged)
├── Gallery          custom_storefront_gallery -> YOB Storefront Product Gallery Image
└── Product Content  launcher into the standalone Section documents
```

### The ownership rule

Exactly one entity owns a public product's images and content:

| Entity | Owns merchandising |
| --- | --- |
| simple Item | its own |
| variant TEMPLATE | the whole family's |
| generated variant | **nothing, ever** |

There is deliberately **no variant→template fallback**, because a variant holds
nothing to fall back *from*: the template's content simply IS the family's, and
the family is what a buyer navigates to (Phase 24). Ownership is judged only by
ERPNext's `variant_of`, never by an item-code pattern — a naming convention is a
coincidence, not a data model. Enforcement is on the server
(`Item.validate` + the Section controller), because Data Import, the REST API and
`bench execute` never run a Client Script.

### Gallery

`YOB Storefront Product Gallery Image` (child of Item): `image` (Attach Image,
required), `is_primary`, `sort_order`, `alt_text`, `caption`. Ordering is
`sort_order` then grid `idx`, so a table of all-zero sort orders is still
deterministic. Zero or one primary is valid; **two are refused rather than
silently repaired** — a silent fix would edit an earlier decision the merchant
would never see. No data cap: the UI shows roughly five thumbnails, but that is a
rendering concern, not a schema one.

### Product Content is section-first, and sections are standalone

Verified empirically: **not one of the 350 child DocTypes in Frappe or ERPNext
owns a `Table` field**, and `load_children_from_db` never recurses. Nested child
tables do not work, so `Item → sections → blocks` is impossible as children.

```
YOB Storefront Product Content Section   (normal doc, Link -> Item)
  └── blocks  ->  YOB Storefront Product Content Block  (child)
```

That buys real ordered blocks, real validation and a real grid editor, for the
price of one link back to the Item. The Item's Product Content panel is the
bridge: it shows the section count and offers *Manage sections* and *Add section*
scoped to that product, with `item` pre-filled. Editing happens in the Section
document, which is where the block grid lives.

### Block types

`rich_text` · `key_value` · `table` · `image` · `download` · `video`

Each block carries only its own fields; the rest are cleared on save, so a block
that changed type cannot leak a stale value into a later projection. `rich_text`
is sanitised on save (first boundary, not the only one). `video` stores an
**http(s) URL only** — embed markup, iframes and scripts are refused, so a
merchant cannot author a script-injection surface in Desk. `download` uses
Frappe Attach semantics, never a filesystem path. The content `image` block is
deliberately distinct from a Gallery row and carries no `is_primary`.

### Structured blocks store real rows, never JSON

A child table cannot nest, so the two structured types link to their own small
normal documents:

| Block | Document | Shape |
| --- | --- | --- |
| `key_value` | `YOB Storefront Product Spec Group` | ordered `key_label` / `value_text` grid |
| `table` | `YOB Storefront Product Table` | 2–6 fixed columns + a row grid |

**The table is bounded rather than free-form.** The three alternatives were
pasted JSON (not an admin experience), a normalised `(row_index, column, value)`
grid (an admin typing row indices by hand), or fixed columns. Two to six covers
specification and comparison tables and keeps the editor an ordinary Frappe grid
with no custom widget: `column_count` is a Select and each **active** column
needs a label. Row order is the grid's own `idx` — dragged, never typed. An
app-owned Desk script hides the inactive columns; the controller enforces the
same bound, because Data Import never loads a script.

**Width is a view, not a deletion.** Labels and cells beyond `column_count` are
hidden in Desk, excluded from validation and excluded from the runtime
projection, but they are **kept in the database exactly as entered**. Narrowing
6 → 3 and back returns the original columns 4–6 untouched: a dropdown change is a
presentation decision and must never silently destroy merchant work. An inactive
blank label therefore cannot block a save either.

> **Phase 27B note.** Because inactive data persists, the projection carries the
> whole responsibility for hiding it: emit columns and cells `1..cint(column_count)`
> **only**, and ignore whatever is stored beyond that. `column_count` is a Select,
> so it arrives as a **string** — compare it through `cint()`, never as an integer.
> Persisted does not mean published.

A test asserts no `JSON` or `Code` field exists anywhere in this model.

### Structured content belongs to ONE product

`item` is **required** on both Spec Group and Product Table, and a Section may
only link to structured data owned by its own product — `Section.item` must equal
the linked document's `item`. Reuse *within* one product is fine and useful (two
sections showing the same specification set); reuse *across* products is refused,
because it would make one product's page mutable from another product's admin
screen and nobody editing the document would know whose pages they were changing.

Generated variants cannot own a Spec Group or a Product Table either, by the same
`variant_of` rule that governs galleries and sections.

### Not a page builder

No tab key, accordion mode, component name, template, CSS/Tailwind class, width,
breakpoint, background or HTML wrapper on any of these DocTypes — asserted by
test. Product content is structured merchandising data; Angular owns rendering.

### Separate from the Phase 25 CMS

`YOB Storefront Product Content Block` is a dedicated child DocType and is **not**
the Phase 25 `YOB Storefront Block` (a reusable master for marketing pages). The
two domains share no model, and tests assert the CMS block types, Pages and
Content Placements are unchanged. In Desk both new DocTypes sit under **Catalog**,
not under Content, in the Workspace card and the v16 left sidebar alike.

## Product Detail merchandising runtime (Phase 27B)

`catalog.get_item` returns `gallery` and `sections` alongside the existing
payload, on **both** branches. One request per product page — no separate gallery
or content endpoint. Both keys are always present and always arrays.

Attached at the two `get_item` branches, deliberately **not** inside
`build_item_detail`, which is shared with `resolve_variant`: merchandising
belongs to the public product entity, so choosing a size must not reload a
gallery. `resolve_variant` is unchanged and carries neither key.

### A page and a resolved SKU are different schemas (27B-1)

The runtime split above has a contract counterpart, and 3.9.0 got it wrong:
`gallery`/`sections` were made required on `ProductDetail`, which is *also* what
`resolve_variant` returns, so a strictly generated client expected merchandising
from a variant selection. Corrected in **3.9.1** by composition rather than by
changing behaviour:

```
ProductDetail          resolved SKU detail      -- no merchandising
ProductMerchandising   { gallery, sections }    -- both required
ProductPageDetail      allOf: ProductDetail + ProductMerchandising
VariantFamily          allOf: family fields + ProductMerchandising

get_item        -> oneOf [ ProductPageDetail, VariantFamily ]
resolve_variant -> ProductDetail
```

Guards read the actual `$ref`/`allOf` structure and resolve composition, because
prose is what failed to catch the conflation the first time. The runtime constants
`PRODUCT_DETAIL_KEYS`, `MERCHANDISING_KEYS` and `PRODUCT_PAGE_KEYS` stay distinct
and are asserted against the schema, so the two cannot be merged again.

### One owner, resolved before anything is read

`merchandising_owner()` starts from the public product and stops: a simple Item
or a template answers itself, a generated variant answers `None`. It never scans
a template's variants for content and never falls back from child to template —
there is no override chain because a variant owns nothing. A child that acquired
rows through a direct database edit is still ignored by its family's page.

### Fail closed, without taking the page down

Phase 27A refuses cross-product links and malformed tables at save; this layer
assumes none of that held (direct edits, restored backups, legacy rows). A block
whose spec group or table belongs to **another product**, is missing, or is
malformed is **skipped** — never published, never raised. A section left with no
publishable blocks is omitted rather than shipped as an empty heading. One bad
block cannot 500 a product page, and a healthy sibling still renders.

### Stored is not published

Phase 27A keeps the cells of a narrowed table so a merchant can widen it again.
That makes the projection responsible for hiding them: it reads
`cint(column_count)` — a Select, so a **string** — and emits only columns
`1..width`, padding every row to exactly that many cells. Widening republishes
the retained values.

### Query shape

Bounded by the content model, not the block count: **at most seven** reads for a
whole page — gallery, sections, blocks, spec groups, spec rows, tables, table
rows — with the last four batched by `IN (...)`. A page of twelve blocks costs
the same as one of two, asserted by a query-counting test.

### No transaction work

Merchandising adds **zero** pricing calls: a bare product and a fully
merchandised one make the identical number of `get_item_pricing` calls, asserted
by spy. No Sales Order, no variant resolution, no stock or UOM work, and no
response cache — `get_item` stays customer-priced.

### A separate union from the Phase 25 CMS

`ProductContentBlock` (six types) is not `ContentBlock` (five types), and the
machine-readable registries are separate too: `x-product-block-fields` beside the
CMS `x-block-fields`. `rich_text` and `image` appear in both as different shapes.
A contract guard asserts the runtime matches the published product registry and
that the CMS one is untouched.

## Chain verification (Phase 25F)

Not a feature. `tests/test_storefront_chain.py` walks the whole storefront in one
scenario, and every step is fed the **published output of the step before it**:

```text
Desk configuration -> cms.get_menu -> destination.target (the only identity
published) -> catalog.get_category_filters -> catalog.get_items(storefront_filters)
-> cms.get_page -> five Blocks -> Product Grid -> family card -> catalog.get_item
-> catalog.resolve_variant -> qty -> cart.add_to_cart
```

Unit tests cannot catch a **seam**: a test that calls
`get_category_filters("power-tools")` supplied the slug itself, so it could never
notice navigation publishing a docname where the endpoint expects a slug. This
file exists only for those joins — 18 tests, and no constant a test author typed
is used as a public identity anywhere in it.

The verdict is recorded in
`docs/changes/CHG-002-storefront-navigation-filters-blocks-report.md`:
**PASS with live environment smoke outstanding.** No live credentials exist in
this environment, so browser verification against a real deployment moves to the
pre-production checklist rather than blocking sign-off; every step of it already
has an automated equivalent.

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
