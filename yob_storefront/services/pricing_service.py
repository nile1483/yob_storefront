#path apps/yob_storefront/yob_storefront/services/pricing.py
"""
PRICING SERVICE – CENTRALIZED PRICING ENGINE
ERPNext v16 Compatible
B2B Secure – Uses Full Sales Order Engine Only
"""

import json
import frappe
from frappe.utils import today, getdate
from erpnext.accounts.party import get_default_price_list
from pprint import pprint 

from erpnext.accounts.doctype.pricing_rule.utils import apply_pricing_rule_on_transaction

# =========================================================
# 1️⃣ ITEM PRICING (Single Item via Sales Order Engine)
# =========================================================

def get_item_pricing(
    customer,
    item_code,
    qty,
    company,
    currency,
    selling_price_list=None,
    coupon_code=None
):
    """
    Secure item pricing using full ERPNext Sales Order engine.
    """

    if not customer:
        frappe.throw("Unauthorized", frappe.PermissionError)

    qty = float(qty)
    validate_item_saleable(item_code)

    # ---------------- CUSTOMER ----------------
    customer_doc = (
        frappe.get_doc("Customer", customer)
        if isinstance(customer, str)
        else customer
    )

    # ---------------- PRICE LIST ----------------
    selling_price_list = get_price_list_for_customer(
        customer_doc,
        selling_price_list
    )

    if not selling_price_list:
        frappe.throw("No selling price list configured")

    # ---------------- TEMP SALES ORDER ----------------
    so = frappe.new_doc("Sales Order")

    so.customer = customer_doc.name
    so.company = company
    so.currency = currency
    so.selling_price_list = selling_price_list
    so.transaction_date = today()

    if coupon_code:
        so.coupon_code = coupon_code

    so.append("items", {
        "item_code": item_code,
        "qty": qty
    })

    # ------------------------------------------------------------------
    # ELEVATION BOUNDARY -- read this before changing it.
    #
    # `so.customer` above came from `customer_doc`, which the caller resolved
    # through get_storefront_customer(auth_context). Authorization has ALREADY
    # happened: the caller proved an enabled STOREFRONT grant for exactly this
    # Customer. A request-supplied customer never reaches this line.
    #
    # ERPNext then re-checks Frappe DocType permissions while filling the
    # order, which an external Website User cannot satisfy:
    #
    #   selling_controller.set_missing_lead_customer_details
    #     -> party._get_party_details            -> Customer read
    #     -> party.set_address_details           -> Address read
    #
    # ERPNext supports skipping exactly those: selling_controller.py passes
    # `ignore_permissions=self.flags.ignore_permissions` into _get_party_details,
    # which forwards it as `check_permissions=not ignore_permissions` to the
    # address lookups. So this flag is ERPNext's own documented parameter, not a
    # bypass we invented.
    #
    # Scope is one throwaway in-memory Sales Order that is never inserted. No
    # global state is touched -- deliberately NOT the global
    # frappe.flags.ignore_permissions (which does not work here anyway:
    # get_item_details calls item.check_permission() on an internally cached
    # Item doc, and Document.has_permission consults that doc's own flags), and
    # the session user is never switched.
    #
    # (Phrased without the literal session-switching call name on purpose: the
    # contract scanner in tests/test_rename.py greps source for forbidden auth
    # primitives, and it should stay a dumb, un-foolable text scan.)
    #
    # The remaining Item read is granted by the `YOB Storefront Buyer` role, not
    # by this flag. Customer read stays denied -- that is the tested boundary.
    # ------------------------------------------------------------------
    so.flags.ignore_permissions = True

    so.set_missing_values()
    so.calculate_taxes_and_totals()

    row = so.items[0]

    # ---------------- TAX LABELS ----------------
    tax_labels = []
    for tax in so.taxes:
        if tax.tax_amount and tax.rate:
            label = tax.description or tax.account_head
            tax_labels.append(f"{label} {tax.rate}%")

    tax_label = ", ".join(tax_labels) if tax_labels else None

    # ---------------- PRICING RULE INFO ----------------
    pricing_rules = row.pricing_rules or []

    if isinstance(pricing_rules, str):
        try:
            pricing_rules = json.loads(pricing_rules)
        except Exception:
            pricing_rules = [pricing_rules]

    pricing_rule_label = None
    pricing_rule_apply_on = None

    if pricing_rules:
        rule = frappe.get_cached_doc("Pricing Rule", pricing_rules[0])
        pricing_rule_label = rule.title or rule.name
        pricing_rule_apply_on = rule.apply_on

    # ---------------- SAFE ITEM DATA ----------------
    item_doc = frappe.get_cached_doc("Item", item_code)

    safe_item = {
        "name": item_doc.name,
        "item_name": item_doc.item_name,
        "item_group": item_doc.item_group,
        "image": item_doc.image,
        "stock_uom": item_doc.stock_uom
    }

    # ---------------- FINAL RESPONSE ----------------
    return {
        "item": safe_item,
        "selling_price_list": selling_price_list,
        "qty": qty,

        "base_price": row.price_list_rate,
        "rate": row.rate,

        "discount_percentage": row.discount_percentage,
        "discount_amount": row.discount_amount,
        "total_discount": row.discount_amount * qty if row.discount_amount else 0,

        "net_amount": row.net_amount,
        "tax_amount": so.total_taxes_and_charges or 0,
        "tax_label": tax_label,
        "total_amount": so.grand_total,

        "pricing_rules": pricing_rules,
        "pricing_rule_label": pricing_rule_label,
        "pricing_rule_apply_on": pricing_rule_apply_on,

        "uom": row.uom
    }


