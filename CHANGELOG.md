# Changelog

All notable changes to this project will be documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [26.8.11] - 2026-08-03

### Added
- **`imaparc restore`** uploads archived `.eml` files back onto an IMAP server —
  for mail that is gone from the server and survives only in the archive, after
  an `after_fetch: delete` profile or when moving provider. `--folder` picks the
  target mailbox (created if missing), `--dry-run` shows what would happen.

  **Idempotent**: each mail is looked up by `Message-ID` before uploading, so a
  second run adds nothing. The reason to restore is usually that you are *unsure*
  whether the mail is there, which makes "safe to repeat" the property that
  matters. It is also the only place in imapArc that writes new mail to a server,
  and it never modifies anything already present — no flags, no moves, no
  deletions.

## [26.8.10] - 2026-08-02

### Changed
- **A byte-identical redelivery no longer writes a second `.eml`.**
  `deliver_eml()` reuses the existing file instead of adding `…-2`.
  Never-overwrite exists to prevent *loss*; identical bytes lose nothing, so the
  copy was pure redundancy. One mail legitimately arrives twice — Gmail lists a
  message in All Mail *and* in its label folder, and a mail uploaded back to the
  server returns under a fresh UID. Content differing by even one byte still
  disambiguates exactly as before. Size is checked before content, so a large
  message is not read off disk without reason.

## [26.8.9] - 2026-08-02

### Added
- **`fetch` shows progress.** It previously ran blind: on a large mailbox over a
  slow link there was no way to tell it apart from a hang. The bar starts
  open-ended, because the number of candidates is only knowable folder by folder,
  and grows as each folder is scanned.
- **One line per mail at `-v`, details at `-vv`**, in all three phases. `-v` names
  the mail as it goes by; `-vv` adds what you need to locate it (folder, UID,
  size on the fetch side; base name and attachment count on the render side).

### Fixed
- **`-v` used to change nothing on screen.** Console levels 1 and 2 were both
  `INFO`, so the documented step from "Progress + Summary" to "+ Entscheidungen"
  did not exist. Level 1 is now `WARNING`: the default stays bars-and-summary and
  the per-mail lines begin at `-v`, as documented all along.

## [26.8.8] - 2026-08-02

### Added
- **`imaparc verify`** checks existing archives: two folders holding the same
  mail, `.eml` files that were never rendered, folders missing their PDF or their
  manifest, and `.staging-*` left by an interrupted run. Read-only — it reports,
  it never repairs, because which of two duplicates to keep is a judgement call
  and an archive built to stay untouched should not be rewritten behind your back.

  Exits 1 **only** on actual damage. Duplicates and leftovers are reported but
  lose nothing, so they must not fail a scheduled check — otherwise the failure
  signal stops meaning anything.

  The duplicate check matters right now: the reservation bug fixed in 26.8.7 left
  duplicate folders behind in archives written before it, and the fix does not
  reach backwards. Anyone who ran imapArc against Gmail is likely to have some.

## [26.8.7] - 2026-08-02

### Fixed
- **The same mail delivered twice produced two PDF folders.** `_resolve_output()`
  checked its in-flight reservations before the manifest, and those reservations
  held names only — so a second copy of one mail saw "name taken" and quietly
  took `<basename>-2` without ever comparing identities. Every later run then
  skipped both folders, leaving the duplicate in place for good.

  Two UIDs for one message are ordinary, not user error: Gmail lists a message in
  All Mail *and* in its label folder, so a recursive scan delivers it twice; a
  mail uploaded back to a server returns with a fresh UID. The reservation map now
  carries the mail identity alongside the name, so a different mail still
  disambiguates while a second copy of the same one skips.

### Added
- **`--log-file <path>`** on `all`, `fetch`, `render` and `eml`. The logging setup
  has always supported a file that receives output *even at `-Q`* — the
  documentation names it as the reason silent mode is usable from cron — but no
  command ever passed a path, so `-Q` simply produced no record at all. Same shape
  as the `gs_jobs` gap: implemented, documented, unreachable.

## [26.8.6] - 2026-08-02

### Added
- **`imaparc doctor`** checks an installation in one go: gs/qpdf/verapdf on PATH,
  a Chromium build matching the installed Playwright, the sRGB ICC profile,
  `profile.yaml`, `.env` completeness, the Finder action, and an actual IMAP login
  per account (`--offline` skips those). Exits 1 on a failure so it works from
  cron; a missing config is a warning, not a failure — a fresh install has none.

  It checks the Chromium **revision**, not merely that some build is present:
  `playwright install` removes outdated builds, so a project venv and a uv-tool
  install with different Playwright versions evict each other's browser and leave
  a cache that looks populated. That exact failure cost real time to diagnose.
  The revision comes from Playwright's `browsers.json` rather than from launching
  a browser — faster, and no asyncio teardown noise on a diagnosis screen.

### Added
- **`imaparc fetch --dry-run`** reports which mail each profile would take and
  what would happen to it on the server, and writes **nothing**: no `.eml`, no
  state entry, no server call. `after_fetch: delete` is irreversible and could
  previously only be checked by running it. Verified against a live GreenMail with
  a deleting profile — the mail is still there afterwards.
- **`imaparc fetch --no-server-actions`** archives as usual but suppresses every
  label, move and delete, for a first run with a new profile.

### Added
- **Naming scheme and safety limits are configurable per profile.**
  `filename_pattern` and `date_format` shape the base name (placeholders `{date}`,
  `{profile}`, `{subject}`; date tokens `YYYY MM DD hh mm ss`), and `gs_jobs`,
  `max_attachment_bytes`, `attachment_timeout_s` and `render_timeout_ms` are no
  longer fixed constants. All of it existed and was tested — it just could not be
  reached from a `profile.yaml`.

