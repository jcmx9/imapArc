"""Convert one attachment to PDF pages, or report why it could not be.

This is where the extension's separator-without-content bug is structurally
excluded: the function never raises for an expected problem — it returns a
:class:`ConversionOutcome`, so the caller builds a separator from a success and
an info page from a failure, never a separator with nothing behind it.

Text rendering is injected as a callback so this stays testable without a
browser; the pipeline passes a callback that renders via Chromium.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from imaparc.attachments.classify import AttachmentKind, classify
from imaparc.attachments.image_to_pdf import ImageConversionError, image_to_pdf
from imaparc.attachments.models import ConversionOutcome, FailureReason
from imaparc.exceptions import RenderError
from imaparc.mail.models import AttachmentPart
from imaparc.pdf.merge import count_pages
from imaparc.pdf.normalize import PdfCorruptError, PdfEncryptedError, normalize_pdf

# Renders (filename, decoded text) to PDF bytes — supplied by the pipeline.
TextRenderer = Callable[[str, str], Awaitable[bytes]]


def _decode(data: bytes) -> str:
    """Decode text bytes leniently (UTF-8, then latin-1, then replace)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


async def convert_attachment(
    att: AttachmentPart,
    *,
    render_text: TextRenderer,
    qpdf: Path,
    work_dir: Path,
    max_bytes: int = 0,
    timeout_s: float = 0,
) -> ConversionOutcome:
    """Convert an attachment to PDF pages; never raises for expected problems.

    Args:
        att: The attachment.
        render_text: Async callback rendering text content to PDF bytes.
        qpdf: Resolved qpdf executable (for PDF attachments).
        work_dir: Writable scratch directory.
        max_bytes: Size cap; 0 disables it.
        timeout_s: Per-attachment conversion timeout in seconds; 0 disables it.
            Bounds a pathological input (e.g. a decompression bomb) so it cannot
            stall the whole run.

    Returns:
        A success outcome with pages, or a failure outcome with a reason. The
        original attachment survives regardless (invariant I2), handled by the
        caller.
    """
    if att.size == 0:
        return ConversionOutcome.failure(FailureReason.EMPTY)
    if max_bytes and att.size > max_bytes:
        return ConversionOutcome.failure(FailureReason.TOO_LARGE)

    kind = classify(att)

    async def _convert() -> bytes:
        if kind is AttachmentKind.IMAGE:
            return await asyncio.to_thread(image_to_pdf, att.content)
        if kind is AttachmentKind.PDF:
            return await asyncio.to_thread(
                normalize_pdf, att.content, qpdf=qpdf, work_dir=work_dir
            )
        # AttachmentKind.TEXT — OPAQUE is filtered before this runs.
        return await render_text(att.filename, _decode(att.content))

    if kind is AttachmentKind.OPAQUE:
        return ConversionOutcome.failure(FailureReason.UNSUPPORTED_TYPE)

    try:
        if timeout_s:
            pdf = await asyncio.wait_for(_convert(), timeout_s)
        else:
            pdf = await _convert()
    except TimeoutError:
        return ConversionOutcome.failure(FailureReason.TIMEOUT)
    except ImageConversionError:
        return ConversionOutcome.failure(FailureReason.TRANSCODE_FAILED)
    except PdfEncryptedError:
        return ConversionOutcome.failure(FailureReason.ENCRYPTED)
    except PdfCorruptError:
        return ConversionOutcome.failure(FailureReason.CORRUPT)
    except RenderError:
        return ConversionOutcome.failure(FailureReason.TRANSCODE_FAILED)

    return ConversionOutcome.success(pdf, count_pages(pdf))
