# Copyright (c) 2026, YOB and Shayona
"""Shared safety rules for merchant-authored destinations and HTML.

Two things a merchant types into Desk can reach a buyer's browser directly: a
LINK and a block of HTML. Both are validated here, once, so every DocType that
accepts them agrees -- and so the rules are testable without a Desk session.

DESTINATIONS
------------
A link is either an INTERNAL route (`/catalog/hand-tools`) or an EXTERNAL
`http`/`https` URL. Nothing else. `javascript:`, `data:`, `vbscript:` and
scheme-relative `//evil.example` are refused: each is a way to run script or
exfiltrate a session from something that merely looks like a link. Refusal
happens on SAVE, so a bad value never reaches a projection, let alone a browser.

HTML
----
Rich Text is stored sanitised. Angular's own sanitizer is the last line of
defence, not the only one -- an API that returns script and relies on every
consumer to strip it has already lost the argument. `frappe.utils.sanitize_html`
is Frappe's own bleach-based cleaner, so YOB adds no second HTML policy.
"""

import re
from urllib.parse import urlparse

import frappe
from frappe import _

#: Schemes a merchant-supplied external link may use.
ALLOWED_SCHEMES = {"http", "https"}

#: An internal route: a single leading slash, then safe path characters. The
#: second character may not be a slash -- `//host` is scheme-relative and would
#: leave the storefront entirely.
INTERNAL_ROUTE = re.compile(r"^/(?!/)[A-Za-z0-9/_\-.~%?=&+:@]*$")

#: At most three Product Grids in ONE rendered content context, at most twelve
#: items each: 36 priced items is what a single request can build inside the
#: Phase 22B performance envelope.
#:
#: It lives here, not beside either placement mechanism, because there are now
#: two of them -- a Storefront Page and a route's System Content Placements --
#: and a buyer paying for a slow response does not care which one produced it.
#: Two copies of "3" would drift the first time one was tuned.
MAX_PRODUCT_GRIDS = 3

#: Slug / key shapes. Deliberately narrow: these become URL segments.
KEY_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MACHINE_KEY_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


def validate_key(value, label, pattern=KEY_PATTERN):
    """A public identity must be predictable. Reject anything else."""

    if not value:
        return

    if not pattern.fullmatch(value):
        frappe.throw(
            _("{0} may contain only lowercase letters, numbers and separators: {1}")
            .format(label, value),
            frappe.ValidationError,
        )


def validate_destination(url, label=None):
    """An internal route or an http(s) URL. Anything else is refused."""

    label = label or _("Link")
    url = (url or "").strip()

    if not url:
        return None

    if url.startswith("/"):
        if not INTERNAL_ROUTE.fullmatch(url):
            frappe.throw(
                _("{0} is not a valid internal route: {1}").format(label, url),
                frappe.ValidationError,
            )
        return url

    parsed = urlparse(url)

    if parsed.scheme.lower() not in ALLOWED_SCHEMES or not parsed.netloc:
        # Covers javascript:, data:, vbscript:, file:, mailto: and bare text.
        frappe.throw(
            _("{0} must be an internal route starting with / or an http(s) URL.").format(label),
            frappe.ValidationError,
        )

    return url


def sanitize_rich_text(html):
    """Merchant HTML, stripped of anything executable, using Frappe's cleaner."""

    if not html:
        return html

    from frappe.utils import sanitize_html

    return sanitize_html(html)


def bounded_int(value, *, field, minimum, maximum, required=False):
    """An integer inside stated bounds, or a clear refusal naming the field."""

    from frappe.utils import cint

    if value in (None, ""):
        if required:
            frappe.throw(_("{0} is required.").format(field), frappe.ValidationError)
        return None

    number = cint(value)

    if number < minimum or number > maximum:
        frappe.throw(
            _("{0} must be between {1} and {2}.").format(field, minimum, maximum),
            frappe.ValidationError,
        )

    return number


