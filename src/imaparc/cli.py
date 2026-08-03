"""Command-line interface."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Annotated, NoReturn

import typer
import yaml
from rich.console import Console
from rich.table import Table

from imaparc import __version__
from imaparc.accounts import Account, ConfigError, load_accounts
from imaparc.adhoc import collect_eml, run_adhoc
from imaparc.bootstrap import init_config, profile_block, render_profiles_file
from imaparc.config import ToolPaths
from imaparc.console import console
from imaparc.doctor import Status, exit_code, run_checks
from imaparc.exceptions import ImapArcError, SourceError, ToolNotFoundError
from imaparc.fetch import run_fetch
from imaparc.logging_setup import setup_logging
from imaparc.profiles import Profile, load_profiles
from imaparc.render_run import run_render
from imaparc.restore import RestoreOutcome, restore_files, summarise
from imaparc.service import (
    APPLICATIONS_DIR,
    SERVICES_DIR,
    action_hint,
    install_action,
)
from imaparc.sources.imap import ImapConnection
from imaparc.state import StateStore
from imaparc.verify import Severity, verify_profile
from imaparc.verify import exit_code as verify_exit_code

logger = logging.getLogger(__name__)


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
_NameOpt = Annotated[
    str,
    typer.Option(
        "--name",
        "-n",
        help="Name segment in the generated file names (the profile's slot).",
    ),
]
_DryRunOpt = Annotated[
    bool,
    typer.Option(
        "--dry-run",
        help="Show what would be archived and what would happen on the server; "
        "write nothing.",
    ),
]
_NoServerActionsOpt = Annotated[
    bool,
    typer.Option(
        "--no-server-actions",
        help="Archive normally, but never label, move or delete on the server.",
    ),
]
_LogFileOpt = Annotated[
    Path | None,
    typer.Option(
        "--log-file",
        help="Also write the log here — including at -Q, which is what makes "
        "silent runs usable from cron.",
    ),
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
    log_file: _LogFileOpt = None,
    silent: _SilentOpt = False,
    verbose: _VerboseOpt = False,
    debug: _DebugOpt = False,
) -> None:
    """Run the whole pipeline: fetch every profile, then render pdf:true ones.

    ``--profile <name>`` restricts the run to one profile.
    """
    verbosity = _verbosity(silent, verbose, debug)
    setup_logging(verbosity, log_file)
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
    only_profile: _ProfileOpt = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")
    ] = False,
) -> None:
    """Forget delivered state so the next fetch re-processes matching mail.

    ``--profile <name>`` limits this to one profile; without it, every profile's
    state goes.

    Clears the dedup database only — your archived ``.eml``/PDF files are left in
    place. Remove an output directory separately if you also want a clean archive.
    """
    db = state_db or DEFAULT_STATE
    scope = f"profile '{only_profile}'" if only_profile else "all profiles"
    if not yes:
        typer.confirm(
            f"Clear delivery state for {scope} ({db})? The next fetch "
            "re-processes that mail.",
            abort=True,
        )
    store = StateStore(db)
    untracked = store.untracked_count() if only_profile else 0
    dropped = store.clear(profile=only_profile)
    typer.echo(f"Delivery state cleared ({dropped} record(s), {scope}).")
    if untracked:
        # Say so rather than let a targeted reset look complete while the mail it
        # was meant to free up stays skipped.
        typer.echo(
            f"Note: {untracked} record(s) predate per-profile tracking and were "
            "left untouched. Run without --profile to clear those too."
        )


@app.command()
def render(
    profile_file: _ProfilesOpt = DEFAULT_PROFILES,
    only_profile: _ProfileOpt = None,
    allow_remote_images: _RemoteOpt = False,
    jobs: _JobsOpt = None,
    log_file: _LogFileOpt = None,
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
    setup_logging(verbosity, log_file)
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
def eml(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(
            help="Files or directories to render. Default: current directory.",
        ),
    ] = None,
    name: _NameOpt = "mail",
    jobs: _JobsOpt = None,
    log_file: _LogFileOpt = None,
    silent: _SilentOpt = False,
    verbose: _VerboseOpt = False,
    debug: _DebugOpt = False,
) -> None:
    """Render loose .eml files where they lie, without a profile.

    For a mail dragged out of a mail client: each ``.eml`` becomes a
    ``<basename>/`` folder next to it, holding the PDFs and the attachments —
    and the ``.eml`` itself is moved in afterwards.

    A directory argument takes the ``.eml`` files directly inside it, not in
    subfolders. Needs no ``.env`` and no profile.yaml, and never touches the
    fetch state.

    Remote images are always loaded here (unlike the profile-driven commands,
    where they are opt-in), so the mail renders as the sender laid it out. Note
    that this fetches tracking pixels too.
    """
    verbosity = _verbosity(silent, verbose, debug)
    setup_logging(verbosity, log_file)
    try:
        files = collect_eml(list(paths or []))
    except SourceError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if not files:
        typer.echo("Nothing to render: no .eml files found.")
        return
    try:
        tools = ToolPaths.resolve()
    except ToolNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    try:
        report = asyncio.run(
            run_adhoc(
                files,
                name=name,
                tools=tools,
                jobs=jobs if jobs is not None else 4,
                verbosity=verbosity,
            )
        )
    except KeyboardInterrupt:
        _abort()
    if verbosity > 0:
        typer.echo(report.summary())


@app.command()
def doctor(
    env_file: _EnvOpt = DEFAULT_ENV,
    profile_file: _ProfilesOpt = DEFAULT_PROFILES,
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Skip the IMAP logins (no network access)."),
    ] = False,
) -> None:
    """Check the installation: tools, browser, config, credentials.

    Exits 1 if anything is broken, so it is usable from cron or CI. A missing
    config counts as a warning, not a failure — a fresh install has none yet.
    """
    checks = run_checks(env_file=env_file, profile_file=profile_file, offline=offline)
    marks = {
        Status.OK: "[green]✓[/]",
        Status.WARN: "[yellow]![/]",
        Status.FAIL: "[red]✗[/]",
    }
    for check in checks:
        console.print(f"{marks[check.status]} {check.name:<14} {check.detail}")

    failed = [c for c in checks if c.status is Status.FAIL]
    warned = [c for c in checks if c.status is Status.WARN]
    console.print()
    if failed:
        console.print(f"[red]{len(failed)} problem(s) found[/]")
    elif warned:
        console.print(f"{len(warned)} note(s), nothing broken")
    else:
        console.print("[green]all good[/]")
    raise typer.Exit(exit_code(checks))


@app.command()
def restore(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(help="Archived .eml files or directories holding them."),
    ] = None,
    env_file: _EnvOpt = DEFAULT_ENV,
    account: Annotated[
        str | None,
        typer.Option("--account", help="Account from .env (required if several)."),
    ] = None,
    folder: Annotated[
        str, typer.Option("--folder", help="Target mailbox; created if missing.")
    ] = "INBOX",
    dry_run: _DryRunOpt = False,
    log_file: _LogFileOpt = None,
    silent: _SilentOpt = False,
    verbose: _VerboseOpt = False,
    debug: _DebugOpt = False,
) -> None:
    """Upload archived .eml files back onto an IMAP server.

    For mail that is gone from the server and survives only in the archive — for
    instance after an ``after_fetch: delete`` profile, or when moving provider.

    Safe to repeat: each mail is looked up by ``Message-ID`` first, so a second
    run adds nothing. Nothing already on the server is modified.
    """
    verbosity = _verbosity(silent, verbose, debug)
    setup_logging(verbosity, log_file)
    try:
        files = collect_eml(list(paths or []))
        accounts = load_accounts(env_file)
    except (SourceError, ConfigError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if not files:
        typer.echo("Nothing to restore: no .eml files found.")
        return

    chosen = _select_account(accounts, account)
    with ImapConnection(chosen) as conn:
        results = restore_files(conn, files, folder=folder, dry_run=dry_run)
    if verbosity > 0 or dry_run:
        typer.echo(summarise(results))
    raise typer.Exit(
        1 if any(r.outcome is RestoreOutcome.FAILED for r in results) else 0
    )


def _select_account(accounts: dict[str, Account], name: str | None) -> Account:
    """Pick the account to restore into, or exit with a usable message."""
    if not accounts:
        typer.echo("Error: no IMAP account defined in the .env", err=True)
        raise typer.Exit(1)
    if name is not None:
        if name.lower() not in accounts:
            known = ", ".join(sorted(accounts))
            typer.echo(f"Error: no account '{name}' (known: {known})", err=True)
            raise typer.Exit(1)
        return accounts[name.lower()]
    if len(accounts) > 1:
        known = ", ".join(sorted(accounts))
        typer.echo(
            f"Error: several accounts defined ({known}) — pick one with --account",
            err=True,
        )
        raise typer.Exit(1)
    return next(iter(accounts.values()))


@app.command()
def verify(
    profile_file: _ProfilesOpt = DEFAULT_PROFILES,
    only_profile: _ProfileOpt = None,
) -> None:
    """Check the archives for damage, duplicates and leftovers.

    Read-only — it reports, it never repairs. Exits 1 only when an archive is
    actually damaged; duplicates and leftovers are reported but lose nothing, so
    they must not make a scheduled check fail.
    """
    try:
        profiles = load_profiles(profile_file)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    profiles = _select_profiles(profiles, only_profile)

    findings = [f for profile in profiles for f in verify_profile(profile)]
    marks = {Severity.WARN: "[yellow]![/]", Severity.FAIL: "[red]✗[/]"}
    for finding in findings:
        console.print(
            f"{marks[finding.severity]} {finding.profile:<12} "
            f"{finding.kind:<12} {finding.detail}"
        )

    console.print()
    if not findings:
        console.print(f"[green]{len(profiles)} archive(s) look sound[/]")
    else:
        damaged = sum(1 for f in findings if f.severity is Severity.FAIL)
        console.print(
            f"{len(findings)} finding(s)"
            + (f", [red]{damaged} of them damage[/]" if damaged else ", none fatal")
        )
    raise typer.Exit(verify_exit_code(findings))


@app.command(name="install-service")
def install_service(
    name: _NameOpt = "mail",
) -> None:
    """Add a file-manager action for .eml files.

    On macOS this is a Finder Quick Action (right-click → Services); on Linux a
    Desktop Entry that Nautilus, Dolphin and Thunar offer under "Open With".
    Either way, selecting one or more ``.eml`` files — or whole folders — and
    invoking it archives them exactly as ``imaparc eml`` would.
    """
    executable = shutil.which("imaparc")
    if executable is None:
        typer.echo(
            "Error: 'imaparc' was not found on PATH. Install it first "
            "(uv tool install …) or run 'uv tool update-shell'.",
            err=True,
        )
        raise typer.Exit(1)
    try:
        written = install_action(
            sys.platform,
            executable=Path(executable),
            name=name,
            services_dir=SERVICES_DIR,
            applications_dir=APPLICATIONS_DIR,
        )
    except (ImapArcError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Installed {written}")
    typer.echo(action_hint(sys.platform))


@app.command()
def fetch(
    env_file: _EnvOpt = DEFAULT_ENV,
    profile_file: _ProfilesOpt = DEFAULT_PROFILES,
    only_profile: _ProfileOpt = None,
    state_db: _StateOpt = None,
    dry_run: _DryRunOpt = False,
    no_server_actions: _NoServerActionsOpt = False,
    log_file: _LogFileOpt = None,
    silent: _SilentOpt = False,
    verbose: _VerboseOpt = False,
    debug: _DebugOpt = False,
) -> None:
    """Fetch new mail from IMAP into the profile eml/ archives (no rendering).

    ``--dry-run`` only shows which mail each profile would take and what would
    happen to it on the server — nothing is written and nothing is touched.
    ``--no-server-actions`` archives normally but suppresses every label, move
    and delete; useful for a first run with a profile that deletes.
    """
    verbosity = _verbosity(silent, verbose, debug)
    setup_logging(verbosity, log_file)
    _do_fetch(
        env_file,
        profile_file,
        only_profile,
        state_db,
        verbosity,
        dry_run=dry_run,
        no_server_actions=no_server_actions,
    )


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
    *,
    dry_run: bool = False,
    no_server_actions: bool = False,
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
        report = run_fetch(
            accounts,
            profiles,
            StateStore(state_db or DEFAULT_STATE),
            verbosity=verbosity,
            dry_run=dry_run,
            no_server_actions=no_server_actions,
        )
    except KeyboardInterrupt:
        _abort()
    # A dry run's whole output *is* the summary, so print it even at -Q.
    if verbosity > 0 or dry_run:
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
            run_render(targets, tools, allow_remote_images, jobs, verbosity)
        )
    except KeyboardInterrupt:
        _abort()
    if verbosity > 0:
        typer.echo(report.summary())
    # Non-conformant PDF/A is reported (see summary), never fatal: the .eml is the
    # guarantee and the PDF a best-effort rendition — the run always succeeds.
    return 0
