# Copyright (c) 2026, YOB and Shayona
"""Who may own product merchandising, and what a content block may contain.

THE OWNERSHIP RULE (Phase 27A)
------------------------------
Exactly one entity owns the Gallery and the Product Content for a public product:

    simple Item          -> owns its own
    variant TEMPLATE     -> owns the whole family's
    generated variant    -> owns NOTHING, ever

There is deliberately **no fallback chain**. A variant does not inherit from its
template and does not override it, because there is nothing to inherit *from* --
the template's content simply IS the family's content, and the family is what a
buyer navigates to (Phase 24). A fallback would imply variants could hold content
of their own, which is exactly the state this module refuses to allow.

That refusal lives on the server, not in Desk visibility: Data Import, the REST
API and `bench execute` never run a Client Script, and a variant that acquired a
gallery through any of them would render a product page nobody authored.

Ownership is judged only by ERPNext's own `variant_of`, never by an item-code
pattern -- a naming convention is a coincidence, not a data model.
"""

import re
from urllib.parse import urlparse

import frappe
from frappe import _

#: The six block types a Product Content section may hold.
BLOCK_TYPES = {
    "rich_text": "Rich Text",
    "key_value": "Key / Value",
    "table": "Table",
    "image": "Image",
    "download": "Download",
    "video": "Video",
}

#: A product table is bounded rather than free-form. Frappe supports one level of
#: child table, so a table cannot own both a column list and rows of variable
#: cells; the alternatives were pasted JSON or an admin typing row indices. Two
#: to six fixed columns covers specification and comparison tables and keeps the
#: editor an ordinary grid.
MIN_TABLE_COLUMNS = 2
MAX_TABLE_COLUMNS = 6

#: Structured block data lives in its OWN document, and that document belongs to
#: exactly one product. Sharing one across products would let an edit made for
#: one product silently rewrite another's page.
OWNED_DATA_DOCTYPE = {
    "key_value": "YOB Storefront Product Spec Group",
    "table": "YOB Storefront Product Table",
}

#: The field each type genuinely needs. A block missing its own field is content
#: that would render as an empty strip on a product page.
REQUIRED_FIELD = {
    "rich_text": "content",
    "key_value": "spec_group",
    "table": "product_table",
    "image": "image",
    "download": "download_file",
    "video": "video_url",
}

#: Fields owned by each type. Everything else is cleared on save, so a block that
#: changed type cannot keep a stale image or URL that a later projection might
#: read -- the same rule Phase 25B applies to CMS blocks.
OWNED_FIELDS = {
    "rich_text": {"content"},
    "key_value": {"spec_group"},
    "table": {"product_table"},
    "image": {"image", "image_alt_text", "image_caption"},
    "download": {"download_file", "download_label", "download_description"},
    "video": {"video_url"},
}

ALL_TYPE_FIELDS = set().union(*OWNED_FIELDS.values())

#: A merchant supplies a video ADDRESS, never markup. Embed HTML would be a
#: script-injection surface authored in Desk and rendered to every buyer.
ALLOWED_VIDEO_SCHEMES = {"http", "https"}
MARKUP = re.compile(r"[<>]")


def is_generated_variant(item_code):
    """True when this Item is a generated child of a template."""

    if not item_code:
        return False

    return bool(frappe.db.get_value("Item", item_code, "variant_of"))


def reject_variant_ownership(item_code, what):
    """Refuse merchandising on a generated variant, naming where it belongs."""

    template = frappe.db.get_value("Item", item_code, "variant_of")

    if not template:
        return

    frappe.throw(
        _("{0} belongs on the variant template {1}, not on an individual variant. "
          "A buyer reaches this SKU through the family page, so the family owns "
          "the product's images and content.").format(what, template),
        frappe.ValidationError)


def validate_block(row, index, owner_item=None):
    """One content block: a known type, its own field present, nothing stale.

    `owner_item` is the product the SECTION belongs to. A block whose structured
    data lives in its own document must point at a document owned by that same
    product -- otherwise editing one product's spec group would silently rewrite
    another product's page, which is the sharing this model refuses.
    """

    block_type = row.get("block_type")

    if block_type not in BLOCK_TYPES:
        frappe.throw(
            _("Row {0}: {1} is not a content block type. Choose one of: {2}.")
            .format(index, block_type or _("(empty)"), ", ".join(BLOCK_TYPES)),
            frappe.ValidationError)

    required = REQUIRED_FIELD[block_type]

    if not row.get(required):
        frappe.throw(
            _("Row {0}: a {1} block needs its {2}.")
            .format(index, BLOCK_TYPES[block_type], _(required.replace("_", " "))),
            frappe.ValidationError)

    if block_type == "video":
        _validate_video(row.get("video_url"), index)

    if block_type == "rich_text":
        from yob_storefront.utils.storefront_content import sanitize_rich_text

        # Sanitised at the boundary a merchant can type at. The client sanitises
        # again when rendering: this is the first line of defence, not the only.
        row.content = sanitize_rich_text(row.get("content"))

    if block_type in OWNED_DATA_DOCTYPE:
        _validate_owned_data(row, index, block_type, owner_item)

    # Clear every field this type does not own, so a block that changed type
    # cannot carry a leftover value into a later projection.
    for field in ALL_TYPE_FIELDS - OWNED_FIELDS[block_type]:
        if row.get(field):
            row.set(field, None)


def _validate_owned_data(row, index, block_type, owner_item):
    """The linked structured document must belong to the SAME product.

    Reuse within one product is fine and useful -- two sections may show the same
    specification set. Reuse ACROSS products is not: it would make one product's
    content mutable from another product's admin screen, and nobody editing the
    spec group would know whose pages they were changing.
    """

    doctype = OWNED_DATA_DOCTYPE[block_type]
    linked = row.get(REQUIRED_FIELD[block_type])

    owner = frappe.db.get_value(doctype, linked, "item")

    if not owner:
        frappe.throw(
            _("Row {0}: {1} {2} does not exist.").format(index, doctype, linked),
            frappe.ValidationError)

    if owner_item and owner != owner_item:
        frappe.throw(
            _("Row {0}: {1} belongs to {2}, but this content belongs to {3}. "
              "Structured content is owned by one product; create one for {3}.")
            .format(index, linked, owner, owner_item),
            frappe.ValidationError)


def _validate_video(url, index):
    value = (url or "").strip()

    if MARKUP.search(value):
        frappe.throw(
            _("Row {0}: paste the video's address, not embed code. Markup is not "
              "accepted.").format(index),
            frappe.ValidationError)

    parsed = urlparse(value)

    if parsed.scheme.lower() not in ALLOWED_VIDEO_SCHEMES or not parsed.netloc:
        frappe.throw(
            _("Row {0}: a video needs a full http(s) address.").format(index),
            frappe.ValidationError)
