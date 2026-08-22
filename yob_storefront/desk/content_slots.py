# Copyright (c) 2026, YOB and Shayona
"""Desk pickers for the system content slot registry.

Desk-only convenience. It exists so a merchant editing a Cart placement is
offered *Above Cart* and *Below Cart* rather than every slot of every route, and
so they read `Above Cart` while the database stores `above_cart`.

It is NOT a security boundary and NOT the authority on anything. The registry in
`utils.system_slots` decides what is valid, and
`YOB Storefront Content Placement.validate` enforces it again on save -- Data
Import, the REST API and `bench execute` never load this file at all.
"""

import frappe

from yob_storefront.utils.system_slots import route_options, slot_options


@frappe.whitelist()
def get_route_options():
    """Every route that can hold content, as `[{value, label}]`."""

    # A Desk helper runs as whoever is logged into Desk. This returns application
    # structure rather than business data, but it is still gated on the DocType
    # that uses it, so a user with no access to placements learns nothing.
    frappe.has_permission("YOB Storefront Content Placement", "read", throw=True)

    return route_options()


@frappe.whitelist()
def get_slot_options(route_key=None):
    """The slots of ONE route, as `[{value, label}]`. Empty for anything else."""

    frappe.has_permission("YOB Storefront Content Placement", "read", throw=True)

    return slot_options(route_key)
