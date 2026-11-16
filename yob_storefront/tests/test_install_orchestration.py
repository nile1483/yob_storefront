# Copyright (c) 2026, YOB and Shayona
"""`after_install()` and `after_migrate()` as whole functions.

THE DEFECT CLASS THIS CLOSES
----------------------------
Phase 32J was exactly this failure: `ensure_payment_processor_identity()`
existed, was correct, was called by `after_install()` -- and was the one
provisioning function missing from `after_migrate()`. Every individual helper
had coverage; nothing asserted that the ENTRY POINTS call them. So a site that
already existed never received the processor's tax-template grant, and Proceed
to Payment answered `PermissionError` for every real buyer on a GST-registered
company.

Two independent kinds of test close it, because either alone can be fooled:

* a STRUCTURAL guard reads what the entry points actually call, so a new
  `ensure_*` that nobody wired up fails immediately, before it can ship;
* a BEHAVIOURAL restore removes real artefacts, calls `after_migrate()`, and
  checks they come back -- which is what the structural guard cannot prove.

WHAT IS REMOVED, AND WHY NOT MORE
---------------------------------
Removals use `frappe.db.delete`, not `delete_doc`: it bypasses link validation
(the buyer Role has `Has Role` children, the gateway may have Payment Methods)
and stays inside the test transaction, so `tearDown` undoes all of it.

The one artefact NOT removed is the `YOB Customer Target` unique index. Dropping
an index is DDL, MariaDB commits implicitly around DDL, and that would end the
test transaction and leak every other change in it. `ensure_target_constraints`
is therefore covered by its real semantics instead -- the constraint is asserted
by column set, repeated provisioning is asserted to be a no-op, and the
first-install branch is exercised with `table_exists` patched.

Only custom fields with NO database column (the Customer Section Break and HTML
pair) are deleted and restored, for the same reason: recreating a `Data` or
`Link` field would run `ALTER TABLE`.
"""

import ast
import inspect
import textwrap
import unittest
from unittest.mock import patch

import frappe

from yob_storefront import install
from yob_storefront.install import (
    PAYMENT_PROCESSOR_ROLE,
    PAYMENT_PROCESSOR_USER,
    after_install,
    after_migrate,
    ensure_payment_gateways,
    ensure_target_constraints,
)
from yob_storefront.permissions.storefront_role import (
    MANAGED_READ_DOCTYPES,
    MANAGED_SELECT_DOCTYPES,
    STOREFRONT_BUYER_ROLE,
)

CUSTOMER_PANEL_FIELDS = ("custom_yob_storefront_users_section",
                         "custom_yob_storefront_users_html")

RAZORPAY = "Razorpay"

TARGET_DOCTYPE = "YOB Customer Target"
TARGET_INDEX_COLUMNS = ("customer", "target_rule")


def _docperm_roles(doctype):
    return set(frappe.get_all("Custom DocPerm", filters={"parent": doctype},
                              pluck="role"))


def _unique_indexes(table):
    """`{index_name: (col, col, ...)}` for every UNIQUE index on a table."""

    rows = frappe.db.sql("""
        select INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME
        from information_schema.STATISTICS
        where TABLE_SCHEMA = database() and TABLE_NAME = %s and NON_UNIQUE = 0
        order by INDEX_NAME, SEQ_IN_INDEX""", f"tab{table}", as_dict=True)

    indexes = {}
    for row in rows:
        indexes.setdefault(row.INDEX_NAME, []).append(row.COLUMN_NAME)
    return {name: tuple(columns) for name, columns in indexes.items()}


