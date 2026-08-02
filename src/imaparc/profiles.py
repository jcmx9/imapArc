"""Load conversation profiles from profile.yaml and match mail against them.

A profile is a conversation: matching rules (sender domain/address, subject
regex, folders, since-date), a freely chosen output directory, whether to render
PDFs, and optional post-fetch actions (label/move). Profiles are checked in file
order; the first match wins.
"""

from __future__ import annotations

import fnmatch
import re
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from imaparc import naming
from imaparc.accounts import Account, ConfigError
from imaparc.mail.models import MailHeaders
from imaparc.naming import extract_email_addresses

type AddressField = Literal["from", "to", "cc", "bcc"]
_ALL_FIELDS: list[AddressField] = ["from", "to", "cc", "bcc"]


class Match(BaseModel):
    """A profile's matching rules.

    A message matches when it satisfies *every* rule that is set (logical AND
    across rule kinds). Within ``domains``/``addresses`` any entry is enough
    (logical OR): the two together mean "an address in one of the searched header
    fields is on one of these domains OR is one of these exact addresses".

    - ``domains``: domains, written with a leading ``@`` to make clear it is a
      domain, not a full address (e.g. ``@hetzner.com``). Matches the exact
      domain and its subdomains (``billing@rechnung.hetzner.com`` too). A bare
      ``hetzner.com`` is accepted as well.
    - ``addresses``: exact addresses (``billing@hetzner.com``).
    - ``mode``: which address header fields ``domains``/``addresses`` are searched
      in — any of ``from``, ``to``, ``cc``, ``bcc``. Default: all four.
    - ``subject``: either a single Python regex (searched, not anchored), or a
      list of case-insensitive wildcard patterns (``*``/``?``, e.g.
      ``['*Rechnung*', 'Invoice*']``) of which any one must match.
    - ``attachments``: file extensions (``pdf``, ``.docx`` — dot optional); the
      message matches only if it carries an attachment with one of them. Default
      ``[]`` (no attachment requirement). Checked at fetch time on the body.
    - ``folders``: IMAP folders to scan (applied at fetch time, default INBOX).
    - ``recursive``: also scan the subfolders of each listed folder (default
      False). ``[INBOX]`` on its own is not recursive.
    - ``trash``: whether a recursive scan descends into the server's Trash folder
      (default False — deleted mail is left alone). The Trash is detected by the
      RFC 6154 ``\\Trash`` special-use flag (fallback: common names). Listing the
      Trash explicitly in ``folders`` always scans it, regardless of this flag.
    - ``since`` / ``until``: ignore messages dated before ``since`` or after
      ``until`` (inclusive bounds).
    """

    domains: list[str] = Field(default_factory=list)
    addresses: list[str] = Field(default_factory=list)
    mode: list[AddressField] = Field(default_factory=lambda: list(_ALL_FIELDS))
    subject: str | list[str] | None = None  # regex, or wildcard patterns (any)
    attachments: list[str] = Field(default_factory=list)  # required file types
    folders: list[str] | None = None  # IMAP folders; applied at fetch time
    recursive: bool = False  # also scan subfolders of each listed folder
    trash: bool = False  # include the Trash folder in a recursive scan
    since: date | None = None
    until: date | None = None


class AfterFetch(BaseModel):
    """Optional server-side actions after a message is safely stored.

    ``move_to`` and ``delete`` are mutually exclusive: a message is either moved
    into another folder or deleted from the server, never both. ``label`` may
    accompany either (though labelling then deleting is pointless). Every action
    runs only after the message is durably in the eml archive and marked delivered,
    so nothing can be lost on the server before it is safely archived locally.
    """

    label: str | None = None
    move_to: str | None = None
    delete: bool = False

    @model_validator(mode="after")
    def _exclusive(self) -> AfterFetch:
        if self.move_to and self.delete:
            raise ValueError(
                "after_fetch: 'move_to' and 'delete' are mutually exclusive"
            )
        return self


class Profile(BaseModel):
    """One conversation profile.

    ``remote_images`` and ``jobs`` are per-profile render settings; the CLI flags
    ``--allow-remote-images`` / ``--jobs`` override them for the whole run
    (otherwise the profile value applies).

    ``filename_pattern`` and ``date_format`` live here rather than in the render
    config because **both phases must agree on them**: fetch names the ``.eml``,
    render names the PDF folder, and the shared base name is what ties a raw mail
    to its rendition. Reading them from the same profile object is what keeps the
    two halves in step.
    """

    name: str
    account: str
    match: Match = Field(default_factory=Match)
    output: Path
    pdf: bool = False
    remote_images: bool = False  # load remote images when rendering this profile
    jobs: int = Field(default=4, ge=1)  # parallel renders; CLI --jobs overrides
    gs_jobs: int = Field(default=2, ge=1)  # parallel Ghostscript runs (memory)
    after_fetch: AfterFetch | None = None

    # Naming — used by both phases, see the class docstring.
    filename_pattern: str = naming.DEFAULT_PATTERN
    date_format: str = naming.DEFAULT_DATE_FORMAT

    # Per-attachment safety limits, applied while rendering.
    max_attachment_bytes: int = Field(default=400 * 1024 * 1024, ge=0)
    attachment_timeout_s: float = Field(default=120.0, gt=0)
    render_timeout_ms: int = Field(default=30_000, gt=0)


