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


def test_an_encrypted_or_broken_pdf_records_nothing() -> None:
    """A foreign PDF in a profile directory must not abort indexing it."""
    doc = pikepdf.Pdf.new()
    doc.add_blank_page()
    out = io.BytesIO()
    doc.save(out, encryption=pikepdf.Encryption(owner="o", user="secret"))

    assert provenance.read(out.getvalue()) is None
    assert provenance.identity(out.getvalue()) is None
    assert provenance.read(b"not a pdf at all") is None


def test_a_pdf_with_broken_metadata_records_nothing() -> None:
    """Not a stream, not XML: still no reason to abort indexing a directory."""
    for metadata in (pikepdf.Dictionary(X=1), None):
        doc = pikepdf.Pdf.new()
        doc.add_blank_page()
        doc.Root.Metadata = (
            metadata if metadata is not None else pikepdf.Stream(doc, b"<not xml")
        )
        out = io.BytesIO()
        doc.save(out, fix_metadata_version=False)

        assert provenance.read(out.getvalue()) is None


def test_a_message_id_with_control_characters_can_still_be_stamped() -> None:
    """The sender picks it; XML forbids them — the mail must render regardless."""
    raw = build_mail(subject="Hi", text="body")

    stamped = provenance.stamp(_pdf(), raw, message_id="<a\x01b\x00@x>")

    recorded = provenance.read(stamped)
    assert recorded is not None
    assert recorded.sha256 == hashlib.sha256(raw).hexdigest()
    assert recorded.message_id == "<ab@x>"


def test_embedded_file_names_are_sorted_as_stored() -> None:
    """A name tree's keys must be in byte order, or a reader's lookup may miss."""
    stamped = provenance.stamp(
        _pdf(),
        b"raw",
        message_id=None,
        eml_name="mail.eml",
        attachments=[
            ("\u00ff.docx", "application/x-a", b"1"),
            ("\u042f.docx", "application/x-b", b"2"),
            ("a.zip", "application/zip", b"3"),
        ],
    )
    with pikepdf.open(io.BytesIO(stamped)) as pdf:
        keys = [bytes(k) for k in list(pdf.Root.Names.EmbeddedFiles.Names)[0::2]]

    assert keys == sorted(keys)
    assert len(provenance.embedded_files(stamped)) == 4


def test_title_and_author_come_from_the_mail() -> None:
    stamped = provenance.stamp(
        _pdf(), b"raw", message_id=None, title="Line one\nline\x00 two ", author="A"
    )
    with pikepdf.open(io.BytesIO(stamped)) as doc:
        # One clean line: control characters and line breaks are not carried over.
        assert str(doc.docinfo["/Title"]) == "Line one line two"
        assert str(doc.docinfo["/Author"]) == "A"


def test_a_mail_without_subject_keeps_the_pdf_untitled() -> None:
    stamped = provenance.stamp(_pdf(), b"raw", message_id=None, title="", author=None)
    with pikepdf.open(io.BytesIO(stamped)) as doc:
        assert "/Title" not in doc.docinfo
        assert "/Author" not in doc.docinfo


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
    assert provenance.embedded_source(stamped) == ("mail.eml", raw)
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
            attachments=[("report.docx", "application/x-doc", b"PK")],
            bookmarks=[("E-Mail", 0)],
            title="Rechnung \u00fcber 3 \u20ac",
            author="Ada <ada@example.com>",
        )
    )
    # Ghostscript titles every file "imapArc archive"; ours must replace it in
    # the info dictionary and the XMP alike, or PDF/A validation fails.
    with pikepdf.open(out) as doc:
        assert str(doc.docinfo["/Title"]) == "Rechnung \u00fcber 3 \u20ac"
        assert str(doc.docinfo["/Author"]) == "Ada <ada@example.com>"
        xmp = doc.Root.Metadata.read_bytes().decode()
    assert "imapArc archive" not in xmp
    assert xmp.count("Rechnung \u00fcber 3 \u20ac") == 1

    assert run_verapdf(tools.verapdf, out).compliant
    if shutil.which("pdfdetach"):
        import subprocess

        listing = subprocess.run(
            ["pdfdetach", "-list", str(out)], capture_output=True, text=True, check=True
        )
        assert "mail.eml" in listing.stdout
