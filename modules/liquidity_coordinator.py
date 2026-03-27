"""
Liquidity Coordinator Module

Coordinates INFORMATION SHARING about liquidity state between hive members.
Each node manages its own funds independently - no sats transfer between nodes.

Information shared:
- Which channels are depleted/saturated (liquidity needs)
- Which peers need more capacity
- Rebalancing activity (to avoid route conflicts)

How this helps without fund transfer:
- Fee coordination: Adjust fees to direct public flow toward peers that help struggling members
- Conflict avoidance: Don't compete for same rebalance routes
- Topology planning: Open channels that benefit the fleet

Security: All operations use cryptographic signatures.
"""

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict

from .protocol import (
    create_liquidity_need,
    create_liquidity_snapshot,
    validate_liquidity_need_payload,
    validate_liquidity_snapshot_payload,
    get_liquidity_need_signing_payload,
    get_liquidity_snapshot_signing_payload,
    LIQUIDITY_NEED_RATE_LIMIT,
    LIQUIDITY_SNAPSHOT_RATE_LIMIT,
    MAX_NEEDS_IN_SNAPSHOT,
)


# Urgency levels for liquidity needs
URGENCY_CRITICAL = "critical"
URGENCY_HIGH = "high"
URGENCY_MEDIUM = "medium"
URGENCY_LOW = "low"

# Need types
NEED_INBOUND = "inbound"
NEED_OUTBOUND = "outbound"
NEED_REBALANCE = "rebalance"

# Reasons for liquidity need
REASON_CHANNEL_DEPLETED = "channel_depleted"
REASON_OPPORTUNITY = "opportunity"
REASON_NNLB_ASSIST = "nnlb_assist"

# Limits
MAX_PENDING_NEEDS = 100  # Max liquidity needs to track


@dataclass
class LiquidityNeed:
    """Tracked liquidity need from a hive member."""
    reporter_id: str
    need_type: str
    target_peer_id: str
    amount_sats: int
    urgency: str
    max_fee_ppm: int
    reason: str
    current_balance_pct: float
    can_provide_inbound: int
    can_provide_outbound: int
    timestamp: int
    signature: str


