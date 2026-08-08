# yob_storefront/services/pricing_display.py

import frappe
from frappe.utils import today, getdate


def get_applicable_pricing_rules(
    item_code,
    item_group=None,
    customer=None
):
    """
    Display-only helper.
    Returns list of applicable pricing rule labels.
    Does NOT calculate pricing.
    """

    today_date = getdate(today())

    filters = {
        "selling": 1,
        "disable": 0
    }

    rules = frappe.get_all(
        "Pricing Rule",
        filters=filters,
        fields=[
            "name",
            "title",
            "apply_on",
            "min_qty",
            "rate",
            "discount_percentage",
            "free_qty",
            "customer",
            "customer_group",
            "valid_from",
            "valid_upto"
        ]
    )

    labels = []

    customer_doc = None
    if customer:
        if isinstance(customer, str):
            customer_doc = frappe.get_doc("Customer", customer)
        else:
            customer_doc = customer

    for rule in rules:

        # ---------------- DATE CHECK ----------------
        if rule.valid_from and getdate(rule.valid_from) > today_date:
            continue

        if rule.valid_upto and getdate(rule.valid_upto) < today_date:
            continue

        # ---------------- CUSTOMER CHECK ----------------
        if rule.customer and customer_doc:
            if rule.customer != customer_doc.name:
                continue

        if rule.customer_group and customer_doc:
            if rule.customer_group != customer_doc.customer_group:
                continue

        # ---------------- APPLY ON CHECK ----------------
        applicable = False

        if rule.apply_on == "Item":
            if frappe.db.exists(
                "Pricing Rule Item Code",
                {"parent": rule.name, "item_code": item_code}
            ):
                applicable = True

        elif rule.apply_on == "Item Group" and item_group:
            if frappe.db.exists(
                "Pricing Rule Item Group",
                {"parent": rule.name, "item_group": item_group}
            ):
                applicable = True

        elif rule.apply_on == "Transaction":
            applicable = True

        if not applicable:
            continue

        label = build_pricing_rule_label(rule)
        if label:
            labels.append(label)

    return sorted(list(set(labels)))


# =========================================================
# LABEL FORMATTER
# =========================================================

def build_pricing_rule_label(rule):

    min_qty = int(rule.min_qty or 1)

    # Buy X Get Y Free
    if rule.free_qty:
        return f"Buy {min_qty} Get {int(rule.free_qty)} Free"

    # Fixed Rate
    if rule.rate:
        return f"Buy {min_qty} @ {rule.rate}"

    # Percentage Discount
    if rule.discount_percentage:
        return f"Buy {min_qty} Get {rule.discount_percentage}% Off"

    return None
