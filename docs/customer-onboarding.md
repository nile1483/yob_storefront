# YOB Storefront — Customer Onboarding (Recurring SOP)

The process staff follows **every time** a new customer is added.
One-time site configuration is in [`site-setup.md`](site-setup.md) and is
assumed complete.

Verified against committed source on **2026-11-16**:
`yob_storefront 9784539` · `yob_auth 1fa3018` · `yob_core 4c85391`.

**The whole SOP is four records, and the buyer can create two of them
themselves.** Staff never needs to open `YOB User Application Access`.

---

## Step 1 — Create the Customer

**Selling → Customer → New.**

### Normal / unregistered customer

| Field | Value |
| --- | --- |
| **`customer_name`** | the business name — **this is the only field you must type** |

Everything else defaults or is unused. Verified by inserting a Customer with
only `customer_name`: `customer_type` defaulted to `Company`, `gst_category` to
`Unregistered`, and `customer_group`, `territory`, `default_price_list`,
`default_currency`, `tax_category`, `payment_terms`,
`customer_primary_address` and `customer_primary_contact` all stayed empty —
and the full flow through to a payment token still worked.

### GST-registered customer

| Field | Value |
| --- | --- |
| `customer_name` | the business name |
| `gst_category` | `Registered Regular` (or the applicable category) |
| `gstin` | their GSTIN — **validated, including the check digit** |

> **Do not** set `customer_group`, `territory`, `default_price_list`,
> `default_currency`, `tax_category`, `payment_terms`,
> `customer_primary_address` or `customer_primary_contact`. All were proven
> unnecessary. See §22 for the one case where a price list is wanted.

---

## Step 2 — Create the storefront user

**Customer → Portal Users tab → YOB Storefront Users.**

This is the standard workflow. **Do not open `YOB User Application Access`
manually** — that is what this panel is for, and three of its four fields are
dictated by context, so filling them by hand is how a grant ends up pointing at
the wrong Customer.

Two buttons:

- **Create Website User** — the buyer has no account yet (§18)
- **Add Existing User** — the account already exists (§19)

> The ERPNext **Customer Portal Users** table directly above is a *different,
> unrelated* feature. It grants the ERPNext `Customer` portal role and links
> Users to Contacts; it creates **no** storefront access. Leave it alone.

### What the panel does internally

| | |
| --- | --- |
| **Reads** | every `YOB User Application Access` where `application = STOREFRONT`, `profile_doctype = Customer`, `profile_name = <this Customer>` — **live, on every open** |
| **Stores on Customer** | **nothing.** The section is an HTML field; there is no child table and no mirror |
| **Writes** | through the real document (`insert()` / `save()` / `delete()`), so the access controller's validation and the role-sync hooks run exactly as from the Desk list |
| **Sets for you** | `application = STOREFRONT`, `profile_doctype = Customer`, `profile_name = <this Customer>` — never shown, never accepted from the browser |
| **Does not manage** | the `YOB Storefront Buyer` role. That is granted and revoked **only** by the existing hooks on the access record |

Columns: **User · Enabled · Valid From · Valid Until · Status**, plus Edit and
Remove. Status is derived from the same three fields `resolve_access` tests, in
the same order, so the badge cannot disagree with what the buyer gets at login:

| Status | Meaning |
| --- | --- |
| **Active** | the buyer can log in |
| **Disabled** | `enabled = 0` |
| **Scheduled** | `valid_from` is in the future |
| **Expired** | `valid_until` is in the past |
| **User Disabled** | the grant is fine but `User.enabled = 0` |

A row that is enabled while the role is missing shows a red **"Buyer role
missing"** warning — reported, never fixed from here.

**Unsaved Customer:** the panel shows *"Save the Customer before adding
storefront users."* and the server refuses too. Save the Customer first.

---

## Step 3 (option A) — Create Website User

Dialog fields:

| Field | Required | Note |
| --- | --- | --- |
| **First Name** | **yes** | |
| **Email** | **yes** | becomes the login id and the User docname |
| **Send Welcome Email** | default ✓ | emails the buyer a link to set their own password. Untick only if you will set a password yourself on the User record |

Verified outcome of one click:

| Created | Not created |
| --- | --- |
| `User` with **`user_type = Website User`** | **no System User** |
| `YOB User Application Access` → this Customer | **no `Customer` role** |
| **`YOB Storefront Buyer`** role, granted automatically by the hooks | **no `User Permission`** |
| | **no `YOB Login Identity`** (password mode never uses one) |
| | **no new Customer** |

**Atomic.** A Frappe request is one transaction and the User controller performs
no commit of its own, so if the grant is refused the User is rolled back with it
— no half-onboarded account is left behind. Asserted by a test that forces the
grant to fail after the User insert.

If the email already belongs to a User, the dialog refuses and tells you to use
**Add Existing User** instead.

---

## Step 3 (option B) — Add Existing User

Dialog: **User** (Link, filtered to enabled Website Users), **Enabled**
(default ✓), **Valid From**, **Valid Until** (both optional — leave blank).

| Case | Behaviour |
| --- | --- |
| Website User, no existing grant | granted; the buyer role appears automatically |
| **System User** | **refused, and the account is NOT converted.** *"… is a System User. Storefront buyers must be Website Users, and this will not change the account's User Type for you. Create a Website User for the buyer instead."* `user_type` is what makes an account Desk-eligible, so flipping it here would quietly change a colleague's account |
| `Guest` / `Administrator` | refused outright |
| **Same user, same Customer** | *"This user already has storefront access to this customer."* No duplicate row |
| **Same user, different Customer** | **blocked, with the Customer named:** *"… already has STOREFRONT access to **\<Customer A\>**. A storefront user can currently belong to only one Customer."* The grant is **never silently moved** |
| **Several users, one Customer** | **supported** — see §26 |

---

## Step 4 — Contact

**Required only at Proceed to Payment.** Not needed for login, browsing,
pricing, adding to cart, or opening checkout. Omitting it answers
`contact_required` (HTTP 422) at Proceed and nothing earlier.

| Field | Required? |
| --- | --- |
| **`first_name`** | **yes — the only one.** The Contact DocType itself has *no* mandatory fields |
| Link → Customer (`Dynamic Link`) | **yes**, and it is appended **server-side** — a caller cannot attach a Contact to another Customer |
| `email_id` | **no** — verified: a Contact with no email at all completed Proceed |
| `mobile_no` / `phone` | no |
| `is_primary_contact` | **no** — any Contact linked to the Customer is sufficient |

**The User's email and the Contact's email need not match.** The Dynamic Link is
the only binding.

- **ADMIN MAY CREATE** — Contacts → New, then link to the Customer.
- **CUSTOMER MAY SELF-CREATE** — the storefront's own *Add Contact* screen
  (`add_contact`) is a buyer endpoint. Pre-create one only to spare a new buyer
  the first-run friction.

---

## Step 5 — Address

**Billing address is required at Proceed to Payment** (`billing_address_required`,
HTTP 422). Shipping is required only when the Cart is shippable — and is
**auto-filled from billing** when blank, so **one address is enough**.

Minimum Indian Address:

| Field | Required? |
| --- | --- |
| **`address_line1`** | **yes** |
| **`city`** | **yes** |
| **`state`** | **YES — and this one is easy to miss.** It is *not* marked mandatory on the field; India Compliance enforces it in a controller. Omitting it returns *"State is a required field for Indian Address"* |
| **`country`** | **yes** — `India` |
| Link → Customer (`Dynamic Link`) | **yes**, appended server-side |
| `gst_category` | auto — defaults to `Unregistered` |
| `address_type` | auto — defaults to `Billing`. Functionally irrelevant: the Cart's `billing_address` / `shipping_address` links decide the role |
| `pincode`, `address_title` | no |
| `is_primary_address` / `is_shipping_address` | **no** — both stay 0 |

**GST-registered customer:** also set `gstin` and
`gst_category = Registered Regular` on the address.

> **`state` is what decides the tax.** It sets `place_of_supply`, which selects
> in-state (CGST+SGST) vs out-of-state (IGST). The customer's own `gst_category`
> and `gstin` do **not** change the rate — verified.

- **ADMIN MAY CREATE** — Contacts → Address → New, then link to the Customer.
- **CUSTOMER MAY SELF-CREATE** — the storefront's *Add Address* screen
  (`add_address`) is a buyer endpoint.

