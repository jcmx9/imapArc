"""Check an existing archive for damage and leftovers.

The archive is the product — it is meant to still be readable in ten years, long
after anyone remembers how it was produced. This walks a profile's ``eml/`` and
``pdf/`` and reports what does not line up.

Read-only by design: it never repairs anything. A finding may need judgement
(which of two duplicate folders to keep?), and silently rewriting an archive
whose whole purpose is to stay untouched would be the wrong instinct.
"""

from __future__ import annotations

import enum
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from imaparc.pipeline import MANIFEST, STAGING_PREFIX, read_identity
from imaparc.profiles import Profile


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


def _duplicates(pdf_dir: Path, profile: str) -> list[Finding]:
    """Folders that hold the same mail twice.

    Left behind by the reservation bug fixed in 26.8.7: a mail delivered under
    two UIDs (Gmail lists one message in All Mail *and* its label folder; a
    re-uploaded mail returns with a fresh UID) rendered into two folders. The fix
    prevents new ones; the existing ones stay until someone looks.
    """
    by_identity: dict[str, list[str]] = defaultdict(list)
    for folder in sorted(p for p in pdf_dir.iterdir() if p.is_dir()):
        if folder.name.startswith(STAGING_PREFIX):
            continue
        identity = read_identity(folder)
        if identity:
            by_identity[identity].append(folder.name)
    return [
        Finding(
            "duplicate",
            Severity.WARN,
            profile,
            f"{len(names)} folders hold {identity}: {', '.join(names)}",
        )
        for identity, names in sorted(by_identity.items())
        if len(names) > 1
    ]


def _folder_findings(pdf_dir: Path, profile: str) -> list[Finding]:
    """Per-folder defects: no manifest, or no PDF to open."""
    findings: list[Finding] = []
    for folder in sorted(p for p in pdf_dir.iterdir() if p.is_dir()):
        if folder.name.startswith(STAGING_PREFIX):
            findings.append(
                Finding(
                    "staging",
                    Severity.WARN,
                    profile,
                    f"leftover from an interrupted run: {folder.name}",
                )
            )
            continue
        if not (folder / MANIFEST).is_file():
            findings.append(
                Finding(
                    "no-manifest",
                    Severity.WARN,
                    profile,
                    f"{folder.name} has no manifest — a re-render cannot tell "
                    "it apart from a name collision",
                )
            )
        if not (folder / f"{folder.name}.pdf").is_file():
            # _store() renames the folder in atomically once complete, so a
            # folder without its PDF means something damaged it afterwards.
            findings.append(
                Finding(
                    "incomplete",
                    Severity.FAIL,
                    profile,
                    f"{folder.name} has no {folder.name}.pdf",
                )
            )
    return findings


def _unrendered(eml_dir: Path, pdf_dir: Path, profile: str) -> list[Finding]:
    """``.eml`` files with no folder of their own — never rendered.

    ``pdf_dir`` may not exist: a profile can have been fetched but never
    rendered, in which case every mail is unrendered.
    """
    rendered = (
        {p.name for p in pdf_dir.iterdir() if p.is_dir()} if pdf_dir.is_dir() else set()
    )
    missing = sorted(
        entry.stem
        for entry in eml_dir.iterdir()
        if entry.is_file() and entry.suffix == ".eml" and entry.stem not in rendered
    )
    if not missing:
        return []
    shown = ", ".join(missing[:5]) + (" …" if len(missing) > 5 else "")
    return [
        Finding(
            "unrendered",
            Severity.WARN,
            profile,
            f"{len(missing)} mail(s) have no PDF folder: {shown}",
        )
    ]


def verify_profile(profile: Profile) -> list[Finding]:
    """Check one profile's archive and return everything that does not line up.

    A ``pdf: false`` profile is only checked for its ``eml/``: it is not supposed
    to have rendered folders, so their absence is not a defect.
    """
    eml_dir = profile.output / "eml"
    pdf_dir = profile.output / "pdf"
    if not eml_dir.is_dir():
        return [
            Finding(
                "no-archive",
                Severity.WARN,
                profile.name,
                f"no eml/ at {eml_dir} — nothing fetched yet?",
            )
        ]
    if not profile.pdf:
        return []
    if not pdf_dir.is_dir():
        # Fetched but never rendered — every mail is simply unrendered.
        return _unrendered(eml_dir, pdf_dir, profile.name)

    return [
        *_duplicates(pdf_dir, profile.name),
        *_folder_findings(pdf_dir, profile.name),
        *_unrendered(eml_dir, pdf_dir, profile.name),
    ]


def exit_code(findings: list[Finding]) -> int:
    """1 only when the archive is damaged.

    Duplicates and leftovers are untidy but lose nothing, so they must not make
    a scheduled check fail — otherwise the failure signal stops meaning anything.
    """
    return 1 if any(f.severity is Severity.FAIL for f in findings) else 0
