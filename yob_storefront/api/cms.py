import frappe
from yob_core.api.boundary import yob_api
from frappe.utils import get_url
from yob_auth.security.decorators import require_application
from yob_storefront.api.response import server_error, success_response
from yob_storefront.utils.context import STOREFRONT_APP
from yob_storefront.utils.store import get_store_settings
from yob_storefront.utils.cache import STORE_CONFIG_CACHE


# ---------------- CONFIG ----------------

@frappe.whitelist(methods=["GET"])
@yob_api
@require_application(STOREFRONT_APP)
def get_config(auth_context=None):
    cache_key = STORE_CONFIG_CACHE
    frappe.cache().delete_value(cache_key)
    cached = frappe.cache().get_value(cache_key)

    if cached:
        return success_response(cached, notice="Store config loaded (cached)")

    try:
        settings = get_store_settings()

        # logo_url = get_url(settings.get("store_logo")) if settings.get("store_logo") else None
        logo_url = settings.get("store_logo") if settings.get("store_logo") else None

        data = {
            "company": settings.get("company"),
            "store_name": settings.get("store_name"),
            "store_domain": settings.get("store_domain"),
            "store_logo": logo_url,
            "default_warehouse": settings.get("default_warehouse"),
            "default_currency": settings.get("default_currency"),
            "default_price_list": settings.get("default_price_list"),
            "default_terms_page": settings.get("default_terms_page"),
            "default_privacy_page": settings.get("default_privacy_page"),
            "allow_guest_purchase": settings.get("allow_guest_purchase"),
            "allowed_payment_modes": [
                {"mode_of_payment": row.get("mode_of_payment")}
                for row in settings.get("allowed_payment_modes", [])
            ]
        }

        # cache for 1 hour
        frappe.cache().set_value(cache_key, data, expires_in_sec=3600)

        return success_response(data, notice="Store config loaded")

    except Exception:
        return server_error("Get Store Config API", "Failed to load store config")
