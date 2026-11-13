"""Tests for .eml delivery (readable basename, 0400, never overwrite)."""

from __future__ import annotations

import contextlib
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from imaparc.exceptions import RemoteResourceError
from imaparc.sources.deliver import (
    deliver_eml,
    error_note,
    failure_reason,
    file_away,
    file_error,
)
from imaparc.sources.eml import EmlSource
from tests.mail_builder import build_mail


@pytest.fixture(autouse=True)
def _restore_perms(tmp_path: Path) -> Iterator[None]:
    yield
    for entry in sorted(tmp_path.rglob("*"), reverse=True):
        with contextlib.suppress(OSError):
            entry.chmod(0o700)


def test_delivers_named_eml_file(tmp_path: Path) -> None:
    eml = tmp_path / "eml"
    raw = build_mail(subject="Rechnung")
    path = deliver_eml(eml, raw, "2026-03-23_00-00-00_p_Rechnung")
    assert path == eml / "2026-03-23_00-00-00_p_Rechnung.eml"
    assert path.read_bytes() == raw


def test_delivered_file_is_read_only(tmp_path: Path) -> None:
    path = deliver_eml(tmp_path / "eml", build_mail(), "base")
    assert stat.S_IMODE(path.stat().st_mode) == 0o400


def test_eml_dir_is_private_and_writable(tmp_path: Path) -> None:
    eml = tmp_path / "eml"
    deliver_eml(eml, build_mail(), "base")
    # 0700: private (no group/other) but the owner can still manage/delete.
    assert stat.S_IMODE(eml.stat().st_mode) == 0o700


def test_never_overwrites_disambiguates(tmp_path: Path) -> None:
    eml = tmp_path / "eml"
    first = deliver_eml(eml, b"AAA", "base")
    second = deliver_eml(eml, b"BBB", "base")  # same basename, different mail
    assert first == eml / "base.eml"
    assert second == eml / "base-2.eml"
    assert first.read_bytes() == b"AAA"
    assert second.read_bytes() == b"BBB"


def test_readable_back_via_eml_source(tmp_path: Path) -> None:
    eml = tmp_path / "eml"
    raw = build_mail(subject="Roundtrip")
    deliver_eml(eml, raw, "2026-03-23_00-00-00_p_Roundtrip")
    mails = list(EmlSource(eml))
    assert len(mails) == 1
    assert mails[0].raw == raw


def test_delivery_leaves_no_temp_files(tmp_path: Path) -> None:
    eml = tmp_path / "eml"
    deliver_eml(eml, build_mail(), "base")
    assert sorted(p.name for p in eml.iterdir()) == ["base.eml"]


# --- a byte-identical redelivery is not a new file --------------------------


def test_identical_content_reuses_the_existing_file(tmp_path: Path) -> None:
    """Never-overwrite guards against *loss*; identical bytes lose nothing.

    The same mail can legitimately arrive twice: Gmail lists one message in All
    Mail and in its label folder, and a mail uploaded back to the server returns
    under a fresh UID. Writing a `-2` copy of identical bytes is pure redundancy.
    """
    raw = b"From: a@b\r\nSubject: x\r\n\r\nbody"

    first = deliver_eml(tmp_path, raw, "mail")
    second = deliver_eml(tmp_path, raw, "mail")

    assert second == first
    assert sorted(p.name for p in tmp_path.iterdir()) == ["mail.eml"]


def test_different_content_still_disambiguates(tmp_path: Path) -> None:
    """The invariant that must not regress: distinct mail is never overwritten."""
    first = deliver_eml(tmp_path, b"one", "mail")
    second = deliver_eml(tmp_path, b"two", "mail")

    assert first != second
    assert first.read_bytes() == b"one"
    assert second.read_bytes() == b"two"


def test_identical_check_walks_the_disambiguated_chain(tmp_path: Path) -> None:
    """A copy may already sit at `-2`; that one counts as existing too."""
    deliver_eml(tmp_path, b"one", "mail")
    second = deliver_eml(tmp_path, b"two", "mail")  # → mail-2.eml

    again = deliver_eml(tmp_path, b"two", "mail")

    assert again == second
    assert len(list(tmp_path.iterdir())) == 2


# --- file_away ---------------------------------------------------------------


def test_file_away_moves_the_mail_into_every_target(tmp_path: Path) -> None:
    source = tmp_path / "in" / "x.eml"
    source.parent.mkdir()
    source.write_bytes(b"RAW")

    paths = file_away(source, b"RAW", [(tmp_path / "a", "m"), (tmp_path / "b", "n")])

    assert [p.read_bytes() for p in paths] == [b"RAW", b"RAW"]
    assert [p.name for p in paths] == ["m.eml", "n.eml"]
    assert not source.exists()


