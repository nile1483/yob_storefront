"""Illustrative capability-guarded guest payment endpoint."""

from __future__ import annotations

import frappe

from yob_core.api.boundary import yob_api
from yob_core.api.response import success_response
from yob_storefront.security.checkout import require_checkout_token
from yob_storefront.services.payments import process_checkout_payment


@frappe.whitelist(allow_guest=True, methods=["POST"])
@yob_api
@require_checkout_token
def process_payment(checkout_context=None):
	"""Use only the server-derived resource bound to the validated token."""
	result = process_checkout_payment(checkout_context=checkout_context)
	return success_response(
		{"payment_request": result.payment_request, "status": result.status}
	)
