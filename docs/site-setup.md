# YOB Storefront — One-Time Site Setup

Everything required **once**, when standing up a new YOB storefront site.
For the per-customer process, see [`customer-onboarding.md`](customer-onboarding.md).

Verified against committed source on **2026-11-16**:
`yob_storefront 9784539` · `yob_auth 1fa3018` · `yob_core 4c85391` · Angular `9cbd4a2`.

Every statement below was read out of current source or observed on a live site.
Where a value is site-specific it is marked **site value**; where something was
*not* verified it says so.

---

## 1. Required apps and installation order

Classification is taken from `required_apps` in each app's `hooks.py`, plus a
grep for cross-app imports across all three YOB apps.

| App | Status for YOB | Evidence |
| --- | --- | --- |
| `frappe` | **REQUIRED** | framework; `[tool.bench.frappe-dependencies] frappe = ">=16.0.0,<17.0.0"` in `yob_core` and `yob_auth` |
| `erpnext` | **REQUIRED** | `yob_storefront/hooks.py: required_apps`; Item, Customer, Sales Order, pricing |
| `payments` | **REQUIRED** | `required_apps`; owns `Payment Request` and `Payment Gateway` |
| `india_compliance` | **REQUIRED** | `required_apps`; GST fields and the regional address hook are on the priced path |
| `yob_core` | **REQUIRED** | `required_apps` of `yob_auth` and `yob_storefront` |
| `yob_auth` | **REQUIRED** | `required_apps` of `yob_storefront`; the only identity authority |
| `yob_storefront` | **REQUIRED** | the solution app itself |
| `hrms` | **NOT USED BY YOB** | absent from every `required_apps`; **zero** imports in any YOB app |
| `crm` | **NOT USED BY YOB** | same |
| `helpdesk` | **NOT USED BY YOB** | same |
| `telephony` | **NOT USED BY YOB** | same; present on the dev bench only as a `crm`/`helpdesk` dependency |

> `hrms`, `crm`, `helpdesk` and `telephony` are installed on the current
> development bench. That is **bench context, not a YOB requirement.** A fresh
> YOB site must not install them to satisfy YOB.

**Dependency direction** (from `required_apps`, enforced by bench):

```
frappe
  └── yob_core            required_apps = []
        └── yob_auth      required_apps = ["yob_core"]
              └── yob_storefront
                    required_apps = ["yob_core", "yob_auth",
                                     "erpnext", "payments", "india_compliance"]
```

### Fresh-site install order

```bash
bench new-site <site>
bench --site <site> install-app erpnext
bench --site <site> install-app payments
bench --site <site> install-app india_compliance
bench --site <site> install-app yob_core
bench --site <site> install-app yob_auth
bench --site <site> install-app yob_storefront
bench --site <site> migrate
```

`yob_core` → `yob_auth` → `yob_storefront` is mandatory. ERPNext, Payments and
India Compliance may be installed in any order among themselves but must all
precede `yob_storefront`, because its `after_install` touches their DocTypes.

---

## 2. Validated baseline versions

These are the versions this release was **verified against**. They are a
validated baseline, **not permanent minimum versions** — recheck after any
upgrade, because ERPNext 16.52.1 in particular introduced permission gates that
earlier 16.x releases did not have.

| Component | Validated baseline |
| --- | --- |
| Frappe | **16.53.0** |
| ERPNext | **16.52.1** |
| India Compliance | **16.11.0** |
| Payments | `0.0.1` — **use branch `version-16`**; its package version is not a release identifier |
| Python | **3.14.7** |
| Node | **24.19.0** (npm 11.17.0, Yarn 1.22.22) |
| bench | **5.31.0** |
| MariaDB | **10.11.14**, `utf8mb4` / `utf8mb4_unicode_ci` |

Bench context only, **not** YOB dependencies: `hrms 16.52.0`, `crm 1.89.1`,
`helpdesk 1.31.0`, `telephony 0.0.1`.

---

