# Copyright (c) 2026, YOB and Shayona
"""Bounded catalog listing: candidate selection, price eligibility, pagination.

WHY THIS EXISTS
---------------
`catalog.get_category()` loads every Item in a category and prices each one with a
throwaway Sales Order. Phase 22A measured that path: one pricing call and one
temporary Sales Order per Item, ~51 ms each, growing linearly -- 100 items took
5.1 s, and a single bad Item aborted the whole response.

This module keeps the expensive work proportional to the PAGE, not the category.

THREE STAGES
------------
1. cheap SQL candidate selection (bounded, keyset-ordered, superset of eligible)
2. exact base Item Price eligibility, using ERPNext's own ranked resolver
3. authoritative ERPNext Sales Order pricing -- unchanged, still the price authority

Stage 1 may return items Stage 2 rejects. It must never omit an item ERPNext would
price: false positives are cheap, false negatives are invisible lost products.

ELIGIBILITY RULE (business decision, Phase 22B-1)
-------------------------------------------------
An Item is catalog-visible only when an applicable **base Item Price > 0** resolves
through the customer's legitimate price-list path, evaluated BEFORE Pricing Rules.

    no Item Price + fixed-rate Pricing Rule   -> NOT visible
    Item Price 0  + Pricing Rule              -> NOT visible
    Item Price 100 + 100% discount rule       -> VISIBLE (final rate may be 0)

Phase 22A proved a fixed-rate Pricing Rule alone yields `price_list_rate = 999` with
no Item Price at all, which is exactly what this rule excludes.
"""

import base64
import binascii
import hashlib
import json

import frappe
from frappe.utils import cint, flt, today

# Cursor payloads are tiny; anything larger is not ours.
MAX_CURSOR_BYTES = 512
CURSOR_VERSION = 1

DEFAULT_PAGE_SIZE = 24
MAX_PAGE_SIZE = 48
MIN_PAGE_SIZE = 1

MAX_SEARCH_LENGTH = 100
MAX_SEARCH_TERMS = 6

# How many candidates to pull per Stage-1 round trip.
#
# Candidates can fail Stage 2 or 3, so a page of 24 is not 24 rows. Over-fetching by
# half absorbs the usual trickle of ineligible rows in one query.
CANDIDATE_OVERFETCH = 1.5
MAX_CANDIDATE_BATCH = 96

# How many candidate rows one REQUEST will examine before handing continuation back
# to the client.
#
# This is a work budget, NOT a correctness boundary -- the difference matters and an
# earlier revision got it wrong. A hard cap on the number of BATCHES ended the scan
# and then reported `has_more=false`, so a run of Stage-1 false positives longer than
# the budget made every product behind it unreachable: the client was told the
# category had ended. Regression-tested in `CandidateScanContinuationCase`.
#
# The budget remains because a single HTTP request must terminate -- a category of
# 100k ineligible rows must not pin a worker indefinitely -- but hitting it now
# produces an HONEST answer: a possibly short page, `has_more=true`, and a cursor
# positioned past everything examined. Load More resumes exactly where the scan
# stopped, so progress is guaranteed and no candidate is rescanned.
MAX_CANDIDATE_SCAN = 2000

# ORDER BY fragments. A strict allow-list keyed by a public enum -- no browser input
# ever reaches SQL ordering, and `modified` is deliberately absent (Phase 22A found
# it is the default order and reshuffles on any edit, which breaks a keyset cursor).
SORT_MODES = {
    "name_asc": {"columns": ("item_name", "name"), "sql": "i.item_name ASC, i.name ASC", "op": ">"},
    "name_desc": {"columns": ("item_name", "name"), "sql": "i.item_name DESC, i.name DESC", "op": "<"},
    "newest": {"columns": ("creation", "name"), "sql": "i.creation DESC, i.name DESC", "op": "<"},
}
DEFAULT_SORT = "name_asc"

SUPPORTED_SCOPE_TYPES = {"category"}


