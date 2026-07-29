"""Tests for `imaparc init` config bootstrapping."""

from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest

from imaparc.accounts import load_accounts
from imaparc.bootstrap import init_config, profile_block
from imaparc.profiles import load_profiles


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_init_creates_config_files(tmp_path: Path) -> None:
    cfg = tmp_path / "imaparc"
    result = init_config(cfg)
    assert (cfg / ".env").exists()
    assert (cfg / "profile.yaml").exists()
    assert set(result.created) == {cfg / ".env", cfg / "profile.yaml"}
    assert result.skipped == []


def test_init_sets_restrictive_permissions(tmp_path: Path) -> None:
    cfg = tmp_path / "imaparc"
    init_config(cfg)
    assert _mode(cfg) == 0o700
    assert _mode(cfg / ".env") == 0o600


def test_init_does_not_overwrite_without_force(tmp_path: Path) -> None:
    cfg = tmp_path / "imaparc"
    init_config(cfg)
    (cfg / ".env").write_text("IMAP_REAL_HOST=secret\n", encoding="utf-8")
    result = init_config(cfg)
    # The real .env survives; it is reported as skipped, not created.
    assert (cfg / ".env").read_text(encoding="utf-8") == "IMAP_REAL_HOST=secret\n"
    assert cfg / ".env" in result.skipped


def test_init_force_overwrites(tmp_path: Path) -> None:
    cfg = tmp_path / "imaparc"
    init_config(cfg)
    (cfg / ".env").write_text("stale\n", encoding="utf-8")
    result = init_config(cfg, force=True)
    assert "IMAP_PRIVAT_HOST" in (cfg / ".env").read_text(encoding="utf-8")
    assert cfg / ".env" in result.created


def test_generated_templates_are_valid(tmp_path: Path) -> None:
    # The shipped templates must load through the real config parsers.
    cfg = tmp_path / "imaparc"
    init_config(cfg)
    accounts = load_accounts(cfg / ".env")
    assert "privat" in accounts
    profiles = load_profiles(cfg / "profile.yaml", accounts)
    assert profiles[0].name == "rechnungen"


# Every option a profile accepts, so the generated block documents them all.
_ALL_OPTION_KEYS = [
    "name",
    "account",
    "match",
    "folders",
    "recursive",
    "domains",
    "addresses",
    "mode",
    "subject",
    "attachments",
    "since",
    "until",
    "output",
    "pdf",
    "remote_images",
    "jobs",
    "after_fetch",
    "label",
    "move_to",
    "delete",
]


@pytest.mark.parametrize("key", _ALL_OPTION_KEYS)
def test_profile_block_documents_every_option(key: str) -> None:
    # Each option appears in the block (active or commented) so nothing is hidden.
    assert f"{key}:" in profile_block("rechnungen")


def test_profile_block_required_active_optional_commented() -> None:
    block = profile_block("acme")
    lines = block.splitlines()

    def active(prefix: str) -> bool:
        return any(line.lstrip().startswith(prefix) for line in lines)

    # Required fields are live; representative optional fields are commented.
    assert active("- name: acme")
    assert active("account:")
    assert active("output:")
    assert active("pdf:")
    assert not active("remote_images:")  # only "# remote_images: …"
    assert not active("domains:")
    assert "# remote_images:" in block
    assert "# domains:" in block


def test_profile_block_quotes_names_needing_it() -> None:
    # A name with YAML-significant characters is quoted (YAML dumps single quotes)
    # so the block stays valid; a plain name stays bare.
    assert "name: 'a: b'" in profile_block("a: b")
    assert "name: plain-name_1" in profile_block("plain-name_1")


def _model_field_names() -> set[str]:
    from imaparc.profiles import AfterFetch, Match, Profile

    return (
        set(Match.model_fields)
        | set(Profile.model_fields)
        | set(AfterFetch.model_fields)
    )


def _yaml_key_present(text: str, field: str) -> bool:
    """True if ``field`` appears as a YAML key — active, commented, or a list item."""
    pattern = rf"^\s*#?\s*(- )?{re.escape(field)}:"
    return re.search(pattern, text, re.MULTILINE) is not None


def test_add_profile_output_mentions_every_model_field() -> None:
    # Strong drift guard: assert against the ACTUAL rendered add-profile block
    # (not just the internal lists), so both a forgotten list entry AND a renderer
    # bug that drops a field are caught. sync-profiles shares this renderer.
    text = profile_block("example", "~/imapArc/example")
    missing = sorted(f for f in _model_field_names() if not _yaml_key_present(text, f))
    assert missing == [], f"add-profile / sync-profiles output omits: {missing}"


def test_example_yaml_mentions_every_model_field() -> None:
    # The checked-in reference file must also document every option.
    example = Path(__file__).resolve().parent.parent / "profile.example.yaml"
    text = example.read_text(encoding="utf-8")
    missing = sorted(f for f in _model_field_names() if not _yaml_key_present(text, f))
    assert missing == [], f"profile.example.yaml omits: {missing}"