class LiquidityCoordinator:
    """
    Coordinates liquidity INFORMATION between hive members.

    Shares information about liquidity state (depleted/saturated channels)
    so nodes can make better independent decisions about fees and rebalancing.

    No fund transfers between nodes - each node manages its own funds.
    """

    def __init__(
        self,
        database: Any,
        plugin: Any,
        our_pubkey: str,
        fee_intel_mgr: Any = None,
        state_manager: Any = None
    ):
        """
        Initialize the liquidity coordinator.

        Args:
            database: HiveDatabase instance
            plugin: Plugin instance for RPC/logging
            our_pubkey: Our node's pubkey
            fee_intel_mgr: FeeIntelligenceManager for health data
            state_manager: StateManager for member topology (Phase 1)
        """
        self.database = database
        self.plugin = plugin
        self.our_pubkey = our_pubkey
        self.fee_intel_mgr = fee_intel_mgr
        self.state_manager = state_manager

        # Lock protecting in-memory state
        self._lock = threading.Lock()

        # In-memory tracking
        self._liquidity_needs: Dict[str, LiquidityNeed] = {}  # reporter_id -> need

        # Liquidity state tracking (information only)
        # Stores latest liquidity reports from cl-revenue-ops instances
        self._member_liquidity_state: Dict[str, Dict[str, Any]] = {}

        # Rate limiting
        self._rate_lock = threading.Lock()
        self._need_rate: Dict[str, List[float]] = defaultdict(list)
        self._snapshot_rate: Dict[str, List[float]] = defaultdict(list)


    def _check_rate_limit(
        self,
        sender: str,
        rate_tracker: Dict[str, List[float]],
        limit: Tuple[int, int]
    ) -> bool:
        """Check if sender is within rate limit."""
        max_count, period = limit
        now = time.time()

        with self._rate_lock:
            # Clean old entries for this sender
            rate_tracker[sender] = [
                ts for ts in rate_tracker[sender]
                if now - ts < period
            ]

            # Evict empty/stale keys to prevent unbounded dict growth
            if len(rate_tracker) > 200:
                stale = [k for k, v in rate_tracker.items() if not v]
                for k in stale:
                    del rate_tracker[k]

            return len(rate_tracker[sender]) < max_count

    def _record_message(
        self,
        sender: str,
        rate_tracker: Dict[str, List[float]]
    ):
        """Record a message for rate limiting."""
        with self._rate_lock:
            rate_tracker[sender].append(time.time())

    def create_liquidity_need_message(
        self,
        need_type: str,
        target_peer_id: str,
        amount_sats: int,
        urgency: str,
        max_fee_ppm: int,
        reason: str,
        current_balance_pct: float,
        can_provide_inbound: int,
        can_provide_outbound: int,
        rpc: Any
    ) -> Optional[bytes]:
        """
        Create a signed LIQUIDITY_NEED message.

        Args:
            need_type: Type of need (inbound/outbound/rebalance)
            target_peer_id: External peer involved
            amount_sats: Amount needed
            urgency: Urgency level
            max_fee_ppm: Maximum fee willing to pay
            reason: Why we need this
            current_balance_pct: Current local balance percentage
            can_provide_inbound: Sats of inbound we can provide
            can_provide_outbound: Sats of outbound we can provide
            rpc: RPC interface for signing

        Returns:
            Serialized and signed message bytes, or None on error
        """
        try:
            timestamp = int(time.time())

            # Build payload for signing
            payload = {
                "reporter_id": self.our_pubkey,
                "timestamp": timestamp,
                "need_type": need_type,
                "target_peer_id": target_peer_id,
                "amount_sats": amount_sats,
                "urgency": urgency,
                "max_fee_ppm": max_fee_ppm,
            }

            # Sign the payload
            signing_msg = get_liquidity_need_signing_payload(payload)
            sig_result = rpc.signmessage(signing_msg)
            signature = sig_result['zbase']

            return create_liquidity_need(
                reporter_id=self.our_pubkey,
                timestamp=timestamp,
                signature=signature,
                need_type=need_type,
                target_peer_id=target_peer_id,
                amount_sats=amount_sats,
                urgency=urgency,
                max_fee_ppm=max_fee_ppm,
                reason=reason,
                current_balance_pct=current_balance_pct,
                can_provide_inbound=can_provide_inbound,
                can_provide_outbound=can_provide_outbound,
            )
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"cl-hive: Failed to create liquidity need message: {e}",
                    level='warn'
                )
            return None

    def handle_liquidity_need(
        self,
        peer_id: str,
        payload: Dict[str, Any],
        rpc: Any
    ) -> Dict[str, Any]:
        """
        Handle incoming LIQUIDITY_NEED message.

        Args:
            peer_id: Sender peer ID
            payload: Message payload
            rpc: RPC interface for signature verification

        Returns:
            Result dict with success/error
        """
        # Validate payload structure
        if not validate_liquidity_need_payload(payload):
            return {"error": "invalid payload"}

        reporter_id = payload.get("reporter_id")

        # Identity binding: sender must match reporter (prevent relay attacks)
        if peer_id != reporter_id:
            return {"error": "identity binding failed"}

        # Verify sender is a hive member
        member = self.database.get_member(reporter_id)
        if not member:
            return {"error": "reporter not a member"}

        # Rate limit check
        if not self._check_rate_limit(
            reporter_id,
            self._need_rate,
            LIQUIDITY_NEED_RATE_LIMIT
        ):
            return {"error": "rate limited"}

        # Verify signature
        signature = payload.get("signature")
        if not signature:
            return {"error": "missing signature"}

        signing_message = get_liquidity_need_signing_payload(payload)

        try:
            verify_result = rpc.checkmessage(signing_message, signature)
            if not verify_result.get("verified"):
                return {"error": "signature verification failed"}

            if verify_result.get("pubkey") != reporter_id:
                return {"error": "signature pubkey mismatch"}
        except Exception as e:
            return {"error": f"signature check failed: {e}"}

        # Record rate limit
        self._record_message(reporter_id, self._need_rate)

        # Store the liquidity need
        need = LiquidityNeed(
            reporter_id=reporter_id,
            need_type=payload.get("need_type", NEED_REBALANCE),
            target_peer_id=payload.get("target_peer_id", ""),
            amount_sats=payload.get("amount_sats", 0),
            urgency=payload.get("urgency", URGENCY_LOW),
            max_fee_ppm=payload.get("max_fee_ppm", 0),
            reason=payload.get("reason", ""),
            current_balance_pct=payload.get("current_balance_pct", 0.5),
            can_provide_inbound=payload.get("can_provide_inbound", 0),
            can_provide_outbound=payload.get("can_provide_outbound", 0),
            timestamp=payload.get("timestamp", int(time.time())),
            signature=signature
        )

        # Store in memory using composite key (consistent with batch path)
        key = f"{reporter_id}:{need.target_peer_id}"
        with self._lock:
            self._liquidity_needs[key] = need

        # Prune old needs if over limit
        self._prune_old_needs()

        # Store in database
        self.database.store_liquidity_need(
            reporter_id=need.reporter_id,
            need_type=need.need_type,
            target_peer_id=need.target_peer_id,
            amount_sats=need.amount_sats,
            urgency=need.urgency,
            max_fee_ppm=need.max_fee_ppm,
            reason=need.reason,
            current_balance_pct=need.current_balance_pct,
            timestamp=need.timestamp
        )

        if self.plugin:
            self.plugin.log(
                f"cl-hive: Received liquidity need from {reporter_id[:16]}...: "
                f"{need.need_type} {need.amount_sats} sats ({need.urgency})",
                level='debug'
            )

        return {"success": True, "stored": True}

    def handle_liquidity_snapshot(
        self,
        peer_id: str,
        payload: Dict[str, Any],
        rpc: Any
    ) -> Dict[str, Any]:
        """
        Handle incoming LIQUIDITY_SNAPSHOT message.

        This is the preferred method for receiving liquidity needs - one message
        contains multiple needs instead of N individual messages.

        Args:
            peer_id: Sender peer ID
            payload: Message payload
            rpc: RPC interface for signature verification

        Returns:
            Result dict with success/error
        """
        # Validate payload structure
        if not validate_liquidity_snapshot_payload(payload):
            return {"error": "invalid payload"}

        reporter_id = payload.get("reporter_id")

        # Identity binding: sender must match reporter (prevent relay attacks)
        if peer_id != reporter_id:
            return {"error": "identity binding failed"}

        # Verify sender is a hive member
        member = self.database.get_member(reporter_id)
        if not member:
            return {"error": "reporter not a member"}

        # Rate limit check for snapshot messages
        if not self._check_rate_limit(
            reporter_id,
            self._snapshot_rate,
            LIQUIDITY_SNAPSHOT_RATE_LIMIT
        ):
            return {"error": "rate limited"}

        # Verify signature
        signature = payload.get("signature")
        if not signature:
            return {"error": "missing signature"}

        signing_message = get_liquidity_snapshot_signing_payload(payload)

        try:
            verify_result = rpc.checkmessage(signing_message, signature)
            if not verify_result.get("verified"):
                return {"error": "signature verification failed"}

            if verify_result.get("pubkey") != reporter_id:
                return {"error": "signature pubkey mismatch"}
        except Exception as e:
            return {"error": f"signature check failed: {e}"}

        # Record rate limit
        self._record_message(reporter_id, self._snapshot_rate)

        # Process each need in the snapshot
        needs = payload.get("needs", [])
        batch_timestamp = payload.get("timestamp", int(time.time()))

        # Build all LiquidityNeed objects first
        parsed_needs = []
        for need_data in needs:
            need = LiquidityNeed(
                reporter_id=reporter_id,
                need_type=need_data.get("need_type", NEED_REBALANCE),
                target_peer_id=need_data.get("target_peer_id", ""),
                amount_sats=need_data.get("amount_sats", 0),
                urgency=need_data.get("urgency", URGENCY_LOW),
                max_fee_ppm=need_data.get("max_fee_ppm", 0),
                reason=need_data.get("reason", ""),
                current_balance_pct=need_data.get("current_balance_pct", 0.5),
                can_provide_inbound=need_data.get("can_provide_inbound", 0),
                can_provide_outbound=need_data.get("can_provide_outbound", 0),
                timestamp=batch_timestamp,
                signature=signature
            )
            parsed_needs.append((f"{reporter_id}:{need.target_peer_id}", need))

        # Batch-insert under a single lock acquisition
        with self._lock:
            for key, need in parsed_needs:
                self._liquidity_needs[key] = need

        # Store in database outside lock (DB has its own locking)
        for _key, need in parsed_needs:
            self.database.store_liquidity_need(
                reporter_id=need.reporter_id,
                need_type=need.need_type,
                target_peer_id=need.target_peer_id,
                amount_sats=need.amount_sats,
                urgency=need.urgency,
                max_fee_ppm=need.max_fee_ppm,
                reason=need.reason,
                current_balance_pct=need.current_balance_pct,
                timestamp=need.timestamp
            )

        stored_count = len(parsed_needs)

        # Prune old needs if over limit
        self._prune_old_needs()

        if self.plugin:
            self.plugin.log(
                f"cl-hive: Received liquidity snapshot from {reporter_id[:16]}... "
                f"with {stored_count} needs",
                level='debug'
            )

        return {"success": True, "needs_stored": stored_count}

    def create_liquidity_snapshot_message(
        self,
        needs: List[Dict[str, Any]],
        rpc: Any
    ) -> Optional[bytes]:
        """
        Create a signed LIQUIDITY_SNAPSHOT message.

        This is the preferred method for sharing liquidity needs. Instead of
        sending N individual messages for N needs, send one snapshot with all
        liquidity needs.

        Args:
            needs: List of liquidity needs, each containing:
                - target_peer_id: External peer or hive member
                - need_type: 'inbound', 'outbound', 'rebalance'
                - amount_sats: How much is needed
                - urgency: 'critical', 'high', 'medium', 'low'
                - max_fee_ppm: Maximum fee willing to pay
                - reason: Why this liquidity is needed
                - current_balance_pct: Current local balance percentage
                - can_provide_inbound: Sats of inbound that can be provided
                - can_provide_outbound: Sats of outbound that can be provided
            rpc: RPC interface for signing

        Returns:
            Serialized message bytes, or None on error
        """
        # Enforce bounds
        if len(needs) > MAX_NEEDS_IN_SNAPSHOT:
            if self.plugin:
                self.plugin.log(
                    f"cl-hive: Liquidity snapshot too large ({len(needs)} > {MAX_NEEDS_IN_SNAPSHOT})",
                    level='warn'
                )
            return None

        timestamp = int(time.time())

        # Build payload for signing
        payload = {
            "reporter_id": self.our_pubkey,
            "timestamp": timestamp,
            "needs": needs,
        }

        # Sign the payload
        signing_message = get_liquidity_snapshot_signing_payload(payload)

        try:
            sig_result = rpc.signmessage(signing_message)
            signature = sig_result["zbase"]
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"cl-hive: Failed to sign liquidity snapshot: {e}",
                    level='warn'
                )
            return None

        return create_liquidity_snapshot(
            reporter_id=self.our_pubkey,
            timestamp=timestamp,
            signature=signature,
            needs=needs
        )

    def _prune_old_needs(self):
        """Remove old liquidity needs to stay under limit."""
        with self._lock:
            if len(self._liquidity_needs) <= MAX_PENDING_NEEDS:
                return

            # Sort by timestamp, remove oldest
            sorted_needs = sorted(
                self._liquidity_needs.items(),
                key=lambda x: x[1].timestamp
            )

            to_remove = len(sorted_needs) - MAX_PENDING_NEEDS
            for key, _ in sorted_needs[:to_remove]:
                del self._liquidity_needs[key]

    def get_prioritized_needs(self) -> List[LiquidityNeed]:
        """
        Get liquidity needs sorted by NNLB priority.

        Struggling nodes get higher priority.

        Returns:
            List of needs sorted by priority (highest first)
        """
        with self._lock:
            needs = list(self._liquidity_needs.values())

        def nnlb_priority(need: LiquidityNeed) -> float:
            """Calculate NNLB priority score."""
            # Get member health
            member_health = self.database.get_member_health(need.reporter_id)
            if member_health:
                health_score = member_health.get("overall_health", 50)
            else:
                health_score = 50

            # Clamp health_score to valid range before priority calc
            health_score = max(0, min(100, health_score))
            # Lower health = higher priority (inverted)
            health_priority = 1.0 - (health_score / 100.0)

            # Urgency multiplier
            urgency_mult = {
                URGENCY_CRITICAL: 2.0,
                URGENCY_HIGH: 1.5,
                URGENCY_MEDIUM: 1.0,
                URGENCY_LOW: 0.5
            }.get(need.urgency, 1.0)

            return health_priority * urgency_mult

        return sorted(needs, key=nnlb_priority, reverse=True)

    def assess_our_liquidity_needs(
        self,
        funds: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Assess what liquidity we currently need.

        If cl-revenue-ops has provided enriched needs (flow-aware, with
        turnover and flow_state context), prefer those over raw threshold
        scanning.

        Args:
            funds: Result of listfunds() call

        Returns:
            List of liquidity needs
        """
        # Prefer enriched needs from cl-revenue-ops if available
        with self._lock:
            our_state = self._member_liquidity_state.get(self.our_pubkey, {})
            enriched = our_state.get("enriched_needs")
        if enriched is not None:
            return enriched

        channels = funds.get("channels", [])
        needs = []

        # Get hive members
        members = self.database.get_all_members()
        member_ids = {m.get("peer_id") for m in members}

        for ch in channels:
            if ch.get("state") != "CHANNELD_NORMAL":
                continue

            peer_id = ch.get("peer_id")
            if peer_id in member_ids:
                continue  # Skip hive channels, focus on external

            capacity = ch.get("amount_msat", 0) // 1000
            local = ch.get("our_amount_msat", 0) // 1000
            local_pct = local / capacity if capacity > 0 else 0.5

            # Determine if we need liquidity
            if local_pct < 0.2:
                # Depleted outbound - need outbound to this peer
                amount_needed = int(capacity * 0.5 - local)
                needs.append({
                    "need_type": NEED_OUTBOUND,
                    "target_peer_id": peer_id,
                    "amount_sats": amount_needed,
                    "urgency": URGENCY_HIGH if local_pct < 0.1 else URGENCY_MEDIUM,
                    "reason": REASON_CHANNEL_DEPLETED,
                    "current_balance_pct": local_pct
                })
            elif local_pct > 0.8:
                # Depleted inbound - need inbound from this peer
                amount_needed = int(local - capacity * 0.5)
                needs.append({
                    "need_type": NEED_INBOUND,
                    "target_peer_id": peer_id,
                    "amount_sats": amount_needed,
                    "urgency": URGENCY_HIGH if local_pct > 0.9 else URGENCY_MEDIUM,
                    "reason": REASON_CHANNEL_DEPLETED,
                    "current_balance_pct": local_pct
                })

        return needs

    def get_nnlb_assistance_status(self) -> Dict[str, Any]:
        """
        Get status of NNLB liquidity assistance.

        Returns:
            Dict with assistance statistics
        """
        needs = self.get_prioritized_needs()

        # Count by urgency
        urgency_counts = defaultdict(int)
        for need in needs:
            urgency_counts[need.urgency] += 1

        # Get struggling members (for informational purposes)
        struggling = self.database.get_struggling_members(threshold=40)

        return {
            "pending_needs": len(needs),
            "critical_needs": urgency_counts.get(URGENCY_CRITICAL, 0),
            "high_needs": urgency_counts.get(URGENCY_HIGH, 0),
            "medium_needs": urgency_counts.get(URGENCY_MEDIUM, 0),
            "low_needs": urgency_counts.get(URGENCY_LOW, 0),
            "struggling_members": len(struggling)
        }

    def get_status(self) -> Dict[str, Any]:
        """
        Get overall liquidity coordination status.

        Returns:
            Dict with coordination status and statistics
        """
        nnlb_status = self.get_nnlb_assistance_status()

        # Count need types under lock to prevent RuntimeError during iteration
        with self._lock:
            inbound_needs = sum(
                1 for n in self._liquidity_needs.values()
                if n.need_type == NEED_INBOUND
            )
            outbound_needs = sum(
                1 for n in self._liquidity_needs.values()
                if n.need_type == NEED_OUTBOUND
            )
            pending_count = len(self._liquidity_needs)

        return {
            "status": "active",
            "pending_needs": pending_count,
            "inbound_needs": inbound_needs,
            "outbound_needs": outbound_needs,
            "nnlb_status": nnlb_status
        }

    # =========================================================================
    # Liquidity Intelligence Sharing (Information Only)
    # =========================================================================
    # These methods coordinate INFORMATION about liquidity state.
    # No fund transfers between nodes - each node manages its own funds.
    # Knowing fleet state helps nodes make better independent decisions about:
    # - Fee adjustments to direct public flow helpfully
    # - Rebalancing timing to avoid route conflicts
    # - Topology planning to prioritize helpful channel opens
    # =========================================================================

    def record_member_liquidity_report(
        self,
        member_id: str,
        depleted_channels: List[Dict[str, Any]],
        saturated_channels: List[Dict[str, Any]],
        rebalancing_active: bool = False,
        rebalancing_peers: List[str] = None,
        enriched_needs: List[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Record a liquidity state report from a cl-revenue-ops instance.

        INFORMATION SHARING - enables coordinated fee/rebalance decisions.
        No sats transfer between nodes.

        Args:
            member_id: Reporting member's pubkey
            depleted_channels: List of {peer_id, local_pct, capacity_sats}
            saturated_channels: List of {peer_id, local_pct, capacity_sats}
            rebalancing_active: Whether member is currently rebalancing
            rebalancing_peers: Which peers they're rebalancing through
            enriched_needs: Flow-aware liquidity needs from cl-revenue-ops
                (overrides raw threshold-based assessment)

        Returns:
            {"status": "recorded", ...}
        """
        # Verify member exists
        member = self.database.get_member(member_id)
        if not member:
            return {"error": "member_not_found"}

        timestamp = int(time.time())

        # Store in database
        self.database.update_member_liquidity_state(
            member_id=member_id,
            depleted_count=len(depleted_channels),
            saturated_count=len(saturated_channels),
            rebalancing_active=rebalancing_active,
            rebalancing_peers=rebalancing_peers or [],
            timestamp=timestamp
        )

        # Update in-memory tracking for fast access
        with self._lock:
            state_entry = {
                "depleted_channels": depleted_channels,
                "saturated_channels": saturated_channels,
                "rebalancing_active": rebalancing_active,
                "rebalancing_peers": rebalancing_peers or [],
                "timestamp": timestamp
            }
            if enriched_needs is not None:
                state_entry["enriched_needs"] = enriched_needs[:10]  # Bound to 10
            self._member_liquidity_state[member_id] = state_entry

        if self.plugin:
            self.plugin.log(
                f"cl-hive: Recorded liquidity state from {member_id[:16]}...: "
                f"depleted={len(depleted_channels)}, saturated={len(saturated_channels)}, "
                f"rebalancing={rebalancing_active}",
                level='debug'
            )

        return {
            "status": "recorded",
            "depleted_count": len(depleted_channels),
            "saturated_count": len(saturated_channels)
        }

    def update_rebalancing_activity(
        self,
        member_id: str,
        rebalancing_active: bool,
        rebalancing_peers: List[str] = None
    ) -> Dict[str, Any]:
        """
        Targeted update of rebalancing activity for a member.

        Unlike record_member_liquidity_report() which overwrites all fields,
        this only updates rebalancing_active and rebalancing_peers, preserving
        existing depleted/saturated channel data.

        Args:
            member_id: Reporting member's pubkey
            rebalancing_active: Whether member is currently rebalancing
            rebalancing_peers: Which peers they're rebalancing through

        Returns:
            {"status": "updated", ...} or {"error": ...}
        """
        # Verify member exists
        member = self.database.get_member(member_id)
        if not member:
            return {"error": "member_not_found"}

        peers = rebalancing_peers or []

        # Targeted DB update (preserves depleted/saturated counts)
        self.database.update_rebalancing_activity(
            member_id=member_id,
            rebalancing_active=rebalancing_active,
            rebalancing_peers=peers
        )

        # Merge into in-memory state (preserve existing fields)
        with self._lock:
            existing = self._member_liquidity_state.get(member_id, {})
            existing["rebalancing_active"] = rebalancing_active
            existing["rebalancing_peers"] = peers
            existing["timestamp"] = int(time.time())
            self._member_liquidity_state[member_id] = existing

        if self.plugin:
            self.plugin.log(
                f"cl-hive: Updated rebalancing activity for {member_id[:16]}...: "
                f"active={rebalancing_active}, peers={len(peers)}",
                level='debug'
            )

        return {
            "status": "updated",
            "rebalancing_active": rebalancing_active,
            "rebalancing_peers_count": len(peers)
        }

    def get_fleet_liquidity_state(self) -> Dict[str, Any]:
        """
        Get fleet-wide liquidity state overview.

        INFORMATION ONLY - helps nodes understand fleet situation
        to make better independent decisions.

        Returns:
            Fleet liquidity summary for coordination
        """
        members = self.database.get_all_members()

        members_with_depleted = 0
        members_with_saturated = 0
        members_rebalancing = 0
        all_rebalancing_peers = set()

        # Snapshot shared state under lock
        with self._lock:
            state_snapshot = dict(self._member_liquidity_state)

        # Get our own state
        our_state = state_snapshot.get(self.our_pubkey, {})

        for member in members:
            member_id = member.get("peer_id")
            state = state_snapshot.get(member_id)

            if state:
                if state.get("depleted_channels"):
                    members_with_depleted += 1
                if state.get("saturated_channels"):
                    members_with_saturated += 1
                if state.get("rebalancing_active"):
                    members_rebalancing += 1
                    all_rebalancing_peers.update(state.get("rebalancing_peers", []))

        # Identify common bottleneck peers (multiple members have issues)
        bottleneck_peers = self._get_common_bottleneck_peers()

        return {
            "active": True,
            "fleet_summary": {
                "total_members": len(members),
                "members_with_depleted_channels": members_with_depleted,
                "members_with_saturated_channels": members_with_saturated,
                "members_rebalancing": members_rebalancing,
                "common_bottleneck_peers": bottleneck_peers
            },
            "our_state": {
                "depleted_channels": len(our_state.get("depleted_channels", [])),
                "saturated_channels": len(our_state.get("saturated_channels", [])),
                "rebalancing_active": our_state.get("rebalancing_active", False)
            },
            "rebalancing_activity": {
                "active_count": members_rebalancing,
                "peers_in_use": list(all_rebalancing_peers)
            }
        }

    def get_fleet_liquidity_needs(self) -> List[Dict[str, Any]]:
        """
        Get fleet liquidity needs for coordination.

        INFORMATION ONLY - helps nodes understand what others need
        so they can make coordinated fee/rebalance decisions.

        Returns:
            List of needs with relevance scores
        """
        needs = []

        with self._lock:
            state_snapshot = dict(self._member_liquidity_state)

        for member_id, state in state_snapshot.items():
            if member_id == self.our_pubkey:
                continue  # Skip ourselves

            # Get member health for priority
            member_health = self.database.get_member_health(member_id)
            health_score = member_health.get("overall_health", 50) if member_health else 50
            health_tier = member_health.get("health_tier", "stable") if member_health else "stable"

            # Process depleted channels (they need outbound)
            for ch in state.get("depleted_channels", []):
                peer_id = ch.get("peer_id")
                if not peer_id:
                    continue

                # Calculate relevance: how much we could help via fee adjustment
                relevance = self._calculate_relevance_score(peer_id)

                needs.append({
                    "member_id": member_id,
                    "need_type": "outbound",
                    "peer_id": peer_id,
                    "local_pct": ch.get("local_pct", 0),
                    "capacity_sats": ch.get("capacity_sats", 0),
                    "severity": "high" if ch.get("local_pct", 0) < 0.1 else "medium",
                    "member_health_tier": health_tier,
                    "our_relevance": relevance
                })

            # Process saturated channels (they need inbound)
            for ch in state.get("saturated_channels", []):
                peer_id = ch.get("peer_id")
                if not peer_id:
                    continue

                relevance = self._calculate_relevance_score(peer_id)

                needs.append({
                    "member_id": member_id,
                    "need_type": "inbound",
                    "peer_id": peer_id,
                    "local_pct": ch.get("local_pct", 1.0),
                    "capacity_sats": ch.get("capacity_sats", 0),
                    "severity": "high" if ch.get("local_pct", 1.0) > 0.9 else "medium",
                    "member_health_tier": health_tier,
                    "our_relevance": relevance
                })

        # Sort by severity and health tier (struggling members first)
        tier_priority = {"struggling": 0, "vulnerable": 1, "stable": 2, "thriving": 3}
        severity_priority = {"high": 0, "medium": 1, "low": 2}

        needs.sort(key=lambda n: (
            tier_priority.get(n["member_health_tier"], 2),
            severity_priority.get(n["severity"], 1),
            -n["our_relevance"]  # Higher relevance first
        ))

        return needs

    def get_fleet_corridor_needs(self) -> List[Dict[str, Any]]:
        """
        Build directional source->destination needs for corridor coordination.

        Prefers enriched directional needs reported by cl-revenue-ops. When no
        enriched pair data is present for a member, derive coarse directional
        pairs from saturated->depleted channel combinations.
        """
        needs = []
        relevance_cache: Dict[str, float] = {}

        def relevance_for(peer_id: str) -> float:
            if peer_id not in relevance_cache:
                relevance_cache[peer_id] = self._calculate_relevance_score(peer_id)
            return relevance_cache[peer_id]

        with self._lock:
            state_snapshot = dict(self._member_liquidity_state)

        tier_priority = {"struggling": 0, "vulnerable": 1, "stable": 2, "thriving": 3}
        severity_priority = {"high": 0, "medium": 1, "low": 2}

        for member_id, state in state_snapshot.items():
            if member_id == self.our_pubkey:
                continue

            member_health = self.database.get_member_health(member_id)
            health_tier = member_health.get("health_tier", "stable") if member_health else "stable"

            enriched_needs = state.get("enriched_needs") or []
            normalized_enriched = []
            for need in enriched_needs:
                source_peer_id = (
                    need.get("source_peer_id")
                    or need.get("from_peer_id")
                    or need.get("source")
                    or ""
                )
                destination_peer_id = (
                    need.get("destination_peer_id")
                    or need.get("to_peer_id")
                    or need.get("destination")
                    or ""
                )
                if (
                    not source_peer_id
                    or not destination_peer_id
                    or source_peer_id == destination_peer_id
                ):
                    continue

                capacity_sats = need.get("capacity_sats")
                if not isinstance(capacity_sats, (int, float)):
                    capacity_sats = need.get("amount_sats", 0)

                priority = str(
                    need.get("priority_tier")
                    or need.get("urgency")
                    or "medium"
                ).lower()
                severity = "high" if priority in {"critical", "high"} else "medium"
                relevance = max(
                    relevance_for(source_peer_id),
                    relevance_for(destination_peer_id),
                )
                normalized_enriched.append({
                    "member_id": member_id,
                    "source_peer_id": source_peer_id,
                    "destination_peer_id": destination_peer_id,
                    "capacity_sats": int(capacity_sats) if isinstance(capacity_sats, (int, float)) else 0,
                    "severity": severity,
                    "member_health_tier": health_tier,
                    "our_relevance": relevance,
                })

            if normalized_enriched:
                needs.extend(normalized_enriched)
                continue

            depleted_channels = [ch for ch in state.get("depleted_channels", []) if ch.get("peer_id")]
            saturated_channels = [ch for ch in state.get("saturated_channels", []) if ch.get("peer_id")]

            for source_ch in saturated_channels:
                source_peer_id = source_ch.get("peer_id", "")
                source_local_pct = float(source_ch.get("local_pct", 1.0) or 1.0)
                source_capacity = int(source_ch.get("capacity_sats", 0) or 0)
                for dest_ch in depleted_channels:
                    destination_peer_id = dest_ch.get("peer_id", "")
                    if (
                        not destination_peer_id
                        or source_peer_id == destination_peer_id
                    ):
                        continue

                    dest_local_pct = float(dest_ch.get("local_pct", 0.0) or 0.0)
                    dest_capacity = int(dest_ch.get("capacity_sats", 0) or 0)
                    severity = (
                        "high"
                        if source_local_pct > 0.9 or dest_local_pct < 0.1
                        else "medium"
                    )
                    relevance = max(
                        relevance_for(source_peer_id),
                        relevance_for(destination_peer_id),
                    )
                    needs.append({
                        "member_id": member_id,
                        "source_peer_id": source_peer_id,
                        "destination_peer_id": destination_peer_id,
                        "capacity_sats": min(source_capacity, dest_capacity),
                        "severity": severity,
                        "member_health_tier": health_tier,
                        "our_relevance": relevance,
                    })

        needs.sort(key=lambda n: (
            tier_priority.get(n["member_health_tier"], 2),
            severity_priority.get(n["severity"], 1),
            -n["our_relevance"],
            -int(n.get("capacity_sats", 0) or 0),
        ))
        return needs

    def _calculate_relevance_score(self, peer_id: str) -> float:
        """
        Calculate how relevant we are to helping with a peer.

        Based on whether we have a channel to this peer and our balance state.
        Higher score = we're better positioned to influence flow via fees.

        Note: Makes an RPC call (listpeerchannels). Callers are responsible for
        ensuring RPC serialization (e.g., via RPC_LOCK or ThreadSafeRpcProxy).
        Currently called from get_fleet_liquidity_needs() which uses
        ThreadSafeRpcProxy for RPC serialization.
        """
        try:
            channels = self.plugin.rpc.call("listpeerchannels", {"id": peer_id})
            our_channels = channels.get("channels", [])

            if not our_channels:
                return 0.0  # No direct connection

            # Sum capacity to this peer
            total_capacity = 0
            total_local = 0

            for ch in our_channels:
                if ch.get("state") != "CHANNELD_NORMAL":
                    continue
                capacity = ch.get("total_msat", 0) // 1000
                local = ch.get("to_us_msat", 0) // 1000
                total_capacity += capacity
                total_local += local

            if total_capacity == 0:
                return 0.0

            # Balanced channels are more useful for flow influence
            balance_ratio = total_local / total_capacity
            # Score peaks at 50% balance
            balance_score = 1.0 - abs(0.5 - balance_ratio) * 2

            # Larger capacity = more influence
            capacity_score = min(1.0, total_capacity / 10_000_000)  # Cap at 10M

            return (balance_score * 0.6 + capacity_score * 0.4)

        except Exception:
            return 0.0

    def _get_common_bottleneck_peers(self) -> List[str]:
        """
        Identify peers that multiple hive members have liquidity issues with.

        These are priority targets for fee coordination or new channel opens.
        """
        peer_issue_count: Dict[str, int] = defaultdict(int)

        with self._lock:
            state_values = list(self._member_liquidity_state.values())

        for state in state_values:
            for ch in state.get("depleted_channels", []):
                peer_id = ch.get("peer_id")
                if peer_id:
                    peer_issue_count[peer_id] += 1

            for ch in state.get("saturated_channels", []):
                peer_id = ch.get("peer_id")
                if peer_id:
                    peer_issue_count[peer_id] += 1

        # Return peers with issues from 2+ members
        bottlenecks = [
            peer_id for peer_id, count in peer_issue_count.items()
            if count >= 2
        ]

        return sorted(bottlenecks, key=lambda p: peer_issue_count[p], reverse=True)[:10]

    def check_rebalancing_conflict(self, peer_id: str) -> Dict[str, Any]:
        """
        Check if another fleet member is actively rebalancing through a peer.

        INFORMATION ONLY - helps avoid competing for the same routes.

        Args:
            peer_id: The peer to check

        Returns:
            Conflict info if found
        """
        with self._lock:
            state_snapshot = dict(self._member_liquidity_state)

        for member_id, state in state_snapshot.items():
            if member_id == self.our_pubkey:
                continue

            if not state.get("rebalancing_active"):
                continue

            if peer_id in state.get("rebalancing_peers", []):
                return {
                    "conflict": True,
                    "member_id": member_id,
                    "peer_id": peer_id,
                    "recommendation": "delay_rebalance",
                    "reason": f"Member {member_id[:12]}... is actively rebalancing through this peer"
                }

        return {"conflict": False}

    def _log(self, message: str, level: str = "debug") -> None:
        """Log a message if plugin is available."""
        if self.plugin:
            self.plugin.log(f"LIQUIDITY_COORD: {message}", level=level)
