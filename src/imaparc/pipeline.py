"""Render one parsed mail into the fixed PDF double-structure.

This is where every render building block comes together: Chromium renders the
three body renditions and each separator/info page, ``convert_attachment`` turns
attachments into pages (or reports why not), the parts are merged and passed
through Ghostscript to PDF/A-3b, and the result is written to the immutable
archive.

Every mail is stored under its own ``<basename>/`` folder. ``<basename>.pdf`` is
the full PDF the reader opens: body + attachment pages when the mail has
attachments, otherwise just the body. A mail **with** attachments additionally
gets ``<basename>_mailonly.pdf`` (the body without attachment pages) and the
original attachment files alongside. So a mail's PDF is never duplicated across
two locations.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
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
from imaparc.pdf.merge import count_pages, merge_pdfs
from imaparc.pdf.pdfa import to_pdfa3b
from imaparc.render.browser import BrowserPool
from imaparc.render.geometry import faithful_rendition, reflowed_rendition
from imaparc.render.pdf_render import render_html_to_pdf
from imaparc.storage import DIR_MODE, make_dir, write_readonly

logger = logging.getLogger(__name__)

# Hidden manifest in each mail's subfolder, holding the mail identity so a rerun
# (same mail) can be told apart from a basename collision (a different mail).
_MANIFEST = ".imaparc-manifest"
_STAGING_PREFIX = ".staging-"
_CONTROL_CHARS = re.compile(r"[\x00-\x1f]")


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
    mail_only_pdf: Path | None = None
    attachments: tuple[AttachmentResult, ...] = field(default_factory=tuple)


async def render_mail(
    parsed: ParsedMail,
    *,
    profile: str,
    output_dir: Path,
    pool: BrowserPool,
    config: RunConfig,
    received: datetime | None = None,
    claimed: set[str] | None = None,
) -> RenderResult:
    """Render one mail to the double PDF structure under ``output_dir``.

    ``output_dir`` must be writable (the caller keeps it unlocked for the run).
    Returns a :class:`RenderResult`; skips (does nothing) if the output already
    exists, which makes a repeat run idempotent.

    ``claimed`` is a shared set of basenames already reserved by concurrent
    renders in the same run. Two distinct mails can resolve to the same base
    (identical ``Date`` header and subject); passing one set across all of a
    profile's concurrent renders makes the name reservation race-free, so the
    second mail disambiguates instead of colliding on the output path.
    """
    timestamp = parsed.headers.date or received
    base = build_base_name(
        timestamp,
        profile,
        parsed.headers.subject or "",
        pattern=config.filename_pattern,
        date_format=config.date_format,
    )
    identity = _mail_identity(parsed, timestamp)
    basename, skip = _resolve_output(output_dir, base, identity, claimed)
    if skip:
        return RenderResult(basename, written=False, skipped=True)

    subfolder = output_dir / basename
    has_attachments = bool(parsed.attachments)

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        async with pool.context() as ctx:
            mail_part, html_ok = await _render_mail_part(ctx, parsed, config)
            attachment_pages, results = await _render_attachments(
                ctx, parsed, work, config
            )

        # <basename>.pdf is the full PDF the reader opens: with attachments it is
        # body + attachment pages; without, just the body. The body-only version
        # is written alongside as <basename>_mailonly.pdf only when it differs
        # (i.e. only when there are attachments).
        mail_only_pdfa = await asyncio.to_thread(
            _merge_to_pdfa, mail_part, work / "mailonly", config
        )
        if has_attachments:
            primary_pdfa = await asyncio.to_thread(
                _merge_to_pdfa, mail_part + attachment_pages, work / "full", config
            )
            mailonly_pdfa: bytes | None = mail_only_pdfa
        else:
            primary_pdfa = mail_only_pdfa
            mailonly_pdfa = None
        _store(
            output_dir,
            basename,
            identity=identity,
            primary_pdfa=primary_pdfa,
            mailonly_pdfa=mailonly_pdfa,
            originals=[(a.filename, a.content) for a in parsed.attachments],
        )

    return RenderResult(
        basename,
        written=True,
        html_rendered=html_ok,
        complete=all(r.outcome.ok for r in results),
        combined_pdf=subfolder / f"{basename}.pdf",
        mail_only_pdf=(
            subfolder / f"{basename}_mailonly.pdf"
            if has_attachments
            else subfolder / f"{basename}.pdf"
        ),
        attachments=tuple(results),
    )


def _mail_identity(parsed: ParsedMail, timestamp: datetime | None) -> str:
    """A stable identity for a mail — the Message-ID, or a header composite."""
    if parsed.headers.message_id:
        return parsed.headers.message_id
    return f"{parsed.headers.from_}|{parsed.headers.subject}|{timestamp}"


def _read_identity(subfolder: Path) -> str | None:
    """Read the stored mail identity from a subfolder's manifest, if present."""
    try:
        return (subfolder / _MANIFEST).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _resolve_output(
    output_dir: Path, base: str, identity: str, claimed: set[str] | None = None
) -> tuple[str, bool]:
    """Pick the per-mail folder name to write under and whether to skip.

    Every mail is stored under its own ``output_dir/<basename>/`` folder, built
    in staging and moved into place with a single atomic rename — so a folder's
    presence means the mail is fully written; there is no half-written folder to
    repair.

    - A basename already reserved by a concurrent render in this run
      (``candidate in claimed``) → disambiguate, so two mails that resolve to the
      same base never race on the output path.
    - An existing folder whose manifest matches this mail → skip (idempotent).
    - An existing folder belonging to a *different* mail (or with an unreadable
      manifest) → try ``base-2``, ``base-3``, … so neither is overwritten.

    A returned non-skip basename is added to ``claimed`` before returning — with
    no ``await`` between here and the write, that reservation is atomic within
    the event loop.
    """
    if claimed is None:
        claimed = set()
    candidate = base
    counter = 1
    while True:
        subfolder = output_dir / candidate
        if candidate in claimed:
            pass  # reserved by an in-flight sibling render — disambiguate
        elif subfolder.exists():
            if _read_identity(subfolder) == identity:
                return candidate, True
            # A different mail (or corrupt folder) owns this name — disambiguate.
        else:
            claimed.add(candidate)
            return candidate, False
        counter += 1
        candidate = f"{base}-{counter}"


