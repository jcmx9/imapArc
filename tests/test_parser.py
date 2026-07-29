"""Tests for the .eml parser."""

from __future__ import annotations

from datetime import datetime

import pytest

from imaparc.exceptions import ParseError
from imaparc.mail.parser import parse_mail
from tests.mail_builder import build_mail


def test_parses_basic_headers() -> None:
    mail = parse_mail(build_mail(subject="Rechnung", from_="a@x.com", to="b@y.com"))
    assert mail.headers.subject == "Rechnung"
    assert mail.headers.from_ == "a@x.com"
    assert mail.headers.to == "b@y.com"
    assert mail.headers.message_id == "<abc@example.com>"


def test_decodes_rfc2047_subject_with_umlauts() -> None:
    # A subject with non-ASCII must come back decoded, not as =?utf-8?...
    mail = parse_mail(build_mail(subject="Grüße über Ümläute"))
    assert mail.headers.subject == "Grüße über Ümläute"


def test_parses_date_header() -> None:
    mail = parse_mail(build_mail(date="Mon, 23 Mar 2026 02:18:04 +0100"))
    assert isinstance(mail.headers.date, datetime)
    assert mail.headers.date.year == 2026
    assert mail.headers.date.month == 3


def test_missing_date_is_none() -> None:
    mail = parse_mail(build_mail(date=None))
    assert mail.headers.date is None


def test_malformed_date_is_none_not_error() -> None:
    mail = parse_mail(build_mail(date="not a real date"))
    assert mail.headers.date is None


def test_extracts_text_body() -> None:
    mail = parse_mail(build_mail(text="Plain body here", html=None))
    assert mail.text_body is not None
    assert mail.text_body.strip() == "Plain body here"
    assert mail.html_body is None


def test_extracts_both_html_and_text() -> None:
    mail = parse_mail(build_mail(text="plain", html="<p>rich</p>"))
    assert mail.text_body is not None
    assert mail.text_body.strip() == "plain"
    assert mail.html_body is not None
    assert "rich" in mail.html_body


def test_html_only_mail_has_no_text_body() -> None:
    mail = parse_mail(build_mail(text=None, html="<p>only html</p>"))
    assert mail.text_body is None
    assert mail.html_body is not None


def test_collects_attachment() -> None:
    mail = parse_mail(
        build_mail(attachments=[("invoice.pdf", "application/pdf", b"%PDF-1.4 x")])
    )
    assert len(mail.attachments) == 1
    att = mail.attachments[0]
    assert att.filename == "invoice.pdf"
    assert att.content_type == "application/pdf"
    assert att.content == b"%PDF-1.4 x"
    assert att.size == 10
    assert att.is_inline is False


def test_inline_image_is_a_related_resource_not_an_attachment() -> None:
    # An inline part with a Content-ID is a related resource (RFC 2387/2392):
    # it belongs in inline_parts for body resolution, never in attachments.
    mail = parse_mail(
        build_mail(
            html="<p><img src='cid:logo'></p>",
            text=None,
            inline_images=[("logo", "image/png", b"\x89PNG\r\n\x1a\n")],
        )
    )
    assert mail.attachments == []
    assert len(mail.inline_parts) == 1
    assert mail.inline_parts[0].content_id == "logo"
    assert mail.inline_parts[0].is_inline is True


def test_nested_related_inline_image_is_reached() -> None:
    # The real-world regression: mixed > [related > [alternative, inline png], pdf].
    # iter_attachments() skips the whole multipart/related as a body candidate and
    # never reaches the nested inline image; the recursive walk must find it.
    raw = (
        b"From: a@x.com\r\nSubject: nested\r\n"
        b'Content-Type: multipart/mixed; boundary="MIX"\r\n\r\n'
        b"--MIX\r\n"
        b'Content-Type: multipart/related; boundary="REL"\r\n\r\n'
        b"--REL\r\n"
        b'Content-Type: multipart/alternative; boundary="ALT"\r\n\r\n'
        b"--ALT\r\nContent-Type: text/plain\r\n\r\nhi\r\n"
        b"--ALT\r\nContent-Type: text/html\r\n\r\n<p><img src=cid:logo></p>\r\n"
        b"--ALT--\r\n"
        b"--REL\r\n"
        b"Content-Type: image/png\r\nContent-ID: <logo>\r\n"
        b"Content-Disposition: inline\r\nContent-Transfer-Encoding: base64\r\n\r\n"
        b"iVBORw0KGgo=\r\n"
        b"--REL--\r\n"
        b"--MIX\r\n"
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="doc.pdf"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0=\r\n"
        b"--MIX--\r\n"
    )
    mail = parse_mail(raw)
    assert [a.filename for a in mail.attachments] == ["doc.pdf"]
    assert [p.content_id for p in mail.inline_parts] == ["logo"]


def test_text_attachment_bytes_are_preserved_not_transcoded() -> None:
    # A text/* attachment must be stored as its original octets (RFC 2045 §6),
    # never decoded via its charset and re-encoded as UTF-8 (which corrupts or
    # loses non-UTF-8 bytes). Here: € é CRLF in windows-1252.
    import base64

    original = bytes([0x80, 0xE9, 0x0D, 0x0A])
    b64 = base64.b64encode(original).decode()
    raw = (
        b"From: a@x.com\r\nSubject: t\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        b"--B\r\n"
        b'Content-Type: text/csv; charset="windows-1252"\r\n'
        b'Content-Disposition: attachment; filename="d.csv"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\n" + b64.encode() + b"\r\n"
        b"--B--\r\n"
    )
    mail = parse_mail(raw)
    att = next(a for a in mail.attachments if a.filename == "d.csv")
    assert att.content == original


