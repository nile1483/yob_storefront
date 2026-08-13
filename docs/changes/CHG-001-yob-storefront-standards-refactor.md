# CHG-001 — YOB Storefront Standards Audit and Refactor

Status: `Approved`

Owner: `Nilesh`

Reviewers: `Nilesh; technical reviewer to be confirmed`

Date: `2026-08-08`

Target release: `Unknown — confirm before implementation`

## 1. Task classification

- Type: `major refactor and compliance audit`
- Owning app: `yob_storefront`
- Related apps: `yob_core`, `yob_auth`
- Framework line: `Frappe/ERPNext v16; verify exact installed versions in docs/context.md and the current bench`
- Business behavior change intended: `No`
- Public API breaking change intended: `No`
- Stored-data migration intended: `No`
- Data patch permitted by this change: `No`

## 2. Problem

`yob_storefront` was developed before the current YOB engineering standards were
defined. Its existing business purpose and workflows must remain, but its source
may not follow the approved architecture, placement, naming, API, authentication,
permission, testing, and documentation rules.

The current source is authoritative evidence of what exists, but existing code is
not automatically considered compliant. Historical/generated documentation is
also not proof of the current implementation.

## 3. Required outcome

Audit the complete current `yob_storefront` app and directly refactor every safe,
non-breaking non-compliant implementation so that:

1. Storefront business purpose and approved business rules remain unchanged.
2. `yob_storefront` owns only storefront/catalog/cart/pricing/checkout/order/
   payment/CMS behavior.
3. Shared response/error behavior comes from `yob_core`.
4. Authentication, identity, session, and application access come from
   `yob_auth`.
5. API adapters and DocType controllers remain thin; cross-document workflows
   live in named services; dependency/provider translation lives in integrations.
6. Existing public API paths and compatible wire behavior remain available.
7. Server-side permissions, record ownership, customer isolation, payment
   safeguards, and rollback behavior are enforced and tested.
8. Internal names and file placement follow the YOB standards wherever they can
   be changed without breaking a stored or published identity.
9. Tests and documentation describe the final implementation, not an intended or
   historical implementation.

Do not stop after producing the audit. Implement all changes that are permitted
by this document, then produce the final compliance report.

## 4. Instruction precedence

When two instructions conflict, use this order:

1. The user's current instruction.
2. This approved change document.
3. Current project facts in `docs/context.md`.
4. The nearest app-specific `AGENTS.md`.
5. The YOB platform standards under `yob_core/docs/platform/`.
6. Verified behavior of the installed Frappe/ERPNext version.
7. Existing approved tests and published compatibility contracts.
8. Safe existing project patterns.
9. Engineering judgment only for small, reversible decisions.

Do not guess a material project fact. Record `Unknown — confirm before
implementation` and ask when the unknown blocks safe implementation.

## 5. Required reading before editing

Read the current versions of:

- repository/bench-level `AGENTS.md`, if present;
- `yob_storefront/AGENTS.md`;
- `yob_storefront/docs/context.md`;
- `yob_storefront/docs/contracts/api-compatibility.md`;
- `yob_storefront/docs/contracts/error-catalog.md`;
- `yob_storefront/docs/security.md`;
- `yob_core/docs/platform/AGENTS.md`;
- `yob_core/docs/platform/context.md`;
- `yob_core/docs/platform/architecture.md`;
- `yob_core/docs/platform/directory-structure.md`;
- `yob_core/docs/platform/naming-conventions.md`;
- `yob_core/docs/platform/api.md`;
- `yob_core/docs/platform/error-handling.md`;
- `yob_core/docs/platform/authentication.md`;
- `yob_core/docs/platform/permissions.md`;
- `yob_core/docs/platform/security.md`;
- `yob_core/docs/platform/development.md`;
- `yob_core/docs/platform/testing.md`;
- `yob_core/docs/platform/deployment.md`;
- accepted ADRs under `yob_core/docs/platform/decisions/`;
- all current Storefront source, DocType JSON, hooks, fixtures, patches, tests,
  Workspaces, reports, public assets, and relevant frontend/client calls.

If a listed path differs in the current repository, locate the corresponding
document and record the actual path in the report.

## 6. Scope of audit

Inspect at least the following:

### 6.1 Application structure and dependencies

