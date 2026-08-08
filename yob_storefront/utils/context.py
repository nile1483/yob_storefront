# Copyright (c) 2026, YOB and Shayona
# path: apps/yob_storefront/yob_storefront/utils/context.py
"""
Storefront authentication context adapter.

This module contains NO authentication logic. It exists only to translate the
trusted ``AuthContext`` produced by ``yob_auth`` into the Customer document
that the existing storefront services and workflows already expect.

All authentication, application-access authorization, session creation,
rate limiting and audit logging live in ``yob_auth``.
"""

import frappe

from yob_auth.security.exceptions import YOBAccessDeniedError

# Application code registered in the "YOB Application" DocType.
# Every external storefront endpoint authorizes against this code.
STOREFRONT_APP = "STOREFRONT"


def get_storefront_customer(auth_context):
    """Return the Customer document for the authenticated storefront session.

    The Customer is taken exclusively from the server-generated
    ``auth_context`` produced by ``yob_auth.security.decorators.require_application``.
    Request parameters are never consulted.

    A full Customer document is returned (not just the name) so that existing
    callers keep working unchanged: the removed legacy storefront auth helper
    returned a dict exposing ``name``, ``customer_name``, ``default_price_list``,
    ``customer_group`` and ``territory``, and the Customer document exposes the
    same attributes.
    """

    if auth_context is None:
        # Can only happen if an endpoint is called without the decorator.
        raise YOBAccessDeniedError("Storefront authorization context is missing")

    if auth_context.profile_doctype != "Customer" or not auth_context.profile_name:
        raise YOBAccessDeniedError("Customer profile is required")

    return frappe.get_doc("Customer", auth_context.profile_name)


def assert_customer_matches(auth_context, supplied_customer):
    """Reject a caller-supplied Customer that is not the authenticated one.

    Some endpoints keep a ``customer`` parameter for frontend compatibility.
    The parameter is never used as authorization truth; it is only allowed to
    agree with the authenticated context.
    """

    if not supplied_customer:
        return

    if supplied_customer != auth_context.profile_name:
        raise YOBAccessDeniedError("Customer does not match the authenticated session")
