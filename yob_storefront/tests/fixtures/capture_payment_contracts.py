# Copyright (c) 2026, YOB and Shayona
"""Capture the EXACT payment API contracts by executing the real endpoints.

    bench --site test.localhost execute \
        yob_storefront.tests.fixtures.capture_payment_contracts.run

Why this exists instead of a wire capture: every payment endpoint mutates
durable business state -- a real call creates Payment Requests, Sales Orders and
provider orders, and leaves the Cart Ordered. Running those against a site to
populate documentation is exactly the "casual payment mutation" that must not
happen. So the endpoints ARE executed, against real seeded data through the real
code path, but inside three safety layers:

1. a savepoint rolled back at the end, so nothing survives;
2. ``frappe.db.commit`` replaced by a recorder -- the Razorpay path contains a
   deliberate commit before the provider call which would otherwise end the
   transaction and defeat layer 1 (this leaked records into the test site once
   before it was caught);
3. a deterministic Razorpay fake enforcing the provider's documented receipt
   rules, so no network call and no real-money transaction can occur.

Responses are therefore REAL envelopes from the real implementation, but they
are labelled ``"verified": "test"`` -- never ``"wire"``. That distinction is the
point: a reader must be able to tell that no HTTP round trip and no live
provider were involved.

Checkout tokens are redacted. They are live bearer credentials.
"""

import copy
import json
import pathlib
from unittest.mock import patch

import frappe

ALLOWED_SITES = {"test.localhost"}

CUSTOMER = "YOB Demo Buyer"
ITEM = "YOB-BOLT-M10"
CONTACT = "Demo Buyer-YOB Demo Buyer"
BILLING = "YOB Demo Billing-Billing"
SHIPPING = "YOB Demo Shipping-Shipping"

REDACTED = "<REDACTED>"


# =========================================================
# SANITISATION
# =========================================================

def _sanitise(value):
    """Redact bearer credentials and provider keys, recursively."""

    if isinstance(value, dict):
        out = {}
        for key, inner in value.items():
            if key in ("token", "csrf_token", "sid", "razorpay_key",
                       "custom_checkout_token", "razorpay_signature"):
                out[key] = REDACTED
            elif key == "payment_url" and isinstance(inner, str):
                out[key] = "/payment/" + REDACTED
            else:
                out[key] = _sanitise(inner)
        return out

    if isinstance(value, list):
        return [_sanitise(v) for v in value]

    return value


def _envelope(inner, status):
    """Wrap an inner YOB body in Frappe's outer `message`, as the wire does."""

    return {"__http_status__": status,
            "message": _sanitise(frappe.parse_json(frappe.as_json(inner)))}


# =========================================================
# DETERMINISTIC PROVIDER
# =========================================================

class FakeRazorpay:
    """Mirrors Razorpay's documented receipt contract. No network."""

    class DuplicateReceipt(Exception):
        pass

    def __init__(self):
        self.orders = {}
        self.payments = {}

    def install(self):
        fake = self

        class _Order:
            def create(self, payload):
                receipt = payload.get("receipt")
                if receipt and any(o["receipt"] == receipt for o in fake.orders.values()):
                    raise fake.DuplicateReceipt("Order with this receipt already exists")
                oid = f"order_EXAMPLE{len(fake.orders) + 1:04d}"
                fake.orders[oid] = {
                    "id": oid, "receipt": receipt, "amount": payload["amount"],
                    "currency": payload["currency"], "status": "created",
                }
                return fake.orders[oid]

            def all(self, data=None):
                receipt = (data or {}).get("receipt")
                return {"items": [o for o in fake.orders.values()
                                  if not receipt or o["receipt"] == receipt]}

            def fetch(self, oid):
                return fake.orders[oid]

            def payments(self, oid):
                return {"items": [p for p in fake.payments.values()
                                  if p["order_id"] == oid]}

        class _Payment:
            def fetch(self, pid):
                return fake.payments[pid]

        class _Utility:
            def verify_payment_signature(self, params):
                if params.get("razorpay_signature") != "valid-signature":
                    import razorpay
                    raise razorpay.errors.SignatureVerificationError("bad signature")

        class _Client:
            order = _Order()
            payment = _Payment()
            utility = _Utility()

        from yob_storefront.integrations.razorpay import client as rz
        return patch.object(rz, "get_client", return_value=_Client())

    def pay(self, oid):
        order = self.orders[oid]
        order["status"] = "paid"
        pid = f"pay_EXAMPLE{len(self.payments) + 1:04d}"
        self.payments[pid] = {
            "id": pid, "order_id": oid, "status": "captured",
            "amount": order["amount"], "currency": order["currency"],
            "method": "card",
        }
        return pid