- Python package and Frappe module layout.
- `hooks.py`, `modules.txt`, `patches.txt`, `pyproject.toml`, package metadata,
  fixtures, assets, and app dependency declarations.
- Imports between `yob_storefront`, `yob_core`, `yob_auth`, ERPNext, Payments,
  provider SDKs, and any sibling solution app.
- Circular, reverse, undeclared, optional-at-runtime, or duplicate dependencies.
- Large generic `utils.py`, `helpers.py`, `common.py`, and mixed-responsibility
  modules that should become named services/adapters.

Required dependency direction:

```text
yob_auth depends on yob_core
yob_storefront depends on yob_core and yob_auth
yob_core and yob_auth never depend on yob_storefront
yob_storefront does not depend on a sibling solution app without an accepted ADR
```

### 6.2 Naming and placement

Check app/module names, Python files, classes, functions, constants, DocTypes,
fields, API modules/methods, services, integrations, hooks, jobs, roles,
Workspaces, tests, and error codes against the naming standard.

Internal Python names may be corrected directly when all references and tests are
updated. Do not rename a public dotted API, DocType, existing fieldname, Module
Def, Workspace stored identity/route, role, fixture identity, or database-backed
record merely for cosmetic compliance. Record such names as compatibility
exceptions unless separately approved.

### 6.3 DocTypes and Desk artifacts

- Inspect every Main, Single, Child, Tree, and other DocType type.
- Verify ownership, module, naming, flags, autoname, fields, indexes,
  permissions, lifecycle controller, client script, and tests.
- Verify that Child DocTypes are used through their parents and receive no
  standalone Workspace/sidebar entry.
- Verify that `YOB Storefront` has a permission-aware Apps Page entry and that
  every Desk-visible module owns an appropriate source-controlled Workspace.
- Preserve the historical `YOB` Workspace identity until its stored route and
  compatibility impact are explicitly approved for migration.
- Treat ERPNext `Sales Order`, Payments `Payment Request`, and other dependency
  DocTypes as dependency-owned. Extend them only through supported Frappe hooks
  or extension mechanisms; do not edit dependency app source.

Known Storefront-owned DocTypes to verify, not blindly assume:

- `YOB Store Settings`
- `Category`
- `Cart`
- `Cart Item` (Child)
- `Payment Method`
- `Payment Method Assignment`
- `Razorpay Payment Log`

### 6.4 API, response, and error handling

- Inventory every whitelisted method, dotted path, HTTP method, guest setting,
  request parameter, response field, HTTP status, error code, and caller.
- Apply the explicit `yob_core` API boundary to all YOB-owned whitelisted
  methods without changing ordinary Frappe Desk or `/api/resource` behavior.
- Use `yob_core` success/error primitives; remove duplicate Storefront
  implementations. A compatibility module may re-export core names but must not
  implement them again.
- Keep Frappe's outer `message` wrapper and the approved inner YOB envelope.
- API methods must validate transport input, call one owning service, and return
  or raise through the standard boundary. They must not contain large workflows,
  manual commits, or broad exception conversion.
- Preserve published endpoint paths, parameters, defaults, methods, envelopes,
  statuses, and error-code values. Use compatibility wrappers for moved internal
  code.
- If current source and documented contracts disagree, verify actual clients and
  tests. Do not silently choose either version.

### 6.5 Authentication and identity

- Remove reachable Storefront implementations of password, OTP, session,
  application access, impersonation, or identity fallback logic.
- Protected external endpoints must use `yob_auth.require_application` and only
  server-generated trusted `auth_context` as identity authority.
- A legacy client-supplied Customer/company may be compared to trusted context
  for compatibility and then discarded; it must never authorize access.
- Normal Desk-internal actions must continue to use Frappe session and DocType
  permissions rather than Storefront application-access rules.
- `utils/context.py` may remain only as a thin adapter from trusted AuthContext
  to Storefront domain identity.

### 6.6 Services, controllers, hooks, and jobs

- Move cross-DocType business workflows from APIs/controllers into focused
  service modules while preserving behavior.
- Keep DocType controllers responsible for document-local lifecycle invariants.
- Put ERPNext, Payments, Razorpay, and other provider translation in named
  Storefront integration adapters.
- Keep hooks and job entry points thin and delegate to the same services used by
  synchronous requests.
