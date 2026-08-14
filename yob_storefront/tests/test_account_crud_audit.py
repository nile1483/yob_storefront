# Copyright (c) 2026, YOB and Shayona
"""Account CRUD contract: `update_address`, `delete_address`, `*_contact`.

HISTORY -- WHAT THIS FILE USED TO SAY
-------------------------------------
Phase 15A audited these endpoints before the storefront exposed them and found
three defects, recorded here as `test_DEFECT_*` tests that ASSERTED THE WRONG
BEHAVIOUR so the audit stayed reproducible:

  1. `update_address` was a full replace. It assigned every field
     unconditionally from `form_dict`, so an edit form posting only the inputs
     it rendered wiped `address_line2`, `phone`, `email_id` and the
     `is_primary_address` / `is_shipping_address` flags. The call SUCCEEDED --
     nothing warned that data had been destroyed.
  2. A partial payload answered `internal_server_error` with no field, because
     india_compliance makes `gst_category` mandatory and the blanked value
     failed Frappe's own mandatory check.
  3. `clear_customer_address_cache(customer)` was passed the Customer DOCUMENT
     while the key is built from `customer.name`, so the key never matched and
     the 30-minute list cache outlived every write.

Phase 15B fixed all three. Those tests are now the positive assertions below --
rewritten, not deleted, so the defect they came from stays legible.

WHAT THIS FILE GUARANTEES NOW
-----------------------------
* `update_address` is a PARTIAL update keyed on request PRESENCE, not
  truthiness. Omitted means unchanged; explicitly empty means clear.
* Document names are stable. `address_title` and contact names are ordinary
  fields -- no rename, so historical Sales Order links never move.
* Link integrity refusals are business errors (409 `address_in_use` /
  `contact_in_use`), and carry no Desk HTML, no `_server_messages`, and no
  referring document name.
* List caches are invalidated on every mutation, so a read straight after a
  write sees the write.

ISOLATION
---------
Every test builds its own fixtures and rolls back completely.
`yob_core.api.response.server_error` calls `frappe.db.rollback()`, which
destroys savepoints, so `tearDown` uses a full rollback rather than a named
savepoint. `frappe.db.commit` is neutered for the whole case: several tests
touch REAL development Sales Orders to prove link integrity, and a stray commit
would make those edits permanent.
"""

import inspect
import json
import unittest
from unittest.mock import patch

import frappe

CUSTOMER = "YOB Demo Buyer"


def _seeded() -> bool:
    return bool(frappe.db.exists("Customer", CUSTOMER))


class AccountCrudCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data on the test site")

    def setUp(self):
        frappe.set_user("Administrator")
        from yob_storefront.api import address as address_api
        from yob_storefront.api import cart as cart_api

        self.api = address_api
        self.cart_api = cart_api
        self.customer = frappe.get_doc("Customer", CUSTOMER)

        for module in (address_api, cart_api):
            p = patch.object(module, "get_storefront_customer", return_value=self.customer)
            p.start()
            self.addCleanup(p.stop)

        # Nothing in this file may outlive the rollback. Real Sales Orders are
        # edited below to prove link integrity; a commit would persist that.
        self.commits = []
        commit_patch = patch.object(frappe.db, "commit",
                                    side_effect=lambda *a, **k: self.commits.append(1))
        commit_patch.start()
        self.addCleanup(commit_patch.stop)

        self.clear_caches()

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()
        self.assertEqual(self.commits, [], "a commit escaped the test rollback")

    # ---------------------------------------------------------------- helpers

    def clear_caches(self):
        frappe.cache().delete_value(self.api.get_addresses_cache_key(CUSTOMER))
        frappe.cache().delete_value(self.api.get_contacts_cache_key(CUSTOMER))

    def call(self, endpoint, **kwargs):
        """Invoke the real endpoint body; only identity resolution is stubbed."""
        return inspect.unwrap(endpoint)(auth_context={}, **kwargs)

    def error_of(self, response):
        if isinstance(response, dict) and "errors" in response:
            return response["errors"][0]
        return None

    def post(self, **payload):
        frappe.form_dict = frappe._dict(payload)

    def make_address(self, title, customer=None, **extra):
        doc = frappe.get_doc({
            "doctype": "Address", "address_title": title, "address_type": "Billing",
            "address_line1": "1 Probe Road", "city": "Ahmedabad", "state": "Gujarat",
            "country": "India", "pincode": "382445",
            "links": [{"link_doctype": "Customer", "link_name": customer or CUSTOMER}],
            **extra,
        })
        doc.insert(ignore_permissions=True)
        return doc.name

    def make_contact(self, first, customer=None):
        doc = frappe.get_doc({
            "doctype": "Contact", "first_name": first, "last_name": "Probe",
            "links": [{"link_doctype": "Customer", "link_name": customer or CUSTOMER}],
        })
        doc.insert(ignore_permissions=True)
        return doc.name

    def stored(self, name, *fields):
        return frappe.db.get_value("Address", name, list(fields), as_dict=True)

    def a_real_order(self):
        orders = frappe.get_all("Sales Order", filters={"customer": CUSTOMER}, limit=1)
        if not orders:
            self.skipTest("no Sales Order on this site")
        return orders[0].name

    def _foreign_customer(self):
        return frappe.get_doc({
            "doctype": "Customer", "customer_name": "Probe Other Co",
            "customer_type": "Company",
            "customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
            "territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
        }).insert(ignore_permissions=True).name


# =========================================================================
# 1. PARTIAL UPDATE  (was test_DEFECT_update_address_silently_wipes_*)
# =========================================================================

