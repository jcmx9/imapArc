"""Tests for image → PDF conversion (uses img2pdf/Pillow, no external tools)."""

from __future__ import annotations

import io

import pikepdf
import pytest
from PIL import Image

from imaparc.attachments.image_to_pdf import ImageConversionError, image_to_pdf


def _encode(size: tuple[int, int], mode: str, fmt: str, color: object) -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, format=fmt)  # type: ignore[arg-type]
    return out.getvalue()


def _pages(pdf: bytes) -> list[pikepdf.Page]:
    with pikepdf.open(io.BytesIO(pdf)) as doc:
        return list(doc.pages)


def _mediabox(pdf: bytes) -> tuple[float, float]:
    with pikepdf.open(io.BytesIO(pdf)) as doc:
        box = doc.pages[0].mediabox
        return float(box[2]) - float(box[0]), float(box[3]) - float(box[1])


def test_jpeg_to_pdf() -> None:
    pdf = image_to_pdf(_encode((20, 20), "RGB", "JPEG", (0, 128, 255)))
    assert pdf.startswith(b"%PDF-")
    assert len(_pages(pdf)) == 1


def test_png_to_pdf() -> None:
    pdf = image_to_pdf(_encode((20, 20), "RGB", "PNG", (255, 0, 0)))
    assert len(_pages(pdf)) == 1


def test_rgba_png_is_flattened_via_preflight() -> None:
    # img2pdf rejects alpha; the preflight composites onto white and retries.
    pdf = image_to_pdf(_encode((20, 20), "RGBA", "PNG", (255, 0, 0, 128)))
    assert pdf.startswith(b"%PDF-")
    assert len(_pages(pdf)) == 1


def test_16bit_png_is_downscaled_via_preflight() -> None:
    pdf = image_to_pdf(_encode((20, 20), "I;16", "PNG", 4096))
    assert len(_pages(pdf)) == 1


def test_portrait_image_gives_portrait_page() -> None:
    w, h = _mediabox(image_to_pdf(_encode((20, 40), "RGB", "JPEG", (0, 0, 0))))
    assert h > w


def test_landscape_image_gives_landscape_page() -> None:
    w, h = _mediabox(image_to_pdf(_encode((40, 20), "RGB", "JPEG", (0, 0, 0))))
    assert w > h


def test_image_keeps_minimum_print_margin() -> None:
    # An image must never bleed to the sheet edge — it is inset by at least the
    # print margin on both axes (the image is placed via a `… cm` matrix whose
    # translation is the bottom-left offset).
    import re

    import img2pdf

    pdf = image_to_pdf(_encode((400, 300), "RGB", "JPEG", (255, 0, 0)))
    with pikepdf.open(io.BytesIO(pdf)) as doc:
        content = doc.pages[0].Contents.read_bytes().decode("latin-1")
    match = re.search(r"[\d.]+ 0 0 [\d.]+ ([\d.]+) ([\d.]+) cm", content)
    assert match is not None
    tx, ty = float(match.group(1)), float(match.group(2))
    min_pt = img2pdf.mm_to_pt(10) - 0.5
    assert tx >= min_pt and ty >= min_pt


def test_empty_bytes_raises() -> None:
    with pytest.raises(ImageConversionError):
        image_to_pdf(b"")


def test_garbage_bytes_raises() -> None:
    with pytest.raises(ImageConversionError):
        image_to_pdf(b"this is not an image at all")
