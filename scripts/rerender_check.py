"""Decide which PDF folders under a directory tree can safely be rendered again.

Called by ``rerender-list.sh`` and ``rerender-apply.sh``; not part of the
``imaparc`` command. Two kinds of archive are found by their
``.imaparc-manifest`` files:

- **profile** archives — ``<profile>/eml/*.eml`` beside ``<profile>/pdf/<mail>/``
  (``imaparc fetch`` + ``render``);
- **eml** folders — ``<dir>/<mail>/`` holding its own ``.eml``
  (``imaparc eml``).

A folder qualifies only when its mail provably still exists as ``.eml`` — in
the profile's ``eml/``, or inside the folder itself:

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
    python rerender_check.py commands OUT_DIR

``list`` writes to OUT_DIR: ``rerender.txt`` (one folder per line),
``keep.txt`` (``REASON<TAB>FOLDER``), ``archives.tsv`` (what was found) and
``profiles.yaml`` (render profiles for archives not in ``--config``).
``recheck`` re-validates a list right before it is applied and prints
``profile<TAB>FOLDER`` or ``eml<TAB>FOLDER<TAB>EML`` per verified folder;
rejected ones go to stderr. ``commands`` turns OUT_DIR/applied.tsv into
OUT_DIR/render.sh. Nothing here writes outside OUT_DIR.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from imaparc.exceptions import ImapArcError, ParseError
from imaparc.html.to_text import html_to_text
from imaparc.humanize import format_date_human
from imaparc.mail.models import ParsedMail
from imaparc.mail.parser import parse_mail
from imaparc.profiles import load_profiles

MANIFEST = ".imaparc-manifest"
HASH_PREFIX = "sha256:"
DEFAULT_CONFIG = Path.home() / ".config" / "imaparc" / "profile.yaml"

# Never searched: macOS caches and the trash. Dot-directories are skipped too.
_SKIP_NAMES = frozenset({"Library", ".Trash"})

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


def _kind(folder: Path) -> str:
    """``profile`` for ``<profile>/pdf/<mail>/``, else ``eml`` (an adhoc folder)."""
    parent = folder.parent
    if parent.name == "pdf" and (parent.parent / "eml").is_dir():
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


def discover(root: Path, excludes: list[Path], config: Path) -> list[Archive]:
    """Every archive under ``root`` holding at least one manifest."""
    # Compared as given *and* resolved, so the walk never has to resolve each of
    # the many thousand directories below a home directory (that is slow).
    skip = {str(e.absolute()) for e in excludes} | {str(e.resolve()) for e in excludes}
    profile_dirs: set[Path] = set()
    eml_dirs: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [
            d
            for d in dirnames
            if not d.startswith(".")
            and d not in _SKIP_NAMES
            and os.path.join(dirpath, d) not in skip
        ]
        if MANIFEST not in filenames:
            continue
        dirnames[:] = []  # a mail folder holds only the mail's own files
        if _kind(here) == "profile":
            profile_dirs.add(here.parent.parent)
        else:
            eml_dirs.add(here.parent)

    configured = _configured_outputs(config)
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
    for path in sorted(eml_dirs):
        folders = sorted(p for p in path.iterdir() if p.is_dir())
        archives.append(Archive("eml", path, _segment(folders, "mail"), False))
    return archives


def _verdicts(archive: Archive) -> list[tuple[Path, str, Path | None]]:
    if archive.kind == "profile":
        mails = _Mails.load(_emls_in(archive.path / "eml"))
        return [(f, *verdict(f, mails)) for f in archive.folders()]
    return [(f, *verdict(f, _Mails.load(_emls_in(f)))) for f in archive.folders()]


def _profiles_yaml(archives: list[Archive]) -> str:
    lines = [
        "# Render profiles for the archives below that no profile.yaml covers.",
        "# Written by scripts/rerender_check.py; used by render.sh.",
        "profiles:",
    ]
    for a in archives:
        if a.kind != "profile" or a.configured:
            continue
        lines += [
            f"  - name: {a.name}",
            "    account: rerender  # not used by render",
            f"    output: {a.path}",
            "    pdf: true",
            "    remote_images: true",
        ]
    if lines[-1] == "profiles:":
        lines[-1] = "profiles: []"
    return "\n".join(lines) + "\n"


def _list(root: Path, out_dir: Path, excludes: list[Path], config: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    archives = discover(root, [*excludes, out_dir], config)
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
    (out_dir / "profiles.yaml").write_text(_profiles_yaml(archives), encoding="utf-8")
    print(f"\n{len(rerender_lines)} folders -> {out_dir / 'rerender.txt'}")
    print(f"{len(keep_lines)} folders -> {out_dir / 'keep.txt'}")
    return 0


def _recheck(listing: Path) -> int:
    profiles: dict[Path, _Mails] = {}
    rejected = 0
    for line in listing.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        folder = Path(line.rstrip("/"))
        kind = _kind(folder)
        if not folder.is_dir():
            result, eml = "not a directory", None
        elif kind == "profile":
            profile_dir = folder.parent.parent
            if profile_dir not in profiles:
                profiles[profile_dir] = _Mails.load(_emls_in(profile_dir / "eml"))
            result, eml = verdict(folder, profiles[profile_dir])
        else:
            result, eml = verdict(folder, _Mails.load(_emls_in(folder)))
        if result not in _REBUILDABLE or eml is None:
            print(f"skipped ({result}): {folder}", file=sys.stderr)
            rejected += 1
        elif kind == "profile":
            print(f"profile\t{folder}")
        else:
            print(f"eml\t{folder}\t{eml}")
    return 1 if rejected else 0


def _commands(out_dir: Path) -> int:
    """Write render.sh: exactly the imaparc calls that rebuild what was applied."""
    archives = {}
    for row in (out_dir / "archives.tsv").read_text(encoding="utf-8").splitlines():
        kind, path, name, configured = row.split("\t")
        archives[Path(path)] = Archive(kind, Path(path), name, configured == "1")

    configured_run = generated_run = False
    loose: dict[Path, list[str]] = defaultdict(list)
    applied = out_dir / "applied.tsv"
    rows = applied.read_text(encoding="utf-8").splitlines() if applied.exists() else []
    for row in rows:
        kind, path = row.split("\t", 1)
        if kind == "profile":
            archive = archives.get(Path(path).parent.parent)
            if archive is None or archive.configured:
                configured_run = True
            else:
                generated_run = True
        else:
            loose[Path(path).parent].append(path)

    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "# Renders the given .eml files that still lie loose. imaparc eml files each",
        "# one into its new folder, so a second run of this script finds nothing",
        "# left to do instead of failing on a missing path.",
        "eml_if_present() {",
        '    local name="$1" f',
        "    local files=()",
        "    shift",
        '    for f in "$@"; do [[ -e "${f}" ]] && files+=("${f}"); done',
        '    [[ ${#files[@]} -eq 0 ]] || imaparc eml --name "${name}" "${files[@]}"',
        "}",
        "",
    ]
    if configured_run:
        lines.append("imaparc render")
    if generated_run:
        lines.append(
            f"imaparc render --profiles {shlex.quote(str(out_dir / 'profiles.yaml'))}"
        )
    for directory, files in sorted(loose.items()):
        archive = archives.get(directory)
        name = archive.name if archive else "mail"
        quoted = " ".join(shlex.quote(f) for f in sorted(files))
        lines.append(f"eml_if_present {shlex.quote(name)} {quoted}")
    script = out_dir / "render.sh"
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
    if len(argv) == 2 and argv[0] == "commands":
        return _commands(Path(argv[1]))
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
