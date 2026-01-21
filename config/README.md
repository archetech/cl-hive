# Configuration Examples

This directory contains example configuration files for cl-hive and the MCP server.

## Node Configuration

Choose the configuration style that matches your setup:

### REST API (Production)
Use `nodes.rest.example.json` for nodes accessed via CLN's REST API:
```bash
cp nodes.rest.example.json nodes.json
# Edit with your actual node URLs and runes
```

### Docker (Development/Polar)
Use `nodes.docker.example.json` for nodes in Docker containers:
```bash
cp nodes.docker.example.json nodes.json
# Edit with your container names
```

### Mixed Mode (Production + Docker)
Use `nodes.mixed.example.json` when you have both REST and Docker nodes:
```bash
cp nodes.mixed.example.json nodes.json
# Per-node "mode" overrides the global mode
```

## MCP Server Configuration

The `mcp-config.example.json` shows how to configure Claude Code to use the hive MCP server.

Copy to your Claude Code config location (typically `~/.claude/claude_code_config.json`):
```bash
cp mcp-config.example.json ~/.claude/claude_code_config.json
# Edit paths to match your installation
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `HIVE_NODES_CONFIG` | Path to nodes.json | Required |
| `ADVISOR_DB_PATH` | Path to advisor SQLite DB | `~/.lightning/advisor.db` |
| `HIVE_STRATEGY_DIR` | Path to strategy prompts | Optional |

## Strategy Prompts

The `strategy-prompts.example/` directory contains templates for AI advisor prompts.

### Setting Up Strategy Prompts

1. Copy the example directory:
   ```bash
   cp -r strategy-prompts.example/ /path/to/your/strategy-prompts/
   ```

2. Edit `system_prompt.md` with your node's context:
   - Update node name and metrics
   - Set your operating mode (GROWTH/CONSOLIDATION/MAINTENANCE)
   - Adjust priorities based on your strategy

3. Edit `approval_criteria.md` with your specific criteria:
   - Adjust thresholds for your node size
   - Set fee ranges appropriate for your strategy
   - Define your on-chain reserve requirements

4. Set the `HIVE_STRATEGY_DIR` environment variable:
   ```bash
   export HIVE_STRATEGY_DIR=/path/to/your/strategy-prompts
   ```

### Strategy Prompt Files

| File | Purpose |
|------|---------|
| `system_prompt.md` | Main AI advisor instructions with node context |
| `approval_criteria.md` | Criteria for approving/rejecting actions |

The MCP server loads these automatically and appends them to relevant tool descriptions.

## Security Notes

- **Never commit** actual runes, API keys, or sensitive node information
- Keep production configs outside the repository
- Use `.gitignore` to exclude `nodes.json` and other sensitive files
- The `.example` suffix indicates files safe to commit

## Directory Structure

```
config/
├── README.md                      # This file
├── nodes.rest.example.json        # REST API config example
├── nodes.docker.example.json      # Docker/Polar config example
├── nodes.mixed.example.json       # Mixed mode config example
├── mcp-config.example.json        # Claude Code MCP config example
└── strategy-prompts.example/      # AI advisor prompt templates
    ├── system_prompt.md           # Main system prompt
    └── approval_criteria.md       # Action approval criteria
```