## 3. Host, DNS and proxy configuration

### Request path

```
browser
  └─ https://<storefront-host>/            → Angular bundle (static)
  └─ https://<storefront-host>/api/method/ → reverse proxy → Frappe backend
                                             (Host header = the Frappe site name)
```

The Angular app calls the backend with a **relative** base path —
`environment.ts: api.methodBasePath = '/api/method/'`. So either the bundle is
served from the same origin as Frappe, or the proxy must forward `/api/` (and
`/assets/`, `/files/`) to Frappe from the storefront host. There is no absolute
backend URL to configure.

### Required proxy behaviour

| Requirement | Why |
| --- | --- |
| **`Host` header must be the Frappe site name** | Frappe resolves the site from `Host`; a mismatch serves the wrong site or none |
| **`X-YOB-Original-Host` must be *overwritten* on every public vhost** | `yob_auth/security/request.py::get_original_host` reads **only** this header and has **no fallback to `request.host`**. It must be set by the proxy on every vhost — including vhosts serving no storefront — because a client that can set it itself chooses which application's domain check it passes |
| HTTPS terminated at the edge | `allowed_origins` entries are scheme-explicit; an `https://` entry refuses the `http://` origin of the same host |
| `Origin` passed through unmodified | it is the browser's own anti-CSRF signal and cannot be forged by page script |

**Fail-closed note:** if `X-YOB-Original-Host` is missing, `get_original_host()`
returns `""` and the domain check **rejects** the request — a request that never
passed through the proxy is never treated as if it had. Failing closed does not
by itself stop a *spoofed* header; only the proxy clearing it does.

### `YOB Application → STOREFRONT`

| Field | What to put | Notes |
| --- | --- | --- |
| `application_code` | `STOREFRONT` | created automatically; do not rename |
| `enabled` | ✓ | |
| **`domains`** | one **hostname** per line, e.g. `store.example.com` | **site value.** Compared against `X-YOB-Original-Host`. Leave empty to skip domain validation entirely — only acceptable on a closed dev bench. A host rule: any scheme, any port |
| **`allowed_origins`** | one **complete browser origin** per line | **site value.** An origin rule: scheme, host and port must all match |

**`allowed_origins` accepts explicit, complete browser origins only.** Valid:

```
https://store.example.com
https://store.example.com:8443
http://localhost:4200
```

**Scheme-less values are not valid and must never be documented as such.** A
bare `store.example.com` or `store.example.com:4200` matches **nothing** — the
scheme is part of an origin's identity, and inferring one would grant access
over a protocol nobody configured. Hostname-level access is what `domains` is
for. Paths, queries, fragments and wildcards also match nothing.

Matching is **canonical, never substring**: `example.com` cannot match
`evil-example.com` or `example.com.attacker.test`. Only things that are the same
origin *by definition* are normalised — scheme and host casing, a trailing dot
on the host, a trailing slash in configuration, and a default port the browser
omits (`http://x:80` ≡ `http://x`, `https://x:443` ≡ `https://x`).

**How the two interact** (`_validate_request_domain`): if `domains` is empty,
nothing is checked. Otherwise the host must be in `domains`; then, if the browser
sent an `Origin`, that origin must either match an `allowed_origins` entry **or**
have a hostname present in `domains`. `Origin` is absent on same-origin GETs and
on non-browser callers, which is why `curl` never exercises this check.

Origin acceptance is decided **before** authentication, so a bad origin answers
**403 `application_access_denied`**, not 401.

---

## 4. Angular configuration

`src/environments/environment.ts` is the production file; `angular.json`
`fileReplacements` swaps it for `environment.development.ts` (dev) or
`environment.mock.ts` (mock). The mock layer is therefore **not in a production
bundle** — it cannot be enabled by a flag, only by editing the file.

Production values currently committed:

