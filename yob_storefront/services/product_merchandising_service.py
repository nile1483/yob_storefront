# Copyright (c) 2026, YOB and Shayona
"""Gallery and Product Content as a buyer receives them (Phase 27B).

WHAT THIS PROJECTS
------------------
The merchandising half of a Product Detail page: an ordered gallery, and ordered
content sections of ordered blocks. It carries **no money and no transaction
context** -- no rate, UOM, stock, warehouse or Pricing Rule -- because none of
that is merchandising, and `get_item` has already done that work for the parts of
the page that need it. Adding content must not add a single pricing call.

ONE OWNER, RESOLVED BEFORE ANYTHING IS READ
-------------------------------------------
    simple Item        -> its own gallery and content
    variant TEMPLATE   -> the family's gallery and content
    generated variant  -> NOTHING, and never consulted

The resolver starts from the PUBLIC product entity and stops there. It never
scans a template's variants looking for content, and it never falls back from a
child to its template -- there is no override chain, because a variant owns
nothing to override with (Phase 27A). A generated variant asked directly answers
`None`, so an accidental call fails closed instead of publishing a SKU's private
images as if they were the family's.

FAIL CLOSED, BUT DON'T TAKE THE PAGE DOWN
-----------------------------------------
Phase 27A refuses cross-product links and malformed tables at save. This layer
assumes none of that held: a direct database edit, a restored backup or a legacy
row could still present a spec group owned by another product, a table claiming
nine columns, or a link to a document that no longer exists. Every such block is
**skipped**, never published and never allowed to raise -- one bad block must not
turn a product page into a 500. A section left with no publishable blocks is
omitted entirely rather than shipped as an empty heading.

QUERY SHAPE
-----------
Bounded by the CONTENT MODEL, not by the number of blocks: at most seven queries
for a whole page regardless of how many sections or blocks exist -- gallery,
sections, blocks, spec groups, spec rows, product tables, table rows. Each of the
last four is a single batched `IN (...)` read, so a page with forty blocks costs
the same as one with two.
"""

import frappe
from frappe.utils import cint

from yob_storefront.utils.product_merchandising import (
    ALLOWED_VIDEO_SCHEMES,
    BLOCK_TYPES,
    MAX_TABLE_COLUMNS,
    MIN_TABLE_COLUMNS,
)

GALLERY_DOCTYPE = "YOB Storefront Product Gallery Image"
SECTION_DOCTYPE = "YOB Storefront Product Content Section"
BLOCK_DOCTYPE = "YOB Storefront Product Content Block"
SPEC_GROUP_DOCTYPE = "YOB Storefront Product Spec Group"
SPEC_ROW_DOCTYPE = "YOB Storefront Product Spec Row"
TABLE_DOCTYPE = "YOB Storefront Product Table"
TABLE_ROW_DOCTYPE = "YOB Storefront Product Table Row"

GALLERY_FIELD = "custom_storefront_gallery"


# =========================================================
# OWNERSHIP
# =========================================================

def merchandising_owner(item_code):
    """The one product whose merchandising a page publishes, or None.

    A simple Item and a variant template each own their own. A generated variant
    owns nothing and answers None -- callers publish empty arrays rather than
    reaching for its template, because inheriting would contradict the single
    owner the whole model is built on.
    """

    if not item_code:
        return None

    row = frappe.db.get_value("Item", item_code, ["name", "variant_of"], as_dict=True)

    if not row or row.variant_of:
        return None

    return row.name


def project_merchandising(item_code):
    """`{"gallery": [...], "sections": [...]}` for one public product.

    Both keys are always present and always arrays. A client should never have to
    tell a missing property from a null from an empty list.
    """

    owner = merchandising_owner(item_code)

    if not owner:
        return {"gallery": [], "sections": []}

    return {"gallery": project_gallery(owner), "sections": project_sections(owner)}


# =========================================================
# GALLERY
# =========================================================