class EntryPointWiringCase(unittest.TestCase):
    """The structural guard: every provisioner must be reachable from a hook."""

    #: What `after_install()` must call. A fresh site has no tables for the
    #: Target DocType yet, so the constraint is deliberately not in this list --
    #: `ensure_target_constraints` returns early on a missing table and the
    #: next migrate applies it.
    EXPECTED_AFTER_INSTALL = {
        "_ensure_storefront_application",
        "ensure_role_and_permissions",
        "ensure_custom_fields",
        "ensure_payment_gateways",
        "ensure_payment_processor_identity",
    }

    #: What `after_migrate()` must call. `_ensure_storefront_application` is
    #: deliberately absent: the application record is created once, at install,
    #: and migrate does not re-create a record an administrator may have
    #: configured. Everything else is re-asserted on every migrate.
    EXPECTED_AFTER_MIGRATE = {
        "ensure_role_and_permissions",
        "ensure_custom_fields",
        "ensure_payment_gateways",
        "ensure_payment_processor_identity",
        "ensure_target_constraints",
    }

    def _calls(self, fn):
        """The global names the function's own bytecode references."""

        return set(fn.__code__.co_names)

    def _provisioners(self):
        """Every provisioning function `install` is responsible for calling.

        Deliberately NOT filtered by defining module: `ensure_role_and_permissions`
        is defined in `permissions.storefront_role` and imported here, and it is
        still a provisioner this entry point must call. Filtering on
        `__module__` would have silently excluded it -- and excluding a
        provisioner from the guard is the defect the guard exists to catch.
        """

        return {
            name for name, value in vars(install).items()
            if inspect.isfunction(value)
            and (name.startswith("ensure_") or name == "_ensure_storefront_application")
        }

    def test_the_provisioner_set_is_what_the_expectations_describe(self):
        """Guard on the guard: a new `ensure_*` must be classified, not ignored."""

        self.assertEqual(
            self._provisioners(),
            self.EXPECTED_AFTER_INSTALL | self.EXPECTED_AFTER_MIGRATE,
            "install.py gained or lost a provisioning function; decide which "
            "entry point must call it and update the expectations here")

    def test_every_provisioner_is_called_by_an_entry_point(self):
        """THE Phase 32J guard. An orphan provisioner reaches no site."""

        reachable = self._calls(after_install) | self._calls(after_migrate)

        for name in sorted(self._provisioners()):
            with self.subTest(provisioner=name):
                self.assertIn(
                    name, reachable,
                    f"install.{name} is never called by after_install() or "
                    "after_migrate(), so it will not reach any site")

    def test_after_install_calls_exactly_the_documented_set(self):
        self.assertEqual(
            self._calls(after_install) & self._provisioners(),
            self.EXPECTED_AFTER_INSTALL)

    def test_after_migrate_calls_exactly_the_documented_set(self):
        self.assertEqual(
            self._calls(after_migrate) & self._provisioners(),
            self.EXPECTED_AFTER_MIGRATE)

    def test_everything_an_existing_site_needs_is_re_asserted_on_migrate(self):
        """An upgraded site gets every provisioner except the one-time record."""

        self.assertEqual(
            self._provisioners() - self.EXPECTED_AFTER_MIGRATE,
            {"_ensure_storefront_application"},
            "a provisioner stopped being re-asserted on migrate; that is the "
            "exact shape of the Phase 32J defect")

    def test_both_entry_points_are_registered_in_hooks(self):
        from yob_storefront import hooks

        self.assertEqual(hooks.after_install, "yob_storefront.install.after_install")
        self.assertEqual(hooks.after_migrate, "yob_storefront.install.after_migrate")


