# YOB Storefront DocType Inventory

Status: Reviewed-archive baseline; verify current JSON/source before change.

| DocType | Module | Category | Frappe type | Navigation note |
| --- | --- | --- | --- | --- |
| `YOB Store Settings` | `YOB Storefront` | Configuration | Single | Link from permitted primary Workspace |
| `Category` | `YOB Storefront` | Master | Normal/Tree unknown | Confirm `is_tree` and naming; link if directly managed |
| `Cart` | `YOB Storefront` | Operational document | Normal | Direct admin link only if approved |
| `Cart Item` | `YOB Storefront` | Child | Child/Table | Never show standalone; access through Cart |
| `Payment Method` | `YOB Storefront` | Master/configuration | Normal | Restrict by approved roles |
| `Payment Method Assignment` | `YOB Storefront` | Configuration mapping | Normal | Restrict by approved roles |
| `Razorpay Payment Log` | `YOB Storefront` | Integration transaction/audit | Normal | Restricted operational view; no secrets |

ERPNext `Sales Order` and the installed stack's `Payment Request` are dependency-
owned transactions. Storefront extends/orchestrates them through supported APIs
and adapters; it does not copy, rename, or claim their ownership.
