# Copyright (c) 2026, YOB and Shayona
"""A storefront page as its ordered, discriminated blocks.

WHAT A CLIENT RECEIVES
----------------------
Every block carries a machine `type` and ONLY the fields that type owns:

    image_banner · rich_text · banner_carousel · product_grid · promo_grid

No client should ever infer a block's kind from which nullable fields happen to
be populated, and no stale field from a previous type appears -- Phase 25B clears
those on save, and each projector reads only its own.

PRODUCT GRID IS NOT A QUERY ENGINE
----------------------------------
A grid is a stored, bounded question: one storefront Category, at most twelve
items, a sort the catalogue supports. Answering it is `list_items()` -- the same
Phase 22-24 service the catalogue itself uses -- so a grid inherits catalog
eligibility, variant-family collapse, selling UOM, warehouse and customer-specific
pricing whole. There is no Item query, no Item Price lookup, no Pricing Rule
evaluation and no variant logic in this file, and there must never be.

That also means grid output is CUSTOMER-SPECIFIC: it is priced through
`SellingContext` against the buyer's own price list. A fully hydrated page must
therefore never be cached across customers. See `docs/context.md`.
"""

import frappe
from frappe.utils import cint

from yob_storefront.services.storefront_destination import project_destination

BLOCK_TYPES = {
    "Image Banner": "image_banner",
    "Rich Text": "rich_text",
    "Banner Carousel": "banner_carousel",
    "Product Grid": "product_grid",
    "Promo Grid": "promo_grid",
}

#: Grid sort label -> the listing service's own sort mode. Price sorting is absent
#: because ordering by price means pricing every candidate before choosing twelve.
SORT_MODES = {
    "Newest": "newest",
    "Name A-Z": "name_asc",
    "Name Z-A": "name_desc",
}

MAX_GRID_ITEMS = 12


def get_page(slug, customer_doc):
    """A published page and its blocks, or None.

    `customer_doc` is required because a page may hold Product Grids, and a priced
    card is meaningless without the buyer it was priced for.
    """

    page = frappe.db.get_value(
        "YOB Storefront Page", {"slug": slug, "enabled": 1},
        ["name", "slug", "title", "meta_title", "meta_description"], as_dict=True)

    if not page:
        return None

    rows = frappe.get_all(
        "YOB Storefront Page Block",
        filters={"parent": page.name, "parenttype": "YOB Storefront Page", "enabled": 1},
        fields=["block", "sequence", "idx"],
        order_by="sequence asc, idx asc", limit_page_length=0)

    blocks = []

    for row in rows:
        projected = project_block(row.block, customer_doc)
        if projected:
            blocks.append(projected)

    return {
        "slug": page.slug,
        "title": page.title,
        "meta_title": page.meta_title,
        "meta_description": page.meta_description,
        "blocks": blocks,
    }


def project_block(block_name, customer_doc):
    """One block as its discriminated payload, or None when unusable."""

    block = frappe.get_cached_doc("YOB Storefront Block", block_name)

    if not block.enabled or block.block_type not in BLOCK_TYPES:
        return None

    machine_type = BLOCK_TYPES[block.block_type]

    payload = {"type": machine_type, "block_name": block.block_name}
    payload.update(PROJECTORS[machine_type](block, customer_doc))

    return payload


# =========================================================
# PROJECTORS -- each reads ONLY its own type's fields
# =========================================================

def _image_banner(block, customer_doc):
    return {
        "desktop_image": block.desktop_image or None,
        "mobile_image": block.mobile_image or None,
        "alt_text": block.alt_text or None,
        "desktop_height_px": cint(block.desktop_height_px) or None,
        "mobile_height_px": cint(block.mobile_height_px) or None,
        "destination": project_destination(block),
    }


def _rich_text(block, customer_doc):
    return {
        "title": block.content_title or None,
        # Already sanitised on save by the block controller. A client still
        # renders it through its own trusted-HTML policy; this is the first
        # boundary, not a licence to bypass the second.
        "html": block.content or "",
        "text_alignment": (block.text_alignment or "Left").lower(),
    }


def _banner_carousel(block, customer_doc):
    return {
        "auto_play": bool(cint(block.auto_play)),
        "interval_ms": cint(block.interval_ms) or None,
        "desktop_height_px": cint(block.desktop_height_px) or None,
        "mobile_height_px": cint(block.mobile_height_px) or None,
        # `idx` is the merchant's order and the only order there is.
        "slides": [_media_row(row) for row in block.slides],
    }


def _promo_grid(block, customer_doc):
    return {
        "cards_per_row": cint(block.cards_per_row) or None,
        "desktop_height_px": cint(block.desktop_height_px) or None,
        "mobile_height_px": cint(block.mobile_height_px) or None,
        "cards": [_media_row(row) for row in block.promo_cards],
    }


def _media_row(row):
    """A slide and a promo card share one shape, so they share one projector."""

    return {
        "desktop_image": row.desktop_image or None,
        "mobile_image": row.mobile_image or None,
        "title": row.title or None,
        "alt_text": row.alt_text or None,
        "destination": project_destination(row),
    }


def _product_grid(block, customer_doc):
    """The configured query, answered by the CATALOGUE, not by this module."""

    from yob_storefront.services.catalog_listing_service import (
        PricingContext,
        list_items,
    )

    payload = {
        "card_type": (block.card_type or "Square").lower(),
        "item_limit": cint(block.item_limit) or MAX_GRID_ITEMS,
        "category": None,
        "items": [],
    }

    category = frappe.db.get_value(
        "Category", block.storefront_category,
        ["name", "slug", "is_active", "is_group"], as_dict=True)

    if not category or not category.is_active or category.is_group:
        # The category was disabled or turned into a group after the block was
        # saved. An empty grid is the only safe answer: falling back to "all
        # products" would silently merchandise something nobody chose.
        return payload

    payload["category"] = category.slug

    limit = min(payload["item_limit"], MAX_GRID_ITEMS)
    ctx = PricingContext(customer_doc, qty=1)

    items, _has_more, _cursor, _scanned = list_items(
        ctx, category.name, terms=[], sort=SORT_MODES.get(block.sort_by, "name_asc"),
        page_size=limit, after_keys=None, scope_type="category",
        scope_value=category.slug)

    # Identical ListingCard rows to the catalogue's own: `price_state` "priced"
    # for a simple item, "select_options" for a variant family with every
    # monetary field null. No child variant is ever chosen to fabricate a price.
    payload["items"] = items

    return payload


PROJECTORS = {
    "image_banner": _image_banner,
    "rich_text": _rich_text,
    "banner_carousel": _banner_carousel,
    "product_grid": _product_grid,
    "promo_grid": _promo_grid,
}
