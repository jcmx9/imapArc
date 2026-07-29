"""Chromium lifecycle and the network lockdown.

One browser process per run (its 300-800 ms startup must be amortised), one
fresh context per mail (cheap, and it isolates cache and storage between
mails).

Nothing may leave the machine while rendering email. Four independent layers
enforce that, so a failure of any one is not a leak:

1. ``--host-resolver-rules=MAP * ~NOTFOUND`` — DNS fails process-wide.
2. A route handler aborting everything that is not ``data:`` / ``about:``.
3. ``java_script_enabled=False`` — mail JS never executes.
4. A CSP meta tag emitted with every generated document.

Layer 2 logs every abort at DEBUG, which doubles as the audit trail proving
no request escaped.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Self

from playwright.async_api import (
    Browser,
    BrowserContext,
    Playwright,
    Route,
    async_playwright,
)

logger = logging.getLogger(__name__)

# Schemes the renderer may load. Everything else is aborted.
ALLOWED_SCHEMES = ("data:", "about:")

# Always applied: these only disable Chromium's own phone-home traffic.
_BASE_ARGS = [
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-default-apps",
    "--no-first-run",
    "--disable-features=Translate,OptimizationHints,MediaRouter",
]

# Layer 1, only in the default (blocking) mode: no name resolves, so nothing
# can be fetched even if a request slipped past the route handler.
_DNS_BLACKHOLE = "--host-resolver-rules=MAP * ~NOTFOUND"


def build_launch_args(*, allow_remote: bool = False) -> list[str]:
    """Return the Chromium launch flags for the given remote-content policy."""
    if allow_remote:
        return list(_BASE_ARGS)
    return [_DNS_BLACKHOLE, *_BASE_ARGS]


def is_allowed_url(url: str, *, allow_remote: bool = False) -> bool:
    """Whether the renderer may load this URL (pure, unit-testable).

    In ``allow_remote`` mode http(s) is permitted as well; every other scheme
    (``file:``, ``ftp:``) stays blocked in both modes.
    """
    low = url.lower()
    if low.startswith(ALLOWED_SCHEMES):
        return True
    return allow_remote and low.startswith(("http://", "https://"))


class BrowserPool:
    """Owns the Chromium process for the duration of a run.

    Use as an async context manager::

        async with BrowserPool() as pool:
            async with pool.context() as ctx:
                ...
    """

    def __init__(self, *, timeout_ms: int = 30_000, allow_remote: bool = False) -> None:
        self._timeout_ms = timeout_ms
        self._allow_remote = allow_remote
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self.blocked_urls: list[str] = []
        self.loaded_urls: list[str] = []

    async def __aenter__(self) -> Self:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            args=build_launch_args(allow_remote=self._allow_remote)
        )
        if self._allow_remote:
            logger.warning(
                "Chromium started with remote content ALLOWED — senders can "
                "observe that these mails were processed"
            )
        else:
            logger.debug("Chromium launched with full network lockdown")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None
        logger.debug("Chromium closed")

    @asynccontextmanager
    async def context(self) -> AsyncGenerator[BrowserContext]:
        """Yield a locked-down browser context for one mail."""
        if self._browser is None:  # pragma: no cover - guarded by __aenter__
            raise RuntimeError("BrowserPool is not started")
        ctx = await self._browser.new_context(
            java_script_enabled=False,
            color_scheme="light",
            viewport={"width": 1024, "height": 1400},
        )
        ctx.set_default_timeout(self._timeout_ms)
        await ctx.route("**/*", self._guard)
        try:
            yield ctx
        finally:
            await ctx.close()

    async def _guard(self, route: Route) -> None:
        """Layer 2: abort anything the current policy does not permit."""
        url = route.request.url
        if is_allowed_url(url, allow_remote=self._allow_remote):
            if not url.lower().startswith(ALLOWED_SCHEMES):
                # Remote fetch in allow_remote mode: record it for the audit
                # trail, since it left the machine.
                self.loaded_urls.append(url)
                logger.info("Fetched remote resource: %s", url)
            await route.continue_()
            return
        self.blocked_urls.append(url)
        logger.debug("Blocked remote request while rendering: %s", url)
        await route.abort()
