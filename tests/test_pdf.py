"""Tests for the PDF tool wrappers (merge, gs args, veraPDF parsing).

Pure/lib tests run everywhere; the real gs→veraPDF round-trip is gated on
requires_tools."""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pikepdf
import pytest

from imaparc.attachments.image_to_pdf import image_to_pdf
from imaparc.config import default_icc_profile
from imaparc.pdf.merge import PdfMergeError, count_pages, merge_pdfs
from imaparc.pdf.pdfa import build_gs_args, to_pdfa3b
from imaparc.pdf.validate import (
    ValidationError,
    ValidationResult,
    parse_verapdf_jobs,
    parse_verapdf_json,
    run_verapdf,
)


def _pdf(pages: int = 1) -> bytes:
    doc = pikepdf.Pdf.new()
    for _ in range(pages):
        doc.add_blank_page()
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _jpeg() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (30, 40), (10, 20, 30)).save(out, format="JPEG")
    return out.getvalue()


# --- build_gs_args (pure) ---------------------------------------------------


def test_build_gs_args_permit_before_safer() -> None:
    args = build_gs_args(
        Path("/usr/bin/gs"),
        Path("s.pdf"),
        Path("d.pdf"),
        Path("def.ps"),
        Path("/x/icc.icc"),
    )
    assert args.index("--permit-file-read=/x/icc.icc") < args.index("-dSAFER")


def test_build_gs_args_essential_flags() -> None:
    args = build_gs_args(Path("gs"), Path("s"), Path("d"), Path("def"), Path("icc"))
    assert "-dPDFA=3" in args
    assert "-dAutoRotatePages=/None" in args
    assert "-sColorConversionStrategy=RGB" in args
    assert "-sDEVICE=pdfwrite" in args
    assert args[-1] == "s"  # source is the last positional
    assert "-sOutputFile=d" in args


# --- merge / count_pages (lib) ----------------------------------------------


def test_count_pages() -> None:
    assert count_pages(_pdf(3)) == 3


def test_merge_concatenates_in_order() -> None:
    merged = merge_pdfs([_pdf(2), _pdf(1), _pdf(3)])
    assert count_pages(merged) == 6


def test_merge_empty_raises() -> None:
    with pytest.raises(PdfMergeError):
        merge_pdfs([])


def test_merge_invalid_part_raises() -> None:
    with pytest.raises(PdfMergeError):
        merge_pdfs([_pdf(1), b"not a pdf"])


# --- parse_verapdf_json (pure) ----------------------------------------------


def _verapdf_json(compliant: bool, *, as_list: bool = False) -> str:
    result = {
        "compliant": compliant,
        "details": {"passedRules": 120, "failedRules": 0 if compliant else 3},
    }
    return json.dumps(
        {"report": {"jobs": [{"validationResult": [result] if as_list else result}]}}
    )


def test_parse_verapdf_compliant() -> None:
    res = parse_verapdf_json(_verapdf_json(True))
    assert res.compliant is True
    assert res.passed_rules == 120


def test_parse_verapdf_non_compliant() -> None:
    res = parse_verapdf_json(_verapdf_json(False))
    assert res.compliant is False
    assert res.failed_rules == 3


def test_parse_verapdf_result_as_list() -> None:
    assert parse_verapdf_json(_verapdf_json(True, as_list=True)).compliant is True


def test_parse_verapdf_malformed_raises() -> None:
    with pytest.raises(ValidationError):
        parse_verapdf_json("not json at all")


def _verapdf_batch_json(*compliances: bool) -> str:
    jobs = [
        {
            "validationResult": {
                "compliant": c,
                "details": {"passedRules": 120, "failedRules": 0 if c else 3},
            }
        }
        for c in compliances
    ]
    return json.dumps({"report": {"jobs": jobs}})


def test_parse_verapdf_jobs_preserves_order() -> None:
    results = parse_verapdf_jobs(_verapdf_batch_json(True, False, True))
    assert [r.compliant for r in results] == [True, False, True]


def test_parse_verapdf_jobs_missing_result_is_non_compliant() -> None:
    # A job veraPDF could not validate (no validationResult) must not pass.
    stdout = json.dumps({"report": {"jobs": [{"itemDetails": {"name": "x.pdf"}}]}})
    results = parse_verapdf_jobs(stdout)
    assert results == [ValidationResult(compliant=False)]


def test_parse_verapdf_jobs_malformed_raises() -> None:
    with pytest.raises(ValidationError):
        parse_verapdf_jobs("not json at all")


# --- real gs → veraPDF round-trip (gated) -----------------------------------


@pytest.mark.requires_tools
def test_pdfa_roundtrip_is_compliant(tmp_path: Path) -> None:
    src = tmp_path / "src.pdf"
    src.write_bytes(image_to_pdf(_jpeg()))
    dst = tmp_path / "out.pdf"
    gs = shutil.which("gs")
    verapdf = shutil.which("verapdf")
    assert gs and verapdf
    to_pdfa3b(src, dst, gs=Path(gs), icc=default_icc_profile(), work_dir=tmp_path)
    assert run_verapdf(Path(verapdf), dst).compliant is True
