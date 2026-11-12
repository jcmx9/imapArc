"""Render one parsed mail into its archive folder.

This is where every render building block comes together: Chromium renders the
three body renditions and each separator/info page, ``convert_attachment`` turns
attachments into pages (or reports why not), the parts are merged and passed
through Ghostscript to PDF/A-3b, and the result is written to the immutable
archive.

Every mail becomes **one file**, ``<output>/<basename>.pdf``: the body followed
by the attachment pages, with the ``.eml`` and every attachment that could not
become pages embedded, bookmarks to each separator page, and the mail's identity
in the XMP metadata. Until 26.11.5 each mail had a folder of its own with a
manifest, a ``_mailonly.pdf`` (until 26.11.3) and the attachment originals;
those folders are still recognised, never rewritten.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from playwright.async_api import BrowserContext

from imaparc.attachments.classify import effective_mime
from imaparc.attachments.models import ConversionOutcome
from imaparc.attachments.to_pdf import convert_attachment
from imaparc.config import RunConfig
from imaparc.exceptions import RenderError
from imaparc.html.inline import inline_resources
from imaparc.html.render_html import (
    prepend_header,
    render_info,
    render_section_separator,
    render_separator,
    render_text,
    render_text_attachment,
)
from imaparc.html.to_text import html_is_trivial_wrapper, html_to_text
from imaparc.humanize import format_date_human, format_file_size
from imaparc.mail.models import ParsedMail
from imaparc.naming import build_base_name
from imaparc.pdf import provenance
from imaparc.pdf.merge import count_pages, merge_pdfs
from imaparc.pdf.pdfa import to_pdfa3b
from imaparc.render.browser import BrowserPool
from imaparc.render.geometry import faithful_rendition, reflowed_rendition
from imaparc.render.pdf_render import render_html_to_pdf
from imaparc.storage import make_dir, write_readonly

logger = logging.getLogger(__name__)

# The legacy on-disk format (until 26.11.5), still read: a per-mail folder with a
# hidden manifest holding the mail identity. Public because `verify` reads it too.
# A PDF written since then carries the identity in its own XMP metadata.
MANIFEST = ".imaparc-manifest"
STAGING_PREFIX = ".staging-"
# write_readonly()'s temporary files, left behind only by a killed process.
TEMP_PREFIX = ".tmp-"
LEGACY_PDF_DIR = "pdf"
_CONTROL_CHARS = re.compile(r"[\x00-\x1f]")


@dataclass(frozen=True, slots=True)
class _Part:
    """A rendered piece of the final PDF, optionally starting a bookmark."""

    pdf: bytes
    bookmark: str | None = None


@dataclass(frozen=True, slots=True)
class AttachmentResult:
    """One attachment's conversion outcome, for the run report."""

    filename: str
    outcome: ConversionOutcome


@dataclass(frozen=True, slots=True)
class RenderResult:
    """What became of one mail."""

    basename: str
    written: bool
    skipped: bool = False
    html_rendered: bool = True
    complete: bool = True  # every attachment became pages
    combined_pdf: Path | None = None
    attachments: tuple[AttachmentResult, ...] = field(default_factory=tuple)


