"""The single shared Rich console (stderr) for logs and progress bars.

Logging (``RichHandler``) and the render progress bars must write through the
*same* ``Console`` so Rich keeps the live progress pinned to the bottom and
prints log lines above it, instead of the two fighting over the terminal.
Progress goes to stderr so stdout stays clean for the final summary / piping.
"""

from __future__ import annotations

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

console: Console = Console(stderr=True)


def make_progress(*, disable: bool) -> Progress:
    """A render/validation progress display bound to the shared console."""
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
