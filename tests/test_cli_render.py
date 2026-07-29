"""Tests for the `imaparc render` command (profile-driven)."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from imaparc.cli import app
from tests.mail_builder import build_mail

runner = CliRunner()


@pytest.fixture(autouse=True)
def _restore_perms(tmp_path: Path) -> Iterator[None]:
    yield
    for entry in sorted(tmp_path.rglob("*"), reverse=True):
        with contextlib.suppress(OSError):
            entry.chmod(0o700)


def _profiles_yaml(tmp_path: Path, output: Path, *, name: str, pdf: bool) -> Path:
    path = tmp_path / "profile.yaml"
    path.write_text(
        "profiles:\n"
        f"  - name: {name}\n"
        "    account: test\n"
        f"    output: {output}\n"
        f"    pdf: {'true' if pdf else 'false'}\n",
        encoding="utf-8",
    )
    return path


def _eml_under(output: Path, *mails: bytes) -> None:
    eml = output / "eml"
    eml.mkdir(parents=True)
    for i, raw in enumerate(mails):
        (eml / f"2026-03-23_00-00-{i:02d}_p_m{i}.eml").write_bytes(raw)


def test_render_missing_tools_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("imaparc.config.shutil.which", lambda name: None)
    output = tmp_path / "vault"
    _eml_under(output)
    profiles = _profiles_yaml(tmp_path, output, name="p", pdf=True)
    result = runner.invoke(app, ["render", "--profiles", str(profiles)])
    assert result.exit_code == 1
    assert "Error" in result.output


def test_render_nothing_when_no_profiles(tmp_path: Path) -> None:
    profiles = tmp_path / "profile.yaml"
    profiles.write_text("profiles: []\n", encoding="utf-8")
    result = runner.invoke(app, ["render", "--profiles", str(profiles)])
    assert result.exit_code == 0
    assert "Nothing to render" in result.output


def test_render_unknown_profile_exits_1(tmp_path: Path) -> None:
    output = tmp_path / "vault"
    profiles = _profiles_yaml(tmp_path, output, name="p", pdf=True)
    result = runner.invoke(
        app, ["render", "--profiles", str(profiles), "--profile", "nope"]
    )
    assert result.exit_code == 1
    assert "no profile named" in result.output


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
def test_render_command_end_to_end(tmp_path: Path) -> None:
    output = tmp_path / "vault"
    _eml_under(
        output,
        build_mail(subject="One", html="<p>first</p>", text="first"),
        build_mail(subject="Two", html="<p>second</p>", text="second"),
    )
    profiles = _profiles_yaml(tmp_path, output, name="acme", pdf=True)

    result = runner.invoke(app, ["render", "--profiles", str(profiles)])
    assert result.exit_code == 0, result.output
    # Each mail is a folder holding its <basename>.pdf (attachment-less here).
    folders = sorted(p for p in (output / "pdf").iterdir() if p.is_dir())
    assert len(folders) == 2
    assert all("_acme_" in p.name for p in folders)
    assert all((f / f"{f.name}.pdf").exists() for f in folders)

    # Second run is idempotent: nothing new written.
    again = runner.invoke(app, ["render", "--profiles", str(profiles)])
    assert again.exit_code == 0
    assert len(sorted(p for p in (output / "pdf").iterdir() if p.is_dir())) == 2