async def render_mail(
    parsed: ParsedMail,
    *,
    raw: bytes,
    profile: str,
    output_dir: Path,
    pool: BrowserPool,
    config: RunConfig,
    received: datetime | None = None,
    claimed: dict[str, str] | None = None,
    existing: ArchiveIndex | None = None,
    gs_semaphore: asyncio.Semaphore | None = None,
) -> RenderResult:
    """Render one mail to ``<output_dir>/<basename>.pdf``.

    ``output_dir`` must be writable (the caller keeps it unlocked for the run).
    Returns a :class:`RenderResult`; skips (does nothing) if the output already
    exists, which makes a repeat run idempotent.

    ``claimed`` maps basenames reserved by concurrent renders in this run to the
    mail each belongs to. Sharing one mapping across a profile's renders makes
    the reservation race-free: a *different* mail resolving to the same base
    disambiguates, while a second copy of the *same* mail skips instead of
    producing a duplicate PDF.

    ``gs_semaphore`` caps how many Ghostscript conversions run at once. It is
    shared across the run like ``claimed``, because Ghostscript is the
    memory-hungry step and needs a tighter bound than ``config.jobs`` — one
    conversion holds the whole document in memory. Without it, peak memory
    scales with render concurrency.
    """
    if gs_semaphore is None:
        gs_semaphore = asyncio.Semaphore(config.gs_jobs)
    timestamp = parsed.headers.date or received
    base = build_base_name(
        timestamp,
        profile,
        parsed.headers.subject or "",
        pattern=config.filename_pattern,
        date_format=config.date_format,
    )
    identity = mail_identity(raw, parsed, timestamp)
    basename, skip = _resolve_output(output_dir, base, identity, claimed, existing)
    if skip:
        return RenderResult(basename, written=False, skipped=True)

    target = output_dir / f"{basename}.pdf"
    eml_name = f"{basename}.eml"

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        async with pool.context() as ctx:
            mail_part, html_ok = await _render_mail_part(ctx, parsed, config)
            attachment_parts, results, kept = await _render_attachments(
                ctx, parsed, work, config, eml_name
            )
        parts = mail_part + attachment_parts

        async with gs_semaphore:
            pdfa = await asyncio.to_thread(
                _merge_to_pdfa, [p.pdf for p in parts], work / "mail", config
            )
        # The PDF records the mail it shows, carries the .eml and every
        # attachment that did not become pages, and bookmarks its sections.
        pdfa = await asyncio.to_thread(
            provenance.stamp,
            pdfa,
            raw,
            message_id=parsed.headers.message_id,
            eml_name=eml_name,
            mod_date=timestamp,
            attachments=kept,
            bookmarks=_bookmarks(parts),
            title=parsed.headers.subject,
            author=parsed.headers.from_,
        )
        make_dir(output_dir, readonly=True)
        target = write_readonly(target, pdfa)

    return RenderResult(
        target.stem,
        written=True,
        html_rendered=html_ok,
        complete=all(r.outcome.ok for r in results),
        combined_pdf=target,
        attachments=tuple(results),
    )


def _bookmarks(parts: list[_Part]) -> list[tuple[str, int]]:
    """``(title, page)`` for every part that starts a bookmark."""
    marks: list[tuple[str, int]] = []
    page = 0
    for part in parts:
        if part.bookmark:
            marks.append((part.bookmark, page))
        page += count_pages(part.pdf)
    return marks


_HASH_PREFIX = "sha256:"


def mail_identity(raw: bytes, parsed: ParsedMail, timestamp: datetime | None) -> str:
    """A mail's identity: its Message-ID *and* a hash of its bytes, one per line.

    The Message-ID alone is not enough. The **sender** chooses it, nothing
    enforces the uniqueness RFC 5322 asks for, and legacy systems emit values
    like ``<1@localhost>``. Two different mails sharing one would look identical
    here, and the second would silently never be rendered.

    The hash decides; the Message-ID is kept because manifests written before
    26.8.12 hold only that, and matching on it lets an existing archive keep
    working instead of duplicating itself on the next run (see :func:`same_mail`).
    """
    primary = parsed.headers.message_id or (
        f"{parsed.headers.from_}|{parsed.headers.subject}|{timestamp}"
    )
    return f"{primary}\n{_HASH_PREFIX}{hashlib.sha256(raw).hexdigest()}"


def same_mail(stored: str | None, current: str) -> bool:
    """Whether a stored manifest describes the same mail as ``current``.

    A hash match is conclusive, so it is preferred whenever both sides carry one.
    Only when the stored manifest predates hashing does this fall back to the
    Message-ID — which is exactly the weaker check, but the alternative is to
    declare every previously archived mail unknown.
    """
    if stored is None:
        return False
    stored_parts = set(stored.splitlines())
    current_parts = set(current.splitlines())
    stored_hash = {p for p in stored_parts if p.startswith(_HASH_PREFIX)}
    current_hash = {p for p in current_parts if p.startswith(_HASH_PREFIX)}
    if stored_hash and current_hash:
        return bool(stored_hash & current_hash)
    return bool((stored_parts - stored_hash) & (current_parts - current_hash))


def read_identity(subfolder: Path) -> str | None:
    """Read the stored mail identity from a legacy folder's manifest, if any."""
    try:
        return (subfolder / MANIFEST).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def stored_identity(output_dir: Path, name: str) -> str | None:
    """The identity recorded under ``name``: its PDF's XMP, or a legacy manifest."""
    pdf = output_dir / f"{name}.pdf"
    if pdf.is_file():
        return provenance.identity(pdf)
    if (output_dir / name).is_dir():
        return read_identity(output_dir / name)
    return None


