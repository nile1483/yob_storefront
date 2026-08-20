# YOB Storefront Architecture and Flows

Status: Existing-architecture baseline. The flows below describe how the app
behaves today; verify current source before changing any of them. Sections
marked **Archive evidence** were carried from the pre-split `yob` app and have
not been re-verified line by line.

Companion documents: [`context.md`](context.md) for ownership and compatibility,
[`doctypes.md`](doctypes.md) for the DocType inventory and navigation policy,
[`security.md`](security.md) for the threat model, and
[`contracts/`](contracts/) for the API and error contracts. Platform-wide rules
live in [`../../yob_core/docs/platform/`](../../yob_core/docs/platform/).

## Scope

This document explains `yob_storefront` at an MVP level. It is a Frappe/ERPNext
B2B commerce solution app that exposes APIs for catalog browsing,
customer-specific cart pricing, checkout, payment method selection, Razorpay
payment verification, Pay Later ordering, address/contact management, and order
history.

The app declares `yob_core`, `yob_auth`, `erpnext`, `payments`, and
`india_compliance` in `required_apps`. It reuses ERPNext masters such as
Customer, Contact, Address, Item, Item Price, Pricing Rule, Coupon Code, Sales
Order, Payment Request, Razorpay Settings, Currency, Company, Price List,
Warehouse, and Web Page.

Identity, sessions, and application access are owned by `yob_auth`; the response
envelope and API error boundary are owned by `yob_core`. This app implements
none of them.

## App Entry Points

### Frappe Hooks

File: `yob_storefront/hooks.py`

- Registers the app as `yob_storefront` with title `YOB Storefront`.
- Adds one Apps Page entry (`add_to_apps_screen`) opening the Storefront
  Workspace.
- Loads desk assets:
  - CSS: `/assets/yob_storefront/css/yob.css`
  - JS: `/assets/yob_storefront/js/yob.js`
- Requires installed apps:
  - `yob_core`
  - `yob_auth`
  - `erpnext`
  - `payments`
  - `india_compliance`
- Registers document events:
  - `File`: make Item and Category files public after insert/update.
  - `YOB Store Settings`: clear store config cache on update.
  - `Customer`, `Contact`: clear customer and pricing cache.
  - `Item`: clear item and pricing cache.
  - `Pricing Rule`, `Price List`, `Item Price`, `Item Group`, `Customer Group`, `Territory`: clear pricing cache.

## Main Data Model

### YOB Store Settings

Single settings DocType used as the store-level configuration source.

Important fields:

- `company`
- `store_name`
- `store_domain`
- `store_logo`
- `default_currency`
- `default_price_list`
- `default_warehouse`
- `allow_guest_purchase`
- `default_terms_page`
- `default_privacy_page`
- `allowed_payment_methods`
- `cart_expiry`

### Category

Tree DocType used for storefront category navigation.

Important fields:

- `category_name`
- `slug`
- `parent_category`
- `is_group`
- `is_active`
- `thumbnail`
- `banner`
- `display_order`
- `meta_title`
- `meta_description`
- `description`

### Cart

Stores the active buying session for a customer.

Important fields:

- `customer`
- `user`
- `company`
- `currency`
- `selling_price_list`
- `company_address`
- `status`: `Draft` or `Ordered`
- `items`
- `coupon_code`
- `contact_person`
- `billing_address`
- `shipping_address`
- `is_shippable`
- `total_quantity`
- `net_total`
- `tax_total`
- `grand_total`
- `coupon_discount`
- `total_discount`

### Cart Item

Child table of Cart.

Important fields:

- `item_code`
- `item_name`
- `image`
- `quantity`
- `uom`
- `stock_uom`
- `conversion_factor`
- `base_price`
- `rate`
- `amount`
- `discount_percentage`
- `discount_amount`
- `line_discount`
- `tax_amount`
- `total_amount`
- `item_slug`
- `pricing_rules`
- `pricing_rule_label`
- `pricing_rule_apply_on`

