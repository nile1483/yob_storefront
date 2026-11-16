# YOB Storefront — Verification Status

Honest statement of what is tested, what is not, and whether new-customer
onboarding is production-ready.

Confirmed against committed source on **2026-11-16**:
`yob_storefront 9784539` · `yob_auth 1fa3018` · `yob_core 4c85391` · Angular `9cbd4a2`.

Phase 33 added test coverage and **one narrowly scoped production fix** in
`yob_auth`: authentication rate-limit counters are now isolated per Frappe site
(§5 below). Nothing else in any app changed, so every other behaviour recorded
here is still the behaviour of those commits; what changed is how much of it is
proved automatically.

Companion documents: [`site-setup.md`](site-setup.md) ·
[`customer-onboarding.md`](customer-onboarding.md).

---

## 1. Latest trustworthy regression result

| Suite | Result | Provenance |
| --- | --- | --- |
| **`yob_storefront`** | **`Ran 1656 tests` · `OK (skipped=2)` · exit 0** · 0 failures, 0 errors | full-app run after the Phase 33 test additions |
| **`yob_auth`** | **`Ran 149 tests` · `OK` · exit 0** | full-app run after the rate-limit site-isolation fix |
| **`yob_core`** | **`Ran 32 tests` · `OK` · exit 0** | clean at `4c85391`, unchanged |

Phase 33 moved these from 1597 / 74 / 32: **+59 storefront** and **+75 auth**
tests, all of them covering behaviour that previously had none.

The storefront number predates the `yob_auth` rate-limit fix, which was made
afterwards. That fix touches only the key the password and OTP limiters
increment, and the limiters are reached from exactly two endpoints
(`login_with_password`, `request_otp`) — no storefront catalogue, pricing,
cart, checkout or payment path calls them. The storefront modules that DO
depend on authentication were rerun individually and are green:
`test_storefront_login` (12), `test_tenant_isolation` (41), `test_rename` (13),
`test_response_contract` (11).

The 2 skips are pre-existing and intentional: both require a GST-registered
company, and the test site's company is `gst_category: Unregistered`. (The
Proceed-to-Payment modules arm GST *inside* their own transaction instead, which
is why in-state and out-of-state GST are tested rather than skipped.)

Test-function totals by app: 1601 `def test_` across 48 storefront modules
(the suite reports 1656 because several classes parametrise), 134 in `yob_auth`
across 7 modules, 32 in `yob_core`.

> **A note on the first Phase 33 full run, kept because it is the useful part.**
> `test_storefront_login` passed 12/12 on its own and then produced 3 failures
> and 3 errors in the full-app run — and, worse, **four of its tests passed for
> the wrong reason**. It had built its fixtures once per class and committed
> them; in a full-app run that commit was not durable, the first test's rollback
> removed the fixture, and the remaining tests either failed or were satisfied by
> an absence (a missing application is refused with the same code as a missing
> grant) or by a stale role cache. The fixtures are now rebuilt per test, depend
> on no commit, and `setUp` asserts they are present — so a vanished fixture
> fails loudly instead of lying. A green module in isolation is not evidence
> until it is green in the whole run.

---

## 2. Coverage matrix

