#path apps/yob_storefront/yob_storefront/services/pricing.py
"""
PRICING SERVICE – CENTRALIZED PRICING ENGINE
ERPNext v16 Compatible
B2B Secure – Uses Full Sales Order Engine Only
"""

import json
import frappe
from frappe.utils import flt, getdate, today
from erpnext.accounts.party import get_default_price_list
from pprint import pprint 

from erpnext.accounts.doctype.pricing_rule.utils import apply_pricing_rule_on_transaction

# =========================================================
# 0️⃣ STOREFRONT METADATA ON THE RESOLVED ITEM PRICE (Phase 29A)
# =========================================================

#: The storefront-owned custom fields carried by an Item Price row.
ITEM_PRICE_STOREFRONT_FIELDS = ("custom_moq", "custom_quantity_multiplier", "custom_mrp")


def resolve_item_price_source(item_code, *, price_list, customer, uom, stock_uom,
                              transaction_date):
    """The NAME of the Item Price row ERPNext's own ranked pick chose, or None.

    WHY THIS EXISTS
    ---------------
    ERPNext resolves the price and then throws away which row produced it:
    `get_price_list_rate_for()` reads `get_item_price()[0]` and returns only
    `price_list_rate`, and the Sales Order Item it fills has no field naming the
    Item Price. So a temporary Sales Order tells us the RATE authoritatively and
    cannot tell us the SOURCE at all.

    Phase 29A needs the source, because MOQ, the quantity multiplier and MRP are
    properties of a price row: the customer-specific price list may carry
    different ones from the generic list for the same SKU.

    NOT A SECOND SELECTION ALGORITHM
    --------------------------------
    The ranked pick stays ERPNext's. This calls `get_item_price()` -- the exact
    function `get_price_list_rate_for()` calls -- so customer-specific before
    generic, latest `valid_from`, batch, then UOM, with `LIMIT 1`, all remain
    ERPNext's ordering. What is mirrored here is only the two-step LADDER around
    that call, and each step mirrors a specific ERPNext line:

    * retry in `stock_uom` when the selling UOM found nothing
      (`get_item_details.py:1280`);
    * fall back from a variant to its template
      (`get_item_details.py:1043`, guarded by `is None`).

    Reproducing the ORDER BY would be a second implementation free to drift.
    Reproducing two documented fallbacks is what keeps this row identical to the
    row the rate came from.

    Returns `None` when ERPNext found no Item Price at all, which is not an
    error: the product simply has no storefront metadata to publish.
    """

    from erpnext.stock.get_item_details import get_item_price

    def pick(code, unit):
        rows = get_item_price(
            frappe._dict({
                "item_code": code,
                "price_list": price_list,
                "customer": customer,
                "supplier": None,
                "uom": unit,
                "transaction_date": transaction_date,
                "batch_no": None,
            }),
            code,
        )
        return rows[0].get("name") if rows else None

    def resolve(code):
        found = pick(code, uom)

        if not found and stock_uom and uom != stock_uom:
            found = pick(code, stock_uom)

        return found

    source = resolve(item_code)

    if not source:
        template = frappe.get_cached_value("Item", item_code, "variant_of")
        if template:
            source = resolve(template)

    return source


def configured_number(value):
    """A configured storefront number, or None.

    Blank, zero and negative all mean NOT CONFIGURED and collapse to the same
    `None`. One rule for all three fields, applied at the runtime boundary rather
    than at save, so a merchant may clear a value by typing 0 and a row written
    before these fields existed behaves identically to a blank one.
    """

    number = flt(value)

    return number if number > 0 else None


