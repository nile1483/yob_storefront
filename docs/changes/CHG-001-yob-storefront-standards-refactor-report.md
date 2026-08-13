# CHG-001 — Compliance Report

Status: `Partially complete — F-04 blocked on client coordination, F-12 blocked by owner decision`

> **Update after CHG-002.** F-01, F-05 and F-11 are **resolved**; F-04 is
> prepared but not applied; F-12 stays blocked.
>
> | Finding | New status | Evidence |
> | --- | --- | --- |
> | F-01 core boundary | **Refactored** | `yob_api` built in `yob_core`; applied to **33/33** endpoints, 0 missing |
> | F-05 traceback leaks | **Refactored** | Unknown faults now return `internal_server_error` + correlation ID; poisoned-exception test shows **zero** leaked fragments. Broad per-endpoint catches were **not** restored |
> | F-11 test gap | **Refactored** | Trusted-host request helper added; 6 new domain tests (valid host passes, missing fails closed, unapproved fails closed, port normalised, no-request fails closed). Full suite green **with the allow-list armed** |
> | F-03/F-14 Razorpay | **Refactored** | All SDK calls now in `integrations/razorpay/client.py`; `create_order` payload proved byte-identical |
> | F-04 HTTP methods | **Refactored** | Owner confirmed no client exists. Applied: 12 read-only → `GET`, 16 mutating → `POST`; 33/33 declared. Mutating GET is now rejected, closing the CSRF-bypass hole. Contract published in `docs/contracts/api-method-matrix.md` |
> | F-12 `cms.get_config` | **Blocked** | Field provable (`allowed_payment_methods`, type Data) but it has **zero** references, is NULL live, and cannot produce the required `[{"mode_of_payment":…}]` shape. Owner directed it stay blocked |
>
> **Verification site: `test.localhost`.** `yob.localhost` was not migrated,
> reinstalled or subjected to write-path tests. Final result: **92 ran, 0
> failures, 0 errors.**
Date: `2026-08-08`
Bench: `/home/shayona/frappe_local/frappe-bench` (native WSL2, not Docker)
Site used: `yob.localhost` — local development site. **No production site was used.**

## 1. Environment confirmed

| Fact | Value |
| --- | --- |
| frappe / erpnext / payments / india_compliance | 16.30.0 / 16.31.1 / 0.0.1 (`version-16`) / 16.8.2 |
| yob_core / yob_auth / yob_storefront | 0.0.1 / 0.1.0 / 0.0.1 (`main`) |
| Designated disposable test site | **Unknown** — `yob.localhost` is the only site; it holds the active dev data |
| `bench migrate` run | **Not run** — no disposable site (CHG-001 §11) |
| SPA/client repository | **Unknown — not located.** Endpoint compatibility judged from source + contracts only |

## 2. Inventory

**Inspected:** 20 Python modules under `api/`, `services/`, `utils/`, `integrations/`; `hooks.py`, `install.py`, `modules.txt`, `patches.txt`, `patches/v1_0/`; 2 fixtures; 2 custom-field JSON; workspace, desktop_icon, dashboard JSON; 3 test modules; all docs under `docs/`.

**DocTypes (7, all verified against JSON):** `YOB Store Settings` (Single), `Category` (Tree), `Cart`, `Cart Item` (Child), `Payment Method`, `Payment Method Assignment`, `Razorpay Payment Log`. Child DocType has no standalone navigation — compliant.

**Endpoints:** 33 whitelisted across the platform — `yob_core` **0** (library only, correct), `yob_auth` 5, `yob_storefront` 28. Guest routes: exactly 3, all in `api/payment.py`, matching `docs/contracts/api-compatibility.md`.

**Dependency map:** `required_apps = [yob_core, yob_auth, erpnext, payments, india_compliance]`. No reverse or sibling dependency. `yob_core` imports nothing upward; `yob_auth` imports no business app. **Dependency law: compliant.**

## 3. Findings

