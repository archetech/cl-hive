"""
Fee Coordination Module (Phase 2 - Fee Coordination)

Provides coordinated fee management for the hive fleet:
- Flow corridor assignment (route ownership)
- Adaptive fee controller (pheromone-based learning)
- Fleet fee floor/ceiling enforcement
- Stigmergic fee coordination (indirect coordination via markers)
- Mycelium defense system (collective defense against draining peers)

This module integrates with cl-revenue-ops for fee execution while
maintaining coordination at the cl-hive layer.
"""

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

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

# Adaptive fee controller (pheromone-based)
BASE_EVAPORATION_RATE = 0.2       # 20% base evaporation per cycle
MIN_EVAPORATION_RATE = 0.1        # Minimum evaporation
MAX_EVAPORATION_RATE = 0.9        # Maximum evaporation
PHEROMONE_EXPLOIT_THRESHOLD = 10.0  # Above this: exploit current fee
PHEROMONE_DEPOSIT_SCALE = 0.001   # Scale factor for deposits

# Stigmergic markers
MARKER_HALF_LIFE_HOURS = 24       # Markers decay with 24-hour half-life
MARKER_MIN_STRENGTH = 0.1         # Below this, markers are ignored

# Mycelium defense
DRAIN_RATIO_THRESHOLD = 5.0       # 5:1 outflow ratio = drain attack
FAILURE_RATE_THRESHOLD = 0.5      # >50% failures = unreliable peer
WARNING_TTL_HOURS = 24            # Warnings expire after 24 hours
DEFENSIVE_FEE_MAX_MULTIPLIER = 3.0  # Max 3x fee increase for defense


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
class RouteMarker:
    """
    Stigmergic marker left after routing attempt.

    Other fleet members read these markers and adjust fees accordingly.
    This enables indirect coordination without explicit messaging.
    """
    depositor: str           # Member who left the marker
    source_peer_id: str
    destination_peer_id: str
    fee_ppm: int
    success: bool
    volume_sats: int
    timestamp: float
    strength: float = 1.0    # Decays over time

    def to_dict(self) -> Dict[str, Any]:
        return {
            "depositor": self.depositor,
            "source_peer_id": self.source_peer_id,
            "destination_peer_id": self.destination_peer_id,
            "fee_ppm": self.fee_ppm,
            "success": self.success,
            "volume_sats": self.volume_sats,
            "timestamp": self.timestamp,
            "strength": round(self.strength, 3)
        }


