#!/bin/bash
#
# Hive Proactive AI Advisor Runner Script
# Runs Claude Code with MCP server to execute the proactive advisor cycle on ALL nodes
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
echo "=== Hive AI Advisor Run: $(date) ===" | tee -a "$LOG_FILE"
echo "================================================================================" >> "$LOG_FILE"

# Load system prompt from file
if [[ -f "${PROD_DIR}/strategy-prompts/system_prompt.md" ]]; then
    SYSTEM_PROMPT=$(cat "${PROD_DIR}/strategy-prompts/system_prompt.md")
else
    echo "WARNING: System prompt file not found, using default" | tee -a "$LOG_FILE"
    SYSTEM_PROMPT="You are an AI advisor for a Lightning node. Review pending actions and make decisions."
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
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
MCPEOF

# Run Claude with MCP server
# The proactive advisor runs a complete 9-phase optimization cycle on ALL nodes:
# 1) Record snapshot 2) Analyze state 3) Check goals 4) Scan opportunities
# 5) Score with learning 6) Auto-execute safe actions 7) Queue risky actions
# 8) Measure outcomes 9) Plan next cycle
# --allowedTools restricts to only hive/revenue/advisor tools for safety
claude -p "Run the proactive advisor cycle on ALL nodes using advisor_run_cycle_all. After the cycle completes, provide a report with these sections:

## FLEET HEALTH (use advisor_get_trends and hive_status)
- Total nodes and their status (online/offline)
- Fleet-wide capacity and revenue trends (7-day)
- Hive membership summary (members/neophytes)
- Any internal competition or coordination issues

## PER-NODE SUMMARIES (for each node)
1) Node state (capacity, channels, ROC%, underwater%)
2) Goals progress and strategy adjustments needed
3) Opportunities found by type and actions taken/queued
4) Next cycle priorities

## PENDING ACTIONS (check hive_pending_actions on each node)
- List actions needing human review with your recommendations" \
    --mcp-config "$MCP_CONFIG_TMP" \
    --system-prompt "$SYSTEM_PROMPT" \
    --model sonnet \
    --max-budget-usd 0.50 \
    --allowedTools "mcp__hive__*" \
    2>&1 | tee -a "$LOG_FILE"

echo "=== Run completed: $(date) ===" | tee -a "$LOG_FILE"

# Cleanup old logs (keep last 7 days)
find "$LOG_DIR" -name "advisor_*.log" -mtime +7 -delete 2>/dev/null || true

exit 0
