"""Tests for the human-facing date and size strings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from imaparc.humanize import format_date_human


def test_header_date_is_local_time_with_its_offset() -> None:
    """09:32 UTC on a summer day reads 11:32 in Berlin — and says which zone."""
    stamped_utc = datetime(2026, 10, 19, 9, 32, tzinfo=UTC)
    assert format_date_human(stamped_utc) == "19.10.2026, 11:32 (UTC+2)"


def test_header_date_in_winter() -> None:
    sent = datetime(2026, 12, 1, 18, 5, tzinfo=timezone(timedelta(hours=-5)))
    assert format_date_human(sent) == "02.12.2026, 00:05 (UTC+1)"


def test_header_date_without_a_zone_claims_none() -> None:
    assert format_date_human(datetime(2026, 5, 18, 16, 6)) == "18.05.2026, 16:06"
    assert format_date_human(None) == ""
