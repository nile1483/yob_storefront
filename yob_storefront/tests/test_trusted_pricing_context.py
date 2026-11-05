# Copyright (c) 2026, YOB and Shayona
"""Trusted server-side pricing context, and the real-buyer layer (Phase 32H).

THE DEFECT THIS FILE EXISTS FOR
-------------------------------
ERPNext 16.52.1 added `validate_pricing_context()` to the **whitelisted**
`get_item_details()` entry point. It authorises what a CLIENT named -- the
transaction doctype/name, the party, the price list:

    has_permission("Sales Order")            <- the one that fired
    has_permission("Customer", "select")
    price-list side check

`SellingContext.item_details()` was calling that browser-facing API from trusted
server code, so a real storefront buyer -- who legitimately holds none of those
permissions -- got `PermissionError` on every product page, every variant
resolution, and every Add-to-Cart that hit the UOM merge guard.

ERPNext's own controllers never call the whitelisted wrapper for a context they
built themselves; they call `_get_item_details()`
(`accounts_controller.py:1300`, `transaction_base.py:299`, `item.py:1655`,
`bom.py` x3). Phase 32H moved YOB onto that same internal entry point, which is
the position YOB is actually in.

WHY THIS WAS INVISIBLE FOR 1482 TESTS
-------------------------------------
Every other storefront test calls `frappe.set_user("Administrator")` in setUp,
and Frappe short-circuits `has_permission` entirely for Administrator. The whole
suite was permission-blind on this path. A green suite and a 500 for every real
buyer coexisted happily.

`RealBuyerCompatibilityCase` is the standing answer: a small, explicit layer that
runs the high-risk public storefront flows as an actual Website User holding
`YOB Storefront Buyer`. It is NOT a rewrite of the suite -- it is the one place
that would fail if a future ERPNext release adds another gate Administrator
would silently walk through.

THE SECOND DEFECT, FOUND BY THE SAME AUDIT
------------------------------------------
Inventorying every whitelisted ERPNext function YOB calls server-side turned up
an identical incompatibility one call away:

    cart.get_or_create_cart -> erpnext ...company.get_default_company_address
      -> has_permission("Company", "select", doc=company)   <- new in 16.52.1
      -> PermissionError

`get_or_create_cart` only resolves a company address when it is CREATING a cart,
so this reached exactly the buyers nobody tests: brand new ones, with no Cart
yet. Every seeded account already had one.

The fix is the same shape -- use the native helper ERPNext's own controllers use
(`frappe.contacts...address.get_company_address`, called from
`accounts/party.py:259`, `sales_order.py:1293/1518`, `delivery_note.py:1037`) --
and `CompanyAddressParityCase` proves it preserves the selection semantics
rather than approximating them.
"""

import ast
import inspect
import unittest
from pathlib import Path

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from yob_auth.security.access import resolve_access
from yob_storefront.permissions.storefront_role import (
    STOREFRONT_BUYER_ROLE,
    ensure_role_and_permissions,
    sync_storefront_role,
)
from yob_storefront.services.pricing_context import context_for
from yob_storefront.utils.context import STOREFRONT_APP, get_storefront_customer

ITEM = "YOB-BOLT-M10"
CUSTOMER = "YOB Demo Buyer"
BUYER = "storefront@yob.test"
CONTACT = "Demo Buyer-YOB Demo Buyer"
BILLING = "YOB Demo Billing-Billing"
SHIPPING = "YOB Demo Shipping-Shipping"
COLOUR = "Colour"
SIZE = "Size"

APP_ROOT = Path(frappe.get_app_path("yob_storefront"))

#: Fields YOB actually consumes out of ERPNext's item details.
#:
#: The entry-point swap must change AUTHORISATION only. Anything here that
#: differed between the two entry points would mean pricing semantics moved too,
#: which is the one thing this change must not do.
CONSUMED_FIELDS = (
    "warehouse", "uom", "stock_uom", "conversion_factor", "price_list_rate",
    "item_code", "item_name", "item_group", "is_stock_item",
    "currency", "price_list_currency", "discount_percentage", "discount_amount",
)


def _request(host="storefront.test"):
    frappe.local.request = Request(
        EnvironBuilder(headers={"X-YOB-Original-Host": host}).get_environ()
    )


def _seeded() -> bool:
    return bool(frappe.db.exists("Customer", CUSTOMER) and frappe.db.exists("Item", ITEM))


def _production_files():
    """Every executable production file in the app -- tests and fixtures excluded."""

    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = path.relative_to(APP_ROOT).as_posix()
        if rel.startswith(("tests/", "fixtures/")) or "__pycache__" in rel:
            continue
        yield rel, path


#: Dependency modules whose functions are inventoried.
#:
#: `erpnext` wholesale, plus Frappe's Address module specifically -- Phase 32H
#: moved a call onto `frappe.contacts...address`, and a dependency that is not
#: scanned is a dependency nobody reviews. Frappe at large is deliberately NOT
#: scanned: `frappe.*` is the framework this app is written in, and inventorying
#: `frappe.utils` would be noise rather than review.
SCANNED_MODULE_PREFIXES = ("erpnext", "frappe.contacts")


def _dependency_imports():
    """{(module, name)} -> [files], parsed with `ast` rather than grepped.

    A regex misses a parenthesised multi-line `from ... import (a, b)`, which is
    exactly how one of these call sites is written.
    """

    found = {}
    for rel, path in _production_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            module = node.module or "" if isinstance(node, ast.ImportFrom) else ""
            if module.startswith(SCANNED_MODULE_PREFIXES):
                for alias in node.names:
                    found.setdefault((module, alias.name), set()).add(rel)
    return found


# =========================================================
# 1. THE INPUTS YOB CONTROLS INSTEAD
# =========================================================

