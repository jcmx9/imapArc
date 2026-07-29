"""Load IMAP accounts from a .env file.

Accounts live in ``.env`` (git-ignored) as ``IMAP_<ACCOUNT>_<FIELD>``::

    IMAP_PRIVAT_HOST=imap.example.com
    IMAP_PRIVAT_PORT=993
    IMAP_PRIVAT_USER=you@example.com
    IMAP_PRIVAT_PASSWORD=your-password
    IMAP_PRIVAT_SSL=true

The account name (here ``privat``) is what profiles reference; it is matched
case-insensitively. Passwords sit in .env in clear text, protected only by file
permissions — keep the .env out of version control.
"""

from __future__ import annotations

import re
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, SecretStr

from imaparc.exceptions import ImapArcError

_ENV_KEY = re.compile(r"^IMAP_(?P<account>.+)_(?P<field>HOST|PORT|USER|PASSWORD|SSL)$")
_TRUE = frozenset({"1", "true", "yes", "on"})


class ConfigError(ImapArcError):
    """Account or profile configuration is invalid."""


class Account(BaseModel):
    """One IMAP account's connection details."""

    name: str
    host: str
    port: int = 993
    user: str
    password: SecretStr
    ssl: bool = True


def load_accounts(env_path: Path) -> dict[str, Account]:
    """Parse ``.env`` into ``{account_name: Account}``.

    Raises:
        ConfigError: If the file is missing or an account is incomplete.
    """
    if not env_path.exists():
        raise ConfigError(f".env not found: {env_path}")

    grouped: dict[str, dict[str, str]] = {}
    for key, value in dotenv_values(env_path).items():
        match = _ENV_KEY.match(key)
        if match and value is not None:
            account = match.group("account").lower()
            grouped.setdefault(account, {})[match.group("field").lower()] = value

    accounts: dict[str, Account] = {}
    for name, fields in grouped.items():
        try:
            accounts[name] = Account(
                name=name,
                host=fields["host"],
                port=int(fields.get("port", "993")),
                user=fields["user"],
                password=SecretStr(fields["password"]),
                ssl=fields.get("ssl", "true").strip().lower() in _TRUE,
            )
        except (KeyError, ValueError) as exc:
            raise ConfigError(f"account '{name}' is incomplete: {exc}") from exc
    return accounts
