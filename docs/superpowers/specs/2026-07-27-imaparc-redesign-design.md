# imapArc — Design Specification (Redesign)

> Renames and reshapes the former `mail2pdf` project. The rendering core built
> so far (email parsing, HTML inlining, headless-Chromium rendering) is kept
> almost verbatim; the output model, operating model and naming change.

## Context

`mail2pdf` was designed PDF-first: the goal was a PDF/A-3b document per email
with every attachment embedded via a document-level `/AF` array — the hardest,
riskiest part of the build (a dedicated spike verified it).

The redesign moves the durability guarantee off the PDF and onto a **Maildir**.
The full raw message always lands in a Maildir, losslessly, without Chromium
ever running. The PDF becomes what it should be: a derived, human-readable,
lossy rendition — acceptable precisely because the original sits next to it.

This also decouples two concerns that were tangled: **collecting** mail (cheap,
robust, idempotent, no browser) from **rendering** it (expensive, fragile). The
default run only fills the Maildir. Rendering is optional and may run later,
repeatedly, or never.

The tool is renamed **imapArc** — the old name described the old design, where
PDF was the goal. Now the core is IMAP archival into a Maildir; PDF is optional.

- Repository & directory: `mail2pdf` → `imapArc` (`~/GitHub/imapArc`).
- Python package & CLI command: `imaparc` (lowercase, `src/imaparc/`).
- Display/project name: `imapArc`.
- Author identity: `jcmx9`.

## Operating model — two decoupled phases

```
imaparc fetch     IMAP  → maildir/     default, cheap, no Chromium, idempotent
imaparc render    maildir/ → pdf/       optional, expensive, repeatable, catch-up
```

- `fetch` reads accounts from `.env`, applies the profiles from `profile.yaml`,
  and delivers each matching message into that profile's `maildir/`. It reads
  with `BODY.PEEK[]`, so the server-side `\Seen` state is untouched. Without an
  `after_fetch` block, `fetch` is fully non-invasive.
- `render` reads a Maildir and produces the PDF structure — only for profiles
  with `pdf: true`, or when invoked manually against a Maildir path. A Chromium
  crash while rendering never touches the collected mail.

`render` needs no IMAP and no network; it converts any existing Maildir. It is
therefore built and tested before `fetch`.

## Configuration

### Accounts — `.env`

One or more IMAP accounts. Credentials live here (git-ignored), never in the
YAML. Referenced by name from profiles.

### Profiles — `profile.yaml`

```yaml
profiles:
  - name: hetzner                 # identifies the "conversation"; used in filenames
    account: privat               # references an account from .env
    match:
      domains: [hetzner.com]
      addresses: [billing@hetzner.com]
      subject: '^Rechnung'        # optional regex
      folders: [INBOX]            # optional; else the account default
      since: 2026-01-01           # optional
    output: ~/Archiv/Hetzner       # conversation target (freely definable)
    pdf: true                     # optional, default false
    after_fetch:                  # optional, combinable, opt-in
      label: Archiviert           # set an IMAP keyword on the source message
      move_to: Archiv/Erledigt    # move the source message into this folder
```

Validated with pydantic; configuration errors are reported at startup, not
mid-run. **A conversation equals a profile** — one profile collects all its
matching mail into one Maildir, purely chronologically. There is no
per-thread grouping (no References/In-Reply-To union-find).

## Storage layout

Everything below a profile's `output` is fixed and **not** parametrisable:

```
<output>/
├── maildir/                                       ← ALWAYS
│   ├── cur/  new/  tmp/                            real Maildir, Thunderbird-usable
│   └── cur/1742694… .M…P…Q….imaparc:2,S            standard Maildir name (neutral host)
└── pdf/                                            ← only when pdf: true
    ├── 2026-03-23_02-18-04_hetzner_Rechnung-086….pdf   ← COMBINED PDF (readable name)
    └── 2026-03-23_02-18-04_hetzner_Rechnung-086…/      ← subfolder, same basename
          ├── <basename>.pdf                             mail-only (PDF/A-3b guaranteed)
          └── <original attachments>
```