class AfterMigrateRestoresCase(unittest.TestCase):
    """Remove it, migrate, get it back -- then migrate again and change nothing.

    `after_migrate()` IS NOT ATOMIC, and this class had to learn it the hard
    way. Recreating a Custom Field makes `create_custom_fields` call
    `frappe.db.updatedb()`, whose `ALTER TABLE` commits implicitly in MariaDB --
    in the MIDDLE of the hook, after the role and field work and BEFORE the
    gateway registration. A test that deleted several artefacts and relied on
    `rollback()` therefore committed the deletions that had not yet been
    restored at that point and lost the Payment Gateway from the site for real.

    So `tearDown` does not trust the rollback: it rolls back, re-runs the
    (idempotent) hook and COMMITS, which leaves the bench fully provisioned
    whatever the hook committed halfway through. A genuinely broken hook makes
    the next test fail loudly, which is the right outcome.
    """

    def setUp(self):
        frappe.set_user("Administrator")

    def tearDown(self):
        frappe.db.rollback()
        # Deterministic restoration; see the class docstring.
        after_migrate()
        frappe.db.commit()
        frappe.clear_cache()

    # ------------------------------------------------------------------ helpers

    def _managed_state(self):
        """Everything the migrate hook is responsible for, as comparable data."""

        return {
            "buyer_role": bool(frappe.db.exists("Role", STOREFRONT_BUYER_ROLE)),
            "processor_role": bool(frappe.db.exists("Role", PAYMENT_PROCESSOR_ROLE)),
            "processor_user": bool(frappe.db.exists("User", PAYMENT_PROCESSOR_USER)),
            "processor_has_role": bool(frappe.db.exists(
                "Has Role", {"parent": PAYMENT_PROCESSOR_USER,
                             "role": PAYMENT_PROCESSOR_ROLE})),
            "buyer_perms": sorted(frappe.get_all(
                "Custom DocPerm", filters={"role": STOREFRONT_BUYER_ROLE},
                fields=["parent", "read", "select", "write", "export"],
                order_by="parent"), key=str),
            "processor_perms": sorted(frappe.get_all(
                "Custom DocPerm", filters={"role": PAYMENT_PROCESSOR_ROLE},
                fields=["parent", "read", "select", "write", "export"],
                order_by="parent"), key=str),
            "customer_fields": sorted(frappe.get_all(
                "Custom Field", filters={"dt": "Customer",
                                         "fieldname": ["in", CUSTOMER_PANEL_FIELDS]},
                fields=["fieldname", "fieldtype", "label", "insert_after"],
                order_by="fieldname"), key=str),
            "gateway": bool(frappe.db.exists("Payment Gateway", RAZORPAY)),
            "target_index": _unique_indexes(TARGET_DOCTYPE),
        }

    def _assert_fully_provisioned(self, state):
        self.assertTrue(state["buyer_role"])
        self.assertTrue(state["processor_role"])
        self.assertTrue(state["processor_user"])
        self.assertTrue(state["processor_has_role"])
        self.assertTrue(state["gateway"])
        self.assertEqual(len(state["customer_fields"]), 2)
        self.assertIn(TARGET_INDEX_COLUMNS, state["target_index"].values())

        buyer = {row["parent"] for row in state["buyer_perms"]}
        for doctype in MANAGED_READ_DOCTYPES + MANAGED_SELECT_DOCTYPES:
            self.assertIn(doctype, buyer, f"the buyer lost {doctype}")

        processor = {row["parent"] for row in state["processor_perms"]}
        for doctype in ("Item", "Account", "Sales Taxes and Charges Template"):
            self.assertIn(doctype, processor, f"the processor lost {doctype}")

    # ------------------------------------------------------- the whole function

    def test_the_site_is_fully_provisioned_to_begin_with(self):
        """Guard: if this fails, every restore assertion below proves nothing."""

        self._assert_fully_provisioned(self._managed_state())

    def test_after_migrate_is_a_no_op_on_a_provisioned_site(self):
        before = self._managed_state()

        after_migrate()
        frappe.clear_cache()

        self.assertEqual(self._managed_state(), before,
                         "after_migrate() changed a provisioned site")

    def test_after_migrate_is_idempotent(self):
        after_migrate()
        once = self._managed_state()

        after_migrate()
        frappe.clear_cache()

        self.assertEqual(self._managed_state(), once,
                         "the second after_migrate() differed from the first")

    def test_after_install_is_safe_to_re_run(self):
        """It is the fresh-install path, but every block guards on existence."""

        before = self._managed_state()

        after_install()
        frappe.clear_cache()

        self.assertEqual(self._managed_state(), before,
                         "after_install() is not idempotent on a live site")

    def test_after_migrate_restores_everything_that_was_removed(self):
        """One call, every managed artefact back. The integration claim."""

        frappe.db.delete("Custom DocPerm", {"role": STOREFRONT_BUYER_ROLE})
        frappe.db.delete("Custom DocPerm", {"role": PAYMENT_PROCESSOR_ROLE})
        frappe.db.delete("Role", {"name": STOREFRONT_BUYER_ROLE})
        frappe.db.delete("Has Role", {"parent": PAYMENT_PROCESSOR_USER,
                                      "role": PAYMENT_PROCESSOR_ROLE})
        frappe.db.delete("Custom Field", {"dt": "Customer",
                                          "fieldname": ["in", CUSTOMER_PANEL_FIELDS]})
        frappe.db.delete("Payment Gateway", {"name": RAZORPAY})
        frappe.clear_cache()

        stripped = self._managed_state()
        self.assertFalse(stripped["buyer_role"])
        self.assertFalse(stripped["gateway"])
        self.assertEqual(stripped["customer_fields"], [])

        after_migrate()
        frappe.clear_cache()

        self._assert_fully_provisioned(self._managed_state())

    # ------------------------------------------------- one artefact at a time

    def test_the_buyer_role_and_its_docperms_come_back(self):
        frappe.db.delete("Custom DocPerm", {"role": STOREFRONT_BUYER_ROLE})
        frappe.db.delete("Role", {"name": STOREFRONT_BUYER_ROLE})
        frappe.clear_cache()

        after_migrate()
        frappe.clear_cache()

        self.assertTrue(frappe.db.exists("Role", STOREFRONT_BUYER_ROLE))
        self.assertEqual(
            frappe.db.get_value("Role", STOREFRONT_BUYER_ROLE, "desk_access"), 0,
            "the restored buyer role can reach Desk")
        for doctype in MANAGED_READ_DOCTYPES:
            self.assertIn(STOREFRONT_BUYER_ROLE, _docperm_roles(doctype))
        for doctype in MANAGED_SELECT_DOCTYPES:
            self.assertIn(STOREFRONT_BUYER_ROLE, _docperm_roles(doctype))

    def test_restoring_the_docperms_keeps_the_standard_roles(self):
        """A Custom DocPerm REPLACES the whole set; `setup_custom_perms` first."""

        frappe.db.delete("Custom DocPerm", {"parent": "Item"})
        frappe.clear_cache()

        after_migrate()
        frappe.clear_cache()

        roles = _docperm_roles("Item")
        self.assertIn(STOREFRONT_BUYER_ROLE, roles)
        for standard in ("Item Manager", "Stock User", "Sales User"):
            with self.subTest(role=standard):
                self.assertIn(standard, roles,
                              f"{standard} lost Item access to the YOB grant")

    def test_the_processor_identity_is_re_asserted(self):
        frappe.db.delete("Custom DocPerm", {"role": PAYMENT_PROCESSOR_ROLE})
        frappe.db.delete("Role", {"name": PAYMENT_PROCESSOR_ROLE})
        frappe.db.delete("Has Role", {"parent": PAYMENT_PROCESSOR_USER,
                                      "role": PAYMENT_PROCESSOR_ROLE})
        frappe.clear_cache()

        after_migrate()
        frappe.clear_cache()

        self.assertTrue(frappe.db.exists("Role", PAYMENT_PROCESSOR_ROLE))
        self.assertTrue(frappe.db.exists(
            "Has Role", {"parent": PAYMENT_PROCESSOR_USER,
                         "role": PAYMENT_PROCESSOR_ROLE}),
            "the processor user lost its role and migrate did not re-attach it")

        self.assertIn(PAYMENT_PROCESSOR_ROLE,
                      _docperm_roles("Sales Taxes and Charges Template"))

    def test_the_restored_processor_grant_is_select_and_not_read(self):
        """Phase 32J's actual fix, re-proved through the migrate hook."""

        frappe.db.delete("Custom DocPerm", {"role": PAYMENT_PROCESSOR_ROLE})
        frappe.clear_cache()

        after_migrate()
        frappe.clear_cache()

        row = frappe.db.get_value(
            "Custom DocPerm",
            {"parent": "Sales Taxes and Charges Template",
             "role": PAYMENT_PROCESSOR_ROLE},
            ["read", "select", "write", "export"], as_dict=True)

        self.assertEqual(row.select, 1)
        self.assertEqual(row.read, 0)
        self.assertEqual(row.write, 0)
        self.assertEqual(row.export, 0)

    def test_the_processor_identity_stays_unable_to_authenticate(self):
        after_migrate()

        self.assertEqual(
            frappe.db.get_value("User", PAYMENT_PROCESSOR_USER, "enabled"), 0,
            "migrate enabled the internal payment identity")

    def test_the_customer_panel_fields_come_back_in_the_portal_users_tab(self):
        frappe.db.delete("Custom Field", {"dt": "Customer",
                                          "fieldname": ["in", CUSTOMER_PANEL_FIELDS]})
        frappe.clear_cache(doctype="Customer")

        after_migrate()
        frappe.clear_cache(doctype="Customer")

        names = [field.fieldname for field in frappe.get_meta("Customer").fields]
        for fieldname in CUSTOMER_PANEL_FIELDS:
            self.assertIn(fieldname, names, f"{fieldname} was not restored")

        self.assertLess(names.index("portal_users"),
                        names.index(CUSTOMER_PANEL_FIELDS[0]))
        self.assertLess(names.index(CUSTOMER_PANEL_FIELDS[1]),
                        names.index("more_info_tab"))

    def test_the_payment_gateway_registration_comes_back(self):
        frappe.db.delete("Payment Gateway", {"name": RAZORPAY})

        after_migrate()

        self.assertTrue(frappe.db.exists("Payment Gateway", RAZORPAY))


