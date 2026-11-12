# imapArc

> Archives IMAP mailboxes into readable .eml files by profile, and optionally renders them to searchable PDF/A.

[Language: [Deutsch](README.md) | **English**] · Version `26.11.10`

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
- **Flat archive:** one PDF per mail directly in `<profile>/`, the `.eml` in `eml-Archiv/<profile>/` — no subfolders. `imaparc eml` files mail from elsewhere the same way, deriving the profile from its headers.
- **Self-identifying PDFs:** every PDF records the Message-ID and SHA-256 of its `.eml` in its metadata and carries that `.eml` as an embedded source file (PDF/A-3), plus every attachment that cannot be shown as pages (Word, Excel, ZIP …). Bookmarks lead to the mail and to each separator page. Copied out or renamed, a PDF stays complete and traceable to its mail.
- **Protective, private archive** — files `0400` (read-only content), directories `0700` (private, yet owner-manageable), **never overwrite** (collision → `…-2`).

## Prerequisites

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/)
- Ghostscript, qpdf, veraPDF — only for `render` (e.g. `brew install ghostscript qpdf verapdf`)
- Git

## Installation

**The easy way (macOS and Linux):** one command installs everything — uv, Ghostscript, qpdf, veraPDF, imapArc, Chromium — and sets up the Finder right-click action:

```bash
curl -fsSL https://raw.githubusercontent.com/jcmx9/imapArc/main/install.sh | bash
```

Anything already present is skipped, nothing is installed twice. Options: `--no-service` (skip the right-click entry), `--no-init` (skip creating the config).

Afterwards your file manager offers an entry that works on selected `.eml` files *and* on whole folders — several at once: on macOS as **right-click → Services → "Mit imapArc archivieren"**, on Linux as **right-click → Open With** (Nautilus, Dolphin, Thunar). To add or refresh it later:

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

**`profile.yaml`** — each profile defines a conversation. The `.eml` go to `eml-Archiv/<output name>/` beside `output`, the PDFs (when `pdf: true`) directly into `output`:

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
      # larger: 5MB                # optional: only mail bigger than (5MB, 500KB, bytes)
      # smaller: 20MB              # optional: only mail smaller than
    output: ~/Archiv/Hetzner       # PDF target; .eml in ~/Archiv/eml-Archiv/Hetzner
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
imaparc eml              # file .eml from anywhere into the archive, profile from headers
imaparc install-service  # add the Finder right-click action
imaparc doctor           # check the install: tools, browser, config, login
imaparc verify           # check the archive: duplicates, damage, leftovers
imaparc restore <path>   # upload archived .eml back onto the IMAP server

imaparc all --profile hetzner    # restrict any mode to one profile
imaparc fetch --profile hetzner
imaparc render --profile hetzner

imaparc --version
imaparc --help           # (also: imaparc with no argument)
```

`imaparc render` needs no `.env` (no IMAP access). Every `fetch` run re-evaluates all mail in the configured folders against the **current** profiles; already-delivered mail is skipped. Change a profile or add one and it takes effect on old mail automatically — no state reset needed. A second `render` run skips already-produced mail.

Times — in file names and in the PDF's header block alike — are always the machine's **local time**, with the daylight-saving rule valid on the mail's date; the header block states the offset as well (`19.10.26, 11:32 (UTC+2)`). Only **one run renders at a time**: a second one (`render`, `all` or `eml`, say a right-click during a scheduled run) waits until the first has finished.

### Filing single mails: `imaparc eml`

Dragging a message out of Apple Mail, Thunderbird or Outlook into the Finder produces an `.eml` file. `imaparc eml` files such files — wherever they lie — into the archive exactly as `fetch` + `render` would. No `.env`, no server contact:

```bash
imaparc eml                        # every .eml in the current directory
imaparc eml ~/Desktop              # every .eml there
imaparc eml a.eml b.eml            # exactly these two
imaparc eml --name hetzner *.eml   # name segment for mail no profile matches
imaparc eml --root ~/Archive a.eml # a different archive root
```

The **profile is derived from the headers**: the rules in `profile.yaml` (sender and recipient addresses, domains, subject, date, attachment types) are applied to the mail; folder rules only apply to IMAP and are ignored here. A mail matching several profiles is filed into each; one matching none into `~/imapArc/00_Mails`. The PDF is written as `<profile>/<basename>.pdf`, and once it exists the `.eml` moves to `eml-Archiv/<profile>/`:

```
~/imapArc/
├── Hetzner/
│   └── 2026-07-01_10-30-00_Hetzner_Rechnung_Juli_2026.pdf   # mail + attachments, .eml embedded
└── eml-Archiv/Hetzner/
    └── 2026-07-01_10-30-00_Hetzner_Rechnung_Juli_2026.eml   # the original
