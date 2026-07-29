"""The convergence point between the render and fetch modes.

Both produce :class:`RawMail` — raw RFC-822 bytes plus an opaque provenance id.
Everything downstream (parsing, rendering, PDF/A assembly) depends only on the
bytes, never on where they came from. That is what guarantees an eml-directory read
(render) and an IMAP fetch yield an identical PDF.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RawMail:
    """One email as raw bytes, with a human-readable provenance id.

    Attributes:
        raw: The complete RFC-822 message bytes.
        source_id: Where it came from — an .eml file path for the render
            mode, an ``account/folder/uid`` locator for the fetch mode. Used for
            logging and error messages only; the pipeline never parses it.
    """

    raw: bytes
    source_id: str


class MailSource(Protocol):
    """Anything that can yield raw emails.

    ``EmlSource`` (render) and ``ImapSource`` (fetch) both implement this.
    The pipeline consumes a ``MailSource`` and is blind to the concrete type.
    """

    def __iter__(self) -> Iterator[RawMail]:
        """Yield raw emails one at a time."""
        ...
