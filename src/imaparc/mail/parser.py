"""Parse raw RFC-822 bytes into a :class:`ParsedMail`."""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Iterator
from datetime import datetime
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import parsedate_to_datetime
from itertools import count

from imaparc.exceptions import ParseError
from imaparc.mail.models import AttachmentPart, MailHeaders, ParsedMail

logger = logging.getLogger(__name__)


def parse_mail(raw: bytes) -> ParsedMail:
    """Parse raw email bytes into a structured :class:`ParsedMail`.

    Args:
        raw: Complete RFC-822 message bytes.

    Returns:
        The parsed email with decoded headers, body renditions, real attachments
        and inline related resources (the two kept apart per RFC 2387).

    Raises:
        ParseError: If the bytes cannot be parsed as an email at all.
    """
    try:
        msg = message_from_bytes(raw, policy=default_policy)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ParseError(f"Could not parse email: {exc}") from exc
    if not isinstance(msg, EmailMessage):  # pragma: no cover - policy guarantees
        raise ParseError("Parsed object is not an EmailMessage")

    headers = _extract_headers(msg)
    html_body = _extract_body(msg, "html")
    text_body = _extract_body(msg, "plain")
    attachments, inline_parts = _extract_parts(msg)
    return ParsedMail(
        headers=headers,
        html_body=html_body,
        text_body=text_body,
        attachments=attachments,
        inline_parts=inline_parts,
    )


def _header(msg: EmailMessage, name: str) -> str:
    value = msg[name]
    return str(value) if value is not None else ""


def _extract_headers(msg: EmailMessage) -> MailHeaders:
    return MailHeaders(
        from_=_header(msg, "From"),
        to=_header(msg, "To"),
        cc=_header(msg, "Cc"),
        bcc=_header(msg, "Bcc"),
        subject=_header(msg, "Subject"),
        date=_parse_date(msg),
        message_id=(str(msg["Message-ID"]).strip() if msg["Message-ID"] else None),
    )


def _parse_date(msg: EmailMessage) -> datetime | None:
    """Parse the Date header, tolerating malformed values.

    The Thunderbird extension turned an unparseable date into ``NaN`` in the
    filename; here a bad date simply yields ``None`` and is logged.
    """
    raw_date = msg["Date"]
    if not raw_date:
        return None
    try:
        return parsedate_to_datetime(str(raw_date))
    except (TypeError, ValueError):
        logger.warning("Unparseable Date header: %r", str(raw_date))
        return None


def _extract_body(msg: EmailMessage, subtype: str) -> str | None:
    """Return the preferred body of the given subtype, decoded to text."""
    part = msg.get_body(preferencelist=(subtype,))
    if part is None:
        return None
    if part.get_content_subtype() != subtype:
        return None
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError) as exc:
        # Unknown charset or undecodable payload: fall back to a lenient decode.
        logger.warning("Body decode fell back to latin-1: %s", exc)
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            return None
        return payload.decode("latin-1", errors="replace")
    return content if isinstance(content, str) else None


def _extract_parts(
    msg: EmailMessage,
) -> tuple[list[AttachmentPart], list[AttachmentPart]]:
    """Split every non-body MIME part into attachments and inline resources.

    Walks the tree per RFC 2046. A ``multipart/*`` node is descended into; every
    other node is a leaf — including a ``message/*`` part, which is opaque
    (RFC 2046 §5.2): its internal parts belong to the encapsulated message and
    must not be pulled up as attachments of this one. The displayed body — the
    ``text/plain``/``text/html`` chosen by :meth:`get_body` — is skipped unless
    it is explicitly an attachment (RFC 2183).

    Of the remaining parts, one carrying a ``Content-ID`` or ``Content-Location``
    is a *related resource* the body references via ``cid:``/Content-Location
    (RFC 2387/2392) — returned separately for inline resolution, never as an
    attachment page. ``iter_attachments()`` is deliberately not used: it treats a
    ``multipart/related`` as a single body candidate and so never reaches the
    inline resources nested inside it.
    """
    body_ids = _body_part_ids(msg)
    attachments: list[AttachmentPart] = []
    inline: list[AttachmentPart] = []
    _collect_parts(msg, body_ids, attachments, inline, count(1))
    return attachments, inline


