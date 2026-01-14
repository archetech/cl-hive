#!/bin/bash
#
# Automated Polar Setup for cl-hive and cl-revenue-ops
#
# This script does EVERYTHING:
# 1. Installs dependencies on Polar containers
# 2. Copies and loads plugins
# 3. Creates a 3-node Hive (alice=admin, bob=member, carol=neophyte)
# 4. Runs verification tests
#
# Usage: ./polar-setup.sh [network_id] [options]
#
# Options:
#   --skip-install    Skip plugin installation (if already done)
#   --skip-clboss     Skip CLBoss installation (optional)
#   --skip-sling      Skip Sling installation (optional for hive, required for revenue-ops rebalancing)
#   --reset           Reset databases before setup
#   --test-only       Only run tests, skip setup
#
# Prerequisites:
#   - Polar installed with network created
#   - Network has CLN nodes: alice, bob, carol
#   - Network is STARTED in Polar
#
# Example:
#   ./polar-setup.sh 1                    # Full setup on network 1
#   ./polar-setup.sh 1 --skip-install     # Setup hive only
#   ./polar-setup.sh 1 --reset            # Reset and start fresh
#

set -e

# =============================================================================
# CONFIGURATION
# =============================================================================

NETWORK_ID="${1:-1}"
shift || true

# Parse options
SKIP_INSTALL=0
SKIP_CLBOSS=1  # Default: skip CLBoss (it's optional)
SKIP_SLING=0   # Default: install Sling (required for revenue-ops)
RESET_DBS=0
TEST_ONLY=0

while [[ $# -gt 0 ]]; do
    case $1 in
        --skip-install) SKIP_INSTALL=1; shift ;;
        --skip-clboss) SKIP_CLBOSS=1; shift ;;
        --with-clboss) SKIP_CLBOSS=0; shift ;;
        --skip-sling) SKIP_SLING=1; shift ;;
        --reset) RESET_DBS=1; shift ;;
        --test-only) TEST_ONLY=1; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HIVE_PATH="${HIVE_PATH:-$(dirname $(dirname $SCRIPT_DIR))}"
REVENUE_OPS_PATH="${REVENUE_OPS_PATH:-/home/sat/cl_revenue_ops}"

# CLI command for Polar CLN containers
CLI="lightning-cli --lightning-dir=/home/clightning/.lightning --network=regtest"

# Nodes
HIVE_NODES="alice bob carol"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

log_header() {
    echo ""
    echo -e "${BLUE}════════════════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}════════════════════════════════════════════════════════════════${NC}"
}

log_step() {
    echo -e "${YELLOW}→${NC} $1"
}

log_ok() {
    echo -e "${GREEN}✓${NC} $1"
}

log_error() {
    echo -e "${RED}✗${NC} $1"
}

log_info() {
    echo -e "  $1"
}

container_exists() {
    docker ps --format '{{.Names}}' | grep -q "^polar-n${NETWORK_ID}-$1$"
}

container_exec() {
    local node=$1
    shift
    docker exec "polar-n${NETWORK_ID}-${node}" "$@"
}

hive_cli() {
    local node=$1
    shift
    container_exec "$node" $CLI "$@" 2>/dev/null
}

get_pubkey() {
    hive_cli "$1" getinfo | jq -r '.id'
}

plugin_loaded() {
    local node=$1
    local plugin=$2
    hive_cli "$node" plugin list | jq -r '.plugins[].name' | grep -q "$plugin"
}

wait_for_sync() {
    local max_wait=30
    local elapsed=0
    log_step "Waiting for state sync..."
    while [ $elapsed -lt $max_wait ]; do
        local alice_hash=$(hive_cli alice hive-status | jq -r '.state_hash // empty')
        local bob_hash=$(hive_cli bob hive-status | jq -r '.state_hash // empty')
        if [ -n "$alice_hash" ] && [ "$alice_hash" == "$bob_hash" ]; then
            log_ok "State synced (hash: ${alice_hash:0:16}...)"
            return 0
        fi
        sleep 1
        ((elapsed++))
    done
    log_error "State sync timeout"
    return 1
}

# =============================================================================
# PHASE 1: VERIFY PREREQUISITES
# =============================================================================

