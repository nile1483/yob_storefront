# YOB Bugs And Improvements

## Critical Bugs

1. Cart item images are returned as tuples instead of strings.
   - File: `yob/services/cart_service.py`
   - Line: 8
   - Issue: `item["image"] = ...,` has a trailing comma, so the value becomes a one-item tuple.
   - Impact: Frontend may receive `["https://..."]` or tuple-like serialized data instead of a URL string.
   - Fix: Remove the trailing comma.

2. Updating quantity for an existing cart item does not reprice the cart.
   - File: `yob/api/cart.py`
   - Lines: 204-221
   - Issue: `reprice_cart()` only runs inside the `else` block when a new item is appended. Existing item quantity changes are saved without recalculating rate, discounts, taxes, and totals.
   - Impact: Cart total can become stale or incorrect after changing quantity.
   - Fix: Run `reprice_cart(cart, customer)` after both add and update paths.

3. Pay Later Sales Order uses the wrong cart item quantity field.
   - File: `yob/api/payment.py`
   - Lines: 269-276
   - Issue: Cart Item schema uses `quantity`, but code reads `item.qty`.
   - Impact: Pay Later order creation can fail or create incorrect quantities.
   - Fix: Use `item.quantity`.

4. Razorpay success Sales Order uses non-existent cart price list field.
   - File: `yob/services/payment_service.py`
   - Line: 201
   - Issue: Cart schema has `selling_price_list`, but code reads `cart.price_list`.
   - Impact: Razorpay order finalization can fail or create Sales Orders without the intended price list.
   - Fix: Use `cart.selling_price_list`.

5. Legacy checkout references missing doctypes.
   - File: `yob/api/order.py`
   - Lines: 16-18
   - Issue: Code references `YOB Cart` and `YOB Order`, but current app defines `Cart` and uses ERPNext `Sales Order`.
   - Impact: Calling `checkout(address_name)` will fail.
   - Fix: Remove this legacy endpoint or migrate it to the current Cart/Sales Order flow.

6. Payment finalization workflow imports a missing service.
   - File: `yob/workflows/payment_finalization_workflow.py`
   - Line: 2
   - Issue: Imports `yob.services.order_service.OrderService`, but no `order_service.py` exists in the app.
   - Impact: Importing or using this workflow will fail.
   - Fix: Create the service, replace with existing payment service logic, or remove the unused workflow.

## High Priority Bugs

1. Public payment APIs expose tracebacks to clients.
   - Files:
     - `yob/api/payment.py`
     - `yob/api/cart.py`
     - `yob/api/catalog.py`
     - `yob/api/address.py`
   - Examples:
     - `yob/api/payment.py` lines 35-37 and 117-119
     - `yob/api/cart.py` lines 184-186 and 230-232
     - `yob/api/catalog.py` lines 212-214
     - `yob/api/address.py` lines 109-111 and 387-389
   - Issue: Several endpoints return `frappe.get_traceback()` directly.
   - Impact: Sensitive internal details can leak to frontend/users.
   - Fix: Log tracebacks server-side and return generic API errors.

2. Several important endpoints have commented-out exception handling.
   - Files:
     - `yob/api/catalog.py`
     - `yob/api/checkout.py`
     - `yob/api/cart.py`
     - `yob/services/payment_service.py`
   - Examples:
     - `get_item()` starts under a commented `try`.
     - `proceed_to_payment()` has commented exception handling.
     - `apply_coupon()` has commented exception handling.
     - `process_success_payment()` has commented exception handling.
   - Impact: Unhandled exceptions can break API responses and skip cleanup/logging.
   - Fix: Restore consistent `try/except` handling with safe client responses.

3. Checkout does not reprice or revalidate cart immediately before creating Payment Request.
   - File: `yob/api/checkout.py`
   - Lines: 16-99
   - Issue: Checkout trusts the current Cart totals.
   - Impact: If prices, taxes, item status, or coupons changed after last cart load, Payment Request amount may be stale.
   - Fix: Call cart repricing before Payment Request creation/update.

