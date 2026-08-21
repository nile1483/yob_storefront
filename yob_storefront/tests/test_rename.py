# Copyright (c) 2026, YOB and Shayona
"""
Rename-integrity tests.

These are pure static/import checks: they need no site data and no fixtures, so
they run fast and catch the classic rename regressions (a stale dotted path in
hooks, a surviving `yob.` import, a scheduler entry pointing at a dead module).
"""

import ast
import importlib
import pathlib
import unittest

import frappe

APP = "yob_storefront"
APP_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _python_files():
    """Every module in the app."""
    for path in APP_ROOT.rglob("*.py"):
        if "__pycache__" not in str(path):
            yield path


def _scannable_files():
    """Files subject to forbidden-string scans.

    The tests package is excluded: these test modules necessarily contain the
    very literals they search for (``from yob.``, ``setdefault("auth_context"``,
    ...), so including them would make every scan flag itself.
    """
    for path in _python_files():
        if "tests" not in path.parts:
            yield path
    for path in APP_ROOT.rglob("*.json"):
        if "__pycache__" not in str(path):
            yield path


class TestPackageRename(unittest.TestCase):
    def test_all_modules_import(self):
        """Every module in the app imports cleanly under the new package name."""
        failures = []
        for path in _python_files():
            rel = path.relative_to(APP_ROOT.parent).with_suffix("")
            dotted = ".".join(rel.parts)
            if dotted.endswith(".__init__"):
                dotted = dotted[: -len(".__init__")]
            try:
                importlib.import_module(dotted)
            except Exception as exc:  # noqa: BLE001 - we want the full picture
                failures.append(f"{dotted}: {type(exc).__name__}: {exc}")
        self.assertEqual(failures, [], "modules failed to import:\n" + "\n".join(failures))

    def test_no_stale_old_package_references(self):
        """No source file still refers to the old `yob` package."""
        offenders = []
        needles = ("from yob.", "import yob.", '"yob.', "'yob.", "/assets/yob/")
        for path in _scannable_files():
            text = path.read_text(encoding="utf-8", errors="ignore")
            for needle in needles:
                if needle in text:
                    offenders.append(f"{path.relative_to(APP_ROOT)}: {needle}")
        self.assertEqual(offenders, [], "stale references:\n" + "\n".join(offenders))

    def test_module_def_name(self):
        self.assertEqual(
            (APP_ROOT / "modules.txt").read_text().strip(), "yob_storefront"
        )

    def test_app_declares_yob_auth_dependency(self):
        """yob_storefront must fail loudly if yob_auth is missing."""
        hooks = frappe.get_hooks(app_name=APP)
        self.assertIn("yob_auth", hooks.get("required_apps", []))


class TestDottedPathsResolve(unittest.TestCase):
    """Every dotted path Frappe will resolve at runtime must be importable."""

    def _assert_resolvable(self, paths, label):
        broken = []
        for dotted in paths:
            try:
                frappe.get_attr(dotted)
            except Exception as exc:  # noqa: BLE001
                broken.append(f"{dotted}: {type(exc).__name__}: {exc}")
        self.assertEqual(broken, [], f"unresolvable {label}:\n" + "\n".join(broken))

    def test_doc_event_paths(self):
        paths = []
        for _doctype, events in (frappe.get_hooks(app_name=APP).get("doc_events") or {}).items():
            for handlers in events.values():
                paths.extend(handlers if isinstance(handlers, list) else [handlers])
        self.assertTrue(paths, "expected doc_events to be registered")
        self._assert_resolvable(paths, "doc_events handlers")

    def test_scheduler_event_paths(self):
        paths = []
        for handlers in (frappe.get_hooks(app_name=APP).get("scheduler_events") or {}).values():
            if isinstance(handlers, dict):
                for group in handlers.values():
                    paths.extend(group)
            else:
                paths.extend(handlers)
        # The app intentionally registers no scheduler events; assert-and-verify.
        self._assert_resolvable(paths, "scheduler handlers")

    def test_patch_paths(self):
        patches = (APP_ROOT / "patches.txt").read_text().splitlines()
        dotted = [
            line.strip()
            for line in patches
            if line.strip() and not line.strip().startswith(("#", "["))
        ]
        self._assert_resolvable([f"{p}.execute" for p in dotted], "patches")


