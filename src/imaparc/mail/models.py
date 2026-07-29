"""Parsed representation of an email.

These are plain, immutable data carriers passed between pipeline stages. They
hold decoded, ready-to-use values so downstream code never touches the raw
MIME tree again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class AttachmentPart:
    """One non-body MIME part — a real attachment or an inline related resource.

    Attributes:
        filename: Decoded filename (NFC-normalised), or a synthesised name if
            the part carried none.
        content_type: Lowercased MIME type, e.g. ``application/pdf``.
        content: Raw decoded bytes of the part.
        content_id: The ``Content-ID`` without angle brackets, if present.
        content_location: The ``Content-Location`` value, if present.
        is_inline: True when ``Content-Disposition`` is ``inline``.
    """

    filename: str
    content_type: str
    content: bytes
    content_id: str | None = None
    content_location: str | None = None
    is_inline: bool = False

    @property
    def size(self) -> int:
        """Byte length of the attachment content."""
        return len(self.content)


@dataclass(frozen=True, slots=True)
class MailHeaders:
    """Decoded, human-facing header values.

    All string fields are RFC-2047-decoded and default to an empty string so
    templates and naming logic never have to guard against ``None``.
    """

    from_: str = ""
    to: str = ""
    cc: str = ""
    bcc: str = ""
    subject: str = ""
    date: datetime | None = None
    message_id: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedMail:
    """The full parsed email, ready for the conversion pipeline.

    ``attachments`` are real attachments (rendered as their own pages, kept as
    originals, and matched by the ``attachments`` profile rule). ``inline_parts``
    are ``multipart/related`` resources referenced from the body via
    ``cid:``/Content-Location (RFC 2387/2392); they are resolved into the body
    during rendering and are never shown as separate attachment pages.
    """

    headers: MailHeaders
    html_body: str | None = None
    text_body: str | None = None
    attachments: list[AttachmentPart] = field(default_factory=list)
    inline_parts: list[AttachmentPart] = field(default_factory=list)