| Key | Value | Change per site? |
| --- | --- | --- |
| `production` | `true` | no |
| `mockApi` | `false` | **no — never `true` in production** |
| `api.methodBasePath` | `/api/method/` | no (relative; see §3) |
| **`api.application`** | **`STOREFRONT`** | no — must match `YOB Application.application_code` |
| `locale.defaultCurrency` | `INR` | per deployment |

Build:

```bash
npm ci
npm run build            # production configuration
```

Deploy the emitted bundle behind the storefront host per §3. `angular.json`
declares no `outputPath` override, so the bundle lands in the Angular CLI
default for this project — read it from the build output rather than assuming a
path.

---

## 5. What install / migrate creates automatically

`yob_storefront/hooks.py`:
`after_install = yob_storefront.install.after_install`,
`after_migrate = yob_storefront.install.after_migrate`.
`yob_auth/hooks.py` declares **`after_install` only — no `after_migrate`.**

| Artifact | Auto? | Hook | Manual action |
| --- | --- | --- | --- |
| `YOB Application` **STOREFRONT** (`enabled`, `required_profile_doctype = Customer`, password login on, both OTP flags **off**) | **Yes** | `after_install` → `_ensure_storefront_application` | Set `domains` and `allowed_origins` — install leaves both **empty** |
| Role **`YOB Storefront Buyer`** (`desk_access = 0`) | **Yes** | `after_install` **+ `after_migrate`** → `ensure_role_and_permissions` | none |
| Buyer DocPerms: `Item read`, `Sales Taxes and Charges Template select` | **Yes** | same | none |
| Role **`YOB Payment Processor`** (`desk_access = 0`, `is_custom`) | **Yes** | `after_install` **+ `after_migrate`** → `ensure_payment_processor_identity` | none |
| User `payment-processor@yob.internal` (`enabled = 0`) | **Yes** | same | none — never enable it |
| Processor DocPerms: `Item read`, `Account read`, `Sales Taxes and Charges Template select` | **Yes** | same | none |
| **Item** custom fields (10): `custom_slug`, `custom_category`, storefront tab, filters, gallery, content | **Yes** | `after_install` **+ `after_migrate`** → `ensure_custom_fields` | none |
| **Item Price** custom fields (4): `custom_moq`, `custom_quantity_multiplier`, `custom_mrp` + section | **Yes** | same | none |
| **Payment Request** custom fields: `custom_cart`, `custom_checkout_token` (**unique**), `custom_checkout_expiry`, `custom_committed_at`, `custom_provider_claim_at`, `custom_source_fingerprint` | **Yes** | same | none |
| **Customer → YOB Storefront Users** fields: `custom_yob_storefront_users_section`, `custom_yob_storefront_users_html` | **Yes** | same | none |
| `Payment Gateway` registration **`Razorpay`** | **Yes** | `after_install` **+ `after_migrate`** → `ensure_payment_gateways` | Supply credentials in `Razorpay Settings` — YOB stores **no** copy |
| Target Management composite unique index (`customer` + `target_rule`) | **Yes** | **`after_migrate` only** → `ensure_target_constraints` | none. On a first install the table arrives later in the same migrate; the next migrate applies it |
| Workspace **`YOB Storefront`** + Sidebar **`YOB Storefront`** (35 items) | **Yes** | app files under `yob_storefront/workspace/` and `sidebar/`, synced by `bench migrate` | none |
| Apps-screen tile → `/desk/yob-storefront` | **Yes** | `add_to_apps_screen` | none |
| `Scheduled Job Type` for the daily target recalculation | **Yes** | `scheduler_events` | scheduler must be enabled (§14) |
| `YOB Auth Settings` single (password on, email OTP on, mobile OTP off) | **Yes** | `yob_auth` `after_install` | — |
| `site_config.json → yob_auth_otp_secret` (64 hex chars) | **Yes** | `yob_auth` `after_install` | **Never rotate casually** — it invalidates every in-flight OTP challenge |
| `Client Script` records for this module | **Yes** | `fixtures` | none |
| **`YOB User Application Access`** | **No — deliberately** | — | An access grant must be an administrator's conscious act: §Part B |

