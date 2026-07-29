"""Tests for configuration and tool resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from imaparc.config import RunConfig, ToolPaths, default_icc_profile
from imaparc.exceptions import ToolNotFoundError


def _tools(tmp_path: Path) -> ToolPaths:
    return ToolPaths(
        gs=tmp_path / "gs", qpdf=tmp_path / "qpdf", verapdf=tmp_path / "verapdf"
    )


def test_resolve_finds_tools_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "imaparc.config.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    tools = ToolPaths.resolve()
    assert tools.gs == Path("/usr/bin/gs")
    assert tools.verapdf == Path("/usr/bin/verapdf")


def test_resolve_honours_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("imaparc.config.shutil.which", lambda name: None)
    tools = ToolPaths.resolve(
        {"gs": "/opt/gs", "qpdf": "/opt/qpdf", "verapdf": "/opt/verapdf"}
    )
    assert tools.gs == Path("/opt/gs")


def test_resolve_reports_all_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("imaparc.config.shutil.which", lambda name: None)
    with pytest.raises(ToolNotFoundError) as exc:
        ToolPaths.resolve()
    message = str(exc.value)
    assert "gs" in message
    assert "qpdf" in message
    assert "verapdf" in message


def test_defaults_are_sane(tmp_path: Path) -> None:
    cfg = RunConfig(tools=_tools(tmp_path))
    assert cfg.verbosity == 1
    assert cfg.jobs == 4
    assert cfg.validate_pdfa is True


def test_filename_defaults_use_profile_scheme(tmp_path: Path) -> None:
    cfg = RunConfig(tools=_tools(tmp_path))
    assert cfg.filename_pattern == "{date}_{profile}_{subject}"
    assert cfg.date_format == "YYYY-MM-DD_hh-mm-ss"


def test_icc_profile_picks_the_first_existing_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The macOS path is tried first, but a Linux one is used when it is absent."""
    absent = tmp_path / "absent.icc"
    present = tmp_path / "present.icc"
    present.write_bytes(b"icc")
    monkeypatch.setattr("imaparc.config.ICC_CANDIDATES", (absent, present))

    assert default_icc_profile() == present


def test_icc_profile_falls_back_to_the_first_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With none present, the first path is returned so the error names it."""
    first = tmp_path / "first.icc"
    monkeypatch.setattr(
        "imaparc.config.ICC_CANDIDATES", (first, tmp_path / "second.icc")
    )

    assert default_icc_profile() == first


def test_icc_profile_is_resolved_per_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolution happens when a RunConfig is built, not at import time."""
    profile = tmp_path / "late.icc"
    profile.write_bytes(b"icc")
    monkeypatch.setattr("imaparc.config.ICC_CANDIDATES", (profile,))

    assert RunConfig(tools=_tools(tmp_path)).icc_profile == profile
