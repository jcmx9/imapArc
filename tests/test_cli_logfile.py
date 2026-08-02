"""Tests for `--log-file`, the flag that makes -Q usable for cron."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from imaparc.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """setup_logging replaces the root handlers; put them back afterwards."""
    root = logging.getLogger()
    saved = list(root.handlers)
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in saved:
        root.addHandler(handler)


@pytest.mark.parametrize("command", ["all", "fetch", "render", "eml"])
def test_every_run_command_accepts_a_log_file(command: str) -> None:
    """Inspect the registered options rather than the rendered help.

    Parsing --help ties the test to the terminal width: at 80 columns the option
    name wraps and the substring is simply not there, which is how this passed
    locally and failed in CI.
    """
    import typer.main

    group = typer.main.get_command(app)
    params = group.commands[command].params  # type: ignore[attr-defined]
    options = {opt for param in params for opt in param.opts}

    assert "--log-file" in options


def test_log_file_is_written_even_when_silent(tmp_path: Path) -> None:
    """The whole point: -Q suppresses the console, not the record.

    A cron job runs silent; without this the run leaves no trace at all.
    """
    log = tmp_path / "logs" / "imaparc.log"

    result = runner.invoke(
        app, ["eml", str(tmp_path), "--silent", "--log-file", str(log)]
    )

    assert result.exit_code == 0
    assert log.exists(), "silent run wrote no log file"


def test_log_file_directory_is_created(tmp_path: Path) -> None:
    log = tmp_path / "deep" / "nested" / "run.log"

    runner.invoke(app, ["eml", str(tmp_path), "--log-file", str(log)])

    assert log.parent.is_dir()
