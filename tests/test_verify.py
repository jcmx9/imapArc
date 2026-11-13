"""Tests for `imaparc verify` — archive integrity."""

from __future__ import annotations

import io
import zlib
from pathlib import Path

import pikepdf

from imaparc.pdf import provenance
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


def test_finds_a_pdf_that_records_no_mail(tmp_path: Path) -> None:
    _eml(tmp_path, "2026-08-01_10-00-00_p_Mail")
    (tmp_path / "2026-08-01_10-00-00_p_Mail.pdf").write_bytes(_blank_pdf())

    assert "no-identity" in _kinds(verify_profile(_profile(tmp_path)))


# --- the flat layout (since 26.11.6) -----------------------------------------


def _blank_pdf() -> bytes:
    doc = pikepdf.Pdf.new()
    doc.add_blank_page()
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _flat(output: Path, name: str, raw: bytes) -> None:
    output.mkdir(parents=True, exist_ok=True)
    stamped = provenance.stamp(_blank_pdf(), raw, message_id=None)
    (output / f"{name}.pdf").write_bytes(stamped)


def _archived(output: Path, name: str, raw: bytes) -> None:
    archive = output.parent / "eml-archive" / output.name
    archive.mkdir(parents=True, exist_ok=True)
    (archive / f"{name}.eml").write_bytes(raw)


def test_a_clean_flat_archive_reports_nothing(tmp_path: Path) -> None:
    output = tmp_path / "p"
    _archived(output, "2026-08-01_10-00-00_p_Mail", b"Subject: x\r\n\r\nbody")
    _flat(output, "2026-08-01_10-00-00_p_Mail", b"Subject: x\r\n\r\nbody")

    assert verify_profile(_profile(output)) == []


def test_a_flat_pdf_is_matched_by_hash_not_name(tmp_path: Path) -> None:
    output = tmp_path / "p"
    _archived(output, "2026-08-01_10-00-00_p_Mail-2", b"RAW")
    _flat(output, "2026-08-01_10-00-00_p_Mail-3", b"RAW")

    assert verify_profile(_profile(output)) == []


def test_two_flat_pdfs_of_one_mail_are_duplicates(tmp_path: Path) -> None:
    output = tmp_path / "p"
    _archived(output, "a", b"RAW")
    _flat(output, "a", b"RAW")
    _flat(output, "a-2", b"RAW")

    assert _kinds(verify_profile(_profile(output))) == {"duplicate"}


def test_a_leftover_temp_file_is_reported(tmp_path: Path) -> None:
    output = tmp_path / "p"
    _archived(output, "a", b"RAW")
    _flat(output, "a", b"RAW")
    (output / ".tmp-x1y2").write_bytes(b"half")

    assert _kinds(verify_profile(_profile(output))) == {"staging"}


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


# --- name drift between .eml and its folder ---------------------------------


def _eml_with(tmp_path: Path, name: str, raw: bytes) -> Path:
    eml = tmp_path / "eml"
    eml.mkdir(parents=True, exist_ok=True)
    path = eml / f"{name}.eml"
    path.write_bytes(raw)
    return path


def test_drifted_folder_name_is_not_reported_as_unrendered(tmp_path: Path) -> None:
    """The suffixes of a .eml and of its PDF folder are assigned independently.

    deliver_eml() counts up ``-2`` for the .eml, _resolve_output() counts up for
    the folder, and neither knows about the other — so a mail delivered twice can
    end up as ``X-2.eml`` next to a folder ``X-3``. Nothing is lost, the folder
    is still reachable through its manifest, and reporting it as "has no PDF
    folder" buries a genuinely missing PDF in noise.
    """
    raw = b"Message-ID: <drift@example.com>\r\n\r\nbody"
    _eml_with(tmp_path, "2026-08-01_10-00-00_p_Mail-2", raw)
    _rendered(tmp_path, "2026-08-01_10-00-00_p_Mail-3", "<drift@example.com>")

    assert "unrendered" not in _kinds(verify_profile(_profile(tmp_path)))


def test_drift_is_recognised_by_hash_when_the_message_id_differs(
    tmp_path: Path,
) -> None:
    """Manifests written since 26.8.12 carry a hash; that is what decides."""
    import hashlib

    raw = b"Message-ID: <one@example.com>\r\n\r\nbody"
    digest = hashlib.sha256(raw).hexdigest()
    _eml_with(tmp_path, "2026-08-01_10-00-00_p_Mail-2", raw)
    _rendered(
        tmp_path,
        "2026-08-01_10-00-00_p_Mail-9",
        f"<other@example.com>\nsha256:{digest}",
    )

    assert "unrendered" not in _kinds(verify_profile(_profile(tmp_path)))


