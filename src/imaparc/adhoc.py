"""File mode: render ``.eml`` files from anywhere into the profile archive.

This is the ``eml`` command's engine. A mail — dragged out of a mail client, or
lying anywhere on disk — is filed exactly as ``fetch`` + ``render`` would file
it: its profile is derived from its **headers** with the rules in
``profile.yaml``, the PDF goes to ``<profile output>/pdf/<basename>/``, and once
that folder exists the ``.eml`` moves into ``eml-Archiv/<output name>/``. A mail
matching several profiles is filed into each of them; one matching none goes to
``<root>/00_Mails/``.

Still isolated from the IMAP side: no connection, no ``.env``, no state store.
Nothing here can affect what a later ``fetch`` considers delivered.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from imaparc.config import RunConfig, ToolPaths
from imaparc.console import make_progress
from imaparc.exceptions import ImapArcError, SourceError
from imaparc.mail.parser import parse_mail
from imaparc.pipeline import ArchiveIndex, RenderResult, render_mail, sweep_staging
from imaparc.profiles import Profile, archived_eml_dir, matching_profiles
from imaparc.render.browser import BrowserPool
from imaparc.report import RunReport, validate_pdfa
from imaparc.sources.deliver import file_away
from imaparc.storage import make_dir

logger = logging.getLogger(__name__)

_SUFFIX = ".eml"
FALLBACK_DIR = "00_Mails"


def collect_eml(paths: list[Path]) -> list[Path]:
    """Resolve command arguments to the ``.eml`` files to render.

    A file argument contributes itself; a directory contributes the ``.eml``
    files directly inside it, sorted by name. The scan is **not** recursive, so
    a directory never pulls in mail attached to another mail inside a folder.

    Argument order is kept, and a file reached twice (named directly and via its
    directory) appears once.

    Args:
        paths: File or directory arguments; empty means the current directory.

    Returns:
        The resolved ``.eml`` paths, in the order they should be rendered.

    Raises:
        SourceError: If a path does not exist or is a file that is not an
            ``.eml``.
    """
    found: list[Path] = []
    seen: set[Path] = set()
    for argument in paths or [Path.cwd()]:
        path = argument.resolve()
        if not path.exists():
            raise SourceError(f"not found: {argument}")
        if path.is_dir():
            entries = sorted(
                entry
                for entry in path.iterdir()
                if entry.is_file() and entry.suffix == _SUFFIX
            )
        elif path.suffix != _SUFFIX:
            raise SourceError(f"not an .eml file: {argument}")
        else:
            entries = [path]
        for entry in entries:
            if entry not in seen:
                seen.add(entry)
                found.append(entry)
    return found


def _received(eml: Path) -> datetime | None:
    """Timestamp fallback when the mail carries no usable ``Date`` header."""
    try:
        return datetime.fromtimestamp(eml.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def fallback_profile(root: Path, name: str) -> Profile:
    """Where a mail goes that no profile claims: ``<root>/00_Mails``."""
    return Profile(name=name, account="-", output=root / FALLBACK_DIR, pdf=True)


@dataclass
class _Target:
    """Per-profile render state, shared by every mail filed into that profile."""

    profile: Profile
    config: RunConfig
    output: Path
    existing: ArchiveIndex
    claimed: dict[str, str] = field(default_factory=dict)


async def run_adhoc(
    files: list[Path],
    *,
    profiles: list[Profile],
    root: Path,
    name: str,
    tools: ToolPaths,
    jobs: int = 4,
    gs_jobs: int = 2,
    verbosity: int = 1,
) -> RunReport:
    """Render ``.eml`` files into the profile archive and archive each one.

    The ``.eml`` is only moved once the PDF folder exists for **every** profile
    it was filed into (rendered now, or recognised as rendered by its manifest);
    a mail that failed anywhere stays where it was, so a rerun picks it up.

    **Remote images are always loaded here**, unlike ``render`` where they are
    opt-in per profile. This command renders a mail the user picked out by hand
    and typically already opened in a client, so the faithful rendition is what
    is wanted and any tracking pixel has long since fired. The trade-off is real
    and one-way: rendering fetches those URLs.

    Args:
        files: The ``.eml`` files to render, as returned by :func:`collect_eml`.
        profiles: The configured profiles; their header rules pick the target.
        root: Archive root; unmatched mail goes to ``<root>/00_Mails``.
        name: Profile name used for unmatched mail (the middle of its name).
        tools: Resolved gs/qpdf/verapdf paths.
        jobs: Mails rendered concurrently.
        gs_jobs: Ghostscript conversions at once. Deliberately lower than
            ``jobs``: one conversion holds a whole document in memory.
        verbosity: 0 silences the progress display.

    Returns:
        A report over everything rendered, with PDF/A findings filled in.
    """
    report = RunReport()
    if not files:
        return report

    fallback = fallback_profile(root, name)
    targets: dict[Path, _Target] = {}

    def target(profile: Profile) -> _Target:
        # Created on first use, synchronously (no await), so concurrent mails
        # filed into the same profile share one reservation map and index.
        output = profile.output / "pdf"
        if output not in targets:
            make_dir(output, readonly=True)
            sweep_staging(output)
            targets[output] = _Target(
                profile=profile,
                config=RunConfig(
                    tools=tools,
                    verbosity=verbosity,
                    jobs=jobs,
                    gs_jobs=gs_jobs,
                    allow_remote=True,
                    filename_pattern=profile.filename_pattern,
                    date_format=profile.date_format,
                    max_attachment_bytes=profile.max_attachment_bytes,
                    attachment_timeout_s=profile.attachment_timeout_s,
                    render_timeout_ms=profile.render_timeout_ms,
                ),
                output=output,
                existing=ArchiveIndex.scan(output),
            )
        return targets[output]

    semaphore = asyncio.Semaphore(jobs)
    # Separate from `semaphore`: Ghostscript holds a whole document in memory,
    # so it needs a tighter bound than the number of mails rendered at once.
    gs_semaphore = asyncio.Semaphore(gs_jobs)

    with make_progress(disable=verbosity == 0) as progress:
        task = progress.add_task("Rendering mail", total=len(files))
        async with BrowserPool(
            allow_remote=True, timeout_ms=fallback.render_timeout_ms
        ) as pool:

            async def process(eml: Path) -> list[RenderResult]:
                done: list[tuple[_Target, RenderResult]] = []
                async with semaphore:
                    try:
                        raw = eml.read_bytes()
                        parsed = parse_mail(raw)
                        chosen = matching_profiles(
                            profiles,
                            parsed.headers,
                            [a.filename for a in parsed.attachments],
                        ) or [fallback]
                        for profile in chosen:
                            t = target(profile)
                            result = await render_mail(
                                parsed,
                                raw=raw,
                                profile=profile.name,
                                output_dir=t.output,
                                pool=pool,
                                config=t.config,
                                received=_received(eml),
                                claimed=t.claimed,
                                existing=t.existing,
                                gs_semaphore=gs_semaphore,
                            )
                            done.append((t, result))
                            # One line per mail at -v; the detail goes to -vv.
                            if result.skipped:
                                logger.debug("∘ %s (already rendered)", result.basename)
                            else:
                                logger.info("%s → %s", profile.name, eml.name)
                                logger.debug("  %s", result.basename)
                        # After every folder's atomic rename, never before: the
                        # mail is only archived once all its PDFs are complete.
                        if all((t.output / r.basename).is_dir() for t, r in done):
                            archived = file_away(
                                eml,
                                raw,
                                [
                                    (archived_eml_dir(t.profile.output), r.basename)
                                    for t, r in done
                                ],
                            )
                            logger.debug("archived %s → %s", eml.name, archived)
                    except (ImapArcError, OSError) as exc:
                        # One unreadable mail must not abort the whole run.
                        logger.error("Failed to render %s: %s", eml, exc)
                    finally:
                        progress.advance(task)
                return [r for _, r in done]

            results = await asyncio.gather(
                *(process(eml) for eml in files), return_exceptions=True
            )
        for outcome in results:
            if isinstance(outcome, list):
                for result in outcome:
                    report.add(result)
            elif isinstance(outcome, BaseException):
                logger.error("Unexpected error while rendering a mail: %s", outcome)
        validate_pdfa(report, tools, progress)
    return report
