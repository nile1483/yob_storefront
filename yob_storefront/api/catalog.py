# Copyright (c) 2026, YOB and Shayona
# path: apps/yob_storefront/yob_storefront/api/catalog.py
"""
CATALOG API
Private B2B Only
Requires Login + Customer
ERPNext v16 Compatible
"""

import frappe
from frappe.utils import get_url
from yob_storefront.api.response import (
    APPLICATION_ACCESS_DENIED,
    CATEGORY_NOT_FOUND,
    HTTP_FORBIDDEN,
    HTTP_NOT_FOUND,
    ITEM_NOT_FOUND,
    error_response,
    server_error,
    success_response,
)
from yob_auth.security.decorators import require_application
from yob_storefront.utils.context import STOREFRONT_APP, get_storefront_customer
from yob_storefront.services.pricing_service import (
    get_item_pricing,
    get_applicable_pricing_rules
)
 

# =========================================================
# 1️⃣ GET CATEGORIES (LOGIN REQUIRED)
# =========================================================

@frappe.whitelist()
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def get_categories(parent_slug=None, auth_context=None):

    try:
        # 🔐 Enforce Login + Customer
        get_storefront_customer(auth_context)

        filters = {"is_active": 1}

        if parent_slug:
            parent = frappe.get_value(
                "Category",
                {"slug": parent_slug},
                "name"
            )

            if not parent:
                return success_response([], notice="No categories found", meta={"count": 0})

            filters["parent_category"] = parent
        else:
            filters["parent_category"] = None

        categories = frappe.get_all(
            "Category",
            filters=filters,
            fields=[
                "name",
                "category_name",
                "slug",
                "thumbnail",
                "banner",
                "display_order",
                "meta_title",
                "meta_description",
                "parent_category"
            ],
            order_by="display_order asc"
        )

        for c in categories:
            if c.get("thumbnail"):
                # c["thumbnail"] = get_url(c["thumbnail"])
                c["thumbnail"] = c["thumbnail"]
            if c.get("banner"):
                # c["banner"] = get_url(c["banner"])
                c["banner"] = c["banner"]

        return success_response(
            categories,
            notice="Categories loaded",
            meta={"count": len(categories)},
        )

    except frappe.PermissionError as exc:
        return error_response(
            APPLICATION_ACCESS_DENIED,
            str(exc) or "You are not authorized to access the storefront.",
            status_code=HTTP_FORBIDDEN,
        )

    except Exception:
        return server_error("Get Categories Error", "Failed to load categories")


# =========================================================
# 2️⃣ GET CATEGORY WITH ITEMS (LOGIN REQUIRED)
# =========================================================