def test_alternative_siblings_are_not_attachments() -> None:
    # Non-selected multipart/alternative parts (text/calendar, amp-html) are the
    # same content in another form (RFC 2046 §5.1.4), not attachment pages.
    raw = (
        b"From: a@x.com\r\nSubject: invite\r\n"
        b'Content-Type: multipart/alternative; boundary="ALT"\r\n\r\n'
        b"--ALT\r\nContent-Type: text/plain\r\n\r\nplain\r\n"
        b"--ALT\r\nContent-Type: text/html\r\n\r\n<p>html</p>\r\n"
        b"--ALT\r\nContent-Type: text/calendar; method=REQUEST\r\n\r\n"
        b"BEGIN:VCALENDAR\r\n"
        b"--ALT--\r\n"
    )
    mail = parse_mail(raw)
    assert mail.attachments == []
    assert mail.html_body is not None and mail.text_body is not None


def test_calendar_attachment_disposition_is_kept() -> None:
    # But an alternative part explicitly marked as an attachment is honoured.
    raw = (
        b"From: a@x.com\r\nSubject: invite\r\n"
        b'Content-Type: multipart/alternative; boundary="ALT"\r\n\r\n'
        b"--ALT\r\nContent-Type: text/plain\r\n\r\nplain\r\n"
        b"--ALT\r\nContent-Type: text/calendar\r\n"
        b'Content-Disposition: attachment; filename="invite.ics"\r\n\r\n'
        b"BEGIN:VCALENDAR\r\n--ALT--\r\n"
    )
    mail = parse_mail(raw)
    assert [a.filename for a in mail.attachments] == ["invite.ics"]


def test_attachment_without_filename_gets_synthesised_name() -> None:
    # Build a part with no filename by hand via the low-level API.
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "a@x.com"
    msg["Subject"] = "no name"
    msg.set_content("body")
    msg.add_attachment(b"data", maintype="application", subtype="zip")
    mail = parse_mail(msg.as_bytes())
    assert len(mail.attachments) == 1
    assert mail.attachments[0].filename == "attachment-01.zip"


def test_multiple_attachments_preserve_order() -> None:
    mail = parse_mail(
        build_mail(
            attachments=[
                ("first.pdf", "application/pdf", b"a"),
                ("second.docx", "application/vnd.openxmlformats", b"b"),
            ]
        )
    )
    assert [a.filename for a in mail.attachments] == ["first.pdf", "second.docx"]


def test_multipart_attachment_does_not_crash_parse() -> None:
    # A mail carrying a nested multipart/mixed as an inline attachment made
    # get_content() raise KeyError('multipart/mixed'), failing the whole parse.
    raw = (
        b"From: sender@example.com\r\n"
        b"Subject: forwarded\r\n"
        b'Content-Type: multipart/mixed; boundary="OUTER"\r\n'
        b"\r\n"
        b"--OUTER\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"see attached\r\n"
        b"--OUTER\r\n"
        b'Content-Type: multipart/mixed; boundary="INNER"\r\n'
        b'Content-Disposition: attachment; filename="nested.eml"\r\n'
        b"\r\n"
        b"--INNER\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"nested body\r\n"
        b"--INNER--\r\n"
        b"--OUTER--\r\n"
    )
    mail = parse_mail(raw)
    assert mail.headers.subject == "forwarded"
    # The nested multipart is kept as an attachment (raw MIME bytes), not lost.
    assert len(mail.attachments) == 1
    assert b"nested body" in mail.attachments[0].content


def test_forwarded_message_is_opaque_not_flattened() -> None:
    # A message/rfc822 part is opaque (RFC 2046 §5.2): its own inline image must
    # stay inside it, not be pulled up as an attachment/inline part of this mail.
    inner = build_mail(
        html="<p><img src=cid:innerlogo></p>",
        text=None,
        inline_images=[("innerlogo", "image/png", b"\x89PNG")],
        subject="inner",
    )
    raw = (
        b"From: a@x.com\r\nSubject: outer\r\n"
        b'Content-Type: multipart/mixed; boundary="MIX"\r\n\r\n'
        b"--MIX\r\nContent-Type: text/plain\r\n\r\nsee attached\r\n"
        b"--MIX\r\n"
        b"Content-Type: message/rfc822\r\n"
        b'Content-Disposition: attachment; filename="fwd.eml"\r\n\r\n'
        + inner
        + b"\r\n--MIX--\r\n"
    )
    mail = parse_mail(raw)
    # Exactly one attachment (the whole forwarded mail); the inner inline image is
    # not surfaced as this mail's own part.
    assert len(mail.attachments) == 1
    assert mail.attachments[0].content_type == "message/rfc822"
    assert mail.inline_parts == []


def test_garbage_bytes_raise_parse_error_or_empty() -> None:
    # The email parser is very lenient; ensure we at least never crash.
    mail = parse_mail(b"this is not an email at all")
    assert isinstance(mail.headers.subject, str)


def test_truly_broken_input_type() -> None:
    with pytest.raises((ParseError, TypeError)):
        parse_mail(None)  # type: ignore[arg-type]
