"""Tests for the SQLite fetch state store (delivered-UID tracking)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from imaparc.state import StateStore


def _store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.db")


def test_new_folder_has_no_delivered_uids(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.delivered_uids("privat", "INBOX", uidvalidity=1) == set()


def test_mark_and_read_back(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.mark_delivered("privat", "INBOX", uidvalidity=1, uid=42)
    store.mark_delivered("privat", "INBOX", uidvalidity=1, uid=43)
    assert store.delivered_uids("privat", "INBOX", uidvalidity=1) == {42, 43}


def test_mark_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.mark_delivered("privat", "INBOX", uidvalidity=1, uid=42)
    store.mark_delivered("privat", "INBOX", uidvalidity=1, uid=42)  # no error
    assert store.delivered_uids("privat", "INBOX", uidvalidity=1) == {42}


def test_uidvalidity_change_is_a_fresh_set(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.mark_delivered("privat", "INBOX", uidvalidity=1, uid=42)
    # A new UIDVALIDITY means UIDs are no longer stable — separate generation.
    assert store.delivered_uids("privat", "INBOX", uidvalidity=2) == set()
    store.mark_delivered("privat", "INBOX", uidvalidity=2, uid=5)
    assert store.delivered_uids("privat", "INBOX", uidvalidity=2) == {5}


def test_folders_are_independent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.mark_delivered("privat", "INBOX", uidvalidity=1, uid=42)
    store.mark_delivered("privat", "Archiv", uidvalidity=1, uid=7)
    assert store.delivered_uids("privat", "INBOX", uidvalidity=1) == {42}
    assert store.delivered_uids("privat", "Archiv", uidvalidity=1) == {7}


def test_persists_across_instances(tmp_path: Path) -> None:
    _store(tmp_path).mark_delivered("privat", "INBOX", uidvalidity=1, uid=42)
    reopened = StateStore(tmp_path / "state.db")
    assert reopened.delivered_uids("privat", "INBOX", uidvalidity=1) == {42}


def test_records_and_reads_back_filename(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.mark_delivered("privat", "INBOX", uidvalidity=1, uid=42, filename="a-2.eml")
    assert store.delivered_filename("privat", "INBOX", 1, 42) == "a-2.eml"


def test_filename_is_none_when_not_recorded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Unknown UID, and a UID marked without a filename (legacy call site).
    assert store.delivered_filename("privat", "INBOX", 1, 99) is None
    store.mark_delivered("privat", "INBOX", uidvalidity=1, uid=42)
    assert store.delivered_filename("privat", "INBOX", 1, 42) is None


def test_migrates_pre_filename_database(tmp_path: Path) -> None:
    # A DB created before the filename column existed must gain it, keeping data.
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE delivered (account TEXT NOT NULL, folder TEXT NOT NULL, "
            "uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL, "
            "PRIMARY KEY (account, folder, uidvalidity, uid))"
        )
        conn.execute("INSERT INTO delivered VALUES ('privat', 'INBOX', 1, 42)")
        conn.commit()
    store = StateStore(db)
    assert store.delivered_uids("privat", "INBOX", 1) == {42}  # data preserved
    assert store.delivered_filename("privat", "INBOX", 1, 42) is None  # legacy row
    # The new column is usable after migration.
    store.mark_delivered("privat", "INBOX", uidvalidity=1, uid=43, filename="x.eml")
    assert store.delivered_filename("privat", "INBOX", 1, 43) == "x.eml"


def test_clear_forgets_everything(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.mark_delivered("privat", "INBOX", uidvalidity=1, uid=1)
    store.mark_delivered("privat", "INBOX", uidvalidity=1, uid=2)
    store.mark_delivered("arbeit", "Sent", uidvalidity=3, uid=9)
    assert store.clear() == 3
    assert store.delivered_uids("privat", "INBOX", uidvalidity=1) == set()
    assert store.clear() == 0  # idempotent
