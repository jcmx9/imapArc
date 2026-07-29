"""Tests for loading and matching conversation profiles."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from imaparc.accounts import Account, ConfigError
from imaparc.mail.models import MailHeaders
from imaparc.profiles import first_match, load_profiles, matches

_ACCOUNTS = {"privat": Account(name="privat", host="h", user="u", password="p")}


def _yaml(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "profile.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def _headers(**kw: object) -> MailHeaders:
    return MailHeaders(**kw)  # type: ignore[arg-type]


# --- loading ----------------------------------------------------------------


def test_loads_profile(tmp_path: Path) -> None:
    yaml_path = _yaml(
        tmp_path,
        """
profiles:
  - name: hetzner
    account: privat
    match:
      domains: [hetzner.com]
    output: ~/Archiv/Hetzner
    pdf: true
    after_fetch:
      label: Archiviert
      move_to: Archiv/Erledigt
""",
    )
    profiles = load_profiles(yaml_path, _ACCOUNTS)
    assert len(profiles) == 1
    p = profiles[0]
    assert p.name == "hetzner"
    assert p.pdf is True
    assert p.match.domains == ["hetzner.com"]
    assert p.after_fetch is not None and p.after_fetch.label == "Archiviert"
    assert str(p.output).startswith("/")  # ~ expanded


def test_render_options_load(tmp_path: Path) -> None:
    yaml_path = _yaml(
        tmp_path,
        """
profiles:
  - name: news
    account: privat
    output: ~/Archiv/News
    pdf: true
    remote_images: true
""",
    )
    p = load_profiles(yaml_path, _ACCOUNTS)[0]
    assert p.remote_images is True


def test_jobs_field(tmp_path: Path) -> None:
    y = _yaml(
        tmp_path,
        "profiles:\n  - name: x\n    account: privat\n"
        "    output: /tmp/x\n    jobs: 8\n",
    )
    assert load_profiles(y, _ACCOUNTS)[0].jobs == 8


def test_jobs_defaults_to_four(tmp_path: Path) -> None:
    y = _yaml(
        tmp_path, "profiles:\n  - name: y\n    account: privat\n    output: /tmp/y\n"
    )
    assert load_profiles(y, _ACCOUNTS)[0].jobs == 4


def test_render_options_default_false(tmp_path: Path) -> None:
    yaml_path = _yaml(
        tmp_path,
        "profiles:\n  - name: x\n    account: privat\n    output: /tmp/x\n",
    )
    p = load_profiles(yaml_path, _ACCOUNTS)[0]
    assert p.remote_images is False


def test_after_fetch_delete_loads(tmp_path: Path) -> None:
    yaml_path = _yaml(
        tmp_path,
        """
profiles:
  - name: hetzner
    account: privat
    output: ~/Archiv/Hetzner
    after_fetch:
      label: Archiviert
      delete: true
""",
    )
    profiles = load_profiles(yaml_path, _ACCOUNTS)
    assert profiles[0].after_fetch is not None
    assert profiles[0].after_fetch.delete is True


def test_after_fetch_move_and_delete_are_exclusive(tmp_path: Path) -> None:
    yaml_path = _yaml(
        tmp_path,
        """
profiles:
  - name: hetzner
    account: privat
    output: ~/Archiv/Hetzner
    after_fetch:
      move_to: Archiv/Erledigt
      delete: true