# =========================================================
# 2️⃣ FULL CART CALCULATION USING SALES ORDER
# =========================================================

def calculate_cart_using_sales_order(cart, customer_doc):
 

    if not customer_doc:
        frappe.throw("Unauthorized", frappe.PermissionError)

    so = frappe.new_doc("Sales Order")

    so.customer = customer_doc.name
    so.company  = cart.company
    so.currency = cart.currency
    so.selling_price_list = cart.selling_price_list
    so.transaction_date = today() 
    
    if cart.coupon_code:
            coupon_name = frappe.db.get_value(
                "Coupon Code",
                {"coupon_code": cart.coupon_code},
                "name"
            )
        
            if coupon_name: 
                so.coupon_code = coupon_name

    # so.tax_category = cart.tax_category
    so.contact_person = cart.contact_person
    so.customer_address = cart.billing_address
    so.shipping_address_name = cart.shipping_address

    for row in cart.items:
       
        so.append("items", {
            "item_code": row.item_code,
            "qty": row.quantity,
            "uom": row.uom or row.stock_uom,
            "stock_uom": row.stock_uom or row.uom,
        })

    # Same targeted elevation as get_item_pricing, and for the same reason:
    # `so.customer` came from `cart.customer`, and the cart was loaded via the
    # authenticated Customer resolved from auth_context. Authorization already
    # happened; ERPNext then re-checks Customer/Address DocType permissions
    # while filling the order, which an external Website User cannot satisfy.
    #
    # Scope is this one throwaway in-memory Sales Order, never inserted. NOT a
    # global flag, and nothing else in cart/pricing/order services is elevated.
    so.flags.ignore_permissions = True

    so.set_missing_values() 
    
    so.calculate_taxes_and_totals()
    
    apply_pricing_rule_on_transaction(so)
    
    so.calculate_taxes_and_totals()

    return so


# =========================================================
# 3️⃣ SYNC SALES ORDER BACK TO CART
# =========================================================