class StorefrontApplicationProvisioningCase(unittest.TestCase):
    """`_ensure_storefront_application`, on a throwaway application code.

    The real `STOREFRONT` record cannot be deleted and recreated in a test: live
    `YOB User Application Access` rows link to it. Patching the module constant
    runs the SAME function against a disposable code instead, so the defaults it
    ships are asserted rather than inferred from the record a human configured
    months ago.
    """

    CODE = "_TESTSFPROV"

    def setUp(self):
        frappe.set_user("Administrator")

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()

    def _provision(self):
        with patch.object(install, "STOREFRONT_APP", self.CODE):
            install._ensure_storefront_application()

    def test_a_fresh_install_creates_the_application_with_its_shipped_defaults(self):
        self._provision()

        app = frappe.get_doc("YOB Application", self.CODE)

        self.assertEqual(app.application_code, self.CODE)
        self.assertEqual(app.enabled, 1)
        self.assertEqual(app.required_profile_doctype, "Customer")
        self.assertEqual(app.allow_password_login, 1)
        self.assertEqual(app.allow_email_otp, 0)
        self.assertEqual(app.allow_mobile_otp, 0)

    def test_it_ships_no_environment_specific_routing(self):
        """`domains` and `allowed_origins` are site facts, not install facts.

        Shipping a host here would either break every other deployment or, worse,
        pass the domain check for a host the operator never approved.
        """

        self._provision()

        app = frappe.get_doc("YOB Application", self.CODE)

        self.assertFalse((app.domains or "").strip())
        self.assertFalse((app.allowed_origins or "").strip())
        self.assertFalse(app.required_role,
                         "a required role was shipped; the buyer role is granted "
                         "by the access hooks, not demanded by the application")

    def test_it_grants_nobody_access(self):
        """Access is an administrator's conscious act, never a side effect."""

        self._provision()

        self.assertFalse(
            frappe.get_all("YOB User Application Access",
                           filters={"application": self.CODE}),
            "provisioning created an access grant")

    def test_an_existing_application_is_never_overwritten(self):
        frappe.get_doc({
            "doctype": "YOB Application", "application_code": self.CODE,
            "application_name": "Configured By An Administrator",
            "enabled": 0, "domains": "configured.test",
            "allowed_origins": "https://configured.test",
            "allow_password_login": 0, "allow_email_otp": 1,
            "allow_mobile_otp": 1, "required_profile_doctype": "Customer",
        }).insert(ignore_permissions=True)

        before = frappe.get_doc("YOB Application", self.CODE).as_dict()
        before.pop("modified", None)

        self._provision()
        self._provision()

        after = frappe.get_doc("YOB Application", self.CODE).as_dict()
        after.pop("modified", None)

        self.assertEqual(after, before,
                         "provisioning overwrote a configured application")

    def test_provisioning_twice_creates_one_record(self):
        self._provision()
        self._provision()

        self.assertEqual(frappe.db.count("YOB Application", {"name": self.CODE}), 1)


