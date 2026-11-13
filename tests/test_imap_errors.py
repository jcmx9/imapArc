"""Expected IMAP failures end in one line, not a traceback (no server needed)."""

from __future__ import annotations

import logging
import socket
from pathlib import Path

import pytest
from imapclient.exceptions import LoginError

from imaparc.accounts import Account
from imaparc.exceptions import ImapConnectError
from imaparc.sources import imap
from imaparc.sources.imap import ImapConnection


def _closed_port() -> int:
    """A local port nothing listens on: bound, then released."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


def _account(port: int) -> Account:
    return Account(
        name="privat", host="127.0.0.1", port=port, user="u", password="p", ssl=False
    )


def test_an_unreachable_server_is_an_imaparc_error() -> None:
    with (
        pytest.raises(ImapConnectError, match=r"cannot reach 127\.0\.0\.1:\d+"),
        ImapConnection(_account(_closed_port()), timeout=2),
    ):
        pass


def test_a_refused_login_is_an_imaparc_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.normalise_times = True

        def login(self, _user: str, _password: str) -> None:
            raise LoginError("[AUTHENTICATIONFAILED] Invalid credentials")

        def shutdown(self) -> None:
            pass

    monkeypatch.setattr(imap, "IMAPClient", _Client)

    with (
        pytest.raises(ImapConnectError, match=r"login as u at .*:993 refused"),
        ImapConnection(_account(993)),
    ):
        pass


def test_fetch_reports_an_unreachable_account_in_one_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from imaparc.fetch import run_fetch
    from imaparc.profiles import Profile
    from imaparc.state import StateStore

    profile = Profile(name="p", account="privat", output=tmp_path / "p")
    state = StateStore(tmp_path / "state.db")
    with caplog.at_level(logging.ERROR):
        run_fetch({"privat": _account(_closed_port())}, [profile], state, verbosity=0)

    [record] = [r for r in caplog.records if r.name == "imaparc.fetch"]
    assert "account 'privat' failed: cannot reach" in record.getMessage()
    assert record.exc_info is None
