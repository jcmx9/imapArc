"""Orchestrate the fetch phase: IMAP → eml archive, with post-processing.

For each account, connect once and, for every folder its profiles care about,
scan all candidate messages (envelope headers only) and match each against the
current profiles. Every run re-evaluates all candidates; the state store records
only what was already delivered, so a changed or newly added profile takes effect
on existing mail without any manual reset. A match's full body is pulled, written
to the profile's eml archive, marked delivered, and only then are post-fetch actions
(label, then move or delete) applied. If delivery fails, nothing is marked or
touched on the server, so no mail is lost and it is retried next run.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from imaparc.accounts import Account
from imaparc.exceptions import ImapArcError
from imaparc.mail.parser import parse_mail
from imaparc.naming import build_base_name
from imaparc.profiles import Profile, attachments_match, matches
from imaparc.sources.deliver import deliver_eml
from imaparc.sources.imap import ImapConnection, ScannedMessage
from imaparc.state import StateStore

logger = logging.getLogger(__name__)


@dataclass
class PlannedDelivery:
    """What a dry run *would* have done with one message."""

    profile: str
    folder: str
    uid: int
    subject: str
    action: str  # human-readable, e.g. "move to 'imapArc'" or "leave in place"


@dataclass
class FetchReport:
    """Counts delivered messages per profile and records failures."""

    delivered: dict[str, int] = field(default_factory=dict)
    failed: int = 0
    post_fetch_failed: int = 0  # archived, but the server-side action failed
    already_archived: int = 0  # candidates seen but archived on a previous run
    post_fetch_skipped: int = 0  # server action skipped: no local .eml copy now
    planned: list[PlannedDelivery] = field(default_factory=list)  # dry run only

    def add(self, profile_name: str) -> None:
        self.delivered[profile_name] = self.delivered.get(profile_name, 0) + 1

    @property
    def total(self) -> int:
        return sum(self.delivered.values())

    def summary(self) -> str:
        if self.planned:
            return self._dry_run_summary()
        lines = [f"{self.total} delivered"]
        for name, count in sorted(self.delivered.items()):
            lines.append(f"  {name}: {count}")
        if self.already_archived:
            lines.append(
                f"{self.already_archived} already archived on a previous run "
                "(use 'imaparc reset' to re-process everything)"
            )
        if self.failed:
            lines.append(f"{self.failed} failed (see log)")
        if self.post_fetch_failed:
            lines.append(
                f"{self.post_fetch_failed} archived but the server-side "
                "label/move/delete failed (see log)"
            )
        if self.post_fetch_skipped:
            lines.append(
                f"{self.post_fetch_skipped} server action(s) skipped — no local "
                ".eml copy in the configured output (nothing moved/deleted)"
            )
        return "\n".join(lines)

    def _dry_run_summary(self) -> str:
        """What a real run would do — nothing has been written or touched."""
        lines: list[str] = []
        for item in self.planned:
            lines.append(
                f"{item.profile:<12} {item.folder}/{item.uid}  {item.subject!r}"
            )
            lines.append(f"{'':<12} → would {item.action}")
        lines.append("")
        lines.append(
            f"{len(self.planned)} mail(s) would be archived — dry run, "
            "nothing written and nothing changed on the server"
        )
        return "\n".join(lines)


def run_fetch(
    accounts: dict[str, Account],
    profiles: list[Profile],
    state: StateStore,
    *,
    dry_run: bool = False,
    no_server_actions: bool = False,
) -> FetchReport:
    """Fetch new mail for all profiles and deliver it into their eml archives.

    ``dry_run`` only reports what would happen: no ``.eml``, no state entry, no
    server call. ``no_server_actions`` archives as usual but suppresses every
    label/move/delete — useful for a first run with a new profile, where
    ``after_fetch: delete`` is otherwise irreversible.
    """
    report = FetchReport()
    by_account: dict[str, list[Profile]] = defaultdict(list)
    for profile in profiles:
        by_account[profile.account.lower()].append(profile)

    for account_name, account_profiles in by_account.items():
        account = accounts[account_name]
        try:
            with ImapConnection(account) as conn:
                delimiter = "/"
                all_folders: list[str] = []
                trash_folders: frozenset[str] = frozenset()
                if any(p.match.recursive for p in account_profiles):
                    delimiter, all_folders = conn.list_folders()
                    # Only ask the server for its Trash folder when a recursive
                    # profile would otherwise scan it (i.e. has not opted in).
                    if any(
                        p.match.recursive and not p.match.trash
                        for p in account_profiles
                    ):
                        trash_folders = frozenset(conn.trash_folders())
                # Never auto-scan a profile's own move target: a recursive scan
                # would otherwise re-encounter moved mail (new UID) and deliver it
                # again as a duplicate. On INBOX-namespaced servers the target
                # (INBOX.imapArc) always sits inside the recursive tree. But if a
                # profile *explicitly* lists that folder as a source, honour it —
                # only exclude targets nobody asked to scan.
                explicit = {
                    f for p in account_profiles for f in (p.match.folders or [])
                }
                excluded = _move_targets(conn, account_profiles) - explicit
                folder_map = _folder_map(
                    account_profiles, all_folders, delimiter, trash_folders
                )
                for folder, folder_profiles in folder_map.items():
                    if folder in excluded:
                        continue
                    _fetch_folder(
                        conn,
                        account_name,
                        folder,
                        folder_profiles,
                        state,
                        report,
                        dry_run=dry_run,
                        no_server_actions=no_server_actions,
                    )
        except ImapArcError as exc:
            logger.error("account '%s' failed: %s", account_name, exc)
        except Exception:
            # One bad account (connection reset, protocol error, …) must not
            # abort the others; nothing was recorded, so it retries next run.
            logger.exception("account '%s' failed unexpectedly", account_name)
    return report


def _effective_folders(
    profile: Profile,
    all_folders: list[str],
    delimiter: str,
    trash_folders: frozenset[str] = frozenset(),
) -> list[str]:
    """The folders a profile scans, expanding subfolders when ``recursive``.

    A recursive expansion drops the server's Trash folder unless the profile opts
    in (``match.trash``) or lists it explicitly — so deleted mail is not silently
    re-archived (and, with ``delete``/``move_to``, re-processed) by a recursive
    scan. A non-recursive profile scans exactly what it lists.
    """
    base = profile.match.folders or ["INBOX"]
    if not profile.match.recursive:
        return base
    result: set[str] = set()
    for folder in base:
        result.add(folder)
        prefix = folder + delimiter
        result.update(name for name in all_folders if name.startswith(prefix))
    if not profile.match.trash:
        result -= trash_folders - set(base)  # explicit listing always wins
    return sorted(result)


def _move_targets(conn: ImapConnection, profiles: list[Profile]) -> set[str]:
    """Resolved server folders that profiles move mail into — never scan these.

    A profile that moves matched mail into ``imapArc`` must not then scan
    ``INBOX.imapArc`` (recursively), or the moved mail — now under a new UID —
    looks new and is delivered again as a duplicate.
    """
    targets: set[str] = set()
    for profile in profiles:
        action = profile.after_fetch
        if action and action.move_to:
            source = (profile.match.folders or ["INBOX"])[0]
            targets.add(conn.resolve_move_destination(source, action.move_to))
    return targets


def _folder_map(
    profiles: list[Profile],
    all_folders: list[str],
    delimiter: str,
    trash_folders: frozenset[str] = frozenset(),
) -> dict[str, list[Profile]]:
    """Map each folder to scan to the profiles that scan it (file order kept)."""
    mapping: dict[str, list[Profile]] = defaultdict(list)
    for profile in profiles:
        folders = _effective_folders(profile, all_folders, delimiter, trash_folders)
        for folder in folders:
            mapping[folder].append(profile)
    return mapping


def _earliest_since(profiles: list[Profile]) -> date | None:
    """Earliest ``since`` across the profiles scanning a folder.

    Returns None if any profile has no ``since`` (it wants everything), so the
    server-side search is only narrowed when every profile agrees to a bound.
    """
    sinces = [p.match.since for p in profiles]
    if not sinces or None in sinces:
        return None
    return min(s for s in sinces if s is not None)


def _fetch_folder(
    conn: ImapConnection,
    account_name: str,
    folder: str,
    profiles: list[Profile],
    state: StateStore,
    report: FetchReport,
    *,
    dry_run: bool = False,
    no_server_actions: bool = False,
) -> None:
    """Scan a folder, match every candidate afresh, deliver the new matches.

    ``dry_run`` reports what would happen and writes nothing at all — no ``.eml``,
    no state entry, no server call. ``no_server_actions`` archives normally but
    suppresses every label/move/delete, for a first run with a new profile.
    """
    uidvalidity, candidates = conn.scan(folder, since=_earliest_since(profiles))
    delivered = state.delivered_uids(account_name, folder, uidvalidity)
    # Make the state store visible: say how many candidates were already archived
    # on a previous run, so "0 delivered" is never a silent mystery.
    already = sum(1 for m in candidates if m.uid in delivered)
    report.already_archived += already
    if already:
        logger.info(
            "scanned %d message(s) in %s (%d already archived on a previous run)",
            len(candidates),
            folder,
            already,
        )
    else:
        logger.info("scanned %d message(s) in %s", len(candidates), folder)
    # When a profile has a server-side action, re-examine already-delivered mail
    # too: a message still in the folder means its move/delete failed before, and
    # a configured action must be enforced, not silently dropped.
    retry_actions = any(p.after_fetch is not None for p in profiles)
    for message in candidates:
        already = message.uid in delivered
        if already and not retry_actions:
            continue
        try:
            profile, raw = _match_candidate(conn, folder, message, profiles)
            if profile is None:
                # Not a match: leave it unmarked so a later profile change can
                # still pick it up on a future run.
                continue
            if dry_run:
                # Record the intent and move on before anything is written: no
                # .eml, no state row, no server call. That is the whole point.
                report.planned.append(
                    PlannedDelivery(
                        profile=profile.name,
                        folder=folder,
                        uid=message.uid,
                        subject=message.headers.subject,
                        action=_describe_action(profile),
                    )
                )
                continue
            eml_dir = profile.output / "eml"
            if not already:
                if raw is None:
                    raw = conn.fetch_body(folder, message.uid)
                if raw is None:  # pragma: no cover - server quirk
                    logger.warning("no body for UID %s in %s", message.uid, folder)
                    continue
                basename = _basename_for(profile, message)
                eml_path = deliver_eml(eml_dir, raw, basename)
                state.mark_delivered(
                    account_name, folder, uidvalidity, message.uid, eml_path.name
                )
                report.add(profile.name)
            else:
                # Re-check the *exact* file recorded for this UID; only fall back
                # to a reconstructed name for legacy rows written before filenames
                # were tracked.
                stored = state.delivered_filename(
                    account_name, folder, uidvalidity, message.uid
                )
                name = stored or f"{_basename_for(profile, message)}.eml"
                eml_path = eml_dir / name
            if profile.after_fetch is not None and not no_server_actions:
                # New match, or a delivered one still present because a prior
                # move/delete failed — (re-)apply so the action is enforced.
                # But never touch the server unless the local .eml exists *now*:
                # if the archive was moved away or the output path changed, a
                # delete/move here would destroy the only copy. "imapArc
                # preserves" holds even for server-side deletion.
                if eml_path.exists():
                    # Best-effort: the archive is durable, so a server failure
                    # warns and is counted, never crashes the folder.
                    _post_fetch(conn, folder, message.uid, profile, report)
                else:
                    report.post_fetch_skipped += 1
                    logger.warning(
                        "UID %s in %s: matches a profile with a server-side "
                        "action, but no local archive at %s — skipping (nothing "
                        "moved or deleted on the server)",
                        message.uid,
                        folder,
                        eml_path,
                    )
        except ImapArcError as exc:
            report.failed += 1
            logger.error("UID %s in %s failed: %s", message.uid, folder, exc)
        except Exception:
            # A single malformed message (parser bug, disk error, …) must not
            # abort the folder; it stays unmarked so it retries next run.
            report.failed += 1
            logger.exception("UID %s in %s failed unexpectedly", message.uid, folder)


def _describe_action(profile: Profile) -> str:
    """The server-side action a real run would apply, in plain words."""
    action = profile.after_fetch
    if action is None:
        return "archive it and leave the server untouched"
    parts: list[str] = []
    if action.label:
        parts.append(f"label it {action.label!r}")
    if action.move_to:
        parts.append(f"move it to {action.move_to!r}")
    elif action.delete:
        parts.append("DELETE it from the server")
    return "archive it, then " + " and ".join(parts) if parts else "archive it"


def _basename_for(profile: Profile, message: ScannedMessage) -> str:
    """The shared ``.eml``/PDF base name for a delivered message.

    Uses the message ``Date`` header, falling back to the IMAP ``INTERNALDATE``.
    The naming options come from the profile, which is also what the render phase
    reads — the two must agree, or the ``.eml`` and its PDF folder end up with
    different names and nothing links them any more.
    """
    timestamp = message.headers.date or message.received
    return build_base_name(
        timestamp,
        profile.name,
        message.headers.subject,
        pattern=profile.filename_pattern,
        date_format=profile.date_format,
    )


def _match_candidate(
    conn: ImapConnection,
    folder: str,
    message: ScannedMessage,
    profiles: list[Profile],
) -> tuple[Profile | None, bytes | None]:
    """First profile matching this message; also the body if it had to be read.

    Header rules are checked on the cheap envelope. Only a profile with an
    ``attachments`` filter forces the body to be pulled (once, lazily) to inspect
    attachment types — returned so the caller does not fetch it twice.
    """
    raw: bytes | None = None
    names: list[str] | None = None
    for profile in profiles:
        if not matches(profile, message.headers, received=message.received):
            continue
        if profile.match.attachments:
            if raw is None:
                raw = conn.fetch_body(folder, message.uid)
                if raw is None:  # pragma: no cover - server quirk
                    return None, None
                names = [a.filename for a in parse_mail(raw).attachments]
            if not attachments_match(profile.match.attachments, names or []):
                continue
        return profile, raw
    return None, raw


def _post_fetch(
    conn: ImapConnection,
    folder: str,
    uid: int,
    profile: Profile,
    report: FetchReport,
) -> None:
    """Apply label, then move or delete — only reached after a safe write.

    ``move_to`` and ``delete`` are mutually exclusive (enforced at config load),
    so at most one of them runs. The label goes first because a move mints a new
    server-side UID — but a label failure (e.g. a server that rejects a custom
    keyword) must NOT prevent the move/delete, so the two are isolated.

    Best-effort throughout: the message is already in the eml archive and marked
    delivered, so a server-side failure is logged as a concise warning (and the
    move/delete failure counted), never raised — one bad action must not flood
    the output with tracebacks or abort the folder.
    """
    action = profile.after_fetch
    if action is None:
        return

    if action.label:
        try:
            conn.label(folder, uid, action.label)
        except Exception as exc:  # a label failure must not block move/delete
            logger.warning(
                "UID %s in %s: label '%s' failed (continuing): %s",
                uid,
                folder,
                action.label,
                exc,
            )

    if not (action.move_to or action.delete):
        return
    try:
        if action.move_to:
            conn.move(folder, uid, action.move_to)
        else:
            conn.delete(folder, uid)
    except Exception as exc:  # best-effort server cleanup — never fatal
        report.post_fetch_failed += 1
        what = f"move to '{action.move_to}'" if action.move_to else "delete"
        logger.warning(
            "UID %s in %s: archived locally, but server-side %s failed: %s",
            uid,
            folder,
            what,
            exc,
        )
