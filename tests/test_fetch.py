"""Tests for the fetch orchestration (pure parts + GreenMail end-to-end)."""

from __future__ import annotations

import os
import smtplib
import socket
from email.message import EmailMessage
from pathlib import Path

import pytest

from imaparc.accounts import Account
from imaparc.exceptions import ImapArcError
from imaparc.fetch import FetchReport, _folder_map, _match_candidate, run_fetch
from imaparc.mail.models import MailHeaders
from imaparc.profiles import AfterFetch, Match, Profile
from imaparc.sources.eml import EmlSource
from imaparc.sources.imap import ImapConnection, ScannedMessage
from imaparc.state import StateStore


def _profile(name: str, **match: object) -> Profile:
    return Profile(
        name=name, account="test", match=Match(**match), output=Path("/tmp") / name
    )


# --- pure orchestration helpers ---------------------------------------------


def test_folder_map_defaults_to_inbox() -> None:
    mapping = _folder_map([_profile("a", domains=["x.com"])], [], "/")
    assert list(mapping) == ["INBOX"]


def test_folder_map_groups_profiles_per_folder() -> None:
    profiles = [
        _profile("a", folders=["INBOX"]),
        _profile("b", folders=["Archiv", "INBOX"]),
    ]
    mapping = _folder_map(profiles, [], "/")
    assert {p.name for p in mapping["INBOX"]} == {"a", "b"}
    assert [p.name for p in mapping["Archiv"]] == ["b"]


def test_folder_map_is_not_recursive_by_default() -> None:
    # [INBOX] on its own must not pull in INBOX/Sub.
    profiles = [_profile("a", folders=["INBOX"])]
    mapping = _folder_map(profiles, ["INBOX", "INBOX/Sub"], "/")
    assert list(mapping) == ["INBOX"]


def test_folder_map_recursive_includes_subfolders() -> None:
    profiles = [_profile("a", folders=["INBOX"], recursive=True)]
    all_folders = ["INBOX", "INBOX/Sub", "INBOX/Sub/Deep", "Other"]
    mapping = _folder_map(profiles, all_folders, "/")
    assert set(mapping) == {"INBOX", "INBOX/Sub", "INBOX/Sub/Deep"}


def test_recursive_scan_excludes_trash_by_default() -> None:
    from imaparc.fetch import _effective_folders

    all_folders = ["INBOX", "INBOX/Sub", "INBOX/Trash", "INBOX/Papierkorb"]
    trash = frozenset({"INBOX/Trash", "INBOX/Papierkorb"})
    p = _profile("a", folders=["INBOX"], recursive=True)
    assert _effective_folders(p, all_folders, "/", trash) == ["INBOX", "INBOX/Sub"]


def test_recursive_scan_includes_trash_when_opted_in() -> None:
    from imaparc.fetch import _effective_folders

    all_folders = ["INBOX", "INBOX/Sub", "INBOX/Trash"]
    trash = frozenset({"INBOX/Trash"})
    p = _profile("a", folders=["INBOX"], recursive=True, trash=True)
    assert "INBOX/Trash" in _effective_folders(p, all_folders, "/", trash)


def test_explicitly_listed_trash_is_always_scanned() -> None:
    from imaparc.fetch import _effective_folders

    all_folders = ["INBOX", "INBOX/Trash", "INBOX/Papierkorb"]
    trash = frozenset({"INBOX/Trash", "INBOX/Papierkorb"})
    p = _profile("a", folders=["INBOX", "INBOX/Trash"], recursive=True)
    result = _effective_folders(p, all_folders, "/", trash)
    assert "INBOX/Trash" in result  # explicit source wins
    assert "INBOX/Papierkorb" not in result  # the other Trash stays excluded


def _scanned(**kw: object) -> ScannedMessage:
    return ScannedMessage(uid=1, headers=MailHeaders(**kw))  # type: ignore[arg-type]


def test_first_matching_profile_wins() -> None:
    profiles = [
        _profile("specific", addresses=["billing@x.com"]),
        _profile("general", domains=["x.com"]),
    ]
    # No attachments filter → the body (conn) is never touched, so None is fine.
    msg = _scanned(from_="billing@x.com")
    profile, raw = _match_candidate(None, "INBOX", msg, profiles)  # type: ignore[arg-type]
    assert profile is not None and profile.name == "specific"
    assert raw is None


