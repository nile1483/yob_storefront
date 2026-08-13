# YOB Storefront App Documentation

`yob_storefront` is one optional YOB solution app. This folder owns Storefront-
specific context, architecture and flows, DocTypes, API compatibility, error
codes, migrations, examples, and open items. Nothing here automatically applies
to School, Executive, or another solution app.

Platform-wide standards live in
[`../../yob_core/docs/platform/`](../../yob_core/docs/platform/) and take
precedence. Read the platform `AGENTS.md` and `context.md`, then this app's
[`AGENTS.md`](../AGENTS.md), then the documents selected below.

## What to read for each task

| Task | Minimum reading |
| --- | --- |
| Understand an existing flow before changing it | `architecture.md`, then the affected source |
| Change a catalog, cart, pricing, or coupon behavior | `architecture.md`, `contracts/api-compatibility.md`, `contracts/error-catalog.md` |
| Change checkout, payment, or Razorpay verification | `architecture.md`, `security.md`, `contracts/error-catalog.md` |
| Add or change a DocType | `doctypes.md`, `context.md`, plus platform `doctypes.md` and `naming-conventions.md` |
| Add or change a guest/token endpoint | `security.md`, `architecture.md`, plus platform `api.md` and `security.md` |
| Change a published endpoint, parameter, or error code | `contracts/api-compatibility.md`, `contracts/error-catalog.md` |
| Desk navigation, Workspace, or sidebar change | `doctypes.md`, plus platform `workspaces.md` |
| Release or migration | `open-items.md`, plus platform `deployment.md` and `migration-plan.md` |

## Document purposes

| Document | It answers |
| --- | --- |
| `context.md` | What does Storefront own, and what stays dependency-owned? |
| `architecture.md` | How do the existing entry points, data model, and flows actually work? |
| `doctypes.md` | Which Storefront DocTypes exist and how are they navigated? |
| `security.md` | Which Storefront-specific threats and guest/payment rules apply? |
| `contracts/api-compatibility.md` | Which endpoints, parameters, and shapes must stay compatible? |
| `contracts/error-catalog.md` | Which storefront error codes are published? |
| `open-items.md` | Which Storefront facts or risks still require confirmation? |
| `examples/` | Illustrative target patterns, not drop-in production code |

`architecture.md` is an existing-architecture baseline: it records how the app
behaves today, not an approved target design. Verify current source before
relying on any flow in it, and update it in the same task that changes the
behavior it describes.
