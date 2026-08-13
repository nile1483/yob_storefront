# Copyright (c) 2026, YOB and Shayona
"""Generate the YOB API contract artifacts from live introspection.

    bench --site test.localhost execute \
        yob_storefront.tests.fixtures.generate_api_docs.run

Emits into ``yob_storefront/docs/contracts/generated/``:

* ``openapi.json``          -- OpenAPI 3.1, the machine-readable source of truth
* ``postman_collection.json`` -- importable collection with sid/CSRF variables

Everything is derived, never hand-written: paths and verbs from Frappe's own
registry, parameters from ``inspect.signature``, guest status from
``frappe.guest_methods``, and response examples captured by actually calling the
endpoints against seeded data. Hand-maintained API docs drift; this cannot.

Read-only with respect to business data, but it DOES execute endpoints, so run
it on a disposable site only.
"""

import importlib
import inspect
import json
import pathlib
import pkgutil

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

ALLOWED_SITES = {"test.localhost"}
BUYER = "storefront@yob.test"
OUT = pathlib.Path(frappe.get_app_path("yob_storefront")).parent / "docs" / "contracts" / "generated"

# Endpoints safe to call for a success example, with the arguments to use.
# Anything mutating or provider-dependent is documented from its contract only.
SAFE_CALLS = {
    "yob_storefront.api.catalog.get_categories": {},
    "yob_storefront.api.catalog.get_category": {"slug": "fasteners", "qty": 10},
    "yob_storefront.api.catalog.get_item": {"slug": "hex-bolt-m10-50", "qty": 10},
    "yob_storefront.api.cms.get_config": {},
    "yob_storefront.api.cart.get_cart": {},
    "yob_storefront.api.order.get_orders": {},
    "yob_storefront.api.address.get_contacts": {},
    "yob_storefront.api.address.get_addresses": {},
    "yob_storefront.api.payment_method.get_payment_methods": {"order_amount": 500},
}

# One deliberate error example per shape, so clients see both branches.
ERROR_CALLS = {
    "yob_storefront.api.catalog.get_category": {"slug": "no-such-slug"},
    "yob_storefront.api.order.get_order_details": {"order_id": "NO-SUCH-ORDER"},
    "yob_storefront.api.payment.get_checkout_data": {"token": "invalid-token"},
}


def _trusted_request():
    domains = frappe.db.get_value("YOB Application", "STOREFRONT", "domains") or ""
    host = next((d.strip() for d in domains.splitlines() if d.strip()), "localhost")
    frappe.local.request = Request(
        EnvironBuilder(headers={"X-YOB-Original-Host": host}).get_environ()
    )
    frappe.local.form_dict = frappe._dict()


def _endpoints():
    """Every whitelisted YOB endpoint with its declared transport contract."""

    found = []
    for app in ("yob_auth", "yob_storefront"):
        pkg = importlib.import_module(f"{app}.api")
        for mod_info in pkgutil.iter_modules(pkg.__path__):
            mod = importlib.import_module(f"{app}.api.{mod_info.name}")
            for name, fn in vars(mod).items():
                if not callable(fn) or not getattr(fn, "__module__", "").startswith(app):
                    continue
                if fn not in frappe.whitelisted:
                    continue
                try:
                    sig = inspect.signature(fn)
                except (TypeError, ValueError):
                    continue
                params = [
                    {"name": p.name,
                     "required": p.default is inspect.Parameter.empty,
                     "default": None if p.default is inspect.Parameter.empty else p.default}
                    for p in sig.parameters.values()
                    if p.name != "auth_context"
                ]
                methods = list(
                    frappe.allowed_http_methods_for_whitelisted_func.get(fn) or []
                )
                found.append({
                    "path": f"{app}.api.{mod_info.name}.{name}",
                    "methods": methods or ["GET", "POST"],
                    "guest": fn in frappe.guest_methods,
                    "params": params,
                    "summary": (inspect.getdoc(fn) or "").split("\n")[0],
                })
    return sorted(found, key=lambda e: e["path"])


def _capture(dotted, kwargs):
    module_path, fn_name = dotted.rsplit(".", 1)
    fn = getattr(importlib.import_module(module_path), fn_name)
    try:
        return frappe.parse_json(frappe.as_json(fn(**kwargs)))
    except Exception as exc:  # documented as-is; never hidden
        return {"__uncaptured__": type(exc).__name__}