def is_rendered(output_dir: Path, name: str) -> bool:
    """Whether a mail's rendition exists under ``name`` (flat PDF or legacy)."""
    return (output_dir / f"{name}.pdf").is_file() or (
        (output_dir / name).is_dir() or (output_dir / LEGACY_PDF_DIR / name).is_dir()
    )


def archive_entries(output_dir: Path) -> list[tuple[Path, str | None]]:
    """Every rendition under ``output_dir`` with the identity it records.

    Flat PDFs (``<output>/<name>.pdf``, identity from XMP) plus legacy folders
    directly in ``output_dir`` or in its ``pdf/`` subdirectory (identity from
    the manifest; folders without one are not imapArc's and are skipped). A PDF
    without imapArc metadata is listed with ``None``.
    """
    entries: list[tuple[Path, str | None]] = []
    for base in (output_dir, output_dir / LEGACY_PDF_DIR):
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.is_file() and entry.suffix == ".pdf":
                entries.append((entry, provenance.identity(entry)))
            elif entry.is_dir() and (entry / MANIFEST).is_file():
                entries.append((entry, read_identity(entry)))
    return entries


@dataclass
class ArchiveIndex:
    """Which mails an output directory already holds — by identity, not name.

    Looking a mail up under the name it *would* get is not enough: the name
    depends on the naming scheme (which changes between versions and per
    profile) and on ``-2``/``-3`` counters, so a renamed scheme would otherwise
    render every archived mail a second time under its new name. Built once per
    output directory and run, before any render starts.

    Matching follows :func:`same_mail`: a stored hash is compared only with the
    hash; a manifest from before 26.8.12 (no hash) by its Message-ID.
    """

    by_hash: dict[str, str] = field(default_factory=dict)
    by_legacy_id: dict[str, str] = field(default_factory=dict)

    @classmethod
    def scan(cls, output_dir: Path) -> ArchiveIndex:
        index = cls()
        for path, stored in archive_entries(output_dir):
            if not stored:
                continue
            name = path.stem if path.is_file() else path.name
            lines = stored.splitlines()
            hashes = [ln for ln in lines if ln.startswith(_HASH_PREFIX)]
            for line in hashes:
                index.by_hash.setdefault(line, name)
            if not hashes:
                index.by_legacy_id.setdefault(lines[0], name)
        return index

    def find(self, identity: str) -> str | None:
        """The name already holding the mail ``identity`` describes, if any."""
        lines = identity.splitlines()
        for line in lines:
            if line.startswith(_HASH_PREFIX) and line in self.by_hash:
                return self.by_hash[line]
        for line in lines:
            if not line.startswith(_HASH_PREFIX) and line in self.by_legacy_id:
                return self.by_legacy_id[line]
        return None


def _resolve_output(
    output_dir: Path,
    base: str,
    identity: str,
    claimed: dict[str, str] | None = None,
    existing: ArchiveIndex | None = None,
) -> tuple[str, bool]:
    """Pick the name to write ``<name>.pdf`` under, and whether to skip.

    - A name reserved in this run **by the same mail** → skip; the sibling render
      is already producing that file.
    - A name reserved by a *different* mail → disambiguate, so the two never race
      on the output path.
    - An existing rendition recording this mail → skip (idempotent).
    - An existing rendition of a *different* mail (or one recording nothing) →
      try ``base-2``, ``base-3``, … so neither is overwritten.

    ``claimed`` maps reserved name → mail identity. It has to carry the identity,
    not just the name: with a plain set, a second copy of one mail saw "name
    taken" and silently took ``-2``, leaving a duplicate that every later run
    then skips. Two UIDs for one message are ordinary — Gmail lists the same
    mail in All Mail and in a label folder, and a re-uploaded mail comes back
    with a fresh UID.

    ``existing`` is checked first: a rendition holding this mail under *any*
    name — an older naming scheme, a drifted suffix — means skip.

    A returned non-skip name is reserved before returning — with no ``await``
    between here and the write, that reservation is atomic within the event loop.
    The file itself is written atomically (temp file, fsync, rename), so its
    presence means the mail is fully rendered.
    """
    if existing is not None and (found := existing.find(identity)) is not None:
        return found, True
    if claimed is None:
        claimed = {}
    candidate = base
    counter = 1
    while True:
        if candidate in claimed:
            if same_mail(claimed[candidate], identity):
                return candidate, True  # same mail, a sibling render has it
            # A different mail holds the name — disambiguate.
        elif (output_dir / f"{candidate}.pdf").exists() or (
            output_dir / candidate
        ).exists():
            if same_mail(stored_identity(output_dir, candidate), identity):
                return candidate, True
            # A different mail (or a foreign file) owns this name.
        else:
            claimed[candidate] = identity
            return candidate, False
        counter += 1
        candidate = f"{base}-{counter}"


