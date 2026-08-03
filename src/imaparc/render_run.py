"""Orchestrate the render phase: profile ``eml/`` directories → ``pdf/`` folders.

The counterpart to :mod:`imaparc.fetch` for the second phase, and the sibling of
:mod:`imaparc.adhoc` for the profile-driven case. Lives here rather than in the
CLI so that ``cli.py`` holds commands and nothing else.

Mails are carried as **paths**, not as loaded bytes: a run over a large archive
otherwise holds every message in memory before rendering the first page.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from rich.progress import Progress, TaskID

from imaparc.config import RunConfig, ToolPaths
from imaparc.console import make_progress
from imaparc.exceptions import ImapArcError
from imaparc.mail.parser import parse_mail
from imaparc.pipeline import RenderResult, render_mail, sweep_staging
from imaparc.profiles import Profile
from imaparc.render.browser import BrowserPool
from imaparc.report import RunReport, validate_pdfa
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
) -> RunReport:
    """Render each profile's ``eml/`` with its own settings, then validate.

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
        eml_dir = profile.output / "eml"
        if not eml_dir.is_dir():
            logger.warning(
                "profile '%s': no eml/ at %s — run fetch first", profile.name, eml_dir
            )
            continue
        jobs = cli_jobs if cli_jobs is not None else profile.jobs
        config = RunConfig(
            tools=tools,
            verbosity=verbosity,
            jobs=jobs,
            gs_jobs=profile.gs_jobs,
            allow_remote=cli_remote or profile.remote_images,
            # Same source as fetch's naming, so the .eml and this profile's PDF
            # folders keep sharing one base name.
            filename_pattern=profile.filename_pattern,
            date_format=profile.date_format,
            max_attachment_bytes=profile.max_attachment_bytes,
            attachment_timeout_s=profile.attachment_timeout_s,
            render_timeout_ms=profile.render_timeout_ms,
        )
        plans.append(
            (profile, config, profile.output / "pdf", EmlSource(eml_dir).paths())
        )

    total = sum(len(mails) for *_, mails in plans)
    with make_progress(disable=verbosity == 0 or total == 0) as progress:
        task = progress.add_task("Rendering mail", total=total)
        for profile, config, output, mails in plans:
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
    """Render every mail in one profile's ``eml/`` into its ``pdf/`` directory."""
    make_dir(output)
    os.chmod(output, 0o700)  # owner-writable, private (same as DIR_MODE at rest)
    sweep_staging(output)  # clear any .staging-* left by a previously aborted run

    # Shared across this profile's concurrent renders: distinct mails resolving
    # to one basename reserve distinct names, while two copies of the same mail
    # (two UIDs, one Message-ID) collapse into a single folder.
    claimed: dict[str, str] = {}

    async def process(path: Path) -> RenderResult | None:
        async with semaphore:
            try:
                # Read here, not when listing: only `jobs` mails are in memory
                # at any moment, regardless of how large the archive is.
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
                        "" if result.complete else ", some kept as originals",
                    )
                return result
            except (ImapArcError, OSError) as exc:
                # One bad mail must not abort the whole profile's render.
                logger.error("Failed to render %s: %s", path, exc)
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
    finally:
        os.chmod(output, DIR_MODE)  # immutable at rest
