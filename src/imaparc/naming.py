"""Build output names from a filename pattern.

A hardened port of the Thunderbird extension's ``filename.js`` + ``sanitize.js``.
Deliberate departures from the original:
  * placeholders and date tokens are replaced globally, not just once;
  * the base name is capped to a filesystem-safe byte length;
  * an unparseable date yields an empty string, never ``NaN``.

The middle segment of the name is the **profile** (the conversation), not a
contact — imapArc groups mail by profile, so the sender/recipient inversion of
the old design is gone.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from email.utils import getaddresses

# Characters illegal on common filesystems, replaced with underscore.
_ILLEGAL = re.compile(r'[/\\:*?"<>|]')
# A run of whitespace and/or underscores collapses to a single underscore, so the
# name has clean single ``_`` separators throughout.
_SEPARATORS = re.compile(r"[\s_]+")
_ANGLE_ADDRESS = re.compile(r"<([^>]+)>")

# Max bytes for the base name (headroom under the 255-byte limit for a subfolder
# of the same name plus the ``.pdf``/``.eml`` extension).
_MAX_BASENAME_BYTES = 200

_DATE_TOKENS = ("YYYY", "MM", "DD", "hh", "mm", "ss")

DEFAULT_PATTERN = "{date}_{profile}_{subject}"
DEFAULT_DATE_FORMAT = "YYYY-MM-DD_hh-mm-ss"


def sanitize(value: str) -> str:
    """Reduce a value to filesystem-safe characters with single ``_`` separators.

    Umlauts and other Unicode are preserved (NFC-normalised). Illegal filesystem
    characters become ``_``; runs of whitespace and/or underscores collapse to a
    single ``_``; leading and trailing ``_`` are trimmed.
    """
    normalised = unicodedata.normalize("NFC", value)
    cleaned = _ILLEGAL.sub("_", normalised)
    return _SEPARATORS.sub("_", cleaned).strip("_")


def extract_email_address(header_value: str) -> str:
    """Return the bare address from ``Name <addr>``, else the trimmed input.

    Kept as a utility for profile matching (domain/address rules) in the fetch
    path; only the first ``<...>`` pair is taken.
    """
    match = _ANGLE_ADDRESS.search(header_value)
    if match:
        return match.group(1).strip()
    return header_value.strip()


def extract_email_addresses(header_value: str) -> list[str]:
    """Return every address in an address header (``To``/``Cc`` may hold many)."""
    if not header_value:
        return []
    return [addr for _name, addr in getaddresses([header_value]) if addr]


def format_date(dt: datetime | None, pattern: str) -> str:
    """Format a datetime using YYYY/MM/DD/hh/mm/ss tokens.

    A ``None`` datetime yields an empty string — never ``NaN``. All occurrences
    of each token are replaced, unlike the JS original.
    """
    if dt is None:
        return ""
    replacements = {
        "YYYY": f"{dt.year:04d}",
        "MM": f"{dt.month:02d}",
        "DD": f"{dt.day:02d}",
        "hh": f"{dt.hour:02d}",
        "mm": f"{dt.minute:02d}",
        "ss": f"{dt.second:02d}",
    }
    result = pattern
    for token in _DATE_TOKENS:
        result = result.replace(token, replacements[token])
    return result


def _truncate_bytes(value: str, limit: int) -> str:
    """Truncate a string to at most ``limit`` UTF-8 bytes, on a char boundary."""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore").rstrip()


def build_base_name(
    timestamp: datetime | None,
    profile_name: str,
    subject: str,
    *,
    pattern: str = DEFAULT_PATTERN,
    date_format: str = DEFAULT_DATE_FORMAT,
) -> str:
    """Expand the filename pattern into a sanitized, length-capped base name.

    The returned name has no extension — it is the shared basename of the
    ``.eml`` file, the combined PDF and the PDF subfolder,
    which is what keeps them traceable to one another.

    Placeholders are ``{date}``, ``{profile}`` and ``{subject}``; unknown
    placeholders are left untouched, and every occurrence of each is replaced.
    The caller resolves ``timestamp`` (the ``Date`` header, or a fallback such
    as the IMAP ``INTERNALDATE``) before calling.
    """
    values = {
        "{date}": format_date(timestamp, date_format),
        "{profile}": profile_name,
        "{subject}": subject,
    }
    result = pattern
    for placeholder, value in values.items():
        result = result.replace(placeholder, value)

    sanitized = sanitize(result)
    capped = _truncate_bytes(sanitized, _MAX_BASENAME_BYTES)
    # Guard against an empty or dot-only name that would yield just ".pdf".
    return capped or "email"
