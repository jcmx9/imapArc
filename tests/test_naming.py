"""Tests for filename building — including the deliberate departures from the
Thunderbird extension's behaviour."""

from __future__ import annotations

from datetime import datetime

import pytest

from imaparc.naming import (
    DEFAULT_DATE_FORMAT,
    build_base_name,
    extract_email_address,
    format_date,
    sanitize,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a/b:c*d?", "a_b_c_d"),  # illegal → _, trailing _ trimmed
        ('quote"less', "quote_less"),
        ("  spaced   out  ", "spaced_out"),  # whitespace runs → single _
        ("a __ b", "a_b"),  # mixed space/underscore run → single _
        ("Grüße", "Grüße"),  # umlauts preserved
        ("a\\b<c>d|e", "a_b_c_d_e"),
    ],
)
def test_sanitize(value: str, expected: str) -> None:
    assert sanitize(value) == expected


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Max Mustermann <max@example.com>", "max@example.com"),
        ("plain@example.com", "plain@example.com"),
        ("  spaced@example.com  ", "spaced@example.com"),
        ("a <a@x.com>, b <b@x.com>", "a@x.com"),  # first wins
    ],
)
def test_extract_email_address(header: str, expected: str) -> None:
    assert extract_email_address(header) == expected


def test_format_date_basic() -> None:
    dt = datetime(2026, 3, 23, 2, 18, 4)
    assert format_date(dt, "YYYY-MM-DD") == "2026-03-23"
    assert format_date(dt, DEFAULT_DATE_FORMAT) == "2026-03-23_02-18-04"


def test_format_date_none_is_empty_not_nan() -> None:
    assert format_date(None, "YYYY-MM-DD") == ""


def test_format_date_replaces_all_occurrences() -> None:
    # The JS original only replaced the first occurrence; we replace all.
    dt = datetime(2026, 3, 23)
    assert format_date(dt, "YYYY_YYYY") == "2026_2026"


def test_build_base_name_default_pattern() -> None:
    name = build_base_name(datetime(2026, 3, 23, 2, 18, 4), "hetzner", "Rechnung 12345")
    assert name == "2026-03-23_02-18-04_hetzner_Rechnung_12345"


def test_build_base_name_profile_is_the_middle_segment() -> None:
    name = build_base_name(datetime(2026, 1, 1), "steuer", "Bescheid")
    assert name == "2026-01-01_00-00-00_steuer_Bescheid"


def test_build_base_name_replaces_all_placeholders() -> None:
    name = build_base_name(
        datetime(2026, 3, 23), "p", "x", pattern="{date}_{date}", date_format="YYYY"
    )
    assert name == "2026_2026"


def test_build_base_name_is_length_capped() -> None:
    name = build_base_name(datetime(2026, 3, 23), "p", "A" * 500)
    assert len(name.encode("utf-8")) <= 200


def test_build_base_name_empty_falls_back() -> None:
    name = build_base_name(None, "", "", pattern="{subject}")
    assert name == "email"


def test_build_base_name_sanitizes_colon_for_maildir() -> None:
    # A subject with ':' must not survive — it is the Maildir info separator.
    name = build_base_name(datetime(2026, 3, 23), "p", "Re: in/voice")
    assert ":" not in name
    assert "/" not in name