@dataclass
class PeerWarning:
    """
    Warning about a threatening peer, broadcast to fleet.
    """
    peer_id: str
    threat_type: str         # "drain", "unreliable", "force_close"
    severity: float          # 0.0 to 1.0
    reporter: str            # Who reported the threat
    timestamp: float
    ttl: float               # Time-to-live in seconds
    evidence: Dict[str, Any] = field(default_factory=dict)

    def is_expired(self) -> bool:
        return time.time() > self.timestamp + self.ttl

    def to_dict(self) -> Dict[str, Any]:
        return {
            "peer_id": self.peer_id,
            "threat_type": self.threat_type,
            "severity": round(self.severity, 2),
            "reporter": self.reporter,
            "timestamp": self.timestamp,
            "ttl": self.ttl,
            "evidence": self.evidence,
            "is_expired": self.is_expired()
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
    stigmergic_influence: float = 0.0
    defensive_multiplier: float = 1.0

    # Confidence
    confidence: float = 0.5
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "peer_id": self.peer_id,
            "recommended_fee_ppm": self.recommended_fee_ppm,
            "is_primary": self.is_primary,
            "corridor_source": self.corridor_source,
            "corridor_destination": self.corridor_destination,
            "floor_applied": self.floor_applied,
            "ceiling_applied": self.ceiling_applied,
            "stigmergic_influence": round(self.stigmergic_influence, 2),
            "defensive_multiplier": round(self.defensive_multiplier, 2),
            "confidence": round(self.confidence, 2),
            "reason": self.reason
        }


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

        # Cache of assignments
        self._assignments: Dict[Tuple[str, str], CorridorAssignment] = {}
        self._assignments_timestamp: float = 0
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

        # Use internal competition detection to find overlapping routes
        competitions = self.liquidity_coordinator.detect_internal_competition()

        corridors = []
        for comp in competitions:
            corridor = FlowCorridor(
                source_peer_id=comp.get("source_peer_id", ""),
                destination_peer_id=comp.get("destination_peer_id", ""),
                source_alias=comp.get("source_alias"),
                destination_alias=comp.get("destination_alias"),
                capable_members=comp.get("competing_members", []),
                total_volume_sats=comp.get("total_fleet_capacity_sats", 0),
                competition_level=self._assess_competition_level(
                    len(comp.get("competing_members", []))
                )
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

        if (not force_refresh and
            self._assignments and
            now - self._assignments_timestamp < self._assignments_ttl):
            return list(self._assignments.values())

        # Refresh assignments
        corridors = self.identify_corridors()
        self._assignments = {}

        for corridor in corridors:
            assignment = self.assign_corridor(corridor)
            key = (corridor.source_peer_id, corridor.destination_peer_id)
            self._assignments[key] = assignment

        self._assignments_timestamp = now
        self._log(f"Refreshed {len(self._assignments)} corridor assignments")

        return list(self._assignments.values())

    def is_primary_for_corridor(
        self,
        member_id: str,
        source: str,
        destination: str
    ) -> bool:
        """Check if member is primary for a specific corridor."""
        key = (source, destination)
        assignment = self._assignments.get(key)
        if assignment:
            return assignment.primary_member == member_id
        return False

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
        key = (source, destination)
        assignment = self._assignments.get(key)

        if not assignment:
            return DEFAULT_FEE_PPM, False

        if assignment.primary_member == member_id:
            return assignment.primary_fee_ppm, True
        else:
            return assignment.secondary_fee_ppm, False


# =============================================================================
# ADAPTIVE FEE CONTROLLER (PHEROMONE-BASED)
# =============================================================================

class AdaptiveFeeController:
    """
    Fee adjustment inspired by ant colony pheromone dynamics.

    Pheromone = "memory" of what worked
    Evaporation = forgetting rate (adaptive based on environment)
    Deposit = reinforcement from success
    """

    def __init__(self, plugin: Any = None):
        self.plugin = plugin
        self.our_pubkey: Optional[str] = None

        # Pheromone levels per channel (fee memory)
        self._pheromone: Dict[str, float] = defaultdict(float)

        # Velocity cache for evaporation rate calculation
        self._velocity_cache: Dict[str, float] = {}
        self._velocity_cache_time: Dict[str, float] = {}

        # Network fee volatility tracking
        self._fee_observations: List[Tuple[float, int]] = []  # (timestamp, fee)

    def set_our_pubkey(self, pubkey: str) -> None:
        self.our_pubkey = pubkey

    def _log(self, msg: str, level: str = "info") -> None:
        if self.plugin:
            self.plugin.log(f"cl-hive: [AdaptiveFee] {msg}", level=level)

    def calculate_evaporation_rate(self, channel_id: str) -> float:
        """
        Dynamic evaporation based on environment stability.

        Stable environment: Low evaporation (exploit known good fees)
        Dynamic environment: High evaporation (explore new fee points)
        """
        # Get balance velocity (if available)
        velocity = self._velocity_cache.get(channel_id, 0.0)

        # Get network fee volatility
        fee_volatility = self._calculate_fee_volatility()

        # Base evaporation
        base = BASE_EVAPORATION_RATE

        # Velocity factor: faster drain = faster adaptation
        velocity_factor = min(0.4, abs(velocity) * 4)

        # Volatility factor: market moving = faster adaptation
        volatility_factor = min(0.3, fee_volatility / 200)

        evap_rate = base + velocity_factor + volatility_factor

        return max(MIN_EVAPORATION_RATE, min(MAX_EVAPORATION_RATE, evap_rate))

    def _calculate_fee_volatility(self) -> float:
        """Calculate recent fee volatility in the network."""
        if len(self._fee_observations) < 2:
            return 0.0

        # Filter to recent observations (last hour)
        now = time.time()
        recent = [f for t, f in self._fee_observations if now - t < 3600]

        if len(recent) < 2:
            return 0.0

        mean_fee = sum(recent) / len(recent)
        variance = sum((f - mean_fee) ** 2 for f in recent) / len(recent)

        return math.sqrt(variance)

    def update_velocity(self, channel_id: str, velocity_pct_per_hour: float) -> None:
        """Update cached velocity for a channel."""
        self._velocity_cache[channel_id] = velocity_pct_per_hour
        self._velocity_cache_time[channel_id] = time.time()

    def record_fee_observation(self, fee_ppm: int) -> None:
        """Record a network fee observation for volatility calculation."""
        self._fee_observations.append((time.time(), fee_ppm))

        # Keep only recent observations
        cutoff = time.time() - 3600
        self._fee_observations = [
            (t, f) for t, f in self._fee_observations if t > cutoff
        ]

    def update_pheromone(
        self,
        channel_id: str,
        current_fee: int,
        routing_success: bool,
        revenue_sats: int = 0
    ) -> None:
        """
        Update fee "pheromone" based on routing outcomes.

        Success → deposit pheromone (reinforce this fee)
        Failure → no deposit (let it evaporate)
        High revenue → stronger deposit
        """
        evap_rate = self.calculate_evaporation_rate(channel_id)

        # Evaporate existing pheromone
        self._pheromone[channel_id] *= (1 - evap_rate)

        if routing_success:
            # Deposit proportional to revenue
            deposit = revenue_sats * PHEROMONE_DEPOSIT_SCALE
            self._pheromone[channel_id] += deposit

            self._log(
                f"Channel {channel_id[:8]}: pheromone deposit {deposit:.2f}, "
                f"total now {self._pheromone[channel_id]:.2f}",
                level="debug"
            )

    def suggest_fee(
        self,
        channel_id: str,
        current_fee: int,
        local_balance_pct: float
    ) -> Tuple[int, str]:
        """
        Suggest fee based on pheromone trails.

        Returns (suggested_fee, reason)
        """
        pheromone = self._pheromone.get(channel_id, 0)

        if pheromone > PHEROMONE_EXPLOIT_THRESHOLD:
            # Strong signal - exploit current fee
            return current_fee, "exploit_strong_pheromone"
        else:
            # Weak signal - explore
            if local_balance_pct < 0.3:
                # Depleting - raise fees to slow outflow
                new_fee = int(current_fee * 1.15)
                return new_fee, "explore_raise_depleting"
            elif local_balance_pct > 0.7:
                # Saturating - lower fees to attract flow
                new_fee = int(current_fee * 0.85)
                return new_fee, "explore_lower_saturating"
            else:
                # Balanced - small exploration
                return current_fee, "exploit_balanced"

    def get_pheromone_level(self, channel_id: str) -> float:
        """Get current pheromone level for a channel."""
        return self._pheromone.get(channel_id, 0.0)

    def get_all_pheromone_levels(self) -> Dict[str, float]:
        """Get all pheromone levels."""
        return dict(self._pheromone)


# =============================================================================
# STIGMERGIC FEE COORDINATION
# =============================================================================

class StigmergicCoordinator:
    """
    Fleet members coordinate fees by observing each other's
    routing outcomes, not through direct messaging.

    The "environment" is the shared routing intelligence map.
    """

    def __init__(self, database: Any, plugin: Any, state_manager: Any = None):
        self.database = database
        self.plugin = plugin
        self.state_manager = state_manager
        self.our_pubkey: Optional[str] = None

        # Route markers (in-memory, also persisted via gossip)
        self._markers: Dict[Tuple[str, str], List[RouteMarker]] = defaultdict(list)

    def set_our_pubkey(self, pubkey: str) -> None:
        self.our_pubkey = pubkey

    def _log(self, msg: str, level: str = "info") -> None:
        if self.plugin:
            self.plugin.log(f"cl-hive: [Stigmergy] {msg}", level=level)

    def deposit_marker(
        self,
        source: str,
        destination: str,
        fee_charged: int,
        success: bool,
        volume_sats: int
    ) -> RouteMarker:
        """
        Leave a marker in shared routing map after routing attempt.

        Other fleet members will see this and adjust their fees
        for the same route accordingly.
        """
        marker = RouteMarker(
            depositor=self.our_pubkey or "",
            source_peer_id=source,
            destination_peer_id=destination,
            fee_ppm=fee_charged,
            success=success,
            volume_sats=volume_sats,
            timestamp=time.time(),
            strength=volume_sats / 100_000  # Larger payments = stronger signal
        )

        key = (source, destination)
        self._markers[key].append(marker)

        # Prune old markers
        self._prune_markers(key)

        self._log(
            f"Deposited marker: {source[:8]}->{destination[:8]} "
            f"fee={fee_charged} success={success} strength={marker.strength:.2f}",
            level="debug"
        )

        return marker

    def _prune_markers(self, key: Tuple[str, str]) -> None:
        """Remove expired markers."""
        now = time.time()
        self._markers[key] = [
            m for m in self._markers[key]
            if self._calculate_marker_strength(m, now) > MARKER_MIN_STRENGTH
        ]

    def _calculate_marker_strength(self, marker: RouteMarker, now: float) -> float:
        """Calculate current strength of a marker (decays over time)."""
        age_hours = (now - marker.timestamp) / 3600
        decay = math.exp(-age_hours * math.log(2) / MARKER_HALF_LIFE_HOURS)
        return marker.strength * decay

    def read_markers(self, source: str, destination: str) -> List[RouteMarker]:
        """
        Read markers left by other fleet members for this route.
        """
        key = (source, destination)
        markers = self._markers.get(key, [])

        now = time.time()
        result = []

        for m in markers:
            # Update strength based on decay
            current_strength = self._calculate_marker_strength(m, now)
            if current_strength > MARKER_MIN_STRENGTH:
                m.strength = current_strength
                result.append(m)

        return result

    def calculate_coordinated_fee(
        self,
        source: str,
        destination: str,
        default_fee: int
    ) -> Tuple[int, float]:
        """
        Set fee based on stigmergic signals from fleet.

        Returns (recommended_fee, confidence)
        """
        markers = self.read_markers(source, destination)

        if not markers:
            return default_fee, 0.3  # No signals, low confidence

        # Separate successful and failed markers
        successful = [m for m in markers if m.success]
        failed = [m for m in markers if not m.success]

        if successful:
            # Find strongest successful marker
            best = max(successful, key=lambda m: m.strength)

            # Don't undercut successful fleet member
            recommended = max(FLEET_FEE_FLOOR_PPM, best.fee_ppm)
            confidence = min(0.9, 0.5 + best.strength * 0.1)

            return recommended, confidence

        if failed:
            # All failures - try lower or avoid
            avg_failed_fee = sum(m.fee_ppm for m in failed) / len(failed)
            recommended = max(FLEET_FEE_FLOOR_PPM, int(avg_failed_fee * 0.8))
            confidence = 0.4

            return recommended, confidence

        return default_fee, 0.3

    def receive_marker_from_gossip(self, marker_data: Dict) -> Optional[RouteMarker]:
        """Process a marker received from fleet gossip."""
        try:
            marker = RouteMarker(
                depositor=marker_data["depositor"],
                source_peer_id=marker_data["source_peer_id"],
                destination_peer_id=marker_data["destination_peer_id"],
                fee_ppm=marker_data["fee_ppm"],
                success=marker_data["success"],
                volume_sats=marker_data["volume_sats"],
                timestamp=marker_data["timestamp"],
                strength=marker_data.get("strength", 1.0)
            )

            key = (marker.source_peer_id, marker.destination_peer_id)
            self._markers[key].append(marker)
            self._prune_markers(key)

            return marker
        except (KeyError, TypeError) as e:
            self._log(f"Invalid marker data: {e}", level="debug")
            return None

    def get_all_markers(self) -> List[RouteMarker]:
        """Get all active markers."""
        result = []
        now = time.time()

        for markers in self._markers.values():
            for m in markers:
                current_strength = self._calculate_marker_strength(m, now)
                if current_strength > MARKER_MIN_STRENGTH:
                    m.strength = current_strength
                    result.append(m)

        return result


# =============================================================================
# MYCELIUM DEFENSE SYSTEM
# =============================================================================

class MyceliumDefenseSystem:
    """
    Fleet-wide defense against draining/malicious peers.

    When one member detects a threat, all members respond.
    Like chemical signals through mycelium network.
    """

    def __init__(self, database: Any, plugin: Any, gossip_mgr: Any = None):
        self.database = database
        self.plugin = plugin
        self.gossip_mgr = gossip_mgr
        self.our_pubkey: Optional[str] = None

        # Active warnings
        self._warnings: Dict[str, PeerWarning] = {}

        # Temporary defensive fees
        self._defensive_fees: Dict[str, Dict] = {}

        # Peer statistics cache
        self._peer_stats: Dict[str, Dict] = {}

    def set_our_pubkey(self, pubkey: str) -> None:
        self.our_pubkey = pubkey

    def _log(self, msg: str, level: str = "info") -> None:
        if self.plugin:
            self.plugin.log(f"cl-hive: [MyceliumDefense] {msg}", level=level)

    def update_peer_stats(
        self,
        peer_id: str,
        inflow_sats: int,
        outflow_sats: int,
        successful_forwards: int,
        failed_forwards: int
    ) -> None:
        """Update statistics for a peer."""
        self._peer_stats[peer_id] = {
            "inflow": inflow_sats,
            "outflow": outflow_sats,
            "successful": successful_forwards,
            "failed": failed_forwards,
            "updated_at": time.time()
        }

    def detect_threat(self, peer_id: str) -> Optional[PeerWarning]:
        """
        Detect peers that are draining us or behaving badly.
        """
        stats = self._peer_stats.get(peer_id)
        if not stats:
            return None

        # Calculate threat indicators
        inflow = max(stats.get("inflow", 0), 1)
        outflow = stats.get("outflow", 0)
        drain_rate = outflow / inflow

        successful = stats.get("successful", 0)
        failed = stats.get("failed", 0)
        total = successful + failed
        failure_rate = failed / total if total > 0 else 0

        # Check for drain attack
        if drain_rate > DRAIN_RATIO_THRESHOLD:
            return PeerWarning(
                peer_id=peer_id,
                threat_type="drain",
                severity=min(1.0, drain_rate / 10),
                reporter=self.our_pubkey or "",
                timestamp=time.time(),
                ttl=WARNING_TTL_HOURS * 3600,
                evidence={"drain_rate": round(drain_rate, 2)}
            )

        # Check for unreliable peer
        if failure_rate > FAILURE_RATE_THRESHOLD:
            return PeerWarning(
                peer_id=peer_id,
                threat_type="unreliable",
                severity=failure_rate,
                reporter=self.our_pubkey or "",
                timestamp=time.time(),
                ttl=WARNING_TTL_HOURS * 3600,
                evidence={"failure_rate": round(failure_rate, 2)}
            )

        return None

    def broadcast_warning(self, warning: PeerWarning) -> bool:
        """
        Send warning to fleet (like chemical signal through mycelium).
        """
        # Store locally
        self._warnings[warning.peer_id] = warning

        # Broadcast via gossip if available
        if self.gossip_mgr:
            try:
                # This would integrate with existing gossip infrastructure
                self._log(
                    f"Broadcasting warning for {warning.peer_id[:12]}: "
                    f"{warning.threat_type} (severity={warning.severity:.2f})"
                )
                return True
            except Exception as e:
                self._log(f"Failed to broadcast warning: {e}", level="error")
                return False

        return True

    def handle_warning(self, warning: PeerWarning) -> Optional[Dict]:
        """
        Respond to warning from another fleet member.

        Returns defensive fee adjustment if applicable.
        """
        # Store warning
        self._warnings[warning.peer_id] = warning

        # Calculate defensive fee increase
        multiplier = 1 + (warning.severity * (DEFENSIVE_FEE_MAX_MULTIPLIER - 1))

        self._defensive_fees[warning.peer_id] = {
            "multiplier": multiplier,
            "expires_at": warning.timestamp + warning.ttl,
            "threat_type": warning.threat_type,
            "reporter": warning.reporter
        }

        self._log(
            f"Defensive fee multiplier {multiplier:.2f}x applied to "
            f"{warning.peer_id[:12]} (warning from {warning.reporter[:12]})"
        )

        return {
            "peer_id": warning.peer_id,
            "multiplier": multiplier,
            "expires_at": warning.timestamp + warning.ttl
        }

    def get_defensive_multiplier(self, peer_id: str) -> float:
        """Get current defensive fee multiplier for a peer."""
        defense = self._defensive_fees.get(peer_id)
        if not defense:
            return 1.0

        # Check if expired
        if time.time() > defense["expires_at"]:
            del self._defensive_fees[peer_id]
            return 1.0

        return defense["multiplier"]

    def check_warning_expiration(self) -> List[str]:
        """
        Check and clean up expired warnings.

        Returns list of peer_ids whose warnings expired.
        """
        now = time.time()
        expired = []

        for peer_id, warning in list(self._warnings.items()):
            if warning.is_expired():
                del self._warnings[peer_id]
                expired.append(peer_id)

        for peer_id in list(self._defensive_fees.keys()):
            if now > self._defensive_fees[peer_id]["expires_at"]:
                del self._defensive_fees[peer_id]
                if peer_id not in expired:
                    expired.append(peer_id)

        if expired:
            self._log(f"Expired warnings for {len(expired)} peers")

        return expired

    def get_active_warnings(self) -> List[PeerWarning]:
        """Get all active (non-expired) warnings."""
        return [w for w in self._warnings.values() if not w.is_expired()]

    def get_defense_status(self) -> Dict:
        """Get current defense system status."""
        self.check_warning_expiration()

        return {
            "active_warnings": len(self._warnings),
            "defensive_fees_active": len(self._defensive_fees),
            "warnings": [w.to_dict() for w in self._warnings.values()],
            "defensive_peers": list(self._defensive_fees.keys())
        }


# =============================================================================
# FEE COORDINATION MANAGER (Main Interface)
# =============================================================================

class FeeCoordinationManager:
    """
    Main interface for Phase 2 fee coordination.

    Integrates:
    - Flow corridor assignment
    - Adaptive fee controller
    - Stigmergic coordination
    - Mycelium defense
    """

    def __init__(
        self,
        database: Any,
        plugin: Any,
        state_manager: Any = None,
        liquidity_coordinator: Any = None,
        gossip_mgr: Any = None
    ):
        self.database = database
        self.plugin = plugin
        self.our_pubkey: Optional[str] = None

        # Initialize components
        self.corridor_mgr = FlowCorridorManager(
            database, plugin, state_manager, liquidity_coordinator
        )
        self.adaptive_controller = AdaptiveFeeController(plugin)
        self.stigmergic_coord = StigmergicCoordinator(
            database, plugin, state_manager
        )
        self.defense_system = MyceliumDefenseSystem(
            database, plugin, gossip_mgr
        )

    def set_our_pubkey(self, pubkey: str) -> None:
        self.our_pubkey = pubkey
        self.corridor_mgr.set_our_pubkey(pubkey)
        self.adaptive_controller.set_our_pubkey(pubkey)
        self.stigmergic_coord.set_our_pubkey(pubkey)
        self.defense_system.set_our_pubkey(pubkey)

    def _log(self, msg: str, level: str = "info") -> None:
        if self.plugin:
            self.plugin.log(f"cl-hive: [FeeCoord] {msg}", level=level)

    def get_fee_recommendation(
        self,
        channel_id: str,
        peer_id: str,
        current_fee: int,
        local_balance_pct: float,
        source_hint: str = None,
        destination_hint: str = None
    ) -> FeeRecommendation:
        """
        Get coordinated fee recommendation for a channel.

        Combines all coordination signals:
        1. Corridor assignment (primary vs secondary)
        2. Pheromone-based adaptive suggestion
        3. Stigmergic markers from fleet
        4. Defensive adjustments
        """
        # Start with current fee
        recommended_fee = current_fee
        is_primary = False
        floor_applied = False
        ceiling_applied = False
        stigmergic_influence = 0.0
        defensive_multiplier = 1.0
        reasons = []

        # 1. Check corridor assignment
        if source_hint and destination_hint:
            corridor_fee, is_primary = self.corridor_mgr.get_fee_for_member(
                self.our_pubkey or "", source_hint, destination_hint
            )
            if corridor_fee != DEFAULT_FEE_PPM:
                recommended_fee = corridor_fee
                reasons.append(f"corridor_{'primary' if is_primary else 'secondary'}")

        # 2. Get adaptive controller suggestion
        adaptive_fee, adaptive_reason = self.adaptive_controller.suggest_fee(
            channel_id, recommended_fee, local_balance_pct
        )
        if adaptive_fee != recommended_fee:
            recommended_fee = adaptive_fee
            reasons.append(adaptive_reason)

        # 3. Check stigmergic markers
        if source_hint and destination_hint:
            stig_fee, stig_confidence = self.stigmergic_coord.calculate_coordinated_fee(
                source_hint, destination_hint, recommended_fee
            )
            if stig_confidence > 0.5 and stig_fee != recommended_fee:
                # Blend stigmergic signal with current recommendation
                blend_weight = stig_confidence * 0.5
                recommended_fee = int(
                    recommended_fee * (1 - blend_weight) +
                    stig_fee * blend_weight
                )
                stigmergic_influence = stig_confidence
                reasons.append(f"stigmergic_{stig_confidence:.2f}")

        # 4. Apply defensive multiplier
        defensive_multiplier = self.defense_system.get_defensive_multiplier(peer_id)
        if defensive_multiplier > 1.0:
            recommended_fee = int(recommended_fee * defensive_multiplier)
            reasons.append(f"defensive_{defensive_multiplier:.2f}x")

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
        if stigmergic_influence > 0:
            confidence += stigmergic_influence * 0.2
        confidence = min(0.95, confidence)

        return FeeRecommendation(
            channel_id=channel_id,
            peer_id=peer_id,
            recommended_fee_ppm=recommended_fee,
            is_primary=is_primary,
            corridor_source=source_hint,
            corridor_destination=destination_hint,
            floor_applied=floor_applied,
            ceiling_applied=ceiling_applied,
            stigmergic_influence=stigmergic_influence,
            defensive_multiplier=defensive_multiplier,
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
        source: str = None,
        destination: str = None
    ) -> None:
        """
        Record a routing outcome to update all coordination systems.
        """
        # Update pheromone
        self.adaptive_controller.update_pheromone(
            channel_id, fee_ppm, success, revenue_sats
        )

        # Record fee observation
        self.adaptive_controller.record_fee_observation(fee_ppm)

        # Deposit stigmergic marker
        if source and destination:
            self.stigmergic_coord.deposit_marker(
                source, destination, fee_ppm, success, revenue_sats if success else 0
            )

    def get_coordination_status(self) -> Dict:
        """Get overall fee coordination status."""
        assignments = self.corridor_mgr.get_assignments()
        markers = self.stigmergic_coord.get_all_markers()
        defense_status = self.defense_system.get_defense_status()
        pheromone_levels = self.adaptive_controller.get_all_pheromone_levels()

        return {
            "corridor_assignments": len(assignments),
            "active_markers": len(markers),
            "defense_status": defense_status,
            "pheromone_channels": len(pheromone_levels),
            "fleet_fee_floor": FLEET_FEE_FLOOR_PPM,
            "fleet_fee_ceiling": FLEET_FEE_CEILING_PPM,
            "assignments": [a.to_dict() for a in assignments[:10]],  # Limit output
            "recent_markers": [m.to_dict() for m in markers[:10]]
        }
