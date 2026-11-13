"""Check an existing archive for damage and leftovers.

The archive is the product — it is meant to still be readable in ten years, long
after anyone remembers how it was produced. This walks a profile's PDFs (and the
per-mail folders of versions before 26.11.6) and its ``.eml`` files, and reports
what does not line up.

Read-only by design: it never repairs anything. A finding may need judgement
(which of two duplicate folders to keep?), and silently rewriting an archive
whose whole purpose is to stay untouched would be the wrong instinct.
"""

from __future__ import annotations

import enum
import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pikepdf

from imaparc.pdf import provenance
from imaparc.pipeline import (
    LEGACY_PDF_DIR,
    STAGING_PREFIX,
    TEMP_PREFIX,
    archive_entries,
)
from imaparc.profiles import (
    EML_ARCHIVE,
    EML_ERROR,
    EML_INPUT,
    LEGACY_EML_ARCHIVE,
    Profile,
    archive_root,
    archived_eml_dir,
    eml_dirs,
    error_eml_dir,
    input_eml_dir,
    pdf_output,
)

_HASH_PREFIX = "sha256:"
_MESSAGE_ID = re.compile(rb"^Message-ID:\s*(.+?)\s*$", re.I | re.M)


class Severity(enum.Enum):
    """How bad a finding is."""

    WARN = "warn"  # untidy or redundant, nothing lost
    FAIL = "fail"  # the archive is damaged


@dataclass(frozen=True)
class Finding:
    """One thing that does not line up."""

    kind: str
    severity: Severity
    profile: str
    detail: str


def _name(path: Path) -> str:
    return path.stem if path.is_file() else path.name


def _duplicates(entries: list[tuple[Path, str | None]], profile: str) -> list[Finding]:
    """Renditions that hold the same mail twice.

    Left behind by the reservation bug fixed in 26.8.7: a mail delivered under
    two UIDs (Gmail lists one message in All Mail *and* its label folder; a
    re-uploaded mail returns with a fresh UID) was rendered twice. The fix
    prevents new ones; the existing ones stay until someone looks.
    """
    by_identity: dict[str, list[str]] = defaultdict(list)
    for path, identity in entries:
        if identity:
            by_identity[identity].append(_name(path))
    return [
        Finding(
            "duplicate",
            Severity.WARN,
            profile,
            f"{len(names)} PDFs hold {identity}: {', '.join(names)}",
        )
        for identity, names in sorted(by_identity.items())
        if len(names) > 1
    ]


def _entry_findings(
    output: Path, entries: list[tuple[Path, str | None]], profile: str
) -> list[Finding]:
    """Per-rendition defects and leftovers of interrupted runs."""
    findings: list[Finding] = []
    for path, identity in entries:
        if path.is_file() and identity is None:
            findings.append(
                Finding(
                    "no-identity",
                    Severity.WARN,
                    profile,
                    f"{path.name} records no mail — not imapArc's, or written "
                    "before 26.11.3; a re-render cannot recognise it",
                )
            )
        elif path.is_dir() and not (path / f"{path.name}.pdf").is_file():
            # A legacy folder was renamed into place only once complete, so one
            # without its PDF means something damaged it afterwards.
            findings.append(
                Finding(
                    "incomplete",
                    Severity.FAIL,
                    profile,
                    f"{path.name} has no {path.name}.pdf",
                )
            )
    for base in (output, output / LEGACY_PDF_DIR):
        if not base.is_dir():
            continue
        for leftover in sorted(base.iterdir()):
            if leftover.name.startswith((STAGING_PREFIX, TEMP_PREFIX)):
                findings.append(
                    Finding(
                        "staging",
                        Severity.WARN,
                        profile,
                        f"leftover from an interrupted run: {leftover.name}",
                    )
                )
    return findings


@dataclass(frozen=True)
class _Rendered:
    """What a profile's renditions record, split by how far it can be trusted."""

    hashes: frozenset[str]  # "sha256:…" lines — conclusive
    legacy_ids: frozenset[str]  # first lines of manifests written before hashing
    legacy_names: frozenset[str]  # names of those same pre-hash folders

    @classmethod
    def of(cls, entries: list[tuple[Path, str | None]]) -> _Rendered:
        hashes: set[str] = set()
        ids: set[str] = set()
        names: set[str] = set()
        for path, identity in entries:
            lines = identity.splitlines() if identity else []
            found = {ln for ln in lines if ln.startswith(_HASH_PREFIX)}
            hashes |= found
            if lines and not found:
                ids.add(lines[0])
                names.add(_name(path))
        return cls(frozenset(hashes), frozenset(ids), frozenset(names))


