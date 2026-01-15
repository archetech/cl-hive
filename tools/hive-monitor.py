#!/usr/bin/env python3
"""
Hive Fleet Monitor - Real-time monitoring and daily reports

This daemon monitors Lightning nodes running cl-hive and cl-revenue-ops,
providing:
- Real-time alerts for pending actions, health issues, and events
- Daily financial and operational reports
- Continuous status tracking

Usage:
    # Start real-time monitor (daemon mode)
    ./hive-monitor.py --config nodes.json monitor

    # Generate daily report
    ./hive-monitor.py --config nodes.json report --output report.json

    # Run with cron for daily reports (add to crontab):
    # 0 9 * * * /path/to/hive-monitor.py --config /path/to/nodes.json report --output /path/to/reports/$(date +%%Y-%%m-%%d).json

Environment:
    HIVE_NODES_CONFIG - Path to nodes.json (alternative to --config)
    HIVE_MONITOR_INTERVAL - Polling interval in seconds (default: 60)
    HIVE_REPORTS_DIR - Directory for daily reports (default: ./reports)
"""

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import signal

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("hive-monitor")


# =============================================================================
# Node Connection (simplified from MCP server)
# =============================================================================

@dataclass
class NodeConnection:
    """Connection to a CLN node via docker exec (for Polar)."""
    name: str
    docker_container: str
    lightning_dir: str = "/home/clightning/.lightning"
    network: str = "regtest"

    def call(self, method: str, params: Dict = None) -> Dict:
        """Call CLN via docker exec."""
        cmd = [
            "docker", "exec", self.docker_container,
            "lightning-cli",
            f"--lightning-dir={self.lightning_dir}",
            f"--network={self.network}",
            method
        ]

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


def load_nodes(config_path: str) -> Dict[str, NodeConnection]:
    """Load node configuration."""
    with open(config_path) as f:
        config = json.load(f)

    nodes = {}
    network = config.get("network", "regtest")
    lightning_dir = config.get("lightning_dir", "/home/clightning/.lightning")

    for node_config in config.get("nodes", []):
        node = NodeConnection(
            name=node_config["name"],
            docker_container=node_config["docker_container"],
            lightning_dir=lightning_dir,
            network=network
        )
        nodes[node.name] = node

    return nodes


# =============================================================================
# State Tracking
# =============================================================================

@dataclass
class NodeState:
    """Tracked state for a node."""
    name: str
    last_check: datetime = None
    pending_action_ids: Set[int] = field(default_factory=set)
    governance_mode: str = ""
    channel_count: int = 0
    total_capacity_sats: int = 0
    onchain_sats: int = 0
    # Revenue ops state
    daily_revenue_sats: int = 0
    daily_costs_sats: int = 0
    active_rebalances: int = 0
    # Health
    is_healthy: bool = True
    last_error: str = ""


@dataclass
class Alert:
    """An alert to be reported."""
    timestamp: datetime
    node: str
    alert_type: str  # pending_action, health_issue, fee_change, rebalance, etc.
    severity: str    # info, warning, critical
    message: str
    details: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        d = asdict(self)
        d['timestamp'] = self.timestamp.isoformat()
        return d


