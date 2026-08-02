"""Map the 4-level verbosity scheme to logging handlers.

Verbosity levels (per project convention):
    0 — silent : no console output, log file gets everything
    1 — normal : progress + summary on console
    2 — verbose: + decisions
    3 — debug  : + debug details, log file at DEBUG
"""

from __future__ import annotations

import logging
from pathlib import Path

from rich.logging import RichHandler

from imaparc.console import console

# Level 1 shows only the progress bars and the closing summary — the per-mail
# INFO lines start at -v, which is what separates "watch it work" from "wait for
# it". Before this, 1 and 2 were both INFO and -v changed nothing on screen.
_CONSOLE_LEVEL = {
    0: logging.CRITICAL + 1,
    1: logging.WARNING,
    2: logging.INFO,
    3: logging.DEBUG,
}
_FILE_LEVEL = {0: logging.INFO, 1: logging.INFO, 2: logging.INFO, 3: logging.DEBUG}

# Third-party loggers that warn about things which are normal and expected in
# this pipeline (img2pdf: a transparent PNG gets a soft mask; PIL internals).
# Raised to ERROR so the console shows imapArc's own progress, not library noise.
_NOISY_LIBRARIES = ("img2pdf", "PIL")


def _quiet_noisy_libraries() -> None:
    """Silence expected third-party WARNING chatter from the console/log."""
    for name in _NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.ERROR)


def setup_logging(verbosity: int, log_file: Path | None = None) -> None:
    """Configure the root logger for the given verbosity.

    Args:
        verbosity: 0 (silent) .. 3 (debug).
        log_file: Optional path that always receives log output, even at
            verbosity 0 — this is what makes ``-Q`` usable for cron jobs.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    _quiet_noisy_libraries()
    # Route Python warnings through logging so any stray warning prints cleanly
    # above the progress bar instead of corrupting the live display.
    logging.captureWarnings(True)

    console_level = _CONSOLE_LEVEL.get(verbosity, logging.INFO)
    if verbosity > 0:
        handler = RichHandler(
            console=console, rich_tracebacks=True, show_path=verbosity >= 3
        )
        handler.setLevel(console_level)
        root.addHandler(handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(_FILE_LEVEL.get(verbosity, logging.INFO))
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
        )
        root.addHandler(file_handler)
