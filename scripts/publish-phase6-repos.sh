#!/usr/bin/env bash
set -euo pipefail

# Publish local Phase 6 scaffold repos to GitHub.
#
# Defaults:
# - Local base dir: ~/bin
# - Org: lightning-goats
# - Repos: cl-hive-comms, cl-hive-archon
#
# By default this script is dry-run (prints actions only).
# Use --apply to execute commands.
#
# Examples:
#   ./scripts/publish-phase6-repos.sh
#   ./scripts/publish-phase6-repos.sh --apply --create-remote --push
#   ./scripts/publish-phase6-repos.sh --apply --org lightning-goats --base-dir /home/sat/bin --push

BASE_DIR="${HOME}/bin"
ORG="lightning-goats"
REPOS=("cl-hive-comms" "cl-hive-archon")
APPLY=0
CREATE_REMOTE=0
PUSH=0
PRIVATE=0

run_cmd() {
    if [[ ${APPLY} -eq 1 ]]; then
        "$@"
    else
        echo "[dry-run] $*"
    fi
}

usage() {
    cat <<EOF
Usage: $0 [options]

Options:
  --base-dir DIR      Local base directory (default: ~/bin)
  --org NAME          GitHub org/user (default: lightning-goats)
  --apply             Execute commands (default is dry-run)
  --create-remote     Create GitHub repos with gh CLI if missing
  --push              Push local main branch to origin
  --private           Create private repos (default public if --create-remote)
  -h, --help          Show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --base-dir)
            BASE_DIR="$2"
            shift 2
            ;;
        --org)
            ORG="$2"
            shift 2
            ;;
        --apply)
            APPLY=1
            shift
            ;;
        --create-remote)
            CREATE_REMOTE=1
            shift
            ;;
        --push)
            PUSH=1
            shift
            ;;
        --private)
            PRIVATE=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [[ ${CREATE_REMOTE} -eq 1 ]]; then
    if ! command -v gh >/dev/null 2>&1; then
        echo "Error: --create-remote requested but gh CLI is not installed." >&2
        exit 1
    fi
    if [[ ${APPLY} -eq 1 ]]; then
        gh auth status >/dev/null
    else
        echo "[dry-run] gh auth status"
    fi
fi

for repo in "${REPOS[@]}"; do
    local_dir="${BASE_DIR}/${repo}"
    remote_url="git@github.com:${ORG}/${repo}.git"
    remote_https="https://github.com/${ORG}/${repo}.git"

    if [[ ! -d "${local_dir}" ]]; then
        echo "Error: missing local directory ${local_dir}" >&2
        exit 1
    fi
    if [[ ! -d "${local_dir}/.git" ]]; then
        echo "Error: ${local_dir} is not a git repo" >&2
        exit 1
    fi

    echo "== ${repo} =="

    if [[ ${CREATE_REMOTE} -eq 1 ]]; then
        if [[ ${PRIVATE} -eq 1 ]]; then
            run_cmd gh repo create "${ORG}/${repo}" --private --source "${local_dir}" --remote origin --push=false
        else
            run_cmd gh repo create "${ORG}/${repo}" --public --source "${local_dir}" --remote origin --push=false
        fi
    fi

    if git -C "${local_dir}" remote get-url origin >/dev/null 2>&1; then
        current_origin="$(git -C "${local_dir}" remote get-url origin)"
        echo "origin already set: ${current_origin}"
    else
        run_cmd git -C "${local_dir}" remote add origin "${remote_url}"
    fi

    if [[ ${PUSH} -eq 1 ]]; then
        # Ensure an initial commit exists before push.
        if [[ -z "$(git -C "${local_dir}" rev-parse --verify HEAD 2>/dev/null || true)" ]]; then
            run_cmd git -C "${local_dir}" add .
            run_cmd git -C "${local_dir}" commit -m "chore: initialize Phase 6 planning scaffold"
        fi
        run_cmd git -C "${local_dir}" branch -M main
        run_cmd git -C "${local_dir}" push -u origin main
    fi

    echo "remote target: ${remote_https}"
done

echo
echo "Done."
if [[ ${APPLY} -eq 0 ]]; then
    echo "Dry-run mode was used. Re-run with --apply to execute."
fi
