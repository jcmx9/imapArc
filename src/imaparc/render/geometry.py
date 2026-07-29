"""Page geometry for the PDF renditions — pure arithmetic, no browser.

Holds the A4 constants, the millimetre→CSS-pixel conversion, the page margins,
and the two body-rendition descriptors. Keeping it browser-free means the
numbers are unit-tested without launching Chromium.
"""

from __future__ import annotations

from dataclasses import dataclass

CSS_PX_PER_INCH = 96.0
MM_PER_INCH = 25.4

A4_WIDTH_MM = 210.0
A4_HEIGHT_MM = 297.0

# The message-pane width the faithful rendition lays the mail out at; mail HTML
# is overwhelmingly designed for 600-800 px, so this reproduces the client look.
FAITHFUL_LAYOUT_PX = 800.0


def mm_to_css_px(mm: float) -> float:
    """Convert millimetres to CSS pixels at 96 dpi."""
    return mm / MM_PER_INCH * CSS_PX_PER_INCH


# Page margins: 20 mm on top/right/bottom, a wider 25 mm on the left (room for
# hole-punching/filing, DIN-5008-style for correspondence).
MARGIN_MM = 20.0
MARGIN_LEFT_MM = 25.0


@dataclass(frozen=True, slots=True)
class Rendition:
    """One way of putting the mail body onto A4.

    Attributes:
        name: Identifier used in logs and bookmarks.
        margin_mm: Page margin on top, right and bottom.
        scale: Print scale; 1.0 means "lay out at the paper width".
        title: Human-readable label for the separator page introducing it.
        fit_page: Scale the whole mail onto a single A4 page.
        margin_left_mm: Left margin; defaults to ``margin_mm`` when unset.
    """

    name: str
    margin_mm: float
    scale: float
    title: str
    fit_page: bool = False
    margin_left_mm: float | None = None

    @property
    def left_mm(self) -> float:
        """The left margin (wider than the others when set)."""
        if self.margin_left_mm is not None:
            return self.margin_left_mm
        return self.margin_mm


def faithful_rendition(margin_mm: float = MARGIN_MM) -> Rendition:
    """Original-fidelity rendition: the whole mail scaled onto one A4 page.

    Laid out at a fixed reference width (so it reads as sent), then the entire
    thing — however tall — is scaled down to fit a single page, giving a faithful
    one-page overview.
    """
    return Rendition(
        name="faithful",
        margin_mm=margin_mm,
        # Inert for this rendition: fit_page renders one tall page and scales it
        # onto A4 via a pikepdf overlay (see _render_fit_page), ignoring `scale`.
        scale=1.0,
        title="Originalgetreue Darstellung",
        fit_page=True,
        margin_left_mm=MARGIN_LEFT_MM,
    )


def reflowed_rendition(margin_mm: float = MARGIN_MM) -> Rendition:
    """Readable rendition: full font size, paginated cleanly by Chromium.

    Rendered 1:1 when the mail fits A4 portrait; if its measured width overflows,
    it is scaled down, and if that would shrink it too much it is put on A4
    landscape instead (see ``render_html_to_pdf``/``_fit_width``).
    """
    return Rendition(
        name="reflowed",
        margin_mm=margin_mm,
        scale=1.0,
        title="Umbrochene Fassung",
        margin_left_mm=MARGIN_LEFT_MM,
    )
