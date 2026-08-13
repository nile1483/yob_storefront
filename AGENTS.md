# `yob_storefront` — App-Specific Mandatory Rules

Read the platform standard under `../yob_core/docs/platform/`, this app's
`docs/contracts/api-compatibility.md`, and the shared API, permission, and
security standards first.

## Purpose

`yob_storefront` is one independently installable solution app. It owns all
catalog, product presentation, cart, pricing, coupon,
address/contact storefront flow, checkout, order, payment, CMS/menu/cache, and
provider orchestration behavior.

It declares `yob_core`, `yob_auth`, and every directly imported dependency app.
It never imports a sibling solution app without an accepted ADR, and another
solution app never needs Storefront merely because both use YOB platform apps.

## Authentication boundary

This app contains no password, OTP, session, application-access, or user
impersonation implementation and no fallback if `yob_auth` is absent. Protected
external endpoints use `require_application`. Customer identity comes only from
trusted `auth_context` through a thin adapter.

A client-supplied Customer/company may only be checked for equality and then
discarded. Normal Desk-internal methods use Frappe DocType permissions, not
storefront application access.

## Response/error boundary

Every YOB whitelisted method uses the core API boundary. The storefront owns
only storefront error codes. A compatibility response module may re-export core
names, but it implements nothing. API methods do not use broad catches to hide
unexpected faults.

## Guest/payment rules

Only explicitly inventoried token/HMAC routes may be guest-accessible. Validate
capability before protected lookup/mutation; derive Customer through server
links; verify amount/currency/state; make retries idempotent; never trust a
request payment result.

## Code structure

Keep API adapters and DocType controllers thin. Place use cases in services and
ERPNext/Payments/Razorpay translation in integration adapters. Use
`ignore_permissions=True` only after independent trusted ownership/capability
validation with a written reason and negative isolation test.

## Desk navigation

Configure one permission-aware `YOB Storefront` Apps Page entry that opens the
primary Storefront Workspace. Every additional Desk-visible Storefront module
owns its own standard public Workspace and sidebar context. Main and Single
DocTypes may be linked according to roles; Child DocTypes never receive
standalone navigation.

## Required tests

Test response/decorator coverage, all storefront errors, customer A versus B,
application/profile denial, legacy parameter mismatch, cart ownership, address
ownership, order isolation, payment token expiry/replay/signature/amount/currency,
idempotency, rollback on failure, Desk permissions, and endpoint/client
compatibility.