class UpdateAddressIsPartialCase(AccountCrudCase):

    def rich_address(self):
        """An address with every optional field populated -- the data at risk."""
        return self.make_address(
            "Probe Rich", address_line2="Suite 5", phone="+91 11111 11111",
            email_id="p@example.invalid", fax="+91 22222 22222",
            is_primary_address=1, is_shipping_address=1)

    OPTIONAL = ("address_line2", "phone", "email_id", "fax",
                "is_primary_address", "is_shipping_address",
                "address_title", "state", "pincode", "gst_category")

    def test_omitted_fields_are_preserved(self):
        """THE regression. A minimal payload must not destroy what it omits."""

        name = self.rich_address()
        before = self.stored(name, *self.OPTIONAL)

        self.post(name=name, address_line1="2 Rich Road")
        response = self.call(self.api.update_address)
        self.assertNotIn("errors", response, f"partial update failed: {response}")

        self.assertEqual(self.stored(name, "address_line1").address_line1,
                         "2 Rich Road", "the supplied field was not applied")

        after = self.stored(name, *self.OPTIONAL)
        self.assertEqual(after, before,
                         "omitted fields were modified -- update_address is "
                         "destroying data it was never asked to touch")

    def test_a_supplied_second_field_is_applied(self):
        """Partial does not mean "only one field"."""

        name = self.rich_address()

        self.post(name=name, address_line1="3 Rich Road", city="Surat")
        self.assertNotIn("errors", self.call(self.api.update_address))

        after = self.stored(name, "address_line1", "city", "phone")
        self.assertEqual(after.address_line1, "3 Rich Road")
        self.assertEqual(after.city, "Surat")
        self.assertEqual(after.phone, "+91 11111 11111", "an untouched field moved")

    def test_an_explicitly_empty_optional_field_is_cleared(self):
        """Omission preserves; an explicit empty value CLEARS. Both are needed."""

        name = self.rich_address()

        self.post(name=name, address_line2="")
        self.assertNotIn("errors", self.call(self.api.update_address))

        after = self.stored(name, "address_line2", "phone")
        self.assertFalse(after.address_line2, "an explicit clear was ignored")
        self.assertEqual(after.phone, "+91 11111 11111",
                         "clearing one field cleared another")

    def test_a_flag_can_be_explicitly_turned_off(self):
        """`0` is a value, not an absence -- truthiness would drop it."""

        name = self.rich_address()

        self.post(name=name, is_primary_address=0)
        self.assertNotIn("errors", self.call(self.api.update_address))

        after = self.stored(name, "is_primary_address", "is_shipping_address")
        self.assertEqual(after.is_primary_address, 0, "an explicit 0 was ignored")
        self.assertEqual(after.is_shipping_address, 1, "the other flag was reset")

    def test_the_same_payload_twice_converges(self):
        """Retry safety: update_address is idempotent for a fixed payload."""

        name = self.rich_address()
        self.post(name=name, address_line1="4 Rich Road")

        self.assertNotIn("errors", self.call(self.api.update_address))
        first = self.stored(name, *self.OPTIONAL, "address_line1")

        self.post(name=name, address_line1="4 Rich Road")
        self.assertNotIn("errors", self.call(self.api.update_address))

        self.assertEqual(self.stored(name, *self.OPTIONAL, "address_line1"), first)

    # ----------------------------------------------------------- validation

    def test_clearing_a_required_field_is_an_attributed_validation_error(self):
        """Was `internal_server_error` with no field. Now names the field."""

        name = self.make_address("Probe Required")

        self.post(name=name, city="")
        error = self.error_of(self.call(self.api.update_address))

        self.assertIsNotNone(error, "a required field was cleared successfully")
        self.assertEqual(error["code"], "validation_failed")
        self.assertEqual(error["field"], "city")
        self.assertEqual(self.stored(name, "city").city, "Ahmedabad",
                         "the rejected write was still applied")

    def test_a_framework_validation_failure_is_not_a_500(self):
        """An invalid Link is the DATA being wrong, not the server breaking."""

        name = self.make_address("Probe BadLink")

        self.post(name=name, country="No Such Country At All")
        error = self.error_of(self.call(self.api.update_address))

        self.assertIsNotNone(error)
        self.assertEqual(error["code"], "validation_failed",
                         "a bad value surfaced as an internal server error")
        self.assertEqual(self.stored(name, "country").country, "India")

    def test_a_partial_payload_no_longer_trips_the_mandatory_gst_category(self):
        """The exact Phase 15A reproduction: name + one field must succeed.

        india_compliance makes `gst_category` mandatory on Address. The old
        full-replace blanked it and the save died as a generic 500.
        """

        name = self.make_address("Probe Partial")
        self.post(name=name, address_line1="only this")

        response = self.call(self.api.update_address)

        self.assertNotIn("errors", response, f"still failing: {response}")
        self.assertEqual(self.stored(name, "address_line1").address_line1, "only this")
        self.assertTrue(self.stored(name, "gst_category").gst_category,
                        "gst_category was blanked by a partial update")

    def test_validation_detail_exposes_no_framework_internals(self):
        name = self.make_address("Probe Clean")
        self.post(name=name, country="No Such Country At All")

        blob = json.dumps(self.call(self.api.update_address))

        for leak in ("Traceback", "/app/", "href", "<a ", "ValidationError"):
            self.assertNotIn(leak, blob, f"{leak!r} leaked in a validation error")

    # ------------------------------------------------------------- identity

    def test_update_does_not_rename_the_document(self):
        """Historical Sales Orders hold `customer_address` as a link."""

        name = self.make_address("Probe Stable")

        self.post(name=name, address_title="Completely Different Title")
        self.assertNotIn("errors", self.call(self.api.update_address))

        self.assertTrue(frappe.db.exists("Address", name), "docname changed")
        self.assertFalse(frappe.db.exists("Address", "Completely Different Title-Billing"))
        self.assertEqual(self.stored(name, "address_title").address_title,
                         "Completely Different Title")

    def test_update_preserves_customer_ownership(self):
        name = self.make_address("Probe Owned")

        self.post(name=name, address_line1="2 Updated Road")
        self.call(self.api.update_address)

        links = frappe.get_all("Dynamic Link",
                               filters={"parenttype": "Address", "parent": name},
                               fields=["link_doctype", "link_name"])
        self.assertEqual(links, [{"link_doctype": "Customer", "link_name": CUSTOMER}])

    def test_contact_update_does_not_rename_the_document(self):
        name = self.make_contact("Alpha")

        self.post(name=name, first_name="Renamed")
        self.assertNotIn("errors", self.call(self.api.update_contact))

        self.assertTrue(frappe.db.exists("Contact", name), "docname changed")
        self.assertEqual(frappe.db.get_value("Contact", name, "first_name"), "Renamed")

    def test_contact_update_preserves_omitted_fields(self):
        name = self.make_contact("Beta")
        frappe.db.set_value("Contact", name, "designation", "Buyer")

        self.post(name=name, first_name="Beta2")
        self.assertNotIn("errors", self.call(self.api.update_contact))

        self.assertEqual(frappe.db.get_value("Contact", name, "designation"), "Buyer")
        self.assertEqual(frappe.db.get_value("Contact", name, "last_name"), "Probe")

    def test_contact_update_rejects_an_explicitly_empty_required_name(self):
        """Was silently ignored, reporting success while nothing changed."""

        name = self.make_contact("Gamma")

        self.post(name=name, first_name="")
        error = self.error_of(self.call(self.api.update_contact))

        self.assertIsNotNone(error, "a required name was cleared successfully")
        self.assertEqual(error["code"], "validation_failed")
        self.assertEqual(error["field"], "first_name")
        self.assertEqual(frappe.db.get_value("Contact", name, "first_name"), "Gamma")

    def test_contact_update_response_reflects_the_saved_record(self):
        """The response echoed the REQUEST, so an omitted email read as null."""

        name = self.make_contact("Delta")
        contact = frappe.get_doc("Contact", name)
        contact.append("email_ids", {"email_id": "delta@example.invalid", "is_primary": 1})
        contact.save(ignore_permissions=True)

        self.post(name=name, designation="Manager")
        data = self.call(self.api.update_contact)["data"]

        self.assertEqual(data["email"], "delta@example.invalid",
                         "the response contradicted the record it just wrote")

    # ------------------------------------------------------------- security

    def test_another_customers_address_is_not_editable_or_deletable(self):
        foreign = self.make_address("Foreign Probe", customer=self._foreign_customer())

        self.post(name=foreign, address_line1="hijacked")
        self.assertEqual(self.error_of(self.call(self.api.update_address))["code"],
                         "address_not_found")
        self.assertEqual(self.error_of(self.call(self.api.delete_address, name=foreign))["code"],
                         "address_not_found")
        self.assertEqual(self.stored(foreign, "address_line1").address_line1, "1 Probe Road")

    def test_another_customers_contact_is_not_editable_or_deletable(self):
        foreign = self.make_contact("Foreign", customer=self._foreign_customer())

        self.post(name=foreign, first_name="hijacked")
        self.assertEqual(self.error_of(self.call(self.api.update_contact))["code"],
                         "contact_not_found")
        self.assertEqual(self.error_of(self.call(self.api.delete_contact, name=foreign))["code"],
                         "contact_not_found")
        self.assertEqual(frappe.db.get_value("Contact", foreign, "first_name"), "Foreign")


