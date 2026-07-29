"""Filesystem helpers for the archive.

Files are written ``0400`` (read-only content — a second guard against
accidental in-place edits), and a name is **never overwritten**: an existing
target is disambiguated (``…-2``, invariant I1), and writes are atomic (temp
file, fsync, rename to a guaranteed-free path). Directories are ``0700`` (owner
rwx) so the owner can still manage the archive — delete or reorganise files
without first unlocking anything — while the never-overwrite guarantee stays
enforced in code, not by permissions.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

FILE_MODE = 0o400
# Owner rwx: the archive is private (no group/other access) yet the owner can
# delete/reorganise files. Never-overwrite is enforced by disambiguate(), not by
# removing directory write permission.
DIR_MODE = 0o700
_UNLOCKED_DIR_MODE = 0o700


def disambiguate(path: Path) -> Path:
    """Return a non-existing path, appending ``-2``, ``-3``, … to the stem.

    Deterministic for a given directory state, so a mail's attachments get the
    same names on every run (invariant I2).
    """
    if not path.exists():
        return path
    counter = 2
    while True:
        candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


@contextmanager
def writable_dir(path: Path) -> Generator[None]:
    """Ensure the directory is owner-writable for a write, then restore its mode.

    Archive directories are already ``0700``, so this is a no-op in the common
    case; it stays for robustness if a directory was tightened elsewhere. The
    owner may chmod regardless of the current bits. Concurrent runs against the
    same archive are not supported.
    """
    original = path.stat().st_mode & 0o777
    os.chmod(path, _UNLOCKED_DIR_MODE)
    try:
        yield
    finally:
        os.chmod(path, original)


def make_dir(path: Path, *, readonly: bool = False) -> None:
    """Create a directory (and parents); set it ``0700`` if ``readonly``.

    ``0700`` keeps the archive private (no group/other access) while leaving the
    owner free to manage it. ``readonly`` is a legacy flag name — the directory
    is writable by the owner either way.
    """
    path.mkdir(parents=True, exist_ok=True)
    if readonly:
        os.chmod(path, DIR_MODE)


def write_readonly(path: Path, data: bytes) -> Path:
    """Atomically write ``data`` to a disambiguated ``0400`` file.

    The parent directory must be writable — wrap the call in :func:`writable_dir`
    if it is already read-only. Returns the path actually written.
    """
    target = disambiguate(path)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    os.chmod(target, FILE_MODE)
    return target
