# YOB Storefront - Frappe v16 Application

Start with the platform guideline at the repository root: [`AGENTS.md`](../../AGENTS.md).
This file covers what is specific to `yob_storefront`.

## Project Overview

`yob_storefront` is the **ecommerce business layer** of the YOB platform. It is
one of three apps, not a monolith:

```text
frappe → yob_core → yob_auth → yob_storefront
```

- **`yob_core`** owns the API response envelope, HTTP constants and generic
  error codes.
- **`yob_auth`** owns *all* authentication and application access.
- **`yob_storefront`** owns the ecommerce domain and nothing else.

The application provides:

- Store Configuration
- Customer APIs
- Cart
- Checkout
- Pricing
- Coupons
- Sales Order Integration
- Payment Integration
- Razorpay
- Menu APIs
- Cache Layer
- Utilities
- Background Jobs

## Boundaries — do not cross these

**This app has no authentication of its own.** There is no local password, OTP
or session implementation, and no legacy fallback. If `yob_auth` is missing the
app must fail loudly, not degrade.

- Authorize every external endpoint with
  `@require_application(STOREFRONT_APP, profile_doctype="Customer")`.
- Read identity **only** from the server-generated `auth_context`, via
  `yob_storefront.utils.context.get_storefront_customer()`. A caller-supplied
  `customer` parameter is only ever allowed to *agree* with it — see
  `assert_customer_matches()`.
- Never re-implement, wrap or "extend" auth here.

**This app owns no generic response code.** `yob_storefront/api/response.py` is
a compatibility module: it re-exports the generics from `yob_core`, takes
`APPLICATION_ACCESS_DENIED` from `yob_auth`, and declares only the storefront
error codes. Keep importing from `yob_storefront.api.response` in this app —
every storefront module already does — but never add a helper implementation to
it.

**Storefront error codes stay here.** Never promote `cart_empty`,
`coupon_invalid`, `order_not_found` or any sibling into `yob_core`. Never rename
one: they are published contract.

## Goals

- High Performance
- Low Database Queries
- Clean Architecture
- Easy Maintenance
- Production Ready
- ERPNext Best Practices

## Coding Standards

- Keep API methods thin
- Business logic belongs in services
- Avoid duplicate code
- Prefer caching for master data
- Minimize DB calls
- Use frappe.qb where appropriate
- Use constants instead of magic strings
- Use transactions correctly
- Validate every public API
- Follow SOLID principles

## Response contract

Every whitelisted endpoint answers through `success_response`,
`error_response`, `errors_response` or `server_error` — never a bare dict.

- A bare `except Exception` must return `server_error` (500). Do not disguise
  an unexpected fault as a business error; a test enforces this.
- Error codes must be declared constants, never inline literals:
  `error_response(CART_EMPTY, "...")`, not `error_response("cart_empty", "...")`.
- Never return a traceback.

Full contract: `apps/yob_core/docs/api-contract.md`.

Exceptions to the scans are configuration in
`tests/test_response_contract.py` (`CONTRACT_EXEMPT`, `DELEGATING_HELPERS`) —
add to those lists with a comment saying why, rather than loosening the check.

## Tests

```bash
bench --site <site> run-tests --app yob_storefront
```

Test storefront codes, endpoint conformance and this app's exemptions. The
generic envelope behaviour is tested once in `yob_core` — do not copy it here.

## Review Checklist

Review the application for:

- Architecture
- Folder Structure
- Naming
- Performance
- Database Queries
- Cache
- Security
- Transactions
- Logging
- Exception Handling
- ERPNext/Frappe Best Practices
- Maintainability
- Scalability

When suggesting improvements:

- Explain WHY
- Explain Impact
- Suggest Better Code
- Suggest Better Architecture
- Prefer Frappe v16 best practices