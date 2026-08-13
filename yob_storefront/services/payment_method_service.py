# Copyright (c) 2026, YOB and Shayona
"""Payment Method eligibility -- the single authority.

Which payment methods a buyer may use was previously implemented TWICE, in
``api/payment_method.get_payment_methods`` and in
``cart_service.get_available_payment_methods``. Two copies of a rule that gates
money is one copy too many: they had already drifted (only one of them guarded
against a missing customer), and ``process_payment`` will shortly need to
re-check the same rule authoritatively.

This module owns the rule. Callers -- the authenticated list, the public
checkout payload, and later ``process_payment`` -- ask this service and never
re-derive it.

No new eligibility rules are introduced here. The behaviour is exactly what the
two previous copies did, with the stricter of their two null-guards kept:

* the Payment Method Assignment must be active;
* an assignment targeting Customer / Customer Group / Company must match;
* the order amount must satisfy minimum_order_amount / maximum_order_amount;
* the Payment Method itself must be active.

Frontend selection is never trusted. ``is_payment_method_eligible`` exists so
that a later ``process_payment`` can re-check the method the browser sent
against the same rule that produced the list, rather than assuming the client
returned something it was offered.
"""

import frappe

#: Fields the client renders. Ordering and presentation are server-owned: the
#: browser displays exactly what the server says is eligible, in this order.
PAYMENT_METHOD_FIELDS = (
    "name",
    "method_code",
    "payment_type",
    "display_order",
    "icon",
    "description",
)


def get_eligible_payment_methods(customer, company, amount) -> list:
    """Payment methods this party may use for an order of this amount.

    ``customer`` and ``company`` are resolved names, never client-supplied
    values -- the caller has already established identity. ``amount`` is the
    authoritative order total (the immutable Payment Request's ``grand_total``
    once an obligation exists, the calculated Cart total before that).

    Returns a list of dicts carrying ``PAYMENT_METHOD_FIELDS``, ordered by
    ``display_order``. Empty list when nothing is eligible -- that is a normal
    answer, not an error.
    """

    amount = float(amount or 0)

    customer_group = (
        frappe.db.get_value("Customer", customer, "customer_group")
        if customer else None
    )

    assignments = frappe.get_all(
        "Payment Method Assignment",
        filters={"is_active": 1},
        fields=[
            "payment_method",
            "reference_doctype",
            "reference_name",
            # The DocType fields are minimum_/maximum_order_amount. The short
            # names raised OperationalError 1054 on every call, which is why
            # this endpoint once returned 500 for every caller.
            "minimum_order_amount",
            "maximum_order_amount",
        ],
    )

    eligible = {
        row.payment_method
        for row in assignments
        if _assignment_applies(row, customer, customer_group, company, amount)
    }

    if not eligible:
        return []

    return frappe.get_all(
        "Payment Method",
        filters={"name": ["in", list(eligible)], "is_active": 1},
        fields=list(PAYMENT_METHOD_FIELDS),
        order_by="display_order asc",
    )


def is_payment_method_eligible(payment_method, customer, company, amount) -> bool:
    """Authoritative re-check of ONE method the client claims to have chosen.

    Derived from the same list rather than from a parallel predicate, so the
    offered set and the accepted set cannot drift apart. ``payment_method`` is
    the Payment Method's ``name``.
    """

    if not payment_method:
        return False

    return any(
        method["name"] == payment_method
        for method in get_eligible_payment_methods(customer, company, amount)
    )


def _assignment_applies(assignment, customer, customer_group, company, amount) -> bool:
    """Does one Payment Method Assignment cover this party and amount?"""

    # -----------------------------
    # Assignment target
    # -----------------------------
    # `reference_doctype` is a Select limited to Company / Customer Group /
    # Customer. An assignment with none of those set falls through as
    # unrestricted, which is the behaviour both previous copies had; it is
    # preserved deliberately rather than tightened, because narrowing it would
    # silently remove a payment method from whoever relies on it today.
    if assignment.reference_doctype == "Customer":
        if not customer or assignment.reference_name != customer:
            return False

    elif assignment.reference_doctype == "Customer Group":
        if not customer_group or assignment.reference_name != customer_group:
            return False

    elif assignment.reference_doctype == "Company":
        if not company or assignment.reference_name != company:
            return False

    # -----------------------------
    # Order amount
    # -----------------------------
    # Falsy bounds mean "no bound", so a 0 minimum is not a constraint. This is
    # the existing semantic and the DocType has no default, so an unset
    # Currency field reads as 0.
    if assignment.minimum_order_amount and amount < assignment.minimum_order_amount:
        return False

    if assignment.maximum_order_amount and amount > assignment.maximum_order_amount:
        return False

    return True
