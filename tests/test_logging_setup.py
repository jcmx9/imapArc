"""Tests for the verbosity-to-logging mapping."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from imaparc.logging_setup import setup_logging


@pytest.fixture(autouse=True)
def _reset_root_logger() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)


def test_silent_has_no_console_handler() -> None:
    setup_logging(0)
    root = logging.getLogger()
    assert root.handlers == []


def test_normal_adds_console_handler() -> None:
    setup_logging(1)
    root = logging.getLogger()
    assert len(root.handlers) == 1


def test_silent_still_writes_log_file(tmp_path: Path) -> None:
    log_file = tmp_path / "sub" / "run.log"
    setup_logging(0, log_file=log_file)
    logging.getLogger("test").info("cron marker")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert log_file.exists()
    assert "cron marker" in log_file.read_text(encoding="utf-8")


def test_debug_sets_file_handler_to_debug(tmp_path: Path) -> None:
    log_file = tmp_path / "run.log"
    setup_logging(3, log_file=log_file)
    file_handlers = [
        h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)
    ]
    assert file_handlers
    assert file_handlers[0].level == logging.DEBUG
