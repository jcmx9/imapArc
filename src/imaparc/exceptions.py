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


class RenderError(ImapArcError):
    """The email body could not be rendered to PDF."""


class PdfaConversionError(ImapArcError):
    """Ghostscript failed to produce a PDF/A document."""