**Idempotency.** Every function above guards on existence before writing, so a
re-run is a no-op: `_ensure_storefront_application` returns early if the record
exists; `ensure_role_and_permissions` and `ensure_payment_processor_identity`
skip each role, DocPerm and `Has Role` that is already present;
`create_custom_fields` inserts when absent and *updates the definition* when
present, leaving stored values untouched; `frappe.db.add_unique` checks
`information_schema` first. Verified empirically: a second `bench migrate`
changes nothing, and deleting a processor DocPerm then migrating restores it.

> **`yob_auth` has no `after_migrate`.** Its settings single and OTP secret are
> created at install only. An existing site that somehow lacks them needs
> `bench --site <site> execute yob_auth.install.after_install`.

### Why a Custom DocPerm needs `setup_custom_perms` first

A single `Custom DocPerm` **replaces a DocType's entire permission set**
(`frappe/model/meta.py::set_custom_permissions`). `install.py` therefore calls
`frappe.permissions.setup_custom_perms(doctype)` before inserting, which copies
the standard rows across first. Without it, adding the buyer's `Item read` would
silently strip Item access from Sales User, Stock User, Item Manager and every
other built-in role — and Administrator would keep working, which is how it
hides in testing.

---

## 6. Company

| Setting | Requirement |
| --- | --- |
| `Company` | one, created by ERPNext setup |
| `default_currency` | must match `YOB Store Settings.default_currency` |
| **`Company.default_warehouse`** | **required** — the terminal warehouse fallback (§10). Auto-populated by ERPNext's `Company.create_default_warehouses()` → `Stores - <abbr>` |
| Default warehouse tree | `All Warehouses`, `Stores`, `Finished Goods`, `Work In Progress`, `Goods In Transit` — created by ERPNext |
| `country` | drives India Compliance behaviour; `India` on this deployment |
| GST status | §11 |

> **Do NOT configure `Stock Settings.default_warehouse`.** The field **no longer
> exists** in ERPNext 16.52.1 — confirmed on the live site
> (`get_meta('Stock Settings').get_field('default_warehouse')` → `None`).
> `Company.default_warehouse` replaced it.

---

## 7. YOB Store Settings — field-by-field

Classification from a grep of every field name across all non-test source.

| Field | Status | Where it is actually read |
| --- | --- | --- |
| **`company`** | **ACTIVE / REQUIRED** | `pricing_context.py:50`, `api/cart.py` (new Cart + company address). `reqd` in the DocType |
| **`default_currency`** | **ACTIVE / REQUIRED** | `pricing_context.py:51`, `catalog_listing_service.py:130`, new Cart's `currency`, `catalog.py:553` |
| **`default_price_list`** | **ACTIVE / REQUIRED** | seeds a **new Cart's** `selling_price_list` (`api/cart.py:83`). Authoritative per-request pricing resolves separately (§8) — keep the two consistent to avoid confusion |
| **`cart_expiry`** (hours) | **ACTIVE / OPTIONAL** | `cart_service.py:245 validate_cart_expiry` — an expired Cart has its items cleared. Blank/0 = never expires |
| `store_name` | **ACTIVE / OPTIONAL** | published by `api/cms.py` for the SPA header. `reqd` in the DocType |
| `store_domain` | **ACTIVE / OPTIONAL** | published by `api/cms.py`. **Not a security control** — the domain check uses `YOB Application.domains` |
| `store_logo` | **ACTIVE / OPTIONAL** | published by `api/cms.py` |
| `default_terms_page` | **ACTIVE / OPTIONAL** | published by `api/cms.py` |
| `default_privacy_page` | **ACTIVE / OPTIONAL** | published by `api/cms.py` |
| `allow_guest_purchase` | **PUBLISHED BUT NOT ENFORCED** | the **only** reference is `api/cms.py:50`, which publishes it to the client. **No server-side code gates on it.** Treat it as informational; do not rely on it to permit or forbid guest purchase |
| **`default_warehouse`** | **LEGACY / INERT** | the only reference is the echo in `api/cms.py:45`. It is **not** in the warehouse resolution chain (§10). Leave it blank |
| **`allowed_payment_methods`** | **LEGACY / INERT** | **no runtime read at all.** `api/cms.py:51` reads a different key (`allowed_payment_modes`) that does not exist on the DocType, so it always yields `[]`. Payment eligibility comes entirely from `Payment Method Assignment` (§12) |