def test_a_genuinely_unrendered_mail_is_still_reported(tmp_path: Path) -> None:
    """The check must stay able to fail — that is the whole point of it."""
    _eml_with(tmp_path, "2026-08-01_10-00-00_p_Never", b"Message-ID: <gone@x>\r\n\r\nb")
    _rendered(tmp_path, "2026-08-01_11-00-00_p_Other", "<other@x>")

    findings = verify_profile(_profile(tmp_path))

    assert "unrendered" in _kinds(findings)
    assert "Never" in next(f for f in findings if f.kind == "unrendered").detail


def _archived_eml(tmp_path: Path, name: str, raw: bytes) -> None:
    archive = tmp_path.parent / "eml-archive" / tmp_path.name
    archive.mkdir(parents=True, exist_ok=True)
    (archive / f"{name}.eml").write_bytes(raw)


def test_an_archived_rendered_mail_is_clean(tmp_path: Path) -> None:
    output = tmp_path / "p"
    _archived_eml(output, "2026-08-01_10-00-00_p_Mail", b"Message-ID: <a@b>\r\n\r\nb")
    _rendered(output, "2026-08-01_10-00-00_p_Mail", "<a@b>")

    assert verify_profile(_profile(output)) == []


def test_an_archived_mail_without_a_folder_is_unrendered(tmp_path: Path) -> None:
    output = tmp_path / "p"
    _archived_eml(output, "2026-08-01_10-00-00_p_Lost", b"Message-ID: <x@y>\r\n\r\nb")
    _rendered(output, "2026-08-01_11-00-00_p_Other", "<other@x>")

    findings = verify_profile(_profile(output))

    assert "unrendered" in _kinds(findings)
    assert "no-archive" not in _kinds(findings)


def test_a_name_match_does_not_hide_an_unrendered_mail(tmp_path: Path) -> None:
    """X.pdf holds mail B; mail A in X.eml was never rendered.

    Pairing by name called A rendered. Ordinary in practice: two mails share a
    base name, and until 26.11.8 the second delivered was rendered first.
    """
    output = tmp_path / "p"
    _archived(output, "X", b"Message-ID: <a@x>\r\n\r\nmail A")
    _archived(output, "X-2", b"Message-ID: <b@x>\r\n\r\nmail B")
    _flat(output, "X", b"Message-ID: <b@x>\r\n\r\nmail B")

    findings = verify_profile(_profile(output))

    assert _kinds(findings) == {"unrendered"}
    assert "X" in findings[0].detail and "X-2" not in findings[0].detail


# --- the directory a mail lies in is its state -------------------------------


def test_mail_waiting_in_the_input_is_pending_not_unrendered(tmp_path: Path) -> None:
    output = tmp_path / "p"
    waiting = tmp_path / "eml-input" / "p"
    waiting.mkdir(parents=True)
    (waiting / "new.eml").write_bytes(b"RAW")

    findings = verify_profile(_profile(output))

    assert _kinds(findings) == {"pending"}
    assert "1 mail(s) wait in eml-input" in findings[0].detail


def test_mail_in_eml_error_is_reported_as_failed(tmp_path: Path) -> None:
    output = tmp_path / "p"
    _archived(output, "ok", b"RAW")
    _flat(output, "ok", b"RAW")
    failed = tmp_path / "eml-error" / "p"
    failed.mkdir(parents=True)
    (failed / "bad.eml").write_bytes(b"OTHER")
    (failed / "bad.error.txt").write_text("why")

    findings = verify_profile(_profile(output))

    assert _kinds(findings) == {"failed"}
    assert findings[0].severity is Severity.WARN
    assert "bad" in findings[0].detail and "error.txt" in findings[0].detail


def test_archives_without_a_profile_are_checked_too(tmp_path: Path) -> None:
    """00_Mails is nobody's profile — a mail failed there must still show."""
    from imaparc.verify import unclaimed_profiles

    configured = _profile(tmp_path / "p")
    failed = tmp_path / "eml-error" / "00_Mails"
    failed.mkdir(parents=True)
    (failed / "bad.eml").write_bytes(b"x")
    (tmp_path / "eml-archive" / "p").mkdir(parents=True)  # the profile's own

    extra = unclaimed_profiles([configured])

    assert [(p.name, p.output) for p in extra] == [("00_Mails", tmp_path / "00_Mails")]
    assert _kinds(verify_profile(extra[0])) == {"failed"}


