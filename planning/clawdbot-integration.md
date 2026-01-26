# Clawdbot Integration Options for cl-hive

This document explores three approaches for integrating [Clawdbot](https://github.com/clawdbot/clawdbot) with cl-hive for Lightning node fleet management.

## Why Clawdbot?

Clawdbot provides several advantages over the current Claude Code CLI approach:

| Feature | Claude Code CLI | Clawdbot |
|---------|-----------------|----------|
| Interface | Terminal only | Telegram, Discord, Slack, Signal, iMessage, etc. |
| AI Backend | Claude only | Anthropic, OpenAI, local models (configurable) |
| Memory | Session-based | Persistent across conversations |
| Proactive | Timer-based scripts | Built-in cron, webhooks, proactive messaging |
| Mobile | Limited | Full mobile access via chat apps |
| Notifications | None | Push to any connected channel |

## Current cl-hive Architecture

```
┌─────────────────┐
│   Claude Code   │  ← AI Decision Making
│  (MCP Client)   │
└────────┬────────┘
         │ MCP Protocol (stdio)
         ▼
┌─────────────────┐
│ mcp-hive-server │  ← 60+ Fleet Management Tools
└────────┬────────┘
         │ REST API / Docker Exec
         ▼
┌─────────────────────────────────────┐
│  Hive Fleet (alice, bob, carol...)  │
│  Running cl-hive + cl-revenue-ops   │
└─────────────────────────────────────┘
```

---

## Option A: MCP Bridge

Connect Clawdbot directly to the existing `mcp-hive-server.py` via MCP protocol.

### Architecture

```
┌──────────────────────────────────────┐
│  User (Telegram/Discord/Signal/etc)  │
└────────────────┬─────────────────────┘
                 │
┌────────────────▼─────────────────────┐
│         Clawdbot Gateway             │
│  (ws://127.0.0.1:18789)              │
└────────────────┬─────────────────────┘
                 │ MCP Client
┌────────────────▼─────────────────────┐
│         mcp-hive-server              │
│  (existing 60+ tools)                │
└────────────────┬─────────────────────┘
                 │ REST API
┌────────────────▼─────────────────────┐
│           Hive Fleet                 │
└──────────────────────────────────────┘
```

### Implementation

1. **Configure Clawdbot's MCP Registry** (`~/.clawdbot/clawdbot.json`):

```json
{
  "mcp": {
    "servers": {
      "hive": {
        "command": "/path/to/cl-hive/.venv/bin/python",
        "args": ["/path/to/cl-hive/tools/mcp-hive-server.py"],
        "env": {
          "HIVE_NODES_CONFIG": "/path/to/nodes.json",
          "ADVISOR_DB_PATH": "/path/to/advisor.db"
        }
      }
    }
  }
}
```

2. **Tool Exposure**: Clawdbot would automatically discover all 60+ MCP tools:
   - `hive_status`, `hive_pending_actions`, `hive_approve_action`
   - `revenue_profitability`, `revenue_dashboard`, `revenue_rebalance`
   - `advisor_get_trends`, `advisor_get_velocities`, etc.

### Pros
- Zero changes to cl-hive codebase
- Reuses existing, tested MCP tools
- Full feature parity with Claude Code CLI
- Clawdbot handles chat interface, memory, notifications

### Cons
- Requires Clawdbot MCP support (documented but not fully detailed)
- Two processes running (Clawdbot Gateway + MCP server)
- May need tool allowlist configuration to avoid exposing dangerous tools

### Status
**Research needed**: Clawdbot's MCP Registry docs are sparse. Need to verify exact configuration format.

---

## Option B: Clawdbot Skill

Create a dedicated `cl-hive` skill that teaches Clawdbot how to manage Lightning nodes.

### Architecture

```
┌──────────────────────────────────────┐
│  User (Telegram/Discord/Signal/etc)  │
└────────────────┬─────────────────────┘
                 │
┌────────────────▼─────────────────────┐
│         Clawdbot Gateway             │
│  + cl-hive skill (SKILL.md)          │
└────────────────┬─────────────────────┘
                 │ HTTP/REST calls
┌────────────────▼─────────────────────┐
│      CLN REST API (direct)           │
│      or mcp-hive-server (proxy)      │
└────────────────┬─────────────────────┘
                 │
┌────────────────▼─────────────────────┘
│           Hive Fleet                 │
└──────────────────────────────────────┘
```

### Skill Definition

Create `~/.clawdbot/skills/cl-hive/SKILL.md`:

```yaml
---
name: cl-hive
description: Manage Lightning Network node fleets with cl-hive swarm intelligence
homepage: https://github.com/lightning-goats/cl-hive
user-invocable: true
metadata: {"clawdbot":{"emoji":"⚡","requires":{"config":["hive.nodes_config"]}}}
---

# cl-hive Fleet Manager

You have access to manage a Lightning Network node fleet running cl-hive.

## Configuration

Nodes are configured at: {config:hive.nodes_config}

## Available Operations

### Status & Monitoring
- `/hive status` - Get fleet-wide status
- `/hive pending` - Show pending actions needing approval
- `/hive members` - List hive members and tiers
- `/hive dashboard` - Financial health overview

### Action Management
- `/hive approve <id> [reason]` - Approve a pending action
- `/hive reject <id> <reason>` - Reject a pending action

### Financial
- `/hive profitability [node]` - Channel ROI analysis
- `/hive trends` - 7/30 day fleet trends
- `/hive velocities` - Channels depleting/filling soon

### Channel Management
- `/hive channels [node]` - List channels with balances
- `/hive fees <channel> <ppm>` - Set channel fees

## Interaction Pattern

When the user asks about their Lightning fleet:
1. First call hive_status to understand current state
2. Check hive_pending_actions for anything needing attention
3. Use advisor tools for trend analysis before recommendations
4. Always explain reasoning before approving/rejecting actions

## Safety Rules

- Never approve actions without explaining the reasoning
- Always check profitability before recommending fee changes
- Warn about high-feerate environments before channel opens
- Default to advisor mode - don't auto-execute without permission
```

### Supporting Tool Configuration

Create `~/.clawdbot/skills/cl-hive/tools.json`:

```json
{
  "tools": [
    {
      "name": "hive_call",
      "description": "Call the cl-hive MCP server",
      "type": "http",
      "config": {
        "base_url": "http://localhost:18800",
        "methods": ["hive_status", "hive_pending_actions", "hive_approve_action", "..."]
      }
    }
  ]
}
```

### Pros
- Natural chat interface (`/hive status`, `/hive approve 5`)
- Skill can include domain-specific prompting
- Works with Clawdbot's memory system
- Can be published to Clawdbot skill ecosystem

### Cons
- Duplicates tool definitions (MCP server + skill)
- Requires HTTP wrapper around MCP server (Clawdbot skills prefer HTTP)
- Maintenance burden: skill must stay in sync with MCP tools

### Implementation Path

1. Create HTTP wrapper for `mcp-hive-server.py` (or use MCP-over-HTTP)
2. Write SKILL.md with tool definitions
3. Add skill configuration for node credentials
4. Test via Clawdbot's skill development mode

---

## Option C: Direct Integration (Clawdbot as AI Backend)

Embed Clawdbot support directly into cl-hive as an alternative to Claude Code CLI.

### Architecture

```
┌──────────────────────────────────────┐
│           cl-hive plugin             │
│  ┌─────────────────────────────────┐ │
│  │     AI Backend Abstraction      │ │
│  │  ┌─────────┐    ┌────────────┐  │ │
│  │  │ Claude  │ OR │  Clawdbot  │  │ │
│  │  │  Code   │    │  Gateway   │  │ │
│  │  └────┬────┘    └─────┬──────┘  │ │
│  └───────┼───────────────┼─────────┘ │
│          ▼               ▼           │
│  ┌─────────────────────────────────┐ │
│  │    proactive_advisor.py         │ │
│  │    (decision engine)            │ │
│  └─────────────────────────────────┘ │
└──────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────┐
│  User Notifications                  │
│  (Telegram/Discord via Clawdbot)     │
└──────────────────────────────────────┘
```

### Implementation: Clawdbot Bridge Module

Create `tools/clawdbot_bridge.py`:

```python
"""
Bridge for Clawdbot integration.

Allows cl-hive to use Clawdbot as the AI backend instead of Claude Code CLI.
Provides bidirectional communication:
- Send fleet status/alerts to user via Clawdbot
- Receive commands from user via Clawdbot webhook
"""

import asyncio
import httpx
import json
from dataclasses import dataclass
from typing import Optional, Callable, Dict, Any

CLAWDBOT_WS = "ws://127.0.0.1:18789"
CLAWDBOT_API = "http://127.0.0.1:18790"


@dataclass
class ClawdbotConfig:
    """Configuration for Clawdbot integration."""
    enabled: bool = False
    gateway_url: str = CLAWDBOT_WS
    agent_id: str = "hive-manager"  # Clawdbot agent to use
    channel: str = "telegram"  # Default notification channel
    chat_id: Optional[str] = None  # Telegram/Discord chat ID


class ClawdbotBridge:
    """Bridge between cl-hive and Clawdbot."""

    def __init__(self, config: ClawdbotConfig):
        self.config = config
        self._client: Optional[httpx.AsyncClient] = None
        self._webhook_handlers: Dict[str, Callable] = {}

    async def connect(self):
        """Connect to Clawdbot gateway."""
        if not self.config.enabled:
            return
        self._client = httpx.AsyncClient(base_url=self.config.gateway_url)

    async def send_message(self, message: str, priority: str = "normal"):
        """Send message to user via Clawdbot."""
        if not self._client:
            return

        await self._client.post("/api/message", json={
            "agent": self.config.agent_id,
            "channel": self.config.channel,
            "chat_id": self.config.chat_id,
            "content": message,
            "priority": priority
        })

    async def send_alert(self, title: str, body: str, action_id: Optional[int] = None):
        """Send actionable alert to user."""
        message = f"⚡ **{title}**\n\n{body}"
        if action_id:
            message += f"\n\nReply `/hive approve {action_id}` or `/hive reject {action_id} <reason>`"
        await self.send_message(message, priority="high")

    async def send_daily_report(self, report: dict):
        """Send formatted daily report."""
        # Format report for chat consumption
        lines = [
            "📊 **Daily Hive Report**",
            "",
            f"💰 Revenue: {report.get('revenue_sats', 0):,} sats",
            f"📈 ROC: {report.get('roc', 0):.2%}",
            f"⚡ Forwards: {report.get('forwards', 0)}",
            f"🔋 Avg Balance: {report.get('avg_balance_pct', 0):.0%}",
            "",
            f"📋 Pending Actions: {report.get('pending_count', 0)}",
        ]
        await self.send_message("\n".join(lines))

    def register_webhook(self, command: str, handler: Callable):
        """Register handler for incoming Clawdbot commands."""
        self._webhook_handlers[command] = handler

    async def handle_webhook(self, payload: dict) -> str:
        """Handle incoming webhook from Clawdbot."""
        command = payload.get("command", "")
        args = payload.get("args", [])

        if command in self._webhook_handlers:
            return await self._webhook_handlers[command](*args)
        return f"Unknown command: {command}"


# Singleton instance
_bridge: Optional[ClawdbotBridge] = None

def get_bridge() -> Optional[ClawdbotBridge]:
    return _bridge

def init_bridge(config: ClawdbotConfig) -> ClawdbotBridge:
    global _bridge
    _bridge = ClawdbotBridge(config)
    return _bridge
```

### Configuration Options

Add to `cl-hive.py` plugin options:

```python
# Clawdbot integration (optional)
plugin.add_option(
    "hive-clawdbot-enabled",
    default=False,
    description="Enable Clawdbot integration for chat-based management"
)
plugin.add_option(
    "hive-clawdbot-gateway",
    default="ws://127.0.0.1:18789",
    description="Clawdbot gateway WebSocket URL"
)
plugin.add_option(
    "hive-clawdbot-channel",
    default="telegram",
    description="Default notification channel (telegram/discord/slack)"
)
plugin.add_option(
    "hive-clawdbot-chat-id",
    default="",
    description="Chat/channel ID for notifications"
)
```

### Integration with Proactive Advisor

Modify `tools/proactive_advisor.py`:

```python
# At cycle end, notify via Clawdbot if enabled
from clawdbot_bridge import get_bridge

async def notify_pending_actions(actions: list):
    """Notify user of pending actions via Clawdbot."""
    bridge = get_bridge()
    if not bridge:
        return

    for action in actions[:5]:  # Max 5 notifications
        await bridge.send_alert(
            title=f"Pending: {action['action_type']}",
            body=action['description'],
            action_id=action['id']
        )
```

### Webhook Endpoint

Add HTTP endpoint for Clawdbot to call back:

```python
# In mcp-hive-server.py or separate webhook server

from aiohttp import web
from clawdbot_bridge import get_bridge

async def clawdbot_webhook(request):
    """Handle commands from Clawdbot."""
    payload = await request.json()
    bridge = get_bridge()

    if not bridge:
        return web.json_response({"error": "Clawdbot not enabled"})

    result = await bridge.handle_webhook(payload)
    return web.json_response({"result": result})

# Register routes
app = web.Application()
app.router.add_post("/webhook/clawdbot", clawdbot_webhook)
```

### Pros
- Deepest integration - Clawdbot becomes first-class citizen
- Bidirectional: alerts push to user, commands come back
- Users can choose their preferred interface
- Single codebase supports both Claude Code CLI and Clawdbot

### Cons
- Most development effort
- Adds Clawdbot as optional dependency
- Need to maintain two AI interaction paths
- Webhook security considerations

---

## Comparison Matrix

| Aspect | Option A: MCP Bridge | Option B: Skill | Option C: Direct |
|--------|---------------------|-----------------|------------------|
| Development effort | Low | Medium | High |
| Changes to cl-hive | None | None | Significant |
| Feature parity | Full | Partial | Full + extras |
| Notifications | Via Clawdbot | Via Clawdbot | Push alerts |
| Two-way comms | Yes | Yes | Native |
| Maintenance | Low | Medium | High |
| User flexibility | High | High | Highest |

## Recommendation

**Start with Option A (MCP Bridge)** as it requires no changes to cl-hive and validates the integration concept. If Clawdbot's MCP support works well:

1. **Phase 1**: Configure Clawdbot to use existing MCP server
2. **Phase 2**: Create a basic skill for better UX (slash commands)
3. **Phase 3**: If demand warrants, add direct integration for push notifications

This incremental approach minimizes risk while providing immediate value.

## Next Steps

1. [ ] Test Clawdbot's MCP Registry with a simple MCP server
2. [ ] Verify `mcp-hive-server.py` works with Clawdbot as MCP client
3. [ ] Document Clawdbot setup for cl-hive users
4. [ ] Create basic SKILL.md for better discoverability
5. [ ] Evaluate push notification needs for Option C

## References

- [Clawdbot GitHub](https://github.com/clawdbot/clawdbot)
- [Clawdbot Documentation](https://docs.clawd.bot)
- [MCP Protocol Specification](https://modelcontextprotocol.io/)
- [cl-hive MCP Server](../MCP_SERVER.md)
