"""Tests for attachment → PDF conversion orchestration and text rendering."""

from __future__ import annotations

import io
import shutil
from pathlib import Path

import pikepdf
import pytest
from PIL import Image

from imaparc.attachments.models import FailureReason
from imaparc.attachments.to_pdf import convert_attachment
from imaparc.html.render_html import render_text_attachment
from imaparc.mail.models import AttachmentPart


def _pdf(pages: int = 1) -> bytes:
    doc = pikepdf.Pdf.new()
    for _ in range(pages):
        doc.add_blank_page()
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (20, 20), (1, 2, 3)).save(out, format="JPEG")
    return out.getvalue()


def _att(content: bytes, ctype: str, name: str) -> AttachmentPart:
    return AttachmentPart(filename=name, content_type=ctype, content=content)


async def _fake_text_renderer(filename: str, text: str) -> bytes:
    return _pdf(1)


QPDF = shutil.which("qpdf")


# --- render_text_attachment (pure) ------------------------------------------


def test_render_text_attachment_shows_name_and_content() -> None:
    html = render_text_attachment("notes.txt", "line one\nline two")
    assert "notes.txt" in html
    assert "line one" in html


def test_render_text_attachment_escapes() -> None:
    html = render_text_attachment("x.txt", "<script>evil()</script>")
    assert "<script>evil" not in html


# --- convert_attachment (image / text / opaque, no external tools) ----------


async def test_convert_image_succeeds() -> None:
    out = await convert_attachment(
        _att(_jpeg(), "image/jpeg", "photo.jpg"),
        render_text=_fake_text_renderer,
        qpdf=Path("qpdf"),
        work_dir=Path("."),
    )
    assert out.ok
    assert out.page_count == 1


async def test_convert_text_uses_renderer(tmp_path: Path) -> None:
    out = await convert_attachment(
        _att(b"# hello", "text/markdown", "readme.md"),
        render_text=_fake_text_renderer,
        qpdf=Path("qpdf"),
        work_dir=tmp_path,
    )
    assert out.ok
    assert out.page_count == 1


async def test_convert_opaque_is_unsupported(tmp_path: Path) -> None:
    out = await convert_attachment(
        _att(b"PK\x03\x04zip", "application/zip", "a.zip"),
        render_text=_fake_text_renderer,
        qpdf=Path("qpdf"),
        work_dir=tmp_path,
    )
    assert not out.ok
    assert out.reason is FailureReason.UNSUPPORTED_TYPE


async def test_convert_empty_is_empty(tmp_path: Path) -> None:
    out = await convert_attachment(
        _att(b"", "application/pdf", "empty.pdf"),
        render_text=_fake_text_renderer,
        qpdf=Path("qpdf"),
        work_dir=tmp_path,
    )
    assert out.reason is FailureReason.EMPTY


async def test_convert_times_out(tmp_path: Path) -> None:
    async def _slow_renderer(filename: str, text: str) -> bytes:
        import asyncio

        await asyncio.sleep(10)
        return _pdf(1)

    out = await convert_attachment(
        _att(b"# slow", "text/markdown", "slow.md"),
        render_text=_slow_renderer,
        qpdf=Path("qpdf"),
        work_dir=tmp_path,
        timeout_s=0.05,
    )
    assert not out.ok
    assert out.reason is FailureReason.TIMEOUT


async def test_convert_too_large(tmp_path: Path) -> None:
    out = await convert_attachment(
        _att(_jpeg(), "image/jpeg", "big.jpg"),
        render_text=_fake_text_renderer,
        qpdf=Path("qpdf"),
        work_dir=tmp_path,
        max_bytes=10,
    )
    assert out.reason is FailureReason.TOO_LARGE


async def test_convert_corrupt_image_transcode_fails(tmp_path: Path) -> None:
    out = await convert_attachment(
        _att(b"\xff\xd8\xffgarbage-jpeg", "image/jpeg", "broken.jpg"),
        render_text=_fake_text_renderer,
        qpdf=Path("qpdf"),
        work_dir=tmp_path,
    )
    assert out.reason is FailureReason.TRANSCODE_FAILED


# --- PDF attachments via qpdf (gated) ---------------------------------------


@pytest.mark.requires_tools
async def test_convert_pdf_succeeds(tmp_path: Path) -> None:
    assert QPDF
    out = await convert_attachment(
        _att(_pdf(3), "application/pdf", "doc.pdf"),
        render_text=_fake_text_renderer,
        qpdf=Path(QPDF),
        work_dir=tmp_path,
    )
    assert out.ok
    assert out.page_count == 3


@pytest.mark.requires_tools
async def test_convert_encrypted_pdf_reports_encrypted(tmp_path: Path) -> None:
    assert QPDF
    enc = io.BytesIO()
    doc = pikepdf.Pdf.new()
    doc.add_blank_page()
    doc.save(enc, encryption=pikepdf.Encryption(owner="o", user="secret"))
    out = await convert_attachment(
        _att(enc.getvalue(), "application/pdf", "locked.pdf"),
        render_text=_fake_text_renderer,
        qpdf=Path(QPDF),
        work_dir=tmp_path,
    )
    assert out.reason is FailureReason.ENCRYPTED
