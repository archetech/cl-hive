#!/bin/bash
#
# Hive Proactive AI Advisor Runner Script
# Runs Claude Code with MCP server to execute the proactive advisor cycle
# The advisor analyzes state, tracks goals, scans opportunities, and learns from outcomes
#
set -euo pipefail

# Determine directories
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROD_DIR="$(dirname "$SCRIPT_DIR")"
HIVE_DIR="$(dirname "$PROD_DIR")"
LOG_DIR="${PROD_DIR}/logs"
DATE=$(date +%Y%m%d)

# Ensure log directory exists
mkdir -p "$LOG_DIR"

# Use daily log file (appends throughout the day)
LOG_FILE="${LOG_DIR}/advisor_${DATE}.log"

# Change to hive directory
cd "$HIVE_DIR"

# Activate virtual environment if it exists
if [[ -f "${HIVE_DIR}/.venv/bin/activate" ]]; then
    source "${HIVE_DIR}/.venv/bin/activate"
fi

echo "" >> "$LOG_FILE"
echo "================================================================================" >> "$LOG_FILE"
echo "=== Proactive AI Advisor Run: $(date) ===" | tee -a "$LOG_FILE"
echo "================================================================================" >> "$LOG_FILE"

# Verify strategy prompt files exist
SYSTEM_PROMPT_FILE="${PROD_DIR}/strategy-prompts/system_prompt.md"
APPROVAL_CRITERIA_FILE="${PROD_DIR}/strategy-prompts/approval_criteria.md"

if [[ ! -f "$SYSTEM_PROMPT_FILE" ]]; then
    echo "ERROR: System prompt file not found: ${SYSTEM_PROMPT_FILE}" | tee -a "$LOG_FILE"
    exit 1
fi

if [[ ! -f "$APPROVAL_CRITERIA_FILE" ]]; then
    echo "WARNING: Approval criteria file not found: ${APPROVAL_CRITERIA_FILE}" | tee -a "$LOG_FILE"
    echo "WARNING: Advisor will run without approval criteria guardrails!" | tee -a "$LOG_FILE"
fi

# Advisor database location
ADVISOR_DB="${PROD_DIR}/data/advisor.db"
mkdir -p "$(dirname "$ADVISOR_DB")"

