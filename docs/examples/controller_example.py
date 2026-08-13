"""Illustrative thin Cart controller."""

from __future__ import annotations

from frappe.model.document import Document

from yob_storefront.services.cart_validation import validate_cart


class Cart(Document):
	def validate(self) -> None:
		validate_cart(self)
