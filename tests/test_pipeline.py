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
from imaparc.pdf import provenance
from imaparc.pipeline import ArchiveIndex, _resolve_output, _store, render_mail
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


def test_store_writes_one_pdf_and_the_originals(tmp_path: Path) -> None:
    _store(
        tmp_path,
        "m-1",
        identity="id-1",
        pdfa=b"FULL",
        originals=[("a.pdf", b"x"), ("a.pdf", b"y")],
    )
    sub = tmp_path / "m-1"
    # No loose PDF at the root — the mail's PDF lives only inside its folder.
    assert not (tmp_path / "m-1.pdf").exists()
    assert (sub / "m-1.pdf").read_bytes() == b"FULL"
    # Two same-named attachments: the second is disambiguated, neither lost.
    assert (sub / "a.pdf").read_bytes() == b"x"
    assert (sub / "a-2.pdf").read_bytes() == b"y"
    assert sorted(p.name for p in sub.iterdir()) == [
        ".imaparc-manifest",
        "a-2.pdf",
        "a.pdf",
        "m-1.pdf",
    ]


def test_store_sets_archive_permissions(tmp_path: Path) -> None:
    _store(
        tmp_path,
        "m-1",
        identity="id-1",
        pdfa=b"F",
        originals=[("a.bin", b"x")],
    )
    assert _mode(tmp_path / "m-1") == 0o700
    assert _mode(tmp_path / "m-1" / "m-1.pdf") == 0o400
    assert _mode(tmp_path / "m-1" / "a.bin") == 0o400


def test_store_reserved_pdf_name_wins_over_attachments(tmp_path: Path) -> None:
    # An attachment literally named like the mail's PDF must not clobber it.
    _store(
        tmp_path,
        "m-1",
        identity="id-1",
        pdfa=b"FULL",
        originals=[("m-1.pdf", b"ATTACH")],
    )
    sub = tmp_path / "m-1"
    assert (sub / "m-1.pdf").read_bytes() == b"FULL"
    assert (sub / "m-1-2.pdf").read_bytes() == b"ATTACH"


def test_store_strips_path_traversal(tmp_path: Path) -> None:
    _store(
        tmp_path,
        "m-1",
        identity="id-1",
        pdfa=b"F",
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
        pdfa=b"M",
        originals=[],
    )
    assert not any(p.name.startswith(".staging") for p in tmp_path.iterdir())


# --- _resolve_output (no browser) -------------------------------------------


def test_resolve_output_skips_fully_rendered_same_mail(tmp_path: Path) -> None:
    _store(
        tmp_path,
        "m",
        identity="id-a",
        pdfa=b"M",
        originals=[],
    )
    name, skip = _resolve_output(tmp_path, "m", "id-a")
    assert (name, skip) == ("m", True)


def _archived(tmp_path: Path, name: str, identity: str) -> None:
    _store(
        tmp_path,
        name,
        identity=identity,
        pdfa=b"M",
        originals=[],
    )


def test_resolve_output_finds_the_same_mail_under_another_name(
    tmp_path: Path,
) -> None:
    """A changed naming scheme must not render the whole archive a second time."""
    _archived(tmp_path, "old_name.", "<a@x>\nsha256:aaa")

    name, skip = _resolve_output(
        tmp_path,
        "new_name",
        "<a@x>\nsha256:aaa",
        existing=ArchiveIndex.scan(tmp_path),
    )

    assert (name, skip) == ("old_name.", True)


def test_resolve_output_finds_a_legacy_folder_by_message_id(tmp_path: Path) -> None:
    _archived(tmp_path, "legacy", "<a@x>")  # manifest from before 26.8.12

    name, skip = _resolve_output(
        tmp_path, "new_name", "<a@x>\nsha256:aaa", existing=ArchiveIndex.scan(tmp_path)
    )

    assert (name, skip) == ("legacy", True)


def test_resolve_output_does_not_trust_a_shared_message_id_over_the_hash(
    tmp_path: Path,
) -> None:
    # Same sender-chosen Message-ID, different bytes: a different mail.
    _archived(tmp_path, "other", "<1@localhost>\nsha256:bbb")

    name, skip = _resolve_output(
        tmp_path,
        "new_name",
        "<1@localhost>\nsha256:aaa",
        existing=ArchiveIndex.scan(tmp_path),
    )

    assert (name, skip) == ("new_name", False)


def test_archive_index_ignores_staging_and_foreign_folders(tmp_path: Path) -> None:
    (tmp_path / ".staging-x").mkdir()
    (tmp_path / ".staging-x" / ".imaparc-manifest").write_text("<a@x>\nsha256:aaa")
    (tmp_path / "my own folder").mkdir()  # a user's folder, no manifest

    index = ArchiveIndex.scan(tmp_path)

    assert index.find("<a@x>\nsha256:aaa") is None


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
        pdfa=b"M",
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

    # Output tree: a per-mail folder with the one PDF (<basename>.pdf) and both
    # originals. Nothing loose at the root, no _mailonly.pdf any more.
    sub = tmp_path / result.basename
    assert not (tmp_path / f"{result.basename}.pdf").exists()
    assert result.combined_pdf == sub / f"{result.basename}.pdf"
    assert result.combined_pdf is not None and result.combined_pdf.exists()
    assert not (sub / f"{result.basename}_mailonly.pdf").exists()
    assert (sub / "doc.pdf").exists()
    assert (sub / "report.docx").read_bytes() == b"PK\x03\x04 not really a docx"

    # The PDF names its mail by the manifest's hash and carries the .eml.
    manifest_hash = (sub / ".imaparc-manifest").read_text().splitlines()[1]
    recorded = provenance.read(result.combined_pdf.read_bytes())
    assert recorded is not None
    assert f"sha256:{recorded.sha256}" == manifest_hash
    assert recorded.message_id == parsed.headers.message_id
    assert provenance.embedded_source(result.combined_pdf.read_bytes()) == raw


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
    # No attachments → a folder holding only <basename>.pdf.
    sub = tmp_path / first.basename
    assert first.combined_pdf == sub / f"{first.basename}.pdf"
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
