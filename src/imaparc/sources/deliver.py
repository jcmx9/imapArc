"""Deliver a message as a readable ``.eml`` file.

The archive is a plain directory of ``<basename>.eml`` files, where ``basename``
is the same ``YYYY-MM-DD_hh-mm-ss_PROFILE_SUBJECT`` used for the PDF — so a mail's
``.eml`` and its PDFs share one traceable name. Files are ``0400`` (read-only
content) and the directory ``0700`` (private, owner-manageable); delivery is
atomic (write to a temp file, fsync, rename) and never overwrites — a basename
collision is disambiguated (``…-2``).
"""

from __future__ import annotations

import os
from pathlib import Path

from imaparc.storage import DIR_MODE, make_dir, writable_dir, write_readonly


def _already_delivered(eml_dir: Path, raw: bytes, basename: str) -> Path | None:
    """An existing file under this base name holding exactly ``raw``.

    Walks the disambiguation chain (``mail.eml``, ``mail-2.eml``, …) because an
    earlier collision may already have pushed the identical copy along it.
    """
    candidate = eml_dir / f"{basename}.eml"
    counter = 1
    while candidate.exists():
        # Compare the cheap thing first: a size mismatch rules it out without
        # reading a possibly very large message off disk.
        try:
            if candidate.stat().st_size == len(raw) and candidate.read_bytes() == raw:
                return candidate
        except OSError:  # unreadable: treat as "not a match" and move along
            pass
        counter += 1
        candidate = eml_dir / f"{basename}-{counter}.eml"
    return None


def deliver_eml(eml_dir: Path, raw: bytes, basename: str) -> Path:
    """Write ``raw`` to ``eml_dir/<basename>.eml`` atomically, never overwriting.

    If a file under this base name already holds **byte-identical** content, that
    file is returned and nothing is written. Never-overwrite exists to prevent
    *loss*; identical bytes lose nothing, so a ``-2`` copy of them would be pure
    redundancy. One mail legitimately arrives twice — Gmail lists a message in
    All Mail *and* in its label folder, and a mail uploaded back to the server
    comes back under a fresh UID.

    Args:
        eml_dir: The profile's ``eml/`` directory (created if missing).
        raw: The complete RFC-822 message bytes.
        basename: The shared base name (no extension).

    Returns:
        The path of the delivered ``0400`` file — an existing identical one, or a
        newly written one (disambiguated when a *different* mail holds the name).
    """
    make_dir(eml_dir)
    existing = _already_delivered(eml_dir, raw, basename)
    if existing is not None:
        return existing
    with writable_dir(eml_dir):
        path = write_readonly(eml_dir / f"{basename}.eml", raw)
    os.chmod(eml_dir, DIR_MODE)  # immutable at rest
    return path