class FleetMonitor:
    """Monitors a fleet of Hive nodes."""

    def __init__(self, nodes: Dict[str, NodeConnection]):
        self.nodes = nodes
        self.state: Dict[str, NodeState] = {}
        self.alerts: List[Alert] = []
        self.report_data: Dict[str, Any] = {}

        # Initialize state for each node
        for name in nodes:
            self.state[name] = NodeState(name=name)

    def add_alert(self, node: str, alert_type: str, severity: str,
                  message: str, details: Dict = None):
        """Add an alert."""
        alert = Alert(
            timestamp=datetime.now(),
            node=node,
            alert_type=alert_type,
            severity=severity,
            message=message,
            details=details or {}
        )
        self.alerts.append(alert)

        # Log based on severity
        log_msg = f"[{node}] {message}"
        if severity == "critical":
            logger.critical(log_msg)
        elif severity == "warning":
            logger.warning(log_msg)
        else:
            logger.info(log_msg)

    def check_node(self, name: str) -> Dict[str, Any]:
        """Check a single node's status."""
        node = self.nodes[name]
        state = self.state[name]
        state.last_check = datetime.now()

        result = {
            "name": name,
            "timestamp": state.last_check.isoformat(),
            "hive": {},
            "revenue_ops": {},
            "errors": []
        }

        # Check hive status
        hive_status = node.call("hive-status")
        if "error" in hive_status:
            result["errors"].append(f"hive-status: {hive_status['error']}")
            state.is_healthy = False
            state.last_error = hive_status['error']
        else:
            state.is_healthy = True
            state.governance_mode = hive_status.get("governance_mode", "unknown")
            result["hive"]["status"] = hive_status

        # Check pending actions
        pending = node.call("hive-pending-actions", {"status": "pending"})
        if "error" not in pending:
            actions = pending.get("actions", [])
            current_ids = {a.get("id") for a in actions}

            # Alert on new pending actions
            new_ids = current_ids - state.pending_action_ids
            for action in actions:
                if action.get("id") in new_ids:
                    self.add_alert(
                        node=name,
                        alert_type="pending_action",
                        severity="warning",
                        message=f"New pending action: {action.get('action_type')} (ID: {action.get('id')})",
                        details=action
                    )

            state.pending_action_ids = current_ids
            result["hive"]["pending_actions"] = len(actions)

        # Check revenue-ops status
        rev_status = node.call("revenue-status")
        if "error" not in rev_status:
            result["revenue_ops"]["status"] = rev_status

        # Check revenue-ops dashboard
        dashboard = node.call("revenue-dashboard", {"window_days": 1})
        if "error" not in dashboard:
            result["revenue_ops"]["dashboard_1d"] = dashboard

        # Get channel info
        funds = node.call("listfunds")
        if "error" not in funds:
            channels = funds.get("channels", [])
            state.channel_count = len(channels)
            state.total_capacity_sats = sum(
                c.get("amount_msat", 0) // 1000 for c in channels
            )
            outputs = funds.get("outputs", [])
            state.onchain_sats = sum(
                o.get("amount_msat", 0) // 1000
                for o in outputs if o.get("status") == "confirmed"
            )
            result["funds"] = {
                "channel_count": state.channel_count,
                "total_capacity_sats": state.total_capacity_sats,
                "onchain_sats": state.onchain_sats
            }

        return result

    def check_all_nodes(self) -> Dict[str, Any]:
        """Check all nodes in the fleet."""
        results = {}
        for name in self.nodes:
            try:
                results[name] = self.check_node(name)
            except Exception as e:
                logger.error(f"Error checking {name}: {e}")
                results[name] = {"error": str(e)}
        return results

    def generate_daily_report(self) -> Dict[str, Any]:
        """Generate a comprehensive daily report."""
        report = {
            "generated_at": datetime.now().isoformat(),
            "report_type": "daily",
            "fleet_summary": {},
            "nodes": {},
            "alerts_24h": [],
            "recommendations": []
        }

        total_capacity = 0
        total_onchain = 0
        total_channels = 0
        total_pending_actions = 0
        nodes_healthy = 0
        nodes_unhealthy = 0

        for name, node in self.nodes.items():
            state = self.state[name]

            # Get detailed info for each node
            node_report = {
                "name": name,
                "healthy": state.is_healthy,
                "governance_mode": state.governance_mode,
                "channels": state.channel_count,
                "capacity_sats": state.total_capacity_sats,
                "onchain_sats": state.onchain_sats,
                "pending_actions": len(state.pending_action_ids),
            }

            # Get profitability summary
            profitability = node.call("revenue-profitability")
            if "error" not in profitability:
                node_report["profitability"] = profitability

            # Get 30-day dashboard
            dashboard = node.call("revenue-dashboard", {"window_days": 30})
            if "error" not in dashboard:
                node_report["dashboard_30d"] = dashboard

            # Get history
            history = node.call("revenue-history")
            if "error" not in history:
                node_report["lifetime_history"] = history

            report["nodes"][name] = node_report

            # Aggregate stats
            total_capacity += state.total_capacity_sats
            total_onchain += state.onchain_sats
            total_channels += state.channel_count
            total_pending_actions += len(state.pending_action_ids)
            if state.is_healthy:
                nodes_healthy += 1
            else:
                nodes_unhealthy += 1

        # Fleet summary
        report["fleet_summary"] = {
            "total_nodes": len(self.nodes),
            "nodes_healthy": nodes_healthy,
            "nodes_unhealthy": nodes_unhealthy,
            "total_channels": total_channels,
            "total_capacity_sats": total_capacity,
            "total_capacity_btc": total_capacity / 100_000_000,
            "total_onchain_sats": total_onchain,
            "total_pending_actions": total_pending_actions
        }

        # Recent alerts (last 24 hours)
        cutoff = datetime.now() - timedelta(hours=24)
        report["alerts_24h"] = [
            a.to_dict() for a in self.alerts
            if a.timestamp > cutoff
        ]

        # Generate recommendations
        if total_pending_actions > 0:
            report["recommendations"].append({
                "type": "action_required",
                "message": f"{total_pending_actions} pending actions need review",
                "priority": "high"
            })

        if nodes_unhealthy > 0:
            report["recommendations"].append({
                "type": "health_check",
                "message": f"{nodes_unhealthy} node(s) reporting errors",
                "priority": "critical"
            })

        self.report_data = report
        return report


