"""Tests for the `imaparc eml` command (profile-free, local .eml files)."""

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


def test_empty_directory_reports_nothing_to_do(tmp_path: Path) -> None:
    result = runner.invoke(app, ["eml", str(tmp_path)])

    assert result.exit_code == 0
    assert "no .eml" in result.output.lower()


def test_missing_path_exits_with_an_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["eml", str(tmp_path / "nope.eml")])

    assert result.exit_code == 1
    assert "not found" in result.output.lower()
    assert "Traceback" not in result.output


def test_non_eml_argument_exits_with_an_error(tmp_path: Path) -> None:
    scan = tmp_path / "scan.pdf"
    scan.write_bytes(b"%PDF-1.4")

    result = runner.invoke(app, ["eml", str(scan)])

    assert result.exit_code == 1
    assert "not an .eml" in result.output.lower()
    assert "Traceback" not in result.output


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
def test_renders_and_moves_the_eml(tmp_path: Path) -> None:
    mail = tmp_path / "Rechnung.eml"
    mail.write_bytes(build_mail(subject="Rechnung Juli"))

    result = runner.invoke(app, ["eml", str(mail)])

    assert result.exit_code == 0
    folders = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(folders) == 1
    folder = folders[0]
    assert (folder / f"{folder.name}.pdf").is_file()
    assert (folder / f"{folder.name}.eml").is_file()
    assert not mail.exists()


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
def test_name_option_is_used_in_the_basename(tmp_path: Path) -> None:
    (tmp_path / "a.eml").write_bytes(build_mail(subject="Rechnung"))

    result = runner.invoke(app, ["eml", "--name", "hetzner", str(tmp_path)])

    assert result.exit_code == 0
    folders = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert "_hetzner_" in folders[0].name
