# copyright (c) 2026, YOB
# path: apps/yob_storefront/yob_storefront/services/order_service.py

import frappe
from frappe.utils import today

from erpnext.accounts.doctype.pricing_rule.utils import (
    apply_pricing_rule_on_transaction,
)


# =========================================================
# HISTORICAL ADDRESS PROJECTION
# =========================================================

def order_address_display(snapshot: str | None, link: str | None) -> str | None:
    """Plain-text address for an order, from its IMMUTABLE order-time snapshot.

    THE BUG THIS EXISTS TO PREVENT. Order detail used to read the linked Address
    master live, so editing an address rewrote the address on every past order.
    An invoice-grade record must show what was true when the order was placed,
    not what the master says today.

    ERPNext already stores that snapshot at Sales Order creation:

        billing   Sales Order.address_display
        shipping  Sales Order.shipping_address     (the LINK lives in
                  `shipping_address_name` -- ERPNext's naming is genuinely
                  inverted here, so read the field, not the name)

    When a snapshot exists it is used and the Address master is NEVER read.

    ``link`` is only a LEGACY fallback for a Sales Order created before the
    snapshot was populated. Best-effort, and it returns the same ``str | None``
    type so the field never changes shape. A missing linked Address returns
    None rather than failing the whole order detail.

    Returns plain text with real newlines -- never HTML. The frontend renders it
    as text and must not need ``[innerHTML]``.
    """

    text = _plain_text_address(snapshot)

    if text:
        return text

    if not link:
        return None

    # LEGACY path only. Reaching here means the order has no stored snapshot.
    try:
        from frappe.contacts.doctype.address.address import get_address_display

        return _plain_text_address(get_address_display(link))
    except (frappe.DoesNotExistError, frappe.PermissionError):
        # A deleted or unreadable legacy Address must not break the whole order
        # detail -- the rest of the order is still valid and worth returning.
        #
        # Deliberately NOT a bare `except Exception`: those two are the real
        # failure modes for a dangling link, and catching everything here would
        # swallow genuine faults behind a silently blank address.
        return None


def _plain_text_address(value: str | None) -> str | None:
    """Stored address display HTML -> plain text, line breaks preserved.

    Uses Frappe's own ``html_to_plain_text``, which converts ``<br>`` to a
    newline and strips script/style. No new dependency: it uses bs4, which
    Frappe already ships.

    ``frappe.utils.strip_html`` is deliberately NOT used -- it removes ``<br>``
    along with every other tag and collapses a postal address onto one line.

    One normalisation on top: the stored value contains ``<br>\\n`` in places,
    which converts to two newlines and leaves blank lines mid-address. Those
    blanks are an artefact of the stored markup, not part of the address, so
    runs of blank lines are collapsed.
    """

    if not value:
        return None

    from frappe.core.utils import html_to_plain_text

    text = html_to_plain_text(value)

    lines = [line.strip() for line in (text or "").splitlines()]
    lines = [line for line in lines if line]

    return "\n".join(lines) or None


# =========================================================
# CREATE SALES ORDER FROM CART
# =========================================================

