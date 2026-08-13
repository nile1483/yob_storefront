# Copyright (c) 2026, YOB and Shayona
"""The one place a Payment Method becomes a provider driver.

    Payment Method -> Payment Gateway -> registry -> YOBGateway

Dispatch is by the Frappe ``Payment Gateway`` link on the Payment Method, NOT
by ``method_code``. ``method_code`` stays for display and frontend
compatibility, but making it the dispatch key is what produces
``if razorpay / if stripe / if paypal`` chains scattered through payment code.

Internal YOB methods -- Pay Later today -- have NO Payment Gateway. That is not
a missing configuration: they have no external provider, so ``resolve_gateway``
returns None and the caller handles them itself. One internal branch is
acceptable; per-provider branches are not.

An unknown or unregistered gateway fails closed with UnsupportedProvider rather
than falling through to a default, because guessing which provider should take
a payment is never safe.
"""

import frappe

from yob_storefront.integrations.gateways.base import (
    UnsupportedProvider,
    YOBGateway,
)

#: gateway name (Payment Gateway.gateway) -> driver class.
_DRIVERS: dict[str, type[YOBGateway]] = {}


def register(driver: type[YOBGateway]) -> type[YOBGateway]:
    """Register a driver. Usable as a decorator on the class."""

    if not driver.provider:
        raise ValueError(f"{driver.__name__} must declare `provider`")

    _DRIVERS[driver.provider] = driver
    return driver


def registered_providers() -> tuple:
    return tuple(sorted(_DRIVERS))


def get_driver(provider: str) -> YOBGateway:
    """Instantiate the driver for a Payment Gateway name. Fails closed."""

    _load_drivers()

    driver = _DRIVERS.get(provider)

    if driver is None:
        raise UnsupportedProvider(provider)

    return driver()


def resolve_gateway(payment_method) -> YOBGateway | None:
    """The driver for a Payment Method, or None for an internal YOB method.

    ``payment_method`` may be a document or a name.

    Returns None ONLY when the method has no ``payment_gateway`` link, which
    means "internal method, no external provider". A method that names a
    gateway with no registered driver raises UnsupportedProvider -- silently
    treating it as internal would let a misconfigured online method be
    fulfilled with no payment at all.
    """

    if isinstance(payment_method, str):
        payment_method = frappe.get_doc("Payment Method", payment_method)

    gateway_name = (payment_method.get("payment_gateway") or "").strip()

    if not gateway_name:
        return None

    # The Payment Gateway record carries the provider identity; the link's own
    # name and its `gateway` value can differ, so read the field rather than
    # assuming they match.
    provider = frappe.db.get_value("Payment Gateway", gateway_name, "gateway")

    if not provider:
        raise UnsupportedProvider(gateway_name)

    return get_driver(provider)


def _load_drivers() -> None:
    """Import driver modules so their registrations run.

    Explicit rather than a package scan: a payment provider appearing because a
    file was dropped in a directory is not a property worth having.
    """

    if _DRIVERS:
        return

    from yob_storefront.integrations.gateways import razorpay_gateway  # noqa: F401