def _openapi(endpoints, examples, errors):
    paths = {}
    for ep in endpoints:
        verb = "post" if "POST" in ep["methods"] else "get"
        op = {
            "summary": ep["summary"] or ep["path"].rsplit(".", 1)[-1],
            "operationId": ep["path"],
            "tags": [ep["path"].split(".")[0]],
            "security": [] if ep["guest"] else [{"sessionCookie": []}],
            "responses": {
                "200": {"description": "Success envelope",
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Success"}}}},
                "4XX": {"description": "Error envelope",
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
            },
        }
        if ep["path"] in examples:
            op["responses"]["200"]["content"]["application/json"]["example"] = examples[ep["path"]]
        if ep["path"] in errors:
            op["responses"]["4XX"]["content"]["application/json"]["example"] = errors[ep["path"]]
        if verb == "post":
            op["requestBody"] = {"content": {"application/json": {"schema": {
                "type": "object",
                "required": [p["name"] for p in ep["params"] if p["required"]],
                "properties": {p["name"]: {"description": f"default: {p['default']!r}"} for p in ep["params"]},
            }}}}
            if not ep["guest"]:
                op["parameters"] = [{"name": "X-Frappe-CSRF-Token", "in": "header",
                                     "required": True, "schema": {"type": "string"}}]
        else:
            op["parameters"] = [
                {"name": p["name"], "in": "query", "required": p["required"], "schema": {"type": "string"}}
                for p in ep["params"]
            ]
        paths[f"/api/method/{ep['path']}"] = {verb: op}

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "YOB Storefront & Auth API",
            "version": "1.0.0",
            "description": (
                "Frappe RPC-style API. Every response is wrapped by Frappe in an outer "
                "`message` object; the inner body is the YOB envelope (`data`/`notice`/`meta` "
                "on success, `errors[]` on failure). There is no `success` boolean: branch on "
                "HTTP status, then `errors[0].code`.\n\n"
                "CSRF is Frappe's own. Non-GET requests need `X-Frappe-CSRF-Token`, obtained "
                "from login or `get_session_context`. Authenticated calls must traverse the "
                "edge proxy that sets `X-YOB-Original-Host`."
            ),
        },
        "servers": [{"url": "https://storefront.local"}],
        "components": {
            "securitySchemes": {"sessionCookie": {"type": "apiKey", "in": "cookie", "name": "sid"}},
            "schemas": {
                "Success": {"type": "object", "properties": {"message": {"type": "object", "properties": {
                    "data": {}, "notice": {"type": "string"}, "meta": {"type": "object"}}}}},
                "Error": {"type": "object", "properties": {"message": {"type": "object", "properties": {
                    "errors": {"type": "array", "items": {"type": "object", "properties": {
                        "code": {"type": "string"}, "detail": {"type": "string"},
                        "field": {"type": "string"}}}}}}}},
            },
        },
        "paths": paths,
    }


def _postman(endpoints):
    items = []
    for ep in endpoints:
        verb = "POST" if "POST" in ep["methods"] else "GET"
        headers = [{"key": "Content-Type", "value": "application/json"}]
        if verb == "POST" and not ep["guest"]:
            headers.append({"key": "X-Frappe-CSRF-Token", "value": "{{csrf_token}}"})
        req = {"method": verb, "header": headers,
               "url": {"raw": "{{base_url}}/api/method/" + ep["path"],
                       "host": ["{{base_url}}"], "path": ["api", "method", ep["path"]]}}
        if verb == "POST":
            body = {p["name"]: "" for p in ep["params"]}
            req["body"] = {"mode": "raw", "raw": json.dumps(body, indent=2)}
        items.append({"name": ep["path"], "request": req})
    return {
        "info": {"name": "YOB Storefront & Auth API",
                 "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
        "variable": [{"key": "base_url", "value": "http://storefront.local:8080"},
                     {"key": "csrf_token", "value": ""}],
        "item": items,
    }


def run():
    site = frappe.local.site
    if site not in ALLOWED_SITES:
        frappe.throw(f"generate_api_docs refuses to run on '{site}'; it executes endpoints.")

    frappe.set_user(BUYER)
    _trusted_request()

    endpoints = _endpoints()
    examples = {p: _capture(p, kw) for p, kw in SAFE_CALLS.items()}
    errors = {p: _capture(p, kw) for p, kw in ERROR_CALLS.items()}

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "openapi.json").write_text(json.dumps(_openapi(endpoints, examples, errors), indent=2))
    (OUT / "postman_collection.json").write_text(json.dumps(_postman(endpoints), indent=2))

    captured = sum(1 for v in examples.values() if "__uncaptured__" not in v)
    print(f"endpoints documented : {len(endpoints)}")
    print(f"success examples     : {captured}/{len(SAFE_CALLS)} captured live")
    print(f"error examples       : {sum(1 for v in errors.values() if '__uncaptured__' not in v)}/{len(ERROR_CALLS)}")
    print(f"written to           : {OUT}")
    for p, v in {**examples, **errors}.items():
        if "__uncaptured__" in v:
            print(f"  UNCAPTURED {p} -> {v['__uncaptured__']}")
