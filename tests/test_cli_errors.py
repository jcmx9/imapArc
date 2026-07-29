"""Error, abort and confirmation paths of the CLI.

The happy paths live in ``test_cli.py`` / ``test_cli_render.py``; this file
covers what happens when config is broken, a rewrite would destroy data, or the
user interrupts a run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from imaparc import cli
from imaparc.cli import app

runner = CliRunner()

_FULL_PROFILE = (
    "profiles:\n"
    "  - name: hetzner\n"
    "    account: privat\n"
    "    match:\n"
    "      domains: ['@hetzner.com']\n"
    "      mode: [from]\n"
    "      subject: '.*Rechnung.*'\n"
    "      attachments: [pdf]\n"
    "      folders: [INBOX, Archiv]\n"
    "      recursive: true\n"
    "      since: 2026-01-01\n"
    "      until: 2026-12-31\n"
    "    after_fetch:\n"
    "      label: archived\n"
    "      move_to: Archiv/Rechnungen\n"
    "    output: ~/imapArc/hetzner\n"
    "    pdf: true\n"
)

_MINIMAL_PROFILE = (
    "profiles:\n"
    "  - name: pixum\n"
    "    account: privat\n"
    "    match:\n"
    "      domains: ['@pixum.com']\n"
    "    output: ~/imapArc/pixum\n"
    "    pdf: true\n"
)


def _profiles(tmp_path: Path, content: str = _MINIMAL_PROFILE) -> Path:
    path = tmp_path / "profile.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def _env(tmp_path: Path) -> Path:
    path = tmp_path / "creds.env"
    path.write_text(
        "IMAP_PRIVAT_HOST=imap.example.com\n"
        "IMAP_PRIVAT_USER=you@example.com\n"
        "IMAP_PRIVAT_PASSWORD=secret\n",
        encoding="utf-8",
    )
    return path


# --- summaries shown by list-profiles -------------------------------------


def test_list_profiles_summarises_every_rule_kind(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["list-profiles", "--profiles", str(_profiles(tmp_path, _FULL_PROFILE))]
    )
    assert result.exit_code == 0
    # Rich wraps the table cells, so assert on fragments that survive wrapping.
    for fragment in ("from:", "subject", "attach", "since", "until", "label"):
        assert fragment in result.output


def test_list_profiles_reports_broken_config(tmp_path: Path) -> None:
    path = _profiles(tmp_path, "profiles:\n  - name: nope\n")  # no account/output
    result = runner.invoke(app, ["list-profiles", "--profiles", str(path)])
    assert result.exit_code == 1
    assert "Error" in result.output


def test_after_summary_renders_delete() -> None:
    from imaparc.profiles import AfterFetch

    assert cli._after_summary(AfterFetch(delete=True)) == "delete"
    assert cli._after_summary(AfterFetch()) == "—"
    assert cli._after_summary(None) == "—"


def test_match_summary_without_rules_is_all_mail() -> None:
    from imaparc.profiles import Match

    assert cli._match_summary(None) == "all mail"
    assert cli._match_summary(Match()) == "in INBOX"


# --- add-profile ----------------------------------------------------------


def test_add_profile_rejects_file_without_profiles_key(tmp_path: Path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text("something_else: true\n", encoding="utf-8")

    result = runner.invoke(app, ["add-profile", "new", "--profiles", str(path)])
    assert result.exit_code == 1
    assert "not an imapArc" in result.output


# --- sync-profiles --------------------------------------------------------


def test_sync_profiles_rejects_invalid_yaml(tmp_path: Path) -> None:
    path = _profiles(tmp_path, "profiles: [unclosed\n")
    result = runner.invoke(app, ["sync-profiles", "--profiles", str(path), "--yes"])
    assert result.exit_code == 1
    assert "invalid YAML" in result.output


def test_sync_profiles_rejects_empty_profile_list(tmp_path: Path) -> None:
    path = _profiles(tmp_path, "profiles: []\n")
    result = runner.invoke(app, ["sync-profiles", "--profiles", str(path), "--yes"])
    assert result.exit_code == 1
    assert "no profiles to sync" in result.output


def test_sync_profiles_aborts_without_confirmation(tmp_path: Path) -> None:
    path = _profiles(tmp_path)
    original = path.read_text(encoding="utf-8")

    result = runner.invoke(app, ["sync-profiles", "--profiles", str(path)], input="n\n")
    assert result.exit_code == 1
    assert path.read_text(encoding="utf-8") == original
    assert not (tmp_path / "profile.yaml.bak").exists()


def test_sync_profiles_numbers_further_backups(tmp_path: Path) -> None:
    path = _profiles(tmp_path)
    for _ in range(2):
        result = runner.invoke(app, ["sync-profiles", "--profiles", str(path), "--yes"])
        assert result.exit_code == 0

    assert (tmp_path / "profile.yaml.bak").exists()
    assert (tmp_path / "profile.yaml.bak.1").exists()


def test_sync_profiles_restores_original_when_rewrite_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rewrite that would not parse back must never reach the user's file."""
    path = _profiles(tmp_path)
    original = path.read_text(encoding="utf-8")
    # A profile without account/output parses as YAML but fails validation.
    monkeypatch.setattr(cli, "render_profiles_file", lambda raw: "profiles:\n  - {}\n")

    result = runner.invoke(app, ["sync-profiles", "--profiles", str(path), "--yes"])

    assert result.exit_code == 1
    assert "restored the original" in result.output
    assert path.read_text(encoding="utf-8") == original
    assert (tmp_path / "profile.yaml.bak").read_text(encoding="utf-8") == original


