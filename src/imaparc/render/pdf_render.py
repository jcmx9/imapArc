"""Turn HTML into PDF bytes — the only module touching Playwright's Page API.

Four settings separate this from a naive ``page.pdf()`` call, and each one
fixes a concrete way email rendering goes wrong:

``wait_until="load"``
    Deterministic. Because every image was inlined as a ``data:`` URI before
    we got here, images resolve from memory and ``load`` genuinely means
    "finished" — replacing the extension's racy fixed 500 ms sleep.

``emulate_media("screen")``
    ``page.pdf()`` switches to print media by default, and plenty of
    newsletters hide content via ``@media print``. This keeps what the reader
    actually saw.

``print_background=True``
    Chromium drops background colours when printing; without this, coloured
    mail renders as white.

explicit ``format``/``margin``/``scale``
    Some Chromium versions ignore ``@page { size: … }``, so paper geometry is
    passed as parameters rather than trusted to CSS.
"""

from __future__ import annotations

import io
import logging

import pikepdf
from playwright.async_api import BrowserContext
from playwright.async_api import Error as PlaywrightError

from imaparc.exceptions import RenderError
from imaparc.render.geometry import (
    A4_HEIGHT_MM,
    A4_WIDTH_MM,
    FAITHFUL_LAYOUT_PX,
    Rendition,
    mm_to_css_px,
)

logger = logging.getLogger(__name__)

# If fitting a wide mail onto A4 portrait would shrink it below this, use A4
# landscape instead (much wider content area) so it stays readable.
_LANDSCAPE_THRESHOLD = 0.85

# Chromium clamps a single PDF page's height (~200 in). Beyond this the faithful
# one-page overview of an extremely long mail may be cut off — warn (the reflowed
# and plain-text renditions stay complete).
_MAX_FAITHFUL_PX = 14000

_PT_PER_MM = 72.0 / 25.4

# Annotation flag bit 3 (value 4) = Print. PDF/A requires annotations to be
# printable and neither Hidden nor NoView, so carried-over links set exactly this.
_ANNOT_FLAG_PRINT = 4

# Injected into every document: forbids anything but in-memory resources and
# stops a huge inline image from blowing up the layout. It also neutralises the
# mail's own paged-media CSS: Outlook/Word mail wraps the body in
# `div.WordSection1 { page: WordSection1 }` with an `@page WordSection1 { size…}`
# rule, which forces a page break before the whole body (so the mail lands on the
# *next* page after our header, and the faithful one-page render captures only
# the empty first page). Forcing `page: auto` and neutral breaks keeps the body
# with the header and lets us control pagination via page.pdf().
_GUARD_STYLE = """
<style>
  html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  img, table { max-width: 100% !important; }
  img { height: auto !important; }
  * {
    page: auto !important;
    page-break-before: auto !important;
    page-break-after: auto !important;
    break-before: auto !important;
    break-after: auto !important;
  }
</style>
"""
_CSP_STRICT = (
    '<meta http-equiv="Content-Security-Policy" '
    "content=\"default-src 'none'; img-src data:; "
    "style-src 'unsafe-inline' data:; font-src data:\">"
)
# Remote images permitted, but scripts and frames stay forbidden.
_CSP_ALLOW_REMOTE = (
    '<meta http-equiv="Content-Security-Policy" '
    "content=\"default-src 'none'; img-src data: https: http:; "
    "style-src 'unsafe-inline' data:; font-src data:\">"
)


def harden_document(html: str, *, allow_remote: bool = False) -> str:
    """Prepend the CSP and guard styles so they win over the mail's own CSS.

    The injection point is found structurally, not by a naive substring search: a
    literal ``<head>`` sitting inside a comment (``<!-- <head> -->``) or a tag
    name like ``<header>`` must not be mistaken for the document head, or the CSP
    and layout guard would be spliced into inert text — silently disabling both
    (RFC-independent, but a real correctness/defence-in-depth bug).
    """
    csp = _CSP_ALLOW_REMOTE if allow_remote else _CSP_STRICT
    prefix = csp + _GUARD_STYLE
    head_end = _open_tag_end(html, "head")
    if head_end != -1:
        return html[:head_end] + prefix + html[head_end:]
    html_end = _open_tag_end(html, "html")
    if html_end != -1:
        return html[:html_end] + f"<head>{prefix}</head>" + html[html_end:]
    return f"<html><head>{prefix}</head><body>{html}</body></html>"


def _inside_comment(lowered: str, idx: int) -> bool:
    """True if position ``idx`` lies inside an unclosed ``<!-- … -->`` comment."""
    return lowered.rfind("<!--", 0, idx) > lowered.rfind("-->", 0, idx)


