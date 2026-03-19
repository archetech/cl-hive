"""
Strategic Positioning Module for Fleet Yield Optimization.

Positions the fleet on critical network paths to maximize routing opportunities:

1. RouteValueAnalyzer: Identify high-value corridors with volume and limited competition
2. FleetPositioningStrategy: Coordinate channel opens without duplication

The goal is strategic capital deployment - position on high-value routes where
the fleet can capture significant routing fees.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from . import network_metrics

# =============================================================================
# CONSTANTS
# =============================================================================

# Route value thresholds
HIGH_VALUE_VOLUME_SATS_DAILY = 10_000_000   # 10M sats/day = high value
MEDIUM_VALUE_VOLUME_SATS_DAILY = 1_000_000  # 1M sats/day = medium value
LOW_COMPETITION_THRESHOLD = 5               # <5 competitors = low competition
MEDIUM_COMPETITION_THRESHOLD = 15           # <15 competitors = medium

# Positioning priorities
EXCHANGE_PRIORITY_BONUS = 1.5               # 50% bonus for exchange channels
UNDERSERVED_PRIORITY_BONUS = 1.2            # 20% bonus for underserved targets

# Centrality-aware targeting (Use Case 4)
# Hive centrality = (hive_peer_connections) / (fleet_size - 1)
# Range: 0.0 (isolated) to 1.0 (fully connected to all hive members)
CENTRALITY_IMPROVEMENT_BONUS = 1.25         # 25% bonus for centrality improvements
LOW_CENTRALITY_MEMBER_BONUS = 1.15          # 15% bonus when member has low centrality
LOW_CENTRALITY_THRESHOLD = 0.3              # Members below 30% connectivity are "low centrality"
MIN_CENTRALITY_IMPROVEMENT = 0.05           # Minimum improvement (+5%) to apply bonus

# Fleet coordination
MAX_MEMBERS_PER_TARGET = 2                  # Max 2 members per target (healthy redundancy)
POSITION_RECOMMENDATION_COOLDOWN_HOURS = 24

# Known high-value exchanges (pubkey prefixes or aliases)
PRIORITY_EXCHANGES = {
    "ACINQ": {"alias_patterns": ["ACINQ", "acinq"], "priority": 1.0},
    "Kraken": {"alias_patterns": ["Kraken", "kraken"], "priority": 0.95},
    "Bitfinex": {"alias_patterns": ["Bitfinex", "bitfinex", "bfx"], "priority": 0.9},
    "River": {"alias_patterns": ["River", "river"], "priority": 0.85},
    "CashApp": {"alias_patterns": ["Cash App", "CashApp", "Block"], "priority": 0.85},
    "Strike": {"alias_patterns": ["Strike", "strike"], "priority": 0.85},
    "Coinbase": {"alias_patterns": ["Coinbase", "coinbase"], "priority": 0.8},
    "WalletOfSatoshi": {"alias_patterns": ["WoS", "Wallet of Satoshi"], "priority": 0.75},
    "Muun": {"alias_patterns": ["Muun", "muun"], "priority": 0.7},
    "Breez": {"alias_patterns": ["Breez", "breez"], "priority": 0.7},
}


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class CorridorValue:
    """
    Value assessment for a routing corridor.
    """
    source_peer_id: str
    destination_peer_id: str
    source_alias: Optional[str] = None
    destination_alias: Optional[str] = None

    # Volume metrics
    daily_volume_sats: int = 0
    monthly_volume_sats: int = 0

    # Competition metrics
    competitor_count: int = 0
    fleet_members_present: int = 0

    # Value score
    value_score: float = 0.0
    margin_estimate_ppm: int = 0

    # Classification
    value_tier: str = "unknown"  # "high", "medium", "low"
    competition_level: str = "unknown"  # "low", "medium", "high"

    # Accessibility
    accessible: bool = True
    accessibility_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_peer_id": self.source_peer_id,
            "destination_peer_id": self.destination_peer_id,
            "source_alias": self.source_alias,
            "destination_alias": self.destination_alias,
            "daily_volume_sats": self.daily_volume_sats,
            "monthly_volume_sats": self.monthly_volume_sats,
            "competitor_count": self.competitor_count,
            "fleet_members_present": self.fleet_members_present,
            "value_score": round(self.value_score, 3),
            "margin_estimate_ppm": self.margin_estimate_ppm,
            "value_tier": self.value_tier,
            "competition_level": self.competition_level,
            "accessible": self.accessible,
            "accessibility_reason": self.accessibility_reason
        }


@dataclass
class PositionRecommendation:
    """
    Recommendation to open a channel for strategic positioning.
    """
    target_peer_id: str
    target_alias: Optional[str] = None

    # Recommended member to open
    recommended_member: Optional[str] = None
    recommended_member_alias: Optional[str] = None

    # Channel parameters
    recommended_capacity_sats: int = 0
    max_fee_rate_ppm: int = 0

    # Reasoning
    reason: str = ""
    priority_score: float = 0.0
    priority_tier: str = "low"  # "critical", "high", "medium", "low"

    # Value sources
    is_exchange: bool = False
    is_bridge_node: bool = False
    is_underserved: bool = False
    corridor_value: Optional[float] = None

    # Current state
    current_fleet_channels: int = 0

    # Centrality impact (Use Case 4)
    member_current_centrality: float = 0.0
    estimated_centrality_improvement: float = 0.0
    improves_network_position: bool = False

    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "target_peer_id": self.target_peer_id,
            "target_alias": self.target_alias,
            "recommended_member": self.recommended_member,
            "recommended_member_alias": self.recommended_member_alias,
            "recommended_capacity_sats": self.recommended_capacity_sats,
            "max_fee_rate_ppm": self.max_fee_rate_ppm,
            "reason": self.reason,
            "priority_score": round(self.priority_score, 3),
            "priority_tier": self.priority_tier,
            "is_exchange": self.is_exchange,
            "is_bridge_node": self.is_bridge_node,
            "is_underserved": self.is_underserved,
            "corridor_value": round(self.corridor_value, 3) if self.corridor_value else None,
            "current_fleet_channels": self.current_fleet_channels,
            "timestamp": self.timestamp
        }
        # Include centrality info if there's a network position improvement
        if self.improves_network_position:
            result["member_current_centrality"] = round(self.member_current_centrality, 3)
            result["estimated_centrality_improvement"] = round(self.estimated_centrality_improvement, 3)
            result["improves_network_position"] = True
        return result


@dataclass
class PositioningSummary:
    """
    Summary of fleet strategic positioning.
    """
    total_targets_analyzed: int = 0
    high_value_corridors: int = 0
    exchange_coverage_pct: float = 0.0

    # Recommendations
    open_recommendations: int = 0

    # Fleet coverage
    underserved_targets: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_targets_analyzed": self.total_targets_analyzed,
            "high_value_corridors": self.high_value_corridors,
            "exchange_coverage_pct": round(self.exchange_coverage_pct, 1),
            "open_recommendations": self.open_recommendations,
            "underserved_targets": self.underserved_targets,
        }


# =============================================================================
# ROUTE VALUE ANALYZER
# =============================================================================

class RouteValueAnalyzer:
    """
    Identify routes with high volume and limited competition.

    Value = f(volume, margin, accessibility)
    """

    def __init__(self, plugin, state_manager=None, fee_coordination_mgr=None):
        """
        Initialize the route value analyzer.

        Args:
            plugin: Plugin reference for RPC calls
            state_manager: StateManager for fleet topology
            fee_coordination_mgr: FeeCoordinationManager for corridor data
        """
        self.plugin = plugin
        self.state_manager = state_manager
        self.fee_coordination_mgr = fee_coordination_mgr
        self._our_pubkey: Optional[str] = None

        # Cache for corridor values
        self._corridor_cache: Dict[Tuple[str, str], CorridorValue] = {}
        self._cache_time: float = 0
        self._cache_ttl: float = 3600  # 1 hour

    def set_our_pubkey(self, pubkey: str) -> None:
        """Set our node's pubkey."""
        self._our_pubkey = pubkey

    def _log(self, message: str, level: str = "debug") -> None:
        """Log a message if plugin is available."""
        if self.plugin:
            self.plugin.log(f"ROUTE_VALUE: {message}", level=level)

    def _get_corridor_data(self) -> List[Any]:
        """Get corridor assignment data from fee coordination."""
        if not self.fee_coordination_mgr:
            return []

        try:
            corridor_mgr = self.fee_coordination_mgr.corridor_manager
            if corridor_mgr:
                return corridor_mgr.get_all_assignments()
            return []
        except Exception as e:
            self._log(f"Error getting corridor data: {e}", level="debug")
            return []

    def _get_fleet_topology(self) -> Dict[str, Set[str]]:
        """Get fleet member topology (who has channels to whom)."""
        if not self.state_manager:
            return {}

        topology = {}
        try:
            all_states = self.state_manager.get_all_peer_states()
            for state in all_states:
                member_id = state.peer_id
                peers = set(getattr(state, 'topology', []) or [])
                topology[member_id] = peers
        except Exception as e:
            self._log(f"Error getting fleet topology: {e}", level="debug")

        return topology

    def _estimate_competitor_count(self, target_peer_id: str) -> int:
        """
        Estimate number of competitors for routing to a target.

        This is a rough estimate based on known network data.
        """
        # In a real implementation, this would query network gossip
        # For now, return a conservative estimate
        return 10

    def _is_exchange(self, alias: str) -> Tuple[bool, float]:
        """
        Check if a node is a known exchange.

        Returns (is_exchange, priority_score)
        """
        if not alias:
            return False, 0.0

        alias_lower = alias.lower()
        for exchange, data in PRIORITY_EXCHANGES.items():
            for pattern in data["alias_patterns"]:
                if pattern.lower() in alias_lower:
                    return True, data["priority"]

        return False, 0.0

    def analyze_corridor(
        self,
        source_peer_id: str,
        destination_peer_id: str,
        volume_sats: int = 0,
        source_alias: str = None,
        destination_alias: str = None
    ) -> CorridorValue:
        """
        Analyze a single corridor's value.

        Args:
            source_peer_id: Source of payments
            destination_peer_id: Destination of payments
            volume_sats: Known volume (monthly)
            source_alias: Source node alias
            destination_alias: Destination node alias

        Returns:
            CorridorValue with full analysis
        """
        corridor = CorridorValue(
            source_peer_id=source_peer_id,
            destination_peer_id=destination_peer_id,
            source_alias=source_alias,
            destination_alias=destination_alias
        )

        # Set volume
        corridor.monthly_volume_sats = volume_sats
        corridor.daily_volume_sats = volume_sats // 30

        # Classify volume tier
        if corridor.daily_volume_sats >= HIGH_VALUE_VOLUME_SATS_DAILY:
            corridor.value_tier = "high"
        elif corridor.daily_volume_sats >= MEDIUM_VALUE_VOLUME_SATS_DAILY:
            corridor.value_tier = "medium"
        else:
            corridor.value_tier = "low"

        # Estimate competition
        corridor.competitor_count = self._estimate_competitor_count(destination_peer_id)

        if corridor.competitor_count < LOW_COMPETITION_THRESHOLD:
            corridor.competition_level = "low"
        elif corridor.competitor_count < MEDIUM_COMPETITION_THRESHOLD:
            corridor.competition_level = "medium"
        else:
            corridor.competition_level = "high"

        # Count fleet members present
        topology = self._get_fleet_topology()
        corridor.fleet_members_present = sum(
            1 for peers in topology.values()
            if destination_peer_id in peers
        )

        # Estimate margin (higher with less competition)
        base_margin = 500  # Base 500 ppm
        competition_factor = max(0.2, 1.0 - (corridor.competitor_count * 0.05))
        corridor.margin_estimate_ppm = int(base_margin * competition_factor)

        # Calculate value score
        # Score = Volume * Margin * (1 / Competition)
        volume_factor = min(1.0, corridor.daily_volume_sats / HIGH_VALUE_VOLUME_SATS_DAILY)
        margin_factor = corridor.margin_estimate_ppm / 1000
        competition_penalty = 1.0 / max(1, corridor.competitor_count ** 0.5)

        corridor.value_score = volume_factor * margin_factor * competition_penalty

        # Check accessibility (can we get a channel?)
        # For now, always accessible
        corridor.accessible = True

        return corridor

    def find_valuable_corridors(self, min_score: float = 0.1) -> List[CorridorValue]:
        """
        Find corridors with high value and limited competition.

        Args:
            min_score: Minimum value score to include

        Returns:
            List of CorridorValue sorted by value score
        """
        corridors = []

        # Get corridor data from fee coordination
        assignments = self._get_corridor_data()

        for assignment in assignments:
            try:
                corridor_data = assignment.corridor if hasattr(assignment, 'corridor') else assignment
                corridor = self.analyze_corridor(
                    source_peer_id=corridor_data.source_peer_id,
                    destination_peer_id=corridor_data.destination_peer_id,
                    volume_sats=corridor_data.total_volume_sats,
                    source_alias=corridor_data.source_alias,
                    destination_alias=corridor_data.destination_alias
                )

                if corridor.value_score >= min_score:
                    corridors.append(corridor)

            except Exception as e:
                self._log(f"Error analyzing corridor: {e}", level="debug")

        # Sort by value score
        corridors.sort(key=lambda c: c.value_score, reverse=True)

        return corridors

    def find_exchange_targets(self) -> List[Dict[str, Any]]:
        """
        Find exchanges that the fleet should connect to.

        Returns:
            List of exchange targets with connection status
        """
        targets = []
        topology = self._get_fleet_topology()

        # Collect all known peer aliases
        # In a real implementation, this would query listchannels
        known_aliases = {}

        for exchange_name, data in PRIORITY_EXCHANGES.items():
            # Check if any fleet member has this exchange
            has_connection = False
            connected_members = []

            for member_id, peers in topology.items():
                for peer_id in peers:
                    alias = known_aliases.get(peer_id, "")
                    is_exchange, _ = self._is_exchange(alias)
                    if is_exchange:
                        # Check if this specific exchange
                        for pattern in data["alias_patterns"]:
                            if pattern.lower() in alias.lower():
                                has_connection = True
                                connected_members.append(member_id)
                                break

            targets.append({
                "exchange": exchange_name,
                "priority": data["priority"],
                "has_connection": has_connection,
                "connected_members": connected_members,
                "needs_channel": not has_connection
            })

        # Sort by priority (uncovered first)
        targets.sort(key=lambda t: (not t["needs_channel"], -t["priority"]))

        return targets


