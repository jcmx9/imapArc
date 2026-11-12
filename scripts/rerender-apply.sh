#!/usr/bin/env bash
set -euo pipefail

# Move or delete the PDF folders listed by rerender-list.sh.
#
# Usage:
#   ./scripts/rerender-apply.sh LIST --move-to DIR [--yes]
#   ./scripts/rerender-apply.sh LIST --delete [--yes]
#
# LIST is the rerender.txt written by rerender-list.sh. --move-to puts each
# folder under DIR/<profile>/ (reversible; DIR must lie outside the archive);
# --delete removes it. Without --yes the count is shown and "yes" must be typed.
#
# The list is checked again before anything is touched — it may be stale, or
# edited by hand. Only folders whose mail still provably exists as .eml are
# acted on; every other line is skipped and reported, and the exit code is 1.
#
# Afterwards run `imaparc render` to rebuild the removed folders from the .eml.

usage() {
    grep '^#' "$0" | sed -n '3,18p' | cut -c3-
    exit 2
}

# Print the physical absolute path of $1, which need not exist yet: the deepest
# existing ancestor is resolved (symlinks included), the rest appended as is.
resolve_path() {
    local path="$1" rest=""
    [[ "${path}" == /* ]] || path="${PWD}/${path}"
    path="${path%/}"
    while [[ ! -d "${path}" ]]; do
        rest="/$(basename "${path}")${rest}"
        path="$(dirname "${path}")"
    done
    printf '%s%s\n' "$(cd "${path}" && pwd -P)" "${rest}"
}

main() {
    local list="" mode="" target="" assume_yes=false
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --move-to)
                [[ $# -ge 2 ]] || usage
                mode="move"; target="$2"; shift 2 ;;
            --delete) mode="delete"; shift ;;
            --yes) assume_yes=true; shift ;;
            -*) usage ;;
            *) [[ -z "${list}" ]] || usage; list="$1"; shift ;;
        esac
    done
    [[ -n "${list}" && -n "${mode}" ]] || usage
    [[ -f "${list}" ]] || { echo "Error: list not found: ${list}" >&2; exit 1; }
    command -v pdftotext >/dev/null || { echo "Error: pdftotext not found" >&2; exit 1; }

    local repo
    repo="$(cd "$(dirname "$0")/.." && pwd -P)"
    # Global, not local: the EXIT trap runs after main() has returned.
    verified="$(mktemp)"
    trap 'rm -f "${verified}"' EXIT

    local skipped=0 folder
    echo "Re-checking ${list} ..."
    uv run --quiet --project "${repo}" python "${repo}/scripts/rerender_check.py" \
        recheck "${list}" >"${verified}" || skipped=1

    local total
    total="$(grep -c . "${verified}" || true)"
    if [[ "${total}" -eq 0 ]]; then
        echo "Nothing to do: no folder in ${list} passed the check."
        exit "${skipped}"
    fi

    if [[ "${mode}" == "move" ]]; then
        # Checked before anything is created: a target inside the archive would
        # otherwise already have written a directory into it. Each listed folder
        # is ARCHIVE/<profile>/pdf/<name>, so its archive is 3 levels up.
        target="$(resolve_path "${target}")"
        local archive
        while IFS= read -r folder; do
            archive="$(cd "${folder}/../../.." && pwd -P)"
            case "${target}/" in
                "${archive}/"*)
                    echo "Error: move target lies inside the archive ${archive}" >&2
                    exit 1 ;;
            esac
        done <"${verified}"
    fi

    if [[ "${assume_yes}" != true ]]; then
        if [[ "${mode}" == "move" ]]; then
            echo "Move ${total} folders to ${target}/<profile>/ ?"
        else
            echo "DELETE ${total} folders permanently?"
        fi
        local answer
        read -r -p 'Type "yes" to continue: ' answer
        [[ "${answer}" == "yes" ]] || { echo "Aborted."; exit 1; }
    fi

    local profile dest done_count=0
    while IFS= read -r folder; do
        if [[ "${mode}" == "move" ]]; then
            profile="$(basename "$(dirname "$(dirname "${folder}")")")"
            dest="${target}/${profile}/$(basename "${folder}")"
            if [[ -e "${dest}" ]]; then
                printf 'skipped (already at target): %s\n' "${folder}" >&2
                skipped=1
                continue
            fi
            mkdir -p "${target}/${profile}"
            mv "${folder}" "${dest}"
        else
            rm -rf "${folder}"
        fi
        done_count=$((done_count + 1))
    done <"${verified}"

    echo "${done_count} folders ${mode}d."
    [[ "${skipped}" -eq 0 ]] || { echo "Some lines were skipped, see above." >&2; exit 1; }
    echo "Next: imaparc render"
}

main "$@"
