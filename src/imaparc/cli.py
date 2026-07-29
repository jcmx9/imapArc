"""Command-line interface."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, NoReturn

import typer
import yaml
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from imaparc import __version__
from imaparc.accounts import ConfigError, load_accounts
from imaparc.bootstrap import init_config, profile_block, render_profiles_file
from imaparc.config import RunConfig, ToolPaths
from imaparc.console import console
from imaparc.exceptions import ImapArcError, ToolNotFoundError
from imaparc.fetch import run_fetch
from imaparc.logging_setup import setup_logging
from imaparc.mail.parser import parse_mail
from imaparc.pdf.validate import (
    ValidationError,
    run_verapdf,
    run_verapdf_batch,
)
from imaparc.pipeline import RenderResult, render_mail, sweep_staging
from imaparc.profiles import Profile, load_profiles
from imaparc.render.browser import BrowserPool
from imaparc.report import RunReport
from imaparc.sources.base import RawMail
from imaparc.sources.eml import EmlSource
from imaparc.state import StateStore
from imaparc.storage import DIR_MODE, make_dir

logger = logging.getLogger(__name__)

# PDFs validated per veraPDF process. One JVM start per batch; kept well below
# the OS argument-length limit even with long archive paths.
_VERAPDF_BATCH = 100


def _config_dir() -> Path:
    """The central config directory: ``$XDG_CONFIG_HOME/imaparc`` (~/.config)."""
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "imaparc"


def _state_path() -> Path:
    """The default state DB: ``$XDG_STATE_HOME/imaparc/state.db`` (~/.local/state)."""
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(base) / "imaparc" / "state.db"


# Central config defaults, resolved once at import (env set before launch).
DEFAULT_ENV = _config_dir() / ".env"
DEFAULT_PROFILES = _config_dir() / "profile.yaml"
DEFAULT_STATE = _state_path()

app = typer.Typer(
    name="imaparc",
    help=f"Archive IMAP mailboxes to .eml, render to PDF/A (v{__version__}).",
    add_completion=False,
    no_args_is_help=True,
)

# Reused option annotations, so the same flag reads identically on the default
# run and on the fetch/render subcommands.
_ProfilesOpt = Annotated[
    Path, typer.Option("--profiles", help="profile.yaml with the profiles.")
]
_ProfileOpt = Annotated[
    str | None, typer.Option("--profile", help="Only this one profile (by name).")
]
_EnvOpt = Annotated[Path, typer.Option("--env", help=".env file with IMAP accounts.")]
_StateOpt = Annotated[
    Path | None, typer.Option("--state", help="SQLite state database.")
]
_RemoteOpt = Annotated[
    bool,
    typer.Option(
        "--allow-remote-images",
        help="Force remote images on for all profiles (else per profile).",
    ),
]
_JobsOpt = Annotated[
    int | None,
    typer.Option("--jobs", "-j", min=1, help="Parallel renders; overrides profile."),
]
_SilentOpt = Annotated[bool, typer.Option("--silent", "-Q", help="No console output.")]
_VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="Verbose output.")]
_DebugOpt = Annotated[bool, typer.Option("--debug", "-vv", help="Debug output.")]


def version_callback(value: bool) -> None:
    """Print the version and exit."""
    if value:
        typer.echo(f"imaparc {__version__}")
        raise typer.Exit()


def _verbosity(silent: bool, verbose: bool, debug: bool) -> int:
    return 0 if silent else (3 if debug else (2 if verbose else 1))


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
) -> None:
    """imapArc — archive IMAP mail to .eml and render to PDF/A.

    Run without a subcommand to see this help. Use ``all`` for the full pipeline
    (fetch, then render), or ``fetch``/``render`` for a single phase.
    """


@app.command(name="all")
def run_all(
    env_file: _EnvOpt = DEFAULT_ENV,
    profile_file: _ProfilesOpt = DEFAULT_PROFILES,
    only_profile: _ProfileOpt = None,
    state_db: _StateOpt = None,
    allow_remote_images: _RemoteOpt = False,
    jobs: _JobsOpt = None,
    silent: _SilentOpt = False,
    verbose: _VerboseOpt = False,
    debug: _DebugOpt = False,
) -> None:
    """Run the whole pipeline: fetch every profile, then render pdf:true ones.

    ``--profile <name>`` restricts the run to one profile.
    """
    verbosity = _verbosity(silent, verbose, debug)
    setup_logging(verbosity)
    _do_fetch(env_file, profile_file, only_profile, state_db, verbosity)
    code = _do_render(
        profile_file,
        only_profile,
        allow_remote_images,
        jobs,
        verbosity,
        respect_pdf=True,
    )
    raise typer.Exit(code)


@app.command()
def init(
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite existing config files."),
    ] = False,
) -> None:
    """Create the central config (~/.config/imaparc/.env and profile.yaml)."""
    result = init_config(_config_dir(), force=force)
    for path in result.created:
        typer.echo(f"Created {path}")
    for path in result.skipped:
        typer.echo(f"Kept    {path} (exists; use --force to overwrite)")
    if any(p.name == ".env" for p in result.created):
        typer.echo(
            "\nNext: edit "
            f"{_config_dir() / '.env'} with your IMAP account, adjust "
            f"{_config_dir() / 'profile.yaml'}, then run 'imaparc fetch'."
        )


def _match_summary(match: object) -> str:
    """A one-line summary of a profile's match rules."""
    from imaparc.profiles import Match

    if not isinstance(match, Match):
        return "all mail"
    parts: list[str] = []
    if match.domains or match.addresses:
        who = ", ".join([*match.domains, *match.addresses])
        fields = "" if len(match.mode) == 4 else f"{'/'.join(match.mode)}: "
        parts.append(f"{fields}{who}")
    if match.subject is not None:
        parts.append(f"subject {match.subject!r}")
    if match.attachments:
        parts.append(f"attach {', '.join(match.attachments)}")
    folders = match.folders or ["INBOX"]
    folder_part = ", ".join(folders) + (" +sub" if match.recursive else "")
    parts.append(f"in {folder_part}")
    if match.since:
        parts.append(f"since {match.since}")
    if match.until:
        parts.append(f"until {match.until}")
    return "; ".join(parts) if parts else "all mail"


