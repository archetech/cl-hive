"""
Fee Coordination Module

Provides coordinated fee management for the trusted fleet:
- Flow corridor assignment (route ownership)
- Competition avoidance (don't undercut fleet members)
- Centrality-based fee adjustment
- Egress desaturation bias
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import network_metrics

# =============================================================================
# CONSTANTS
# =============================================================================

# Fleet fee bounds
FLEET_FEE_FLOOR_PPM = 50          # Never go below this (avoid drain)
FLEET_FEE_CEILING_PPM = 2500      # Don't price out flow
DEFAULT_FEE_PPM = 500             # Default when no signals

# Primary/secondary fee multipliers
PRIMARY_FEE_MULTIPLIER = 1.0      # Primary: competitive fee
SECONDARY_FEE_MULTIPLIER = 1.5    # Secondary: premium for overflow

# =============================================================================
# SALIENCE DETECTION (Noise Filtering)
# =============================================================================

# Fee change salience
SALIENT_FEE_CHANGE_PCT = 0.05        # 5% fee change minimum to be salient
SALIENT_FEE_CHANGE_MIN_PPM = 10      # At least 10 ppm absolute change
SALIENT_FEE_CHANGE_COOLDOWN = 3600   # 1 hour between fee changes per channel

# =============================================================================
# CENTRALITY-BASED FEE ADJUSTMENT
# =============================================================================

CENTRALITY_FEE_ADJUSTMENT_ENABLED = True
CENTRALITY_FEE_HIGH_THRESHOLD = 0.7
CENTRALITY_FEE_LOW_THRESHOLD = 0.3
CENTRALITY_FEE_MAX_PREMIUM_PCT = 0.15
CENTRALITY_FEE_MAX_DISCOUNT_PCT = 0.10

# Hive egress desaturation bias
EGRESS_DESATURATION_MIN_LOCAL_PCT = 90.0
EGRESS_DESATURATION_BASE_SURCHARGE_PPM = 25
EGRESS_DESATURATION_MAX_SURCHARGE_PPM = 150


def is_fee_change_salient(
    current_fee: int,
    new_fee: int,
    last_change_time: float = 0
) -> Tuple[bool, str]:
    """
    Determine if a fee change is significant enough to warrant action.

    Returns:
        Tuple of (is_salient, reason)
    """
    # Check cooldown
    now = time.time()
    if last_change_time > 0 and (now - last_change_time) < SALIENT_FEE_CHANGE_COOLDOWN:
        return False, "cooldown_active"

    if current_fee == new_fee:
        return False, "no_change"

    # Calculate absolute and percentage change
    abs_change = abs(new_fee - current_fee)
    pct_change = abs_change / max(current_fee, 1)

    # Must meet BOTH minimum thresholds
    if abs_change < SALIENT_FEE_CHANGE_MIN_PPM:
        return False, f"abs_change_too_small ({abs_change} < {SALIENT_FEE_CHANGE_MIN_PPM} ppm)"

    if pct_change < SALIENT_FEE_CHANGE_PCT:
        return False, f"pct_change_too_small ({pct_change * 100:.1f}% < {SALIENT_FEE_CHANGE_PCT * 100}%)"

    return True, "salient"


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class FlowCorridor:
    """
    A flow corridor represents a (source, destination) pair that the fleet serves.
    """
    source_peer_id: str
    destination_peer_id: str
    source_alias: Optional[str] = None
    destination_alias: Optional[str] = None

    # Members that can serve this corridor
    capable_members: List[str] = field(default_factory=list)

    # Assigned primary member (gets competitive fee)
    primary_member: Optional[str] = None

    # Metrics for assignment decisions
    total_volume_sats: int = 0
    avg_fee_earned_ppm: int = 0
    competition_level: str = "none"  # "none", "low", "medium", "high"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_peer_id": self.source_peer_id,
            "destination_peer_id": self.destination_peer_id,
            "source_alias": self.source_alias,
            "destination_alias": self.destination_alias,
            "capable_members": self.capable_members,
            "primary_member": self.primary_member,
            "total_volume_sats": self.total_volume_sats,
            "avg_fee_earned_ppm": self.avg_fee_earned_ppm,
            "competition_level": self.competition_level
        }


@dataclass
class CorridorAssignment:
    """
    Assignment of a flow corridor to a primary member.
    """
    corridor: FlowCorridor
    primary_member: str
    secondary_members: List[str]

    # Fee recommendations
    primary_fee_ppm: int
    secondary_fee_ppm: int

    # Assignment reasoning
    assignment_reason: str
    confidence: float  # 0.0 to 1.0

    timestamp: int = 0

    def __post_init__(self):
        self.timestamp = int(time.time())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "corridor": self.corridor.to_dict(),
            "primary_member": self.primary_member,
            "secondary_members": self.secondary_members,
            "primary_fee_ppm": self.primary_fee_ppm,
            "secondary_fee_ppm": self.secondary_fee_ppm,
            "assignment_reason": self.assignment_reason,
            "confidence": round(self.confidence, 2),
            "timestamp": self.timestamp
        }


@dataclass
class FeeRecommendation:
    """
    Coordinated fee recommendation for a channel.
    """
    channel_id: str
    peer_id: str
    recommended_fee_ppm: int

    # Context
    is_primary: bool
    corridor_source: Optional[str] = None
    corridor_destination: Optional[str] = None

    # Factors that influenced recommendation
    floor_applied: bool = False
    ceiling_applied: bool = False
    centrality_adjustment_pct: float = 0.0
    size_adjustment_pct: float = 0.0
    our_hive_centrality: float = 0.0

    # Salience detection (noise filtering)
    current_fee_ppm: int = 0
    is_salient: bool = True
    salience_reason: str = ""

    # Confidence
    confidence: float = 0.5
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "channel_id": self.channel_id,
            "peer_id": self.peer_id,
            "current_fee_ppm": self.current_fee_ppm,
            "recommended_fee_ppm": self.recommended_fee_ppm,
            "is_primary": self.is_primary,
            "corridor_source": self.corridor_source,
            "corridor_destination": self.corridor_destination,
            "floor_applied": self.floor_applied,
            "ceiling_applied": self.ceiling_applied,
            "is_salient": self.is_salient,
            "salience_reason": self.salience_reason,
            "confidence": round(self.confidence, 2),
            "reason": self.reason
        }
        if self.centrality_adjustment_pct != 0.0:
            result["centrality_adjustment_pct"] = round(self.centrality_adjustment_pct * 100, 1)
            result["our_hive_centrality"] = round(self.our_hive_centrality, 3)
        if self.size_adjustment_pct != 0.0:
            result["size_adjustment_pct"] = round(self.size_adjustment_pct * 100, 1)
        return result


# =============================================================================
# FLOW CORRIDOR ASSIGNMENT
# =============================================================================

class FlowCorridorManager:
    """
    Manages flow corridor assignment to eliminate internal competition.

    Assigns a "primary" member for each (source, destination) pair.
    Primary member gets competitive fees, others get premium fees.
    """

    def __init__(
        self,
        database: Any,
        plugin: Any,
        state_manager: Any = None,
        liquidity_coordinator: Any = None
    ):
        self.database = database
        self.plugin = plugin
        self.state_manager = state_manager
        self.liquidity_coordinator = liquidity_coordinator
        self.our_pubkey: Optional[str] = None

        # Cache of assignments -- single atomic tuple: (dict, timestamp)
        self._assignments_snapshot: Tuple[Dict[Tuple[str, str], CorridorAssignment], float] = ({}, 0)
        self._assignments_ttl: float = 3600  # 1 hour cache

    def set_our_pubkey(self, pubkey: str) -> None:
        self.our_pubkey = pubkey

    def _log(self, msg: str, level: str = "info") -> None:
        if self.plugin:
            self.plugin.log(f"cl-hive: [FlowCorridor] {msg}", level=level)

    def identify_corridors(self) -> List[FlowCorridor]:
        """
        Identify all flow corridors the fleet can serve.

        A corridor exists when 2+ members can route between same (source, dest).
        """
        if not self.liquidity_coordinator:
            return []

        # Build corridors from fleet liquidity needs
        needs = self.liquidity_coordinator.get_fleet_liquidity_needs()
        corridors = []
        seen = set()
        for need in needs:
            peer_id = need.get("target_peer_id", "")
            if not peer_id or peer_id in seen:
                continue
            seen.add(peer_id)
            members = [need.get("reporter_id", "")]
            corridor = FlowCorridor(
                source_peer_id=peer_id,
                destination_peer_id=peer_id,
                source_alias=None,
                destination_alias=None,
                capable_members=members,
                total_volume_sats=need.get("amount_sats", 0),
                competition_level=self._assess_competition_level(len(members))
            )
            corridors.append(corridor)

        return corridors

    def _assess_competition_level(self, member_count: int) -> str:
        """Assess competition level based on number of competing members."""
        if member_count <= 1:
            return "none"
        elif member_count == 2:
            return "low"
        elif member_count <= 4:
            return "medium"
        else:
            return "high"

    def assign_corridor(self, corridor: FlowCorridor) -> CorridorAssignment:
        """
        Assign a corridor to a primary member.

        Selection criteria:
        1. Position (shortest path to both source and dest)
        2. Capacity (more liquidity available)
        3. Historical performance (higher success rate)
        """
        if not corridor.capable_members:
            return CorridorAssignment(
                corridor=corridor,
                primary_member="",
                secondary_members=[],
                primary_fee_ppm=DEFAULT_FEE_PPM,
                secondary_fee_ppm=int(DEFAULT_FEE_PPM * SECONDARY_FEE_MULTIPLIER),
                assignment_reason="no_capable_members",
                confidence=0.0
            )

        # Score each member
        member_scores: Dict[str, float] = {}
        for member_id in corridor.capable_members:
            score = self._score_member_for_corridor(
                member_id, corridor.source_peer_id, corridor.destination_peer_id
            )
            member_scores[member_id] = score

        # Select primary (highest score)
        sorted_members = sorted(
            member_scores.items(),
            key=lambda x: x[1],
            reverse=True
        )

        primary_member = sorted_members[0][0]
        primary_score = sorted_members[0][1]
        secondary_members = [m[0] for m in sorted_members[1:]]

        # Calculate fees
        base_fee = self._estimate_corridor_base_fee(corridor)
        primary_fee = max(FLEET_FEE_FLOOR_PPM, int(base_fee * PRIMARY_FEE_MULTIPLIER))
        secondary_fee = max(
            FLEET_FEE_FLOOR_PPM * 2,
            int(base_fee * SECONDARY_FEE_MULTIPLIER)
        )

        # Calculate confidence
        confidence = min(1.0, primary_score / 10.0) if primary_score > 0 else 0.3

        return CorridorAssignment(
            corridor=corridor,
            primary_member=primary_member,
            secondary_members=secondary_members,
            primary_fee_ppm=primary_fee,
            secondary_fee_ppm=secondary_fee,
            assignment_reason=f"highest_score_{primary_score:.2f}",
            confidence=confidence
        )

    def _score_member_for_corridor(
        self,
        member_id: str,
        source: str,
        destination: str
    ) -> float:
        """
        Score a member's suitability for a corridor.
        """
        score = 0.0

        # Get member state
        if self.state_manager:
            state = self.state_manager.get_peer_state(member_id)
            if state:
                # Capacity score (normalized)
                capacity = getattr(state, 'capacity_sats', 0)
                score += capacity / 10_000_000  # 10M sats = 1 point

                # Check if this is us - we can get more detailed info
                if member_id == self.our_pubkey and self.plugin:
                    try:
                        for peer_id in [source, destination]:
                            channels = self.plugin.rpc.listpeerchannels(id=peer_id)
                            for ch in channels.get("channels", []):
                                if ch.get("state") == "CHANNELD_NORMAL":
                                    cap = ch.get("total_msat", 0) // 1000
                                    local = ch.get("to_us_msat", 0) // 1000
                                    # Balanced channels are better
                                    if cap > 0:
                                        balance_pct = local / cap
                                        balance_score = 1.0 - abs(0.5 - balance_pct) * 2
                                        score += balance_score
                    except Exception:
                        pass

        return score

    def _estimate_corridor_base_fee(self, corridor: FlowCorridor) -> int:
        """Estimate base fee for a corridor based on competition and volume."""
        # Higher competition = lower fees needed
        competition_factor = {
            "none": 1.5,
            "low": 1.2,
            "medium": 1.0,
            "high": 0.8
        }.get(corridor.competition_level, 1.0)

        return int(DEFAULT_FEE_PPM * competition_factor)

    def get_assignments(self, force_refresh: bool = False) -> List[CorridorAssignment]:
        """Get all corridor assignments, refreshing if needed."""
        now = time.time()

        assignments, ts = self._assignments_snapshot
        if (not force_refresh and
            assignments and
            now - ts < self._assignments_ttl):
            return list(assignments.values())

        # Refresh assignments (build into local dict, then atomic swap)
        corridors = self.identify_corridors()
        new_assignments = {}

        for corridor in corridors:
            assignment = self.assign_corridor(corridor)
            key = (corridor.source_peer_id, corridor.destination_peer_id)
            new_assignments[key] = assignment

        self._assignments_snapshot = (new_assignments, now)
        self._log(f"Refreshed {len(new_assignments)} corridor assignments")

        return list(new_assignments.values())

    def get_fee_for_member(
        self,
        member_id: str,
        source: str,
        destination: str
    ) -> Tuple[int, bool]:
        """
        Get recommended fee for member on a corridor.

        Returns (fee_ppm, is_primary)
        """
        now = time.time()
        assignments, ts = self._assignments_snapshot
        if (not assignments) or (now - ts >= self._assignments_ttl):
            self.get_assignments(force_refresh=True)

        key = (source, destination)
        assignments, _ = self._assignments_snapshot
        assignment = assignments.get(key)

        if not assignment:
            return DEFAULT_FEE_PPM, False

        if assignment.primary_member == member_id:
            return assignment.primary_fee_ppm, True
        else:
            return assignment.secondary_fee_ppm, False


# =============================================================================
# FEE COORDINATION MANAGER (Main Interface)
# =============================================================================

class FeeCoordinationManager:
    """
    Main interface for fee coordination.

    Integrates:
    - Flow corridor assignment (competition avoidance)
    - Centrality-based fee adjustment
    - Size-aware fee adjustment
    - Egress desaturation bias
    """

    def __init__(
        self,
        database: Any,
        plugin: Any,
        state_manager: Any = None,
        liquidity_coordinator: Any = None,
        gossip_mgr: Any = None,
    ):
        self.database = database
        self.plugin = plugin
        self.our_pubkey: Optional[str] = None

        # Initialize corridor manager
        self.corridor_mgr = FlowCorridorManager(
            database, plugin, state_manager, liquidity_coordinator
        )

        # Lock protecting fee change time tracking
        self._lock = threading.Lock()

        # Salience detection: Track last fee change times per channel
        self._fee_change_times: Dict[str, float] = {}

        # Optional reference to FeeIntelligenceManager for cross-system blending
        self.fee_intelligence_mgr = None

        # Optional reference to TrafficIntelligenceManager for size-aware fees
        self.traffic_intel_mgr = None

    def set_our_pubkey(self, pubkey: str) -> None:
        self.our_pubkey = pubkey
        self.corridor_mgr.set_our_pubkey(pubkey)

    def set_fee_intelligence_mgr(self, mgr: Any) -> None:
        """Set reference to FeeIntelligenceManager for cross-system blending."""
        self.fee_intelligence_mgr = mgr

    def set_traffic_intel_mgr(self, mgr: Any) -> None:
        """Set reference to TrafficIntelligenceManager for size-aware fee enrichment."""
        self.traffic_intel_mgr = mgr

    def _is_hive_member_peer(self, peer_id: str) -> bool:
        """Return True when the peer is a fleet member."""
        if not self.database or not peer_id:
            return False
        member = self.database.get_member(peer_id)
        return bool(member)

    def _resolve_peer_id_from_channel(self, channel_id: Optional[str]) -> str:
        """Resolve peer_id from a short_channel_id when available."""
        if not channel_id or not self.plugin:
            return ""

        try:
            channels = self.plugin.rpc.listpeerchannels()
            for ch in channels.get("channels", []):
                if ch.get("short_channel_id") == channel_id:
                    return ch.get("peer_id", "")
        except Exception:
            pass
        return ""

    @staticmethod
    def _normalize_local_pct(value: Any) -> float:
        """Normalize local balance percent values from ratio or percent form."""
        try:
            local_pct = float(value)
        except (TypeError, ValueError):
            return 0.0

        if local_pct <= 1.0:
            local_pct *= 100.0
        return max(0.0, min(100.0, local_pct))

    def _get_our_saturated_hive_channels(self) -> List[Dict[str, Any]]:
        """Return locally saturated fleet-member channels from existing liquidity state."""
        liquidity_coord = self.corridor_mgr.liquidity_coordinator
        if not liquidity_coord or not self.our_pubkey:
            return []

        lock = getattr(liquidity_coord, "_lock", None)
        state_map = getattr(liquidity_coord, "_member_liquidity_state", None)
        if lock is None or state_map is None:
            return []

        with lock:
            our_state = dict(state_map.get(self.our_pubkey, {}))

        saturated = []
        for channel in our_state.get("saturated_channels", []):
            hive_peer_id = channel.get("peer_id", "")
            if not self._is_hive_member_peer(hive_peer_id):
                continue
            enriched = dict(channel)
            enriched["local_pct"] = self._normalize_local_pct(channel.get("local_pct"))
            if enriched["local_pct"] < EGRESS_DESATURATION_MIN_LOCAL_PCT:
                continue
            saturated.append(enriched)

        saturated.sort(key=lambda ch: ch.get("local_pct", 0.0), reverse=True)
        return saturated

    def _peer_competes_with_saturated_hive_egress(
        self,
        target_peer_id: str,
        hive_peer_id: str
    ) -> Tuple[bool, str]:
        """
        Check whether an external peer competes with a saturated fleet-member egress.
        """
        state_manager = self.corridor_mgr.state_manager
        if state_manager:
            state = state_manager.get_peer_state(hive_peer_id)
            topology = set(getattr(state, "topology", []) or [])
            if target_peer_id in topology:
                return True, "member_topology"

        assignments = self.corridor_mgr.get_assignments()
        for assignment in assignments:
            if assignment.primary_member != hive_peer_id and hive_peer_id not in assignment.secondary_members:
                continue
            corridor = assignment.corridor
            if target_peer_id in (corridor.source_peer_id, corridor.destination_peer_id):
                return True, "corridor_assignment"

        return False, ""

    def get_egress_desaturation_bias(
        self,
        channel_id: Optional[str] = None,
        peer_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Report whether a local non-fleet exit should be biased upward to favor a
        saturated local fleet egress.
        """
        resolved_peer_id = peer_id or self._resolve_peer_id_from_channel(channel_id)
        base_result = {
            "channel_id": channel_id,
            "peer_id": resolved_peer_id,
            "matched": False,
            "recommended_surcharge_ppm": 0,
            "max_surcharge_ppm": EGRESS_DESATURATION_MAX_SURCHARGE_PPM,
            "confidence": 0.0,
            "reason": "",
            "signal_source": "",
            "saturated_hive_peer_id": "",
            "saturated_hive_channel_id": "",
            "saturated_local_pct": 0.0,
        }

        if not resolved_peer_id:
            base_result["reason"] = "peer_not_found"
            return base_result

        saturated_hive_channels = self._get_our_saturated_hive_channels()
        if not saturated_hive_channels:
            base_result["reason"] = "no_saturated_hive_egress"
            return base_result

        for saturated in saturated_hive_channels:
            hive_peer_id = saturated.get("peer_id", "")
            competes, signal_source = self._peer_competes_with_saturated_hive_egress(
                resolved_peer_id,
                hive_peer_id,
            )
            if not competes:
                continue

            local_pct = self._normalize_local_pct(saturated.get("local_pct"))
            severity = max(
                0.0,
                min(1.0, (local_pct - EGRESS_DESATURATION_MIN_LOCAL_PCT) / 10.0),
            )
            surcharge = int(round(
                EGRESS_DESATURATION_BASE_SURCHARGE_PPM +
                severity * (
                    EGRESS_DESATURATION_MAX_SURCHARGE_PPM -
                    EGRESS_DESATURATION_BASE_SURCHARGE_PPM
                )
            ))
            surcharge = max(
                EGRESS_DESATURATION_BASE_SURCHARGE_PPM,
                min(EGRESS_DESATURATION_MAX_SURCHARGE_PPM, surcharge),
            )

            return {
                **base_result,
                "matched": True,
                "recommended_surcharge_ppm": surcharge,
                "confidence": round(min(0.95, 0.5 + severity * 0.4), 2),
                "reason": "competes_with_saturated_hive_egress",
                "signal_source": signal_source,
                "saturated_hive_peer_id": hive_peer_id,
                "saturated_hive_channel_id": saturated.get("channel_id", ""),
                "saturated_local_pct": local_pct,
            }

        base_result["reason"] = "no_competing_saturated_hive_egress"
        return base_result

    def _log(self, msg: str, level: str = "info") -> None:
        if self.plugin:
            self.plugin.log(f"cl-hive: [FeeCoord] {msg}", level=level)

    def _get_last_fee_change_time(self, channel_id: str) -> float:
        """Get the timestamp of the last fee change for a channel."""
        with self._lock:
            return self._fee_change_times.get(channel_id, 0)

    def record_fee_change(self, channel_id: str) -> None:
        """Record that a fee change was made for a channel."""
        with self._lock:
            self._fee_change_times[channel_id] = time.time()

            # Evict entries past their cooldown (no longer useful)
            if len(self._fee_change_times) > 500:
                cutoff = time.time() - SALIENT_FEE_CHANGE_COOLDOWN * 2
                self._fee_change_times = {
                    k: v for k, v in self._fee_change_times.items()
                    if v > cutoff
                }
        self._log(f"Recorded fee change for {channel_id}")

    def _get_centrality_fee_adjustment(self) -> Tuple[float, float]:
        """
        Calculate fee adjustment based on our node's fleet centrality.

        Returns:
            Tuple of (adjustment_pct, our_centrality)
        """
        if not CENTRALITY_FEE_ADJUSTMENT_ENABLED:
            return 0.0, 0.0

        if not self.our_pubkey:
            return 0.0, 0.0

        calculator = network_metrics.get_calculator()
        if not calculator:
            return 0.0, 0.0

        metrics = calculator.get_member_metrics(self.our_pubkey)
        if not metrics:
            return 0.0, 0.0

        centrality = metrics.hive_centrality

        if centrality >= CENTRALITY_FEE_HIGH_THRESHOLD:
            excess = centrality - CENTRALITY_FEE_HIGH_THRESHOLD
            max_excess = 1.0 - CENTRALITY_FEE_HIGH_THRESHOLD
            adjustment = CENTRALITY_FEE_MAX_PREMIUM_PCT * (excess / max_excess) if max_excess > 0 else 0
            return adjustment, centrality

        if centrality <= CENTRALITY_FEE_LOW_THRESHOLD:
            deficit = CENTRALITY_FEE_LOW_THRESHOLD - centrality
            max_deficit = CENTRALITY_FEE_LOW_THRESHOLD
            adjustment = -CENTRALITY_FEE_MAX_DISCOUNT_PCT * (deficit / max_deficit) if max_deficit > 0 else 0
            return adjustment, centrality

        return 0.0, centrality

    def get_size_aware_adjustment(self, peer_id: str) -> float:
        """
        Calculate fee adjustment based on fleet traffic intelligence forward sizes.

        Returns:
            Fee multiplier bounded to [0.8, 1.3]
        """
        if not self.traffic_intel_mgr:
            return 1.0

        try:
            profile = self.traffic_intel_mgr.get_aggregated_profile(peer_id)
        except Exception:
            return 1.0

        if not profile:
            return 1.0

        avg_fwd = profile.get("avg_forward_size_sats", 0)
        daily_vol = profile.get("daily_volume_sats", 0)
        confidence = profile.get("confidence", 0)

        if confidence < 0.3:
            return 1.0

        multiplier = 1.0

        if avg_fwd > 500_000:
            multiplier = 0.9
        elif avg_fwd < 10_000 and avg_fwd > 0:
            multiplier = 1.1

        if daily_vol > 10_000_000:
            multiplier += 0.05

        return max(0.8, min(1.3, multiplier))

    def get_fee_recommendation(
        self,
        channel_id: str,
        peer_id: str,
        current_fee: int,
        local_balance_pct: float,
        source_hint: str = None,
        destination_hint: str = None,
        use_centrality_adjustment: bool = True
    ) -> FeeRecommendation:
        """
        Get coordinated fee recommendation for a channel.

        Combines corridor assignment and centrality signals.
        """
        # Start with current fee
        recommended_fee = current_fee
        is_primary = False
        floor_applied = False
        ceiling_applied = False
        centrality_adjustment_pct = 0.0
        our_hive_centrality = 0.0
        reasons = []

        # 1. Check corridor assignment
        if source_hint and destination_hint:
            corridor_fee, is_primary = self.corridor_mgr.get_fee_for_member(
                self.our_pubkey or "", source_hint, destination_hint
            )
            if corridor_fee != DEFAULT_FEE_PPM:
                recommended_fee = corridor_fee
                reasons.append(f"corridor_{'primary' if is_primary else 'secondary'}")

        # 2. Incorporate fee intelligence if available
        if self.fee_intelligence_mgr:
            try:
                intel = self.fee_intelligence_mgr.get_fee_recommendation(
                    target_peer_id=peer_id,
                    our_health=50
                )
                if intel.get("confidence", 0) > 0.3:
                    intel_fee = intel["recommended_fee_ppm"]
                    blend_weight = min(0.3, intel["confidence"] * 0.4)
                    recommended_fee = int(
                        recommended_fee * (1 - blend_weight) +
                        intel_fee * blend_weight
                    )
                    reasons.append(f"intelligence_{intel['confidence']:.2f}")
            except Exception:
                pass

        # 3. Apply centrality-based adjustment
        if use_centrality_adjustment:
            centrality_adjustment_pct, our_hive_centrality = self._get_centrality_fee_adjustment()
            if centrality_adjustment_pct != 0.0:
                adjustment = int(recommended_fee * centrality_adjustment_pct)
                recommended_fee += adjustment
                if centrality_adjustment_pct > 0:
                    reasons.append(f"centrality_premium_{centrality_adjustment_pct*100:.1f}%")
                else:
                    reasons.append(f"centrality_discount_{abs(centrality_adjustment_pct)*100:.1f}%")

        # 4. Apply size-aware adjustment
        size_adjustment_pct = 0.0
        size_multiplier = self.get_size_aware_adjustment(peer_id)
        if size_multiplier != 1.0:
            size_adjustment_pct = size_multiplier - 1.0
            recommended_fee = int(recommended_fee * size_multiplier)
            if size_multiplier > 1.0:
                reasons.append(f"size_premium_{size_adjustment_pct*100:.1f}%")
            else:
                reasons.append(f"size_discount_{size_adjustment_pct*100:.1f}%")

        # 5. Apply floor and ceiling
        if recommended_fee < FLEET_FEE_FLOOR_PPM:
            recommended_fee = FLEET_FEE_FLOOR_PPM
            floor_applied = True
            reasons.append("floor_applied")

        if recommended_fee > FLEET_FEE_CEILING_PPM:
            recommended_fee = FLEET_FEE_CEILING_PPM
            ceiling_applied = True
            reasons.append("ceiling_applied")

        # Calculate confidence
        confidence = 0.5
        if is_primary:
            confidence += 0.2
        if our_hive_centrality >= CENTRALITY_FEE_HIGH_THRESHOLD:
            confidence += 0.1
        confidence = min(0.95, confidence)

        # 6. Check salience (is this change worth making?)
        is_salient, salience_reason = is_fee_change_salient(
            current_fee=current_fee,
            new_fee=recommended_fee,
            last_change_time=self._get_last_fee_change_time(channel_id)
        )

        if not is_salient:
            recommended_fee = current_fee
            reasons.append(f"not_salient:{salience_reason}")
        elif recommended_fee != current_fee:
            self.record_fee_change(channel_id)

        return FeeRecommendation(
            channel_id=channel_id,
            peer_id=peer_id,
            current_fee_ppm=current_fee,
            recommended_fee_ppm=recommended_fee,
            is_primary=is_primary,
            corridor_source=source_hint,
            corridor_destination=destination_hint,
            floor_applied=floor_applied,
            ceiling_applied=ceiling_applied,
            centrality_adjustment_pct=centrality_adjustment_pct,
            size_adjustment_pct=size_adjustment_pct,
            our_hive_centrality=our_hive_centrality,
            is_salient=is_salient,
            salience_reason=salience_reason,
            confidence=confidence,
            reason="; ".join(reasons) if reasons else "default"
        )

    def record_routing_outcome(
        self,
        channel_id: str,
        peer_id: str,
        fee_ppm: int,
        success: bool,
        revenue_sats: int,
        volume_sats: int = 0,
        source: str = None,
        destination: str = None
    ) -> None:
        """
        Record a routing outcome (no-op after simplification, kept for caller compatibility).
        """
        pass

    def save_state_to_database(self) -> Dict[str, int]:
        """
        No-op after simplification. Corridor assignments are ephemeral cache.
        """
        return {}

    def restore_state_from_database(self) -> Dict[str, int]:
        """
        No-op after simplification. Corridor assignments are ephemeral cache.
        """
        return {}

    def should_auto_backfill(self) -> bool:
        """No longer needed -- corridor assignments don't require backfill."""
        return False

    def get_coordination_status(self) -> Dict:
        """Get overall fee coordination status."""
        assignments = self.corridor_mgr.get_assignments()

        return {
            "corridor_assignments": len(assignments),
            "fleet_fee_floor": FLEET_FEE_FLOOR_PPM,
            "fleet_fee_ceiling": FLEET_FEE_CEILING_PPM,
            "assignments": [a.to_dict() for a in assignments[:10]],
        }

    def get_time_fee_adjustment(
        self,
        channel_id: str,
        base_fee: int
    ) -> Dict[str, Any]:
        """No-op stub kept for caller compatibility."""
        return {
            "channel_id": channel_id,
            "base_fee_ppm": base_fee,
            "adjusted_fee_ppm": base_fee,
            "adjustment_pct": 0.0,
            "adjustment_type": "none",
        }

    def get_time_fee_status(self) -> Dict[str, Any]:
        """No-op stub kept for caller compatibility."""
        return {"enabled": False, "active_adjustments": 0}

    def get_channel_peak_hours(self, channel_id: str) -> List[Dict[str, Any]]:
        """No-op stub kept for caller compatibility."""
        return []

    def get_channel_low_hours(self, channel_id: str) -> List[Dict[str, Any]]:
        """No-op stub kept for caller compatibility."""
        return []
