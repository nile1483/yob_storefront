# Copyright (c) 2026, YOB and Shayona
"""
Repoint every metadata record owned by the old ``yob`` Module Def at the new
``yob_storefront`` Module Def.

DATA SAFETY
-----------
This patch renames metadata ownership ONLY. It does not drop, rename, truncate
or recreate any business table, and it does not touch any business record.
DocType names, field names and all existing rows are preserved exactly:

    Cart, Cart Item, Category, Payment Method,
    Payment Method Assignment, Razorpay Payment Log, YOB Store Settings

The patch is idempotent and safe to re-run after a partial failure.
"""

import frappe

OLD_MODULE = "yob"
NEW_MODULE = "yob_storefront"

# (doctype, fieldname holding the module reference)
MODULE_OWNED_RECORDS = [
    ("DocType", "module"),
    ("Custom Field", "module"),
    ("Property Setter", "module"),
    ("Client Script", "module"),
    ("Server Script", "module"),
    ("Workspace", "module"),
    ("Dashboard", "module"),
    ("Dashboard Chart", "module"),
    ("Report", "module"),
    ("Print Format", "module"),
    ("Notification", "module"),
    ("Web Form", "module"),
]


def execute():
    if not frappe.db.exists("Module Def", OLD_MODULE):
        # Already migrated (or a fresh install) -- nothing to do.
        return

    _ensure_new_module_def()
    _repoint_module_owned_records()
    _repoint_workspace_sidebar()
    _drop_old_module_def()

    frappe.clear_cache()


def _ensure_new_module_def():
    if frappe.db.exists("Module Def", NEW_MODULE):
        return

    doc = frappe.new_doc("Module Def")
    doc.module_name = NEW_MODULE
    doc.app_name = "yob_storefront"
    doc.custom = 0
    doc.insert(ignore_permissions=True)


def _repoint_module_owned_records():
    for doctype, fieldname in MODULE_OWNED_RECORDS:
        if not frappe.db.table_exists(doctype):
            continue

        # Guard against framework versions where the column is absent.
        if not frappe.db.has_column(doctype, fieldname):
            continue

        names = frappe.get_all(
            doctype,
            filters={fieldname: OLD_MODULE},
            pluck="name",
        )

        for name in names:
            frappe.db.set_value(
                doctype, name, fieldname, NEW_MODULE, update_modified=False
            )

        if names:
            print(f"  yob_storefront: repointed {len(names)} x {doctype}")


def _repoint_workspace_sidebar():
    """The Workspace Sidebar record is re-synced from the renamed JSON file.

    ``bench migrate`` creates the new ``yob_storefront`` record from
    workspace_sidebar/yob_storefront.json, which would leave the old ``yob``
    record orphaned in the sidebar. Remove it.
    """

    if not frappe.db.table_exists("Workspace Sidebar"):
        return

    if frappe.db.exists("Workspace Sidebar", OLD_MODULE):
        frappe.delete_doc(
            "Workspace Sidebar", OLD_MODULE, force=True, ignore_permissions=True
        )
        print("  yob_storefront: removed orphaned 'yob' Workspace Sidebar")


def _drop_old_module_def():
    still_referenced = []

    for doctype, fieldname in MODULE_OWNED_RECORDS:
        if not frappe.db.table_exists(doctype):
            continue
        if not frappe.db.has_column(doctype, fieldname):
            continue
        if frappe.db.exists(doctype, {fieldname: OLD_MODULE}):
            still_referenced.append(doctype)

    if still_referenced:
        # Do not delete while anything still points at it -- leave the old
        # Module Def in place so the situation is visible and re-runnable.
        print(
            "  yob_storefront: keeping old 'yob' Module Def, still referenced by: "
            + ", ".join(still_referenced)
        )
        return

    frappe.delete_doc("Module Def", OLD_MODULE, force=True, ignore_permissions=True)
    print("  yob_storefront: removed old 'yob' Module Def")
