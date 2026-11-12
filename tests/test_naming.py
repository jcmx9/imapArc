"""Tests for filename building — including the deliberate departures from the
Thunderbird extension's behaviour."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from imaparc.naming import (
    DEFAULT_DATE_FORMAT,
    build_base_name,
    extract_email_address,
    format_date,
    sanitize,
    to_local,
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


def test_build_base_name_drops_dots_and_commas_from_the_subject() -> None:
    name = build_base_name(
        datetime(2026, 11, 12, 8, 45, 4),
        "Acme",
        "Re: Termin am 17. November, 14 Uhr",
    )
    assert name == "2026-11-12_08-45-04_Acme_Re_Termin_am_17_November_14_Uhr"


def test_build_base_name_leaves_no_trailing_dot() -> None:
    name = build_base_name(datetime(2026, 1, 1), "p", "Receipt from Cline Bot Inc.")
    assert name.endswith("_Cline_Bot_Inc")


def test_build_base_name_keeps_dots_the_date_format_asks_for() -> None:
    # Only the mail-supplied parts are cleaned; a chosen date format is not.
    name = build_base_name(
        datetime(2026, 3, 23), "p", "Rechnung 1.5", date_format="DD.MM.YYYY"
    )
    assert name == "23.03.2026_p_Rechnung_1_5"


# --- sender-controlled characters and times --------------------------------


@pytest.mark.parametrize("char", ["\x00", "\x1b", "\x7f", "\x85"])
def test_control_characters_never_reach_a_file_name(char: str, tmp_path: Path) -> None:
    """A NUL in a subject made every file operation on that mail raise."""
    name = build_base_name(datetime(2026, 1, 2, 3, 4, 5), "P", f"a{char}b")

    assert char not in name
    assert name == "2026-01-02_03-04-05_P_a_b"
    (tmp_path / f"{name}.eml").write_bytes(b"x")  # must be a usable file name


def test_an_aware_time_is_named_in_local_time() -> None:
    """Exchange stamps UTC: 09:32Z on a summer day is 11:32 in Berlin."""
    stamped_utc = datetime(2026, 10, 19, 9, 32, 56, tzinfo=UTC)
    assert build_base_name(stamped_utc, "P", "x") == "2026-10-19_11-32-56_P_x"


def test_summer_and_winter_mail_each_keep_their_own_offset() -> None:
    """The offset valid *on the mail's date* applies, whenever it is processed."""
    summer = datetime(2026, 7, 1, 10, 0, 0, tzinfo=UTC)
    winter = datetime(2026, 12, 1, 10, 0, 0, tzinfo=UTC)
    assert build_base_name(summer, "P", "x").startswith("2026-07-01_12-00-00")
    assert build_base_name(winter, "P", "x").startswith("2026-12-01_11-00-00")


def test_the_same_instant_gets_the_same_name_whatever_its_offset() -> None:
    """What makes fetch (server's offset) and render (Date header) agree."""
    as_sent = datetime(2026, 10, 16, 11, 4, 20, tzinfo=timezone(timedelta(hours=2)))
    as_utc = as_sent.astimezone(UTC)
    assert build_base_name(as_sent, "P", "x") == build_base_name(as_utc, "P", "x")


def test_a_naive_time_is_taken_as_it_is() -> None:
    naive = datetime(2026, 10, 19, 9, 32, 56)
    assert to_local(naive) is naive
    assert to_local(None) is None
