# copyright (c) 2026, YOB and Shayona
# path: apps/yob_storefront/yob_storefront/utils/cache.py
import frappe

# =====================================================
# CACHE KEY PREFIXES (CONSISTENT NAMESPACE)
# =====================================================

STORE_CONFIG_CACHE = "yob:store:config"
MENU_CACHE_PREFIX = "yob:menu:"
CUSTOMER_CACHE_PREFIX = "yob:customer:"
PRICING_CACHE_PREFIX = "yob:pricing:"
ITEM_CACHE_PREFIX = "yob:item:"
CATEGORY_CACHE_PREFIX = "yob:category:"
CMS_CACHE_PREFIX = "yob:cms:"


# =====================================================
# STORE CONFIG
# =====================================================

def clear_store_config_cache(doc=None, method=None):
    frappe.cache().delete_value(STORE_CONFIG_CACHE)


# =====================================================
# MENUS
# =====================================================

def clear_menu_cache(doc=None, method=None):
    frappe.cache().delete_keys(f"{MENU_CACHE_PREFIX}*")


# =====================================================
# CUSTOMER CONTEXT
# =====================================================

def clear_customer_cache(doc, method=None):
    """
    Clears customer cache when Customer or Contact changes.
    Also clears pricing cache because pricing depends on customer.
    """

    if doc.doctype == "Customer":
        customer_name = doc.name

        emails = frappe.db.sql("""
            SELECT ce.email_id
            FROM `tabContact Email` ce
            JOIN `tabDynamic Link` dl ON dl.parent = ce.parent
            WHERE dl.link_name = %s
              AND dl.link_doctype = 'Customer'
        """, customer_name, as_dict=True)

        for row in emails:
            frappe.cache().delete_value(
                f"{CUSTOMER_CACHE_PREFIX}{row.email_id}"
            )

    elif doc.doctype == "Contact":
        emails = frappe.db.get_all(
            "Contact Email",
            filters={"parent": doc.name},
            pluck="email_id"
        )

        for email in emails:
            frappe.cache().delete_value(
                f"{CUSTOMER_CACHE_PREFIX}{email}"
            )

    # Customer change affects pricing
    clear_pricing_cache()


# =====================================================
# PRICING
# =====================================================

def clear_pricing_cache(doc=None, method=None):
    frappe.cache().delete_keys(f"{PRICING_CACHE_PREFIX}*")


# =====================================================
# ITEM
# =====================================================

def clear_item_cache(doc, method=None):
    """
    Clears item cache and pricing cache,
    because pricing depends on item.
    """
    frappe.cache().delete_keys(f"{ITEM_CACHE_PREFIX}{doc.name}*")
    clear_pricing_cache()


# =====================================================
# CATEGORY
# =====================================================

def clear_category_cache(doc=None, method=None):
    frappe.cache().delete_keys(f"{CATEGORY_CACHE_PREFIX}*")


# =====================================================
# CMS
# =====================================================

def clear_cms_cache(doc=None, method=None):
    frappe.cache().delete_keys(f"{CMS_CACHE_PREFIX}*")


# =====================================================
# MASTER CLEAR (Dev / Deploy Only)
# =====================================================

def clear_all_yob_cache():
    """
    Clears all portal-related cache.
    Use only during development or deployments.
    """
    frappe.cache().delete_keys("yob:*")


# =====================================================
# PARTY DATA (ADDRESS / CONTACT LISTS)
# =====================================================
#
# These are per-CUSTOMER list caches, distinct from the per-EMAIL customer
# resolution cache above. `clear_customer_cache` deletes
# `yob:customer:<email>` and nothing else, so before Phase 32E a Contact or
# Address created in Desk stayed invisible to the storefront for the full
# 30-minute TTL: the storefront APIs cleared these keys themselves, but a
# backend change had no path to them at all and `Address` had no doc_event.

def address_cache_key(customer_name: str) -> str:
    return f"{CUSTOMER_CACHE_PREFIX}{customer_name}:addresses"


def contact_cache_key(customer_name: str) -> str:
    return f"{CUSTOMER_CACHE_PREFIX}{customer_name}:contacts"


def clear_customer_party_cache(customer_names) -> None:
    """Drop the cached Address and Contact lists for these Customers.

    Customer-scoped on purpose: a party change must never flush unrelated
    storefront caches, and must never reach a Customer it does not concern.

    Accepts a single name or any iterable, de-duplicates, and ignores blanks.
    `delete_value` on an absent key is a no-op, so this is safe to call for a
    Customer that was never cached and is idempotent on repeat.
    """

    if isinstance(customer_names, str):
        customer_names = [customer_names]

    for name in {n for n in (customer_names or []) if n}:
        frappe.cache().delete_value(address_cache_key(name))
        frappe.cache().delete_value(contact_cache_key(name))


def _linked_customers(doc) -> set:
    """Customer names in this document's Dynamic Links.

    ONLY `Customer` links. An Address may also link a Supplier, a Lead or a
    Sales Partner; those share the row but not the storefront cache, and
    invalidating on them would clear a Customer's list for an unrelated party's
    edit.
    """

    return {
        row.link_name
        for row in (doc.get("links") or [])
        if row.get("link_doctype") == "Customer" and row.get("link_name")
    }


def clear_party_cache_from_links(doc, method=None) -> None:
    """Invalidate every Customer this Address/Contact touches, before AND after.

    A relink -- Customer A -> Customer B -- has to clear BOTH: A because the
    record left its list, B because it joined one. The previous owner is read
    from Frappe's own `get_doc_before_save()` rather than guessed; it is None on
    insert and on delete, where the current links are the whole story.
    """

    affected = _linked_customers(doc)

    # `callable(getattr(...))`, not `hasattr`: `frappe._dict.__getattr__` answers
    # None for any missing key instead of raising, so hasattr() is True for a
    # method that does not exist and the call then fails with
    # "'NoneType' object is not callable".
    previous = getattr(doc, "get_doc_before_save", None)
    before = previous() if callable(previous) else None

    if before is not None:
        affected |= _linked_customers(before)

    clear_customer_party_cache(affected)