def create_sales_order_from_cart(cart):
    """
    Create a Sales Order from Cart.

    Args:
        cart (Cart): Cart document.

    Returns:
        Sales Order: Inserted Sales Order document.
    """

    so = frappe.new_doc("Sales Order")

    # -----------------------------------------------------
    # Basic Details
    # -----------------------------------------------------
    so.customer = cart.customer
    so.company = cart.company
    so.currency = cart.currency
    so.selling_price_list = cart.selling_price_list
    so.transaction_date = today()
    so.delivery_date = today()

    # -----------------------------------------------------
    # Coupon
    # -----------------------------------------------------
    if cart.coupon_code:
        coupon_name = frappe.db.get_value(
            "Coupon Code",
            {"coupon_code": cart.coupon_code},
            "name"
        )

        if coupon_name:
            so.coupon_code = coupon_name

    # -----------------------------------------------------
    # Customer Details
    # -----------------------------------------------------
    so.contact_person = cart.contact_person
    so.customer_address = cart.billing_address
    so.shipping_address_name = cart.shipping_address

    # -----------------------------------------------------
    # Taxes
    # -----------------------------------------------------
    # NO tax template is copied from the Cart. Cart owns calculated RESULTS
    # (net_total / tax_total / grand_total / total_discount), written back by
    # sync_sales_order_to_cart from the priced Sales Order. It has no
    # `taxes_and_charges` field and never did -- the previous
    # `if cart.taxes_and_charges:` raised AttributeError on every call, which
    # is why Pay Later has never worked.
    #
    # set_missing_values() below derives `taxes_and_charges` from the party,
    # address and Tax Category exactly as calculate_cart_using_sales_order()
    # does when the Cart is priced, so both sides reach the same template by
    # the same route. The caller then asserts the three-way financial
    # invariant (Cart == Payment Request == Sales Order) rather than trusting
    # that derivation.

    # -----------------------------------------------------
    # Items
    # -----------------------------------------------------
    for row in cart.items:
        so.append("items", {
            "item_code": row.item_code,
            "qty": row.quantity,
            "uom": row.uom or row.stock_uom,
            "stock_uom": row.stock_uom or row.uom,
            "rate": row.rate,
            "delivery_date": today(),
        })

    # -----------------------------------------------------
    # ERPNext Pricing Engine
    # -----------------------------------------------------
    # PUBLIC-PAYMENT AUTHORIZATION BOUNDARY.
    #
    # /payment/<token> is intentionally public: a payer may arrive from a shared
    # link with no storefront session at all, as Guest. Authorization for that
    # flow is the payment TOKEN, resolved server-side to exactly one Payment
    # Request -- not the session user's ERPNext DocType permissions.
    #
    # This flag must be set BEFORE set_missing_values(), not only on insert().
    # SellingController.set_missing_lead_customer_details passes
    # `ignore_permissions=self.flags.ignore_permissions` into
    # `_get_party_details`, which then runs
    # `frappe.has_permission(party_type, ptype, party, throw=True)`
    # (erpnext/accounts/party.py) and propagates `check_permissions` to the
    # billing/shipping address and contact fetches. Without the flag, a Guest
    # payer hits PermissionError on Customer during party resolution -- before
    # insert() is ever reached, which is why an `insert(ignore_permissions=True)`
    # alone did not fix it.
    #
    # The bypass is DocType-permission only and is scoped to this one
    # server-constructed document. It is safe here only because the caller has
    # already passed the full YOB authorization chain before this function is
    # reached: exact single-match token resolution, expiry and usability, source
    # binding read from the Payment Request (never from caller input), party
    # identity, fingerprint/amount/currency invariants, and payment-method
    # eligibility. Contact and address ownership were validated against the
    # customer when they were set, by authenticated endpoints, and are pinned by
    # the source fingerprint thereafter.
    #
    # It does NOT skip validation: ERPNext party checks, required fields, tax
    # and pricing consistency and India Compliance rules all still run, and the
    # caller asserts the Cart == Payment Request == Sales Order invariant after
    # this returns.
    #
    # The question this answers is deliberately NOT "may Guest read Customer?"
    # but "does this validated token authorize payment of this exact Payment
    # Request, whose trusted source authorizes exactly this Sales Order?".
    so.flags.ignore_permissions = True

    so.set_missing_values()

    apply_pricing_rule_on_transaction(so)

    so.calculate_taxes_and_totals()

    # -----------------------------------------------------
    # Save
    # -----------------------------------------------------
    so.insert(ignore_permissions=True)

    # Uncomment if you want immediate submission
    # so.submit()

    return so


# =========================================================
# SUBMIT SALES ORDER
# =========================================================

def submit_sales_order(so):
    """
    Submit Sales Order if not already submitted.
    """

    if isinstance(so, str):
        so = frappe.get_doc("Sales Order", so)

    if so.docstatus == 0:
        so.submit()

    return so


# =========================================================
# CANCEL SALES ORDER
# =========================================================

def cancel_sales_order(so):
    """
    Cancel Sales Order.
    """

    if isinstance(so, str):
        so = frappe.get_doc("Sales Order", so)

    if so.docstatus == 1:
        so.cancel()

    return so