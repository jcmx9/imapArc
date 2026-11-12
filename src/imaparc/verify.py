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

from imaparc.pipeline import (
    LEGACY_PDF_DIR,
    STAGING_PREFIX,
    TEMP_PREFIX,
    archive_entries,
)
from imaparc.profiles import Profile, archived_eml_dir, eml_dirs

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


def _is_rendered_elsewhere(eml: Path, known: set[str]) -> bool:
    """Whether some folder's manifest describes this ``.eml`` under another name.

    The ``-2``/``-3`` suffixes are handed out independently by ``deliver_eml()``
    and ``_resolve_output()``, so a mail delivered twice can end up as
    ``X-2.eml`` beside a folder ``X-3``. Comparing names alone would call that
    unrendered, which is both wrong and expensive: it buries a genuinely missing
    PDF among hundreds of false ones.

    The Message-ID is tried first because it is readable from the header block
    alone; the hash costs reading the whole file and is only computed when the
    cheap check found nothing.
    """
    try:
        raw = eml.read_bytes()
    except OSError:  # pragma: no cover - unreadable file, reported elsewhere
        return False
    head = re.sub(rb"\r?\n[ \t]+", b" ", raw[:65536].split(b"\r\n\r\n")[0])
    found = _MESSAGE_ID.search(head)
    if found and found.group(1).decode("utf-8", "replace").strip() in known:
        return True
    return f"{_HASH_PREFIX}{hashlib.sha256(raw).hexdigest()}" in known


def _unrendered(
    eml_dir: Path, known: set[str], rendered: set[str], profile: str
) -> list[Finding]:
    """``.eml`` files with no rendition — never rendered."""
    missing = sorted(
        entry.stem
        for entry in eml_dir.iterdir()
        if entry.is_file()
        and entry.suffix == ".eml"
        and entry.stem not in rendered
        and not _is_rendered_elsewhere(entry, known)
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


def verify_profile(profile: Profile) -> list[Finding]:
    """Check one profile's archive and return everything that does not line up.

    A ``pdf: false`` profile is only checked for its ``.eml`` files: it is not
    supposed to have PDFs, so their absence is not a defect. Both ``.eml``
    locations are read — the ``eml-Archiv`` and the legacy ``eml/`` inbox.
    """
    present = [d for d in eml_dirs(profile) if d.is_dir()]
    if not present:
        return [
            Finding(
                "no-archive",
                Severity.WARN,
                profile.name,
                f"no mail at {archived_eml_dir(profile.output)} — nothing fetched yet?",
            )
        ]
    if not profile.pdf:
        return []
    entries = archive_entries(profile.output)
    known = {line for _, i in entries if i for line in i.splitlines()}
    rendered = {_name(path) for path, _ in entries}
    return [
        *_duplicates(entries, profile.name),
        *_entry_findings(profile.output, entries, profile.name),
        *(f for d in present for f in _unrendered(d, known, rendered, profile.name)),
    ]


def exit_code(findings: list[Finding]) -> int:
    """1 only when the archive is damaged.

    Duplicates and leftovers are untidy but lose nothing, so they must not make
    a scheduled check fail — otherwise the failure signal stops meaning anything.
    """
    return 1 if any(f.severity is Severity.FAIL for f in findings) else 0
