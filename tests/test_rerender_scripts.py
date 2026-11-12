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
    profile = check._Profile.load(archive["eml"])
    assert check.verdict(archive[case], profile) == expected


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


# --- the shell wrappers, end to end ---------------------------------------


def _run(*args: str | Path, stdin: str = "") -> subprocess.CompletedProcess[str]:
    # The environment is already synced for this test run; letting every
    # parallel worker's `uv run` re-sync it serialises them on uv's lock.
    return subprocess.run(
        ["bash", *map(str, args)],
        capture_output=True,
        text=True,
        input=stdin,
        check=False,
        env={**os.environ, "UV_NO_SYNC": "1"},
    )


def _list(archive: dict[str, Path], out: Path) -> list[str]:
    result = _run(_LIST, archive["root"], out)
    assert result.returncode == 0, result.stderr
    return (out / "rerender.txt").read_text().splitlines()


def test_list_selects_exactly_the_provable_folders(
    archive: dict[str, Path], tmp_path: Path
) -> None:
    rerender = _list(archive, tmp_path / "out")

    assert sorted(rerender) == sorted([str(archive["hash"]), str(archive["content"])])
    keep = (tmp_path / "out" / "keep.txt").read_text()
    assert ".staging-V" not in keep + "\n".join(rerender)


def test_list_writes_nothing_into_the_archive(
    archive: dict[str, Path], tmp_path: Path
) -> None:
    before = sorted(p.relative_to(archive["root"]) for p in archive["root"].rglob("*"))
    _list(archive, tmp_path / "out")
    after = sorted(p.relative_to(archive["root"]) for p in archive["root"].rglob("*"))
    assert before == after


def test_apply_moves_listed_folders_out_of_the_archive(
    archive: dict[str, Path], tmp_path: Path
) -> None:
    out = tmp_path / "out"
    _list(archive, out)
    target = tmp_path / "old"

    result = _run(_APPLY, out / "rerender.txt", "--move-to", target, "--yes")

    assert result.returncode == 0, result.stderr
    assert not archive["hash"].exists()
    assert not archive["content"].exists()
    assert (target / "P" / "H-3" / "H-3.pdf").is_file()
    for kept in ("hash_gone", "mismatch", "no_eml", "no_text", "no_manifest"):
        assert archive[kept].exists()
    assert len(list(archive["eml"].glob("*.eml"))) == 4


def test_apply_deletes_listed_folders(archive: dict[str, Path], tmp_path: Path) -> None:
    out = tmp_path / "out"
    _list(archive, out)

    result = _run(_APPLY, out / "rerender.txt", "--delete", "--yes")

    assert result.returncode == 0, result.stderr
    assert not archive["hash"].exists()
    assert archive["mismatch"].exists()


def test_apply_rechecks_a_stale_list(archive: dict[str, Path], tmp_path: Path) -> None:
    """A folder listed earlier whose .eml has since gone must stay."""
    out = tmp_path / "out"
    _list(archive, out)
    (archive["eml"] / "H-2.eml").unlink()

    result = _run(_APPLY, out / "rerender.txt", "--delete", "--yes")

    assert result.returncode == 1
    assert "no-eml" in result.stderr
    assert archive["hash"].exists()
    assert not archive["content"].exists()  # still provable, so still applied


def test_apply_refuses_folders_the_list_would_keep(
    archive: dict[str, Path], tmp_path: Path
) -> None:
    kept = ("hash_gone", "mismatch", "no_eml", "no_text", "no_manifest")
    hand_made = tmp_path / "hand.txt"
    hand_made.write_text("\n".join(str(archive[k]) for k in kept) + "\n")

    result = _run(_APPLY, hand_made, "--delete", "--yes")

    assert result.returncode == 1
    for k in kept:
        assert archive[k].exists()


def test_apply_refuses_a_move_target_inside_the_archive(
    archive: dict[str, Path], tmp_path: Path
) -> None:
    out = tmp_path / "out"
    _list(archive, out)

    result = _run(
        _APPLY, out / "rerender.txt", "--move-to", archive["root"] / "old", "--yes"
    )

    assert result.returncode == 1
    assert "inside the archive" in result.stderr
    assert archive["hash"].exists()
    assert not (archive["root"] / "old").exists()  # nothing created either


def test_apply_without_confirmation_touches_nothing(
    archive: dict[str, Path], tmp_path: Path
) -> None:
    out = tmp_path / "out"
    _list(archive, out)

    result = _run(_APPLY, out / "rerender.txt", "--delete", stdin="no\n")

    assert result.returncode == 1
    assert archive["hash"].exists()
