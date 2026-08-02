"""Tests for .eml delivery (readable basename, 0400, never overwrite)."""

from __future__ import annotations

import contextlib
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from imaparc.sources.deliver import deliver_eml
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