def storefront_price_metadata(item_code, *, price_list, customer, uom, stock_uom,
                              transaction_date, pricing_rules=None):
    """MRP and quantity guidance from the SAME Item Price the rate came from.

    `quantity_control.allowed` -- WHEN THE GUIDANCE MAY BE APPLIED
    --------------------------------------------------------------
    `False` exactly when the authoritative pricing preview attached **at least
    one Pricing Rule** to this row, and `True` otherwise. That is read from
    `pricing_rules` on the priced Sales Order row -- the signal this service
    already produces and already publishes as `pricing_rule_label` -- so nothing
    here evaluates, predicts or re-discovers a rule.

    ERPNext funnels every promotional mechanism through that one field: a rate
    or discount Pricing Rule, a promotional scheme, a Product Discount and a
    free-item rule all register on the row they applied to. So the single check
    covers them without this module knowing what any of them are.

    The reason is quantity, not price. A rule that changes behaviour at a
    quantity threshold makes "start at 10, step by 6" a claim the storefront
    cannot honour, because the price at 16 may not follow from the price at 10.
    Rather than predict the rule, the backend says the guidance is unsafe and the
    client falls back to its ordinary quantity input.

    Deliberately NOT a prediction engine. Answering "would a rule apply at 16?"
    would mean evaluating hypothetical quantities through ERPNext's rule stack --
    a second pricing engine, and the thing Phase 22B exists to avoid.

    MOQ and the multiplier are a PAIR: one `allowed` covers both, because a start
    without a safe step and a step without a safe start are equally unusable. The
    configured values are still published when `allowed` is false, for
    transparency in Desk and in support -- the contract says they must not be
    applied, not that they must be hidden.

    MRP is INDEPENDENT of all of this. It is informational, has no quantity
    behaviour and therefore no conflict to have, so it is published whenever it
    is configured, whatever `allowed` says.
    """

    source = resolve_item_price_source(
        item_code, price_list=price_list, customer=customer, uom=uom,
        stock_uom=stock_uom, transaction_date=transaction_date)

    row = None

    if source:
        row = frappe.db.get_value(
            "Item Price", source, ITEM_PRICE_STOREFRONT_FIELDS, as_dict=True)

    return {
        "mrp": configured_number(row.custom_mrp) if row else None,
        "quantity_control": {
            "moq": configured_number(row.custom_moq) if row else None,
            "quantity_multiplier": (
                configured_number(row.custom_quantity_multiplier) if row else None),
            "allowed": not pricing_rules,
        },
    }


# =========================================================
# 1️⃣ ITEM PRICING (Single Item via Sales Order Engine)
# =========================================================

