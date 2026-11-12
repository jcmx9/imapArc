"""Tests for scripts/rerender_check.py and the two shell wrappers around it.

The scripts let a user rebuild an archive's PDFs with a newer imapArc: list the
folders whose mail still exists as .eml, move or delete them, then
``imaparc render``. A folder must only ever be touched when its .eml provably
exists — by the manifest's content hash, or for older manifests by the PDF's
own text — and never by matching names.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pikepdf
import pytest

from tests.mail_builder import build_mail

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_LIST = _SCRIPTS / "rerender-list.sh"
_APPLY = _SCRIPTS / "rerender-apply.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("pdftotext") is None, reason="scripts need pdftotext"
)

_DATE = "Mon, 23 Mar 2026 02:18:04 +0100"
_UV_CACHE = Path(
    subprocess.run(
        ["uv", "cache", "dir"], capture_output=True, text=True, check=False
    ).stdout.strip()
    or Path.home() / ".cache" / "uv"
)


def _load_check() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "rerender_check", _SCRIPTS / "rerender_check.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # @dataclass resolves its module through here
    spec.loader.exec_module(module)
    return module


check = _load_check()


@pytest.fixture(autouse=True)
def _restore_perms(tmp_path: Path) -> Iterator[None]:
    yield
    for entry in sorted(tmp_path.rglob("*"), reverse=True):
        with contextlib.suppress(OSError):
            entry.chmod(0o700)


def _pdf(lines: list[str]) -> bytes:
    """A one-page PDF whose text layer holds ``lines`` (ASCII only)."""
    pdf = pikepdf.new()
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.Type1,
            BaseFont=pikepdf.Name.Helvetica,
        )
    )
    ops = ["BT /F1 10 Tf 14 TL 50 800 Td"]
    ops += [f"({line}) Tj T*" for line in lines]
    ops.append("ET")
    page = pikepdf.Dictionary(
        Type=pikepdf.Name.Page,
        MediaBox=[0, 0, 595, 842],
        Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font)),
        Contents=pdf.make_stream("\n".join(ops).encode("latin-1")),
    )
    pdf.pages.append(pikepdf.Page(page))
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def _shown(subject: str, body: str) -> bytes:
    """The PDF imapArc would have rendered for build_mail(subject, text=body)."""
    return _pdf(
        [
            "Von: sender@example.com",
            "An: me@example.com",
            "Datum: 23.03.2026 02:18",
            f"Betreff: {subject}",
            body,
        ]
    )


def _folder(pdf_dir: Path, name: str, manifest: str | None, pdf: bytes) -> Path:
    """A rendered-mail folder with archive permissions (files 0400, dir 0700)."""
    folder = pdf_dir / name
    folder.mkdir(parents=True)
    (folder / f"{name}.pdf").write_bytes(pdf)
    if manifest is not None:
        (folder / ".imaparc-manifest").write_text(manifest, encoding="utf-8")
    for entry in folder.iterdir():
        entry.chmod(0o400)
    folder.chmod(0o700)
    return folder


@pytest.fixture
def archive(tmp_path: Path) -> dict[str, Path]:
    """One profile holding every case the check distinguishes."""
    root = tmp_path / "archive"
    eml, pdf = root / "P" / "eml", root / "P" / "pdf"
    eml.mkdir(parents=True)

    hashed = build_mail(subject="Hashed", message_id="<h@x>", date=_DATE)
    # Suffixes differ on purpose: pairing by name would miss these.
    (eml / "H-2.eml").write_bytes(hashed)
    (eml / "O.eml").write_bytes(
        build_mail(
            subject="Old mail", message_id="<o@x>", date=_DATE, text="Opening line"
        )
    )
    # A second, different mail with the same (sender-chosen) Message-ID.
    (eml / "O-2.eml").write_bytes(
        build_mail(subject="Other", message_id="<o@x>", date=_DATE, text="Else")
    )
    (eml / "C.eml").write_bytes(
        build_mail(subject="Collide", message_id="<c@x>", date=_DATE, text="Real")
    )

    sha = hashlib.sha256(hashed).hexdigest()
    gone = hashlib.sha256(b"no longer on disk").hexdigest()
    return {
        "root": root,
        "eml": eml,
        "hash": _folder(pdf, "H-3", f"<h@x>\nsha256:{sha}", _shown("Hashed", "x")),
        "hash_gone": _folder(pdf, "G", f"<g@x>\nsha256:{gone}", _shown("G", "x")),
        "content": _folder(pdf, "O-5", "<o@x>", _shown("Old mail", "Opening line")),
        "mismatch": _folder(pdf, "C", "<c@x>", _shown("Collide", "Something else")),
        "no_eml": _folder(pdf, "N", "<n@x>", _shown("N", "x")),
        "no_text": _folder(pdf, "T", "<o@x>", _pdf([])),
        "no_manifest": _folder(pdf, "W", None, _shown("W", "x")),
        "staging": _folder(pdf, ".staging-V", f"<h@x>\nsha256:{sha}", _pdf([])),
    }


_EXPECTED = {
    "hash": "hash",
    "hash_gone": "no-eml",
    "content": "content",
    "mismatch": "content-mismatch",
    "no_eml": "no-eml",
    "no_text": "no-pdf-text",
    "no_manifest": "no-manifest",
}


@pytest.mark.parametrize(("case", "expected"), sorted(_EXPECTED.items()))
def test_verdict(archive: dict[str, Path], case: str, expected: str) -> None:
    mails = check._Mails.load(list(archive["eml"].glob("*.eml")))
    assert check.verdict(archive[case], mails)[0] == expected


def test_verdict_names_the_proving_eml(archive: dict[str, Path]) -> None:
    mails = check._Mails.load(list(archive["eml"].glob("*.eml")))
    assert check.verdict(archive["hash"], mails)[1] == archive["eml"] / "H-2.eml"
    assert check.verdict(archive["content"], mails)[1] == archive["eml"] / "O.eml"


def test_verdict_survives_dropped_ligatures() -> None:
    """pdftotext may drop an "fi" ligature entirely; that must not cause a miss."""
    mail = check.parse_mail(
        build_mail(subject="Termin findet statt", date=_DATE, text="stattfindet")
    )
    text = check._norm(
        "sender@example.com 23.03.2026 02:18 Termin ndet statt statt ndet"
    )
    assert check._shown_in(mail, text)


def test_a_mail_with_almost_nothing_to_compare_is_never_accepted() -> None:
    mail = check.parse_mail(
        build_mail(from_="", subject="", date=None, text=None, html=None)
    )
    assert not check._shown_in(mail, check._norm("anything at all"))


# --- folders written by `imaparc eml` (the .eml lives inside) ---------------


@pytest.fixture
def loose(tmp_path: Path) -> dict[str, Path]:
    """A user directory holding `imaparc eml` output beside unrelated files."""
    directory = tmp_path / "Desktop" / "001"
    directory.mkdir(parents=True)
    (directory / "notes.pdf").write_bytes(b"%PDF-1.7")  # the user's own file
    raw = build_mail(subject="Loose", message_id="<l@x>", date=_DATE)
    sha = hashlib.sha256(raw).hexdigest()
    name = "2026-03-23_02-18-04_mail_Loose."
    folder = _folder(directory, name, f"<l@x>\nsha256:{sha}", _shown("Loose", "x"))
    folder.chmod(0o700)
    (folder / f"{name}.eml").write_bytes(raw)
    (folder / f"{name}.eml").chmod(0o400)
    gone = _folder(directory, "G", "<g@x>\nsha256:" + "0" * 64, _shown("G", "x"))
    return {"dir": directory, "folder": folder, "gone": gone}


def test_eml_folder_is_proven_by_the_eml_inside(loose: dict[str, Path]) -> None:
    mails = check._Mails.load(list(loose["folder"].glob("*.eml")))
    result, eml = check.verdict(loose["folder"], mails)
    assert result == "hash"
    assert eml is not None and eml.parent == loose["folder"]


# --- the flat layout (since 26.11.6) ------------------------------------------


@pytest.fixture
def flat(tmp_path: Path) -> dict[str, Path]:
    """A flat profile: <root>/F/<mail>.pdf, the .eml in <root>/eml-Archiv/F/."""
    from imaparc.pdf import provenance

    root = tmp_path / "flatroot"
    profile, archive = root / "F", root / "eml-Archiv" / "F"
    profile.mkdir(parents=True)
    archive.mkdir(parents=True)
    raw = build_mail(subject="Flat", message_id="<f@x>", date=_DATE)
    (archive / "drifted-name.eml").write_bytes(raw)
    proven = profile / "M.pdf"
    proven.write_bytes(provenance.stamp(_pdf([]), raw, message_id="<f@x>"))
    gone = profile / "G.pdf"
    gone.write_bytes(provenance.stamp(_pdf([]), b"gone", message_id=None))
    foreign = profile / "scan.pdf"
    foreign.write_bytes(_pdf(["not imapArc's"]))
    return {"root": root, "proven": proven, "gone": gone, "foreign": foreign}


def test_flat_pdfs_are_proven_by_their_recorded_hash(flat: dict[str, Path]) -> None:
    mails = check._Mails.load(check._profile_emls(flat["proven"].parent))
    assert check.verdict_pdf(flat["proven"], mails)[0] == "hash"
    assert check.verdict_pdf(flat["gone"], mails) == ("no-eml", None)
    assert check.verdict_pdf(flat["foreign"], mails) == ("no-identity", None)


def test_discover_finds_a_flat_archive(flat: dict[str, Path], tmp_path: Path) -> None:
    found, loose_emls = check.discover(tmp_path, [], tmp_path / "none.yaml")

    assert [(a.kind, a.path) for a in found] == [("flat", flat["root"] / "F")]
    assert loose_emls == []  # the eml-Archiv is never loose


def test_apply_sets_flat_pdfs_aside(flat: dict[str, Path], tmp_path: Path) -> None:
    out = tmp_path / "out"
    rerender = _list(tmp_path, out)
    assert rerender == [str(flat["proven"])]
    target = flat["root"] / "pdf-Löscharchiv"

    result = _run(
        _APPLY, out / "rerender.txt", "--move-to", target, "--yes", home=tmp_path
    )

    assert result.returncode == 0, result.stderr
    assert (target / "flatroot" / "F" / "M.pdf").is_file()
    assert not flat["proven"].exists()
    assert flat["gone"].exists() and flat["foreign"].exists()
    assert (out / "applied.tsv").read_text() == f"flat\t{flat['proven']}\n"


# --- discovery -------------------------------------------------------------


def test_discover_finds_both_kinds_and_honours_excludes(
    archive: dict[str, Path], loose: dict[str, Path], tmp_path: Path
) -> None:
    moved = tmp_path / "alt" / "P"
    _folder(moved, "H-9", "<h@x>", _pdf([]))  # a previous --move-to target
    hidden = tmp_path / ".cache" / "x"
    _folder(hidden, "Y", "<y@x>", _pdf([]))

    found, _ = check.discover(tmp_path, [tmp_path / "alt"], tmp_path / "none.yaml")

    assert {(a.kind, a.path, a.name) for a in found} == {
        ("profile", archive["root"] / "P", "P"),
        ("eml", loose["dir"], "mail"),
    }


def test_discover_lists_loose_eml_but_not_inboxes_folders_or_archives(
    archive: dict[str, Path], loose: dict[str, Path], tmp_path: Path
) -> None:
    stray = tmp_path / "Downloads" / "export"
    stray.mkdir(parents=True)
    (stray / "a.eml").write_bytes(b"x")
    filed = tmp_path / "archive" / "eml-Archiv" / "P"
    filed.mkdir(parents=True)
    (filed / "done.eml").write_bytes(b"x")
    set_aside = tmp_path / "archive" / "pdf-Löscharchiv" / "x"
    _folder(set_aside, "Z", "<z@x>", _pdf([]))
    (set_aside / "z.eml").write_bytes(b"x")
    config = tmp_path / "profile.yaml"
    config.write_text(
        "profiles:\n"
        "  - name: P\n    account: a\n"
        f"    output: {archive['root'] / 'P'}\n",
        encoding="utf-8",
    )

    found, loose_emls = check.discover(tmp_path, [], config)

    # P's inbox belongs to `imaparc render`; the mail folders' own .eml, the
    # eml-Archiv and the pdf-Löscharchiv are never loose.
    assert loose_emls == [stray / "a.eml"]
    assert all("pdf-Löscharchiv" not in str(a.path) for a in found)


def test_unconfigured_inbox_is_loose(archive: dict[str, Path], tmp_path: Path) -> None:
    _, loose_emls = check.discover(tmp_path, [], tmp_path / "none.yaml")
    assert sorted(loose_emls) == sorted(archive["eml"].glob("*.eml"))


def test_a_folder_is_proven_by_the_eml_archive(
    archive: dict[str, Path], tmp_path: Path
) -> None:
    """Once rendered, the .eml lives in eml-Archiv/<profile>/ — still proof."""
    filed = archive["root"] / "eml-Archiv" / "P"
    filed.mkdir(parents=True)
    (archive["eml"] / "H-2.eml").rename(filed / "H-2.eml")

    mails = check._Mails.load(check._profile_emls(archive["root"] / "P"))

    assert check.verdict(archive["hash"], mails) == ("hash", filed / "H-2.eml")


# --- the shell wrappers, end to end ---------------------------------------


def _run(
    *args: str | Path, home: Path, stdin: str = ""
) -> subprocess.CompletedProcess[str]:
    # The environment is already synced for this test run; letting every
    # parallel worker's `uv run` re-sync it serialises them on uv's lock.
    return subprocess.run(
        ["bash", *map(str, args)],
        capture_output=True,
        text=True,
        input=stdin,
        check=False,
        env={
            **os.environ,
            "UV_NO_SYNC": "1",
            # uv keeps its cache under $HOME; leave it where it already is.
            "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", str(_UV_CACHE)),
            "HOME": str(home),
        },
    )


def _list(root: Path, out: Path, *extra: str | Path) -> list[str]:
    result = _run(
        _LIST, root, out, "--config", out.parent / "none.yaml", *extra, home=root
    )
    assert result.returncode == 0, result.stderr
    return (out / "rerender.txt").read_text().splitlines()


@pytest.fixture
def home(archive: dict[str, Path], loose: dict[str, Path], tmp_path: Path) -> Path:
    """A home directory holding one profile archive and one `imaparc eml` dir."""
    return tmp_path


def test_list_selects_exactly_the_provable_folders(
    archive: dict[str, Path], loose: dict[str, Path], home: Path
) -> None:
    rerender = _list(home, home / "out")

    assert sorted(rerender) == sorted(
        str(p) for p in (archive["hash"], archive["content"], loose["folder"])
    )
    keep = (home / "out" / "keep.txt").read_text()
    assert ".staging-V" not in keep + "\n".join(rerender)
    assert str(loose["gone"]) in keep


def test_list_writes_nothing_outside_its_out_dir(home: Path) -> None:
    before = sorted(home.rglob("*"))
    _list(home, home / "out")
    after = [p for p in sorted(home.rglob("*")) if not p.is_relative_to(home / "out")]
    assert before == after


def test_apply_moves_folders_below_target_mirroring_home(
    archive: dict[str, Path], loose: dict[str, Path], home: Path
) -> None:
    out = home / "out"
    _list(home, out)
    target = home / "old"

    result = _run(_APPLY, out / "rerender.txt", "--move-to", target, "--yes", home=home)

    assert result.returncode == 0, result.stderr
    assert (target / "archive" / "P" / "pdf" / "H-3" / "H-3.pdf").is_file()
    assert not archive["hash"].exists()
    assert not archive["content"].exists()
    for kept in ("hash_gone", "mismatch", "no_eml", "no_text", "no_manifest"):
        assert archive[kept].exists()
    assert len(list(archive["eml"].glob("*.eml"))) == 4
    # The `imaparc eml` folder's .eml was taken out first and survives beside it.
    name = loose["folder"].name
    assert (loose["dir"] / f"{name}.eml").is_file()
    assert not (target / "Desktop" / "001" / name / f"{name}.eml").exists()
    assert (target / "Desktop" / "001" / name / f"{name}.pdf").is_file()
    assert (loose["dir"] / "notes.pdf").is_file()


def test_apply_writes_collect_and_render_scripts(
    loose: dict[str, Path], home: Path
) -> None:
    out = home / "out"
    _list(home, out)

    result = _run(_APPLY, out / "rerender.txt", "--delete", "--yes", home=home)

    assert result.returncode == 0, result.stderr
    assert "collect.sh" in result.stdout
    inbox = home / "imapArc" / "emlinput"
    assert f" collect {out} {inbox}" in (out / "collect.sh").read_text()
    render = (out / "render.sh").read_text()
    assert "\nimaparc render\n" in render
    assert f"imaparc eml {inbox}" in render


def test_collect_gathers_every_pending_eml_and_can_run_twice(
    archive: dict[str, Path], loose: dict[str, Path], home: Path
) -> None:
    out = home / "out"
    _list(home, out)
    _run(_APPLY, out / "rerender.txt", "--delete", "--yes", home=home)
    extracted = loose["dir"] / f"{loose['folder'].name}.eml"
    inbox_emls = sorted(archive["eml"].glob("*.eml"))  # unconfigured: loose
    expected = {p.read_bytes() for p in [extracted, *inbox_emls]}

    for _ in range(2):
        result = _run(out / "collect.sh", home=home)
        assert result.returncode == 0, result.stderr

    inbox = home / "imapArc" / "emlinput"
    collected = sorted(inbox.glob("*.eml"))
    assert {p.read_bytes() for p in collected} == expected
    assert not extracted.exists()
    assert not any(p.exists() for p in inbox_emls)
    # One log line per moved file: where it came from, what it is called now.
    log = (inbox / "emlinput.log").read_text().splitlines()
    assert len(log) == len(expected)
    assert f"{extracted}\t{extracted.name}" in log


def test_apply_deletes_listed_folders(archive: dict[str, Path], home: Path) -> None:
    out = home / "out"
    _list(home, out)

    result = _run(_APPLY, out / "rerender.txt", "--delete", "--yes", home=home)

    assert result.returncode == 0, result.stderr
    assert not archive["hash"].exists()
    assert archive["mismatch"].exists()


def test_apply_rechecks_a_stale_list(archive: dict[str, Path], home: Path) -> None:
    """A folder listed earlier whose .eml has since gone must stay."""
    out = home / "out"
    _list(home, out)
    (archive["eml"] / "H-2.eml").unlink()

    result = _run(_APPLY, out / "rerender.txt", "--delete", "--yes", home=home)

    assert result.returncode == 1
    assert "no-eml" in result.stderr
    assert archive["hash"].exists()
    assert not archive["content"].exists()  # still provable, so still applied


def test_apply_refuses_folders_the_list_would_keep(
    archive: dict[str, Path], home: Path
) -> None:
    kept = ("hash_gone", "mismatch", "no_eml", "no_text", "no_manifest")
    hand_made = home / "hand.txt"
    hand_made.write_text("\n".join(str(archive[k]) for k in kept) + "\n")

    result = _run(_APPLY, hand_made, "--delete", "--yes", home=home)

    assert result.returncode == 1
    for k in kept:
        assert archive[k].exists()


def test_apply_refuses_a_move_target_inside_an_archive(
    archive: dict[str, Path], home: Path
) -> None:
    out = home / "out"
    _list(home, out)

    result = _run(
        _APPLY,
        out / "rerender.txt",
        "--move-to",
        archive["root"] / "P" / "old",
        "--yes",
        home=home,
    )

    assert result.returncode == 1
    assert "inside the archive" in result.stderr
    assert archive["hash"].exists()
    assert not (archive["root"] / "P" / "old").exists()  # nothing created either


def test_apply_may_set_aside_into_the_archive_root(
    archive: dict[str, Path], home: Path
) -> None:
    """~/imapArc/pdf-Löscharchiv sits beside the profiles and is never listed."""
    out = home / "out"
    _list(home, out)
    target = archive["root"] / "pdf-Löscharchiv"

    result = _run(_APPLY, out / "rerender.txt", "--move-to", target, "--yes", home=home)

    assert result.returncode == 0, result.stderr
    assert (target / "archive" / "P" / "pdf" / "H-3" / "H-3.pdf").is_file()
    # A second listing does not offer the set-aside folders again.
    assert all("pdf-Löscharchiv" not in line for line in _list(home, home / "out2"))


def test_apply_without_confirmation_touches_nothing(
    archive: dict[str, Path], home: Path
) -> None:
    out = home / "out"
    _list(home, out)

    result = _run(_APPLY, out / "rerender.txt", "--delete", stdin="no\n", home=home)

    assert result.returncode == 1
    assert archive["hash"].exists()
