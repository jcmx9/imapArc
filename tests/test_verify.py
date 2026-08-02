"""Tests for `imaparc verify` — archive integrity."""

from __future__ import annotations

from pathlib import Path

from imaparc.profiles import Profile
from imaparc.verify import Finding, Severity, verify_profile


def _profile(tmp_path: Path, *, pdf: bool = True) -> Profile:
    return Profile(name="p", account="a", output=tmp_path, pdf=pdf)


def _eml(tmp_path: Path, name: str) -> Path:
    eml = tmp_path / "eml"
    eml.mkdir(parents=True, exist_ok=True)
    path = eml / f"{name}.eml"
    path.write_bytes(b"raw")
    return path


def _rendered(
    tmp_path: Path, name: str, identity: str, *, with_pdf: bool = True
) -> Path:
    folder = tmp_path / "pdf" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".imaparc-manifest").write_text(identity, encoding="utf-8")
    if with_pdf:
        (folder / f"{name}.pdf").write_bytes(b"%PDF-1.4")
    return folder


def _kinds(findings: list[Finding]) -> set[str]:
    return {f.kind for f in findings}


def test_a_clean_archive_reports_nothing(tmp_path: Path) -> None:
    _eml(tmp_path, "2026-08-01_10-00-00_p_Mail")
    _rendered(tmp_path, "2026-08-01_10-00-00_p_Mail", "<a@b>")

    assert verify_profile(_profile(tmp_path)) == []


def test_finds_two_folders_holding_the_same_mail(tmp_path: Path) -> None:
    """The duplicate the pre-26.8.7 reservation bug left behind.

    The fix stops new ones appearing; archives written before it still carry
    them, and nothing else would ever point them out.
    """
    _eml(tmp_path, "2026-08-01_10-00-00_p_Az")
    _rendered(tmp_path, "2026-08-01_10-00-00_p_Az", "<same@id>")
    _rendered(tmp_path, "2026-08-01_10-00-00_p_Az-2", "<same@id>")

    findings = verify_profile(_profile(tmp_path))

    assert "duplicate" in _kinds(findings)
    duplicate = next(f for f in findings if f.kind == "duplicate")
    assert "<same@id>" in duplicate.detail
    assert duplicate.severity is Severity.WARN  # a copy, not data loss


def test_finds_an_eml_that_was_never_rendered(tmp_path: Path) -> None:
    _eml(tmp_path, "2026-08-01_10-00-00_p_Mail")
    _eml(tmp_path, "2026-08-02_10-00-00_p_Unrendered")
    _rendered(tmp_path, "2026-08-01_10-00-00_p_Mail", "<a@b>")

    findings = verify_profile(_profile(tmp_path))

    assert "unrendered" in _kinds(findings)
    assert "Unrendered" in next(f for f in findings if f.kind == "unrendered").detail


def test_ignores_missing_pdfs_when_the_profile_does_not_render(tmp_path: Path) -> None:
    """A `pdf: false` profile has no pdf/ directory — that is not a defect."""
    _eml(tmp_path, "2026-08-01_10-00-00_p_Mail")

    assert verify_profile(_profile(tmp_path, pdf=False)) == []


def test_finds_a_folder_without_its_pdf(tmp_path: Path) -> None:
    """The atomic rename should make this impossible — so it means damage."""
    _eml(tmp_path, "2026-08-01_10-00-00_p_Mail")
    _rendered(tmp_path, "2026-08-01_10-00-00_p_Mail", "<a@b>", with_pdf=False)

    findings = verify_profile(_profile(tmp_path))

    assert "incomplete" in _kinds(findings)
    assert next(f for f in findings if f.kind == "incomplete").severity is Severity.FAIL


def test_finds_a_folder_without_a_manifest(tmp_path: Path) -> None:
    folder = tmp_path / "pdf" / "2026-08-01_10-00-00_p_Mail"
    folder.mkdir(parents=True)
    (folder / "2026-08-01_10-00-00_p_Mail.pdf").write_bytes(b"%PDF-1.4")
    _eml(tmp_path, "2026-08-01_10-00-00_p_Mail")

    assert "no-manifest" in _kinds(verify_profile(_profile(tmp_path)))


def test_finds_leftover_staging_directories(tmp_path: Path) -> None:
    """An interrupted run leaves these; harmless but they accrue."""
    _eml(tmp_path, "2026-08-01_10-00-00_p_Mail")
    _rendered(tmp_path, "2026-08-01_10-00-00_p_Mail", "<a@b>")
    (tmp_path / "pdf" / ".staging-something").mkdir()

    findings = verify_profile(_profile(tmp_path))

    assert "staging" in _kinds(findings)
    assert next(f for f in findings if f.kind == "staging").severity is Severity.WARN


def test_missing_output_directory_is_reported_once(tmp_path: Path) -> None:
    findings = verify_profile(_profile(tmp_path / "does-not-exist"))

    assert _kinds(findings) == {"no-archive"}


def test_exit_code_only_fails_on_real_damage() -> None:
    from imaparc.verify import exit_code

    assert exit_code([]) == 0
    assert exit_code([Finding("duplicate", Severity.WARN, "p", "x")]) == 0
    assert exit_code([Finding("incomplete", Severity.FAIL, "p", "x")]) == 1


def test_pdf_profile_that_was_never_rendered(tmp_path: Path) -> None:
    """`pdf: true` but no pdf/ yet — a fetch has run, a render has not.

    Ordinary state, not damage: it must be reported, not crash.
    """
    _eml(tmp_path, "2026-08-01_10-00-00_p_Mail")

    findings = verify_profile(_profile(tmp_path))

    assert _kinds(findings) == {"unrendered"}
    assert findings[0].severity is Severity.WARN
