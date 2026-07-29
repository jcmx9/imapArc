"""Tests for the render pipeline (assembly, storage, end-to-end)."""

from __future__ import annotations

import contextlib
import io
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path

import pikepdf
import pytest

from imaparc.config import RunConfig, ToolPaths
from imaparc.mail.parser import parse_mail
from imaparc.pdf.validate import run_verapdf
from imaparc.pipeline import _resolve_output, _store, render_mail
from imaparc.render.browser import BrowserPool
from tests.mail_builder import build_mail

QPDF = shutil.which("qpdf")


@pytest.fixture(autouse=True)
def _restore_perms(tmp_path: Path) -> Iterator[None]:
    yield
    for entry in sorted(tmp_path.rglob("*"), reverse=True):
        with contextlib.suppress(OSError):
            entry.chmod(0o700)


def _pdf(pages: int = 1) -> bytes:
    doc = pikepdf.Pdf.new()
    for _ in range(pages):
        doc.add_blank_page()
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- _store (no browser) ----------------------------------------------------


def test_store_attachment_mail_has_full_and_mailonly_pdf(tmp_path: Path) -> None:
    _store(
        tmp_path,
        "m-1",
        identity="id-1",
        primary_pdfa=b"FULL",
        mailonly_pdfa=b"MAILONLY",
        originals=[("a.pdf", b"x"), ("a.pdf", b"y")],
    )
    sub = tmp_path / "m-1"
    # No loose PDF at the root — the mail's PDF lives only inside its folder.
    assert not (tmp_path / "m-1.pdf").exists()
    # <basename>.pdf is the full PDF (mail + attachments); mailonly sits alongside.
    assert (sub / "m-1.pdf").read_bytes() == b"FULL"
    assert (sub / "m-1_mailonly.pdf").read_bytes() == b"MAILONLY"
    # Two same-named attachments: the second is disambiguated, neither lost.
    assert (sub / "a.pdf").read_bytes() == b"x"
    assert (sub / "a-2.pdf").read_bytes() == b"y"


def test_store_attachmentless_mail_has_only_mail_pdf(tmp_path: Path) -> None:
    _store(
        tmp_path,
        "m-1",
        identity="id-1",
        primary_pdfa=b"MAILONLY",
        mailonly_pdfa=None,
        originals=[],
    )
    sub = tmp_path / "m-1"
    # A folder with just the one PDF — no _mailonly duplicate, no originals.
    assert (sub / "m-1.pdf").read_bytes() == b"MAILONLY"
    assert not (sub / "m-1_mailonly.pdf").exists()
    assert sorted(p.name for p in sub.iterdir()) == [".imaparc-manifest", "m-1.pdf"]


def test_store_sets_archive_permissions(tmp_path: Path) -> None:
    _store(
        tmp_path,
        "m-1",
        identity="id-1",
        primary_pdfa=b"F",
        mailonly_pdfa=b"M",
        originals=[("a.bin", b"x")],
    )
    assert _mode(tmp_path / "m-1") == 0o700
    assert _mode(tmp_path / "m-1" / "m-1.pdf") == 0o400
    assert _mode(tmp_path / "m-1" / "a.bin") == 0o400


def test_store_reserved_pdf_names_win_over_attachments(tmp_path: Path) -> None:
    # An attachment literally named like our reserved PDFs must not clobber them.
    _store(
        tmp_path,
        "m-1",
        identity="id-1",
        primary_pdfa=b"FULL",
        mailonly_pdfa=b"MAILONLY",
        originals=[("m-1_mailonly.pdf", b"ATTACH")],
    )
    sub = tmp_path / "m-1"
    assert (sub / "m-1_mailonly.pdf").read_bytes() == b"MAILONLY"
    assert (sub / "m-1_mailonly-2.pdf").read_bytes() == b"ATTACH"


def test_store_strips_path_traversal(tmp_path: Path) -> None:
    _store(
        tmp_path,
        "m-1",
        identity="id-1",
        primary_pdfa=b"F",
        mailonly_pdfa=b"M",
        originals=[("../../evil.sh", b"x")],
    )
    # The name is reduced to its basename; nothing escapes the subfolder.
    assert (tmp_path / "m-1" / "evil.sh").exists()
    assert not (tmp_path.parent / "evil.sh").exists()


def test_store_no_staging_left_behind(tmp_path: Path) -> None:
    _store(
        tmp_path,
        "m-1",
        identity="id-1",
        primary_pdfa=b"M",
        mailonly_pdfa=None,
        originals=[],
    )
    assert not any(p.name.startswith(".staging") for p in tmp_path.iterdir())


# --- _resolve_output (no browser) -------------------------------------------


def test_resolve_output_skips_fully_rendered_same_mail(tmp_path: Path) -> None:
    _store(
        tmp_path,
        "m",
        identity="id-a",
        primary_pdfa=b"M",
        mailonly_pdfa=None,
        originals=[],
    )
    name, skip = _resolve_output(tmp_path, "m", "id-a")
    assert (name, skip) == ("m", True)


