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
    run_lock,
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


# --- never-overwrite against another process, and the run lock ---------------


def test_write_never_overwrites_a_name_taken_in_the_meantime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another process creates the target between the check and the write."""
    from imaparc import storage

    real = storage.disambiguate
    calls = 0

    def racing(path: Path) -> Path:
        nonlocal calls
        calls += 1
        target = real(path)
        if calls == 1:
            target.write_bytes(b"theirs")  # the other process wins the name
        return target

    monkeypatch.setattr(storage, "disambiguate", racing)

    written = write_readonly(tmp_path / "m.pdf", b"ours")

    assert (tmp_path / "m.pdf").read_bytes() == b"theirs"
    assert written == tmp_path / "m-2.pdf"
    assert written.read_bytes() == b"ours"
    assert not list(tmp_path.glob(".tmp-*"))


def test_a_second_run_waits_for_the_first() -> None:
    import threading

    events: list[str] = []
    first_holds = threading.Event()
    release_first = threading.Event()

    def first() -> None:
        with run_lock():
            first_holds.set()
            release_first.wait(timeout=5)
            events.append("first done")

    thread = threading.Thread(target=first)
    thread.start()
    assert first_holds.wait(timeout=5)

    def announce() -> None:
        events.append("second waits")
        release_first.set()

    with run_lock(announce):
        events.append("second runs")
    thread.join(timeout=5)

    assert events == ["second waits", "first done", "second runs"]


def test_the_lock_never_follows_a_planted_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In a shared /tmp another user could link the lock name to our file."""
    from imaparc.exceptions import LockError

    victim = tmp_path / "precious.txt"
    victim.write_text("keep me")
    locks = tmp_path / "locks"
    locks.mkdir(mode=0o700)
    (locks / "render.lock").symlink_to(victim)
    monkeypatch.setenv("IMAPARC_LOCK_DIR", str(locks))

    with pytest.raises(LockError), run_lock():
        pass

    assert victim.read_text() == "keep me"


def test_the_lock_refuses_a_directory_that_is_not_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from imaparc.exceptions import LockError

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "locks").symlink_to(elsewhere)
    monkeypatch.setenv("IMAPARC_LOCK_DIR", str(tmp_path / "locks"))

    with pytest.raises(LockError), run_lock():
        pass

    assert list(elsewhere.iterdir()) == []


def test_the_lock_directory_is_private_and_the_file_not_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locks = tmp_path / "locks"
    locks.mkdir(mode=0o755)  # as 26.11.9 created it
    (locks / "render.lock").write_text("content")
    monkeypatch.setenv("IMAPARC_LOCK_DIR", str(locks))

    with run_lock():
        pass

    assert locks.stat().st_mode & 0o777 == 0o700
    assert (locks / "render.lock").read_text() == "content"