def sync_sales_order_to_cart(cart, so):

    # -----------------------------
    # Cart Totals
    # -----------------------------
    cart.total_quantity = so.total_qty
    cart.net_total = so.net_total
    cart.tax_total = so.total_taxes_and_charges
    cart.grand_total = so.grand_total
    
    cart.coupon_discount = so.discount_amount or 0
    
    cart.total_discount = 0

    # -----------------------------
    # Map cart items by item_code
    # -----------------------------
    cart_items_map = {row.item_code: row for row in cart.items}

    for so_row in so.items:

        cart_row = cart_items_map.get(so_row.item_code)

        if not cart_row:
            continue

        # -----------------------------
        # Pricing
        # -----------------------------
        cart_row.base_price = so_row.price_list_rate
        cart_row.rate = so_row.rate
        cart_row.discount_percentage = so_row.discount_percentage
        cart_row.discount_amount = so_row.discount_amount
        cart_row.amount = so_row.net_amount

        # -----------------------------
        # Discount
        # -----------------------------
        line_discount = (so_row.discount_amount or 0) * (so_row.qty or 0)
        
        cart_row.line_discount = line_discount
        cart.total_discount += line_discount
        
        # -----------------------------
        # Tax Handling
        # -----------------------------
        cart_row.tax_amount   = get_item_tax_amount(so, so_row)
        cart_row.total_amount = so_row.net_amount + cart_row.tax_amount
  
        # -----------------------------
        # Pricing Rules
        # -----------------------------
        pricing_rules = so_row.pricing_rules

        if isinstance(pricing_rules, str):
            try:
                pricing_rules = json.loads(pricing_rules)
            except Exception:
                pricing_rules = []

        if not isinstance(pricing_rules, list):
            pricing_rules = []

        cart_row.pricing_rules = json.dumps(pricing_rules) if pricing_rules else None        
        
        # -----------------------------
        # Pricing Rule Details
        # -----------------------------
        if pricing_rules:
            rule_name = pricing_rules[0]

            try:
                rule = frappe.get_cached_doc("Pricing Rule", rule_name)

                cart_row.pricing_rule_label = rule.title or rule.name
                cart_row.pricing_rule_apply_on = rule.apply_on

            except Exception:
                cart_row.pricing_rule_label = None
                cart_row.pricing_rule_apply_on = None
        else:
            cart_row.pricing_rule_label = None
            cart_row.pricing_rule_apply_on = None


# =========================================================
# 4️⃣ PRICE LIST RESOLUTION
# =========================================================

def get_price_list_for_customer(customer_doc, fallback=None):

    price_list = get_default_price_list(customer_doc)

    if not price_list:
        price_list = get_default_selling_price_list()

    return price_list or fallback


# =========================================================
# 5️⃣ PRICING RULE LIST (Display Only)
# =========================================================

def get_applicable_pricing_rules(customer, item_code, item_group, brand=None):
    
    today_date = getdate(today())

    customer_group, territory = frappe.db.get_value(
        "Customer",
        customer,
        ["customer_group", "territory"],
    )
 
    rules = frappe.get_all(
        "Pricing Rule",
        filters={
            "selling": 1,
            "disable": 0,
            "coupon_code_based": 0,
            "valid_from": ["<=", today()],
        },
        fields=[
            "name",
            "title",
            "apply_on",
            "applicable_for",
            "price_or_product_discount",
            "discount_percentage",
            "customer",
            "customer_group",
            "territory",
            "rate",
            "min_qty",
            "max_qty",
            "min_amt",
            "max_amt",
            "free_item",
            "free_qty",
            "is_recursive",
            "round_free_qty",
            "dont_enforce_free_item_qty",
            "valid_from",
            "valid_upto",
        ],
        order_by="min_qty asc",
    )
    
    
    offers = []
    excluded = []

    for rule in rules:

        reason = validate_pricing_rule(
            rule=rule,
            today_date=today_date,
            customer=customer,
            customer_group=customer_group,
            territory=territory,
            item_code=item_code,
            item_group=item_group,
            brand=brand,
        )

        if reason:
            excluded.append(
                {
                    "rule": rule.name,
                    "title": rule.title,
                    "reason": reason,
                }
            )
            continue

        label = get_pricing_rule_label(rule)

        if label:
            offers.append(label)

    return {
                "offers": sorted(set(offers)),
                "excluded": excluded,
           }



def validate_pricing_rule(
    rule,
    today_date,
    customer,
    customer_group,
    territory,
    item_code,
    item_group,
    brand=None,
):
    # -------------------------
    # Date Validation
    # -------------------------

    if rule.valid_from and getdate(rule.valid_from) > today_date:
        return "Rule not started yet"

    if rule.valid_upto and getdate(rule.valid_upto) < today_date:
        return "Rule expired"

    # -------------------------
    # Apply On
    # -------------------------

    if rule.apply_on == "Item Code":
        if not frappe.db.exists(
            "Pricing Rule Item Code",
            {
                "parent": rule.name,
                "item_code": item_code,
            },
        ):
            return "Item Code not matched"

    elif rule.apply_on == "Item Group":
        if not frappe.db.exists(
            "Pricing Rule Item Group",
            {
                "parent": rule.name,
                "item_group": item_group,
            },
        ):
            return "Item Group not matched"

    elif rule.apply_on == "Brand":
        if not brand:
            return "Brand not provided"

        if not frappe.db.exists(
            "Pricing Rule Brand",
            {
                "parent": rule.name,
                "brand": brand,
            },
        ):
            return "Brand not matched"

    # -------------------------
    # Applicable For
    # -------------------------

    if rule.applicable_for == "Customer":
        if rule.customer != customer:
            return "Customer not matched"

    elif rule.applicable_for == "Customer Group":
        if rule.customer_group != customer_group:
            return "Customer Group not matched"

    elif rule.applicable_for == "Territory":
        if rule.territory not in ("All Territories", territory):
            return "Territory not matched"

    return None


