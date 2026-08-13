# Copyright (c) 2026, YOB and Shayona
"""Idempotent demo data for a disposable Storefront site.

Purpose: give every documented endpoint a realistic, reproducible response so
API documentation shows real payloads instead of empty collections.

    bench --site test.localhost execute \
        yob_storefront.tests.fixtures.seed_demo_data.run

Re-running is safe: every step checks for existence first. Nothing here is a
migration or a patch -- it creates ordinary business records.

NEVER run this against a production or active data site. `run()` refuses any
site whose name is not explicitly allow-listed below.
"""

import frappe
from frappe.utils import add_days, nowdate

ALLOWED_SITES = {"test.localhost"}

STORE_USER = "storefront@yob.test"
STORE_PASSWORD = "Storefront@123"
CUSTOMER = "YOB Demo Buyer"
APP_CODE = "STOREFRONT"
PRICE_LIST = "Standard Selling"

CATEGORIES = [
    # (name, slug, is_group, parent)
    ("Industrial Supplies", "industrial-supplies", 1, None),
    ("Fasteners", "fasteners", 0, "Industrial Supplies"),
    ("Safety Gear", "safety-gear", 0, "Industrial Supplies"),
]

# india_compliance makes gst_hsn_code mandatory on Item, so each row carries a
# real 6/8-digit HSN (india_compliance rejects 4-digit): 731811 = screws/bolts,
# 61161000 = impregnated gloves, 650610 = safety headgear.
ITEMS = [
    # (code, name, category, slug, rate, hsn)
    ("YOB-BOLT-M10", "Hex Bolt M10 x 50mm", "Fasteners", "hex-bolt-m10-50", 12.50, "731811"),
    ("YOB-NUT-M10", "Hex Nut M10", "Fasteners", "hex-nut-m10", 3.75, "731811"),
    ("YOB-GLOVE-L", "Safety Gloves (Large)", "Safety Gear", "safety-gloves-large", 249.00, "61161000"),
    ("YOB-HELMET-W", "Safety Helmet (White)", "Safety Gear", "safety-helmet-white", 615.00, "650610"),
]


def _log(msg):
    print(f"  {msg}")


def _company():
    return frappe.db.get_value("Company", {}, "name")


def store_settings():
    doc = frappe.get_single("YOB Store Settings")
    if doc.company and doc.default_price_list:
        _log("store settings: already configured")
        return
    doc.store_name = "YOB Demo Store"
    doc.company = _company()
    doc.default_currency = "INR"
    doc.default_price_list = PRICE_LIST
    doc.store_domain = "storefront.test"
    doc.cart_expiry = 24
    doc.save(ignore_permissions=True)
    _log("store settings: configured")


def customer_and_access():
    if not frappe.db.exists("Customer", CUSTOMER):
        frappe.get_doc({
            "doctype": "Customer", "customer_name": CUSTOMER,
            "customer_type": "Company", "customer_group": "Commercial",
            "territory": "India",
        }).insert(ignore_permissions=True)
        _log(f"customer: {CUSTOMER}")

    if not frappe.db.exists("User", STORE_USER):
        user = frappe.get_doc({
            "doctype": "User", "email": STORE_USER, "first_name": "Storefront",
            "send_welcome_email": 0, "user_type": "Website User", "enabled": 1,
        })
        user.new_password = STORE_PASSWORD
        user.insert(ignore_permissions=True)
        _log(f"user: {STORE_USER}")

    if not frappe.db.exists("YOB User Application Access",
                            {"user": STORE_USER, "application": APP_CODE}):
        frappe.get_doc({
            "doctype": "YOB User Application Access", "user": STORE_USER,
            "application": APP_CODE, "enabled": 1, "profile_doctype": "Customer",
            "profile_name": CUSTOMER, "company": _company(),
        }).insert(ignore_permissions=True)
        _log("application access granted")


def categories():
    for name, slug, is_group, parent in CATEGORIES:
        if frappe.db.exists("Category", name):
            continue
        frappe.get_doc({
            "doctype": "Category", "category_name": name, "slug": slug,
            "is_group": is_group, "is_active": 1, "parent_category": parent,
            "display_order": 1,
            "description": f"{name} available to approved B2B buyers.",
        }).insert(ignore_permissions=True)
        _log(f"category: {name}")