### Payment Method

Defines payment methods visible to checkout.

Important fields:

- `payment_method_name`
- `method_code`
- `payment_type`: `Online` or `Offline`
- `is_active`
- `display_order`
- `icon`
- `description`

### Payment Method Assignment

Controls which payment method is available for which customer/company/customer group and amount range.

Important fields:

- `payment_method`
- `reference_doctype`: `Company`, `Customer Group`, or `Customer`
- `reference_name`
- `is_active`
- `minimum_order_amount`
- `maximum_order_amount`

### Razorpay Payment Log

Stores gateway response and payment metadata after Razorpay verification.

Important fields:

- `razorpay_order_id`
- `razorpay_payment_id`
- `razorpay_signature`
- `payment_status`
- `payment_method`
- `payment_amount`
- `currency`
- `customer`
- `email`
- `contact`
- `reference_doctype`
- `reference_name`
- `payment_request`
- `gateway_response`

## Authentication And Customer Resolution

File: `yob_storefront/utils/context.py`

The app is private B2B by design. It implements **no** authentication itself.

Flow (verified against source, CHG-001 F-10):

1. `yob_auth.api.auth.login_with_password` (or `login_with_otp`) authenticates and
   creates the standard Frappe `sid` session. Storefront is not involved.
2. Every protected endpoint is wrapped in
   `require_application(STOREFRONT_APP, profile_doctype="Customer")`, which strips
   any caller-supplied `auth_context` and injects a server-built one.
3. `get_storefront_customer(auth_context)` in `utils/context.py` is a thin
   adapter: it rejects a missing context, requires `profile_doctype == "Customer"`
   with a non-empty `profile_name`, and returns that Customer document.
4. A client-supplied `customer` is only ever compared for equality
   (`assert_customer_matches`) and then discarded. It never authorizes anything.

> **Superseded:** earlier revisions of this document described an
> `Authorization: Bearer <token>` header resolved through the Frappe cache, with
> `require_login()` / `require_customer()` setting the Frappe user. **That flow no
> longer exists.** `CacheKey` has no `auth_token` member, and
> `tests/test_rename.py` fails the build if `get_user_from_token`,
> `require_login`, `require_customer`, `frappe.set_user` or `check_password`
> reappear anywhere in this app. Identity is session + `auth_context` only.

## Store Configuration Flow

File: `yob_storefront/api/cms.py`

Endpoint:

- `get_config()`

Flow:

1. Reads cached store config using `STORE_CONFIG_CACHE`.
2. If no cache exists, reads `YOB Store Settings`.
3. Converts store logo to a full URL.
4. Returns company, store name, domain, currency, price list, warehouse, policy pages, guest purchase flag, and configured payment modes.

`default_warehouse` is echoed here and **nowhere else**: no pricing, cart, order
or availability path reads it. Warehouse is resolved by ERPNext per transaction
(see `docs/context.md`, "Warehouse and transaction context"). The field remains in
the response for compatibility only.
5. Caches the response for one hour.

## Catalog Flow

File: `yob_storefront/api/catalog.py`

### Get Categories

Endpoint:

- `get_categories(parent_slug=None)`

Flow:

1. Requires logged-in Customer.
2. If `parent_slug` is passed, finds that Category and returns active children.
3. If no parent is passed, returns active root categories.
4. Orders by `display_order`.
5. Converts thumbnail and banner paths into full URLs.

### Get Category

Endpoint:

- `get_category(slug)`

Flow:

1. Requires logged-in Customer.
2. Loads active Category by slug.
3. If the category is a group, returns active child categories.
4. Returns category metadata. **No products.**

Steps 4-5 previously loaded every Item in a leaf category and called
`get_item_pricing()` for each one. That embedded product payload was retired in
Phase 22B-3 — along with the `items` field, `meta.item_count` and the `qty`
parameter, which existed only to price it. All product listing now belongs to
`get_items(scope_type, scope_value, search, sort, page_size, cursor, qty)`, which is
bounded, cursor-paginated and isolates per-item failures. See `docs/context.md`.

