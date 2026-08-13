# Contract: Storefront API Compatibility Inventory

Status: Historical baseline from the supplied archive; verify against current
source and SPA before release.

All approved current routes use the response contract in `api-response.md`.
Request parameters and HTTP methods remain stable during the architecture
refactor.

## Protected storefront routes

Each requires application code `STOREFRONT`; rows marked Customer also require
`profile_doctype="Customer"` and use `auth_context.profile_name`.

| Endpoint | Key parameters | Profile |
| --- | --- | --- |
| `yob_storefront.api.cart.get_cart` | none | Customer |
| `yob_storefront.api.cart.add_to_cart` | `item_code`, `qty` (**delta**) | Customer |
| `yob_storefront.api.cart.remove_from_cart` | `item_code` | Customer |
| `yob_storefront.api.cart.clear_cart` | none | Customer |
| `yob_storefront.api.cart.set_cart_contact` | `contact_person` | Customer |
| `yob_storefront.api.cart.set_cart_billing_address` | `billing_address` | Customer |
| `yob_storefront.api.cart.set_cart_shipping_address` | `shipping_address` | Customer |
| `yob_storefront.api.cart.apply_coupon` | `code` | Customer |
| `yob_storefront.api.cart.remove_coupon` | none | Customer |
| `yob_storefront.api.catalog.get_categories` | `parent_slug` | Customer |
| `yob_storefront.api.catalog.get_category` | `slug`, `qty` | Customer |
| `yob_storefront.api.catalog.get_item` | `slug`, `qty` | Customer |
| `yob_storefront.api.checkout.proceed_to_payment` | existing contract | Customer |
| `yob_storefront.api.order.get_orders` | existing contract | Customer |
| `yob_storefront.api.order.get_order_details` | `order_id` | Customer |
| `yob_storefront.api.address.get_contacts` | none | Customer |
| `yob_storefront.api.address.update_contact` | existing form contract | Customer |
| `yob_storefront.api.address.delete_contact` | `name` | Customer |
| `yob_storefront.api.address.get_addresses` | none | Customer |
| `yob_storefront.api.address.add_address` | existing form contract | Customer |
| `yob_storefront.api.address.update_address` | existing form contract | Customer |
| `yob_storefront.api.address.delete_address` | `name` | Customer |
| `yob_storefront.api.cms.get_config` | none | None unless current source says otherwise |
| `yob_storefront.api.payment_method.get_payment_methods` | legacy `customer`, `company` retained only for compatibility | Customer |

`get_payment_methods` must compare any supplied Customer to the trusted context,
reject mismatch, then use the context value. The request value is never
authorization.

## Explicit guest routes

| Endpoint | Capability guard |
| --- | --- |
| `yob_storefront.api.payment.get_checkout_data` | Unexpired checkout token |
| `yob_storefront.api.payment.process_payment` | Unexpired checkout token plus idempotency |
| `yob_storefront.api.payment.verify_payment` | Verified Razorpay signature plus idempotency |

Customer is derived through trusted server-side resource links, not the request.

## Desk-internal route

`yob_storefront.api.address.get_contact_for_customer` uses normal Frappe
Customer read permission and the core response boundary. It does not use
storefront application access.

## Auth routes replacing legacy storefront auth

| Purpose | Route |
| --- | --- |
| Password login | `yob_auth.api.auth.login_with_password` |
| Session context | `yob_auth.api.auth.get_session_context` |
| Logout | `yob_auth.api.auth.logout` |
| OTP | Current `request_otp` / `login_with_otp` dotted paths must be verified |

## Routes that must remain removed

- `yob.api.user_api.users`
- old `yob.api.auth.*` replacements
- generic `yob.api.frontend.user`, `.profile`, `.config`, and `.category`
  scaffolding unless a new secured contract is approved.

No `yob.*` forwarding aliases were approved in the archive. Confirm the client
migration is complete before deleting any currently deployed compatibility
route.

## Known client/server mismatches requiring confirmation

The archive reported SPA calls to `cart.update_cart_item`,
`address.add_contact`, and `checkout.create_checkout` without server methods.
It also reported missing Desk fixture calls. These are open items, not approved
API requirements.
