# imapArc

> Archiviert IMAP-Postfächer profilgesteuert als lesbare .eml-Dateien und rendert sie optional zu durchsuchbarem PDF/A.

[Sprache: **Deutsch** | [English](README.en.md)] · Version `26.8.12`

[![CI](https://github.com/jcmx9/imapArc/actions/workflows/ci.yml/badge.svg)](https://github.com/jcmx9/imapArc/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

## Beschreibung

`imapArc` sammelt E-Mails aus einem oder mehreren IMAP-Konten profilgesteuert ein und legt sie als **`.eml`-Dateien mit lesbaren Namen** ab (identisch zum späteren PDF). Die Umwandlung zu PDF ist ein davon **entkoppelter, optionaler** zweiter Schritt.

Leitprinzip: **imapArc bewahrt, es verändert nicht.** Die vollständige Rohmail liegt verlustfrei als `.eml` vor; das PDF ist eine abgeleitete, lesbare Repräsentation. Originale werden nie umgewandelt — sie liegen unverändert daneben.

### Wichtigste Merkmale

- **Zwei entkoppelte Phasen:** `fetch` (IMAP → `.eml`, billig, idempotent) und `render` (`.eml` → PDF, optional, wiederholbar).
- **Lesbare `.eml`-Dateien** mit demselben Namen wie das PDF; in jedem Mailprogramm einzeln zu öffnen.
- **Echtes Browser-Rendering** über Chromium — auch komplexes Newsletter-HTML originalgetreu, bis zu drei Fassungen je Mail (umgebrochen, Nur-Text sowie eine skalierte Ein-Seiten-Übersicht bei mehrseitigen Mails).
- **Kein Nachladen externer Ressourcen** — Tracking-Pixel werden vor dem Rendern entfernt. (Ausnahme: `imaparc eml` lädt sie bewusst, siehe dort.)
- **Schützendes, privates Archiv** — Dateien `0400` (read-only Inhalt), Verzeichnisse `0700` (privat, aber vom Besitzer verwaltbar), **nie überschreiben** (Kollision → `…-2`).

## Voraussetzungen

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/)
- Ghostscript, qpdf, veraPDF — nur für `render` (z. B. `brew install ghostscript qpdf verapdf`)
- Git

## Installation

**Der einfache Weg (macOS und Linux):** ein Befehl installiert alles — uv, Ghostscript, qpdf, veraPDF, imapArc, Chromium — und richtet den Finder-Rechtsklick ein:

```bash
curl -fsSL https://raw.githubusercontent.com/jcmx9/imapArc/main/install.sh | bash
```

Vorhandenes wird übersprungen, nichts doppelt installiert. Optionen: `--no-service` (ohne Rechtsklick-Eintrag), `--no-init` (ohne Konfiguration anzulegen).

Danach steht ein Eintrag im Dateimanager bereit, der auf markierte `.eml`-Dateien *und* ganze Ordner wirkt — auch auf mehrere gleichzeitig: unter macOS als **Rechtsklick → Dienste → „Mit imapArc archivieren"**, unter Linux als **Rechtsklick → Öffnen mit** (Nautilus, Dolphin, Thunar). Nachträglich einrichten oder erneuern:

```bash
imaparc install-service
```

### Manuelle Installation

Aus dem lokalen Git-Repository als eigenständiges uv-Tool:

```bash
git clone https://github.com/jcmx9/imapArc.git
cd imapArc
uv tool install .
```

Oder direkt von GitHub, ohne Klon:

```bash
uv tool install git+https://github.com/jcmx9/imapArc.git
```

`uv tool install` legt die ausführbare Datei nach `~/.local/bin`. Liegt das noch nicht im `PATH`:

```bash
uv tool update-shell        # trägt ~/.local/bin dauerhaft in die Shell ein
```

Für die `render`-Phase zusätzlich einmalig den Browser installieren:

```bash
"$(uv tool dir)/imaparc/bin/python" -m playwright install chromium
```

Der Umweg über den Interpreter ist nötig, weil `uv tool install` nur die ausführbaren Dateien von imapArc selbst nach `~/.local/bin` legt, nicht die seiner Abhängigkeiten — ein blankes `playwright` gibt es also nicht. Der Pfad stellt außerdem sicher, dass genau die Chromium-Version geladen wird, die das installierte imapArc erwartet.

## Erste Einrichtung (Bootstrap)

`imaparc init` legt das zentrale Konfigurationsverzeichnis mit Vorlagen an
(`.env` mit `0600`, Verzeichnis `0700`); vorhandene Dateien werden nie
überschrieben:

```bash
imaparc init                          # legt ~/.config/imaparc/{.env,profile.yaml} an
$EDITOR ~/.config/imaparc/.env        # dein IMAP-Konto eintragen
$EDITOR ~/.config/imaparc/profile.yaml   # Profile/Zielverzeichnisse anpassen
imaparc fetch                         # loslegen
```

Kompletter Bootstrap von GitHub in einer Zeile:

```bash
uv tool install git+https://github.com/jcmx9/imapArc.git && imaparc init
```

## Update

```bash
uv tool install --force .                                    # aus dem lokalen Klon
uv tool install --force git+https://github.com/jcmx9/imapArc.git   # von GitHub
```

## Deinstallation

```bash
uv tool uninstall imaparc
```

## Konfiguration

imapArc liest standardmäßig aus einem zentralen Verzeichnis (XDG-konform):

| Datei | Zweck |
|-------|-------|
| `~/.config/imaparc/.env` | IMAP-Konten (Zugangsdaten) |
| `~/.config/imaparc/profile.yaml` | Profile: Match-Regeln + Zielverzeichnisse |
| `~/.local/state/imaparc/state.db` | bereits zugestellte Mails (Dedup, automatisch) |

**`.env`** — ein oder mehrere Konten im Format `IMAP_<KONTO>_<FELD>`:

```dotenv
IMAP_PRIVAT_HOST=imap.example.com
IMAP_PRIVAT_USER=you@example.com
IMAP_PRIVAT_PASSWORD=your-password
IMAP_PRIVAT_SSL=true
```

`imaparc init` legt diese Datei als Vorlage an (`0600`); trage dort dein Konto ein.

**`profile.yaml`** — jedes Profil definiert eine Konversation. Unter `output` entstehen immer `eml/` und (bei `pdf: true`) `pdf/`:

```yaml
profiles:
  - name: hetzner
    account: privat                # verweist auf IMAP_PRIVAT_* in .env
    match:                         # alle gesetzten Regeln müssen zutreffen (AND)
      domains: ['@hetzner.com']    # optional: Domain(s), mit führendem @
      # addresses: [billing@hetzner.com]  # optional: exakte Adresse(n)
      # mode: [from, to, cc, bcc]  # optional: welche Felder durchsucht werden (Default alle)
      subject: ['*Rechnung*']      # optional: Regex ODER Wildcard-Liste
      attachments: [pdf]           # optional: nur Mails mit Anhang dieses Typs
      folders: [INBOX]             # optional: IMAP-Ordner (Default INBOX)
      recursive: false             # optional: auch Unterordner (Default false)
      trash: false                 # optional: Papierkorb bei rekursivem Scan einbeziehen (Default false)
      since: 2026-01-01            # optional: Mails davor ignorieren
      # until: 2026-12-31          # optional: Mails danach ignorieren
    output: ~/Archiv/Hetzner       # Ziel; darunter eml/ und ggf. pdf/
    pdf: true                      # optional: auch PDFs rendern (Default false)
    # remote_images: false       # externe Bilder beim Rendern laden (CLI erzwingt)
    # jobs: 4                    # parallele Renderings (CLI --jobs überschreibt)
    # gs_jobs: 2                 # parallele Ghostscript-Läufe (begrenzt den Speicher)
    # filename_pattern: '{date}_{profile}_{subject}'   # Namensschema
    # date_format: YYYY-MM-DD_hh-mm-ss                 # Tokens für {date}
    # max_attachment_bytes: 419430400   # größere Anhänge nicht konvertieren
    # attachment_timeout_s: 120         # Zeitlimit je Anhang
    # render_timeout_ms: 30000          # Zeitlimit je Mail-Body
    after_fetch:                   # optional; erst nach gesicherter Ablage
      label: Archiviert            #   IMAP-Keyword setzen
      move_to: Archiv/Erledigt     #   verschieben (exklusiv zu delete)
      # delete: true               #   löschen (exklusiv zu move_to)
```

Vollständige Referenz mit allen Parametern und Defaults: [`profile.example.yaml`](profile.example.yaml).

## Verwendung

Alles wird über `profile.yaml` gesteuert. `imaparc` ohne Befehl zeigt die Hilfe:

```bash
imaparc all              # voller Durchlauf ALLER Profile: fetch, dann render (nur pdf: true)
imaparc fetch            # nur einsammeln (IMAP → .eml); pdf-Flag egal
imaparc fetch --dry-run  # nur zeigen, was passieren würde — schreibt nichts
imaparc render           # nur rendern — ALLE Profile (pdf-Flag wird ignoriert)
imaparc init             # zentrale Config anlegen
imaparc add-profile NAME # ein neues, voll dokumentiertes Profil an profile.yaml anhängen
imaparc list-profiles    # definierte Profile als Tabelle anzeigen (Regeln, Ziel, Aktion)
imaparc sync-profiles    # bestehende profile.yaml ins Vollformat überführen (Backup)
imaparc reset            # Zustell-Status löschen → nächster fetch verarbeitet alles neu
imaparc eml              # lose .eml-Dateien rendern, ganz ohne Profil
imaparc install-service  # Finder-Rechtsklick „Mit imapArc archivieren" einrichten
imaparc doctor           # Installation prüfen: Werkzeuge, Browser, Config, Login
imaparc verify           # Archiv prüfen: Duplikate, Schäden, Reste
imaparc restore <pfad>   # archivierte .eml zurück auf den IMAP-Server laden

imaparc all --profile hetzner    # jeden Modus auf ein Profil einschränken
imaparc fetch --profile hetzner
imaparc render --profile hetzner

imaparc --version
imaparc --help           # (auch: imaparc ohne Argument)
```

`imaparc render` braucht kein `.env` (kein IMAP-Zugriff). Jeder `fetch`-Lauf bewertet alle Mails der konfigurierten Ordner neu gegen die **aktuellen** Profile; bereits zugestellte werden übersprungen. Änderst du ein Profil oder fügst eins hinzu, greift es automatisch auf alte Mails — kein State-Zurücksetzen nötig. Ein zweiter `render`-Lauf überspringt bereits erzeugte Mails.

### Einzelne Mails ohne Profil: `imaparc eml`

Zieht man eine Mail aus Apple Mail, Thunderbird oder Outlook in den Finder, entsteht eine `.eml`-Datei. `imaparc eml` rendert solche Dateien dort, wo sie liegen — ohne Profil, ohne `.env`, ohne Serverkontakt:

```bash
imaparc eml                        # alle .eml im aktuellen Verzeichnis
imaparc eml ~/Desktop              # alle .eml dort
imaparc eml a.eml b.eml            # genau diese beiden
imaparc eml --name hetzner *.eml   # eigener Namensteil statt "mail"
```

Aus jeder `.eml` wird ein Ordner daneben, der die Mail vollständig enthält — und die `.eml` wandert anschließend hinein:

```
2026-07-01_10-30-00_mail_Rechnung_Juli_2026/
├── 2026-07-01_10-30-00_mail_Rechnung_Juli_2026.pdf           # Mail + Anhangseiten
├── 2026-07-01_10-30-00_mail_Rechnung_Juli_2026_mailonly.pdf  # nur die Mail
├── 2026-07-01_10-30-00_mail_Rechnung_Juli_2026.eml           # das Original
└── rechnung.pdf                                              # Anhang im Original
```

Ein Verzeichnisargument nimmt nur die `.eml` **direkt darin**, nicht in Unterordnern — die bereits einsortierten Mails werden also nie erneut eingesammelt. Ein zweiter Lauf ist damit folgenlos, und ein zwischendrin abgebrochener Lauf repariert sich beim nächsten Aufruf von selbst.

Dieser Befehl arbeitet getrennt vom Archiv: er liest keine `profile.yaml`, kontaktiert keinen IMAP-Server und schreibt nichts in die Zustell-Datenbank. Ein späteres `imaparc fetch` verhält sich exakt so, als hätte es ihn nie gegeben.

> **Externe Bilder werden hier immer geladen** — anders als bei den profilgesteuerten Befehlen, wo das ausdrücklich eingeschaltet werden muss. Eine von Hand herausgezogene Mail soll so aussehen, wie der Absender sie gesetzt hat, und sie war im Mailprogramm ohnehin schon geöffnet. Der Preis: Beim Rendern werden auch Tracking-Pixel abgerufen, der Absender erfährt also Zeitpunkt und IP-Adresse der Verarbeitung. Wer das nicht will, legt die Mail stattdessen in ein Profil-Archiv und nutzt `imaparc render`.

## Optionen

| Befehl | Option | Beschreibung |
|--------|--------|--------------|
| global | `--version`, `-V` | Version anzeigen und beenden |
| global | `--help` | Hilfe anzeigen |
| `all` | *(alle `fetch`- und `render`-Optionen)* | Voller Lauf: fetch, dann render |
| `init` | `--force` | Vorhandene Config-Dateien überschreiben |
| `add-profile` | `NAME` | Name des neuen Profils (Argument) |
| `add-profile` | `--profiles <pfad>` | `profile.yaml` (Default `~/.config/imaparc/profile.yaml`) |
| `add-profile` | `--output <pfad>` | Zielverzeichnis (Default `~/imapArc/<name>`) |
| `list-profiles` | `--profiles <pfad>` | `profile.yaml` (Default `~/.config/imaparc/profile.yaml`) |
| `sync-profiles` | `--profiles <pfad>` | `profile.yaml` (Default `~/.config/imaparc/profile.yaml`) |
| `sync-profiles` | `--yes`, `-y` | Ohne Rückfrage neu schreiben |
| `reset` | `--state <pfad>` | SQLite-Statusdatei |
| `reset` | `--profile <name>` | Nur den Status dieses Profils löschen |
| `reset` | `--yes`, `-y` | Zustell-Status ohne Rückfrage löschen |
| `fetch` | `--env <pfad>` | `.env` mit Konten (Default `~/.config/imaparc/.env`) |
| `fetch` | `--profiles <pfad>` | `profile.yaml` (Default `~/.config/imaparc/profile.yaml`) |
| `fetch` | `--profile <name>` | Nur dieses eine Profil ausführen |
| `fetch` | `--state <pfad>` | SQLite-Statusdatei |
| `fetch` | `--dry-run` | Nur anzeigen, was archiviert würde und was auf dem Server geschähe; schreibt nichts |
| `fetch` | `--no-server-actions` | Normal archivieren, aber nie labeln, verschieben oder löschen |
| `render` | `--profiles <pfad>` | `profile.yaml` (Default `~/.config/imaparc/profile.yaml`) |
| `render` | `--profile <name>` | Nur dieses Profil rendern |
| `render` | `--allow-remote-images` | Externe Bilder erzwingen (sonst pro Profil `remote_images`) |
| `render` | `--jobs`, `-j` | Parallele Renderings |
| `eml` | `[PFADE...]` | Dateien oder Verzeichnisse (Default: aktuelles Verzeichnis) |
| `eml` | `--name`, `-n` | Namensteil in den erzeugten Dateinamen (Default `mail`) |
| `eml` | `--jobs`, `-j` | Parallele Renderings |
| `install-service` | `--name`, `-n` | Namensteil, den der Rechtsklick-Eintrag verwendet |
| `doctor` | `--offline` | IMAP-Logins überspringen (kein Netzwerkzugriff) |
| `verify` | `--profiles <pfad>` | `profile.yaml` mit den zu prüfenden Archiven |
| `verify` | `--profile <name>` | Nur dieses eine Archiv prüfen |
| `restore` | `[PFADE...]` | Archivierte `.eml`-Dateien oder Verzeichnisse |
| `restore` | `--account <name>` | Konto aus der `.env` (nötig bei mehreren) |
| `restore` | `--folder <name>` | Zielordner, wird angelegt (Default `INBOX`) |
| `restore` | `--dry-run` | Nur zeigen, was hochgeladen würde |
| `doctor` | `--env`, `--profiles` | Zu prüfende Konfigurationsdateien |
| `all` / `fetch` / `render` / `eml` | `--log-file <pfad>` | Log zusätzlich in diese Datei — **auch bei `-Q`** |
| `all` / `fetch` / `render` / `eml` | `-Q` / `-v` / `-vv` | Silent / Verbose / Debug |

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

## Versionierung

[CalVer](https://calver.org/) im Format `YY.M.MICRO`, verwaltet mit [bump-my-version](https://github.com/callowayproject/bump-my-version).

## Changelog

Siehe [CHANGELOG.md](CHANGELOG.md).

## Lizenz

MIT – siehe [LICENSE](LICENSE).