# Generate MCP config with absolute paths
MCP_CONFIG_TMP="${PROD_DIR}/.mcp-config-runtime.json"
cat > "$MCP_CONFIG_TMP" << MCPEOF
{
  "mcpServers": {
    "hive": {
      "command": "${HIVE_DIR}/.venv/bin/python",
      "args": ["${HIVE_DIR}/tools/mcp-hive-server.py"],
      "env": {
        "HIVE_NODES_CONFIG": "${PROD_DIR}/nodes.production.json",
        "HIVE_STRATEGY_DIR": "${PROD_DIR}/strategy-prompts",
        "ADVISOR_DB_PATH": "${ADVISOR_DB}",
        "ADVISOR_LOG_DIR": "${LOG_DIR}",
        "HIVE_ALLOW_INSECURE_TLS": "true",
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
MCPEOF

# Increase Node.js heap size to handle large MCP responses
export NODE_OPTIONS="--max-old-space-size=2048"

# Run Claude with MCP server
# The advisor uses enhanced automation tools for efficient fleet management

# Build the prompt by concatenating system prompt + approval criteria + action directive.
# All content is written to a temp file and piped via stdin to avoid shell escaping issues.
ADVISOR_PROMPT_FILE=$(mktemp)
trap 'rm -f "$ADVISOR_PROMPT_FILE"' EXIT
{
    # Include the full system prompt (strategy, toolset, safety constraints, workflow)
    cat "$SYSTEM_PROMPT_FILE"
    echo ""
    echo "---"
    echo ""

    # Include approval criteria
    if [[ -f "$APPROVAL_CRITERIA_FILE" ]]; then
        cat "$APPROVAL_CRITERIA_FILE"
        echo ""
        echo "---"
        echo ""
    fi

    # Action directive — tells the advisor to execute the workflow defined above
    cat << 'PROMPTEOF'
## Action Directive

Run the complete advisor workflow now on BOTH nodes (hive-nexus-01 and hive-nexus-02).

Follow the Every Run Workflow phases defined above exactly:

**Phase 0**: Call advisor_get_context_brief, advisor_get_goals, advisor_get_learning — establish memory and context
**Phase 1**: Call fleet_health_summary, membership_dashboard, routing_intelligence_health on BOTH nodes
**Phase 2**: Call process_all_pending(dry_run=true), review, then process_all_pending(dry_run=false)
**Phase 3**: Call advisor_measure_outcomes, config_measure_outcomes, config_effectiveness — learn from past decisions, make config adjustments if warranted
**Phase 4**: On BOTH nodes:
  - critical_velocity → identify urgent channels
  - stagnant_channels, remediate_stagnant(dry_run=true) → analyze stagnation
  - Review and SET fee anchors for channels needing fee guidance
  - rebalance_recommendations → identify rebalance needs
  - For needed rebalances: fleet_rebalance_path (check hive route), execute_hive_circular_rebalance (prefer zero-fee), revenue_rebalance (fallback)
  - advisor_scan_opportunities → find additional opportunities
  - advisor_get_trends → revenue/capacity trends
  - advisor_record_decision for EVERY action taken (fee anchors, rebalances, config changes)
**Phase 5**: Call advisor_record_snapshot, then generate ONE structured report

## Reminders
- Call tools FIRST, report EXACT values — never fabricate data
- Use revenue_fee_anchor to set soft fee targets for channels that need attention
- PREFER hive routes for rebalancing (zero-fee) — use revenue_rebalance only as fallback
- Use config_adjust to tune cl-revenue-ops parameters with tracking
- Record EVERY decision with advisor_record_decision for learning
- Do NOT call revenue_set_fee, hive_set_fees (non-hive), execute_safe_opportunities, or remediate_stagnant(dry_run=false)
- Hive-internal channels MUST stay at 0 ppm — never anchor them
- After writing "End of Report", STOP. Do not continue or regenerate.
PROMPTEOF
} > "$ADVISOR_PROMPT_FILE"

# Pipe prompt via stdin - avoids all command-line escaping issues
# Capture exit code so post-run cleanup (summary, wake event) still runs
CLAUDE_EXIT=0
claude -p \
    --mcp-config "$MCP_CONFIG_TMP" \
    --model sonnet \
    --allowedTools "mcp__hive__*" \
    --output-format text \
    < "$ADVISOR_PROMPT_FILE" \
    2>&1 | tee -a "$LOG_FILE" || CLAUDE_EXIT=$?

if [[ $CLAUDE_EXIT -ne 0 ]]; then
    echo "WARNING: Claude exited with code ${CLAUDE_EXIT}" | tee -a "$LOG_FILE"
fi

echo "=== Run completed: $(date) ===" | tee -a "$LOG_FILE"

# Cleanup old logs (keep last 7 days)
find "$LOG_DIR" -name "advisor_*.log" -mtime +7 -delete 2>/dev/null || true

# Write summary to a file for Hex to pick up on next heartbeat
SUMMARY_FILE="${PROD_DIR}/data/last-advisor-summary.txt"
{
    echo "=== Advisor Run $(date) ==="
    tail -200 "$LOG_FILE" | grep -v "^===" | head -100
} > "$SUMMARY_FILE"

# Also send wake event to OpenClaw main session via gateway API
GATEWAY_PORT=18789
WAKE_TEXT="Hive Advisor cycle completed at $(date). Review summary at: ${SUMMARY_FILE}"

curl -s -X POST "http://127.0.0.1:${GATEWAY_PORT}/api/cron/wake" \
    -H "Content-Type: application/json" \
    -d "{\"text\": \"${WAKE_TEXT}\", \"mode\": \"now\"}" \
    2>/dev/null || true

exit 0
