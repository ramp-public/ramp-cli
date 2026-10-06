"""A thin border drawn on the content-facing edge of each border cell.

Box-drawing lines sit mid-cell, and cells are about twice as tall as wide, so
`solid` leaves a bottom gap roughly double the side gap. Eighth blocks hugging
the content (`▕ ▏ ▁ ▔`) make every gap zero, which is the only size that
matches on both axes. Corners stay blank: no glyph joins those edges.
"""

from textual import _border
from textual.css import constants

INSET = "inset"
# A bottom edge on the top of its cell, so content touches it; spanning the
# corners lets a mid-cell `solid` side meet it.
LEDGE = "ledge"


def register() -> None:
    if LEDGE in _border.BORDER_CHARS:
        return
    _border.BORDER_CHARS[LEDGE] = (
        ("▔", "▔", "▔"),
        ("│", " ", "│"),
        ("▔", "▔", "▔"),
    )
    _border.BORDER_LOCATIONS[LEDGE] = ((0, 0, 0), (0, 0, 0), (0, 0, 0))
    _border.BORDER_LABEL_LOCATIONS[LEDGE] = (0, 0)
    constants.VALID_BORDER.add(LEDGE)
    _border.BORDER_CHARS[INSET] = (
        (" ", "▁", " "),
        ("▕", " ", "▏"),
        (" ", "▔", " "),
    )
    _border.BORDER_LOCATIONS[INSET] = ((0, 0, 0), (0, 0, 0), (0, 0, 0))
    _border.BORDER_LABEL_LOCATIONS[INSET] = (0, 0)
    # The CSS parser validates against this same set object.
    constants.VALID_BORDER.add(INSET)
