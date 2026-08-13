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
