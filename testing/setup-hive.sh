#!/bin/bash
#
# Setup a 3-node Hive for testing
#
# This script brings up a complete Hive with:
# - Alice: admin (genesis)
# - Bob: member (promoted)
# - Carol: neophyte
#
# Prerequisites:
# - Polar network running with alice, bob, carol nodes
# - install.sh already run to install plugins
#
# Usage: ./setup-hive.sh [network_id]
#

set -e

NETWORK_ID="${1:-1}"
CLI="lightning-cli --lightning-dir=/home/clightning/.lightning --network=regtest"

# Node IDs (will be populated)
ALICE_ID=""
BOB_ID=""
CAROL_ID=""

echo "========================================"
echo "Hive Setup Script"
echo "========================================"
echo "Network ID: $NETWORK_ID"
echo ""

#
# Helper functions
#
container_exec() {
    local node=$1
    shift
    docker exec polar-n${NETWORK_ID}-${node} "$@"
}

hive_cli() {
    local node=$1
    shift
    container_exec $node $CLI "$@"
}

get_pubkey() {
    local node=$1
    hive_cli $node getinfo 2>/dev/null | grep '"id"' | head -1 | sed 's/.*"id": "//;s/".*//'
}

wait_for_plugin() {
    local node=$1
    local plugin=$2
    local max_wait=30
    local elapsed=0

    while [ $elapsed -lt $max_wait ]; do
        if hive_cli $node plugin list 2>/dev/null | grep -q "$plugin"; then
            return 0
        fi
        sleep 1
        ((elapsed++))
    done
    return 1
}

#
# Step 1: Verify plugins are loaded
#
echo "=== Step 1: Verify Plugins ==="
for node in alice bob carol; do
    echo -n "$node: "
    if hive_cli $node plugin list 2>/dev/null | grep -q "cl-hive"; then
        echo "cl-hive loaded"
    else
        echo "MISSING cl-hive - run install.sh first"
        exit 1
    fi
done
echo ""

#
# Step 2: Get node pubkeys
#
echo "=== Step 2: Get Node Pubkeys ==="
ALICE_ID=$(get_pubkey alice)
BOB_ID=$(get_pubkey bob)
CAROL_ID=$(get_pubkey carol)

echo "Alice: $ALICE_ID"
echo "Bob:   $BOB_ID"
echo "Carol: $CAROL_ID"
echo ""

#
# Step 3: Check current hive status
#
echo "=== Step 3: Check Current Status ==="
ALICE_STATUS=$(hive_cli alice hive-status 2>/dev/null | grep '"status":' | sed 's/.*"status": "//;s/".*//')
echo "Alice hive status: $ALICE_STATUS"

if [ "$ALICE_STATUS" == "active" ]; then
    echo "Hive already exists. Checking members..."
    MEMBER_COUNT=$(hive_cli alice hive-members 2>/dev/null | grep '"count":' | sed 's/.*"count": //;s/,.*//')
    echo "Current members: $MEMBER_COUNT"

    if [ "$MEMBER_COUNT" -ge 3 ]; then
        echo "Hive already has 3+ members. Setup complete."
        exit 0
    fi
fi
echo ""

#
# Step 4: Reset databases if needed
#
if [ "$ALICE_STATUS" != "active" ]; then
    echo "=== Step 4: Reset Databases ==="
    for node in alice bob carol; do
        container_exec $node rm -f /home/clightning/.lightning/cl_hive.db
        echo "$node: database reset"
    done

    # Restart plugins to pick up fresh database
    for node in alice bob carol; do
        hive_cli $node plugin stop /home/clightning/.lightning/plugins/cl-hive/cl-hive.py 2>/dev/null || true
        hive_cli $node -k plugin subcommand=start \
            plugin=/home/clightning/.lightning/plugins/cl-hive/cl-hive.py \
            hive-min-vouch-count=1 2>/dev/null
    done
    sleep 2
    echo ""
fi

#
# Step 5: Alice creates genesis
#
echo "=== Step 5: Genesis ==="
ALICE_STATUS=$(hive_cli alice hive-status 2>/dev/null | grep '"status":' | sed 's/.*"status": "//;s/".*//')

if [ "$ALICE_STATUS" == "genesis_required" ]; then
    echo "Creating genesis on Alice..."
    GENESIS=$(hive_cli alice hive-genesis 2>/dev/null)
    HIVE_ID=$(echo "$GENESIS" | grep '"hive_id":' | sed 's/.*"hive_id": "//;s/".*//')
    echo "Created Hive: $HIVE_ID"
else
    echo "Genesis already complete"