verify_prerequisites() {
    log_header "Phase 1: Verify Prerequisites"

    log_step "Checking Docker..."
    if ! command -v docker &>/dev/null; then
        log_error "Docker not found"
        exit 1
    fi
    log_ok "Docker available"

    log_step "Checking Polar containers..."
    local missing=0
    for node in $HIVE_NODES; do
        if container_exists "$node"; then
            log_ok "Container polar-n${NETWORK_ID}-${node} running"
        else
            log_error "Container polar-n${NETWORK_ID}-${node} NOT FOUND"
            ((missing++))
        fi
    done

    if [ $missing -gt 0 ]; then
        log_error "Missing containers. Is Polar network $NETWORK_ID started?"
        exit 1
    fi

    log_step "Checking plugin paths..."
    if [ ! -f "$HIVE_PATH/cl-hive.py" ]; then
        log_error "cl-hive not found at $HIVE_PATH"
        exit 1
    fi
    log_ok "cl-hive found at $HIVE_PATH"

    if [ ! -f "$REVENUE_OPS_PATH/cl-revenue-ops.py" ]; then
        log_error "cl-revenue-ops not found at $REVENUE_OPS_PATH"
        exit 1
    fi
    log_ok "cl-revenue-ops found at $REVENUE_OPS_PATH"
}

# =============================================================================
# PHASE 2: INSTALL PLUGINS
# =============================================================================

install_dependencies() {
    local node=$1
    log_step "Installing dependencies on $node..."

    docker exec -u root "polar-n${NETWORK_ID}-${node}" bash -c "
        apt-get update -qq 2>/dev/null
        apt-get install -y -qq python3 python3-pip jq > /dev/null 2>&1
        pip3 install --break-system-packages -q pyln-client 2>/dev/null
    " || true

    log_ok "$node: dependencies installed"
}

install_sling() {
    local node=$1

    if [ "$SKIP_SLING" == "1" ]; then
        log_info "$node: Skipping Sling (--skip-sling)"
        return 0
    fi

    # Check if already installed
    if container_exec "$node" test -f /home/clightning/.lightning/plugins/sling 2>/dev/null; then
        log_ok "$node: Sling already installed"
        return 0
    fi

    log_step "Building Sling on $node (this takes a few minutes)..."

    docker exec "polar-n${NETWORK_ID}-${node}" bash -c "
        # Install Rust if not present
        if ! command -v cargo &>/dev/null; then
            curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
            source \$HOME/.cargo/env
        fi
        source \$HOME/.cargo/env

        cd /tmp
        if [ ! -d sling ]; then
            git clone https://github.com/daywalker90/sling.git
        fi
        cd sling
        cargo build --release
        cp target/release/sling /home/clightning/.lightning/plugins/
    " 2>&1 | while read line; do echo "    $line"; done

    log_ok "$node: Sling built and installed"
}

copy_plugins() {
    local node=$1
    local container="polar-n${NETWORK_ID}-${node}"

    log_step "Copying plugins to $node..."

    # Create plugins directory
    container_exec "$node" mkdir -p /home/clightning/.lightning/plugins

    # Copy cl-revenue-ops
    docker cp "$REVENUE_OPS_PATH" "$container:/home/clightning/.lightning/plugins/cl-revenue-ops"

    # Copy cl-hive
    docker cp "$HIVE_PATH" "$container:/home/clightning/.lightning/plugins/cl-hive"

    # Fix permissions
    docker exec -u root "$container" chown -R clightning:clightning /home/clightning/.lightning/plugins
    container_exec "$node" chmod +x /home/clightning/.lightning/plugins/cl-revenue-ops/cl-revenue-ops.py
    container_exec "$node" chmod +x /home/clightning/.lightning/plugins/cl-hive/cl-hive.py

    log_ok "$node: plugins copied"
}

load_plugins() {
    local node=$1

    log_step "Loading plugins on $node..."

    # Load order: sling → cl-revenue-ops → cl-hive

    if [ "$SKIP_SLING" != "1" ]; then
        if ! plugin_loaded "$node" "sling"; then
            hive_cli "$node" plugin start /home/clightning/.lightning/plugins/sling 2>/dev/null || true
            sleep 1
        fi
        if plugin_loaded "$node" "sling"; then
            log_ok "$node: sling loaded"
        else
            log_info "$node: sling not loaded (optional for hive)"
        fi
    fi

    if ! plugin_loaded "$node" "cl-revenue-ops"; then
        hive_cli "$node" plugin start /home/clightning/.lightning/plugins/cl-revenue-ops/cl-revenue-ops.py || true
        sleep 1
    fi
    if plugin_loaded "$node" "cl-revenue-ops"; then
        log_ok "$node: cl-revenue-ops loaded"
    else
        log_error "$node: cl-revenue-ops FAILED to load"
    fi

    if ! plugin_loaded "$node" "cl-hive"; then
        # Start with testing-friendly config
        hive_cli "$node" -k plugin subcommand=start \
            plugin=/home/clightning/.lightning/plugins/cl-hive/cl-hive.py \
            hive-min-vouch-count=1 \
            hive-probation-days=0 \
            hive-heartbeat-interval=30 || true
        sleep 1
    fi
    if plugin_loaded "$node" "cl-hive"; then
        log_ok "$node: cl-hive loaded"
    else
        log_error "$node: cl-hive FAILED to load"
    fi
}

