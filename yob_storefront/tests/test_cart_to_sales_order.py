# Copyright (c) 2026, YOB and Shayona
"""Gate 2 — Cart → Draft Sales Order financial parity.

The payment lifecycle commits a Cart into a Draft Sales Order and then collects
against it. That is only safe if the Sales Order reproduces the Cart's
commercial obligation *by recalculating it*, never by copying totals across.
These tests pin that.

Hard invariant, asserted in every successful conversion:

    cart.grand_total == so.grand_total
    cart.currency    == so.currency

Discount comparison uses SEMANTIC equivalents, not same-named fields:

    cart.total_discount   <-> sum(so.items[].discount_amount * qty)   line level
    cart.coupon_discount  <-> so.discount_amount                      transaction level
    cart.tax_total        <-> so.total_taxes_and_charges

Comparing ``cart.total_discount`` to ``so.discount_amount`` looks like a
mismatch and is simply the wrong pairing.

Every test creates only the records it needs and rolls back to a savepoint, so
the site is left exactly as found. Explicit rollback matters beyond tidiness:
``process_payment`` will later CATCH India Compliance validation errors and
return an API envelope, so request-end auto-rollback is not sufficient.
"""

import unittest

import frappe

CUSTOMER = "YOB Demo Buyer"
ITEM = "YOB-BOLT-M10"          # 12.50 list; PRLE-0001 gives 10% at qty >= 10
GSTIN = "24ABCDE1234F1Z6"      # checksum-valid Gujarat test value


def _seeded() -> bool:
    return bool(frappe.db.exists("Customer", CUSTOMER) and frappe.db.exists("Item", ITEM))


def _money(a, b) -> bool:
    """Currency comparison at Frappe's practical precision."""
    return abs(float(a or 0) - float(b or 0)) < 0.005


def _line_discount(so) -> float:
    """Sales Order line-level discount, the equivalent of cart.total_discount.

    ERPNext stores discount_amount PER UNIT on the row, so it is multiplied by
    qty to reach the same figure the Cart accumulates.
    """
    return round(sum(float(r.discount_amount or 0) * float(r.qty or 0) for r in so.items), 2)


class CartToSalesOrderCase(unittest.TestCase):
    """Base: seeded prerequisites, savepoint isolation, shared assertions."""

    @classmethod
    def setUpClass(cls):
        if not _seeded():
            raise unittest.SkipTest("requires seed_demo_data on the test site")

    def setUp(self):
        frappe.set_user("Administrator")
        self.so_before = frappe.db.count("Sales Order")
        frappe.db.savepoint("gate2")

    def tearDown(self):
        frappe.db.rollback(save_point="gate2")
        # Rollback restores the DB but NOT Frappe's document cache. A test that
        # mutates a cached doc (Customer.disabled, tax_category) would otherwise
        # leak a stale value into the next test in the same connection.
        frappe.clear_cache()

    # ----------------------------------------------------------- helpers

    def build_cart(self, qty=12, billing_address=None):
        """Cart built through the REAL pricing path, never fabricated values."""

        from yob_storefront.api.cart import get_or_create_cart
        from yob_storefront.services.cart_service import reprice_cart

        customer = frappe.get_doc("Customer", CUSTOMER)
        cart = get_or_create_cart(customer)
        cart.set("items", [])
        cart.append("items", {"item_code": ITEM, "quantity": qty,
                              "uom": "Nos", "stock_uom": "Nos"})
        if billing_address:
            cart.billing_address = billing_address
        reprice_cart(cart, customer)
        cart.save(ignore_permissions=True)
        return cart

    def convert(self, cart):
        from yob_storefront.services.order_service import create_sales_order_from_cart
        return create_sales_order_from_cart(cart)

    def assert_parity(self, cart, so, expect_tax=None):
        """The hard invariant plus semantically-paired breakdown."""

        self.assertTrue(_money(cart.grand_total, so.grand_total),
                        f"grand_total Cart={cart.grand_total} SO={so.grand_total}")
        self.assertEqual(cart.currency, so.currency)
        self.assertEqual(so.docstatus, 0, "commitment must leave the SO Draft")

        self.assertTrue(_money(cart.net_total, so.net_total),
                        f"net_total Cart={cart.net_total} SO={so.net_total}")
        self.assertTrue(_money(cart.tax_total, so.total_taxes_and_charges),
                        f"tax Cart={cart.tax_total} SO={so.total_taxes_and_charges}")
        self.assertTrue(_money(cart.total_discount, _line_discount(so)),
                        f"line discount Cart={cart.total_discount} SO={_line_discount(so)}")
        self.assertTrue(_money(cart.coupon_discount, so.discount_amount),
                        "transaction-level discount differs")

        if expect_tax == "nonzero":
            self.assertGreater(float(cart.tax_total), 0, "not a taxable case")

    def make_tax_fixture(self, rate=18.0):
        """Tax Category -> Template -> Tax Rule -> Customer. Savepoint-scoped."""

        company = frappe.db.get_value("Company", {}, "name")
        account = frappe.db.get_value(
            "Account", {"company": company, "account_type": "Tax", "is_group": 0}, "name")
        self.assertTrue(account, "no tax account on this site")

        category = frappe.get_doc({"doctype": "Tax Category",
                                   "title": "_Gate2 TC"}).insert(ignore_permissions=True)
        template = frappe.get_doc({
            "doctype": "Sales Taxes and Charges Template",
            "title": "_Gate2 Tax", "company": company, "tax_category": category.name,
            "taxes": [{"charge_type": "On Net Total", "account_head": account,
                       "description": f"Tax {rate}%", "rate": rate}],
        }).insert(ignore_permissions=True)
        frappe.get_doc({"doctype": "Tax Rule", "tax_type": "Sales",
                        "tax_category": category.name, "sales_tax_template": template.name,
                        "company": company}).insert(ignore_permissions=True)
        frappe.db.set_value("Customer", CUSTOMER, "tax_category", category.name)
        frappe.clear_cache()
        return category.name

    def make_address(self, title, country="India", gst_category=None, gstin=None):
        doc = frappe.get_doc({
            "doctype": "Address", "address_title": title, "address_type": "Billing",
            "address_line1": "1 Test Street", "city": "Ahmedabad",
            "state": "Gujarat", "pincode": "382445", "country": country,
            "links": [{"link_doctype": "Customer", "link_name": CUSTOMER}],
        })
        if gst_category:
            doc.gst_category = gst_category
        if gstin:
            doc.gstin = gstin
        doc.insert(ignore_permissions=True)
        return doc.name


