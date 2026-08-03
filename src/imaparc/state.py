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
    profile TEXT,
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
        """Add columns a pre-existing table is missing.

        Rows written before a column existed keep NULL there. For ``profile``
        that means a targeted clear cannot reach them — see
        :meth:`untracked_count`, which is why the number is surfaced rather than
        quietly ignored.
        """
        columns = {row[1] for row in conn.execute("PRAGMA table_info(delivered)")}
        if "filename" not in columns:
            conn.execute("ALTER TABLE delivered ADD COLUMN filename TEXT")
        if "profile" not in columns:
            conn.execute("ALTER TABLE delivered ADD COLUMN profile TEXT")

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
        profile: str | None = None,
    ) -> None:
        """Record that ``uid`` has been delivered, with its ``.eml`` filename.

        ``profile`` is stored so ``reset --profile`` can drop one profile's
        state without discarding every other profile's.
        """
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO delivered "
                "(account, folder, uidvalidity, uid, filename, profile) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (account, folder, uidvalidity, uid, filename, profile),
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

    def clear(self, profile: str | None = None) -> int:
        """Forget delivered UIDs; return how many were dropped.

        Without ``profile`` this drops everything and the next fetch re-evaluates
        every matching mail. With one, only that profile's rows go — correcting a
        single profile's rules should not cost every other profile its state and
        send the next run through the whole mailbox again.

        Rows written before profiles were recorded carry NULL and are never
        matched by a targeted clear; :meth:`untracked_count` reports how many.
        """
        with closing(self._connect()) as conn:
            if profile is None:
                count = conn.execute("SELECT COUNT(*) FROM delivered").fetchone()[0]
                conn.execute("DELETE FROM delivered")
            else:
                count = conn.execute(
                    "SELECT COUNT(*) FROM delivered WHERE profile = ?", (profile,)
                ).fetchone()[0]
                conn.execute("DELETE FROM delivered WHERE profile = ?", (profile,))
            conn.commit()
        return int(count)

    def untracked_count(self) -> int:
        """Rows with no profile recorded — unreachable by a targeted clear.

        Surfaced so ``reset --profile`` cannot appear to have worked while the
        mail it was meant to free up stays skipped.
        """
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM delivered WHERE profile IS NULL"
            ).fetchone()
        return int(row[0])
