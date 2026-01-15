#!/usr/bin/env python3
"""
MCP Server for cl-hive Fleet Management

This MCP server allows Claude Code to manage a fleet of Lightning nodes
running cl-hive. It connects to nodes via CLN's REST API and exposes
tools for:
- Viewing pending actions and approving/rejecting them
- Checking hive status across all nodes
- Broadcasting AI messages to the fleet
- Managing channels and topology

Usage:
    # Add to Claude Code settings (~/.claude/claude_code_config.json):
    {
      "mcpServers": {
        "hive": {
          "command": "python3",
          "args": ["/path/to/mcp-hive-server.py"],
          "env": {
            "HIVE_NODES_CONFIG": "/path/to/nodes.json"
          }
        }
      }
    }

    # nodes.json format:
    {
      "nodes": [
        {
          "name": "alice",
          "rest_url": "https://localhost:8181",
          "rune": "...",
          "ca_cert": "/path/to/ca.pem"
        }
      ]
    }
"""

import asyncio
import json
import logging
import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

# MCP SDK imports
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
except ImportError:
    print("MCP SDK not installed. Run: pip install mcp")
    raise

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-hive")

# =============================================================================
# Node Connection
# =============================================================================

@dataclass
class NodeConnection:
    """Connection to a CLN node via REST API or Docker exec (for Polar)."""
    name: str
    rest_url: str = ""
    rune: str = ""
    ca_cert: Optional[str] = None
    client: Optional[httpx.AsyncClient] = None
    # Polar/Docker mode
    docker_container: Optional[str] = None
    lightning_dir: str = "/home/clightning/.lightning"
    network: str = "regtest"

    async def connect(self):
        """Initialize the HTTP client (if using REST)."""
        if self.docker_container:
            logger.info(f"Using docker exec for {self.name} ({self.docker_container})")
            return

        ssl_context = None
        if self.ca_cert and os.path.exists(self.ca_cert):
            ssl_context = ssl.create_default_context()
            ssl_context.load_verify_locations(self.ca_cert)

        self.client = httpx.AsyncClient(
            base_url=self.rest_url,
            headers={"Rune": self.rune},
            verify=ssl_context if ssl_context else False,
            timeout=30.0
        )
        logger.info(f"Connected to {self.name} at {self.rest_url}")

    async def close(self):
        """Close the HTTP client."""
        if self.client:
            await self.client.aclose()

    async def call(self, method: str, params: Dict = None) -> Dict:
        """Call a CLN RPC method via REST or docker exec."""
        # Docker exec mode (for Polar)
        if self.docker_container:
            return await self._call_docker(method, params)

        # REST mode
        if not self.client:
            await self.connect()

        try:
            response = await self.client.post(
                f"/v1/{method}",
                json=params or {}
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"RPC error on {self.name}: {e}")
            return {"error": str(e)}

    async def _call_docker(self, method: str, params: Dict = None) -> Dict:
        """Call CLN via docker exec (for Polar testing)."""
        import subprocess

        # Build command
        cmd = [
            "docker", "exec", self.docker_container,
            "lightning-cli",
            f"--lightning-dir={self.lightning_dir}",
            f"--network={self.network}",
            method
        ]

        # Add params as JSON if provided
        if params:
            for key, value in params.items():
                if isinstance(value, bool):
                    cmd.append(f"{key}={'true' if value else 'false'}")
                elif isinstance(value, (int, float)):
                    cmd.append(f"{key}={value}")
                elif isinstance(value, str):
                    cmd.append(f"{key}={value}")
                else:
                    cmd.append(f"{key}={json.dumps(value)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode != 0:
                return {"error": result.stderr.strip()}
            return json.loads(result.stdout) if result.stdout.strip() else {}
        except subprocess.TimeoutExpired:
            return {"error": "Command timed out"}
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON response: {e}"}
        except Exception as e:
            return {"error": str(e)}


class HiveFleet:
    """Manages connections to multiple Hive nodes."""

    def __init__(self):
        self.nodes: Dict[str, NodeConnection] = {}

    def load_config(self, config_path: str):
        """Load node configuration from JSON file."""
        with open(config_path) as f:
            config = json.load(f)

        mode = config.get("mode", "rest")
        network = config.get("network", "regtest")
        lightning_dir = config.get("lightning_dir", "/home/clightning/.lightning")

        for node_config in config.get("nodes", []):
            if mode == "docker":
                # Docker exec mode (for Polar testing)
                node = NodeConnection(
                    name=node_config["name"],
                    docker_container=node_config["docker_container"],
                    lightning_dir=lightning_dir,
                    network=network
                )
            else:
                # REST mode (for production)
                node = NodeConnection(
                    name=node_config["name"],
                    rest_url=node_config["rest_url"],
                    rune=node_config["rune"],
                    ca_cert=node_config.get("ca_cert")
                )
            self.nodes[node.name] = node

        logger.info(f"Loaded {len(self.nodes)} nodes from config (mode={mode})")

    async def connect_all(self):
        """Connect to all nodes."""
        for node in self.nodes.values():
            try:
                await node.connect()
            except Exception as e:
                logger.error(f"Failed to connect to {node.name}: {e}")

    async def close_all(self):
        """Close all connections."""
        for node in self.nodes.values():
            await node.close()

    def get_node(self, name: str) -> Optional[NodeConnection]:
        """Get a node by name."""
        return self.nodes.get(name)

    async def call_all(self, method: str, params: Dict = None) -> Dict[str, Any]:
        """Call an RPC method on all nodes."""
        results = {}
        for name, node in self.nodes.items():
            results[name] = await node.call(method, params)
        return results


# Global fleet instance
fleet = HiveFleet()


# =============================================================================
# MCP Server
# =============================================================================

server = Server("hive-fleet-manager")


@server.list_tools()
async def list_tools() -> List[Tool]:
    """List available tools for Hive management."""
    return [
        Tool(
            name="hive_status",
            description="Get status of all Hive nodes in the fleet. Shows membership, health, and pending actions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {
                        "type": "string",
                        "description": "Specific node name (optional, defaults to all nodes)"
                    }
                }
            }
        ),
        Tool(
            name="hive_pending_actions",
            description="Get pending actions that need approval across the fleet. Shows channel opens, bans, expansions waiting for decision.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {
                        "type": "string",
                        "description": "Specific node name (optional, defaults to all nodes)"
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "approved", "rejected", "executed"],
                        "description": "Filter by status (default: pending)"
                    }
                }
            }
        ),
        Tool(
            name="hive_approve_action",
            description="Approve a pending action on a node. The action will be executed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {
                        "type": "string",
                        "description": "Node name where action exists"
                    },
                    "action_id": {
                        "type": "integer",
                        "description": "ID of the action to approve"
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason for approval"
                    }
                },
                "required": ["node", "action_id"]
            }
        ),
        Tool(
            name="hive_reject_action",
            description="Reject a pending action on a node. The action will not be executed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {
                        "type": "string",
                        "description": "Node name where action exists"
                    },
                    "action_id": {
                        "type": "integer",
                        "description": "ID of the action to reject"
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason for rejection"
                    }
                },
                "required": ["node", "action_id", "reason"]
            }
        ),
        Tool(
            name="hive_members",
            description="List all members of the Hive with their status and health scores.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {
                        "type": "string",
                        "description": "Node to query (optional, defaults to first node)"
                    }
                }
            }
        ),
        Tool(
            name="hive_node_info",
            description="Get detailed info about a specific Lightning node including channels, balance, and peers.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {
                        "type": "string",
                        "description": "Node name to get info for"
                    }
                },
                "required": ["node"]
            }
        ),
        Tool(
            name="hive_channels",
            description="List channels for a node with balance and fee information.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {
                        "type": "string",
                        "description": "Node name"
                    }
                },
                "required": ["node"]
            }
        ),
        Tool(
            name="hive_set_fees",
            description="Set channel fees for a specific channel on a node.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {
                        "type": "string",
                        "description": "Node name"
                    },
                    "channel_id": {
                        "type": "string",
                        "description": "Channel ID (short_channel_id format)"
                    },
                    "fee_ppm": {
                        "type": "integer",
                        "description": "Fee rate in parts per million"
                    },
                    "base_fee_msat": {
                        "type": "integer",
                        "description": "Base fee in millisatoshis (default: 0)"
                    }
                },
                "required": ["node", "channel_id", "fee_ppm"]
            }
        ),
        Tool(
            name="hive_topology_analysis",
            description="Get topology analysis from the Hive planner. Shows opportunities for channel opens and optimizations.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {
                        "type": "string",
                        "description": "Node to analyze"
                    }
                },
                "required": ["node"]
            }
        ),
        Tool(
            name="hive_broadcast_message",
            description="Broadcast an AI Oracle message to all Hive members.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {
                        "type": "string",
                        "description": "Node to broadcast from"
                    },
                    "msg_type": {
                        "type": "string",
                        "enum": ["ai_state_summary", "ai_opportunity_signal", "ai_alert"],
                        "description": "Type of AI message to broadcast"
                    },
                    "payload": {
                        "type": "object",
                        "description": "Message payload"
                    }
                },
                "required": ["node", "msg_type", "payload"]
            }
        ),
        Tool(
            name="hive_governance_mode",
            description="Get or set the governance mode for a node (advisor, autonomous, oracle).",
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {
                        "type": "string",
                        "description": "Node name"
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["advisor", "autonomous", "oracle"],
                        "description": "New mode to set (optional, omit to just get current mode)"
                    }
                },
                "required": ["node"]
            }
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: Dict) -> List[TextContent]:
    """Handle tool calls."""

    try:
        if name == "hive_status":
            result = await handle_hive_status(arguments)
        elif name == "hive_pending_actions":
            result = await handle_pending_actions(arguments)
        elif name == "hive_approve_action":
            result = await handle_approve_action(arguments)
        elif name == "hive_reject_action":
            result = await handle_reject_action(arguments)
        elif name == "hive_members":
            result = await handle_members(arguments)
        elif name == "hive_node_info":
            result = await handle_node_info(arguments)
        elif name == "hive_channels":
            result = await handle_channels(arguments)
        elif name == "hive_set_fees":
            result = await handle_set_fees(arguments)
        elif name == "hive_topology_analysis":
            result = await handle_topology_analysis(arguments)
        elif name == "hive_broadcast_message":
            result = await handle_broadcast_message(arguments)
        elif name == "hive_governance_mode":
            result = await handle_governance_mode(arguments)
        else:
            result = {"error": f"Unknown tool: {name}"}

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    except Exception as e:
        logger.exception(f"Error in tool {name}")
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


