"""Tests for the macOS Quick Action (right-click → Services) installer."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from imaparc.exceptions import ImapArcError
from imaparc.service import SERVICE_NAME, install_quick_action


def _bundle(services_dir: Path) -> Path:
    return services_dir / f"{SERVICE_NAME}.workflow"


def _wflow(services_dir: Path) -> dict[str, object]:
    path = _bundle(services_dir) / "Contents" / "document.wflow"
    return plistlib.loads(path.read_bytes())


def _info(services_dir: Path) -> dict[str, object]:
    path = _bundle(services_dir) / "Contents" / "Info.plist"
    return plistlib.loads(path.read_bytes())


def _params(services_dir: Path) -> dict[str, object]:
    actions = _wflow(services_dir)["actions"]
    assert isinstance(actions, list)
    return dict(actions[0]["action"]["ActionParameters"])


def test_creates_the_bundle_structure(tmp_path: Path) -> None:
    install_quick_action(tmp_path, executable=Path("/opt/bin/imaparc"))

    contents = _bundle(tmp_path) / "Contents"
    assert (contents / "Info.plist").is_file()
    assert (contents / "document.wflow").is_file()


def test_runs_the_given_executable_with_all_arguments(tmp_path: Path) -> None:
    """Finder passes every selected file or folder; all of them must arrive."""
    install_quick_action(tmp_path, executable=Path("/opt/bin/imaparc"))

    command = _params(tmp_path)["COMMAND_STRING"]
    assert isinstance(command, str)
    assert '"/opt/bin/imaparc"' in command
    assert " eml " in command
    assert '"$@"' in command


def test_passes_input_as_arguments_not_stdin(tmp_path: Path) -> None:
    """inputMethod 1 = 'as arguments'; 0 would pipe the paths to stdin instead."""
    install_quick_action(tmp_path, executable=Path("/opt/bin/imaparc"))

    assert _params(tmp_path)["inputMethod"] == 1


def test_registers_as_a_finder_service_for_files_and_folders(tmp_path: Path) -> None:
    install_quick_action(tmp_path, executable=Path("/opt/bin/imaparc"))

    services = _info(tmp_path)["NSServices"]
    assert isinstance(services, list)
    entry = services[0]
    assert entry["NSMessage"] == "runWorkflowAsService"
    assert entry["NSMenuItem"]["default"] == SERVICE_NAME
    # public.item covers both files and directories, so a mixed selection works.
    assert entry["NSSendFileTypes"] == ["public.item"]
    assert entry["NSRequiredContext"]["NSApplicationIdentifier"] == "com.apple.finder"


def test_absolute_executable_path_is_required(tmp_path: Path) -> None:
    """Services do not inherit PATH — a bare name would never resolve."""
    with pytest.raises(ImapArcError, match="absolute"):
        install_quick_action(tmp_path, executable=Path("imaparc"))


def test_reinstall_replaces_a_previous_version(tmp_path: Path) -> None:
    install_quick_action(tmp_path, executable=Path("/old/imaparc"))
    stale = _bundle(tmp_path) / "Contents" / "leftover.txt"
    stale.write_text("from an older install", encoding="utf-8")

    install_quick_action(tmp_path, executable=Path("/new/imaparc"))

    assert '"/new/imaparc"' in str(_params(tmp_path)["COMMAND_STRING"])
    assert not stale.exists()


def test_name_option_is_passed_through(tmp_path: Path) -> None:
    install_quick_action(tmp_path, executable=Path("/opt/bin/imaparc"), name="hetzner")

    assert "--name hetzner" in str(_params(tmp_path)["COMMAND_STRING"])


# --- CLI ------------------------------------------------------------------


def test_cli_installs_and_reports_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from imaparc.cli import app

    monkeypatch.setattr("imaparc.cli.SERVICES_DIR", tmp_path)
    monkeypatch.setattr("shutil.which", lambda _cmd: "/opt/bin/imaparc")

    result = CliRunner().invoke(app, ["install-service"])

    assert result.exit_code == 0
    assert _bundle(tmp_path).is_dir()
    assert "Rechtsklick" in result.output or "right-click" in result.output.lower()


def test_cli_fails_clearly_when_imaparc_is_not_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from imaparc.cli import app

    monkeypatch.setattr("imaparc.cli.SERVICES_DIR", tmp_path)
    monkeypatch.setattr("shutil.which", lambda _cmd: None)

    result = CliRunner().invoke(app, ["install-service"])

    assert result.exit_code == 1
    assert "Traceback" not in result.output


# --- PATH for the tools (a Service inherits none) ---------------------------


def test_bakes_in_the_directories_of_the_external_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finder runs the action with a minimal PATH, so gs/qpdf/verapdf vanish.

    Without this the action dies with "Missing required tool(s): gs, qpdf,
    verapdf" even though they are installed and work fine in a terminal.
    """
    found = {
        "gs": "/opt/homebrew/bin/gs",
        "qpdf": "/opt/homebrew/bin/qpdf",
        "verapdf": "/Users/someone/verapdf/verapdf",
    }
    monkeypatch.setattr("imaparc.service.shutil.which", lambda cmd: found.get(cmd))

    install_quick_action(tmp_path, executable=Path("/opt/bin/imaparc"))

    command = str(_params(tmp_path)["COMMAND_STRING"])
    assert "export PATH=" in command
    assert "/opt/homebrew/bin" in command
    assert "/Users/someone/verapdf" in command
    # The inherited PATH still comes last, so a terminal run is unaffected.
    assert "$PATH" in command


