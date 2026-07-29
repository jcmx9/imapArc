"""Merge PDFs and count pages via pikepdf (no external tools)."""

from __future__ import annotations

import io
import warnings

import pikepdf

from imaparc.exceptions import ImapArcError


class PdfMergeError(ImapArcError):
    """Two or more PDFs could not be merged."""


def count_pages(pdf: bytes) -> int:
    """Return the page count of a PDF given as bytes."""
    with pikepdf.open(io.BytesIO(pdf)) as doc:
        return len(doc.pages)


def merge_pdfs(parts: list[bytes]) -> bytes:
    """Concatenate PDFs (as bytes) into one, preserving order.

    Source documents are kept open until the merged output is saved, which
    pikepdf requires — pages reference their originating document.

    Raises:
        PdfMergeError: If there is nothing to merge or a part is unreadable.
    """
    if not parts:
        raise PdfMergeError("no PDF parts to merge")
    merged = pikepdf.Pdf.new()
    sources: list[pikepdf.Pdf] = []
    try:
        # Attachment PDFs may carry AcroForm fields; concatenating flattens them,
        # which pikepdf warns about. The originals are archived unchanged
        # alongside the combined PDF, so losing form interactivity here is
        # intended (PDF/A forbids it anyway) — not a fault worth flooding logs.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=pikepdf.PageCopyWarning)
            for index, part in enumerate(parts):
                try:
                    src = pikepdf.open(io.BytesIO(part))
                except pikepdf.PdfError as exc:
                    raise PdfMergeError(
                        f"part {index} is not a valid PDF: {exc}"
                    ) from exc
                sources.append(src)
                merged.pages.extend(src.pages)
            out = io.BytesIO()
            merged.save(out)
            return out.getvalue()
    finally:
        for src in sources:
            src.close()
        merged.close()