def load_profiles(
    yaml_path: Path, accounts: dict[str, Account] | None = None
) -> list[Profile]:
    """Parse profile.yaml into profiles.

    When ``accounts`` is given (the fetch path), each profile's ``account`` must
    reference a known one. When ``None`` (the render path, which needs no IMAP
    credentials), that check is skipped.

    Raises:
        ConfigError: On a missing file, invalid YAML, an invalid profile, or a
            reference to an unknown account.
    """
    if not yaml_path.exists():
        raise ConfigError(f"profile.yaml not found: {yaml_path}")
    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {yaml_path}: {exc}") from exc

    profiles: list[Profile] = []
    for raw in data.get("profiles", []):
        try:
            profile = Profile(**raw)
        except ValidationError as exc:
            raise ConfigError(f"invalid profile: {exc}") from exc
        if accounts is not None and profile.account.lower() not in accounts:
            raise ConfigError(
                f"profile '{profile.name}' references unknown account "
                f"'{profile.account}'"
            )
        profiles.append(
            profile.model_copy(update={"output": profile.output.expanduser()})
        )
    return profiles


def _addresses_in(headers: MailHeaders, mode: list[AddressField]) -> list[str]:
    """Every lowercased address from the header fields named in ``mode``."""
    by_field = {
        "from": headers.from_,
        "to": headers.to,
        "cc": headers.cc,
        "bcc": headers.bcc,
    }
    found: list[str] = []
    for field in mode:
        found.extend(a.lower() for a in extract_email_addresses(by_field[field]))
    return found


def matches(
    profile: Profile, headers: MailHeaders, *, received: datetime | None = None
) -> bool:
    """Whether a mail matches a profile's content rules.

    Domains/addresses are checked against the addresses in the ``mode`` header
    fields, the subject against the regex, and the date against ``since``/
    ``until``. The folder restriction is applied when fetching, not here.

    For the date bounds the mail's own ``Date`` header is preferred; when it is
    missing, ``received`` (the IMAP ``INTERNALDATE``, passed at fetch time) is
    used so ``since``/``until`` still apply to undated mail. When neither is
    known, no date bound can be enforced and the mail is not rejected on age.
    """
    rules = profile.match

    if rules.domains or rules.addresses:
        candidates = _addresses_in(headers, rules.mode)
        # Accept domains written as "@hetzner.com" (recommended) or "hetzner.com";
        # match the exact domain and its subdomains.
        wanted = [d.lower().lstrip("@") for d in rules.domains]
        domain_ok = any(
            addr.endswith(f"@{dom}") or addr.endswith(f".{dom}")
            for addr in candidates
            for dom in wanted
        )
        addresses = {a.lower() for a in rules.addresses}
        address_ok = any(addr in addresses for addr in candidates)
        if not (domain_ok or address_ok):
            return False

    if rules.subject is not None and not _subject_matches(
        rules.subject, headers.subject
    ):
        return False

    if rules.since or rules.until:
        effective = headers.date or received
        if effective is not None:
            day = effective.date()
            if rules.since and day < rules.since:
                return False
            if rules.until and day > rules.until:
                return False
    return True


def _subject_matches(rule: str | list[str], subject: str) -> bool:
    """A regex (str) is searched; a list of wildcard patterns matches if any do."""
    if isinstance(rule, str):
        return re.search(rule, subject or "") is not None
    low = (subject or "").lower()
    return any(fnmatch.fnmatch(low, pattern.lower()) for pattern in rule)


def attachments_match(required: list[str], attachment_names: list[str]) -> bool:
    """Whether the attachments satisfy a profile's ``attachments`` filter.

    True when no filter is set, or at least one attachment's extension is among
    the required ones (dot and case are ignored). Checked on the fetched body,
    since the envelope carries no attachment information.
    """
    if not required:
        return True
    wanted = {ext.lower().lstrip(".") for ext in required}
    return any(
        Path(name).suffix.lower().lstrip(".") in wanted for name in attachment_names
    )


def first_match(profiles: list[Profile], headers: MailHeaders) -> Profile | None:
    """Return the first profile whose content rules match, or None."""
    return next((p for p in profiles if matches(p, headers)), None)