class TrustedContextInputsCase(unittest.TestCase):
    """The three things `validate_pricing_context` checks, answered by YOB.

    Skipping a check is only defensible if the thing it guards cannot be
    attacker-chosen in the first place. These are those proofs; they are the
    reason the entry-point swap is a compatibility fix and not a bypass.
    """

    #: Pricing-context inputs no client may name, under any circumstances.
    #:
    #: Deliberately NOT `customer`, `company` or `name`:
    #:
    #: * `customer`/`company` survive on two endpoints as FRONTEND COMPATIBILITY
    #:   parameters. `payment_method.get_payment_methods` runs
    #:   `assert_customer_matches(auth_context, customer)` and then overwrites the
    #:   value with the authenticated Customer, so a mismatch is a 403 and a match
    #:   is redundant. `address.get_contact_for_customer` is an internal DESK API
    #:   gated by ordinary DocType permissions, which a storefront buyer fails.
    #:   `test_a_client_supplied_customer_is_never_authority` pins both.
    #: * `name` is the docname of an Address or Contact whose ownership is checked
    #:   server-side. It is not a TRANSACTION name -- YOB never sends one of those,
    #:   which `test_no_transaction_name_is_ever_supplied` asserts separately.
    #:
    #: What remains is the set with no legitimate client use at all.
    FORBIDDEN_PARAMS = ("doctype", "price_list", "selling_price_list",
                        "buying_price_list", "rate", "price_list_rate",
                        "warehouse", "set_warehouse", "conversion_factor",
                        "ignore_pricing_rule", "company_address")

    def _endpoints(self):
        """Every whitelisted storefront API function, with its signature."""

        import importlib
        import pkgutil

        import yob_storefront.api as api_pkg

        for info in pkgutil.iter_modules(api_pkg.__path__):
            module = importlib.import_module(f"yob_storefront.api.{info.name}")
            for name, fn in vars(module).items():
                if not callable(fn) or name.startswith("_"):
                    continue
                target = inspect.unwrap(fn)
                if getattr(target, "__module__", "") != module.__name__:
                    continue
                if not getattr(fn, "__wrapped__", None) and not hasattr(fn, "__wrapped__"):
                    # Only whitelisted/decorated endpoints are a client surface.
                    continue
                try:
                    yield f"{info.name}.{name}", inspect.signature(target), target
                except (TypeError, ValueError):  # pragma: no cover
                    continue

    def test_no_storefront_endpoint_accepts_a_pricing_context_input(self):
        """A client has no parameter through which to name any of the three."""

        offenders = []
        for label, signature, _fn in self._endpoints():
            for param in signature.parameters:
                if param in self.FORBIDDEN_PARAMS:
                    offenders.append(f"{label}({param})")

        self.assertEqual(offenders, [],
                         "a storefront endpoint accepts a trusted pricing input:\n"
                         + "\n".join(offenders))

    def test_the_transaction_doctype_is_a_server_side_constant(self):
        """`Sales Order` is written in YOB's own source, never received."""

        source = inspect.getsource(context_for(frappe.get_doc("Customer", CUSTOMER)).item_details)

        self.assertIn('"doctype": "Sales Order"', source,
                      "the transaction doctype is no longer a literal; if it became "
                      "an argument, a client could name any priceable doctype")

    def test_no_transaction_name_is_ever_supplied(self):
        """With no `name`, the gate's document-level branch had nothing to read.

        Worth pinning: YOB prices an item against a transaction TYPE, never
        against an existing document it might not own.
        """

        customer = frappe.get_doc("Customer", CUSTOMER)
        source = inspect.getsource(context_for(customer).item_details)

        self.assertNotIn('"name":', source)

    def test_the_customer_comes_from_the_server_side_grant(self):
        """Identity is resolved from the access row, not from the request."""

        _request()
        try:
            customer = get_storefront_customer(resolve_access(BUYER, STOREFRONT_APP))
        finally:
            frappe.local.request = None

        self.assertEqual(customer.name, CUSTOMER)
        self.assertEqual(
            frappe.db.get_value("YOB User Application Access",
                                {"user": BUYER, "application": STOREFRONT_APP},
                                "profile_name"),
            customer.name,
            "the Customer used for pricing is not the one the grant authorises")

    def test_a_client_supplied_customer_is_never_authority(self):
        """The two endpoints that still accept `customer`, and why that is safe.

        This is the justification for leaving `customer` out of the scan above.
        Each endpoint must be covered by ONE of the two documented mechanisms; a
        third endpoint appearing with neither is what this test exists to catch.
        """

        #: endpoint -> the mechanism that makes a client-supplied Customer inert.
        SANCTIONED = {
            "payment_method.get_payment_methods": "assert_customer_matches",
            "address.get_contact_for_customer": "frappe.has_permission",
        }

        # Only CLIENT surfaces count. An internal helper that takes a Customer
        # DOCUMENT as an ordinary argument from trusted server code
        # (`build_item_detail`, `check_address_owner`, `get_or_create_cart`) is
        # not a parameter a browser can set, and treating it as one made the
        # first version of this test fail on correct code.
        found = {
            label: inspect.getsource(fn)
            for label, signature, fn in self._endpoints()
            if "customer" in signature.parameters
        }

        self.assertEqual(sorted(found), sorted(SANCTIONED),
                         "an endpoint accepting `customer` appeared or vanished; "
                         "each needs a reviewed mechanism before it is excluded "
                         "from FORBIDDEN_PARAMS")

        for label, source in found.items():
            with self.subTest(endpoint=label):
                self.assertIn(SANCTIONED[label], source,
                              f"{label} accepts `customer` without {SANCTIONED[label]}")

    def test_the_company_is_resolved_from_store_settings(self):
        """Cart creation takes its Company from configuration, not the request.

        `get_or_create_cart` accepts a Customer DOCUMENT and nothing else, so
        there is no parameter through which a Company could arrive.
        """

        from yob_storefront.api.cart import get_or_create_cart
        from yob_storefront.utils.store import get_store_settings

        signature = inspect.signature(get_or_create_cart)
        self.assertEqual(list(signature.parameters), ["customer"],
                         "get_or_create_cart grew a parameter; a Company or an "
                         "Address could now be supplied by a caller")

        source = inspect.getsource(get_or_create_cart)
        self.assertIn("settings.company", source)
        self.assertIn("get_store_settings()", source)

        context = context_for(frappe.get_doc("Customer", CUSTOMER))
        self.assertEqual(context.company, get_store_settings().company)

    def test_only_the_legacy_payment_endpoint_accepts_a_company(self):
        """`company` survives on ONE endpoint, and it is not a cart surface.

        Left out of FORBIDDEN_PARAMS for the same frontend-compatibility reason
        as `customer`; recorded here so a new endpoint accepting a Company is a
        decision rather than an accident.
        """

        accepting = sorted(label for label, signature, _fn in self._endpoints()
                           if "company" in signature.parameters)

        self.assertEqual(accepting, ["payment_method.get_payment_methods"],
                         "an endpoint accepting `company` appeared or vanished")

    def test_the_price_list_is_resolved_server_side(self):
        """Customer -> Customer Group -> Selling Settings. No client input."""

        from yob_storefront.services.pricing_service import get_price_list_for_customer

        customer = frappe.get_doc("Customer", CUSTOMER)
        context = context_for(customer)

        self.assertEqual(context.price_list, get_price_list_for_customer(customer))
        self.assertTrue(
            frappe.get_cached_value("Price List", context.price_list, "selling"),
            "the resolved price list is not a selling list -- the check ERPNext's "
            "gate would have made")


