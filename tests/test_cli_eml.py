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


def _isolated(tmp_path: Path) -> list[str]:
    """Never the user's real profile.yaml or ~/imapArc."""
    profiles = tmp_path / "empty.yaml"
    profiles.write_text("profiles: []\n", encoding="utf-8")
    return ["--profiles", str(profiles), "--root", str(tmp_path / "arc")]


def test_a_mistyped_profiles_path_is_an_error(tmp_path: Path) -> None:
    """Otherwise every mail would silently be filed into 00_Mails."""
    (tmp_path / "a.eml").write_bytes(build_mail())

    result = runner.invoke(
        app, ["eml", "--profiles", str(tmp_path / "nope.yaml"), str(tmp_path / "a.eml")]
    )

    assert result.exit_code == 1
    assert "not found" in result.output
    assert (tmp_path / "a.eml").exists()


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
def test_renders_into_the_archive_and_archives_the_eml(tmp_path: Path) -> None:
    mail = tmp_path / "Rechnung.eml"
    mail.write_bytes(build_mail(subject="Rechnung Juli"))

    result = runner.invoke(app, ["eml", *_isolated(tmp_path), str(mail)])

    assert result.exit_code == 0, result.output
    [pdf] = (tmp_path / "arc" / "00_Mails").iterdir()
    assert pdf.suffix == ".pdf"
    archive = tmp_path / "arc" / "eml-Archiv" / "00_Mails"
    assert (archive / f"{pdf.stem}.eml").is_file()
    assert not mail.exists()


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
def test_profiles_from_the_given_file_pick_the_target(tmp_path: Path) -> None:
    profiles = tmp_path / "profile.yaml"
    profiles.write_text(
        "profiles:\n"
        "  - name: Amt\n"
        "    account: x\n"
        f"    output: {tmp_path / 'arc' / 'Amt'}\n"
        "    match:\n"
        "      addresses: [amt@stadt.de]\n",
        encoding="utf-8",
    )
    mail = tmp_path / "a.eml"
    mail.write_bytes(build_mail(from_="amt@stadt.de", subject="Termin"))

    root = str(tmp_path / "arc")
    result = runner.invoke(
        app, ["eml", "--profiles", str(profiles), "--root", root, str(mail)]
    )

    assert result.exit_code == 0, result.output
    [pdf] = (tmp_path / "arc" / "Amt").iterdir()
    assert "_Amt_" in pdf.name


def test_broken_profiles_file_exits_with_an_error(tmp_path: Path) -> None:
    profiles = tmp_path / "profile.yaml"
    profiles.write_text("profiles: [ {name: x", encoding="utf-8")
    (tmp_path / "a.eml").write_bytes(build_mail())

    result = runner.invoke(
        app, ["eml", "--profiles", str(profiles), str(tmp_path / "a.eml")]
    )

    assert result.exit_code == 1
    assert "Error:" in result.output


@pytest.mark.requires_tools  # ToolPaths.resolve() runs before the browser starts
def test_missing_chromium_exits_with_an_error_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mails = tmp_path / "mails"
    mails.mkdir()
    (mails / "a.eml").write_bytes(build_mail())
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "no-browsers"))

    result = runner.invoke(app, ["eml", *_isolated(tmp_path), str(mails)])

    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "playwright install chromium" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
