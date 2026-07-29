"""Small formatting helpers for human-facing strings."""

from __future__ import annotations

from datetime import datetime


def format_date_human(dt: datetime | None) -> str:
    """Return a readable date string for the mail header block.

    ``23.03.2026 02:18``, in the datetime's own timezone. Empty string for a
    missing date — separate from the filename date format.
    """
    if dt is None:
        return ""
    return dt.strftime("%d.%m.%Y %H:%M")


def format_file_size(num_bytes: int) -> str:
    """Return a compact, human-readable size string.

    Uses a decimal point (not a comma), matching the Thunderbird extension:
    ``512 B``, ``12.3 KB``, ``1.2 MB``, ``3.4 GB``.
    """
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    if num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    return f"{num_bytes / (1024 * 1024 * 1024):.1f} GB"