def get_item_pricing(
    customer,
    item_code,
    qty,
    company,
    currency,
    selling_price_list=None,
    coupon_code=None,
    with_price_metadata=False,
):
    """
    Secure item pricing using full ERPNext Sales Order engine.

    `with_price_metadata` adds `mrp` and `quantity_control` from the SAME Item
    Price row this rate came from (Phase 29A). OFF by default and deliberately
    opt-in: it costs one extra ranked lookup plus one row read per item, which is
    nothing on a product page and 48 extra queries on a 24-card listing. The
    product-detail serializer asks for it; the catalogue listing does not, and
    listing payloads are unchanged.
    """

    if not customer:
        frappe.throw("Unauthorized", frappe.PermissionError)

    qty = float(qty)
    validate_item_saleable(item_code)

    # ---------------- CUSTOMER ----------------
    customer_doc = (
        frappe.get_doc("Customer", customer)
        if isinstance(customer, str)
        else customer
    )

    # ---------------- PRICE LIST ----------------
    selling_price_list = get_price_list_for_customer(
        customer_doc,
        selling_price_list
    )

    if not selling_price_list:
        frappe.throw("No selling price list configured")

    # ---------------- TEMP SALES ORDER ----------------
    so = frappe.new_doc("Sales Order")

    so.customer = customer_doc.name
    so.company = company
    so.currency = currency
    so.selling_price_list = selling_price_list
    so.transaction_date = today()

    if coupon_code:
        so.coupon_code = coupon_code

    so.append("items", {
        "item_code": item_code,
        "qty": qty
    })

    # ------------------------------------------------------------------
    # ELEVATION BOUNDARY -- read this before changing it.
    #
    # `so.customer` above came from `customer_doc`, which the caller resolved
    # through get_storefront_customer(auth_context). Authorization has ALREADY
    # happened: the caller proved an enabled STOREFRONT grant for exactly this
    # Customer. A request-supplied customer never reaches this line.
    #
    # ERPNext then re-checks Frappe DocType permissions while filling the
    # order, which an external Website User cannot satisfy:
    #
    #   selling_controller.set_missing_lead_customer_details
    #     -> party._get_party_details            -> Customer read
    #     -> party.set_address_details           -> Address read
    #
    # ERPNext supports skipping exactly those: selling_controller.py passes
    # `ignore_permissions=self.flags.ignore_permissions` into _get_party_details,
    # which forwards it as `check_permissions=not ignore_permissions` to the
    # address lookups. So this flag is ERPNext's own documented parameter, not a
    # bypass we invented.
    #
    # Scope is one throwaway in-memory Sales Order that is never inserted. No
    # global state is touched -- deliberately NOT the global
    # frappe.flags.ignore_permissions (which does not work here anyway:
    # get_item_details calls item.check_permission() on an internally cached
    # Item doc, and Document.has_permission consults that doc's own flags), and
    # the session user is never switched.
    #
    # (Phrased without the literal session-switching call name on purpose: the
    # contract scanner in tests/test_rename.py greps source for forbidden auth
    # primitives, and it should stay a dumb, un-foolable text scan.)
    #
    # The remaining Item read is granted by the `YOB Storefront Buyer` role, not
    # by this flag. Customer read stays denied -- that is the tested boundary.
    # ------------------------------------------------------------------
    so.flags.ignore_permissions = True

    so.set_missing_values()
    so.calculate_taxes_and_totals()

    row = so.items[0]

    # ---------------- TAX LABELS ----------------
    tax_labels = []
    for tax in so.taxes:
        if tax.tax_amount and tax.rate:
            label = tax.description or tax.account_head
            tax_labels.append(f"{label} {tax.rate}%")

    tax_label = ", ".join(tax_labels) if tax_labels else None

    # ---------------- PRICING RULE INFO ----------------
    pricing_rules = row.pricing_rules or []

    if isinstance(pricing_rules, str):
        try:
            pricing_rules = json.loads(pricing_rules)
        except Exception:
            pricing_rules = [pricing_rules]

    pricing_rule_label = None
    pricing_rule_apply_on = None

    if pricing_rules:
        rule = frappe.get_cached_doc("Pricing Rule", pricing_rules[0])
        pricing_rule_label = rule.title or rule.name
        pricing_rule_apply_on = rule.apply_on

    # ---------------- SAFE ITEM DATA ----------------
    item_doc = frappe.get_cached_doc("Item", item_code)

    safe_item = {
        "name": item_doc.name,
        "item_name": item_doc.item_name,
        "item_group": item_doc.item_group,
        "image": item_doc.image,
        "stock_uom": item_doc.stock_uom
    }

    # ---------------- FINAL RESPONSE ----------------
    pricing = {
        "item": safe_item,
        "selling_price_list": selling_price_list,
        "qty": qty,

        "base_price": row.price_list_rate,
        "rate": row.rate,

        "discount_percentage": row.discount_percentage,
        "discount_amount": row.discount_amount,
        "total_discount": row.discount_amount * qty if row.discount_amount else 0,

        "net_amount": row.net_amount,
        "tax_amount": so.total_taxes_and_charges or 0,
        "tax_label": tax_label,
        "total_amount": so.grand_total,

        "pricing_rules": pricing_rules,
        "pricing_rule_label": pricing_rule_label,
        "pricing_rule_apply_on": pricing_rule_apply_on,

        # The unit this price is PER, decided by ERPNext (`sales_uom` when the
        # Item has one, else `stock_uom`), plus the two values that let a client
        # LABEL the transaction without computing anything:
        #
        #   uom               what the buyer's quantity is counted in  -- "Strip"
        #   stock_uom         what stock is counted in                 -- "Nos"
        #   conversion_factor ERPNext's factor between them            -- 10
        #   stock_qty         this quantity in stock units             -- 20
        #
        # Availability (`actual_qty`) stays in stock units and is never converted
        # here. A storefront must show "2 Strips" and "125 Nos available" as the
        # two different facts they are.
        "uom": row.uom,
        "stock_uom": row.stock_uom,
        "conversion_factor": row.conversion_factor,
        "stock_qty": row.stock_qty,
    }

    if with_price_metadata:
        # Resolved from the row ERPNext just priced against: its OWN selling UOM,
        # its OWN price list and this customer -- so the metadata cannot come
        # from a different Item Price than the rate did. `pricing_rules` is the
        # authoritative preview's own answer, not a second evaluation.
        pricing.update(storefront_price_metadata(
            item_code,
            price_list=selling_price_list,
            customer=customer_doc.name,
            uom=row.uom,
            stock_uom=row.stock_uom,
            transaction_date=so.transaction_date,
            pricing_rules=pricing_rules,
        ))

    return pricing


# =========================================================
# 2️⃣ FULL CART CALCULATION USING SALES ORDER
# =========================================================