# =========================================================
# 2. THE SWAP CHANGED AUTHORISATION ONLY
# =========================================================

class EntryPointParityCase(unittest.TestCase):
    """Public and internal entry points, compared where BOTH are permitted.

    Run as Administrator deliberately: the public wrapper refuses a buyer, so a
    buyer cannot produce the comparison at all. Administrator is the only
    identity that can answer "do these two differ in anything but the gate?"
    """

    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data on the test site")

    def setUp(self):
        frappe.set_user("Administrator")

    def _args(self, item_code):
        customer = frappe.get_doc("Customer", CUSTOMER)
        context = context_for(customer)
        return frappe._dict({
            "item_code": item_code, "customer": context.customer,
            "company": context.company, "currency": context.currency,
            "price_list": context.price_list,
            "transaction_date": context.transaction_date,
            "qty": 1, "doctype": "Sales Order", "conversion_rate": 1,
            "plc_conversion_rate": 1, "price_list_currency": context.currency,
            "ignore_pricing_rule": 1,
        })

    def test_both_entry_points_agree_on_every_field_yob_consumes(self):
        from erpnext.stock.get_item_details import _get_item_details, get_item_details

        public = get_item_details(self._args(ITEM), doc=None, for_validate=True)
        internal = _get_item_details(self._args(ITEM), doc=None, for_validate=True)

        for field in CONSUMED_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(public.get(field), internal.get(field),
                                 f"{field} changed with the entry point -- the swap "
                                 "moved pricing semantics, not just authorisation")

    def test_the_two_results_are_wholly_identical(self):
        """Stronger than the field list, and true on this ERPNext.

        If a future release makes the wrapper add its own keys this will fail
        while the test above still passes, which is the signal to narrow the
        claim to CONSUMED_FIELDS rather than to assume nothing changed.
        """

        from erpnext.stock.get_item_details import _get_item_details, get_item_details

        public = dict(get_item_details(self._args(ITEM), doc=None, for_validate=True))
        internal = dict(_get_item_details(self._args(ITEM), doc=None, for_validate=True))

        differing = sorted(k for k in set(public) | set(internal)
                           if public.get(k) != internal.get(k))

        self.assertEqual(differing, [],
                         f"wrapper-only differences appeared: {differing}")

    def test_yob_uses_the_internal_entry_point(self):
        """The production call site, asserted rather than assumed.

        `_get_item_details` is an UNDERSCORE API. That is a deliberate, recorded
        risk: ERPNext calls it from seven of its own places so it is stable in
        practice, but it is not a declared public contract. If an upgrade renames
        or removes it, this test and the real-buyer tests below fail loudly and
        immediately -- which is the mitigation. No compatibility shim exists, by
        decision: a shim would turn a clear failure into a silent fallback.
        """

        from yob_storefront.services import pricing_context

        source = inspect.getsource(pricing_context.SellingContext.item_details)

        self.assertIn("import _get_item_details", source)
        self.assertNotIn("import get_item_details", source,
                         "the whitelisted browser entry point is back in trusted code")

    def test_the_internal_entry_point_still_exists(self):
        """A named, early failure if ERPNext removes the private function."""

        from erpnext.stock import get_item_details as module

        self.assertTrue(callable(getattr(module, "_get_item_details", None)),
                        "ERPNext no longer exposes _get_item_details; Phase 32H's "
                        "entry-point choice needs revisiting")


# =========================================================
# 2b. THE COMPANY-ADDRESS SWAP CHANGED AUTHORISATION ONLY
# =========================================================

