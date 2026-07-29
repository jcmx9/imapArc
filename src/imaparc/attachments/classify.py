"""Classify an attachment into a conversion strategy.

The declared MIME type is not trusted on its own: mail clients routinely label
PDFs and images as ``application/octet-stream``. We sniff the leading bytes and
let a positive sniff override the declared type.
"""

from __future__ import annotations

import enum
from pathlib import PurePosixPath

from imaparc.mail.models import AttachmentPart


class AttachmentKind(enum.Enum):
    """How an attachment should be turned into pages."""

    PDF = "pdf"
    IMAGE = "image"
    TEXT = "text"  # txt / md — typeset to pages via imapArc's own templates
    OPAQUE = "opaque"  # only listed on an info page, no rendered pages


# Plain-text formats typeset to pages via imapArc's own templates (font-safe).
_TEXT_TYPES = frozenset({"text/plain", "text/markdown", "text/x-markdown"})
_TEXT_EXTENSIONS = frozenset({".txt", ".md", ".markdown", ".text"})


# Image MIME types img2pdf / pillow-heif can turn into a page losslessly.
_IMAGE_TYPES = frozenset(
    {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/gif",
        "image/tiff",
        "image/webp",
        "image/heic",
        "image/heif",
        "image/avif",
    }
)


def sniff_mime(data: bytes) -> str | None:
    """Return a MIME type sniffed from magic bytes, or None if unrecognised."""
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:2] in (b"II", b"MM") and data[2:4] in (b"\x2a\x00", b"\x00\x2a"):
        return "image/tiff"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    # ISO-BMFF (HEIF/HEIC/AVIF): 'ftyp' box with a known brand.
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"heic", b"heix", b"heim", b"heis", b"mif1", b"msf1"):
            return "image/heic"
        if brand in (b"avif", b"avis"):
            return "image/avif"
    return None


def effective_mime(att: AttachmentPart) -> str:
    """Return the MIME type to act on, letting a magic-byte sniff win."""
    sniffed = sniff_mime(att.content)
    if sniffed is not None:
        return sniffed
    return att.content_type


def _is_text(att: AttachmentPart, mime: str) -> bool:
    """Whether an attachment is a plain-text/markdown document.

    Uses the MIME type and the filename extension, since ``.md`` is often
    delivered as ``application/octet-stream`` or ``text/plain``.
    """
    if mime in _TEXT_TYPES:
        return True
    suffix = PurePosixPath(att.filename).suffix.lower()
    return suffix in _TEXT_EXTENSIONS


def classify(att: AttachmentPart) -> AttachmentKind:
    """Decide how the attachment becomes pages in the combined PDF."""
    mime = effective_mime(att)
    if mime == "application/pdf":
        return AttachmentKind.PDF
    if mime in _IMAGE_TYPES:
        return AttachmentKind.IMAGE
    if _is_text(att, mime):
        return AttachmentKind.TEXT
    return AttachmentKind.OPAQUE
