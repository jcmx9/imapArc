"""Tests for the render pipeline (assembly, storage, end-to-end)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path

import pikepdf
import pytest

from imaparc.config import RunConfig, ToolPaths
from imaparc.mail.parser import parse_mail
from imaparc.pdf import provenance
from imaparc.pdf.validate import run_verapdf
from imaparc.pipeline import ArchiveIndex, _resolve_output, is_rendered, render_mail
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


# --- archive identity and _resolve_output (no browser) -----------------------


def _flat(output: Path, name: str, raw: bytes, message_id: str | None) -> str:
    """A rendered mail as written since 26.11.6; returns its identity."""
    output.mkdir(parents=True, exist_ok=True)
    stamped = provenance.stamp(_pdf(), raw, message_id=message_id)
    (output / f"{name}.pdf").write_bytes(stamped)
    lines = [message_id] if message_id else []
    return "\n".join([*lines, f"sha256:{hashlib.sha256(raw).hexdigest()}"])


def _legacy(base: Path, name: str, identity: str) -> None:
    """A per-mail folder as written before 26.11.6."""
    folder = base / name
    folder.mkdir(parents=True)
    (folder / ".imaparc-manifest").write_text(identity, encoding="utf-8")
    (folder / f"{name}.pdf").write_bytes(_pdf())


def test_resolve_output_skips_a_rendered_same_mail(tmp_path: Path) -> None:
    identity = _flat(tmp_path, "m", b"RAW", "<a@x>")
    assert _resolve_output(tmp_path, "m", identity) == ("m", True)


def test_resolve_output_disambiguates_different_mail(tmp_path: Path) -> None:
    _flat(tmp_path, "m", b"RAW", "<a@x>")
    other = f"<b@x>\nsha256:{hashlib.sha256(b'OTHER').hexdigest()}"
    assert _resolve_output(tmp_path, "m", other) == ("m-2", False)


def test_a_foreign_pdf_under_the_name_is_never_overwritten(tmp_path: Path) -> None:
    (tmp_path / "m.pdf").write_bytes(_pdf())  # records no mail
    assert _resolve_output(tmp_path, "m", "<a@x>\nsha256:aaa") == ("m-2", False)


def test_resolve_output_finds_the_same_mail_under_another_name(
    tmp_path: Path,
) -> None:
    """A changed naming scheme must not render the whole archive a second time."""
    identity = _flat(tmp_path, "old_name", b"RAW", "<a@x>")

    found = _resolve_output(
        tmp_path, "new_name", identity, existing=ArchiveIndex.scan(tmp_path)
    )

    assert found == ("old_name", True)


@pytest.mark.parametrize("base", [".", "pdf"])
def test_legacy_folders_are_still_recognised(tmp_path: Path, base: str) -> None:
    """Folders from before 26.11.6, beside the PDFs or in the old pdf/ subdir."""
    _legacy(tmp_path / base, "legacy", "<a@x>\nsha256:aaa")

    index = ArchiveIndex.scan(tmp_path)

    assert index.find("<a@x>\nsha256:aaa") == "legacy"
    assert is_rendered(tmp_path, "legacy")


def test_resolve_output_finds_a_legacy_folder_by_message_id(tmp_path: Path) -> None:
    _legacy(tmp_path, "legacy", "<a@x>")  # manifest from before 26.8.12

    name, skip = _resolve_output(
        tmp_path, "new_name", "<a@x>\nsha256:aaa", existing=ArchiveIndex.scan(tmp_path)
    )

    assert (name, skip) == ("legacy", True)


def test_resolve_output_does_not_trust_a_shared_message_id_over_the_hash(
    tmp_path: Path,
) -> None:
    # Same sender-chosen Message-ID, different bytes: a different mail.
    _flat(tmp_path, "other", b"OTHER", "<1@localhost>")

    name, skip = _resolve_output(
        tmp_path,
        "new_name",
        "<1@localhost>\nsha256:aaa",
        existing=ArchiveIndex.scan(tmp_path),
    )

    assert (name, skip) == ("new_name", False)


def test_archive_index_ignores_staging_and_foreign_entries(tmp_path: Path) -> None:
    (tmp_path / ".staging-x").mkdir()
    (tmp_path / ".staging-x" / ".imaparc-manifest").write_text("<a@x>\nsha256:aaa")
    (tmp_path / "my own folder").mkdir()  # a user's folder, no manifest
    (tmp_path / "scan.pdf").write_bytes(_pdf())  # a PDF recording no mail

    index = ArchiveIndex.scan(tmp_path)

    assert index.find("<a@x>\nsha256:aaa") is None
    assert index.by_hash == {}


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


def test_sweep_removes_temp_files_and_staging_folders(tmp_path: Path) -> None:
    from imaparc.pipeline import sweep_staging

    (tmp_path / ".tmp-abc").write_bytes(b"half")
    (tmp_path / ".staging-x").mkdir()
    (tmp_path / "keep.pdf").write_bytes(b"x")

    sweep_staging(tmp_path)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep.pdf"]


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

    # Output: one file, <basename>.pdf, directly in the output directory.
    pdf = tmp_path / f"{result.basename}.pdf"
    assert result.combined_pdf == pdf
    assert sorted(p.name for p in tmp_path.iterdir()) == [pdf.name]
    data = pdf.read_bytes()

    # The PDF names its mail, carries the .eml and the attachment that did not
    # become pages (the PDF attachment did, so it is not embedded again).
    recorded = provenance.read(data)
    assert recorded is not None
    assert recorded.sha256 == hashlib.sha256(raw).hexdigest()
    assert recorded.message_id == parsed.headers.message_id
    files = provenance.embedded_files(data)
    assert files[f"{result.basename}.eml"] == ("Source", raw)
    assert files["report.docx"] == ("Data", b"PK\x03\x04 not really a docx")
    assert "doc.pdf" not in files

    # Bookmarks: the mail, then one per attachment, on their pages.
    with pikepdf.open(pdf) as doc, doc.open_outline() as outline:
        titles = [item.title for item in outline.root]
    assert titles[0] == "E-Mail"
    assert titles[-2:] == ["Anhang 1: doc.pdf", "Anhang 2: report.docx"]

    assert run_verapdf(tools.verapdf, pdf).compliant


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
    assert second.basename == first.basename
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"{first.basename}.pdf"]


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