### Get Single Item

Endpoint:

- `get_item(slug, qty=1)`

Flow:

1. Requires logged-in Customer.
2. Loads Item by `custom_slug`.
3. Reads store currency from `YOB Store Settings`.
4. Calls `get_item_pricing()` for exact ERPNext-calculated pricing.
5. Calls `get_applicable_pricing_rules()` for offer labels.
6. Returns product details, price breakup, tax label, UOM, pricing rule metadata, and available offers.

## Pricing Flow

File: `yob_storefront/services/pricing_service.py`

The MVP pricing design is centralized around ERPNext's Sales Order engine.

### Single Item Pricing

Function:

- `get_item_pricing(customer, item_code, qty, company, currency, selling_price_list=None, coupon_code=None)`

Flow:

1. Validates that a Customer exists.
2. Converts quantity to float.
3. Validates Item is saleable:
   - not disabled
   - marked as sales item
   - not past end of life
4. Resolves price list from customer/default selling settings/fallback.
5. Creates an in-memory Sales Order.
6. Adds the item and optional coupon.
7. Calls ERPNext methods:
   - `set_missing_values()`
   - `calculate_taxes_and_totals()`
8. Reads calculated item row and order totals.
9. Extracts tax labels and pricing rule metadata.
10. Returns base price, rate, discounts, net amount, tax amount, total amount, UOM, pricing rules, and safe item details.

### Cart Pricing

Function:

- `calculate_cart_using_sales_order(cart, customer_doc)`

Flow:

1. Creates an in-memory Sales Order from the Cart.
2. Copies customer, company, currency, price list, contact, billing address, shipping address, and coupon.
3. Appends each Cart Item into Sales Order items.
4. Calls:
   - `set_missing_values()`
   - `calculate_taxes_and_totals()`
   - `apply_pricing_rule_on_transaction()`
   - `calculate_taxes_and_totals()`
5. Returns the calculated Sales Order.

### Sync Pricing Back To Cart

Function:

- `sync_sales_order_to_cart(cart, so)`

Flow:

1. Copies total quantity, net total, tax total, grand total, coupon discount, and total discount to Cart.
2. For each **paid** Sales Order item (`is_free_item` rows are skipped — they are
   pricing output, never buyer intent), finds the matching Cart Item.
3. Records the unit ERPNext resolved for that line — `uom`, `conversion_factor`,
   `stock_uom` — so the buyer's quantity keeps one meaning on every later reprice
   (Phase 23B-5U). A conversion factor that has moved since the line was created
   is reported through `uom_changed_items`, never applied silently.
4. Copies base price, rate, discount, amount, tax, total amount, pricing rule list, pricing rule label, and pricing rule apply-on field.
5. `Cart Item.tax_amount` is a NON-AUTHORITATIVE snapshot, allocated
   proportionally by net amount. Authoritative per-row tax lives on
   `cart.pricing_rows`, built from ERPNext's own item-wise tax details
   (Phase 23B-3), and the Cart totals remain the document totals.
6. Returns the pricing projection (`cart.pricing_rows`): one row per Sales Order
   line, paid and promotion alike.

## Cart Flow

File: `yob_storefront/api/cart.py`

### Cart Creation

Function:

- `get_or_create_cart(customer)`

Flow:

1. Finds existing Draft Cart for the Customer.
2. If found, returns it.
3. If missing, creates a new Cart with:
   - customer
   - logged-in user
   - company
   - default currency
   - default price list
   - company address
   - Draft status

### Cart Repricing

Function:

- `reprice_cart(cart, customer)`

Flow:

1. Takes a snapshot of current item rate and total.
2. Removes invalid items:
   - item no longer exists
   - item disabled
   - item not marked as sales item
3. If empty, resets totals to zero.
4. Builds a temporary ERPNext Sales Order.
5. Syncs Sales Order calculations back into the Cart.
6. Detects items whose rate or total changed.
7. Returns removed item codes and price-updated item codes.

