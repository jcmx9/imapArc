"""Bootstrap the central config directory (`imaparc init`).

Creates ``~/.config/imaparc`` with a ready-to-edit ``.env`` and ``profile.yaml``.
Existing files are never overwritten unless ``force`` is set, so real
credentials are safe. The directory is ``0700`` and ``.env`` is ``0600``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

ENV_TEMPLATE = """\
# imapArc IMAP accounts. Fill in your account(s) and keep this file private.
#
# Format:  IMAP_<ACCOUNT>_<FIELD>=<value>
#   <ACCOUNT>  A name you choose (PRIVAT, ARBEIT, …). Its lowercase form is what
#              a profile's `account:` in profile.yaml references.
#   <FIELD>    One of HOST, PORT, USER, PASSWORD, SSL.

IMAP_PRIVAT_HOST=imap.example.com    # REQUIRED. IMAP server hostname.
IMAP_PRIVAT_PORT=993                 # Optional, default 993.
IMAP_PRIVAT_USER=you@example.com     # REQUIRED. Login user name.
IMAP_PRIVAT_PASSWORD=change-me       # REQUIRED. Login password.
IMAP_PRIVAT_SSL=true                 # Optional, default true (true/1/yes/on).

# A second account, if needed:
# IMAP_ARBEIT_HOST=imap.work.example
# IMAP_ARBEIT_USER=me@work.example
# IMAP_ARBEIT_PASSWORD=change-me
"""

PROFILE_HEADER = """\
# imapArc central profiles. Accounts live next to this file in .env
#
# Each profile picks matching mail from one account into <output>/eml/, and
# (when pdf: true) also renders to <output>/pdf/ — one folder per mail holding
# <basename>.pdf (full: mail + attachment pages) plus, when there are
# attachments, <basename>_mailonly.pdf and the original attachment files.
#
# Required per profile: name, account, output. Everything else is optional and
# shown commented below with its default — uncomment what you need. Add another
# profile with:  imaparc add-profile <name>
# Full reference: profile.example.yaml in the imapArc repository.

profiles:
"""


# Comment column: pad code out to here, then "# comment". Cosmetic only.
_COMMENT_COL = 36

# Optional match.* rules: (key, comment, commented placeholder shown when unset).
_MATCH_OPTIONAL = [
    ("recursive", "also scan subfolders (default false)", "recursive: false"),
    (
        "trash",
        "include the Trash folder in a recursive scan (default false)",
        "trash: false",
    ),
    ("domains", "sender domain(s), each with a leading @", "domains: ['@example.com']"),
    (
        "addresses",
        "exact sender address(es), no @ prefix",
        "addresses: [alerts@example.com]",
    ),
    (
        "mode",
        "which header fields to search (default all)",
        "mode: [from, to, cc, bcc]",
    ),
    ("subject", "a regex, OR a list of wildcard patterns", "subject: ['*Rechnung*']"),
    (
        "attachments",
        "require an attachment of one of these types",
        "attachments: [pdf]",
    ),
    ("since", "ignore mail before this day (YYYY-MM-DD)", "since: 2026-01-01"),
    ("until", "ignore mail after this day (YYYY-MM-DD)", "until: 2026-12-31"),
    ("larger", "only mail bigger than this (5MB, 500KB, bytes)", "larger: 5MB"),
    ("smaller", "only mail smaller than this", "smaller: 20MB"),
]
# Optional profile.* render settings.
_PROFILE_OPTIONAL = [
    ("remote_images", "load external images when rendering", "remote_images: false"),
    ("jobs", "parallel renders for this profile (default 4)", "jobs: 4"),
    (
        "gs_jobs",
        "parallel Ghostscript runs (default 2); keep below jobs, it is the "
        "memory-hungry step",
        "gs_jobs: 2",
    ),
    (
        "filename_pattern",
        "name scheme for the .eml and its PDF folder; placeholders {date}, "
        "{profile}, {subject}",
        "filename_pattern: '{date}_{profile}_{subject}'",
    ),
    (
        "date_format",
        "date tokens for {date}: YYYY MM DD hh mm ss",
        "date_format: YYYY-MM-DD_hh-mm-ss",
    ),
    (
        "max_attachment_bytes",
        "skip attachments larger than this (default 400 MB)",
        "max_attachment_bytes: 419430400",
    ),
    (
        "attachment_timeout_s",
        "give up converting one attachment after this long (default 120)",
        "attachment_timeout_s: 120",
    ),
    (
        "render_timeout_ms",
        "give up rendering one mail body after this long (default 30000)",
        "render_timeout_ms: 30000",
    ),
]
_AFTER_FETCH_FIELDS = [
    ("label", "add this IMAP keyword", "label: Archiviert"),
    (
        "move_to",
        "move the source mail (exclusive with delete)",
        "move_to: imapArc",
    ),
    ("delete", "delete the source mail (exclusive with move_to)", "delete: true"),
]


def _kv(key: str, value: object) -> str:
    """Serialize ``key: value`` with the value inline (lists/scalars, no braces)."""
    dumped = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True).strip()
    # safe_dump appends a "..." document-end marker after a bare scalar.
    if dumped.endswith("..."):
        dumped = dumped[:-3].strip()
    return f"{key}: {dumped}"


def _annotate(code: str, comment: str) -> str:
    """Pad ``code`` and append ``# comment`` (or just ``code`` if no comment)."""
    if not comment:
        return code
    pad = max(1, _COMMENT_COL - len(code))
    return f"{code}{' ' * pad}# {comment}"


