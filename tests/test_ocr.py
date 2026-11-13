"""Tests for the text layer imapArc lays over attachment pages (OCR)."""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import img2pdf
import pikepdf
import pytest
from PIL import Image, ImageDraw, ImageFont

from imaparc.config import RunConfig, ToolPaths
from imaparc.mail.parser import parse_mail
from imaparc.pdf import ocr, provenance
from imaparc.pdf.validate import run_verapdf
from imaparc.pipeline import render_mail
from imaparc.render.browser import BrowserPool
from imaparc.report import RunReport
from tests.mail_builder import build_mail


def scanned_png(text: str) -> bytes:
    """A picture of ``text`` — what a scanner or a phone camera delivers."""
    image = Image.new("RGB", (1654, 600), "white")
    font = ImageFont.load_default(size=72)
    ImageDraw.Draw(image).text((80, 220), text, fill="black", font=font)
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def scanned_pdf(text: str) -> bytes:
    """A one-page PDF holding only that picture: no text layer at all."""
    return bytes(img2pdf.convert(scanned_png(text)))


def text_of(pdf: bytes | Path, tmp_path: Path) -> str:
    """The PDF's text as a search would see it, whitespace collapsed."""
    if isinstance(pdf, bytes):
        path = tmp_path / "probe.pdf"
        path.write_bytes(pdf)
    else:
        path = pdf
    out = subprocess.run(
        ["pdftotext", str(path), "-"], capture_output=True, text=True, check=True
    ).stdout
    return " ".join(out.split())


def _typed_footer(pdf_bytes: bytes, text: str) -> bytes:
    """Add a line of real, typed text to the first page (a stamp, a footer)."""
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        page = pdf.pages[0]
        font = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Font,
                Subtype=pikepdf.Name.Type1,
                BaseFont=pikepdf.Name.Helvetica,
            )
        )
        page.obj.Resources.Font = pikepdf.Dictionary(F1=font)
        page.contents_add(f"BT /F1 10 Tf 20 12 Td ({text}) Tj ET".encode())
        out = io.BytesIO()
        pdf.save(out)
    return out.getvalue()


def _tools() -> tuple[Path, Path]:
    ocrmypdf, tesseract = shutil.which("ocrmypdf"), shutil.which("tesseract")
    assert ocrmypdf and tesseract
    return Path(ocrmypdf), Path(tesseract)


needs_pdftotext = pytest.mark.skipif(
    shutil.which("pdftotext") is None, reason="needs pdftotext"
)


# --- which pages ---------------------------------------------------------------


def _small_logo_page(text: str) -> bytes:
    """A digitally written page: typed text, and a small image in a corner."""
    logo = Image.new("RGB", (120, 60), "navy")
    raw = io.BytesIO()
    logo.save(raw, "PNG")
    with pikepdf.open(io.BytesIO(bytes(img2pdf.convert(raw.getvalue())))) as pdf:
        page = pdf.pages[0]
        image_name = next(iter(page.obj.Resources.XObject.keys()))
        page.obj.MediaBox = pikepdf.Array([0, 0, 595, 842])
        font = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Font,
                Subtype=pikepdf.Name.Type1,
                BaseFont=pikepdf.Name.Helvetica,
            )
        )
        page.obj.Resources.Font = pikepdf.Dictionary(F1=font)
        content = (
            f"q 90 0 0 45 40 770 cm {image_name} Do Q "
            f"BT /F1 11 Tf 40 700 Td ({text}) Tj ET"
        )
        page.obj.Contents = pdf.make_stream(content.encode())
        out = io.BytesIO()
        pdf.save(out)
    return out.getvalue()


def _content(pdf_bytes: bytes) -> ocr.PageContent:
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        return ocr.analyse_page(pdf.pages[0])


def test_a_scan_is_recognised() -> None:
    content = _content(scanned_pdf("SCAN"))
    assert content.image_share > 0.9 and content.text_chars == 0
    assert content.wants_ocr


