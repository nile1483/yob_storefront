"""Illustrative protected storefront endpoint."""

from __future__ import annotations

import frappe

from yob_auth.security.decorators import require_application
from yob_core.api.boundary import yob_api
from yob_core.api.response import success_response
from yob_storefront.services.orders import list_customer_orders


@frappe.whitelist(methods=["GET"])
@yob_api
@require_application("STOREFRONT", profile_doctype="Customer")
def get_orders(auth_context=None):
	orders = list_customer_orders(auth_context=auth_context)
	return success_response(orders, meta={"count": len(orders)})
