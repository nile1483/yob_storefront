# Copyright (c) 2026, YOB and Shayona
"""Install-time setup for yob_storefront."""

import frappe

from yob_storefront.utils.context import STOREFRONT_APP


def after_install():
    _ensure_storefront_application()


def _ensure_storefront_application():
    """Register the STOREFRONT application in yob_auth.

    Without this record every storefront endpoint returns 403, because
    ``require_application("STOREFRONT", ...)`` cannot resolve the application.
    Creating it here makes a fresh install functional out of the box.

    This registers the application *definition* only. It grants nobody access:
    a user still needs an explicit ``YOB User Application Access`` record, which
    is deliberately NOT created by code -- access grants must be an
    administrator's conscious act.

    ``domains`` is intentionally left empty because the allowed host is
    site-specific, and because domain validation requires the edge proxy to send
    ``X-YOB-Original-Host``. See docs/yob_storefront_deployment.md.
    """

    if frappe.db.exists("YOB Application", STOREFRONT_APP):
        return

    doc = frappe.new_doc("YOB Application")
    doc.application_code = STOREFRONT_APP
    doc.application_name = "YOB Storefront"
    doc.enabled = 1
    doc.domains = ""
    # Enforced centrally: every access grant for this application must resolve
    # to a Customer, which is what the storefront endpoints require.
    doc.required_profile_doctype = "Customer"
    doc.allow_password_login = 1
    doc.allow_email_otp = 0
    doc.allow_mobile_otp = 0
    doc.insert(ignore_permissions=True)

    print(f"yob_storefront: created YOB Application '{STOREFRONT_APP}'")
