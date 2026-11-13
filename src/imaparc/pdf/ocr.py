"""Give scanned attachment pages an invisible text layer (OCR).

A scanned letter or a photo of a document is a picture: nothing in it can be
searched, so the PDF holding it is not found by what it says. ``ocrmypdf``
(Tesseract) recognises the text and lays it invisibly over the page — the page
looks exactly as before, and now it can be searched and its text selected.

Only the *rendition* is touched. The attachment as it was sent stays inside the
``.eml`` that every PDF embeds, byte for byte.

**Which pages** is decided per page, by what the page holds (``PageContent``):

- an image and no text at all → recognised, whatever the image's size: a
  photo or a strip on a page is all there is by which it could be found;
- a big image and only a little text → recognised. That is a scan with a stamp,
  a page number or a typed footer, which must not make the scanned body
  unsearchable;
- a big image under a full text layer → left as it is. Someone recognised it
  already, possibly with better software than Tesseract; replacing that could
  only make the file harder to find;
- no big image → left as it is. A digitally written page is exact, and a logo
  in its letterhead is not worth "reading".

On the pages it does touch, ``--redo-ocr`` keeps visible text and replaces what
little invisible text there was. OCR is an addition, never a condition: without
the tools, or when recognition fails on a file, the pages are used as they are
and the caller reports it. The content of the PDF is complete either way — what
is missing is searchability.
"""

from __future__ import annotations

import functools
import io
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pikepdf

from imaparc.exceptions import ImapArcError

logger = logging.getLogger(__name__)

# Seconds allowed per page on top of a base, so a long scan is not cut off
# while a hanging process still is.
_BASE_TIMEOUT_S = 60.0
_PER_PAGE_TIMEOUT_S = 20.0


class OcrError(ImapArcError):
    """Text recognition could not be completed for a document."""


# A page counts as a scan when one image covers at least this share of it …
_SCAN_IMAGE_SHARE = 0.5
# … and as *already recognised* when it carries at least this many characters
# of text. Below that the text is a stamp, a page number, a footer — not the
# body. A sparse page with a full text layer may fall under it and be redone;
# that costs a second opinion on a few words, never content.
_RECOGNISED_CHARS = 120
# How deep form XObjects are followed when looking for images and text.
_FORM_DEPTH = 3

_TEXT_OPERATORS = frozenset({"Tj", "TJ", "'", '"'})


@dataclass(frozen=True)
class PageContent:
    """What one page holds, as far as the OCR decision needs to know."""

    image_share: float  # largest image's area relative to the page, 0..1
    text_chars: int  # characters shown by text operators, visible or not

    @property
    def wants_ocr(self) -> bool:
        """A picture of text that nothing on the page makes searchable yet.

        - an image and **no text at all** → recognise, whatever the image's
          size: a photo or a narrow strip placed on a page is all there is on
          it, so there is nothing else by which it could be found;
        - a big image and only a little text → recognise: a scan with a stamp,
          a page number or a typed footer;
        - a big image under a full text layer → leave: someone recognised it
          already, possibly with better software than ours;
        - text and at most a small image → leave: a digital page with a logo.
        """
        if self.image_share <= 0:
            return False
        if self.text_chars == 0:
            return True
        return (
            self.image_share >= _SCAN_IMAGE_SHARE
            and self.text_chars < _RECOGNISED_CHARS
        )


def analyse_page(page: pikepdf.Page) -> PageContent:
    """Measure the largest image and the amount of text on ``page``.

    Walks the content stream (and the form XObjects it draws) tracking the
    transformation matrix, so an image's size *on the page* is known — a logo
    and a full-page scan are both "an image", and only one of them is a scan.
    Anything that cannot be read counts as nothing found.
    """
    try:
        left, bottom, right, top = (float(v) for v in page.mediabox)
        area = abs((right - left) * (top - bottom)) or 1.0
        found = _walk(page.obj, _IDENTITY, _FORM_DEPTH)
    except (pikepdf.PdfError, ValueError, TypeError, KeyError, IndexError):
        return PageContent(0.0, 0)
    return PageContent(min(found[0] / area, 1.0), found[1])


_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_Matrix = tuple[float, float, float, float, float, float]


def _multiply(m: _Matrix, n: _Matrix) -> _Matrix:
    """``m`` applied first, then ``n`` (PDF's ``cm`` concatenation)."""
    return (
        m[0] * n[0] + m[1] * n[2],
        m[0] * n[1] + m[1] * n[3],
        m[2] * n[0] + m[3] * n[2],
        m[2] * n[1] + m[3] * n[3],
        m[4] * n[0] + m[5] * n[2] + n[4],
        m[4] * n[1] + m[5] * n[3] + n[5],
    )


