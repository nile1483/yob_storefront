"""Illustrative storefront service; transport-independent and context-driven."""

from __future__ import annotations

import frappe

from yob_auth.security.context import AuthContext


def list_customer_orders(*, auth_context: AuthContext) -> list[dict]:
	if auth_context.profile_doctype != "Customer" or not auth_context.profile_name:
		raise ValueError("The caller must supply a validated Customer AuthContext")

	# External storefront users may intentionally lack generic Sales Order read
	# permission. This bypass is safe only because the Customer filter comes from
	# the server-generated context and cross-customer tests enforce it.
	return frappe.get_all(
		"Sales Order",
		filters={"customer": auth_context.profile_name},
		fields=["name", "transaction_date", "grand_total", "status"],
		order_by="transaction_date desc",
		limit_page_length=50,
	)