The **PDF** carries the human-readable basename (combined `.pdf`, subfolder and
mail-only PDF all share it — traceable end to end). The **Maildir** file uses a
standard, spec-conformant unique name instead, trading name parity for maximum
Maildir-reader compatibility (see *Maildir delivery* below). Both derive their
chronology from the message `Date` header, so Thunderbird and a PDF file listing
still sort identically.

### Naming

`basename = YYYY-MM-DD_hh-mm-ss_PROFILENAME_SUBJECT` — the **PDF** basename. The
Maildir file does not use it (it gets a standard unique name); the shared anchor
between the two is the timestamp, which both derive from the same `Date` header.

- Timestamp derives from the message `Date` header; on a missing/invalid header,
  the IMAP `INTERNALDATE` is the fallback. The same timestamp seeds the Maildir
  name's time part and the PDF basename, so chronology in Thunderbird (reads the
  header) and in a PDF file listing (reads the name) coincide.
- The middle segment is the **profile name**, not the contact — the profile
  identifies the conversation. In `naming.py` the `{contact}` placeholder
  becomes `{profile}`; the sender/recipient inversion logic and `own_addresses`
  fall away for the default name.
- Subject is sanitised (illegal filesystem characters → `_`, including the `:`
  that would otherwise collide with the Maildir info separator) and the whole
  basename is capped to a filesystem-safe byte length.
- Collision within a profile (same second, same subject) appends a short
  discriminator (`…_SUBJECT-2`) so neither Maildir file nor PDF is overwritten.

## Maildir delivery

Real Maildir (`cur/new/tmp`), usable as a local folder in Thunderbird — the
decisive criterion for choosing this format over a flat `.eml` folder.

- Fetched messages go **only into `cur/`**, never `new/`, with the `:2,S` (Seen)
  flag — archive semantics, so Thunderbird does not present them as new/unread.
- Delivery is atomic: write into `tmp/`, `fsync`, then `os.rename` into `cur/`.
- Filenames are the **standard Maildir unique name**
  `<time>.M<usec>P<pid>Q<counter>.<host>` plus the `:2,S` flag, with a fixed
  neutral host part `imaparc` instead of the machine hostname (no hostname leak,
  identical across machines, still unique via the time/pid/counter part).
  Delivery is written directly rather than via `mailbox.Maildir.add()` only to
  pin the neutral host and the `:2,S` flag — the name *format* is the library's.
  Readability by Python's `mailbox.Maildir` is verified by spike.
- **Why standard names, not the PDF basename:** the earlier design gave the
  Maildir file the readable basename too, for parity. That was dropped in favour
  of maximum reader compatibility: only the whole name must be unique per the
  spec, but readers vary in how they tolerate unusual names, and the archive's
  durability guarantee rests on the Maildir. Readability lives in the PDF; the
  Maildir optimises purely for being read back correctly.
- Directories (`maildir/`, `cur/new/tmp/`) rest at `0500`, files at `0400`,
  briefly unlocked only for an atomic delivery.

## PDF generation

The **rendered mail part** is the shared core of both PDFs — produced once,
placed into both:

```
Mail part:
  · Rendition 1: original-fidelity, scaled      (Chromium)
  · Separator
  · Rendition 2: reflowed-readable              (Chromium)
  · Separator
  · Plain-text version
```

**Mail-only PDF** — `pdf/<basename>/<basename>.pdf` — is *only* this core, so it
is **guaranteed PDF/A-3b conformant** (no foreign PDFs merged in). Named with
the basename, not `mail.pdf`, because an attachment could itself be `mail.pdf`.

**Combined PDF** — `pdf/<basename>.pdf` — is the same core plus, per attachment,
a separator and:

| Attachment type | Handling | PDF/A-3b |
|---|---|---|
| txt, md | typeset to pages | stays conformant |
| images (jpg/png/heic/…) | to pages via img2pdf | stays conformant |
| PDF | appended 1:1, taken as-is | **may break** |
| docx, zip, anything else | interstitial page "not included in PDF" | stays conformant |

There is **no `/AF` embedding**. Attachments become either *pages* or *original
files* in the subfolder. The original `.eml` is **not** copied into the
subfolder — the full raw message already lives in the Maildir.

### Office/OpenDocument formats are never converted

A deliberate archival stance: imapArc **preserves, it does not alter**. Opening
an open-document format and re-rendering it *is* an alteration. Two independent
reasons:

1. **Layout engine**: only LibreOffice renders Office formats with fidelity;
   pandoc re-renders the content (no original layout). Both are a falsification.
2. **Fonts**: docx/odt almost never embed fonts — they only reference names
   (Calibri, Cambria). When the proprietary font is absent on Linux/macOS,
   LibreOffice substitutes metric-compatible replacements
   (Carlito/Caladea/Liberation) — line and page breaks shift. The formats
   imapArc *does* convert (txt/md via its own templates, images as pixels, PDF
   appended 1:1 with fonts already embedded) are all font-safe.

The original sits unchanged in the subfolder; the user opens it with their real
office suite. Security supports the same decision: converting untrusted office
attachments is a real attack vector (OLE `xlink` SSRF/LFI, EPS/Ghostscript) —
even with macros disabled.

### PDF/A policy

PDF/A-3b is targeted and guaranteed for the mail-only PDF and for combined PDFs
whose attachments are all convertible. It is **knowingly sacrificed** where a
foreign PDF is appended 1:1 — the file is taken as-is. Conformance is validated
with veraPDF and reported, never silently assumed. The `_unvollständig` suffix
from the old design is gone; a non-included attachment shows as an interstitial
page instead.

The PDF/A spike was not wasted: only its embedding half is dropped. The
Ghostscript findings (`--permit-file-read` before `-dSAFER`, generating
`PDFA_def.ps` at runtime) remain valid for attachment conversion and the
mail-only PDF.

## Post-fetch actions

The fixed double-PDF structure is not parametrisable, but **what happens to the
source message on the server after collection is** — per profile, via
`after_fetch` (label and/or move, combinable, opt-in). Three hard rules:

- **Only after a successful atomic Maildir write.** If the local write fails, the
  message is never moved or labelled — it stays untouched on the server. This is
  the "no data loss" guarantee in the IMAP world.
- **Order label → move.** A move mints a new UID; labelling first avoids
  pointing at a dead UID.
- **`fetch` reads with `BODY.PEEK[]`**, so `\Seen` is untouched; only an explicit
  `label`/`move_to` changes anything server-side.

## File integrity (permissions)

Hardening that matches the "preserve, don't alter" stance:

- **All written files `0400`** (`r--------`): owner-read only, no `w`, no `x`.
  Immutable, non-executable (guards against accidentally running a malicious
  attachment), and it reinforces the never-overwrite invariant.