class CompanyAddressParityCase(unittest.TestCase):
    """The replacement resolver must SELECT the same address, not just any one.

    ERPNext's whitelisted `get_default_company_address` takes
    `max(linked non-disabled addresses, key=is_primary_address)`. Frappe's
    native `get_company_address` orders by `is_primary_address DESC` and takes
    the first. Those agree, but "they look similar" is not evidence -- each case
    below is constructed and compared.

    Run as Administrator because the old wrapper refuses anyone else, which is
    the whole reason it had to be replaced.
    """

    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data on the test site")

    def setUp(self):
        frappe.set_user("Administrator")
        self.company = frappe.get_single("YOB Store Settings").company

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()

    def address(self, title, primary, disabled=0, company=None):
        return frappe.get_doc({
            "doctype": "Address", "address_title": title, "address_type": "Billing",
            "address_line1": "1 Street", "city": "Ahmedabad", "state": "Gujarat",
            "pincode": "380001", "country": "India",
            "is_primary_address": primary, "disabled": disabled,
            "links": [{"link_doctype": "Company", "link_name": company or self.company}],
        }).insert(ignore_permissions=True).name

    def both(self):
        """(whitelisted answer, native answer) for the store's company."""

        from erpnext.setup.doctype.company.company import get_default_company_address
        from frappe.contacts.doctype.address.address import get_company_address

        frappe.clear_cache()
        return (get_default_company_address(self.company),
                get_company_address(self.company).company_address)

    def test_no_address_answers_none_on_both(self):
        frappe.db.delete("Dynamic Link", {"link_doctype": "Company",
                                          "link_name": self.company})
        legacy, native = self.both()

        self.assertIsNone(native)
        self.assertEqual(legacy, native)

    def test_a_single_address_is_chosen_by_both(self):
        name = self.address("_32H Only", primary=0)
        legacy, native = self.both()

        self.assertEqual(native, name)
        self.assertEqual(legacy, native)

    def test_a_primary_address_wins_however_late_it_was_created(self):
        """The precedence that matters: order of creation must not decide it."""

        self.address("_32H NonPrimary First", primary=0)
        primary = self.address("_32H Primary Second", primary=1)
        legacy, native = self.both()

        self.assertEqual(native, primary, "the non-primary address was chosen")
        self.assertEqual(legacy, native)

    def test_a_primary_address_wins_however_early_it_was_created(self):
        primary = self.address("_32H Primary First", primary=1)
        self.address("_32H NonPrimary Second", primary=0)
        legacy, native = self.both()

        self.assertEqual(native, primary)
        self.assertEqual(legacy, native)

    def test_a_non_primary_address_is_still_returned_when_none_is_primary(self):
        """NOT None. Losing this would silently blank the Cart's company address."""

        name = self.address("_32H NoneArePrimary", primary=0)
        legacy, native = self.both()

        self.assertEqual(native, name)
        self.assertEqual(legacy, native)

    def test_a_disabled_address_is_excluded_even_when_primary(self):
        active = self.address("_32H Active", primary=0)
        self.address("_32H DisabledPrimary", primary=1, disabled=1)
        legacy, native = self.both()

        self.assertEqual(native, active, "a disabled address was selected")
        self.assertEqual(legacy, native)

    def test_an_address_linked_to_something_else_is_never_selected(self):
        """Scoped to the Company link, so no stray Address can be redirected in."""

        frappe.db.delete("Dynamic Link", {"link_doctype": "Company",
                                          "link_name": self.company})
        frappe.get_doc({
            "doctype": "Address", "address_title": "_32H CustomerOnly",
            "address_type": "Billing", "address_line1": "9 Elsewhere",
            "city": "Ahmedabad", "state": "Gujarat", "pincode": "380001",
            "country": "India", "is_primary_address": 1,
            "links": [{"link_doctype": "Customer", "link_name": CUSTOMER}],
        }).insert(ignore_permissions=True)

        legacy, native = self.both()

        self.assertIsNone(native, "an address linked to a Customer was returned "
                                  "as the company address")
        self.assertEqual(legacy, native)

    def test_production_uses_the_native_resolver(self):
        """The call site itself, and the absence of the whitelisted one."""

        from yob_storefront.api import cart

        source = inspect.getsource(cart.get_or_create_cart)

        self.assertIn("get_company_address", source)
        self.assertNotIn("get_default_company_address(", source,
                         "the whitelisted ERPNext company helper is back")


# =========================================================
# 3. THE REAL-BUYER LAYER  (the standing regression)
# =========================================================

