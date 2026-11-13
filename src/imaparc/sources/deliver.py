"""Deliver a message as a readable ``.eml`` file.

The archive is a plain directory of ``<basename>.eml`` files, where ``basename``
is the same ``YYYY-MM-DD_hh-mm-ss_PROFILE_SUBJECT`` used for the PDF — so a mail's
``.eml`` and its PDF share one traceable name. Files are ``0400`` (read-only
content) and the directory ``0700`` (private, owner-manageable); delivery is
atomic (write to a temp file, fsync, link) and never overwrites — a basename
collision is disambiguated (``…-2``), identical content is reused.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from imaparc import __version__
from imaparc.exceptions import RemoteResourceError
from imaparc.profiles import ERROR_NOTE_SUFFIX
from imaparc.storage import DIR_MODE, make_dir, writable_dir, write_readonly


def _already_delivered(eml_dir: Path, raw: bytes) -> Path | None:
    """A file in ``eml_dir`` that already holds exactly ``raw``, under any name.

    Any name, because the name is no identity: an earlier collision may have
    pushed the identical copy along the ``-2`` chain, the naming scheme may have
    changed since, and file mode names a mail after its PDF. The cheap thing is
    compared first — a size mismatch rules a file out without reading it, so
    only true candidates are read off disk.
    """
    if not eml_dir.is_dir():
        return None
    for candidate in sorted(eml_dir.glob("*.eml")):
        try:
            if candidate.stat().st_size == len(raw) and candidate.read_bytes() == raw:
                return candidate
        except OSError:  # unreadable: treat as "not a match" and move along
            continue
    return None


def deliver_eml(eml_dir: Path, raw: bytes, basename: str) -> Path:
    """Write ``raw`` to ``eml_dir/<basename>.eml`` atomically, never overwriting.

    If a file in ``eml_dir`` already holds **byte-identical** content — under
    this base name or any other — that file is returned and nothing is written.
    Never-overwrite exists to prevent *loss*; identical bytes lose nothing, so
    a second copy of them would be pure redundancy. One mail legitimately
    arrives twice — Gmail lists a message in All Mail *and* in its label folder,
    and a mail uploaded back to the server comes back under a fresh UID.

    Args:
        eml_dir: The directory to deliver into — a profile's ``eml-input``,
            ``eml-archive`` or ``eml-error`` directory (created if missing).
        raw: The complete RFC-822 message bytes.
        basename: The shared base name (no extension).

    Returns:
        The path of the delivered ``0400`` file — an existing identical one, or a
        newly written one (disambiguated when a *different* mail holds the name).
    """
    if not eml_dir.parent.exists():
        # A directory imapArc creates for mail (eml-input, …) is private.
        make_dir(eml_dir.parent, readonly=True)
    make_dir(eml_dir)
    existing = _already_delivered(eml_dir, raw)
    if existing is not None:
        return existing
    with writable_dir(eml_dir):
        path = write_readonly(eml_dir / f"{basename}.eml", raw)
    os.chmod(eml_dir, DIR_MODE)  # immutable at rest
    return path


def file_away(source: Path, raw: bytes, targets: list[tuple[Path, str]]) -> list[Path]:
    """Move a rendered mail's ``.eml`` into one or more archive directories.

    ``raw`` is delivered to every ``(directory, basename)`` in ``targets`` first
    (atomically, never overwriting, an identical copy reused — see
    :func:`deliver_eml`); only once **all** copies exist is ``source`` removed.
    An interruption therefore leaves an extra copy, never a missing one, and a
    rerun completes the move.

    ``source`` is kept when one of the delivered paths *is* ``source`` — a mail
    rendered from where it is already archived would otherwise be deleted as its
    own duplicate.

    Returns:
        The archived paths, one per target.
    """
    delivered = [deliver_eml(target, raw, name) for target, name in targets]
    if not source.exists():
        return delivered
    origin = source.resolve()
    if any(path.resolve() == origin for path in delivered):
        return delivered
    if source.read_bytes() != raw:
        # Changed on disk since it was read: what was archived is not what is
        # there now, so removing it could lose the newer content.
        return delivered
    source.unlink()
    # A mail that failed before and is filed now needs its error note no more.
    error_note(source).unlink(missing_ok=True)
    return delivered


def error_note(eml: Path) -> Path:
    """The note beside ``eml`` that says why it could not be rendered."""
    return eml.with_name(f"{eml.stem}{ERROR_NOTE_SUFFIX}")


def file_error(
    source: Path,
    raw: bytes,
    error_dir: Path,
    basename: str,
    reason: str,
    details: list[str] | None = None,
) -> Path:
    """Move a mail that could not be rendered correctly into ``error_dir``.

    The ``.eml`` is delivered there exactly like into the archive (atomic,
    never overwriting, an identical copy reused) and the source removed only
    afterwards. Beside it ``<name>.error.txt`` records what went wrong, in
    plain text a person — or a script — can read: the directory says *that* a
    mail failed, the note says *why*. A mail that fails again from within the
    error directory stays where it is and gets a fresh note.

    Returns:
        The path of the ``.eml`` in ``error_dir``.
    """
    [target] = file_away(source, raw, [(error_dir, basename)])
    lines = [
        "imapArc could not render this mail correctly; no PDF was written.",
        "",
        f"File:    {target.name}",
        f"Source:  {source}",
        f"Time:    {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"Version: {__version__}",
        f"Reason:  {reason}",
    ]
    if details:
        lines += ["", "Details:", *(f"  - {detail}" for detail in details)]
    lines += [
        "",
        "To try again, pass this file or its directory to `imaparc eml`.",
    ]
    note = error_note(target)
    note.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def failure_reason(exc: BaseException) -> tuple[str, list[str]]:
    """``(reason, details)`` for an error note, from what stopped the render."""
    if isinstance(exc, RemoteResourceError):
        return (
            "remote resources did not load (the PDF would be incomplete)",
            [f"{url} — {why}" for url, why in exc.failures],
        )
    return f"{type(exc).__name__}: {exc}", []
