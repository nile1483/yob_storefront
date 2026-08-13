# YOB Storefront Security Rules

Apply the shared platform security and permission standards plus these domain
rules:

- A client-supplied Customer/company, cart, address, contact, order, price,
  discount, total, currency, or payment status is never authority.
- Storefront Customer scope is derived from server-generated `AuthContext` and
  verified document links/state.
- Customer A cannot list, name, read, write, or infer Customer B's cart,
  contact, address, order, checkout, or payment resources.
- Anonymous payment-method lookup authorized by caller-supplied Customer and
  ambiguous Customer resolution through email/Contact with `LIMIT 1` are
  forbidden.
- Every guest checkout/payment route validates its scoped token or provider
  signature before protected lookup/mutation and tests expiry, replay, wrong
  resource, amount, currency, signature, and idempotency.
- `Cart Item` access follows the authorized `Cart`; it is never independent CRUD
  or standalone Desk navigation.
- Payment/audit logs expose no provider secret, raw token/signature, session,
  complete request body, or unnecessary personal data.