---

## 8. Pricing

### Authoritative resolution order

`services/pricing_service.py::get_price_list_for_customer` →
`erpnext.accounts.party.get_default_price_list` (`party.py:374`):

```
1. Customer.default_price_list                 (only if that Price List is enabled)
2. Customer Group.default_price_list           (only if enabled)
3. Selling Settings.selling_price_list
4. + Selling Settings.fallback_to_default_price_list, when on, adds a SECOND
     list that is also searched for a rate
```

Everything that prices a storefront transaction goes through
`services/pricing_context.py::SellingContext`, which exists precisely so the
product page and the Cart cannot resolve different answers. Nothing in it is
accepted from the browser.

### One-time setup

| Item | Requirement |
| --- | --- |
| **`Selling Settings.selling_price_list`** | **required** — the site-wide fallback that makes per-customer price lists unnecessary |
| `Price List` | at least one with `selling = 1`, `enabled = 1`, currency matching the store |
| **`Item Price`** rows | **required per sellable item.** No `Item Price` ⇒ no rate ⇒ the catalogue drops the candidate rather than failing the page |
| `Selling Settings.fallback_to_default_price_list` | optional |
| Customer / Customer Group `default_price_list` | optional overrides — see Part B §22 |
| `Pricing Rule` | optional. ERPNext owns every rule, discount and free item. **Note:** `pricing_context.item_details()` passes `ignore_pricing_rule = 1` for the *context* read, while the Cart's authoritative repricing applies rules — so a Pricing Rule affects the charged total |

---

## 9. Catalogue

A product is listed only if **all** of these hold
(`catalog_listing_service.py:375-385`, verified):

| Condition | Field |
| --- | --- |
| not disabled | `Item.disabled = 0` |
| sellable | `Item.is_sales_item = 1` |
| **has a public slug** | `IFNULL(Item.custom_slug,'') != ''` |
| not a generated variant | `IFNULL(Item.variant_of,'') = ''` |
| simple item, or an attribute-based family | `has_variants = 0` OR `variant_based_on = 'Item Attribute'` |
| **has a resolvable price** | an `Item Price` on the resolved price list (family: on at least one buyable variant) |

### Required vs merchandising-only

| Field | Required? |
| --- | --- |
| `Item.item_code` / `item_name` | **required** (ERPNext) |
| `Item.stock_uom` | **required** (ERPNext) |
| **`Item.custom_slug`** | **required in practice** for anything a buyer navigates to — a simple Item or a variant template. Must be unique. Deliberately **not** `reqd` on the field, because generated variants legitimately have none and making it mandatory would stop ERPNext creating variants at all |
| **`Item.custom_category`** | **required** to appear under a category listing |
| **`Item Price`** (selling) | **required** — see above |
| `Item.sales_uom` | optional; ERPNext sells in `sales_uom` else `stock_uom`, and YOB asks ERPNext rather than reimplementing the choice |
| `Item.image` | merchandising |
| `custom_storefront_gallery` | merchandising |
| Product Content Sections | merchandising (separate documents; the Item shows a launcher) |
| `custom_storefront_filter_set` + `custom_storefront_filters` | merchandising facets — unrelated to variant attributes, which resolve an actual SKU |
| `Item Price.custom_moq` / `custom_quantity_multiplier` | **guidance only.** The buying area opens at MOQ and steps by the multiplier; **nothing in cart, checkout or Sales Order rejects another quantity.** Unrelated to UOM or pack size |
| `Item Price.custom_mrp` | **informational display only.** Never a base price; no discount or saving is derived from it; not validated against the selling rate |