def _is_rendered(eml: Path, rendered: _Rendered) -> bool:
    """Whether some rendition records exactly this ``.eml``.

    Decided by the content hash, never by the name: the ``-2``/``-3`` suffixes
    of ``.eml`` and PDF are handed out independently, so ``X.eml`` and ``X.pdf``
    can be two different mails — a name match hid an unrendered mail until
    26.11.8. Only a rendition from before 26.8.12, which records no hash, is
    matched the way it was then: by Message-ID, or by its name.
    """
    try:
        raw = eml.read_bytes()
    except OSError:  # pragma: no cover - unreadable file, reported elsewhere
        return False
    if f"{_HASH_PREFIX}{hashlib.sha256(raw).hexdigest()}" in rendered.hashes:
        return True
    if eml.stem in rendered.legacy_names:
        return True
    head = re.sub(rb"\r?\n[ \t]+", b" ", raw[:65536].split(b"\r\n\r\n")[0])
    found = _MESSAGE_ID.search(head)
    return bool(
        found
        and found.group(1).decode("utf-8", "replace").strip() in rendered.legacy_ids
    )


def _unrendered(eml_dir: Path, rendered: _Rendered, profile: str) -> list[Finding]:
    """``.eml`` files with no rendition — never rendered."""
    missing = sorted(
        entry.stem
        for entry in eml_dir.iterdir()
        if entry.is_file()
        and entry.suffix == ".eml"
        and not _is_rendered(entry, rendered)
    )
    if not missing:
        return []
    shown = ", ".join(missing[:5]) + (" …" if len(missing) > 5 else "")
    return [
        Finding(
            "unrendered",
            Severity.WARN,
            profile,
            f"{len(missing)} mail(s) have no PDF: {shown}",
        )
    ]


def _eml_hashes(present: list[Path]) -> dict[str, list[Path]]:
    """Content hash → the ``.eml`` files holding exactly that content."""
    found: dict[str, list[Path]] = defaultdict(list)
    for directory in present:
        for entry in sorted(directory.glob("*.eml")):
            try:
                digest = hashlib.sha256(entry.read_bytes()).hexdigest()
            except OSError:  # pragma: no cover - unreadable, reported as unrendered
                continue
            found[digest].append(entry)
    return found


def _deep_findings(
    entries: list[tuple[Path, str | None]], present: list[Path], profile: str
) -> list[Finding]:
    """The checks that read every file: slow, and worth it now and then.

    - ``embedded-mismatch`` (damage): the ``.eml`` inside a PDF is not the mail
      the PDF says it shows.
    - ``no-embedded-eml``: a PDF records its mail but does not carry it.
    - ``no-eml``: a PDF's mail is in none of the eml directories any more — it survives
      inside the PDF, and ``imaparc extract`` gets it back out.
    - ``duplicate-eml``: byte-identical ``.eml`` files (mail delivered twice
      before identical copies were merged).
    """
    findings: list[Finding] = []
    emls = _eml_hashes(present)
    copies = {digest: paths for digest, paths in emls.items() if len(paths) > 1}
    if copies:
        surplus = sum(len(paths) - 1 for paths in copies.values())
        example = next(iter(copies.values()))
        findings.append(
            Finding(
                "duplicate-eml",
                Severity.WARN,
                profile,
                f"{surplus} .eml are byte-identical copies of {len(copies)} others, "
                f"e.g. {example[0].name} = {example[1].name}",
            )
        )
    for path, identity in entries:
        if not path.is_file() or not identity:
            continue
        recorded = next(
            line.removeprefix(_HASH_PREFIX)
            for line in identity.splitlines()
            if line.startswith(_HASH_PREFIX)
        )
        if recorded not in emls:
            findings.append(
                Finding(
                    "no-eml",
                    Severity.WARN,
                    profile,
                    f"{path.name}: its .eml is not in the eml-archive "
                    "(still embedded in the PDF — imaparc extract)",
                )
            )
        try:
            source = provenance.embedded_source(path)
        except (pikepdf.PdfError, OSError) as exc:
            findings.append(
                Finding("unreadable", Severity.FAIL, profile, f"{path.name}: {exc}")
            )
            continue
        if source is None:
            findings.append(
                Finding(
                    "no-embedded-eml",
                    Severity.WARN,
                    profile,
                    f"{path.name} does not carry its .eml",
                )
            )
        elif hashlib.sha256(source[1]).hexdigest() != recorded:
            findings.append(
                Finding(
                    "embedded-mismatch",
                    Severity.FAIL,
                    profile,
                    f"{path.name}: the embedded .eml is not the mail it records",
                )
            )
    return findings