install_all() {
    log_header "Phase 2: Install Plugins"

    for node in $HIVE_NODES; do
        install_dependencies "$node"
    done

    for node in $HIVE_NODES; do
        install_sling "$node"
    done

    for node in $HIVE_NODES; do
        copy_plugins "$node"
    done

    for node in $HIVE_NODES; do
        load_plugins "$node"
    done
}

# =============================================================================
# PHASE 3: RESET (if requested)
# =============================================================================

reset_databases() {
    log_header "Phase 3: Reset Databases"

    for node in $HIVE_NODES; do
        log_step "Resetting $node..."

        # Stop plugins
        hive_cli "$node" plugin stop cl-hive 2>/dev/null || true
        hive_cli "$node" plugin stop cl-revenue-ops 2>/dev/null || true

        # Remove databases
        container_exec "$node" rm -f /home/clightning/.lightning/regtest/cl_hive.db 2>/dev/null || true
        container_exec "$node" rm -f /home/clightning/.lightning/regtest/revenue_ops.db 2>/dev/null || true
        container_exec "$node" rm -f /home/clightning/.lightning/cl_hive.db 2>/dev/null || true
        container_exec "$node" rm -f /home/clightning/.lightning/revenue_ops.db 2>/dev/null || true

        log_ok "$node: databases reset"
    done

    # Reload plugins
    for node in $HIVE_NODES; do
        load_plugins "$node"
    done

    sleep 2
}

# =============================================================================
# PHASE 4: SETUP HIVE
# =============================================================================

setup_hive() {
    log_header "Phase 4: Setup Hive"

    # Get pubkeys
    log_step "Getting node pubkeys..."
    ALICE_ID=$(get_pubkey alice)
    BOB_ID=$(get_pubkey bob)
    CAROL_ID=$(get_pubkey carol)

    log_info "Alice: ${ALICE_ID:0:20}..."
    log_info "Bob:   ${BOB_ID:0:20}..."
    log_info "Carol: ${CAROL_ID:0:20}..."

    # Check if hive already exists
    local alice_status=$(hive_cli alice hive-status | jq -r '.status // "unknown"')

    if [ "$alice_status" == "active" ]; then
        local member_count=$(hive_cli alice hive-members | jq -r '.count // 0')
        if [ "$member_count" -ge 3 ]; then
            log_ok "Hive already setup with $member_count members"
            return 0
        fi
    fi

    # Genesis
    log_step "Creating genesis on Alice..."
    if [ "$alice_status" == "genesis_required" ]; then
        local genesis=$(hive_cli alice hive-genesis)
        local hive_id=$(echo "$genesis" | jq -r '.hive_id // empty')
        log_ok "Hive created: ${hive_id:0:16}..."
    else
        log_ok "Genesis already complete"
    fi

    # Ensure peer connections
    log_step "Ensuring peer connections..."
    hive_cli bob connect "${ALICE_ID}@polar-n${NETWORK_ID}-alice:9735" 2>/dev/null || true
    hive_cli carol connect "${ALICE_ID}@polar-n${NETWORK_ID}-alice:9735" 2>/dev/null || true
    sleep 1
    log_ok "Peers connected"

    # Bob joins
    log_step "Bob joining hive..."
    local bob_status=$(hive_cli bob hive-status | jq -r '.status // "unknown"')
    if [ "$bob_status" == "genesis_required" ]; then
        local ticket=$(hive_cli alice hive-invite | jq -r '.ticket')
        hive_cli bob hive-join ticket="$ticket"
        sleep 2
        log_ok "Bob joined as neophyte"
    else
        log_ok "Bob already in hive"
    fi

    # Carol joins
    log_step "Carol joining hive..."
    local carol_status=$(hive_cli carol hive-status | jq -r '.status // "unknown"')
    if [ "$carol_status" == "genesis_required" ]; then
        local ticket=$(hive_cli alice hive-invite | jq -r '.ticket')
        hive_cli carol hive-join ticket="$ticket"
        sleep 2
        log_ok "Carol joined as neophyte"
    else
        log_ok "Carol already in hive"
    fi

    # Wait for sync
    wait_for_sync || true

    # Promote Bob
    log_step "Promoting Bob to member..."
    local bob_tier=$(hive_cli alice hive-members | jq -r --arg id "$BOB_ID" '.members[] | select(.peer_id == $id) | .tier // empty')
    if [ "$bob_tier" == "neophyte" ]; then
        hive_cli bob hive-request-promotion || true
        sleep 1
        hive_cli alice hive-vouch "$BOB_ID" || true
        sleep 2
        bob_tier=$(hive_cli alice hive-members | jq -r --arg id "$BOB_ID" '.members[] | select(.peer_id == $id) | .tier // empty')
    fi
    log_ok "Bob tier: $bob_tier"

    log_ok "Hive setup complete"
}