def _after_summary(after: object) -> str:
    """A one-line summary of a profile's after_fetch action."""
    from imaparc.profiles import AfterFetch

    if not isinstance(after, AfterFetch):
        return "—"
    parts: list[str] = []
    if after.label:
        parts.append(f"label {after.label}")
    if after.move_to:
        parts.append(f"move → {after.move_to}")
    if after.delete:
        parts.append("delete")
    return ", ".join(parts) if parts else "—"


@app.command(name="list-profiles")
def list_profiles(profile_file: _ProfilesOpt = DEFAULT_PROFILES) -> None:
    """List the profiles defined in profile.yaml (name, target, rules)."""
    try:
        profiles = load_profiles(profile_file)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if not profiles:
        typer.echo(f"No profiles in {profile_file}.")
        return

    table = Table(title=f"{len(profiles)} profile(s) in {profile_file}")
    table.add_column("Name", style="bold")
    table.add_column("Account")
    table.add_column("Output")
    table.add_column("PDF", justify="center")
    table.add_column("Match")
    table.add_column("After fetch")
    for profile in profiles:
        table.add_row(
            profile.name,
            profile.account,
            str(profile.output),
            "yes" if profile.pdf else "no",
            _match_summary(profile.match),
            _after_summary(profile.after_fetch),
        )
    Console().print(table)


@app.command(name="add-profile")
def add_profile(
    name: Annotated[str, typer.Argument(help="Name for the new profile.")],
    profile_file: _ProfilesOpt = DEFAULT_PROFILES,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Target dir (default ~/imapArc/<name>)."),
    ] = None,
) -> None:
    """Append a new, fully-annotated profile block to profile.yaml.

    Required fields are filled with placeholders; every optional field is present
    but commented, with its default — edit and uncomment what you need.
    """
    if not profile_file.exists():
        typer.echo(
            f"Error: {profile_file} does not exist. Run 'imaparc init' first.",
            err=True,
        )
        raise typer.Exit(1)

    text = profile_file.read_text(encoding="utf-8")
    if not re.search(r"^profiles:", text, re.MULTILINE):
        typer.echo(
            f"Error: {profile_file} has no 'profiles:' key — not an imapArc "
            "profile file.",
            err=True,
        )
        raise typer.Exit(1)

    # Reject a name that already names an active (uncommented) profile.
    active_name = re.compile(
        rf"^\s*-\s*name:\s*['\"]?{re.escape(name)}['\"]?\s*(#.*)?$", re.MULTILINE
    )
    if active_name.search(text):
        typer.echo(f"Error: a profile named '{name}' already exists.", err=True)
        raise typer.Exit(1)

    out = str(output) if output is not None else None
    block = profile_block(name, out)
    separator = "" if text.endswith("\n") else "\n"
    profile_file.write_text(f"{text}{separator}\n{block}", encoding="utf-8")
    typer.echo(
        f"Added profile '{name}' to {profile_file}.\n"
        "Edit it (account, output, match rules), then run 'imaparc fetch'."
    )


