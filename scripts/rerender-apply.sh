#!/usr/bin/env bash
set -euo pipefail

# Move or delete the PDF folders listed by rerender-list.sh.
#
# Usage:
#   ./scripts/rerender-apply.sh LIST --move-to DIR [--yes]
#   ./scripts/rerender-apply.sh LIST --delete [--yes]
#
# LIST is the rerender.txt written by rerender-list.sh. --move-to puts each
# folder under DIR at its path relative to $HOME (reversible; DIR must not lie
# inside an archive); --delete removes it. Without --yes the count is shown and
# "yes" must be typed.
#
# The list is checked again before anything is touched — it may be stale, or
# edited by hand. Only folders whose mail still provably exists as .eml are
# acted on; every other line is skipped and reported, and the exit code is 1.
# A folder written by `imaparc eml` holds its own .eml: that file is first moved
# out beside the folder, so it survives and can be rendered again.
#
# Afterwards run the render.sh written next to LIST: it holds exactly the
# imaparc calls that rebuild what was removed.

usage() {
    grep '^#' "$0" | sed -n '3,22p' | cut -c3-
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

# The directory an entry's archive occupies: ARCHIVE for ARCHIVE/<profile>/pdf/
# <mail>, and the directory holding the folder for an `imaparc eml` folder.
archive_of() {
    local kind="$1" folder="$2"
    if [[ "${kind}" == "profile" ]]; then
        (cd "${folder}/../../.." && pwd -P)
    else
        (cd "${folder}/.." && pwd -P)
    fi
}

# Where --move-to puts a folder: its path below $HOME, or below / otherwise.
move_destination() {
    local folder="$1" home
    folder="$(cd "${folder}" && pwd -P)"
    home="$(cd "${HOME}" && pwd -P)"
    case "${folder}" in
        "${home}/"*) printf '%s/%s\n' "${target}" "${folder#"${home}/"}" ;;
        *) printf '%s/%s\n' "${target}" "${folder#/}" ;;
    esac
}

main() {
    local list="" mode="" assume_yes=false
    target=""
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

    local repo out_dir
    repo="$(cd "$(dirname "$0")/.." && pwd -P)"
    out_dir="$(cd "$(dirname "${list}")" && pwd -P)"
    # Global, not local: the EXIT trap runs after main() has returned.
    verified="$(mktemp)"
    trap 'rm -f "${verified}"' EXIT

    local skipped=0 kind folder eml
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
        # Checked before anything is created: a target inside an archive would
        # otherwise already have written a directory into it.
        target="$(resolve_path "${target}")"
        local archive
        while IFS=$'\t' read -r kind folder eml; do
            archive="$(archive_of "${kind}" "${folder}")"
            case "${target}/" in
                "${archive}/"*)
                    echo "Error: move target lies inside the archive ${archive}" >&2
                    exit 1 ;;
            esac
        done <"${verified}"
    fi

    if [[ "${assume_yes}" != true ]]; then
        if [[ "${mode}" == "move" ]]; then
            echo "Move ${total} folders below ${target}/ ?"
        else
            echo "DELETE ${total} folders permanently?"
        fi
        local answer
        read -r -p 'Type "yes" to continue: ' answer
        [[ "${answer}" == "yes" ]] || { echo "Aborted."; exit 1; }
    fi

    local applied="${out_dir}/applied.tsv" dest loose done_count=0
    : >"${applied}"
    while IFS=$'\t' read -r kind folder eml; do
        if [[ "${mode}" == "move" ]]; then
            dest="$(move_destination "${folder}")"
            if [[ -e "${dest}" ]]; then
                printf 'skipped (already at target): %s\n' "${folder}" >&2
                skipped=1
                continue
            fi
        fi
        if [[ "${kind}" == "eml" ]]; then
            # The folder holds the only copy of this .eml: move it out first.
            loose="$(dirname "${folder}")/$(basename "${folder}").eml"
            if [[ -e "${loose}" ]]; then
                printf 'skipped (%s already exists): %s\n' "${loose}" "${folder}" >&2
                skipped=1
                continue
            fi
            mv "${eml}" "${loose}"
            printf 'eml\t%s\n' "${loose}" >>"${applied}"
        else
            printf 'profile\t%s\n' "${folder}" >>"${applied}"
        fi
        if [[ "${mode}" == "move" ]]; then
            mkdir -p "$(dirname "${dest}")"
            mv "${folder}" "${dest}"
        else
            rm -rf "${folder}"
        fi
        done_count=$((done_count + 1))
    done <"${verified}"

    echo "${done_count} folders ${mode}d."
    uv run --quiet --project "${repo}" python "${repo}/scripts/rerender_check.py" \
        commands "${out_dir}" >/dev/null
    [[ "${skipped}" -eq 0 ]] || echo "Some lines were skipped, see above." >&2
    echo "Next: bash ${out_dir}/render.sh"
    [[ "${skipped}" -eq 0 ]] || exit 1
}

main "$@"