# =========================================================
# ENDPOINT INVOCATION
# =========================================================

def _raw(endpoint):
    import inspect
    return inspect.unwrap(endpoint)


def _status():
    """The HTTP status the endpoint just set on the Frappe response."""

    return (getattr(frappe.local, "response", {}) or {}).get("http_status_code", 200)


def _call(fn, **kwargs):
    inner = _raw(fn)(**kwargs)
    return _envelope(inner, _status())


# =========================================================
# CAPTURE
# =========================================================

def capture():
    """Execute every payment endpoint branch and return the contract map."""

    from yob_storefront.api import checkout as checkout_api
    from yob_storefront.api import payment as payment_api
    from yob_storefront.api import payment_method as pm_api

    customer = frappe.get_doc("Customer", CUSTOMER)
    fake = FakeRazorpay()
    out = {}

    # ---- a Draft Cart in checkout-ready state ------------------------------
    from yob_storefront.api.cart import get_or_create_cart
    from yob_storefront.services.cart_service import reprice_cart

    cart = get_or_create_cart(customer)
    cart.set("items", [])
    cart.append("items", {"item_code": ITEM, "quantity": 12,
                          "uom": "Nos", "stock_uom": "Nos"})
    cart.contact_person = CONTACT
    cart.billing_address = BILLING
    cart.shipping_address = SHIPPING
    reprice_cart(cart, customer)
    cart.save(ignore_permissions=True)

    # ---- proceed_to_payment ------------------------------------------------
    with patch.object(checkout_api, "get_storefront_customer", return_value=customer):
        created = _call(checkout_api.proceed_to_payment, auth_context={})
        reused = _call(checkout_api.proceed_to_payment, auth_context={})

    out["proceed_to_payment.created"] = created
    out["proceed_to_payment.reused"] = reused

    # The real token is needed to drive the remaining calls; it never leaves
    # this function.
    token = frappe.db.get_value("Payment Request",
                                created["message"]["data"]["payment_request"],
                                "custom_checkout_token")

    # ---- get_checkout_data, Cart-backed ------------------------------------
    out["get_checkout_data.cart_backed"] = _call(
        payment_api.get_checkout_data, token=token)

    # ---- error branches on the public GET ----------------------------------
    out["get_checkout_data.blank_token"] = _call(payment_api.get_checkout_data, token="")
    out["get_checkout_data.invalid_token"] = _call(
        payment_api.get_checkout_data, token="not-a-real-token")

    # Expired: move the expiry into the past, capture, then restore.
    pr_name = created["message"]["data"]["payment_request"]
    real_expiry = frappe.db.get_value("Payment Request", pr_name, "custom_checkout_expiry")
    frappe.db.set_value("Payment Request", pr_name, "custom_checkout_expiry",
                        frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-2))
    frappe.clear_document_cache("Payment Request", pr_name)
    out["get_checkout_data.expired_token"] = _call(payment_api.get_checkout_data, token=token)
    frappe.db.set_value("Payment Request", pr_name, "custom_checkout_expiry", real_expiry)
    frappe.clear_document_cache("Payment Request", pr_name)

    # Stale: change the Cart so it no longer matches the issued obligation.
    stale_cart = frappe.get_doc("Cart", cart.name)
    stale_cart.items[0].quantity = 20
    reprice_cart(stale_cart, customer)
    stale_cart.save(ignore_permissions=True)
    out["get_checkout_data.stale"] = _call(payment_api.get_checkout_data, token=token)
    out["process_payment.stale"] = _call(payment_api.process_payment,
                                         token=token, payment_method="Pay Later")

    # Restore the cart to the obligation's state.
    stale_cart.reload()
    stale_cart.items[0].quantity = 12
    reprice_cart(stale_cart, customer)
    stale_cart.save(ignore_permissions=True)

    # ---- process_payment validation branches -------------------------------
    out["process_payment.missing_method"] = _call(
        payment_api.process_payment, token=token, payment_method=None)
    out["process_payment.unknown_method"] = _call(
        payment_api.process_payment, token=token, payment_method="No Such Method")

    # Ineligible: deactivate the assignment, capture, restore.
    frappe.db.set_value("Payment Method Assignment", "Pay Later", "is_active", 0)
    frappe.clear_cache()
    out["process_payment.ineligible_method"] = _call(
        payment_api.process_payment, token=token, payment_method="Pay Later")
    frappe.db.set_value("Payment Method Assignment", "Pay Later", "is_active", 1)
    frappe.clear_cache()

    # ---- get_payment_methods (authenticated) -------------------------------
    with patch.object(pm_api, "assert_customer_matches"), \
            patch.object(pm_api, "get_storefront_customer", return_value=customer):
        out["get_payment_methods"] = _call(
            pm_api.get_payment_methods, customer=CUSTOMER,
            company=cart.company, order_amount=500, auth_context={})

    # ---- Razorpay initiation ----------------------------------------------
    frappe.db.set_single_value("Razorpay Settings", "api_key", "rzp_test_EXAMPLE")
    frappe.clear_document_cache("Razorpay Settings", "Razorpay Settings")

    with fake.install():
        out["process_payment.razorpay"] = _call(
            payment_api.process_payment, token=token, payment_method="Razorpay")

    razorpay_data = out["process_payment.razorpay"]["message"].get("data") or {}
    order_id = razorpay_data.get("order_id")

    # ---- get_checkout_data, Sales-Order-backed (after commitment) ----------
    out["get_checkout_data.sales_order_backed"] = _call(
        payment_api.get_checkout_data, token=token)

    # ---- Pay Later against the SAME committed obligation -------------------
    with fake.install():
        out["process_payment.paylater"] = _call(
            payment_api.process_payment, token=token, payment_method="Pay Later")

    # ---- provider failure AFTER local commitment ---------------------------
    from yob_storefront.integrations.razorpay import client as rz

    with patch.object(rz, "get_client", side_effect=Exception("provider unreachable")):
        out["process_payment.provider_failure"] = _call(
            payment_api.process_payment, token=token, payment_method="Razorpay")

    # ---- verify_payment ----------------------------------------------------
    payment_id = fake.pay(order_id) if order_id else None

    if payment_id:
        with fake.install():
            out["verify_payment.bad_signature"] = _call(
                payment_api.verify_payment,
                razorpay_order_id=order_id, razorpay_payment_id=payment_id,
                razorpay_signature="forged-signature")

            out["verify_payment.success"] = _call(
                payment_api.verify_payment,
                razorpay_order_id=order_id, razorpay_payment_id=payment_id,
                razorpay_signature="valid-signature")

            out["verify_payment.idempotent_retry"] = _call(
                payment_api.verify_payment,
                razorpay_order_id=order_id, razorpay_payment_id=payment_id,
                razorpay_signature="valid-signature")

    out["verify_payment.missing_field"] = _call(
        payment_api.verify_payment, razorpay_order_id=None,
        razorpay_payment_id="pay_x", razorpay_signature="s")

    # Inside the fake: signature verification must PASS so the branch actually
    # under test -- an order id that matches no Payment Request -- is the thing
    # that answers. Run outside the fake, the real SDK fails on the absent
    # api_secret and this captures a credential error instead of the contract.
    with fake.install():
        out["verify_payment.unknown_order"] = _call(
            payment_api.verify_payment, razorpay_order_id="order_DOES_NOT_EXIST",
            razorpay_payment_id="pay_x", razorpay_signature="valid-signature")

    return out


def run():
    """Capture, then restore the site exactly as found."""

    site = frappe.local.site

    if site not in ALLOWED_SITES:
        frappe.throw(
            f"capture_payment_contracts refuses to run on '{site}': it executes "
            "payment endpoints."
        )

    frappe.set_user("Administrator")

    # Layer 2 BEFORE layer 1: the Razorpay path commits before its provider
    # call, which would end the transaction the savepoint lives in.
    commits = []
    commit_patch = patch.object(frappe.db, "commit",
                                side_effect=lambda: commits.append(True))
    commit_patch.start()

    frappe.db.savepoint("yob_contract_capture")

    try:
        captured = capture()
    finally:
        frappe.db.rollback(save_point="yob_contract_capture")
        frappe.clear_cache()
        commit_patch.stop()

    out = pathlib.Path("/tmp/yob_payment_contracts.json")
    out.write_text(json.dumps(captured, indent=2))

    print(f"captured branches : {len(captured)}")
    print(f"commits suppressed: {len(commits)}")
    print(f"written to        : {out}")

    return captured
