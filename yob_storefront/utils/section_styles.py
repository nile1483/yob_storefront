# Copyright (c) 2026, YOB and Shayona
"""Semantic section styles for a content PLACEMENT (Phase 25I).

WHAT A SECTION STYLE IS
-----------------------
One approved, semantic key naming the band a block sits in:

    FULL-WIDTH SECTION          <- the style paints this
      FIXED-WIDTH CONTAINER     <- Angular owns this width
        the existing block      <- unchanged

The backend says `muted`. It does not say what muted looks like, and it must
never learn: colour, padding, breakpoints, text colour, container width and the
full-bleed behaviour are all Angular's, defined in source-controlled CSS.

WHAT IT IS DELIBERATELY NOT
---------------------------
Not arbitrary CSS, not a Tailwind class, not a layout or width setting, not a
merchant-invented class name. A merchant picks one of five approved words. That
is the entire vocabulary, and keeping it a closed set is what stops presentation
becoming merchant-configurable -- the same rule that keeps YOB from being a
generic page builder.

WHY IT LIVES ON THE PLACEMENT, NOT THE BLOCK
--------------------------------------------
A `YOB Storefront Block` is authored ONCE and placed many times. The same
`Welcome Text` may want a muted band on an About page and a dark one on the home
route. Storing the style on the Block would force those two placements to agree,
and the first merchant who wanted them to differ would duplicate the Block --
losing the single-source content that the whole design exists to provide.

So both placement mechanisms carry it, and neither the Block nor any individual
block-type projector knows it exists.
"""

from frappe import _

#: machine key -> friendly label. Order is the order Desk offers them.
SECTION_STYLES = {
    "default": "Default",
    "muted": "Muted",
    "brand_soft": "Brand Soft",
    "accent": "Accent",
    "dark": "Dark",
}

#: What a row that predates this field means. `default` is "the ordinary page
#: background", so a historical blank projects exactly as it rendered before the
#: field existed -- no migration rewrites stored data to say so.
DEFAULT_STYLE = "default"


class SectionStyleError(Exception):
    def __init__(self, message, field="section_style"):
        super().__init__(message)
        self.message = message
        self.field = field


def style_keys():
    return tuple(SECTION_STYLES)


def style_label(key):
    return SECTION_STYLES.get(key, key)


def normalise(value):
    """A stored value -> the key to project. Blank means `default`.

    Read on the projection path, so it must never raise: a row written before
    this field existed, or by an import that omitted it, renders as it always
    did rather than failing a whole page.
    """

    return value if value in SECTION_STYLES else DEFAULT_STYLE


def validate_style(value, *, label=None):
    """Raise unless the value is one of the approved keys. Blank is allowed.

    Blank is accepted because Frappe writes no default into rows that already
    exist; it is normalised on the way out instead. Anything else -- a Tailwind
    class, a CSS declaration, a colour -- is refused here, at the only boundary
    where a merchant can type.
    """

    if not value:
        return DEFAULT_STYLE

    if value not in SECTION_STYLES:
        raise SectionStyleError(
            _("{0} is not an approved section style. Choose one of: {1}.")
            .format(value, ", ".join(SECTION_STYLES)),
            field=label or "section_style")

    return value


def style_options():
    """Newline-joined keys, the shape a Frappe Select field stores."""

    return "\n".join(SECTION_STYLES)