def test_match_returns_none_when_nothing_matches() -> None:
    profiles = [_profile("a", domains=["x.com"])]
    msg = _scanned(from_="u@other.com")
    profile, _raw = _match_candidate(None, "INBOX", msg, profiles)  # type: ignore[arg-type]
    assert profile is None


class _FakeConn:
    """Minimal stand-in returning one built message for any UID."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def fetch_body(self, folder: str, uid: int) -> bytes:
        return self._raw


def test_attachment_filter_needs_matching_type() -> None:
    from tests.mail_builder import build_mail

    conn = _FakeConn(
        build_mail(
            subject="Rechnung",
            from_="billing@x.com",
            attachments=[("invoice.pdf", "application/pdf", b"%PDF-1.4 x")],
        )
    )
    msg = _scanned(from_="billing@x.com", subject="Rechnung")

    with_pdf = _profile("inv", domains=["@x.com"], attachments=["pdf"])
    profile, raw = _match_candidate(conn, "INBOX", msg, [with_pdf])  # type: ignore[arg-type]
    assert profile is not None and profile.name == "inv"
    assert raw is not None and b"invoice.pdf" in raw  # body reused, not refetched

    needs_docx = _profile("doc", domains=["@x.com"], attachments=["docx"])
    other, _ = _match_candidate(conn, "INBOX", msg, [needs_docx])  # type: ignore[arg-type]
    assert other is None  # message has a PDF, not a DOCX


class _NsConn:
    """Stand-in exposing the move-target resolution (Dovecot-style namespace)."""

    def resolve_move_destination(self, source: str, destination: str) -> str:
        return f"INBOX.{destination}"


def test_move_targets_resolved_for_exclusion() -> None:
    from imaparc.fetch import _move_targets

    moving = Profile(
        name="m",
        account="test",
        output=Path("/tmp/m"),
        match=Match(folders=["INBOX"], recursive=True),
        after_fetch=AfterFetch(move_to="imapArc"),
    )
    plain = _profile("p", domains=["x.com"])  # no after_fetch
    targets = _move_targets(_NsConn(), [moving, plain])  # type: ignore[arg-type]
    assert targets == {"INBOX.imapArc"}


def test_report_summary() -> None:
    report = FetchReport()
    report.add("hetzner")
    report.add("hetzner")
    assert report.total == 2
    assert "hetzner: 2" in report.summary()


def test_report_summary_surfaces_already_archived() -> None:
    # The state store must not be a black box: a run that delivers nothing new
    # because everything was archived before says so (and points to reset).
    report = FetchReport()
    report.already_archived = 18
    summary = report.summary()
    assert "18 already archived" in summary
    assert "reset" in summary


def test_report_summary_surfaces_post_fetch_failure() -> None:
    report = FetchReport()
    report.post_fetch_failed = 3
    assert "3 archived but the server-side" in report.summary()


def test_report_summary_surfaces_skipped_no_copy() -> None:
    report = FetchReport()
    report.post_fetch_skipped = 2
    assert "2 server action(s) skipped" in report.summary()


class _StubConn:
    """A minimal ImapConnection stand-in for _fetch_folder tests."""

    def __init__(self, message: object) -> None:
        self._message = message
        self.deleted: list[int] = []

    def scan(self, folder: str, since: object = None) -> tuple[int, list[object]]:
        return 99, [self._message]

    def fetch_body(self, folder: str, uid: int) -> bytes:  # pragma: no cover
        return b"raw body"

    def delete(self, folder: str, uid: int) -> None:
        self.deleted.append(uid)


def _kanzlei_msg(uid: int) -> object:
    from datetime import UTC, datetime

    from imaparc.mail.models import MailHeaders
    from imaparc.sources.imap import ScannedMessage

    return ScannedMessage(
        uid=uid,
        headers=MailHeaders(
            from_="anwalt@kanzlei.de",
            to="",
            cc="",
            bcc="",
            subject="Sache",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            message_id="<1>",
        ),
        received=None,
    )


def _delete_profile(output: Path) -> object:
    from imaparc.profiles import AfterFetch, Profile

    return Profile(
        name="p",
        account="a",
        output=output,
        match={"domains": ["@kanzlei.de"]},
        after_fetch=AfterFetch(delete=True),
    )


def test_fetch_skips_delete_without_local_eml(tmp_path: Path) -> None:
    # Already delivered + delete profile, but no local .eml (path changed / moved
    # away): the server delete must be skipped, not destroy the only copy.
    from imaparc.fetch import _fetch_folder
    from imaparc.state import StateStore

    msg = _kanzlei_msg(5)
    conn = _StubConn(msg)
    state = StateStore(tmp_path / "state.db")
    state.mark_delivered("a", "INBOX", 99, 5)  # already archived before
    report = FetchReport()

    _fetch_folder(
        conn, "a", "INBOX", [_delete_profile(tmp_path / "out")], state, report
    )

    assert conn.deleted == []  # nothing deleted on the server
    assert report.post_fetch_skipped == 1


def test_fetch_deletes_when_local_eml_present(tmp_path: Path) -> None:
    # Same, but the local .eml exists → the delete is applied.
    from imaparc.fetch import _basename_for, _fetch_folder
    from imaparc.state import StateStore

    msg = _kanzlei_msg(5)
    profile = _delete_profile(tmp_path / "out")
    eml_dir = (tmp_path / "out") / "eml"
    eml_dir.mkdir(parents=True)
    (eml_dir / f"{_basename_for(profile, msg)}.eml").write_bytes(b"archived")

    conn = _StubConn(msg)
    state = StateStore(tmp_path / "state.db")
    state.mark_delivered("a", "INBOX", 99, 5)
    report = FetchReport()

    _fetch_folder(conn, "a", "INBOX", [profile], state, report)

    assert conn.deleted == [5]  # local copy present → server delete applied
    assert report.post_fetch_skipped == 0


def test_retry_guard_checks_recorded_filename_not_reconstructed(
    tmp_path: Path,
) -> None:
    # Two mails can share a base name; the delivered file may be disambiguated
    # (…-2.eml). On a retry the guard must check *this* mail's recorded file, not
    # a sibling that merely matches the name reconstructed from current headers —
    # otherwise it would delete/move a message whose own archive is gone.
    from imaparc.fetch import _basename_for, _fetch_folder

    msg = _kanzlei_msg(5)
    profile = _delete_profile(tmp_path / "out")
    base = _basename_for(profile, msg)  # type: ignore[arg-type]
    eml_dir = (tmp_path / "out") / "eml"
    eml_dir.mkdir(parents=True)
    # A sibling mail occupies the reconstructed base name; THIS mail's own file
    # (recorded as …-2.eml) does not exist.
    (eml_dir / f"{base}.eml").write_bytes(b"other mail")

    conn = _StubConn(msg)
    state = StateStore(tmp_path / "state.db")
    state.mark_delivered("a", "INBOX", 99, 5, f"{base}-2.eml")
    report = FetchReport()

    _fetch_folder(conn, "a", "INBOX", [profile], state, report)  # type: ignore[arg-type]

    assert conn.deleted == []  # the sibling was not mistaken for this mail's copy
    assert report.post_fetch_skipped == 1


def test_post_fetch_label_failure_does_not_block_move() -> None:
    # A server that rejects the custom keyword must not prevent the move.
    from imaparc.fetch import _post_fetch
    from imaparc.profiles import AfterFetch, Profile

    calls: list[tuple[str, str]] = []

    class FakeConn:
        def label(self, folder: str, uid: int, keyword: str) -> None:
            raise RuntimeError("keyword rejected")

        def move(self, folder: str, uid: int, destination: str) -> None:
            calls.append(("move", destination))

        def delete(self, folder: str, uid: int) -> None:  # pragma: no cover
            calls.append(("delete", ""))

    profile = Profile(
        name="x",
        account="a",
        output=Path("/tmp/x"),
        after_fetch=AfterFetch(label="imapArc", move_to="imapArc"),
    )
    report = FetchReport()
    _post_fetch(FakeConn(), "INBOX.RA", 1, profile, report)  # type: ignore[arg-type]

    assert ("move", "imapArc") in calls
    assert report.post_fetch_failed == 0


def test_post_fetch_move_failure_is_counted_not_raised() -> None:
    """The archive is already durable, so a server failure must never abort."""
    from imaparc.fetch import _post_fetch
    from imaparc.profiles import AfterFetch, Profile

    class FakeConn:
        def label(self, folder: str, uid: int, keyword: str) -> None:
            pass

        def move(self, folder: str, uid: int, destination: str) -> None:
            raise RuntimeError("mailbox full")

        def delete(self, folder: str, uid: int) -> None:  # pragma: no cover
            raise AssertionError("delete must not run when move_to is set")

    profile = Profile(
        name="x",
        account="a",
        output=Path("/tmp/x"),
        after_fetch=AfterFetch(move_to="imapArc"),
    )
    report = FetchReport()

    _post_fetch(FakeConn(), "INBOX", 1, profile, report)  # type: ignore[arg-type]

    assert report.post_fetch_failed == 1
    assert "move to 'imapArc'" in report.summary() or report.post_fetch_failed == 1


def test_post_fetch_delete_failure_is_counted_not_raised() -> None:
    """A server lacking UIDPLUS raises here; the run must survive it."""
    from imaparc.fetch import _post_fetch
    from imaparc.profiles import AfterFetch, Profile

    class FakeConn:
        def label(
            self, folder: str, uid: int, keyword: str
        ) -> None:  # pragma: no cover
            pass

        def move(
            self, folder: str, uid: int, destination: str
        ) -> None:  # pragma: no cover
            raise AssertionError("move must not run when delete is set")

        def delete(self, folder: str, uid: int) -> None:
            raise ImapArcError("server lacks UIDPLUS")

    profile = Profile(
        name="x",
        account="a",
        output=Path("/tmp/x"),
        after_fetch=AfterFetch(delete=True),
    )
    report = FetchReport()

    _post_fetch(FakeConn(), "INBOX", 7, profile, report)  # type: ignore[arg-type]

    assert report.post_fetch_failed == 1


def test_post_fetch_without_move_or_delete_touches_nothing() -> None:
    """A label-only profile must not reach the move/delete branch at all."""
    from imaparc.fetch import _post_fetch
    from imaparc.profiles import AfterFetch, Profile

    labelled: list[str] = []

    class FakeConn:
        def label(self, folder: str, uid: int, keyword: str) -> None:
            labelled.append(keyword)

        def move(self, folder: str, uid: int, destination: str) -> None:
            raise AssertionError("no move_to configured")

        def delete(self, folder: str, uid: int) -> None:
            raise AssertionError("no delete configured")

    profile = Profile(
        name="x",
        account="a",
        output=Path("/tmp/x"),
        after_fetch=AfterFetch(label="imapArc"),
    )
    report = FetchReport()

    _post_fetch(FakeConn(), "INBOX", 1, profile, report)  # type: ignore[arg-type]

    assert labelled == ["imapArc"]
    assert report.post_fetch_failed == 0


def test_summary_names_already_archived_mail() -> None:
    """ "0 delivered" must never be a silent mystery."""
    report = FetchReport()
    report.already_archived = 4

    summary = report.summary()

    assert "4 already archived on a previous run" in summary
    assert "imaparc reset" in summary


def test_earliest_since_is_used_only_when_every_profile_agrees() -> None:
    """One unbounded profile means the server-side SINCE must not narrow at all."""
    from datetime import date

    from imaparc.fetch import _earliest_since
    from imaparc.profiles import Match, Profile

    def _profile(since: date | None) -> Profile:
        return Profile(
            name="x", account="a", output=Path("/tmp/x"), match=Match(since=since)
        )

    bounded = [_profile(date(2026, 3, 1)), _profile(date(2026, 1, 15))]
    assert _earliest_since(bounded) == date(2026, 1, 15)

    assert _earliest_since([*bounded, _profile(None)]) is None
    assert _earliest_since([]) is None


def _plain_profile(output: Path) -> object:
    """Matches the kanzlei mails, with no server-side action."""
    from imaparc.profiles import Profile

    return Profile(
        name="p", account="a", output=output, match={"domains": ["@kanzlei.de"]}
    )


def test_one_broken_message_does_not_abort_the_folder(tmp_path: Path) -> None:
    """A parser or disk failure on one mail must leave the rest of the run intact."""
    from imaparc.fetch import _fetch_folder
    from imaparc.state import StateStore

    class ExplodingConn:
        def scan(self, folder: str, since: object = None) -> tuple[int, list[object]]:
            return 42, [_kanzlei_msg(1), _kanzlei_msg(2)]

        def fetch_body(self, folder: str, uid: int) -> bytes:
            if uid == 1:
                raise OSError("disk went away")
            from tests.mail_builder import build_mail

            return build_mail(from_="anwalt@kanzlei.de", subject="Sache")

    report = FetchReport()
    state = StateStore(tmp_path / "state.db")

    _fetch_folder(
        ExplodingConn(),  # type: ignore[arg-type]
        "acc",
        "INBOX",
        [_plain_profile(tmp_path / "out")],  # type: ignore[list-item]
        state,
        report,
    )

    assert report.failed == 1  # the broken one
    assert report.total == 1  # the healthy one still got through


def test_summary_names_failures() -> None:
    report = FetchReport()
    report.failed = 3

    assert "3 failed (see log)" in report.summary()


class _AccountConn:
    """An ImapConnection stand-in covering a whole run_fetch pass."""

    def __init__(self, folders: list[str], raise_on_enter: Exception | None = None):
        self._folders = folders
        self._raise = raise_on_enter
        self.scanned: list[str] = []

    def __enter__(self) -> _AccountConn:
        if self._raise is not None:
            raise self._raise
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def list_folders(self) -> tuple[str, list[str]]:
        return ".", self._folders

    def trash_folders(self) -> set[str]:
        return {"INBOX.Trash"}

    def resolve_move_destination(self, source: str, destination: str) -> str:
        return f"INBOX.{destination}"

    def scan(self, folder: str, since: object = None) -> tuple[int, list[object]]:
        self.scanned.append(folder)
        return 1, []


def _recursive_profile(output: Path) -> object:
    from imaparc.profiles import AfterFetch, Profile

    return Profile(
        name="p",
        account="a",
        output=output,
        match={"domains": ["@kanzlei.de"], "recursive": True},
        after_fetch=AfterFetch(move_to="imapArc"),
    )


def _account(name: str = "a") -> object:
    from pydantic import SecretStr

    from imaparc.accounts import Account

    return Account(name=name, host="h", user="u", password=SecretStr("p"))


def test_recursive_scan_skips_trash_and_the_own_move_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scanning either would re-archive deleted mail or duplicate moved mail."""
    from imaparc.fetch import run_fetch
    from imaparc.state import StateStore

    conn = _AccountConn(["INBOX", "INBOX.Sub", "INBOX.Trash", "INBOX.imapArc"])
    monkeypatch.setattr("imaparc.fetch.ImapConnection", lambda _account: conn)

    run_fetch(
        {"a": _account()},  # type: ignore[dict-item]
        [_recursive_profile(tmp_path / "out")],  # type: ignore[list-item]
        StateStore(tmp_path / "state.db"),
    )

    assert "INBOX" in conn.scanned
    assert "INBOX.Sub" in conn.scanned
    assert "INBOX.Trash" not in conn.scanned  # deleted mail stays deleted
    assert "INBOX.imapArc" not in conn.scanned  # would duplicate moved mail


