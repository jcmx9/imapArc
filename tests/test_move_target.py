"""Tests for resolving a move target to the server's namespace (pure, no server)."""

from __future__ import annotations

import pytest

from imaparc.sources.imap import resolve_move_target


@pytest.mark.parametrize(
    ("destination", "prefix", "delimiter", "expected"),
    [
        # INBOX-prefixed personal namespace (e.g. Dovecot): friendly names land
        # under the prefix.
        ("imapArc", "INBOX.", ".", "INBOX.imapArc"),
        ("Archiv/Erledigt", "INBOX.", ".", "INBOX.Archiv.Erledigt"),
        # Already namespaced: left as is (no double prefix).
        ("INBOX.imapArc", "INBOX.", ".", "INBOX.imapArc"),
        # Slashes the user typed are normalised to the server delimiter.
        ("Archiv/2026", "INBOX.", ".", "INBOX.Archiv.2026"),
        # Empty personal prefix (top-level namespace): no prefix added.
        ("imapArc", "", "/", "imapArc"),
        ("Archiv/Done", "", "/", "Archiv/Done"),
        # A prefix with a different delimiter still composes correctly.
        ("Work/Client", "Personal/", "/", "Personal/Work/Client"),
    ],
)
def test_resolve_move_target(
    destination: str, prefix: str, delimiter: str, expected: str
) -> None:
    assert resolve_move_target(destination, prefix, delimiter) == expected


def test_resolve_move_target_empty_delimiter_defaults_to_slash() -> None:
    assert resolve_move_target("Archiv/Done", "", "") == "Archiv/Done"