### Fixed
- **The naming scheme was only half wired up.** `pipeline.py` honoured
  `filename_pattern`/`date_format`, `fetch.py` did not. Since fetch names the
  `.eml` and render names the PDF folder, configuring a scheme would have given
  the two different names — and the shared base name is the only thing linking a
  raw mail to its rendition. Nothing would have failed; the archive would just
  have quietly stopped matching up. Both phases now read the options from the same
  `Profile` object, with a test that asserts the two agree.

### Fixed
- **`gs_jobs` now does what it says.** The field existed in `config.py` and in two
  sentences of the documentation, but was never read: Ghostscript ran through
  `asyncio.to_thread` with no bound of its own, i.e. at up to `jobs` conversions
  at once instead of the promised 2. Since one conversion holds a whole document
  in memory, peak memory scaled with render concurrency. The render orchestrators
  now hand `render_mail()` a shared semaphore, the same way they already share the
  `claimed` name-reservation set. A test drives six mails at `jobs=6, gs_jobs=2`
  and asserts that no more than two conversions are ever in flight.
- **A render run no longer loads the whole archive into memory.** It listed every
  mail's *contents* up front so the progress bar could show a total — for 10 000
  mails at 200 KB that is 2 GB before the first page is rendered. `EmlSource` grew
  a `paths()` method that lists without reading; each mail is read when its turn
  comes, so only `jobs` messages are resident at any moment. This is the pattern
  `adhoc.py` already used.

### Changed
- Render orchestration moved out of `cli.py` into a new `render_run.py`, next to
  the equivalents for the other two phases (`fetch.py`, `adhoc.py`). `cli.py`
  shrinks from 755 to 634 lines and now contains commands only.

### Removed
- **Dead configuration.** `RunConfig.overwrite` and `RunConfig.validate_pdfa` were
  never set and never read. Both advertised options that must not exist:
  `overwrite` would contradict the never-overwrite invariant `disambiguate()`
  enforces, `validate_pdfa: false` the PDF/A guarantee.
- **Unused dependencies.** `puremagic` was a *runtime* dependency imported nowhere
  — the magic-byte sniffing in `attachments/classify.py` is hand-written. `syrupy`
  sat unused in the dev group.

### Fixed
- **An attachment named `..` is no longer taken at face value.** `Path("..").name`
  returns `".."` — pathlib treats it as a name, not a path component — so joining
  it onto the staging folder pointed at the parent directory. Nothing escaped in
  practice, because `disambiguate()` saw the existing path and made `..-2` of it,
  but that is a side effect rather than a guarantee, and a mail *sender* picks
  this string. `.` and `..` are now rejected outright, with a test that walks a
  mail carrying exactly those attachment names through the pipeline.
- `test_sweep_staging_noop_when_dir_missing` carried no assertion and could not
  fail; it now checks that a missing directory is also not created as a side
  effect.

## [26.8.5] - 2026-08-01

### Changed
- **Python 3.14 is no longer claimed** (`requires-python = ">=3.12,<3.14"`).
  Python 3.14 turned `imaplib.IMAP4.file` into a read-only property; imapclient
  assigns to it while subclassing, so on 3.14 every IMAP operation raises
  `AttributeError: property 'file' … has no setter`. The fix is merged upstream
  but unreleased (imapclient is still at 3.1.0), so `imaparc fetch` simply does
  not work there. Declaring `>=3.12` while shipping that was a promise the tool
  could not keep — an install on 3.14 now fails cleanly instead of breaking at
  runtime. 3.12 and 3.13 were verified against a live GreenMail and are fine.
  This became visible only because the IMAP tests finally run in CI; they had
  been skipping on every Python version.
- CI matrix runs with `fail-fast: false`. With the default, the broken 3.14 job
  cancelled the 3.13 one, whose result then looked like a failure too — the kind
  of thing that leads to narrowing support further than the evidence warrants.

### Fixed
- **CI never ran the IMAP tests.** `test_imap.py` and the GreenMail half of
  `test_fetch.py` gate themselves on port 3143 and skipped on every CI run, so the
  code that *moves and deletes mail on the server* — `move()`, `delete()`,
  `_expunge_uid()`, `_post_fetch()` — went unverified there. Same failure mode as
  the veraPDF gap fixed in 26.7.46: a silently skipped test group covering the
  riskiest part of the pipeline. GreenMail now runs as a service container.

### Added
- Tests for the paths that were only reachable with a live IMAP server or not at
  all: a failing `move`/`delete` is counted rather than raised (the archive is
  already durable, so a server error must never abort a run), `\Noselect` and
  `\NonExistent` mailboxes are excluded from a scan, move targets resolve through
  the server's NAMESPACE with an INBOX-rooted fallback, a recursive scan skips both
  the Trash and the profile's own move target, one failing account or one broken
  message does not abort the rest, and a malformed subject header degrades instead
  of raising. Coverage of `fetch.py` 70 % → 98 % and of `sources/imap.py`
  48 % → 96 %, both now above the 95 % the conventions ask of critical modules.

## [26.8.4] - 2026-08-01

### Fixed
- **The Finder right-click action failed with "Missing required tool(s): gs, qpdf,
  verapdf".** A macOS Service is launched by launchd with a bare `PATH`, so
  `ToolPaths.resolve()` found none of the external tools even though they are
  installed and a terminal run works. The absolute path to `imaparc` had been
  handled, but the tools it shells out to had not. Their directories are now
  resolved at install time and baked into the action, with the usual Homebrew
  locations appended so a tool installed later is still found. Verified by running
  the generated command under `env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin`.

## [26.8.3] - 2026-08-01

### Added
- **`install.sh` — one-command setup on macOS.** Installs uv, Ghostscript, qpdf,
  veraPDF, imapArc and the Chromium build it renders with, skipping whatever is
  already present, then creates the config and the Finder action. veraPDF is not
  in Homebrew core, so it falls back to the official IzPack installer driven by a
  generated auto-install profile — the same approach CI uses. `--no-service` and
  `--no-init` opt out of the last two steps.