# --- reset ----------------------------------------------------------------


def test_reset_clears_state(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    result = runner.invoke(app, ["reset", "--state", str(db), "--yes"])
    assert result.exit_code == 0
    assert "cleared" in result.output


def test_reset_aborts_without_confirmation(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    result = runner.invoke(app, ["reset", "--state", str(db)], input="n\n")
    assert result.exit_code == 1
    assert not db.exists()


# --- profile selection and interruption -----------------------------------


def test_fetch_unknown_profile_exits_1(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "fetch",
            "--env",
            str(_env(tmp_path)),
            "--profiles",
            str(_profiles(tmp_path)),
            "--profile",
            "does-not-exist",
        ],
    )
    assert result.exit_code == 1
    assert "no profile named" in result.output


def test_fetch_reports_missing_env(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "fetch",
            "--env",
            str(tmp_path / "absent.env"),
            "--profiles",
            str(_profiles(tmp_path)),
        ],
    )
    assert result.exit_code == 1
    assert "Error" in result.output


def test_fetch_interrupted_exits_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C reports a clean abort instead of a traceback."""

    def _interrupt(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_fetch", _interrupt)

    result = runner.invoke(
        app,
        [
            "fetch",
            "--env",
            str(_env(tmp_path)),
            "--profiles",
            str(_profiles(tmp_path)),
            "--state",
            str(tmp_path / "state.db"),
        ],
    )
    assert result.exit_code == 130
    assert "Aborted" in result.output
    assert "re-run" in result.output


# --- misc helpers ---------------------------------------------------------


def test_received_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert cli._received(str(tmp_path / "gone.eml")) is None


def test_received_reads_mtime(tmp_path: Path) -> None:
    path = tmp_path / "mail.eml"
    path.write_bytes(b"x")
    received = cli._received(str(path))
    assert received is not None


def test_init_creates_then_keeps_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_config_dir", lambda: tmp_path / "config")

    first = runner.invoke(app, ["init"])
    assert first.exit_code == 0
    assert "Created" in first.output
    assert "Next: edit" in first.output

    second = runner.invoke(app, ["init"])
    assert second.exit_code == 0
    assert "Kept" in second.output
    assert "--force" in second.output
