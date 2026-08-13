# Copyright (c) 2026, YOB and Shayona
"""Display-safe payment summary for either payment source.

A Payment Request starts Cart-backed and becomes Sales-Order-backed at
commitment. Code that assumes "a Payment Request always references a Cart" is
correct only until the first commitment, and then silently wrong -- which is
what this dispatcher exists to prevent.

    Payment Request -> Cart          (before commitment)
    Payment Request -> Sales Order   (after commitment)

Two rules this module encodes:

* The MONEY always comes from the immutable Payment Request, never from the
  source document. The source supplies what to show; the obligation supplies
  what is owed. Those are equal by validation, and reading the obligation is
  what keeps them equal.
* A committed Sales-Order-backed obligation is NEVER compared against a current
  or new Cart. Its Cart is finished. ``payment_source.snapshot_for`` and
  ``validate_payment_request_source_current`` deal only in Cart fingerprints,
  and a Sales Order must not be run through either.

Separate from ``payment_source.py`` on purpose: that module builds the
canonical FINGERPRINT input and deliberately excludes presentation fields, so
mixing a display DTO into it would contradict its own contract.

NOT wired into ``get_checkout_data`` in Phase 2A -- see the note on
``payment_summary``.
"""

import frappe

from yob_storefront.api.response import (
    CART_NOT_FOUND,
    HTTP_NOT_FOUND,
    HTTP_UNPROCESSABLE,
    ORDER_NOT_FOUND,
    PAYMENT_REFERENCE_INVALID,
    error_response,
)

SUPPORTED_SOURCES = ("Cart", "Sales Order")


def payment_summary(pr, cart=None) -> dict:
    """Display-safe view of what this Payment Request is asking to be paid.

    Returns a dict, or an error envelope when the source is unusable.

    Deliberately NOT wired into ``get_checkout_data`` yet. Today an
    SO-backed Payment Request answers ``payment_reference_invalid`` there, and
    Pay Later leaves its checkout token live after moving the reference. Making
    SO-backed sources renderable would therefore newly expose order data through
    a token that currently returns an error -- a public behaviour change that
    belongs with Phase 2B's coherent initiation/settlement rewrite, not as a
    side effect of adding a dispatcher.

    ``cart`` may be passed when the caller already holds the repriced Cart from
    ``validate_payment_request_source_current``, avoiding a second calculation.
    """

    if pr.reference_doctype == "Cart":
        return _cart_summary(pr, cart)

    if pr.reference_doctype == "Sales Order":
        return _sales_order_summary(pr)

    return error_response(
        PAYMENT_REFERENCE_INVALID,
        "This checkout link is not valid.",
        status_code=HTTP_UNPROCESSABLE,
    )


def _cart_summary(pr, cart=None) -> dict:
    """Pre-commitment view, from the Cart the obligation was issued for."""

    if cart is None:
        if not frappe.db.exists("Cart", pr.reference_name):
            return error_response(
                CART_NOT_FOUND,
                "The cart for this checkout link no longer exists.",
                status_code=HTTP_NOT_FOUND,
            )
        cart = frappe.get_doc("Cart", pr.reference_name)

    return {
        "source_doctype": "Cart",
        "source_name": cart.name,
        "customer": cart.customer,
        "company": cart.company,
        # From the obligation, not the Cart -- see module docstring.
        "amount": pr.grand_total,
        "currency": pr.currency,
        "items": [
            {
                "item_code": row.item_code,
                "item_name": row.item_name,
                "quantity": row.quantity,
                "uom": row.uom,
                "rate": row.rate,
                "amount": row.total_amount,
            }
            for row in cart.items
        ],
        "billing_address": cart.billing_address,
        "shipping_address": cart.shipping_address,
        "contact_person": cart.contact_person,
        "is_shippable": int(cart.is_shippable or 0),
    }


def _sales_order_summary(pr) -> dict:
    """Post-commitment view, from the authoritative Sales Order.

    The Sales Order recalculated its own totals at commitment and the three-way
    invariant was asserted then, so its lines are the right thing to display.
    The payable amount still comes from the Payment Request.
    """

    if not pr.reference_name or not frappe.db.exists("Sales Order", pr.reference_name):
        return error_response(
            ORDER_NOT_FOUND,
            "The order for this payment no longer exists.",
            status_code=HTTP_NOT_FOUND,
        )

    so = frappe.get_doc("Sales Order", pr.reference_name)

    return {
        "source_doctype": "Sales Order",
        "source_name": so.name,
        "customer": so.customer,
        "company": so.company,
        "amount": pr.grand_total,
        "currency": pr.currency,
        "items": [
            {
                "item_code": row.item_code,
                "item_name": row.item_name,
                "quantity": row.qty,
                "uom": row.uom,
                "rate": row.rate,
                "amount": row.amount,
            }
            for row in so.items
        ],
        "billing_address": so.customer_address,
        "shipping_address": so.shipping_address_name,
        "contact_person": so.contact_person,
        # Order status is display-safe and is what a post-commitment payment
        # page needs; docstatus is included because Draft vs Submitted changes
        # what the buyer may still do.
        "order_status": so.status,
        "docstatus": so.docstatus,
    }