def test_a_small_picture_alone_on_a_page_is_recognised() -> None:
    """A photo or a strip set on a page fills little of it — and is all of it."""
    strip = Image.new("RGB", (800, 120), "white")
    raw = io.BytesIO()
    strip.save(raw, "PNG")
    with pikepdf.open(io.BytesIO(bytes(img2pdf.convert(raw.getvalue())))) as pdf:
        pdf.pages[0].obj.MediaBox = pikepdf.Array([0, 0, 595, 842])
        out = io.BytesIO()
        pdf.save(out)

    content = _content(out.getvalue())

    assert 0 < content.image_share < 0.5 and content.text_chars == 0
    assert content.wants_ocr


def test_a_scan_with_a_stamp_or_footer_is_still_recognised() -> None:
    """A few typed words must not hide the scanned body from the search."""
    content = _content(_typed_footer(scanned_pdf("BODY"), "Seite 1 von 3"))
    assert 0 < content.text_chars < 120
    assert content.wants_ocr


def test_a_scan_that_already_has_a_text_layer_is_left_alone() -> None:
    """Someone recognised it already — perhaps better than Tesseract would."""
    layer = "Sehr geehrte Damen und Herren, " * 6  # a body's worth of text
    content = _content(_typed_footer(scanned_pdf("BODY"), layer))
    assert content.image_share > 0.9 and content.text_chars >= 120
    assert not content.wants_ocr


def test_a_digital_page_with_a_logo_is_left_alone() -> None:
    content = _content(_small_logo_page("Rechnung"))
    assert 0 < content.image_share < 0.05
    assert not content.wants_ocr


def test_pages_are_chosen_one_by_one() -> None:
    with (
        pikepdf.open(io.BytesIO(scanned_pdf("ONE"))) as scan,
        pikepdf.open(io.BytesIO(_small_logo_page("typed"))) as digital,
    ):
        both = pikepdf.Pdf.new()
        both.pages.extend([digital.pages[0], scan.pages[0], digital.pages[0]])
        out = io.BytesIO()
        both.save(out)

    assert ocr.pages_wanting_ocr(out.getvalue()) == [2]
    assert ocr.pages_wanting_ocr(b"not a pdf") == []


def test_languages_are_reduced_to_the_installed_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ocr, "available_languages", lambda _t: frozenset({"eng", "osd"})
    )
    assert ocr.usable_languages("deu+eng", Path("/t")) == "eng"
    assert ocr.usable_languages("deu", Path("/t")) == ""
    assert ocr.usable_languages("deu+eng", None) == ""


def test_a_failing_ocrmypdf_raises_ocr_error(tmp_path: Path) -> None:
    with pytest.raises(ocr.OcrError):
        ocr.add_text_layer(
            scanned_pdf("X"),
            tmp_path,
            ocrmypdf=Path("/nonexistent/ocrmypdf"),
            languages="eng",
            pages=[1],
        )


# --- recognition ---------------------------------------------------------------


@pytest.mark.requires_ocr
@needs_pdftotext
def test_a_scanned_page_becomes_searchable(tmp_path: Path) -> None:
    ocrmypdf, tesseract = _tools()
    scan = scanned_pdf("RECHNUNG 4711")
    assert text_of(scan, tmp_path) == ""

    out = ocr.add_text_layer(
        scan,
        tmp_path,
        ocrmypdf=ocrmypdf,
        languages=ocr.usable_languages("deu+eng", tesseract),
        pages=[1],
    )

    assert "4711" in text_of(out, tmp_path)
    with pikepdf.open(io.BytesIO(out)) as pdf:
        assert len(pdf.pages) == 1


@pytest.mark.requires_ocr
@needs_pdftotext
def test_a_page_with_some_typed_text_is_recognised_fully(tmp_path: Path) -> None:
    """A stamp or footer must not make imapArc skip the scanned body: the
    image is recognised, the typed text is kept as it is."""
    ocrmypdf, tesseract = _tools()
    mixed = _typed_footer(scanned_pdf("MAHNUNG 8899"), "Seite 1 von 1")
    assert "8899" not in text_of(mixed, tmp_path)

    out = ocr.add_text_layer(
        mixed,
        tmp_path,
        ocrmypdf=ocrmypdf,
        languages=ocr.usable_languages("deu+eng", tesseract),
        pages=[1],
    )

    text = text_of(out, tmp_path)
    assert "8899" in text
    assert "Seite 1 von 1" in text