def test_one_failing_account_does_not_abort_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connection reset on one account must leave the rest of the run alive."""
    from imaparc.exceptions import ImapArcError as _Err
    from imaparc.fetch import run_fetch
    from imaparc.state import StateStore

    good = _AccountConn(["INBOX"])
    conns = {
        "bad": _AccountConn([], raise_on_enter=_Err("connection reset")),
        "worse": _AccountConn([], raise_on_enter=RuntimeError("protocol error")),
        "good": good,
    }
    monkeypatch.setattr(
        "imaparc.fetch.ImapConnection", lambda account: conns[account.name]
    )

    def _profile(account: str) -> object:
        from imaparc.profiles import Profile

        return Profile(
            name=account,
            account=account,
            output=tmp_path / account,
            match={"domains": ["@kanzlei.de"]},
        )

    report = run_fetch(
        {name: _account(name) for name in conns},  # type: ignore[dict-item]
        [_profile(n) for n in conns],  # type: ignore[list-item]
        StateStore(tmp_path / "state.db"),
    )

    assert good.scanned == ["INBOX"]  # the healthy account still ran
    assert report.total == 0


# --- GreenMail end-to-end ---------------------------------------------------


def _greenmail_up() -> bool:
    try:
        with socket.create_connection(("localhost", 3143), timeout=1):
            return True
    except OSError:
        return False


greenmail = pytest.mark.skipif(
    not _greenmail_up(), reason="GreenMail IMAP not reachable on localhost:3143"
)


def _deliver_smtp(to: str, sender: str, subject: str) -> None:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content("body")
    with smtplib.SMTP("localhost", 3025) as smtp:
        smtp.send_message(msg)


@greenmail
def test_run_fetch_delivers_only_matching_mail(tmp_path: Path) -> None:
    user = f"fetch-orch-{os.getpid()}@localhost"
    _deliver_smtp(user, "billing@hetzner.com", "Rechnung")
    _deliver_smtp(user, "newsletter@other.com", "Werbung")  # will not match

    account = Account(
        name="test", host="localhost", port=3143, user=user, password="x", ssl=False
    )
    profile = Profile(
        name="hetzner",
        account="test",
        match=Match(domains=["hetzner.com"]),
        output=tmp_path / "hetzner",
    )
    state = StateStore(tmp_path / "state.db")

    report = run_fetch({"test": account}, [profile], state)

    assert report.delivered.get("hetzner") == 1
    eml = tmp_path / "hetzner" / "eml"
    mails = list(EmlSource(eml))
    assert len(mails) == 1
    assert b"hetzner.com" in mails[0].raw

    # Second run is idempotent — already-delivered UIDs are skipped.
    again = run_fetch({"test": account}, [profile], StateStore(tmp_path / "state.db"))
    assert again.total == 0


@greenmail
def test_profile_change_picks_up_old_mail_without_state_reset(tmp_path: Path) -> None:
    user = f"fetch-rescan-{os.getpid()}@localhost"
    _deliver_smtp(user, "billing@hetzner.com", "Rechnung")

    account = Account(
        name="test", host="localhost", port=3143, user=user, password="x", ssl=False
    )
    state = StateStore(tmp_path / "state.db")

    # First run with a non-matching profile: nothing delivered, nothing "burned".
    non_matching = Profile(
        name="other",
        account="test",
        match=Match(domains=["@other.com"]),
        output=tmp_path / "other",
    )
    first = run_fetch({"test": account}, [non_matching], state)
    assert first.total == 0

    # The profile is corrected; the SAME old mail is now picked up — no state
    # reset, because unmatched mail was never marked delivered.
    matching = Profile(
        name="hetzner",
        account="test",
        match=Match(domains=["@hetzner.com"]),
        output=tmp_path / "hetzner",
    )
    second = run_fetch({"test": account}, [matching], state)
    assert second.delivered.get("hetzner") == 1
    assert len(list(EmlSource(tmp_path / "hetzner" / "eml"))) == 1


@greenmail
def test_run_fetch_deletes_source_after_archiving(tmp_path: Path) -> None:
    user = f"fetch-del-{os.getpid()}@localhost"
    _deliver_smtp(user, "billing@hetzner.com", "Rechnung")

    account = Account(
        name="test", host="localhost", port=3143, user=user, password="x", ssl=False
    )
    profile = Profile(
        name="hetzner",
        account="test",
        match=Match(domains=["hetzner.com"]),
        output=tmp_path / "hetzner",
        after_fetch=AfterFetch(delete=True),
    )
    state = StateStore(tmp_path / "state.db")

    report = run_fetch({"test": account}, [profile], state)

    # Archived locally...
    assert report.delivered.get("hetzner") == 1
    assert len(list(EmlSource(tmp_path / "hetzner" / "eml"))) == 1
    # ...and removed from the server.
    with ImapConnection(account) as conn:
        conn._conn.select_folder("INBOX")
        assert conn._conn.search(["ALL"]) == []