def render_profile_from_raw(raw: dict[str, object]) -> str:
    """Render one profile as a fully-annotated block, preserving its values.

    Fields present in ``raw`` are emitted active with their real values; every
    other optional field is present but commented, with its default. Required
    fields (name, account, output) and folders/pdf are always active. This is the
    single source of truth for the profile layout — used by ``init`` (from an
    example dict), ``add-profile``, and ``sync-profiles`` (from the parsed file).
    """
    match = raw.get("match")
    match = match if isinstance(match, dict) else {}
    lines: list[str] = [
        _annotate(
            f"  - {_kv('name', raw.get('name', 'unnamed'))}",
            "REQUIRED. Identifier; middle of PDF names.",
        ),
        _annotate(
            f"    {_kv('account', raw.get('account', 'privat'))}",
            "REQUIRED. References IMAP_PRIVAT_* in .env.",
        ),
        _annotate("    match:", "Optional. All set rules must match (AND)."),
        _annotate(
            f"      {_kv('folders', match.get('folders', ['INBOX']))}",
            "IMAP folders to scan (default [INBOX]).",
        ),
    ]
    for key, comment, placeholder in _MATCH_OPTIONAL:
        if key in match:
            lines.append(_annotate(f"      {_kv(key, match[key])}", comment))
        else:
            lines.append(_annotate(f"      # {placeholder}", comment))

    lines.append(
        _annotate(f"    {_kv('output', raw.get('output', '~/imapArc/unnamed'))}", "")
    )
    lines.append(
        _annotate(
            f"    {_kv('pdf', raw.get('pdf', False))}",
            "also render PDFs (needs Chromium) (default false)",
        )
    )
    for key, comment, placeholder in _PROFILE_OPTIONAL:
        if key in raw:
            lines.append(_annotate(f"    {_kv(key, raw[key])}", comment))
        else:
            lines.append(_annotate(f"    # {placeholder}", comment))

    after = raw.get("after_fetch")
    if isinstance(after, dict):
        lines.append(
            _annotate("    after_fetch:", "server action, only after safe archiving")
        )
        for key, comment, placeholder in _AFTER_FETCH_FIELDS:
            if key in after:
                lines.append(_annotate(f"      {_kv(key, after[key])}", comment))
            else:
                lines.append(_annotate(f"      # {placeholder}", comment))
    else:
        lines.append(
            _annotate("    # after_fetch:", "server action, only after safe archiving")
        )
        for _, comment, placeholder in _AFTER_FETCH_FIELDS:
            lines.append(_annotate(f"    #   {placeholder}", comment))

    return "\n".join(lines) + "\n"


def profile_block(name: str, output: str | None = None) -> str:
    """Return a fresh, fully-annotated profile block for a new profile.

    Required fields are active with placeholders; every optional field is present
    but commented, with its default. ``after_fetch: move_to: imapArc`` is active
    by default so matched mail is moved out of the source folder after archiving —
    which also means the same mail is not re-scanned on later runs. Shares
    :func:`render_profile_from_raw` with ``sync-profiles`` so init/add-profile and
    sync stay identical in layout.
    """
    return render_profile_from_raw(
        {
            "name": name,
            "account": "privat",
            "match": {"folders": ["INBOX"]},
            "output": output or f"~/imapArc/{name}",
            "pdf": True,
            "after_fetch": {"move_to": "imapArc"},
        }
    )


def render_profiles_file(raw_profiles: list[dict[str, object]]) -> str:
    """Render a whole profile.yaml (header + one annotated block per profile)."""
    blocks = "\n".join(render_profile_from_raw(p) for p in raw_profiles)
    return PROFILE_HEADER + blocks


PROFILE_TEMPLATE = PROFILE_HEADER + profile_block("rechnungen")


@dataclass
class InitResult:
    """What ``init_config`` did: created files and those left untouched."""

    created: list[Path]
    skipped: list[Path]


def init_config(config_dir: Path, *, force: bool = False) -> InitResult:
    """Create ``config_dir`` with a ``.env`` and ``profile.yaml`` template.

    Existing files are kept (listed in ``skipped``) unless ``force`` is set.
    The directory is created ``0700``; ``.env`` is written ``0600`` and
    ``profile.yaml`` ``0644``.

    Returns:
        An :class:`InitResult` recording created and skipped paths.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(config_dir, 0o700)

    created: list[Path] = []
    skipped: list[Path] = []
    for name, content, mode in (
        (".env", ENV_TEMPLATE, 0o600),
        ("profile.yaml", PROFILE_TEMPLATE, 0o644),
    ):
        path = config_dir / name
        if path.exists() and not force:
            skipped.append(path)
            continue
        path.write_text(content, encoding="utf-8")
        os.chmod(path, mode)
        created.append(path)
    return InitResult(created=created, skipped=skipped)