class PaymentGatewayProvisioningCase(unittest.TestCase):
    """`ensure_payment_gateways` registers a gateway and owns no credentials."""

    def setUp(self):
        frappe.set_user("Administrator")
        # The registration is a precondition here, not an assumption: an earlier
        # module may legitimately have been mid-way through its own fixture.
        ensure_payment_gateways()

    def tearDown(self):
        frappe.db.rollback()
        ensure_payment_gateways()
        frappe.db.commit()
        frappe.clear_cache()

    def test_the_razorpay_gateway_exists_after_provisioning(self):
        frappe.db.delete("Payment Gateway", {"name": RAZORPAY})

        ensure_payment_gateways()

        self.assertTrue(frappe.db.exists("Payment Gateway", RAZORPAY))

    def test_provisioning_twice_creates_no_duplicate(self):
        frappe.db.delete("Payment Gateway", {"name": RAZORPAY})

        ensure_payment_gateways()
        ensure_payment_gateways()

        self.assertEqual(frappe.db.count("Payment Gateway", {"gateway": RAZORPAY}), 1)

    def test_an_existing_gateway_configuration_is_not_rewritten(self):
        """The administrator's `gateway_controller` must survive a migrate."""

        frappe.db.set_value("Payment Gateway", RAZORPAY, {
            "gateway_settings": "Razorpay Settings",
            "gateway_controller": "_test-configured-by-admin"})
        before = frappe.db.get_value(
            "Payment Gateway", RAZORPAY,
            ["gateway", "gateway_settings", "gateway_controller"], as_dict=True)

        ensure_payment_gateways()
        ensure_payment_gateways()

        self.assertEqual(
            frappe.db.get_value("Payment Gateway", RAZORPAY,
                                ["gateway", "gateway_settings", "gateway_controller"],
                                as_dict=True),
            before)

    def test_provisioning_writes_no_credentials(self):
        """Credentials have exactly one home, and it is not YOB."""

        frappe.db.delete("Payment Gateway", {"name": RAZORPAY})
        # Read with SQL: `Singles` is a table, not a DocType, so `get_all`
        # refuses it. Its rows are where a gateway's Settings Single lives.
        stored = lambda: frappe.db.sql(
            "select field, value from tabSingles where doctype = %s order by field",
            "Razorpay Settings")
        before = stored()

        ensure_payment_gateways()

        self.assertEqual(stored(), before,
                         "provisioning touched the gateway's own credential store")

        # EXECUTABLE code only: the docstring legitimately explains that
        # credentials live in `Razorpay Settings`, and scanning the whole source
        # would fail on its own documentation.
        body = ast.parse(textwrap.dedent(inspect.getsource(ensure_payment_gateways)))
        function = body.body[0]
        if ast.get_docstring(function):
            function.body = function.body[1:]
        code = ast.unparse(function)

        for secret in ("api_key", "api_secret", "password", "Settings"):
            self.assertNotIn(secret, code,
                             f"the registration step references {secret}")


