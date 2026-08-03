"""Tests for `imaparc restore` — putting archived mail back on a server."""

from __future__ import annotations

import imaplib
import os
import socket
from pathlib import Path

import pytest
from pydantic import SecretStr

from imaparc.accounts import Account
from imaparc.restore import RestoreOutcome, restore_files
from imaparc.sources.imap import ImapConnection
from tests.mail_builder import build_mail


def _greenmail_up() -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("localhost", 3143)) == 0


pytestmark = pytest.mark.skipif(not _greenmail_up(), reason="GreenMail not running")


def _account(user: str) -> Account:
    return Account(
        name="a",
        host="localhost",
        port=3143,
        ssl=False,
        user=user,
        password=SecretStr("x"),
    )


def _mailbox_count(user: str, folder: str = "INBOX") -> int:
    box = imaplib.IMAP4("localhost", 3143)
    box.login(user, "x")
    box.select(folder)
    _typ, data = box.search(None, "ALL")
    box.logout()
    return len(data[0].split())


def _user(tag: str) -> str:
    return f"restore-{tag}-{os.getpid()}@localhost"


def test_uploads_an_archived_mail(tmp_path: Path) -> None:
    user = _user("upload")
    eml = tmp_path / "mail.eml"
    eml.write_bytes(build_mail(to=user, subject="Az 4711", message_id="<r1@test>"))

    with ImapConnection(_account(user)) as conn:
        results = restore_files(conn, [eml], folder="INBOX")

    assert [r.outcome for r in results] == [RestoreOutcome.UPLOADED]
    assert _mailbox_count(user) == 1


def test_running_twice_does_not_duplicate(tmp_path: Path) -> None:
    """Restoring is idempotent: the mail is matched by Message-ID first.

    Without this, every re-run would add another copy to the mailbox — and the
    obvious reason to restore something is that you are not sure it is there.
    """
    user = _user("twice")
    eml = tmp_path / "mail.eml"
    eml.write_bytes(build_mail(to=user, subject="Az 4711", message_id="<r2@test>"))

    with ImapConnection(_account(user)) as conn:
        restore_files(conn, [eml], folder="INBOX")
        second = restore_files(conn, [eml], folder="INBOX")

    assert [r.outcome for r in second] == [RestoreOutcome.ALREADY_THERE]
    assert _mailbox_count(user) == 1


def test_dry_run_uploads_nothing(tmp_path: Path) -> None:
    user = _user("dry")
    eml = tmp_path / "mail.eml"
    eml.write_bytes(build_mail(to=user, subject="Az 4711", message_id="<r3@test>"))

    with ImapConnection(_account(user)) as conn:
        results = restore_files(conn, [eml], folder="INBOX", dry_run=True)

    assert [r.outcome for r in results] == [RestoreOutcome.WOULD_UPLOAD]
    assert _mailbox_count(user) == 0


def test_creates_a_missing_target_folder(tmp_path: Path) -> None:
    user = _user("folder")
    eml = tmp_path / "mail.eml"
    eml.write_bytes(build_mail(to=user, subject="Az", message_id="<r4@test>"))

    with ImapConnection(_account(user)) as conn:
        restore_files(conn, [eml], folder="Wiederhergestellt")

    assert _mailbox_count(user, "Wiederhergestellt") == 1


def test_an_unreadable_file_does_not_stop_the_rest(tmp_path: Path) -> None:
    user = _user("broken")
    broken = tmp_path / "aa-broken.eml"
    broken.write_bytes(build_mail(to=user, message_id="<r5@test>"))
    broken.chmod(0o000)
    good = tmp_path / "zz-good.eml"
    good.write_bytes(build_mail(to=user, subject="Good", message_id="<r6@test>"))

    try:
        with ImapConnection(_account(user)) as conn:
            results = restore_files(conn, [broken, good], folder="INBOX")
    finally:
        broken.chmod(0o600)

    outcomes = [r.outcome for r in results]
    assert RestoreOutcome.FAILED in outcomes
    assert RestoreOutcome.UPLOADED in outcomes
    assert _mailbox_count(user) == 1
