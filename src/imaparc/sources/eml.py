"""Render source: emails read from an ``eml/`` directory.

This is the source the ``render`` path consumes. It reads the raw bytes of each
``.eml`` file and yields them as :class:`RawMail` — so everything downstream
(parsing, rendering, PDF assembly) is blind to whether a mail came from disk here
or, on the fetch path, straight from IMAP.

Files are yielded in basename order. imapArc names them
``YYYY-MM-DD_hh-mm-ss_PROFILE_SUBJECT.eml``, so basename order is chronological.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from imaparc.exceptions import SourceError
from imaparc.sources.base import RawMail

_SUFFIXED = re.compile(r"^(.*)-(\d+)$")


def _chain_order(path: Path) -> tuple[str, int]:
    """Sort ``X.eml`` before ``X-2.eml`` before ``X-10.eml``.

    Plain name order puts ``X-2.eml`` first (``-`` sorts before ``.``), so of
    two mails sharing a base name the *second* delivered was rendered first and
    took the unsuffixed PDF name — every such pair ended up with swapped names.
    """
    match = _SUFFIXED.match(path.stem)
    if match:
        return match.group(1), int(match.group(2))
    return path.stem, 0


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

    def paths(self) -> list[Path]:
        """The ``.eml`` files in basename order, **without reading them**.

        A render run needs the count up front (for the progress bar) but not the
        contents — reading every mail eagerly made peak memory scale with the
        size of the archive rather than with concurrency.
        """
        return sorted(
            (
                entry
                for entry in self._path.iterdir()
                if entry.is_file() and entry.suffix == ".eml"
            ),
            key=_chain_order,
        )

    def __iter__(self) -> Iterator[RawMail]:
        for entry in self.paths():
            yield RawMail(raw=entry.read_bytes(), source_id=str(entry))
