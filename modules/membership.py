"""
Membership module for cl-hive.

Implements single-role membership management, uptime tracking, and bridge policy sync.
"""

import time
from typing import Any, Dict, List, Optional

from . import network_metrics


ACTIVE_MEMBER_WINDOW_SECONDS = 24 * 3600

# Single membership tier — all members have equal privileges.
MEMBER_TIER = "member"


class MembershipManager:
    """Membership logic: add/remove members, uptime, bridge policy sync."""

    def __init__(self, db, state_manager, contribution_mgr, bridge, config, plugin=None,
                 metrics_calculator=None):
        self.db = db
        self.state_manager = state_manager
        self.contribution_mgr = contribution_mgr
        self.bridge = bridge
        self.config = config
        self.plugin = plugin
        self.metrics_calculator = metrics_calculator

    def _log(self, msg: str, level: str = "info") -> None:
        if self.plugin:
            self.plugin.log(f"[Membership] {msg}", level=level)

    # =========================================================================
    # CORE MEMBERSHIP MANAGEMENT
    # =========================================================================

    def get_tier(self, peer_id: str) -> Optional[str]:
        member = self.db.get_member(peer_id)
        return member["tier"] if member else None

    def set_tier(self, peer_id: str) -> bool:
        """Update a peer's membership record (always member tier)."""
        updated = self.db.update_member(peer_id, tier=MEMBER_TIER)
        return bool(updated)

    def add_member(self, peer_id: str, tier: str = MEMBER_TIER) -> bool:
        """Add a new member to the hive."""
        return self.db.add_member(peer_id, tier=MEMBER_TIER, joined_at=int(time.time()))

    def remove_member(self, peer_id: str) -> bool:
        """Remove a member from the hive."""
        return self.db.remove_member(peer_id)

    def get_members(self) -> List[Dict[str, Any]]:
        """Get all hive members."""
        return self.db.get_all_members()

    def is_member(self, peer_id: str) -> bool:
        """Check if a peer is a hive member."""
        return self.db.get_member(peer_id) is not None

    # =========================================================================
    # UPTIME TRACKING
    # =========================================================================

    def calculate_uptime(self, peer_id: str) -> float:
        presence = self.db.get_presence(peer_id)
        if not presence:
            return 0.0

        now = int(time.time())
        online_seconds = presence["online_seconds_rolling"]
        last_change = presence["last_change_ts"]
        window_start = presence["window_start_ts"]
        is_online = bool(presence["is_online"])
        window_seconds = max(1, now - window_start)

        if is_online:
            online_seconds += max(0, now - last_change)

        uptime_pct = min(100.0, (online_seconds / window_seconds) * 100.0)
        return uptime_pct

    def calculate_contribution_ratio(self, peer_id: str) -> float:
        stats = self.contribution_mgr.get_contribution_stats(peer_id, window_days=30)
        forwarded = stats["forwarded"]
        received = stats["received"]
        if received == 0:
            return 1.0 if forwarded == 0 else 10.0
        return forwarded / received

    # =========================================================================
    # NETWORK METRICS
    # =========================================================================

    def get_unique_peers(self, peer_id: str) -> List[str]:
        """
        Get external peers that only this member connects to.

        Uses shared NetworkMetricsCalculator if available for cached,
        consistent results across all modules.
        """
        # Try shared calculator first (preferred)
        calculator = self.metrics_calculator or network_metrics.get_calculator()
        if calculator:
            return calculator.get_unique_peers(peer_id)

        # Fallback: local calculation
        return self._calculate_unique_peers_local(peer_id)

    def _calculate_unique_peers_local(self, peer_id: str) -> List[str]:
        """Local fallback for unique peers calculation."""
        peer_state = self.state_manager.get_peer_state(peer_id)
        if not peer_state:
            return []

        peer_topology = set(peer_state.topology or [])
        if not peer_topology:
            return []

        member_peers = set()
        for member in self.db.get_all_members():
            state = self.state_manager.get_peer_state(member["peer_id"])
            if state and state.topology:
                member_peers.update(state.topology)

        unique = peer_topology - member_peers
        return list(unique)

    # =========================================================================
    # ACTIVE MEMBERS
    # =========================================================================

    def get_active_members(self) -> List[str]:
        now = int(time.time())
        active = []
        for member in self.db.get_all_members():
            last_seen = member.get("last_seen")
            if not isinstance(last_seen, int):
                continue
            if now - last_seen > ACTIVE_MEMBER_WINDOW_SECONDS:
                continue
            if self.db.is_banned(member["peer_id"]):
                continue
            active.append(member["peer_id"])
        return active

    def get_all_members(self) -> List[Dict[str, Any]]:
        """Alias for get_members() used by background loops."""
        return self.get_members()

    # =========================================================================
    # TIMESTAMP VALIDATION (used by protocol handlers)
    # =========================================================================

    @staticmethod
    def _check_timestamp_freshness(payload: dict, max_age: int,
                                    label: str = "message",
                                    plugin=None,
                                    max_clock_skew: int = 120) -> bool:
        """
        Check if a message timestamp is fresh enough to process.

        Args:
            payload: Message payload containing 'timestamp' field
            max_age: Maximum allowed age in seconds
            label: Message type label for logging
            plugin: Optional plugin instance for logging
            max_clock_skew: Maximum allowed clock skew in seconds

        Returns:
            True if timestamp is acceptable, False if stale/invalid
        """
        ts = payload.get("timestamp")
        if not isinstance(ts, (int, float)) or ts <= 0:
            return False
        now = int(time.time())
        age = now - int(ts)
        if age > max_age:
            if plugin:
                plugin.log(
                    f"[Membership] {label} rejected: timestamp too old ({age}s > {max_age}s)",
                    level='debug'
                )
            return False
        if age < -max_clock_skew:
            if plugin:
                plugin.log(
                    f"[Membership] {label} rejected: timestamp {-age}s in the future",
                    level='debug'
                )
            return False
        return True