def project_gallery(owner):
    """Ordered gallery rows, `sort_order` then the merchant's row order.

    A primary image is NOT moved to the front. Order and primacy are separate
    facts: the merchant chose the thumbnail sequence, and `is_primary` only says
    which one opens first. Reordering here would silently overrule them.

    Nothing synthetic is invented -- an Item with no gallery answers `[]` and the
    existing `image` field on the product payload remains what it always was, so
    a client can tell "no gallery configured" from "gallery configured".
    """

    rows = frappe.get_all(
        GALLERY_DOCTYPE,
        filters={"parent": owner, "parenttype": "Item",
                 "parentfield": GALLERY_FIELD},
        fields=["image", "alt_text", "caption", "is_primary", "sort_order", "idx"],
        order_by="sort_order asc, idx asc", limit_page_length=0)

    return [
        {
            "image": row.image,
            "alt_text": row.alt_text or None,
            "caption": row.caption or None,
            "is_primary": bool(row.is_primary),
        }
        for row in rows if row.image
    ]


# =========================================================
# SECTIONS
# =========================================================

def project_sections(owner):
    """Enabled sections with at least one publishable block, in merchant order."""

    sections = frappe.get_all(
        SECTION_DOCTYPE,
        filters={"item": owner, "enabled": 1},
        fields=["name", "title", "sort_order"],
        order_by="sort_order asc, name asc", limit_page_length=0)

    if not sections:
        return []

    blocks_by_section = _blocks_by_section([s.name for s in sections], owner)

    projected = []

    for section in sections:
        blocks = blocks_by_section.get(section.name) or []

        # An enabled section whose blocks all failed to project is a bare
        # heading. Publishing it would put an empty strip on the page with no
        # way for a buyer to tell it apart from a rendering fault.
        if not blocks:
            continue

        projected.append({"title": section.title, "blocks": blocks})

    return projected


def _blocks_by_section(section_names, owner):
    """Every block of every section, projected, in one bounded read each."""

    rows = frappe.get_all(
        BLOCK_DOCTYPE,
        filters={"parent": ["in", section_names], "parenttype": SECTION_DOCTYPE},
        fields=["parent", "block_type", "sort_order", "idx", "content",
                "spec_group", "product_table", "image", "image_alt_text",
                "image_caption", "download_file", "download_label",
                "download_description", "video_url"],
        order_by="sort_order asc, idx asc", limit_page_length=0)

    if not rows:
        return {}

    # Structured data is fetched ONCE for the whole page, keyed by document, so
    # cost tracks the content model rather than the number of blocks.
    specs = _load_spec_groups({r.spec_group for r in rows if r.spec_group}, owner)
    tables = _load_tables({r.product_table for r in rows if r.product_table}, owner)

    grouped = {}

    for row in rows:
        block = _project_block(row, specs, tables)

        if block:
            grouped.setdefault(row.parent, []).append(block)

    return grouped


def _project_block(row, specs, tables):
    """One block, or None when it cannot be published safely."""

    block_type = row.block_type

    if block_type not in BLOCK_TYPES:
        return None

    if block_type == "rich_text":
        return _rich_text(row)

    if block_type == "key_value":
        return _key_value(row, specs)

    if block_type == "table":
        return _table(row, tables)

    if block_type == "image":
        return _image(row)

    if block_type == "download":
        return _download(row)

    if block_type == "video":
        return _video(row)

    return None


# ---------------------------------------------------------- simple blocks

def _rich_text(row):
    from yob_storefront.utils.storefront_content import sanitize_rich_text

    # Sanitised AGAIN at the read boundary. Phase 27A cleans on save, but a
    # direct database edit or a restored backup never passed through it, and the
    # cost of re-cleaning is trivial next to publishing a stored script.
    content = sanitize_rich_text(row.content or "")

    if not (content or "").strip():
        return None

    return {"type": "rich_text", "content": content}


def _image(row):
    if not row.image:
        return None

    return {
        "type": "image",
        # The stored relative path, exactly as the catalogue returns media.
        "image": row.image,
        "alt_text": row.image_alt_text or None,
        "caption": row.image_caption or None,
    }


