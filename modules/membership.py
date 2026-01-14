"""
Membership module for cl-hive.

Implements tier management, promotion evaluation, and quorum logic.
"""

import math
import time
from enum import Enum
from typing import Any, Dict, List, Optional


ACTIVE_MEMBER_WINDOW_SECONDS = 24 * 3600
UPTIME_PASS_THRESHOLD = 99.5
BAN_QUORUM_THRESHOLD = 0.51  # 51% quorum for ban proposals


class MembershipTier(str, Enum):
    """Membership tiers."""
    NEOPHYTE = "neophyte"
    MEMBER = "member"
    ADMIN = "admin"


class MembershipManager:
    """Membership logic and promotion evaluation."""

    def __init__(self, db, state_manager, contribution_mgr, bridge, config, plugin=None):
        self.db = db
        self.state_manager = state_manager
        self.contribution_mgr = contribution_mgr
        self.bridge = bridge
        self.config = config
        self.plugin = plugin

    def _log(self, msg: str, level: str = "info") -> None:
        if self.plugin:
            self.plugin.log(f"[Membership] {msg}", level=level)

    def get_tier(self, peer_id: str) -> Optional[str]:
        member = self.db.get_member(peer_id)
        return member["tier"] if member else None

    def set_tier(self, peer_id: str, tier: str) -> bool:
        now = int(time.time())
        # Set promoted_at for member and admin tiers
        promoted_at = now if tier in (MembershipTier.MEMBER.value, MembershipTier.ADMIN.value) else None

        updated = self.db.update_member(peer_id, tier=tier, promoted_at=promoted_at)
        if not updated:
            return False

        # Members and admins get hive policy (0 PPM fees)
        is_full_member = tier in (MembershipTier.MEMBER.value, MembershipTier.ADMIN.value)
        if self.bridge and getattr(self.bridge, "status", None) and self.bridge.status.value == "enabled":
            try:
                self.bridge.set_hive_policy(peer_id, is_member=is_full_member)
            except Exception:
                self._log(f"Bridge policy update failed for {peer_id[:16]}...", level="warn")

        return True

    def is_probation_complete(self, peer_id: str) -> bool:
        member = self.db.get_member(peer_id)
        if not member:
            return False
        joined_at = member.get("joined_at")
        if not isinstance(joined_at, int):
            return False
        return int(time.time()) >= joined_at + (self.config.probation_days * 86400)

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
            return 1.0 if forwarded == 0 else float("inf")
        return forwarded / received

    def get_unique_peers(self, peer_id: str) -> List[str]:
        peer_state = self.state_manager.get_peer_state(peer_id)
        if not peer_state:
            return []

        peer_topology = set(peer_state.topology or [])
        if not peer_topology:
            return []

        member_peers = set()
        for member in self.db.get_all_members():
            if member.get("tier") != MembershipTier.MEMBER.value:
                continue
            state = self.state_manager.get_peer_state(member["peer_id"])
            if state and state.topology:
                member_peers.update(state.topology)

        unique = peer_topology - member_peers
        return list(unique)

    def evaluate_promotion(self, peer_id: str) -> Dict[str, Any]:
        reasons: List[str] = []

        member = self.db.get_member(peer_id)
        if not member:
            reasons.append("unknown_peer")
            return {"eligible": False, "reasons": reasons}

        if member.get("tier") != MembershipTier.NEOPHYTE.value:
            reasons.append("not_neophyte")
            return {"eligible": False, "reasons": reasons}

        if not self.is_probation_complete(peer_id):
            reasons.append("probation_incomplete")

        uptime = self.calculate_uptime(peer_id)
        if uptime < UPTIME_PASS_THRESHOLD:
            reasons.append("uptime_below_threshold")

        ratio = self.calculate_contribution_ratio(peer_id)
        if ratio < 1.0:
            reasons.append("contribution_ratio_below_threshold")

        unique_peers = self.get_unique_peers(peer_id)
        if not unique_peers:
            reasons.append("no_unique_peers")

        eligible = len(reasons) == 0
        return {
            "eligible": eligible,
            "reasons": reasons,
            "uptime_pct": uptime,
            "contribution_ratio": ratio,
            "unique_peers": unique_peers
        }

    def get_active_members(self) -> List[str]:
        now = int(time.time())
        active = []
        for member in self.db.get_all_members():
            if member.get("tier") != MembershipTier.MEMBER.value:
                continue
            last_seen = member.get("last_seen")
            if not isinstance(last_seen, int):
                continue
            if now - last_seen > ACTIVE_MEMBER_WINDOW_SECONDS:
                continue
            if self.db.is_banned(member["peer_id"]):
                continue
            active.append(member["peer_id"])
        return active

    def calculate_quorum(self, active_members: int) -> int:
        threshold = math.ceil(active_members * self.config.vouch_threshold_pct)
        return max(self.config.min_vouch_count, threshold)

    def build_vouch_message(self, target_pubkey: str, request_id: str, timestamp: int) -> str:
        return f"hive:vouch:{target_pubkey}:{request_id}:{timestamp}"
