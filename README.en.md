# imapArc

> Archives e-mail as the original (`.eml`) and as one searchable PDF/A per mail — from IMAP mailboxes or from single files.

[Language: [Deutsch](README.md) | **English**] · Version `26.11.12`

[![CI](https://github.com/jcmx9/imapArc/actions/workflows/ci.yml/badge.svg)](https://github.com/jcmx9/imapArc/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

## What imapArc does

imapArc collects e-mail from IMAP accounts by profile (or takes `.eml` files from disk) and stores every mail twice:

- as the **original** — the unchanged `.eml`, which opens in any mail client;
- as **PDF/A-3** — one file per mail, rendered in a browser, searchable, with the attachments as pages.

Guiding principle: **imapArc preserves, it does not alter.** The original is kept byte for byte; the PDF is its readable rendition. Nothing is ever overwritten.

### The archive

```
~/imapArc/
├── Hetzner/                                   # one directory per profile: PDFs only
│   ├── 2026-07-01_10-30-00_Hetzner_Rechnung_Juli_2026.pdf
│   └── 2026-08-01_10-30-12_Hetzner_Rechnung_August_2026.pdf
├── 00_Mails/                                  # mail that matches no profile
├── eml-Archiv/                                # the originals, per profile
│   └── Hetzner/
│       ├── 2026-07-01_10-30-00_Hetzner_Rechnung_Juli_2026.eml
│       └── 2026-08-01_10-30-12_Hetzner_Rechnung_August_2026.eml
└── emlinput/                                  # optional: collecting point for loose .eml
```

Names follow the scheme `DATE_TIME_PROFILE_SUBJECT`, configurable per profile. Times are local time.

### What every PDF holds

| Content | What for |
|---------|----------|
| The mail with a header block (subject, from, date, to/cc) | reads as in a mail client; elaborate mails also get a one-page overview, and every mail a plain-text rendition |
| The attachments as pages (PDF, images, text — attached mails too, with their attachments) | everything in one file, searchable |
| **Bookmarks** to the mail, each rendition and each attachment | quick navigation |
| **The original `.eml`, embedded** | the PDF carries its own original — `imaparc extract` takes it out |
| **Attachments that cannot become pages** (Word, Excel, ZIP …), embedded | open straight from the PDF reader's attachment panel; an info page points to them |
| **Fingerprint** (SHA-256 of the `.eml`) and Message-ID in the metadata | the PDF identifies itself — renamed or copied out, too |
| Title and author = subject and sender | shown in Finder, Spotlight, PDF readers |

## Prerequisites

- Python ≥ 3.12 and < 3.14
- [uv](https://docs.astral.sh/uv/)
- Ghostscript, qpdf, veraPDF and Chromium — for rendering only (e.g. `brew install ghostscript qpdf verapdf`)
- Git

## Installation

**The easy way (macOS and Linux):** one command installs everything — uv, Ghostscript, qpdf, veraPDF, imapArc, Chromium — and sets up the right-click entry:

```bash
curl -fsSL https://raw.githubusercontent.com/jcmx9/imapArc/main/install.sh | bash
```

What is already there is skipped. Options: `--no-service` (no right-click entry), `--no-init` (do not create a configuration).

### Manual installation

```bash
uv tool install git+https://github.com/jcmx9/imapArc.git        # from GitHub
# or from a local clone:  git clone … && cd imapArc && uv tool install .

uv tool update-shell                                             # if ~/.local/bin is not on PATH
"$(uv tool dir)/imaparc/bin/python" -m playwright install chromium   # browser for rendering
```

Going through the interpreter makes sure exactly the Chromium build the installed imapArc expects is fetched. `imaparc doctor` then checks that everything is in place.

### Update and uninstall

```bash
uv tool install --force git+https://github.com/jcmx9/imapArc.git   # update
uv tool uninstall imaparc                                           # uninstall
```

## Setup

```bash
imaparc init                             # creates ~/.config/imaparc/{.env,profile.yaml}
$EDITOR ~/.config/imaparc/.env           # enter the IMAP account
$EDITOR ~/.config/imaparc/profile.yaml   # adjust the profiles
imaparc doctor                           # check installation, configuration and login
```

| File | Purpose |
|------|---------|
| `~/.config/imaparc/.env` | IMAP accounts (credentials, `0600`) |
| `~/.config/imaparc/profile.yaml` | profiles: rules and target directories |
| `~/.local/state/imaparc/state.db` | which mail has been fetched already (automatic) |

**`.env`** — one or more accounts as `IMAP_<ACCOUNT>_<FIELD>`:

```dotenv
IMAP_PRIVAT_HOST=imap.example.com
IMAP_PRIVAT_USER=you@example.com
IMAP_PRIVAT_PASSWORD=your-password
IMAP_PRIVAT_SSL=true
```

**`profile.yaml`** — each profile says which mail belongs together and where it goes:

```yaml
profiles:
  - name: Hetzner
    account: privat                # refers to IMAP_PRIVAT_* in .env
    match:                         # all rules that are set must match (AND)
      domains: ['@hetzner.com']    # domain(s), with a leading @
      # addresses: [billing@hetzner.com]  # exact address(es)
      # mode: [from, to, cc, bcc]  # which fields are searched (default all)
      subject: ['*Rechnung*']      # a regex OR a list of wildcards
      attachments: [pdf]           # only mail with an attachment of this type
      folders: [INBOX]             # IMAP folders (default INBOX)
      recursive: false             # subfolders too
      since: 2026-01-01            # ignore mail before this day
      # until: 2026-12-31          # ignore mail after this day
      # larger: 5MB                # only larger / smaller: only smaller mail
    output: ~/imapArc/Hetzner      # PDF target; .eml in ~/imapArc/eml-Archiv/Hetzner
    pdf: true                      # also render PDFs (default false)
    # remote_images: false         # load remote images when rendering
    # filename_pattern: '{date}_{profile}_{subject}'
    # date_format: YYYY-MM-DD_hh-mm-ss
    after_fetch:                   # optional; only after the mail is safely stored
      label: imapArc               #   set an IMAP keyword
      move_to: imapArc             #   move (exclusive with delete)
      # delete: true               #   delete (exclusive with move_to)
```

All parameters with their defaults: [`profile.example.yaml`](profile.example.yaml). `imaparc add-profile NAME` appends a fully commented profile, `imaparc list-profiles` shows the profiles as a table.

## Usage

### Archiving mailboxes

```bash
imaparc all              # fetch, then render (only profiles with pdf: true)
imaparc fetch            # fetch only (IMAP → .eml)
imaparc fetch --dry-run  # only show what would happen — writes nothing
imaparc render           # render only (every profile, whatever its pdf flag)
imaparc all --profile Hetzner   # restrict any command to one profile
```

On every run `fetch` evaluates all mail in the folders against the **current** profiles and skips what was fetched before — so a changed or new profile picks up old mail automatically. Server actions (`after_fetch`) only run once the mail is safely stored locally. `render` skips every mail that already has its PDF, and needs no `.env`.

### Filing single mails: `imaparc eml`

Dragging a message out of Apple Mail, Thunderbird or Outlook into the Finder produces an `.eml` file. `imaparc eml` files such files — wherever they lie — into the archive just as `fetch` + `render` would:

```bash
imaparc eml a.eml b.eml            # exactly these two
imaparc eml ~/Desktop              # every .eml there (not in subfolders)
imaparc eml --no-remote-images ~/imapArc/emlinput   # many old mails, loading nothing
```

The **profile is derived from the headers**: the rules in `profile.yaml` (addresses, domains, subject, date, attachment types) are applied to the mail; folder rules only apply to IMAP. A mail matching several profiles is filed into each; one matching none into `~/imapArc/00_Mails`. Once the PDF exists the `.eml` moves to the `eml-Archiv` — the source file only disappears when every copy has been written.

A **right-click** does the same in the file manager, on selected `.eml` files and whole folders: on macOS **Services → "Mit imapArc archivieren"**, on Linux **Open With** (Nautilus, Dolphin, Thunar). Set it up or renew it with `imaparc install-service`.

> **Remote images** are loaded by default with `imaparc eml` (with `render` only where the profile allows it): a mail picked out by hand should look the way it was set. That fetches tracking pixels too. `--no-remote-images` turns it off.

### Working with the PDFs

```bash
imaparc mailonly a.pdf          # a_mailonly.pdf: the mail alone, without attachment pages
imaparc extract a.pdf           # take the original .eml out of the PDF
imaparc extract --all a.pdf     # and the embedded attachments
```

- **`mailonly`** copies the pages before the first attachment into a new, slim PDF — nothing is rendered again. The embedded `.eml` and attachments are left out (`--keep-embedded` keeps them): meant for passing on.
- **`extract`** writes the embedded mail as a file and says whether it matches the PDF's fingerprint. A single PDF is thus enough to restore the mail.

Both write to the current directory (or `--output`), never overwrite, and do not touch the archive.

### Checking the archive

```bash
imaparc verify           # mail without a PDF, duplicate PDFs, leftovers of aborted runs
imaparc verify --deep    # also read every file: is the embedded mail intact?
imaparc doctor           # the installation: tools, browser, configuration, login
```

`verify` only reads — it reports, it never repairs. Matching is by fingerprint, never by name. The exit code is 1 only for real damage, so a scheduled run does not go off over harmless duplicates. `--deep` also reports PDFs whose `.eml` is no longer in the archive (it is still inside the PDF then) and byte-identical `.eml` copies.

### Back onto the server

```bash
imaparc restore ~/imapArc/eml-Archiv/Hetzner --folder Archiv --dry-run
```

`restore` uploads archived `.eml` back onto an IMAP server — after changing provider, say. Repeatable: what is already there is recognised and not added twice.

## Good to know

- **Never overwrite.** A name collision becomes `…-2`; byte-identical mails are merged instead of copied. Files are read-only (`0400`), directories private (`0700`).
- **Times are local time** of the machine, with the daylight-saving rule valid on the mail's date — in the file name and in the header block, which states the offset as well (`19.10.26, 11:32 (UTC+2)`).
- **One run at a time.** A second one (`render`, `all`, `eml` — a right-click during a scheduled run, say) waits.
- **Names are no identity.** An `.eml` and its PDF usually share a name, but need not (collisions, renamed profiles). What counts is the fingerprint in the PDF.
- **PDF/A-3b** is checked with veraPDF and reported in the run. A PDF that is not conformant because of an exotic attachment is kept all the same.
- **Nothing is loaded** by `render` unless the profile allows it: remote resources and tracking pixels are stripped before rendering.
- **Long runs:** `-v` shows every mail, `-vv` details, `-Q` is silent; `--log-file` records in any case. A mail that fails is named in the summary, and the run goes on.

## Re-rendering an archive with a newer version

`render` skips every mail that already has a PDF. To rebuild older PDFs with a newer version, set them aside first — but only those whose `.eml` provably still exists. There are three steps for that (they need `uv` and `pdftotext`):

```bash
./scripts/rerender-list.sh ~ ~/rerender                 # 1. read-only: what can be rebuilt?
./scripts/rerender-apply.sh ~/rerender/rerender.txt --move-to ~/imapArc/pdf-Löscharchiv
bash ~/rerender/collect.sh                              # 2. loose .eml into ~/imapArc/emlinput
bash ~/rerender/render.sh                               # 3. render everything again
```

- **`rerender-list.sh`** finds every imapArc PDF below the given directory and checks by fingerprint whether its `.eml` exists (for very old folders without a fingerprint: by Message-ID **and** the PDF's own text). `rerender.txt` holds what can safely be rebuilt, `keep.txt` the rest with a reason, `loose.txt` every `.eml` outside the archive.
- **`rerender-apply.sh`** re-checks the list, asks for confirmation and moves (`--move-to`, reversible) or deletes (`--delete`).
- **`collect.sh`** gathers loose `.eml` in `~/imapArc/emlinput`; `emlinput.log` records where each came from. That is the place to weed before rendering.
- **`render.sh`** runs `imaparc render` for the profiles and `imaparc eml` for the rest.

`eml-Archiv/`, `emlinput/` and `pdf-Löscharchiv/` are never searched. Every step can be started again after an interruption.

## Commands and options

| Command | Option | Description |
|---------|--------|-------------|
| global | `--version`, `-V` / `--help` | Show the version or the help (`imaparc` without a command shows the help) |
| `all` | *(all `fetch` and `render` options)* | Fetch, then render |
| `fetch` | `--profile <name>` | Only this profile |
| `fetch` | `--dry-run` | Only show what would be archived and what would happen on the server |
| `fetch` | `--no-server-actions` | Archive, but never label, move or delete |
| `fetch` | `--env`, `--profiles`, `--state <path>` | Other configuration and state files |
| `render` | `--profile <name>` | Only this profile |
| `render` | `--allow-remote-images` | Force remote images on (otherwise per profile `remote_images`) |
| `render` | `--jobs`, `-j` | Parallel renders |
| `eml` | `[PATHS...]` | Files or directories (default: current directory) |
| `eml` | `--profiles <path>` | `profile.yaml` whose rules pick the profile |
| `eml` | `--root <path>` | Archive root for `00_Mails` (default `~/imapArc`) |
| `eml` | `--name`, `-n` | Name segment for mail no profile matches (default `mail`) |
| `eml` | `--no-remote-images` | Do not load remote images |
| `eml` | `--jobs`, `-j` | Parallel renders |
| `mailonly` | `PDFS...` | PDFs to make `<name>_mailonly.pdf` from |
| `mailonly` | `--output`, `-o` | Target directory (default: current directory) |
| `mailonly` | `--keep-embedded` | Keep the embedded `.eml` and attachments |
| `extract` | `PDFS...` | PDFs to take the embedded `.eml` out of |
| `extract` | `--output`, `-o` | Target directory (default: current directory) |
| `extract` | `--all` | Also extract the embedded attachments |
| `verify` | `--profile <name>` | Check only this archive |
| `verify` | `--deep` | Also open every PDF and compare every `.eml` |
| `verify` | `--verbose`, `-v` | Each finding on its own instead of summarised |
| `doctor` | `--offline` | Skip the IMAP logins |
| `restore` | `[PATHS...]` | Archived `.eml` files or directories |
| `restore` | `--account <name>` | Account from `.env` (needed if there are several) |
| `restore` | `--folder <name>` | Target folder, created if missing (default `INBOX`) |
| `restore` | `--dry-run` | Only show what would be uploaded |
| `init` | `--force` | Overwrite existing configuration files |
| `add-profile` | `NAME`, `--output <path>` | Append a new profile (default target `~/imapArc/<name>`) |
| `list-profiles` | | Profiles as a table |
| `sync-profiles` | `--yes`, `-y` | Rewrite `profile.yaml` in full format (with a backup) |
| `reset` | `--profile <name>`, `--yes` | Clear the fetch state — the next `fetch` evaluates everything again |
| `install-service` | `--name`, `-n` | Set up the right-click entry |
| `all` / `fetch` / `render` / `eml` | `--log-file <path>` | Also log to this file — **even with `-Q`** |
| `all` / `fetch` / `render` / `eml` | `-Q` / `-v` / `-vv` | Silent / verbose / debug |

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

The IMAP tests need a local GreenMail server and skip themselves otherwise; details and the architecture are in [`CLAUDE.md`](CLAUDE.md).

## Versioning

[CalVer](https://calver.org/) in the format `YY.M.MICRO`, managed with [bump-my-version](https://github.com/callowayproject/bump-my-version).

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

MIT – see [LICENSE](LICENSE).