# =============================================================================
# Monitor Daemon
# =============================================================================

class MonitorDaemon:
    """Background monitor that runs continuously."""

    def __init__(self, monitor: FleetMonitor, interval: int = 60):
        self.monitor = monitor
        self.interval = interval
        self.running = False
        self.reports_dir = Path(os.environ.get("HIVE_REPORTS_DIR", "./reports"))

    async def run(self):
        """Main monitoring loop."""
        self.running = True
        logger.info(f"Starting monitor daemon (interval: {self.interval}s)")
        logger.info(f"Monitoring {len(self.monitor.nodes)} nodes")

        # Initial check
        self.monitor.check_all_nodes()

        last_daily_report = None

        while self.running:
            try:
                # Regular status check
                self.monitor.check_all_nodes()

                # Generate daily report at midnight or on first run
                now = datetime.now()
                if last_daily_report is None or now.date() > last_daily_report.date():
                    self._save_daily_report()
                    last_daily_report = now

                await asyncio.sleep(self.interval)

            except asyncio.CancelledError:
                logger.info("Monitor daemon cancelled")
                break
            except Exception as e:
                logger.error(f"Monitor error: {e}")
                await asyncio.sleep(self.interval)

    def stop(self):
        """Stop the daemon."""
        self.running = False

    def _save_daily_report(self):
        """Generate and save daily report."""
        self.reports_dir.mkdir(parents=True, exist_ok=True)

        report = self.monitor.generate_daily_report()
        filename = self.reports_dir / f"{datetime.now().strftime('%Y-%m-%d')}.json"

        with open(filename, 'w') as f:
            json.dump(report, f, indent=2)

        logger.info(f"Daily report saved to {filename}")


# =============================================================================
# CLI
# =============================================================================

def cmd_monitor(args):
    """Run the monitor daemon."""
    config_path = args.config or os.environ.get("HIVE_NODES_CONFIG")
    if not config_path:
        logger.error("No config file specified. Use --config or set HIVE_NODES_CONFIG")
        sys.exit(1)

    nodes = load_nodes(config_path)
    if not nodes:
        logger.error("No nodes configured")
        sys.exit(1)

    monitor = FleetMonitor(nodes)
    daemon = MonitorDaemon(
        monitor,
        interval=args.interval or int(os.environ.get("HIVE_MONITOR_INTERVAL", 60))
    )

    # Handle shutdown gracefully
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown(signum, frame):
        logger.info("Shutdown signal received")
        daemon.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        loop.run_until_complete(daemon.run())
    finally:
        loop.close()


def cmd_report(args):
    """Generate a report."""
    config_path = args.config or os.environ.get("HIVE_NODES_CONFIG")
    if not config_path:
        logger.error("No config file specified. Use --config or set HIVE_NODES_CONFIG")
        sys.exit(1)

    nodes = load_nodes(config_path)
    if not nodes:
        logger.error("No nodes configured")
        sys.exit(1)

    monitor = FleetMonitor(nodes)

    # Initial check to populate state
    logger.info("Checking all nodes...")
    monitor.check_all_nodes()

    # Generate report
    logger.info("Generating report...")
    report = monitor.generate_daily_report()

    # Output
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w') as f:
            json.dump(report, f, indent=2)
        logger.info(f"Report saved to {output_path}")
    else:
        print(json.dumps(report, indent=2))


def cmd_check(args):
    """Quick check of all nodes."""
    config_path = args.config or os.environ.get("HIVE_NODES_CONFIG")
    if not config_path:
        logger.error("No config file specified")
        sys.exit(1)

    nodes = load_nodes(config_path)
    monitor = FleetMonitor(nodes)
    results = monitor.check_all_nodes()
    print(json.dumps(results, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="Hive Fleet Monitor - Real-time monitoring and reports"
    )
    parser.add_argument("--config", "-c", help="Path to nodes.json config file")

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # monitor command
    monitor_parser = subparsers.add_parser("monitor", help="Run continuous monitoring daemon")
    monitor_parser.add_argument("--interval", "-i", type=int, default=60,
                                help="Check interval in seconds (default: 60)")

    # report command
    report_parser = subparsers.add_parser("report", help="Generate a daily report")
    report_parser.add_argument("--output", "-o", help="Output file path (default: stdout)")

    # check command
    check_parser = subparsers.add_parser("check", help="Quick status check")

    args = parser.parse_args()

    if args.command == "monitor":
        cmd_monitor(args)
    elif args.command == "report":
        cmd_report(args)
    elif args.command == "check":
        cmd_check(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