def _waiting(directory: Path, kind: str, text: str, profile: str) -> list[Finding]:
    """One finding for the ``.eml`` files sitting in ``directory``, if any."""
    if not directory.is_dir():
        return []
    names = sorted(p.stem for p in directory.glob("*.eml"))
    if not names:
        return []
    shown = ", ".join(names[:5]) + (" …" if len(names) > 5 else "")
    return [
        Finding(kind, Severity.WARN, profile, f"{len(names)} mail(s) {text}: {shown}")
    ]


def verify_profile(profile: Profile, *, deep: bool = False) -> list[Finding]:
    """Check one profile's archive and return everything that does not line up.

    The directory a mail lies in says where it stands, so each is read for what
    it means: ``eml-input`` holds mail still to render (``pending``),
    ``eml-error`` mail that could not be rendered correctly (``failed``), and
    everything in ``eml-archive`` — or the directories of earlier versions —
    must have its PDF (``unrendered`` if not).

    A ``pdf: false`` profile is only checked for having mail at all: it is not
    supposed to have PDFs, so their absence is not a defect.

    ``deep`` adds the checks that open every PDF and compare every ``.eml``
    (see :func:`_deep_findings`).
    """
    output = profile.output
    waiting, failed = input_eml_dir(output), error_eml_dir(output)
    present = [d for d in eml_dirs(profile) if d.is_dir()]
    if not present:
        return [
            Finding(
                "no-archive",
                Severity.WARN,
                profile.name,
                f"no mail at {archived_eml_dir(output)} — nothing fetched yet?",
            )
        ]
    if not profile.pdf:
        return []
    entries = archive_entries(output)
    rendered = _Rendered.of(entries)
    archived = [d for d in present if d not in (waiting, failed)]
    return [
        *_duplicates(entries, profile.name),
        *_entry_findings(output, entries, profile.name),
        *(f for d in archived for f in _unrendered(d, rendered, profile.name)),
        *_waiting(waiting, "pending", "wait in eml-input for a render", profile.name),
        *_waiting(
            failed,
            "failed",
            "could not be rendered (see the .error.txt beside each)",
            profile.name,
        ),
        *(_deep_findings(entries, present, profile.name) if deep else []),
    ]


def unclaimed_profiles(profiles: list[Profile]) -> list[Profile]:
    """Stand-in profiles for mail directories no configured profile covers.

    ``imaparc eml`` files mail that matches no profile into ``00_Mails``, and a
    profile may have been removed from ``profile.yaml`` while its archive
    stayed. Such directories hold mail and PDFs like any other, so they are
    checked like any other — or a mail waiting or failed there would be
    reported nowhere.
    """
    # By root and name: the PDFs may lie in <root>/<name> or <root>/imaparc-pdf/<name>.
    claimed = {(archive_root(p.output).resolve(), p.output.name) for p in profiles}
    found: dict[Path, Profile] = {}
    for root in sorted({archive_root(p.output) for p in profiles}):
        for group in (EML_INPUT, EML_ARCHIVE, EML_ERROR, LEGACY_EML_ARCHIVE):
            directory = root / group
            if not directory.is_dir():
                continue
            for entry in sorted(directory.iterdir()):
                output = pdf_output(root, entry.name)
                if (
                    entry.is_dir()
                    and not entry.name.startswith(".")
                    and (root.resolve(), entry.name) not in claimed
                ):
                    found.setdefault(
                        output,
                        Profile(name=entry.name, account="-", output=output, pdf=True),
                    )
    return list(found.values())


def verify_loose(profiles: list[Profile]) -> list[Finding]:
    """Mail lying directly in an ``eml-input`` or ``eml-error`` directory.

    Not in a profile's subdirectory: dropped there by hand, or collected by the
    rerender scripts. It belongs to no profile yet, so it is reported under a
    name of its own.
    """
    findings: list[Finding] = []
    for root in sorted({archive_root(p.output) for p in profiles}):
        findings += _waiting(
            root / EML_INPUT, "pending", "wait in eml-input for a render", "(loose)"
        )
        findings += _waiting(
            root / EML_ERROR,
            "failed",
            "could not be rendered (see the .error.txt beside each)",
            "(loose)",
        )
    return findings


def exit_code(findings: list[Finding]) -> int:
    """1 only when the archive is damaged.

    Duplicates and leftovers are untidy but lose nothing, so they must not make
    a scheduled check fail — otherwise the failure signal stops meaning anything.
    """
    return 1 if any(f.severity is Severity.FAIL for f in findings) else 0
