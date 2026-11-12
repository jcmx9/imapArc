#!/usr/bin/env bash
set -euo pipefail

# List the imapArc PDF folders under a directory tree that can be rendered again.
#
# Usage:
#   ./scripts/rerender-list.sh ROOT OUT_DIR [--exclude DIR]... [--config FILE]
#
# ROOT is searched for imapArc archives (e.g. ~ or ~/imapArc): profile
# archives (<profile>/eml + <profile>/pdf) and folders written by `imaparc eml`
# (<dir>/<mail>/ with the .eml inside). Library/, .Trash and dot-directories are
# skipped, as is every --exclude DIR (e.g. a previous --move-to target).
# --config names the profile.yaml whose archives `imaparc render` already
# covers (default ~/.config/imaparc/profile.yaml).
#
# OUT_DIR receives:
#   rerender.txt   one folder per line whose mail provably still exists as .eml
#   keep.txt       REASON<TAB>FOLDER for every folder that must stay:
#                    no-eml            no .eml with this hash / Message-ID
#                    no-pdf-text       PDF missing or without a text layer
#                    content-mismatch  the PDF does not show the candidate .eml
#   archives.tsv   the archives found
#   profiles.yaml  render profiles for archives no profile.yaml covers
#
# Proof is the sha256 in the manifest or, for manifests older than 26.8.12, the
# PDF's own text matching the .eml (see scripts/rerender_check.py). Read-only:
# nothing outside OUT_DIR is written. Needs uv and pdftotext (poppler).

usage() {
    grep '^#' "$0" | sed -n '3,28p' | cut -c3-
    exit 2
}

main() {
    [[ $# -ge 2 ]] || usage
    [[ -d "$1" ]] || { echo "Error: not a directory: $1" >&2; exit 1; }
    command -v pdftotext >/dev/null || { echo "Error: pdftotext not found" >&2; exit 1; }

    local repo
    repo="$(cd "$(dirname "$0")/.." && pwd -P)"
    uv run --quiet --project "${repo}" python "${repo}/scripts/rerender_check.py" \
        list "$@"
}

main "$@"