def get_pricing_rule_label(rule):
    """Return a user-friendly label for Pricing Rule."""

    # ---------------------------------------------------------
    # Product Discount
    # ---------------------------------------------------------
    if rule.price_or_product_discount == "Product":

        free_item = (
            frappe.db.get_value("Item", rule.free_item, "item_name")
            or rule.free_item
        )

        buy_qty = int(rule.min_qty or 1)
        free_qty = int(rule.free_qty or 1)

        label = (
            f"Buy {buy_qty} and get {free_qty} {free_item} FREE"
        )

        extras = []

        if rule.is_recursive:
            extras.append("Offer repeats")

        if rule.round_free_qty:
            extras.append("Rounded free quantity")

        if rule.dont_enforce_free_item_qty:
            extras.append("Free quantity not enforced")

        if extras:
            label += f" ({', '.join(extras)})"

        return label

    # ---------------------------------------------------------
    # Transaction Discount
    # ---------------------------------------------------------
    if rule.apply_on == "Transaction":

        if rule.discount_percentage:

            if rule.min_amt and rule.max_amt:
                return (
                    f"{rule.discount_percentage:g}% OFF "
                    f"on orders between ₹{rule.min_amt:g} and ₹{rule.max_amt:g}"
                )

            if rule.min_amt:
                return (
                    f"{rule.discount_percentage:g}% OFF "
                    f"on orders of ₹{rule.min_amt:g} or more"
                )

            if rule.max_amt:
                return (
                    f"{rule.discount_percentage:g}% OFF "
                    f"on orders up to ₹{rule.max_amt:g}"
                )

            return f"{rule.discount_percentage:g}% OFF"

        if rule.rate:
            return f"Flat price ₹{rule.rate:g}"

    # ---------------------------------------------------------
    # Item / Item Group / Brand Discount
    # ---------------------------------------------------------
    if rule.discount_percentage:

        if rule.min_qty and rule.max_qty:
            return (
                f"Buy {int(rule.min_qty)} to {int(rule.max_qty)} items "
                f"and get {rule.discount_percentage:g}% OFF"
            )

        if rule.min_qty:
            return (
                f"Buy {int(rule.min_qty)} or more items "
                f"and get {rule.discount_percentage:g}% OFF"
            )

        if rule.max_qty:
            return (
                f"Buy up to {int(rule.max_qty)} items "
                f"and get {rule.discount_percentage:g}% OFF"
            )

        return f"Get {rule.discount_percentage:g}% OFF"

    if rule.rate:

        if rule.min_qty:
            return (
                f"Buy {int(rule.min_qty)} or more items "
                f"@ ₹{rule.rate:g}"
            )

        return f"Price ₹{rule.rate:g}"

    return rule.title

# =========================================================
# 6️⃣ ITEM VALIDATION
# =========================================================

def validate_item_saleable(item_code):

    item = frappe.get_doc("Item", item_code)
    today_date = getdate(today())

    if item.disabled:
        frappe.throw(f"Item {item_code} is disabled")

    if not item.is_sales_item:
        frappe.throw(f"Item {item_code} is not marked as sales item")

    if item.end_of_life and getdate(item.end_of_life) < today_date:
        frappe.throw(f"Item {item_code} is past end of life")


# =========================================================
# 7️⃣ DEFAULT SELLING PRICE LIST
# =========================================================

def get_default_selling_price_list():
    return frappe.get_single_value(
        "Selling Settings",
        "selling_price_list"
    )
    
def get_item_tax_amount(so, so_row):
    if not so.total_taxes_and_charges:
        return 0

    total_net = sum(i.net_amount for i in so.items)

    if not total_net:
        return 0

    ratio = so_row.net_amount / total_net

    return so.total_taxes_and_charges * ratio