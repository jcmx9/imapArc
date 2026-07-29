"""Helper to build synthetic .eml bytes for tests.

Keeping fixtures as readable code rather than opaque blobs — each test
constructs exactly the mail shape it needs.
"""

from __future__ import annotations

from email.message import EmailMessage


def build_mail(
    *,
    from_: str = "sender@example.com",
    to: str = "me@example.com",
    subject: str = "Test",
    date: str | None = "Mon, 23 Mar 2026 02:18:04 +0100",
    message_id: str | None = "<abc@example.com>",
    text: str | None = "Hello world",
    html: str | None = None,
    cc: str | None = None,
    attachments: list[tuple[str, str, bytes]] | None = None,
    inline_images: list[tuple[str, str, bytes]] | None = None,
) -> bytes:
    """Assemble a synthetic email and return its raw bytes.

    Args:
        attachments: list of (filename, mime_type, content).
        inline_images: list of (content_id, mime_type, content); attached with
            ``Content-Disposition: inline`` and a ``Content-ID``.
    """
    msg = EmailMessage()
    msg["From"] = from_
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    if date is not None:
        msg["Date"] = date
    if message_id is not None:
        msg["Message-ID"] = message_id

    if text is not None:
        msg.set_content(text)
    if html is not None:
        if text is not None:
            msg.add_alternative(html, subtype="html")
        else:
            msg.set_content(html, subtype="html")

    for filename, mime, content in attachments or []:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(
            content, maintype=maintype, subtype=subtype, filename=filename
        )

    for cid, mime, content in inline_images or []:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(
            content,
            maintype=maintype,
            subtype=subtype,
            disposition="inline",
            cid=f"<{cid}>",
        )

    return msg.as_bytes()