| Area | Verdict | Test module(s) |
| --- | --- | --- |
| **INSTALLATION** | | |
| `ensure_custom_fields` — idempotent, preserves stored values | **TESTED** | `test_storefront_install` (`test_the_installer_is_idempotent`, `test_reinstalling_preserves_stored_values`), `test_product_merchandising` |
| `ensure_role_and_permissions` — idempotent | **TESTED** | `test_pricing_elevation` (explicitly *"after_migrate runs on every deploy; a second run must change nothing"*), `test_trusted_pricing_context`, `test_proceed_to_payment_buyer` |
| `ensure_payment_processor_identity` — idempotent | **TESTED** | `test_public_payment_authorization:450-460` (calls it twice) |
| `ensure_payment_gateways` — registers, idempotent, overwrites no configuration, writes no credential | **TESTED** | `test_install_orchestration` (`PaymentGatewayProvisioningCase`, 4) |
| `ensure_target_constraints` — constraint by column set, repeat-safe, first-install branch | **TESTED** | `test_install_orchestration` (`TargetConstraintCase`, 4) |
| `_ensure_storefront_application` — shipped defaults, no environment routing, grants nobody, never overwrites | **TESTED** | `test_install_orchestration` (`StorefrontApplicationProvisioningCase`, 5) |
| `after_install()` / `after_migrate()` **as whole functions** | **TESTED** | `test_install_orchestration` — `EntryPointWiringCase` (6) asserts every provisioner is reachable from an entry point, `AfterMigrateRestoresCase` (12) removes real artefacts and migrates them back |
| Workspace / Sidebar present, app-owned, single | **TESTED** | `test_storefront_install` (6 tests) |
| **Customer → YOB Storefront Users custom fields exist / are placed in the Portal Users tab** | **TESTED** | `test_storefront_install` (`CustomerStorefrontPanelFieldsCase`, 9 — including that the nearest preceding Tab Break is `portal_users_tab`, and that no child table mirrors the access records) + `test_install_orchestration` (restored in the right place by migrate) |
| **AUTHENTICATION** | | |
| **Password login (`login_with_password`)** — envelope, context, real Frappe session, wrong password, unknown user, disabled user, missing/disabled/expired/not-yet-valid grant, wrong application, disabled application, method off site-wide and per application | **TESTED** | `yob_auth/test_login_endpoint` (26) + `test_storefront_login` (12, against the real `STOREFRONT` application and a real Customer) |
| **A refused login creates NO session** | **TESTED** | `yob_auth/test_login_endpoint` — asserted on every refusal branch |
| **Login does not bypass the domain/origin guard** | **TESTED** | `yob_auth/test_login_endpoint` (missing host, unapproved host, third-party Origin, trusted host still succeeds) |
| **Password rate limit at the endpoint** | **TESTED** | `yob_auth/test_login_endpoint` |
| **Rate-limit counters are isolated per site** — password user/IP/pair windows, OTP hourly windows, OTP resend cooldown; and two calls on the SAME site still share one counter | **TESTED** | `yob_auth/test_rate_limit_isolation` (15). 13 of the 15 fail against the pre-fix key construction |
| **Non-enumeration** — unknown user ≡ wrong password, unknown application ≡ disabled application | **TESTED** | `yob_auth/test_login_endpoint` |
| **Email OTP / Mobile OTP (`request_otp`, `login_with_otp`)** — challenge creation and lifetime, digest-only storage, valid/invalid/replay/expired/attempt budget, resend cooldown, access-scoped resolution, verified+enabled mobile identity, normalisation, generic notice | **TESTED** | `yob_auth/test_otp_endpoints` (34), delivery always mocked |
| OTP delivery boundary (`sendmail`, configured SMS sender, unconfigured sender refused) | **TESTED** | `yob_auth/test_otp_endpoints` — transport patched; no message leaves the bench |
| The shipped `STOREFRONT` OTP posture (password-only) | **TESTED** | `test_storefront_login` |
| Origin matching (canonical, scheme-explicit, no substring) | **TESTED** | `yob_auth/test_origin_matching` (46 tests) |
| Domain enforcement / `X-YOB-Original-Host` fail-closed | **TESTED** | `yob_auth/test_domain_enforcement` (6) |
| `resolve_access` — enabled, validity window, profile, user disabled | **TESTED** | `test_customer_storefront_users`, `test_tenant_isolation`, `test_pricing_elevation`, `test_trusted_pricing_context` |
| Response envelope / error contract | **TESTED** | `yob_core` (32), `yob_auth/test_response_contract`, `test_response_contract`, `test_http_methods` |
| **NEW-CUSTOMER ONBOARDING** | | |
| Customer with minimum fields | **TESTED** | `test_customer_storefront_users` fixtures create Customers with `customer_name` only |
| Website User created, type and roles correct | **TESTED** | `test_customer_storefront_users` (`test_creating_a_website_user_also_grants_access`) |
| Buyer role granted automatically | **TESTED** | `test_customer_storefront_users`, `test_pricing_elevation` |
| Contact (minimum fields, Dynamic Link, self-service) | **TESTED** | `test_account_crud_audit` (48), `test_proceed_to_payment_buyer` |
| Address (minimum fields, ownership, self-service) | **TESTED** | `test_account_crud_audit`, `test_address_pricing_freshness` (11), `test_order_address_history` (8) |
| First Cart auto-created with server defaults | **TESTED** | `test_trusted_pricing_context` (`CompanyAddressParityCase`), `test_cart_to_sales_order` |
| **CUSTOMER → YOB STOREFRONT USERS PANEL** | | |
| Add existing user; context fields fixed server-side | **TESTED** | `test_customer_storefront_users` |
| Create Website User (+ nothing else created) | **TESTED** | same |
| Atomicity — failed grant leaves no orphan User | **TESTED** | same (`test_a_failed_grant_leaves_no_orphan_user`) |
| Duplicate on the same Customer refused | **TESTED** | same |
| Cross-Customer refusal, named, and **not moved** | **TESTED** | same |
| Multiple users per Customer | **TESTED** | same |
| Disable → role revoked; re-enable → restored | **TESTED** | same |
| Remove → grant deleted, User/Customer kept | **TESTED** | same |
| **`after_delete` role revocation** (the fixed defect) | **TESTED** | same (`test_removing_access_deletes_only_the_grant` fails without the fix) |
| Validity dates stored, enforced, clearable | **TESTED** | same (5 tests, cross-checked against real `resolve_access` refusals) |
| Unauthorised operator / buyer / Guest refused | **TESTED** | same (`StorefrontUserPermissionCase`, 7 tests) + endpoint inventory and non-Guest guards |
| Panel holds no persisted mirror | **TESTED** | same (`test_listing_reflects_the_access_records_and_nothing_else`, `test_opening_a_customer_creates_nothing`) |
| No role management inside the adapter | **TESTED** | same (source guard) |
| **Browser rendering of the panel, dialogs, confirm prompt** | **NOT TESTED** | manual only — see §3 |
| **STOREFRONT** | | |
| Catalogue listing, eligibility, filters, suggestions | **TESTED** | `test_catalog_listing` (87), `test_catalog_category` (28), `test_storefront_filters` (18), `test_product_suggestions` (54) |
| Product detail + merchandising | **TESTED** | `test_product_detail_merchandising` (81), `test_product_merchandising` (90), `test_content_placements` (40), `test_content_widths` (27), `test_section_styles` (23) |
| Variants | **TESTED** | `test_variant_catalog` (54), `test_variant_contract` (22) |
| Pricing — convergence, elevation, transaction semantics, trusted context | **TESTED** | `test_pricing_convergence` (11), `test_pricing_elevation` (22), `test_pricing_transaction_semantics` (5), `test_trusted_pricing_context` (44), `test_item_price_guidance` (40) |
| Warehouse resolution incl. `Company.default_warehouse` fallback | **TESTED** | `test_warehouse_context` (25) |
| Selling UOM | **TESTED** | `test_selling_uom` (32) |
| Cart — projection, quantity semantics, expiry | **TESTED** | `test_cart_pricing_projection` (9), `test_cart_quantity_semantics` (6), `test_cart_to_sales_order` (6) |
| Navigation projection | **TESTED** | `test_storefront_navigation` (54) |
| End-to-end chain | **TESTED** | `test_storefront_chain` (18), `test_storefront_runtime` (95) |
| Tenant isolation (customer A vs B) | **TESTED** | `test_tenant_isolation` (17 classes/41 cases) |
| **CHECKOUT** | | |
| Contact required at Proceed | **TESTED** | `test_payment_lifecycle`, `test_proceed_to_payment_buyer` |
| Billing / shipping address required, shipping auto-fill | **TESTED** | same + `test_account_crud_audit` |
| GST taxes applied (in-state CGST/SGST) | **TESTED** | `test_proceed_to_payment_buyer` (arms India Compliance inside the test transaction) |
| GST **out-of-state / IGST** through the real Proceed | **TESTED** | `test_proceed_to_payment_buyer` (`ProceedToPaymentOutOfStateCase`, 7 — out-state template selected, IGST present, CGST/SGST absent, `place_of_supply` follows the customer address, parity holds, processor boundary intact) |
| **Proceed to Payment as a REAL Website User** | **TESTED** | `test_proceed_to_payment_buyer` (18) — the only module that runs the whole Proceed as a non-Administrator buyer, now in both jurisdictions |
| Row-level tax projection | **TESTED** | `test_row_tax_projection` (18) |
| **PAYMENT** | | |
| Draft Sales Order shape | **TESTED** | `test_commitment` (32), `test_proceed_to_payment_buyer` |
| Payment Request shape, `custom_cart`, `custom_committed_at` | **TESTED** | `test_payment_lifecycle` (27), `test_commitment`, `test_payment_cutover` (41) |
| Processor **DocShare** is document-specific and read-only | **TESTED** | `test_proceed_to_payment_buyer`, `test_public_payment_authorization` (33) |
| Token issue / rotate / revoke / expiry / replay | **TESTED** | `test_gateway_preflight` (35), `test_payment_cutover`, `test_gateway_foundation` (18) |
| Amount parity Cart = SO = PR | **TESTED** | `test_proceed_to_payment_buyer`, `test_payment_lifecycle` |
| Idempotency (repeat Proceed, changed cart, provider retry) | **TESTED** | `test_payment_idempotency` (8), `test_payment_cutover` |
| Public payment page loads with no session | **TESTED** | `test_public_payment_authorization`, `test_proceed_to_payment_buyer` |
| Processor permission surface is exactly the proven minimum | **TESTED** | `test_public_payment_authorization` |
| **Real gateway capture / webhook against live Razorpay** | **NOT TESTED** | cannot be — see §3 |
| **TARGET MANAGEMENT** | | |
| **Draft payment Sales Order excluded** (`docstatus = 1`) | **TESTED** | `test_target_management:998` `test_draft_and_cancelled_orders_are_excluded` |
| Rules, periods, slabs, progress, admin reports | **TESTED** | `test_target_management` (103), `test_target_admin` (80), `test_target_progress_block` (48) |
| Daily recalculation job | **PARTIALLY TESTED** | `recalculate_due_targets` is tested directly; the **scheduler firing it** is not |