# =============================================================================
# Tool Handlers
# =============================================================================

async def handle_hive_status(args: Dict) -> Dict:
    """Get Hive status from nodes."""
    node_name = args.get("node")

    if node_name:
        node = fleet.get_node(node_name)
        if not node:
            return {"error": f"Unknown node: {node_name}"}
        result = await node.call("hive-status")
        return {node_name: result}
    else:
        return await fleet.call_all("hive-status")


async def handle_pending_actions(args: Dict) -> Dict:
    """Get pending actions from nodes."""
    node_name = args.get("node")
    status = args.get("status", "pending")

    if node_name:
        node = fleet.get_node(node_name)
        if not node:
            return {"error": f"Unknown node: {node_name}"}
        result = await node.call("hive-pending-actions", {"status": status})
        return {node_name: result}
    else:
        results = {}
        for name, node in fleet.nodes.items():
            results[name] = await node.call("hive-pending-actions", {"status": status})
        return results


async def handle_approve_action(args: Dict) -> Dict:
    """Approve a pending action."""
    node_name = args.get("node")
    action_id = args.get("action_id")
    reason = args.get("reason", "Approved by Claude Code")

    node = fleet.get_node(node_name)
    if not node:
        return {"error": f"Unknown node: {node_name}"}

    return await node.call("hive-approve-action", {
        "action_id": action_id,
        "reason": reason
    })