class RealBuyerCompatibilityCase(unittest.TestCase):
    """High-risk public flows, executed as a genuine Website User.

    Administrator bypasses `has_permission` outright, so no Administrator test
    can ever prove a buyer-facing permission gate is clear. This class is the
    only place in the suite that can, and it is deliberately small: the six
    flows a storefront cannot function without.

    Fixtures are built as Administrator -- creating catalogue data is not a thing
    a buyer may do, and pretending otherwise would test the wrong boundary -- and
    every ASSERTION runs as the buyer.
    """

    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data on the test site")
        ensure_role_and_permissions()
        sync_storefront_role(BUYER)
        frappe.db.commit()
        frappe.clear_cache()

    def setUp(self):
        frappe.set_user("Administrator")
        self.commits = []
        from unittest.mock import patch
        commit_patch = patch.object(
            frappe.db, "commit", side_effect=lambda *a, **k: self.commits.append(1))
        commit_patch.start()
        self.addCleanup(commit_patch.stop)

        self.customer = frappe.get_doc("Customer", CUSTOMER)
        self.item_group = frappe.db.get_value("Item", ITEM, "item_group")
        self.stock_uom = frappe.db.get_value("Item", ITEM, "stock_uom")
        self.hsn = frappe.db.get_value("Item", ITEM, "gst_hsn_code")
        self.price_list = frappe.get_single("Selling Settings").selling_price_list

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.local.request = None
        frappe.db.rollback()
        frappe.clear_cache()
        self.assertEqual(self.commits, [], "a commit escaped the test rollback")

    # ------------------------------------------------------------- as a buyer

    def as_buyer(self):
        _request()
        frappe.set_user(BUYER)

    def assert_still_unprivileged(self):
        """The other half of every assertion here.

        A flow that works because the buyer quietly acquired Sales Order or
        Customer access is not fixed, it is broken differently.
        """

        self.assertFalse(frappe.has_permission("Sales Order", "read"),
                         "the buyer gained Sales Order read")
        self.assertFalse(frappe.has_permission("Customer", "read"),
                         "the buyer gained Customer read")
        self.assertFalse(frappe.has_permission("Customer", "select"),
                         "the buyer gained Customer select")
        self.assertFalse(frappe.has_permission("Company", "select"),
                         "the buyer gained Company select")
        self.assertFalse(frappe.has_permission("Company", "read"),
                         "the buyer gained Company read")
        self.assertIn(STOREFRONT_BUYER_ROLE, frappe.get_roles())
        self.assertEqual(
            frappe.db.get_value("User", BUYER, "user_type"), "Website User",
            "the buyer became a System User")

    # ------------------------------------------------------------- fixtures

    def make_item(self, code, price=100, **kw):
        doc = {"doctype": "Item", "item_code": code, "item_name": code,
               "item_group": self.item_group, "stock_uom": self.stock_uom,
               "is_stock_item": 1, "is_sales_item": 1, "gst_hsn_code": self.hsn,
               "custom_slug": code.lower()}
        doc.update(kw)
        frappe.get_doc(doc).insert(ignore_permissions=True)
        if price is not None:
            self.make_price(code, price)
        return code

    def make_price(self, item, rate):
        return frappe.get_doc({
            "doctype": "Item Price", "item_code": item, "price_list": self.price_list,
            "price_list_rate": rate, "selling": 1,
            "uom": self.stock_uom}).insert(ignore_permissions=True).name

    def attribute_value(self, attribute, wanted):
        values = frappe.get_all("Item Attribute Value", filters={"parent": attribute},
                                pluck="attribute_value")
        if not values:
            raise unittest.SkipTest(f"{attribute} has no values on this bench")
        return wanted if wanted in values else values[0]

    def make_family(self, code="_32H-TEE"):
        from erpnext.controllers.item_variant import create_variant

        colour = self.attribute_value(COLOUR, "Red")
        size = self.attribute_value(SIZE, "Medium")

        template = frappe.get_doc({
            "doctype": "Item", "item_code": code, "item_name": code,
            "item_group": self.item_group, "stock_uom": self.stock_uom,
            "is_stock_item": 1, "is_sales_item": 1, "gst_hsn_code": self.hsn,
            "custom_slug": code.lower(), "has_variants": 1,
            "attributes": [{"attribute": COLOUR}, {"attribute": SIZE}],
        }).insert(ignore_permissions=True)

        variant = create_variant(template.name, {COLOUR: colour, SIZE: size})
        variant.insert(ignore_permissions=True)
        self.make_price(variant.name, 250)
        return template, variant, {COLOUR: colour, SIZE: size}

    def catalog_api(self, name, **kwargs):
        from unittest.mock import patch

        from yob_storefront.api import catalog

        with patch.object(catalog, "get_storefront_customer", return_value=self.customer):
            return inspect.unwrap(getattr(catalog, name))(auth_context={}, **kwargs)

    def cart_api(self, name, **kwargs):
        from unittest.mock import patch

        from yob_storefront.api import cart

        with patch.object(cart, "get_storefront_customer", return_value=self.customer):
            return inspect.unwrap(getattr(cart, name))(auth_context={}, **kwargs)

    # ------------------------------------------------------- the pricing context

    def test_a_buyer_can_resolve_the_pricing_context(self):
        """THE regression. This raised PermissionError before Phase 32H."""

        self.as_buyer()

        details = context_for(self.customer).item_details(ITEM)

        self.assertIsNotNone(details, "ERPNext refused to describe the item")
        self.assertTrue(details.get("item_code"))
        self.assert_still_unprivileged()

    def test_a_buyer_can_resolve_the_warehouse(self):
        self.as_buyer()

        warehouse = context_for(self.customer).resolved_warehouse(ITEM)

        self.assertTrue(warehouse, "no warehouse resolved for a real buyer")
        self.assertEqual(
            warehouse,
            frappe.db.get_value("Warehouse", warehouse, "name"),
            "the resolved warehouse is not a real Warehouse")
        self.assert_still_unprivileged()

    def test_the_buyer_resolves_the_same_warehouse_as_administrator(self):
        """Identity must not change the answer, only whether it is permitted."""

        as_admin = context_for(self.customer).resolved_warehouse(ITEM)

        self.as_buyer()
        as_buyer = context_for(self.customer).resolved_warehouse(ITEM)

        self.assertEqual(as_buyer, as_admin)

    def test_a_buyer_can_resolve_the_selling_uom(self):
        """The Add-to-Cart merge guard's dependency."""

        self.as_buyer()

        uom = context_for(self.customer).resolved_selling_uom(ITEM)

        self.assertTrue(uom)
        self.assert_still_unprivileged()

    # ------------------------------------------------------------ public flows

    def test_a_buyer_can_load_the_catalogue_listing(self):
        self.as_buyer()

        response = self.catalog_api("get_items", scope_type="category", page_size=3)

        self.assertNotIn("errors", response, f"listing failed: {response}")
        self.assert_still_unprivileged()

    def test_a_buyer_can_load_a_product_detail_page(self):
        """The exact flow that answered PermissionError in manual acceptance."""

        item = self.make_item("_32H-DETAIL")
        self.as_buyer()

        response = self.catalog_api("get_item", slug="_32h-detail")

        self.assertNotIn("errors", response, f"product detail failed: {response}")
        detail = response.get("data", response)
        self.assertEqual(detail["name"], item)
        self.assertEqual(detail["rate"], 100)
        self.assertTrue(detail["warehouse"], "the product page lost its warehouse")
        self.assert_still_unprivileged()

    def test_a_buyer_can_resolve_a_variant(self):
        import json

        template, variant, selection = self.make_family()
        self.as_buyer()

        response = self.catalog_api("resolve_variant", template=template.name,
                                    attributes=json.dumps(selection), qty=1)

        self.assertNotIn("errors", response, f"variant resolution failed: {response}")
        self.assertEqual(response.get("data", response)["name"], variant.name)
        self.assert_still_unprivileged()

    def test_a_buyer_can_load_the_cart(self):
        self.as_buyer()

        response = self.cart_api("get_cart")

        self.assertNotIn("errors", response, f"cart load failed: {response}")
        self.assert_still_unprivileged()

    def test_a_buyer_can_add_to_cart_and_then_merge_the_same_item(self):
        """The second add is the one that exercises the UOM merge guard.

        `add_to_cart` consults `resolved_selling_uom` ONLY when a line for that
        SKU already exists, so a single add would never have reached the broken
        call. Two adds is the whole point of this test.
        """

        item = self.make_item("_32H-MERGE")
        self.as_buyer()

        first = self.cart_api("add_to_cart", item_code=item, qty=2)
        self.assertNotIn("errors", first, f"first add failed: {first}")

        second = self.cart_api("add_to_cart", item_code=item, qty=3)
        self.assertNotIn("errors", second,
                         f"the UOM merge guard failed for a real buyer: {second}")

        cart = self.cart_api("get_cart").get("data", {}).get("cart", {})
        rows = [r for r in (cart.get("items") or []) if r.get("item_code") == item]
        self.assertEqual(len(rows), 1, "the merge created a second row")
        # The Cart projection names this `quantity`; `qty` is the Sales Order row.
        self.assertEqual(rows[0]["quantity"], 5, "the quantities did not merge")
        self.assertEqual(cart["total_quantity"], 5)
        self.assert_still_unprivileged()

    # ------------------------------------------------- first-time cart (32H)

    def no_cart_yet(self):
        """Make this buyer a first-time buyer, inside the test transaction.

        Parking the Draft cart rather than deleting it: a delete would cascade
        through Cart Items and tell us less about the real first-run path, which
        is simply "no Draft cart matches this customer".
        """

        for name in frappe.get_all("Cart", filters={"customer": CUSTOMER,
                                                    "status": "Draft"}, pluck="name"):
            frappe.db.set_value("Cart", name, "status", "Ordered")
        frappe.clear_cache()

    def test_a_first_time_buyer_can_create_a_cart(self):
        """THE second regression. This raised PermissionError before Phase 32H.

        Reached only when no Draft cart exists, which is why every seeded
        account hid it.
        """

        from yob_storefront.api.cart import get_or_create_cart

        self.no_cart_yet()
        self.as_buyer()

        cart = get_or_create_cart(self.customer)

        self.assertTrue(cart.name)
        self.assertEqual(cart.customer, CUSTOMER)
        self.assertEqual(cart.status, "Draft")
        self.assertEqual(cart.user, BUYER, "the cart was not attributed to the buyer")
        self.assert_still_unprivileged()

    def test_a_first_time_cart_uses_the_server_selected_company(self):
        """Company and currency come from YOB Store Settings, not the request."""

        from yob_storefront.api.cart import get_or_create_cart
        from yob_storefront.utils.store import get_store_settings

        settings = get_store_settings()
        self.no_cart_yet()
        self.as_buyer()

        cart = get_or_create_cart(self.customer)

        self.assertEqual(cart.company, settings.company)
        self.assertEqual(cart.currency, settings.default_currency)
        self.assert_still_unprivileged()

    def test_a_first_time_cart_gets_the_companys_default_address(self):
        """And it is the PRIMARY one, resolved without any Company permission."""

        from yob_storefront.api.cart import get_or_create_cart
        from yob_storefront.utils.store import get_store_settings

        company = get_store_settings().company
        frappe.db.delete("Dynamic Link", {"link_doctype": "Company",
                                          "link_name": company})

        def address(title, primary):
            return frappe.get_doc({
                "doctype": "Address", "address_title": title,
                "address_type": "Billing", "address_line1": "1 Street",
                "city": "Ahmedabad", "state": "Gujarat", "pincode": "380001",
                "country": "India", "is_primary_address": primary,
                "links": [{"link_doctype": "Company", "link_name": company}],
            }).insert(ignore_permissions=True).name

        address("_32H Cart NonPrimary", 0)
        primary = address("_32H Cart Primary", 1)

        self.no_cart_yet()
        self.as_buyer()

        cart = get_or_create_cart(self.customer)

        self.assertEqual(cart.company_address, primary,
                         "the first-time cart did not take the primary company "
                         "address")
        self.assert_still_unprivileged()

    def test_a_first_time_buyer_can_add_to_cart_through_the_api(self):
        """The real public path, not just the helper."""

        item = self.make_item("_32H-FIRSTADD")
        self.no_cart_yet()
        self.as_buyer()

        response = self.cart_api("add_to_cart", item_code=item, qty=2)

        self.assertNotIn("errors", response, f"first add failed: {response}")
        rows = [r for r in response["data"]["items"] if r["item_code"] == item]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["quantity"], 2)
        self.assert_still_unprivileged()

    def test_a_first_time_buyer_can_load_the_cart_through_the_api(self):
        self.no_cart_yet()
        self.as_buyer()

        response = self.cart_api("get_cart")

        self.assertNotIn("errors", response, f"first cart load failed: {response}")
        self.assertEqual(response["data"]["cart"]["customer"], CUSTOMER)
        self.assert_still_unprivileged()

    def test_an_existing_draft_cart_is_reused_not_replaced(self):
        """Phase 32H must change nothing for a buyer who already has a cart."""

        from yob_storefront.api.cart import get_or_create_cart

        self.as_buyer()

        first = get_or_create_cart(self.customer)
        second = get_or_create_cart(self.customer)

        self.assertEqual(first.name, second.name, "a second cart was created")
        self.assertEqual(
            frappe.db.count("Cart", {"customer": CUSTOMER, "status": "Draft"}), 1,
            "more than one Draft cart exists for this customer")

    def test_reuse_does_not_re_resolve_the_company_address(self):
        """An existing cart keeps its stored address; only creation resolves one.

        Worth pinning: if reuse started resolving, a merchant address change
        would silently rewrite historical carts.
        """

        from yob_storefront.api.cart import get_or_create_cart

        self.as_buyer()
        cart = get_or_create_cart(self.customer)
        before = cart.company_address

        frappe.set_user("Administrator")
        frappe.db.set_value("Cart", cart.name, "company_address", None)
        frappe.clear_cache()
        self.as_buyer()

        again = get_or_create_cart(self.customer)

        self.assertIsNone(again.company_address,
                          "cart reuse re-resolved the company address")
        self.assertEqual(again.name, cart.name)
        del before

    def test_the_buyer_holds_no_desk_access(self):
        """Nothing in this phase may turn a buyer into a Desk user."""

        self.as_buyer()

        self.assertEqual(frappe.db.get_value("User", BUYER, "user_type"), "Website User")
        self.assertFalse(frappe.db.get_value("User", BUYER, "desk_theme") == "Desk"
                         and frappe.has_permission("DocType", "read"))
        self.assertFalse(frappe.has_permission("Sales Order", "read"))
        self.assertFalse(frappe.has_permission("Item", "write"))


