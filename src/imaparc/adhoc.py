"""Ad-hoc rendering of loose ``.eml`` files, without a profile.

This is the ``eml`` command's engine. It renders mail where it already lies on
disk — typically a message dragged out of a mail client — and afterwards moves
the ``.eml`` into the folder that was rendered for it.

Deliberately isolated from the archive path: no IMAP connection, no ``.env``, no
state store. Nothing here can affect what a later ``fetch`` considers delivered.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from imaparc.config import RunConfig, ToolPaths
from imaparc.console import make_progress
from imaparc.exceptions import ImapArcError, SourceError
from imaparc.mail.parser import parse_mail
from imaparc.pipeline import RenderResult, render_mail, sweep_staging
from imaparc.render.browser import BrowserPool
from imaparc.report import RunReport, validate_pdfa
from imaparc.storage import FILE_MODE, disambiguate

logger = logging.getLogger(__name__)

_SUFFIX = ".eml"


def collect_eml(paths: list[Path]) -> list[Path]:
    """Resolve command arguments to the ``.eml`` files to render.

    A file argument contributes itself; a directory contributes the ``.eml``
    files directly inside it, sorted by name. The scan is **not** recursive: a
    rendered mail's ``.eml`` ends up one level down in its own folder, and a
    recursive scan would collect it again on the next run.

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


def move_into_folder(eml: Path, folder: Path) -> Path | None:
    """Move a rendered mail's ``.eml`` into its output folder.

    Runs *after* the folder was moved into place by a single atomic rename, so
    the "folder present means fully rendered" guarantee still holds for the PDFs.
    The move is the one thing that happens afterwards, and it is idempotent: a
    source file that is already gone means a previous run did it, and a run
    interrupted between rendering and moving is repaired by simply running again.

    The file is renamed to the folder's own name, matching how the archive names
    a mail's ``.eml``. An attachment that happens to occupy that name is not
    overwritten — the ``.eml`` is disambiguated instead.

    Args:
        eml: The source ``.eml``.
        folder: The mail's output folder (``0700``, so no unlocking is needed).

    Returns:
        The path written, or ``None`` if there was nothing left to move.
    """
    if not eml.exists():
        return None
    target = disambiguate(folder / f"{folder.name}{_SUFFIX}")
    # shutil, not os.rename: a mail dragged out of a client lands in a temp
    # directory that may be on a different filesystem than the target.
    shutil.move(str(eml), str(target))
    os.chmod(target, FILE_MODE)
    return target


def _received(eml: Path) -> datetime | None:
    """Timestamp fallback when the mail carries no usable ``Date`` header."""
    try:
        return datetime.fromtimestamp(eml.stat().st_mtime, tz=UTC)
    except OSError:
        return None


async def run_adhoc(
    files: list[Path],
    *,
    name: str,
    tools: ToolPaths,
    allow_remote: bool = False,
    jobs: int = 4,
    verbosity: int = 1,
) -> RunReport:
    """Render loose ``.eml`` files in place, then move each into its folder.

    Each mail is rendered into ``<basename>/`` **next to its own ``.eml``**, so
    arguments from several directories each stay where they came from.

    Unlike the archive path, the containing directory is left exactly as it is:
    it belongs to the user (a Desktop, a downloads folder), not to imapArc, so
    its permissions are never touched. Only the per-mail folder that imapArc
    creates gets archive permissions.

    Args:
        files: The ``.eml`` files to render, as returned by :func:`collect_eml`.
        name: Fills the profile slot of the base name.
        tools: Resolved gs/qpdf/verapdf paths.
        allow_remote: Permit loading remote images while rendering.
        jobs: Mails rendered concurrently.
        verbosity: 0 silences the progress display.

    Returns:
        A report over everything rendered, with PDF/A findings filled in.
    """
    report = RunReport()
    if not files:
        return report

    by_directory: dict[Path, list[Path]] = defaultdict(list)
    for eml in files:
        by_directory[eml.parent].append(eml)
    for directory in by_directory:
        sweep_staging(directory)

    config = RunConfig(
        tools=tools, verbosity=verbosity, jobs=jobs, allow_remote=allow_remote
    )
    semaphore = asyncio.Semaphore(config.jobs)

    with make_progress(disable=verbosity == 0) as progress:
        task = progress.add_task("Rendering mail", total=len(files))
        async with BrowserPool(
            allow_remote=config.allow_remote, timeout_ms=config.render_timeout_ms
        ) as pool:
            # One reservation set per directory: base names only have to be
            # unique within the folder they are written to.
            claimed: dict[Path, set[str]] = {d: set() for d in by_directory}

            async def process(eml: Path) -> RenderResult | None:
                async with semaphore:
                    try:
                        result = await render_mail(
                            parse_mail(eml.read_bytes()),
                            profile=name,
                            output_dir=eml.parent,
                            pool=pool,
                            config=config,
                            received=_received(eml),
                            claimed=claimed[eml.parent],
                        )
                    except (ImapArcError, OSError) as exc:
                        # One unreadable mail must not abort the whole run.
                        logger.error("Failed to render %s: %s", eml, exc)
                        return None
                    finally:
                        progress.advance(task)
                    # After the folder's atomic rename, never before: the mail is
                    # only archived once its PDFs are complete. Idempotent, so an
                    # interrupted run is repaired by simply running again.
                    folder = eml.parent / result.basename
                    if folder.is_dir():
                        moved = move_into_folder(eml, folder)
                        if moved is not None:
                            logger.debug("moved %s → %s", eml.name, moved)
                    return result

            results = await asyncio.gather(
                *(process(eml) for eml in files), return_exceptions=True
            )
        for outcome in results:
            if isinstance(outcome, RenderResult):
                report.add(outcome)
            elif isinstance(outcome, BaseException):
                logger.error("Unexpected error while rendering a mail: %s", outcome)
        validate_pdfa(report, tools, progress)
    return report
