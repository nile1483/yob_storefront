# Copyright (c) 2026, YOB and Shayona
"""Payment-source snapshot and fingerprint.

A Payment Request represents ONE immutable payment obligation. To know whether
a mutable source (today: Cart) still represents the obligation a PR was issued
for, we take a canonical snapshot of the payment-relevant state and hash it.

    fingerprint = sha256(canonical_json(snapshot))

Deliberately NOT used as fingerprint input:

* ``modified`` / ``creation`` / any timestamp -- repricing rewrites `modified`
  without changing the obligation, and unrelated writes bump it too.
* ``name`` / ``idx`` / ``owner`` -- identity and ordering metadata.
* ``item_name`` / ``image`` / ``item_slug`` / ``pricing_rule_label`` --
  presentation only; renaming an item must not invalidate a live payment link.
* ``status`` / ``sales_order`` / ``ordered_on`` / ``checkout_by`` -- lifecycle
  fields that change AS A RESULT of payment, which would make every commitment
  self-invalidating.
* ``build_cart_response()`` output -- it is an API view with formatting and
  volatile fields.

Included is what determines the money and what becomes the Sales Order, since
``create_sales_order_from_cart`` reads exactly these.

Not hardcoded to Cart: ``snapshot_for`` dispatches on the source doctype so a
later phase can add Sales Order without reshaping callers. A Sales-Order-backed
Payment Request must never be compared against a Cart fingerprint.
"""

import hashlib
import json

import frappe

SUPPORTED_SOURCES = ("Cart",)


def cart_payment_snapshot(cart) -> dict:
    """Canonical, deterministic view of a Cart's payment obligation."""

    return {
        "source_doctype": "Cart",
        # Party / commercial context -- all become Sales Order fields.
        "customer": cart.customer,
        "company": cart.company,
        "currency": cart.currency,
        "selling_price_list": cart.selling_price_list,
        # Order-relevant selections. Included even though they may not change
        # the total: they become part of the committed Sales Order, so changing
        # a delivery address IS a different obligation.
        "contact_person": cart.contact_person,
        "billing_address": cart.billing_address,
        "shipping_address": cart.shipping_address,
        "is_shippable": int(cart.is_shippable or 0),
        # Server-calculated money. Strings via repr of float would be
        # platform-sensitive, so values are normalised to 6dp.
        "net_total": _num(cart.net_total),
        "tax_total": _num(cart.tax_total),
        "total_discount": _num(cart.total_discount),
        "coupon_discount": _num(cart.coupon_discount),
        "coupon_code": cart.coupon_code,
        "grand_total": _num(cart.grand_total),
        # Lines, ordered canonically so row order can never change the hash.
        #
        # Sorting on a SUBSET of fields (item_code, uom, quantity, rate) is not
        # enough: two rows tying on those but differing in discount, tax,
        # conversion factor or pricing metadata would sort non-deterministically,
        # so the same Cart could hash two ways depending on stored row order.
        # Each line is fully normalised first, then sorted by its own canonical
        # serialisation -- every fingerprinted property participates in the order.
        #
        # Multiplicity is preserved: two identical lines stay two entries, so a
        # cart with one row of qty 5 never collides with two rows of qty 5.
        "items": sorted(
            (_line(row) for row in cart.items),
            key=_canonical,
        ),
    }


def _line(row) -> dict:
    """Complete payment-relevant normalisation of one Cart Item row."""

    return {
        "item_code": row.item_code,
        "quantity": _num(row.quantity),
        "uom": row.uom,
        "conversion_factor": _num(row.conversion_factor),
        "rate": _num(row.rate),
        "amount": _num(row.amount),
        "discount_percentage": _num(row.discount_percentage),
        "discount_amount": _num(row.discount_amount),
        "tax_amount": _num(row.tax_amount),
        "total_amount": _num(row.total_amount),
        # Which rule applied is financially relevant; its LABEL is not.
        "pricing_rules": row.pricing_rules or None,
        "pricing_rule_apply_on": row.pricing_rule_apply_on or None,
    }


def _canonical(value) -> str:
    """Deterministic serialisation, used both for ordering and for hashing."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=str)


def _num(value) -> str:
    """Normalise a numeric to a stable decimal string.

    Floats are formatted rather than embedded raw so that 135.0 and 135 cannot
    produce two different fingerprints for one obligation.
    """

    return f"{float(value or 0):.6f}"


def snapshot_for(source_doctype: str, source_name: str) -> dict:
    """Snapshot any supported payment source. Dispatch, not a framework."""

    if source_doctype not in SUPPORTED_SOURCES:
        frappe.throw(
            f"Unsupported payment source: {source_doctype}",
            frappe.ValidationError,
        )
    return cart_payment_snapshot(frappe.get_doc(source_doctype, source_name))


def fingerprint(snapshot: dict) -> str:
    """SHA-256 over canonical JSON. 64 hex chars."""

    return hashlib.sha256(_canonical(snapshot).encode("utf-8")).hexdigest()


def cart_fingerprint(cart) -> str:
    """Convenience: snapshot + hash for an already-loaded, repriced Cart."""

    return fingerprint(cart_payment_snapshot(cart))