def calculate_cart_using_sales_order(cart, customer_doc):
 

    if not customer_doc:
        frappe.throw("Unauthorized", frappe.PermissionError)

    so = frappe.new_doc("Sales Order")

    # ------------------------------------------------------------------
    # TRANSACTION CONTEXT -- resolved, not read back off the Cart.
    #
    # `cart.selling_price_list` used to be the authority here. It is written once
    # by get_or_create_cart() from YOB Store Settings.default_price_list, which
    # ignores the Customer and Customer Group entirely -- so a customer whose own
    # price list said 600 was charged the store default of 1000, and the product
    # page (which resolves properly) disagreed with the Cart. Reproduced in Phase
    # 23B-1 and pinned by test_pricing_convergence.py.
    #
    # It was also written only at cart CREATION, so a later change to the
    # customer's price list never reached an existing cart.
    #
    # The resolved value is written back so the stored field converges and
    # create_sales_order_from_cart(), which still reads it, stays in parity.
    # ------------------------------------------------------------------
    from yob_storefront.services.pricing_context import context_for

    ctx = context_for(customer_doc)

    if cart.selling_price_list != ctx.price_list:
        cart.selling_price_list = ctx.price_list

    so.customer = customer_doc.name
    so.company  = cart.company or ctx.company
    so.currency = cart.currency or ctx.currency
    so.selling_price_list = ctx.price_list
    so.transaction_date = today() 
    
    if cart.coupon_code:
            coupon_name = frappe.db.get_value(
                "Coupon Code",
                {"coupon_code": cart.coupon_code},
                "name"
            )
        
            if coupon_name: 
                so.coupon_code = coupon_name

    # so.tax_category = cart.tax_category
    so.contact_person = cart.contact_person
    so.customer_address = cart.billing_address
    so.shipping_address_name = cart.shipping_address

    for row in cart.items:

        so.append("items", cart_row_to_order_item(row))

    # Same targeted elevation as get_item_pricing, and for the same reason:
    # `so.customer` came from `cart.customer`, and the cart was loaded via the
    # authenticated Customer resolved from auth_context. Authorization already
    # happened; ERPNext then re-checks Customer/Address DocType permissions
    # while filling the order, which an external Website User cannot satisfy.
    #
    # Scope is this one throwaway in-memory Sales Order, never inserted. NOT a
    # global flag, and nothing else in cart/pricing/order services is elevated.
    so.flags.ignore_permissions = True

    so.set_missing_values() 
    
    so.calculate_taxes_and_totals()
    
    apply_pricing_rule_on_transaction(so)
    
    so.calculate_taxes_and_totals()

    return so


# =========================================================
# CART ROW -> SALES ORDER ROW
# =========================================================

def cart_row_to_order_item(row):
    """One Cart line as ERPNext should receive it. Used for pricing AND commitment.

    ## UOM

    A Cart row carries the selling unit ERPNext ITSELF resolved the first time the
    line was priced (`sales_uom` when the Item has one, else `stock_uom`), written
    back by `sync_sales_order_to_cart`. Passing it here is what keeps the buyer's
    quantity meaning ONE thing: "2" stays 2 Strips from the product page through
    the Cart to the Sales Order, and a merchant who later changes the Item's
    `sales_uom` cannot silently reinterpret a quantity someone already chose.

    An UNPRICED row -- a line just appended by `add_to_cart` -- has no uom, and
    then nothing is sent, so ERPNext resolves it. That absence is the fix: the old
    code sent `row.uom or row.stock_uom`, which meant the stock UOM was forced
    even when the Item sold in Boxes, and the Cart charged the per-Nos rate for a
    quantity the product page had priced per Box (Phase 23B-5W).

    ## Conversion factor

    Never sent. ERPNext derives it from the Item's own UOM table on every
    calculation (`get_conversion_factor`), so a corrected conversion factor
    reaches an existing cart instead of being frozen into it. `stock_qty` follows
    from `qty * conversion_factor`, which is what Pricing Rules compare against.
    """

    item = {
        "item_code": row.item_code,
        "qty": row.quantity,
    }

    if row.uom:
        item["uom"] = row.uom

    return item


# =========================================================
# 3️⃣ SYNC SALES ORDER BACK TO CART
# =========================================================

