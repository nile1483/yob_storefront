# YOB Storefront

Ecommerce/B2B ordering solution app for Frappe v16. Owns catalog, cart, pricing,
coupons, storefront contact/address flows, checkout, orders, payment
orchestration, and CMS/menu/cache behavior.

`yob_storefront` is one solution app on the YOB platform, not the platform
itself. It is a sibling of future solution apps such as `yob_school`; another
site may install those without installing Storefront.

---

## Requirements

Versions recorded on 2026-08-08 from the running bench:

| App | Version | Branch |
| --- | --- | --- |
| Frappe Framework | 16.30.0 | `version-16` |
| ERPNext | 16.31.1 | `version-16` |
| Payments | 0.0.1 | `version-16` |
| India Compliance | 16.8.2 | `version-16` |
| `yob_core` | 0.0.1 | `main` |
| `yob_auth` | 0.1.0 | `main` |

`payments` does not maintain its `0.0.1` as a release identifier — use the
branch for compatibility checks.

### Declared dependencies

`hooks.py` declares all five directly:

```python
required_apps = ["yob_core", "yob_auth", "erpnext", "payments", "india_compliance"]
```

`install-app` fails if any is missing. `india_compliance` is required because
Storefront writes the GST fields it adds to `Address` (`gstin`, `gst_category`,
`gst_state`, `gst_state_number`).

---

## Installation (Frappe v16)

### 1. Create a bench

```bash
bench init frappe-bench --frappe-branch version-16
cd frappe-bench
```

### 2. Get the apps

Order matters — `yob_core` is the lowest dependency:

```bash
bench get-app erpnext --branch version-16
bench get-app payments --branch version-16
bench get-app india_compliance --branch version-16
bench get-app https://github.com/nile1483/yob_core --branch main
bench get-app https://github.com/nile1483/yob_auth --branch main
bench get-app https://github.com/nile1483/yob_storefront --branch main
```

### 3. Create a site

```bash
bench new-site site1.local
```

### 4. Install on the site

Install in dependency order:

```bash
bench --site site1.local install-app erpnext
bench --site site1.local install-app payments
bench --site site1.local install-app india_compliance
bench --site site1.local install-app yob_core
bench --site site1.local install-app yob_auth
bench --site site1.local install-app yob_storefront
```

### 5. Migrate and restart

```bash
bench --site site1.local migrate
```

Then **restart the bench stack**. Apps are installed editable, so a running web
or worker process will not import a newly installed app and will return
`ModuleNotFoundError` until it restarts:

```bash
bench start
```

---

## Update the app

```bash
bench update --app yob_storefront
```

---

## Development setup (existing bench)

If you already have a Frappe v16 bench with `yob_core` and `yob_auth`:

```bash
bench get-app https://github.com/nile1483/yob_storefront --branch main
bench --site <your-site> install-app yob_storefront
bench --site <your-site> migrate
```

Restart the stack afterwards.

> **Never run `bench build` or `bench get-app` while the bench stack is
> stopped.** The asset manifest is written through Redis; building without it
> rewrites the hashed CSS/JS filenames on disk but leaves the cached manifest
> stale, so every stylesheet 404s and the Desk renders unstyled. Recover with
> `bench build && bench --site <site> clear-cache`.

---

## Useful commands

```bash
bench --site <site> list-apps      # installed apps and versions
bench --site <site> migrate        # run patches and schema sync
bench --site <site> clear-cache    # after asset or metadata changes
```

---

## App information

- **App name:** `yob_storefront`
- **Repository:** https://github.com/nile1483/yob_storefront
- **Branch:** `main`
- **Module:** `YOB Storefront`
- **License:** MIT

---

## Documentation

- [`AGENTS.md`](AGENTS.md) — mandatory rules for anyone (human or agent)
  changing this app
- [`docs/context.md`](docs/context.md) — ownership, DocTypes, navigation,
  compatibility
- [`docs/security.md`](docs/security.md) — storefront threat model
- [`docs/contracts/`](docs/contracts/) — API compatibility and error catalog
- Platform standards live in
  [`../yob_core/docs/platform/`](../yob_core/docs/platform/)

---

## Support

For issues, bugs, or feature requests, create an issue in the GitHub repository.
