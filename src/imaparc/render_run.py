"""Orchestrate the render phase: each profile's waiting mail → one PDF each.

The counterpart to :mod:`imaparc.fetch` for the second phase, and the sibling of
:mod:`imaparc.adhoc` for the profile-driven case. Lives here rather than in the
CLI so that ``cli.py`` holds commands and nothing else.

Mails are carried as **paths**, not as loaded bytes: a run over a large archive
otherwise holds every message in memory before rendering the first page.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from rich.progress import Progress, TaskID

from imaparc.config import RunConfig, ToolPaths
from imaparc.console import make_progress
from imaparc.exceptions import ImapArcError
from imaparc.mail.parser import parse_mail
from imaparc.pipeline import (
    ArchiveIndex,
    RenderResult,
    is_rendered,
    render_mail,
    sweep_staging,
)
from imaparc.profiles import (
    Profile,
    archived_eml_dir,
    error_eml_dir,
    input_eml_dir,
    pending_eml_dirs,
)
from imaparc.render.browser import BrowserPool
from imaparc.report import RunReport, validate_pdfa
from imaparc.sources.deliver import failure_reason, file_away, file_error
from imaparc.sources.eml import EmlSource
from imaparc.storage import DIR_MODE, make_dir

logger = logging.getLogger(__name__)


def received_from_file(path: Path) -> datetime | None:
    """Delivery time (file mtime) — the timestamp fallback for a mail without Date."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


async def run_render(
    profiles: list[Profile],
    tools: ToolPaths,
    cli_remote: bool,
    cli_jobs: int | None,
    verbosity: int,
    *,
    cli_no_ocr: bool = False,
) -> RunReport:
    """Render each profile's mail with its own settings, then validate.

    ``remote_images``/``jobs`` are per-profile; the CLI flags override them for
    the whole run. Each profile gets its own :class:`BrowserPool` and
    concurrency, so a profile that does not want remote images keeps the full
    network lockdown even when another profile in the same run does.

    Every mail is *listed* up front so the progress bar knows its total from the
    start — listing is cheap because the contents stay on disk until each mail's
    turn. A second bar tracks PDF/A validation.
    """
    report = RunReport()
    plans: list[tuple[Profile, RunConfig, Path, list[Path]]] = []
    for profile in profiles:
        # Only what waits: the profile's eml-input directory (and the eml/ inbox
        # of versions before 26.11.6). The eml-archive is never read here.
        sources = [d for d in pending_eml_dirs(profile.output) if d.is_dir()]
        mails = [path for source in sources for path in EmlSource(source).paths()]
        if not sources:
            logger.debug(
                "profile '%s': no %s — nothing fetched yet",
                profile.name,
                input_eml_dir(profile.output),
            )
            continue
        jobs = cli_jobs if cli_jobs is not None else profile.jobs
        config = RunConfig(
            tools=tools,
            verbosity=verbosity,
            jobs=jobs,
            gs_jobs=profile.gs_jobs,
            allow_remote=cli_remote or profile.remote_images,
            ocr=profile.ocr and not cli_no_ocr,
            ocr_languages=profile.ocr_languages,
            # Same source as fetch's naming, so the .eml and this profile's PDF
            # folders keep sharing one base name.
            filename_pattern=profile.filename_pattern,
            date_format=profile.date_format,
            max_attachment_bytes=profile.max_attachment_bytes,
            attachment_timeout_s=profile.attachment_timeout_s,
            render_timeout_ms=profile.render_timeout_ms,
        )
        plans.append((profile, config, profile.output, mails))

    total = sum(len(mails) for *_, mails in plans)
    with make_progress(disable=verbosity == 0 or total == 0) as progress:
        task = progress.add_task("Rendering mail", total=total)
        for profile, config, output, mails in plans:
            if not mails:
                # Nothing waits: no browser to start. Leftovers of an aborted
                # run are still cleared, as a render would have done.
                if output.is_dir():
                    sweep_staging(output)
                continue
            semaphore = asyncio.Semaphore(config.jobs)
            # Separate bound: Ghostscript holds a whole document in memory, so it
            # must not scale with the number of mails rendered at once.
            gs_semaphore = asyncio.Semaphore(config.gs_jobs)
            async with BrowserPool(
                allow_remote=config.allow_remote, timeout_ms=config.render_timeout_ms
            ) as pool:
                await _render_profile(
                    profile,
                    output,
                    mails,
                    pool,
                    semaphore,
                    gs_semaphore,
                    config,
                    report,
                    progress,
                    task,
                )
        validate_pdfa(report, tools, progress)
    return report


