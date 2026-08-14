# Authorization Matrix

Every frontend-relevant endpoint and exactly what authorises it.

## Classifications

| Class | Meaning |
|---|---|
| **AUTHENTICATED STOREFRONT** | needs the `sid` cookie; non-GET also needs `X-Frappe-CSRF-Token` |
| **PUBLIC TOKEN** | `allow_guest`; authorised by the checkout token, **no session** |
| **PUBLIC PROVIDER CALLBACK** | `allow_guest`; authorised by Razorpay's HMAC signature, **no session, no token** |
| **PUBLIC AUTH** | `allow_guest`; the login endpoints themselves |
| **INTERNAL ONLY** | server-side; never called by the browser |

---

## The matrix

| Domain | Endpoint | HTTP | Session | CSRF | Token | Class |
|---|---|---|---|---|---|---|
| Auth | `yob_auth.api.auth.login_with_password` | POST | – | – | – | PUBLIC AUTH |
| Auth | `yob_auth.api.auth.request_otp` | POST | – | – | – | PUBLIC AUTH |
| Auth | `yob_auth.api.auth.login_with_otp` | POST | – | – | – | PUBLIC AUTH |
| Auth | `yob_auth.api.auth.get_session_context` | GET | ✅ | – | – | AUTHENTICATED |
| Auth | `yob_auth.api.auth.logout` | POST | ✅ | ✅ | – | AUTHENTICATED |
| Catalog | `catalog.get_categories` | GET | ✅ | – | – | AUTHENTICATED |
| Catalog | `catalog.get_category` | GET | ✅ | – | – | AUTHENTICATED |
| Catalog | `catalog.get_item` | GET | ✅ | – | – | AUTHENTICATED |
| CMS | `cms.get_config` | GET | ✅ | – | – | AUTHENTICATED |
| Cart | `cart.get_cart` | GET | ✅ | – | – | AUTHENTICATED |
| Cart | `cart.add_to_cart` | POST | ✅ | ✅ | – | AUTHENTICATED |
| Cart | `cart.remove_from_cart` | POST | ✅ | ✅ | – | AUTHENTICATED |
| Cart | `cart.clear_cart` | POST | ✅ | ✅ | – | AUTHENTICATED |
| Cart | `cart.apply_coupon` | POST | ✅ | ✅ | – | AUTHENTICATED |
| Cart | `cart.remove_coupon` | POST | ✅ | ✅ | – | AUTHENTICATED |
| Cart | `cart.set_cart_contact` | POST | ✅ | ✅ | – | AUTHENTICATED |
| Cart | `cart.set_cart_billing_address` | POST | ✅ | ✅ | – | AUTHENTICATED |
| Cart | `cart.set_cart_shipping_address` | POST | ✅ | ✅ | – | AUTHENTICATED |
| Address | `address.get_addresses` | GET | ✅ | – | – | AUTHENTICATED |
| Address | `address.get_contacts` | GET | ✅ | – | – | AUTHENTICATED |
| Address | `address.add_address` | POST | ✅ | ✅ | – | AUTHENTICATED |
| Address | `address.add_contact` | POST | ✅ | ✅ | – | AUTHENTICATED |
| Address | `address.update_address` | POST | ✅ | ✅ | – | AUTHENTICATED |
| Address | `address.update_contact` | POST | ✅ | ✅ | – | AUTHENTICATED |
| Address | `address.delete_address` | POST | ✅ | ✅ | – | AUTHENTICATED |
| Address | `address.delete_contact` | POST | ✅ | ✅ | – | AUTHENTICATED |
| Address | `address.get_contact_for_customer` | GET | ✅ | – | – | **INTERNAL ONLY** — Desk Client Script |
| Checkout | `checkout.proceed_to_payment` | POST | ✅ | ✅ | – | AUTHENTICATED |
| Payment | `payment_method.get_payment_methods` | GET | ✅ | – | – | AUTHENTICATED |
| **Payment** | **`payment.get_checkout_data`** | **GET** | **❌** | **❌** | **✅** | **PUBLIC TOKEN** |
| **Payment** | **`payment.process_payment`** | **POST** | **❌** | **❌** | **✅** | **PUBLIC TOKEN** |
| **Payment** | **`payment.verify_payment`** | **POST** | **❌** | **❌** | **❌** | **PUBLIC PROVIDER CALLBACK** |
| Orders | `order.get_orders` | GET | ✅ | – | – | AUTHENTICATED |
| Orders | `order.get_order_details` | GET | ✅ | – | – | AUTHENTICATED |

**Totals:** 34 endpoints — 28 authenticated, 6 public (3 auth, 2 public-token,
1 provider callback). One authenticated endpoint (`get_contact_for_customer`)
is internal Desk tooling and is not part of the browser contract.

---

## The rule you must not get wrong

> ### The public payment page must remain accessible without `sid`.

`/payment/<token>` is intentionally public. A payer may reach it from a
Checkout redirect, a shared link, an email or WhatsApp message, or an incognito
window with no storefront login whatsoever.

**Verified in a real browser:** a completed Razorpay Test Mode payment through a
direct link in a private window with no session.

If you place the payment route behind a session guard, every one of those
journeys breaks — and the failure will look like an auth bug rather than a
routing decision.

```
Router
├── /catalog, /cart, /checkout   →  session guard    ✅
└── /payment/:token              →  NO guard         ✅  ← public
```

`verify_payment` is even more open: it takes **no token**, because Razorpay's
HMAC signature is the credential. It must be reachable from the Checkout
callback with no session and no token.

---

## What the token is, and is not

The checkout token is a **capability for one payment flow**, not a session.

Holding a valid token lets the bearer:

- read that one Payment Request's payment page;
- initiate payment for that one obligation.

It does **not** grant:

- any Frappe/ERPNext DocType access;
- `/api/resource/Item`, `/api/resource/Customer`, Sales Order or Payment Request
  APIs;
- any authenticated storefront endpoint;
- anything about another Payment Request.

Verified by test both **before and after** a successful payment: Guest still has
no read permission on Customer, Address, Contact, Item or Sales Order.

---

## Internal execution identity — not a frontend concern

The backend briefly runs internal ERPNext work as a dedicated, **login-disabled**
service identity so that ERPNext's nested permission checks succeed without ever
granting Guest anything.

**This is entirely server-side implementation detail.**

- The browser never authenticates as it.
- It has no credentials the frontend could use.
- It must **never** appear in frontend code, config, mocks, docs or DTOs.
- Its permissions are not a frontend requirement.

It is mentioned here only so nobody encountering it in a backend log mistakes it
for something the client is supposed to send.