def test_file_away_reuses_an_identical_archived_copy(tmp_path: Path) -> None:
    deliver_eml(tmp_path / "arc", b"RAW", "m")
    source = tmp_path / "x.eml"
    source.write_bytes(b"RAW")

    [path] = file_away(source, b"RAW", [(tmp_path / "arc", "m")])

    assert path == tmp_path / "arc" / "m.eml"
    assert sorted(p.name for p in (tmp_path / "arc").iterdir()) == ["m.eml"]
    assert not source.exists()


def test_file_away_never_deletes_the_archived_copy_itself(tmp_path: Path) -> None:
    archived = deliver_eml(tmp_path / "arc", b"RAW", "m")

    file_away(archived, b"RAW", [(tmp_path / "arc", "m")])

    assert archived.read_bytes() == b"RAW"


def test_file_away_keeps_a_source_that_changed_meanwhile(tmp_path: Path) -> None:
    source = tmp_path / "x.eml"
    source.write_bytes(b"NEWER")

    file_away(source, b"RAW", [(tmp_path / "arc", "m")])

    assert source.read_bytes() == b"NEWER"
    assert (tmp_path / "arc" / "m.eml").read_bytes() == b"RAW"


def test_file_away_tolerates_a_source_already_gone(tmp_path: Path) -> None:
    [path] = file_away(tmp_path / "gone.eml", b"RAW", [(tmp_path / "arc", "m")])
    assert path.read_bytes() == b"RAW"


# --- identity is the content, not the name -----------------------------------


def test_identical_content_is_reused_under_any_name(tmp_path: Path) -> None:
    """File mode names a mail after its PDF; the archive may know it otherwise."""
    first = deliver_eml(tmp_path / "eml", b"RAW", "fetched-name")

    again = deliver_eml(tmp_path / "eml", b"RAW", "pdf-name")

    assert again == first
    assert [p.name for p in (tmp_path / "eml").iterdir()] == ["fetched-name.eml"]


def test_a_same_sized_different_mail_is_still_delivered(tmp_path: Path) -> None:
    deliver_eml(tmp_path / "eml", b"AAA", "a")

    other = deliver_eml(tmp_path / "eml", b"BBB", "b")

    assert other.read_bytes() == b"BBB"
    assert len(list((tmp_path / "eml").iterdir())) == 2


def test_a_newly_created_archive_parent_is_private(tmp_path: Path) -> None:
    deliver_eml(tmp_path / "eml-archive" / "p", b"RAW", "m")

    assert stat.S_IMODE((tmp_path / "eml-archive").stat().st_mode) == 0o700


# --- eml-error: the mail, and beside it why ----------------------------------


def test_file_error_moves_the_mail_and_writes_a_readable_note(tmp_path: Path) -> None:
    source = tmp_path / "in" / "x.eml"
    source.parent.mkdir()
    source.write_bytes(b"RAW")

    target = file_error(
        source, b"RAW", tmp_path / "eml-error" / "p", "x", "it broke", ["a — b"]
    )

    assert target == tmp_path / "eml-error" / "p" / "x.eml"
    assert target.read_bytes() == b"RAW"
    assert not source.exists()
    note = error_note(target).read_text()
    assert error_note(target).name == "x.error.txt"
    assert "Reason:  it broke" in note
    assert "  - a — b" in note
    assert f"Source:  {source}" in note


def test_a_mail_failing_again_in_eml_error_stays_and_gets_a_fresh_note(
    tmp_path: Path,
) -> None:
    error_dir = tmp_path / "eml-error" / "p"
    first = file_error(tmp_path / "gone.eml", b"RAW", error_dir, "x", "first")

    again = file_error(first, b"RAW", error_dir, "x", "second")

    assert again == first and first.read_bytes() == b"RAW"
    assert "Reason:  second" in error_note(first).read_text()
    assert sorted(p.name for p in error_dir.iterdir()) == ["x.eml", "x.error.txt"]


def test_archiving_a_once_failed_mail_removes_its_note(tmp_path: Path) -> None:
    error_dir = tmp_path / "eml-error" / "p"
    failed = file_error(tmp_path / "gone.eml", b"RAW", error_dir, "x", "first")

    file_away(failed, b"RAW", [(tmp_path / "eml-archive" / "p", "x")])

    assert list(error_dir.iterdir()) == []
    assert (tmp_path / "eml-archive" / "p" / "x.eml").read_bytes() == b"RAW"


def test_failure_reason_lists_the_resources_that_did_not_load() -> None:
    reason, details = failure_reason(
        RemoteResourceError([("http://a/x.png", "HTTP 404"), ("http://b/y", "timeout")])
    )
    assert "remote resources did not load" in reason
    assert details == ["http://a/x.png — HTTP 404", "http://b/y — timeout"]
    assert failure_reason(ValueError("boom")) == ("ValueError: boom", [])
