# Copyright (c) 2026, YOB and Shayona
"""The application-owned registry of content slots (Phase 25G).

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
---------------------------------------------
A merchant may drop a reusable Content Block into an EXISTING application page --
above the cart, below a product -- without Angular knowing what a Cart looks like
with a banner in it. That is the whole feature.

It is **not** a page builder. Angular owns application structure absolutely: it
decides which routes exist and where, inside each route, a slot is rendered. This
file is the contract between those two facts, and it is HARD-CODED on purpose. A
merchant can choose WHAT goes in a slot and in WHICH ORDER; they can never create
a route, invent a position, or move one.

So the registry below is application code, not configuration. Adding a route or a
slot is a code change in BOTH repositories -- Angular renders
`<yob-content-slot slot="cart.above_cart" />`, and this file admits it. If only
one side changes, the other refuses: a slot Angular never renders is content
nobody sees, and a slot Angular renders that is missing here can hold nothing.

ONE AUTHORITY
-------------
Every consumer reads THIS module: DocType validation, the Desk pickers, the
runtime projection, the published OpenAPI enums, and the tests. Nothing
re-declares a route or slot string of its own -- a second list would drift, and
the drift would be invisible until a merchant's content vanished.

EXCLUSIONS ARE A DECISION
-------------------------
Login, checkout, payment and the provider callback have NO slots and are absent
below. They are transaction-critical: arbitrary merchant HTML next to a payment
form is a security and a conversion risk, and a Product Grid mid-checkout would
price products while a buyer is trying to pay. Adding them later is an explicit
decision, never an oversight -- which is why they are not silently omitted but
recorded here.
"""

from frappe import _

#: route_key -> (label, ((slot_key, label), ...))
#:
#: Order matters twice over: routes are listed as a buyer meets them, and slots
#: are listed top-to-bottom as Angular renders them, so Desk reads like the page.
SYSTEM_CONTENT_SLOTS = {
    "home": ("Home", (
        ("hero", "Hero"),
        ("main", "Main"),
        ("bottom", "Bottom"),
    )),
    "catalog": ("Catalog", (
        ("above_listing", "Above Listing"),
        ("below_listing", "Below Listing"),
    )),
    "category": ("Category Listing", (
        ("above_listing", "Above Listing"),
        ("below_listing", "Below Listing"),
    )),
    "product": ("Product Detail", (
        ("above_product", "Above Product"),
        ("below_product", "Below Product"),
    )),
    "cart": ("Cart", (
        ("above_cart", "Above Cart"),
        ("below_cart", "Below Cart"),
    )),
    "account": ("Account", (
        ("above_content", "Above Content"),
        ("below_content", "Below Content"),
    )),
    "orders": ("Orders", (
        ("above_list", "Above List"),
        ("below_list", "Below List"),
    )),
    "order_detail": ("Order Detail", (
        ("above_order", "Above Order"),
        ("below_order", "Below Order"),
    )),
}

#: Routes that exist in the SPA but deliberately expose no slot, with the reason.
#: Listed rather than omitted so "not yet considered" and "considered and
#: refused" cannot be confused by whoever reads this next.
EXCLUDED_ROUTES = {
    "login": "access flow; merchant content must not surround a credential form",
    "checkout": "transaction-critical; no merchandising between cart and payment",
    "payment": "token-authorised and guest-reachable; no merchant HTML near a "
               "payment form",
    "payment_callback": "provider return path; must stay deterministic",
}


class SlotError(Exception):
    """An unknown route or slot, carrying which of the two was wrong.

    Two causes, two messages, because a client fixes them differently: an unknown
    route means the caller asked for a page that has no content contract, while a
    wrong slot usually means a slot from ANOTHER route was pasted in.
    """

    def __init__(self, message, field):
        super().__init__(message)
        self.message = message
        self.field = field


def route_keys():
    """Every route that may hold content, in registry order."""

    return tuple(SYSTEM_CONTENT_SLOTS)


def slot_keys(route_key):
    """The slots of one route, in render order. `()` for an unknown route."""

    entry = SYSTEM_CONTENT_SLOTS.get(route_key)

    return tuple(slot for slot, _label in entry[1]) if entry else ()


def route_label(route_key):
    entry = SYSTEM_CONTENT_SLOTS.get(route_key)

    return entry[0] if entry else route_key


def slot_label(route_key, slot_key):
    entry = SYSTEM_CONTENT_SLOTS.get(route_key)

    if entry:
        for slot, label in entry[1]:
            if slot == slot_key:
                return label

    return slot_key


def is_route(route_key):
    return route_key in SYSTEM_CONTENT_SLOTS


def validate_placement(route_key, slot_key):
    """Raise unless this exact (route, slot) pair is one the application renders.

    The pair is what matters, never the two halves separately. `above_listing` is
    a real slot and `cart` is a real route, but `cart.above_listing` is a
    position that exists nowhere -- content stored there would simply never be
    drawn, which is worse than a refusal because it looks like it worked.
    """

    if not route_key or not is_route(route_key):
        raise SlotError(
            _("{0} is not an application route that can hold content.")
            .format(route_key or _("(empty)")), "route_key")

    if not slot_key or slot_key not in slot_keys(route_key):
        raise SlotError(
            _("{0} is not a position on the {1} route. Available: {2}.")
            .format(slot_key or _("(empty)"), route_label(route_key),
                    ", ".join(slot_keys(route_key))), "slot_key")


def route_options():
    """`[{value, label}]` for a Desk picker, in registry order."""

    return [{"value": key, "label": label}
            for key, (label, _slots) in SYSTEM_CONTENT_SLOTS.items()]


def slot_options(route_key):
    """`[{value, label}]` for one route's slots. Empty for an unknown route."""

    entry = SYSTEM_CONTENT_SLOTS.get(route_key)

    return [{"value": slot, "label": label} for slot, label in entry[1]] if entry else []