def sweep_staging(output_dir: Path) -> None:
    """Remove temp files and staging folders left by an interrupted run.

    A PDF only appears via one atomic rename, so an interruption can never leave
    a half-written *final* file — only a ``.tmp-*`` file (or, from versions
    before 26.11.6, a ``.staging-*`` folder), harmless but not to accrue.
    Called once per output directory before any render starts, so nothing in
    flight is touched.
    """
    if not output_dir.exists():
        return
    for staging in output_dir.glob(f"{STAGING_PREFIX}*"):
        _remove_tree(staging)
    for temp in output_dir.glob(f"{TEMP_PREFIX}*"):
        if temp.is_file():
            temp.unlink(missing_ok=True)


def _remove_tree(path: Path) -> None:
    """Delete a directory tree, unlocking read-only (0400/0500) entries first."""
    if not path.exists():
        return
    for entry in path.rglob("*"):
        with contextlib.suppress(OSError):
            entry.chmod(0o700)
    with contextlib.suppress(OSError):
        path.chmod(0o700)
    shutil.rmtree(path, ignore_errors=True)


def _safe_attachment_name(name: str) -> str:
    """Reduce an attachment name to a safe basename (no path, no control chars).

    ``Path(name).name`` strips directories, but returns ``".."`` unchanged — it
    is a *name*, not a path component, as far as pathlib is concerned. Joining
    that onto a directory would point at its parent, so the two directory
    aliases are rejected outright. Nothing currently escapes without this (an
    existing target is disambiguated to ``..-2``), but that is a side effect of
    :func:`~imaparc.storage.disambiguate`, not a guarantee — and a mail sender
    chooses this string, so it must not depend on one.
    """
    cleaned = _CONTROL_CHARS.sub("", Path(name).name).strip()
    if cleaned in {".", ".."}:
        return "attachment"
    return cleaned or "attachment"


async def _render_page(ctx: BrowserContext, html: str, config: RunConfig) -> bytes:
    """Render a self-contained helper page (separator/info/text) at A4 width."""
    return await render_html_to_pdf(
        ctx, html, reflowed_rendition(), allow_remote=config.allow_remote
    )


