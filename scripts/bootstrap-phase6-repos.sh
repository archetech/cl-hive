#!/usr/bin/env bash
set -euo pipefail

# Bootstrap local Phase 6 repos in ~/bin without implementing runtime code.
#
# Default behavior:
# - Creates local directories:
#   ~/bin/cl-hive-comms
#   ~/bin/cl-hive-archon
# - Adds planning-only skeleton files
# - Optionally initializes git repos
#
# Usage:
#   ./scripts/bootstrap-phase6-repos.sh
#   ./scripts/bootstrap-phase6-repos.sh --base-dir /home/sat/bin --init-git

BASE_DIR="${HOME}/bin"
ORG="lightning-goats"
INIT_GIT=0
FORCE=0

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
        --init-git)
            INIT_GIT=1
            shift
            ;;
        --force)
            FORCE=1
            shift
            ;;
        -h|--help)
            cat <<EOF
Usage: $0 [options]

Options:
  --base-dir DIR   Base directory for repo creation (default: ~/bin)
  --org NAME       GitHub org hint written to README files (default: lightning-goats)
  --init-git       Run 'git init -b main' in each created repo
  --force          Overwrite existing skeleton files
EOF
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

mkdir -p "${BASE_DIR}"

create_repo() {
    local name="$1"
    local dir="${BASE_DIR}/${name}"

    mkdir -p "${dir}/docs" "${dir}/scripts"

    if [[ ${FORCE} -eq 1 || ! -f "${dir}/README.md" ]]; then
        cat > "${dir}/README.md" <<EOF
# ${name}

Status: Planning-only scaffold (Phase 6 not yet implemented).

Intended upstream repo:
- https://github.com/${ORG}/${name}

This local repo was created in advance to prepare Phase 6 repository split.
Runtime extraction and feature implementation are intentionally deferred until
Phases 1-5 are production ready.
EOF
    fi

    if [[ ${FORCE} -eq 1 || ! -f "${dir}/docs/ROADMAP.md" ]]; then
        cat > "${dir}/docs/ROADMAP.md" <<EOF
# ${name} Roadmap

Phase 6 scaffold created.

Next steps (deferred):
1. Define module ownership boundaries.
2. Add CI and release workflow.
3. Implement plugin runtime only after Phase 6 readiness gates pass.
EOF
    fi

    if [[ ${FORCE} -eq 1 || ! -f "${dir}/.gitignore" ]]; then
        cat > "${dir}/.gitignore" <<'EOF'
__pycache__/
*.pyc
.venv/
.pytest_cache/
dist/
build/
EOF
    fi

    if [[ ${INIT_GIT} -eq 1 ]]; then
        if [[ ! -d "${dir}/.git" ]]; then
            git -C "${dir}" init -b main >/dev/null
        fi
    fi

    echo "Prepared: ${dir}"
}

create_repo "cl-hive-comms"
create_repo "cl-hive-archon"

cat <<EOF

Done.

Local repos created under:
  ${BASE_DIR}

Suggested next manual steps:
1. Create remote repos in GitHub org '${ORG}'.
2. Add origin remotes and push initial scaffold commits.
3. Keep repos planning-only until Phase 6 gates are approved.
EOF
