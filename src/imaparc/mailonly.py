"""Cut the mail itself out of a rendered PDF.

A rendered PDF holds the mail followed by its attachments' pages, and embeds
the ``.eml``. To pass on *just the mail* — without the attachments, and without
the original tucked inside — this copies the pages before the first attachment
bookmark into a new, slim PDF. Nothing is rendered again; the pages are taken
over as they are.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path

import pikepdf

from imaparc.profiles import archived_eml_dir
from imaparc.storage import disambiguate

logger = logging.getLogger(__name__)

# The bookmark render_mail() puts on each attachment's first page.
_ATTACHMENT_MARK = "Anhang "
SUFFIX = "_mailonly"


@dataclass(frozen=True)
class MailOnly:
    """The outcome for one PDF."""

    pdf: Path
    written: Path | None = None
    pages: int = 0
    of_pages: int = 0
    error: str | None = None


def mail_only(pdf: Path, target: Path, *, keep_embedded: bool = False) -> MailOnly:
    """Write ``<name>_mailonly.pdf`` — the mail without its attachments' pages.

    Args:
        pdf: A PDF written by imapArc 26.11.6 or later (it needs the bookmarks).
        target: Directory to write into (created if missing). Never the archive
            directory the PDF lives in: a second PDF recording the same mail
            there would count as a duplicate rendition.
        keep_embedded: Keep the embedded ``.eml`` and attachments. By default
            they are left out, which is the point of a copy to hand on.

    Returns:
        The file written and how many of the PDF's pages it holds; ``error`` is
        set, and nothing written, when the PDF cannot be used.
    """
    if _is_archive_dir(target, pdf):
        return MailOnly(pdf, error="refusing to write into the archive — use --output")
    try:
        with pikepdf.open(pdf) as doc:
            total = len(doc.pages)
            with doc.open_outline() as outline:
                marks = [(item.title, _page_of(doc, item)) for item in outline.root]
            if not marks:
                return MailOnly(pdf, error="has no bookmarks (written before 26.11.6?)")
            cut = next(
                (page for title, page in marks if title.startswith(_ATTACHMENT_MARK)),
                total,
            )
            del doc.pages[cut:]
            with doc.open_outline() as outline:
                outline.root.clear()
                outline.root.extend(
                    pikepdf.OutlineItem(title, page)
                    for title, page in marks
                    if page < cut
                )
            if not keep_embedded:
                if "/AF" in doc.Root:
                    del doc.Root["/AF"]
                names = doc.Root.get("/Names")
                if names is not None and "/EmbeddedFiles" in names:
                    del names["/EmbeddedFiles"]
            out = io.BytesIO()
            doc.save(
                out,
                compress_streams=False,
                object_stream_mode=pikepdf.ObjectStreamMode.preserve,
            )
    except (pikepdf.PdfError, pikepdf.PasswordError, OSError) as exc:
        return MailOnly(pdf, error=f"cannot read: {exc}")

    target.mkdir(parents=True, exist_ok=True)
    written = disambiguate(target / f"{pdf.stem}{SUFFIX}.pdf")
    written.write_bytes(out.getvalue())
    return MailOnly(pdf, written, pages=cut, of_pages=total)


def _page_of(doc: pikepdf.Pdf, item: pikepdf.OutlineItem) -> int:
    """The zero-based page a bookmark points at (the last page if unresolvable)."""
    destination = item.destination
    if isinstance(destination, int):
        return destination
    if not isinstance(destination, pikepdf.Array) or len(destination) == 0:
        return len(doc.pages)
    try:
        return doc.pages.index(pikepdf.Page(destination[0]))
    except (ValueError, TypeError, pikepdf.PdfError):
        return len(doc.pages)


def _is_archive_dir(target: Path, pdf: Path) -> bool:
    """Whether ``target`` is the profile directory ``pdf`` is archived in."""
    try:
        same = target.resolve() == pdf.parent.resolve()
    except OSError:
        return False
    return same and archived_eml_dir(pdf.parent.resolve()).is_dir()
