"""Decide which PDF folders under a directory tree can safely be rendered again.

Called by ``rerender-list.sh`` and ``rerender-apply.sh``; not part of the
``imaparc`` command. Two kinds of archive are found by their
``.imaparc-manifest`` files:

- **profile** archives — ``<profile>/pdf/<mail>/`` with the ``.eml`` in
  ``<profile>/eml/`` or, once rendered, ``eml-Archiv/<profile>/`` beside it;
- **eml** folders — ``<dir>/<mail>/`` holding its own ``.eml`` (``imaparc
  eml`` before 26.11.4);
- **flat** archives (since 26.11.6) — ``<profile>/<mail>.pdf`` with the
  ``.eml`` in ``eml-Archiv/<profile>/``; the PDF itself records the mail's hash
  in its XMP metadata, so a flat PDF is proven by **hash** only.

A folder qualifies only when its mail provably still exists as ``.eml`` — in
one of the profile's two ``.eml`` directories, or inside the folder itself:

- **hash** — the manifest (26.8.12+) records ``sha256:`` of the raw mail, and a
  ``.eml`` with exactly those bytes exists. Nothing else is consulted.
- **content** — older manifests hold only the Message-ID, which the *sender*
  picks and which is not guaranteed unique. So the Message-ID only nominates
  candidate ``.eml`` files; the folder qualifies when the PDF's own text layer
  shows the sender, date, subject and the opening of the body of one of them.

Folders are never paired with a ``.eml`` by name: the ``-2``/``-3`` suffixes of
the two are handed out independently and drift apart.

Usage::

    python rerender_check.py list ROOT OUT_DIR [--exclude DIR]... [--config FILE]
    python rerender_check.py recheck LIST
    python rerender_check.py commands OUT_DIR [INBOX]
    python rerender_check.py collect OUT_DIR INBOX

``list`` writes to OUT_DIR: ``rerender.txt`` (one folder per line),
``keep.txt`` (``REASON<TAB>FOLDER``), ``archives.tsv`` (what was found) and
``loose.txt`` (every ``.eml`` outside the inboxes of the ``--config`` profiles,
for ``imaparc eml`` to file into the archive).
``recheck`` re-validates a list right before it is applied and prints
``profile<TAB>FOLDER`` or ``eml<TAB>FOLDER<TAB>EML`` per verified folder;
rejected ones go to stderr. ``commands`` writes OUT_DIR/collect.sh and
OUT_DIR/render.sh (INBOX defaults to ~/imapArc/emlinput); ``collect`` is what
collect.sh runs — it moves the pending ``.eml`` files into INBOX. Apart from
``collect``, nothing here writes outside OUT_DIR.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from imaparc.exceptions import ImapArcError, ParseError
from imaparc.html.to_text import html_to_text
from imaparc.mail.models import ParsedMail
from imaparc.mail.parser import parse_mail
from imaparc.pdf import provenance
from imaparc.profiles import EML_ARCHIVE, archived_eml_dir, load_profiles
from imaparc.sources.deliver import file_away

MANIFEST = ".imaparc-manifest"
HASH_PREFIX = "sha256:"
DEFAULT_CONFIG = Path.home() / ".config" / "imaparc" / "profile.yaml"

# Never searched: macOS caches, the trash, the eml-Archiv (rendered mail, not
# loose) and the pdf-Löscharchiv (folders already set aside — finding them again
# would offer them a second time). Dot-directories are skipped too.
PDF_SET_ASIDE = "pdf-Löscharchiv"
INBOX_NAME = "emlinput"
_SKIP_NAMES = frozenset(
    unicodedata.normalize("NFC", n)
    for n in ("Library", ".Trash", EML_ARCHIVE, PDF_SET_ASIDE, INBOX_NAME)
)
# Where collect.sh gathers every loose .eml before `imaparc eml` files it away.
DEFAULT_INBOX = Path.home() / "imapArc" / INBOX_NAME

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
# The profile segment of a folder named by the default scheme.
_SEGMENT = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_([^_]+)_")

_REBUILDABLE = ("hash", "content")


def _norm(text: str) -> str:
    """Reduce text to comparable letters and digits, immune to layout."""
    return _NON_WORD.sub("", _LIGATURES.sub("", text.casefold()))


@dataclass
class _Mail:
    path: Path
    parsed: ParsedMail


@dataclass
class _Mails:
    """A set of .eml files, indexed by content hash and Message-ID."""

    by_hash: dict[str, Path] = field(default_factory=dict)
    by_message_id: dict[str, list[_Mail]] = field(
        default_factory=lambda: defaultdict(list)
    )
    without_message_id: list[_Mail] = field(default_factory=list)

    @classmethod
    def load(cls, paths: list[Path]) -> _Mails:
        mails = cls()
        for path in sorted(paths):
            raw = path.read_bytes()
            mails.by_hash.setdefault(hashlib.sha256(raw).hexdigest(), path)
            try:
                parsed = parse_mail(raw)
            except ParseError:
                continue
            mail = _Mail(path, parsed)
            if parsed.headers.message_id:
                mails.by_message_id[parsed.headers.message_id].append(mail)
            else:
                mails.without_message_id.append(mail)
        return mails

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


def _emls_in(directory: Path) -> list[Path]:
    return [p for p in directory.glob("*.eml") if p.is_file()]


def _profile_emls(profile_dir: Path) -> list[Path]:
    """A profile's mail: the ``eml/`` inbox and its ``eml-Archiv`` directory."""
    return [
        *_emls_in(profile_dir / "eml"),
        *_emls_in(archived_eml_dir(profile_dir)),
    ]