```

A directory argument takes only the `.eml` files **directly inside** it, not in subfolders. A mail already archived is recognised by its PDF's metadata and not rendered twice; a second copy of the same bytes is merged with the archived one. An interrupted run leaves at most a surplus copy, never a missing one — the next call clears it up.

This command has nothing to do with IMAP: no server contact, no entry in the delivery database. Without a `profile.yaml` everything goes to `00_Mails`.

> **External images are always loaded here** — unlike `imaparc render`, where this must be switched on per profile. A mail picked out by hand should look the way its sender set it, and it was already opened in the mail client anyway. The price: rendering also fetches tracking pixels, so the sender learns the time and IP address of processing.

### Re-rendering an archive with a newer version

`imaparc render` never overwrites: a mail that is already rendered is skipped — recognised by its PDF's metadata (in older archives by the manifest in the mail folder), not by its name. To rebuild older PDFs with a newer version, first set the affected PDFs aside — but only those whose mail provably still exists as `.eml`. Two scripts in the repository do that (they need `uv` and `pdftotext`):

```bash
./scripts/rerender-list.sh ~ ~/rerender --exclude ~/imapArc-alt              # read-only
./scripts/rerender-apply.sh ~/rerender/rerender.txt --move-to ~/imapArc/pdf-Löscharchiv
bash ~/rerender/collect.sh       # every other .eml into ~/imapArc/emlinput
bash ~/rerender/render.sh        # after looking through emlinput: rebuilds everything
```

`rerender-list.sh` searches the given directory for PDFs — in flat profile archives (`<profile>/*.pdf`, the `.eml` in `eml-Archiv/<profile>/`), in mail folders of older versions (`<profile>/pdf/<mail>/`) and in folders written by older `imaparc eml` versions (with the `.eml` inside). Every PDF is matched to its `.eml` — by the hash of the raw bytes from its metadata or manifest or, for manifests older than 26.8.12, by the Message-ID **and** the PDF's own text (sender, date, subject, opening of the body). Never by name. `rerender.txt` holds what can safely be rebuilt; `keep.txt` the rest with a reason; `loose.txt` every other `.eml` below the directory (except in the configured profiles' inboxes). `eml-Archiv/`, `emlinput/` and `pdf-Löscharchiv/` are never searched. `rerender-apply.sh` re-checks the list before acting, asks for confirmation and moves (`--move-to`, reversible, under the path relative to `~`) or deletes (`--delete`). For folders of older `imaparc eml` versions it first takes the `.eml` out and places it beside the folder. Then `collect.sh` gathers every other `.eml` in `~/imapArc/emlinput` (same names become `-2`, byte-identical copies are merged; `emlinput.log` records where each came from) — so it can be looked through and weeded before rendering. `render.sh` then runs `imaparc render` for the profiles and `imaparc eml ~/imapArc/emlinput` for the rest: everything ends up in the archive under `~/imapArc`, the `.eml` in the `eml-Archiv`, and `emlinput` is empty afterwards. Both scripts can simply be started again after an interruption.

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
| `reset` | `--profile <name>` | Clear only this profile's state |
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
| `eml` | `--profiles <path>` | `profile.yaml` whose rules pick the profile (default `~/.config/imaparc/profile.yaml`) |
| `eml` | `--root <path>` | Archive root; mail no profile matches goes to `00_Mails` (default `~/imapArc`) |
| `eml` | `--name`, `-n` | Name segment for mail no profile matches (default `mail`) |
| `eml` | `--jobs`, `-j` | Parallel renders |
| `install-service` | `--name`, `-n` | Name segment for mail no profile matches, baked into the right-click entry |
| `doctor` | `--offline` | Skip the IMAP logins (no network access) |
| `verify` | `--profiles <path>` | `profile.yaml` naming the archives to check |
| `verify` | `--profile <name>` | Check only this one archive |
| `restore` | `[PATHS...]` | Archived `.eml` files or directories |
| `restore` | `--account <name>` | Account from the `.env` (required if several) |
| `restore` | `--folder <name>` | Target mailbox, created if missing (default `INBOX`) |
| `restore` | `--dry-run` | Only show what would be uploaded |
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
