"""Project-specific exceptions."""

from __future__ import annotations


class ImapArcError(Exception):
    """Base class for all imaparc errors."""


class ToolNotFoundError(ImapArcError):
    """A required external tool (gs, qpdf, verapdf) is missing."""


class ParseError(ImapArcError):
    """Raw message bytes could not be parsed."""


class SourceError(ImapArcError):
    """A mail source (e.g. an ``eml/`` directory) could not be read."""


class BrowserLaunchError(ImapArcError):
    """Chromium could not be started, typically because its build is missing."""


class RenderError(ImapArcError):
    """The email body could not be rendered to PDF."""


class PdfaConversionError(ImapArcError):
    """Ghostscript failed to produce a PDF/A document."""


class LockError(ImapArcError):
    """The run lock cannot be taken safely (see ``storage.run_lock``)."""


class RemoteResourceError(RenderError):
    """A remote resource of a mail did not load, so the page is incomplete.

    Raised only when remote content is allowed: then every image the mail
    refers to belongs to the rendition, and one that is missing — a timeout, a
    dead host, a "404 Not Found" — means the PDF would silently lack content.
    ``failures`` lists ``(url, reason)`` for the error note.
    """

    def __init__(self, failures: list[tuple[str, str]]) -> None:
        self.failures = failures
        shown = "; ".join(f"{url} ({reason})" for url, reason in failures[:3])
        more = f" and {len(failures) - 3} more" if len(failures) > 3 else ""
        super().__init__(
            f"{len(failures)} remote resource(s) did not load: {shown}{more}"
        )