# The header date as PDFs without a manifest hash (before 26.8.12) print it.
# Fixed here rather than taken from imaparc.humanize, whose format has changed
# since (26.11.3) — only those old PDFs ever reach the content check.
_LEGACY_DATE = "%d.%m.%Y %H:%M"


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
        _norm(headers.date.strftime(_LEGACY_DATE) if headers.date else ""),
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


def verdict(folder: Path, mails: _Mails) -> tuple[str, Path | None]:
    """Whether ``folder`` may go, and the ``.eml`` that proves it.

    Returns ``("hash" | "content", eml)`` if its mail is among ``mails``,
    otherwise the reason it stays and ``None``.
    """
    try:
        lines = (folder / MANIFEST).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return "no-manifest", None
    if not lines:
        return "no-manifest", None
    hashes = [
        line.removeprefix(HASH_PREFIX) for line in lines if line.startswith(HASH_PREFIX)
    ]
    if hashes:
        found = mails.by_hash.get(hashes[0])
        return ("hash", found) if found else ("no-eml", None)
    candidates = mails.candidates(lines[0])
    if not candidates:
        return "no-eml", None
    pdf = folder / f"{folder.name}.pdf"
    text = _pdf_text(pdf) if pdf.is_file() else ""
    if not text:
        return "no-pdf-text", None
    for mail in candidates:
        if _shown_in(mail.parsed, text):
            return "content", mail.path
    return "content-mismatch", None


def verdict_pdf(pdf: Path, mails: _Mails) -> tuple[str, Path | None]:
    """Whether a flat ``<mail>.pdf`` may go: its recorded hash names a ``.eml``."""
    recorded = provenance.read(pdf)
    if recorded is None:
        return "no-identity", None
    found = mails.by_hash.get(recorded.sha256)
    return ("hash", found) if found else ("no-eml", None)


def _kind(folder: Path) -> str:
    """``profile`` for ``<profile>/pdf/<mail>/``, else ``eml`` (an adhoc folder)."""
    parent = folder.parent
    if parent.name == "pdf" and (
        (parent.parent / "eml").is_dir() or archived_eml_dir(parent.parent).is_dir()
    ):
        return "profile"
    return "eml"


@dataclass(frozen=True)
class Archive:
    """One place to re-render: a profile directory or an adhoc output directory."""

    kind: str
    path: Path
    name: str
    configured: bool

    def folders(self) -> list[Path]:
        if self.kind == "flat":
            return sorted(p for p in self.path.glob("*.pdf") if p.is_file())
        base = self.path / "pdf" if self.kind == "profile" else self.path
        return sorted(
            p
            for p in base.iterdir()
            if p.is_dir() and not p.name.startswith(".") and (p / MANIFEST).exists()
        )


def _segment(folders: list[Path], fallback: str) -> str:
    """The profile name the default naming scheme put into these folders."""
    found = Counter(m.group(1) for f in folders if (m := _SEGMENT.match(f.name)))
    return found.most_common(1)[0][0] if found else fallback


def _configured_outputs(config: Path) -> set[Path]:
    if not config.is_file():
        return set()
    try:
        profiles = load_profiles(config)
    except ImapArcError as exc:
        print(f"warning: cannot read {config}: {exc}", file=sys.stderr)
        return set()
    return {p.output.expanduser().resolve() for p in profiles}