# =============================================================================
# FLEET POSITIONING STRATEGY
# =============================================================================

class FleetPositioningStrategy:
    """
    Coordinate channel opens to maximize fleet coverage.

    Principles:
    1. Don't duplicate - one member per target (max 2 for redundancy)
    2. Complementary positions - cover different regions
    3. Bridge priority - control chokepoints
    """

    def __init__(
        self,
        plugin,
        state_manager=None,
        route_analyzer: RouteValueAnalyzer = None,
        planner=None
    ):
        """
        Initialize the fleet positioning strategy.

        Args:
            plugin: Plugin reference
            state_manager: StateManager for fleet state
            route_analyzer: RouteValueAnalyzer for value assessment
            planner: Planner for underserved targets
        """
        self.plugin = plugin
        self.state_manager = state_manager
        self.route_analyzer = route_analyzer
        self.planner = planner
        self._our_pubkey: Optional[str] = None

        # Track recent recommendations
        self._recent_recommendations: Dict[str, float] = {}

    def set_our_pubkey(self, pubkey: str) -> None:
        """Set our node's pubkey."""
        self._our_pubkey = pubkey
        if self.route_analyzer:
            self.route_analyzer.set_our_pubkey(pubkey)

    def _log(self, message: str, level: str = "debug") -> None:
        """Log a message if plugin is available."""
        if self.plugin:
            self.plugin.log(f"POSITIONING: {message}", level=level)

    def _get_fleet_members(self) -> List[str]:
        """Get list of fleet member pubkeys."""
        if not self.state_manager:
            return []

        try:
            all_states = self.state_manager.get_all_peer_states()
            return [s.peer_id for s in all_states]
        except Exception:
            return []

    def _count_fleet_channels_to_target(self, target_peer_id: str) -> int:
        """Count how many fleet members have channels to a target."""
        if not self.state_manager:
            return 0

        count = 0
        try:
            all_states = self.state_manager.get_all_peer_states()
            for state in all_states:
                topology = set(getattr(state, 'topology', []) or [])
                if target_peer_id in topology:
                    count += 1
        except Exception:
            pass

        return count

    def _get_member_centrality(self, member_id: str) -> float:
        """
        Get hive centrality for a member.

        Hive centrality is a connectivity ratio measuring internal fleet connectivity:
            hive_centrality = (hive_peer_connections) / (fleet_size - 1)

        Values:
            0.0 = No direct connections to other hive members
            0.5 = Connected to half the fleet
            1.0 = Connected to all other hive members (fully meshed)

        This is NOT betweenness/closeness/eigenvector centrality from graph theory.
        It's a simpler metric focused on direct hive connectivity for:
            - Identifying poorly-connected members needing more hive channels
            - Selecting rebalance hubs (high centrality = good intermediary)
            - Prioritizing expansion to improve fleet mesh density

        Args:
            member_id: Node public key

        Returns:
            Hive centrality score (0.0 to 1.0), default 0.5 if unknown
        """
        calculator = network_metrics.get_calculator()
        if not calculator:
            return 0.5  # Default to middle value

        metrics = calculator.get_member_metrics(member_id)
        if not metrics:
            return 0.5

        return metrics.hive_centrality

    def _estimate_centrality_improvement(
        self,
        member_id: str,
        target_peer_id: str
    ) -> float:
        """
        Estimate how much opening a channel to target would improve member's centrality.

        For hive targets (internal channels):
            - First hive connection: +0.30 (major improvement)
            - Second hive connection: +0.15 (significant)
            - Additional connections: diminishing returns (0.1 / current_count)

        For external targets:
            - Minimal improvement (+0.02) since they don't affect hive_centrality
            - External channels improve bridge_score instead (separate metric)

        Args:
            member_id: Node public key of the member opening channel
            target_peer_id: Node public key of potential channel target

        Returns:
            Estimated centrality improvement (0.0 to 0.3)
        """
        calculator = network_metrics.get_calculator()
        if not calculator:
            return 0.0

        # Get current member metrics
        member_metrics = calculator.get_member_metrics(member_id)
        if not member_metrics:
            return 0.0

        current_centrality = member_metrics.hive_centrality
        hive_peer_count = member_metrics.hive_peer_count

        # Check if target is a hive member (internal channel)
        topology = calculator._get_topology_snapshot()
        if not topology:
            return 0.0

        is_hive_target = target_peer_id in topology.member_topologies

        if is_hive_target:
            # Opening to hive member improves internal connectivity
            # Improvement is inversely proportional to current hive connections
            if hive_peer_count == 0:
                # First hive connection is a big improvement
                return 0.3
            elif hive_peer_count == 1:
                # Second hive connection still significant
                return 0.15
            else:
                # Diminishing returns
                return max(0.02, 0.1 / hive_peer_count)
        else:
            # Opening to external target - check if it improves bridge position
            # (bridge bonus if target is well-connected externally)
            # This is a heuristic - real bridge detection would need network graph
            return 0.02  # Minimal centrality boost for external targets

    def _select_best_member_for_target(self, target_peer_id: str) -> Optional[str]:
        """
        Select the best fleet member to open a channel to target.

        Criteria:
        - Doesn't already have a channel to target
        - Has available on-chain funds
        - Has capacity for another channel
        - Complements existing positions
        - Considers hive centrality (Use Case 4):
          - Members with lower centrality get priority for strategic targets
          - Targets that improve centrality get higher scores
        """
        members = self._get_fleet_members()
        if not members:
            return None

        candidates = []

        for member_id in members:
            if not self.state_manager:
                continue

            state = self.state_manager.get_peer_state(member_id)
            if not state:
                continue

            topology = set(getattr(state, 'topology', []) or [])

            # Skip if already has channel to target
            if target_peer_id in topology:
                continue

            # Score based on position complementarity
            # (member with fewer channels to similar targets is better)
            score = 1.0

            # Prefer members with fewer total channels (more focused)
            channel_count = len(topology)
            if channel_count < 20:
                score += 0.2
            elif channel_count > 50:
                score -= 0.2

            # Use Case 4: Centrality-aware scoring
            member_centrality = self._get_member_centrality(member_id)

            # Bonus for members with low centrality (they need connections more)
            if member_centrality < LOW_CENTRALITY_THRESHOLD:
                score *= LOW_CENTRALITY_MEMBER_BONUS
                self._log(
                    f"Member {member_id[:16]}... has low centrality ({member_centrality:.2f}), "
                    f"applying {LOW_CENTRALITY_MEMBER_BONUS}x bonus",
                    level="debug"
                )

            # Bonus if this target would significantly improve centrality
            centrality_improvement = self._estimate_centrality_improvement(member_id, target_peer_id)
            if centrality_improvement >= MIN_CENTRALITY_IMPROVEMENT:
                score *= CENTRALITY_IMPROVEMENT_BONUS
                self._log(
                    f"Target {target_peer_id[:16]}... would improve {member_id[:16]}...'s "
                    f"centrality by ~{centrality_improvement:.2f}",
                    level="debug"
                )

            candidates.append((member_id, score, member_centrality, centrality_improvement))

        if not candidates:
            return None

        # Return highest scoring candidate
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    def _select_best_member_with_metrics(
        self,
        target_peer_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Select best member and return selection metrics for recommendations.

        Returns:
            Dict with member_id, centrality, and improvement estimate
        """
        members = self._get_fleet_members()
        if not members:
            return None

        candidates = []

        for member_id in members:
            if not self.state_manager:
                continue

            state = self.state_manager.get_peer_state(member_id)
            if not state:
                continue

            topology = set(getattr(state, 'topology', []) or [])

            if target_peer_id in topology:
                continue

            score = 1.0
            channel_count = len(topology)
            if channel_count < 20:
                score += 0.2
            elif channel_count > 50:
                score -= 0.2

            member_centrality = self._get_member_centrality(member_id)
            centrality_improvement = self._estimate_centrality_improvement(member_id, target_peer_id)

            if member_centrality < LOW_CENTRALITY_THRESHOLD:
                score *= LOW_CENTRALITY_MEMBER_BONUS

            if centrality_improvement >= MIN_CENTRALITY_IMPROVEMENT:
                score *= CENTRALITY_IMPROVEMENT_BONUS

            candidates.append({
                "member_id": member_id,
                "score": score,
                "centrality": member_centrality,
                "centrality_improvement": centrality_improvement
            })

        if not candidates:
            return None

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[0]

    def recommend_next_open(
        self,
        member_id: Optional[str] = None
    ) -> Optional[PositionRecommendation]:
        """
        Recommend next channel open for optimal positioning.

        Args:
            member_id: Specific member to recommend for, or None for any

        Returns:
            PositionRecommendation or None
        """
        # Cleanup stale recommendation cooldown entries
        now = time.time()
        stale = [k for k, v in self._recent_recommendations.items()
                 if now - v > POSITION_RECOMMENDATION_COOLDOWN_HOURS * 3600]
        for k in stale:
            del self._recent_recommendations[k]

        # Check cooldown
        cooldown_key = member_id or "fleet"
        last_rec = self._recent_recommendations.get(cooldown_key, 0)
        if now - last_rec < POSITION_RECOMMENDATION_COOLDOWN_HOURS * 3600:
            return None

        # Get valuable corridors
        if self.route_analyzer:
            corridors = self.route_analyzer.find_valuable_corridors(min_score=0.05)
        else:
            corridors = []

        # Find best target
        best_target = None
        best_score = 0.0

        for corridor in corridors:
            target = corridor.destination_peer_id

            # Check fleet coverage
            fleet_channels = self._count_fleet_channels_to_target(target)
            if fleet_channels >= MAX_MEMBERS_PER_TARGET:
                continue  # Already covered

            # Calculate priority score
            priority = corridor.value_score

            # Apply bonuses
            is_exchange, exchange_priority = self.route_analyzer._is_exchange(
                corridor.destination_alias
            ) if self.route_analyzer else (False, 0)

            if is_exchange:
                priority *= EXCHANGE_PRIORITY_BONUS

            if fleet_channels == 0:
                priority *= UNDERSERVED_PRIORITY_BONUS

            if priority > best_score:
                best_score = priority
                best_target = corridor

        if not best_target:
            return None

        # Select member to open
        if member_id:
            recommended_member = member_id
        else:
            recommended_member = self._select_best_member_for_target(
                best_target.destination_peer_id
            )

        if not recommended_member:
            return None

        # Create recommendation
        is_exchange, _ = self.route_analyzer._is_exchange(
            best_target.destination_alias
        ) if self.route_analyzer else (False, 0)

        fleet_channels = self._count_fleet_channels_to_target(best_target.destination_peer_id)

        # Determine priority tier
        if best_score >= 0.5:
            priority_tier = "critical"
        elif best_score >= 0.3:
            priority_tier = "high"
        elif best_score >= 0.15:
            priority_tier = "medium"
        else:
            priority_tier = "low"

        rec = PositionRecommendation(
            target_peer_id=best_target.destination_peer_id,
            target_alias=best_target.destination_alias,
            recommended_member=recommended_member,
            recommended_capacity_sats=5_000_000,  # Default 5M sats
            max_fee_rate_ppm=1000,  # Max 1000 ppm opening fee
            reason=f"High-value corridor ({best_target.value_tier} volume, "
                   f"{best_target.competition_level} competition)",
            priority_score=best_score,
            priority_tier=priority_tier,
            is_exchange=is_exchange,
            is_bridge_node=False,  # Would require network analysis
            is_underserved=fleet_channels == 0,
            corridor_value=best_target.value_score,
            current_fleet_channels=fleet_channels
        )

        # Record recommendation time
        self._recent_recommendations[cooldown_key] = time.time()

        return rec

    def get_positioning_recommendations(self, count: int = 5) -> List[PositionRecommendation]:
        """
        Get top positioning recommendations for the fleet.

        Now includes hive centrality analysis (Use Case 4):
        - Selects members who would benefit most from new connections
        - Estimates centrality improvement from target connection
        - Applies bonuses for network position improvements

        Args:
            count: Number of recommendations to return

        Returns:
            List of PositionRecommendation
        """
        recommendations = []

        # Get valuable corridors
        if self.route_analyzer:
            corridors = self.route_analyzer.find_valuable_corridors(min_score=0.03)
        else:
            corridors = []

        seen_targets = set()

        for corridor in corridors:
            if len(recommendations) >= count:
                break

            target = corridor.destination_peer_id
            if target in seen_targets:
                continue

            # Check fleet coverage
            fleet_channels = self._count_fleet_channels_to_target(target)
            if fleet_channels >= MAX_MEMBERS_PER_TARGET:
                continue

            # Select member with centrality metrics (Use Case 4)
            member_selection = self._select_best_member_with_metrics(target)
            if not member_selection:
                continue

            recommended_member = member_selection["member_id"]
            member_centrality = member_selection["centrality"]
            centrality_improvement = member_selection["centrality_improvement"]

            # Calculate priority
            priority = corridor.value_score

            is_exchange, _ = self.route_analyzer._is_exchange(
                corridor.destination_alias
            ) if self.route_analyzer else (False, 0)

            if is_exchange:
                priority *= EXCHANGE_PRIORITY_BONUS
            if fleet_channels == 0:
                priority *= UNDERSERVED_PRIORITY_BONUS

            # Use Case 4: Apply centrality improvement bonus
            improves_network_position = centrality_improvement >= MIN_CENTRALITY_IMPROVEMENT
            if improves_network_position:
                priority *= CENTRALITY_IMPROVEMENT_BONUS

            # Determine priority tier
            if priority >= 0.5:
                priority_tier = "critical"
            elif priority >= 0.3:
                priority_tier = "high"
            elif priority >= 0.15:
                priority_tier = "medium"
            else:
                priority_tier = "low"

            # Build reason with centrality context
            reason_parts = [f"{corridor.value_tier} value corridor"]
            if improves_network_position:
                reason_parts.append(f"improves hive centrality by ~{centrality_improvement:.0%}")
            if member_centrality < LOW_CENTRALITY_THRESHOLD:
                reason_parts.append(f"member needs better connectivity")

            rec = PositionRecommendation(
                target_peer_id=target,
                target_alias=corridor.destination_alias,
                recommended_member=recommended_member,
                recommended_capacity_sats=5_000_000,
                max_fee_rate_ppm=1000,
                reason="; ".join(reason_parts),
                priority_score=priority,
                priority_tier=priority_tier,
                is_exchange=is_exchange,
                is_underserved=fleet_channels == 0,
                corridor_value=corridor.value_score,
                current_fleet_channels=fleet_channels,
                # Centrality fields (Use Case 4)
                member_current_centrality=member_centrality,
                estimated_centrality_improvement=centrality_improvement,
                improves_network_position=improves_network_position
            )

            recommendations.append(rec)
            seen_targets.add(target)

        # Sort by priority
        recommendations.sort(key=lambda r: r.priority_score, reverse=True)

        return recommendations


# =============================================================================
# STRATEGIC POSITIONING MANAGER
# =============================================================================

class StrategicPositioningManager:
    """
    Main interface for strategic positioning.

    Coordinates:
    - Route value analysis (corridor identification)
    - Fleet positioning strategy (channel open recommendations)
    """

    def __init__(
        self,
        plugin,
        database=None,
        state_manager=None,
        fee_coordination_mgr=None,
        planner=None,
        # Legacy kwargs accepted but ignored for backwards compatibility
        yield_metrics_mgr=None,
    ):
        """
        Initialize the strategic positioning manager.

        Args:
            plugin: Plugin reference
            database: Database for persistence
            state_manager: StateManager for fleet state
            fee_coordination_mgr: FeeCoordinationManager for corridor data
            planner: Planner for underserved targets
        """
        self.plugin = plugin
        self.database = database

        # Initialize components
        self.route_analyzer = RouteValueAnalyzer(
            plugin=plugin,
            state_manager=state_manager,
            fee_coordination_mgr=fee_coordination_mgr
        )

        self.positioning_strategy = FleetPositioningStrategy(
            plugin=plugin,
            state_manager=state_manager,
            route_analyzer=self.route_analyzer,
            planner=planner
        )

        self._our_pubkey: Optional[str] = None

        # Remote corridor/proposal storage for fleet sharing
        self._remote_corridors: Dict[str, List[Dict[str, Any]]] = {}
        self._remote_proposals: List[Dict[str, Any]] = []

    def set_our_pubkey(self, pubkey: str) -> None:
        """Set our node's pubkey."""
        self._our_pubkey = pubkey
        self.route_analyzer.set_our_pubkey(pubkey)
        self.positioning_strategy.set_our_pubkey(pubkey)

    def _log(self, message: str, level: str = "debug") -> None:
        """Log a message if plugin is available."""
        if self.plugin:
            self.plugin.log(f"STRATEGIC_POS: {message}", level=level)

    def get_valuable_corridors(self, min_score: float = 0.05) -> List[Dict[str, Any]]:
        """
        Get high-value corridors for potential positioning.

        Args:
            min_score: Minimum value score

        Returns:
            List of corridor value dicts
        """
        corridors = self.route_analyzer.find_valuable_corridors(min_score=min_score)
        return [c.to_dict() for c in corridors]

    def get_exchange_coverage(self) -> Dict[str, Any]:
        """
        Get exchange connectivity status.

        Returns:
            Dict with exchange coverage analysis
        """
        targets = self.route_analyzer.find_exchange_targets()

        covered = sum(1 for t in targets if t["has_connection"])
        total = len(targets)

        return {
            "total_priority_exchanges": total,
            "covered_exchanges": covered,
            "coverage_pct": round(covered / total * 100, 1) if total > 0 else 0,
            "exchanges": targets
        }

    def get_positioning_recommendations(self, count: int = 5) -> List[Dict[str, Any]]:
        """
        Get channel open recommendations for strategic positioning.

        Args:
            count: Number of recommendations

        Returns:
            List of recommendation dicts
        """
        recs = self.positioning_strategy.get_positioning_recommendations(count=count)
        return [r.to_dict() for r in recs]

    def get_positioning_summary(self) -> Dict[str, Any]:
        """
        Get summary of strategic positioning.

        Returns:
            PositioningSummary dict
        """
        summary = PositioningSummary()

        # Get corridor data
        corridors = self.route_analyzer.find_valuable_corridors(min_score=0.01)
        summary.total_targets_analyzed = len(corridors)
        summary.high_value_corridors = sum(1 for c in corridors if c.value_tier == "high")

        # Get exchange coverage
        exchange_data = self.get_exchange_coverage()
        summary.exchange_coverage_pct = exchange_data["coverage_pct"]

        # Get recommendations
        position_recs = self.positioning_strategy.get_positioning_recommendations(count=20)
        summary.open_recommendations = len(position_recs)
        summary.underserved_targets = sum(1 for r in position_recs if r.is_underserved)

        return summary.to_dict()

    def get_status(self) -> Dict[str, Any]:
        """
        Get overall strategic positioning status.

        Returns:
            Status dict
        """
        summary = self.get_positioning_summary()

        return {
            "enabled": True,
            "summary": summary,
            "thresholds": {
                "high_value_volume_daily": HIGH_VALUE_VOLUME_SATS_DAILY,
                "max_members_per_target": MAX_MEMBERS_PER_TARGET
            },
            "priority_exchanges": list(PRIORITY_EXCHANGES.keys())
        }

    # =========================================================================
    # FLEET INTELLIGENCE SHARING
    # =========================================================================

    def get_shareable_corridors(
        self,
        min_value_score: float = 0.05,
        max_corridors: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Get valuable corridors suitable for sharing with fleet.

        Args:
            min_value_score: Minimum value score to share
            max_corridors: Maximum number of corridors

        Returns:
            List of corridor dicts ready for serialization
        """
        shareable = []

        try:
            corridors = self.route_analyzer.find_valuable_corridors(min_score=min_value_score)

            for c in corridors:
                shareable.append({
                    "source_peer_id": c.source_peer_id,
                    "destination_peer_id": c.destination_peer_id,
                    "source_alias": c.source_alias,
                    "destination_alias": c.destination_alias,
                    "daily_volume_sats": c.daily_volume_sats,
                    "value_score": round(c.value_score, 4),
                    "competition_level": c.competition_level,
                    "competitor_count": c.competitor_count,
                    "margin_estimate_ppm": c.margin_estimate_ppm,
                    "fleet_coverage": c.fleet_members_present
                })

        except Exception as e:
            self._log(f"Error collecting shareable corridors: {e}", level="debug")

        return shareable[:max_corridors]

    def get_shareable_positioning_recommendations(
        self,
        max_recommendations: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Get positioning recommendations suitable for sharing.

        Args:
            max_recommendations: Maximum number of recommendations

        Returns:
            List of positioning proposal dicts
        """
        shareable = []

        try:
            recs = self.positioning_strategy.get_positioning_recommendations(count=max_recommendations)

            for r in recs:
                shareable.append({
                    "target_peer_id": r.target_peer_id,
                    "recommended_member": r.recommended_member or "",
                    "priority_tier": r.priority_tier,
                    "target_capacity_sats": r.recommended_capacity_sats,
                    "reason": r.reason,
                    "value_score": round(r.priority_score, 4),
                    "is_exchange": r.is_exchange,
                    "is_underserved": r.is_underserved
                })

        except Exception as e:
            self._log(f"Error collecting positioning recommendations: {e}", level="debug")

        return shareable

    def receive_corridor_from_fleet(
        self,
        reporter_id: str,
        corridor_data: Dict[str, Any]
    ) -> bool:
        """
        Receive a corridor value report from another fleet member.

        Args:
            reporter_id: The fleet member who reported this
            corridor_data: Dict with corridor details

        Returns:
            True if stored successfully
        """
        source = corridor_data.get("source_peer_id")
        dest = corridor_data.get("destination_peer_id")
        if not source or not dest:
            return False

        key = f"{source}:{dest}"
        entry = {
            "reporter_id": reporter_id,
            "daily_volume_sats": corridor_data.get("daily_volume_sats", 0),
            "value_score": corridor_data.get("value_score", 0),
            "competition_level": corridor_data.get("competition_level", "unknown"),
            "timestamp": time.time()
        }

        if key not in self._remote_corridors:
            self._remote_corridors[key] = []

        self._remote_corridors[key].append(entry)

        # Keep only last 5 reports per corridor
        if len(self._remote_corridors[key]) > 5:
            self._remote_corridors[key] = self._remote_corridors[key][-5:]

        return True

    def receive_positioning_proposal_from_fleet(
        self,
        reporter_id: str,
        proposal_data: Dict[str, Any]
    ) -> bool:
        """
        Receive a positioning proposal from another fleet member.

        Args:
            reporter_id: The fleet member who proposed this
            proposal_data: Dict with proposal details

        Returns:
            True if stored successfully
        """
        target = proposal_data.get("target_peer_id")
        if not target:
            return False

        entry = {
            "reporter_id": reporter_id,
            "target_peer_id": target,
            "recommended_member": proposal_data.get("recommended_member", ""),
            "priority_tier": proposal_data.get("priority_tier", "low"),
            "target_capacity_sats": proposal_data.get("target_capacity_sats", 0),
            "reason": proposal_data.get("reason", ""),
            "value_score": proposal_data.get("value_score", 0),
            "timestamp": time.time()
        }

        self._remote_proposals.append(entry)

        # Keep only last 50 proposals
        if len(self._remote_proposals) > 50:
            self._remote_proposals = self._remote_proposals[-50:]

        return True

    def get_fleet_corridor_consensus(self, source: str, dest: str) -> Optional[Dict[str, Any]]:
        """Get consensus corridor value from fleet reports."""
        key = f"{source}:{dest}"
        reports = self._remote_corridors.get(key, [])
        if not reports:
            return None

        now = time.time()
        recent = [r for r in reports if now - r.get("timestamp", 0) < 7 * 86400]
        if not recent:
            return None

        avg_volume = sum(r.get("daily_volume_sats", 0) for r in recent) / len(recent)
        avg_score = sum(r.get("value_score", 0) for r in recent) / len(recent)

        return {
            "source": source,
            "destination": dest,
            "avg_daily_volume_sats": int(avg_volume),
            "avg_value_score": round(avg_score, 4),
            "reporter_count": len(recent)
        }

    def cleanup_old_remote_data(self, max_age_days: float = 7) -> int:
        """Remove old remote positioning data."""
        cutoff = time.time() - (max_age_days * 86400)
        cleaned = 0

        # Cleanup corridors
        for key in list(self._remote_corridors.keys()):
            before = len(self._remote_corridors[key])
            self._remote_corridors[key] = [
                r for r in self._remote_corridors[key]
                if r.get("timestamp", 0) > cutoff
            ]
            cleaned += before - len(self._remote_corridors[key])
            if not self._remote_corridors[key]:
                del self._remote_corridors[key]

        # Cleanup proposals
        before = len(self._remote_proposals)
        self._remote_proposals = [
            p for p in self._remote_proposals
            if p.get("timestamp", 0) > cutoff
        ]
        cleaned += before - len(self._remote_proposals)

        return cleaned