def _download(row):
    if not row.download_file:
        return None

    return {
        "type": "download",
        # The Frappe file URL as stored -- never a filesystem path, and no File
        # DocType internals.
        "file": row.download_file,
        "label": row.download_label or None,
        "description": row.download_description or None,
    }


def _video(row):
    from urllib.parse import urlparse

    url = (row.video_url or "").strip()

    # Re-checked rather than trusted: only an http(s) address is ever published,
    # so corrupted embed markup cannot reach a page as something to render.
    if "<" in url or ">" in url:
        return None

    parsed = urlparse(url)

    if parsed.scheme.lower() not in ALLOWED_VIDEO_SCHEMES or not parsed.netloc:
        return None

    return {"type": "video", "url": url}


# ------------------------------------------------------ structured blocks

def _load_spec_groups(names, owner):
    """Spec groups owned by THIS product, with their rows. One query each."""

    if not names:
        return {}

    groups = frappe.get_all(
        SPEC_GROUP_DOCTYPE,
        filters={"name": ["in", list(names)], "item": owner},
        pluck="name")

    if not groups:
        return {}

    rows = frappe.get_all(
        SPEC_ROW_DOCTYPE,
        filters={"parent": ["in", groups], "parenttype": SPEC_GROUP_DOCTYPE},
        fields=["parent", "key_label", "value_text", "sort_order", "idx"],
        order_by="sort_order asc, idx asc", limit_page_length=0)

    loaded = {name: [] for name in groups}

    for row in rows:
        if row.key_label:
            loaded[row.parent].append({"key": row.key_label,
                                       "value": row.value_text or ""})

    return loaded


def _key_value(row, specs):
    # `specs` only ever contains groups owned by this product, so a group
    # belonging to ANOTHER product is simply absent and the block is skipped --
    # cross-product merchandising is never published, even if it was stored.
    items = specs.get(row.spec_group)

    if not items:
        return None

    return {"type": "key_value", "items": items}


def _load_tables(names, owner):
    """Product tables owned by THIS product, narrowed to their active width."""

    if not names:
        return {}

    label_fields = [f"column_{n}_label" for n in range(1, MAX_TABLE_COLUMNS + 1)]

    tables = frappe.get_all(
        TABLE_DOCTYPE,
        filters={"name": ["in", list(names)], "item": owner},
        fields=["name", "column_count"] + label_fields)

    if not tables:
        return {}

    rows = frappe.get_all(
        TABLE_ROW_DOCTYPE,
        filters={"parent": ["in", [t.name for t in tables]],
                 "parenttype": TABLE_DOCTYPE},
        fields=["parent", "idx"] + [f"col_{n}" for n in range(1, MAX_TABLE_COLUMNS + 1)],
        order_by="idx asc", limit_page_length=0)

    rows_by_table = {}
    for row in rows:
        rows_by_table.setdefault(row.parent, []).append(row)

    loaded = {}

    for table in tables:
        # `column_count` is a Select, so it arrives as a STRING -- compared
        # through cint, never as an integer. A corrupted width is refused here
        # rather than producing ragged rows a client cannot render.
        width = cint(table.column_count)

        if not MIN_TABLE_COLUMNS <= width <= MAX_TABLE_COLUMNS:
            continue

        columns = [table.get(f"column_{n}_label") for n in range(1, width + 1)]

        if not all((c or "").strip() for c in columns):
            continue

        # Only the ACTIVE columns are published. Phase 27A deliberately KEEPS the
        # cells of a narrowed table so a merchant can widen it again without
        # losing work -- which makes this the layer that must hide them. Stored
        # is not published.
        loaded[table.name] = {
            "columns": columns,
            "rows": [[row.get(f"col_{n}") or "" for n in range(1, width + 1)]
                     for row in rows_by_table.get(table.name, [])],
        }

    return loaded


def _table(row, tables):
    table = tables.get(row.product_table)

    if not table:
        return None

    return {"type": "table", "columns": table["columns"], "rows": table["rows"]}