- **`imaparc install-service` — Finder right-click action.** Writes an Automator
  Quick Action to `~/Library/Services`, after which right-clicking `.eml` files
  *or* whole folders offers "Mit imapArc archivieren". It registers for
  `public.item`, so a mixed selection of files and folders is accepted, and passes
  the whole selection as arguments — which `collect_eml` already handles. The
  bundle is generated from `plistlib` rather than shipped as a binary blob, so the
  absolute path to `imaparc` is baked in at install time; a Service does not
  inherit `PATH`, so a bare command name would never resolve.

## [26.8.2] - 2026-08-01

### Fixed
- **Links on the one-page overview were dead.** The faithful rendition is scaled
  onto a single A4 page with `pikepdf`'s `add_overlay`, which embeds the source
  page as a form XObject — that carries the *content stream* only. Link
  annotations hang off the page's `/Annots` array and were silently dropped, so
  every link on that page did nothing. Since the overview is **page 1**, it is the
  page a reader clicks in, which is why this looked like "PDFs have no links at
  all" even though the reflowed rendition was fine throughout. The annotations are
  now carried across and their rectangles mapped with the same aspect-preserving,
  centred fit, so each link stays on its own text. Output remains PDF/A-3b
  compliant (annotations are flagged Print, as the standard requires).

## [26.8.1] - 2026-08-01

### Changed
- **`imaparc eml` now always loads remote images.** The command renders a mail the
  user picked out by hand and had already opened in a client, so the faithful
  rendition is what is wanted and any tracking pixel has long since fired. This is
  the one place in imapArc where remote fetching is on by default; the
  profile-driven commands keep their opt-in lockdown. The now-meaningless
  `--allow-remote-images` flag was removed from `eml`. Note the trade-off:
  rendering fetches tracking pixels, so the sender learns when and from where the
  mail was processed — use a profile plus `render` if that matters.

### Fixed
- **The documented Playwright setup step did not work.** Both READMEs told users to
  run `playwright install chromium` after `uv tool install`, but `uv tool install`
  only exposes imapArc's own entry points — not those of its dependencies — so no
  `playwright` executable exists on `PATH`. Rendering then failed with
  `Executable doesn't exist`. The install step now goes through the tool
  environment's interpreter, which also pins the Chromium build to the one the
  installed imapArc actually expects (the project venv and the tool environment
  can hold different Playwright versions). The development section likewise needed
  `uv run` in front.

## [26.8.0] - 2026-08-01

### Added
- **`imaparc eml` — render loose `.eml` files without a profile.** Dragging a
  message out of a mail client into the Finder produces an `.eml`; this command
  renders such files where they lie. Each mail becomes a `<basename>/` folder next
  to its own `.eml`, and the `.eml` is moved in afterwards. Arguments are files or
  directories (default: the current directory), `--name` fills the profile slot of
  the generated names. A directory argument is deliberately **not** scanned
  recursively, so mails already filed into their folders are never collected twice.
  The command reads no `profile.yaml`, needs no `.env`, contacts no server and
  writes nothing to the delivery state — a later `fetch` is entirely unaffected.

### Changed
- `validate_pdfa()` moved from `cli.py` into `report.py`, and the progress-display
  factory into `console.py`, so the new command shares them instead of duplicating
  them. No behaviour change.

### Fixed
- **`release.sh prod --new-month` skipped the `.0` release.** It ran `bump month`
  *and* then `bump micro`; since a month bump already resets micro to zero, the
  first release of a new month came out as `26.8.1` and `26.8.0` was never
  published. The two are now alternatives. The month bump also ran before
  `git checkout dev`, so it could rewrite the version on whatever branch happened
  to be checked out — it now runs after the checkout and pull.

## [26.7.46] - 2026-07-29

First public release. Everything below this entry was developed in a private
repository; the published history therefore starts here.

### Added
- **Release workflow** (`.github/workflows/release.yml`): pushing a `v*` tag now
  builds the distribution as a packaging smoke test and creates the GitHub release
  with generated notes and the artifacts attached. `.devN` tags are marked as
  pre-releases so they never replace the latest stable one. Nothing is published to
  PyPI — imapArc is installed straight from the repository with `uv tool install`.
- Tests for the run summary (`report.py`, now fully covered) and for the CLI's
  error, abort and confirmation paths — including the `sync-profiles` fail-safe
  that restores the original file when a rewrite would not parse back, and the
  Ctrl-C abort that exits 130 instead of printing a traceback.

### Fixed
- **PDF rendering works on Linux.** The sRGB ICC profile embedded as the PDF/A
  output intent was hard-coded to the macOS system path, with no fallback — on any
  other system every render aborted with `ICC profile not found`. imapArc now picks
  the first profile that exists from a candidate list (macOS, icc-profiles-free,
  colord, Ghostscript). Found only because veraPDF now runs in CI: the tests that
  cover this path had been skipped there all along.
- **CI is green again.** It had failed since v26.7.43 for two independent reasons:
  `pdftotext` (poppler-utils) was missing on the runner, so a render test errored,
  and the coverage gate was missed (76.2 %) because the tests needing veraPDF are
  skipped there. The workflow now installs poppler-utils, and the new tests lift
  the runner's coverage back over the gate.
- **veraPDF now runs in CI too.** It is not packaged for apt, so the PDF/A
  validation tests were skipped on every run — the part of the pipeline that
  proves the archive is actually PDF/A-3b went unverified, and the skipped tests
  were what pushed coverage under the gate. The workflow installs it headless via
  the official IzPack installer (`.github/verapdf-auto-install.xml`).

### Removed
- **`spike/`** — throwaway exploration scripts from the design phase. They no
  longer ran (`demo_real_mail.py` still imported the pre-redesign
  `imaparc.eml.parser`) and were the only files failing `ruff check`/`ruff format`
  repository-wide, which broke the repo-wide pre-commit hooks.

## [26.7.45] - 2026-07-29

### Added
- Image attachments rendered to PDF now keep a minimum print margin (~10 mm)
  instead of bleeding to the sheet edge, so nothing is clipped when printed.

