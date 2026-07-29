"""Shared pytest fixtures and tool gating."""

from __future__ import annotations

import shutil
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

    for item in items:
        if "requires_chromium" in item.keywords and not chromium_ok:
            item.add_marker(skip_chromium)
        if "requires_tools" in item.keywords and missing:
            item.add_marker(skip_tools)
