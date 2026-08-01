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
            git checkout dev
            git pull origin dev
            # month and micro are alternatives, never both: bumping the month
            # already resets micro to 0, so running micro afterwards would
            # publish 26.8.1 and silently skip 26.8.0 entirely.
            if [[ "${2:-}" == "--new-month" ]]; then
                bump-my-version bump month
            else
                bump-my-version bump micro
            fi
            git push origin dev --follow-tags
            echo "Tag pushed — the release workflow builds it and creates the"
            echo "GitHub release. Remaining: fast-forward main to dev and push"
            echo "  git branch -f main dev && git push origin main"
            ;;
        *)
            usage
            ;;
    esac
}

main "$@"
