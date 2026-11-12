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

import fcntl
import os
import tempfile
from collections.abc import Callable, Generator
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

    Never-overwrite holds even against another process: the finished temp file
    is *linked* to its final name, which fails if that name was taken in the
    meantime — then the next free name is tried. Only where hard links are not
    supported does this fall back to a rename.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, FILE_MODE)
        while True:
            target = disambiguate(path)
            try:
                os.link(tmp_name, target)
            except FileExistsError:
                continue  # taken since disambiguate() looked: next name
            except OSError:
                os.replace(tmp_name, target)  # no hard links on this filesystem
                return target
            return target
    finally:
        Path(tmp_name).unlink(missing_ok=True)


@contextmanager
def run_lock(wait_message: Callable[[], None] | None = None) -> Generator[None]:
    """Hold the per-user lock that serialises rendering runs.

    Two runs writing into one archive at once — a scheduled ``all`` and a
    right-click ``eml`` — would each sweep the other's temp files and could
    render one mail twice. So a second run waits for the first; ``wait_message``
    is called once if it has to. The lock file lives in the temp directory
    (``IMAPARC_LOCK_DIR`` overrides it) and is released by the OS if the process
    dies.
    """
    directory = Path(
        os.environ.get("IMAPARC_LOCK_DIR")
        or Path(tempfile.gettempdir()) / f"imaparc-{os.getuid()}"
    )
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / "render.lock", "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if wait_message is not None:
                wait_message()
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
