"""Tests for the immutable-archive filesystem helpers."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from imaparc.storage import (
    DIR_MODE,
    FILE_MODE,
    disambiguate,
    make_dir,
    writable_dir,
    write_readonly,
)


@pytest.fixture(autouse=True)
def _restore_perms(tmp_path: Path) -> Iterator[None]:
    # 0500/0400 entries would block pytest's tmp cleanup; reopen them afterwards.
    yield
    for entry in sorted(tmp_path.rglob("*"), reverse=True):
        with contextlib.suppress(OSError):
            entry.chmod(0o700)


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_disambiguate_returns_path_when_free(tmp_path: Path) -> None:
    assert disambiguate(tmp_path / "a.pdf") == tmp_path / "a.pdf"


def test_disambiguate_appends_counter(tmp_path: Path) -> None:
    (tmp_path / "a.pdf").write_bytes(b"x")
    assert disambiguate(tmp_path / "a.pdf") == tmp_path / "a-2.pdf"
    (tmp_path / "a-2.pdf").write_bytes(b"x")
    assert disambiguate(tmp_path / "a.pdf") == tmp_path / "a-3.pdf"


def test_write_readonly_content_and_mode(tmp_path: Path) -> None:
    written = write_readonly(tmp_path / "f.bin", b"hello")
    assert written.read_bytes() == b"hello"
    assert _mode(written) == FILE_MODE


def test_write_readonly_never_overwrites(tmp_path: Path) -> None:
    first = write_readonly(tmp_path / "f.bin", b"one")
    second = write_readonly(tmp_path / "f.bin", b"two")
    assert first != second
    assert first.read_bytes() == b"one"
    assert second.read_bytes() == b"two"


def test_write_readonly_leaves_no_temp_files(tmp_path: Path) -> None:
    write_readonly(tmp_path / "f.bin", b"x")
    assert [p.name for p in tmp_path.iterdir()] == ["f.bin"]


def test_make_dir_readonly_is_private(tmp_path: Path) -> None:
    d = tmp_path / "sub"
    make_dir(d, readonly=True)
    assert _mode(d) == DIR_MODE  # 0700: private, owner-manageable
    assert os.access(d, os.W_OK)  # owner can still add/delete entries


def test_writable_dir_unlocks_then_restores(tmp_path: Path) -> None:
    d = tmp_path / "sub"
    make_dir(d, readonly=True)
    assert _mode(d) == DIR_MODE
    with writable_dir(d):
        assert os.access(d, os.W_OK)
        write_readonly(d / "inside.bin", b"x")
    assert _mode(d) == DIR_MODE  # restored


def test_write_into_readonly_dir_via_writable_dir(tmp_path: Path) -> None:
    d = tmp_path / "sub"
    make_dir(d, readonly=True)
    with writable_dir(d):
        written = write_readonly(d / "note.txt", b"content")
    assert written.read_bytes() == b"content"
    assert _mode(written) == FILE_MODE
