#!/bin/bash
# hive-watchdog.sh - Monitor and auto-restart hung cl-hive plugins
# Run via cron: */15 * * * * /home/sat/bin/cl-hive/tools/hive-watchdog.sh

set -euo pipefail

NODES_CONFIG="${HIVE_NODES_CONFIG:-/home/sat/bin/cl-hive/production/nodes.production.json}"
LOG_FILE="/tmp/hive-watchdog.log"
MAX_LOG_SIZE=$((1024 * 1024))  # 1MB
TIMEOUT=10

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Rotate log if it exceeds max size
if [[ -f "$LOG_FILE" ]] && [[ $(stat -f%z "$LOG_FILE" 2>/dev/null || stat -c%s "$LOG_FILE" 2>/dev/null || echo 0) -gt $MAX_LOG_SIZE ]]; then
    mv "$LOG_FILE" "${LOG_FILE}.1"
fi

_curl_with_rune() {
    # Pass rune via curl config file to avoid exposing it in /proc/cmdline
    local rune="$1"; shift
    local config_file
    config_file=$(mktemp)
    chmod 600 "$config_file"
    printf 'header = "Rune: %s"\n' "$rune" > "$config_file"
    curl -sK "$config_file" "$@"
    local rc=$?
    rm -f "$config_file"
    return $rc
}

check_and_restart_plugin() {
    local node_name="$1"
    local rest_url="$2"
    local rune="$3"
    local plugin_path="$4"

    # Test hive-status with timeout
    response=$(timeout "$TIMEOUT" _curl_with_rune "$rune" \
        -k -X POST \
        -H "Content-Type: application/json" \
        -d '{}' \
        "${rest_url}/v1/hive-status" 2>&1) || response="TIMEOUT"

    if [[ "$response" == "TIMEOUT" ]] || [[ "$response" == *"error"* && "$response" != *"governance_mode"* ]]; then
        log "WARNING: $node_name hive-status failed, restarting plugin..."

        # Stop plugin
        timeout 15 _curl_with_rune "$rune" \
            -k -X POST \
            -H "Content-Type: application/json" \
            -d "{\"subcommand\": \"stop\", \"plugin\": \"$plugin_path\"}" \
            "${rest_url}/v1/plugin" 2>/dev/null || true

        sleep 2

        # Start plugin
        restart_result=$(timeout 15 _curl_with_rune "$rune" \
            -k -X POST \
            -H "Content-Type: application/json" \
            -d "{\"subcommand\": \"start\", \"plugin\": \"$plugin_path\"}" \
            "${rest_url}/v1/plugin" 2>&1) || restart_result="FAILED"

        if [[ "$restart_result" == *"active\":true"* ]]; then
            log "OK: $node_name plugin restarted successfully"
        else
            log "ERROR: $node_name plugin restart failed: $restart_result"
        fi
    else
        log "OK: $node_name healthy"
    fi
}

# Main
log "=== Hive Watchdog Check ==="

if [[ ! -f "$NODES_CONFIG" ]]; then
    log "ERROR: Config not found: $NODES_CONFIG"
    exit 1
fi

# Parse nodes config and check each
# Note: Adjust plugin paths per node as needed
jq -r '.nodes[] | "\(.name)|\(.rest_url)|\(.rune)"' "$NODES_CONFIG" | while IFS='|' read -r name url rune; do
    # Determine plugin path based on node
    if [[ "$name" == "hive-nexus-01" ]]; then
        plugin_path="/data/lightningd/plugins/cl-hive/cl-hive.py"
    else
        plugin_path="/opt/cl-hive/cl-hive.py"
    fi

    check_and_restart_plugin "$name" "$url" "$rune" "$plugin_path"
done

log "=== Watchdog Complete ==="
