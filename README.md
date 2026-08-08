# YOB Frappe App

Custom Frappe/ERPNext application for YOB eCommerce and backend integrations.

---

## Requirements

The app is built and tested with the following versions:

- **Frappe Framework:** v16.2.1 (version-16)
- **ERPNext:** v16.1.0 (version-16)
- **India Compliance:** v16.0.0-dev (version-16)
- **Payments:** v0.0.1 (develop)

---

## Installation (Frappe v16)

### 1. Create a bench
```bash
bench init frappe-bench --frappe-branch version-16
cd frappe-bench
```

### 2. Get required apps
```bash
bench get-app erpnext --branch version-16
bench get-app india_compliance --branch version-16
bench get-app payments --branch develop
bench get-app https://github.com/anand903/frappe_yob.git --branch version-16
```

### 3. Create a new site
```bash
bench new-site site1.local
```

### 4. Install apps on the site
```bash
bench --site site1.local install-app erpnext
bench --site site1.local install-app india_compliance
bench --site site1.local install-app payments
bench --site site1.local install-app yob
```

### 5. Run migrations and restart
```bash
bench --site site1.local migrate
bench restart
```

---

## Update the App
```bash
cd ~/frappe-bench
bench update --app yob
```

---

## Development Setup (existing bench)

If you already have a Frappe v16 bench:

```bash
cd ~/frappe-bench
bench get-app https://github.com/anand903/frappe_yob.git --branch version-16
bench --site your-site-name install-app yob
bench --site your-site-name migrate
bench restart
```

---

## Useful Commands

### Check installed apps
```bash
bench --site site1.local list-apps
```

### Run migrations
```bash
bench --site site1.local migrate
```

### Restart bench
```bash
bench restart
```

---

## App Information

- **App Name:** yob
- **Branch:** version-16
- **Framework:** Frappe v16
- **License:** MIT

---

## Support

For issues, bugs, or feature requests, please create an issue in the GitHub repository.
