#!/bin/bash
# =============================================================================
# cl-hive Hot Upgrade Script
# =============================================================================
# Upgrades plugins by pulling latest code on HOST and restarting in container.
# Requires plugins to be mounted from host (default in docker-compose.yml).
#
# Usage:
#   ./hot-upgrade.sh              # Upgrade both plugins
#   ./hot-upgrade.sh hive         # Upgrade only cl-hive
#   ./hot-upgrade.sh revenue      # Upgrade only cl-revenue-ops
#   ./hot-upgrade.sh --check      # Check for updates without applying
#   ./hot-upgrade.sh --restart    # Just restart plugins (no git pull)
#
# Required directory structure:
#   parent/
#     cl-hive/           <- this repo (mounted to /opt/cl-hive)
#     cl_revenue_ops/    <- revenue ops repo (mounted to /opt/cl-revenue-ops)
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER_DIR="$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="$(dirname "$DOCKER_DIR")"
REVENUE_OPS_ROOT="$(dirname "$PROJECT_ROOT")/cl_revenue_ops"
CONTAINER_NAME="${CONTAINER_NAME:-cl-hive-node}"
NETWORK="${NETWORK:-bitcoin}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'
BOLD='\033[1m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step() { echo -e "\n${CYAN}==> $1${NC}"; }

check_container() {
    if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        log_error "Container ${CONTAINER_NAME} is not running"
        exit 1
    fi
}

get_git_version() {
    local repo="$1"
    git -C "$repo" rev-parse --short HEAD 2>/dev/null || echo "not-found"
}

get_remote_version() {
    local repo="$1"
    git -C "$repo" fetch --quiet 2>/dev/null || true
    git -C "$repo" rev-parse --short origin/main 2>/dev/null || echo "unknown"
}

check_mount() {
    local name="$1"
    local host_path="$2"
    local container_path="$3"

    # Check host repo exists
    if [ ! -d "$host_path/.git" ]; then
        log_error "$name not found at: $host_path"
        echo ""
        echo "Please clone the repo:"
        echo "  git clone https://github.com/lightning-goats/$name.git $host_path"
        return 1
    fi

    # Check if mounted by comparing a file
    local host_version=$(get_git_version "$host_path")
    local container_version=$(docker exec "$CONTAINER_NAME" cat "$container_path/.git/HEAD" 2>/dev/null | head -1 || echo "not-mounted")

    if [[ "$container_version" == "not-mounted" ]] || [[ "$container_version" != *"ref:"* && "$container_version" != "$host_version"* ]]; then
        log_error "$name is not mounted from host"
        echo ""
        echo "Add to docker-compose.yml or docker-compose.override.yml:"
        echo "  volumes:"
        echo "    - $host_path:$container_path:ro"
        echo ""
        echo "Then run: docker-compose up -d"
        return 1
    fi

    return 0
}

upgrade_repo() {
    local name="$1"
    local repo_path="$2"

    log_step "Checking $name for updates..."

    if [ ! -d "$repo_path/.git" ]; then
        log_warn "$name repo not found at $repo_path"
        return 0
    fi

    cd "$repo_path"

    local current=$(get_git_version .)
    local remote=$(get_remote_version .)

    echo "  Current: $current"
    echo "  Remote:  $remote"

    if [ "$current" == "$remote" ]; then
        log_info "$name is up to date"
        return 0
    fi

    if [ "$CHECK_ONLY" == "true" ]; then
        log_warn "Update available: $current -> $remote"
        return 1
    fi

    log_info "Pulling latest $name..."

    # Stash local changes if any
    if ! git diff --quiet 2>/dev/null; then
        log_warn "Stashing local changes..."
        git stash
    fi

    git pull origin main

    log_info "$name upgraded: $current -> $(get_git_version .)"
    return 1  # Signal upgrade was performed
}

restart_plugin() {
    local plugin_path="$1"
    local plugin_name="$2"

    log_step "Restarting $plugin_name plugin..."

    # Try to stop the plugin gracefully
    local stop_output
    stop_output=$(docker exec "$CONTAINER_NAME" lightning-cli --lightning-dir="/data/lightning/$NETWORK" plugin stop "$plugin_path" 2>&1) || true
    if [[ "$stop_output" == *"Successfully"* ]]; then
        log_info "Stopped $plugin_name"
        sleep 1
    else
        log_warn "Could not stop $plugin_name (may not be running)"
    fi

    # Start the plugin
    local start_output
    start_output=$(docker exec "$CONTAINER_NAME" lightning-cli --lightning-dir="/data/lightning/$NETWORK" plugin start "$plugin_path" 2>&1)
    local start_result=$?

    if [ $start_result -eq 0 ]; then
        log_info "Started $plugin_name"
        return 0
    else
        log_error "Failed to start $plugin_name:"
        echo "$start_output" | head -20
        return 1
    fi
}

