"""Tests for the renderer.

Pure tests run everywhere; the browser tests are marked ``requires_chromium``
and prove the security guarantees end to end (a tracking pixel is really
blocked, a cid: image really lands in the PDF, @media print really loses).
"""

from __future__ import annotations

import base64
import io

import pikepdf
import pytest

from imaparc.html.inline import inline_resources
from imaparc.mail.models import AttachmentPart
from imaparc.render.browser import BrowserPool, build_launch_args, is_allowed_url
from imaparc.render.geometry import (
    A4_HEIGHT_MM,
    A4_WIDTH_MM,
    faithful_rendition,
    reflowed_rendition,
)
from imaparc.render.pdf_render import (
    _PT_PER_MM,
    _place_on_a4,
    harden_document,
    render_html_to_pdf,
)

# --- pure tests -------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("data:image/png;base64,AAA", True),
        ("about:blank", True),
        ("https://tracker.example.com/p.gif", False),
        ("http://x.com/a.jpg", False),
        ("//cdn.example.com/x.css", False),
        ("file:///etc/passwd", False),
        ("ftp://x.com/f", False),
    ],
)
def test_is_allowed_url(url: str, allowed: bool) -> None:
    assert is_allowed_url(url) is allowed


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("data:image/png;base64,AAA", True),
        ("https://cdn.example.com/logo.png", True),
        ("http://cdn.example.com/logo.png", True),
        # Even in permissive mode these stay blocked.
        ("file:///etc/passwd", False),
        ("ftp://x.com/f", False),
    ],
)
def test_is_allowed_url_permissive(url: str, allowed: bool) -> None:
    assert is_allowed_url(url, allow_remote=True) is allowed


def test_dns_blackhole_only_in_strict_mode() -> None:
    strict = build_launch_args()
    permissive = build_launch_args(allow_remote=True)
    assert any("NOTFOUND" in a for a in strict)
    assert not any("NOTFOUND" in a for a in permissive)
    # The anti-telemetry flags apply either way.
    assert "--disable-background-networking" in permissive


def test_csp_differs_by_policy() -> None:
    strict = harden_document("<html><body></body></html>")
    permissive = harden_document("<html><body></body></html>", allow_remote=True)
    assert "img-src data:;" in strict
    assert "https:" not in strict
    assert "https:" in permissive
    # Scripts stay forbidden in both.
    assert "default-src 'none'" in strict
    assert "default-src 'none'" in permissive


def test_harden_document_injects_into_existing_head() -> None:
    out = harden_document("<html><head><title>x</title></head><body>b</body></html>")
    assert "Content-Security-Policy" in out
    assert out.index("Content-Security-Policy") < out.index("<title>")


def test_harden_document_creates_head_when_missing() -> None:
    out = harden_document("<html><body>b</body></html>")
    assert "Content-Security-Policy" in out
    assert "print-color-adjust" in out


def test_harden_document_wraps_bare_fragment() -> None:
    out = harden_document("<p>loose</p>")
    assert "<html>" in out
    assert "loose" in out
    assert "Content-Security-Policy" in out


def test_harden_document_ignores_head_inside_comment() -> None:
    # A literal "<head>" inside a comment must not be mistaken for the document
    # head, or the CSP + guard would be spliced into inert comment text.
    out = harden_document("<html><body><!-- x <head> y --><p>hi</p></body></html>")
    open_c, close_c = out.index("<!--"), out.index("-->")
    guard = out.index("page: auto")
    assert not (open_c < guard < close_c)  # guard is NOT inside the comment
    assert "Content-Security-Policy" in out


def test_harden_document_head_with_attributes_no_duplicate() -> None:
    doc = '<html><head lang="de"><title>t</title></head><body>b</body></html>'
    out = harden_document(doc)
    assert out.lower().count("<head") == 1  # no second head injected
    assert out.index("Content-Security-Policy") < out.lower().index("</head>")


def test_harden_document_does_not_treat_header_as_head() -> None:
    out = harden_document("<html><body><header>h</header><p>x</p></body></html>")
    # <header> is not the document head; a head is created inside <html> instead.
    assert "Content-Security-Policy" in out
    assert out.lower().index("<head>") < out.lower().index("<body")


def test_harden_document_neutralises_mail_paged_media() -> None:
    # Outlook/Word mail carries `page: WordSection1` rules that would force the
    # body onto a new page; the guard overrides them with `page: auto`. It must
    # NOT set an `@page` margin (that would override the per-rendition margins).
    out = harden_document("<html><body>x</body></html>")
    assert "page: auto" in out
    assert "@page" not in out


