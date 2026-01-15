# MCP Server for Claude Code Integration

The `mcp-hive-server.py` provides a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that allows Claude Code to manage your Hive fleet directly. This turns Claude Code into an AI oracle that can monitor, analyze, and execute decisions across your Lightning node fleet.

## Overview

Instead of running an embedded oracle plugin on each node, this approach lets Claude Code itself act as the AI decision-maker:

```
┌─────────────────┐
│   Claude Code   │  ← AI Decision Making
│  (MCP Client)   │
└────────┬────────┘
         │ MCP Protocol
         ▼
┌─────────────────┐
│ mcp-hive-server │  ← Fleet Management Tools
└────────┬────────┘
         │ REST API / Docker Exec
         ▼
┌─────────────────────────────────────┐
│  Hive Fleet (alice, bob, carol...)  │
│  Running cl-hive plugin             │
└─────────────────────────────────────┘
```

## Prerequisites

- Python 3.10+
- Claude Code CLI installed
- Hive fleet running with cl-hive plugin

## Installation

### 1. Create Python Virtual Environment

```bash
cd /path/to/cl-hive
python3 -m venv .venv
.venv/bin/pip install mcp httpx
```

### 2. Create Node Configuration

Create a `nodes.json` file with your fleet configuration. Two modes are supported:

#### Production Mode (REST API)

For production nodes with CLN REST API enabled:

```json
{
  "mode": "rest",
  "nodes": [
    {
      "name": "node1",
      "rest_url": "https://node1.example.com:3001",
      "rune": "your-rune-here",
      "ca_cert": "/path/to/ca.pem"
    },
    {
      "name": "node2",
      "rest_url": "https://node2.example.com:3001",
      "rune": "your-rune-here",
      "ca_cert": "/path/to/ca.pem"
    }
  ]
}
```

**Getting a Rune:**
```bash
lightning-cli createrune
```

**REST API Setup:**
Ensure your CLN node has the `clnrest` plugin enabled with appropriate configuration:
```
# In your CLN config
clnrest-port=3001
clnrest-host=0.0.0.0
```

#### Development Mode (Docker Exec)

For testing with Polar or local Docker containers:

```json
{
  "mode": "docker",
  "network": "regtest",
  "lightning_dir": "/home/clightning/.lightning",
  "nodes": [
    {
      "name": "alice",
      "docker_container": "polar-n1-alice"
    },
    {
      "name": "bob",
      "docker_container": "polar-n1-bob"
    }
  ]
}
```

### 3. Configure Claude Code

Create `.mcp.json` in your cl-hive directory:

```json
{
  "mcpServers": {
    "hive": {
      "command": "/path/to/cl-hive/.venv/bin/python",
      "args": ["/path/to/cl-hive/mcp-hive-server.py"],
      "env": {
        "HIVE_NODES_CONFIG": "/path/to/cl-hive/nodes.json"
      }
    }
  }
}
```

### 4. Enable the MCP Server

Restart Claude Code in the cl-hive directory. It will detect the `.mcp.json` and prompt you to enable the hive server.

## Available Tools

| Tool | Description |
|------|-------------|
| `hive_status` | Get hive status from all nodes (membership, health, governance mode) |
| `hive_pending_actions` | View actions awaiting approval in advisor mode |
| `hive_approve_action` | Approve a pending action for execution |
| `hive_reject_action` | Reject a pending action with reason |
| `hive_members` | List all hive members with tier and stats |
| `hive_node_info` | Get detailed node info (peers, channels, balance) |
| `hive_channels` | List channels with balance and fee information |
| `hive_set_fees` | Set channel fees for a specific channel |
| `hive_topology_analysis` | Get planner log and topology view |
| `hive_broadcast_message` | Broadcast AI oracle messages to fleet |
| `hive_governance_mode` | Get or set governance mode (advisor/autonomous/oracle) |

## Usage Examples

Once enabled, you can ask Claude Code to manage your fleet:

```
"Show me the status of all hive nodes"
"What pending actions need approval?"
"Approve action 5 on alice - good expansion target"
"Set fees to 500 PPM on channel 123x1x0 on bob"
"What's the current topology analysis for carol?"
"Switch alice to autonomous mode"
```

## Security Considerations

1. **Rune Permissions**: Create restricted runes that only allow the RPC methods needed:
   ```bash
   lightning-cli createrune restrictions='[["method^hive-"],["method^getinfo"],["method^listfunds"],["method^listpeerchannels"],["method^setchannel"]]'
   ```

2. **Network Security**:
   - Use TLS certificates for REST API connections
   - Consider VPN for remote node access
   - Never expose REST API to public internet without authentication

3. **Governance Mode**: Start with `advisor` mode to review all actions before execution:
   ```bash
   lightning-cli hive-set-mode advisor
   ```

## Troubleshooting

### MCP Server Won't Start

Check the virtual environment has required packages:
```bash
.venv/bin/python -c "import mcp; import httpx; print('OK')"
```

### Connection Errors (REST Mode)

1. Verify REST API is running: `curl -k https://node:3001/v1/getinfo -H "Rune: your-rune"`
2. Check CA certificate path is correct
3. Ensure firewall allows connection

### Connection Errors (Docker Mode)

1. Verify container is running: `docker ps | grep polar`
2. Test command manually: `docker exec polar-n1-alice lightning-cli getinfo`
3. Check lightning directory path matches container configuration

### Tool Errors

If a tool returns an error, check:
1. The node has cl-hive plugin loaded: `lightning-cli plugin list | grep hive`
2. The specific RPC command exists: `lightning-cli help | grep hive`

## Development

### Testing Without MCP

You can test the server's functionality directly:

```python
import asyncio
import json

# Test docker exec mode
async def test():
    from mcp_hive_server import HiveFleet

    fleet = HiveFleet()
    fleet.load_config("nodes.json")
    await fleet.connect_all()

    # Test hive-status on all nodes
    results = await fleet.call_all("hive-status")
    print(json.dumps(results, indent=2))

    await fleet.close_all()

asyncio.run(test())
```

### Adding New Tools

To add a new tool:

1. Add the tool definition in `list_tools()`
2. Add a handler function `handle_your_tool(args)`
3. Add the dispatch in `call_tool()`

## Related Documentation

- [AI Oracle Protocol](specs/ai-oracle-protocol.md) - Message types for AI communication
- [Governance Modes](../README.md#governance-modes) - Understanding advisor/autonomous/oracle modes
- [Polar Testing](testing/polar.md) - Testing with Polar network