def discover(
    root: Path, excludes: list[Path], config: Path
) -> tuple[list[Archive], list[Path]]:
    """Every archive under ``root`` holding a manifest, and every loose ``.eml``.

    *Loose* is any ``.eml`` that is not in a mail folder (where it is the
    folder's own mail or an attachment) and not in the ``eml/`` inbox of a
    profile from ``config`` — ``imaparc render`` takes care of those.
    """
    # Compared as given *and* resolved, so the walk never has to resolve each of
    # the many thousand directories below a home directory (that is slow).
    skip = {str(e.absolute()) for e in excludes} | {str(e.resolve()) for e in excludes}
    configured = _configured_outputs(config)
    inboxes = {str(o / "eml") for o in configured}
    profile_dirs: set[Path] = set()
    flat_dirs: set[Path] = set()
    eml_dirs: set[Path] = set()
    loose: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [
            d
            for d in dirnames
            if not d.startswith(".")
            and unicodedata.normalize("NFC", d) not in _SKIP_NAMES
            and os.path.join(dirpath, d) not in skip
        ]
        if MANIFEST not in filenames:
            if dirpath not in inboxes and str(here.resolve()) not in inboxes:
                loose.extend(here / f for f in sorted(filenames) if f.endswith(".eml"))
            if (
                any(f.endswith(".pdf") for f in filenames)
                and archived_eml_dir(here).is_dir()
            ):
                flat_dirs.add(here)
            continue
        dirnames[:] = []  # a mail folder holds only the mail's own files
        if _kind(here) == "profile":
            profile_dirs.add(here.parent.parent)
        else:
            eml_dirs.add(here.parent)

    archives: list[Archive] = []
    for path in sorted(profile_dirs):
        folders = sorted(p for p in (path / "pdf").iterdir() if p.is_dir())
        archives.append(
            Archive(
                "profile",
                path,
                _segment(folders, path.name),
                path.resolve() in configured,
            )
        )
    for path in sorted(flat_dirs):
        archives.append(Archive("flat", path, path.name, path.resolve() in configured))
    for path in sorted(eml_dirs):
        folders = sorted(p for p in path.iterdir() if p.is_dir())
        archives.append(Archive("eml", path, _segment(folders, "mail"), False))
    return archives, sorted(loose)


def _verdicts(archive: Archive) -> list[tuple[Path, str, Path | None]]:
    if archive.kind == "flat":
        mails = _Mails.load(_profile_emls(archive.path))
        return [(f, *verdict_pdf(f, mails)) for f in archive.folders()]
    if archive.kind == "profile":
        mails = _Mails.load(_profile_emls(archive.path))
        return [(f, *verdict(f, mails)) for f in archive.folders()]
    return [(f, *verdict(f, _Mails.load(_emls_in(f)))) for f in archive.folders()]