### Fixed
- The recursive folder scan now also skips `\NonExistent` mailboxes (RFC 5258),
  not only `\Noselect` — a server using the former no longer produces a failing
  `SELECT` warning.

### Removed
- Dead, mis-computed faithful-rendition scale value and its now-unused
  `compute_scale`/`usable_width_px` helpers (the faithful page is scaled onto A4
  by the pikepdf overlay, so `Rendition.scale` was never read for it).

## [26.7.44] - 2026-07-29

### Removed
- **`strict_pdfa` profile option and `--strict-pdfa` flag.** Failing the whole run
  on PDF/A non-conformance was misaligned with the tool's philosophy: the `.eml`
  is the lossless guarantee, the PDF a best-effort rendition. Ghostscript already
  converts the whole document (body + attachment pages) to PDF/A-3b; veraPDF still
  validates, but non-conformance is now only **reported**, never fatal — the run
  always exits 0. A non-conformant **mail-only** rendition (imapArc's own output,
  which should always be conformant) is flagged in the summary as an anomaly.

## [26.7.43] - 2026-07-29

### Added
- **`match.trash` option** (default `false`). A recursive scan no longer descends
  into the server's Trash folder by default, so deleted mail is not silently
  re-archived (and, with `delete`/`move_to`, re-processed). The Trash is detected
  by the RFC 6154 `\Trash` special-use flag, falling back to well-known folder
  names when the server advertises none. Set `trash: true` to include it, or list
  the Trash explicitly under `folders` (an explicit source always wins).

## [26.7.42] - 2026-07-29

### Added
- A parity test guaranteeing the `add-profile`/`sync-profiles` template and
  `profile.example.yaml` cover **every** `Match`/`Profile`/`after_fetch` model
  field — adding an option without wiring it into the template now fails CI
  instead of silently producing incomplete profiles.

### Changed
- **Graceful Ctrl-C.** Interrupting a `fetch`/`render`/`all` run now prints a
  clean "Aborted — everything already archived is complete and safe" message and
  exits 130 instead of a traceback. Nothing was ever at risk (the `.eml` and each
  per-mail PDF folder are written atomically, and the server is never touched
  before the local archive is durable); this just makes the exit tidy.
- Each render run now sweeps stray `.staging-*` folders left by a previously
  interrupted run, so an aborted render leaves no litter behind.

## [26.7.41] - 2026-07-29

RFC-conformance audit of the mail-parsing, HTML-inlining, rendering and IMAP
layers. Nine confirmed defects fixed (each reproduced before the change).

### Fixed
- **Text attachments were corrupted (data loss).** `_attachment_bytes` decoded a
  `text/*` part via its charset and re-encoded it as UTF-8, altering — or lossily
  replacing with U+FFFD — the original bytes of any `.txt`/`.csv`/`.ics` with a
  non-UTF-8 or mismatched charset. Attachments are now stored as their exact
  Content-Transfer-Encoding-decoded octets (RFC 2045 §6).
- **Calendar/AMP alternatives became spurious attachment pages.** A non-selected
  `multipart/alternative` sibling (`text/calendar`, `text/x-amp-html`) is the same
  content in another form (RFC 2046 §5.1.4), not an attachment — no longer
  rendered as its own page. An `alternative` part explicitly marked
  `Content-Disposition: attachment` is still kept.
- **`move_to` failed on servers without the MOVE extension (notably Gmail).** It
  now falls back to the RFC 3501 sequence (COPY + `\Deleted` + expunge) when
  RFC 6851 MOVE is unavailable.
- **`delete` could leave mail flagged but not removed.** `UID EXPUNGE` needs
  UIDPLUS (RFC 4315); without it a bare `EXPUNGE` would remove *other* `\Deleted`
  mail, so delete now refuses (leaving the message flagged and locally archived)
  and reports it, rather than risking collateral.
- **`srcset` was stripped even under `--allow-remote-images`.** Responsive images
  (and `<picture><source srcset>` with no `src`) vanished. Each `srcset` candidate
  now goes through the same cid:/remote policy as `src`; blocked ones are recorded.
- **`cid:` matching was case-insensitive** — two distinct Content-IDs could
  collide and resolve to the wrong image. Matching is now case-sensitive
  (RFC 2392 / RFC 5322 msg-id).