async def _render_profile(
    profile: Profile,
    output: Path,
    mails: list[Path],
    pool: BrowserPool,
    semaphore: asyncio.Semaphore,
    gs_semaphore: asyncio.Semaphore,
    config: RunConfig,
    report: RunReport,
    progress: Progress,
    task: TaskID,
) -> None:
    """Render one profile's waiting mail to ``<output>/<basename>.pdf``.

    Each mail leaves the input one of two ways: with its PDF written (or found
    already rendered), its ``.eml`` moves to ``eml-archive``; if it cannot be
    rendered correctly, no PDF is written and the ``.eml`` moves to
    ``eml-error`` with a note saying why. Archiving happens after every render
    has finished, so a copy skipped because its twin was still rendering finds
    that PDF in place too.
    """
    make_dir(output)
    os.chmod(output, 0o700)  # owner-writable, private (same as DIR_MODE at rest)
    sweep_staging(output)  # clear temp files left by a previously aborted run

    # Shared across this profile's concurrent renders: distinct mails resolving
    # to one basename reserve distinct names, while two copies of the same mail
    # (two UIDs, one Message-ID) collapse into a single PDF.
    claimed: dict[str, str] = {}
    # What is already rendered, by identity: a mail stored under an older naming
    # scheme is recognised instead of being rendered again under its new name.
    existing = ArchiveIndex.scan(output)
    archive_dir = archived_eml_dir(output)
    error_dir = error_eml_dir(output)
    pending: list[tuple[Path, str, str]] = []

    def failed(path: Path, raw: bytes | None, exc: BaseException) -> None:
        reason, details = failure_reason(exc)
        report.failed.append((str(path), reason))
        if raw is None:
            return  # could not even be read: nothing to move
        try:
            file_error(path, raw, error_dir, path.stem, reason, details)
        except OSError as move_error:
            logger.error("Could not move %s to %s: %s", path, error_dir, move_error)

    async def process(path: Path) -> RenderResult | None:
        async with semaphore:
            raw: bytes | None = None
            try:
                # Read here, not when listing: only `jobs` mails are in memory
                # at any moment, regardless of how much mail is waiting.
                raw = path.read_bytes()
                parsed = parse_mail(raw)
                result = await render_mail(
                    parsed,
                    raw=raw,
                    profile=profile.name,
                    output_dir=output,
                    pool=pool,
                    config=config,
                    received=received_from_file(path),
                    claimed=claimed,
                    existing=existing,
                    gs_semaphore=gs_semaphore,
                )
                # One line per mail at -v so a long run can be watched; the
                # locating and diagnostic detail goes to -vv.
                if result.skipped:
                    logger.debug(
                        "%s ∘ %s (already rendered)", profile.name, result.basename
                    )
                else:
                    logger.info(
                        "%s → %s",
                        profile.name,
                        parsed.headers.subject or "(no subject)",
                    )
                    logger.debug(
                        "  %s  %d attachment(s)%s",
                        result.basename,
                        len(parsed.attachments),
                        "" if result.complete else ", some embedded as files",
                    )
                digest = hashlib.sha256(raw).hexdigest()
                pending.append((path, result.basename, digest))
                return result
            except (ImapArcError, OSError) as exc:
                # One bad mail must not abort the whole profile's render.
                logger.error("Failed to render %s: %s", path, exc)
                failed(path, raw, exc)
                return None
            except Exception as exc:
                # Anything else a mail can provoke (the sender controls its
                # bytes): named, counted, and the run goes on.
                logger.exception("Failed to render %s: %s", path, exc)
                failed(path, raw, exc)
                return None
            finally:
                progress.advance(task)

    try:
        results = await asyncio.gather(
            *(process(m) for m in mails), return_exceptions=True
        )
        for result in results:
            if isinstance(result, RenderResult):
                report.add(result)
            elif isinstance(result, BaseException):
                logger.error("Unexpected error while rendering a mail: %s", result)
        # Only now, with every render finished: a copy skipped because its twin
        # was still being written finds that PDF in place too.
        for path, basename, digest in pending:
            if not is_rendered(output, basename):
                continue  # its twin failed: stays in the input for the next run
            try:
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != digest:
                    # Not the bytes the PDF was made from: leave it for the
                    # next run rather than archive a mail no PDF matches.
                    logger.warning("%s changed while rendering; left", path)
                    continue
                file_away(path, data, [(archive_dir, path.stem)])
            except OSError as exc:
                logger.error("Could not archive %s: %s", path, exc)
    finally:
        os.chmod(output, DIR_MODE)  # immutable at rest
