"""Tests for the provenance a PDF records about its source mail."""

from __future__ import annotations

import hashlib
import io
import shutil
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pikepdf
import pytest

from imaparc.config import ToolPaths, default_icc_profile
from imaparc.pdf import provenance
from imaparc.pdf.pdfa import to_pdfa3b
from imaparc.pdf.validate import run_verapdf
from tests.mail_builder import build_mail


def _pdf() -> bytes:
    doc = pikepdf.Pdf.new()
    doc.add_blank_page()
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def test_a_plain_pdf_records_nothing() -> None:
    pdf = _pdf()
    assert provenance.read(pdf) is None
    assert provenance.embedded_source(pdf) is None


def test_stamp_records_hash_and_message_id() -> None:
    raw = build_mail(subject="Hi", text="body")
    stamped = provenance.stamp(_pdf(), raw, message_id="<id@x>")
    assert provenance.read(stamped) == provenance.Provenance(
        hashlib.sha256(raw).hexdigest(), "<id@x>"
    )
    # Hash only: nothing embedded unless asked for.
    assert provenance.embedded_source(stamped) is None


def test_stamp_without_message_id_records_the_hash_alone() -> None:
    raw = b"Subject: x\r\n\r\nbody"
    recorded = provenance.read(provenance.stamp(_pdf(), raw, message_id=None))
    assert recorded == provenance.Provenance(hashlib.sha256(raw).hexdigest())


def test_stamp_embeds_the_mail_as_the_pdf_source() -> None:
    raw = build_mail(subject="Hi", text="body")
    stamped = provenance.stamp(
        _pdf(),
        raw,
        message_id=None,
        eml_name="mail.eml",
        mod_date=datetime(2026, 5, 18, 16, 6, tzinfo=timezone(timedelta(hours=2))),
    )
    assert provenance.embedded_source(stamped) == raw
    with pikepdf.open(io.BytesIO(stamped)) as pdf:
        spec = pdf.Root.AF[0]
        assert str(spec.UF) == "mail.eml"
        assert spec.AFRelationship == pikepdf.Name.Source
        assert spec.EF.F.Subtype == pikepdf.Name("/message/rfc822")
        assert str(spec.EF.F.Params.ModDate) == "D:20260518160600+02'00'"
        assert pdf.Root.Names.EmbeddedFiles.Names[1] == spec


def test_pdf_date_without_timezone() -> None:
    assert provenance._pdf_date(datetime(2026, 1, 2, 3, 4, 5)) == "D:20260102030405"
    west = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=-5)))
    assert provenance._pdf_date(west) == "D:20260102030405-05'00'"
    assert provenance._pdf_date(datetime(2026, 1, 2, tzinfo=UTC)).endswith("+00'00'")


@pytest.mark.requires_tools
def test_a_stamped_pdfa_stays_pdfa_3b(tmp_path: Path) -> None:
    tools = ToolPaths.resolve()
    icc = default_icc_profile()
    assert icc is not None
    src = tmp_path / "in.pdf"
    src.write_bytes(_pdf())
    pdfa = tmp_path / "pdfa.pdf"
    to_pdfa3b(src, pdfa, gs=tools.gs, icc=icc, work_dir=tmp_path)
    raw = build_mail(subject="Hi", text="body")

    out = tmp_path / "stamped.pdf"
    out.write_bytes(
        provenance.stamp(
            pdfa.read_bytes(),
            raw,
            message_id="<id@x>",
            eml_name="mail.eml",
            mod_date=datetime.now(UTC),
        )
    )

    assert run_verapdf(tools.verapdf, out).compliant
    if shutil.which("pdfdetach"):
        import subprocess

        listing = subprocess.run(
            ["pdfdetach", "-list", str(out)], capture_output=True, text=True, check=True
        )
        assert "mail.eml" in listing.stdout
