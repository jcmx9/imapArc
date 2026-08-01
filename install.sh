#!/usr/bin/env bash
set -euo pipefail

# One-shot installer for imapArc on macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/jcmx9/imapArc/main/install.sh | bash
#
# Installs, in this order, skipping whatever is already present:
#   uv, Ghostscript, qpdf, veraPDF, imapArc itself, Chromium, and (optionally)
#   the Finder right-click action.
#
# Options (as arguments, or as environment variables):
#   --no-service     do not install the Finder Quick Action
#   --no-init        do not create ~/.config/imaparc
#   IMAPARC_REF=…    install from a branch/tag instead of main

REPO="https://github.com/jcmx9/imapArc.git"
REF="${IMAPARC_REF:-}"
VERAPDF_INSTALLER="https://software.verapdf.org/rel/verapdf-installer.zip"
WITH_SERVICE=1
WITH_INIT=1

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror\033[0m %s\n' "$*" >&2; exit 1; }

parse_args() {
    for arg in "$@"; do
        case "${arg}" in
            --no-service) WITH_SERVICE=0 ;;
            --no-init)    WITH_INIT=0 ;;
            *) die "unknown option: ${arg}" ;;
        esac
    done
}

require_macos() {
    [[ "$(uname -s)" == "Darwin" ]] ||
        die "this installer targets macOS; on Linux install uv, ghostscript, qpdf and veraPDF with your package manager, then: uv tool install git+${REPO}"
}

ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        log "uv is present"
        return
    fi
    log "installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # The installer writes here; make it visible to the rest of this script.
    export PATH="${HOME}/.local/bin:${PATH}"
    command -v uv >/dev/null 2>&1 || die "uv installation did not put uv on PATH"
}

ensure_brew_tools() {
    local missing=()
    for tool in gs qpdf; do
        command -v "${tool}" >/dev/null 2>&1 || missing+=("${tool}")
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
        log "Ghostscript and qpdf are present"
        return
    fi
    command -v brew >/dev/null 2>&1 ||
        die "Homebrew is required to install ${missing[*]} — see https://brew.sh"
    log "installing ${missing[*]} via Homebrew"
    # Formula names differ from the binary names.
    local formulae=()
    for tool in "${missing[@]}"; do
        if [[ "${tool}" == "gs" ]]; then
            formulae+=("ghostscript")
        else
            formulae+=("${tool}")
        fi
    done
    brew install "${formulae[@]}"
}

ensure_verapdf() {
    if command -v verapdf >/dev/null 2>&1; then
        log "veraPDF is present"
        return
    fi
    if command -v brew >/dev/null 2>&1 && brew install verapdf 2>/dev/null; then
        log "veraPDF installed via Homebrew"
        return
    fi
    # veraPDF is not in Homebrew core; fall back to its official IzPack
    # installer, driven by an auto-install profile (same approach as CI).
    log "installing veraPDF from software.verapdf.org"
    local tmp install_dir installer
    tmp="$(mktemp -d)"
    install_dir="${HOME}/verapdf"
    trap 'rm -rf "${tmp}"' RETURN
    curl -sL "${VERAPDF_INSTALLER}" -o "${tmp}/verapdf.zip"
    unzip -q "${tmp}/verapdf.zip" -d "${tmp}"
    installer="$(find "${tmp}" -maxdepth 1 -type d -name 'verapdf-greenfield-*' | head -1)"
    [[ -n "${installer}" ]] || die "unexpected veraPDF archive layout"
    cat > "${tmp}/auto-install.xml" <<XML
<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<AutomatedInstallation langpack="eng">
  <com.izforge.izpack.panels.htmlhello.HTMLHelloPanel id="welcome"/>
  <com.izforge.izpack.panels.target.TargetPanel id="install_dir">
    <installpath>${install_dir}</installpath>
  </com.izforge.izpack.panels.target.TargetPanel>
  <com.izforge.izpack.panels.packs.PacksPanel id="sdk_pack_select">
    <pack index="0" name="veraPDF GUI" selected="true"/>
    <pack index="1" name="veraPDF Batch files" selected="true"/>
    <pack index="2" name="veraPDF Validation model" selected="false"/>
    <pack index="3" name="veraPDF Documentation" selected="false"/>
    <pack index="4" name="veraPDF Sample Plugins" selected="false"/>
  </com.izforge.izpack.panels.packs.PacksPanel>
  <com.izforge.izpack.panels.install.InstallPanel id="install"/>
  <com.izforge.izpack.panels.finish.FinishPanel id="finish"/>
</AutomatedInstallation>
XML
    "${installer}/verapdf-install" "${tmp}/auto-install.xml"
    export PATH="${install_dir}:${PATH}"
    command -v verapdf >/dev/null 2>&1 ||
        warn "veraPDF installed to ${install_dir} — add that to your PATH"
}

install_imaparc() {
    local target="git+${REPO}"
    [[ -n "${REF}" ]] && target="${target}@${REF}"
    log "installing imapArc (${REF:-main})"
    uv tool install --force "${target}"
}

ensure_chromium() {
    # uv tool install exposes only imapArc's own entry points, so there is no
    # `playwright` command — go through the tool environment's interpreter,
    # which also pins the browser build to what this imapArc expects.
    log "installing the Chromium build imapArc renders with"
    "$(uv tool dir)/imaparc/bin/python" -m playwright install chromium
}

main() {
    parse_args "$@"
    require_macos
    ensure_uv
    ensure_brew_tools
    ensure_verapdf
    install_imaparc
    ensure_chromium

    if [[ ${WITH_INIT} -eq 1 ]]; then
        log "creating the config (existing files are kept)"
        imaparc init
    fi
    if [[ ${WITH_SERVICE} -eq 1 ]]; then
        log "installing the Finder right-click action"
        imaparc install-service
    fi

    printf '\n'
    log "done — imapArc $(imaparc --version | awk '{print $2}') is ready"
    cat <<'NEXT'

  Einzelne Mails archivieren (kein Setup nötig):
      Mail aus dem Mailprogramm in einen Ordner ziehen, dann
      Rechtsklick → Dienste → „Mit imapArc archivieren"
    oder im Terminal:
      imaparc eml ~/Desktop

  Postfächer automatisch archivieren:
      $EDITOR ~/.config/imaparc/.env          # IMAP-Konto eintragen
      $EDITOR ~/.config/imaparc/profile.yaml  # Profile anpassen
      imaparc all
NEXT
}

main "$@"
