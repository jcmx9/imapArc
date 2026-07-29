"""SQLite state store for IMAP fetch idempotency.

Tracks, per ``(account, folder, uidvalidity)``, the set of UIDs already
delivered into an eml archive. Fetching re-evaluates *every* message in the scanned
folders against the current profiles on each run; the state only prevents
delivering the same message twice. This means a changed or newly added profile
takes effect on existing mail with no manual state reset — unmatched mail is
never marked, so it is simply re-considered next time.

A UID is stable only while ``UIDVALIDITY`` is unchanged; entries are keyed by it,
so a server-side ``UIDVALIDITY`` change naturally starts a fresh delivered set.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivered (
    account TEXT NOT NULL,
    folder TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    uid INTEGER NOT NULL,
    filename TEXT,
    PRIMARY KEY (account, folder, uidvalidity, uid)
);
"""


class StateStore:
    """Persists which messages have already been delivered, for dedup.

    Besides the delivered UID set (dedup), each row records the exact ``.eml``
    filename that was written for that message. On a later run a message still on
    the server (its move/delete failed before) is re-checked against *its own*
    archived file — not against a name merely reconstructed from the current
    headers, which could collide with another mail sharing the same base name.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Add the ``filename`` column to a pre-existing table if it is missing."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(delivered)")}
        if "filename" not in columns:
            conn.execute("ALTER TABLE delivered ADD COLUMN filename TEXT")

    def delivered_uids(self, account: str, folder: str, uidvalidity: int) -> set[int]:
        """Return the set of UIDs already delivered for this folder generation."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT uid FROM delivered "
                "WHERE account = ? AND folder = ? AND uidvalidity = ?",
                (account, folder, uidvalidity),
            ).fetchall()
        return {int(row[0]) for row in rows}

    def mark_delivered(
        self,
        account: str,
        folder: str,
        uidvalidity: int,
        uid: int,
        filename: str | None = None,
    ) -> None:
        """Record that ``uid`` has been delivered, with its ``.eml`` filename."""
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO delivered "
                "(account, folder, uidvalidity, uid, filename) VALUES (?, ?, ?, ?, ?)",
                (account, folder, uidvalidity, uid, filename),
            )
            conn.commit()

    def delivered_filename(
        self, account: str, folder: str, uidvalidity: int, uid: int
    ) -> str | None:
        """The exact ``.eml`` filename recorded for a delivered message, if any.

        Returns None for a message not yet delivered or for a legacy row written
        before filenames were tracked (the caller then falls back to the name
        reconstructed from the current headers).
        """
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT filename FROM delivered "
                "WHERE account = ? AND folder = ? AND uidvalidity = ? AND uid = ?",
                (account, folder, uidvalidity, uid),
            ).fetchone()
        return None if row is None or row[0] is None else str(row[0])

    def clear(self) -> int:
        """Forget all delivered UIDs; return how many were dropped.

        The next fetch then re-evaluates and re-delivers every matching mail.
        """
        with closing(self._connect()) as conn:
            count = conn.execute("SELECT COUNT(*) FROM delivered").fetchone()[0]
            conn.execute("DELETE FROM delivered")
            conn.commit()
        return int(count)
