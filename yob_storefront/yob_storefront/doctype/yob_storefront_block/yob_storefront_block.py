# Copyright (c) 2026, YOB and Shayona
"""A reusable piece of storefront content.

FIVE TYPES, ONE DOCUMENT
------------------------
`block_type` discriminates. Every type owns a set of fields, and the fields of
the OTHER four are cleared on save -- the prototype cleared them only for Product
Grid, which left a block that had changed type carrying a stale image or a stale
category that a projection could still read. Clearing is the cleanest of the
three options (clear / ignore in projection / reject) because it removes the
ambiguity from the stored data rather than from one reader of it.

WHAT A BLOCK IS NOT
-------------------
Not a pricing engine and not a layout builder. A Product Grid stores a BOUNDED
QUERY -- one storefront Category, at most twelve items, a sort the catalogue can
actually do -- and Phase 25C will run it through the existing `list_items()` so a
grid inherits Phase 22-24 behaviour whole: simple-item pricing, family cards with
`price_state = select_options`, selling UOM, customer price list, catalog
eligibility. No pricing code appears here, now or later.

`Promo Grid` is deliberately not called `Offer Grid`: in YOB an offer is an
ERPNext Pricing Rule, and a promo card is a picture with a link.

`Target Progress` stores WHICH targets a storefront surface exposes and WHEN.
Those `Show From` / `Show Until` dates are a THIRD date concept, and the whole
point of putting them here rather than on `YOB Target Rule`:

    Target Period   which sales count          (YOB Target Period)
    Report Window   when recalculation runs    (YOB Target Rule)
    Show From/Until when a block displays it   (HERE)

A January target may still be shown on 10 February, long after its sales period
closed and while its report window is still open. One rule may also be exposed by
several blocks on different dates. None of that is a property of the rule, so
none of it is stored on the rule -- and a block never writes to one.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, getdate

from yob_storefront.utils.storefront_content import (
    apply_destination,
    bounded_int,
    sanitize_rich_text,
)

#: One destination shape for menus and content alike. Stored as a type plus a
#: link to a real record; Phase 25C turns that into an Angular route. A merchant
#: never types an internal URL, and no route-building lives in Desk JS.
DESTINATION_FIELD_MAP = {
    "Storefront Category": "link_category",
    "Storefront Page": "link_page",
    "Product": "link_item",
    "External URL": "link_external_url",
}

IMAGE_BANNER = "Image Banner"
RICH_TEXT = "Rich Text"
BANNER_CAROUSEL = "Banner Carousel"
PRODUCT_GRID = "Product Grid"
PROMO_GRID = "Promo Grid"
TARGET_PROGRESS = "Target Progress"

BLOCK_TYPES = (IMAGE_BANNER, RICH_TEXT, BANNER_CAROUSEL, PRODUCT_GRID, PROMO_GRID,
               TARGET_PROGRESS)

#: Every type's own fields. Anything not listed for the current type is cleared.
TYPE_FIELDS = {
    IMAGE_BANNER: {"desktop_image", "mobile_image", "alt_text",
                   "link_type", "link_category", "link_page", "link_item",
                   "link_external_url", "open_in_new_tab",
                   "desktop_height_px", "mobile_height_px"},
    RICH_TEXT: {"content_title", "text_alignment", "content"},
    BANNER_CAROUSEL: {"slides", "auto_play", "interval_ms",
                      "desktop_height_px", "mobile_height_px"},
    PRODUCT_GRID: {"storefront_category", "card_type", "item_limit", "sort_by"},
    PROMO_GRID: {"promo_cards", "cards_per_row",
                 "desktop_height_px", "mobile_height_px"},
    TARGET_PROGRESS: {"target_rows"},
}

TABLE_FIELDS = {"slides", "promo_cards", "target_rows"}

#: The sorts `catalog_listing_service` supports. Price sorting is absent on
#: purpose: a price is produced per customer by an ERPNext Sales Order, so
#: ordering by it would mean pricing every candidate before choosing twelve.
SORT_OPTIONS = {"Newest", "Name A-Z", "Name Z-A"}

MAX_GRID_ITEMS = 12
MIN_INTERVAL_MS = 2000
MAX_INTERVAL_MS = 15000
MIN_HEIGHT_PX = 80
MAX_HEIGHT_PX = 1200


class YOBStorefrontBlock(Document):
    def validate(self):
        self.validate_block_type()
        self.clear_other_type_fields()
        self.validate_media_rows()
        self.validate_type_rules()
        self.validate_target_rows()
        self.validate_display_settings()

    def validate_block_type(self):
        if self.block_type not in BLOCK_TYPES:
            frappe.throw(
                _("Invalid Block Type: {0}").format(self.block_type), frappe.ValidationError)

    def clear_other_type_fields(self):
        """Only this type's fields survive a save."""

        if self.block_type != IMAGE_BANNER:
            # Blocks other than the banner are not clickable as a whole: a
            # carousel and a promo grid carry their links per row.
            self.link_type = None

        mine = TYPE_FIELDS[self.block_type]

        for block_type, fields in TYPE_FIELDS.items():
            if block_type == self.block_type:
                continue

            for fieldname in fields - mine:
                if fieldname in TABLE_FIELDS:
                    self.set(fieldname, [])
                else:
                    self.set(fieldname, None)

    def validate_target_rows(self):
        """Which targets this block exposes, and between which display dates.

        Read-only with respect to Target Management: nothing here writes to a
        `YOB Target Rule` or a `YOB Customer Target`. A block decides what is
        SHOWN; the rule decides what is EARNED.
        """

        if self.block_type != TARGET_PROGRESS:
            return

        if not self.target_rows:
            frappe.throw(
                _("A Target Progress block needs at least one Target Rule."),
                frappe.ValidationError)

        seen = {}

        for row in self.target_rows:
            if not row.target_rule:
                frappe.throw(
                    _("Row {0}: Target Rule is required.").format(row.idx),
                    frappe.ValidationError)

            if not row.show_from:
                frappe.throw(
                    _("Row {0}: Show From is required.").format(row.idx),
                    frappe.ValidationError)

            if not row.show_until:
                frappe.throw(
                    _("Row {0}: Show Until is required.").format(row.idx),
                    frappe.ValidationError)

            if getdate(row.show_from) > getdate(row.show_until):
                frappe.throw(
                    _("Row {0}: Show From {1} cannot be after Show Until {2}.").format(
                        row.idx, row.show_from, row.show_until),
                    frappe.ValidationError)

            # One rule, one row. Two rows for the same rule would either show a
            # customer the same card twice or make the answer depend on which row
            # a reader happened to evaluate first -- and a merchant wanting two
            # display windows for one rule is describing two blocks.
            if row.target_rule in seen:
                frappe.throw(
                    _("Target Rule {0} appears in rows {1} and {2}. List a rule once "
                      "per block.").format(row.target_rule, seen[row.target_rule], row.idx),
                    frappe.ValidationError)

            seen[row.target_rule] = row.idx

            rule = frappe.db.get_value(
                "YOB Target Rule", row.target_rule,
                ["customer_visible", "docstatus"], as_dict=True)

            if not rule:
                frappe.throw(
                    _("Row {0}: Target Rule {1} does not exist.").format(
                        row.idx, row.target_rule),
                    frappe.ValidationError)

            # The rule's own master switch. Refused at configuration time as well
            # as re-checked at projection: a merchant should find out while
            # building the block, not discover a silently empty one later.
            if not rule.customer_visible:
                frappe.throw(
                    _("Row {0}: Target Rule {1} is not marked Customer Visible, so it "
                      "cannot be shown in the storefront.").format(
                        row.idx, row.target_rule),
                    frappe.ValidationError)

            if cint(rule.docstatus) != 1:
                frappe.throw(
                    _("Row {0}: Target Rule {1} is not submitted.").format(
                        row.idx, row.target_rule),
                    frappe.ValidationError)

    def validate_media_rows(self):
        """Slides and promo cards share one shape, so they share one check."""

        rows = (self.slides or []) if self.block_type == BANNER_CAROUSEL else \
               (self.promo_cards or []) if self.block_type == PROMO_GRID else []

        for row in rows:
            if not row.desktop_image:
                frappe.throw(
                    _("Row {0}: a desktop image is required.").format(row.idx),
                    frappe.ValidationError)

            apply_destination(row, type_field="link_type",
                              field_map=DESTINATION_FIELD_MAP,
                              new_tab_field="open_in_new_tab")

    def validate_type_rules(self):
        handler = {
            IMAGE_BANNER: self._image_banner,
            RICH_TEXT: self._rich_text,
            BANNER_CAROUSEL: self._banner_carousel,
            PRODUCT_GRID: self._product_grid,
            PROMO_GRID: self._promo_grid,
            TARGET_PROGRESS: self._target_progress,
        }[self.block_type]

        handler()

    def _target_progress(self):
        """The block-level rule: a Target Progress block must configure targets.

        Per-row validation lives in `validate_target_rows()`, which runs first --
        this is the same split the other types use between their row checks and
        their own.
        """

        if not self.target_rows:
            frappe.throw(
                _("A Target Progress block needs at least one Target Rule."),
                frappe.ValidationError)

    def _image_banner(self):
        if not self.desktop_image:
            frappe.throw(_("A desktop image is required."), frappe.ValidationError)

        apply_destination(self, type_field="link_type",
                          field_map=DESTINATION_FIELD_MAP,
                          new_tab_field="open_in_new_tab")

    def _rich_text(self):
        if not self.content:
            frappe.throw(_("Content is required."), frappe.ValidationError)

        # Stored clean. Angular's sanitizer stays the last line of defence, not
        # the only one -- an API that ships script and hopes every consumer
        # strips it has already lost.
        self.content = sanitize_rich_text(self.content)

        if not frappe.utils.strip_html(self.content or "").strip():
            frappe.throw(
                _("Content is empty once unsafe markup is removed."), frappe.ValidationError)

    def _banner_carousel(self):
        if not (self.slides or []):
            frappe.throw(_("Add at least one slide."), frappe.ValidationError)

        if cint(self.auto_play):
            self.interval_ms = bounded_int(
                self.interval_ms or MIN_INTERVAL_MS, field=_("Interval (ms)"),
                minimum=MIN_INTERVAL_MS, maximum=MAX_INTERVAL_MS, required=True)
        else:
            self.interval_ms = None

    def _product_grid(self):
        if not self.storefront_category:
            frappe.throw(_("A storefront Category is required."), frappe.ValidationError)

        category = frappe.db.get_value(
            "Category", self.storefront_category, ["is_active", "is_group"], as_dict=True)

        if not category or not category.is_active:
            frappe.throw(
                _("Category {0} is not active.").format(self.storefront_category),
                frappe.ValidationError)

        if category.is_group:
            frappe.throw(
                _("Category {0} holds sub-categories rather than products.")
                .format(self.storefront_category), frappe.ValidationError)

        # `or MAX_GRID_ITEMS` would have turned an explicit 0 into 12 -- a
        # merchant who typed "none" would silently get a full grid. The DocType
        # default supplies 12 for a blank field, so anything reaching here is a
        # real choice and is judged on its own.
        self.item_limit = bounded_int(
            self.item_limit, field=_("Item Limit"),
            minimum=1, maximum=MAX_GRID_ITEMS, required=True)

        if self.sort_by and self.sort_by not in SORT_OPTIONS:
            frappe.throw(
                _("Sort {0} is not supported by the catalogue.").format(self.sort_by),
                frappe.ValidationError)

    def _promo_grid(self):
        if not (self.promo_cards or []):
            frappe.throw(_("Add at least one promo card."), frappe.ValidationError)

        if str(self.cards_per_row or "") not in {"1", "2", "3"}:
            frappe.throw(_("Cards Per Row must be 1, 2 or 3."), frappe.ValidationError)

    def validate_display_settings(self):
        for fieldname, label in (("desktop_height_px", _("Desktop Height (px)")),
                                 ("mobile_height_px", _("Mobile Height (px)"))):
            if self.get(fieldname):
                self.set(fieldname, bounded_int(
                    self.get(fieldname), field=label,
                    minimum=MIN_HEIGHT_PX, maximum=MAX_HEIGHT_PX))
