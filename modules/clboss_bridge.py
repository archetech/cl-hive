"""
CLBoss Bridge Module for cl-hive.

Provides a small gateway wrapper for CLBoss integration:
- Detect availability from plugin list.
- Attempt to ignore/unignore peers (but command may not exist).

IMPORTANT: CLBoss v0.15.1 does NOT have clboss-ignore or clboss-unignore.
These would be for peer-level channel open coordination but don't exist.
CLBoss only has:
- clboss-ignore-onchain: Ignore addresses for on-chain sweeps (different purpose)
- clboss-unmanage: Stop managing fees for a peer (used by cl-revenue-ops)
- clboss-manage: Resume managing fees for a peer

The Hive uses the Intent Lock Protocol for channel open coordination instead.
This protocol uses gossip messages to announce intent and deterministic tie-breakers
(lowest pubkey wins) to prevent thundering herd problems.

Explicitly avoids clboss-manage/unmanage; fee control belongs to cl-revenue-ops.
"""

from typing import Any, Dict

from pyln.client import RpcError


class CLBossBridge:
    """Gateway wrapper around CLBoss RPC calls.

    NOTE: CLBoss v0.15.1 does NOT have clboss-ignore or clboss-unignore commands.
    These are for peer-level channel open coordination. CLBoss only has:
    - clboss-ignore-onchain: Ignore addresses for on-chain sweeps (different purpose)
    - clboss-unmanage: Stop managing fees for a peer (used by cl-revenue-ops)

    The Hive uses the Intent Lock Protocol instead for channel coordination.
    """

    def __init__(self, rpc, plugin=None):
        self.rpc = rpc
        self.plugin = plugin
        self._available = False
        self._supports_ignore = True  # Assume true until we get "Unknown command"
        self._supports_unignore = True

    def _log(self, msg: str, level: str = "info") -> None:
        if self.plugin:
            self.plugin.log(f"[CLBossBridge] {msg}", level=level)

    def detect_clboss(self) -> bool:
        """Detect whether CLBoss is registered and active."""
        try:
            plugins = self.rpc.plugin("list")
            for entry in plugins.get("plugins", []):
                if "clboss" in entry.get("name", "").lower():
                    self._available = entry.get("active", False)
                    return self._available
            self._available = False
            return False
        except Exception as exc:
            self._available = False
            self._log(f"CLBoss detection failed: {exc}", level="warn")
            return False

    def ignore_peer(self, peer_id: str) -> bool:
        """Tell CLBoss to ignore a peer for channel management.

        NOTE: This command does not exist in CLBoss v0.15.1.
        The Hive uses Intent Lock Protocol for coordination instead.
        """
        if not self._available:
            self._log(f"CLBoss not available, cannot ignore {peer_id[:16]}...")
            return False
        if not self._supports_ignore:
            # Already know this command doesn't exist
            return False
        try:
            self.rpc.call("clboss-ignore", {"nodeid": peer_id})
            self._log(f"CLBoss ignoring {peer_id[:16]}...")
            return True
        except RpcError as exc:
            msg = str(exc).lower()
            if "unknown command" in msg or "method not found" in msg:
                self._supports_ignore = False
                self._log("CLBoss does not support clboss-ignore (using Intent Lock Protocol)", level="info")
            else:
                self._log(f"CLBoss ignore failed: {exc}", level="warn")
            return False

    def unignore_peer(self, peer_id: str) -> bool:
        """Tell CLBoss to stop ignoring a peer, if supported.

        NOTE: This command does not exist in CLBoss v0.15.1.
        """
        if not self._available or not self._supports_unignore:
            return False
        try:
            self.rpc.call("clboss-unignore", {"nodeid": peer_id})
            self._log(f"CLBoss unignoring {peer_id[:16]}...")
            return True
        except RpcError as exc:
            msg = str(exc).lower()
            if "unknown command" in msg or "method not found" in msg:
                self._supports_unignore = False
                self._log("CLBoss does not support clboss-unignore", level="info")
            else:
                self._log(f"CLBoss unignore failed: {exc}", level="warn")
            return False

    def supports_peer_ignore(self) -> bool:
        """Check if CLBoss supports peer-level ignore commands.

        Returns False for CLBoss v0.15.1 which lacks clboss-ignore.
        The Hive falls back to Intent Lock Protocol for coordination.
        """
        return self._available and self._supports_ignore

    def get_status(self) -> Dict[str, Any]:
        """Get CLBoss bridge status for diagnostics."""
        return {
            "clboss_available": self._available,
            "supports_ignore": self._supports_ignore,
            "supports_unignore": self._supports_unignore,
            "coordination_method": "clboss-ignore" if self._supports_ignore else "intent_lock_protocol"
        }