- **The CSP + layout guard could be injected into an HTML comment** (a `<head>`
  inside `<!-- … -->`), silently disabling both. Injection is now comment- and
  attribute-aware (and won't mistake `<header>` for `<head>`).
- **Content-Location relative refs** (`./image001.png` vs `image001.png`) now
  resolve (RFC 2557, common form).
- **`<link rel="stylesheet" href="cid:…">`** is now resolved to a `data:` URI, or
  the unfetchable link dropped.

## [26.7.40] - 2026-07-29

### Fixed
- **Inline images in `multipart/related` mail now render.** The parser used
  `iter_attachments()`, which treats a `multipart/related` as a single body
  candidate and never reaches the inline resources nested inside it — so a
  signature logo referenced by `cid:` silently vanished from the rendered PDF
  (independent of the remote-image setting). The MIME tree is now walked
  RFC-correctly (RFC 2046/2183/2387/2392): `multipart/*` is descended into,
  `message/*` is treated as opaque (a forwarded mail stays one attachment), the
  displayed body is skipped, and parts carrying a `Content-ID`/`Content-Location`
  are kept as *related resources* for inline resolution rather than shown as
  attachment pages. Re-render to update existing archives.

## [26.7.39] - 2026-07-29

### Fixed
- **imapArc-generated pages now use their intended fonts.** `base.css` was injected
  through Jinja with autoescaping on, which turned the quotes in every
  `font-family` (e.g. `"SF Mono"`) into `&#34;`. Inside `<style>` that is invalid,
  so Chromium dropped the whole declaration and the plain-text, separator and info
  pages fell back to the default serif — the plain-text rendition was not
  monospace. The stylesheet is now embedded raw. Re-render to update existing
  archives (remove the affected `pdf/<basename>/` folder, then run `render`).

## [26.7.38] - 2026-07-29

### Changed
- **The scaled one-page overview is now added only when a mail spans more than one
  page.** For a mail that already fits a single page it was just a smaller
  duplicate of the reflowed rendition, so it is skipped: a simple one-page letter
  now yields the reflowed + plain-text renditions instead of three near-identical
  variants.

### Fixed
- **`since`/`until` now also bound undated mail:** a message without a `Date`
  header falls back to the IMAP `INTERNALDATE`, so age filters are no longer
  silently ignored for such mail (fetch).
- **Airtight post-fetch retry guard:** the delivered `.eml` filename is recorded in
  the state store, and a message still on the server is re-checked against *its
  own* archived file rather than a name reconstructed from the current headers —
  which could collide with a different mail sharing the same base name. Existing
  state databases are migrated automatically.

## [26.7.37] - 2026-07-29

### Fixed
- A folder a profile **explicitly** lists as a source is no longer skipped just
  because another profile moves mail into it — the move-target scan exclusion now
  applies only to folders nobody asked to scan (fetch).
- The faithful one-page overview now **logs a warning** when a mail is long enough
  that Chromium may truncate the single tall page (the reflowed and plain-text
  renditions stay complete) — no more silent cut-off.
- Removed two stale `0500` references in docstrings (directories are `0700`).

## [26.7.36] - 2026-07-29

### Fixed
- Completed the README options table: added the `all` command (it takes every
  `fetch` and `render` option), `reset --state`, and corrected the verbosity row
  (it applies to `all`/`fetch`/`render`, not every command). Both READMEs.

## [26.7.35] - 2026-07-29

### Changed
- **The reflowed rendition now fits wide mail without clipping — measured, not
  assumed.** It measures the mail's content width (mails are built to fit a screen
  without horizontal scrolling, so the width is bounded): a mail that fits A4
  portrait still renders 1:1; one that overflows is scaled down; and if fitting
  portrait would shrink it below 85%, the page switches to **A4 landscape** (a far
  wider content area) so the mail stays readable. Chromium paginates cleanly
  between lines throughout, and the output stays vector/searchable and PDF/A-3b
  conformant (mixed portrait/landscape pages allowed). Wide tables and images are
  still capped to the page width by the render guard as before.

## [26.7.34] - 2026-07-29

### Changed
- **Reverted the scaled rendition to vector (searchable) output.** 26.7.33 made it
  a raster screenshot; that bloated file size and dropped selectable text. The
  scaled rendition is again one tall **vector** PDF page scaled onto A4 — it stays
  searchable and small. Chromium cannot export SVG, but the vector page.pdf output
  is the scalable, selectable equivalent, and the guard's `* { page: auto }`
  already neutralises the paged-media CSS the screenshot was meant to sidestep.
  Exotic-CSS layout remains best-effort; the `.eml` and plain-text rendition stay
  the lossless/deterministic fallback.

## [26.7.33] - 2026-07-29

### Changed
- **The scaled ("Originalgetreue") rendition is now a client-fidelity screenshot.**
  It captures a full-page screenshot of the mail at a fixed reference width —
  exactly what a mail client shows in continuous screen media — and scales that
  image onto one A4 page. Because a screenshot never paginates, the mail's own
  paged-media CSS (`@page`, `page:`, page breaks) can no longer distort this
  rendition: it looks like the client, just scaled down for a long mail. Trade-off:
  this one rendition is a raster image (no selectable text) — the reflowed and
  plain-text renditions remain vector/selectable. Rendered at 2× device pixels
  for crispness; falls back to the vector method for extremely long pages. Still
  PDF/A-3b conformant.

## [26.7.32] - 2026-07-29

### Changed
- **A mail whose HTML is just plain text in a trivial wrapper now renders as
  text only** — the scaled and reflowed HTML renditions are skipped, since they
  would look identical to the plain-text version. The detection
  (`html_is_trivial_wrapper`) is deliberately conservative: it treats HTML as
  trivial only when it contains no image, table, link, list, heading, emphasis,
  background or border — so any real formatting is still rendered as HTML and
  never flattened away (and the `.eml` preserves everything regardless).

## [26.7.31] - 2026-07-29

### Fixed
- **Zero page margins in the reflowed/text renditions.** The 26.7.27 WordSection
  fix injected `@page { margin: 0 }`, and a CSS `@page` margin overrides
  Chromium's `page.pdf` margin parameter — so every reflowed/text/separator page
  rendered edge-to-edge. The guard now neutralises the mail's named page with
  `* { page: auto }` only (no `@page` margin), restoring the intended margins.

### Changed
- **Letter-style page margins:** 20 mm on top/right/bottom and a wider **25 mm on
  the left** (a filing edge, DIN-5008-style), applied to all renditions including
  the scaled one-page overview.

## [26.7.30] - 2026-07-29

### Fixed
- **A profile that both moves mail and scans recursively no longer duplicates it.**
  With `recursive: true` and `after_fetch: move_to: imapArc`, the target
  `INBOX.imapArc` sits *inside* the recursively-scanned INBOX tree — so the moved
  message (now under a new UID) was re-scanned, matched again, and delivered a
  second time (`…-2.eml`, and a second PDF folder). fetch now resolves every
  profile's move target and **excludes those folders from scanning**, so mail
  imapArc has already filed away is never re-processed.

## [26.7.29] - 2026-07-29