- **Directories `0500`** (`r-x------`): owner-only, no `w` at rest. This also
  prevents *deletion/renaming* of archive entries (governed by the directory's
  `w`, not the file's) — so the archive is fully immutable at rest.
- **Temporary unlock while writing**: delivery needs `w` on `tmp/`/`cur/`. The
  owner may `chmod` at any time (ownership suffices, `w` not required). The tool
  sets the target directory to `0700` for the duration of its own write, then
  back to `0500` — under the lock file that already prevents overlapping runs,
  so no race.
- Consequence: Thunderbird uses the Maildir read-only (confirmed). A deliberate
  re-render needs an explicit `--force` (chmod, replace).

## Invariants

- **I1 — never overwrite.** If a target name exists, disambiguate
  (`foto.jpg` → `foto-2.jpg` → `foto-3.jpg`); never replace. Applies to every
  written file: attachments, PDFs, Maildir entries. Enforced additionally at the
  filesystem level by `0400`/`0500`.
- **I2 — deterministic disambiguation within a mail.** Names follow the MIME-tree
  attachment order, so the same input always yields the same names.
- **I3 — the mail output is an idempotent unit.** The combined PDF plus its
  subfolder are produced as one unit; if it exists, the mail is skipped — so a
  second `render` never produces `foto-3.jpg`, `foto-4.jpg`. Writes are atomic
  (build in `tmp/`, then `os.rename`) so an aborted run leaves no half-state.
- **I4 — collection is non-invasive by default.** Without `after_fetch`, the
  server is only read, never modified.

## Impact on existing code

The expensive, already-verified rendering core (#1–#5, committed on `dev`)
survives almost unchanged.

| Kept | Changes | Removed |
|---|---|---|
| `eml/parser.py`, `eml/models.py` | `naming.py`: `{profile}` for `{contact}`, date format `YYYY-MM-DD_hh-mm-ss`, pattern `{date}_{profile}_{subject}` | `/AF` embedding (the pikepdf spike half) |
| `html/inline.py` (+ allow-remote) | `sources/`: `MaildirSource` instead of loose `.eml`; add `ImapSource` | `_unvollständig` suffix → interstitial page |
| `render/*` (browser, geometry, pdf) whole | `pdf/`: merge + gs PDF/A, **without** embed | — |
| the three body renditions | | |

**New:** package/CLI/repo rename to `imaparc`/`imapArc`; Maildir delivery (atomic,
standard unique names with a neutral host, `cur/` only); `sources/imap.py`; `config/profiles.py` (`.env` +
`profile.yaml` via pydantic); attachment→PDF conversion with interstitial pages;
a state store (SQLite) for IMAP idempotency; the `fetch`/`render` commands and
post-fetch actions.

## Build order

1. **Rename** repo, directory, package and CLI to `imapArc`/`imaparc`
   (first implementation step; verify tests still pass).
2. **`render` path** (Maildir → PDF), built and tested without IMAP:
   - adjust `naming.py` (`{profile}`, date format);
   - `sources/maildir.py` reader;
   - attachment classification + conversion (txt/md/image→pages, PDF 1:1,
     interstitial page for the rest);
   - `pdf/` merge + Ghostscript PDF/A (no embed) + veraPDF validation;
   - orchestration producing the fixed double structure with invariants I1–I3;
   - `imaparc render` command.
3. **`fetch` path** (IMAP → Maildir):
   - `config/profiles.py` (`.env` + `profile.yaml`);
   - Maildir delivery writer (atomic, `cur/`-only, standard unique names, neutral host);
   - `sources/imap.py` with the SQLite state store;
   - profile matching;
   - post-fetch actions (label → move), with the no-data-loss ordering;
   - `imaparc fetch` command.

## Spikes to run before relying on assumptions

1. **Maildir/Thunderbird compatibility** — deliver messages with standard unique
   names (neutral host) into `cur/` with `:2,S`, then bind the Maildir as a local
   folder in Thunderbird and confirm it lists them correctly. Blocks the delivery
   design if it fails. *(Done: the neutral-host standard-name decision came out of
   this spike, replacing the initial readable-name idea.)*
2. (Already done) PDF/A-3b via Ghostscript — the conversion half remains valid.

## Verification

- Unit tests carry the mass without a browser: naming, profile matching and YAML
  validation, attachment classification, Maildir filename building, the
  overwrite-avoidance/disambiguation logic, gs argv builders.
- Structural PDF assertions via pikepdf (page count/order, sentinels in extracted
  text, XMP `pdfaid:part=3` for the mail-only PDF), not byte golden files.
- `render` idempotency: run twice, second run is a no-op with unchanged mtimes,
  no `-2`/`-3` name inflation.
- IMAP tests against a local test server (e.g. a container), not a real mailbox;
  the post-fetch path (label/move) verified including the UID change after move.
- Manual end-to-end on the real Hetzner sample `.eml` already in the tree.