def _collect_parts(
    part: EmailMessage,
    body_ids: set[int],
    attachments: list[AttachmentPart],
    inline: list[AttachmentPart],
    counter: Iterator[int],
    *,
    in_alternative: bool = False,
) -> None:
    """Recurse the MIME tree, appending each non-body leaf to the right list."""
    disposition = part.get_content_disposition()
    # Descend into a multipart container — but not one that is itself an
    # attachment (a whole compound object attached, e.g. a forwarded message):
    # that stays a single attachment, its inner parts are not this mail's.
    if part.get_content_maintype() == "multipart" and disposition != "attachment":
        child_in_alternative = part.get_content_subtype() == "alternative"
        for sub in part.iter_parts():
            if isinstance(sub, EmailMessage):
                _collect_parts(
                    sub,
                    body_ids,
                    attachments,
                    inline,
                    counter,
                    in_alternative=child_in_alternative,
                )
        return

    if id(part) in body_ids and disposition != "attachment":
        return  # the displayed body, not an attachment

    # A non-selected sibling inside multipart/alternative is the same content in
    # another form (text/calendar, text/x-amp-html, …), not an attachment
    # (RFC 2046 §5.1.4) — present only the chosen body, never these as pages.
    if in_alternative and disposition != "attachment":
        return

    index = next(counter)
    try:
        built = _build_attachment(part, index)
    except Exception:
        # A single malformed part must never fail the whole mail; the raw
        # message is still delivered to the eml archive intact.
        logger.warning("Skipping unparseable attachment part #%d", index)
        return
    if built is None:
        return
    if disposition != "attachment" and (built.content_id or built.content_location):
        inline.append(built)  # related resource referenced from the body
    else:
        attachments.append(built)


def _body_part_ids(msg: EmailMessage) -> set[int]:
    """Object ids of the parts chosen as the displayed body (html and plain)."""
    ids: set[int] = set()
    for subtype in ("html", "plain"):
        part = msg.get_body(preferencelist=(subtype,))
        if part is not None and part.get_content_subtype() == subtype:
            ids.add(id(part))
    return ids


def _build_attachment(part: EmailMessage, index: int) -> AttachmentPart | None:
    """Turn one attachment part into an AttachmentPart, or None if undecodable."""
    content = _attachment_bytes(part)
    if content is None:
        logger.warning("Skipping undecodable attachment part #%d", index)
        return None
    content_type = part.get_content_type().lower()
    location = part.get("Content-Location")
    return AttachmentPart(
        filename=_attachment_filename(part, index, content_type),
        content_type=content_type,
        content=content,
        content_id=_strip_angle_brackets(part.get("Content-ID")),
        content_location=str(location) if location else None,
        is_inline=part.get_content_disposition() == "inline",
    )


def _attachment_bytes(part: EmailMessage) -> bytes | None:
    """Return the original octets of an attachment part, or None if undecodable.

    The Content-Transfer-Encoding-decoded payload *is* the canonical file content
    (RFC 2045 §6); the charset is a display concern and gives no licence to alter
    the stored bytes. So a leaf part uses ``get_payload(decode=True)`` — never
    ``get_content()``, which would decode a ``text/*`` part to ``str`` via its
    charset and re-encode as UTF-8, corrupting (or lossily replacing) the original
    bytes of every ``.txt``/``.csv``/``.ics`` with a non-UTF-8 or mismatched
    charset. Parts with no single octet stream — ``message/rfc822`` and inline
    ``multipart/*`` (a forwarded message) — keep their full raw MIME bytes.
    """
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    # No decodable octet stream: an encapsulated message or a nested multipart.
    try:
        content = part.get_content()
    except (LookupError, ValueError, TypeError):
        content = None
    if isinstance(content, EmailMessage):
        # An attached message (message/rfc822): keep its full raw bytes.
        return content.as_bytes()
    if part.is_multipart():
        return part.as_bytes()
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    # multipart or otherwise non-decodable: keep the part's raw MIME bytes so an
    # embedded message is not lost; only give up if even that is unavailable.
    if part.get_content_maintype() == "multipart":
        return part.as_bytes()
    return None


def _attachment_filename(part: EmailMessage, index: int, content_type: str) -> str:
    """Return a usable, NFC-normalised filename, synthesising one if absent."""
    name = part.get_filename()
    if name:
        return unicodedata.normalize("NFC", name)
    # No filename: synthesise from the subtype so nothing is nameless.
    subtype = content_type.split("/", 1)[-1] or "bin"
    return f"attachment-{index:02d}.{subtype}"


def _strip_angle_brackets(value: str | None) -> str | None:
    if not value:
        return None
    return str(value).strip().lstrip("<").rstrip(">") or None