### Category

`Category` is autonamed from `category_name`. For storefront use set
`category_name`, **`slug`**, `is_active`, and optionally `parent_category`,
`display_order`, `thumbnail`, `banner`, `storefront_filter_set`, SEO fields.
Variants: create an attribute-based template (`variant_based_on = 'Item Attribute'`)
with a slug; generated variants are reached through the family page and carry no
slug of their own.

---

## 10. Warehouse resolution (ERPNext 16.52.1)

Exact precedence, read from `erpnext/stock/get_item_details.py:729-743`:

```
1. ctx.set_warehouse                       (transaction-level; YOB never sets it)
2. Item Default.default_warehouse          (per company)
3. Item Group.default_warehouse
4. Brand.default_warehouse
5. ctx.warehouse                           (an already-populated row value)
   ─────────────────────────────────────────────────────────────────
6. Company.default_warehouse               ← terminal fallback, only if 1-5 gave nothing
```

**Minimum recommended configuration: set `Company.default_warehouse` and nothing
else.** Per-item `Item Default` rows are optional, for genuinely different stock
locations. `YOB Store Settings.default_warehouse` is **not** consulted (§7), and
`Stock Settings.default_warehouse` **no longer exists** (§6).

If nothing resolves, YOB reports availability as *unknown* rather than inventing
a warehouse or failing the catalogue read.

---

## 11. Tax / India Compliance

### Path A — GST-registered company

All **site-level**, all required together:

| Item | Note |
| --- | --- |
| `Company.gstin` | validated, including the check digit |
| `Company.gst_category` | e.g. `Registered Regular` |
| **A Company `Address` carrying a GSTIN** | **India Compliance refuses company registration without one.** Easy to miss |
| `Sales Taxes and Charges Template` set | `Output GST In-state`, `Output GST Out-state` (+ RCM variants) — created by India Compliance setup |
| India Compliance app configuration | per its own documentation; e-invoice/e-way bill settings are out of YOB's scope |

### Path B — GST-unregistered company

India Compliance **short-circuits** (`ignore_gst_validations`). Consequence:
**no tax template resolves and orders carry zero tax rows.** Checkout still
works end to end — verified. This is also why two tests in the suite skip, and
why a GST-registered company is the only configuration in which the tax path is
exercised.

### How the rate is chosen

Verified by running three real Proceeds against an armed company:

| Customer | Address `state` | Template resolved | Tax rows |
| --- | --- | --- | --- |
| `Registered Regular`, GSTIN `24…` | Gujarat (= company state) | `Output GST In-state` | **SGST 9% + CGST 9%** |
| `Registered Regular`, GSTIN `27…` | Maharashtra | `Output GST Out-state` | **IGST 18%** |
| **`Unregistered`, no GSTIN** | Gujarat | `Output GST In-state` | **SGST 9% + CGST 9%** |

**The decisive field is the customer Address `state`**, which sets
`place_of_supply` and therefore in-state vs out-of-state. The customer's own
`gst_category` and `gstin` did **not** change the rate — the unregistered
customer was charged identically. They matter for compliance filing and
e-invoicing, not for rate selection.

The chain that reaches the template, and the permission it needs:

```
party.py:304 set_address_details → get_regional_address_details
  → india_compliance transaction.py:942 get_gst_details
    → accounts_controller.py:3648 get_taxes_and_charges
      → erpnext.require_permission("Sales Taxes and Charges Template", …, "select")
```

That gate is new in ERPNext 16.52.1. Both the buyer role and the payment
processor hold `select` on the tax master for exactly this reason (§5, §12).

---

## 12. Payment

### Required for the payment page to offer a usable payment method / complete payment