# =============================================================================
# PHASE 5: VERIFY
# =============================================================================

verify_setup() {
    log_header "Phase 5: Verify Setup"

    local errors=0

    # Check plugins loaded
    log_step "Checking plugins..."
    for node in $HIVE_NODES; do
        if plugin_loaded "$node" "cl-hive"; then
            log_ok "$node: cl-hive ✓"
        else
            log_error "$node: cl-hive NOT loaded"
            ((errors++))
        fi
    done

    # Check hive status
    log_step "Checking hive status..."
    for node in $HIVE_NODES; do
        local status=$(hive_cli "$node" hive-status | jq -r '.status // "error"')
        local member_count=$(hive_cli "$node" hive-status | jq -r '.members.total // 0')
        if [ "$status" == "active" ]; then
            log_ok "$node: status=active, members=$member_count"
        else
            log_error "$node: status=$status"
            ((errors++))
        fi
    done

    # Check member count
    log_step "Checking members..."
    local member_count=$(hive_cli alice hive-members | jq -r '.count // 0')
    if [ "$member_count" -ge 3 ]; then
        log_ok "Member count: $member_count"
    else
        log_error "Member count: $member_count (expected 3+)"
        ((errors++))
    fi

    # Check state sync (verify member counts match)
    log_step "Checking state sync..."
    local alice_count=$(hive_cli alice hive-status | jq -r '.members.total // 0')
    local bob_count=$(hive_cli bob hive-status | jq -r '.members.total // 0')
    local carol_count=$(hive_cli carol hive-status | jq -r '.members.total // 0')

    if [ "$alice_count" == "$bob_count" ] && [ "$alice_count" == "$carol_count" ] && [ "$alice_count" -ge 3 ]; then
        log_ok "State synced: all nodes report $alice_count members"
    else
        log_error "State sync mismatch!"
        log_info "Alice: $alice_count members"
        log_info "Bob:   $bob_count members"
        log_info "Carol: $carol_count members"
        ((errors++))
    fi

    # Check revenue-ops bridge
    log_step "Checking cl-revenue-ops bridge..."
    local bridge_status=$(hive_cli alice hive-status | jq -r '.bridge_status // "unknown"')
    if [ "$bridge_status" == "enabled" ]; then
        log_ok "Bridge status: enabled"
    else
        log_info "Bridge status: $bridge_status (revenue-ops integration)"
    fi

    # Summary
    echo ""
    if [ $errors -eq 0 ]; then
        log_header "SUCCESS: All checks passed!"
    else
        log_header "FAILED: $errors check(s) failed"
        exit 1
    fi
}

# =============================================================================
# PHASE 6: SHOW STATUS
# =============================================================================

show_status() {
    log_header "Hive Status Summary"

    echo ""
    echo "Members:"
    echo "────────────────────────────────────────────────────"
    hive_cli alice hive-members | jq -r '.members[] | "  \(.peer_id[0:16])...  \(.tier)  \(.status // "active")"'

    echo ""
    echo "Quick Commands:"
    echo "────────────────────────────────────────────────────"
    echo "  # Check status"
    echo "  docker exec polar-n${NETWORK_ID}-alice $CLI hive-status"
    echo ""
    echo "  # View members"
    echo "  docker exec polar-n${NETWORK_ID}-alice $CLI hive-members"
    echo ""
    echo "  # View topology"
    echo "  docker exec polar-n${NETWORK_ID}-alice $CLI hive-topology"
    echo ""
    echo "  # Run test suite"
    echo "  ./test.sh hive ${NETWORK_ID}"
}

# =============================================================================
# MAIN
# =============================================================================

main() {
    echo ""
    echo -e "${GREEN}╔════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║           cl-hive Polar Automated Setup                        ║${NC}"
    echo -e "${GREEN}╚════════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo "Network ID:    $NETWORK_ID"
    echo "Hive Path:     $HIVE_PATH"
    echo "Revenue Path:  $REVENUE_OPS_PATH"
    echo "Skip Install:  $SKIP_INSTALL"
    echo "Skip CLBoss:   $SKIP_CLBOSS"
    echo "Skip Sling:    $SKIP_SLING"
    echo "Reset DBs:     $RESET_DBS"
    echo ""

    verify_prerequisites

    if [ "$TEST_ONLY" == "1" ]; then
        verify_setup
        show_status
        exit 0
    fi

    if [ "$SKIP_INSTALL" == "0" ]; then
        install_all
    fi

    if [ "$RESET_DBS" == "1" ]; then
        reset_databases
    fi

    setup_hive
    verify_setup
    show_status
}

main "$@"
