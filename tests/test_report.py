"""Tests for the run summary (imaparc.report)."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.progress import Progress

from imaparc.config import ToolPaths
from imaparc.pdf.validate import ValidationResult
from imaparc.pipeline import RenderResult
from imaparc.report import RunReport, validate_pdfa


def _result(
    name: str,
    *,
    written: bool = True,
    skipped: bool = False,
    html_rendered: bool = True,
    complete: bool = True,
) -> RenderResult:
    return RenderResult(
        basename=name,
        written=written,
        skipped=skipped,
        html_rendered=html_rendered,
        complete=complete,
    )


def test_empty_report_counts_zero() -> None:
    assert RunReport().summary() == "0 written, 0 skipped"


def test_partitions_written_and_skipped() -> None:
    report = RunReport()
    report.add(_result("a"))
    report.add(_result("b", written=False, skipped=True))

    assert [r.basename for r in report.written] == ["a"]
    assert [r.basename for r in report.skipped] == ["b"]
    assert report.summary().startswith("1 written, 1 skipped")


def test_incomplete_counts_only_written_mails() -> None:
    report = RunReport()
    report.add(_result("a", complete=False))
    # A skipped mail is not re-rendered, so its completeness is not reported.
    report.add(_result("b", written=False, skipped=True, complete=False))

    assert [r.basename for r in report.incomplete] == ["a"]
    assert "1 with attachments not shown as pages" in report.summary()


def test_html_fallback_is_reported() -> None:
    report = RunReport()
    report.add(_result("a", html_rendered=False))

    assert [r.basename for r in report.html_fallback] == ["a"]
    assert "1 fell back to the text version" in report.summary()


def test_non_compliant_lists_file_names() -> None:
    report = RunReport()
    report.add(_result("a"))
    report.non_compliant.append("/archive/pdf/a/a.pdf")

    summary = report.summary()
    assert "1 not PDF/A-3b compliant" in summary
    assert "  - a.pdf" in summary
    assert "more" not in summary


def test_non_compliant_truncates_after_ten() -> None:
    report = RunReport()
    report.non_compliant.extend(f"/archive/mail-{i}.pdf" for i in range(12))

    summary = report.summary()
    assert "12 not PDF/A-3b compliant" in summary
    assert summary.count("  - ") == 10
    assert "… and 2 more" in summary


# --- PDF/A validation ------------------------------------------------------


def _tools() -> ToolPaths:
    return ToolPaths(gs=Path("/gs"), qpdf=Path("/qpdf"), verapdf=Path("/verapdf"))


def _quiet_progress() -> Progress:
    return Progress(disable=True)


def test_validate_deduplicates_pdf_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PDF reported twice is validated once."""
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    report = RunReport()
    for _ in range(2):
        report.add(RenderResult(basename="a", written=True, combined_pdf=pdf))
    validated: list[list[Path]] = []

    def _batch(_verapdf: Path, paths: list[Path]) -> list[ValidationResult]:
        validated.append(list(paths))
        return [ValidationResult(compliant=True) for _ in paths]

    monkeypatch.setattr("imaparc.report.run_verapdf_batch", _batch)
    with _quiet_progress() as progress:
        validate_pdfa(report, _tools(), progress)

    assert validated == [[pdf]]
    assert report.non_compliant == []


def test_validate_records_non_compliant_pdfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = tmp_path / "good.pdf"
    bad = tmp_path / "bad.pdf"
    for path in (good, bad):
        path.write_bytes(b"%PDF-1.4")
    report = RunReport()
    report.add(RenderResult(basename="a", written=True, combined_pdf=good))
    report.add(RenderResult(basename="b", written=True, combined_pdf=bad))

    def _batch(_verapdf: Path, paths: list[Path]) -> list[ValidationResult]:
        return [ValidationResult(compliant=p.name == "good.pdf") for p in paths]

    monkeypatch.setattr("imaparc.report.run_verapdf_batch", _batch)
    with _quiet_progress() as progress:
        validate_pdfa(report, _tools(), progress)

    assert report.non_compliant == [str(bad)]


def test_validate_skips_missing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = RunReport()
    report.add(
        RenderResult(basename="a", written=True, combined_pdf=tmp_path / "gone.pdf")
    )
    called = False

    def _batch(_verapdf: Path, paths: list[Path]) -> list[ValidationResult]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr("imaparc.report.run_verapdf_batch", _batch)
    with _quiet_progress() as progress:
        validate_pdfa(report, _tools(), progress)

    assert not called