async def handle_reject_action(args: Dict) -> Dict:
    """Reject a pending action."""
    node_name = args.get("node")
    action_id = args.get("action_id")
    reason = args.get("reason")

    node = fleet.get_node(node_name)
    if not node:
        return {"error": f"Unknown node: {node_name}"}

    return await node.call("hive-reject-action", {
        "action_id": action_id,
        "reason": reason
    })


async def handle_members(args: Dict) -> Dict:
    """Get Hive members."""
    node_name = args.get("node")

    if node_name:
        node = fleet.get_node(node_name)
    else:
        # Use first available node
        node = next(iter(fleet.nodes.values()), None)

    if not node:
        return {"error": "No nodes available"}

    return await node.call("hive-members")


async def handle_node_info(args: Dict) -> Dict:
    """Get node info."""
    node_name = args.get("node")

    node = fleet.get_node(node_name)
    if not node:
        return {"error": f"Unknown node: {node_name}"}

    info = await node.call("getinfo")
    funds = await node.call("listfunds")

    return {
        "info": info,
        "funds_summary": {
            "onchain_sats": sum(o.get("amount_msat", 0) // 1000
                               for o in funds.get("outputs", [])
                               if o.get("status") == "confirmed"),
            "channel_count": len(funds.get("channels", [])),
            "total_channel_sats": sum(c.get("amount_msat", 0) // 1000
                                      for c in funds.get("channels", []))
        }
    }