# =========================================================================
# 2. DELETE LINK CONFLICTS
# =========================================================================

class DeleteLinkConflictCase(AccountCrudCase):
    """Link integrity refusals are business errors, not crashes.

    Nothing here works around Frappe's link check: a refused delete stays
    refused. Only the CONTRACT around the refusal changed.
    """

    # What Frappe actually queues, captured from the running framework:
    #
    #   Cannot delete or cancel because Contact <a href="http://<site>/desk/
    #   contact/LeakProbe-YOB%20Demo%20Buyer" ...>...</a> is linked with Cart
    #   <a href="http://<site>/desk/cart/CART-2026-08-0002" ...>...</a>
    #
    # -- an absolute Desk URL including the host, the referring docname, and
    # HTML, at HTTP 417. None of it may reach a storefront caller.
    FORBIDDEN_IN_RESPONSE = ("Traceback", "_server_messages", "href", "<a ",
                             "/desk/", "/app/", "http://", "https://",
                             "LinkExistsError", "You can disable",
                             "Cannot delete or cancel")

    def assert_conflict(self, response, code, *must_not_appear):
        error = self.error_of(response)

        self.assertIsNotNone(error, "a linked record was deleted")
        self.assertEqual(error["code"], code)

        blob = json.dumps(response)
        for leak in self.FORBIDDEN_IN_RESPONSE + must_not_appear:
            self.assertNotIn(leak, blob, f"{leak!r} leaked to the storefront")

        # Frappe queued its Desk-flavoured refusal for `_server_messages`
        # BEFORE raising. If that log is not truncated, the framework serialises
        # it onto the same HTTP response as our clean envelope.
        self.assertEqual(frappe.local.message_log, [],
                         "Frappe's raw refusal is still queued for "
                         "_server_messages and would reach the browser")

    def setUp(self):
        super().setUp()
        frappe.local.message_log = []

    # ------------------------------------------------------------- address

    def test_unreferenced_address_deletes(self):
        name = self.make_address("Probe Deletable")

        self.assertNotIn("errors", self.call(self.api.delete_address, name=name))
        self.assertFalse(frappe.db.exists("Address", name))

    def test_cart_selected_address_is_refused_with_409(self):
        name = self.make_address("Probe CartLinked")
        cart = self.cart_api.get_or_create_cart(self.customer)
        cart.billing_address = name
        cart.save(ignore_permissions=True)

        response = self.call(self.api.delete_address, name=name)

        self.assert_conflict(response, "address_in_use", cart.name)
        self.assertTrue(frappe.db.exists("Address", name))

    def test_order_referenced_address_is_refused_with_409(self):
        order = self.a_real_order()
        name = self.make_address("Probe OrderLinked")
        frappe.db.set_value("Sales Order", order, "customer_address", name)

        response = self.call(self.api.delete_address, name=name)

        self.assert_conflict(response, "address_in_use", order)
        self.assertTrue(frappe.db.exists("Address", name))

    def test_customer_default_address_is_refused_with_409(self):
        name = self.make_address("Probe DefaultLinked")
        frappe.db.set_value("Customer", CUSTOMER, "customer_primary_address", name)

        response = self.call(self.api.delete_address, name=name)

        self.assert_conflict(response, "address_in_use", CUSTOMER)
        self.assertTrue(frappe.db.exists("Address", name))

    def test_a_refused_delete_detaches_nothing(self):
        """The referring document must be exactly as it was."""

        order = self.a_real_order()
        name = self.make_address("Probe Intact")
        frappe.db.set_value("Sales Order", order, "customer_address", name)
        snapshot = frappe.db.get_value(
            "Sales Order", order, ["customer_address", "address_display", "docstatus"],
            as_dict=True)

        self.call(self.api.delete_address, name=name)

        self.assertEqual(
            frappe.db.get_value("Sales Order", order,
                                ["customer_address", "address_display", "docstatus"],
                                as_dict=True),
            snapshot, "the refused delete modified the referring order")

    # ------------------------------------------------------------- contact

    def test_unreferenced_contact_deletes(self):
        name = self.make_contact("Deletable")

        self.assertNotIn("errors", self.call(self.api.delete_contact, name=name))
        self.assertFalse(frappe.db.exists("Contact", name))

    def test_cart_selected_contact_is_refused_with_409(self):
        """Was a raw Frappe LinkExistsError -- HTTP 417, not a YOB envelope."""

        name = self.make_contact("CartLinked")
        cart = self.cart_api.get_or_create_cart(self.customer)
        cart.contact_person = name
        cart.save(ignore_permissions=True)

        response = self.call(self.api.delete_contact, name=name)

        self.assert_conflict(response, "contact_in_use", cart.name)
        self.assertTrue(frappe.db.exists("Contact", name))

    def test_order_referenced_contact_is_refused_with_409(self):
        order = self.a_real_order()
        name = self.make_contact("OrderLinked")
        frappe.db.set_value("Sales Order", order, "contact_person", name)

        response = self.call(self.api.delete_contact, name=name)

        self.assert_conflict(response, "contact_in_use", order)
        self.assertTrue(frappe.db.exists("Contact", name))

    def test_link_conflict_does_not_raise(self):
        """Explicitly: no exception escapes to become a raw framework response."""

        name = self.make_contact("NoRaise")
        cart = self.cart_api.get_or_create_cart(self.customer)
        cart.contact_person = name
        cart.save(ignore_permissions=True)

        try:
            response = self.call(self.api.delete_contact, name=name)
        except Exception as exc:                       # noqa: BLE001 -- the point
            self.fail(f"delete_contact raised {type(exc).__name__} instead of "
                      f"returning a YOB error envelope")

        self.assertEqual(self.error_of(response)["code"], "contact_in_use")

    # ------------------------------------------------------- non-disclosure

    def test_unknown_and_foreign_addresses_are_indistinguishable(self):
        foreign = self.make_address("Foreign Probe", customer=self._foreign_customer())

        missing = self.error_of(self.call(self.api.delete_address, name="No-Such-Address"))
        other = self.error_of(self.call(self.api.delete_address, name=foreign))

        self.assertEqual(missing, other)

    def test_unknown_and_foreign_contacts_are_indistinguishable(self):
        foreign = self.make_contact("Foreign", customer=self._foreign_customer())

        missing = self.error_of(self.call(self.api.delete_contact, name="No-Such-Contact"))
        other = self.error_of(self.call(self.api.delete_contact, name=foreign))

        self.assertEqual(missing, other)

    def test_in_use_is_distinct_from_not_found_and_server_error(self):
        """The three outcomes a client must tell apart."""

        deletable = self.make_address("Probe Distinct")
        linked = self.make_address("Probe DistinctLinked")
        cart = self.cart_api.get_or_create_cart(self.customer)
        cart.billing_address = linked
        cart.save(ignore_permissions=True)

        self.assertNotIn("errors", self.call(self.api.delete_address, name=deletable))
        self.assertEqual(
            self.error_of(self.call(self.api.delete_address, name=linked))["code"],
            "address_in_use")
        self.assertEqual(
            self.error_of(self.call(self.api.delete_address, name="Nope"))["code"],
            "address_not_found")