def _backup_path(path: Path) -> Path:
    """A non-existing ``<name>.bak`` (or .bak.1, .bak.2, …) next to ``path``."""
    candidate = path.with_name(path.name + ".bak")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak.{counter}")
        counter += 1
    return candidate


@app.command(name="sync-profiles")
def sync_profiles(
    profile_file: _ProfilesOpt = DEFAULT_PROFILES,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")
    ] = False,
) -> None:
    """Rewrite profile.yaml into the canonical full-option format.

    Your real values stay active; every option you did not set appears commented
    with its default, so nothing is hidden. The old file is backed up first. Note:
    your own inline comments are NOT preserved (the file is rebuilt from values).
    """
    if not profile_file.exists():
        typer.echo(
            f"Error: {profile_file} does not exist. Run 'imaparc init' first.",
            err=True,
        )
        raise typer.Exit(1)
    try:
        data = yaml.safe_load(profile_file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        typer.echo(f"Error: invalid YAML in {profile_file}: {exc}", err=True)
        raise typer.Exit(1) from exc

    raw = data.get("profiles")
    if not isinstance(raw, list) or not raw:
        typer.echo(f"Error: {profile_file} has no profiles to sync.", err=True)
        raise typer.Exit(1)

    if not yes:
        typer.confirm(
            f"Rewrite {profile_file} into the full-option format? Your values are "
            "kept and the old file is backed up, but your own inline comments are "
            "not preserved.",
            abort=True,
        )

    original = profile_file.read_text(encoding="utf-8")
    backup = _backup_path(profile_file)
    backup.write_text(original, encoding="utf-8")
    profile_file.write_text(render_profiles_file(raw), encoding="utf-8")

    # Fail safe: if the rewrite does not parse back, restore the original.
    try:
        load_profiles(profile_file)
    except ConfigError as exc:
        profile_file.write_text(original, encoding="utf-8")
        typer.echo(
            f"Error: rewrite would be invalid ({exc}); restored the original. "
            f"Backup left at {backup}.",
            err=True,
        )
        raise typer.Exit(1) from exc

    names = [p.get("name", "?") for p in raw if isinstance(p, dict)]
    typer.echo(
        f"Synced {len(raw)} profile(s) ({', '.join(map(str, names))}) in "
        f"{profile_file}.\nBackup of the previous file: {backup}"
    )


@app.command()
def reset(
    state_db: _StateOpt = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")
    ] = False,
) -> None:
    """Forget delivered state so the next fetch re-processes all matching mail.

    Clears the dedup database only — your archived ``.eml``/PDF files are left in
    place. Remove an output directory separately if you also want a clean archive.
    """
    db = state_db or DEFAULT_STATE
    if not yes:
        typer.confirm(
            f"Clear delivery state ({db})? The next fetch re-processes all "
            "matching mail.",
            abort=True,
        )
    dropped = StateStore(db).clear()
    typer.echo(f"Delivery state cleared ({dropped} record(s)).")


@app.command()
def render(
    profile_file: _ProfilesOpt = DEFAULT_PROFILES,
    only_profile: _ProfileOpt = None,
    allow_remote_images: _RemoteOpt = False,
    jobs: _JobsOpt = None,
    silent: _SilentOpt = False,
    verbose: _VerboseOpt = False,
    debug: _DebugOpt = False,
) -> None:
    """Render profiles' eml/ archives into per-mail PDF folders.

    Renders **every** profile (the ``pdf`` flag is ignored here — asking to
    render means render), each ``<output>/eml`` into ``<output>/pdf`` under
    its own name. Needs no ``.env``.
    """
    verbosity = _verbosity(silent, verbose, debug)
    setup_logging(verbosity)
    code = _do_render(
        profile_file,
        only_profile,
        allow_remote_images,
        jobs,
        verbosity,
        respect_pdf=False,
    )
    raise typer.Exit(code)