async def _render_mail_part(
    ctx: BrowserContext, parsed: ParsedMail, config: RunConfig
) -> tuple[list[_Part], bool]:
    """Render the mail body: overview + reflowed body, then the text version.

    The first part is bookmarked "E-Mail", each section separator by its title.
    """
    parts: list[_Part] = []
    date_str = format_date_human(parsed.headers.date)
    html_ok = True

    # A mail whose HTML is only plain text in a trivial wrapper (no image, table,
    # link, list, heading, emphasis, background …) gets no HTML renditions: they
    # would look identical to the plain-text version, so only that is produced.
    html_is_visual = bool(parsed.html_body) and not html_is_trivial_wrapper(
        parsed.html_body or ""
    )

    if html_is_visual and parsed.html_body:
        try:
            # Inline resolution needs the related resources (cid:/Content-Location
            # parts nested in multipart/related), not just the real attachments.
            prepared = inline_resources(
                parsed.html_body,
                parsed.attachments + parsed.inline_parts,
                allow_remote=config.allow_remote,
            )
            body = prepend_header(
                prepared.html, parsed.headers, show_bcc=True, date_str=date_str
            )
            reflowed = await render_html_to_pdf(
                ctx, body, reflowed_rendition(), allow_remote=config.allow_remote
            )
            # The scaled one-page overview only helps when the mail spans more than
            # one page; for a mail that already fits a single page it is just a
            # smaller duplicate of the reflowed version, so skip it (and its
            # separator — the reflowed then leads with the header).
            if count_pages(reflowed) > 1:
                parts.append(
                    _Part(
                        await render_html_to_pdf(
                            ctx,
                            body,
                            faithful_rendition(),
                            allow_remote=config.allow_remote,
                        )
                    )
                )
                parts.append(await _separator(ctx, "Umbrochene Fassung", config))
            parts.append(_Part(reflowed))
        except RenderError:
            logger.warning("HTML rendering failed; falling back to text only")
            parts.clear()
            html_ok = False

    # The plain-text version is always included: the mail's text/plain part, or —
    # for an HTML-only mail — one derived from the HTML.
    text_body = parsed.text_body or html_to_text(parsed.html_body or "")
    if text_body:
        if parts:
            parts.append(await _separator(ctx, "Nur-Text-Version", config))
        parts.append(
            _Part(
                await _render_page(
                    ctx,
                    render_text(
                        parsed.headers, text_body, show_bcc=True, date_str=date_str
                    ),
                    config,
                )
            )
        )

    if not parts:
        header_only = prepend_header(
            "<html><body></body></html>",
            parsed.headers,
            show_bcc=True,
            date_str=date_str,
        )
        parts.append(_Part(await _render_page(ctx, header_only, config)))

    parts[0] = _Part(parts[0].pdf, "E-Mail")
    return parts, html_ok


async def _separator(ctx: BrowserContext, title: str, config: RunConfig) -> _Part:
    """A section separator page, bookmarked with its own title."""
    return _Part(
        await _render_page(ctx, render_section_separator(title), config), title
    )


async def _render_attachments(
    ctx: BrowserContext,
    parsed: ParsedMail,
    work: Path,
    config: RunConfig,
    eml_name: str,
) -> tuple[list[_Part], list[AttachmentResult], list[tuple[str, str, bytes]]]:
    """Convert attachments to pages.

    Returns the parts (each attachment's separator or info page bookmarked
    "Anhang N: name"), the per-attachment results, and the attachments that did
    not become pages as ``(name, MIME type, content)`` — embedded into the PDF
    unless larger than the attachment size limit (the ``.eml`` holds them anyway).
    """

    async def render_text_cb(filename: str, text: str) -> bytes:
        return await _render_page(ctx, render_text_attachment(filename, text), config)

    parts: list[_Part] = []
    results: list[AttachmentResult] = []
    kept: list[tuple[str, str, bytes]] = []
    for index, att in enumerate(parsed.attachments, start=1):
        outcome = await convert_attachment(
            att,
            render_text=render_text_cb,
            qpdf=config.tools.qpdf,
            work_dir=work,
            max_bytes=config.max_attachment_bytes,
            timeout_s=config.attachment_timeout_s,
        )
        results.append(AttachmentResult(att.filename, outcome))
        size = format_file_size(att.size)
        mark = f"Anhang {index}: {att.filename}"
        if outcome.ok and outcome.pdf_bytes is not None:
            separator = render_separator(index, att.filename, outcome.page_count, size)
            parts.append(_Part(await _render_page(ctx, separator, config), mark))
            parts.append(_Part(outcome.pdf_bytes))
        else:
            embedded = att.size <= config.max_attachment_bytes
            if embedded:
                kept.append(
                    (
                        _safe_attachment_name(att.filename),
                        effective_mime(att),
                        att.content,
                    )
                )
            reason = outcome.reason.message if outcome.reason else ""
            info = render_info(
                index,
                att.filename,
                effective_mime(att),
                size,
                reason,
                embedded=embedded,
                eml_name=eml_name,
            )
            parts.append(_Part(await _render_page(ctx, info, config), mark))
    return parts, results, kept


def _merge_to_pdfa(pages: list[bytes], stem: Path, config: RunConfig) -> bytes:
    """Merge PDF page-parts and convert the result to PDF/A-3b bytes."""
    merged = merge_pdfs(pages)
    src = stem.with_name(stem.name + "-merged.pdf")
    dst = stem.with_name(stem.name + "-pdfa.pdf")
    src.write_bytes(merged)
    to_pdfa3b(
        src, dst, gs=config.tools.gs, icc=config.icc_profile, work_dir=stem.parent
    )
    return dst.read_bytes()