> **Precision, corrected in Phase 33.** Proceed to Payment and the issuing of a
> `/payment/<token>` link are a SEPARATE stage from payment-method eligibility.
> A buyer with no applicable `Payment Method Assignment` still gets a Payment
> Request and still reaches the payment page; what they cannot do there is
> choose a method or pay. So the rows below are not a precondition for reaching
> the page — they are what makes the page able to take money.

| Item | Note |
| --- | --- |
| `Payment Method` rows, `is_active = 1` | e.g. `PayLater` (Offline), `Razorpay` (Online) |
| **At least one `Payment Method Assignment`, `is_active = 1`, that covers the buyer** | the only source of eligibility |
| `Payment Gateway` registration | `Razorpay` created automatically (§5) |

**Eligibility rule** (`services/payment_method_service.py::_assignment_applies`):
assignment active → target matches (`Customer` / `Customer Group` / `Company`;
an assignment with **none** of those set is unrestricted) → the order amount
satisfies `minimum_order_amount` / `maximum_order_amount` (a falsy bound means
*no bound*) → the Payment Method itself is active.

**Recommended:** assign at **`reference_doctype = Company`** so every customer
of that company is covered and **no per-customer payment setup is ever needed.**

### Required only for online capture

| Item | Note |
| --- | --- |
| `Razorpay Settings` credentials | Frappe Payments owns them. **YOB stores no copy** and writes no `gateway_controller` override |

`PayLater` needs no gateway and no credentials.

### The YOB payment processor identity

Created automatically; **never** a payer and never interactive.

| Property | Value |
| --- | --- |
| User | `payment-processor@yob.internal` |
| **`enabled`** | **`0`** — intentional. A disabled user keeps its roles when entered through the server-side execution-user switch but cannot authenticate and has no session, so the identity is reachable only from trusted server code |
| Roles | `YOB Payment Processor` only |
| `Role.desk_access` | `0` |
| **DocPerms — the complete surface** | `Item` **read** · `Account` **read** · `Sales Taxes and Charges Template` **select** |
| Deliberately **not** granted | `Sales Order`, `Customer`, `Payment Request`, `Address`, `Contact` — the Sales Order's own `flags.ignore_permissions` covers the party path, and per-attempt access is a document-specific `DocShare` instead |

Each grant was added only after a test proved the commitment path needs it.
`Administrator` is deliberately not used, and neither is the buyer role.

---

## 13. Authentication

| Mode | Enabled by default? | What it needs |
| --- | --- | --- |
| **Password** | **YES** — `YOB Auth Settings.password_login_enabled = 1` **and** `YOB Application.allow_password_login = 1` | a `User` with a password. **No `YOB Login Identity`.** Uses Frappe's own `LoginManager.authenticate()` |
| **Email OTP** | **NO** — `YOB Auth Settings.email_otp_enabled = 1` but `YOB Application.allow_email_otp = 0`. **Both** must be on | a `User` whose docname is the email. **No `YOB Login Identity`** — `otp.resolve_user` looks the identifier up directly as `User.name`. Plus a working mail queue |
| **Mobile OTP** | **NO** — both flags `0` | a **`YOB Login Identity`** with `identity_type = Mobile`, `normalized_value = +91…`, `enabled = 1`, **`verified = 1`**; plus `YOB Auth Settings.mobile_otp_enabled` and `sms_sender_method` |

`YOB Login Identity` is used by **mobile OTP only** — it is that method's sole
consumer. Do not create one for password or email-OTP buyers.

`site_config.json → yob_auth_otp_secret` (≥32 chars) is required for any OTP
and is generated at install. Rotating it invalidates every in-flight challenge.

**Rate limits** (`YOB Auth Settings`): 10 password attempts per user and 50 per
IP per 300 s; 8 OTP requests/hour; 5 OTP verify attempts; 300 s OTP expiry.
Per-IP limits are only meaningful when `yob_auth_client_ip_header` names a
header a trusted proxy overwrites — otherwise every caller shares one bucket,
which is useless as a control and usable as a denial-of-service lever.

