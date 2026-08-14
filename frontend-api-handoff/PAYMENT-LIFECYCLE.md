# Payment Lifecycle

The current, verified flow. Backend internals appear only where they explain
something the frontend must handle.

---

## The whole picture

```
AUTHENTICATED                          │  PUBLIC — no session
                                       │
 Cart + contact + billing (+ shipping) │
            │                          │
   proceed_to_payment  ────────────────┼──►  /payment/<token>
   201 created / 200 reused            │          │
   → payment_request, token,           │     get_checkout_data
     payment_url                       │          │
                                       │     process_payment
                                       │      (token, payment_method)
                                       │          │
                                       │     ┌────┴─────┐
                                       │  Pay Later   Razorpay
                                       │     │           │
                                       │  Draft SO   Checkout.js
                                       │  Unpaid         │
                                       │             verify_payment
                                       │                 │
                                       │             Paid + token revoked
```

---

## 1. Checkout initiation — authenticated

The cart must have a contact and a billing address, plus a shipping address if
`is_shippable` is true. `proceed_to_payment` then returns a Payment Request, a
token, and `payment_url`.

- **201** — a Payment Request was created (first time, or a **replacement**
  because the cart changed).
- **200** — an existing open obligation was reused: same request, same token.

A Payment Request is an **immutable obligation**. Once issued, its amount and
currency never change. If the cart changes, the server issues a *new* obligation
and **revokes the old token immediately**.

The token expires **1 hour** after issue.

## 2. Public payment — no session

`/payment/<token>` needs no login. `get_checkout_data` resolves the token to one
exact Payment Request and returns either a **Cart-backed** or **Sales
Order-backed** payload — branch on `source_doctype`.

If the cart moved since the obligation was issued, you get `payment_request_stale`
(409). Send the buyer back to the cart; the payment link cannot be silently
re-priced.

`payment_methods[]` is server-computed eligibility. Render exactly that list.

## 3. Commitment — server-side

`process_payment` commits the Cart into **exactly one Draft Sales Order** before
contacting any provider. This is idempotent: repeated calls converge on the same
Sales Order.

After commitment the Payment Request references the **Sales Order**, and
`get_checkout_data` switches to the Sales Order shape. The Cart is no longer
payment truth — later cart edits cannot affect a committed payment.

The frontend's only obligation here is to handle both source shapes.

### Trusted execution — server-side only

Committing the order runs briefly under a dedicated, **login-disabled** internal
service identity, because ERPNext performs nested permission checks against the
executing user and the public payer is Guest.

**This is entirely server-side.** The browser never authenticates as it, never
sees it, and must never reference it.

## 4. Razorpay

```
process_payment → { razorpay_key, order_id, amount (paise), currency }
                → checkout.razorpay.com/v1/checkout.js
                → hosted modal (Razorpay's own UI)
                → handler: { razorpay_order_id, razorpay_payment_id, razorpay_signature }
                → verify_payment  → 200 → show success
```

Server-side, on success: HMAC verified → Payment Request **Paid** →
`mode_of_payment = Razorpay` → provider payment id stored → **token revoked**.
The Cart is already `Ordered` and points at the Sales Order.

**One Payment Request → one canonical provider order.** The backend guarantees
this with a durable creation claim taken before the network call; a retry
recovers rather than creating a second order.

> **Provider limitation, verified on the wire:** Razorpay does **not** enforce
> receipt uniqueness, and its receipt lookup is eventually consistent (~10 s).
> The backend compensates. Nothing here changes the frontend contract — it is
> why a retry is safe.

## 5. Pay Later

No provider call. The order is committed and returned with
`payment_status: "Unpaid"`.

The Payment Request stays **outstanding** — deliberately not Paid, not
cancelled, and its **token stays usable** so a future "Pay Now" can work against
the same order.

## 6. Abandoned mid-flight — a valid state

Observed in real development and fully supported:

```
Payment Request : Draft, unpaid
Sales Order     : committed (Draft)
Provider order  : exists
Provider payment: none
Token           : still LIVE
```

A buyer opened Checkout and walked away. **This is not an error.** Reopening the
same payment link works and does **not** duplicate the Sales Order or the
provider order — verified by test.

Your UI should treat "has a Sales Order but is unpaid" as resumable, not as
failed.

## 7. Terminal states

| State | Symptom | Frontend behaviour |
|---|---|---|
| Paid | token revoked → `checkout_token_invalid` | show payment complete; do not retry |
| Expired | `checkout_token_expired` | ask the buyer to start checkout again |
| Superseded | old token → `checkout_token_invalid` | the cart changed; restart from the cart |
| Stale | `payment_request_stale` | return to cart, re-proceed |

---

## Verified security properties

Each proven by an endpoint-level automated test, and the happy path additionally
by real Razorpay Test Mode transactions in a browser.

| Property | Result |
|---|---|
| Tampered signature | rejected; no settlement, no state change |
| Tampered signed value (order id swapped, signature kept) | rejected |
| Cross-transaction identifiers (payment from another transaction) | rejected, fails closed |
| Replay of a valid success | idempotent — one Sales Order, one settlement |
| Already-paid retry | no second charge can be started |
| Revoked token | denies both the page and payment initiation |
| Abandoned-state reopen | safe; no duplicate order or provider order |
| Ineligible payment method | rejected server-side; no provider order created |
| Financial/source mismatch | rejected before any provider operation |
| Guest privilege isolation | no ERPNext access before or after paying |
| PR ↔ Sales Order ↔ provider order | 1:1, verified against real records |

**Signature material:** Razorpay signs `order_id|payment_id`. Verification
happens server-side before any state change. The frontend never verifies it and
never sees the secret.

---

## Current accounting boundary

> **No `Payment Entry` is created.** Verified: zero Payment Entry records exist
> after real settled payments, and no code path creates one.
>
> Sales Orders remain **Draft (`docstatus = 0`)** after payment — they are not
> submitted.

Settlement today means: Payment Request marked Paid, `mode_of_payment` set,
provider identifiers stored, token revoked, Razorpay Payment Log written.

**Do not build UI that implies ERPNext accounting settlement has occurred.** If
payment-entry accounting is added later it will be a deliberate, separate
change.