def sync_sales_order_to_cart(cart, so):

    # Lines whose UNIT meaning moved under the buyer -- a merchant edited the
    # Item's conversion factor, or removed the UOM the row was priced in. Rare,
    # and never silent: reported through the Cart response so the buyer can be
    # told that "2" is now worth something different.
    uom_changed = []

    # -----------------------------
    # Cart Totals
    # -----------------------------
    cart.total_quantity = so.total_qty
    cart.net_total = so.net_total
    cart.tax_total = so.total_taxes_and_charges
    cart.grand_total = so.grand_total
    
    cart.coupon_discount = so.discount_amount or 0
    
    cart.total_discount = 0

    # -----------------------------
    # Map cart items by item_code -- PAID ROWS ONLY
    # -----------------------------
    # A Cart Item is customer PAID INTENT. ERPNext-generated promotion rows are
    # transient pricing output and must never be written onto it.
    #
    # This loop used to walk every Sales Order row. Two things broke as a result,
    # both reproduced in Phase 23A:
    #
    #   * a same-SKU free row (rate 0) mapped onto the SAME cart row as its paid
    #     row and, arriving last, overwrote base_price/rate/amount with ZERO --
    #     the buyer saw a free line beside a non-zero total;
    #   * a different-SKU gift had no cart row at all and was silently dropped,
    #     so an earned free product never appeared.
    #
    # Promotions are now carried by the projection built below instead.
    cart_items_map = {row.item_code: row for row in cart.items}

    for so_row in so.items:

        if so_row.get("is_free_item"):
            continue                      # promotion output, not customer intent

        cart_row = cart_items_map.get(so_row.item_code)

        if not cart_row:
            continue

        # -----------------------------
        # Unit of measure -- ERPNext's answer, recorded as the row's meaning
        # -----------------------------
        # The first pricing of a row is where the selling unit is decided
        # (`sales_uom` or `stock_uom`, chosen by ERPNext), and writing it here is
        # what turns that answer into stable buyer intent: every later reprice
        # sends this value back, so the quantity keeps meaning what it meant when
        # the buyer chose it.
        #
        # The conversion factor is stored as ERPNext's CURRENT derivation and is
        # never sent back to it. When that derivation moves -- the merchant edited
        # the Item's conversion table, or dropped the UOM the row was priced in --
        # what the stored quantity is WORTH has changed, so the line is reported
        # instead of quietly repricing. An unpriced row (no uom, or a factor of 0
        # from before this field was written) has no previous meaning to protect.
        was_priced = bool(cart_row.uom)
        previous_factor = flt(cart_row.conversion_factor)

        unit_moved = was_priced and (
            (so_row.uom and so_row.uom != cart_row.uom)
            or (previous_factor and previous_factor != flt(so_row.conversion_factor))
        )

        if unit_moved:
            uom_changed.append(cart_row.item_code)

        cart_row.uom = so_row.uom
        cart_row.stock_uom = so_row.stock_uom or cart_row.stock_uom
        cart_row.conversion_factor = so_row.conversion_factor

        # -----------------------------
        # Pricing
        # -----------------------------
        cart_row.base_price = so_row.price_list_rate
        cart_row.rate = so_row.rate
        cart_row.discount_percentage = so_row.discount_percentage
        cart_row.discount_amount = so_row.discount_amount
        cart_row.amount = so_row.net_amount

        # -----------------------------
        # Discount
        # -----------------------------
        line_discount = (so_row.discount_amount or 0) * (so_row.qty or 0)
        
        cart_row.line_discount = line_discount
        cart.total_discount += line_discount
        
        # -----------------------------
        # Tax Handling
        # -----------------------------
        cart_row.tax_amount   = get_item_tax_amount(so, so_row)
        cart_row.total_amount = so_row.net_amount + cart_row.tax_amount
  
        # -----------------------------
        # Pricing Rules
        # -----------------------------
        pricing_rules = so_row.pricing_rules

        if isinstance(pricing_rules, str):
            try:
                pricing_rules = json.loads(pricing_rules)
            except Exception:
                pricing_rules = []

        if not isinstance(pricing_rules, list):
            pricing_rules = []

        cart_row.pricing_rules = json.dumps(pricing_rules) if pricing_rules else None        
        
        # -----------------------------
        # Pricing Rule Details
        # -----------------------------
        if pricing_rules:
            rule_name = pricing_rules[0]

            try:
                rule = frappe.get_cached_doc("Pricing Rule", rule_name)

                cart_row.pricing_rule_label = rule.title or rule.name
                cart_row.pricing_rule_apply_on = rule.apply_on

            except Exception:
                cart_row.pricing_rule_label = None
                cart_row.pricing_rule_apply_on = None
        else:
            cart_row.pricing_rule_label = None
            cart_row.pricing_rule_apply_on = None

    cart.flags.uom_changed_items = sorted(set(uom_changed))

    return build_pricing_projection(so, cart)


# =========================================================
# CART PRICING PROJECTION
# =========================================================

def normalize_pricing_rules(value):
    """ERPNext's `pricing_rules` in one shape, whatever it arrives as.

    Phase 23A found paid rows carrying a JSON array (`["PRLE-0001"]`) while the
    free rows generated from the same rule carry a bare string (`PRLE-0001`).
    Parsing that assumed either format would drop half the promotions, so both
    are accepted and a list is always returned.
    """

    if not value:
        return []

    if isinstance(value, list):
        return [str(v) for v in value if v]

    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                return [text]
            if isinstance(parsed, list):
                return [str(v) for v in parsed if v]
            return [str(parsed)] if parsed else []
        return [text]

    return [str(value)]


def _ensure_gst_tax_type(so):
    """Populate India Compliance's `gst_tax_type` on the pricing order's tax rows.

    IC sets this field inside `validate_transaction`, which runs as the document
    VALIDATE event. The pricing Sales Order is deliberately never validated -- it
    is an in-memory projection that is never inserted -- so without this the field
    is empty and every GST component would come back unclassified.

    This calls IC's OWN `set_gst_tax_type`, which maps account heads through IC's
    account map. It classifies nothing here: writing a YOB lookup, or matching
    "CGST" against account names, would be a second source of truth for a
    question India Compliance already answers.

    Metadata only -- no tax is calculated, added or moved, and the call is
    idempotent. Absent IC (another site, another deployment) the components stay
    generic rather than the request failing.
    """

    if any(t.get("gst_tax_type") for t in so.get("taxes") or []):
        return

    try:
        from india_compliance.gst_india.overrides.transaction import set_gst_tax_type
    except ImportError:
        return          # India Compliance not installed; generic components stand

    set_gst_tax_type(so)