def _list(root: Path, out_dir: Path, excludes: list[Path], config: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    archives, loose = discover(root, [*excludes, out_dir], config)
    rerender_lines: list[str] = []
    keep_lines: list[str] = []
    for archive in archives:
        counts: Counter[str] = Counter()
        for folder, result, _ in _verdicts(archive):
            counts[result] += 1
            if result in _REBUILDABLE:
                rerender_lines.append(str(folder))
            else:
                keep_lines.append(f"{result}\t{folder}")
        summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        print(f"{archive.kind:<7} {archive.name:<14} {archive.path}\n        {summary}")

    def write(name: str, lines: list[str]) -> None:
        (out_dir / name).write_text("".join(f"{x}\n" for x in lines), encoding="utf-8")

    write("rerender.txt", rerender_lines)
    write("keep.txt", keep_lines)
    write(
        "archives.tsv",
        [f"{a.kind}\t{a.path}\t{a.name}\t{int(a.configured)}" for a in archives],
    )
    write("loose.txt", [str(p) for p in loose])
    print(f"\n{len(rerender_lines)} folders -> {out_dir / 'rerender.txt'}")
    print(f"{len(keep_lines)} folders -> {out_dir / 'keep.txt'}")
    print(f"{len(loose)} loose .eml -> {out_dir / 'loose.txt'}")
    return 0


def _recheck(listing: Path) -> int:
    profiles: dict[Path, _Mails] = {}
    rejected = 0
    for line in listing.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        folder = Path(line.rstrip("/"))
        kind = "flat" if folder.suffix == ".pdf" else _kind(folder)
        if kind == "flat":
            if not folder.is_file():
                result, eml = "not a file", None
            else:
                if folder.parent not in profiles:
                    profiles[folder.parent] = _Mails.load(_profile_emls(folder.parent))
                result, eml = verdict_pdf(folder, profiles[folder.parent])
        elif not folder.is_dir():
            result, eml = "not a directory", None
        elif kind == "profile":
            profile_dir = folder.parent.parent
            if profile_dir not in profiles:
                profiles[profile_dir] = _Mails.load(_profile_emls(profile_dir))
            result, eml = verdict(folder, profiles[profile_dir])
        else:
            result, eml = verdict(folder, _Mails.load(_emls_in(folder)))
        if result not in _REBUILDABLE or eml is None:
            print(f"skipped ({result}): {folder}", file=sys.stderr)
            rejected += 1
        elif kind in ("profile", "flat"):
            print(f"{kind}\t{folder}")
        else:
            print(f"eml\t{folder}\t{eml}")
    return 1 if rejected else 0


def _pending(out_dir: Path) -> list[Path]:
    """The ``.eml`` files still to collect: ``loose.txt`` plus extracted ones."""
    files: list[str] = []
    loose = out_dir / "loose.txt"
    if loose.exists():
        files += [x for x in loose.read_text(encoding="utf-8").splitlines() if x]
    applied = out_dir / "applied.tsv"
    rows = applied.read_text(encoding="utf-8").splitlines() if applied.exists() else []
    files += [path for kind, path in (r.split("\t", 1) for r in rows) if kind == "eml"]
    return [Path(f) for f in dict.fromkeys(files)]


def _collect(out_dir: Path, inbox: Path) -> int:
    """Move every pending ``.eml`` into ``inbox``, logging where each came from.

    Same guarantees as the archive: written atomically, never overwritten (a
    different mail under a taken name becomes ``-2``), an identical copy reused,
    and the source only removed once the copy exists. A file already gone was
    collected by an earlier run, so running again is harmless.
    """
    log = inbox / "emlinput.log"
    moved = 0
    for source in _pending(out_dir):
        if not source.is_file():
            continue
        raw = source.read_bytes()
        [target] = file_away(source, raw, [(inbox, source.stem)])
        with log.open("a", encoding="utf-8") as handle:
            handle.write(f"{source}\t{target.name}\n")
        moved += 1
    print(f"{moved} .eml collected into {inbox} (log: {log})")
    return 0


def _commands(out_dir: Path, inbox: Path) -> int:
    """Write collect.sh and render.sh, run in that order by the user.

    ``collect.sh`` moves every ``.eml`` that is not in a configured profile's
    inbox — loose in ``loose.txt``, or taken out of an ``imaparc eml`` folder by
    the apply step — into ``inbox``, so it can be looked through before
    anything is rendered. ``render.sh`` then runs ``imaparc render`` for the
    configured profiles and ``imaparc eml`` on ``inbox``: each mail's profile
    comes from its headers, its PDF lands in the archive and its ``.eml`` in the
    ``eml-Archiv``.
    """
    repo = Path(__file__).resolve().parent.parent
    collect = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        f"# Moves the .eml files listed in {out_dir}/loose.txt (and those taken",
        f"# out of mail folders) into {inbox}. Logged in emlinput.log there.",
        "# Files already moved are skipped, so this can simply be run again.",
        "uv run --quiet --project "
        + shlex.quote(str(repo))
        + " python "
        + shlex.quote(str(repo / "scripts" / "rerender_check.py"))
        + " collect "
        + shlex.quote(str(out_dir))
        + " "
        + shlex.quote(str(inbox)),
    ]
    render = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "# 1. The configured profiles: render their eml/ inboxes, then archive.",
        "imaparc render",
        "",
        f"# 2. Everything collected into {inbox}: profile from the headers.",
        f"if [[ -d {shlex.quote(str(inbox))} ]]; then",
        f"    imaparc eml {shlex.quote(str(inbox))}",
        "fi",
    ]
    for name, lines in (("collect.sh", collect), ("render.sh", render)):
        script = out_dir / name
        script.write_text("\n".join(lines) + "\n", encoding="utf-8")
        script.chmod(0o755)
        print(script)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[0] == "list":
        excludes: list[Path] = []
        config = DEFAULT_CONFIG
        rest = argv[3:]
        while rest:
            flag, value, *rest = rest if len(rest) >= 2 else ["", ""]
            if flag == "--exclude":
                excludes.append(Path(value))
            elif flag == "--config":
                config = Path(value)
            else:
                print(__doc__, file=sys.stderr)
                return 2
        return _list(Path(argv[1]), Path(argv[2]), excludes, config)
    if len(argv) == 2 and argv[0] == "recheck":
        return _recheck(Path(argv[1]))
    if len(argv) in (2, 3) and argv[0] == "commands":
        inbox = Path(argv[2]) if len(argv) == 3 else DEFAULT_INBOX
        return _commands(Path(argv[1]), inbox)
    if len(argv) == 3 and argv[0] == "collect":
        return _collect(Path(argv[1]), Path(argv[2]))
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