def _open_tag_end(html: str, name: str) -> int:
    """Index just after the first real ``<name …>`` opening tag, or ``-1``.

    Skips matches inside comments and rejects longer tag names (``<header>`` for
    ``head``); tolerates attributes (``<head lang="de">``).
    """
    lowered = html.lower()
    needle = f"<{name}"
    i = 0
    while True:
        idx = lowered.find(needle, i)
        if idx == -1:
            return -1
        after = lowered[idx + len(needle) : idx + len(needle) + 1]
        if after not in ("", ">", "/", " ", "\t", "\n", "\r", "\f"):
            i = idx + len(needle)  # e.g. <header> — not the tag we want
            continue
        if _inside_comment(lowered, idx):
            end = lowered.find("-->", idx)
            if end == -1:
                return -1
            i = end + 3
            continue
        close = lowered.find(">", idx)
        return close + 1 if close != -1 else -1


async def render_html_to_pdf(
    context: BrowserContext,
    html: str,
    rendition: Rendition,
    *,
    allow_remote: bool = False,
) -> bytes:
    """Render one HTML document to PDF bytes using the given rendition.

    Args:
        context: A locked-down context from the ``BrowserPool``.
        html: A self-contained document (no ``cid:``, no remote URLs).
        rendition: Page geometry and scale.
        allow_remote: Must match the pool's policy, so the CSP does not
            contradict the route handler.

    Returns:
        The PDF bytes.

    Raises:
        RenderError: If Chromium could not produce a PDF.
    """
    margin = f"{rendition.margin_mm}mm"
    left = f"{rendition.left_mm}mm"
    page = await context.new_page()
    try:
        await page.set_content(
            harden_document(html, allow_remote=allow_remote), wait_until="load"
        )
        # Must come after set_content: print media would hide @media print
        # content that the reader saw on screen.
        await page.emulate_media(media="screen")
        if rendition.fit_page:
            return await _render_fit_page(page, rendition.margin_mm, rendition.left_mm)
        landscape, scale = await _fit_width(page, rendition)
        return await page.pdf(
            format="A4",
            landscape=landscape,
            print_background=True,
            scale=scale,
            prefer_css_page_size=False,
            margin={
                "top": margin,
                "bottom": margin,
                "left": left,
                "right": margin,
            },
        )
    except PlaywrightError as exc:
        raise RenderError(
            f"Chromium failed to render the {rendition.name} rendition: {exc}"
        ) from exc
    finally:
        await page.close()


async def _fit_width(page: object, rendition: Rendition) -> tuple[bool, float]:
    """Choose orientation and print scale so the mail's width fits the page.

    Mails are built to fit a screen without horizontal scrolling, so the content
    width is bounded — so measure it rather than assume a reference width. If it
    fits A4 portrait, render 1:1. If it overflows, scale down; but if that would
    drop below :data:`_LANDSCAPE_THRESHOLD`, switch to A4 **landscape** (a much
    wider content area) so a wide mail stays readable. Chromium then paginates
    cleanly between lines. Returns ``(landscape, scale)``.
    """
    portrait_px = mm_to_css_px(A4_WIDTH_MM - rendition.left_mm - rendition.margin_mm)
    await page.set_viewport_size(  # type: ignore[attr-defined]
        {"width": max(int(portrait_px), 1), "height": 1000}
    )
    measured = await page.evaluate(  # type: ignore[attr-defined]
        "() => Math.max(document.documentElement.scrollWidth,"
        " document.body.scrollWidth)"
    )
    content_px = max(float(measured), 1.0)
    if content_px <= portrait_px:
        return False, rendition.scale
    portrait_scale = portrait_px / content_px
    if portrait_scale >= _LANDSCAPE_THRESHOLD:
        return False, portrait_scale
    landscape_px = mm_to_css_px(A4_HEIGHT_MM - rendition.left_mm - rendition.margin_mm)
    return True, min(rendition.scale, landscape_px / content_px)


