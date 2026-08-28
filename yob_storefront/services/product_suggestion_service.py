# Copyright (c) 2026, YOB and Shayona
"""Header typeahead: a few public products, for navigation only (Phase 26A).

WHAT THIS IS
------------
Eight products at most, enough to render a dropdown and open a product page.
It is not a listing, not a results page, and not a miniature catalogue: there is
no pagination, no cursor, no facets, no scope and deliberately **no money**.

THE ONE RULE THAT MATTERS
-------------------------
A suggestion must be the SAME public product the catalogue would list. Building a
second, cheaper notion of "searchable" would create a parallel product universe --
products that autocomplete offers but a category page never shows, or the reverse --
and the two would drift silently. So this module owns no eligibility rules at all.
It reuses, in order:

    Stage 1  catalog_listing_service.fetch_candidates(category=None, ...)
             the identical bounded SQL: disabled, is_sales_item, public slug,
             family collapse (`variant_of` empty), manufacturer fail-closed,
             end-of-life, the same AND-across-terms `item_name` search, and the
             same broad Item Price EXISTS.

    Stage 2  is_catalog_eligible()          for a simple Item
             family_has_sellable_variant()  for a template
             the authoritative visibility rule -- an applicable base Item Price
             above zero, decided by ERPNext's own `get_price_list_rate_for`.

WHAT IT DELIBERATELY SKIPS
--------------------------
**Stage 3.** `price_candidate` builds a throwaway ERPNext Sales Order per product
-- Phase 22A measured ~51 ms each -- and exists only to produce rates, taxes and
UOM. A suggestion shows none of those, so no Sales Order is ever constructed
here. That is a saving in WORK, never a weakening of the RULE: eligibility is a
statement about the base price (Stage 2), and Stage 3 can neither add nor remove
a product from the catalogue.

The remaining cost is therefore one Stage-1 query plus at most a bounded handful
of `get_price_list_rate_for` lookups -- the same cheap call the listing makes,
and never more of them than the scan budget below allows.
"""

import frappe

#: Angular starts asking at three characters; the server enforces the same floor
#: rather than trusting it. Below this nothing is queried at all -- a one-letter
#: typeahead against a whole catalogue is the request that hurts.
MIN_SEARCH_LENGTH = 3

#: A dropdown, not a page. Server-owned and fixed: there is no parameter for it,
#: so no client can ask for a thousand.
MAX_SUGGESTIONS = 8

#: Candidates examined before giving up. Stage 1 is a superset, so some rows fail
#: Stage 2; this bounds the work when a term matches many ineligible products.
#: Small on purpose -- a typeahead that has not found eight products in this many
#: candidates should return what it has rather than keep a worker busy while
#: someone is still typing.
MAX_SUGGESTION_SCAN = 48


def suggest_products(ctx, terms):
    """Up to `MAX_SUGGESTIONS` public products matching every term.

    Ordering is the catalogue's own `name_asc` -- `item_name`, then `name` as a
    tie-break -- so the same query always answers in the same order. There is no
    relevance ranking in the catalogue today and none is invented here; inventing
    one would make suggestion order disagree with the listing a click away.
    """

    if not terms:
        return []

    # Imported at CALL time, through the module, deliberately. The catalogue's
    # helpers are the ones that must run, and binding them at import would make a
    # spy on `catalog_listing_service` silently miss -- so a test asserting "the
    # shared eligibility rule was used" could pass while it was not. Same pattern
    # as `content_service._product_grid`.
    from yob_storefront.services import catalog_listing_service as catalog

    suggestions = []
    scanned = 0

    # One round is normally enough: 8 wanted, a batch of up to the scan budget.
    # The loop exists only for the case where Stage 1 returns rows that Stage 2
    # rejects, and it stops as soon as the budget is spent.
    rows = catalog.fetch_candidates(
        ctx, None, terms, catalog.DEFAULT_SORT, None, MAX_SUGGESTION_SCAN)

    for row in rows:
        if len(suggestions) >= MAX_SUGGESTIONS or scanned >= MAX_SUGGESTION_SCAN:
            break

        scanned += 1

        if not _is_publishable(ctx, row):
            continue

        suggestions.append(_suggestion(row))

    return suggestions


def _is_publishable(ctx, row):
    """Stage 2, asked the same way the listing asks it.

    A family is publishable when at least ONE of its variants could be sold --
    a template carries no Item Price of its own, so asking about the template
    would hide every variant product in the catalogue.
    """

    from yob_storefront.services import catalog_listing_service as catalog

    if row["has_variants"]:
        return catalog.family_has_sellable_variant(ctx, row["name"])

    return catalog.is_catalog_eligible(
        ctx, row["name"], row["stock_uom"], row["variant_of"])


def _suggestion(row):
    """The lightweight row a dropdown needs, and nothing else.

    No rate, no UOM, no stock, no warehouse, no discount, no tax. The product
    page is authoritative once the buyer clicks, and a price shown in a dropdown
    would be a second place for money to be wrong.

    `image` keeps the catalogue's own convention -- the stored relative path, or
    None -- so a client's existing media helper works unchanged.
    """

    return {
        "item_code": row["name"],
        "item_name": row["item_name"],
        "slug": row["custom_slug"],
        "image": row["image"] or None,
        # The one type fact a client needs: a family opens an options page rather
        # than an add-to-cart page. `bool` rather than 0/1 because it is a flag,
        # not a quantity.
        "is_template": bool(row["has_variants"]),
    }
