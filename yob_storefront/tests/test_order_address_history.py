# Copyright (c) 2026, YOB and Shayona
"""Order detail must show the address AS IT WAS, not as the master is today.

THE BUG THIS PINS
-----------------
`get_order_details` used to read the linked Address master live:

    data["billing_address"] = frappe.db.get_value("Address", order.customer_address, ...)

So editing an address silently rewrote the address on every past order. A
customer who moved would find last year's invoice showing their new address.
For an order -- an invoice-grade historical record -- that is data corruption,
and it is invisible: nothing errors, the numbers still add up.

WHAT REPLACED IT
----------------
ERPNext already snapshots the rendered address onto the Sales Order at creation:

    billing   Sales Order.address_display
    shipping  Sales Order.shipping_address    (the LINK is in
              `shipping_address_name` -- ERPNext's naming is inverted here)

The API now projects those as plain-text `billing_address_display` /
`shipping_address_display`, and does not read the Address master at all when a
snapshot exists.

IF THESE TESTS FAIL
-------------------
Do not make them pass by re-reading the Address master. That reintroduces the
bug. Either the snapshot stopped being populated at commitment, or the
projection regressed.

Every fixture here is savepoint-isolated. Real development orders are never
touched.
"""

import inspect
import unittest

import frappe

CUSTOMER = "YOB Demo Buyer"
ITEM = "YOB-BOLT-M10"


def _seeded() -> bool:
    return bool(frappe.db.exists("Customer", CUSTOMER)
                and frappe.db.exists("Item", ITEM))


class OrderAddressHistoryCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data on the test site")

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.db.savepoint("order_history")
        self.customer = frappe.get_doc("Customer", CUSTOMER)

    def tearDown(self):
        frappe.db.rollback(save_point="order_history")
        frappe.clear_cache()

    # ----------------------------------------------------------- fixtures

    def make_address(self, title, line1):
        doc = frappe.get_doc({
            "doctype": "Address", "address_title": title,
            "address_type": "Billing", "address_line1": line1,
            "city": "Testville", "state": "Gujarat", "pincode": "380001",
            "country": "India",
            "links": [{"link_doctype": "Customer", "link_name": CUSTOMER}],
        })
        doc.insert(ignore_permissions=True)
        return doc.name

    def make_order(self, billing=None, shipping=None,
                   billing_snapshot=None, shipping_snapshot=None):
        """A Draft Sales Order with explicitly controlled snapshot values."""

        so = frappe.new_doc("Sales Order")
        so.customer = CUSTOMER
        so.company = frappe.db.get_value("Company", {}, "name")
        so.transaction_date = frappe.utils.today()
        so.delivery_date = frappe.utils.today()
        so.customer_address = billing
        so.shipping_address_name = shipping
        so.append("items", {"item_code": ITEM, "qty": 1, "rate": 100,
                            "delivery_date": frappe.utils.today()})
        so.flags.ignore_permissions = True
        so.set_missing_values()
        so.insert(ignore_permissions=True)

        # set_missing_values() helpfully fills BOTH the address links and their
        # rendered snapshots from the Customer's defaults. That defeats a
        # fixture trying to express "no shipping address at all", so the links
        # and snapshots are both forced to exactly what the test asked for.
        frappe.db.set_value("Sales Order", so.name, {
            "customer_address": billing,
            "address_display": billing_snapshot,
            "shipping_address_name": shipping,
            "shipping_address": shipping_snapshot,
        }, update_modified=False)
        frappe.clear_document_cache("Sales Order", so.name)

        return so.name

    def detail(self, order_id):
        from unittest.mock import patch

        from yob_storefront.api import order as order_api

        with patch.object(order_api, "get_storefront_customer",
                          return_value=self.customer):
            response = inspect.unwrap(order_api.get_order_details)(
                order_id=order_id, auth_context={})

        self.assertNotIn("errors", response, f"order detail failed: {response}")
        return response["data"]

    # ----------------------------------------------------------- billing

    def test_billing_snapshot_survives_address_master_edit(self):
        """THE regression. Editing the master must not rewrite past orders."""

        address = self.make_address("History Billing", "123 Old Street")
        order = self.make_order(billing=address,
                                billing_snapshot="123 Old Street<br>Testville")

        self.assertIn("123 Old Street",
                      self.detail(order)["billing_address_display"])

        # The customer moves.
        frappe.db.set_value("Address", address, "address_line1", "456 New Street")
        frappe.clear_document_cache("Address", address)

        after = self.detail(order)["billing_address_display"]

        self.assertIn("123 Old Street", after,
                      "the order lost its historical billing address")
        self.assertNotIn("456 New Street", after,
                         "editing the Address master rewrote a past order -- the "
                         "live-master read has been reintroduced")

    # ----------------------------------------------------------- shipping

    def test_shipping_snapshot_survives_address_master_edit(self):
        """Same regression on the shipping side."""

        address = self.make_address("History Shipping", "1 Old Depot Road")
        order = self.make_order(shipping=address,
                                shipping_snapshot="1 Old Depot Road<br>Testville")

        self.assertIn("1 Old Depot Road",
                      self.detail(order)["shipping_address_display"])

        frappe.db.set_value("Address", address, "address_line1", "2 New Depot Road")
        frappe.clear_document_cache("Address", address)

        after = self.detail(order)["shipping_address_display"]

        self.assertIn("1 Old Depot Road", after,
                      "the order lost its historical shipping address")
        self.assertNotIn("2 New Depot Road", after,
                         "editing the Address master rewrote a past order")

    # ----------------------------------------------------------- no live read

    def test_snapshot_backed_order_never_reads_the_address_master(self):
        """Proof by observation, not by inspection.

        Deleting the linked Address entirely must not change the rendered
        order. If any live read remained, this would fail or blank out.
        """

        address = self.make_address("Doomed Address", "9 Vanishing Lane")
        order = self.make_order(billing=address,
                                billing_snapshot="9 Vanishing Lane<br>Testville")

        before = self.detail(order)["billing_address_display"]

        frappe.delete_doc("Address", address, force=True, ignore_permissions=True)
        frappe.clear_cache()

        self.assertEqual(self.detail(order)["billing_address_display"], before,
                         "order detail still depends on the Address master")

    # ----------------------------------------------------------- shape

    def test_display_fields_are_always_string_or_none(self):
        """Stable types: never an object, in any branch."""

        order = self.make_order(billing_snapshot="Only Billing<br>Testville")
        data = self.detail(order)

        self.assertIsInstance(data["billing_address_display"], str)
        self.assertIsNone(data["shipping_address_display"],
                          "a blank snapshot with no link must be None, not {}")

        # And the removed live-object fields must stay removed.
        self.assertNotIn("billing_address", data,
                         "the mutable live Address object came back")
        self.assertNotIn("shipping_address", data)

        # Identifiers remain available and distinct from display.
        self.assertIn("billing_address_name", data)
        self.assertIn("shipping_address_name", data)

    def test_display_is_plain_text_not_html(self):
        """The frontend must never need [innerHTML] for an address."""

        order = self.make_order(
            billing_snapshot="Line One<br>Line Two<br>\nLine Three<br>")

        display = self.detail(order)["billing_address_display"]

        self.assertNotIn("<", display, "HTML leaked into the display field")
        self.assertEqual(display.splitlines(),
                         ["Line One", "Line Two", "Line Three"],
                         "line breaks must be preserved without blank lines")

    # ----------------------------------------------------------- legacy

    def test_legacy_blank_snapshot_falls_back_to_the_current_master(self):
        """Best effort for orders predating the snapshot -- still a string."""

        address = self.make_address("Legacy Billing", "77 Legacy Way")
        order = self.make_order(billing=address, billing_snapshot=None)

        display = self.detail(order)["billing_address_display"]

        self.assertIsInstance(display, str)
        self.assertIn("77 Legacy Way", display)
        self.assertNotIn("<", display)

    def test_legacy_blank_snapshot_and_missing_address_is_safe(self):
        """A deleted legacy Address must not fail the whole order detail."""

        address = self.make_address("Legacy Gone", "0 Nowhere")
        order = self.make_order(billing=address, billing_snapshot=None)

        frappe.delete_doc("Address", address, force=True, ignore_permissions=True)
        frappe.clear_cache()

        data = self.detail(order)          # must not raise

        self.assertIsNone(data["billing_address_display"])
        self.assertEqual(data["name"], order, "the rest of the order still renders")


class OrderListCurrencyCase(unittest.TestCase):
    """Each order row carries its OWN currency, not the store default."""

    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data on the test site")

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.db.savepoint("order_currency")
        self.customer = frappe.get_doc("Customer", CUSTOMER)

    def tearDown(self):
        frappe.db.rollback(save_point="order_currency")
        frappe.clear_cache()

    def test_every_order_row_carries_its_own_currency(self):
        """The client must never infer order currency from configuration."""

        from unittest.mock import patch

        from yob_storefront.api import order as order_api

        with patch.object(order_api, "get_storefront_customer",
                          return_value=self.customer):
            response = inspect.unwrap(order_api.get_orders)(auth_context={})

        rows = response["data"]

        if not rows:
            self.skipTest("no orders for the demo buyer on this site")

        for row in rows:
            self.assertIn("currency", row,
                          "an order row has no currency; the client would have "
                          "to guess it from environment config")
            self.assertTrue(row["currency"], "currency must not be blank")
            self.assertEqual(
                row["currency"],
                frappe.db.get_value("Sales Order", row["name"], "currency"),
                "the row currency must be the order's OWN stored currency, "
                "never substituted or calculated")


if __name__ == "__main__":
    unittest.main()