@app.command()
def fetch(
    env_file: _EnvOpt = DEFAULT_ENV,
    profile_file: _ProfilesOpt = DEFAULT_PROFILES,
    only_profile: _ProfileOpt = None,
    state_db: _StateOpt = None,
    silent: _SilentOpt = False,
    verbose: _VerboseOpt = False,
    debug: _DebugOpt = False,
) -> None:
    """Fetch new mail from IMAP into the profile eml/ archives (no rendering)."""
    verbosity = _verbosity(silent, verbose, debug)
    setup_logging(verbosity)
    _do_fetch(env_file, profile_file, only_profile, state_db, verbosity)


def _select_profiles(
    profiles: list[Profile], only_profile: str | None
) -> list[Profile]:
    """Restrict to a single named profile, or exit 1 if the name is unknown."""
    if only_profile is None:
        return profiles
    selected = [p for p in profiles if p.name == only_profile]
    if not selected:
        typer.echo(f"Error: no profile named '{only_profile}'", err=True)
        raise typer.Exit(1)
    return selected


def _abort() -> NoReturn:
    """Report a clean, non-traceback abort on Ctrl-C and exit with code 130.

    Safe by construction: the ``.eml`` write and the per-mail PDF folder are both
    atomic (temp + fsync + rename), and the server is never touched before the
    local archive is durable — so an interruption never loses mail or leaves a
    half-written file. A re-run simply continues.
    """
    typer.echo(
        "\nAborted. Everything already archived is complete and safe — re-run "
        "to continue where it stopped.",
        err=True,
    )
    raise typer.Exit(130)


def _do_fetch(
    env_file: Path,
    profile_file: Path,
    only_profile: str | None,
    state_db: Path | None,
    verbosity: int,
) -> None:
    """Load config, then fetch the selected profiles into their eml/ archives."""
    try:
        accounts = load_accounts(env_file)
        profiles = load_profiles(profile_file, accounts)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    profiles = _select_profiles(profiles, only_profile)
    try:
        report = run_fetch(accounts, profiles, StateStore(state_db or DEFAULT_STATE))
    except KeyboardInterrupt:
        _abort()
    if verbosity > 0:
        typer.echo(report.summary())


def _do_render(
    profile_file: Path,
    only_profile: str | None,
    allow_remote_images: bool,
    jobs: int | None,
    verbosity: int,
    *,
    respect_pdf: bool,
) -> int:
    """Render the selected profiles; return the process exit code.

    ``respect_pdf`` True renders only ``pdf: true`` profiles (the full run's
    render phase); False renders all selected profiles (explicit ``render``).
    """
    try:
        profiles = load_profiles(profile_file)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    profiles = _select_profiles(profiles, only_profile)
    targets = [p for p in profiles if p.pdf] if respect_pdf else profiles
    if not targets:
        typer.echo(
            "Nothing to render: no profile has 'pdf: true'."
            if respect_pdf
            else "Nothing to render: no profiles."
        )
        return 0

    try:
        tools = ToolPaths.resolve()
    except ToolNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    try:
        report = asyncio.run(
            _run_render(targets, tools, allow_remote_images, jobs, verbosity)
        )
    except KeyboardInterrupt:
        _abort()
    if verbosity > 0:
        typer.echo(report.summary())
    # Non-conformant PDF/A is reported (see summary), never fatal: the .eml is the
    # guarantee and the PDF a best-effort rendition — the run always succeeds.
    return 0


