"""Shared pytest fixtures and tool gating."""

from __future__ import annotations

import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

_REQUIRED_TOOLS = ("gs", "qpdf", "verapdf")


def _chromium_installed() -> bool:
    """Whether a Playwright Chromium build is present in the local cache."""
    candidates = [
        Path.home() / "Library" / "Caches" / "ms-playwright",  # macOS
        Path.home() / ".cache" / "ms-playwright",  # Linux
    ]
    return any(base.exists() and any(base.glob("chromium*")) for base in candidates)


def _missing_tools() -> list[str]:
    return [tool for tool in _REQUIRED_TOOLS if shutil.which(tool) is None]


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip tests whose external dependencies are unavailable."""
    chromium_ok = _chromium_installed()
    missing = _missing_tools()

    skip_chromium = pytest.mark.skip(reason="Playwright Chromium not installed")
    skip_tools = pytest.mark.skip(reason=f"missing tools: {', '.join(missing)}")

    ocr_ok = shutil.which("ocrmypdf") is not None and shutil.which("tesseract")
    skip_ocr = pytest.mark.skip(reason="ocrmypdf/tesseract not installed")

    for item in items:
        if "requires_chromium" in item.keywords and not chromium_ok:
            item.add_marker(skip_chromium)
        if "requires_tools" in item.keywords and missing:
            item.add_marker(skip_tools)
        if "requires_ocr" in item.keywords and not ocr_ok:
            item.add_marker(skip_ocr)


@pytest.fixture(autouse=True)
def _fixed_timezone(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run every test in Europe/Berlin.

    Names and the header block show *local* time, so the expected strings
    depend on the machine's zone — CI runs in UTC, a developer's laptop not.
    """
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture(autouse=True)
def _own_run_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Give each test its own render lock, so parallel workers never queue."""
    monkeypatch.setenv("IMAPARC_LOCK_DIR", str(tmp_path / ".locks"))
