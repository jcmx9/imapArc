"""Put archived ``.eml`` files back onto an IMAP server.

The reverse of the fetch phase, for when a mail is gone from the server and the
archive is the only copy left — after an ``after_fetch: delete`` profile, or when
moving to a different provider.

Idempotent by design: each mail is looked up by its ``Message-ID`` before being
uploaded. The reason to restore something is usually that you are *unsure*
whether it is there, so a second run must not leave a second copy behind.

This is the only place in imapArc that writes new mail to a server, and it never
touches anything already there — no flags, no moves, no deletions.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from imaparc.mail.parser import parse_mail
from imaparc.sources.imap import ImapConnection

logger = logging.getLogger(__name__)


class RestoreOutcome(enum.Enum):
    """What happened to one file."""

    UPLOADED = "uploaded"
    ALREADY_THERE = "already there"  # same Message-ID found in the folder
    WOULD_UPLOAD = "would upload"  # dry run
    FAILED = "failed"


@dataclass(frozen=True)
class RestoreResult:
    """One file's fate, for the summary."""

    path: Path
    outcome: RestoreOutcome
    detail: str = ""


def _received(parsed_date: datetime | None, path: Path) -> datetime | None:
    """The timestamp to hand the server as INTERNALDATE.

    The ``Date`` header first — it is what the sender stated and what clients
    display. The file's mtime is a poor second, but better than letting the mail
    claim it arrived just now and sort to the bottom of every mailbox view.
    """
    if parsed_date is not None:
        return parsed_date
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except OSError:
        return None


def restore_files(
    conn: ImapConnection,
    files: list[Path],
    *,
    folder: str = "INBOX",
    dry_run: bool = False,
) -> list[RestoreResult]:
    """Upload each ``.eml`` into ``folder``, skipping ones already present.

    Args:
        conn: An open connection to the target account.
        files: The ``.eml`` files to restore.
        folder: Target mailbox; created (and subscribed) if missing.
        dry_run: Report what would happen and upload nothing.

    Returns:
        One result per file, in the order given. A file that cannot be read or
        uploaded is recorded as failed and the rest still go up: a restore that
        stopped at the first bad file would be worse than one that names the
        single mail which did not make it.
    """
    results: list[RestoreResult] = []
    for path in files:
        try:
            raw = path.read_bytes()
            parsed = parse_mail(raw)
            message_id = parsed.headers.message_id
            if message_id and conn.contains_message(
                folder, message_id, parsed.headers.subject
            ):
                logger.info("∘ %s (already on the server)", path.name)
                results.append(RestoreResult(path, RestoreOutcome.ALREADY_THERE))
                continue
            if dry_run:
                results.append(RestoreResult(path, RestoreOutcome.WOULD_UPLOAD))
                continue
            conn.append(folder, raw, _received(parsed.headers.date, path))
            logger.info("↑ %s", path.name)
            logger.debug("  → %s  %s", folder, message_id or "(no Message-ID)")
            results.append(RestoreResult(path, RestoreOutcome.UPLOADED))
        except Exception as exc:
            logger.error("Failed to restore %s: %s", path, exc)
            results.append(RestoreResult(path, RestoreOutcome.FAILED, str(exc)))
    return results


def summarise(results: list[RestoreResult]) -> str:
    """A short human-readable summary of a restore run."""
    counts: dict[RestoreOutcome, int] = {}
    for result in results:
        counts[result.outcome] = counts.get(result.outcome, 0) + 1
    parts = [
        f"{count} {outcome.value}"
        for outcome, count in sorted(counts.items(), key=lambda kv: kv[0].value)
    ]
    return ", ".join(parts) if parts else "nothing to restore"