Every one of these counters is scoped to the **current site**, so two sites on
one bench (sharing one Redis) cannot consume each other's allowance. They
deliberately live outside Frappe's own cache namespace, which means
`bench clear-cache` does **not** reset an in-flight rate-limit window.

**Website User, not System User.** A storefront buyer is a `Website User`; the
buyer role carries `desk_access = 0`. The new Customer panel refuses System
Users outright (Part B §19).

`domains` / `allowed_origins`: §3.

---

## 14. Scheduler and workers

YOB declares exactly **one** scheduled job:

| Job | Frequency | Method |
| --- | --- | --- |
| Target Management recalculation | **Daily** | `yob_storefront.services.target_service.recalculate_due_targets` |

Confirmed on the live site as a `Scheduled Job Type` row, `frequency = Daily`,
`stopped = 0`. It is an upsert over a deterministic input, so a second run over
unchanged sales produces identical figures, and it uses a per-pair savepoint so
one bad configuration cannot abort the run or leave a half-written row.

**Production must run:**

- the **scheduler** (`System Settings.enable_scheduler` on; `bench enable-scheduler`),
- at least one **worker** (the scheduled job, welcome/OTP e-mail, and Razorpay
  webhook work are queued),
- **Redis** queue and cache,
- Frappe web workers behind the proxy.

No YOB job is `all`, `hourly`, `weekly` or `monthly`. Every other job in the
site's daily list belongs to Frappe, HRMS, CRM or Helpdesk — not YOB.

---

## 15. Fresh-install verification checklist

```
☐ 1  Apps installed, correct order
     bench --site <site> list-apps
     → frappe, erpnext, payments, india_compliance, yob_core, yob_auth, yob_storefront

☐ 2  Migrate succeeds
     bench --site <site> migrate            → exit 0
     bench --site <site> migrate            → exit 0 again (idempotency)

☐ 3  Application registered
     YOB Application STOREFRONT exists, enabled,
     required_profile_doctype = Customer
     → then SET domains and allowed_origins (install leaves them empty)

☐ 4  Navigation
     Apps screen shows YOB Storefront → /desk/yob-storefront
     Workspace "YOB Storefront" and Sidebar "YOB Storefront" both present

☐ 5  Customer panel
     Open any Customer → Portal Users tab
     → ERPNext "Customer Portal Users" unchanged
     → "YOB Storefront Users" section present, with
       [Add Existing User] [Create Website User]

☐ 6  Buyer permissions — exactly two
     YOB Storefront Buyer: Item read, Sales Taxes and Charges Template select
     Role.desk_access = 0

☐ 7  Processor permissions — exactly three
     payment-processor@yob.internal: enabled = 0, Website User,
       role YOB Payment Processor only
     DocPerms: Item read, Account read,
               Sales Taxes and Charges Template select
     NOT granted: Sales Order, Customer, Payment Request, Address, Contact

☐ 8  Company and warehouse
     Company.default_warehouse set (e.g. Stores - <abbr>)
     Stock Settings.default_warehouse — confirm the field does NOT exist

☐ 9  Pricing
     Selling Settings.selling_price_list set
     Item Price rows exist on that list

☐ 10 Catalogue
     At least one Item with custom_slug + custom_category + an Item Price
     Category with slug + is_active
     → category page lists it, product page shows a price

☐ 11 Cart
     Add to cart as a real buyer → a Cart is created AUTOMATICALLY with
     company / currency / selling_price_list from YOB Store Settings

☐ 12 Checkout + payment token
     Select Contact and Address → Proceed to Payment → HTTP 201
     → Draft Sales Order + Payment Request + token
     → /payment/<token> loads with the correct amount and methods
     → STOP before completing a real payment

☐ 13 Target Management
     Scheduled Job Type for recalculate_due_targets exists, Daily, not stopped
     Scheduler enabled

☐ 14 Auth posture
     Password login on; email/mobile OTP off unless deliberately enabled
     site_config yob_auth_otp_secret present
     bench --site <site> execute yob_auth.setup.check_configuration
```