fi
echo ""

#
# Step 6: Ensure peer connections
#
echo "=== Step 6: Peer Connections ==="
# Bob to Alice
if ! hive_cli bob listpeers 2>/dev/null | grep -q "$ALICE_ID"; then
    echo "Connecting Bob to Alice..."
    hive_cli bob connect "${ALICE_ID}@polar-n${NETWORK_ID}-alice:9735" 2>/dev/null || true
fi

# Carol to Alice
if ! hive_cli carol listpeers 2>/dev/null | grep -q "$ALICE_ID"; then
    echo "Connecting Carol to Alice..."
    hive_cli carol connect "${ALICE_ID}@polar-n${NETWORK_ID}-alice:9735" 2>/dev/null || true
fi
echo "Peer connections established"
echo ""

#
# Step 7: Bob joins hive
#
echo "=== Step 7: Bob Joins Hive ==="
BOB_STATUS=$(hive_cli bob hive-status 2>/dev/null | grep '"status":' | sed 's/.*"status": "//;s/".*//')

if [ "$BOB_STATUS" == "genesis_required" ]; then
    echo "Generating invite for Bob..."
    TICKET=$(hive_cli alice hive-invite 2>/dev/null | grep '"ticket":' | sed 's/.*"ticket": "//;s/".*//')

    echo "Bob joining..."
    hive_cli bob hive-join ticket="$TICKET" 2>/dev/null
    sleep 3

    BOB_STATUS=$(hive_cli bob hive-status 2>/dev/null | grep '"status":' | sed 's/.*"status": "//;s/".*//')
    echo "Bob status: $BOB_STATUS"
else
    echo "Bob already in hive (status: $BOB_STATUS)"
fi
echo ""

#
# Step 8: Carol joins hive
#
echo "=== Step 8: Carol Joins Hive ==="
CAROL_STATUS=$(hive_cli carol hive-status 2>/dev/null | grep '"status":' | sed 's/.*"status": "//;s/".*//')

if [ "$CAROL_STATUS" == "genesis_required" ]; then
    echo "Generating invite for Carol..."
    TICKET=$(hive_cli alice hive-invite 2>/dev/null | grep '"ticket":' | sed 's/.*"ticket": "//;s/".*//')

    echo "Carol joining..."
    hive_cli carol hive-join ticket="$TICKET" 2>/dev/null
    sleep 3

    CAROL_STATUS=$(hive_cli carol hive-status 2>/dev/null | grep '"status":' | sed 's/.*"status": "//;s/".*//')
    echo "Carol status: $CAROL_STATUS"
else
    echo "Carol already in hive (status: $CAROL_STATUS)"
fi
echo ""

#
# Step 9: Promote Bob to member
#
echo "=== Step 9: Promote Bob ==="
BOB_TIER=$(hive_cli alice hive-members 2>/dev/null | grep -A5 "$BOB_ID" | grep '"tier":' | sed 's/.*"tier": "//;s/".*//')

if [ "$BOB_TIER" == "neophyte" ]; then
    echo "Bob requesting promotion..."
    hive_cli bob hive-request-promotion 2>/dev/null
    sleep 2

    echo "Alice vouching for Bob..."
    hive_cli alice hive-vouch "$BOB_ID" 2>/dev/null
    sleep 2

    BOB_TIER=$(hive_cli alice hive-members 2>/dev/null | grep -A5 "$BOB_ID" | grep '"tier":' | sed 's/.*"tier": "//;s/".*//')
    echo "Bob tier: $BOB_TIER"
elif [ "$BOB_TIER" == "member" ]; then
    echo "Bob already promoted to member"
else
    echo "Bob tier: $BOB_TIER"
fi
echo ""

#
# Step 10: Final status
#
echo "========================================"
echo "Hive Setup Complete"
echo "========================================"
echo ""
echo "Members:"
hive_cli alice hive-members 2>/dev/null | grep -E '"peer_id"|"tier"' | paste - - | while read line; do
    peer=$(echo "$line" | grep -o '"peer_id": "[^"]*"' | sed 's/"peer_id": "//;s/"//')
    tier=$(echo "$line" | grep -o '"tier": "[^"]*"' | sed 's/"tier": "//;s/"//')

    if [ "$peer" == "$ALICE_ID" ]; then
        echo "  Alice: $tier"
    elif [ "$peer" == "$BOB_ID" ]; then
        echo "  Bob:   $tier"
    elif [ "$peer" == "$CAROL_ID" ]; then
        echo "  Carol: $tier"
    else
        echo "  ${peer:0:16}...: $tier"
    fi
done
echo ""