4. Pay Later order flow does not mark Cart as Ordered.
   - File: `yob/api/payment.py`
   - Lines: 238-285
   - Issue: `process_pay_later()` creates Sales Order and updates Payment Request reference, but does not set Cart status to `Ordered`.
   - Impact: Same Draft Cart may remain active and be reused.
   - Fix: Mark Cart as Ordered and link Sales Order, same as Razorpay flow.

5. Pay Later flow is not idempotent.
   - File: `yob/api/payment.py`
   - Lines: 238-285
   - Issue: Repeated calls can create multiple Sales Orders for the same Payment Request unless the reference has already changed.
   - Impact: Duplicate orders are possible.
   - Fix: Add idempotency checks and row locking around Payment Request processing.

6. Razorpay verification is not transactionally protected.
   - File: `yob/services/payment_service.py`
   - Lines: 241-363
   - Issue: Payment Request is checked for `Paid`, but there is no database lock around processing.
   - Impact: Two simultaneous verification calls can both create Sales Orders.
   - Fix: Lock the Payment Request row with `FOR UPDATE` or use a dedicated idempotency key/status transition.

7. Payment method API uses field names that do not match the DocType.
   - File: `yob/api/payment_method.py`
   - Lines: 17-26 and 53-56
   - Issue: Code reads `min_order_amount` and `max_order_amount`, but `Payment Method Assignment` has `minimum_order_amount` and `maximum_order_amount`.
   - Impact: API can fail or ignore amount limits.
   - Fix: Use the actual DocType field names or remove this duplicate API in favor of `get_available_payment_methods()`.

8. Address/contact cache clearing passes the customer object instead of customer name.
   - File: `yob/api/address.py`
   - Lines: 300, 368, 403
   - Issue: `clear_customer_address_cache(customer)` is called, but helper expects `customer_name`.
   - Impact: Address cache may not clear correctly.
   - Fix: Pass `customer.name`.

9. `get_logged_user()` resolves Customer differently from the rest of the app.
   - File: `yob/api/auth.py`
   - Lines: 7-19
   - Issue: It searches `Customer` by `user`, while `require_customer()` resolves through Contact Email and Dynamic Link.
   - Impact: A valid commerce user may be rejected by `get_logged_user()`.
   - Fix: Reuse `require_customer()` or centralize customer resolution.

## Medium Priority Bugs

1. Debug output remains in request handlers.
   - Files:
     - `yob/api/address.py` line 27
     - `yob/api/checkout.py` lines 84 and 87
   - Issue: `print()` and `pprint()` are used in API flow.
   - Impact: Noisy logs and possible sensitive data exposure.
   - Fix: Remove debug prints or replace with controlled debug logging.

2. `get_contacts()` and `get_addresses()` read cache but do not use it.
   - File: `yob/api/address.py`
   - Lines: 63-66 and 218-221
   - Issue: Cached value is read, but return-from-cache logic is commented out.
   - Impact: Extra database load while giving the impression caching is active.
   - Fix: Either enable cache return or remove the unused cache read.

3. `reprice_cart()` performs an explicit database commit inside a helper.
   - File: `yob/api/cart.py`
   - Line: 139
   - Issue: Helper commits during request flow.
   - Impact: Makes rollback behavior harder and can persist partial work before the endpoint finishes.
   - Fix: Let Frappe request transaction management handle commit/rollback.

4. Unused database query in cart repricing.
   - File: `yob/api/cart.py`
   - Lines: 128-137
   - Issue: `rows = frappe.db.get_all(...)` is assigned but never used.
   - Impact: Unnecessary database query.
   - Fix: Remove it.

5. Tokenized checkout data does not reprice cart before display.
   - File: `yob/api/payment.py`
   - Lines: 42-89
   - Issue: `get_checkout_data()` loads Cart but does not recalculate totals.
   - Impact: Payment page may show stale totals if cart data changed after token creation.
   - Fix: Reprice or compare Payment Request amount against current Cart before showing payment methods.