---

## Step 6 — Pricing per customer

**Normally staff does nothing.** Verified: a Customer with
`default_price_list = None` and `customer_group = None` priced correctly from
`Standard Selling` via `Selling Settings.selling_price_list`.

The only checks worth making: the items this customer will buy have an
`Item Price` on the site's selling price list.

Do something extra **only** for:

| Want | Do |
| --- | --- |
| A price list for **this one customer** | set `Customer.default_price_list`, **and** add `Item Price` rows on that list. A list with no rows means no price |
| A price list for a **group** of customers | set `Customer Group.default_price_list` and assign the customer to that group |

Resolution order: `Customer.default_price_list` → `Customer Group.default_price_list`
→ `Selling Settings.selling_price_list`. Each step is skipped if the Price List
is disabled.

---

## Step 7 — Payment per customer

**Nothing.** On this deployment both payment methods are assigned at
`reference_doctype = Company`, so every customer of that company is covered —
verified: a brand-new customer was offered the full method list with zero
per-customer configuration.

Exceptions, i.e. the only reasons to add a per-customer assignment:

- this customer should get a method others do not (or must be denied one),
- this customer needs different `minimum_order_amount` / `maximum_order_amount`.

Then add a `Payment Method Assignment` with `reference_doctype = Customer`.

---

## What is created automatically

| Artifact | Created by | When |
| --- | --- | --- |
| **`YOB User Application Access`** | **the Customer panel** | when you click Add / Create |
| **`YOB Storefront Buyer` role** | **the access record's own hooks** (`after_insert` / `on_update`) | immediately — and **revoked** on disable or delete |
| **`Cart`** | **checkout flow**, first `get_cart` / `add_to_cart` | server-derived `company`, `currency`, `selling_price_list`, `company_address` from `YOB Store Settings`; `status = Draft` |
| **Draft `Sales Order`** | **checkout**, inside the trusted boundary at Proceed | |
| **`DocShare`** | **checkout**, same moment | exactly one row, read-only, to `payment-processor@yob.internal` |
| **`Payment Request`** | **checkout**, same moment | `reference_doctype = Sales Order`, `custom_cart` set, `custom_committed_at` empty |
| **Checkout token + expiry + fingerprint** | **checkout**, same moment | the `/payment/<token>` credential |

**Never create any of these by hand.** In particular:

- the administrator does **not** create the Sales Order,
- the administrator does **not** create the Payment Request,
- no `Cart` record should ever be created manually.

A **Draft payment Sales Order does not count toward Target Management** —
`target_achievement.py:294` filters `so.docstatus = 1`. A *successful order* is
the **settled** obligation: the Payment Request paid and its Cart closed against
the Sales Order (`custom_committed_at` set). A live token on a Draft Sales Order
is an **unpaid attempt**, deliberately retained so the payer can return.

---

## Disabling or removing storefront access

**Customer → Portal Users → YOB Storefront Users.**

| Action | Use when |
| --- | --- |
| **Edit → untick Enabled** | temporary suspension, keeps the history and the validity dates |
| **Remove** | the person has left, or you want to free their account for another Customer |

Either way:

| | |
| --- | --- |
| `YOB Storefront Buyer` role | **revoked** (recomputed from the user's remaining enabled grants) |
| The Frappe **User** | **kept** |
| The **Customer** | **kept** |
| **Contact** and **Address** | **kept** |
| Cart, Sales Orders, Payment Requests | **kept** |

Remove asks for confirmation and states plainly that it removes storefront
access only.

> **Role revocation on delete is correct as of `9784539`.** The role sync is
> registered on **`after_delete`**. It had been on `on_trash`, which Frappe
> fires *before* the row is removed, so the recompute still saw the row it was
> reacting to and left the role attached — deleting a grant never revoked it.
> If you administer a site that has not taken this commit, run `bench migrate`
> and spot-check any user whose grant was deleted earlier.

Re-enabling (untick → tick) restores the role automatically. Verified in both
directions.

**Other ways access ends**, all checked on every request by `resolve_access`:
`User.enabled = 0`; `valid_from` in the future (*"not active yet"*);
`valid_until` in the past (*"has expired"*). Leave both dates blank unless
access is genuinely time-boxed — a stale `valid_until` silently locks the buyer
out.

---

## Multiple users under one Customer

**Supported.** Verified with three users on one Customer, each with their own
grant and role, and **no duplicate Customer created**.

For the second and third person:

1. **Reuse the existing Customer** — do not create another.
2. **Create Website User** (or Add Existing User) from the same panel.
3. A separate **Contact** is optional but recommended — any Contact linked to
   the Customer satisfies checkout, so a colleague can select the first
   person's. Give each person their own if you want the order to record who
   placed it.
4. **No extra Address** — addresses belong to the Customer.

> **Brief staff on this:** all users of one Customer **share one Draft Cart.**
> The Cart is keyed on `{customer, status: "Draft"}`, so two colleagues add to
> the same basket and quantities accumulate — verified. `Cart.user` records only
> the **last** person to touch it.

---

## One user, multiple Customers

**Not currently supported.** Blocked at two independent levels:

1. `YOB User Application Access` allows **one row per `(user, application)`**,
   enforced in its controller and **ignoring `enabled`** — so even a disabled
   duplicate is refused.
2. `resolve_access` reads **one** row with `frappe.db.get_value(...)`. There is
   no list, no ordering and no selection, and `AuthContext.profile_name` is a
   single value.

**What to do instead: create a second User.** One person who buys for two
businesses needs two logins, one per Customer.

Why not loosen it: the Customer **is** the tenant boundary. Every ownership
check — cart, address, contact, order, payment — derives from
`auth_context.profile_name`. Supporting several customers per user would need an
explicit authenticated profile-selection step plus re-scoping of all of those
checks; until that exists, an ambiguous resolution is cross-tenant data
exposure. Changing this is an ADR-level decision, not configuration.

---

## FINAL CUSTOMER CHECKLIST

> **Add a new customer** — one page, in order.

**☐ 1. Create Customer** — Selling → Customer → New
- `customer_name` = business name **← the only required field**
- *GST-registered only:* `gst_category` = `Registered Regular`, `gstin`

**☐ 2. Open Customer → Portal Users tab → YOB Storefront Users**

**☐ 3. Add the storefront user** — one of:
- **Create Website User**: First Name · Email · Send Welcome Email ✓
- **Add Existing User**: pick an enabled **Website User**; Enabled ✓; leave Valid From / Valid Until **blank**
- *Do not assign any role — it is granted automatically*

**☐ 4. Contact** — admin creates, or let the buyer do it in the storefront
- `first_name` **← the only required field** · link → Customer

**☐ 5. Address** — admin creates, or let the buyer do it in the storefront
- `address_line1` · `city` · **`state`** · `country` = `India` · link → Customer
- one address covers billing **and** shipping
- *GST-registered only:* `gstin`, `gst_category`

**☐ 6. Verify pricing** — usually nothing to do
- confirm the items they will buy have an `Item Price` on the site selling price list
- special pricing only: set `Customer.default_price_list` **and** add `Item Price` rows on it

**☐ 7. Test login** — storefront URL, or `login_with_password` with `application=STOREFRONT`
- expect `authenticated: true` and `context.profile_name` = this Customer

**☐ 8. Test product and pricing** — open a product page, confirm **a price appears**

**☐ 9. Test cart** — add to cart; the Cart is created automatically, nothing to create by hand

**☐ 10. Test Proceed to Payment** — select the Contact and the Address
- expect **HTTP 201** and a `/payment/<token>` link that opens with the right amount and methods
- **STOP THERE.** Do not complete a real payment unless you are deliberately testing the gateway

### If a step fails, read the error code

| Code / message | Fix |
| --- | --- |
| `application_access_denied` | step 3 — grant missing, disabled, or outside its validity dates |
| *"not allowed from the current domain / origin"* | site setup: `YOB Application.domains` / `allowed_origins`, or the proxy is not setting `X-YOB-Original-Host` |
| no price on the product page | step 6 — missing `Item Price`, or a customer price list with no rows |
| `cart_empty` | nothing added to the cart |
| `contact_required` | step 4 |
| `billing_address_required` | step 5 — **often a rejected Address: check `state`** |
| no payment methods on `/payment/<token>` | site setup: `Payment Method Assignment` |