def items_and_prices():
    for code, name, category, slug, rate, hsn in ITEMS:
        if not frappe.db.exists("Item", code):
            frappe.get_doc({
                "doctype": "Item", "item_code": code, "item_name": name,
                "item_group": "Products", "stock_uom": "Nos",
                # is_stock_item=1 marks these as PHYSICAL goods, which is what drives
                # cart.is_shippable in reprice_cart. Bolts and helmets ship.
                "is_sales_item": 1, "is_stock_item": 1, "disabled": 0,
                "custom_category": category, "custom_slug": slug,
                "gst_hsn_code": hsn,
                "description": f"{name} -- demo catalogue item.",
            }).insert(ignore_permissions=True)
            _log(f"item: {code}")

        if not frappe.db.exists("Item Price", {"item_code": code, "price_list": PRICE_LIST}):
            frappe.get_doc({
                "doctype": "Item Price", "item_code": code, "price_list": PRICE_LIST,
                "selling": 1, "price_list_rate": rate, "currency": "INR",
            }).insert(ignore_permissions=True)
            _log(f"item price: {code} = {rate}")


def pricing_rule():
    """Bulk discount so catalogue responses show a pricing_rule_label."""

    name = "YOB Demo Bulk Discount"
    if frappe.db.exists("Pricing Rule", {"title": name}):
        return name
    doc = frappe.get_doc({
        "doctype": "Pricing Rule", "title": name,
        # Pricing Rule.apply_on options are Item Code / Item Group / Brand /
        # Transaction. NOT "Item" -- that value saves but then crashes ERPNext
        # in update_pricing_rule_uom(), which maps apply_on -> child table and
        # iterates the result: {"Item Code": "items", ...}.get("Item") is None.
        "apply_on": "Item Code",
        "price_or_product_discount": "Price", "rate_or_discount": "Discount Percentage",
        "discount_percentage": 10, "min_qty": 10, "selling": 1,
        "company": _company(), "currency": "INR", "for_price_list": PRICE_LIST,
        "items": [{"item_code": row[0]} for row in ITEMS],
    }).insert(ignore_permissions=True)
    _log(f"pricing rule: {name} (10% at qty>=10)")
    return doc.name


def coupons(rule):
    """Valid / expired / exhausted, so every coupon error code is reachable."""

    specs = [
        ("YOB Demo Valid", "DEMO10", nowdate(), add_days(nowdate(), 90), 100, 0),
        ("YOB Demo Expired", "EXPIRED10", add_days(nowdate(), -60), add_days(nowdate(), -30), 100, 0),
        ("YOB Demo Exhausted", "USEDUP10", nowdate(), add_days(nowdate(), 90), 1, 1),
    ]
    for title, code, valid_from, valid_upto, maximum_use, used in specs:
        if frappe.db.exists("Coupon Code", {"coupon_code": code}):
            continue
        doc = frappe.get_doc({
            "doctype": "Coupon Code", "coupon_name": title, "coupon_code": code,
            "coupon_type": "Promotional", "pricing_rule": rule,
            "valid_from": valid_from, "valid_upto": valid_upto,
            "maximum_use": maximum_use,
        }).insert(ignore_permissions=True)
        if used:
            frappe.db.set_value("Coupon Code", doc.name, "used", used)
        _log(f"coupon: {code}")


def contacts_and_addresses():
    if not frappe.db.exists("Contact", {"first_name": "Demo", "last_name": "Buyer"}):
        c = frappe.get_doc({
            "doctype": "Contact", "first_name": "Demo", "last_name": "Buyer",
            "links": [{"link_doctype": "Customer", "link_name": CUSTOMER}],
        })
        c.append("email_ids", {"email_id": STORE_USER, "is_primary": 1})
        c.append("phone_nos", {"phone": "+91 98250 00000", "is_primary_phone": 1})
        c.insert(ignore_permissions=True)
        _log("contact: Demo Buyer")

    specs = [
        # GSTIN check digit is validated by india_compliance; 24ABCDE1234F1Z6 is a
        # checksum-valid Gujarat (24) test value, not a real registration.
        ("YOB Demo Billing", "Billing", "24ABCDE1234F1Z6", "Registered Regular"),
        ("YOB Demo Shipping", "Shipping", None, None),
    ]
    for title, atype, gstin, gst_category in specs:
        if frappe.db.exists("Address", {"address_title": title}):
            continue
        doc = frappe.get_doc({
            "doctype": "Address", "address_title": title, "address_type": atype,
            "address_line1": "Plot 42, GIDC Industrial Estate", "city": "Ahmedabad",
            "state": "Gujarat", "country": "India", "pincode": "382445",
            "links": [{"link_doctype": "Customer", "link_name": CUSTOMER}],
        })
        if gstin:
            doc.gstin = gstin
            doc.gst_category = gst_category
        doc.insert(ignore_permissions=True)
        _log(f"address: {title}")


