# Copyright (c) 2026, YOB and Shayona
"""One reusable Content Block, placed into one application-owned slot.

WHAT THIS DOCTYPE OWNS
----------------------
Three things and nothing else: **where**, in **what order**, and whether it is
**live**. It owns no content. The Block it points at remains responsible for its
own images, sanitised rich text, carousel rules, Product Grid configuration,
Promo Grid configuration and typed destinations -- all validated by the Block's
own controller (Phase 25B). Re-checking any of that here would create a second
opinion about a Block, and second opinions drift.

WHY THE ROUTE IS NOT FREE TEXT
------------------------------
A merchant chooses among positions the application ALREADY renders. They cannot
invent a route or a position, because Angular -- not this record -- decides where
a `<yob-content-slot>` exists. `utils.system_slots` is the single registry both
sides answer to, and this controller asks it rather than carrying a list of its
own.

THE PRODUCT GRID CAP IS PER ROUTE, NOT PER RECORD
-------------------------------------------------
A Storefront Page holds its blocks in one document, so its cap is a loop over
child rows. Placements are separate documents, so the same guarantee has to count
siblings: the fourth enabled Product Grid anywhere on a route is refused at SAVE
time, exactly as a Page refuses its fourth. Enforcing it at render time instead
would mean discovering the problem while a buyer waits.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from yob_storefront.utils.storefront_content import MAX_PRODUCT_GRIDS
from yob_storefront.utils.system_slots import (
    SlotError,
    route_label,
    slot_label,
    validate_placement,
)

DOCTYPE = "YOB Storefront Content Placement"


class YOBStorefrontContentPlacement(Document):
    def validate(self):
        self.validate_slot()
        self.validate_block()
        self.validate_not_duplicated()
        self.validate_product_grid_budget()

    # ----------------------------------------------------------------- slot

    def validate_slot(self):
        """The (route, slot) PAIR, judged by the application's own registry.

        Checking the two halves separately would accept `cart.above_listing` --
        both parts real, the combination rendered nowhere. Content stored there
        would silently never appear, which is worse than a refusal because it
        looks like it worked.
        """

        try:
            validate_placement(self.route_key, self.slot_key)
        except SlotError as exc:
            frappe.throw(exc.message, frappe.ValidationError, exc.field)

    # ---------------------------------------------------------------- block

    def validate_block(self):
        """The Block must exist. Everything ABOUT it is the Block's own business."""

        if not self.block:
            frappe.throw(_("A Content Block is required."), frappe.ValidationError)

        if not frappe.db.exists("YOB Storefront Block", self.block):
            frappe.throw(
                _("Content Block {0} does not exist.").format(self.block),
                frappe.ValidationError)

    # ------------------------------------------------------------ duplicate

    def validate_not_duplicated(self):
        """The same Block twice in the SAME slot is always a mistake.

        Everything else is legitimate reuse and must stay allowed: the same Block
        in another slot, on another route, and on a Storefront Page as well. A
        Block is authored once and placed many times -- that is the point of the
        whole design.
        """

        twin = frappe.db.exists(DOCTYPE, {
            "route_key": self.route_key,
            "slot_key": self.slot_key,
            "block": self.block,
            "name": ["!=", self.name or ""],
        })

        if twin:
            frappe.throw(
                _("{0} is already placed in {1} on {2}.")
                .format(self.block, slot_label(self.route_key, self.slot_key),
                        route_label(self.route_key)),
                frappe.DuplicateEntryError)

    # ------------------------------------------------------- pricing budget

    def validate_product_grid_budget(self):
        """At most three enabled Product Grids across the WHOLE route.

        The limit is the same constant a Storefront Page uses, and it is about
        one rendered response rather than one document: `get_route_content`
        returns every slot at once, so three grids in three different slots cost
        exactly what three grids on one page cost. Counting per slot would let a
        route quietly carry six.
        """

        if not cint(self.enabled) or self.block_type() != "Product Grid":
            return

        siblings = frappe.get_all(
            DOCTYPE,
            filters={"route_key": self.route_key, "enabled": 1,
                     "name": ["!=", self.name or ""]},
            pluck="block")

        grids = 1 + sum(1 for block in siblings
                        if frappe.db.get_value("YOB Storefront Block", block,
                                               "block_type") == "Product Grid")

        if grids > MAX_PRODUCT_GRIDS:
            frappe.throw(
                _("The {0} route may show at most {1} Product Grids; this would "
                  "make {2}. Disable or remove another Product Grid placement first.")
                .format(route_label(self.route_key), MAX_PRODUCT_GRIDS, grids),
                frappe.ValidationError)

    def block_type(self):
        return frappe.db.get_value("YOB Storefront Block", self.block, "block_type")