async def handle_channels(args: Dict) -> Dict:
    """Get channel list."""
    node_name = args.get("node")

    node = fleet.get_node(node_name)
    if not node:
        return {"error": f"Unknown node: {node_name}"}

    return await node.call("listpeerchannels")


async def handle_set_fees(args: Dict) -> Dict:
    """Set channel fees."""
    node_name = args.get("node")
    channel_id = args.get("channel_id")
    fee_ppm = args.get("fee_ppm")
    base_fee_msat = args.get("base_fee_msat", 0)

    node = fleet.get_node(node_name)
    if not node:
        return {"error": f"Unknown node: {node_name}"}

    return await node.call("setchannel", {
        "id": channel_id,
        "feebase": base_fee_msat,
        "feeppm": fee_ppm
    })


async def handle_topology_analysis(args: Dict) -> Dict:
    """Get topology analysis from planner log and topology view."""
    node_name = args.get("node")

    node = fleet.get_node(node_name)
    if not node:
        return {"error": f"Unknown node: {node_name}"}

    # Get both planner log and topology info
    planner_log = await node.call("hive-planner-log", {"limit": 10})
    topology = await node.call("hive-topology")

    return {
        "planner_log": planner_log,
        "topology": topology
    }


async def handle_broadcast_message(args: Dict) -> Dict:
    """Broadcast AI message."""
    node_name = args.get("node")
    msg_type = args.get("msg_type")
    payload = args.get("payload", {})

    node = fleet.get_node(node_name)
    if not node:
        return {"error": f"Unknown node: {node_name}"}

    return await node.call("hive-ai-broadcast", {
        "msg_type": msg_type,
        "payload": payload
    })


async def handle_governance_mode(args: Dict) -> Dict:
    """Get or set governance mode."""
    node_name = args.get("node")
    mode = args.get("mode")

    node = fleet.get_node(node_name)
    if not node:
        return {"error": f"Unknown node: {node_name}"}

    if mode:
        return await node.call("hive-set-mode", {"mode": mode})
    else:
        status = await node.call("hive-status")
        return {"mode": status.get("governance_mode", "unknown")}


# =============================================================================
# Main
# =============================================================================

async def main():
    """Run the MCP server."""
    # Load node configuration
    config_path = os.environ.get("HIVE_NODES_CONFIG")
    if config_path and os.path.exists(config_path):
        fleet.load_config(config_path)
        await fleet.connect_all()
    else:
        logger.warning("No HIVE_NODES_CONFIG set - running without nodes")
        logger.info("Set HIVE_NODES_CONFIG=/path/to/nodes.json to connect to nodes")

    # Run the MCP server
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

    # Cleanup
    await fleet.close_all()


if __name__ == "__main__":
    asyncio.run(main())