# --- in the pipeline -----------------------------------------------------------


@pytest.mark.requires_ocr
@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@needs_pdftotext
async def test_scanned_attachments_make_the_mail_findable(tmp_path: Path) -> None:
    """The point of it all: a search for what the scan says finds the PDF —
    and the attachment as sent is still in the embedded .eml, byte for byte."""
    scan = scanned_pdf("KONTOAUSZUG 3344")
    photo = scanned_png("QUITTUNG 5566")
    raw = build_mail(
        subject="Unterlagen",
        text="siehe Anhang",
        attachments=[
            ("scan.pdf", "application/pdf", scan),
            ("foto.png", "image/png", photo),
        ],
    )
    tools = ToolPaths.resolve()
    out = tmp_path / "out"

    async with BrowserPool() as pool:
        result = await render_mail(
            parse_mail(raw),
            raw=raw,
            profile="test",
            output_dir=out,
            pool=pool,
            config=RunConfig(tools=tools),
        )

    assert result.combined_pdf is not None and result.ocr_failed == ()
    text = text_of(result.combined_pdf, tmp_path)
    assert "3344" in text and "5566" in text
    assert run_verapdf(tools.verapdf, result.combined_pdf).compliant
    # Untouched original: the embedded .eml still carries the scan as sent.
    source = provenance.embedded_source(result.combined_pdf)
    assert source is not None and source[1] == raw
    sent = {a.filename: a.content for a in parse_mail(source[1]).attachments}
    assert sent["scan.pdf"] == scan and sent["foto.png"] == photo


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
async def test_a_failed_recognition_does_not_cost_the_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OCR adds searchability; without it the content is still complete."""

    def broken(*_args: object, **_kwargs: object) -> bytes:
        raise ocr.OcrError("tesseract gave up")

    monkeypatch.setattr(ocr, "add_text_layer", broken)
    monkeypatch.setattr(ocr, "usable_languages", lambda *_a: "eng")
    raw = build_mail(
        subject="Scan",
        attachments=[("scan.pdf", "application/pdf", scanned_pdf("X"))],
    )
    tools = ToolPaths.resolve().model_copy(update={"ocrmypdf": Path("/x/ocrmypdf")})

    async with BrowserPool() as pool:
        result = await render_mail(
            parse_mail(raw),
            raw=raw,
            profile="test",
            output_dir=tmp_path / "out",
            pool=pool,
            config=RunConfig(tools=tools),
        )

    assert result.written and result.combined_pdf is not None
    assert result.ocr_failed == ("scan.pdf",)
    report = RunReport()
    report.add(result)
    assert "1 attachment(s) could not be made searchable" in report.summary()
    assert "  - scan.pdf" in report.summary()


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
async def test_ocr_can_be_switched_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def spy(*_args: object, **_kwargs: object) -> bytes:
        nonlocal called
        called = True
        raise AssertionError("must not run")

    monkeypatch.setattr(ocr, "add_text_layer", spy)
    raw = build_mail(
        subject="Scan",
        attachments=[("scan.pdf", "application/pdf", scanned_pdf("X"))],
    )

    async with BrowserPool() as pool:
        result = await render_mail(
            parse_mail(raw),
            raw=raw,
            profile="test",
            output_dir=tmp_path / "out",
            pool=pool,
            config=RunConfig(tools=ToolPaths.resolve(), ocr=False),
        )

    assert result.written and not called


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
async def test_file_mode_passes_no_ocr_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`imaparc eml --no-ocr` must reach the pipeline, whatever the profile says."""
    from imaparc.adhoc import run_adhoc

    def spy(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("must not run")

    monkeypatch.setattr(ocr, "add_text_layer", spy)
    mail = tmp_path / "in" / "a.eml"
    mail.parent.mkdir()
    mail.write_bytes(
        build_mail(attachments=[("scan.pdf", "application/pdf", scanned_pdf("X"))])
    )

    report = await run_adhoc(
        [mail],
        profiles=[],
        root=tmp_path / "arc",
        name="mail",
        tools=ToolPaths.resolve(),
        allow_remote=False,
        ocr=False,
        verbosity=0,
    )

    assert len(report.written) == 1 and report.failed == []