6. Checkout token remains usable after Pay Later order creation unless reference changes are carefully validated everywhere.
   - File: `yob/api/payment.py`
   - Lines: 126-148 and 280-283
   - Issue: Validation only checks expiry and `reference_name`; it does not check status, paid/ordered state, or expected reference type in all paths.
   - Impact: Old tokens may behave unpredictably.
   - Fix: Validate status and reference doctype consistently.

7. File hook commits inside document event.
   - File: `yob/api/file_hooks.py`
   - Line: 22
   - Issue: Calls `frappe.db.commit()` inside a hook.
   - Impact: Can interfere with parent transaction behavior.
   - Fix: Save document and let the request transaction commit naturally.

8. Some imports are unused.
   - Examples:
     - `pprint` in `catalog.py`, `cart.py`, `address.py`, `checkout.py`
     - `require_login` and `require_customer` in `payment.py`
     - ERPNext pricing utility imports in `catalog.py`
   - Impact: Lower maintainability and harder code review.
   - Fix: Remove unused imports.

## Improvements

1. Centralize checkout/order creation.
   - Current state: Pay Later order creation, Razorpay order creation, legacy order checkout, and workflow classes each implement different paths.
   - Improvement: Create one `OrderService` that converts Cart to Sales Order for all payment types.

2. Centralize API response helpers.
   - Current state: Both `yob/api/response.py` and `yob/utils/response.py` exist.
   - Improvement: Keep one response module and use it everywhere.

3. Make cart operations consistent.
   - Always reprice after add, update, remove, clear, coupon apply, coupon remove, address changes that affect tax, and checkout.

4. Add validation for payment method ownership/availability.
   - Before processing selected `payment_method`, confirm it is valid for the cart customer, company, and order amount.

5. Improve checkout token security.
   - Store token as a hash instead of raw token.
   - Invalidate old tokens after order creation.
   - Validate Payment Request status and reference doctype on every tokenized endpoint.

6. Add idempotency and locking for payment finalization.
   - Lock Payment Request before creating Sales Order.
   - Store gateway payment id uniqueness.
   - Return existing Sales Order for duplicate callbacks.

7. Add tests for MVP flows.
   - Customer resolution.
   - Category listing.
   - Single item pricing.
   - Cart add/update/remove.
   - Coupon apply/remove.
   - Checkout Payment Request creation.
   - Pay Later order creation.
   - Razorpay verification idempotency.
   - Payment method assignment amount filtering.

8. Improve error response quality.
   - Use HTTP status codes consistently.
   - Do not return tracebacks to clients.
   - Return stable error codes for frontend handling.

9. Remove stale/dead code.
   - Remove old `YOB Cart`/`YOB Order` checkout code.
   - Remove unused workflow or complete it with actual services.
   - Remove commented-out duplicate coupon handlers.

10. Improve amount precision.
   - Use Frappe/Decimal helpers for money instead of raw floats where possible.
   - Avoid `int(amount * 100)` without rounding rules for payment gateway amounts.

11. Review cache key consistency.
   - `yob/utils/cache.py` uses `yob:store:config`.
   - `yob/utils/store.py` uses `yob_store_settings`.
   - Improvement: Use one namespace and invalidation strategy.

12. Add pagination to catalog item listing.
   - Current category item listing loads all matching items.
   - Improvement: Add page/limit parameters for performance.

13. Add inventory/stock checks if MVP requires stock availability.
   - Current checks validate item saleable status but not stock availability.
   - Improvement: Validate warehouse stock before checkout/order creation.

14. Normalize customer lookup.
   - Use one customer resolution method across auth, orders, cart, and profile APIs.

15. Make public APIs explicit.
   - Review `allow_guest=True` endpoints and document why each is public.
   - Ensure tokenized public endpoints do not reveal more data than required.