def sweep_staging(output_dir: Path) -> None:
    """Remove any ``.staging-*`` folders left by a previously interrupted run.

    A per-mail folder only appears via one atomic rename, so an interruption can
    never leave a half-written *final* folder — only a leftover staging folder,
    which is harmless (the idempotency check ignores it) but should not accrue.
    Called once at the start of a profile's render, before any concurrent mail
    render creates its own staging folder, so nothing in flight is touched.
    """
    if not output_dir.exists():
        return
    for staging in output_dir.glob(f"{_STAGING_PREFIX}*"):
        _remove_tree(staging)


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
    that onto the staging folder would point at its parent, so the two directory
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
) -> tuple[list[bytes], bool]:
    """Render the shared mail part: faithful + reflowed body, then text."""
    parts: list[bytes] = []
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
                    await render_html_to_pdf(
                        ctx,
                        body,
                        faithful_rendition(),
                        allow_remote=config.allow_remote,
                    )
                )
                parts.append(
                    await _render_page(
                        ctx, render_section_separator("Umbrochene Fassung"), config
                    )
                )
            parts.append(reflowed)
        except RenderError:
            logger.warning("HTML rendering failed; falling back to text only")
            parts.clear()
            html_ok = False

    # The plain-text version is always included: the mail's text/plain part, or —
    # for an HTML-only mail — one derived from the HTML.
    text_body = parsed.text_body or html_to_text(parsed.html_body or "")
    if text_body:
        if parts:
            parts.append(
                await _render_page(
                    ctx, render_section_separator("Nur-Text-Version"), config
                )
            )
        parts.append(
            await _render_page(
                ctx,
                render_text(
                    parsed.headers, text_body, show_bcc=True, date_str=date_str
                ),
                config,
            )
        )

    if not parts:
        header_only = prepend_header(
            "<html><body></body></html>",
            parsed.headers,
            show_bcc=True,
            date_str=date_str,
        )
        parts.append(await _render_page(ctx, header_only, config))

    return parts, html_ok


async def _render_attachments(
    ctx: BrowserContext, parsed: ParsedMail, work: Path, config: RunConfig
) -> tuple[list[bytes], list[AttachmentResult]]:
    """Convert attachments to pages; return the pages and per-attachment results."""

    async def render_text_cb(filename: str, text: str) -> bytes:
        return await _render_page(ctx, render_text_attachment(filename, text), config)

    pages: list[bytes] = []
    results: list[AttachmentResult] = []
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
        if outcome.ok and outcome.pdf_bytes is not None:
            separator = render_separator(index, att.filename, outcome.page_count, size)
            pages.append(await _render_page(ctx, separator, config))
            pages.append(outcome.pdf_bytes)
        else:
            reason = outcome.reason.message if outcome.reason else ""
            info = render_info(index, att.filename, effective_mime(att), size, reason)
            pages.append(await _render_page(ctx, info, config))
    return pages, results


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


def _store(
    output_dir: Path,
    basename: str,
    *,
    identity: str,
    primary_pdfa: bytes,
    mailonly_pdfa: bytes | None,
    originals: list[tuple[str, bytes]],
) -> None:
    """Write one mail's folder atomically with archive permissions.

    Every mail gets its own ``<basename>/`` folder. ``<basename>.pdf`` is the
    full PDF the reader opens (body + attachment pages when there are
    attachments; otherwise just the body). When it has attachments, the
    body-only version is written alongside as ``<basename>_mailonly.pdf`` and the
    original attachment files are kept in the folder too. There is never a loose
    PDF at the output root, so a mail's PDF is not duplicated across two places.

    The folder is built under a staging name and moved into place with a single
    atomic rename, so its presence means the mail is complete — nothing is
    written after the rename. Our reserved PDF names are written before the
    originals, so an attachment that happens to share a name is disambiguated
    rather than clobbering them. Files are 0400, the folder 0700 (via
    write_readonly + disambiguate); a ``.imaparc-manifest`` records the mail
    identity so a later run tells a re-render from a distinct basename collision.
    """
    staging = output_dir / f"{_STAGING_PREFIX}{basename}"
    _remove_tree(staging)
    make_dir(staging)
    write_readonly(staging / _MANIFEST, identity.encode("utf-8"))
    write_readonly(staging / f"{basename}.pdf", primary_pdfa)
    if mailonly_pdfa is not None:
        write_readonly(staging / f"{basename}_mailonly.pdf", mailonly_pdfa)
    for name, content in originals:
        write_readonly(staging / _safe_attachment_name(name), content)
    os.chmod(staging, DIR_MODE)
    os.rename(staging, output_dir / basename)