def test_resolve_output_reserves_basename_for_concurrent_renders(
    tmp_path: Path,
) -> None:
    # Two distinct mails resolving to the same base (identical Date + subject)
    # must get distinct names when a shared `claimed` set is threaded through,
    # even though nothing has been written to disk yet.
    claimed: set[str] = set()
    first = _resolve_output(tmp_path, "m", "id-a", claimed)
    second = _resolve_output(tmp_path, "m", "id-b", claimed)
    assert first == ("m", False)
    assert second == ("m-2", False)
    assert claimed == {"m", "m-2"}


def test_resolve_output_disambiguates_different_mail(tmp_path: Path) -> None:
    _store(
        tmp_path,
        "m",
        identity="id-a",
        primary_pdfa=b"M",
        mailonly_pdfa=None,
        originals=[],
    )
    # A distinct mail that would collide on the basename gets its own slot.
    name, skip = _resolve_output(tmp_path, "m", "id-b")
    assert (name, skip) == ("m-2", False)


# --- end-to-end (gated) -----------------------------------------------------


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
async def test_render_mail_end_to_end(tmp_path: Path) -> None:
    raw = build_mail(
        subject="Vorgang",
        html="<p>HTML SENTINEL body</p>",
        text="TEXT SENTINEL body",
        attachments=[
            ("doc.pdf", "application/pdf", _pdf(2)),
            (
                "report.docx",
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document",
                b"PK\x03\x04 not really a docx",
            ),
        ],
    )
    parsed = parse_mail(raw)
    tools = ToolPaths.resolve()
    config = RunConfig(tools=tools)

    async with BrowserPool() as pool:
        result = await render_mail(
            parsed, profile="test", output_dir=tmp_path, pool=pool, config=config
        )

    assert result.written
    # The DOCX cannot become pages, so the mail is "not complete".
    assert result.complete is False
    outcomes = {a.filename: a.outcome.ok for a in result.attachments}
    assert outcomes["doc.pdf"] is True
    assert outcomes["report.docx"] is False

    # Output tree: a per-mail folder with the full PDF (<basename>.pdf), the
    # mail-only PDF (<basename>_mailonly.pdf), and both originals. Nothing loose
    # at the root.
    sub = tmp_path / result.basename
    assert not (tmp_path / f"{result.basename}.pdf").exists()
    assert result.combined_pdf == sub / f"{result.basename}.pdf"
    assert result.combined_pdf is not None and result.combined_pdf.exists()
    assert result.mail_only_pdf is not None
    assert result.mail_only_pdf == sub / f"{result.basename}_mailonly.pdf"
    assert result.mail_only_pdf.exists()
    assert (sub / "doc.pdf").exists()
    assert (sub / "report.docx").read_bytes() == b"PK\x03\x04 not really a docx"

    # Mail-only PDF is PDF/A-3b conformant (no foreign PDF merged in).
    assert run_verapdf(tools.verapdf, result.mail_only_pdf).compliant


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
async def test_render_mail_is_idempotent(tmp_path: Path) -> None:
    raw = build_mail(subject="Once", html="<p>x</p>", text="x")
    parsed = parse_mail(raw)
    config = RunConfig(tools=ToolPaths.resolve())
    async with BrowserPool() as pool:
        first = await render_mail(
            parsed, profile="p", output_dir=tmp_path, pool=pool, config=config
        )
        second = await render_mail(
            parsed, profile="p", output_dir=tmp_path, pool=pool, config=config
        )
    assert first.written and not first.skipped
    assert second.skipped and not second.written
    # No attachments → a folder holding only <basename>.pdf; the full and
    # mail-only paths coincide, and there is no separate _mailonly file.
    sub = tmp_path / first.basename
    assert first.combined_pdf == sub / f"{first.basename}.pdf"
    assert first.mail_only_pdf == sub / f"{first.basename}.pdf"
    assert not (sub / f"{first.basename}_mailonly.pdf").exists()
    assert sorted(p.name for p in sub.iterdir()) == [
        ".imaparc-manifest",
        f"{first.basename}.pdf",
    ]


def test_sweep_staging_removes_leftovers_only(tmp_path: Path) -> None:
    from imaparc.pipeline import sweep_staging
    from imaparc.storage import write_readonly

    out = tmp_path / "pdf"
    out.mkdir()
    # A leftover staging dir from an interrupted run (files are 0400).
    staging = out / ".staging-2026_mail"
    staging.mkdir()
    write_readonly(staging / "x.pdf", b"partial")
    # A real, completed mail folder must be left untouched.
    final = out / "2026_mail"
    final.mkdir()
    (final / "keep.pdf").write_bytes(b"done")

    sweep_staging(out)

    assert not staging.exists()  # leftover swept
    assert final.exists() and (final / "keep.pdf").exists()  # real folder kept


def test_sweep_staging_noop_when_dir_missing(tmp_path: Path) -> None:
    from imaparc.pipeline import sweep_staging

    sweep_staging(tmp_path / "does-not-exist")  # must not raise
