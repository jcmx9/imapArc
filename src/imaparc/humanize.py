"""Small formatting helpers for human-facing strings."""

from __future__ import annotations

from datetime import datetime

from imaparc.naming import to_local


def format_date_human(dt: datetime | None) -> str:
    """Return a readable date string for the mail header block.

    ``23.03.2026, 02:18 (UTC+1)`` — local time, with the offset it is given in, so
    the line is unambiguous on its own. A naive datetime is shown as it is,
    without an offset. Empty string for a missing date. Deliberately separate
    from the filename date format.
    """
    if dt is None:
        return ""
    local = to_local(dt) or dt
    text = local.strftime("%d.%m.%Y, %H:%M")
    offset = local.utcoffset()
    if offset is None:
        return text
    minutes = int(offset.total_seconds()) // 60
    sign = "+" if minutes >= 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    zone = f"UTC{sign}{hours}" + (f":{rest:02d}" if rest else "")
    return f"{text} ({zone})"


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
