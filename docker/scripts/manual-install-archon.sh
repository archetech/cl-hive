#!/bin/bash
# =============================================================================
# Manual Install: cl-hive-archon into a running local Docker container
# =============================================================================
#
# This script copies a local cl-hive-archon checkout into a running container
# and starts the plugin immediately via `lightning-cli plugin start`.
#
# Usage:
#   ./manual-install-archon.sh
#   ./manual-install-archon.sh --source /path/to/cl-hive-archon
#   ./manual-install-archon.sh --container cl-hive-node --network bitcoin
#   ./manual-install-archon.sh --persist
#
# Notes:
# - This is a manual install for local/dev containers.
# - /opt inside a container is not persistent across rebuild/recreate unless
#   you also mount the repo in docker-compose.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER_DIR="$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="$(dirname "$DOCKER_DIR")"
DEFAULT_SOURCE_DIR="$(dirname "$PROJECT_ROOT")/cl-hive-archon"

CONTAINER_NAME="${CONTAINER_NAME:-cl-hive-node}"
NETWORK="${NETWORK:-bitcoin}"
SOURCE_DIR="$DEFAULT_SOURCE_DIR"
PERSIST=false
INSTALL_DEPS=false

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step() { echo -e "\n${CYAN}==> $1${NC}"; }

usage() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  --source PATH         Path to local cl-hive-archon checkout
                        (default: $DEFAULT_SOURCE_DIR)
  --container NAME      Docker container name (default: $CONTAINER_NAME)
  --network NAME        CLN network dir name (default: $NETWORK)
  --persist             Append plugin line to config for restart persistence
  --install-deps        Install Python deps from requirements.txt inside container
  --help, -h            Show this help

Examples:
  ./manual-install-archon.sh
  ./manual-install-archon.sh --source ~/bin/cl-hive-archon --persist
  ./manual-install-archon.sh --install-deps
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            SOURCE_DIR="${2:-}"
            shift 2
            ;;
        --container)
            CONTAINER_NAME="${2:-}"
            shift 2
            ;;
        --network)
            NETWORK="${2:-}"
            shift 2
            ;;
        --persist)
            PERSIST=true
            shift
            ;;
        --install-deps)
            INSTALL_DEPS=true
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            log_error "Unknown argument: $1"
            usage
            exit 1
            ;;
    esac
done

if ! command -v docker >/dev/null 2>&1; then
    log_error "docker is not installed or not on PATH"
    exit 1
fi

if [[ ! -f "$SOURCE_DIR/cl-hive-archon.py" ]]; then
    log_error "cl-hive-archon.py not found in source dir: $SOURCE_DIR"
    exit 1
fi

if [[ ! -d "$SOURCE_DIR/modules" ]]; then
    log_error "modules/ not found in source dir: $SOURCE_DIR"
    exit 1
fi

if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    log_error "Container not running: $CONTAINER_NAME"
    exit 1
fi

log_step "Copying cl-hive-archon into container"
docker exec "$CONTAINER_NAME" mkdir -p /opt/cl-hive-archon
docker exec "$CONTAINER_NAME" rm -rf /opt/cl-hive-archon/*
tar -C "$SOURCE_DIR" --exclude ".git" --exclude "__pycache__" -cf - . \
  | docker exec -i "$CONTAINER_NAME" tar -C /opt/cl-hive-archon -xf -
docker exec "$CONTAINER_NAME" chmod +x /opt/cl-hive-archon/cl-hive-archon.py
log_info "Copied source to /opt/cl-hive-archon"

if [[ "$INSTALL_DEPS" == "true" ]]; then
    log_step "Installing Python requirements (if any)"
    docker exec "$CONTAINER_NAME" bash -lc \
      "if [ -f /opt/cl-hive-archon/requirements.txt ]; then /opt/cln-plugins-venv/bin/pip install --no-cache-dir -r /opt/cl-hive-archon/requirements.txt; fi"
    log_info "Requirements installed"
else
    log_info "Skipping dependency install (use --install-deps to enable)"
fi

log_step "Restarting cl-hive-archon plugin"
docker exec "$CONTAINER_NAME" lightning-cli --lightning-dir="/data/lightning/$NETWORK" \
  plugin stop /opt/cl-hive-archon/cl-hive-archon.py >/dev/null 2>&1 || true
docker exec "$CONTAINER_NAME" lightning-cli --lightning-dir="/data/lightning/$NETWORK" \
  plugin start /opt/cl-hive-archon/cl-hive-archon.py
log_info "Plugin started"

if [[ "$PERSIST" == "true" ]]; then
    log_step "Persisting plugin line in CLN config"
    docker exec "$CONTAINER_NAME" bash -lc \
      "CFG=/data/lightning/$NETWORK/config; touch \"\$CFG\"; grep -Fqx 'plugin=/opt/cl-hive-archon/cl-hive-archon.py' \"\$CFG\" || echo 'plugin=/opt/cl-hive-archon/cl-hive-archon.py' >> \"\$CFG\""
    log_warn "Config updated. Restart lightningd/container to apply persistent startup line."
fi

log_step "Verifying plugin presence"
docker exec "$CONTAINER_NAME" lightning-cli --lightning-dir="/data/lightning/$NETWORK" plugin list \
  | grep -E "cl-hive-archon|cl-hive-archon.py" >/dev/null
log_info "cl-hive-archon is present in plugin list"

echo ""
log_info "Manual install completed for container: $CONTAINER_NAME"