""",
    )
    with pytest.raises(ConfigError, match="mutually exclusive"):
        load_profiles(yaml_path, _ACCOUNTS)


def test_unknown_account_raises(tmp_path: Path) -> None:
    yaml_path = _yaml(
        tmp_path,
        "profiles:\n  - name: x\n    account: nope\n    output: /tmp/x\n",
    )
    with pytest.raises(ConfigError, match="unknown account"):
        load_profiles(yaml_path, _ACCOUNTS)


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    yaml_path = _yaml(tmp_path, "profiles: [: :\n")
    with pytest.raises(ConfigError):
        load_profiles(yaml_path, _ACCOUNTS)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_profiles(tmp_path / "nope.yaml", _ACCOUNTS)


# --- matching ---------------------------------------------------------------


def _profile(**match: object) -> object:
    from imaparc.profiles import Match, Profile

    return Profile(
        name="p", account="privat", match=Match(**match), output=Path("/tmp")
    )


def test_matches_domain() -> None:
    p = _profile(domains=["hetzner.com"])
    assert matches(p, _headers(from_="Billing <billing@hetzner.com>"))
    assert matches(p, _headers(from_="x@mail.hetzner.com"))  # subdomain
    assert not matches(p, _headers(from_="x@other.com"))


def test_matches_domain_with_at_prefix() -> None:
    # The recommended "@domain" form behaves like the bare form.
    p = _profile(domains=["@hetzner.com"])
    assert matches(p, _headers(from_="billing@hetzner.com"))
    assert matches(p, _headers(from_="x@mail.hetzner.com"))  # subdomain
    assert not matches(p, _headers(from_="x@nothetzner.com"))


def test_matches_address() -> None:
    p = _profile(addresses=["billing@hetzner.com"])
    assert matches(p, _headers(from_="billing@hetzner.com"))
    assert not matches(p, _headers(from_="other@hetzner.com"))


def test_matches_subject_regex() -> None:
    p = _profile(domains=["x.com"], subject=r"^Rechnung")
    assert matches(p, _headers(from_="a@x.com", subject="Rechnung 123"))
    assert not matches(p, _headers(from_="a@x.com", subject="Angebot"))


def test_matches_subject_wildcard_list() -> None:
    # A list of case-insensitive wildcard patterns; any one may match.
    p = _profile(domains=["x.com"], subject=["*Rechnung*", "Invoice*"])
    assert matches(p, _headers(from_="a@x.com", subject="Re: rechnung 5"))
    assert matches(p, _headers(from_="a@x.com", subject="Invoice 2026-03"))
    assert not matches(p, _headers(from_="a@x.com", subject="Angebot"))


def test_attachments_match_filter() -> None:
    from imaparc.profiles import attachments_match

    assert attachments_match([], [])  # no filter → always ok
    assert attachments_match(["pdf"], ["rechnung.pdf"])
    assert attachments_match([".pdf"], ["a.txt", "b.PDF"])  # dot + case ignored
    assert not attachments_match(["docx"], ["rechnung.pdf"])
    assert not attachments_match(["pdf"], [])


def test_matches_since() -> None:
    from datetime import date

    p = _profile(domains=["x.com"], since=date(2026, 1, 1))
    assert matches(p, _headers(from_="a@x.com", date=datetime(2026, 3, 1)))
    assert not matches(p, _headers(from_="a@x.com", date=datetime(2025, 12, 1)))


def test_matches_until() -> None:
    from datetime import date

    p = _profile(domains=["x.com"], until=date(2026, 6, 30))
    assert matches(p, _headers(from_="a@x.com", date=datetime(2026, 3, 1)))
    assert not matches(p, _headers(from_="a@x.com", date=datetime(2026, 7, 1)))


def test_matches_since_and_until_window() -> None:
    from datetime import date

    p = _profile(domains=["x.com"], since=date(2026, 1, 1), until=date(2026, 6, 30))
    assert matches(p, _headers(from_="a@x.com", date=datetime(2026, 3, 15)))
    assert not matches(p, _headers(from_="a@x.com", date=datetime(2025, 12, 31)))
    assert not matches(p, _headers(from_="a@x.com", date=datetime(2026, 7, 1)))


def test_matches_undated_falls_back_to_received() -> None:
    # A mail without a Date header is bounded by the IMAP INTERNALDATE instead.
    from datetime import date

    p = _profile(domains=["x.com"], since=date(2026, 1, 1), until=date(2026, 6, 30))
    inside = datetime(2026, 3, 15)
    after = datetime(2026, 7, 1)
    assert matches(p, _headers(from_="a@x.com", date=None), received=inside)
    assert not matches(p, _headers(from_="a@x.com", date=None), received=after)


def test_matches_undated_without_received_is_not_rejected_on_age() -> None:
    # Neither Date nor INTERNALDATE known → no date bound can be enforced.
    from datetime import date

    p = _profile(domains=["x.com"], since=date(2026, 1, 1), until=date(2026, 6, 30))
    assert matches(p, _headers(from_="a@x.com", date=None), received=None)


def test_matches_prefers_date_header_over_received() -> None:
    # The mail's own Date wins; a stale INTERNALDATE does not override it.
    from datetime import date

    p = _profile(domains=["x.com"], until=date(2026, 6, 30))
    within = datetime(2026, 3, 1)
    stale_received = datetime(2026, 12, 1)
    assert matches(p, _headers(from_="a@x.com", date=within), received=stale_received)


def test_mode_default_searches_all_recipient_fields() -> None:
    # Default mode = from/to/cc/bcc; a match on To counts even if From differs.
    p = _profile(domains=["@hetzner.com"])
    assert matches(p, _headers(from_="me@other.com", to="billing@hetzner.com"))
    assert matches(p, _headers(from_="me@other.com", cc="x@hetzner.com"))


def test_mode_restricts_to_named_fields() -> None:
    # mode: [from] must ignore a hit that is only in To.
    p = _profile(domains=["@hetzner.com"], mode=["from"])
    assert not matches(p, _headers(from_="me@other.com", to="billing@hetzner.com"))
    assert matches(p, _headers(from_="billing@hetzner.com"))


def test_mode_to_only_matches_recipient() -> None:
    p = _profile(addresses=["team@x.com"], mode=["to"])
    assert matches(p, _headers(from_="a@other.com", to="Team <team@x.com>, b@y.com"))
    assert not matches(p, _headers(from_="team@x.com", to="c@y.com"))


def test_first_match_wins(tmp_path: Path) -> None:
    yaml_path = _yaml(
        tmp_path,
        """
profiles:
  - name: specific
    account: privat
    match: {addresses: [billing@hetzner.com]}
    output: /tmp/a
  - name: general
    account: privat
    match: {domains: [hetzner.com]}
    output: /tmp/b
""",
    )
    profiles = load_profiles(yaml_path, _ACCOUNTS)
    hit = first_match(profiles, _headers(from_="billing@hetzner.com"))
    assert hit is not None and hit.name == "specific"
    none = first_match(profiles, _headers(from_="x@other.com"))
    assert none is None
