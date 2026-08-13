# YOB API — Request Examples

Concrete payloads for all 33 endpoints. Values are taken from the
`seed_demo_data` fixtures, so they work as-is on a seeded test site.

## Transport

```
POST|GET  {base}/api/method/<dotted.path>
Content-Type: application/json
Cookie: sid=<from login>                     # all authenticated calls
X-Frappe-CSRF-Token: <data.csrf_token>       # all POST calls
```

`GET` endpoints take their parameters as **query string**; `POST` endpoints take
a **JSON body**. Authenticated calls must reach Frappe through the edge proxy
that sets `X-YOB-Original-Host`, or they fail with `application_access_denied`.

**Never send `customer`, `auth_context`, or any profile identifier as
authorization.** The server resolves the Customer from the session. Where a
`customer` field is accepted it is compared for equality and discarded.

---

## Authentication — `yob_auth`

```jsonc
// POST yob_auth.api.auth.login_with_password        (guest, no CSRF on first login)
{"application": "STOREFRONT", "username": "storefront@yob.test", "password": "Storefront@123"}

// POST yob_auth.api.auth.request_otp                (guest)
{"application": "STOREFRONT", "identifier": "storefront@yob.test", "method": "email_otp"}
//   method: "email_otp" | "mobile_otp"

// POST yob_auth.api.auth.login_with_otp             (guest)
{"challenge_id": "<from request_otp>", "otp": "123456"}

// GET  yob_auth.api.auth.get_session_context?application=STOREFRONT
//   returns a fresh csrf_token -- use this instead of re-logging in

// POST yob_auth.api.auth.logout                     (no body)
{}
```

Login returns `data.csrf_token`. Send it on every POST from then on — including
a **repeat login on the same session**, which is rejected without it.

---

## Catalog — GET, read-only

```
GET yob_storefront.api.catalog.get_categories
GET yob_storefront.api.catalog.get_categories?parent_slug=industrial-supplies
GET yob_storefront.api.catalog.get_category?slug=fasteners&qty=10
GET yob_storefront.api.catalog.get_item?slug=hex-bolt-m10-50&qty=10
GET yob_storefront.api.cms.get_config
```

`qty` drives quantity-based pricing rules — `qty=10` triggers the seeded 10%
bulk discount, `qty=1` does not.

---

## Cart — POST, CSRF required

```jsonc
// add_to_cart -- `qty` is a DELTA, added to any existing quantity
{"item_code": "YOB-BOLT-M10", "qty": 12}
//   Sending this twice leaves 24, not 12. One row per item is kept, never a
//   second row for the same item_code -- ERPNext evaluates a Pricing Rule's
//   min_qty against the ROW quantity, so 4 + 4 + 3 on one row correctly
//   triggers a min_qty=10 rule that three separate rows would miss.
//
//   NOT idempotent: guard against double-submits client-side. There is no
//   absolute-set path today, and qty <= 0 is rejected with quantity_invalid,
//   so a delta cannot be negative.

// remove_from_cart
{"item_code": "YOB-BOLT-M10"}

// clear_cart / remove_coupon   (no body)
{}

// apply_coupon
{"code": "DEMO10"}
//   seeded: DEMO10 (valid) | EXPIRED10 | USEDUP10 (limit reached)

// set_cart_contact
{"contact_person": "Demo Buyer-YOB Demo Buyer"}

// set_cart_billing_address
{"billing_address": "YOB Demo Billing-Billing"}

// set_cart_shipping_address
{"shipping_address": "YOB Demo Shipping-Shipping"}
```

```
GET yob_storefront.api.cart.get_cart
```

Never send `rate`, `amount`, `discount_percentage` or any total. Those are
server-computed; a client-supplied price is ignored.

---

## Addresses & contacts

```
GET yob_storefront.api.address.get_addresses
GET yob_storefront.api.address.get_contacts
```

`add_address`, `update_address` and `update_contact` read the **whole
`form_dict`**, not named arguments — send every field you want set:

```jsonc
// POST add_address
{
  "address_title": "Head Office", "address_type": "Billing",
  "address_line1": "Plot 42, GIDC Industrial Estate", "address_line2": "Phase II",
  "city": "Ahmedabad", "state": "Gujarat", "country": "India", "pincode": "382445",
  "email": "buyer@example.com", "phone": "+91 98250 00000",
  "is_primary_address": 1, "is_shipping_address": 0, "disabled": 0,
  "gstin": "24ABCDE1234F1Z6", "gst_category": "Registered Regular"
}
//   state + pincode are REQUIRED for Indian addresses (india_compliance)
//   gstin check digit is validated -- an invented value is rejected

// POST update_address   -- same fields, plus the required record name
{"name": "YOB Demo Billing-Billing", "city": "Surat"}

// POST update_contact
{"name": "Demo Buyer-YOB Demo Buyer", "first_name": "Demo", "last_name": "Buyer",
 "salutation": "Mr", "designation": "Purchase Manager",
 "email": "buyer@example.com", "phone": "+91 98250 00000"}

// POST delete_address / delete_contact
{"name": "YOB Demo Billing-Billing"}
```

---

## Orders

```
GET yob_storefront.api.order.get_orders
GET yob_storefront.api.order.get_order_details?order_id=SAL-ORD-2026-00001
```

Only the authenticated Customer's own orders are reachable; an unowned
`order_id` answers `order_not_found`, identical to a non-existent one.

---

## Checkout & payment

```jsonc
// POST yob_storefront.api.checkout.proceed_to_payment   (no body)
{}
//   -> data.token, data.payment_url, data.payment_request

// GET  yob_storefront.api.payment_method.get_payment_methods
//        ?company=Shayona%20Technology&order_amount=500
//   customer is accepted for compatibility only and is discarded

// GET  yob_storefront.api.payment.get_checkout_data?token=<checkout token>   (GUEST)

// POST yob_storefront.api.payment.process_payment                            (GUEST)
{"token": "<checkout token>", "payment_method": "Razorpay"}
//   payment_method is the Payment Method NAME; dispatch is by method_code
//   ("paylater" | "razorpay")

// POST yob_storefront.api.payment.verify_payment                             (GUEST)
{"razorpay_order_id": "order_Nabc123XYZ",
 "razorpay_payment_id": "pay_Ndef456UVW",
 "razorpay_signature": "<hmac-sha256 returned by Razorpay checkout>"}
```

The three guest endpoints carry **no session**. They are authorised by the
32-byte checkout token or Razorpay's HMAC, validated before any lookup or
mutation. Treat the token as a bearer credential.

---

## Desk-internal — not for the SPA

```
GET yob_storefront.api.address.get_contact_for_customer?customer=YOB%20Demo%20Buyer
```

Guarded by Frappe DocType permissions, not storefront application access, and it
returns a **plain value, not an envelope**. Storefront buyers cannot call it.

---

## Response shape

```jsonc
// success
{"message": {"data": {...}, "notice": "Cart loaded", "meta": {"count": 3}}}

// failure
{"message": {"errors": [{"code": "category_not_found",
                         "detail": "Category not found.", "field": "slug"}]}}
```

No `success` boolean. Branch on HTTP status, then `errors[0].code`.
