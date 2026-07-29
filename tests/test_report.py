"""Tests for the run summary (imaparc.report)."""

from __future__ import annotations

from imaparc.pipeline import RenderResult
from imaparc.report import RunReport


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
    assert "1 with attachments not embedded as pages" in report.summary()


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


def test_non_compliant_mailonly_is_flagged_as_anomaly() -> None:
    """A mail-only rendition is imapArc's own output — non-compliance is a bug."""
    report = RunReport()
    report.non_compliant.append("/archive/pdf/a/a_mailonly.pdf")

    assert "anomaly" in report.summary()


def test_non_compliant_combined_only_is_not_an_anomaly() -> None:
    """A combined PDF may carry an attachment that resists conversion."""
    report = RunReport()
    report.non_compliant.append("/archive/pdf/a/a.pdf")

    assert "anomaly" not in report.summary()