# =========================================================
# TYPED DESTINATIONS
# =========================================================
#
# ONE destination concept for menus and for clickable content. Both once carried
# their own idea of "a link" -- a menu had typed targets while a banner had a free
# text box -- which is two routing systems waiting to disagree. A merchant should
# never have to know an Angular route, and Angular should never have to guess what
# a merchant meant.
#
# What is STORED is a type plus a link to a real record. What Angular receives is
# a resolved route, built by the Phase 25C projection. No route-building lives in
# Desk JavaScript, and none lives here either: this module only proves the target
# is real and publishable.

#: Types that need no target: the route is implied by the type itself.
#:
#: `All Products` joins them in Phase 28C. It carries no field of its own on
#: purpose -- the route is fixed by the type, so there is nothing for a merchant
#: to mistype and nothing for a projection to re-validate.
IMPLIED_ROUTE_TYPES = {"", None, "None", "Home", "Catalog", "All Products", "Group"}


def apply_destination(doc, *, type_field, field_map, new_tab_field=None):
    """Validate one typed destination and clear every other type's field.

    `field_map` maps a destination TYPE to the fieldname that carries its target,
    e.g. ``{"Storefront Category": "link_category"}``. A type absent from the map
    is an implied route (Home, Catalog, All Products) and needs no target.

    Clearing the other types' fields is what stops a destination that changed type
    from leaving a stale target behind for a projection to read later.
    """

    destination_type = doc.get(type_field)
    keep = field_map.get(destination_type)

    for fieldname in field_map.values():
        if fieldname != keep:
            doc.set(fieldname, None)

    if new_tab_field and destination_type != "External URL":
        doc.set(new_tab_field, 0)

    if destination_type in IMPLIED_ROUTE_TYPES:
        return

    if not keep:
        frappe.throw(
            _("Unsupported destination type: {0}").format(destination_type),
            frappe.ValidationError)

    target = doc.get(keep)

    if not target:
        frappe.throw(
            _("{0} is required for a {1} destination.")
            .format(_(doc.meta.get_label(keep)), destination_type),
            frappe.ValidationError)

    VALIDATORS[destination_type](doc, keep, target)


def _validate_category(doc, fieldname, target):
    """A destination category must be one a buyer can actually browse."""

    category = frappe.db.get_value(
        "Category", target, ["is_active", "is_group"], as_dict=True)

    if not category:
        frappe.throw(_("Category {0} does not exist.").format(target), frappe.ValidationError)

    if not category.is_active:
        frappe.throw(_("Category {0} is not active.").format(target), frappe.ValidationError)

    if category.is_group:
        # A group category holds sub-categories, not products, and the catalogue
        # answers `category_not_listable` for it (Phase 22B).
        frappe.throw(
            _("Category {0} holds sub-categories rather than products.").format(target),
            frappe.ValidationError)


def _validate_page(doc, fieldname, target):
    if not frappe.db.exists("YOB Storefront Page", target):
        frappe.throw(_("Page {0} does not exist.").format(target), frappe.ValidationError)


def _validate_product(doc, fieldname, target):
    """Only an item that is genuinely a public storefront product.

    Phase 24 routing is authoritative: a public URL belongs to a simple Item or to
    a variant FAMILY, and a generated variant is reached by resolving attributes on
    its family's page. Letting a variant child become its own destination would
    invent a second route for something the catalogue never lists.
    """

    item = frappe.db.get_value(
        "Item", target,
        ["custom_slug", "variant_of", "disabled", "is_sales_item", "end_of_life"],
        as_dict=True)

    if not item:
        frappe.throw(_("Item {0} does not exist.").format(target), frappe.ValidationError)

    if item.variant_of:
        frappe.throw(
            _("{0} is a generated variant. Link to its family {1} instead -- a "
              "variant is reached by choosing options on the family page.")
            .format(target, item.variant_of),
            frappe.ValidationError)

    if not item.custom_slug:
        frappe.throw(
            _("{0} has no storefront slug, so it has no public page.").format(target),
            frappe.ValidationError)

    if item.disabled or not item.is_sales_item:
        frappe.throw(
            _("{0} is not available in the storefront.").format(target),
            frappe.ValidationError)


def _validate_external(doc, fieldname, target):
    doc.set(fieldname, validate_destination(target, _("External URL")))


VALIDATORS = {
    "Storefront Category": _validate_category,
    "Storefront Page": _validate_page,
    "Product": _validate_product,
    "External URL": _validate_external,
}