def _walk(container: pikepdf.Object, ctm: _Matrix, depth: int) -> tuple[float, int]:
    """``(largest image area in points², text characters)`` below ``container``."""
    largest, chars = 0.0, 0
    resources = container.get("/Resources")
    xobjects = resources.get("/XObject") if resources is not None else None
    stack: list[_Matrix] = []
    for instruction in pikepdf.parse_content_stream(container):
        if not isinstance(instruction, pikepdf.ContentStreamInstruction):
            continue  # an inline image: small by nature, never a scanned page
        operands, name = instruction.operands, str(instruction.operator)
        if name == "q":
            stack.append(ctm)
        elif name == "Q":
            ctm = stack.pop() if stack else ctm
        elif name == "cm" and len(operands) == 6:
            ctm = _multiply(tuple(float(v) for v in operands), ctm)  # type: ignore[arg-type]
        elif name in _TEXT_OPERATORS:
            chars += sum(_text_length(operand) for operand in operands)
        elif name == "Do" and xobjects is not None and operands:
            target = xobjects.get(str(operands[0]))
            if target is None:
                continue
            subtype = str(target.get("/Subtype"))
            if subtype == "/Image":
                # An image fills the unit square; the matrix scales it.
                largest = max(largest, abs(ctm[0] * ctm[3] - ctm[1] * ctm[2]))
            elif subtype == "/Form" and depth > 0:
                matrix = target.get("/Matrix")
                inner = (
                    _multiply(tuple(float(v) for v in matrix), ctm)  # type: ignore[arg-type]
                    if matrix is not None
                    else ctm
                )
                area, more = _walk(target, inner, depth - 1)
                largest, chars = max(largest, area), chars + more
    return largest, chars


def _text_length(operand: object) -> int:
    if isinstance(operand, pikepdf.String):
        return len(bytes(operand))
    if isinstance(operand, pikepdf.Array):
        return sum(_text_length(item) for item in operand)
    return 0


def pages_wanting_ocr(pdf_bytes: bytes) -> list[int]:
    """The 1-based numbers of the pages to recognise (see ``PageContent``).

    Empty for a PDF that cannot be read — it is used as it is.
    """
    try:
        with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
            return [
                number
                for number, page in enumerate(pdf.pages, start=1)
                if analyse_page(page).wants_ocr
            ]
    except (pikepdf.PdfError, pikepdf.PasswordError):
        return []


@functools.lru_cache(maxsize=4)
def available_languages(tesseract: Path) -> frozenset[str]:
    """The languages this Tesseract has data for (empty if it cannot be asked)."""
    try:
        result = subprocess.run(
            [str(tesseract), "--list-langs"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    lines = (result.stdout or result.stderr).splitlines()
    return frozenset(line.strip() for line in lines[1:] if line.strip())


def usable_languages(wanted: str, tesseract: Path | None) -> str:
    """``wanted`` (``deu+eng``) reduced to the languages actually installed."""
    if tesseract is None:
        return ""
    installed = available_languages(tesseract)
    return "+".join(lang for lang in wanted.split("+") if lang in installed)


def add_text_layer(
    pdf_bytes: bytes,
    work_dir: Path,
    *,
    ocrmypdf: Path,
    languages: str,
    pages: list[int],
    name: str = "attachment",
) -> bytes:
    """Return ``pdf_bytes`` with a text layer on the given pages.

    On those pages raster images are recognised, visible text is kept as it is
    and earlier invisible text is replaced (``--redo-ocr``); every other page is
    passed through. Page count, page size and appearance are unchanged; images
    are not recompressed (``--optimize 0``).

    Args:
        pdf_bytes: The attachment's pages.
        work_dir: A writable scratch directory.
        ocrmypdf: The ``ocrmypdf`` executable.
        languages: Tesseract languages, ``+``-separated and installed.
        pages: 1-based numbers of the pages to recognise (not empty).
        name: Used for the scratch file names.

    Raises:
        OcrError: If recognition fails, times out or changes the page count.
    """
    src = work_dir / f"{name}-ocr-in.pdf"
    dst = work_dir / f"{name}-ocr-out.pdf"
    src.write_bytes(pdf_bytes)
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        page_count = len(pdf.pages)
    command = [
        str(ocrmypdf),
        "--redo-ocr",  # recognise images; keep visible text; replace old OCR
        "--pages",
        ",".join(str(number) for number in pages),
        "--output-type",
        "pdf",  # PDF/A conversion happens later, once, for the whole mail
        "--optimize",
        "0",  # never recompress what the sender sent
        "--jobs",
        "1",  # mails already run in parallel
        "--quiet",
        "-l",
        languages,
        str(src),
        str(dst),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=_BASE_TIMEOUT_S + _PER_PAGE_TIMEOUT_S * len(pages),
        )
    except subprocess.TimeoutExpired as exc:
        raise OcrError(f"text recognition timed out after {exc.timeout:.0f}s") from exc
    except OSError as exc:
        raise OcrError(f"could not start ocrmypdf: {exc}") from exc
    if result.returncode != 0 or not dst.is_file():
        reason = (result.stderr or result.stdout).strip().splitlines()
        raise OcrError(
            f"ocrmypdf exited with {result.returncode}: "
            + (reason[-1] if reason else "no output")
        )
    recognised = dst.read_bytes()
    with pikepdf.open(io.BytesIO(recognised)) as pdf:
        if len(pdf.pages) != page_count:
            raise OcrError("text recognition changed the number of pages")
    return recognised
