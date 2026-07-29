"""Outcome model for converting one attachment into PDF pages.

The conversion never raises for an expected problem — it returns a
:class:`ConversionOutcome`. That is what structurally rules out the Thunderbird
extension's separator-without-content bug: the caller builds a separator only
from a successful outcome, and an info page from a failed one.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class FailureReason(enum.Enum):
    """Why an attachment could not be turned into pages.

    The value is the German text shown on the info page in the combined PDF.
    """

    UNSUPPORTED_TYPE = "Dieser Dateityp wird nicht in die PDF übernommen."
    ENCRYPTED = "Die Datei ist verschlüsselt und konnte nicht geöffnet werden."
    CORRUPT = "Die Datei ist beschädigt und konnte nicht gelesen werden."
    TOO_LARGE = "Die Datei ist zu groß und wurde nicht in die PDF übernommen."
    TRANSCODE_FAILED = "Die Datei konnte nicht in eine PDF-Seite umgewandelt werden."
    EMPTY = "Die Datei ist leer."
    TIMEOUT = "Die Umwandlung hat zu lange gedauert und wurde abgebrochen."

    @property
    def message(self) -> str:
        """The human-facing German explanation for the info page."""
        return self.value


@dataclass(frozen=True, slots=True)
class ConversionOutcome:
    """The result of converting one attachment to PDF pages.

    On success, ``pdf_bytes`` holds the rendered pages and ``page_count`` their
    number. On failure, ``reason`` explains why and there are no pages — the
    attachment still survives untouched in the subfolder (invariant I2).
    """

    ok: bool
    pdf_bytes: bytes | None = None
    page_count: int = 0
    reason: FailureReason | None = None

    @classmethod
    def success(cls, pdf_bytes: bytes, page_count: int) -> ConversionOutcome:
        return cls(ok=True, pdf_bytes=pdf_bytes, page_count=page_count)

    @classmethod
    def failure(cls, reason: FailureReason) -> ConversionOutcome:
        return cls(ok=False, reason=reason)