def test_loose_mail_in_the_input_root_is_reported(tmp_path: Path) -> None:
    from imaparc.verify import verify_loose

    (tmp_path / "eml-input").mkdir()
    (tmp_path / "eml-input" / "dropped.eml").write_bytes(b"x")

    findings = verify_loose([_profile(tmp_path / "p")])

    assert [(f.kind, f.profile) for f in findings] == [("pending", "(loose)")]
    assert "dropped" in findings[0].detail


# --- --deep: the checks that read every file -------------------------------


def _carrying(output: Path, name: str, raw: bytes, embedded: bytes | None) -> None:
    """A flat PDF recording ``raw`` and carrying ``embedded`` as its .eml."""
    output.mkdir(parents=True, exist_ok=True)
    stamped = provenance.stamp(
        _blank_pdf(),
        raw,
        message_id=None,
        eml_name=None if embedded is None else "m.eml",
    )
    if embedded is not None and embedded != raw:
        # Tamper with the embedded file only; the recorded hash stays raw's.
        with pikepdf.open(io.BytesIO(stamped)) as doc:
            doc.Root.AF[0].EF.F.write(
                zlib.compress(embedded), filter=pikepdf.Name.FlateDecode
            )
            out = io.BytesIO()
            doc.save(out)
        stamped = out.getvalue()
    (output / f"{name}.pdf").write_bytes(stamped)


def test_deep_is_silent_on_a_sound_archive(tmp_path: Path) -> None:
    output = tmp_path / "p"
    _archived(output, "a", b"RAW")
    _carrying(output, "a", b"RAW", b"RAW")

    assert verify_profile(_profile(output), deep=True) == []


def test_deep_finds_an_embedded_mail_that_is_not_the_recorded_one(
    tmp_path: Path,
) -> None:
    output = tmp_path / "p"
    _archived(output, "a", b"RAW")
    _carrying(output, "a", b"RAW", b"SOMETHING ELSE")

    findings = verify_profile(_profile(output), deep=True)

    assert _kinds(findings) == {"embedded-mismatch"}
    assert findings[0].severity is Severity.FAIL


def test_deep_reports_a_pdf_that_does_not_carry_its_mail(tmp_path: Path) -> None:
    output = tmp_path / "p"
    _archived(output, "a", b"RAW")
    _carrying(output, "a", b"RAW", None)

    assert _kinds(verify_profile(_profile(output), deep=True)) == {"no-embedded-eml"}


def test_deep_reports_a_pdf_whose_eml_is_gone(tmp_path: Path) -> None:
    output = tmp_path / "p"
    _archived(output, "other", b"OTHER")
    _carrying(output, "other", b"OTHER", b"OTHER")
    _carrying(output, "a", b"RAW", b"RAW")  # its .eml was deleted

    findings = verify_profile(_profile(output), deep=True)

    assert _kinds(findings) == {"no-eml"}
    assert "a.pdf" in findings[0].detail and "extract" in findings[0].detail


def test_deep_reports_identical_eml_copies_once(tmp_path: Path) -> None:
    output = tmp_path / "p"
    for name in ("a", "a-2", "a-3"):
        _archived(output, name, b"RAW")
    _carrying(output, "a", b"RAW", b"RAW")

    findings = verify_profile(_profile(output), deep=True)

    assert _kinds(findings) == {"duplicate-eml"}
    assert findings[0].detail.startswith("2 .eml are byte-identical copies of 1")
    # Not part of the ordinary run: it reads every file.
    assert verify_profile(_profile(output)) == []


# --- report shape ----------------------------------------------------------


def test_many_warnings_of_a_kind_collapse_into_one_line() -> None:
    """832 duplicate lines are unreadable, and they bury the damage report."""
    from imaparc.cli import _verify_lines

    findings = [
        Finding("duplicate", Severity.WARN, "school", f"folder {i}") for i in range(40)
    ]

    summary = _verify_lines(findings, verbose=False)

    assert len(summary) == 1
    assert "40 findings" in summary[0]
    assert len(_verify_lines(findings, verbose=True)) == 40


def test_damage_is_never_summarised_away() -> None:
    from imaparc.cli import _verify_lines

    findings = [
        *(Finding("duplicate", Severity.WARN, "p", f"folder {i}") for i in range(10)),
        Finding("incomplete", Severity.FAIL, "p", "X has no X.pdf"),
        Finding("incomplete", Severity.FAIL, "p", "Y has no Y.pdf"),
    ]

    lines = _verify_lines(findings, verbose=False)

    assert sum("has no" in ln for ln in lines) == 2  # both, individually
    assert len(lines) == 3


def test_a_lone_warning_keeps_its_detail() -> None:
    from imaparc.cli import _verify_lines

    findings = [Finding("staging", Severity.WARN, "p", "leftover: .staging-x")]

    assert ".staging-x" in _verify_lines(findings, verbose=False)[0]
