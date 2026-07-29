"""Tests for reading an eml/ directory as a render source."""

from __future__ import annotations

from pathlib import Path

import pytest

from imaparc.exceptions import SourceError
from imaparc.sources.eml import EmlSource
from tests.mail_builder import build_mail


def _write(eml_dir: Path, name: str, *, body: str = "x") -> None:
    eml_dir.mkdir(parents=True, exist_ok=True)
    (eml_dir / name).write_bytes(build_mail(subject=body))


def test_reads_eml_files(tmp_path: Path) -> None:
    eml = tmp_path / "eml"
    _write(eml, "2026-03-23_02-18-04_p_A.eml")
    _write(eml, "2026-03-24_09-00-00_p_B.eml")
    mails = list(EmlSource(eml))
    assert len(mails) == 2
    assert all(b"Subject:" in m.raw for m in mails)


def test_chronological_order_by_basename(tmp_path: Path) -> None:
    eml = tmp_path / "eml"
    _write(eml, "2026-03-24_09-00-00_p_later.eml", body="later")
    _write(eml, "2026-03-23_02-18-04_p_earlier.eml", body="earlier")
    names = [m.source_id.rsplit("/", 1)[-1] for m in EmlSource(eml)]
    assert names == [
        "2026-03-23_02-18-04_p_earlier.eml",
        "2026-03-24_09-00-00_p_later.eml",
    ]


def test_ignores_non_eml_files(tmp_path: Path) -> None:
    eml = tmp_path / "eml"
    _write(eml, "2026-03-23_00-00-00_p_real.eml")
    (eml / ".DS_Store").write_bytes(b"junk")
    (eml / "notes.txt").write_bytes(b"junk")
    assert len(list(EmlSource(eml))) == 1


def test_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(SourceError):
        EmlSource(tmp_path / "nope")
