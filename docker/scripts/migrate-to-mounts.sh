#!/bin/bash
# =============================================================================
# Migrate to Volume Mounts
# =============================================================================
# Migrates existing Docker deployments from baked-in plugins to host-mounted
# plugins, enabling hot upgrades without rebuilding images.
#
# What this script does:
#   1. Clones cl-hive and cl_revenue_ops repos on host (if needed)
#   2. Creates docker-compose.override.yml with volume mounts
#   3. Recreates the container with new mounts (data is preserved)
#   4. Verifies plugins are working
#
# Safety:
#   - All Lightning data is in Docker volumes (preserved across recreates)
#   - Script confirms before making changes
#   - Creates backup of any existing override file
#
# Usage:
#   ./migrate-to-mounts.sh
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER_DIR="$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="$(dirname "$DOCKER_DIR")"
PARENT_DIR="$(dirname "$PROJECT_ROOT")"
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
log_step() { echo -e "\n${CYAN}${BOLD}==> $1${NC}"; }

confirm() {
    local prompt="$1"
    local response
    echo -e -n "${YELLOW}$prompt [y/N]: ${NC}"
    read -r response
    [[ "$response" =~ ^[Yy]$ ]]
}

check_prerequisites() {
    log_step "Checking prerequisites..."

    # Check Docker
    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed"
        exit 1
    fi
    log_info "Docker found"

    # Check docker-compose
    if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
        log_error "docker-compose is not installed"
        exit 1
    fi
    log_info "docker-compose found"

    # Check git
    if ! command -v git &> /dev/null; then
        log_error "git is not installed"
        exit 1
    fi
    log_info "git found"

    # Check container is running
    if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        log_warn "Container ${CONTAINER_NAME} is not running"
        log_info "Will start it after migration"
    else
        log_info "Container ${CONTAINER_NAME} is running"
    fi
}

show_current_state() {
    log_step "Current state..."

    echo "  Container: ${CONTAINER_NAME}"
    echo "  Docker dir: ${DOCKER_DIR}"
    echo "  Parent dir: ${PARENT_DIR}"
    echo ""

    # Check if repos exist
    if [ -d "$PROJECT_ROOT/.git" ]; then
        echo -e "  cl-hive repo: ${GREEN}found${NC} at $PROJECT_ROOT"
    else
        echo -e "  cl-hive repo: ${YELLOW}not found${NC}"
    fi

    if [ -d "$PARENT_DIR/cl_revenue_ops/.git" ]; then
        echo -e "  cl_revenue_ops repo: ${GREEN}found${NC} at $PARENT_DIR/cl_revenue_ops"
    else
        echo -e "  cl_revenue_ops repo: ${YELLOW}will clone${NC}"
    fi

    # Check current plugin state in container
    if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        echo ""
        echo "  Current plugin state in container:"
        if docker exec "$CONTAINER_NAME" test -d /opt/cl-hive/.git 2>/dev/null; then
            echo -e "    cl-hive: ${GREEN}git repo (mountable)${NC}"
        else
            echo -e "    cl-hive: ${YELLOW}baked in image${NC}"
        fi
        if docker exec "$CONTAINER_NAME" test -d /opt/cl-revenue-ops/.git 2>/dev/null; then
            echo -e "    cl-revenue-ops: ${GREEN}git repo (mountable)${NC}"
        else
            echo -e "    cl-revenue-ops: ${YELLOW}baked in image${NC}"
        fi
    fi
}

clone_repos() {
    log_step "Setting up repositories..."

    # cl-hive should already exist (we're running from it)
    if [ ! -d "$PROJECT_ROOT/.git" ]; then
        log_error "cl-hive repo not found. This script should be run from within cl-hive."
        exit 1
    fi

    # Pull latest cl-hive
    log_info "Updating cl-hive..."
    cd "$PROJECT_ROOT"
    git pull origin main

    # Clone cl_revenue_ops if needed
    if [ ! -d "$PARENT_DIR/cl_revenue_ops" ]; then
        log_info "Cloning cl_revenue_ops..."
        git clone https://github.com/lightning-goats/cl_revenue_ops.git "$PARENT_DIR/cl_revenue_ops"
    else
        log_info "Updating cl_revenue_ops..."
        cd "$PARENT_DIR/cl_revenue_ops"
        git pull origin main
    fi

    log_info "Repositories ready"
}

create_override() {
    log_step "Creating docker-compose.override.yml..."

    local override_file="$DOCKER_DIR/docker-compose.override.yml"

    # Backup existing override if present
    if [ -f "$override_file" ]; then
        local backup="${override_file}.backup.$(date +%Y%m%d%H%M%S)"
        log_warn "Backing up existing override to: $backup"
        cp "$override_file" "$backup"
    fi

    # Create override file
    cat > "$override_file" << EOF
# Auto-generated by migrate-to-mounts.sh
# Enables hot upgrades by mounting plugins from host
#
# To upgrade plugins: ./scripts/hot-upgrade.sh

services:
  cln:
    volumes:
      # Plugin mounts for hot upgrades
      - ${PROJECT_ROOT}:/opt/cl-hive:ro
      - ${PARENT_DIR}/cl_revenue_ops:/opt/cl-revenue-ops:ro
EOF

    log_info "Created: $override_file"
    echo ""
    cat "$override_file"
}

