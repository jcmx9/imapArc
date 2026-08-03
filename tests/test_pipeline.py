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
    # must get distinct names when a shared `claimed` mapping is threaded
    # through, even though nothing has been written to disk yet.
    claimed: dict[str, str] = {}
    first = _resolve_output(tmp_path, "m", "id-a", claimed)
    second = _resolve_output(tmp_path, "m", "id-b", claimed)
    assert first == ("m", False)
    assert second == ("m-2", False)
    # The mapping records which mail holds which name — that is what lets a
    # second copy of one mail skip instead of taking yet another name.
    assert claimed == {"m": "id-a", "m-2": "id-b"}


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
            parsed,
            raw=raw,
            profile="test",
            output_dir=tmp_path,
            pool=pool,
            config=config,
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
            parsed, raw=raw, profile="p", output_dir=tmp_path, pool=pool, config=config
        )
        second = await render_mail(
            parsed, raw=raw, profile="p", output_dir=tmp_path, pool=pool, config=config
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
    """A missing output directory is not an error — and must not be created."""
    from imaparc.pipeline import sweep_staging

    missing = tmp_path / "does-not-exist"

    sweep_staging(missing)

    assert not missing.exists()


# --- attachment names must not escape the mail's folder ---------------------


@pytest.mark.parametrize(
    ("evil", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("/etc/passwd", "passwd"),
        ("a/../../b", "b"),
        # These two are the dangerous ones: Path("..").name is ".." — a name,
        # not a path component — so staging/.. would be the parent directory.
        ("..", "attachment"),
        (".", "attachment"),
        ("", "attachment"),
        ("   ", "attachment"),
        ("normal.pdf", "normal.pdf"),
    ],
)
def test_attachment_name_never_leaves_the_folder(evil: str, expected: str) -> None:
    from imaparc.pipeline import _safe_attachment_name

    assert _safe_attachment_name(evil) == expected


def test_attachment_name_cannot_reach_the_parent_directory(tmp_path: Path) -> None:
    """The concrete failure: joining the result must stay inside the folder."""
    from imaparc.pipeline import _safe_attachment_name

    folder = tmp_path / "staging"
    folder.mkdir()

    for evil in ("..", ".", "../..", "../"):
        target = (folder / _safe_attachment_name(evil)).resolve()
        assert target.parent == folder.resolve(), f"{evil!r} escaped to {target}"


# --- the same mail twice in one run must yield one folder -------------------


def test_same_mail_reserved_twice_in_one_run_is_skipped() -> None:
    """Two copies of one mail (same Message-ID) must not become two folders.

    `claimed` used to hold names only, so the second copy saw "name taken" and
    disambiguated to `-2` without ever consulting the identity — leaving a
    duplicate folder that a later run then skips forever.

    Real trigger: Gmail lists the same message in All Mail *and* in a label
    folder, with two UIDs, so a recursive scan delivers it twice.
    """
    from imaparc.pipeline import _resolve_output

    claimed: dict[str, str] = {}
    first, skip_first = _resolve_output(Path("/out"), "base", "<same@id>", claimed)
    second, skip_second = _resolve_output(Path("/out"), "base", "<same@id>", claimed)

    assert (first, skip_first) == ("base", False)
    assert skip_second is True, "the second copy must skip, not take another name"
    assert second == "base"


def test_a_different_mail_still_disambiguates() -> None:
    """The behaviour that must not regress: distinct mails keep distinct names."""
    from imaparc.pipeline import _resolve_output

    claimed: dict[str, str] = {}
    first, _ = _resolve_output(Path("/out"), "base", "<one@id>", claimed)
    second, skip = _resolve_output(Path("/out"), "base", "<other@id>", claimed)

    assert first == "base"
    assert (second, skip) == ("base-2", False)


# --- identity: content hash, with backward compatibility --------------------


def test_same_bytes_are_the_same_mail() -> None:
    from imaparc.pipeline import mail_identity, same_mail

    raw = b"From: a@b\r\nMessage-ID: <x@y>\r\nSubject: s\r\n\r\nbody"
    parsed = parse_mail(raw)

    identity = mail_identity(raw, parsed, None)

    assert same_mail(identity, identity)


def test_a_colliding_message_id_is_not_enough(tmp_path: Path) -> None:
    """Two different mails may carry one Message-ID — the sender picks it.

    `<1@localhost>` and friends come out of legacy systems and test tooling, and
    nothing stops a sender choosing any value. The content decides.
    """
    from imaparc.pipeline import mail_identity, same_mail

    one = b"Message-ID: <1@localhost>\r\nSubject: Rechnung\r\n\r\n100 Euro"
    two = b"Message-ID: <1@localhost>\r\nSubject: Rechnung\r\n\r\n5000 Euro"

    assert not same_mail(
        mail_identity(one, parse_mail(one), None),
        mail_identity(two, parse_mail(two), None),
    )


def test_a_manifest_written_before_the_hash_still_matches() -> None:
    """Old archives must keep working: their manifests hold only a Message-ID.

    Without this, the first render after upgrading would treat every stored mail
    as a different one and duplicate the entire archive into `…-2` folders.
    """
    from imaparc.pipeline import mail_identity, same_mail

    raw = b"Message-ID: <old@example.com>\r\nSubject: s\r\n\r\nbody"
    legacy_manifest = "<old@example.com>"  # what pre-26.8.12 wrote

    assert same_mail(legacy_manifest, mail_identity(raw, parse_mail(raw), None))


def test_a_mail_without_a_message_id_still_has_an_identity() -> None:
    from imaparc.pipeline import mail_identity, same_mail

    raw = b"From: a@b\r\nSubject: no id\r\n\r\nbody"
    identity = mail_identity(raw, parse_mail(raw), None)

    assert same_mail(identity, identity)
    assert "sha256:" in identity