---

## 3. Remaining gaps — stated plainly

### Never tested at all

1. **The daily scheduler actually firing** the target recalculation.
   `recalculate_due_targets` is tested directly; that the scheduler invokes it
   is not.
2. **`after_migrate()` is not atomic.** Not a coverage gap but a behaviour worth
   recording, found while writing the restore tests: recreating a Custom Field
   makes `create_custom_fields` run `frappe.db.updatedb()`, whose `ALTER TABLE`
   commits implicitly in MariaDB — in the MIDDLE of the hook. A migrate that
   fails after that point leaves a partially-applied provisioning state. It is
   idempotent, so re-running `bench migrate` repairs it.

### Tested server-side only — no browser involved

3. The **Customer → YOB Storefront Users panel UI**. Every behaviour was
   exercised against the real endpoints as a real operator, its fields and
   placement are now asserted from metadata, and the rendering was confirmed
   visually once by hand — but there is **no automated frontend test**: not the
   table, the status pills, the two dialogs, the confirm prompt, nor the
   permission-based hiding of the buttons. A JS regression would ship silently.
4. The **Angular storefront** as a whole. The backend contract is
   comprehensively tested, and the login endpoints the SPA calls are now tested
   as endpoints; the SPA itself has no test in this repository's suite.

### Requires manual browser verification

