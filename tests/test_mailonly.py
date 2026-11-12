"""Tests for `imaparc mailonly` — the mail without its attachments' pages."""

from __future__ import annotations

import io
from pathlib import Path

import pikepdf
import pytest
from typer.testing import CliRunner

from imaparc.cli import app
from imaparc.config import ToolPaths, default_icc_profile
from imaparc.mailonly import mail_only
from imaparc.pdf import provenance
from imaparc.pdf.pdfa import to_pdfa3b
from imaparc.pdf.validate import run_verapdf

runner = CliRunner()

_MARKS = [
    ("E-Mail", 0),
    ("Nur-Text-Version", 1),
    ("Anhang 1: a.pdf", 2),
    ("Anhang 2: b.docx", 4),
]


def _pages(count: int) -> bytes:
    doc = pikepdf.Pdf.new()
    for _ in range(count):
        doc.add_blank_page()
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _rendered(path: Path, *, marks: list[tuple[str, int]] | None = None) -> Path:
    """A 5-page PDF as render writes it: 2 pages of mail, 3 of attachments."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        provenance.stamp(
            _pages(5),
            b"raw mail",
            message_id="<m@x>",
            eml_name="m.eml",
            attachments=[("b.docx", "application/x-doc", b"PK")],
            bookmarks=_MARKS if marks is None else marks,
            title="Betreff",
            author="a@x",
        )
    )
    return path


def _titles(pdf: Path) -> list[str]:
    with pikepdf.open(pdf) as doc, doc.open_outline() as outline:
        return [item.title for item in outline.root]


def test_only_the_pages_before_the_first_attachment_remain(tmp_path: Path) -> None:
    pdf = _rendered(tmp_path / "in" / "X.pdf")

    result = mail_only(pdf, tmp_path / "out")

    assert result.written == tmp_path / "out" / "X_mailonly.pdf"
    assert (result.pages, result.of_pages) == (2, 5)
    with pikepdf.open(result.written) as doc:
        assert len(doc.pages) == 2
        assert str(doc.docinfo["/Title"]) == "Betreff"
    assert _titles(result.written) == ["E-Mail", "Nur-Text-Version"]


def test_the_copy_carries_no_embedded_files_but_still_names_its_mail(
    tmp_path: Path,
) -> None:
    pdf = _rendered(tmp_path / "in" / "X.pdf")

    written = mail_only(pdf, tmp_path / "out").written

    assert written is not None
    assert provenance.embedded_files(written) == {}
    assert provenance.read(written) == provenance.read(pdf)
    assert written.stat().st_size < pdf.stat().st_size
    # The original is untouched.
    assert len(provenance.embedded_files(pdf)) == 2


def test_keep_embedded_keeps_the_mail_and_attachments(tmp_path: Path) -> None:
    pdf = _rendered(tmp_path / "in" / "X.pdf")

    written = mail_only(pdf, tmp_path / "out", keep_embedded=True).written

    assert written is not None
    assert sorted(provenance.embedded_files(written)) == ["b.docx", "m.eml"]


def test_a_mail_without_attachments_keeps_every_page(tmp_path: Path) -> None:
    pdf = _rendered(tmp_path / "in" / "X.pdf", marks=[("E-Mail", 0)])

    result = mail_only(pdf, tmp_path / "out")

    assert (result.pages, result.of_pages) == (5, 5)


def test_a_pdf_without_bookmarks_is_refused(tmp_path: Path) -> None:
    pdf = _rendered(tmp_path / "in" / "X.pdf", marks=[])

    result = mail_only(pdf, tmp_path / "out")

    assert result.written is None
    assert "no bookmarks" in (result.error or "")
    assert not (tmp_path / "out").exists()


def test_never_overwrites(tmp_path: Path) -> None:
    pdf = _rendered(tmp_path / "in" / "X.pdf")
    first = mail_only(pdf, tmp_path / "out").written

    second = mail_only(pdf, tmp_path / "out").written

    assert first is not None and second is not None
    assert second.name == "X_mailonly-2.pdf"


def test_refuses_to_write_into_the_archive(tmp_path: Path) -> None:
    """A second PDF recording the same mail there would count as a duplicate."""
    profile = tmp_path / "arc" / "P"
    (tmp_path / "arc" / "eml-Archiv" / "P").mkdir(parents=True)
    pdf = _rendered(profile / "X.pdf")

    result = mail_only(pdf, profile)

    assert "refusing" in (result.error or "")
    assert [p.name for p in profile.iterdir()] == ["X.pdf"]


def test_cli_mailonly(tmp_path: Path) -> None:
    pdf = _rendered(tmp_path / "in" / "X.pdf")

    result = runner.invoke(app, ["mailonly", str(pdf), "-o", str(tmp_path / "out")])

    assert result.exit_code == 0, result.output
    assert "X_mailonly.pdf" in result.output and "2 of 5 pages" in result.output


def test_cli_mailonly_exits_1_for_an_unusable_pdf(tmp_path: Path) -> None:
    plain = tmp_path / "plain.pdf"
    plain.write_bytes(_pages(1))

    result = runner.invoke(app, ["mailonly", str(plain), "-o", str(tmp_path / "o")])

    assert result.exit_code == 1
    assert "no bookmarks" in result.output


@pytest.mark.requires_tools
def test_the_copy_of_a_pdfa_stays_pdfa_3b(tmp_path: Path) -> None:
    tools = ToolPaths.resolve()
    src = tmp_path / "in.pdf"
    src.write_bytes(_pages(5))
    pdfa = tmp_path / "pdfa.pdf"
    to_pdfa3b(src, pdfa, gs=tools.gs, icc=default_icc_profile(), work_dir=tmp_path)
    full = tmp_path / "arc" / "X.pdf"
    full.parent.mkdir()
    full.write_bytes(
        provenance.stamp(
            pdfa.read_bytes(),
            b"raw",
            message_id="<m@x>",
            eml_name="m.eml",
            bookmarks=_MARKS,
            title="Betreff",
            author="a@x",
        )
    )

    for keep in (False, True):
        written = mail_only(full, tmp_path / f"out{keep}", keep_embedded=keep).written
        assert written is not None
        assert run_verapdf(tools.verapdf, written).compliant
