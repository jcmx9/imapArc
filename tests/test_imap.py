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


# --- message size ----------------------------------------------------------


def _deliver_sized(subject: str, body_bytes: int) -> None:
    """Deliver a mail whose body is roughly ``body_bytes`` long."""
    msg = EmailMessage()
    msg["From"] = "sender@example.com"
    msg["To"] = _USER
    msg["Subject"] = subject
    msg.set_content("x" * body_bytes)
    with smtplib.SMTP("localhost", 3025) as smtp:
        smtp.send_message(msg)


def test_scan_reports_the_message_size() -> None:
    """Needed for the size filter, and free — it rides along in the same FETCH."""
    _deliver_sized("Sized Small", 100)
    _deliver_sized("Sized Large", 200_000)

    with ImapConnection(_account()) as conn:
        _, messages = conn.scan("INBOX")
    by_subject = {m.headers.subject: m for m in messages}

    assert by_subject["Sized Small"].size is not None
    assert by_subject["Sized Small"].size < 10_000
    assert by_subject["Sized Large"].size > 100_000


def test_scan_can_let_the_server_drop_small_mail() -> None:
    """IMAP LARGER runs on the server, so small mail costs no header fetch."""
    _deliver_sized("Larger Tiny", 100)
    _deliver_sized("Larger Huge", 200_000)

    with ImapConnection(_account()) as conn:
        _, messages = conn.scan("INBOX", larger=100_000)
    subjects = {m.headers.subject for m in messages}

    assert "Larger Huge" in subjects
    assert "Larger Tiny" not in subjects


def test_scan_can_let_the_server_drop_large_mail() -> None:
    _deliver_sized("Smaller Tiny", 100)
    _deliver_sized("Smaller Huge", 200_000)

    with ImapConnection(_account()) as conn:
        _, messages = conn.scan("INBOX", smaller=100_000)
    subjects = {m.headers.subject for m in messages}

    assert "Smaller Tiny" in subjects
    assert "Smaller Huge" not in subjects