class BaselineCase(CartToSalesOrderCase):
    def test_baseline_cart_converts_with_parity(self):
        """No tax, no discount threshold: the simplest obligation."""

        cart = self.build_cart(qty=2)          # below the qty>=10 rule
        so = self.convert(cart)

        self.assertEqual(len(so.items), len(cart.items))
        self.assertTrue(_money(so.items[0].qty, cart.items[0].quantity))
        self.assert_parity(cart, so)


class DiscountedCase(CartToSalesOrderCase):
    def test_pricing_rule_discount_reproduced_by_sales_order(self):
        """qty>=10 triggers the seeded 10% Item Code rule on BOTH sides."""

        cart = self.build_cart(qty=12)
        self.assertGreater(float(cart.total_discount), 0, "rule did not apply to the Cart")

        so = self.convert(cart)
        self.assert_parity(cart, so)
        self.assertGreater(_line_discount(so), 0, "rule did not apply to the Sales Order")

    def test_pricing_rule_apply_on_mirrors_the_rule(self):
        cart = self.build_cart(qty=12)
        row = cart.items[0]
        if row.pricing_rule_apply_on:
            self.assertEqual(row.pricing_rule_apply_on, "Item Code")


class TaxableCase(CartToSalesOrderCase):
    def test_non_zero_tax_parity(self):
        """18% on net: proves the SO derives its own template and agrees."""

        self.make_tax_fixture(rate=18.0)
        cart = self.build_cart(qty=12)
        so = self.convert(cart)

        self.assert_parity(cart, so, expect_tax="nonzero")
        self.assertTrue(_money(so.total_taxes_and_charges, float(cart.net_total) * 0.18))


class GSTValidCase(CartToSalesOrderCase):
    def test_valid_india_gst_cart_commits(self):
        """India Compliance validation must ACCEPT the Draft Sales Order."""

        self.make_tax_fixture(rate=18.0)
        address = self.make_address("_Gate2 Valid GST", country="India",
                                    gst_category="Registered Regular", gstin=GSTIN)
        cart = self.build_cart(qty=12, billing_address=address)
        so = self.convert(cart)          # india_compliance validate() runs here

        self.assert_parity(cart, so)
        self.assertEqual(so.docstatus, 0)


class ConversionRollbackCase(CartToSalesOrderCase):
    def test_conversion_time_validation_rolls_back_completely(self):
        """A caught validation error must leave NOTHING behind.

        process_payment will catch this and return an API envelope, so the
        rollback cannot rely on request-end behaviour.
        """

        # The trigger must fire during the Cart -> Sales Order CONVERSION,
        # because that is where process_payment will meet it and catch it.
        #
        # The address country/GST-Category rule is NOT usable here: india_compliance
        # enforces it on Address.insert(), and the Sales Order hooks do not
        # re-check it -- verified by probing the installed version. Corrupting the
        # address below the ORM therefore produces no conversion-time failure.
        #
        # HSN is NOT usable either: india_compliance calls validate_hsn_codes(doc)
        # with throw=False, so a missing HSN only warns. Verified at runtime.
        #
        # A disabled Customer IS a hard transaction-time validation -- ERPNext's
        # party validation raises during Sales Order insert. It is disabled below
        # the ORM so the failure lands in the conversion, not in fixture setup.
        cart = self.build_cart(qty=12)
        cart_name = cart.name

        frappe.db.set_value("Customer", CUSTOMER, "disabled", 1, update_modified=False)
        frappe.clear_cache()

        frappe.db.savepoint("commit_attempt")
        raised = None
        try:
            cart.reload()
            self.convert(cart)
        except Exception as exc:            # noqa: BLE001 - classified below
            raised = exc
            frappe.db.rollback(save_point="commit_attempt")

        self.assertIsNotNone(raised, "expected a transaction-time validation failure")
        self.assertIsInstance(raised, frappe.ValidationError)

        self.assertEqual(frappe.db.count("Sales Order"), self.so_before,
                         "a Sales Order survived a failed commitment")
        self.assertEqual(frappe.db.get_value("Cart", cart_name, "status"), "Draft")
        self.assertFalse(frappe.db.get_value("Cart", cart_name, "sales_order"))