show_versions() {
    echo ""
    echo "Current versions (on host):"
    echo -n "  cl-hive:        "
    get_git_version "$PROJECT_ROOT"
    echo -n "  cl-revenue-ops: "
    get_git_version "$REVENUE_OPS_ROOT"
}

print_usage() {
    cat << 'EOF'
Usage: hot-upgrade.sh [OPTION] [COMPONENT]

Hot upgrade plugins without rebuilding the Docker image.
Pulls latest code on HOST, then restarts plugins in container.

Components:
    hive        Upgrade only cl-hive
    revenue     Upgrade only cl-revenue-ops
    (none)      Upgrade both

Options:
    --check, -c     Check for updates without applying
    --restart, -r   Just restart plugins (skip git pull)
    --help, -h      Show this help

Examples:
    ./hot-upgrade.sh              # Upgrade and restart all plugins
    ./hot-upgrade.sh --check      # Check what updates are available
    ./hot-upgrade.sh hive         # Upgrade and restart cl-hive only
    ./hot-upgrade.sh --restart    # Just restart plugins (no git)

Required setup:
    Both repos must be cloned on host and mounted into container.
    See docker-compose.yml for the default configuration.
EOF
}

main() {
    local upgrade_hive=true
    local upgrade_revenue=true
    CHECK_ONLY=false
    RESTART_ONLY=false

    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case "$1" in
            hive)
                upgrade_revenue=false
                shift
                ;;
            revenue)
                upgrade_hive=false
                shift
                ;;
            --check|-c)
                CHECK_ONLY=true
                shift
                ;;
            --restart|-r)
                RESTART_ONLY=true
                shift
                ;;
            --help|-h)
                print_usage
                exit 0
                ;;
            *)
                log_error "Unknown option: $1"
                print_usage
                exit 1
                ;;
        esac
    done

    # Header
    echo ""
    echo -e "${CYAN}======================================================================${NC}"
    echo -e "${CYAN}${BOLD}            cl-hive Hot Upgrade Script                               ${NC}"
    echo -e "${CYAN}======================================================================${NC}"
    if [ "$CHECK_ONLY" == "true" ]; then
        echo -e "${YELLOW}                    [CHECK MODE]                                     ${NC}"
    fi
    if [ "$RESTART_ONLY" == "true" ]; then
        echo -e "${YELLOW}                    [RESTART ONLY]                                   ${NC}"
    fi

    check_container

    # Verify mounts
    local mounts_ok=true
    if [ "$upgrade_hive" == "true" ]; then
        if ! check_mount "cl-hive" "$PROJECT_ROOT" "/opt/cl-hive"; then
            mounts_ok=false
        fi
    fi
    if [ "$upgrade_revenue" == "true" ]; then
        if ! check_mount "cl_revenue_ops" "$REVENUE_OPS_ROOT" "/opt/cl-revenue-ops"; then
            mounts_ok=false
        fi
    fi

    if [ "$mounts_ok" == "false" ]; then
        echo ""
        log_error "Mount check failed. Fix the issues above and retry."
        exit 1
    fi

    show_versions

    local hive_upgraded=false
    local revenue_upgraded=false

    # In restart-only mode, skip git operations
    if [ "$RESTART_ONLY" != "true" ]; then
        if [ "$upgrade_hive" == "true" ]; then
            if ! upgrade_repo "cl-hive" "$PROJECT_ROOT"; then
                hive_upgraded=true
            fi
        fi

        if [ "$upgrade_revenue" == "true" ]; then
            if ! upgrade_repo "cl_revenue_ops" "$REVENUE_OPS_ROOT"; then
                revenue_upgraded=true
            fi
        fi
    fi

    if [ "$CHECK_ONLY" == "true" ]; then
        echo ""
        log_info "Check complete (no changes made)"
        exit 0
    fi

    # Restart plugins
    log_step "Restarting plugins..."

    local restart_failed=false

    if [ "$upgrade_hive" == "true" ]; then
        if ! restart_plugin "/opt/cl-hive/cl-hive.py" "cl-hive"; then
            restart_failed=true
        fi
    fi

    if [ "$upgrade_revenue" == "true" ]; then
        if ! restart_plugin "/opt/cl-revenue-ops/cl-revenue-ops.py" "cl-revenue-ops"; then
            restart_failed=true
        fi
    fi

    echo ""
    show_versions
    echo ""

    if [ "$restart_failed" == "true" ]; then
        log_error "Some plugins failed to restart. Check errors above."
        exit 1
    fi

    if [ "$hive_upgraded" == "true" ] || [ "$revenue_upgraded" == "true" ]; then
        log_info "Hot upgrade complete!"
    else
        log_info "Plugins restarted successfully"
    fi
}

main "$@"