recreate_container() {
    log_step "Recreating container with new mounts..."

    cd "$DOCKER_DIR"

    echo ""
    echo -e "${YELLOW}${BOLD}IMPORTANT:${NC}"
    echo "  - Your Lightning data is stored in Docker volumes"
    echo "  - Volumes are PRESERVED when recreating containers"
    echo "  - Your channels, keys, and databases are SAFE"
    echo ""

    if ! confirm "Proceed with container recreation?"; then
        log_warn "Aborted by user"
        exit 0
    fi

    echo ""
    log_info "Stopping container..."
    docker-compose down

    log_info "Starting container with new mounts..."
    docker-compose up -d

    log_info "Waiting for node to become healthy..."
    local retries=30
    while [ $retries -gt 0 ]; do
        if docker exec "$CONTAINER_NAME" lightning-cli --lightning-dir="/data/lightning/$NETWORK" getinfo >/dev/null 2>&1; then
            log_info "Node is healthy"
            return 0
        fi
        echo -n "."
        sleep 2
        retries=$((retries - 1))
    done
    echo ""

    log_error "Node did not become healthy"
    log_warn "Check logs: docker-compose logs -f"
    return 1
}

verify_migration() {
    log_step "Verifying migration..."

    # Check mounts
    local mount_ok=true

    if docker exec "$CONTAINER_NAME" test -d /opt/cl-hive/.git 2>/dev/null; then
        log_info "cl-hive: mounted from host"
    else
        log_error "cl-hive: NOT mounted correctly"
        mount_ok=false
    fi

    if docker exec "$CONTAINER_NAME" test -d /opt/cl-revenue-ops/.git 2>/dev/null; then
        log_info "cl-revenue-ops: mounted from host"
    else
        log_error "cl-revenue-ops: NOT mounted correctly"
        mount_ok=false
    fi

    if [ "$mount_ok" == "false" ]; then
        log_error "Migration verification failed"
        return 1
    fi

    # Check plugins are loaded
    echo ""
    log_info "Checking plugins..."

    if docker exec "$CONTAINER_NAME" lightning-cli --lightning-dir="/data/lightning/$NETWORK" plugin list 2>/dev/null | grep -q "cl-hive"; then
        log_info "cl-hive plugin: loaded"
    else
        log_warn "cl-hive plugin: not loaded (may need restart)"
    fi

    if docker exec "$CONTAINER_NAME" lightning-cli --lightning-dir="/data/lightning/$NETWORK" plugin list 2>/dev/null | grep -q "cl-revenue-ops"; then
        log_info "cl-revenue-ops plugin: loaded"
    else
        log_warn "cl-revenue-ops plugin: not loaded (may need restart)"
    fi

    # Test MCF command
    echo ""
    log_info "Testing hive-mcf-status..."
    if docker exec "$CONTAINER_NAME" lightning-cli --lightning-dir="/data/lightning/$NETWORK" hive-mcf-status >/dev/null 2>&1; then
        log_info "MCF commands: working"
    else
        log_warn "MCF commands: not available (plugin may need restart)"
        echo ""
        echo "Try: ./scripts/hot-upgrade.sh --restart"
    fi

    return 0
}

print_success() {
    echo ""
    echo -e "${GREEN}======================================================================${NC}"
    echo -e "${GREEN}${BOLD}                    Migration Complete!                              ${NC}"
    echo -e "${GREEN}======================================================================${NC}"
    echo ""
    echo "Your node is now configured for hot upgrades."
    echo ""
    echo "To upgrade plugins in the future, simply run:"
    echo -e "  ${CYAN}cd $DOCKER_DIR/scripts${NC}"
    echo -e "  ${CYAN}./hot-upgrade.sh${NC}"
    echo ""
    echo "Directory structure:"
    echo "  $PROJECT_ROOT"
    echo "  $PARENT_DIR/cl_revenue_ops"
    echo ""
}

main() {
    echo ""
    echo -e "${CYAN}======================================================================${NC}"
    echo -e "${CYAN}${BOLD}          cl-hive Migration to Volume Mounts                         ${NC}"
    echo -e "${CYAN}======================================================================${NC}"
    echo ""
    echo "This script will configure your Docker deployment for hot upgrades"
    echo "by mounting the plugin repositories from your host machine."
    echo ""

    check_prerequisites
    show_current_state

    echo ""
    if ! confirm "Continue with migration?"; then
        log_warn "Aborted by user"
        exit 0
    fi

    clone_repos
    create_override
    recreate_container
    verify_migration
    print_success
}

main "$@"
