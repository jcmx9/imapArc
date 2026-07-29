"""Fetch source: messages from an IMAP account.

An :class:`ImapConnection` is an open, logged-in session to one account, used
both to fetch and to post-process. Reads use ``BODY.PEEK[]``, so the server
``\\Seen`` flag is never touched — collection is non-invasive unless a profile
explicitly labels, moves or deletes the source message.

Fetching is two-phase and cheap: :meth:`scan` lists candidate messages with only
their envelope headers (server-side ``SINCE`` narrows the set), the caller
matches those against the profiles, and only a match's full body is pulled via
:meth:`fetch_body`. Every run re-evaluates all candidates; the state store only
records what was already delivered, so profile changes take effect on old mail.
"""

from __future__ import annotations

import contextlib
import email.header
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from types import TracebackType
from typing import Any, Self

from imapclient import IMAPClient

from imaparc.accounts import Account
from imaparc.exceptions import ImapArcError
from imaparc.mail.models import MailHeaders

logger = logging.getLogger(__name__)

# LIST flags marking a mailbox that cannot be SELECTed and must be skipped.
_UNSELECTABLE_FLAGS = frozenset({"\\noselect", "\\nonexistent"})

# Well-known Trash folder leaf names, used only when a server advertises no
# RFC 6154 \Trash special-use flag (matched case-insensitively on the last path
# segment).
_TRASH_NAMES = frozenset(
    {
        "trash",
        "deleted",
        "deleted messages",
        "deleted items",
        "papierkorb",
        "bin",
    }
)


def _flag_name(flag: object) -> str:
    """Normalise a LIST flag (bytes or str) to a lowercase string for comparison."""
    return flag.decode().lower() if isinstance(flag, bytes) else str(flag).lower()


@dataclass(frozen=True, slots=True)
class ScannedMessage:
    """A candidate message: its UID, envelope headers and server receive time."""

    uid: int
    headers: MailHeaders
    received: datetime | None = None


def _decode_mime(raw: bytes | None) -> str:
    """RFC-2047-decode an envelope byte string (Subject) to text."""
    if not raw:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(raw.decode())))
    except (UnicodeDecodeError, ValueError):
        return raw.decode("latin-1", errors="replace")


def _one_address(addr: Any) -> str:
    """Format a single envelope address as ``mailbox@host``."""
    mailbox = (addr.mailbox or b"").decode(errors="replace")
    host = (addr.host or b"").decode(errors="replace")
    return f"{mailbox}@{host}" if host else mailbox


def _address_list(addresses: Any) -> str:
    """Join an envelope address list into a comma-separated header string."""
    if not addresses:
        return ""
    return ", ".join(_one_address(a) for a in addresses)


def _headers_from_envelope(env: Any) -> MailHeaders:
    """Build the header subset needed for matching from an IMAP ENVELOPE."""
    env_date = env.date if isinstance(env.date, datetime) else None
    message_id = env.message_id.decode(errors="replace") if env.message_id else None
    return MailHeaders(
        from_=_address_list(env.from_),
        to=_address_list(env.to),
        cc=_address_list(env.cc),
        bcc=_address_list(env.bcc),
        subject=_decode_mime(env.subject),
        date=env_date,
        message_id=message_id,
    )


def resolve_move_target(destination: str, prefix: str, delimiter: str) -> str:
    """Resolve a user-given move target to the server's namespace and delimiter.

    The user writes a friendly name (``imapArc``, ``Archiv/Erledigt``); servers
    differ in hierarchy separator and personal-namespace prefix. This normalises
    any ``/`` or server-delimiter the user typed into path components, rejoins
    them with ``delimiter``, and prepends the personal-namespace ``prefix`` (e.g.
    ``INBOX.``) when the target is not already there — so ``imapArc`` becomes
    ``INBOX.imapArc`` on a server whose personal namespace is ``INBOX.``, and
    stays ``imapArc`` where the prefix is empty. ``prefix``/``delimiter`` come
    from the server (NAMESPACE / LIST), so nothing is hard-coded per server.
    """
    delimiter = delimiter or "/"
    seps = "".join(re.escape(s) for s in {"/", delimiter})
    parts = [p for p in re.split(f"[{seps}]", destination) if p]
    name = delimiter.join(parts) if parts else destination
    if prefix and not name.startswith(prefix):
        name = prefix + name
    return name


