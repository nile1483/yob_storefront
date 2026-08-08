import frappe


STORE_SETTINGS_CACHE_KEY = "yob_store_settings"


def get_store_settings():
    """
    Get store settings with caching
    """
    
    doc = frappe.get_single("YOB Store Settings")
    settings = doc.as_dict()
    return settings
        
    settings = frappe.cache().get_value(STORE_SETTINGS_CACHE_KEY)

    if not settings:
        doc = frappe.get_single("YOB Store Settings")
        settings = doc.as_dict()
        frappe.cache().set_value(STORE_SETTINGS_CACHE_KEY, settings)

    return settings
