"""Integration tests for ImapConnection against a local GreenMail server.

The whole module is skipped unless a GreenMail IMAP server is reachable on
localhost:3143 (started with -Dgreenmail.auth.disabled). Start it with:

    docker run -d -p 3025:3025 -p 3143:3143 \\
      -e GREENMAIL_OPTS='-Dgreenmail.setup.test.all -Dgreenmail.auth.disabled' \\
      greenmail/standalone:2.1.0
"""

from __future__ import annotations

import os
import smtplib
import socket
from email.message import EmailMessage

import pytest

from imaparc.accounts import Account
from imaparc.sources.imap import ImapConnection


def _greenmail_up() -> bool:
    try:
        with socket.create_connection(("localhost", 3143), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _greenmail_up(), reason="GreenMail IMAP not reachable on localhost:3143"
)

# A per-process unique mailbox, so tests do not see each other's mail.
_USER = f"fetch-test-{os.getpid()}@localhost"


def _deliver(subject: str) -> None:
    msg = EmailMessage()
    msg["From"] = "sender@example.com"
    msg["To"] = _USER
    msg["Subject"] = subject
    msg.set_content("body")
    with smtplib.SMTP("localhost", 3025) as smtp:
        smtp.send_message(msg)


def _account() -> Account:
    return Account(
        name="test",
        host="localhost",
        port=3143,
        user=_USER,
        password="x",
        ssl=False,
    )


def test_scan_returns_envelope_headers() -> None:
    _deliver("Fetch One")
    _deliver("Fetch Two")

    with ImapConnection(_account()) as conn:
        uidvalidity, messages = conn.scan("INBOX")
        assert uidvalidity > 0
        subjects = {m.headers.subject for m in messages}
        assert {"Fetch One", "Fetch Two"} <= subjects
        # Envelope carries the sender for matching, without pulling the body.
        assert all(m.headers.from_ == "sender@example.com" for m in messages)


def test_fetch_body_reads_via_peek() -> None:
    _deliver("Body Please")

    with ImapConnection(_account()) as conn:
        _uidvalidity, messages = conn.scan("INBOX")
        target = next(m for m in messages if m.headers.subject == "Body Please")
        raw = conn.fetch_body("INBOX", target.uid)
        assert raw is not None and b"Body Please" in raw


def test_label_and_move() -> None:
    _deliver("To Be Moved")

    with ImapConnection(_account()) as conn:
        _uidvalidity, messages = conn.scan("INBOX")
        uid = next(m.uid for m in messages if m.headers.subject == "To Be Moved")
        conn.label("INBOX", uid, "Archiviert")
        conn.move("INBOX", uid, "Archiv-Test")

        moved = conn._conn.select_folder("Archiv-Test", readonly=True)
        assert int(moved[b"EXISTS"]) >= 1
        # The freshly created target is subscribed, so a mail client shows it.
        subscribed = {name for _flags, _delim, name in conn._conn.list_sub_folders()}
        assert "Archiv-Test" in subscribed