# =========================================================================
# 3. CACHE INVALIDATION  (was test_DEFECT_address_list_cache_is_not_*)
# =========================================================================

class ListCacheInvalidationCase(AccountCrudCase):
    """A read straight after a write must see the write.

    The bug: `clear_customer_address_cache(customer)` received the Customer
    DOCUMENT, while the key is `f"...{customer_name}:addresses"`. Formatting a
    Document produces a completely different string, so the key never matched,
    the delete was a no-op, and the 30-minute list survived every mutation.

    These tests fail again if a Customer object is ever passed instead of a name.
    """

    def addresses(self):
        response = self.call(self.api.get_addresses)
        self.assertNotIn("errors", response, f"listing failed: {response}")
        return response

    def contacts(self):
        response = self.call(self.api.get_contacts)
        self.assertNotIn("errors", response, f"listing failed: {response}")
        return response

    def row(self, response, name):
        return next((r for r in response["data"] if r["name"] == name), None)

    def test_an_edit_is_visible_on_the_next_read(self):
        name = self.make_address("Probe Cache")
        self.addresses()                                  # populate the cache

        self.post(name=name, address_line1="3 CacheTest Road")
        self.assertNotIn("errors", self.call(self.api.update_address))

        listed = self.addresses()
        self.assertEqual(self.row(listed, name)["address_line1"], "3 CacheTest Road",
                         "the list cache outlived the write -- the customer sees "
                         "stale data until the 30-minute TTL expires")

    def test_a_new_address_is_visible_on_the_next_read(self):
        self.addresses()                                  # populate the cache

        self.post(address_title="Probe Fresh", address_type="Billing",
                  address_line1="9 Fresh Road", city="Ahmedabad", state="Gujarat",
                  country="India", pincode="382445")
        created = self.call(self.api.add_address)
        self.assertNotIn("errors", created, f"add failed: {created}")

        self.assertIsNotNone(self.row(self.addresses(), created["data"]["name"]),
                             "a newly created address is missing from the list")

    def test_a_deleted_address_disappears_on_the_next_read(self):
        name = self.make_address("Probe Vanish")
        self.assertIsNotNone(self.row(self.addresses(), name))

        self.assertNotIn("errors", self.call(self.api.delete_address, name=name))

        self.assertIsNone(self.row(self.addresses(), name),
                          "a deleted address is still listed")

    def test_a_refused_delete_leaves_the_list_intact(self):
        name = self.make_address("Probe StillThere")
        cart = self.cart_api.get_or_create_cart(self.customer)
        cart.billing_address = name
        cart.save(ignore_permissions=True)
        self.addresses()

        self.call(self.api.delete_address, name=name)

        self.assertIsNotNone(self.row(self.addresses(), name),
                             "a refused delete removed the address from the list")

    def test_a_new_contact_is_visible_on_the_next_read(self):
        self.contacts()                                   # populate the cache

        self.post(first_name="Probe", last_name="Fresh")
        created = self.call(self.api.add_contact)
        self.assertNotIn("errors", created, f"add failed: {created}")

        self.assertIsNotNone(self.row(self.contacts(), created["data"]["name"]),
                             "a newly created contact is missing from the list")

    def test_a_contact_edit_is_visible_on_the_next_read(self):
        name = self.make_contact("Stale")
        self.contacts()

        self.post(name=name, first_name="Refreshed")
        self.assertNotIn("errors", self.call(self.api.update_contact))

        self.assertEqual(self.row(self.contacts(), name)["first_name"], "Refreshed",
                         "the contact list cache outlived the write")

    def test_a_deleted_contact_disappears_on_the_next_read(self):
        name = self.make_contact("Vanish")
        self.assertIsNotNone(self.row(self.contacts(), name))

        self.assertNotIn("errors", self.call(self.api.delete_contact, name=name))

        self.assertIsNone(self.row(self.contacts(), name),
                          "a deleted contact is still listed")


