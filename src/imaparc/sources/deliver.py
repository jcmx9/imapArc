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


def deliver_eml(eml_dir: Path, raw: bytes, basename: str) -> Path:
    """Write ``raw`` to ``eml_dir/<basename>.eml`` atomically, never overwriting.

    Args:
        eml_dir: The profile's ``eml/`` directory (created if missing).
        raw: The complete RFC-822 message bytes.
        basename: The shared base name (no extension).

    Returns:
        The path of the delivered ``0400`` file (disambiguated on collision).
    """
    make_dir(eml_dir)
    with writable_dir(eml_dir):
        path = write_readonly(eml_dir / f"{basename}.eml", raw)
    os.chmod(eml_dir, DIR_MODE)  # immutable at rest
    return path
