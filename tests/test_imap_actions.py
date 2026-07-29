"""Unit tests for move/delete capability fallbacks (no live server needed)."""

from __future__ import annotations

import pytest

from imaparc.exceptions import ImapArcError
from imaparc.sources.imap import ImapConnection


class _FakeClient:
    """Records IMAP calls and reports a configurable capability set."""

    def __init__(self, capabilities: set[str]) -> None:
        self._caps = capabilities
        self.calls: list[tuple[object, ...]] = []

    def has_capability(self, capability: str) -> bool:
        return capability in self._caps

    def select_folder(self, folder: str, readonly: bool = False) -> None:
        self.calls.append(("select", folder))

    def folder_exists(self, folder: str) -> bool:
        return True

    def create_folder(self, folder: str) -> None:  # pragma: no cover - unused here
        self.calls.append(("create", folder))

    def subscribe_folder(self, folder: str) -> None:
        self.calls.append(("subscribe", folder))

    def move(self, uids: list[int], folder: str) -> None:
        self.calls.append(("move", tuple(uids), folder))

    def copy(self, uids: list[int], folder: str) -> None:
        self.calls.append(("copy", tuple(uids), folder))

    def delete_messages(self, uids: list[int], silent: bool = False) -> None:
        self.calls.append(("delete_messages", tuple(uids)))

    def expunge(self, messages: list[int] | None = None) -> None:
        self.calls.append(("expunge", tuple(messages) if messages else None))

    def kinds(self) -> list[object]:
        return [c[0] for c in self.calls]


def _conn_with(client: _FakeClient) -> ImapConnection:
    conn = ImapConnection.__new__(ImapConnection)
    conn._client = client  # type: ignore[attr-defined]
    conn.resolve_move_destination = lambda source, dest: dest  # type: ignore[method-assign]
    return conn


def test_move_uses_move_when_supported() -> None:
    client = _FakeClient({"MOVE"})
    _conn_with(client).move("INBOX", 5, "Archiv")
    assert ("move", (5,), "Archiv") in client.calls
    assert "copy" not in client.kinds()


def test_move_falls_back_to_copy_without_move() -> None:
    # No MOVE (e.g. Gmail) → COPY + \Deleted + UID EXPUNGE.
    client = _FakeClient({"UIDPLUS"})
    _conn_with(client).move("INBOX", 5, "Archiv")
    kinds = client.kinds()
    assert "move" not in kinds
    assert ("copy", (5,), "Archiv") in client.calls
    assert "delete_messages" in kinds
    assert ("expunge", (5,)) in client.calls


def test_delete_uses_uid_expunge_with_uidplus() -> None:
    client = _FakeClient({"UIDPLUS"})
    _conn_with(client).delete("INBOX", 5)
    assert ("delete_messages", (5,)) in client.calls
    assert ("expunge", (5,)) in client.calls


def test_delete_without_uidplus_refuses_to_expunge() -> None:
    # A bare EXPUNGE would remove other \Deleted mail — refuse, leave it flagged.
    client = _FakeClient(set())
    with pytest.raises(ImapArcError):
        _conn_with(client).delete("INBOX", 5)
    assert ("delete_messages", (5,)) in client.calls
    assert "expunge" not in client.kinds()


class _FoldersClient:
    """Stand-in exposing a fixed list_folders() result: (flags, delim, name)."""

    def __init__(self, folders: list[tuple[list[bytes], bytes, str]]) -> None:
        self._folders = folders

    def list_folders(self) -> list[tuple[list[bytes], bytes, str]]:
        return self._folders


def _conn_folders(folders: list[tuple[list[bytes], bytes, str]]) -> ImapConnection:
    conn = ImapConnection.__new__(ImapConnection)
    conn._client = _FoldersClient(folders)  # type: ignore[attr-defined]
    return conn


def test_trash_folders_by_special_use_flag() -> None:
    conn = _conn_folders(
        [
            ([b"\\HasNoChildren"], b"/", "INBOX"),
            ([b"\\Trash"], b"/", "INBOX/Bin"),  # RFC 6154 special-use, any name
        ]
    )
    assert conn.trash_folders() == {"INBOX/Bin"}


def test_trash_folders_name_fallback_without_special_use() -> None:
    conn = _conn_folders(
        [
            ([b"\\HasNoChildren"], b"/", "INBOX"),
            ([b"\\HasNoChildren"], b"/", "INBOX/Trash"),
            ([b"\\HasNoChildren"], b"/", "INBOX/Papierkorb"),
        ]
    )
    assert conn.trash_folders() == {"INBOX/Trash", "INBOX/Papierkorb"}


def test_special_use_flag_wins_over_name_match() -> None:
    conn = _conn_folders(
        [
            ([b"\\Trash"], b"/", "INBOX/Bin"),
            ([b"\\HasNoChildren"], b"/", "INBOX/Trash"),  # name only, no flag
        ]
    )
    assert conn.trash_folders() == {"INBOX/Bin"}
