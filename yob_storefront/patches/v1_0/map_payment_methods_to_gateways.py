# Copyright (c) 2026, YOB and Shayona
"""Map existing Payment Methods to their Frappe Payment Gateway.

Provider dispatch moves from ``method_code`` to the ``payment_gateway`` link.
Existing rows predate that field, so they need a one-time backfill.

Mapping is by EVIDENCE only:

    method_code == "razorpay"  -> Payment Gateway "Razorpay"
    method_code == "paylater"  -> NULL (internal YOB method, no provider)
    anything else              -> left NULL and REPORTED

An unrecognised method is deliberately not guessed at. Guessing wrong in either
direction is harmful: inventing a gateway link could send a payment to the wrong
provider, and it is better for an administrator to see the method listed here
than for it to be silently mapped.

A method left NULL keeps working as an internal method, so this patch cannot
break an existing site; it can only leave a method needing manual attention.
"""

import frappe

#: method_code -> Payment Gateway name. Only codes YOB actually ships.
KNOWN = {"razorpay": "Razorpay"}

#: method_code values that are internal YOB methods with no external provider.
INTERNAL = {"paylater"}


def execute():
    if not frappe.db.has_column("Payment Method", "payment_gateway"):
        # DocType sync runs before patches on a normal migrate; if the column is
        # genuinely absent there is nothing to backfill yet.
        return

    from yob_storefront.install import ensure_payment_gateways

    ensure_payment_gateways()

    unmapped = []

    for method in frappe.get_all("Payment Method",
                                 fields=["name", "method_code", "payment_gateway"]):
        if method.payment_gateway:
            continue                                  # already mapped

        code = (method.method_code or "").strip().lower()

        if code in INTERNAL:
            continue                                  # correctly NULL

        gateway = KNOWN.get(code)

        if not gateway:
            unmapped.append(f"{method.name} (method_code={method.method_code!r})")
            continue

        if not frappe.db.exists("Payment Gateway", gateway):
            unmapped.append(f"{method.name} -> Payment Gateway '{gateway}' missing")
            continue

        frappe.db.set_value("Payment Method", method.name,
                            "payment_gateway", gateway)
        print(f"yob_storefront: Payment Method '{method.name}' -> gateway '{gateway}'")

    if unmapped:
        print(
            "yob_storefront: the following Payment Methods need a MANUAL "
            "payment_gateway mapping (left unset, treated as internal):\n  "
            + "\n  ".join(unmapped)
        )