class TestNoIndependentAuthRemains(unittest.TestCase):
    """yob_storefront must contain no password/OTP/session implementation."""

    FORBIDDEN = (
        "login_manager",
        "LoginManager",
        "frappe.set_user",
        "check_password",
        "get_user_from_token",
        "require_customer",
        "require_login",
    )

    #: The ONE approved use of `frappe.set_user` in this app: the trusted
    #: execution boundary for the PUBLIC /payment/<token> flow.
    #:
    #: It is not authentication and creates no session. Guest holds no roles and
    #: gains none; after the token has been resolved to one exact Payment
    #: Request and every source/financial/state/eligibility check has passed,
    #: the internal Cart -> Sales Order work runs briefly as the disabled
    #: `YOB Payment Processor` identity, because ERPNext permission-checks
    #: documents YOB never constructs (get_item_details' cached Item, the tax
    #: Account) against the execution user, and Frappe 16.30.0 offers no
    #: request-local bypass context.
    #:
    #: Exempting the file rather than dropping the rule: every OTHER module must
    #: still be unable to switch users, which is what this guard exists for.
    SET_USER_EXEMPT = {"services/payment_request_service.py"}

    def test_no_forbidden_auth_primitives(self):
        offenders = []
        for path in _scannable_files():
            if path.suffix != ".py":
                continue
            relative = path.relative_to(APP_ROOT).as_posix()
            text = path.read_text(encoding="utf-8", errors="ignore")
            for needle in self.FORBIDDEN:
                if needle not in text:
                    continue
                if (needle == "frappe.set_user"
                        and relative in self.SET_USER_EXEMPT):
                    continue
                offenders.append(f"{relative}: {needle}")
        self.assertEqual(offenders, [], "legacy auth remains:\n" + "\n".join(offenders))

    def test_set_user_exemption_is_exactly_one_file(self):
        """The exemption must not quietly spread to other modules."""

        self.assertEqual(
            self.SET_USER_EXEMPT, {"services/payment_request_service.py"})

    def test_no_auth_context_setdefault(self):
        """Caller-supplied auth_context must never be honoured."""
        for path in _scannable_files():
            text = path.read_text(encoding="utf-8", errors="ignore")
            self.assertNotIn('setdefault("auth_context"', text, str(path))
            self.assertNotIn("setdefault('auth_context'", text, str(path))


class TestEndpointProtection(unittest.TestCase):
    """Static audit: every whitelisted endpoint has a deliberate auth posture."""

    # Guest endpoints authorized by checkout token / Razorpay HMAC instead of a session.
    ALLOWED_GUEST = {
        "get_checkout_data",
        "process_payment",
        "verify_payment",
    }
    # Desk-internal endpoint authorized by standard Frappe DocType permissions.
    #: Desk-only endpoints. They serve the Frappe Desk (link queries, tree
    #: loaders) rather than the storefront, so they carry an explicit
    #: `frappe.has_permission` check instead of storefront application access.
    #: They live under `yob_storefront/desk/`, deliberately outside `api/`.
    DESK_PERMISSION_GUARDED = {
        "get_contact_for_customer",
        "filters_in_set",
        "get_children",
        "add_node",
    }

    def _endpoints(self):
        for path in _scannable_files():
            if path.suffix != ".py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                decorators = [ast.unparse(d) for d in node.decorator_list]
                if not any("whitelist" in d for d in decorators):
                    continue
                yield path, node, decorators

    def test_every_endpoint_is_protected(self):
        unprotected = []
        for path, node, decorators in self._endpoints():
            whitelist = next(d for d in decorators if "whitelist" in d)
            is_guest = "allow_guest=True" in whitelist
            has_app = any("require_application" in d for d in decorators)

            if has_app:
                self.assertFalse(
                    is_guest,
                    f"{node.name} is both allow_guest and require_application",
                )
                continue
            if node.name in self.ALLOWED_GUEST and is_guest:
                continue
            if node.name in self.DESK_PERMISSION_GUARDED:
                source = path.read_text(encoding="utf-8")
                self.assertIn("frappe.has_permission", source, node.name)
                continue
            unprotected.append(f"{path.relative_to(APP_ROOT)}::{node.name}")

        self.assertEqual(
            unprotected, [], "endpoints without an auth boundary:\n" + "\n".join(unprotected)
        )

    def test_no_unexpected_guest_endpoints(self):
        guests = {
            node.name
            for _p, node, decs in self._endpoints()
            if "allow_guest=True" in next(d for d in decs if "whitelist" in d)
        }
        self.assertEqual(
            guests,
            self.ALLOWED_GUEST,
            "the set of guest-accessible endpoints changed unexpectedly",
        )

    def test_customer_endpoints_accept_auth_context(self):
        """require_application injects auth_context; the function must accept it."""
        bad = []
        for path, node, decorators in self._endpoints():
            if not any("require_application" in d for d in decorators):
                continue
            args = [a.arg for a in node.args.args]
            if "auth_context" not in args:
                bad.append(f"{path.relative_to(APP_ROOT)}::{node.name}")
        self.assertEqual(bad, [], "missing auth_context parameter:\n" + "\n".join(bad))