def extract_row_taxes(so):
    """Authoritative per-ROW tax, straight out of ERPNext's final calculation.

    ## Where this comes from

    `calculate_taxes_and_totals` leaves `doc._item_wise_tax_details` on the
    document: one entry per (item row, tax row) pair, carrying `rate`, `amount`
    and `taxable_amount`. YOB reads that and converts nothing of substance --
    ERPNext and India Compliance decide jurisdiction, CGST/SGST vs IGST, Item Tax
    Templates and rates. No percentage is ever applied here.

    ## Row identity, not item_code

    Each entry holds the actual item ROW OBJECT, so this groups on `id(item)`.
    `item_code` would be wrong: a same-SKU promotion produces two rows sharing one
    code, and keying on it would merge a paid row's tax onto its free row.
    ERPNext's own `get_itemised_tax()` does key by `item_code`, which is exactly
    why it cannot be reused here.

    ## Currency

    `_item_wise_tax_details` amounts are **base/company currency** -- built from
    `base_tax_amount` precision, and `adjust_rounding_in_item_wise_tax_details`
    reconciles them against `tax.base_tax_amount_after_discount_amount`. The
    storefront prices in transaction currency, so amounts are divided by
    `conversion_rate`. Returning them raw would put company-currency tax beside a
    transaction-currency rate.

    ## Rounding

    ERPNext has already pushed the rounding difference onto the last breakup row
    so the total reconciles with the tax row. Nothing is re-rounded per row here;
    values are only converted and rounded once at the transaction precision.

    Returns `{id(item_row): [component, ...]}`.
    """

    details = so.get("_item_wise_tax_details") or []
    if not details:
        return {}

    _ensure_gst_tax_type(so)

    conversion_rate = flt(so.get("conversion_rate")) or 1
    precision = so.precision("tax_amount", "taxes")

    by_row = {}

    for detail in details:
        item = detail.get("item")
        tax = detail.get("tax")
        if not item or not tax:
            continue

        # Valuation-only charges never reach the customer. ERPNext skips them in
        # its own itemised view for the same reason.
        if getattr(tax, "category", None) == "Valuation":
            continue

        amount = flt(flt(detail.get("amount")) / conversion_rate, precision)
        taxable = flt(flt(detail.get("taxable_amount")) / conversion_rate, precision)

        by_row.setdefault(id(item), []).append({
            # India Compliance sets `gst_tax_type` ("cgst"/"sgst"/"igst"/...) on
            # the tax row. Authoritative metadata, so no account-name substring
            # guessing -- and absent for non-GST charges, which stay generic
            # rather than being mislabelled as GST.
            "tax_type": (tax.get("gst_tax_type") or "").upper() or None,
            "label": tax.description or None,
            "rate": flt(detail.get("rate")),
            "amount": amount,
            "taxable_amount": taxable,
            "included_in_print_rate": 1 if tax.get("included_in_print_rate") else 0,
            "charge_type": tax.charge_type,
            # Presentation order follows the transaction's own tax rows.
            "idx": tax.idx,
        })

    for components in by_row.values():
        components.sort(key=lambda c: c["idx"])

    return by_row


