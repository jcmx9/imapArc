"""Take the original mail back out of a PDF.

Every PDF imapArc has written since 26.11.3 carries the ``.eml`` it was rendered
from (and, since 26.11.6, the attachments that could not become pages). This is
the way back: a PDF that was mailed on, copied out of the archive, or whose
``.eml`` was lost is enough to recover the mail.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import pikepdf

from imaparc.pdf import provenance
from imaparc.storage import disambiguate

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Extracted:
    """What came out of one PDF."""

    pdf: Path
    written: tuple[Path, ...] = ()
    verified: bool | None = None  # the .eml matches the hash the PDF records
    error: str | None = None


def extract(pdf: Path, target: Path, *, everything: bool = False) -> Extracted:
    """Write the ``.eml`` embedded in ``pdf`` into ``target``.

    Existing files are never overwritten (``…-2``). With ``everything`` the
    embedded attachments are written too.

    Args:
        pdf: A PDF written by imapArc.
        target: Directory to write into (created if missing).
        everything: Also extract the embedded attachments.

    Returns:
        The files written, and whether the ``.eml`` matches the hash the PDF
        records (``None`` if the PDF records none). ``error`` is set, and
        nothing written, when the PDF cannot be read or embeds no mail.
    """
    try:
        files = provenance.embedded_files(pdf)
    except (pikepdf.PdfError, pikepdf.PasswordError, OSError) as exc:
        return Extracted(pdf, error=f"cannot read: {exc}")
    sources = {n: data for n, (rel, data) in files.items() if rel == "Source"}
    if not sources:
        return Extracted(pdf, error="embeds no .eml (written before 26.11.3?)")

    wanted = files if everything else {n: files[n] for n in sources}
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, (_relation, data) in wanted.items():
        # The name comes out of the PDF: keep the file inside ``target``.
        path = disambiguate(target / (Path(name).name or "attachment"))
        path.write_bytes(data)
        written.append(path)

    recorded = provenance.read(pdf)
    digests = {hashlib.sha256(data).hexdigest() for data in sources.values()}
    verified = None if recorded is None else recorded.sha256 in digests
    return Extracted(pdf, tuple(written), verified)