5. The storefront **login screen** — the Angular form, its error rendering and
   the session it establishes in a real browser. The server endpoint behind it
   is now covered by 38 endpoint tests, so what remains is the client.
6. The Customer panel after any Frappe/ERPNext upgrade. The metadata test
   (`CustomerStorefrontPanelFieldsCase`) now fails loudly if the section leaves
   the Portal Users tab, so this is a visual confirmation rather than the only
   line of defence.
7. The storefront product page, cart and checkout screens.

### Requires a real payment gateway

8. **Razorpay capture, signature verification against live keys, and webhook
   delivery.** The preflight, token, idempotency, amount and signature *logic*
   is tested with fixtures (`test_gateway_preflight`, `test_gateway_foundation`,
   `test_payment_cutover`); an actual authorised payment has never been run, by
   deliberate instruction.

### Cannot be safely automated

9. Real-money capture and refund.
10. Production proxy behaviour — that the edge really **overwrites**
    `X-YOB-Original-Host` on every vhost. Tests prove the server fails closed
    when the header is absent, and that login itself cannot bypass the check;
    they cannot prove the proxy is configured.
11. E-mail and SMS delivery (welcome mail, OTP). Delivery is queued and
    failures are logged, so a broken mail queue looks identical to a successful
    send from the caller's side. The *boundary* is tested with the transport
    patched; no message is ever sent by the suite.