def build_pricing_projection(so, cart=None):
    """The authoritative pricing RESULT for a cart, derived from the priced order.

    One row per Sales Order line, paid and promotion alike, in ERPNext's own
    order. This is what the storefront should render: the Cart child table holds
    what the customer asked for, this holds what they will actually be charged.

    Deliberately NOT keyed by `item_code`: a same-SKU promotion produces two rows
    sharing one code, so any structure indexed on it collapses them -- the exact
    defect this replaces. Identity is `item_code` + `pricing_rules` +
    `is_free_item`, per Phase 23A.

    `source_line_ids` exists so a future move to independent intent lines has
    somewhere to record provenance. Today's Cart merges duplicate SKUs, so a paid
    row maps to exactly one Cart child row and a promotion row to none.
    """

    # Paid row -> the Cart child row it came from. One entry today because the
    # Cart merges duplicate SKUs; a list because a future independent-line Cart
    # will map several.
    cart_row_names = {}
    if cart is not None:
        for row in cart.items:
            cart_row_names.setdefault(row.item_code, []).append(row.name)

    taxes_by_row = extract_row_taxes(so)
    precision = so.precision("tax_amount", "taxes")

    projection = []

    for idx, row in enumerate(so.items):
        is_free = bool(row.get("is_free_item"))
        rules = normalize_pricing_rules(row.get("pricing_rules"))

        components = taxes_by_row.get(id(row), [])
        tax_amount = flt(sum(c["amount"] for c in components), precision)

        # net_amount, NOT amount. For an INCLUSIVE tax the row `amount` already
        # contains the tax, so `amount + tax` would bill it twice (118 -> 136).
        # `net_amount` is the taxable base under both treatments, so this is
        # correct for inclusive and exclusive alike.
        total_amount = flt(flt(row.net_amount) + tax_amount, precision)

        projection.append({
            # Positional, stable within one pricing result. Not a database id and
            # not something a client may send back as authority.
            "row_id": f"{idx}",
            "line_role": "Promotion" if is_free else "Paid",
            "is_free_item": 1 if is_free else 0,

            "item_code": row.item_code,
            "item_name": row.item_name,
            "uom": row.uom,
            "stock_uom": row.stock_uom,
            "conversion_factor": row.conversion_factor,
            "qty": row.qty,
            "stock_qty": row.stock_qty,

            "base_price": row.price_list_rate,
            "rate": row.rate,
            "discount_percentage": row.discount_percentage,
            "discount_amount": row.discount_amount,
            "amount": row.amount,
            "net_amount": row.net_amount,

            # Authoritative row tax from the SAME final calculation that produced
            # the totals. A promotion row carries its own tax; it is never assumed
            # to be zero, and never inherits its paid row's.
            "tax_amount": tax_amount,
            "total_amount": total_amount,
            "tax_components": [
                {k: v for k, v in c.items() if k != "idx"} for c in components
            ],

            "pricing_rules": rules,
            "source_line_ids": cart_row_names.get(row.item_code, []) if not is_free else [],
        })

    return projection


# =========================================================
# 4️⃣ PRICE LIST RESOLUTION
# =========================================================

def get_price_list_for_customer(customer_doc, fallback=None):

    price_list = get_default_price_list(customer_doc)

    if not price_list:
        price_list = get_default_selling_price_list()

    return price_list or fallback


# =========================================================
# 5️⃣ PRICING RULE LIST (Display Only)
# =========================================================

def get_applicable_pricing_rules(customer, item_code, item_group, brand=None):
    
    today_date = getdate(today())

    customer_group, territory = frappe.db.get_value(
        "Customer",
        customer,
        ["customer_group", "territory"],
    )
 
    rules = frappe.get_all(
        "Pricing Rule",
        filters={
            "selling": 1,
            "disable": 0,
            "coupon_code_based": 0,
            "valid_from": ["<=", today()],
        },
        fields=[
            "name",
            "title",
            "apply_on",
            "applicable_for",
            "price_or_product_discount",
            "discount_percentage",
            "customer",
            "customer_group",
            "territory",
            "rate",
            "min_qty",
            "max_qty",
            "min_amt",
            "max_amt",
            "free_item",
            "free_qty",
            "is_recursive",
            "round_free_qty",
            "dont_enforce_free_item_qty",
            "valid_from",
            "valid_upto",
        ],
        order_by="min_qty asc",
    )
    
    
    offers = []
    excluded = []

    for rule in rules:

        reason = validate_pricing_rule(
            rule=rule,
            today_date=today_date,
            customer=customer,
            customer_group=customer_group,
            territory=territory,
            item_code=item_code,
            item_group=item_group,
            brand=brand,
        )

        if reason:
            excluded.append(
                {
                    "rule": rule.name,
                    "title": rule.title,
                    "reason": reason,
                }
            )
            continue

        label = get_pricing_rule_label(rule)

        if label:
            offers.append(label)

    return {
                "offers": sorted(set(offers)),
                "excluded": excluded,
           }



def validate_pricing_rule(
    rule,
    today_date,
    customer,
    customer_group,
    territory,
    item_code,
    item_group,
    brand=None,
):
    # -------------------------
    # Date Validation
    # -------------------------

    if rule.valid_from and getdate(rule.valid_from) > today_date:
        return "Rule not started yet"

    if rule.valid_upto and getdate(rule.valid_upto) < today_date:
        return "Rule expired"

    # -------------------------
    # Apply On
    # -------------------------

    if rule.apply_on == "Item Code":
        if not frappe.db.exists(
            "Pricing Rule Item Code",
            {
                "parent": rule.name,
                "item_code": item_code,
            },
        ):
            return "Item Code not matched"

    elif rule.apply_on == "Item Group":
        if not frappe.db.exists(
            "Pricing Rule Item Group",
            {
                "parent": rule.name,
                "item_group": item_group,
            },
        ):
            return "Item Group not matched"

    elif rule.apply_on == "Brand":
        if not brand:
            return "Brand not provided"

        if not frappe.db.exists(
            "Pricing Rule Brand",
            {
                "parent": rule.name,
                "brand": brand,
            },
        ):
            return "Brand not matched"

    # -------------------------
    # Applicable For
    # -------------------------

    if rule.applicable_for == "Customer":
        if rule.customer != customer:
            return "Customer not matched"

    elif rule.applicable_for == "Customer Group":
        if rule.customer_group != customer_group:
            return "Customer Group not matched"

    elif rule.applicable_for == "Territory":
        if rule.territory not in ("All Territories", territory):
            return "Territory not matched"

    return None


