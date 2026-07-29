"""Tests for attachment classification and MIME sniffing."""

from __future__ import annotations

import pytest

from imaparc.attachments.classify import (
    AttachmentKind,
    classify,
    effective_mime,
    sniff_mime,
)
from imaparc.mail.models import AttachmentPart

PDF_MAGIC = b"%PDF-1.7\n..."
PNG_MAGIC = b"\x89PNG\r\n\x1a\n...."
JPEG_MAGIC = b"\xff\xd8\xff\xe0...."
HEIC_MAGIC = b"\x00\x00\x00\x18ftypheic...."


def _att(content: bytes, content_type: str, filename: str = "f") -> AttachmentPart:
    return AttachmentPart(filename=filename, content_type=content_type, content=content)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (PDF_MAGIC, "application/pdf"),
        (PNG_MAGIC, "image/png"),
        (JPEG_MAGIC, "image/jpeg"),
        (HEIC_MAGIC, "image/heic"),
        (b"GIF89a....", "image/gif"),
        (b"unknown bytes", None),
        (b"", None),
    ],
)
def test_sniff_mime(data: bytes, expected: str | None) -> None:
    assert sniff_mime(data) == expected


def test_sniff_overrides_octet_stream() -> None:
    # A PDF mislabelled as octet-stream is still classified as a PDF.
    att = _att(PDF_MAGIC, "application/octet-stream")
    assert effective_mime(att) == "application/pdf"
    assert classify(att) == AttachmentKind.PDF


def test_declared_type_used_when_no_sniff() -> None:
    att = _att(b"random opaque bytes", "application/vnd.ms-excel")
    assert effective_mime(att) == "application/vnd.ms-excel"
    assert classify(att) == AttachmentKind.OPAQUE


def test_classify_pdf() -> None:
    assert classify(_att(PDF_MAGIC, "application/pdf")) == AttachmentKind.PDF


def test_classify_image() -> None:
    assert classify(_att(PNG_MAGIC, "image/png")) == AttachmentKind.IMAGE


def test_classify_docx_is_opaque() -> None:
    att = _att(
        b"PK\x03\x04opaque",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert classify(att) == AttachmentKind.OPAQUE


def test_classify_webp_image() -> None:
    webp = b"RIFF\x00\x00\x00\x00WEBPVP8 "
    assert classify(_att(webp, "application/octet-stream")) == AttachmentKind.IMAGE


def test_classify_text_by_mime() -> None:
    assert classify(_att(b"hello", "text/plain", "note.txt")) == AttachmentKind.TEXT
    assert classify(_att(b"# md", "text/markdown", "readme.md")) == AttachmentKind.TEXT


def test_classify_markdown_by_extension() -> None:
    # .md often arrives labelled as octet-stream.
    att = _att(b"# Title", "application/octet-stream", "readme.md")
    assert classify(att) == AttachmentKind.TEXT


def test_classify_binary_octet_stream_is_opaque() -> None:
    att = _att(b"\x00\x01binary", "application/octet-stream", "data.bin")
    assert classify(att) == AttachmentKind.OPAQUE
