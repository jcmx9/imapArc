"""Smoke tests for the CLI surface."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from imaparc import __version__
from imaparc.cli import app

runner = CliRunner()


def test_version_flag_prints_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_flag_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output
    for command in (
        "all",
        "init",
        "fetch",
        "render",
        "add-profile",
        "sync-profiles",
        "list-profiles",
    ):
        assert command in result.output


def test_list_profiles_shows_names(tmp_path: Path) -> None:
    pf = tmp_path / "profile.yaml"
    pf.write_text(
        "profiles:\n"
        "  - name: pixum\n"
        "    account: privat\n"
        "    match:\n"
        "      domains: ['@pixum.com']\n"
        "    output: ~/imapArc/pixum\n"
        "    pdf: true\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["list-profiles", "--profiles", str(pf)])
    assert result.exit_code == 0
    assert "pixum" in result.output
    assert "@pixum.com" in result.output


def test_list_profiles_empty(tmp_path: Path) -> None:
    pf = tmp_path / "profile.yaml"
    pf.write_text("profiles: []\n", encoding="utf-8")
    result = runner.invoke(app, ["list-profiles", "--profiles", str(pf)])
    assert result.exit_code == 0
    assert "No profiles" in result.output


def test_add_profile_appends_block(tmp_path: Path) -> None:
    pf = tmp_path / "profile.yaml"
    pf.write_text(
        "profiles:\n  - name: first\n    account: privat\n    output: ~/a\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["add-profile", "second", "--profiles", str(pf)])
    assert result.exit_code == 0
    text = pf.read_text(encoding="utf-8")
    assert "name: second" in text
    # The first profile is untouched; both load through the real parser.
    from imaparc.profiles import load_profiles

    assert [p.name for p in load_profiles(pf)] == ["first", "second"]


def test_add_profile_rejects_duplicate(tmp_path: Path) -> None:
    pf = tmp_path / "profile.yaml"
    pf.write_text(
        "profiles:\n  - name: dup\n    account: privat\n    output: ~/a\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["add-profile", "dup", "--profiles", str(pf)])
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_add_profile_requires_existing_file(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["add-profile", "x", "--profiles", str(tmp_path / "nope.yaml")]
    )
    assert result.exit_code == 1
    assert "init" in result.output


def test_add_profile_custom_output(tmp_path: Path) -> None:
    pf = tmp_path / "profile.yaml"
    pf.write_text(
        "profiles:\n  - name: a\n    account: privat\n    output: ~/a\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app, ["add-profile", "b", "--profiles", str(pf), "--output", "/data/b"]
    )
    assert result.exit_code == 0
    assert "output: /data/b" in pf.read_text(encoding="utf-8")


def test_no_args_shows_help() -> None:
    # No subcommand now shows the help text (Click's no-command exit 2), not the
    # full run.
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    assert "Usage" in result.output
    assert "all" in result.output


def test_all_runs_full_and_fails_without_config(tmp_path: Path) -> None:
    # The `all` subcommand runs the full pipeline (fetch + render). With a missing
    # .env it must fail cleanly (exit 1), not print help.
    missing = tmp_path / "none"
    result = runner.invoke(
        app, ["all", "--env", f"{missing}.env", "--profiles", f"{missing}.yaml"]
    )
    assert result.exit_code == 1
    assert "Error" in result.output


def test_sync_profiles_rewrites_and_keeps_values(tmp_path: Path) -> None:
    pf = tmp_path / "profile.yaml"
    pf.write_text(
        "profiles:\n"
        "  - name: pixum\n"
        "    account: privat\n"
        "    match:\n"
        "      domains: ['@pixum.com']\n"
        "    output: ~/imapArc/pixum\n"
        "    pdf: true\n"
        "    remote_images: true\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["sync-profiles", "--profiles", str(pf), "--yes"])
    assert result.exit_code == 0
    text = pf.read_text(encoding="utf-8")
    # Set values stay active; unset options appear commented; a backup is made.
    assert "domains: ['@pixum.com']" in text
    assert "remote_images: true" in text
    assert (tmp_path / "profile.yaml.bak").exists()

    from imaparc.profiles import load_profiles

    profiles = load_profiles(pf)
    assert profiles[0].name == "pixum"
    assert profiles[0].remote_images is True
    assert profiles[0].match.domains == ["@pixum.com"]


def test_sync_profiles_requires_existing_file(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["sync-profiles", "--profiles", str(tmp_path / "nope.yaml"), "--yes"]
    )
    assert result.exit_code == 1
    assert "init" in result.output
