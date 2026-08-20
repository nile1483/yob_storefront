# YOB Storefront Open Items

| Priority | Item | Required action |
| --- | --- | --- |
| Blocking | Customer/storefront isolation model | Approve separate-site versus in-site model and add cross-customer tests |
| High | Guest payment token/HMAC lifecycle | Document issuer, expiry, uniqueness, signed data, replay prevention, amount/currency validation, and idempotency |
| High | Storefront direct dependency list | Scan current imports/metadata and declare only confirmed dependencies |
| Medium | Historical missing SPA endpoints | Confirm current SPA; remove calls or approve/implement contracts |
| Medium | Payment Method field-name mismatch | Inspect JSON/query, add failing test, then fix compatibly |
| Medium | Stale/unwired Custom Field fixture | Compare live metadata, source fixture, and hooks; approve export/migration |
| Medium | Historical Workspace identity (`YOB` versus target label) | Inspect stored/source Workspace; preserve or migrate through an approved change |
| Medium | Payment log retention/privacy | Define access, retention, export, deletion/anonymization, and secret masking |

## Phase 24A variant decisions — APPROVED and implemented in 24B

| # | Decision | Outcome |
| --- | --- | --- |
| 1 | Product page model | one page per FAMILY; simple Items unchanged |
| 2 | Listing model | one card per family, `price_state = select_options`, no invented price |
| 3 | Addressing | slug belongs to simple Items and families; variants carry none; patch + uniqueness hook |
| 4 | Unavailable combinations | server sends only real combinations; clients disable the rest |
| 5 | Resolution | `catalog.resolve_variant` published, returning the full resolved detail |

Recorded 2026-08-20. See `context.md`, "Variant products (Phase 24A audit, Phase 24B build)".
