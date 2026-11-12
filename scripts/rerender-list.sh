#!/usr/bin/env bash
set -euo pipefail

# List the PDF folders of an imapArc archive that can be rendered again.
#
# Usage:
#   ./scripts/rerender-list.sh ARCHIVE_ROOT [OUT_DIR]
#
# ARCHIVE_ROOT holds one directory per profile, each with eml/ and pdf/
# (e.g. ~/imapArc). OUT_DIR (default: current directory) receives:
#
#   rerender.txt  one PDF folder per line whose mail provably still exists as
#                 .eml in the same profile — `imaparc render` can rebuild it
#   keep.txt      REASON<TAB>FOLDER for every folder that must stay:
#                   no-manifest       no readable .imaparc-manifest
#                   no-eml            no .eml with this hash / Message-ID
#                   no-pdf-text       PDF missing or without a text layer
#                   content-mismatch  the PDF does not show the candidate .eml
#
# Proof is the sha256 in the manifest or, for manifests older than 26.8.12, the
# PDF's own text matching the .eml (see scripts/rerender_check.py). Read-only:
# nothing under ARCHIVE_ROOT is written. Needs uv and pdftotext (poppler).

usage() {
    grep '^#' "$0" | sed -n '3,23p' | cut -c3-
    exit 2
}

main() {
    [[ $# -ge 1 && $# -le 2 ]] || usage
    local root="$1"
    local out_dir="${2:-.}"
    [[ -d "${root}" ]] || { echo "Error: not a directory: ${root}" >&2; exit 1; }
    command -v pdftotext >/dev/null || { echo "Error: pdftotext not found" >&2; exit 1; }

    local repo
    repo="$(cd "$(dirname "$0")/.." && pwd -P)"
    uv run --quiet --project "${repo}" python "${repo}/scripts/rerender_check.py" \
        list "${root}" "${out_dir}"
}

main "$@"