class TargetConstraintCase(unittest.TestCase):
    """`ensure_target_constraints`, by semantics rather than by index name.

    The index is NOT dropped and recreated: DDL commits implicitly in MariaDB,
    which would end the test transaction. What matters is the constraint, so it
    is asserted by COLUMN SET, and the provisioning call is asserted to be a
    no-op once the constraint is in place.
    """

    def setUp(self):
        frappe.set_user("Administrator")

    def tearDown(self):
        frappe.db.rollback()

    def test_the_composite_constraint_exists_on_the_right_columns(self):
        indexes = _unique_indexes(TARGET_DOCTYPE)

        self.assertIn(
            TARGET_INDEX_COLUMNS, indexes.values(),
            "no UNIQUE index covers (customer, target_rule); two scheduler "
            f"workers can create duplicate targets. Found: {indexes}")

    def test_provisioning_again_is_harmless(self):
        before = _unique_indexes(TARGET_DOCTYPE)

        ensure_target_constraints()
        ensure_target_constraints()

        self.assertEqual(_unique_indexes(TARGET_DOCTYPE), before)

    def test_it_issues_no_ddl_once_the_constraint_exists(self):
        """`db.add_unique` checks information_schema, so migrate stays cheap."""

        with patch.object(frappe.db, "add_unique") as add_unique:
            ensure_target_constraints()

        add_unique.assert_called_once()
        self.assertEqual(add_unique.call_args.args[0], TARGET_DOCTYPE)
        self.assertEqual(list(add_unique.call_args.args[1]),
                         list(TARGET_INDEX_COLUMNS))

    def test_the_first_install_branch_does_nothing(self):
        """The table arrives later in the same migrate; the next one indexes it."""

        with patch.object(frappe.db, "table_exists", return_value=False), \
             patch.object(frappe.db, "add_unique") as add_unique:
            ensure_target_constraints()

        add_unique.assert_not_called()


if __name__ == "__main__":
    unittest.main()
