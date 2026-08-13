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
