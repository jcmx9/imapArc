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
