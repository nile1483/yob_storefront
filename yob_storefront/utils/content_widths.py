# Copyright (c) 2026, YOB and Shayona
"""Horizontal containment for a content PLACEMENT (Phase 25K).

TWO INDEPENDENT AXES
--------------------
A placement now carries two presentation keys, and they answer different
questions about different elements:

    section_style   what the full-width BAND behind the block looks like
    content_width   whether the BLOCK ITSELF is contained or spans that band

```text
contained                        full_width
  <section>                        <section>
    <div class=container>            …block…
      …block…                      </section>
    </div>
  </section>
```

Neither is derived from the other, and every combination is legitimate: a hero
carousel is usually `default` + `full_width`, a product grid `brand_soft` +
`contained`. Deriving one from the other would quietly remove a choice a merchant
is entitled to make.

WHAT THIS KEY DOES *NOT* CONTROL
--------------------------------
Only horizontal containment. Not the section background, not the section style,
not vertical spacing, not block or image height, not which responsive image is
chosen, and no margin or padding value. Those are separate concerns and stay in
source-controlled CSS.

The backend implements no CSS meaning at all. It stores one of two words; Angular
decides what containment looks like at every breakpoint.

WHY IT LIVES ON THE PLACEMENT
-----------------------------
Same reason as `section_style` (see `section_styles.py`): a Block is authored
once and placed many times. The identical hero Banner may run full width on the
home route and sit contained inside a dynamic page. Storing width on the Block
would force those to agree, and the merchant who wanted both would duplicate the
Block -- losing the single-source content the design exists to provide.

DELIBERATELY TWO VALUES
-----------------------
No narrow, wide, boxed, fluid, percentage, viewport or custom width. Two choices
cover the real need; a third gets added deliberately, when a business case
actually arrives, rather than pre-emptively turning this into a width system.
"""

from frappe import _

#: machine key -> friendly label. Order is the order Desk offers them.
CONTENT_WIDTHS = {
    "contained": "Contained",
    "full_width": "Full Width",
}

#: What a row that predates this field means. `contained` is exactly what every
#: placement did before Phase 25K, so historical rows keep rendering identically
#: without anything being written to them.
DEFAULT_WIDTH = "contained"


class ContentWidthError(Exception):
    def __init__(self, message, field="content_width"):
        super().__init__(message)
        self.message = message
        self.field = field


def width_keys():
    return tuple(CONTENT_WIDTHS)


def width_label(key):
    return CONTENT_WIDTHS.get(key, key)


def normalise(value):
    """A stored value -> the key to project. Blank means `contained`.

    Read on the projection path, so it must never raise: a row written before
    this field existed renders as it always did rather than failing a whole page.
    """

    return value if value in CONTENT_WIDTHS else DEFAULT_WIDTH


def validate_width(value, *, label=None):
    """Raise unless the value is one of the two approved keys. Blank is allowed.

    Blank is accepted because Frappe writes no default into rows that already
    exist; it is normalised on the way out instead. Anything else -- `100%`,
    `100vw`, `max-w-none`, a class name -- is refused here, at the only boundary
    where a merchant can type.
    """

    if not value:
        return DEFAULT_WIDTH

    if value not in CONTENT_WIDTHS:
        raise ContentWidthError(
            _("{0} is not an approved content width. Choose one of: {1}.")
            .format(value, ", ".join(CONTENT_WIDTHS)),
            field=label or "content_width")

    return value


def width_options():
    """Newline-joined keys, the shape a Frappe Select field stores."""

    return "\n".join(CONTENT_WIDTHS)
