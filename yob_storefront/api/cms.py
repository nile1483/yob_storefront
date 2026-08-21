import frappe
from yob_core.api.boundary import yob_api
from frappe.utils import get_url
from yob_auth.security.decorators import require_application
from yob_storefront.api.response import (
    HTTP_NOT_FOUND,
    HTTP_UNPROCESSABLE,
    MENU_NOT_FOUND,
    PAGE_NOT_FOUND,
    VALIDATION_FAILED,
    error_response,
    server_error,
    success_response,
)
from yob_storefront.utils.context import STOREFRONT_APP, get_storefront_customer
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


# ---------------- NAVIGATION ----------------

@frappe.whitelist(methods=["GET"])
@yob_api
@require_application(STOREFRONT_APP)
def get_menu(menu_key=None, auth_context=None):
    """One published navigation menu, ready to render.

    Publishing is decided entirely here: an enabled Menu, enabled items, enabled
    parents, and destinations that still resolve. A merchant's disabled category
    or unpublished page silently drops the item rather than shipping a dead link,
    and a Group whose children all dropped goes with them.

    Returns a normalised destination per item -- a semantic type and a public
    slug -- never `link_category`/`link_page`/`link_item`, which are database
    identity and stay on the server.
    """

    if not menu_key:
        return error_response(
            VALIDATION_FAILED, "Menu key is required.", field="menu_key",
            status_code=HTTP_UNPROCESSABLE)

    try:
        from yob_storefront.services.navigation_service import get_menu_tree

        menu, items = get_menu_tree(menu_key)

        if not menu:
            # A disabled menu answers exactly like a missing one: the storefront
            # has nothing to render either way.
            return error_response(
                MENU_NOT_FOUND, "Menu not found.", field="menu_key",
                status_code=HTTP_NOT_FOUND)

        return success_response(
            {"key": menu.menu_key, "label": menu.menu_name, "items": items},
            notice="Menu loaded")

    except Exception:
        return server_error("Get Menu Error", "Failed to load the menu")


# ---------------- CONTENT PAGES ----------------

@frappe.whitelist(methods=["GET"])
@yob_api
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def get_page(slug=None, auth_context=None):
    """A published storefront page and its ordered, discriminated blocks.

    Requires the storefront Customer because a page may hold Product Grids, and a
    grid is priced through the buyer's own `SellingContext`. That is also why the
    hydrated response must never be cached across customers.
    """

    if not slug:
        return error_response(
            VALIDATION_FAILED, "Page slug is required.", field="slug",
            status_code=HTTP_UNPROCESSABLE)

    try:
        from yob_storefront.services.content_service import get_page as project_page

        customer = get_storefront_customer(auth_context)
        page = project_page(slug, customer)

        if not page:
            return error_response(
                PAGE_NOT_FOUND, "Page not found.", field="slug",
                status_code=HTTP_NOT_FOUND)

        return success_response(page, notice="Page loaded")

    except Exception:
        return server_error("Get Page Error", "Failed to load the page")
