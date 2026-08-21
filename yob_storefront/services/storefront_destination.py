# Copyright (c) 2026, YOB and Shayona
"""One runtime projection for every stored destination.

Phase 25B stores a destination as a TYPE plus a link to a real record, validated
by `utils.storefront_content.apply_destination()`. This module is the other half:
it turns that stored pair into something a browser can navigate to.

WHY ONE HELPER
--------------
A menu item, a banner, a carousel slide and a promo card all answer the same
question -- *where does this go?* -- and if each projected its own answer they
would drift the first time a route changed. Every caller goes through
`project_destination()`.

WHAT ANGULAR RECEIVES
---------------------
A semantic type, the public identity of the target, and a route when one already
exists:

    {"type": "storefront_category", "target": "power-tools",
     "href": "/catalog/power-tools", "external": false, "open_in_new_tab": false}

`target` is a SLUG, never a Frappe docname: `link_category`, `link_page` and
`link_item` are database identity and never leave the server. A destination whose
target has since been disabled, unpublished or unslugged projects as `None`, so a
caller renders nothing rather than a dead link.

ROUTES
------
`href` mirrors the routes Angular already serves (`/catalog`,
`/catalog/:categorySlug`, `/catalog/item/:itemSlug`). A page destination carries
`type` + `target` and a **null href**: the dynamic page route is `/pages/:slug`,
and a client builds it from `target`. Storing that route here would put an SPA
routing decision in the backend and make a route change a data migration.
"""

import frappe

#: Stored type -> (machine type, field carrying the target)
TYPE_MAP = {
    "Home": ("home", None),
    "Catalog": ("catalog", None),
    "Storefront Category": ("storefront_category", "category"),
    "Storefront Page": ("storefront_page", "page"),
    "Product": ("product", "item"),
    "External URL": ("external_url", "url"),
}

#: Fieldname suffixes as used by menu items and by content blocks. The two spell
#: their fields differently, so callers pass the mapping they actually store.
MENU_FIELDS = {
    "type": "item_type",
    "category": "storefront_category",
    "page": "storefront_page",
    "item": None,                       # menus do not link to a single product
    "url": "external_url",
    "new_tab": "open_in_new_tab",
}

CONTENT_FIELDS = {
    "type": "link_type",
    "category": "link_category",
    "page": "link_page",
    "item": "link_item",
    "url": "link_external_url",
    "new_tab": "open_in_new_tab",
}


def project_destination(doc, fields=CONTENT_FIELDS):
    """A stored destination as an Angular-ready link, or None.

    `None` means "not clickable" and covers both a blank destination and one whose
    target is no longer publishable. Callers render the first identically to the
    second, which is the point: a merchant disabling a category should quietly
    stop the link working, not ship a 404 to buyers.
    """

    stored_type = doc.get(fields["type"])

    if not stored_type or stored_type not in TYPE_MAP:
        return None

    machine_type, target_key = TYPE_MAP[stored_type]

    if target_key is None:
        return _link(machine_type, href="/" if machine_type == "home" else "/catalog")

    fieldname = fields.get(target_key)

    if not fieldname:
        return None

    target = doc.get(fieldname)

    if not target:
        return None

    return RESOLVERS[machine_type](doc, target, fields)


def _link(machine_type, *, target=None, href=None, external=False, new_tab=False):
    return {
        "type": machine_type,
        "target": target,
        "href": href,
        "external": external,
        "open_in_new_tab": bool(new_tab),
    }


def _category(doc, target, fields):
    row = frappe.db.get_value(
        "Category", target, ["slug", "is_active", "is_group"], as_dict=True)

    if not row or not row.is_active or row.is_group or not row.slug:
        return None

    return _link("storefront_category", target=row.slug, href=f"/catalog/{row.slug}")


def _page(doc, target, fields):
    row = frappe.db.get_value(
        "YOB Storefront Page", target, ["slug", "enabled"], as_dict=True)

    if not row or not row.enabled:
        return None

    # No href: the SPA route is `/pages/:slug`, and it builds that from `target`.
    # Storing the route here would put an SPA routing decision in the backend and
    # turn a future route change into a data migration.
    return _link("storefront_page", target=row.slug, href=None)


def _product(doc, target, fields):
    """A public product page: a simple Item or a variant FAMILY, never a child.

    Phase 24 routing is authoritative -- a generated variant is reached by
    resolving attributes on its family's page, so it has no public URL of its own.
    Phase 25B refuses to STORE one; this refuses to project one even if a variant
    were created after the link was saved.
    """

    row = frappe.db.get_value(
        "Item", target,
        ["custom_slug", "variant_of", "disabled", "is_sales_item"], as_dict=True)

    if not row or row.variant_of or not row.custom_slug:
        return None

    if row.disabled or not row.is_sales_item:
        return None

    return _link("product", target=row.custom_slug, href=f"/catalog/item/{row.custom_slug}")


def _external(doc, target, fields):
    from yob_storefront.utils.storefront_content import ALLOWED_SCHEMES

    from urllib.parse import urlparse

    parsed = urlparse(target)

    # Validated on save, re-checked here: a value written before this rule existed,
    # or through a direct database edit, must never reach a browser as an href.
    if parsed.scheme.lower() not in ALLOWED_SCHEMES or not parsed.netloc:
        return None

    new_tab_field = fields.get("new_tab")

    return _link("external_url", target=target, href=target, external=True,
                 new_tab=doc.get(new_tab_field) if new_tab_field else False)


RESOLVERS = {
    "storefront_category": _category,
    "storefront_page": _page,
    "product": _product,
    "external_url": _external,
}