| ID | Area | File/symbol | Status | Rule | Finding | Action | Risk | Verification |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| F-01 | API boundary | all 28 storefront endpoints | **Blocked** | `api.md` §decorator order | The mandated `yob_api` core boundary **does not exist in `yob_core`**. Endpoints are `@frappe.whitelist()` + `@require_application` only | Build boundary in `yob_core`, then apply | High — affects checkout/payments | Confirmed absent: no `yob_api` symbol in `yob_core` |
| F-02 | Security | `services/payment_service.get_razorpay_settings` | **Refactored** | `security.md` "do not store secrets" | Decrypted Razorpay `api_secret` written to Redis (`use_redis_auth: false`) | Helper deleted; credentials read via adapter, never cached | Medium → resolved | `grep api_secret` clean outside adapter |
| F-03 | Integrations | `api/payment.py`, `services/payment_service.py` | **Refactored** | §6.6 provider translation in adapters | 4 direct `razorpay.Client(...)` constructions duplicating auth | Added `integrations/razorpay/client.py`; all construction centralised | Low | `grep razorpay.Client` outside adapter → empty; 74 tests unchanged |
| F-04 | API/CSRF | 28 storefront endpoints | **Blocked** | `api.md` "declares HTTP method" | No `methods=` on any endpoint; Frappe CSRF-checks only unsafe verbs, so `cart.clear_cart`, `address.*`, `checkout.*` execute via **GET with no token** | Add `methods=["POST"]` to mutating endpoints | High — breaks any client using GET | Reproduced live: GET `clear_cart` returned 200 |
| F-05 | Error handling | `api/cart.py:444`, `api/catalog.py:268`, `api/address.py:221`, `services/payment_service.py:266` | **Blocked** | `AGENTS.md` "never expose a traceback" | 4 commented-out `try:` blocks; unexpected faults escape as raw Frappe traceback | Correct fix is F-01 boundary, not restoring broad catches (`error-handling.md` forbids per-endpoint `except Exception`) | Medium | Reproduced live earlier |
| F-06 | Auth | `utils/context.py` | **Compliant** | §6.5 thin adapter | 59 lines, no token/session/password logic; uses `auth_context` only | None | None | Source read in full |
| F-07 | Response | `api/response.py` | **Compliant** | `error-handling.md` compat modules | Pure re-export of `yob_core` helpers; 0 defs/classes | None | None | `grep -cE '^def \|^class '` → 0 |
| F-08 | Transactions | all services | **Compliant** | `api.md` no manual commit | No `frappe.db.commit()` outside tests | None | None | Repo-wide grep |
| F-09 | Naming | `utils/constants.py` `ErrorCode` | **Refactored (pending)** | naming standard | Legacy uppercase class, zero references — dead code | Remove | None | Grep shows no importers |
| F-10 | Docs | `docs/architecture.md` §Authentication | **Exception** | §6.8 docs match source | Describes a bearer-token/`require_login` flow that no longer exists; `tests/test_rename.py` actively forbids those symbols | Rewrite section | None (doc only) | Cross-checked against `utils/context.py` |
| F-11 | Testing | `tests/test_tenant_isolation.py:228` | **Blocked (test gap)** | §11 tests must pass | 2 errors: tests call the decorator with no HTTP request, so the domain allow-list fails closed and returns an envelope dict | Set a request with `X-YOB-Original-Host` in test setup | Low — test-only | Proved: same call with mocked header returns `AuthContext` |
| F-12 | API bug | `api/cms.get_config` | **Blocked** | correctness | Reads `allowed_payment_modes`; the field is `allowed_payment_methods`, so it always returns `[]` | Fix field name | Medium — changes a response value clients may depend on | Field list read from `yob_store_settings.json` |
| F-13 | Structure | `api/file_hooks.py` | **Exception** | directory-structure | `doc_events` handler placed under `api/`; not a whitelisted endpoint | Move to a non-API module; dotted path is internal (referenced only by `hooks.py`) | Low | `hooks.py:156-160` |
| F-14 | API size | `api/address.py` 532, `api/cart.py` 519, `api/payment.py` 491 | **Partially refactored** | §6.6 thin adapters | Workflows inline in API modules | Razorpay translation extracted (F-03); cart/address/checkout workflow extraction remains | Medium | Line counts |
| F-15 | Input validation | 19 endpoints across `yob_auth` + `yob_storefront` | **Refactored** | `error-handling.md` "missing param = known validation error" | Required params without defaults produced raw `TypeError` 500 with traceback and signature, **before** auth/rate-limit ran; guest payment routes exposed it unauthenticated | All 19 given defaults + explicit guards returning published codes | Medium → resolved | Signature audit 19 → **0**; all 18 verified over HTTP |
| F-16 | Security | `api/payment.get_checkout_data` | **Refactored** | §6.7 validate before lookup | `token=None` renders as `WHERE custom_checkout_token IS NULL`, matching any Payment Request created outside checkout — guest data disclosure | Guard before lookup; answers identically to a wrong token | **High → resolved** | Generated SQL inspected |

## 4. Compatibility exceptions retained

DocType names, fieldnames, Module Def `YOB Storefront`, the historical `YOB` Workspace identity, all published dotted API paths, request keys, response fields, and all 36 published error codes are unchanged. `api/response.py` retained as a compatibility re-export module.

## 5. Tests and commands executed

| Command | Result |
| --- | --- |
| `python -m py_compile` on every edited file | **Pass** (6 files) |
| Signature audit (whitelisted params without defaults) | **19 → 0** |
| Unit suite: core dependency-direction, core envelope, auth contract, storefront rename/response/isolation | **74 ran, 0 failures, 2 errors** — identical before and after; both are F-11, pre-existing |
| Live HTTP suite `yob-edge/smoke.sh` (login, protected endpoints, error envelope, domain control, CSRF) | **14/14 pass** |
| Live verification of 18 missing-parameter cases | **All enveloped**, no traceback |
| `bench run-tests` | **Not run** — writes test records into the only available site |
| `bench migrate` | **Not run** — no disposable site |
| Clean-site install | **Not run** — no disposable site |

## 6. Blocked — approval required

1. **F-01 core API boundary.** `yob_api` does not exist in `yob_core`. Building it is a shared-platform change outside this storefront change document, and applying it alters error behaviour on every endpoint including checkout and payments.
2. **F-04 `methods=` restriction.** The correct CSRF fix, but any client calling a mutating endpoint by GET would break.
3. **F-05 traceback leaks.** Correct fix depends on F-01.
4. **F-12 `allowed_payment_modes`.** Fixing changes a published response value.
5. **F-11 test gap.** Test-only, but changes an approved test.

## 7. Remaining risks and follow-up

- Until F-01/F-04/F-05 are approved, unexpected faults still leak tracebacks and mutating endpoints remain CSRF-bypassable via GET.
- `api/payment.py` still calls `client.order.*` directly (F-14); the adapter exists to receive them.
- Recommended next change document: **CHG-002 — yob_core API boundary (`yob_api`)**, covering `yob_core`, `yob_auth` and all solution apps together.
