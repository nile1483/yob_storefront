# Copyright (c) 2026, YOB and Shayona
"""Desk link queries for storefront filters.

Desk-only: a Frappe link query runs for a logged-in Desk user against normal
DocType permissions. It is a convenience, never a security boundary -- every rule
these queries express is enforced again in `Item.validate`.
"""

import frappe


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def filters_in_set(doctype, txt, searchfield, start, page_len, filters):
    """Filters belonging to one Filter Set, for the Item grid's Filter column."""

    # Explicit permission check. A link query runs for whoever is logged into
    # Desk, and `frappe.get_list` would apply permissions on its own -- but this
    # helper drops to raw SQL for the join, so the check is made here rather than
    # inherited from a query builder that is not being used.
    frappe.has_permission("YOB Storefront Filter", "read", throw=True)

    filter_set = (filters or {}).get("filter_set")

    if not filter_set:
        return []

    return frappe.db.sql(
        """
        SELECT f.name, f.label
        FROM `tabYOB Storefront Filter Set Filter` sf
        JOIN `tabYOB Storefront Filter` f ON f.name = sf.filter
        WHERE sf.parent = %(filter_set)s
          AND sf.parenttype = 'YOB Storefront Filter Set'
          AND f.enabled = 1
          AND (f.name LIKE %(txt)s OR f.label LIKE %(txt)s)
        ORDER BY sf.sequence ASC, sf.idx ASC
        LIMIT %(start)s, %(page_len)s
        """,
        {
            "filter_set": filter_set,
            "txt": f"%{txt or ''}%",
            "start": frappe.utils.cint(start),
            "page_len": frappe.utils.cint(page_len) or 20,
        },
    )