async def _render_fit_page(page: object, margin_mm: float, left_mm: float) -> bytes:
    """Render the whole document as one tall **vector** page, scaled onto one A4.

    Laid out at a fixed reference width (as a mail client's message pane would),
    the full content is rendered as a *single* tall PDF page — one page, so there
    is no internal pagination, and with the guard's ``* { page: auto }`` the
    mail's named page breaks are inert. The vector page is then scaled
    (aspect-preserving) onto one A4 page: a faithful one-page overview that stays
    **vector and searchable**, and small in file size, no matter how long the
    mail. Layout of exotic CSS is best-effort (the .eml and the plain-text
    rendition remain the lossless/deterministic fallback).
    """
    await page.set_viewport_size(  # type: ignore[attr-defined]
        {"width": int(FAITHFUL_LAYOUT_PX), "height": 1000}
    )
    dims = await page.evaluate(  # type: ignore[attr-defined]
        "() => {const e=document.documentElement,b=document.body;"
        "return {w: Math.max(e.scrollWidth,b.scrollWidth),"
        "h: Math.max(e.scrollHeight,b.scrollHeight)};}"
    )
    width_px = max(int(dims["w"]), 1)
    height_px = max(int(dims["h"]) + 2, 1)  # +2px so nothing clips to page 2
    if height_px > _MAX_FAITHFUL_PX:
        logger.warning(
            "faithful one-page overview of a very long mail (%d px) may be "
            "truncated by Chromium; the reflowed and plain-text renditions are "
            "complete",
            height_px,
        )
    tall = await page.pdf(  # type: ignore[attr-defined]
        width=f"{width_px}px",
        height=f"{height_px}px",
        print_background=True,
        scale=1.0,
        prefer_css_page_size=False,
        margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
    )
    return _place_on_a4(tall, margin_mm, left_mm)


def _carry_over_links(
    out: pikepdf.Pdf,
    dst: pikepdf.Page,
    src: pikepdf.Pdf,
    source: pikepdf.Page,
    box: pikepdf.Rectangle,
) -> None:
    """Re-attach the source page's link annotations, moved onto the scaled art.

    :meth:`~pikepdf.Page.add_overlay` embeds the source page as a form XObject,
    which carries the *content stream* only. Link annotations are not content —
    they hang off the page's ``/Annots`` array — so without this the one-page
    overview would look perfect and have every link dead. That page is page 1 of
    the PDF, so it is exactly the one a reader clicks in.

    The rectangles are mapped with the same aspect-preserving, centred fit that
    ``add_overlay`` applies, so a link keeps sitting on its own text.
    """
    links = [
        annot
        for annot in list(source.get("/Annots", []))
        if annot.get("/Subtype") == pikepdf.Name.Link
    ]
    if not links:
        return  # leave the page without an /Annots array at all

    src_box = source.trimbox
    src_x, src_y = float(src_box[0]), float(src_box[1])
    src_w = max(float(src_box[2]) - src_x, 1e-9)
    src_h = max(float(src_box[3]) - src_y, 1e-9)
    scale = min(box.width / src_w, box.height / src_h)
    dx = box.llx + (box.width - src_w * scale) / 2
    dy = box.lly + (box.height - src_h * scale) / 2

    moved = []
    for annot in links:
        x0, y0, x1, y1 = (float(v) for v in annot.Rect)
        # copy_foreign needs an indirect handle; an annotation may sit directly
        # in the /Annots array. make_indirect returns an already-indirect object
        # unchanged, so this is safe either way.
        copied = out.copy_foreign(src.make_indirect(annot))
        copied.Rect = [
            (x0 - src_x) * scale + dx,
            (y0 - src_y) * scale + dy,
            (x1 - src_x) * scale + dx,
            (y1 - src_y) * scale + dy,
        ]
        # PDF/A requires annotations to be printable and not hidden.
        copied.F = _ANNOT_FLAG_PRINT
        moved.append(copied)
    dst.Annots = out.make_indirect(pikepdf.Array(moved))


def _place_on_a4(pdf_bytes: bytes, margin_mm: float, left_mm: float) -> bytes:
    """Scale a single-page PDF onto one A4 page, aspect-preserving and centred.

    Uses a wider left margin (``left_mm``) than the other three, matching the
    reflowed rendition's letter-style page. Link annotations are carried across
    separately — see :func:`_carry_over_links`.
    """
    m = margin_mm * _PT_PER_MM
    left = left_mm * _PT_PER_MM
    w_pt, h_pt = A4_WIDTH_MM * _PT_PER_MM, A4_HEIGHT_MM * _PT_PER_MM
    with pikepdf.open(io.BytesIO(pdf_bytes)) as src:
        out = pikepdf.Pdf.new()
        dst = out.add_blank_page(page_size=(w_pt, h_pt))
        box = pikepdf.Rectangle(left, m, w_pt - m, h_pt - m)
        source = src.pages[0]
        dst.add_overlay(source, box)
        _carry_over_links(out, dst, src, source, box)
        buffer = io.BytesIO()
        out.save(buffer)
        return buffer.getvalue()