def test_falls_back_to_the_usual_homebrew_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool missing at install time must not leave the action unusable later."""
    monkeypatch.setattr("imaparc.service.shutil.which", lambda _cmd: None)

    install_quick_action(tmp_path, executable=Path("/opt/bin/imaparc"))

    command = str(_params(tmp_path)["COMMAND_STRING"])
    assert "/opt/homebrew/bin" in command
    assert "/usr/local/bin" in command


def test_path_entries_are_not_duplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "imaparc.service.shutil.which", lambda _cmd: "/opt/homebrew/bin/x"
    )

    install_quick_action(tmp_path, executable=Path("/opt/bin/imaparc"))

    command = str(_params(tmp_path)["COMMAND_STRING"])
    export_line = next(ln for ln in command.splitlines() if "export PATH=" in ln)
    assert export_line.count("/opt/homebrew/bin") == 1


# --- Linux: a Desktop Entry action instead of a Quick Action ----------------


def test_linux_action_runs_the_executable_with_all_arguments(tmp_path: Path) -> None:
    """File managers pass the selection; all of it must reach imaparc."""
    from imaparc.service import install_desktop_action

    path = install_desktop_action(tmp_path, executable=Path("/opt/bin/imaparc"))

    body = path.read_text(encoding="utf-8")
    assert "Exec=" in body
    assert "/opt/bin/imaparc" in body
    assert "%F" in body  # every selected file, not just the first (%f)


def test_linux_action_is_offered_for_eml_and_directories(tmp_path: Path) -> None:
    from imaparc.service import install_desktop_action

    body = install_desktop_action(
        tmp_path, executable=Path("/opt/bin/imaparc")
    ).read_text(encoding="utf-8")

    assert "message/rfc822" in body  # .eml
    assert "inode/directory" in body


def test_linux_action_is_hidden_from_the_application_menu(tmp_path: Path) -> None:
    """It is a file action, not something to launch from an app grid."""
    from imaparc.service import install_desktop_action

    body = install_desktop_action(
        tmp_path, executable=Path("/opt/bin/imaparc")
    ).read_text(encoding="utf-8")

    assert "NoDisplay=true" in body


def test_linux_action_needs_an_absolute_path(tmp_path: Path) -> None:
    from imaparc.service import install_desktop_action

    with pytest.raises(ImapArcError, match="absolute"):
        install_desktop_action(tmp_path, executable=Path("imaparc"))
