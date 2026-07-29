#!/usr/bin/env bash
set -euo pipefail

# Release helper for imapArc (CalVer YY.M.MICRO via bump-my-version).
#
# Usage:
#   ./scripts/release.sh dev              Dev pre-release on dev branch (.devN)
#   ./scripts/release.sh prod             Merge dev -> main, tag, GitHub release
#   ./scripts/release.sh prod --new-month Bump the CalVer month first

usage() {
    grep '^#' "$0" | cut -c3-
    exit 1
}

main() {
    local mode="${1:-}"
    case "${mode}" in
        dev)
            git checkout dev
            git pull origin dev
            bump-my-version bump dev
            git push origin dev --follow-tags
            ;;
        prod)
            if [[ "${2:-}" == "--new-month" ]]; then
                bump-my-version bump month
            fi
            git checkout dev
            git pull origin dev
            bump-my-version bump micro
            git push origin dev --follow-tags
            echo "Tag pushed — the release workflow builds it and creates the"
            echo "GitHub release. Remaining manual step: open a PR dev -> main."
            ;;
        *)
            usage
            ;;
    esac
}

main "$@"