@frappe.whitelist()
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def get_category(slug, qty=1, auth_context=None):

    try:
        # 🔐 Enforce Login + Customer
        customer = get_storefront_customer(auth_context)
        
        settings = frappe.get_cached_doc("YOB Store Settings")
        
        category = frappe.get_value(
            "Category",
            {"slug": slug, "is_active": 1},
            [
                "name",
                "category_name",
                "slug",
                "thumbnail",
                "banner",
                "meta_title",
                "meta_description",
                "description",
                "is_group",
                "parent_category"
            ],
            as_dict=True
        )

        if not category:
            return error_response(
                CATEGORY_NOT_FOUND,
                "Category not found.",
                field="slug",
                status_code=HTTP_NOT_FOUND,
            )

        if category.get("thumbnail"):
            # category["thumbnail"] = get_url(category["thumbnail"])
            category["thumbnail"] = category["thumbnail"]

        if category.get("banner"):
            # category["banner"] = get_url(category["banner"])
            category["banner"] = category["banner"]

        subcategories = []
        items = []

        # ---------------- GROUP CATEGORY ----------------
        if category["is_group"]:

            subcategories = frappe.get_all(
                "Category",
                filters={
                    "parent_category": category["name"],
                    "is_active": 1
                },
                fields=[
                    "name",
                    "category_name",
                    "slug",
                    "thumbnail",
                    "display_order",
                    "is_group"
                ],
                order_by="display_order asc"
            )

            for child in subcategories:
                if child.get("thumbnail"):
                    # child["thumbnail"] = get_url(child["thumbnail"])
                    child["thumbnail"] = child["thumbnail"]

        # ---------------- LEAF CATEGORY ----------------
        else:

            raw_items = frappe.get_all(
                "Item",
                filters={
                    "custom_category": category["name"],
                    "disabled": 0,
                    "is_sales_item": 1
                },
                fields=[
                    "name",
                    "item_name",
                    "custom_slug",
                    "image",
                    "stock_uom",
                ]
            )

            for item in raw_items:

                pricing = get_item_pricing(
                    customer=customer,
                    item_code=item["name"],
                    qty=qty,
                    company=settings.company,
                    currency=settings.default_currency
                )
                 
                items.append({
                    "name": item["name"],
                    "item_name": item["item_name"],
                    "slug": item["custom_slug"],
                    "stock_uom": item["stock_uom"],
                    # "image": get_url(item["image"]) if item.get("image") else None,
                    "image": item["image"] if item.get("image") else None,
                    

                    "base_price": pricing["base_price"],
                    "rate": pricing["rate"],
                    "discount_percentage": pricing["discount_percentage"],
                    "discount_amount": pricing["discount_amount"],

                    "net_amount": pricing["net_amount"],
                    "tax_amount": pricing["tax_amount"],
                    "total_amount": pricing["total_amount"],

                    "pricing_rule_label": pricing["pricing_rule_label"]
                })
                 
        return success_response(
            {
                "category": category,
                "subcategories": subcategories,
                "items": items
            },
            notice="Category loaded",
            meta={
                "subcategory_count": len(subcategories),
                "item_count": len(items),
            },
        )

    except frappe.PermissionError as exc:
        return error_response(
            APPLICATION_ACCESS_DENIED,
            str(exc) or "You are not authorized to access the storefront.",
            status_code=HTTP_FORBIDDEN,
        )

    except Exception:
        return server_error("Get Category Error", "Failed to load category")


# =========================================================
# 3️⃣ GET SINGLE ITEM (LOGIN REQUIRED)
# =========================================================

@frappe.whitelist()
@require_application(STOREFRONT_APP, profile_doctype="Customer")
def get_item(slug, qty=1, auth_context=None):

    # try:
        # 🔐 Enforce Login + Customer
        customer = get_storefront_customer(auth_context)

        settings = frappe.get_single("YOB Store Settings")

        item = frappe.get_value(
            "Item",
            {"custom_slug": slug},
            ["name", "item_name", "item_group", "image"],
            as_dict=True
        )

        if not item:
            return error_response(
                ITEM_NOT_FOUND,
                "Item not found.",
                field="slug",
                status_code=HTTP_NOT_FOUND,
            )

        # ✅ Always use store default currency
        currency = settings.default_currency
 
           
        pricing = get_item_pricing(
            customer=customer,
            item_code=item["name"],
            qty=qty,
            company=settings.company,
            currency=currency
        )

        rules = get_applicable_pricing_rules(
            customer=customer,
            item_code=item["name"],
            item_group=item["item_group"]
        )

        
        offers = rules["offers"]

        return success_response({
            "name": item["name"],
            "item_name": item["item_name"],
            # "image": get_url(item["image"]) if item.get("image") else None,
            "image": item.get("image") if item.get("image") else None,
            "item_group": item["item_group"],
            "custom_slug": slug, 
            "qty": float(qty),

            "base_price": pricing["base_price"],
            "rate": pricing["rate"],
            "discount_percentage": pricing["discount_percentage"],
            "discount_amount": pricing["discount_amount"],

            "net_amount": pricing["net_amount"],
            "tax_amount": pricing["tax_amount"],
            "tax_label":  pricing["tax_label"],
            "total_amount": pricing["total_amount"],

            "uom": pricing["uom"],
            "pricing": pricing,

            "pricing_rule_label": pricing["pricing_rule_label"],
            "pricing_rule_apply_on": pricing["pricing_rule_apply_on"],

            "available_rules": offers
        }, notice="Item loaded")

    # except frappe.PermissionError:
    #     return error_response(APPLICATION_ACCESS_DENIED, "Unauthorized", status_code=HTTP_FORBIDDEN)

    # except Exception:
    #     return server_error("Get Item Error", "Failed to load item")