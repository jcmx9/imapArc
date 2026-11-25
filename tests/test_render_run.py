"""Orchestration of a render run: what starts a browser, what is said once."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import imaparc.render.browser as browser
import imaparc.render_run as render_run
from imaparc.config import ToolPaths
from imaparc.profiles import Profile, input_eml_dir

_TOOLS = ToolPaths(gs=Path("/gs"), qpdf=Path("/qpdf"), verapdf=Path("/verapdf"))


async def test_a_profile_with_nothing_waiting_starts_no_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "imaparc-pdf" / "p"
    output.mkdir(parents=True)
    leftover = output / ".tmp-aborted.pdf"
    leftover.write_bytes(b"half")
    input_eml_dir(output).mkdir(parents=True)  # exists, but empty

    def _no_browser(**_kwargs: object) -> None:
        raise AssertionError("Chromium started for a profile with no mail")

    monkeypatch.setattr(render_run, "BrowserPool", _no_browser)

    report = await render_run.run_render(
        [Profile(name="p", account="a", output=output, remote_images=True)],
        _TOOLS,
        cli_remote=False,
        cli_jobs=None,
        verbosity=0,
    )

    assert report.failed == []
    assert not leftover.exists(), "leftovers of an aborted run are still swept"


@pytest.mark.requires_chromium
async def test_the_remote_content_warning_appears_once_per_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(browser, "_remote_warned", False)

    with caplog.at_level(logging.WARNING, logger="imaparc.render.browser"):
        for _ in range(3):
            async with browser.BrowserPool(allow_remote=True):
                pass

    warnings = [r for r in caplog.records if "remote content ALLOWED" in r.message]
    assert len(warnings) == 1