# =========================================================
# 4. WHITELISTED-ERPNEXT DEPENDENCY INVENTORY AND GUARD
# =========================================================

class WhitelistedErpnextCallInventoryCase(unittest.TestCase):
    """Trusted server code calling a browser-facing ERPNext API is a PATTERN.

    Phase 32H fixed one instance. The guard below exists because the next
    instance should be a review decision rather than another production
    incident: ERPNext may add an authorisation gate to any whitelisted function
    in any release, and Administrator-only tests will not notice.

    Deliberately an INVENTORY, not a ban. Several of these are legitimate; the
    test only insists that a new ERPNext dependency in production code is
    written down with a verdict before it ships.
    """

    #: (module, imported name) -> verdict recorded in Phase 32H.
    #:
    #: `whitelisted` is whether ERPNext exposes it to browsers; `verdict` is
    #: APPROVED (server-controlled input, no gate, or a gate YOB satisfies),
    #: REPLACED (moved to a trusted internal equivalent) or MONITOR (whitelisted
    #: and could acquire a gate).
    INVENTORY = {
        ("erpnext.stock.get_item_details", "_get_item_details"):
            "REPLACED -- Phase 32H. Internal entry point, the one ERPNext's own "
            "controllers use. Replaced the whitelisted `get_item_details`, whose "
            "16.52.1 gate refused real buyers.",
        ("erpnext.stock.get_item_details", "get_bin_details"):
            "APPROVED -- whitelisted, and it DOES gate: "
            "`has_permission('Item', 'select')` at get_item_details.py:1715, also "
            "new in 16.52.1. The storefront buyer passes it because Phase 32B "
            "granted Item select, so api/catalog.py:1163 works -- verified as a "
            "real buyer, not assumed. Both arguments are server-derived: the "
            "warehouse is the one ERPNext itself resolved.",
        ("frappe.contacts.doctype.address.address", "get_company_address"):
            "REPLACED -- Phase 32H. Frappe's NON-whitelisted resolver, the one "
            "ERPNext's own controllers use (accounts/party.py:259, "
            "sales_order.py:1293/1518, delivery_note.py:1037). It replaced "
            "`erpnext...company.get_default_company_address`, which is "
            "whitelisted and since 16.52.1 gates on "
            "`has_permission('Company', 'select', doc=name)` -- a permission no "
            "storefront buyer holds, so a buyer with no Cart yet could not "
            "create one. Selection semantics proven equivalent by "
            "CompanyAddressParityCase; the company comes from YOB Store "
            "Settings, never from a request.",
        ("frappe.contacts.doctype.address.address", "get_address_display"):
            "APPROVED -- whitelisted, and it DOES gate: `render_address` runs "
            "`address.check_permission()` by default. Pre-existing, and the call "
            "site (services/order_service.py:52) is a LEGACY fallback that "
            "already catches frappe.PermissionError and answers None, so an "
            "unreadable address degrades one field instead of the order. Note "
            "`get_company_address` is unaffected: it renders with "
            "`check_permissions=False` (address.py:305).",
        ("erpnext.stock.doctype.item.item", "get_uom_conv_factor"):
            "APPROVED -- whitelisted, but it performs no permission check "
            "(item.py:1674). Called from services/target_achievement.py with UOMs "
            "read off stored documents, never from a request.",
        ("erpnext.stock.get_item_details", "get_price_list_rate_for"):
            "APPROVED -- not whitelisted. Stage-2 catalogue eligibility.",
        ("erpnext.stock.get_item_details", "get_item_price"):
            "APPROVED -- not whitelisted. Recovers which Item Price ERPNext ranked.",
        ("erpnext.accounts.party", "get_default_price_list"):
            "APPROVED -- not whitelisted. Price list resolution.",
        ("erpnext.controllers.item_variant", "find_variant"):
            "APPROVED -- not whitelisted. Attributes -> SKU resolution.",
        ("erpnext.accounts.doctype.pricing_rule.utils", "apply_pricing_rule_on_transaction"):
            "APPROVED -- not whitelisted. Document-level pricing rules.",
    }

    def test_every_erpnext_dependency_is_inventoried(self):
        """A new ERPNext import in production code must be reviewed.

        Failing here is not an error in the new code -- it is a prompt to decide
        whether the function is whitelisted, whether it validates anything, and
        whether YOB's input is server-controlled, then record the verdict above.
        """

        found = _dependency_imports()
        unlisted = {key: sorted(files) for key, files in found.items()
                    if key not in self.INVENTORY}

        self.assertEqual(
            unlisted, {},
            "new dependency in production code. Decide whether it is "
            "@frappe.whitelist(), whether it authorises client-supplied context, "
            "and whether YOB's input is server-derived -- then add it to "
            "WhitelistedErpnextCallInventoryCase.INVENTORY:\n"
            + "\n".join(f"  {m}.{n}  <- {files}" for (m, n), files in unlisted.items()))

    def test_the_inventory_names_no_dependency_that_is_gone(self):
        """A stale entry hides the fact that nothing reviews it any more."""

        found = _dependency_imports()
        stale = sorted(f"{m}.{n}" for (m, n) in self.INVENTORY if (m, n) not in found)

        self.assertEqual(stale, [],
                         "inventory entries no longer imported anywhere:\n"
                         + "\n".join(stale))

    #: Whitelisted ERPNext functions Phase 32H REMOVED from production code,
    #: each because it gates on a permission a storefront buyer does not hold.
    #: Returning either reintroduces a buyer-facing 500 on a first-run path.
    RETIRED = {
        ("erpnext.stock.get_item_details", "get_item_details"):
            "gates has_permission('Sales Order'); use _get_item_details",
        ("erpnext.setup.doctype.company.company", "get_default_company_address"):
            "gates has_permission('Company', 'select'); use "
            "frappe.contacts...address.get_company_address",
    }

    def test_no_retired_whitelisted_entry_point_came_back(self):
        """The two specific regressions, guarded by name.

        Narrow on purpose. Importing either wrapper is not wrong in general -- it
        is wrong in trusted server code that has already authorised its own
        context, which is all of this app.
        """

        found = _dependency_imports()
        offenders = {f"{m}.{n}": sorted(found[(m, n)])
                     for (m, n) in self.RETIRED if (m, n) in found}

        self.assertEqual(
            offenders, {},
            "a whitelisted ERPNext entry point retired by Phase 32H is back in "
            "production code:\n"
            + "\n".join(f"  {k} in {v} -- {self.RETIRED[tuple(k.rsplit('.', 1))]}"
                         for k, v in offenders.items()))

    def test_the_retired_entry_points_still_carry_the_gates_that_retired_them(self):
        """If upstream drops a gate, the replacement is no longer load-bearing.

        Not a failure to fix blindly -- a prompt to re-read the verdict. A gate
        disappearing would mean Phase 32H's reasoning no longer applies.
        """

        import inspect as _inspect

        from erpnext.setup.doctype.company import company as company_module
        from erpnext.stock import get_item_details as gid

        self.assertIn("validate_pricing_context",
                      _inspect.getsource(gid.get_item_details),
                      "get_item_details no longer validates client context")
        self.assertIn('has_permission("Company", ptype="select"',
                      _inspect.getsource(company_module.get_default_company_address),
                      "get_default_company_address no longer gates on Company select")

    def test_the_gates_on_the_inventoried_functions_are_unchanged(self):
        """Pin which whitelisted dependencies currently gate, and on what.

        The INVENTORY verdicts above are only valid while these hold. A gate
        appearing on a function recorded as ungated, or an existing gate changing
        DocType, is exactly the event that produced Phase 32H -- so it should
        break a test rather than a buyer.
        """

        import inspect as _inspect

        from erpnext.setup.doctype.company import company as company_module
        from erpnext.stock import get_item_details as gid
        from erpnext.stock.doctype.item import item as item_module

        expected = {
            "get_bin_details":
                (gid.get_bin_details, 'has_permission("Item", ptype="select"'),
            "get_default_company_address":
                (company_module.get_default_company_address,
                 'has_permission("Company", ptype="select"'),
        }
        for name, (fn, needle) in expected.items():
            with self.subTest(function=name):
                self.assertIn(needle, _inspect.getsource(fn),
                              f"{name}'s authorisation gate changed; re-check its "
                              "INVENTORY verdict")

        self.assertNotIn(
            "has_permission", _inspect.getsource(item_module.get_uom_conv_factor),
            "get_uom_conv_factor acquired a permission gate; it is called from "
            "target_achievement.py and its verdict needs re-checking")

    def test_a_buyer_keeps_the_item_select_that_the_product_page_needs(self):
        """Phase 32B's `Item select` grant is load-bearing for `get_bin_details`.

        Recorded here because the dependency is invisible: removing that grant
        would break the product page's availability read through ERPNext's gate,
        not through any YOB code.
        """

        if not frappe.db.exists("User", BUYER):
            self.skipTest("buyer fixture absent")

        frappe.set_user(BUYER)
        try:
            self.assertTrue(frappe.has_permission("Item", "select"))
        finally:
            frappe.set_user("Administrator")

    def test_the_inventory_reaches_the_real_call_sites(self):
        """Guard the guard: a scanner that finds nothing would pass silently."""

        found = _dependency_imports()

        self.assertGreaterEqual(len(found), 5,
                                "the dependency import scan found almost nothing; it "
                                "is probably not scanning production source any more")
        self.assertIn(("frappe.contacts.doctype.address.address", "get_company_address"),
                      found,
                      "the Phase 32H company-address call site was not found by the "
                      "scan; check SCANNED_MODULE_PREFIXES")
        self.assertIn(("erpnext.stock.get_item_details", "_get_item_details"), found,
                      "the Phase 32H pricing call site was not found by the scan")


if __name__ == "__main__":
    unittest.main()