- Verify transaction boundaries, retries, idempotency, queues, timeouts, and
  failure recording. Do not add manual `frappe.db.commit()` to normal services.

### 6.7 Permissions, security, and data isolation

- Verify server-side permissions for every read and write path; client-side
  checks alone are insufficient.
- Every `frappe.get_all`, raw SQL access, `ignore_permissions=True`, and
  privileged dependency call must have independent trusted authorization,
  written justification, and a negative isolation test.
- Prove that Customer A cannot list, read, infer, modify, or pay for Customer B's
  cart, contacts, addresses, orders, checkout, or payment records.
- Guest routes are forbidden unless explicitly inventoried and protected by a
  scoped token/HMAC/provider signature validated before protected lookup or
  mutation.
- Payment flows must verify resource ownership, state, amount, currency,
  signature, expiry, replay, and idempotency. Never trust client-provided totals
  or payment status.
- Do not preserve a confirmed insecure fallback for compatibility. Remove or
  secure it, add regression tests, and clearly report any client impact.
- Do not log secrets, authorization/cookie headers, OTPs, session IDs, payment
  tokens/signatures, complete request bodies, or unnecessary personal data.

### 6.8 Tests and documentation

- Locate and run existing tests before changing behavior when the environment
  permits.
- Add characterization/contract tests for approved current behavior before
  moving or renaming implementation code.
- Add missing unit, integration, API contract, permission, isolation, payment,
  idempotency, rollback, Workspace, migration-sync, and dependency-direction
  tests as applicable.
- Update Storefront documentation, API inventory, error catalog, DocType list,
  security notes, and examples to match the final source.

## 7. Audit classification and report

Create:

```text
docs/changes/CHG-001-yob-storefront-standards-refactor-report.md
```

For every finding, record:

| Field | Required content |
| --- | --- |
| ID | Stable finding number |
| Area | Structure, naming, API, auth, DocType, permission, test, etc. |
| File/symbol | Exact path and relevant symbol/DocType |
| Status | `Compliant`, `Refactored`, `Exception`, `Blocked`, or `Not applicable` |
| Rule | Standard that applies |
| Finding | Evidence from current source |
| Action | Exact change made or required |
| Compatibility/data risk | None, low, medium, or high with reason |
| Verification | Test or inspection proving the result |

The report must also contain:

- files and DocTypes inspected;
- dependency/import map;
- public endpoint inventory and client references;
- duplicate response/auth/helper inventory;
- naming exceptions retained for compatibility;
- tests run and exact results;
- remaining blockers and recommended next change document.

Do not mark an item compliant only because a document says it should be. Verify
the source and, where material, its tests/runtime behavior.

## 8. Acceptance criteria

1. The entire current Storefront app is inventoried and assessed against the
   applicable YOB standards.
2. Every finding has source evidence and a clear disposition.
3. All safe, non-breaking non-compliant source is directly refactored; the task
   is not completed with only a report.
4. Storefront contains no reachable duplicate response/error implementation and
   no authentication/session/application-access implementation.
5. All YOB Storefront endpoints use the approved core response/error boundary;
   protected external endpoints use trusted AuthContext/application access.
6. APIs/controllers are thin, workflows are in services, and dependency/provider
   translation is in integrations.
7. Direct dependencies are declared and no reverse/sibling dependency violation
   remains.
8. Existing business outcomes and published compatible API behavior remain
   covered by tests.
9. Cross-customer negative tests, permission tests, guest/payment tests,
   idempotency tests, and rollback tests pass for applicable flows.
10. DocType JSON, hooks, fixtures, Workspace artifacts, and docs are synchronized
    and verified on a designated test site when available.
11. No released patch is edited, no data patch is added, no production data is
    transformed, and no production deployment is performed.
12. All unimplemented material changes are listed as blocked items with their
    risk and required approval.

## 9. Direct-change and no-patch policy

Permitted without another approval:

- rename/move private internal Python modules, classes, and functions while
  updating all imports and tests;
- split large APIs/controllers into services and integrations;
- replace duplicate response/auth helpers with imports or compatibility
  re-exports from the owning app;
- improve validation, authorization, logging safety, typing, comments, and tests;
- correct dependency declarations and internal file placement;
- make additive DocType JSON/Workspace metadata changes followed by normal
  schema synchronization on a designated test site, if existing data and
  compatibility are not changed.