class ImapConnection:
    """An open IMAP session to one account, for fetching and post-processing."""

    def __init__(self, account: Account, *, timeout: float = 30.0) -> None:
        self._account = account
        self._timeout = timeout
        self._client: IMAPClient | None = None

    def __enter__(self) -> Self:
        client = IMAPClient(
            self._account.host,
            port=self._account.port,
            ssl=self._account.ssl,
            timeout=self._timeout,
        )
        client.login(self._account.user, self._account.password.get_secret_value())
        self._client = client
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            with contextlib.suppress(OSError):  # best-effort close
                self._client.logout()
            self._client = None

    @property
    def _conn(self) -> IMAPClient:
        if self._client is None:  # pragma: no cover - guarded by __enter__
            raise RuntimeError("ImapConnection is not open")
        return self._client

    def list_folders(self) -> tuple[str, list[str]]:
        """Return the folder-name delimiter and every selectable folder name.

        Unselectable folders are excluded: ``\\Noselect`` (a pure container,
        RFC 3501) and ``\\NonExistent`` (a listed but non-existent name, RFC 5258)
        — both would fail ``SELECT``. Recursion still descends through them via
        the returned subfolder names.
        """
        delimiter = "/"
        names: list[str] = []
        for flags, delim, name in self._conn.list_folders():
            if delim:
                delimiter = delim.decode() if isinstance(delim, bytes) else str(delim)
            if any(_flag_name(f) in _UNSELECTABLE_FLAGS for f in flags):
                continue
            names.append(name)
        return delimiter, names

    def trash_folders(self) -> set[str]:
        """Server folder names that are the Trash, for excluding from a scan.

        Prefers the RFC 6154 ``\\Trash`` special-use flag (the clean, declared
        answer). If the server advertises none (e.g. GreenMail, older Dovecot),
        fall back to the well-known folder *leaf* names so a recursive scan still
        skips the obvious Trash rather than re-archiving deleted mail.
        """
        by_flag: set[str] = set()
        by_name: set[str] = set()
        delimiter = "/"
        with contextlib.suppress(Exception):
            for flags, delim, name in self._conn.list_folders():
                if delim:
                    delimiter = (
                        delim.decode() if isinstance(delim, bytes) else str(delim)
                    )
                if any(_flag_name(f) == "\\trash" for f in flags):
                    by_flag.add(name)
                elif name.rsplit(delimiter, 1)[-1].lower() in _TRASH_NAMES:
                    by_name.add(name)
        return by_flag or by_name

    def scan(
        self, folder: str, *, since: date | None = None
    ) -> tuple[int, list[ScannedMessage]]:
        """List candidate messages in ``folder`` with their envelope headers.

        ``since`` narrows the server-side search (IMAP ``SINCE``) to messages on
        or after that day; ``None`` scans the whole folder. Only headers are
        fetched here — bodies are pulled later, per match, via :meth:`fetch_body`.

        Returns:
            The folder ``UIDVALIDITY`` and the candidates sorted by UID.
        """
        info = self._conn.select_folder(folder, readonly=True)
        uidvalidity = int(info[b"UIDVALIDITY"])
        criteria: list[Any] = ["SINCE", since] if since else ["ALL"]
        uids = self._conn.search(criteria)
        if not uids:
            return uidvalidity, []

        messages: list[ScannedMessage] = []
        for uid, data in self._conn.fetch(uids, ["ENVELOPE", "INTERNALDATE"]).items():
            env = data.get(b"ENVELOPE")
            if env is None:  # pragma: no cover - server quirk
                continue
            received = data.get(b"INTERNALDATE")
            messages.append(
                ScannedMessage(
                    uid=int(uid),
                    headers=_headers_from_envelope(env),
                    received=received if isinstance(received, datetime) else None,
                )
            )
        messages.sort(key=lambda m: m.uid)
        return uidvalidity, messages

    def fetch_body(self, folder: str, uid: int) -> bytes | None:
        """Return the raw bytes of one message via BODY.PEEK[] (non-invasive)."""
        self._conn.select_folder(folder, readonly=True)
        data = self._conn.fetch([uid], ["BODY.PEEK[]"]).get(uid)
        if data is None:  # pragma: no cover - server quirk
            return None
        raw = data.get(b"BODY[]")
        return raw if isinstance(raw, bytes) else None

    def label(self, folder: str, uid: int, keyword: str) -> None:
        """Add an IMAP keyword to a message (opens the folder writable)."""
        self._conn.select_folder(folder)
        self._conn.add_flags([uid], [keyword])

    def _delimiter(self) -> str:
        """The server's folder-hierarchy delimiter (from LIST), default ``/``."""
        with contextlib.suppress(Exception):
            for _flags, delim, _name in self._conn.list_folders():
                if delim:
                    return delim.decode() if isinstance(delim, bytes) else str(delim)
        return "/"

    def _move_namespace(self, source_folder: str) -> tuple[str, str]:
        """The personal-namespace ``(prefix, delimiter)`` for a moved message.

        Prefers the RFC 2342 NAMESPACE command (the clean, server-declared
        answer). If the server lacks NAMESPACE (e.g. GreenMail) or reports an
        empty personal prefix, fall back to the LIST delimiter and infer an
        ``INBOX``-rooted prefix only when the source folder itself lives under
        ``INBOX`` — so nothing is hard-coded per server.
        """
        prefix: str | None = None
        delimiter = self._delimiter()
        with contextlib.suppress(Exception):
            personal = self._conn.namespace().personal
            if personal:
                ns_prefix, ns_sep = personal[0]
                prefix = ns_prefix or ""
                if ns_sep:
                    delimiter = (
                        ns_sep.decode() if isinstance(ns_sep, bytes) else str(ns_sep)
                    )
        if not prefix:
            # No NAMESPACE, or an empty personal prefix: put a sibling of an
            # INBOX-rooted source folder back under INBOX.
            root = f"INBOX{delimiter}"
            prefix = root if source_folder.startswith(root) else ""
        return prefix, delimiter

    def resolve_move_destination(self, source_folder: str, destination: str) -> str:
        """The server folder a ``move_to: destination`` would land in.

        Same resolution as :meth:`move` (namespace + delimiter), exposed so the
        fetch loop can *exclude* a profile's move target from scanning — otherwise
        moved mail would be re-scanned (new UID) and delivered again as a copy.
        """
        prefix, delimiter = self._move_namespace(source_folder)
        return resolve_move_target(destination, prefix, delimiter)

    def move(self, folder: str, uid: int, destination: str) -> None:
        """Move a message into ``destination`` (created if missing).

        ``destination`` is resolved to the server's namespace and delimiter (see
        :func:`resolve_move_target`), so a friendly name like ``imapArc`` lands in
        the right place. A move mints a new UID, so this runs after any label.
        """
        self._conn.select_folder(folder)
        target = self.resolve_move_destination(folder, destination)
        if not self._conn.folder_exists(target):
            self._conn.create_folder(target)
        # Subscribe (idempotent) every time: many mail clients show only
        # subscribed folders, so an unsubscribed target would look like the mail
        # vanished. Doing it on every move also re-subscribes a folder that a
        # previous version created without subscribing.
        with contextlib.suppress(Exception):
            self._conn.subscribe_folder(target)
        if self._conn.has_capability("MOVE"):
            self._conn.move([uid], target)
        else:
            # RFC 6851 MOVE is optional and some common servers (notably Gmail
            # IMAP) do not advertise it. Fall back to the RFC 3501 sequence:
            # COPY to the target, flag \Deleted, expunge.
            self._conn.copy([uid], target)
            self._conn.delete_messages([uid])
            self._expunge_uid(uid)

    def delete(self, folder: str, uid: int) -> None:
        """Permanently delete a message from the server (\\Deleted + expunge).

        Only ever reached after the message is durably in the eml archive and its UID
        recorded, so the local archive is the source of truth before removal.
        """
        self._conn.select_folder(folder)
        self._conn.delete_messages([uid])
        self._expunge_uid(uid)

    def _expunge_uid(self, uid: int) -> None:
        """Expunge exactly one message by UID, or refuse if it is not safe.

        ``UID EXPUNGE`` (RFC 4315 / UIDPLUS) removes only the named message. When
        the server lacks UIDPLUS, a bare ``EXPUNGE`` would remove *every*
        ``\\Deleted`` message in the folder — including ones imapArc never touched
        — so we refuse rather than risk that collateral. The message stays flagged
        ``\\Deleted`` (and is safely archived locally); the caller reports it.
        """
        if self._conn.has_capability("UIDPLUS"):
            self._conn.expunge([uid])  # UID EXPUNGE — precise
            return
        raise ImapArcError(
            f"server lacks UIDPLUS: left UID {uid} flagged \\Deleted rather than "
            "risk expunging other messages in the folder"
        )