# =========================================================================
# 4. LIVE CART vs IMMUTABLE ORDER
# =========================================================================

class LiveCartVersusOrderHistoryCase(AccountCrudCase):
    """The distinction Phase 14.5 established, checked from the CRUD side.

    A Cart is live: it points at the Address master and must show the edit.
    An Order is historical: it carries its own order-time snapshot and must not.
    """

    def test_editing_an_address_does_not_change_a_past_order(self):
        order = self.a_real_order()
        linked = frappe.db.get_value("Sales Order", order, "customer_address")
        if not linked:
            self.skipTest("order has no linked address")

        before = frappe.db.get_value("Sales Order", order, "address_display")

        self.post(name=linked, address_line1="999 Rewritten Street")
        self.assertNotIn("errors", self.call(self.api.update_address))

        self.assertEqual(frappe.db.get_value("Sales Order", order, "address_display"),
                         before,
                         "editing the Address master rewrote a historical order")

    def test_a_cart_reflects_the_edited_master(self):
        """Cart addresses are deliberately NOT snapshotted."""

        name = self.make_address("Probe CartLive")
        cart = self.cart_api.get_or_create_cart(self.customer)
        cart.billing_address = name
        cart.save(ignore_permissions=True)

        self.post(name=name, address_line1="7 Live Road")
        self.assertNotIn("errors", self.call(self.api.update_address))

        self.assertEqual(
            frappe.db.get_value("Address", cart.billing_address, "address_line1"),
            "7 Live Road",
            "the Cart's address did not follow the master edit")


if __name__ == "__main__":
    unittest.main()
