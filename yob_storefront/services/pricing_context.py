# Copyright (c) 2026, YOB and Shayona
"""The one trusted selling-transaction context for the storefront.

WHY THIS EXISTS
---------------
Phase 23B-1 reproduced a real customer-visible defect: the product page showed
600 and the Cart charged 1000 for the same item, same customer, same session.

The cause was two independent resolvers for the same question:

    product preview   get_price_list_for_customer()
                      -> Customer -> Customer Group -> Selling Settings
    cart              get_or_create_cart()
                      -> YOB Store Settings.default_price_list   (ignores both)

Whenever a Customer or Customer Group carried its own price list, the two
disagreed -- and the Cart, being authoritative, won. Worse, the Cart stored that
value at CREATION and never re-resolved it, so a later change to the customer's
price list never reached an existing cart.

Everything that prices a storefront transaction now resolves through this one
object, so the two paths cannot drift again.

WHAT IT IS NOT
--------------
It is not a pricing engine. It answers "what transaction is this?" -- customer,
company, currency, price list, date, warehouse. ERPNext still owns every rate,
discount, rule and free item. Nothing here is accepted from the browser.
"""

import frappe
from frappe.utils import cint, flt, today


class SellingContext:
    """Trusted, request-level transaction context. Server-derived only."""

    def __init__(self, customer_doc, qty=1):
        from yob_storefront.services.pricing_service import get_price_list_for_customer
        from yob_storefront.utils.store import get_store_settings

        settings = get_store_settings()

        self.customer_doc = customer_doc
        self.customer = customer_doc.name
        self.qty = flt(qty) or 1

        # Company and currency stay the store's: a storefront sells one company's
        # catalogue in one currency, and neither is a per-customer decision today.
        self.company = settings.company
        self.currency = settings.default_currency
        self.transaction_date = today()

        # THE fix. Customer -> Customer Group -> Selling Settings, exactly as
        # ERPNext resolves it, rather than YOB Store Settings.default_price_list.
        self.price_list = get_price_list_for_customer(customer_doc)

        selling = frappe.get_cached_doc("Selling Settings")
        self.fallback_enabled = bool(cint(selling.fallback_to_default_price_list))
        self.default_price_list = selling.selling_price_list

    @property
    def price_lists(self):
        """Every list a price could legitimately come from."""
        lists = [self.price_list]
        if self.fallback_enabled and self.default_price_list:
            lists.append(self.default_price_list)
        return [pl for pl in dict.fromkeys(lists) if pl]

    def resolved_warehouse(self, item_code):
        """The warehouse ERPNext itself would put on a Sales Order line.

        Deliberately ASKS ERPNext instead of reimplementing the precedence
        (Item Default per company -> Item Group -> Stock Settings). Item defaults
        are a child table keyed on company, and any YOB copy of that chain would
        be a second source of truth free to disagree with the order it is meant
        to describe.

        Returns None when ERPNext resolves nothing -- which is a real answer, not
        a failure, and callers must not invent a warehouse to fill the gap.
        """

        from erpnext.stock.get_item_details import get_item_details

        args = frappe._dict({
            "item_code": item_code,
            "customer": self.customer,
            "company": self.company,
            "currency": self.currency,
            "price_list": self.price_list,
            "transaction_date": self.transaction_date,
            "qty": 1,
            "doctype": "Sales Order",
            "conversion_rate": 1,
            "plc_conversion_rate": 1,
            "price_list_currency": self.currency,
            "ignore_pricing_rule": 1,
        })
        try:
            return get_item_details(args, doc=None, for_validate=True).get("warehouse")
        except (frappe.ValidationError, frappe.DoesNotExistError):
            # An item ERPNext refuses to describe has no transaction warehouse --
            # a real answer, so availability is reported as unknown rather than
            # failing the catalogue read.
            #
            # Deliberately NOT a bare `except Exception`: those two are the real
            # failure modes here, and swallowing everything would hide a genuine
            # fault behind a silently missing stock figure.
            return None


def context_for(customer_doc, qty=1):
    """Build the trusted context. The only supported entry point."""

    return SellingContext(customer_doc, qty=qty)