def get_pricing_rule_label(rule):
    """Return a user-friendly label for Pricing Rule."""

    # ---------------------------------------------------------
    # Product Discount
    # ---------------------------------------------------------
    if rule.price_or_product_discount == "Product":

        free_item = (
            frappe.db.get_value("Item", rule.free_item, "item_name")
            or rule.free_item
        )

        buy_qty = int(rule.min_qty or 1)
        free_qty = int(rule.free_qty or 1)

        label = (
            f"Buy {buy_qty} and get {free_qty} {free_item} FREE"
        )

        extras = []

        if rule.is_recursive:
            extras.append("Offer repeats")

        if rule.round_free_qty:
            extras.append("Rounded free quantity")

        if rule.dont_enforce_free_item_qty:
            extras.append("Free quantity not enforced")

        if extras:
            label += f" ({', '.join(extras)})"

        return label

    # ---------------------------------------------------------
    # Transaction Discount
    # ---------------------------------------------------------
    if rule.apply_on == "Transaction":

        if rule.discount_percentage:

            if rule.min_amt and rule.max_amt:
                return (
                    f"{rule.discount_percentage:g}% OFF "
                    f"on orders between ₹{rule.min_amt:g} and ₹{rule.max_amt:g}"
                )

            if rule.min_amt:
                return (
                    f"{rule.discount_percentage:g}% OFF "
                    f"on orders of ₹{rule.min_amt:g} or more"
                )

            if rule.max_amt:
                return (
                    f"{rule.discount_percentage:g}% OFF "
                    f"on orders up to ₹{rule.max_amt:g}"
                )

            return f"{rule.discount_percentage:g}% OFF"

        if rule.rate:
            return f"Flat price ₹{rule.rate:g}"

    # ---------------------------------------------------------
    # Item / Item Group / Brand Discount
    # ---------------------------------------------------------
    if rule.discount_percentage:

        if rule.min_qty and rule.max_qty:
            return (
                f"Buy {int(rule.min_qty)} to {int(rule.max_qty)} items "
                f"and get {rule.discount_percentage:g}% OFF"
            )

        if rule.min_qty:
            return (
                f"Buy {int(rule.min_qty)} or more items "
                f"and get {rule.discount_percentage:g}% OFF"
            )

        if rule.max_qty:
            return (
                f"Buy up to {int(rule.max_qty)} items "
                f"and get {rule.discount_percentage:g}% OFF"
            )

        return f"Get {rule.discount_percentage:g}% OFF"

    if rule.rate:

        if rule.min_qty:
            return (
                f"Buy {int(rule.min_qty)} or more items "
                f"@ ₹{rule.rate:g}"
            )

        return f"Price ₹{rule.rate:g}"

    return rule.title

# =========================================================
# 6️⃣ ITEM VALIDATION
# =========================================================

def validate_item_saleable(item_code):

    item = frappe.get_doc("Item", item_code)
    today_date = getdate(today())

    if item.disabled:
        frappe.throw(f"Item {item_code} is disabled")

    # A template is a FAMILY, not a product. ERPNext refuses to price one
    # ("please select one of its variants") and refuses an Item Price on it, so
    # the only question is where the refusal happens. Answering here keeps it a
    # storefront validation error instead of an exception surfacing from deep
    # inside ERPNext as an unexpected fault (Phase 24A reproduced that as a 500).
    if item.has_variants:
        frappe.throw(f"Item {item_code} is a template; select one of its variants")

    if not item.is_sales_item:
        frappe.throw(f"Item {item_code} is not marked as sales item")

    if item.end_of_life and getdate(item.end_of_life) < today_date:
        frappe.throw(f"Item {item_code} is past end of life")


# =========================================================
# 7️⃣ DEFAULT SELLING PRICE LIST
# =========================================================

def get_default_selling_price_list():
    return frappe.get_single_value(
        "Selling Settings",
        "selling_price_list"
    )
    
def get_item_tax_amount(so, so_row):
    if not so.total_taxes_and_charges:
        return 0

    total_net = sum(i.net_amount for i in so.items)

    if not total_net:
        return 0

    ratio = so_row.net_amount / total_net

    return so.total_taxes_and_charges * ratio