# --- browser tests ----------------------------------------------------------
# Skipping is handled centrally in conftest.py via the requires_chromium marker.

RED_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg=="
)

HOSTILE_HTML = """<html><head><style>
  body { background-color: #eef6ff; }
  @media print { .only-screen { display: none; } }
</style></head><body>
  <div class="only-screen">SCREEN_ONLY_MARKER</div>
  <table width="600"><tr><td bgcolor="#0055aa">NESTED_MARKER</td>
  <td><table><tr><td>inner</td></tr></table></td></tr></table>
  <img src="cid:logo123">
  <img src="https://tracker.example.com/pixel.gif">
</body></html>"""


def _inline_png() -> AttachmentPart:
    return AttachmentPart(
        filename="logo.png",
        content_type="image/png",
        content=RED_PNG,
        content_id="logo123",
        is_inline=True,
    )


def _pdf_text(data: bytes, tmp_path_factory: pytest.TempPathFactory) -> str:
    import subprocess

    path = tmp_path_factory.mktemp("pdf") / "out.pdf"
    path.write_bytes(data)
    result = subprocess.run(
        ["pdftotext", str(path), "-"], capture_output=True, text=True, check=False
    )
    return result.stdout


@pytest.mark.requires_chromium
async def test_renders_hostile_mail_correctly(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The five classic email-rendering traps, all in one document."""
    prepared = inline_resources(HOSTILE_HTML, [_inline_png()])
    # The tracking pixel must already be gone before the browser sees it.
    assert "tracker.example.com" not in prepared.html
    assert prepared.blocked_remote

    async with BrowserPool() as pool, pool.context() as ctx:
        pdf = await render_html_to_pdf(ctx, prepared.html, faithful_rendition())

    assert pdf.startswith(b"%PDF-")
    text = _pdf_text(pdf, tmp_path_factory)
    # @media print tried to hide this; emulate_media("screen") keeps it.
    assert "SCREEN_ONLY_MARKER" in text
    # Nested table layout — where html-to-pdfmake failed in the extension.
    assert "NESTED_MARKER" in text


@pytest.mark.requires_chromium
async def test_cid_image_is_embedded_in_pdf() -> None:
    import pikepdf

    prepared = inline_resources(
        '<html><body><img src="cid:logo123"></body></html>', [_inline_png()]
    )
    async with BrowserPool() as pool, pool.context() as ctx:
        pdf = await render_html_to_pdf(ctx, prepared.html, faithful_rendition())

    import io

    def image_xobjects(resources: object, seen: set[int]) -> list[object]:
        # Walk XObjects recursively: the faithful rendition wraps the whole page
        # in a Form XObject (single-page A4 overlay), so images sit one level in.
        found: list[object] = []
        xobjects = resources.get("/XObject", {}) if resources else {}  # type: ignore[union-attr]
        for obj in xobjects.values():
            if id(obj) in seen:
                continue
            seen.add(id(obj))
            subtype = obj.get("/Subtype")
            if subtype == "/Image":
                found.append(obj)
            elif subtype == "/Form":
                found.extend(image_xobjects(obj.get("/Resources", {}), seen))
        return found

    with pikepdf.open(io.BytesIO(pdf)) as doc:
        seen: set[int] = set()
        images = [
            img
            for page in doc.pages
            for img in image_xobjects(page.get("/Resources", {}), seen)
        ]
    assert images, "the inlined cid: image should appear as an image XObject"


REMOTE_IMG_HTML = (
    '<html><body><img src="https://tracker.example.com/p.gif"></body></html>'
)


@pytest.mark.requires_chromium
async def test_route_guard_aborts_remote_request() -> None:
    """Layer 2 in isolation: without a CSP, the route handler must abort."""
    async with BrowserPool() as pool:
        async with pool.context() as ctx:
            page = await ctx.new_page()
            # Deliberately un-hardened, so the CSP does not pre-empt the request.
            await page.set_content(REMOTE_IMG_HTML, wait_until="load")
            await page.close()
        assert any("tracker.example.com" in u for u in pool.blocked_urls)


@pytest.mark.requires_chromium
async def test_csp_prevents_the_request_being_made_at_all() -> None:
    """Layer 4 pre-empts layer 2: with the CSP, no request is even attempted."""
    async with BrowserPool() as pool:
        async with pool.context() as ctx:
            pdf = await render_html_to_pdf(ctx, REMOTE_IMG_HTML, faithful_rendition())
        # Nothing to abort, because the CSP stopped it before the network stack.
        assert not any("tracker.example.com" in u for u in pool.blocked_urls)
    assert pdf.startswith(b"%PDF-")


@pytest.mark.requires_chromium
async def test_both_renditions_produce_valid_pdfs() -> None:
    prepared = inline_resources(HOSTILE_HTML, [_inline_png()])
    async with BrowserPool() as pool, pool.context() as ctx:
        faithful = await render_html_to_pdf(ctx, prepared.html, faithful_rendition())
        reflowed = await render_html_to_pdf(ctx, prepared.html, reflowed_rendition())
    assert faithful.startswith(b"%PDF-")
    assert reflowed.startswith(b"%PDF-")
    assert faithful != reflowed


@pytest.mark.requires_chromium
async def test_reflowed_switches_wide_mail_to_landscape() -> None:
    import io

    import pikepdf

    narrow = "<html><body><div style='max-width:520px'>schmal</div></body></html>"
    wide = (
        "<html><body><div style='width:1300px;white-space:nowrap'>"
        + "breit " * 40
        + "</div></body></html>"
    )
    async with BrowserPool() as pool, pool.context() as ctx:
        narrow_pdf = await render_html_to_pdf(ctx, narrow, reflowed_rendition())
        wide_pdf = await render_html_to_pdf(ctx, wide, reflowed_rendition())

    def is_landscape(pdf: bytes) -> bool:
        with pikepdf.open(io.BytesIO(pdf)) as doc:
            box = doc.pages[0].mediabox
            return (float(box[2]) - float(box[0])) > (float(box[3]) - float(box[1]))

    assert not is_landscape(narrow_pdf)  # fits A4 portrait → stays portrait
    assert is_landscape(wide_pdf)  # too wide for portrait → A4 landscape


# --- link annotations survive the one-page overview ------------------------


def _pdf_with_link(
    *, size: tuple[float, float] = (800.0, 600.0), rect: list[float]
) -> bytes:
    """A one-page PDF carrying a single link annotation at ``rect``."""
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=size)
    page.Annots = pdf.make_indirect(
        [
            pikepdf.Dictionary(
                Type=pikepdf.Name.Annot,
                Subtype=pikepdf.Name.Link,
                Rect=rect,
                Border=[0, 0, 0],
                A=pikepdf.Dictionary(
                    S=pikepdf.Name.URI, URI="https://example.com/target"
                ),
            )
        ]
    )
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def _links(pdf_bytes: bytes) -> list[tuple[str, list[float]]]:
    """Every link as ``(uri, rect)``.

    The values are read inside the context: a pikepdf handle is destroyed once
    the Pdf closes, so returning the objects themselves would hand back corpses.
    """
    with pikepdf.open(io.BytesIO(pdf_bytes)) as doc:
        return [
            (str(a.A.URI), [float(v) for v in a.Rect])
            for page in doc.pages
            for a in list(page.get("/Annots", []))
            if a.get("/Subtype") == "/Link"
        ]


def test_place_on_a4_keeps_link_annotations() -> None:
    """The faithful overview is page 1 — a dead link there is the one users hit."""
    placed = _place_on_a4(_pdf_with_link(rect=[100, 200, 300, 250]), 20.0, 25.0)

    links = _links(placed)
    assert len(links) == 1
    assert links[0][0] == "https://example.com/target"


def test_place_on_a4_transforms_the_link_rectangle() -> None:
    """A link must sit on the scaled artwork, not at its original coordinates."""
    source = (800.0, 600.0)
    placed = _place_on_a4(
        _pdf_with_link(size=source, rect=[0, 0, 800, 600]), 20.0, 25.0
    )

    # Same aspect-fit pikepdf applies: scale to fit, then centre in the box.
    margin, left = 20.0 * _PT_PER_MM, 25.0 * _PT_PER_MM
    w_pt, h_pt = A4_WIDTH_MM * _PT_PER_MM, A4_HEIGHT_MM * _PT_PER_MM
    box_w, box_h = (w_pt - margin) - left, (h_pt - margin) - margin
    scale = min(box_w / source[0], box_h / source[1])
    x0 = left + (box_w - source[0] * scale) / 2
    y0 = margin + (box_h - source[1] * scale) / 2

    rect = _links(placed)[0][1]
    assert rect == pytest.approx(
        [x0, y0, x0 + source[0] * scale, y0 + source[1] * scale], abs=0.01
    )


def test_place_on_a4_without_annotations_stays_clean() -> None:
    """A page with no links must not gain an empty /Annots array."""
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(800, 600))
    buffer = io.BytesIO()
    pdf.save(buffer)

    placed = _place_on_a4(buffer.getvalue(), 20.0, 25.0)

    with pikepdf.open(io.BytesIO(placed)) as doc:
        assert "/Annots" not in doc.pages[0]