Not permitted under this change without a separate approved decision/change:

- rename or delete an existing DocType, fieldname, Module Def, Workspace stored
  identity/route, role, public dotted API, request key, response field, or
  published error code;
- add a data patch or a new `patches.txt` entry;
- edit an already released patch;
- backfill, reinterpret, merge, delete, or move stored production data;
- make a breaking API, permission, authentication, tenant, financial, inventory,
  pricing, tax, checkout, order, or payment behavior change;
- edit Frappe, ERPNext, Payments, or another dependency app's source;
- deploy to production or run migration against a production site.

If a standards violation can be corrected only through a prohibited change,
leave the stored/public identity intact, record it as `Blocked` or `Exception`,
and propose a separate change document. Do not create a fake direct-file
workaround that loses data or silently breaks callers.

## 10. Implementation sequence

1. Check repository instructions and working-tree state; preserve unrelated user
   changes.
2. Confirm installed versions, test site, dependency availability, and relevant
   clients. Record unknown blockers.
3. Inventory source, DocTypes, hooks, APIs, fixtures, patches, Workspaces,
   dependencies, callers, and tests.
4. Run the available baseline tests and capture approved current contracts.
5. Create the initial compliance report and prioritize findings by security,
   compatibility, correctness, and maintainability.
6. Refactor in small responsibility-based batches in this order:
   response/error boundary; identity/auth boundary; permissions/isolation;
   API/service/controller separation; integrations/jobs/hooks; naming/placement;
   tests/docs.
7. Run relevant focused tests after each batch.
8. Run the complete verification suite, update documentation, and finish the
   report and this file's completion evidence.

## 11. Required verification

Use commands appropriate to the current bench and record the exact commands and
results. At minimum, attempt or explain why unavailable:

```text
bench --site <designated-test-site> run-tests --app yob_storefront
bench --site <designated-test-site> run-tests --app yob_auth
bench --site <designated-test-site> run-tests --app yob_core
bench --site <designated-test-site> migrate
```

Also verify:

- Python syntax/static checks configured by the repository;
- clean import/dependency-direction checks;
- all documented Storefront endpoint contract tests;
- Customer A versus Customer B isolation;
- legacy parameter match/mismatch behavior;
- guest token/signature expiry, replay, wrong-resource, amount, currency, and
  idempotency cases;
- rollback after expected and unexpected failures;
- normal Desk login and DocType permission behavior;
- Workspace/Apps Page behavior after normal sync;
- relevant SPA/client smoke tests when client source is available;
- clean-site install only if a safe disposable site is available.

Never use a production site for test migration. Do not claim a test passed when
it was not run; record `Not run` and the reason.

## 12. Deployment and recovery

- Deployment is outside this change unless separately instructed.
- Expected source deployment order:

```text
yob_core -> yob_auth -> yob_storefront -> dependent client
```

- Prefer backward-compatible source changes and compatibility wrappers.
- Because this change permits no data transformation, rollback should normally
  be a code rollback. Document any metadata sync that would make old code
  incompatible before performing it.
- Record all required `bench migrate`, asset build, cache clear, restart, and
  smoke-test steps for the eventual release; do not execute production steps.

## 13. Open facts to confirm

- Exact repository/bench path: `Unknown — inspect current environment`
- Designated disposable test site: `Unknown — confirm before running migrate`
- Production deployment topology: `Unknown — not required for source refactor`
- Active SPA/client repository and version: `Unknown — locate or confirm`
- Approved current API compatibility baseline: `Verify source, clients, tests,
  and docs/contracts/api-compatibility.md`
- Current production data volume and stored naming usage: `Unknown — required
  only if a later data/schema identity change is proposed`

Only the unknowns that block a specific safe change require an immediate
question. Continue with inspection and independent non-blocked work.

## 14. Completion evidence

Complete this section after implementation:

- Files inspected: `Pending`
- Files changed: `Pending`
- DocTypes inspected: `Pending`
- Public endpoints verified: `Pending`
- Tests run/results: `Pending`
- Test/migration site used: `Pending`
- Documentation updated: `Pending`
- Compatibility exceptions retained: `Pending`
- Blocked/proposed follow-up changes: `Pending`
- Final result: `Pending`

