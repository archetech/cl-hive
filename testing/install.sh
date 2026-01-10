#!/bin/bash
#
# Install clboss, cl-revenue-ops, and cl-hive plugins on Polar CLN nodes
#
# Usage: ./install.sh <network-id> [nodes]
# Example: ./install.sh 1
# Example: ./install.sh 1 "alice bob"
#
# Environment variables:
#   REVENUE_OPS_PATH - Path to cl_revenue_ops repo (default: /home/sat/cl_revenue_ops)
#   HIVE_PATH - Path to cl-hive repo (default: /home/sat/cl-hive)
#   SKIP_CLBOSS - Set to 1 to skip clboss installation
#

set -e

NETWORK_ID="${1:-1}"
NODES="${2:-alice bob carol}"
REVENUE_OPS_PATH="${REVENUE_OPS_PATH:-/home/sat/cl_revenue_ops}"
HIVE_PATH="${HIVE_PATH:-/home/sat/cl-hive}"
SKIP_CLBOSS="${SKIP_CLBOSS:-0}"

echo "========================================"
echo "Polar Plugin Installer"
echo "========================================"
echo "Network ID: $NETWORK_ID"
echo "Nodes: $NODES"
echo "cl-revenue-ops: $REVENUE_OPS_PATH"
echo "cl-hive: $HIVE_PATH"
echo "Skip CLBOSS: $SKIP_CLBOSS"
echo ""

for node in $NODES; do
    CONTAINER="polar-n${NETWORK_ID}-${node}"

    echo "========================================"
    echo "Installing on $CONTAINER"
    echo "========================================"

    # Check container exists
    if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
        echo "ERROR: Container $CONTAINER not found. Is Polar running?"
        exit 1
    fi

    # Install build dependencies
    echo "[1/6] Installing dependencies..."
    docker exec -u root $CONTAINER apt-get update -qq
    docker exec -u root $CONTAINER apt-get install -y -qq \
        build-essential autoconf autoconf-archive automake libtool pkg-config \
        libev-dev libcurl4-gnutls-dev libsqlite3-dev \
        python3 python3-pip git > /dev/null 2>&1
    docker exec -u root $CONTAINER pip3 install -q pyln-client 2>/dev/null

    # Create plugins directory
    docker exec $CONTAINER mkdir -p /home/clightning/.lightning/plugins

    # Build and install CLBOSS
    if [ "$SKIP_CLBOSS" != "1" ]; then
        echo "[2/6] Building CLBOSS (this may take several minutes)..."

        # Check if clboss already exists
        if docker exec $CONTAINER test -f /home/clightning/.lightning/plugins/clboss; then
            echo "      CLBOSS already installed, skipping build"
        else
            docker exec $CONTAINER bash -c "
                cd /tmp &&
                if [ ! -d clboss ]; then
                    git clone --recurse-submodules https://github.com/ZmnSCPxj/clboss.git
                fi &&
                cd clboss &&
                autoreconf -i &&
                ./configure &&
                make -j\$(nproc) &&
                cp clboss /home/clightning/.lightning/plugins/
            " 2>&1 | while read line; do echo "      $line"; done
            echo "      CLBOSS build complete"
        fi
    else
        echo "[2/6] Skipping CLBOSS (SKIP_CLBOSS=1)"
    fi

    # Copy cl-revenue-ops
    echo "[3/6] Copying cl-revenue-ops..."
    docker cp "$REVENUE_OPS_PATH" $CONTAINER:/home/clightning/.lightning/plugins/cl-revenue-ops

    # Copy cl-hive
    echo "[4/6] Copying cl-hive..."
    docker cp "$HIVE_PATH" $CONTAINER:/home/clightning/.lightning/plugins/cl-hive

    # Set permissions
    echo "[5/6] Setting permissions..."
    docker exec -u root $CONTAINER chown -R clightning:clightning /home/clightning/.lightning/plugins
    docker exec $CONTAINER chmod +x /home/clightning/.lightning/plugins/cl-revenue-ops/cl-revenue-ops.py
    docker exec $CONTAINER chmod +x /home/clightning/.lightning/plugins/cl-hive/cl-hive.py

    # Load plugins in order
    echo "[6/6] Loading plugins..."

    if [ "$SKIP_CLBOSS" != "1" ]; then
        if docker exec $CONTAINER lightning-cli plugin start /home/clightning/.lightning/plugins/clboss 2>/dev/null; then
            echo "      clboss: loaded"
        else
            echo "      clboss: FAILED (check logs)"
        fi
    fi

    if docker exec $CONTAINER lightning-cli plugin start /home/clightning/.lightning/plugins/cl-revenue-ops/cl-revenue-ops.py 2>/dev/null; then
        echo "      cl-revenue-ops: loaded"
    else
        echo "      cl-revenue-ops: FAILED (check logs)"
    fi

    if docker exec $CONTAINER lightning-cli plugin start /home/clightning/.lightning/plugins/cl-hive/cl-hive.py 2>/dev/null; then
        echo "      cl-hive: loaded"
    else
        echo "      cl-hive: FAILED (check logs)"
    fi

    echo ""
done

echo "========================================"
echo "Installation Complete"
echo "========================================"
echo ""
echo "Verify plugins:"
echo "  docker exec polar-n${NETWORK_ID}-alice lightning-cli plugin list | grep -E '(clboss|revenue|hive)'"
echo ""
echo "Check CLBOSS status:"
echo "  docker exec polar-n${NETWORK_ID}-alice lightning-cli clboss-status"
echo ""
echo "Check revenue-ops status:"
echo "  docker exec polar-n${NETWORK_ID}-alice lightning-cli revenue-status"
echo ""
echo "Check hive status:"
echo "  docker exec polar-n${NETWORK_ID}-alice lightning-cli hive-status"
echo ""
echo "View logs:"
echo "  docker exec polar-n${NETWORK_ID}-alice tail -50 /home/clightning/.lightning/regtest/log"