### Added
- **A server-side `move`/`delete` is now only applied when the mail's `.eml`
  exists in the configured `output/eml/` at that moment.** This makes "imapArc
  preserves" hold even for server deletion: if you switch a profile to `delete`
  and the archive was moved away or the output path changed, imapArc skips the
  action (warns, and the summary reports "N server action(s) skipped — no local
  .eml copy") instead of removing the only copy from the server. A fresh delivery
  is always safe (the `.eml` was just written); the guard matters for the
  re-applied action on already-delivered mail.

## [26.7.28] - 2026-07-29

### Changed
- **Archive directories are now `0700` instead of `0500`**, so you can delete or
  reorganise your archive directly (`rm -rf …`, Finder) without first running
  `chmod`. Files stay `0400` (read-only content, a guard against accidental
  in-place edits), and the **never-overwrite** guarantee is unchanged — it is
  enforced in code by `disambiguate()`/atomic-rename, not by removing directory
  write permission. `0700` also keeps the archive private (no group/other access).
  Existing archives keep their old `0500` dirs until touched; run
  `chmod -R u+rwx ~/imapArc` once to relax them immediately.

## [26.7.27] - 2026-07-29

### Fixed
- **Outlook/Word mail no longer renders with an empty first page.** Such mail
  wraps its body in `div.WordSection1 { page: WordSection1 }` with an
  `@page WordSection1 { size…; margin… }` rule — valid paged-media CSS that
  Chromium honoured, forcing a page break *before* the whole body. The result:
  the reflowed version showed the body only on the following page, and the
  faithful one-page render captured just the empty first page (header only). The
  render guard now neutralises the mail's own paged-media CSS (`page: auto`,
  neutral breaks, `@page { margin: 0 }`), so header and body stay together and
  pagination is controlled solely by imapArc.

## [26.7.26] - 2026-07-29

### Fixed
- The folder a mail is moved into (`after_fetch: move_to`) is now **subscribed**,
  so it actually shows up in mail clients. Before, `create_folder` made the
  folder but left it unsubscribed, so clients that list only subscribed folders
  did not display it — the mail looked like it had vanished (it was safe on the
  server and in the local `.eml` archive all along). Subscription is done on every
  move (idempotent), which also re-subscribes a folder an earlier version created.

## [26.7.25] - 2026-07-29

### Fixed
- A failing `after_fetch: label` no longer blocks the `move`/`delete`. The label
  runs first (a move re-mints the UID), but it is now isolated: if the server
  rejects the keyword, imapArc warns and still performs the move/delete, instead
  of the shared error handler swallowing the exception and skipping the move.
  This is why a profile with both `label` and `move_to` set could look like
  "move_to does not work".

## [26.7.24] - 2026-07-29

### Changed
- Move-target resolution now asks the server for its personal namespace via the
  **IMAP NAMESPACE command (RFC 2342)** instead of guessing from the source
  folder — so the prefix (`INBOX.`) and hierarchy delimiter are whatever the
  server declares, not hard-coded. If a server does not advertise NAMESPACE (e.g.
  GreenMail), it falls back to the LIST delimiter and only infers an `INBOX.`
  prefix when the source folder itself lives under `INBOX`. This makes
  `after_fetch: move_to` correct across servers, not just Dovecot.

## [26.7.23] - 2026-07-29

### Changed
- **The delivery state is no longer a silent black box.** `fetch` now reports how
  many scanned messages were already archived on a previous run — per folder
  (`scanned 18 message(s) in … (18 already archived on a previous run)`) and in
  the summary (`18 already archived … (use 'imaparc reset' to re-process
  everything)`). So a run that delivers nothing new explains itself instead of
  looking like it found no mail.

## [26.7.22] - 2026-07-29

### Fixed
- **`after_fetch: move_to` now works on servers with an INBOX namespace** (e.g.
  Dovecot, where folders are `INBOX.Trennung.…`). The target is resolved to the
  server's hierarchy delimiter and namespace, so `move_to: imapArc` becomes
  `INBOX.imapArc` and `Archiv/Erledigt` becomes `INBOX.Archiv.Erledigt`. Before,
  the move failed with "nonexistent namespace / prefix with INBOX".