def payment_methods():
    specs = [
        ("Pay Later", "paylater", "Offline", 1),
        ("Razorpay", "razorpay", "Online", 2),
    ]
    for name, code, ptype, order in specs:
        if not frappe.db.exists("Payment Method", {"method_code": code}):
            frappe.get_doc({
                "doctype": "Payment Method", "payment_method_name": name,
                "method_code": code, "payment_type": ptype, "is_active": 1,
                "display_order": order,
                "description": f"{name} demo payment method.",
            }).insert(ignore_permissions=True)
            _log(f"payment method: {code}")

        pm = frappe.db.get_value("Payment Method", {"method_code": code}, "name")
        if not frappe.db.exists("Payment Method Assignment",
                                {"payment_method": pm, "reference_name": _company()}):
            frappe.get_doc({
                "doctype": "Payment Method Assignment", "payment_method": pm,
                "reference_doctype": "Company", "reference_name": _company(),
                "is_active": 1, "minimum_order_amount": 0, "maximum_order_amount": 0,
            }).insert(ignore_permissions=True)
            _log(f"payment method assignment: {code}")


def fiscal_year():
    """A Fiscal Year covering today, or Sales Order submission fails.

    The site's setup wizard reported "Fiscal Year End Date should be one year
    after Fiscal Year Start Date" and left none usable, so this creates the
    standard Indian FY window that contains the current date.
    """

    from frappe.utils import getdate

    today = getdate(nowdate())
    start_year = today.year if today.month >= 4 else today.year - 1
    start, end = f"{start_year}-04-01", f"{start_year + 1}-03-31"
    name = f"{start_year}-{start_year + 1}"

    if frappe.db.exists("Fiscal Year", name):
        _log(f"fiscal year: {name} already present")
        return

    doc = frappe.get_doc({
        "doctype": "Fiscal Year", "year": name,
        "year_start_date": start, "year_end_date": end,
    })
    doc.flags.ignore_mandatory = True
    doc.insert(ignore_permissions=True)
    _log(f"fiscal year: {name} ({start} to {end})")


def sales_order():
    """One submitted order so order history is non-empty."""

    if frappe.db.exists("Sales Order", {"customer": CUSTOMER, "docstatus": 1}):
        _log("sales order: already present")
        return
    so = frappe.get_doc({
        "doctype": "Sales Order", "customer": CUSTOMER, "company": _company(),
        "currency": "INR", "selling_price_list": PRICE_LIST,
        "transaction_date": nowdate(), "delivery_date": add_days(nowdate(), 7),
        "items": [
            {"item_code": "YOB-BOLT-M10", "qty": 20, "rate": 12.50,
             "delivery_date": add_days(nowdate(), 7)},
            {"item_code": "YOB-GLOVE-L", "qty": 2, "rate": 249.00,
             "delivery_date": add_days(nowdate(), 7)},
        ],
    })
    so.insert(ignore_permissions=True)
    so.submit()
    _log(f"sales order: {so.name} submitted")


def run():
    """Seed the site. Refuses to run anywhere but an allow-listed test site."""

    site = frappe.local.site
    if site not in ALLOWED_SITES:
        frappe.throw(
            f"seed_demo_data refuses to run on '{site}'. "
            f"Allowed: {sorted(ALLOWED_SITES)}. This creates business records and "
            f"must never touch a production or active data site."
        )

    print(f"Seeding {site}")
    # Commit after each step. Without this a late failure rolls back every
    # earlier step, so a re-run has to redo all of them and only ever reaches
    # the same next error.
    steps = [
        ("store settings", store_settings),
        ("customer + access", customer_and_access),
        ("fiscal year", fiscal_year),
        ("categories", categories),
        ("items + prices", items_and_prices),
        ("contacts + addresses", contacts_and_addresses),
        ("payment methods", payment_methods),
    ]
    for label, step in steps:
        step()
        frappe.db.commit()

    rule = pricing_rule()
    frappe.db.commit()
    coupons(rule)
    frappe.db.commit()
    sales_order()
    frappe.db.commit()
    print("Seed complete.")
