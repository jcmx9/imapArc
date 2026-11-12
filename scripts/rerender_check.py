"""Decide which PDF folders of an imapArc archive can safely be rendered again.

Called by ``rerender-list.sh`` and ``rerender-apply.sh``; not part of the
``imaparc`` command. A folder qualifies only when its mail provably still exists
as ``.eml`` in the same profile, so that ``imaparc render`` can rebuild it:

- **hash** — the manifest (26.8.12+) records ``sha256:`` of the raw mail, and a
  ``.eml`` with exactly those bytes exists. Nothing else is consulted.
- **content** — older manifests hold only the Message-ID, which the *sender*
  picks and which is not guaranteed unique. So the Message-ID only nominates
  candidate ``.eml`` files; the folder qualifies when the PDF's own text layer
  shows the sender, date, subject and the opening of the body of one of them.

Folders are never paired with a ``.eml`` by name: the ``-2``/``-3`` suffixes of
the two are handed out independently and drift apart.

Usage::

    python rerender_check.py list ARCHIVE_ROOT OUT_DIR
    python rerender_check.py recheck LIST

``list`` writes ``OUT_DIR/rerender.txt`` (one folder per line) and
``OUT_DIR/keep.txt`` (``REASON<TAB>FOLDER``). ``recheck`` re-validates a list
right before it is applied: verified folders go to stdout, rejected ones with
their reason to stderr. Neither mode writes anything inside the archive.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from imaparc.exceptions import ParseError
from imaparc.html.to_text import html_to_text
from imaparc.humanize import format_date_human
from imaparc.mail.models import ParsedMail
from imaparc.mail.parser import parse_mail

MANIFEST = ".imaparc-manifest"
HASH_PREFIX = "sha256:"

# Pages of the PDF searched for the mail's text. The header block and the start
# of the body sit on the first pages of every rendition.
_PAGES = 12
# Words of the body taken as the content sample, and its normalised length.
_SAMPLE_WORDS = 40
_SAMPLE_CHARS = 60
# The ligatures pdftotext may drop entirely ("stattfindet" → "statt ndet").
# They are removed from both sides, so a dropped ligature cannot cause a miss.
_LIGATURES = re.compile(r"ffi|ffl|ff|fi|fl")
_NON_WORD = re.compile(r"[\W_]+")


def _norm(text: str) -> str:
    """Reduce text to comparable letters and digits, immune to layout."""
    return _NON_WORD.sub("", _LIGATURES.sub("", text.casefold()))


@dataclass
class _Mail:
    path: Path
    parsed: ParsedMail


@dataclass
class _Profile:
    """The .eml side of one profile, indexed by content hash and Message-ID."""

    hashes: set[str] = field(default_factory=set)
    by_message_id: dict[str, list[_Mail]] = field(
        default_factory=lambda: defaultdict(list)
    )
    without_message_id: list[_Mail] = field(default_factory=list)

    @classmethod
    def load(cls, eml_dir: Path) -> _Profile:
        profile = cls()
        for path in sorted(eml_dir.glob("*.eml")):
            raw = path.read_bytes()
            profile.hashes.add(hashlib.sha256(raw).hexdigest())
            try:
                parsed = parse_mail(raw)
            except ParseError:
                continue
            mail = _Mail(path, parsed)
            if parsed.headers.message_id:
                profile.by_message_id[parsed.headers.message_id].append(mail)
            else:
                profile.without_message_id.append(mail)
        return profile

    def candidates(self, identity: str) -> list[_Mail]:
        """The mails an old manifest's first line may refer to.

        Without a Message-ID imapArc wrote ``from|subject|timestamp``; the
        timestamp may come from the file's mtime, so only from and subject are
        compared — the content check decides the rest.
        """
        if identity in self.by_message_id:
            return self.by_message_id[identity]
        sender, _, rest = identity.partition("|")
        subject = rest.rpartition("|")[0]
        return [
            m
            for m in self.without_message_id
            if m.parsed.headers.from_ == sender and m.parsed.headers.subject == subject
        ]


def _pdf_text(pdf: Path) -> str:
    result = subprocess.run(
        ["pdftotext", "-l", str(_PAGES), str(pdf), "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    return _norm(result.stdout) if result.returncode == 0 else ""


def _shown_in(mail: ParsedMail, text: str) -> bool:
    """True if the PDF text shows this mail's header block and body opening.

    At least two of the probes must exist, so a mail with almost nothing to
    compare (no subject, no body) is never accepted on one weak match.
    """
    headers = mail.headers
    probes = [
        _norm(headers.from_ or ""),
        _norm(format_date_human(headers.date)),
        _norm(headers.subject or ""),
    ]
    samples = [
        _norm(" ".join(body.split()[:_SAMPLE_WORDS]))[:_SAMPLE_CHARS]
        for body in (mail.text_body or "", html_to_text(mail.html_body or ""))
        if body.strip()
    ]
    present = [p for p in probes if p]
    if len(present) + bool(samples) < 2:
        return False
    if not all(p in text for p in present):
        return False
    return not samples or any(s in text for s in samples)


def verdict(folder: Path, profile: _Profile) -> str:
    """``hash`` or ``content`` if the folder may go, else the reason it stays."""
    try:
        lines = (folder / MANIFEST).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return "no-manifest"
    if not lines:
        return "no-manifest"
    hashes = [
        ln.removeprefix(HASH_PREFIX) for ln in lines if ln.startswith(HASH_PREFIX)
    ]
    if hashes:
        return "hash" if hashes[0] in profile.hashes else "no-eml"
    candidates = profile.candidates(lines[0])
    if not candidates:
        return "no-eml"
    pdf = folder / f"{folder.name}.pdf"
    text = _pdf_text(pdf) if pdf.is_file() else ""
    if not text:
        return "no-pdf-text"
    if any(_shown_in(m.parsed, text) for m in candidates):
        return "content"
    return "content-mismatch"


def _list(root: Path, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    rerender_lines: list[str] = []
    keep_lines: list[str] = []
    for profile_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        eml_dir, pdf_dir = profile_dir / "eml", profile_dir / "pdf"
        if not (eml_dir.is_dir() and pdf_dir.is_dir()):
            continue
        profile = _Profile.load(eml_dir)
        counts: Counter[str] = Counter()
        # Dot-entries (leftover .staging-* folders) are never listed.
        for folder in sorted(pdf_dir.iterdir()):
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            result = verdict(folder, profile)
            counts[result] += 1
            if result in ("hash", "content"):
                rerender_lines.append(str(folder))
            else:
                keep_lines.append(f"{result}\t{folder}")
        summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        print(f"{profile_dir.name:<24} {summary}")
    (out_dir / "rerender.txt").write_text(
        "".join(f"{line}\n" for line in rerender_lines), encoding="utf-8"
    )
    (out_dir / "keep.txt").write_text(
        "".join(f"{line}\n" for line in keep_lines), encoding="utf-8"
    )
    print(f"\n{len(rerender_lines)} folders -> {out_dir / 'rerender.txt'}")
    print(f"{len(keep_lines)} folders -> {out_dir / 'keep.txt'}")
    return 0


def _recheck(listing: Path) -> int:
    profiles: dict[Path, _Profile] = {}
    rejected = 0
    for line in listing.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        folder = Path(line.rstrip("/"))
        profile_dir = folder.parent.parent
        if not folder.is_dir():
            reason = "not a directory"
        elif folder.parent.name != "pdf" or not (profile_dir / "eml").is_dir():
            reason = "not inside <profile>/pdf/"
        else:
            if profile_dir not in profiles:
                profiles[profile_dir] = _Profile.load(profile_dir / "eml")
            result = verdict(folder, profiles[profile_dir])
            reason = "" if result in ("hash", "content") else result
        if reason:
            print(f"skipped ({reason}): {folder}", file=sys.stderr)
            rejected += 1
        else:
            print(folder)
    return 1 if rejected else 0


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "list":
        return _list(Path(argv[1]), Path(argv[2]))
    if len(argv) == 2 and argv[0] == "recheck":
        return _recheck(Path(argv[1]))
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