- **A failed server-side action no longer floods the output with tracebacks.** A
  label/move/delete failure is best-effort — the mail is already safely archived —
  so it now logs one concise warning, is counted in the summary ("N archived but
  the server-side … failed"), and never aborts the folder.
- **A configured `move`/`delete` is enforced on later runs.** A matched message
  still present in the folder (because a prior move/delete failed) has its action
  re-applied, instead of being skipped forever just because it was already
  archived. Once the move/delete succeeds the mail leaves the folder, which is the
  natural de-duplication — the state DB no longer strands it.

### Changed
- New profiles created by `init`/`add-profile` now default to
  `after_fetch: move_to: imapArc` (active), so matched mail is moved out of the
  source folder after archiving.

## [26.7.21] - 2026-07-29

### Added
- `imaparc list-profiles` — show the defined profiles as a table (name, account,
  output, whether PDFs are rendered, a one-line match-rule summary, and the
  after-fetch action), so you can eyeball a profile's rules at a glance.

### Changed
- The default `output` in a generated profile block is now `~/imapArc/<name>`
  (was `~/Archiv/imapArc/<name>`) — used by `init`, `add-profile`, and as the
  `add-profile --output` default.

## [26.7.20] - 2026-07-29

### Added
- `imaparc sync-profiles` — rewrite an existing `profile.yaml` into the canonical
  full-option format: your set values stay active, every option you did not set
  appears commented with its default. The old file is backed up (`.bak`) and, if
  the rewrite would not parse, the original is restored. (Your own inline comments
  are not preserved — the file is rebuilt from the parsed values.)

### Changed
- **`imaparc` with no subcommand now shows the help** instead of running the full
  pipeline. The full run (fetch, then render `pdf: true` profiles) moved to the
  explicit **`imaparc all`** command; `--profile` still restricts it.
- `init`, `add-profile` and `sync-profiles` now share one profile-block renderer
  (`bootstrap.render_profile_from_raw`), so the generated layout is identical
  everywhere.

## [26.7.19] - 2026-07-29

### Added
- `imaparc add-profile <name>` — append a new, fully-annotated profile block to
  `profile.yaml`. Required fields (name, account, output) are filled with
  placeholders; every optional field is present but commented with its default.
  `--output` sets the target dir; a name that already exists is refused.

### Changed
- The `profile.yaml` written by `imaparc init` now contains **every** profile
  option, from one canonical block shared with `add-profile`: the required
  fields active, all optional fields present but commented with their defaults —
  so nothing is hidden and you just uncomment what you need.

## [26.7.18] - 2026-07-29

### Changed
- **`render` now shows modern progress bars** instead of streaming log lines. A
  "Rendering mail" bar appears from the start with the total mail count and
  advances per mail (count, percent, elapsed and remaining time); a second
  "Validating PDF/A" bar tracks the veraPDF batches. Logs and warnings print
  cleanly above the bars (shared Rich console). Bars are hidden at `-Q/--silent`.
- The run summary now lists the file names of any PDFs that are not PDF/A-3b
  compliant (up to ten), so "N not compliant" says *which*.

## [26.7.17] - 2026-07-29

### Fixed
- Silenced expected third-party warning spam that flooded the console on runs with
  image or form attachments: img2pdf's per-image "Image contains an alpha channel"
  notice (transparent PNGs) and pikepdf's `PageCopyWarning` about form fields not
  surviving the attachment merge. Both are expected here — the originals are
  archived unchanged, and PDF/A forbids interactive forms anyway — so they are
  suppressed; the console now shows only imapArc's own progress and summary.

## [26.7.16] - 2026-07-28

### Changed
- **One folder per mail, no duplicated PDF.** Every mail is now stored under its
  own `pdf/<basename>/` folder. `<basename>.pdf` is the full PDF you open (mail
  body + attachment pages when there are attachments, otherwise just the body).
  A mail **with** attachments additionally gets `<basename>_mailonly.pdf` (the
  body without attachment pages) and the original attachment files in the same
  folder. There is no longer a loose PDF at the `pdf/` root, so a mail's PDF no
  longer appears in two places with the same name. This replaces the previous
  layout (a combined PDF at the root plus a subfolder repeating the mail as
  `<basename>.pdf`, and a flat single PDF for attachment-less mails).
- The per-mail folder is moved into place with a single atomic rename, so its
  presence alone means the mail is fully rendered — there is no half-written
  folder to detect and repair on a later run.

## [26.7.15] - 2026-07-28

### Fixed
- **Final PDF/A validation no longer appears to hang on large archives.** veraPDF
  was invoked once per PDF, and each call starts a fresh JVM — for hundreds of
  mails that meant hundreds of JVM starts and a silent, seemingly stuck run.
  Validation now runs in batches (one veraPDF process per 100 files) and logs
  `validating N PDF(s) with veraPDF` so the closing phase is visible. Paths are
  deduplicated first, so a flat mail (whose combined and mail-only PDFs are the
  same file) is validated once, not twice.

## [26.7.14] - 2026-07-28

### Fixed
- `render` no longer crashes (and aborts the whole run) when two mails resolve to
  the same basename — identical `Date` header **and** subject, which happens for
  duplicate/forwarded copies. Concurrent renders now reserve basenames through a
  shared set, so the second mail disambiguates to `…-2` instead of racing on the
  output path (previously a `PermissionError` on the subfolder rename). A single
  mail's render failure is also isolated now, so one bad message cannot abort the
  rest of the profile.

## [26.7.13] - 2026-07-28

### Changed
- The "Originalgetreue Darstellung" (faithful rendition) now scales the **whole
  mail onto a single A4 page** — an at-a-glance overview of the message as sent,
  no matter how long it is. It is laid out at a fixed reference width, rendered
  as one tall page, then fitted onto A4 (aspect-preserving, centred). The
  readable full-size version follows as the "Umbrochene Fassung".
- **An attachment-less mail is now archived as one flat `<basename>.pdf`**, with
  no subfolder. Without attachments the combined PDF equals the mail-only PDF, so
  the subfolder would have held nothing but a duplicate. Mails **with** attachments
  keep the double structure (combined PDF + subfolder with the mail-only PDF/A and
  the originals). Idempotency for a flat mail is tracked by a hidden
  `.<basename>.imaparc-manifest` sidecar.

## [26.7.12] - 2026-07-28

### Fixed
- Removed a double page margin: the Chromium print margin (per rendition) and the
  CSS `.page` padding (20mm) stacked, giving the reflowed and text pages ~40mm
  margins. The CSS padding is now 0, so only the print margin applies (10mm
  faithful, 20mm reflowed/text).

## [26.7.11] - 2026-07-28

### Added
- `imaparc reset` — clear the delivery-dedup state so the next fetch re-processes
  all matching mail, instead of hand-deleting `state.db`. Archived `.eml`/PDF
  files are left untouched; `--yes` skips the confirmation.

## [26.7.10] - 2026-07-28

### Fixed
- The plain-text version (with its "Nur-Text-Version" separator) is now **always**
  part of the combined PDF — for an HTML-only mail it is derived from the HTML
  (`html_to_text`), where before such mails had no text version at all.

## [26.7.9] - 2026-07-28

### Added
- `fetch` logs how many messages it scanned per folder ("scanned N message(s) in
  INBOX"), so a "0 delivered" run tells apart "connected but nothing matched" from
  "nothing scanned" (connection/folder problem).

## [26.7.8] - 2026-07-28

### Added
- Per-profile `jobs` (default 4) — the last render setting to become a profile
  field, so every behaviour option now lives in the profile. `--jobs` overrides
  it for the run. Each profile renders with its own concurrency.

## [26.7.7] - 2026-07-28

### Changed
- Redesigned the PDF separator, section and info pages: calmer, centred cards
  with an uppercase eyebrow, the filename as title, and hairline-separated meta;
  section dividers are a centred uppercase rule; the info page carries a warm
  hairline to signal "kept as original". More whitespace and subtler greys.

## [26.7.6] - 2026-07-28

### Added
- Per-profile render settings `remote_images` and `strict_pdfa` (both default
  `false`). The CLI flags `--allow-remote-images` / `--strict-pdfa` force them on
  for the whole run; otherwise each profile's value applies. Each profile renders
  in its own browser pool, so a profile without `remote_images` keeps the full
  network lockdown even when another profile in the same run enables it.

## [26.7.5] - 2026-07-28

### Changed
- **Archive format is now readable `.eml` files, not a Maildir.** `fetch` writes
  `<output>/eml/<basename>.eml`, where `<basename>` is the same
  `YYYY-MM-DD_hh-mm-ss_PROFILE_SUBJECT` used for the PDF — so a mail's `.eml` and
  its PDFs share one name. Files are `0400`, the directory `0500`, never
  overwritten (a collision is disambiguated `…-2`). `render` reads `<output>/eml/`.
  This trades live Thunderbird-Maildir binding for readable, PDF-matching names.
- Base-name sanitising now collapses runs of whitespace and/or underscores to a
  single `_` (and trims leading/trailing `_`), for clean single separators.

## [26.7.4] - 2026-07-28

### Added
- `match.mode`: which address header fields (`from`/`to`/`cc`/`bcc`, default all
  four) `domains`/`addresses` are searched in — e.g. `mode: [to]` matches on the
  recipient, not the sender.
- `match.subject` now also accepts a list of case-insensitive wildcard patterns
  (`['*Rechnung*', 'Invoice*']`), any of which may match, in addition to a single
  regex string.
- `match.attachments`: require the message to carry an attachment of a given type
  (`[pdf]`, dot optional). Checked on the fetched body, so the envelope scan stays
  cheap — the body is pulled once, only for a header match that needs it.
- `match.until`: upper date bound alongside `since` (inclusive window).

## [26.7.3] - 2026-07-28

### Changed
- The CLI is now three profile-driven modes plus setup, all reading
  `profile.yaml`:
  - `imaparc` (no subcommand) runs the whole pipeline over all profiles: fetch
    every profile, then render those with `pdf: true`.
  - `imaparc fetch` fetches only (the `pdf` flag is irrelevant).
  - `imaparc render` renders only, for **all** profiles — the `pdf` flag is
    ignored (asking to render means render), each `<output>/maildir` into
    `<output>/pdf`. Needs no `.env`.
  - `--profile <name>` restricts any mode to a single profile.

  Render's positional maildir argument and `--output`/`--profile-name` are gone;
  everything comes from `profile.yaml`.
- Removed the unused `RunConfig.output_dir` field.

## [26.7.2] - 2026-07-28

### Added
- `match.recursive` (default `false`): also scan the subfolders of each listed
  folder. `[INBOX]` on its own stays non-recursive.
- `fetch --profile <name>`: run only the named profile.

### Changed
- `fetch` now re-evaluates **every** message in the scanned folders against the
  *current* profiles on each run — matching on envelope headers and pulling the
  full body only for a match. The state store records only *delivered* messages
  (dedup), so a changed or newly added profile takes effect on existing mail with
  no manual state reset; unmatched mail is never marked. This replaces the former
  UID high-water mark, which silently ignored old mail after a profile change.

## [26.7.1] - 2026-07-28

### Fixed
- `fetch`/parsing no longer crashes on a mail whose attachment is a nested
  `multipart/*` part (an embedded or forwarded message attached inline), which
  made `email.get_content()` raise `KeyError`. Such parts are now kept as raw
  MIME bytes, and any single unparseable attachment part is skipped rather than
  failing delivery of the whole message.

## [26.7.0] - 2026-07-28

imapArc's first functional shape: fetch IMAP mail into a Maildir, and optionally
render it to PDF/A. Renamed and redesigned from the earlier `mail2pdf` prototype
(package and CLI are `imaparc`); the durability guarantee moved off the PDF and
onto the Maildir, and collection and rendering are decoupled.

### Added
- `imaparc init` — bootstrap the central config: create `~/.config/imaparc`
  with a `.env` (`0600`) and `profile.yaml` template (dir `0700`), idempotent and
  never overwriting an existing file without `--force`.
- Central config by default: `fetch` reads `~/.config/imaparc/.env` and
  `profile.yaml`, state in `~/.local/state/imaparc/` (XDG; flags still override).
- Profile domain matching accepts a leading `@` (`@hetzner.com`) to read
  unmistakably as a domain; matches the domain and its subdomains.
- `imaparc fetch` — collect mail from IMAP accounts (`.env`) by profile
  (`profile.yaml`) into a real Maildir (standard names with a neutral host,
  `cur/` Seen, files `0400`, dirs `0500`). UID-based idempotency (SQLite),
  non-invasive `BODY.PEEK[]`, optional post-fetch label → move/delete. Verified
  against a local GreenMail server.
- `after_fetch.delete` — optionally remove the source message from the server
  (\Deleted + expunge) after it is durably archived and its UID recorded;
  mutually exclusive with `move_to`.
- `imaparc render` — render a Maildir into the fixed double structure: a combined
  PDF (three body renditions with separators, attachment pages or info pages)
  plus a subfolder holding the mail-only PDF/A-3b and the original attachments.
  Atomic, never-overwrite, identity-aware idempotency (`.imaparc-manifest`).
- Chromium rendering with a four-layer network lockdown, screen-media emulation,
  and `--allow-remote-images` opt-in.
- Attachment handling: PDF appended 1:1 (qpdf), images (img2pdf) and txt/md
  rendered to pages; Office/other kept as originals behind an info page — Office
  formats are preserved, never converted (fidelity and untrusted-input reasons).
- PDF/A-3b via Ghostscript, validated with veraPDF; immutable archive
  (files `0400`, directories `0500`, never overwrite).

### Security
- IMAP passwords held as `SecretStr` (kept out of logs/reprs).
- Remote-resource stripping broadened before rendering: SVG `href`/`xlink:href`,
  bare-string CSS `@import`, and fetch-triggering `<link>` rels (preload,
  prefetch, dns-prefetch, preconnect, icon, …); plain hyperlinks untouched.
- Attachment conversion bounded by a timeout; image decode capped against
  decompression bombs.
- Per-message and per-account fetch failures isolated so one bad item cannot
  abort a folder or the whole run.

