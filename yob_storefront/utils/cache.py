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
