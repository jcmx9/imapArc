# imapArc

> Archives IMAP mailboxes into readable .eml files by profile, and optionally renders them to searchable PDF/A.

[Language: [Deutsch](README.md) | **English**] · Version `26.8.9`

[![CI](https://github.com/jcmx9/imapArc/actions/workflows/ci.yml/badge.svg)](https://github.com/jcmx9/imapArc/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

## Description

`imapArc` collects email from one or more IMAP accounts by profile and stores it as **`.eml` files with readable names** (identical to the later PDF). Rendering to PDF is a **decoupled, optional** second step.

Guiding principle: **imapArc preserves, it does not alter.** The full raw message sits losslessly as `.eml`; the PDF is a derived, readable rendition. Originals are never converted — they sit unchanged alongside.

### Key features

- **Two decoupled phases:** `fetch` (IMAP → `.eml`, cheap, idempotent) and `render` (`.eml` → PDF, optional, repeatable).
- **Readable `.eml` files** sharing the PDF's name; open any one in any mail client.
- **Real browser rendering** via Chromium — even complex newsletter HTML faithfully, up to three renditions per email (reflowed, plain text, plus a scaled one-page overview for multi-page mail).
- **No external resource loading** — tracking pixels are stripped before rendering. (Exception: `imaparc eml` loads them on purpose, see there.)
- **Protective, private archive** — files `0400` (read-only content), directories `0700` (private, yet owner-manageable), **never overwrite** (collision → `…-2`).

## Prerequisites

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/)
- Ghostscript, qpdf, veraPDF — only for `render` (e.g. `brew install ghostscript qpdf verapdf`)
- Git

## Installation

**The easy way (macOS):** one command installs everything — uv, Ghostscript, qpdf, veraPDF, imapArc, Chromium — and sets up the Finder right-click action:

```bash
curl -fsSL https://raw.githubusercontent.com/jcmx9/imapArc/main/install.sh | bash
```

Anything already present is skipped, nothing is installed twice. Options: `--no-service` (skip the right-click entry), `--no-init` (skip creating the config).

Afterwards the Finder offers **right-click → Services → "Mit imapArc archivieren"**, which works on selected `.eml` files *and* on whole folders — several at once. To add or refresh it later:

```bash
imaparc install-service
```

### Manual installation

From the local Git repository as a standalone uv tool:

```bash
git clone https://github.com/jcmx9/imapArc.git
cd imapArc
uv tool install .
```

Or straight from GitHub, without cloning:

```bash
uv tool install git+https://github.com/jcmx9/imapArc.git
```

`uv tool install` places the executable in `~/.local/bin`. If that is not yet on your `PATH`:

```bash
uv tool update-shell        # persistently adds ~/.local/bin to your shell
```

For the `render` phase, also install the browser once:

```bash
"$(uv tool dir)/imaparc/bin/python" -m playwright install chromium
```

Going through the interpreter is necessary because `uv tool install` only places imapArc's own executables in `~/.local/bin`, not those of its dependencies — so a bare `playwright` does not exist. The path also guarantees you get exactly the Chromium build the installed imapArc expects.

## First-time setup (bootstrap)

`imaparc init` creates the central config directory with templates (`.env` at
`0600`, directory `0700`); existing files are never overwritten:

```bash
imaparc init                          # creates ~/.config/imaparc/{.env,profile.yaml}
$EDITOR ~/.config/imaparc/.env        # enter your IMAP account
$EDITOR ~/.config/imaparc/profile.yaml   # adjust profiles / target directories
imaparc fetch                         # go
```

Full bootstrap from GitHub in one line:

```bash
uv tool install git+https://github.com/jcmx9/imapArc.git && imaparc init
```

## Update

```bash
uv tool install --force .                                    # from the local clone
uv tool install --force git+https://github.com/jcmx9/imapArc.git   # from GitHub
```

## Uninstall

```bash
uv tool uninstall imaparc
```

## Configuration

By default imapArc reads from a central directory (XDG-compliant):

| File | Purpose |
|------|---------|
| `~/.config/imaparc/.env` | IMAP accounts (credentials) |
| `~/.config/imaparc/profile.yaml` | profiles: match rules + target directories |
| `~/.local/state/imaparc/state.db` | already-delivered mail (dedup, automatic) |

**`.env`** — one or more accounts in the form `IMAP_<ACCOUNT>_<FIELD>`:

```dotenv
IMAP_PRIVAT_HOST=imap.example.com
IMAP_PRIVAT_USER=you@example.com
IMAP_PRIVAT_PASSWORD=your-password
IMAP_PRIVAT_SSL=true
```

`imaparc init` creates this file as a template (`0600`); enter your account there.

**`profile.yaml`** — each profile defines a conversation. Under `output`, `eml/` (and `pdf/` when `pdf: true`) are always created:

```yaml
profiles:
  - name: hetzner
    account: privat                # references IMAP_PRIVAT_* in .env
    match:                         # every rule set must hold (AND)
      domains: ['@hetzner.com']    # optional: domain(s), with leading @
      # addresses: [billing@hetzner.com]  # optional: exact address(es)
      # mode: [from, to, cc, bcc]  # optional: which fields to search (default all)
      subject: ['*Rechnung*']      # optional: a regex OR a wildcard-pattern list
      attachments: [pdf]           # optional: only mail with an attachment of this type
      folders: [INBOX]             # optional: IMAP folders (default INBOX)
      recursive: false             # optional: also scan subfolders (default false)
      trash: false                 # optional: include the Trash folder in a recursive scan (default false)
      since: 2026-01-01            # optional: ignore mail before this day
      # until: 2026-12-31          # optional: ignore mail after this day
    output: ~/Archiv/Hetzner       # target; eml/ and (if pdf) pdf/ go here
    pdf: true                      # optional: also render PDFs (default false)
    # remote_images: false       # load external images when rendering (CLI can force)
    # jobs: 4                    # parallel renders (CLI --jobs overrides)
    # gs_jobs: 2                 # parallel Ghostscript runs (this bounds memory)
    # filename_pattern: '{date}_{profile}_{subject}'   # naming scheme
    # date_format: YYYY-MM-DD_hh-mm-ss                 # tokens for {date}
    # max_attachment_bytes: 419430400   # do not convert larger attachments
    # attachment_timeout_s: 120         # per-attachment time limit
    # render_timeout_ms: 30000          # per-mail-body time limit
    after_fetch:                   # optional; only after safe archiving
      label: Archiviert            #   set an IMAP keyword
      move_to: Archiv/Erledigt     #   move (mutually exclusive with delete)
      # delete: true               #   delete (mutually exclusive with move_to)
```

Full reference with every parameter and default: [`profile.example.yaml`](profile.example.yaml).

## Usage

Everything is driven by `profile.yaml`. `imaparc` with no command shows the help:

```bash
imaparc all              # full run over ALL profiles: fetch, then render (pdf: true only)
imaparc fetch            # collect only (IMAP → .eml); pdf flag irrelevant
imaparc fetch --dry-run  # only show what would happen — writes nothing
imaparc render           # render only — ALL profiles (pdf flag is ignored)
imaparc init             # create the central config
imaparc add-profile NAME # append a new, fully documented profile to profile.yaml
imaparc list-profiles    # show the defined profiles as a table (rules, target, action)
imaparc sync-profiles    # rewrite an existing profile.yaml to the full format (backup)
imaparc reset            # clear delivery state → next fetch re-processes everything
imaparc eml              # render loose .eml files, with no profile at all
imaparc install-service  # add the Finder right-click action
imaparc doctor           # check the install: tools, browser, config, login
imaparc verify           # check the archive: duplicates, damage, leftovers

imaparc all --profile hetzner    # restrict any mode to one profile
imaparc fetch --profile hetzner
imaparc render --profile hetzner

imaparc --version
imaparc --help           # (also: imaparc with no argument)
```

`imaparc render` needs no `.env` (no IMAP access). Every `fetch` run re-evaluates all mail in the configured folders against the **current** profiles; already-delivered mail is skipped. Change a profile or add one and it takes effect on old mail automatically — no state reset needed. A second `render` run skips already-produced mail.

### Single mails without a profile: `imaparc eml`

Dragging a message out of Apple Mail, Thunderbird or Outlook into the Finder produces an `.eml` file. `imaparc eml` renders such files right where they lie — no profile, no `.env`, no server contact:

```bash
imaparc eml                        # every .eml in the current directory
imaparc eml ~/Desktop              # every .eml there
imaparc eml a.eml b.eml            # exactly these two
imaparc eml --name hetzner *.eml   # a custom name segment instead of "mail"
```

Each `.eml` turns into a folder beside it holding the complete mail — and the `.eml` itself is moved in afterwards:

```
2026-07-01_10-30-00_mail_Rechnung_Juli_2026/
├── 2026-07-01_10-30-00_mail_Rechnung_Juli_2026.pdf           # mail + attachment pages
├── 2026-07-01_10-30-00_mail_Rechnung_Juli_2026_mailonly.pdf  # the mail alone
├── 2026-07-01_10-30-00_mail_Rechnung_Juli_2026.eml           # the original
└── rechnung.pdf                                              # attachment, untouched
```

A directory argument takes only the `.eml` files **directly inside it**, not in subfolders — so mails already filed away are never collected again. A second run is therefore a no-op, and a run interrupted midway repairs itself the next time you call it.

This command is separate from the archive: it reads no `profile.yaml`, contacts no IMAP server and writes nothing to the delivery database. A later `imaparc fetch` behaves exactly as if it had never run.

> **Remote images are always loaded here** — unlike the profile-driven commands, where you have to opt in. A mail you pulled out by hand should look the way its sender laid it out, and it was already open in your mail client anyway. The price: rendering fetches tracking pixels too, so the sender learns when and from which IP the mail was processed. If you would rather avoid that, file the mail into a profile archive and use `imaparc render` instead.

## Options

| Command | Option | Description |
|---------|--------|-------------|
| global | `--version`, `-V` | Show version and exit |
| global | `--help` | Show help |
| `all` | *(all `fetch` and `render` options)* | Full run: fetch, then render |
| `init` | `--force` | Overwrite existing config files |
| `add-profile` | `NAME` | Name of the new profile (argument) |
| `add-profile` | `--profiles <path>` | `profile.yaml` (default `~/.config/imaparc/profile.yaml`) |
| `add-profile` | `--output <path>` | Target directory (default `~/imapArc/<name>`) |
| `list-profiles` | `--profiles <path>` | `profile.yaml` (default `~/.config/imaparc/profile.yaml`) |
| `sync-profiles` | `--profiles <path>` | `profile.yaml` (default `~/.config/imaparc/profile.yaml`) |
| `sync-profiles` | `--yes`, `-y` | Rewrite without confirmation |
| `reset` | `--state <path>` | SQLite state file |
| `reset` | `--yes`, `-y` | Clear delivery state without confirmation |
| `fetch` | `--env <path>` | `.env` with accounts (default `~/.config/imaparc/.env`) |
| `fetch` | `--profiles <path>` | `profile.yaml` (default `~/.config/imaparc/profile.yaml`) |
| `fetch` | `--profile <name>` | Run only this one profile |
| `fetch` | `--state <path>` | SQLite state file |
| `fetch` | `--dry-run` | Only show what would be archived and what would happen on the server; writes nothing |
| `fetch` | `--no-server-actions` | Archive normally, but never label, move or delete |
| `render` | `--profiles <path>` | `profile.yaml` (default `~/.config/imaparc/profile.yaml`) |
| `render` | `--profile <name>` | Render only this profile |
| `render` | `--allow-remote-images` | Force remote images (else per-profile `remote_images`) |
| `render` | `--jobs`, `-j` | Parallel renders |
| `eml` | `[PATHS...]` | Files or directories (default: current directory) |
| `eml` | `--name`, `-n` | Name segment in the generated file names (default `mail`) |
| `eml` | `--jobs`, `-j` | Parallel renders |
| `install-service` | `--name`, `-n` | Name segment the right-click entry uses |
| `doctor` | `--offline` | Skip the IMAP logins (no network access) |
| `verify` | `--profiles <path>` | `profile.yaml` naming the archives to check |
| `verify` | `--profile <name>` | Check only this one archive |
| `doctor` | `--env`, `--profiles` | Config files to inspect |
| `all` / `fetch` / `render` / `eml` | `--log-file <path>` | Also write the log here — **including at `-Q`** |
| `all` / `fetch` / `render` / `eml` | `-Q` / `-v` / `-vv` | Silent / Verbose / Debug |

## Development

```bash
git clone https://github.com/jcmx9/imapArc.git
cd imapArc
uv sync
uv run playwright install chromium

uv run pytest
uv run ruff check --fix . && uv run ruff format .
uv run mypy src/
```

## Versioning

[CalVer](https://calver.org/) in the format `YY.M.MICRO`, managed with [bump-my-version](https://github.com/callowayproject/bump-my-version).

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

MIT – see [LICENSE](LICENSE).