class ListingError(Exception):
    """An expected, client-fixable listing problem. Carries a stable code."""

    def __init__(self, code, message, field=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


# =========================================================
# REQUEST-LEVEL PRICING CONTEXT
# =========================================================

class PricingContext:
    """Everything that is constant for one listing request.

    Phase 22A found the per-item loop re-resolving the Customer document, the price
    list, Selling Settings, company, currency and transaction date once per Item.
    None of those can change within a single listing, so they are resolved once here.

    Built ONLY from trusted `auth_context` and site configuration. Nothing on this
    object comes from the browser except `qty`, which is a number.
    """

    def __init__(self, customer_doc, qty=1):
        from yob_storefront.services.pricing_service import get_price_list_for_customer
        from yob_storefront.utils.store import get_store_settings

        settings = get_store_settings()

        self.customer_doc = customer_doc
        self.customer = customer_doc.name
        self.qty = flt(qty) or 1
        self.company = settings.company
        self.currency = settings.default_currency
        self.transaction_date = today()

        # Customer -> Customer Group -> Selling Settings (Phase 22A section C).
        self.price_list = get_price_list_for_customer(customer_doc)

        selling = frappe.get_cached_doc("Selling Settings")
        self.fallback_enabled = bool(cint(selling.fallback_to_default_price_list))
        self.default_price_list = selling.selling_price_list

    @property
    def price_lists(self):
        """Every list a price could legitimately come from, for the Stage-1 filter."""
        lists = [self.price_list]
        if self.fallback_enabled and self.default_price_list:
            lists.append(self.default_price_list)
        return [pl for pl in dict.fromkeys(lists) if pl]


# =========================================================
# STAGE 2 -- EXACT BASE ITEM PRICE ELIGIBILITY
# =========================================================

def resolve_base_item_price(ctx, item_code, stock_uom, variant_of=None):
    """The applicable base Item Price for this customer, or None.

    Delegates the ranked pick to ERPNext's own `get_price_list_rate_for`, which owns
    the ordering YOB must not reimplement: customer-specific before generic, latest
    `valid_from`, batch, then UOM, with `LIMIT 1`. Reproducing that in YOB SQL would
    be a second implementation free to drift.

    Two fallbacks are layered on top, each mirroring ERPNext's own condition exactly:

    * **variant -> template**, guarded by `is None` (as `get_item_details.py:1043`
      does). A zero on the variant is a real answer and does NOT reach the template.
    * **selected list -> default selling list**, guarded by falsiness (as
      `get_item_details.py:125` does). A zero on the selected list IS falsy, so it
      DOES trigger the fallback. The two guards genuinely differ in ERPNext, and the
      difference is preserved here rather than tidied.

    Pricing Rules are deliberately not applied: eligibility is a statement about the
    base price, so a rule can never make an unpriced Item visible.

    Returns the raw rate -- including 0.0 -- and leaves the `> 0` judgement to the
    caller, so the resolver stays a faithful mirror of ERPNext.
    """

    from erpnext.stock.get_item_details import get_price_list_rate_for

    def price_ctx(price_list):
        return frappe._dict({
            "price_list": price_list,
            "customer": ctx.customer,
            "supplier": None,
            "uom": stock_uom,
            "stock_uom": stock_uom,
            "transaction_date": ctx.transaction_date,
            "qty": ctx.qty,
            "batch_no": None,
            "ignore_party": False,
            "price_list_uom_dependant": None,
        })

    def resolve_on(price_list):
        rate = get_price_list_rate_for(price_ctx(price_list), item_code)
        # `is None`, not falsiness: a stored 0 is an answer, not a miss.
        if rate is None and variant_of:
            rate = get_price_list_rate_for(price_ctx(price_list), variant_of)
        return rate

    rate = resolve_on(ctx.price_list)

    # Falsiness here on purpose -- ERPNext falls back on 0 as well as on None.
    if not rate and ctx.fallback_enabled and ctx.default_price_list \
            and ctx.default_price_list != ctx.price_list:
        fallback_rate = resolve_on(ctx.default_price_list)
        if fallback_rate:
            rate = fallback_rate

    return rate


def is_catalog_eligible(ctx, item_code, stock_uom, variant_of=None):
    """The YOB visibility rule: an applicable base Item Price strictly above zero."""

    rate = resolve_base_item_price(ctx, item_code, stock_uom, variant_of)
    return rate is not None and flt(rate) > 0


# =========================================================
# SEARCH NORMALISATION
# =========================================================

def normalize_search(raw):
    """Trim, collapse whitespace and split into AND terms.

    `%` and `_` are escaped so a buyer typing them searches for those characters
    instead of injecting LIKE wildcards -- "100%" must not match everything.
    """

    if raw is None:
        return []

    text = " ".join(str(raw).split())
    if not text:
        return []

    if len(text) > MAX_SEARCH_LENGTH:
        raise ListingError(
            "search_too_long",
            f"Search is limited to {MAX_SEARCH_LENGTH} characters.",
            field="search",
        )

    terms = [t for t in text.split(" ") if t]
    if len(terms) > MAX_SEARCH_TERMS:
        raise ListingError(
            "search_too_long",
            f"Search is limited to {MAX_SEARCH_TERMS} words.",
            field="search",
        )

    return terms


def _like_pattern(term):
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# =========================================================
# CURSOR
# =========================================================

def _binding_fingerprint(ctx, scope_type, scope_value, terms, sort, selection=None):
    """Ties a cursor to the query and customer that produced it.

    Not a security control -- scope and customer are re-authorised on every request
    regardless. This exists so a cursor from "category A / sort name_asc" cannot be
    replayed against "category B / newest" and silently return a nonsense page.

    The merchandising selection joins it for the same reason (Phase 25C): a cursor
    issued for `Material = Steel` must not resume inside a page of
    `Material = Aluminium`, where the keyset position means something else entirely.
    The selection arrives ALREADY NORMALISED -- filters sorted, values sorted and
    de-duplicated -- so `["red","blue"]` and `["blue","red"]` are one logical query
    sharing one cursor rather than two.
    """

    payload = json.dumps(
        [scope_type, scope_value, sorted(terms), sort, ctx.customer, ctx.price_list,
         selection or []],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def encode_cursor(ctx, scope_type, scope_value, terms, sort, last_row, selection=None):
    columns = SORT_MODES[sort]["columns"]
    payload = {
        "v": CURSOR_VERSION,
        "b": _binding_fingerprint(ctx, scope_type, scope_value, terms, sort, selection),
        "k": [str(last_row.get(col)) for col in columns],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor, ctx, scope_type, scope_value, terms, sort, selection=None):
    """Untrusted input. Every failure is a clean validation error, never a traceback."""

    if not cursor:
        return None

    if len(cursor) > MAX_CURSOR_BYTES:
        raise ListingError("cursor_invalid", "The pagination cursor is not valid.", field="cursor")

    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise ListingError("cursor_invalid", "The pagination cursor is not valid.", field="cursor")

    if not isinstance(payload, dict):
        raise ListingError("cursor_invalid", "The pagination cursor is not valid.", field="cursor")

    if payload.get("v") != CURSOR_VERSION:
        raise ListingError(
            "cursor_invalid", "This pagination cursor is no longer supported.", field="cursor")

    keys = payload.get("k")
    if not isinstance(keys, list) or len(keys) != 2 or not all(isinstance(k, str) for k in keys):
        raise ListingError("cursor_invalid", "The pagination cursor is not valid.", field="cursor")

    if payload.get("b") != _binding_fingerprint(
            ctx, scope_type, scope_value, terms, sort, selection):
        # Category, search, sort, customer or price list changed under the cursor.
        raise ListingError(
            "cursor_invalid",
            "The pagination cursor does not match this search. Please start again.",
            field="cursor",
        )

    return keys


# =========================================================
# STAGE 1 -- BOUNDED CANDIDATE SELECTION
# =========================================================

def fetch_candidates(ctx, category, terms, sort, after_keys, limit, selection=None):
    """One bounded page of plausible Items, in the requested deterministic order.

    `category` may be **None**, which searches the whole catalogue instead of one
    category (Phase 26A header suggestions). Every other rule below is unchanged
    and applies identically, which is the point: a globally-searched product and
    a category-listed one must be the same public entity, decided by one query
    rather than two that could drift.

    Every predicate is either a constant, a validated enum, or a bound parameter.
    The only interpolation is `sort_sql`, taken from the SORT_MODES allow-list.

    The Item Price test is a deliberately BROAD `EXISTS`: it asks "could any positive
    Item Price legitimately apply?", never "which price wins". Getting that backwards
    is the trap called out in the phase brief -- with a generic row at 100 and a
    customer-specific row at 0, the customer-specific row wins in ERPNext, so a
    `price_list_rate > 0` test ranks the WRONG row and would wrongly include the
    Item. That is fine here precisely because Stage 2 re-decides authoritatively.

    `EXISTS` rather than a join: a join against Item Price multiplies Item rows when
    several prices match, which would corrupt both the page size and the cursor.
    """

    mode = SORT_MODES[sort]
    params = {
        "today": ctx.transaction_date,
        "customer": ctx.customer,
        "limit": int(limit),
    }

    where = [
        "i.disabled = 0",
        "i.is_sales_item = 1",
        # A public card needs a public URL. A variant has no slug of its own by
        # design (Phase 24B), so this also keeps unroutable rows out.
        "IFNULL(i.custom_slug, '') != ''",
        # ONE card per family, never one per variant (Decision 2). An individual
        # variant is reached by choosing attributes on its family's page.
        "IFNULL(i.variant_of, '') = ''",
        # Manufacturer-based families have no attribute selector, so they are not
        # presentable; they fail closed rather than being listed unusable.
        "(IFNULL(i.has_variants, 0) = 0 OR i.variant_based_on = 'Item Attribute')",
        "(i.end_of_life IS NULL OR i.end_of_life = '0000-00-00' OR i.end_of_life >= %(today)s)",
    ]

    # Scoped to one category for a listing page; omitted entirely for a global
    # search. Absent is NOT "all categories via a wildcard" -- the predicate is
    # simply not applied, so no pattern reaches SQL.
    if category:
        params["category"] = category
        where.append("i.custom_category = %(category)s")

    for idx, term in enumerate(terms):
        params[f"term{idx}"] = _like_pattern(term)
        # AND across terms: "red cotton" needs both, not either.
        where.append(f"i.item_name LIKE %(term{idx})s ESCAPE '\\\\'")

    price_lists = ctx.price_lists
    if not price_lists:
        return []
    params["price_lists"] = tuple(price_lists)

    # A simple Item is listable when IT has a possible price. A FAMILY is listable
    # when at least one of its salable variants does -- a template can never carry
    # an Item Price at all (ERPNext refuses to create one), so asking about the
    # template itself would hide every variant product in the catalogue.
    #
    # Still a deliberate SUPERSET, exactly as before: false positives are filtered
    # by Stage 2, false negatives would be lost products.
    price_exists = """
        SELECT 1 FROM `tabItem Price` ip
        WHERE ip.item_code = {item}
          AND ip.selling = 1
          AND ip.price_list IN %(price_lists)s
          AND ip.price_list_rate > 0
          AND IFNULL(ip.uom, '') IN ('', {uom})
          AND (ip.customer = %(customer)s
               OR (IFNULL(ip.customer, '') = '' AND IFNULL(ip.supplier, '') = ''))
          AND IFNULL(ip.valid_from, '2000-01-01') <= %(today)s
          AND IFNULL(ip.valid_upto, '2500-12-31') >= %(today)s
          AND IFNULL(ip.batch_no, '') = ''
    """

    where.append(f"""(
        (IFNULL(i.has_variants, 0) = 0 AND EXISTS (
            {price_exists.format(item="i.name", uom="i.stock_uom")}
        ))
        OR
        (i.has_variants = 1 AND EXISTS (
            SELECT 1 FROM `tabItem` v
            WHERE v.variant_of = i.name
              AND v.disabled = 0
              AND v.is_sales_item = 1
              AND IFNULL(v.has_variants, 0) = 0
              AND (v.end_of_life IS NULL OR v.end_of_life = '0000-00-00'
                   OR v.end_of_life >= %(today)s)
              AND EXISTS (
                {price_exists.format(item="v.name", uom="v.stock_uom")}
              )
        ))
    )""")

    if after_keys:
        col = mode["columns"][0]
        op = mode["op"]
        params["k0"] = after_keys[0]
        params["k1"] = after_keys[1]
        # Strict keyset continuation on the exact ORDER BY tuple, so a row is never
        # returned twice and never skipped.
        where.append(
            f"(i.{col} {op} %(k0)s OR (i.{col} = %(k0)s AND i.name {op} %(k1)s))"
        )

    # ------------------------------------------------------------------
    # MERCHANDISING FILTERS (Phase 25C)
    #
    #     values within one filter -> OR      (an item needs any of them)
    #     different filters        -> AND     (an item needs all of them)
    #
    # One correlated EXISTS per selected filter. A JOIN would multiply a row by
    # its matching assignments -- an item with Red AND Blue would appear twice,
    # inflating the page and corrupting the keyset cursor that walks it. EXISTS
    # answers "does at least one match" and leaves the row count alone.
    #
    # This runs in STAGE 1, so filtering happens entirely before Stage 2's price
    # check and Stage 3's Sales Order: a narrower selection costs FEWER pricing
    # calls, never more.
    # ------------------------------------------------------------------
    for index, (filter_name, value_names) in enumerate(selection or []):
        params[f"filter_{index}"] = filter_name
        params[f"values_{index}"] = tuple(value_names)

        where.append(f"""EXISTS (
            SELECT 1 FROM `tabYOB Storefront Item Filter` sf_{index}
            WHERE sf_{index}.parent = i.name
              AND sf_{index}.parenttype = 'Item'
              AND sf_{index}.parentfield = 'custom_storefront_filters'
              AND sf_{index}.filter = %(filter_{index})s
              AND sf_{index}.filter_value IN %(values_{index})s
        )""")

    return frappe.db.sql(
        f"""
        SELECT i.name, i.item_name, i.custom_slug, i.image, i.stock_uom,
               i.variant_of, i.has_variants, i.creation
        FROM `tabItem` i
        WHERE {' AND '.join(where)}
        ORDER BY {mode['sql']}
        LIMIT %(limit)s
        """,
        params,
        as_dict=True,
    )


# =========================================================
# STAGE 3 + PIPELINE
# =========================================================

# Item-local problems: this Item cannot be sold right now. Skipping it is correct;
# failing the whole page because of it is the Phase 22A defect being fixed.
ITEM_LOCAL_EXCEPTIONS = (
    frappe.ValidationError,
    frappe.DoesNotExistError,
    frappe.PermissionError,
)


def family_has_sellable_variant(ctx, template):
    """Does this family have at least ONE variant a buyer could be sold?

    Stops at the first eligible variant, so the ordinary family costs one cheap
    price lookup. Only a family where NOTHING is eligible walks its whole variant
    list -- and each step is `get_price_list_rate_for`, a single query, never a
    Sales Order. No family card ever triggers the pricing engine.
    """

    from yob_storefront.services.variant_service import salable_variants

    for variant in salable_variants(template):
        stock_uom = frappe.get_cached_value("Item", variant.name, "stock_uom")

        if is_catalog_eligible(ctx, variant.name, stock_uom, template):
            return True

    return False


def family_candidate(row):
    """A family card: identity and options, deliberately no money.

    `price_state` is explicit so a client renders "Select options" from a stated
    fact rather than inferring it from missing fields. The monetary keys are
    present and null so the row keeps ONE shape across simple and family cards.
    """

    return {
        "name": row["name"],
        "item_name": row["item_name"],
        "slug": row["custom_slug"],
        "stock_uom": row["stock_uom"],
        "uom": None,
        "conversion_factor": None,
        "image": row["image"] or None,
        "has_variants": 1,
        "price_state": "select_options",
        "base_price": None,
        "rate": None,
        "discount_percentage": None,
        "discount_amount": None,
        "net_amount": None,
        "tax_amount": None,
        "total_amount": None,
        "pricing_rule_label": None,
    }


def price_candidate(ctx, row):
    """Stage 3: authoritative ERPNext pricing for one candidate, or None to skip.

    Only item-local exceptions are absorbed. A database, programming or system fault
    propagates and fails the request, because silently returning a short page while
    the backend is broken is worse than an error.
    """

    from yob_storefront.services.pricing_service import get_item_pricing

    try:
        pricing = get_item_pricing(
            customer=ctx.customer_doc,
            item_code=row["name"],
            qty=ctx.qty,
            company=ctx.company,
            currency=ctx.currency,
        )
    except ITEM_LOCAL_EXCEPTIONS:
        # Sanitised on purpose: the item code is ours, the exception text is not.
        frappe.log_error(
            title="YOB catalog listing: item skipped",
            message=f"item={row['name']}\n\n{frappe.get_traceback()}",
        )
        return None

    return {
        "name": row["name"],
        "item_name": row["item_name"],
        "slug": row["custom_slug"],
        "stock_uom": row["stock_uom"],
        # A simple product states its condition too, so a client reads ONE shape
        # for both kinds of card instead of inferring from missing keys.
        "has_variants": 0,
        "price_state": "priced",
        # The unit the price above is PER. `get_item_pricing` prices in the UOM
        # ERPNext resolves for a selling transaction (`sales_uom` when set), so a
        # listing card that showed only `stock_uom` would label a per-Box price
        # as a per-Nos one.
        "uom": pricing["uom"],
        "conversion_factor": pricing["conversion_factor"],
        "image": row["image"] or None,
        "base_price": pricing["base_price"],
        "rate": pricing["rate"],
        "discount_percentage": pricing["discount_percentage"],
        "discount_amount": pricing["discount_amount"],
        "net_amount": pricing["net_amount"],
        "tax_amount": pricing["tax_amount"],
        "total_amount": pricing["total_amount"],
        "pricing_rule_label": pricing["pricing_rule_label"],
    }


def _keys_of(row, sort):
    return [str(row.get(col)) for col in SORT_MODES[sort]["columns"]]


def list_items(ctx, category, terms, sort, page_size, after_keys, scope_type, scope_value,
               selection=None):
    """Run the pipeline until the page is filled, the budget is spent, or candidates
    are exhausted -- and report truthfully which of the three happened.

    Fills `page_size + 1`. The extra row establishes `has_more` by proving a further
    Item survives the FULL pipeline -- Stage 2 and Stage 3 -- rather than merely
    existing as a raw row that might turn out ineligible.

    THREE OUTCOMES, THREE CURSORS
    -----------------------------
    * **page filled** -- `has_more=true`, cursor at the last RETURNED Item. The
      lookahead sits just past it and is re-examined as the first row of the next
      page, so it is never skipped.
    * **scan budget spent** -- `has_more=true`, cursor at the last EXAMINED
      candidate. The page may be short or even empty, which is honest: the scan
      found no more products *yet*. Resuming past the examined region is what
      guarantees forward progress and stops Load More re-walking the same false
      positives forever.
    * **candidates exhausted** -- `has_more=false`, no cursor. The only terminal
      state, and it is reached only by genuinely running out of rows.

    The distinction is the whole point: an earlier revision collapsed the middle case
    into the last one, which stranded every product behind a long run of Stage-1
    false positives.
    """

    wanted = page_size + 1
    collected = []
    keys_by_item = {}
    keys = after_keys
    scanned = 0
    last_scanned_keys = None
    exhausted = False
    budget_spent = False

    batch_size = min(MAX_CANDIDATE_BATCH, max(page_size, int(page_size * CANDIDATE_OVERFETCH)))

    while len(collected) < wanted:
        rows = fetch_candidates(ctx, category, terms, sort, keys, batch_size, selection)
        if not rows:
            exhausted = True
            break

        for row in rows:
            scanned += 1
            last_scanned_keys = _keys_of(row, sort)

            # Stage 2 before Stage 3: never build a Sales Order for an Item with no
            # valid base price. This is what keeps expensive work on the page rather
            # than on the category.
            if row.get("has_variants"):
                # A FAMILY card. It is listable when it can actually sell
                # something, and it is never priced: ERPNext refuses an Item Price
                # on a template, and inventing a "representative" rate from one
                # variant would quote a number no buyer is charged. Pricing every
                # variant instead would restore the unbounded per-item loop Phase
                # 22B removed. So the card carries a state, not a price
                # (Decision 2).
                if not family_has_sellable_variant(ctx, row["name"]):
                    continue
                priced = family_candidate(row)
            else:
                if not is_catalog_eligible(ctx, row["name"], row["stock_uom"],
                                           row.get("variant_of")):
                    continue
                priced = price_candidate(ctx, row)

            if priced is not None:
                collected.append(priced)
                keys_by_item[priced["name"]] = last_scanned_keys
                if len(collected) >= wanted:
                    break

        if len(collected) >= wanted:
            break

        if len(rows) < batch_size:
            exhausted = True           # a short batch means the query ran dry
            break

        keys = _keys_of(rows[-1], sort)

        if scanned >= MAX_CANDIDATE_SCAN:
            budget_spent = True
            break

    filled = len(collected) > page_size
    page = collected[:page_size]

    if filled:
        has_more = True
        resume_keys = keys_by_item.get(page[-1]["name"])
    elif budget_spent:
        # NOT exhaustion. Hand back a cursor past everything examined so the client
        # can continue; claiming `has_more=false` here is the bug this replaces.
        has_more = True
        resume_keys = last_scanned_keys
    else:
        has_more = False
        resume_keys = None

    next_cursor = None
    if has_more and resume_keys:
        next_cursor = encode_cursor(
            ctx, scope_type, scope_value, terms, sort,
            dict(zip(SORT_MODES[sort]["columns"], resume_keys)),
            selection,
        )

    if has_more and not next_cursor:
        # A cursorless `has_more=true` is a dead end for the client. Nothing should
        # reach this, so treat it as exhaustion rather than stranding them.
        has_more = False

    return page, has_more, next_cursor, scanned
