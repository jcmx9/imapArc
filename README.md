# imapArc

> Archiviert E-Mails als Original (`.eml`) und als eine durchsuchbare PDF/A pro Mail — aus IMAP-Postfächern oder aus einzelnen Dateien.

[Sprache: **Deutsch** | [English](README.en.md)] · Version `26.11.13`

[![CI](https://github.com/jcmx9/imapArc/actions/workflows/ci.yml/badge.svg)](https://github.com/jcmx9/imapArc/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

## Was imapArc macht

imapArc holt E-Mails profilgesteuert aus IMAP-Konten (oder nimmt `.eml`-Dateien von der Festplatte) und legt jede Mail zweimal ab:

- als **Original** — die unveränderte `.eml`, in jedem Mailprogramm zu öffnen;
- als **PDF/A-3** — eine Datei pro Mail, im Browser gerendert, durchsuchbar, mit den Anhängen als Seiten.

Leitprinzip: **imapArc bewahrt, es verändert nicht.** Das Original bleibt Byte für Byte erhalten; die PDF ist die lesbare Fassung davon. Nichts wird je überschrieben.

### Das Archiv

```
~/imapArc/
├── Hetzner/                                   # ein Ordner je Profil: nur PDFs
│   ├── 2026-07-01_10-30-00_Hetzner_Rechnung_Juli_2026.pdf
│   └── 2026-08-01_10-30-12_Hetzner_Rechnung_August_2026.pdf
├── 00_Mails/                                  # Mails, die zu keinem Profil passen
├── eml-Archiv/                                # die Originale, je Profil
│   └── Hetzner/
│       ├── 2026-07-01_10-30-00_Hetzner_Rechnung_Juli_2026.eml
│       └── 2026-08-01_10-30-12_Hetzner_Rechnung_August_2026.eml
└── emlinput/                                  # optional: Sammelordner für lose .eml
```

Der Name folgt dem Schema `DATUM_UHRZEIT_PROFIL_BETREFF` und ist je Profil einstellbar. Uhrzeiten sind Ortszeit.

### Was in jeder PDF steckt

| Inhalt | Wozu |
|--------|------|
| Die Mail mit Kopfblock (Betreff, Von, Datum, An/CC) | lesbar wie im Mailprogramm; bei aufwendigen Mails zusätzlich eine Ein-Seiten-Übersicht und immer eine Nur-Text-Fassung |
| Die Anhänge als Seiten (PDF, Bilder, Text — auch angehängte Mails samt deren Anhängen) | alles in einer Datei, durchsuchbar |
| **Lesezeichen** zur Mail, zu jeder Fassung und zu jedem Anhang | schnelle Navigation |
| **Die Original-`.eml`, eingebettet** | die PDF trägt ihr Original in sich — `imaparc extract` holt es heraus |
| **Anhänge, die keine Seiten werden** (Word, Excel, ZIP …), eingebettet | im PDF-Programm unter „Anhänge“ direkt zu öffnen; eine Info-Seite weist darauf hin |
| **Fingerabdruck** (SHA-256 der `.eml`) und Message-ID in den Metadaten | die PDF weist sich selbst aus — auch umbenannt oder herauskopiert |
| Titel und Autor = Betreff und Absender | Anzeige in Finder, Spotlight, PDF-Programmen |

## Voraussetzungen

- Python ≥ 3.12 und < 3.14
- [uv](https://docs.astral.sh/uv/)
- Ghostscript, qpdf, veraPDF und Chromium — nur zum Rendern (z. B. `brew install ghostscript qpdf verapdf`)
- Git

## Installation

**Der einfache Weg (macOS und Linux):** ein Befehl installiert alles — uv, Ghostscript, qpdf, veraPDF, imapArc, Chromium — und richtet den Rechtsklick-Eintrag ein:

```bash
curl -fsSL https://raw.githubusercontent.com/jcmx9/imapArc/main/install.sh | bash
```

Vorhandenes wird übersprungen. Optionen: `--no-service` (ohne Rechtsklick-Eintrag), `--no-init` (ohne Konfiguration anzulegen).

### Manuelle Installation

```bash
uv tool install git+https://github.com/jcmx9/imapArc.git        # von GitHub
# oder aus einem lokalen Klon:  git clone … && cd imapArc && uv tool install .

uv tool update-shell                                             # falls ~/.local/bin nicht im PATH ist
"$(uv tool dir)/imaparc/bin/python" -m playwright install chromium   # Browser fürs Rendern
```

Der Umweg über den Interpreter stellt sicher, dass genau die Chromium-Version geladen wird, die das installierte imapArc erwartet. `imaparc doctor` prüft anschließend, ob alles da ist.

### Update und Deinstallation

```bash
uv tool install --force git+https://github.com/jcmx9/imapArc.git   # Update
uv tool uninstall imaparc                                           # Deinstallation
```

## Einrichtung

```bash
imaparc init                             # legt ~/.config/imaparc/{.env,profile.yaml} an
$EDITOR ~/.config/imaparc/.env           # IMAP-Konto eintragen
$EDITOR ~/.config/imaparc/profile.yaml   # Profile anpassen
imaparc doctor                           # Installation, Konfiguration und Login prüfen
```

| Datei | Zweck |
|-------|-------|
| `~/.config/imaparc/.env` | IMAP-Konten (Zugangsdaten, `0600`) |
| `~/.config/imaparc/profile.yaml` | Profile: Regeln und Zielverzeichnisse |
| `~/.local/state/imaparc/state.db` | welche Mails schon abgeholt sind (automatisch) |

**`.env`** — ein oder mehrere Konten im Format `IMAP_<KONTO>_<FELD>`:

```dotenv
IMAP_PRIVAT_HOST=imap.example.com
IMAP_PRIVAT_USER=you@example.com
IMAP_PRIVAT_PASSWORD=your-password
IMAP_PRIVAT_SSL=true
```

**`profile.yaml`** — jedes Profil beschreibt, welche Mails zusammengehören und wohin sie kommen:

```yaml
profiles:
  - name: Hetzner
    account: privat                # verweist auf IMAP_PRIVAT_* in .env
    match:                         # alle gesetzten Regeln müssen zutreffen (UND)
      domains: ['@hetzner.com']    # Domain(s), mit führendem @
      # addresses: [billing@hetzner.com]  # exakte Adresse(n)
      # mode: [from, to, cc, bcc]  # welche Felder durchsucht werden (Default alle)
      subject: ['*Rechnung*']      # Regex ODER Wildcard-Liste
      attachments: [pdf]           # nur Mails mit Anhang dieses Typs
      folders: [INBOX]             # IMAP-Ordner (Default INBOX)
      recursive: false             # auch Unterordner
      since: 2026-01-01            # Mails davor ignorieren
      # until: 2026-12-31          # Mails danach ignorieren
      # larger: 5MB                # nur größere / smaller: nur kleinere Mails
    output: ~/imapArc/Hetzner      # Ziel der PDFs; .eml in ~/imapArc/eml-Archiv/Hetzner
    pdf: true                      # auch PDFs rendern (Default false)
    # remote_images: false         # externe Bilder beim Rendern laden
    # filename_pattern: '{date}_{profile}_{subject}'
    # date_format: YYYY-MM-DD_hh-mm-ss
    after_fetch:                   # optional; erst nach gesicherter Ablage
      label: imapArc               #   IMAP-Keyword setzen
      move_to: imapArc             #   verschieben (exklusiv zu delete)
      # delete: true               #   löschen (exklusiv zu move_to)
```

Alle Parameter mit Defaults: [`profile.example.yaml`](profile.example.yaml). `imaparc add-profile NAME` hängt ein vollständig kommentiertes Profil an, `imaparc list-profiles` zeigt die Profile als Tabelle.

## Verwendung

### Postfächer archivieren

```bash
imaparc all              # abholen, dann rendern (nur Profile mit pdf: true)
imaparc fetch            # nur abholen (IMAP → .eml)
imaparc fetch --dry-run  # nur zeigen, was passieren würde — schreibt nichts
imaparc render           # nur rendern (alle Profile, pdf-Flag egal)
imaparc all --profile Hetzner   # jeden Befehl auf ein Profil beschränken
```

`fetch` bewertet bei jedem Lauf alle Mails der Ordner gegen die **aktuellen** Profile und überspringt, was schon abgeholt ist — ein geändertes oder neues Profil greift also automatisch auch auf alte Mails. Server-Aktionen (`after_fetch`) laufen erst, wenn die Mail lokal sicher liegt. `render` überspringt jede Mail, die ihre PDF schon hat, und braucht kein `.env`.

### Einzelne Mails ablegen: `imaparc eml`

Zieht man eine Mail aus Apple Mail, Thunderbird oder Outlook in den Finder, entsteht eine `.eml`-Datei. `imaparc eml` legt solche Dateien — egal, wo sie liegen — genauso ins Archiv wie `fetch` + `render`:

```bash
imaparc eml a.eml b.eml            # genau diese beiden
imaparc eml ~/Desktop              # alle .eml dort (nicht in Unterordnern)
imaparc eml --no-remote-images ~/imapArc/emlinput   # viele alte Mails, ohne Nachladen
```

Das **Profil ergibt sich aus dem Header**: Die Regeln der `profile.yaml` (Adressen, Domains, Betreff, Datum, Anhangtypen) werden auf die Mail angewandt; Ordnerregeln gelten nur für IMAP. Passt die Mail zu mehreren Profilen, landet sie in jedem; passt sie zu keinem, in `~/imapArc/00_Mails`. Sobald die PDF existiert, wandert die `.eml` ins `eml-Archiv` — die Quelldatei verschwindet erst, wenn alle Kopien geschrieben sind.

Per **Rechtsklick** geht dasselbe im Dateimanager, auf markierte `.eml` und ganze Ordner: unter macOS **Dienste → „Mit imapArc archivieren“**, unter Linux **Öffnen mit** (Nautilus, Dolphin, Thunar). Einrichten oder erneuern: `imaparc install-service`.

> **Externe Bilder** werden bei `imaparc eml` standardmäßig geladen (bei `render` nur, wenn das Profil es erlaubt): Eine von Hand herausgezogene Mail soll aussehen, wie sie gesetzt wurde. Dabei werden auch Tracking-Pixel abgerufen. `--no-remote-images` schaltet das ab.

### Mit den PDFs arbeiten

```bash
imaparc mailonly a.pdf          # a_mailonly.pdf: nur die Mail, ohne Anhangseiten
imaparc extract a.pdf           # die Original-.eml aus der PDF herausholen
imaparc extract --all a.pdf     # dazu die eingebetteten Anhänge
```

- **`mailonly`** kopiert die Seiten vor dem ersten Anhang in eine neue, schlanke PDF — nichts wird neu gerendert. Die eingebettete `.eml` und die Anhänge bleiben weg (mit `--keep-embedded` drin): gedacht zum Weitergeben.
- **`extract`** schreibt die eingebettete Mail als Datei und meldet, ob sie zum Fingerabdruck der PDF passt. Damit genügt eine einzelne PDF, um die Mail wiederherzustellen.

Beide schreiben ins aktuelle Verzeichnis (oder nach `--output`), überschreiben nie und fassen das Archiv nicht an.

### Archiv prüfen

```bash
imaparc verify           # Mails ohne PDF, doppelte PDFs, Reste abgebrochener Läufe
imaparc verify --deep    # zusätzlich jede Datei lesen: eingebettete Mail unversehrt?
imaparc doctor           # Installation: Werkzeuge, Browser, Konfiguration, Login
```

`verify` liest nur — es meldet, es repariert nie. Zugeordnet wird über den Fingerabdruck, nie über den Namen. Der Exit-Code ist nur bei echtem Schaden 1, sodass ein geplanter Lauf nicht wegen harmloser Doppel anschlägt. `--deep` meldet außerdem PDFs, deren `.eml` nicht mehr im Archiv liegt (sie steckt dann noch in der PDF), und byte-gleiche `.eml`-Kopien.

### Zurück auf den Server

```bash
imaparc restore ~/imapArc/eml-Archiv/Hetzner --folder Archiv --dry-run
```

`restore` lädt archivierte `.eml` wieder auf einen IMAP-Server — etwa nach einem Anbieterwechsel. Wiederholbar: Was schon dort liegt, wird erkannt und nicht doppelt angelegt.

## Gut zu wissen

- **Nie überschreiben.** Eine Namenskollision wird zu `…-2`; byte-gleiche Mails werden zusammengelegt statt kopiert. Dateien sind schreibgeschützt (`0400`), Ordner privat (`0700`).
- **Uhrzeiten sind Ortszeit** des Rechners, mit der am Tag der Mail gültigen Sommer-/Winterzeit — im Dateinamen wie im Kopfblock, der die Abweichung dazu nennt (`19.10.26, 11:32 (UTC+2)`).
- **Ein Lauf zur Zeit.** Startet ein zweiter (`render`, `all`, `eml` — etwa per Rechtsklick während eines geplanten Laufs), wartet er.
- **Namen sind keine Zuordnung.** `.eml` und PDF heißen meist gleich, müssen es aber nicht (Kollisionen, umbenannte Profile). Maßgeblich ist der Fingerabdruck in der PDF.
- **PDF/A-3b** wird mit veraPDF geprüft und im Lauf gemeldet. Eine PDF, die wegen eines exotischen Anhangs nicht konform ist, bleibt trotzdem erhalten.
- **Kein Nachladen** bei `render`, solange das Profil es nicht erlaubt: Externe Ressourcen und Tracking-Pixel werden vor dem Rendern entfernt.
- **Lange Läufe:** `-v` zeigt jede Mail, `-vv` Details, `-Q` schweigt; `--log-file` schreibt in jedem Fall mit. Schlägt eine Mail fehl, steht sie mit Namen in der Zusammenfassung, und der Lauf geht weiter.

## Archiv mit neuer Version neu rendern

`render` überspringt jede Mail, die schon eine PDF hat. Wer ältere PDFs mit einer neueren Version neu erzeugen will, räumt sie vorher beiseite — aber nur die, deren `.eml` nachweislich noch da ist. Dafür gibt es drei Schritte (benötigen `uv` und `pdftotext`):

```bash
./scripts/rerender-list.sh ~ ~/rerender                 # 1. nur lesend: was kann neu entstehen?
./scripts/rerender-apply.sh ~/rerender/rerender.txt --move-to ~/imapArc/pdf-Löscharchiv
bash ~/rerender/collect.sh                              # 2. lose .eml nach ~/imapArc/emlinput
bash ~/rerender/render.sh                               # 3. alles neu rendern
```

- **`rerender-list.sh`** findet alle PDFs von imapArc unter dem angegebenen Ordner und prüft für jede über den Fingerabdruck, ob ihre `.eml` vorliegt (bei sehr alten Ordnern ohne Fingerabdruck über Message-ID **und** den Text der PDF). `rerender.txt` enthält, was sicher neu entstehen kann, `keep.txt` den Rest mit Grund, `loose.txt` jede `.eml` außerhalb des Archivs.
- **`rerender-apply.sh`** prüft die Liste erneut, fragt nach und verschiebt (`--move-to`, umkehrbar) oder löscht (`--delete`).
- **`collect.sh`** sammelt lose `.eml` in `~/imapArc/emlinput`; `emlinput.log` hält fest, woher jede kam. Dort lässt sich vor dem Rendern aussortieren.
- **`render.sh`** ruft `imaparc render` für die Profile und `imaparc eml` für den Rest auf.

`eml-Archiv/`, `emlinput/` und `pdf-Löscharchiv/` werden nie durchsucht. Alle Schritte lassen sich nach einem Abbruch erneut starten.

## Befehle und Optionen

| Befehl | Option | Beschreibung |
|--------|--------|--------------|
| global | `--version`, `-V` / `--help` | Version bzw. Hilfe anzeigen (`imaparc` ohne Befehl zeigt die Hilfe) |
| `all` | *(alle `fetch`- und `render`-Optionen)* | Abholen, dann rendern |
| `fetch` | `--profile <name>` | Nur dieses Profil |
| `fetch` | `--dry-run` | Nur anzeigen, was archiviert würde und was auf dem Server geschähe |
| `fetch` | `--no-server-actions` | Archivieren, aber nie labeln, verschieben oder löschen |
| `fetch` | `--env`, `--profiles`, `--state <pfad>` | Abweichende Konfigurations- und Statusdateien |
| `render` | `--profile <name>` | Nur dieses Profil |
| `render` | `--allow-remote-images` | Externe Bilder erzwingen (sonst je Profil `remote_images`) |
| `render` | `--jobs`, `-j` | Parallele Renderings |
| `eml` | `[PFADE...]` | Dateien oder Verzeichnisse (Default: aktuelles Verzeichnis) |
| `eml` | `--profiles <pfad>` | `profile.yaml`, deren Regeln das Profil bestimmen |
| `eml` | `--root <pfad>` | Archiv-Wurzel für `00_Mails` (Default `~/imapArc`) |
| `eml` | `--name`, `-n` | Namensteil für Mails ohne passendes Profil (Default `mail`) |
| `eml` | `--no-remote-images` | Keine externen Bilder laden |
| `eml` | `--jobs`, `-j` | Parallele Renderings |
| `mailonly` | `PDFS...` | PDFs, aus denen `<name>_mailonly.pdf` entsteht |
| `mailonly` | `--output`, `-o` | Zielverzeichnis (Default: aktuelles Verzeichnis) |
| `mailonly` | `--keep-embedded` | Eingebettete `.eml` und Anhänge behalten |
| `extract` | `PDFS...` | PDFs, aus denen die eingebettete `.eml` geholt wird |
| `extract` | `--output`, `-o` | Zielverzeichnis (Default: aktuelles Verzeichnis) |
| `extract` | `--all` | Auch die eingebetteten Anhänge herausholen |
| `verify` | `--profile <name>` | Nur dieses Archiv prüfen |
| `verify` | `--deep` | Auch jede PDF öffnen und jede `.eml` vergleichen |
| `verify` | `--verbose`, `-v` | Jeden Befund einzeln statt zusammengefasst |
| `doctor` | `--offline` | IMAP-Logins überspringen |
| `restore` | `[PFADE...]` | Archivierte `.eml`-Dateien oder Verzeichnisse |
| `restore` | `--account <name>` | Konto aus der `.env` (nötig bei mehreren) |
| `restore` | `--folder <name>` | Zielordner, wird angelegt (Default `INBOX`) |
| `restore` | `--dry-run` | Nur zeigen, was hochgeladen würde |
| `init` | `--force` | Vorhandene Konfigurationsdateien überschreiben |
| `add-profile` | `NAME`, `--output <pfad>` | Neues Profil anhängen (Default-Ziel `~/imapArc/<name>`) |
| `list-profiles` | | Profile als Tabelle |
| `sync-profiles` | `--yes`, `-y` | `profile.yaml` ins Vollformat überführen (mit Backup) |
| `reset` | `--profile <name>`, `--yes` | Abhol-Status löschen — der nächste `fetch` bewertet alles neu |
| `install-service` | `--name`, `-n` | Rechtsklick-Eintrag einrichten |
| `all` / `fetch` / `render` / `eml` | `--log-file <pfad>` | Log zusätzlich in diese Datei — **auch bei `-Q`** |
| `all` / `fetch` / `render` / `eml` | `-Q` / `-v` / `-vv` | Still / ausführlich / Debug |

## Entwicklung

```bash
git clone https://github.com/jcmx9/imapArc.git
cd imapArc
uv sync
uv run playwright install chromium

uv run pytest
uv run ruff check --fix . && uv run ruff format .
uv run mypy src/
```

Die IMAP-Tests brauchen einen lokalen GreenMail-Server und überspringen sich sonst; Einzelheiten und die Architektur stehen in [`CLAUDE.md`](CLAUDE.md).

## Versionierung

[CalVer](https://calver.org/) im Format `YY.M.MICRO`, verwaltet mit [bump-my-version](https://github.com/callowayproject/bump-my-version).

## Changelog

Siehe [CHANGELOG.md](CHANGELOG.md).

## Lizenz

MIT – siehe [LICENSE](LICENSE).