def _received(source_id: str) -> datetime | None:
    """Delivery time (eml file mtime) — the timestamp fallback."""
    try:
        return datetime.fromtimestamp(Path(source_id).stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _make_progress(*, disable: bool) -> Progress:
    """A modern render/validation progress display on the shared console."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        console=console,
        disable=disable,
        transient=False,
    )


async def _run_render(
    profiles: list[Profile],
    tools: ToolPaths,
    cli_remote: bool,
    cli_jobs: int | None,
    verbosity: int,
) -> RunReport:
    """Render each profile's eml/ with its own settings; then validate.

    ``remote_images``/``jobs`` are per-profile; the CLI flags override them for
    the whole run. Each profile gets its own :class:`BrowserPool`
    and concurrency, so a profile that does not want remote images keeps the full
    network lockdown even when another profile in the same run does.

    Every mail is listed up front so the progress bar knows its total from the
    start; a second bar tracks PDF/A validation.
    """
    report = RunReport()
    plans: list[tuple[Profile, RunConfig, Path, list[RawMail]]] = []
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
            allow_remote=cli_remote or profile.remote_images,
        )
        mails = list(EmlSource(eml_dir))
        plans.append((profile, config, profile.output / "pdf", mails))

    total = sum(len(mails) for *_, mails in plans)
    with _make_progress(disable=verbosity == 0 or total == 0) as progress:
        task = progress.add_task("Rendering mail", total=total)
        for profile, config, output, mails in plans:
            semaphore = asyncio.Semaphore(config.jobs)
            async with BrowserPool(
                allow_remote=config.allow_remote, timeout_ms=config.render_timeout_ms
            ) as pool:
                await _render_profile(
                    profile,
                    output,
                    mails,
                    pool,
                    semaphore,
                    config,
                    report,
                    progress,
                    task,
                )
        _validate_pdfa(report, tools, progress)
    return report


async def _render_profile(
    profile: Profile,
    output: Path,
    mails: list[RawMail],
    pool: BrowserPool,
    semaphore: asyncio.Semaphore,
    config: RunConfig,
    report: RunReport,
    progress: Progress,
    task: TaskID,
) -> None:
    """Render every mail in one profile's ``eml/`` into its ``pdf/`` directory."""
    make_dir(output)
    os.chmod(output, 0o700)  # owner-writable, private (same as DIR_MODE at rest)
    sweep_staging(output)  # clear any .staging-* left by a previously aborted run

    # Shared across this profile's concurrent renders so two mails that resolve
    # to the same basename (same Date header + subject) reserve distinct names
    # instead of racing on the output path.
    claimed: set[str] = set()

    async def process(raw: RawMail) -> RenderResult | None:
        async with semaphore:
            try:
                parsed = parse_mail(raw.raw)
                return await render_mail(
                    parsed,
                    profile=profile.name,
                    output_dir=output,
                    pool=pool,
                    config=config,
                    received=_received(raw.source_id),
                    claimed=claimed,
                )
            except (ImapArcError, OSError) as exc:
                # One bad mail must not abort the whole profile's render.
                logger.error("Failed to render %s: %s", raw.source_id, exc)
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


def _validate_pdfa(report: RunReport, tools: ToolPaths, progress: Progress) -> None:
    """Validate the written PDFs with veraPDF in batches (one JVM per batch).

    Per-file validation starts a fresh JVM each time, which is unusably slow at
    scale (hundreds of mails → hundreds of JVM starts). Instead each veraPDF
    process validates a whole chunk of files. Paths are deduplicated first — an
    attachment-less mail's combined and mail-only PDFs are the same file.
    """
    seen: set[Path] = set()
    paths: list[Path] = []
    for result in report.written:
        for pdf in (result.combined_pdf, result.mail_only_pdf):
            if pdf is not None and pdf not in seen and pdf.exists():
                seen.add(pdf)
                paths.append(pdf)
    if not paths:
        return

    task = progress.add_task("Validating PDF/A", total=len(paths))
    for start in range(0, len(paths), _VERAPDF_BATCH):
        chunk = paths[start : start + _VERAPDF_BATCH]
        try:
            results = run_verapdf_batch(tools.verapdf, chunk)
        except ValidationError as exc:
            # Batch alignment failed — fall back to per-file for this chunk so a
            # single odd PDF cannot mislabel its neighbours.
            logger.warning("veraPDF batch failed (%s); validating individually", exc)
            results = [run_verapdf(tools.verapdf, pdf) for pdf in chunk]
        for pdf, res in zip(chunk, results, strict=True):
            if not res.compliant:
                report.non_compliant.append(str(pdf))
        progress.advance(task, len(chunk))
