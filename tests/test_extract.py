"""Tests for `imaparc extract` — the mail back out of its PDF."""

from __future__ import annotations

import io
from pathlib import Path

import pikepdf
from typer.testing import CliRunner

from imaparc.cli import app
from imaparc.extract import extract
from imaparc.pdf import provenance
from tests.mail_builder import build_mail

runner = CliRunner()


def _blank() -> bytes:
    doc = pikepdf.Pdf.new()
    doc.add_blank_page()
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _rendered(path: Path, raw: bytes, *, attachment: bool = False) -> Path:
    path.write_bytes(
        provenance.stamp(
            _blank(),
            raw,
            message_id="<m@x>",
            eml_name="2026-01-02_mail_Hi.eml",
            attachments=[("report.docx", "application/x-doc", b"PK")]
            if attachment
            else [],
        )
    )
    return path


def test_extract_writes_the_mail_and_verifies_it(tmp_path: Path) -> None:
    raw = build_mail(subject="Hi")
    pdf = _rendered(tmp_path / "a.pdf", raw)

    result = extract(pdf, tmp_path / "out")

    assert [p.name for p in result.written] == ["2026-01-02_mail_Hi.eml"]
    assert result.written[0].read_bytes() == raw
    assert result.verified is True and result.error is None


def test_extract_all_includes_the_attachments(tmp_path: Path) -> None:
    pdf = _rendered(tmp_path / "a.pdf", b"raw", attachment=True)

    result = extract(pdf, tmp_path / "out", everything=True)

    assert sorted(p.name for p in result.written) == [
        "2026-01-02_mail_Hi.eml",
        "report.docx",
    ]
    assert (tmp_path / "out" / "report.docx").read_bytes() == b"PK"


def test_extract_never_overwrites(tmp_path: Path) -> None:
    pdf = _rendered(tmp_path / "a.pdf", b"raw")
    out = tmp_path / "out"
    out.mkdir()
    (out / "2026-01-02_mail_Hi.eml").write_bytes(b"mine")

    result = extract(pdf, out)

    assert (out / "2026-01-02_mail_Hi.eml").read_bytes() == b"mine"
    assert [p.name for p in result.written] == ["2026-01-02_mail_Hi-2.eml"]


def test_extract_keeps_a_hostile_name_inside_the_target(tmp_path: Path) -> None:
    """The file name comes out of the PDF — it must not choose the directory."""
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(
        provenance.stamp(_blank(), b"raw", message_id=None, eml_name="../../evil.eml")
    )

    result = extract(pdf, tmp_path / "out" / "deep")

    assert [p.parent for p in result.written] == [tmp_path / "out" / "deep"]
    assert not (tmp_path / "evil.eml").exists()


def test_extract_reports_a_pdf_without_a_mail(tmp_path: Path) -> None:
    plain = tmp_path / "plain.pdf"
    plain.write_bytes(_blank())
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf")

    assert "embeds no .eml" in (extract(plain, tmp_path / "out").error or "")
    assert "cannot read" in (extract(broken, tmp_path / "out").error or "")
    assert not (tmp_path / "out").exists()  # nothing created for nothing


def test_cli_extract_prints_what_it_wrote(tmp_path: Path) -> None:
    pdf = _rendered(tmp_path / "a.pdf", b"raw")

    result = runner.invoke(app, ["extract", str(pdf), "-o", str(tmp_path / "out")])

    assert result.exit_code == 0, result.output
    assert "2026-01-02_mail_Hi.eml" in result.output
    assert (tmp_path / "out" / "2026-01-02_mail_Hi.eml").read_bytes() == b"raw"


def test_cli_extract_exits_1_for_a_foreign_pdf(tmp_path: Path) -> None:
    plain = tmp_path / "plain.pdf"
    plain.write_bytes(_blank())

    result = runner.invoke(app, ["extract", str(plain), "-o", str(tmp_path / "out")])

    assert result.exit_code == 1
    assert "embeds no .eml" in result.output