### API Endpoints

- `get_cart()`: loads or creates Cart, reprices it, returns enriched cart response.
- `add_to_cart(item_code, qty=1)`: adds a new line, or **increments** an existing
  one — `qty` is a delta, not a replacement total. One row is kept per item
  rather than appending a second row for the same `item_code`, because ERPNext
  evaluates a Pricing Rule's `min_qty`/`max_qty` against the ROW quantity: 4+4+3
  accumulated on one row triggers a `min_qty=10` rule that three separate rows
  would each fall short of. Not idempotent — a repeated call adds again.
- `remove_from_cart(item_code)`: removes item from Draft Cart.
- `clear_cart()`: removes all items.
- `set_cart_contact(contact_person)`: validates Contact belongs to Customer and stores it on Cart.
- `set_cart_billing_address(billing_address)`: validates Address belongs to Customer and stores billing address; also defaults shipping address when empty.
- `set_cart_shipping_address(shipping_address)`: validates Address belongs to Customer and stores shipping address when cart is shippable.
- `apply_coupon(code)`: validates coupon through `CouponService`, stores coupon code, reprices Cart.
- `remove_coupon()`: clears coupon, reprices Cart.

### Cart Response Shape

File: `yob_storefront/services/cart_service.py`

`build_cart_response()` returns:

- raw Cart data
- enriched Contact summary
- enriched Billing Address summary
- enriched Shipping Address summary
- `cart_updated`
- `removed_items`
- `price_updated_items`

## Coupon Flow

File: `yob_storefront/services/coupon_service.py`

Flow:

1. Normalizes coupon code to uppercase.
2. Finds ERPNext `Coupon Code` by `coupon_code`.
3. Validates:
   - coupon exists
   - valid date range
   - usage limit
   - customer-specific coupon applicability
   - linked Pricing Rule exists
   - Pricing Rule is enabled and selling
   - company and currency match the Cart
   - Cart net total matches min/max amount rules
4. On apply, stores the coupon code on Cart.
5. Cart repricing applies the actual discount using ERPNext Sales Order pricing logic.

## Checkout Flow

File: `yob_storefront/api/checkout.py`

Endpoint:

- `proceed_to_payment()`

Flow:

1. Requires logged-in Customer.
2. Finds the Customer's Draft Cart.
3. Validates:
   - Cart exists
   - Cart has items
   - contact person selected
   - billing address selected
   - shipping address selected when required
4. Reuses an existing unpaid/non-cancelled Payment Request for the Cart, or creates a new one.
5. Updates Payment Request grand total and currency from Cart.
6. Generates a secure checkout token with one-hour expiry.
7. Saves token and expiry on Payment Request custom fields.
8. Returns `/payment/<token>` along with the Payment Request name and token.

## Payment Flow

File: `yob_storefront/api/payment.py`

### Get Checkout Data

Endpoint:

- `get_checkout_data(token)`

Flow:

1. Allows guest access because the payment page uses a token.
2. Finds Payment Request by `custom_checkout_token`.
3. Validates token exists and is not expired.
4. Loads referenced Cart and Customer.
5. Builds Cart response.
6. Adds payment request, amount, currency, and available payment methods.

### Process Payment

Endpoint:

- `process_payment(token, payment_method)`

Flow:

1. Allows guest access with checkout token.
2. Loads Payment Request by token.
3. Validates expiry and reference document.
4. Loads selected Payment Method.
5. If method code is `paylater`, creates a Sales Order immediately and returns unpaid status.
6. If method code is `razorpay`, creates or reuses a Razorpay order and returns Razorpay checkout data.

### Razorpay Verification

Endpoint:

- `verify_payment(razorpay_order_id, razorpay_payment_id, razorpay_signature)`

Flow:

1. Calls `process_success_payment()`.
2. Verifies Razorpay signature.
3. Finds Payment Request using `custom_razorpay_order_id`.
4. Stops if Payment Request is already Paid.
5. Fetches Razorpay order and payment.
6. Validates:
   - order is paid
   - payment is captured
   - currency matches
   - payment amount matches Payment Request
   - order amount matches Payment Request
7. Saves Razorpay Payment Log.
8. Creates ERPNext Sales Order from Cart.
9. Marks Cart as Ordered and links Sales Order.
10. Marks Payment Request as Paid and changes reference from Cart to Sales Order.
11. Updates payment log as completed.

## Payment Method Selection

Files:

- `yob_storefront/services/cart_service.py`
- `yob_storefront/api/payment_method.py`

Flow:

1. Reads active Payment Method Assignment rows.
2. Checks target:
   - exact Customer
   - Customer Group
   - Company
3. Checks minimum and maximum order amount.
4. Returns active Payment Method rows sorted by display order.

## Address And Contact Flow

File: `yob_storefront/api/address.py`

### Contacts

- `get_contacts()`: returns Contacts linked to the Customer.
- `update_contact()`: validates Contact ownership, updates basic fields, email, and phone.
- `delete_contact(name)`: validates ownership and deletes Contact.

### Addresses

- `get_addresses()`: returns Addresses linked to the Customer.
- `add_address()`: creates Address and links it to Customer.
- `update_address()`: validates ownership and updates Address fields.
- `delete_address(name)`: validates ownership and deletes Address.
- `get_contact_for_customer(customer)`: helper to find a linked Contact.

## Order Flow

File: `yob_storefront/api/order.py`

Current active order APIs use ERPNext Sales Order:

- `get_orders()`: lists Sales Orders for the logged-in Customer.
- `get_order_details(order_id)`: validates Sales Order belongs to Customer, then returns totals, discounts, addresses, item rows, taxes, payment schedule, sales team, and matching Razorpay Payment Log.

There is also an older `checkout(address_name)` function that references `YOB Cart` and `YOB Order`. Those doctypes are not present in the current YOB app schema and appear to be stale legacy code.

## File Visibility Flow

File: `yob_storefront/api/file_hooks.py`

Flow:

1. Runs on File insert/update.
2. Only applies to files attached to `Item` or `Category`.
3. If the file is private, sets `is_private = 0`.
4. Saves the File so storefront images can be loaded publicly.

## Cache Flow

File: `yob_storefront/utils/cache.py`

Cache groups:

- store config
- menus
- customer data
- pricing
- item
- category
- CMS
- token

Cache invalidation is hooked into Frappe document events so changes to customers, contacts, items, pricing rules, price lists, item prices, and settings clear related caches.

## MVP User Journey

1. User logs in or sends a valid bearer token.
2. API resolves the user to a linked ERPNext Customer.
3. Frontend loads store config.
4. Frontend loads root categories.
5. User opens a category.
6. If category is a group, child categories are shown.
7. If category is a leaf, Items are loaded with ERPNext-calculated customer pricing.
8. User opens an item, sees exact price, tax, discount, and available offers.
9. User adds item to cart.
10. Cart is created if missing.
11. Cart is repriced using a temporary Sales Order.
12. User selects contact, billing address, and shipping address if needed.
13. User optionally applies a coupon.
14. Checkout creates or updates a Payment Request and tokenized payment URL.
15. Payment page loads checkout data using the token.
16. User selects Pay Later or Razorpay.
17. Pay Later creates Sales Order with unpaid status.
18. Razorpay creates an order, accepts payment, verifies signature, logs payment, creates Sales Order, marks Cart Ordered, and marks Payment Request Paid.
19. User can list and view Sales Orders through order APIs.

## Important Notes

- The pricing source of truth is ERPNext's Sales Order calculation engine.
- The Cart is an intermediate customer-facing state, not the final accounting document.
- Payment Request bridges Cart checkout and final Sales Order creation.
- Razorpay Payment Log is used for audit/reference after online payment.
- Most APIs are private and require a linked Customer, except tokenized payment APIs and the payment method API.
