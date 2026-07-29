"""Tests for loading IMAP accounts from .env."""

from __future__ import annotations

from pathlib import Path

import pytest

from imaparc.accounts import ConfigError, load_accounts


def _env(tmp_path: Path, content: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(content, encoding="utf-8")
    return path


def test_loads_one_account(tmp_path: Path) -> None:
    env = _env(
        tmp_path,
        "IMAP_PRIVAT_HOST=imap.example.com\n"
        "IMAP_PRIVAT_USER=you@example.com\n"
        "IMAP_PRIVAT_PASSWORD=secret\n",
    )
    accounts = load_accounts(env)
    assert set(accounts) == {"privat"}
    acc = accounts["privat"]
    assert acc.host == "imap.example.com"
    assert acc.user == "you@example.com"
    assert acc.password.get_secret_value() == "secret"
    assert acc.port == 993  # default
    assert acc.ssl is True  # default
    # The password must not leak through repr/str (SecretStr).
    assert "secret" not in repr(acc)
    assert "secret" not in str(acc)


def test_loads_multiple_accounts(tmp_path: Path) -> None:
    env = _env(
        tmp_path,
        "IMAP_PRIVAT_HOST=a.example.com\nIMAP_PRIVAT_USER=u1\n"
        "IMAP_PRIVAT_PASSWORD=p1\n"
        "IMAP_ARBEIT_HOST=b.example.com\nIMAP_ARBEIT_USER=u2\n"
        "IMAP_ARBEIT_PASSWORD=p2\nIMAP_ARBEIT_PORT=143\nIMAP_ARBEIT_SSL=false\n",
    )
    accounts = load_accounts(env)
    assert set(accounts) == {"privat", "arbeit"}
    assert accounts["arbeit"].port == 143
    assert accounts["arbeit"].ssl is False


def test_account_name_is_lowercased(tmp_path: Path) -> None:
    env = _env(
        tmp_path,
        "IMAP_Privat_HOST=a\nIMAP_Privat_USER=u\nIMAP_Privat_PASSWORD=p\n",
    )
    assert "privat" in load_accounts(env)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_accounts(tmp_path / "nope.env")


def test_incomplete_account_raises(tmp_path: Path) -> None:
    env = _env(tmp_path, "IMAP_PRIVAT_HOST=a.example.com\n")  # no user/password
    with pytest.raises(ConfigError, match="incomplete"):
        load_accounts(env)


def test_ignores_unrelated_keys(tmp_path: Path) -> None:
    env = _env(
        tmp_path,
        "SOME_OTHER=x\nIMAP_PRIVAT_HOST=a\nIMAP_PRIVAT_USER=u\n"
        "IMAP_PRIVAT_PASSWORD=p\n",
    )
    assert set(load_accounts(env)) == {"privat"}
