"""
Plugin option registration and config reload helpers for cl-hive.

Extracted from cl-hive.py monolith. Contains:
- _parse_bool(): Boolean option parser
- RateLimiter: Token bucket rate limiter for gossip flooding prevention
- register_options(): All plugin.add_option() calls
- OPTION_TO_CONFIG_MAP: Option name -> config attribute mapping
- _parse_setconfig_value(): Typed value parser for setconfig
"""

import threading
import time
from typing import Dict, Optional, Any


class RateLimiter:
    """
    Token bucket rate limiter for gossip message flooding prevention.

    Tracks message rates per sender and rejects messages that exceed
    the configured rate. Uses a sliding window approach.

    Memory bounded: evicts inactive peers when MAX_TRACKED_PEERS exceeded.
    """

    # Maximum peers to track (DoS protection)
    MAX_TRACKED_PEERS = 1000

    def __init__(self, max_per_minute: int = 10, window_seconds: int = 60):
        """
        Initialize the rate limiter.

        Args:
            max_per_minute: Maximum messages allowed per window
            window_seconds: Size of the sliding window in seconds
        """
        self._max_messages = max_per_minute
        self._window = window_seconds
        self._timestamps: Dict[str, list] = {}  # peer_id -> list of timestamps
        self._lock = threading.Lock()

    def is_allowed(self, peer_id: str) -> bool:
        """
        Check if a message from this peer is allowed.

        Args:
            peer_id: The sender's pubkey

        Returns:
            True if allowed, False if rate limited
        """
        now = time.time()
        cutoff = now - self._window

        with self._lock:
            # Get or create timestamp list for this peer
            if peer_id not in self._timestamps:
                # Enforce max tracked peers (DoS protection)
                if len(self._timestamps) >= self.MAX_TRACKED_PEERS:
                    # Evict peers with no recent activity
                    inactive = [
                        pid for pid, ts_list in self._timestamps.items()
                        if not ts_list or max(ts_list) <= cutoff
                    ]
                    for pid in inactive[:100]:  # Evict up to 100 at a time
                        del self._timestamps[pid]
                    # If still at limit after eviction, reject new peer
                    if len(self._timestamps) >= self.MAX_TRACKED_PEERS:
                        return False
                self._timestamps[peer_id] = []

            # Remove old timestamps outside the window
            self._timestamps[peer_id] = [
                ts for ts in self._timestamps[peer_id] if ts > cutoff
            ]

            # Check if under limit
            if len(self._timestamps[peer_id]) >= self._max_messages:
                return False

            # Record this message
            self._timestamps[peer_id].append(now)
            return True

    def get_stats(self, peer_id: str = None) -> Dict[str, Any]:
        """Get rate limiter statistics."""
        now = time.time()
        cutoff = now - self._window

        with self._lock:
            if peer_id:
                timestamps = self._timestamps.get(peer_id, [])
                recent = [ts for ts in timestamps if ts > cutoff]
                return {
                    "peer_id": peer_id,
                    "messages_in_window": len(recent),
                    "max_per_window": self._max_messages,
                    "window_seconds": self._window,
                }

            # Overall stats
            total_peers = len(self._timestamps)
            total_messages = sum(
                len([ts for ts in timestamps if ts > cutoff])
                for timestamps in self._timestamps.values()
            )
            return {
                "tracked_peers": total_peers,
                "total_messages_in_window": total_messages,
                "max_per_peer": self._max_messages,
                "window_seconds": self._window,
            }

    def cleanup(self) -> int:
        """Remove stale entries. Returns number of peers cleaned."""
        now = time.time()
        cutoff = now - self._window
        cleaned = 0

        with self._lock:
            stale_peers = [
                peer_id for peer_id, timestamps in self._timestamps.items()
                if not any(ts > cutoff for ts in timestamps)
            ]
            for peer_id in stale_peers:
                del self._timestamps[peer_id]
                cleaned += 1

        return cleaned


def _parse_bool(value: Any, default: bool = False) -> bool:
    """Parse a boolean-ish option value safely."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def register_options(plugin):
    """Register all cl-hive plugin options on the given Plugin instance."""
    # Database path is NOT dynamic (immutable after init)
    plugin.add_option(
        name='hive-db-path',
        default='~/.lightning/cl_hive.db',
        description='Path to the SQLite database for Hive state (immutable)'
    )

    # All other options are dynamic (hot-reloadable via `lightning-cli setconfig`)

    plugin.add_option(
        name='hive-max-members',
        default='50',
        description='Maximum Hive members (Dunbar cap for gossip efficiency)',
        dynamic=True
    )

    plugin.add_option(
        name='hive-market-share-cap',
        default='0.20',
        description='Maximum market share per target (0.20 = 20%, anti-monopoly)',
        dynamic=True
    )

    plugin.add_option(
        name='hive-auto-join',
        default='false',
        description='Auto-discover hive peers on connect (disabled to avoid CLN crash bug)',
        dynamic=True
    )

    plugin.add_option(
        name='hive-intent-hold-seconds',
        default='60',
        description='Hold period before committing an Intent (conflict resolution)',
        dynamic=True
    )

    plugin.add_option(
        name='hive-gossip-threshold',
        default='0.10',
        description='Capacity change threshold to trigger gossip (0.10 = 10%)',
        dynamic=True
    )

    plugin.add_option(
        name='hive-heartbeat-interval',
        default='300',
        description='Heartbeat broadcast interval in seconds (default: 5 min)',
        dynamic=True
    )

    plugin.add_option(
        name='hive-planner-interval',
        default='3600',
        description='Planner cycle interval in seconds (default: 1 hour, minimum: 300)',
        dynamic=True
    )


# =============================================================================
# CONFIG RELOAD SUPPORT
# =============================================================================
# Note: CLN's setconfig command updates option values, but there's no
# notification mechanism for plugins. Use `hive-reload-config` RPC to
# sync the internal config object after using `lightning-cli setconfig`.

# Mapping from plugin option names to config attribute names and types
OPTION_TO_CONFIG_MAP: Dict[str, tuple] = {
    'hive-max-members': ('max_members', int),
    'hive-market-share-cap': ('market_share_cap_pct', float),
    'hive-auto-join': ('auto_join_enabled', bool),
    'hive-intent-hold-seconds': ('intent_hold_seconds', int),
    'hive-gossip-threshold': ('gossip_threshold_pct', float),
    'hive-heartbeat-interval': ('heartbeat_interval', int),
    'hive-planner-interval': ('planner_interval', int),
}


def _parse_setconfig_value(value: Any, target_type: type) -> Any:
    """Parse a setconfig value to the target type."""
    if target_type == bool:
        if isinstance(value, bool):
            return value
        return str(value).lower() in ('true', '1', 'yes', 'on')
    elif target_type == int:
        return int(value)
    elif target_type == float:
        return float(value)
    else:
        return str(value)
