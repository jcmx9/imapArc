"""Render source: emails read from an ``eml/`` directory.

This is the source the ``render`` path consumes. It reads the raw bytes of each
``.eml`` file and yields them as :class:`RawMail` — so everything downstream
(parsing, rendering, PDF assembly) is blind to whether a mail came from disk here
or, on the fetch path, straight from IMAP.

Files are yielded in basename order. imapArc names them
``YYYY-MM-DD_hh-mm-ss_PROFILE_SUBJECT.eml``, so basename order is chronological.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from imaparc.exceptions import SourceError
from imaparc.sources.base import RawMail


class EmlSource:
    """A :class:`~imaparc.sources.base.MailSource` backed by an ``eml/`` directory.

    Args:
        eml_dir: Directory holding the ``.eml`` files.

    Raises:
        SourceError: If the path is not a directory.
    """

    def __init__(self, eml_dir: Path) -> None:
        self._path = eml_dir
        if not eml_dir.is_dir():
            raise SourceError(f"Not an eml directory: {eml_dir}")

    def __iter__(self) -> Iterator[RawMail]:
        entries = sorted(
            entry
            for entry in self._path.iterdir()
            if entry.is_file() and entry.suffix == ".eml"
        )
        for entry in entries:
            yield RawMail(raw=entry.read_bytes(), source_id=str(entry))