### Resolved in this phase

**Cross-site rate-limit leakage — fixed.** The password and
OTP windows were living at raw Redis keys outside Frappe's `<db_name>|`
namespace, because `frappe.cache.incrby` is the one cache call Frappe does
not route through `RedisWrapper.make_key` — so two sites on one Redis
instance shared one window and could throttle each other. It failed *safe*
for authentication (a shared counter can only read high, so nobody gained
attempts); the problem was availability.

`rate_limit.rate_limit_key()` is now the single place any YOB auth
rate-limit key is composed, and it includes the current site. The counters
deliberately stay OUTSIDE Frappe's cache namespace: `frappe.clear_cache()`
deletes every key inside it, so namespacing them that way would let a
`bench clear-cache` or a migrate reset all rate limiting. The site segment
is percent-encoded rather than character-replaced, because replacement is
not injective — mapping `:` to `-` would make a site named `a:b` share a
bucket with one named `a-b`.

Proved by `yob_auth/tests/test_rate_limit_isolation.py` (15 tests),
including that two calls on the SAME site still share one counter — the
guard against "fixing" isolation by making every key unique and silently
switching rate limiting off.

The OTP **attempt** budget needed no fix and the same module says why: it
lives inside the challenge payload, which is stored with `set_value` and so
is already inside the per-site namespace.

**Nothing here is "fully tested."** The storefront, checkout, payment and
authentication *server* paths are tested thoroughly, including as a real
unprivileged buyer and through the real login endpoints. Browser rendering, the
Angular SPA, and anything involving a real gateway or a real proxy are not.

---

## 4. Production-readiness verdict

> ## YES, WITH THESE MANUAL CHECKS

New-customer onboarding is production-ready. The four-record SOP has been
exercised end to end on a throwaway customer — Customer → Website User →
access grant → Contact → Address → Cart → Checkout → Proceed → payment token →
public payment page — with 21/21 steps passing and every record rolled back.
The Desk panel that staff will actually use has 39 passing tests covering its
behaviour and its permission boundary, and its fields and placement are now
asserted from metadata. Authentication is no longer a blind spot: the three
login endpoints have 72 tests between them, including the proof that a refused
login creates no session and that login cannot bypass the domain guard.

**The reasons it is not an unqualified YES — each must be checked by hand:**

0. **Log in once through the Angular screen, per deployment.** The endpoints
   behind it are now tested; the SPA form, its error rendering and the browser
   session are not (gaps 4, 5).
1. **Exercise the Customer panel's two dialogs in the browser once per deploy** —
   add, create, disable, remove (gap 3). The server logic and the field
   placement are tested; the rendering is not.
2. **Complete one real payment in a controlled transaction** before trusting
   the gateway (gap 8). Everything up to the payment page is tested; capture
   has never been run.
3. **Verify the edge proxy overwrites `X-YOB-Original-Host` on every public
   vhost** (gap 10). If it does not, a client can choose which application's
   domain check it passes. No test can establish this.
4. **Confirm the scheduler and at least one worker are running** (gap 1, and
   `site-setup.md` §14), or target recalculation and queued mail silently never
   run.

**One operational note that is not a gap but will surprise staff:** all users of
one Customer **share a single Draft Cart**, because the Cart is keyed on the
Customer. Brief staff before rolling out multi-user customers.

**One upgrade note:** sites that have not taken `9784539` carry the
`on_trash` role-sync defect — deleting a storefront grant there left
`YOB Storefront Buyer` attached. Run `bench migrate` and spot-check any user
whose grant was deleted previously.
