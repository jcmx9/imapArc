"""Diagnose an imapArc installation: tools, browser, config, credentials.

Every dependency imapArc needs sits somewhere else — Ghostscript on PATH, a
Chromium build in Playwright's cache, an sRGB profile in a system directory,
credentials in ``.env``. When one is missing the failure surfaces late and far
from its cause: a Finder action that dies with "Missing required tool(s)", a
render that reports "Executable doesn't exist". This checks all of them at once
and says which one is wrong.

Checks never raise: a broken environment is what this command is *for*, so every
probe turns its failure into a finding.
"""

from __future__ import annotations

import enum
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from imaparc.accounts import Account, load_accounts
from imaparc.config import REQUIRED_TOOLS, default_icc_profile
from imaparc.profiles import load_profiles
from imaparc.service import SERVICE_NAME, SERVICES_DIR


class Status(enum.Enum):
    """How a single check came out."""

    OK = "ok"
    WARN = "warn"  # not broken, but worth saying — e.g. config not created yet
    FAIL = "fail"


@dataclass(frozen=True)
class Check:
    """One diagnosis line."""

    name: str
    status: Status
    detail: str


def _tool_checks() -> list[Check]:
    """gs, qpdf and verapdf — needed for rendering, not for fetching."""
    checks: list[Check] = []
    for tool in REQUIRED_TOOLS:
        found = shutil.which(tool)
        checks.append(
            Check(tool, Status.OK, found)
            if found
            else Check(tool, Status.FAIL, "not found on PATH — rendering will fail")
        )
    return checks


def _icc_check() -> Check:
    """The sRGB profile embedded as the PDF/A output intent."""
    profile = default_icc_profile()
    if profile.exists():
        return Check("sRGB ICC", Status.OK, str(profile))
    return Check(
        "sRGB ICC",
        Status.FAIL,
        f"none of the known locations exist (tried {profile} first) — "
        "PDF/A conversion will abort",
    )


def chromium_revision() -> str | None:
    """The Chromium build this Playwright expects, per its ``browsers.json``."""
    try:
        import playwright
    except ImportError:  # pragma: no cover - playwright is a hard dependency
        return None
    manifest = Path(playwright.__file__).parent / "driver/package/browsers.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for browser in data.get("browsers", []):
        if browser.get("name") == "chromium":
            revision = browser.get("revision")
            return str(revision) if revision is not None else None
    return None


def _chromium_check() -> Check:
    """A Chromium build matching the installed Playwright.

    Deliberately reads the expected revision and looks for it on disk instead of
    launching a browser: starting one here is slow and leaves asyncio teardown
    noise on the console, which is the last thing a diagnosis should print.

    Checking the *revision* rather than "some chromium is present" is the whole
    point. `playwright install` deletes outdated builds, so a project venv and a
    uv-tool install with different Playwright versions evict each other's
    browser — leaving a cache that looks populated but holds the wrong build.
    """
    revision = chromium_revision()
    if revision is None:
        return Check("Chromium", Status.FAIL, "cannot read Playwright's browsers.json")
    caches = [
        Path.home() / "Library" / "Caches" / "ms-playwright",  # macOS
        Path.home() / ".cache" / "ms-playwright",  # Linux
    ]
    for cache in caches:
        for name in (f"chromium_headless_shell-{revision}", f"chromium-{revision}"):
            if (cache / name).is_dir():
                return Check(
                    "Chromium", Status.OK, f"build {revision} ({cache / name})"
                )
    present = sorted(
        p.name for cache in caches if cache.is_dir() for p in cache.glob("chromium*")
    )
    have = f"; cache holds {', '.join(present)}" if present else "; cache is empty"
    return Check(
        "Chromium",
        Status.FAIL,
        f"build {revision} missing{have} — run "
        '"$(uv tool dir)/imaparc/bin/python" -m playwright install chromium',
    )


def _profiles_check(profile_file: Path) -> tuple[Check, int]:
    """profile.yaml parses, and how many profiles it defines."""
    if not profile_file.exists():
        return (
            Check("profile.yaml", Status.WARN, f"not created yet ({profile_file})"),
            0,
        )
    try:
        profiles = load_profiles(profile_file)
    except Exception as exc:
        return Check("profile.yaml", Status.FAIL, str(exc)), 0
    if not profiles:
        return Check("profile.yaml", Status.WARN, "parses, but defines no profile"), 0
    return Check("profile.yaml", Status.OK, f"{len(profiles)} profile(s)"), len(
        profiles
    )


def _accounts_check(env_file: Path) -> tuple[Check, dict[str, Account]]:
    """.env parses and every account it names is complete."""
    if not env_file.exists():
        return Check(".env", Status.WARN, f"not created yet ({env_file})"), {}
    try:
        accounts = load_accounts(env_file)
    except Exception as exc:
        return Check(".env", Status.FAIL, str(exc)), {}
    if not accounts:
        return Check(".env", Status.WARN, "no IMAP_* account defined"), {}
    return Check(".env", Status.OK, f"{len(accounts)} account(s)"), accounts


def _service_check() -> Check:
    """The Finder Quick Action, if it was ever installed."""
    bundle = SERVICES_DIR / f"{SERVICE_NAME}.workflow"
    if not bundle.is_dir():
        return Check(
            "Finder action", Status.WARN, "not installed (imaparc install-service)"
        )
    return Check("Finder action", Status.OK, str(bundle))


def _login_check(account: Account) -> Check:
    """An actual IMAP login — the only way to prove the credentials work."""
    from imaparc.sources.imap import ImapConnection

    try:
        with ImapConnection(account, timeout=10.0):
            pass
    except Exception as exc:
        return Check(f"login {account.name}", Status.FAIL, str(exc))
    return Check(f"login {account.name}", Status.OK, f"{account.user}@{account.host}")


def run_checks(
    *,
    env_file: Path,
    profile_file: Path,
    offline: bool = False,
) -> list[Check]:
    """Run every diagnosis and return the findings in display order.

    Args:
        env_file: The ``.env`` to inspect.
        profile_file: The ``profile.yaml`` to inspect.
        offline: Skip the IMAP logins, which are the only checks that use the
            network and the only ones with a visible side effect on a server.
    """
    checks = [*_tool_checks(), _chromium_check(), _icc_check()]
    profiles_check, _count = _profiles_check(profile_file)
    accounts_check, accounts = _accounts_check(env_file)
    checks += [profiles_check, accounts_check, _service_check()]
    if not offline:
        checks += [_login_check(a) for a in accounts.values()]
    return checks


def exit_code(checks: list[Check]) -> int:
    """1 if anything failed, else 0 — so cron and CI can act on it.

    A warning is not a failure: a fresh install without config is expected.
    """
    return 1 if any(c.status is Status.FAIL for c in checks) else 0
