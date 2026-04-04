"""
Planner Module for cl-hive (Phase 6: Topology Optimization)

Implements the "Gardner" algorithm for automated topology management:
- Saturation Analysis: Calculate Hive market share per target
- Guard Mechanism: Prevent redundant channel opens to saturated targets
- Expansion Proposals: Cooperative expansion with feerate gate

Security Constraints (Red Team - PHASE6_THREAT_MODEL):
- Gossip capacity is CLAMPED to public listchannels data
- Max 5 new unmanages per cycle (abort if exceeded)
- All decisions logged to hive_planner_log table

Implements saturation detection, guard mechanism, and expansion logic.

Author: Lightning Goats Team
"""

import math
import time
import secrets
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from pyln.client import RpcError
except ImportError:
    # For testing without pyln installed
    class RpcError(Exception):
        """Stub RpcError for testing."""
        pass

try:
    from modules.intent_manager import IntentType
except ImportError:
    # For testing - define stubs
    class IntentType:
        CHANNEL_OPEN = 'channel_open'

try:
    from modules.quality_scorer import PeerQualityScorer
except ImportError:
    # For testing without quality_scorer
    PeerQualityScorer = None


# =============================================================================
# CONSTANTS
# =============================================================================

# Cache refresh interval (seconds) - avoid hammering listchannels
NETWORK_CACHE_TTL_SECONDS = 300

# Maximum ignores per cycle (Red Team mitigation)
MAX_IGNORES_PER_CYCLE = 5

# Saturation release threshold (hysteresis to avoid flip-flopping)
SATURATION_RELEASE_THRESHOLD_PCT = 0.15  # Release ignore at 15%

# Minimum public capacity to consider a target (anti-Sybil)
MIN_TARGET_CAPACITY_SATS = 100_000_000  # 1 BTC

# Underserved threshold (targets with low Hive share)
UNDERSERVED_THRESHOLD_PCT = 0.05  # < 5% Hive share = underserved

# Legacy minimum channel size (now configurable via planner_min_channel_sats)
# Kept for backwards compatibility in case config is not available
MIN_CHANNEL_SIZE_SATS_FALLBACK = 1_000_000  # 1M sats fallback

# Maximum expansion proposals per cycle (rate limiting)
MAX_EXPANSIONS_PER_CYCLE = 1

# Quality scoring thresholds (Phase 6.2)
MIN_QUALITY_SCORE = 0.45  # Minimum quality score for expansion
QUALITY_SCORE_DAYS = 90   # Days of history to consider for quality scoring

# =============================================================================
# COOPERATION MODULE INTEGRATION (Phase 7)
# =============================================================================

# Hive coverage diversity thresholds
HIVE_COVERAGE_HIGH_PCT = 0.60        # >60% of hive has channels = well covered
HIVE_COVERAGE_MAJORITY_PCT = 0.50    # >50% = majority covered

# Network competition thresholds (peer's total channel count)
LOW_COMPETITION_CHANNELS = 30         # <30 channels = low competition, good target
MEDIUM_COMPETITION_CHANNELS = 100     # 30-100 = moderate competition
HIGH_COMPETITION_CHANNELS = 200       # >200 = high competition (e.g., Kraken)

# Competition discount factors (applied to base score)
COMPETITION_DISCOUNT_LOW = 1.0        # No discount for low competition
COMPETITION_DISCOUNT_MEDIUM = 0.85    # 15% discount for medium competition
COMPETITION_DISCOUNT_HIGH = 0.65      # 35% discount for high competition

# Bottleneck bonus (for peers identified by liquidity_coordinator)
BOTTLENECK_BONUS_MULTIPLIER = 1.5     # 50% bonus for bottleneck peers

# Physarum positioning bonuses (integrate with strategic_positioning)
CORRIDOR_VALUE_BONUS_HIGH = 1.4       # 40% bonus for high-value corridors
CORRIDOR_VALUE_BONUS_MEDIUM = 1.15    # 15% bonus for medium-value corridors


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class ChannelInfo:
    """Represents a channel from listchannels."""
    source: str
    destination: str
    short_channel_id: str
    capacity_sats: int
    active: bool


@dataclass
class SaturationResult:
    """Result of saturation calculation for a target."""
    target: str
    hive_capacity_sats: int
    public_capacity_sats: int
    hive_share_pct: float
    is_saturated: bool
    should_release: bool


@dataclass
class UnderservedResult:
    """Result identifying an underserved target for expansion."""
    target: str
    public_capacity_sats: int
    hive_share_pct: float
    score: float  # Higher = more attractive for expansion
    quality_score: float = 0.5  # Peer quality score (Phase 6.2)
    quality_confidence: float = 0.0  # Confidence in quality score
    quality_recommendation: str = "neutral"  # Quality recommendation


@dataclass
class ChannelSizeResult:
    """Result of intelligent channel sizing calculation."""
    recommended_size_sats: int
    factors: Dict[str, Any]
    reasoning: str


@dataclass
class ExpansionRecommendation:
    """
    Recommendation for expanding capacity to a peer.

    Integrates cooperation module data for smarter topology decisions.
    """
    target: str
    recommendation_type: str  # "open_channel" | "no_action"
    score: float
    reasoning: str
    details: Dict[str, Any]

    # Cooperation data
    hive_coverage_pct: float      # % of hive members with channels
    hive_members_count: int       # Count of members with channels
    network_channels: int         # Peer's total channel count
    is_bottleneck: bool           # From liquidity_coordinator
    competition_level: str        # "low" | "medium" | "high" | "very_high"


# =============================================================================
# INTELLIGENT CHANNEL SIZING
# =============================================================================

class ChannelSizer:
    """
    Intelligent channel sizing engine for Hive expansion proposals.

    Factors considered:
    1. Target capacity - larger nodes warrant larger channels (credibility)
    2. Hive share gap - lower share → larger channel to reach target share
    3. Routing potential - nodes with high connectivity get larger channels
    4. Available liquidity - don't overcommit, leave operational reserve
    5. Economics - expected fee revenue vs capital lockup cost
    6. Quality score - peer reliability based on historical hive data (Phase 6.3)

    The algorithm produces a weighted score that determines channel size
    within the configured min/max bounds.
    """

    # Weight factors for each sizing component (sum to 1.0)
    # Phase 6.3: Redistributed to include quality factor
    WEIGHT_TARGET_CAPACITY = 0.15  # 15% - larger targets get larger channels
    WEIGHT_SHARE_GAP = 0.20        # 20% - underserved targets get priority
    WEIGHT_ROUTING_POTENTIAL = 0.20  # 20% - high-connectivity nodes
    WEIGHT_LIQUIDITY = 0.15        # 15% - available balance consideration
    WEIGHT_ECONOMICS = 0.10        # 10% - expected ROI
    WEIGHT_QUALITY = 0.20          # 20% - peer quality score (Phase 6.3)

    # Routing potential thresholds
    HIGH_CONNECTIVITY_CHANNELS = 50   # Node with 50+ channels = high connectivity
    VERY_HIGH_CONNECTIVITY_CHANNELS = 200  # 200+ = major routing node

    # Mid-size preference thresholds (avoid very large nodes with high minimums)
    # Nodes with 300+ channels or 500+ BTC often require 5M+ sat minimums
    PREFER_MID_SIZE = True  # Enable mid-size node preference
    MID_SIZE_OPTIMAL_CHANNELS_MIN = 20   # Sweet spot lower bound
    MID_SIZE_OPTIMAL_CHANNELS_MAX = 100  # Sweet spot upper bound
    MID_SIZE_OPTIMAL_BTC_MIN = 10        # Optimal capacity lower bound (BTC)
    MID_SIZE_OPTIMAL_BTC_MAX = 100       # Optimal capacity upper bound (BTC)
    LARGE_NODE_PENALTY = 0.5             # Score multiplier for very large nodes

    # Economic assumptions
    EXPECTED_ANNUAL_FEE_RATE = 0.001  # 0.1% annual return on channel capacity
    OPPORTUNITY_COST_RATE = 0.05      # 5% opportunity cost of locked capital

    # Quality score thresholds (Phase 6.3)
    QUALITY_EXCELLENT_THRESHOLD = 0.75  # Excellent quality - bonus sizing
    QUALITY_GOOD_THRESHOLD = 0.55       # Good quality - normal sizing
    QUALITY_NEUTRAL_THRESHOLD = 0.40    # Neutral - slightly reduced
    # Below NEUTRAL = caution - significantly reduced

    def __init__(self, plugin=None, quality_scorer=None):
        """
        Initialize the ChannelSizer.

        Args:
            plugin: Plugin instance for logging
            quality_scorer: PeerQualityScorer instance for quality lookups (Phase 6.3)
        """
        self.plugin = plugin
        self.quality_scorer = quality_scorer

    def _log(self, msg: str, level: str = "debug") -> None:
        """Log a message if plugin is available."""
        if self.plugin:
            self.plugin.log(f"[ChannelSizer] {msg}", level=level)

    def calculate_size(
        self,
        target: str,
        target_capacity_sats: int,
        target_channel_count: int,
        hive_share_pct: float,
        target_share_cap: float,
        onchain_balance_sats: int,
        min_channel_sats: int,
        max_channel_sats: int,
        default_channel_sats: int,
        hive_total_capacity_sats: int = 0,
        avg_fee_rate_ppm: int = 500,
        quality_score: float = None,
        quality_confidence: float = 0.0,
        quality_recommendation: str = "neutral",
        available_budget_sats: int = None,
    ) -> ChannelSizeResult:
        """
        Calculate the optimal channel size for a target.

        Args:
            target: Target node pubkey
            target_capacity_sats: Target's total public capacity
            target_channel_count: Number of channels the target has
            hive_share_pct: Current hive share to this target (0.0-1.0)
            target_share_cap: Maximum share we want (e.g., 0.20 for 20%)
            onchain_balance_sats: Available onchain balance
            min_channel_sats: Minimum allowed channel size
            max_channel_sats: Maximum allowed channel size
            default_channel_sats: Default channel size (baseline)
            hive_total_capacity_sats: Total hive capacity (for liquidity calc)
            avg_fee_rate_ppm: Average fee rate in ppm for economic calc
            quality_score: Peer quality score 0-1 (Phase 6.3, optional)
            quality_confidence: Confidence in quality score 0-1 (Phase 6.3)
            quality_recommendation: Quality recommendation string (Phase 6.3)
            available_budget_sats: Available budget for channel opens (optional)
                If provided, caps the channel size to stay within budget.

        Returns:
            ChannelSizeResult with recommended size and reasoning
        """
        factors = {}

        # Phase 6.3: Lookup quality if not provided and scorer is available
        if quality_score is None and self.quality_scorer:
            quality_result = self.quality_scorer.calculate_score(target)
            quality_score = quality_result.overall_score
            quality_confidence = quality_result.confidence
            quality_recommendation = quality_result.recommendation
        elif quality_score is None:
            # Default to neutral if no quality data
            quality_score = 0.5
            quality_confidence = 0.0
            quality_recommendation = "neutral"

        # =================================================================
        # Factor 1: Target Capacity Score (0.0 to 2.0)
        # Mid-sized nodes preferred - they accept smaller channel minimums
        # =================================================================
        btc_capacity = target_capacity_sats / 100_000_000
        if btc_capacity <= 0:
            capacity_score = 0.5
        elif self.PREFER_MID_SIZE:
            # Mid-size preference: peak score at optimal range, lower for very large
            if self.MID_SIZE_OPTIMAL_BTC_MIN <= btc_capacity <= self.MID_SIZE_OPTIMAL_BTC_MAX:
                # Optimal range - full score with slight boost
                capacity_score = 1.8
            elif btc_capacity < self.MID_SIZE_OPTIMAL_BTC_MIN:
                # Smaller than optimal - scale up to optimal range
                capacity_score = min(1.5, 0.5 + 0.5 * math.log10(max(1, btc_capacity)))
            elif btc_capacity > self.MID_SIZE_OPTIMAL_BTC_MAX * 5:
                # Very large node (1000+ BTC) - likely requires 5M+ minimums
                capacity_score = 1.0 * self.LARGE_NODE_PENALTY
            else:
                # Above optimal but not huge - gradual reduction
                overage_factor = btc_capacity / self.MID_SIZE_OPTIMAL_BTC_MAX
                capacity_score = max(1.0, 1.8 - 0.2 * math.log10(overage_factor))
        else:
            # Original behavior: larger is better (logarithmic scale)
            capacity_score = min(2.0, 0.5 + 0.5 * math.log10(max(1, btc_capacity)))
        factors['capacity_score'] = round(capacity_score, 3)
        factors['target_capacity_btc'] = round(btc_capacity, 2)

        # =================================================================
        # Factor 2: Share Gap Score (0.0 to 2.0)
        # Lower current share = higher score (need to catch up)
        # =================================================================
        share_gap = target_share_cap - hive_share_pct
        if share_gap <= 0:
            # Already at or above target share
            share_score = 0.5
        else:
            # Scale: 0% share = 2.0, target_share = 1.0
            share_score = 1.0 + (share_gap / target_share_cap)
        share_score = min(2.0, max(0.5, share_score))
        factors['share_score'] = round(share_score, 3)
        factors['share_gap_pct'] = round(share_gap * 100, 2)

        # =================================================================
        # Factor 3: Routing Potential Score (0.0 to 2.0)
        # Mid-connectivity nodes preferred for better channel acceptance
        # =================================================================
        if self.PREFER_MID_SIZE:
            # Mid-size preference: sweet spot at 30-150 channels
            if self.MID_SIZE_OPTIMAL_CHANNELS_MIN <= target_channel_count <= self.MID_SIZE_OPTIMAL_CHANNELS_MAX:
                routing_score = 2.0  # Optimal range - well-connected but reasonable minimums
            elif target_channel_count > self.MID_SIZE_OPTIMAL_CHANNELS_MAX * 2:
                # Very large hub (300+ channels) - likely high minimums
                routing_score = 1.2 * self.LARGE_NODE_PENALTY
            elif target_channel_count > self.MID_SIZE_OPTIMAL_CHANNELS_MAX:
                # Above optimal (150-300) - still good but not ideal
                routing_score = 1.5
            elif target_channel_count >= 20:
                routing_score = 1.5  # Moderately connected
            elif target_channel_count >= 10:
                routing_score = 1.0  # Average
            else:
                routing_score = 0.7  # Low connectivity (risky)
        else:
            # Original behavior: more channels = better
            if target_channel_count >= self.VERY_HIGH_CONNECTIVITY_CHANNELS:
                routing_score = 2.0  # Major routing hub
            elif target_channel_count >= self.HIGH_CONNECTIVITY_CHANNELS:
                routing_score = 1.5  # Well-connected node
            elif target_channel_count >= 20:
                routing_score = 1.2  # Moderately connected
            elif target_channel_count >= 10:
                routing_score = 1.0  # Average
            else:
                routing_score = 0.7  # Low connectivity (risky)
        factors['routing_score'] = routing_score
        factors['target_channel_count'] = target_channel_count

        # =================================================================
        # Factor 4: Liquidity Score (0.5 to 1.5)
        # Don't overcommit - leave operational reserve
        # =================================================================
        # Reserve: keep at least 20% of balance for other operations
        available_for_channel = onchain_balance_sats * 0.8

        # Score based on how comfortable we are with the allocation
        if available_for_channel >= max_channel_sats * 3:
            liquidity_score = 1.5  # Very comfortable
        elif available_for_channel >= max_channel_sats * 2:
            liquidity_score = 1.3  # Comfortable
        elif available_for_channel >= max_channel_sats:
            liquidity_score = 1.0  # Adequate
        elif available_for_channel >= min_channel_sats * 2:
            liquidity_score = 0.8  # Tight
        else:
            liquidity_score = 0.5  # Very tight - use minimum
        factors['liquidity_score'] = liquidity_score
        factors['available_sats'] = int(available_for_channel)

        # =================================================================
        # Factor 5: Economics Score (0.5 to 1.5)
        # Expected fee revenue vs capital lockup cost
        # =================================================================
        # Simple model: (expected_annual_fees / locked_capital) vs threshold
        # Higher fee rate environments = larger channels make more sense

        # Expected annual fee revenue per sat locked
        fee_multiplier = avg_fee_rate_ppm / 1_000_000
        annual_turns = 12  # Assume capital turns over ~12x per year
        expected_annual_return = fee_multiplier * annual_turns

        # ROI score
        if expected_annual_return >= 0.02:  # 2%+ return
            economics_score = 1.5
        elif expected_annual_return >= 0.01:  # 1%+ return
            economics_score = 1.2
        elif expected_annual_return >= 0.005:  # 0.5%+ return
            economics_score = 1.0
        else:
            economics_score = 0.7  # Low return environment
        factors['economics_score'] = economics_score
        factors['expected_annual_return_pct'] = round(expected_annual_return * 100, 2)
        factors['avg_fee_rate_ppm'] = avg_fee_rate_ppm

        # =================================================================
        # Factor 6: Quality Score (0.5 to 2.0) - Phase 6.3
        # Higher quality peers get larger channels
        # =================================================================
        # Quality score ranges from 0 to 1, we map to 0.5 to 2.0
        # - Excellent (>0.75): 1.5 to 2.0 - larger channels
        # - Good (0.55-0.75): 1.0 to 1.5 - normal to bonus
        # - Neutral (0.40-0.55): 0.8 to 1.0 - slightly reduced
        # - Caution (<0.40): 0.5 to 0.8 - significantly reduced

        if quality_confidence < 0.3:
            # Low confidence - use neutral scoring
            quality_factor = 1.0
            factors['quality_note'] = 'low_confidence_neutral'
        elif quality_score >= self.QUALITY_EXCELLENT_THRESHOLD:
            # Excellent quality - bonus sizing
            excess = quality_score - self.QUALITY_EXCELLENT_THRESHOLD
            quality_factor = 1.5 + (excess / 0.25) * 0.5  # 1.5 to 2.0
            quality_factor = min(2.0, quality_factor)
        elif quality_score >= self.QUALITY_GOOD_THRESHOLD:
            # Good quality - normal to bonus
            ratio = (quality_score - self.QUALITY_GOOD_THRESHOLD) / (
                self.QUALITY_EXCELLENT_THRESHOLD - self.QUALITY_GOOD_THRESHOLD
            )
            quality_factor = 1.0 + ratio * 0.5  # 1.0 to 1.5
        elif quality_score >= self.QUALITY_NEUTRAL_THRESHOLD:
            # Neutral - slightly reduced
            ratio = (quality_score - self.QUALITY_NEUTRAL_THRESHOLD) / (
                self.QUALITY_GOOD_THRESHOLD - self.QUALITY_NEUTRAL_THRESHOLD
            )
            quality_factor = 0.8 + ratio * 0.2  # 0.8 to 1.0
        else:
            # Caution - significantly reduced
            ratio = quality_score / self.QUALITY_NEUTRAL_THRESHOLD
            quality_factor = 0.5 + ratio * 0.3  # 0.5 to 0.8

        factors['quality_factor'] = round(quality_factor, 3)
        factors['quality_score'] = round(quality_score, 3)
        factors['quality_confidence'] = round(quality_confidence, 3)
        factors['quality_recommendation'] = quality_recommendation

        # =================================================================
        # Calculate Weighted Score
        # =================================================================
        weighted_score = (
            capacity_score * self.WEIGHT_TARGET_CAPACITY +
            share_score * self.WEIGHT_SHARE_GAP +
            routing_score * self.WEIGHT_ROUTING_POTENTIAL +
            liquidity_score * self.WEIGHT_LIQUIDITY +
            economics_score * self.WEIGHT_ECONOMICS +
            quality_factor * self.WEIGHT_QUALITY
        )
        factors['weighted_score'] = round(weighted_score, 3)

        # =================================================================
        # Convert Score to Channel Size
        # =================================================================
        # Score range: ~0.5 to ~2.0
        # Map to channel size range: min to max
        # Score of 1.0 = default size
        # Score of 0.5 = min size
        # Score of 2.0 = max size

        if weighted_score <= 1.0:
            # Below average: scale between min and default
            ratio = max(0.0, (weighted_score - 0.5) / 0.5)  # 0.0 to 1.0
            size_range = default_channel_sats - min_channel_sats
            recommended_size = min_channel_sats + int(size_range * ratio)
        else:
            # Above average: scale between default and max
            ratio = (weighted_score - 1.0) / 1.0  # 0.0 to 1.0
            size_range = max_channel_sats - default_channel_sats
            recommended_size = default_channel_sats + int(size_range * ratio)

        # =================================================================
        # Apply Hard Limits
        # =================================================================
        size_before_limits = recommended_size

        # Never exceed available liquidity (with reserve)
        max_from_liquidity = int(available_for_channel * 0.5)  # Max 50% of available
        recommended_size = min(recommended_size, max_from_liquidity)

        # Never exceed available budget (if provided)
        budget_limited = False
        if available_budget_sats is not None and available_budget_sats > 0:
            if recommended_size > available_budget_sats:
                recommended_size = available_budget_sats
                budget_limited = True
            factors['available_budget_sats'] = available_budget_sats
            factors['budget_limited'] = budget_limited

        # Ensure within config bounds
        recommended_size = max(min_channel_sats, min(recommended_size, max_channel_sats))

        # If budget is less than minimum, we can't open this channel
        if available_budget_sats is not None and available_budget_sats < min_channel_sats:
            factors['insufficient_budget'] = True
            factors['budget_shortfall_sats'] = min_channel_sats - available_budget_sats

        factors['size_before_limits'] = size_before_limits
        factors['max_from_liquidity'] = max_from_liquidity

        # =================================================================
        # Generate Reasoning
        # =================================================================
        reasoning_parts = []

        if self.PREFER_MID_SIZE:
            # Mid-size preference reasoning
            if self.MID_SIZE_OPTIMAL_BTC_MIN <= btc_capacity <= self.MID_SIZE_OPTIMAL_BTC_MAX:
                reasoning_parts.append(f"optimal mid-size ({btc_capacity:.1f} BTC)")
            elif btc_capacity > self.MID_SIZE_OPTIMAL_BTC_MAX * 5:
                reasoning_parts.append(f"very large node ({btc_capacity:.1f} BTC, likely high min)")
            elif capacity_score >= 1.5:
                reasoning_parts.append(f"good size ({btc_capacity:.1f} BTC)")
            elif capacity_score <= 0.7:
                reasoning_parts.append(f"small target ({btc_capacity:.1f} BTC)")
        else:
            if capacity_score >= 1.5:
                reasoning_parts.append(f"large target ({btc_capacity:.1f} BTC)")
            elif capacity_score <= 0.7:
                reasoning_parts.append(f"small target ({btc_capacity:.1f} BTC)")

        if share_score >= 1.5:
            reasoning_parts.append(f"underserved ({share_gap*100:.1f}% gap)")

        if self.PREFER_MID_SIZE and self.MID_SIZE_OPTIMAL_CHANNELS_MIN <= target_channel_count <= self.MID_SIZE_OPTIMAL_CHANNELS_MAX:
            reasoning_parts.append(f"optimal connectivity ({target_channel_count} channels)")
        elif routing_score >= 1.5:
            reasoning_parts.append(f"high routing potential ({target_channel_count} channels)")
        elif routing_score <= 0.8:
            reasoning_parts.append(f"low connectivity ({target_channel_count} channels)")

        if liquidity_score <= 0.7:
            reasoning_parts.append("liquidity constrained")

        if economics_score >= 1.3:
            reasoning_parts.append(f"favorable economics ({expected_annual_return*100:.1f}% expected)")

        # Phase 6.3: Add quality to reasoning
        if quality_confidence >= 0.3:
            if quality_factor >= 1.5:
                reasoning_parts.append(f"excellent quality ({quality_score:.2f})")
            elif quality_factor >= 1.0:
                reasoning_parts.append(f"good quality ({quality_score:.2f})")
            elif quality_factor < 0.8:
                reasoning_parts.append(f"quality concern ({quality_score:.2f}/{quality_recommendation})")

        # Add budget constraints to reasoning
        if budget_limited:
            reasoning_parts.append(f"budget-limited to {available_budget_sats:,} sats")
        if factors.get('insufficient_budget'):
            reasoning_parts.append(f"INSUFFICIENT BUDGET (need {min_channel_sats:,}, have {available_budget_sats:,})")

        if reasoning_parts:
            reasoning = f"Size factors: {', '.join(reasoning_parts)}"
        else:
            reasoning = "Standard sizing applied"

        self._log(
            f"Sizing for {target[:16]}...: {recommended_size:,} sats "
            f"(score={weighted_score:.2f}, quality={quality_score:.2f}, {reasoning})"
        )

        return ChannelSizeResult(
            recommended_size_sats=recommended_size,
            factors=factors,
            reasoning=reasoning
        )


# =============================================================================
# PLANNER CLASS
# =============================================================================

class Planner:
    """
    Topology optimization engine for the Hive swarm.

    Analyzes network topology to:
    1. Detect targets where Hive has excessive market share (saturation)
    2. Record saturation events for analytics and native expansion control
    3. Release saturation flags when share drops below threshold

    Thread Safety:
    - Uses config snapshot pattern (cfg passed to run_cycle)
    - Network cache is refreshed per-cycle
    - No sleeping inside run_cycle
    """

    def __init__(self, state_manager, database, bridge, plugin=None,
                 intent_manager=None, recommendation_logger=None,
                 liquidity_coordinator=None,
                 health_aggregator=None, rationalization_mgr=None,
                 strategic_positioning_mgr=None):
        """
        Initialize the Planner (recommendation-only).

        The planner generates expansion recommendations and detects saturation
        but does NOT execute channel opens or broadcasts directly.

        Args:
            state_manager: StateManager for accessing Hive peer states
            database: HiveDatabase for logging and membership data
            bridge: Integration Bridge for cl-revenue-ops
            plugin: Plugin reference for RPC and logging
            intent_manager: IntentManager for checking pending intents
            recommendation_logger: RecommendationLogger for logging decisions
            liquidity_coordinator: LiquidityCoordinator for bottleneck detection
            health_aggregator: HealthScoreAggregator for fleet health
            rationalization_mgr: RationalizationManager for redundancy detection
            strategic_positioning_mgr: StrategicPositioningManager for corridor value
        """
        self.state_manager = state_manager
        self.db = database
        self.bridge = bridge
        self.plugin = plugin
        self.intent_manager = intent_manager
        self.recommendation_logger = recommendation_logger

        # Cooperation modules - can be set after init via setter
        self.liquidity_coordinator = liquidity_coordinator
        self.health_aggregator = health_aggregator

        # Yield optimization modules
        self.rationalization_mgr = rationalization_mgr
        self.strategic_positioning_mgr = strategic_positioning_mgr

        # Quality scorer for peer evaluation
        if PeerQualityScorer and database:
            self.quality_scorer = PeerQualityScorer(database, plugin)
        else:
            self.quality_scorer = None

        # Network cache (refreshed each cycle).
        # NOTE: Only accessed from planner_loop's single thread — no snapshot needed.
        self._network_cache: Dict[str, List[ChannelInfo]] = {}
        self._network_cache_time: int = 0

        # Track currently ignored peers (to avoid duplicate ignores)
        self._ignored_peers: Set[str] = set()

    def _log(self, msg: str, level: str = "info") -> None:
        """Log a message if plugin is available."""
        if self.plugin:
            self.plugin.log(f"[Planner] {msg}", level=level)

    def set_cooperation_modules(
        self,
        liquidity_coordinator=None,
        health_aggregator=None,
        rationalization_mgr=None,
        strategic_positioning_mgr=None,
    ) -> None:
        """
        Set cooperation modules after initialization.

        This allows the planner to be initialized before the cooperation
        modules are available, then linked later.

        Args:
            liquidity_coordinator: LiquidityCoordinator for bottleneck detection
            health_aggregator: HealthScoreAggregator for fleet health
            rationalization_mgr: RationalizationManager for redundancy detection
            strategic_positioning_mgr: StrategicPositioningManager for corridor value
        """
        if liquidity_coordinator is not None:
            self.liquidity_coordinator = liquidity_coordinator
        if health_aggregator is not None:
            self.health_aggregator = health_aggregator
        if rationalization_mgr is not None:
            self.rationalization_mgr = rationalization_mgr
        if strategic_positioning_mgr is not None:
            self.strategic_positioning_mgr = strategic_positioning_mgr

        self._log(
            f"Cooperation modules set: liquidity={liquidity_coordinator is not None}, "
            f"health={health_aggregator is not None}, "
            f"rationalization={rationalization_mgr is not None}, "
            f"positioning={strategic_positioning_mgr is not None}",
            level='debug'
        )

    # =========================================================================
    # COOPERATION MODULE INTEGRATION (Phase 7)
    # =========================================================================

    def _count_hive_members_with_target(self, target: str) -> Tuple[int, int]:
        """
        Count how many distinct hive members have channels to a target.

        Uses state_manager to check topology data from all hive members.
        This helps determine hive coverage diversity - if most members
        already have channels to a peer, opening another is less valuable.

        Args:
            target: Target node pubkey

        Returns:
            (members_with_channels, total_members)
        """
        if not self.state_manager:
            return 0, 0

        hive_members = self._get_hive_members()
        if not hive_members:
            return 0, 0

        all_states_list = self.state_manager.get_all_peer_states()
        all_states = {s.peer_id: s for s in all_states_list}

        members_with_channel = 0
        for member_pubkey in hive_members:
            state = all_states.get(member_pubkey)
            if not state:
                continue

            topology = getattr(state, 'topology', []) or []
            if target in topology:
                members_with_channel += 1

        return members_with_channel, len(hive_members)

    def _calculate_competition_score(self, target: str) -> Tuple[float, str]:
        """
        Calculate competition discount based on peer's channel count.

        Peers with many channels (like Kraken with 500+) have high
        competition for routing fees. Opening a channel to them
        provides less marginal value than to smaller nodes.

        Args:
            target: Target node pubkey

        Returns:
            (discount_factor, competition_level)
            - discount_factor: 0.5 to 1.0, multiplied against base score
            - competition_level: "low", "medium", "high", or "very_high"
        """
        channel_count = self._get_target_channel_count(target)

        if channel_count < LOW_COMPETITION_CHANNELS:
            return COMPETITION_DISCOUNT_LOW, "low"
        elif channel_count < MEDIUM_COMPETITION_CHANNELS:
            return COMPETITION_DISCOUNT_MEDIUM, "medium"
        elif channel_count < HIGH_COMPETITION_CHANNELS:
            return COMPETITION_DISCOUNT_HIGH, "high"
        else:
            # Very high competition (>200 channels) - even bigger discount
            return 0.50, "very_high"

    def _is_bottleneck_peer(self, target: str) -> bool:
        """
        Check if peer is a common bottleneck identified by liquidity_coordinator.

        Bottleneck peers are those that multiple hive members have liquidity
        issues with (depleted or saturated channels). These are priority
        targets for new capacity as they benefit multiple fleet members.

        Args:
            target: Target node pubkey

        Returns:
            True if peer is a bottleneck
        """
        if not self.liquidity_coordinator:
            return False

        try:
            # Use the private method that returns bottleneck peers
            bottlenecks = self.liquidity_coordinator._get_common_bottleneck_peers()
            return target in bottlenecks
        except Exception as e:
            self._log(f"Error checking bottleneck status: {e}", level='debug')
            return False

    def _get_corridor_value_bonus(self, target: str) -> tuple:
        """
        Get corridor value bonus from strategic positioning.

        Uses route value analyzer to determine if this target
        is on a high-value routing corridor.

        Slime mold principle: Prioritize routes where nutrients (fees)
        flow most abundantly.

        Args:
            target: Target node pubkey

        Returns:
            Tuple of (bonus_multiplier: float, value_tier: str)
        """
        if not self.strategic_positioning_mgr:
            return 1.0, "unknown"

        try:
            corridors = self.strategic_positioning_mgr.get_valuable_corridors(min_score=0.01)

            # Find corridors that include this target
            best_tier = "low"
            for corridor in corridors:
                if corridor.get("destination_peer_id") == target:
                    tier = corridor.get("value_tier", "low")
                    if tier == "high":
                        best_tier = "high"
                        break
                    elif tier == "medium" and best_tier != "high":
                        best_tier = "medium"

            if best_tier == "high":
                return CORRIDOR_VALUE_BONUS_HIGH, "high"
            elif best_tier == "medium":
                return CORRIDOR_VALUE_BONUS_MEDIUM, "medium"
            else:
                return 1.0, "low"

        except Exception as e:
            self._log(f"Error getting corridor value: {e}", level='debug')
            return 1.0, "unknown"

    def _score_routing_improvement(self, target: str, cfg) -> float:
        """Score how much a virtual channel to target would improve routing.

        Creates a temporary askrene layer with a virtual channel to the
        target, calls getroutes to high-value destinations, and measures
        whether routes improve (lower fees, fewer hops).

        Returns:
            Score multiplier (1.0 = no improvement, up to 1.5 for significant gains).
        """
        if not self.plugin:
            return 1.0

        our_id = None
        try:
            info = self.plugin.rpc.getinfo()
            our_id = info.get("id")
        except Exception:
            return 1.0
        if not our_id:
            return 1.0

        temp_layer = f"hive-expansion-test-{target[:8]}"

        try:
            # Create temporary layer with virtual channel
            self.plugin.rpc.call("askrene-create-layer", {"layer": temp_layer})
            virtual_scid = "999x999x0"
            self.plugin.rpc.call("askrene-create-channel", {
                "layer": temp_layer,
                "source": our_id,
                "destination": target,
                "short_channel_id": virtual_scid,
                "capacity_msat": "5000000000",  # 5M sats virtual capacity
            })
            # Set reasonable fees on the virtual channel
            for direction in (0, 1):
                self.plugin.rpc.call("askrene-update-channel", {
                    "layer": temp_layer,
                    "short_channel_id_dir": f"{virtual_scid}/{direction}",
                    "fee_base_msat": 0,
                    "fee_proportional_millionths": 100,
                    "cltv_expiry_delta": 18,
                })

            # Pick a few well-known high-value destinations to test routes
            # Use peers from our own channels that have high capacity
            test_destinations = []
            try:
                channels = self.plugin.rpc.listpeerchannels()
                peers_by_cap = []
                for ch in channels.get("channels", []):
                    if ch.get("state") != "CHANNELD_NORMAL":
                        continue
                    pid = ch.get("peer_id", "")
                    total = ch.get("total_msat", 0)
                    if isinstance(total, str):
                        total = int(total.rstrip("msat"))
                    if pid and pid != target:
                        peers_by_cap.append((total, pid))
                peers_by_cap.sort(reverse=True)
                test_destinations = [pid for _, pid in peers_by_cap[:3]]
            except Exception:
                pass

            if not test_destinations:
                return 1.0

            # Compare route quality with and without the virtual channel
            # Build layer list dynamically — only include layers that exist
            improvement_count = 0
            layers_base = ["auto.localchans", "auto.sourcefree"]
            try:
                existing = self.plugin.rpc.call("askrene-listlayers", {})
                for l in existing.get("layers", []):
                    name = l.get("layer", "")
                    if name.startswith("hive-"):
                        layers_base.append(name)
            except Exception:
                pass
            layers_with = layers_base + [temp_layer]

            for dest in test_destinations:
                try:
                    # Route without virtual channel
                    try:
                        base_result = self.plugin.rpc.call("getroutes", {
                            "source": our_id,
                            "destination": dest,
                            "amount_msat": 10000000,
                            "layers": layers_base,
                            "maxfee_msat": 1000000,
                            "final_cltv": 18,
                        })
                    except Exception:
                        continue
                    base_routes = base_result.get("routes", [])

                    # Route with virtual channel
                    try:
                        with_result = self.plugin.rpc.call("getroutes", {
                            "source": our_id,
                            "destination": dest,
                            "amount_msat": 10000000,
                            "layers": layers_with,
                            "maxfee_msat": 1000000,
                            "final_cltv": 18,
                        })
                    except Exception:
                        continue
                    with_routes = with_result.get("routes", [])

                    # Compare: better probability or fewer hops = improvement
                    base_prob = base_result.get("probability_ppm", 0)
                    with_prob = with_result.get("probability_ppm", 0)

                    base_hops = len(base_routes[0].get("path", [])) if base_routes else 99
                    with_hops = len(with_routes[0].get("path", [])) if with_routes else 99

                    if with_prob > base_prob * 1.1 or with_hops < base_hops:
                        improvement_count += 1

                except Exception:
                    continue

            # Score: 1.0 base, +0.15 per destination that improved, max 1.5
            score = 1.0 + (improvement_count * 0.15)
            return min(1.5, score)

        except Exception:
            return 1.0
        finally:
            # Always clean up temporary layer
            try:
                self.plugin.rpc.call("askrene-remove-layer", {"layer": temp_layer})
            except Exception:
                pass

    def get_expansion_recommendation(
        self,
        target: str,
        cfg
    ) -> ExpansionRecommendation:
        """
        Get comprehensive expansion recommendation for a target.

        Integrates all cooperation modules to determine:
        1. Should we expand at all?
        2. What's the priority score?

        This provides richer analysis than get_underserved_targets()
        for individual peer evaluation.

        Args:
            target: Target node pubkey
            cfg: Config snapshot

        Returns:
            ExpansionRecommendation with action and reasoning
        """
        # Gather cooperation module data
        members_with, total_members = self._count_hive_members_with_target(target)
        hive_coverage_pct = members_with / total_members if total_members > 0 else 0

        network_channels = self._get_target_channel_count(target)
        competition_factor, competition_level = self._calculate_competition_score(target)

        is_bottleneck = self._is_bottleneck_peer(target)

        # Calculate hive share using existing method
        result = self._calculate_hive_share(target, cfg)

        # Base score calculation (from existing logic)
        public_capacity = self._get_public_capacity_to_target(target)
        capacity_btc = public_capacity / 100_000_000
        base_score = capacity_btc * (1 - result.hive_share_pct)

        # Apply competition discount
        adjusted_score = base_score * competition_factor

        # Apply bottleneck bonus
        if is_bottleneck:
            adjusted_score *= BOTTLENECK_BONUS_MULTIPLIER

        # DECISION LOGIC
        reasoning_parts = []
        recommendation_type = "open_channel"

        # Routing improvement scoring disabled — creating temporary askrene
        # layers with virtual channels triggers askrene crashes that take
        # down lightningd.  Re-enable when CLN fixes the askrene stability
        # issues (tracked in ElementsProject/lightning#9032).
        # routing_multiplier = self._score_routing_improvement(target, cfg)

        # Check 1: Majority coverage - sufficient coverage exists
        if hive_coverage_pct >= HIVE_COVERAGE_MAJORITY_PCT:
            recommendation_type = "no_action"
            adjusted_score = 0
            reasoning_parts.append(
                f"{members_with}/{total_members} hive members already have channels; "
                f"sufficient coverage exists"
            )

        # Check 2: High competition - deprioritize
        if competition_level in ["high", "very_high"]:
            reasoning_parts.append(
                f"Peer has {network_channels} channels (high competition); "
                f"score reduced by {int((1-competition_factor)*100)}%"
            )

        # Check 3: Bottleneck bonus
        if is_bottleneck:
            reasoning_parts.append(
                f"Peer is a common bottleneck for fleet liquidity; "
                f"score boosted by 50%"
            )

        # Check 4: Underserved check
        if result.hive_share_pct < UNDERSERVED_THRESHOLD_PCT:
            reasoning_parts.append(
                f"Hive share is {result.hive_share_pct:.1%} < {UNDERSERVED_THRESHOLD_PCT:.0%}; "
                f"underserved target"
            )

        if not reasoning_parts:
            reasoning_parts.append("Standard expansion candidate")

        return ExpansionRecommendation(
            target=target,
            recommendation_type=recommendation_type,
            score=adjusted_score,
            reasoning="; ".join(reasoning_parts),
            details={
                "hive_share_pct": result.hive_share_pct,
                "public_capacity_sats": public_capacity,
                "base_score": base_score,
                "competition_factor": competition_factor,
                "bottleneck_bonus": BOTTLENECK_BONUS_MULTIPLIER if is_bottleneck else 1.0
            },
            hive_coverage_pct=hive_coverage_pct,
            hive_members_count=members_with,
            network_channels=network_channels,
            is_bottleneck=is_bottleneck,
            competition_level=competition_level
        )

    # =========================================================================
    # NETWORK CACHE
    # =========================================================================

    def _refresh_network_cache(self, force: bool = False) -> bool:
        """
        Refresh the network channel cache from listchannels.

        Implements efficient caching to minimize RPC load.
        Deduplicates bidirectional channels (A->B and B->A counted once).

        Args:
            force: Force refresh even if cache is fresh

        Returns:
            True if cache was refreshed successfully, False on error
        """
        now = int(time.time())

        # Use cached data if still fresh
        if not force and (now - self._network_cache_time) < NETWORK_CACHE_TTL_SECONDS:
            return True

        if not self.plugin:
            self._log("Cannot refresh network cache: no plugin reference", level='warn')
            return False

        try:
            # Fetch all public channels
            result = self.plugin.rpc.listchannels()
            channels_raw = result.get('channels', [])

            # Build capacity map: target -> list of channels TO that target
            # Deduplicate: for bidirectional channels, count capacity once
            capacity_map: Dict[str, List[ChannelInfo]] = {}
            seen_pairs: Set[str] = set()

            for ch in channels_raw:
                source = ch.get('source', '')
                dest = ch.get('destination', '')
                scid = ch.get('short_channel_id', '')

                if not source or not dest or not scid:
                    continue

                # Parse capacity (may be int or dict with msat)
                capacity_field = 'amount_msat' if 'amount_msat' in ch else 'satoshis'
                capacity_raw = ch.get(capacity_field, 0)
                is_msat_field = 'msat' in capacity_field
                if isinstance(capacity_raw, dict):
                    capacity_sats = capacity_raw.get('msat', 0) // 1000
                elif isinstance(capacity_raw, str) and capacity_raw.endswith('msat'):
                    capacity_sats = int(capacity_raw[:-4]) // 1000
                elif isinstance(capacity_raw, int):
                    if is_msat_field:
                        capacity_sats = capacity_raw // 1000
                    else:
                        capacity_sats = capacity_raw
                else:
                    capacity_sats = 0

                active = ch.get('active', True)

                # Create normalized pair key for dedup (smaller pubkey first)
                pair_key = tuple(sorted([source, dest])) + (scid,)
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                info = ChannelInfo(
                    source=source,
                    destination=dest,
                    short_channel_id=scid,
                    capacity_sats=capacity_sats,
                    active=active
                )

                # Index by destination (target)
                if dest not in capacity_map:
                    capacity_map[dest] = []
                capacity_map[dest].append(info)

                # Also index by source (for bidirectional lookup)
                if source not in capacity_map:
                    capacity_map[source] = []
                capacity_map[source].append(info)

            self._network_cache = capacity_map
            self._network_cache_time = now

            self._log(f"Network cache refreshed: {len(seen_pairs)} channels, "
                     f"{len(capacity_map)} targets", level='debug')
            return True

        except RpcError as e:
            self._log(f"listchannels RPC failed: {e}", level='warn')
            return False
        except Exception as e:
            self._log(f"Network cache refresh error: {e}", level='warn')
            return False

    def _get_public_capacity_to_target(self, target: str) -> int:
        """
        Get total public network capacity to a target.

        Args:
            target: Target node pubkey

        Returns:
            Total capacity in satoshis (0 if not found)
        """
        channels = self.get_unique_channels_for(target)
        return sum(ch.capacity_sats for ch in channels if ch.active)

    # =========================================================================
    # NODE SUMMARY
    # =========================================================================

    def compute_node_summary(self) -> Optional[Dict[str, Any]]:
        """
        Compute a summary of this node's channel state.

        Returns a dict with channel counts, capacity, and underwater metrics,
        or None if the plugin is unavailable or RPC fails (fail-closed).

        Returns:
            Dict with keys: active_channels, pending_channels, closing_channels,
            total_capacity_sats, underwater_count, underwater_pct
            Or None on failure.
        """
        if self.plugin is None:
            return None

        # States considered "pending" (channel not yet usable)
        pending_states = {
            'CHANNELD_AWAITING_LOCKIN',
            'DUALOPEND_AWAITING_LOCKIN',
            'DUALOPEND_OPEN_INIT',
        }

        try:
            peer_channels = self.plugin.rpc.listpeerchannels()
            channels = peer_channels.get('channels', [])
        except Exception as e:
            self._log(f"compute_node_summary: listpeerchannels failed: {e}", level='warn')
            return None

        active = 0
        pending = 0
        closing = 0
        total_capacity_msat = 0
        we_opened = 0
        they_opened = 0

        for ch in channels:
            state = ch.get('state', '')
            if state == 'CHANNELD_NORMAL':
                active += 1
                total_capacity_msat += ch.get('total_msat', 0)
                opener = ch.get('opener', 'local')
                if opener == 'local':
                    we_opened += 1
                else:
                    they_opened += 1
            elif state in pending_states:
                pending += 1
            else:
                closing += 1

        total_capacity_sats = total_capacity_msat // 1000

        # Get underwater count from bridge (revenue-profitability)
        underwater_count = 0
        if self.bridge:
            try:
                prof_result = self.bridge.safe_call('revenue-profitability', {})
                if prof_result and isinstance(prof_result, dict):
                    prof_channels = prof_result.get('channels', [])
                    for pch in prof_channels:
                        if pch.get('profitability_class') in ('underwater', 'bleeder'):
                            underwater_count += 1
            except Exception:
                # Bridge failure is non-fatal; default to 0
                pass

        underwater_pct = round(underwater_count * 100.0 / active, 1) if active > 0 else 0.0

        return {
            'active_channels': active,
            'pending_channels': pending,
            'closing_channels': closing,
            'total_capacity_sats': total_capacity_sats,
            'underwater_count': underwater_count,
            'underwater_pct': underwater_pct,
            'we_opened': we_opened,
            'they_opened': they_opened,
        }

    # =========================================================================
    # CHANNEL DEDUP ACCESSOR
    # =========================================================================

    def get_unique_channels_for(self, target: str) -> List['ChannelInfo']:
        """
        Get deduplicated channels for a target from the network cache.

        The cache indexes each channel under both endpoints (source and dest).
        This method deduplicates by short_channel_id to prevent double-counting.

        Args:
            target: Target node pubkey

        Returns:
            List of unique ChannelInfo objects for this target
        """
        channels = self._network_cache.get(target, [])
        seen_scids: set = set()
        unique: list = []
        for ch in channels:
            if ch.short_channel_id not in seen_scids:
                seen_scids.add(ch.short_channel_id)
                unique.append(ch)
        return unique

    # =========================================================================
    # SATURATION LOGIC
    # =========================================================================

    def _get_hive_members(self) -> List[str]:
        """Get list of Hive member pubkeys."""
        if not self.db:
            return []
        members = self.db.get_all_members()
        return [m['peer_id'] for m in members]

    def _has_existing_or_pending_channel(self, target: str) -> Tuple[bool, Optional[str], Optional[int], Optional[str]]:
        """
        Check if we already have an existing or pending channel to this target.

        This prevents double-opening channels to the same peer when one is
        already in CHANNELD_AWAITING_LOCKIN state.

        Args:
            target: Target node pubkey

        Returns:
            Tuple of (has_channel, state, capacity_sats, opener)
            - has_channel: True if we have an active or pending channel
            - state: Channel state if found (e.g., 'CHANNELD_NORMAL', 'CHANNELD_AWAITING_LOCKIN')
            - capacity_sats: Channel capacity if found
            - opener: 'local' or 'remote' indicating who opened the channel
        """
        if not self.plugin:
            return (False, None, None, None)

        try:
            peer_channels = self.plugin.rpc.call("listpeerchannels", {"id": target})
            channels = peer_channels.get('channels', [])
            for ch in channels:
                state = ch.get('state', '')
                # Check for active or pending channels
                if state in ('CHANNELD_AWAITING_LOCKIN', 'CHANNELD_NORMAL',
                             'DUALOPEND_AWAITING_LOCKIN', 'DUALOPEND_OPEN_INIT'):
                    capacity_sats = ch.get('total_msat', 0) // 1000
                    opener = ch.get('opener', 'local')
                    return (True, state, capacity_sats, opener)
        except Exception:
            # If RPC fails, assume channel exists to prevent duplicate opens.
            return (True, None, None, None)

        return (False, None, None, None)

    def _get_hive_capacity_to_target(self, target: str, hive_members: List[str]) -> int:
        """
        Calculate total Hive capacity to a target.

        SECURITY: Clamps gossip-reported capacity to public listchannels maximum.
        This prevents attackers from inflating saturation via fake gossip.

        Args:
            target: Target node pubkey
            hive_members: List of Hive member pubkeys

        Returns:
            Total Hive capacity in satoshis (clamped to public reality)
        """
        if not self.state_manager:
            return 0

        # Get all known Hive peer states (list -> dict for lookup)
        all_states_list = self.state_manager.get_all_peer_states()
        all_states = {s.peer_id: s for s in all_states_list}

        # Get public capacity for reality check
        public_channels = self._network_cache.get(target, [])

        # Build map: (source, dest) -> max public capacity
        public_capacity_map: Dict[Tuple[str, str], int] = {}
        for ch in public_channels:
            key = (ch.source, ch.destination)
            public_capacity_map[key] = max(
                public_capacity_map.get(key, 0),
                ch.capacity_sats
            )
            # Also check reverse direction
            key_rev = (ch.destination, ch.source)
            public_capacity_map[key_rev] = max(
                public_capacity_map.get(key_rev, 0),
                ch.capacity_sats
            )

        total_hive_capacity = 0

        for member_pubkey in hive_members:
            state = all_states.get(member_pubkey)
            if not state:
                continue

            # Check if this member's topology includes the target
            topology = getattr(state, 'topology', []) or []
            if target not in topology:
                continue

            # Use verified public channel capacity (no gossip dependency)
            # Gossip capacity_sats is total hive capacity, not per-target,
            # so we use the public channel data directly.
            public_max = public_capacity_map.get((member_pubkey, target), 0)
            if public_max == 0:
                # Also try reverse
                public_max = public_capacity_map.get((target, member_pubkey), 0)

            if public_max > 0:
                total_hive_capacity += public_max

        return total_hive_capacity

    def _calculate_hive_share(self, target: str, cfg) -> SaturationResult:
        """
        Calculate Hive's market share for a target.

        Args:
            target: Target node pubkey
            cfg: Config snapshot for thresholds

        Returns:
            SaturationResult with share calculation
        """
        hive_members = self._get_hive_members()

        # Get public capacity (denominator)
        public_capacity = self._get_public_capacity_to_target(target)

        # Get Hive capacity (numerator, clamped)
        hive_capacity = self._get_hive_capacity_to_target(target, hive_members)

        # Calculate share
        if public_capacity <= 0:
            hive_share = 0.0
        else:
            hive_share = hive_capacity / public_capacity

        # Check saturation threshold
        is_saturated = hive_share >= getattr(cfg, 'market_share_cap_pct', 0.20)

        # Check release threshold (hysteresis)
        should_release = hive_share < SATURATION_RELEASE_THRESHOLD_PCT

        return SaturationResult(
            target=target,
            hive_capacity_sats=hive_capacity,
            public_capacity_sats=public_capacity,
            hive_share_pct=hive_share,
            is_saturated=is_saturated,
            should_release=should_release
        )

    def get_saturated_targets(self, cfg) -> List[SaturationResult]:
        """
        Get all targets where Hive exceeds market share cap.

        Args:
            cfg: Config snapshot

        Returns:
            List of SaturationResult for saturated targets
        """
        saturated = []

        # Check all known targets in network cache
        for target in self._network_cache.keys():
            # Skip targets below minimum capacity (anti-Sybil)
            public_capacity = self._get_public_capacity_to_target(target)
            if public_capacity < MIN_TARGET_CAPACITY_SATS:
                continue

            result = self._calculate_hive_share(target, cfg)
            if result.is_saturated:
                saturated.append(result)

        return saturated

    # =========================================================================
    # GUARD MECHANISM
    # =========================================================================

    def _enforce_saturation(self, cfg, run_id: str) -> List[Dict[str, Any]]:
        """
        Enforce saturation limits by recording saturated targets.

        SECURITY CONSTRAINTS:
        - Max 5 new saturation detections per cycle (abort if exceeded)
        - Idempotent: skip already-flagged peers
        - Log all decisions to hive_planner_log

        Args:
            cfg: Config snapshot
            run_id: Unique identifier for this cycle

        Returns:
            List of decision records for testing
        """
        decisions = []

        # Refresh network cache
        if not self._refresh_network_cache():
            self._log("Failed to refresh network cache, aborting saturation enforcement", level='warn')
            self.db.log_planner_action(
                action_type='saturation_check',
                result='failed',
                details={'reason': 'network_cache_refresh_failed', 'run_id': run_id}
            )
            return decisions

        # Get saturated targets
        saturated_targets = self.get_saturated_targets(cfg)

        # Count new ignores needed
        new_ignores_needed = []
        for result in saturated_targets:
            if result.target not in self._ignored_peers:
                new_ignores_needed.append(result)

        # SECURITY: Check rate limit
        if len(new_ignores_needed) > MAX_IGNORES_PER_CYCLE:
            self._log(
                f"Mass Saturation Detected: {len(new_ignores_needed)} targets exceed cap. "
                f"Aborting cycle (max {MAX_IGNORES_PER_CYCLE}/cycle).",
                level='warn'
            )
            self.db.log_planner_action(
                action_type='saturation_check',
                result='aborted',
                details={
                    'reason': 'mass_saturation_detected',
                    'targets_count': len(new_ignores_needed),
                    'max_allowed': MAX_IGNORES_PER_CYCLE,
                    'run_id': run_id
                }
            )
            decisions.append({
                'action': 'abort',
                'reason': 'mass_saturation_detected',
                'targets_count': len(new_ignores_needed)
            })
            return decisions

        # Issue ignores for new saturated targets
        ignores_issued = 0
        for result in new_ignores_needed:
            if ignores_issued >= MAX_IGNORES_PER_CYCLE:
                break

            # Record saturation for analytics and native expansion control
            self._ignored_peers.add(result.target)
            ignores_issued += 1

            self._log(
                f"Saturated target {result.target[:16]}... "
                f"(share={result.hive_share_pct:.1%})"
            )
            self.db.log_planner_action(
                action_type='saturation_detected',
                result='info',
                target=result.target,
                details={
                    'hive_share_pct': round(result.hive_share_pct, 4),
                    'hive_capacity_sats': result.hive_capacity_sats,
                    'public_capacity_sats': result.public_capacity_sats,
                    'run_id': run_id
                }
            )
            decisions.append({
                'action': 'saturation_detected',
                'target': result.target,
                'hive_share_pct': round(result.hive_share_pct, 4),
            })

        # Log summary
        self.db.log_planner_action(
            action_type='saturation_check',
            result='completed',
            details={
                'saturated_targets': len(saturated_targets),
                'new_ignores_issued': ignores_issued,
                'run_id': run_id
            }
        )

        return decisions

    def _release_saturation(self, cfg, run_id: str) -> List[Dict[str, Any]]:
        """
        Release ignores for targets that are no longer saturated.

        Uses hysteresis (15% threshold) to prevent flip-flopping.

        Args:
            cfg: Config snapshot
            run_id: Unique identifier for this cycle

        Returns:
            List of decision records
        """
        decisions = []

        # Check currently ignored peers
        peers_to_release = []
        for peer in list(self._ignored_peers):
            result = self._calculate_hive_share(peer, cfg)
            if result.should_release:
                peers_to_release.append((peer, result))

        # Release saturation flags
        for peer, result in peers_to_release:
            self._ignored_peers.discard(peer)

            self._log(
                f"Released saturation flag on {peer[:16]}... "
                f"(share={result.hive_share_pct:.1%} < {SATURATION_RELEASE_THRESHOLD_PCT:.0%})"
            )
            self.db.log_planner_action(
                action_type='saturation_released',
                result='success',
                target=peer,
                details={
                    'hive_share_pct': round(result.hive_share_pct, 4),
                    'run_id': run_id
                }
            )
            decisions.append({
                'action': 'saturation_released',
                'target': peer,
                'result': 'success'
            })

        return decisions

    # =========================================================================
    # EXPANSION LOGIC
    # =========================================================================

    def get_underserved_targets(self, cfg, include_low_quality: bool = False) -> List[UnderservedResult]:
        """
        Get targets with low Hive coverage that are candidates for expansion.

        Criteria:
        - Public capacity > MIN_TARGET_CAPACITY_SATS (1 BTC)
        - Hive share < UNDERSERVED_THRESHOLD_PCT (5%)
        - Target exists in public graph (verified via network cache)
        - Quality score >= MIN_QUALITY_SCORE (Phase 6.2) or insufficient data

        ENHANCED with cooperation module integration (Phase 7):
        - Filters out targets where majority of hive has channels
        - Applies competition discount for high-channel-count peers
        - Boosts bottleneck peers identified by liquidity_coordinator

        Args:
            cfg: Config snapshot
            include_low_quality: If True, include targets with low quality scores
                                 (they will be flagged but not filtered)

        Returns:
            List of UnderservedResult sorted by combined score (highest first)
        """
        underserved = []

        # Batch-fetch all our peer channels once (avoid O(n) RPC per target)
        existing_channel_peers: Set[str] = set()
        if self.plugin:
            try:
                all_peer_channels = self.plugin.rpc.listpeerchannels()
                for ch in all_peer_channels.get('channels', []):
                    state = ch.get('state', '')
                    if state in ('CHANNELD_NORMAL', 'CHANNELD_AWAITING_LOCKIN',
                                 'DUALOPEND_AWAITING_LOCKIN', 'DUALOPEND_OPEN_INIT'):
                        # Only skip peers where WE opened the channel.
                        # Remote-opened channels don't prevent expansion proposals.
                        if ch.get('opener', 'local') == 'local':
                            peer_id = ch.get('peer_id', '')
                            if peer_id:
                                existing_channel_peers.add(peer_id)
            except Exception as e:
                self._log(f"Batch listpeerchannels failed, falling back to empty set: {e}", level='debug')

        for target in self._network_cache.keys():
            # Check minimum capacity (anti-Sybil)
            public_capacity = self._get_public_capacity_to_target(target)
            if public_capacity < MIN_TARGET_CAPACITY_SATS:
                continue

            # Skip if we already have an existing or pending channel to this target
            if target in existing_channel_peers:
                self._log(
                    f"Skipping {target[:16]}... - already have active/pending channel",
                    level='debug'
                )
                continue

            # Skip if another hive member has a pending intent to open to this target
            if self.intent_manager:
                remote_intents = self.intent_manager.get_remote_intents(target=target)
                pending_opens = [i for i in remote_intents
                                 if i.intent_type == 'channel_open' and i.status == 'pending']
                if pending_opens:
                    initiator = pending_opens[0].initiator[:16] if pending_opens[0].initiator else 'unknown'
                    self._log(
                        f"Skipping {target[:16]}... - hive member {initiator}... "
                        f"has pending channel open intent",
                        level='debug'
                    )
                    continue

            # Calculate Hive share
            result = self._calculate_hive_share(target, cfg)

            # Check if underserved (< 5% Hive share)
            if result.hive_share_pct >= UNDERSERVED_THRESHOLD_PCT:
                continue

            # Phase 7: Check hive coverage diversity
            members_with, total_members = self._count_hive_members_with_target(target)
            hive_coverage_pct = members_with / total_members if total_members > 0 else 0

            # Skip if majority already has channels (diminishing returns)
            if hive_coverage_pct >= HIVE_COVERAGE_MAJORITY_PCT:
                self._log(
                    f"Skipping {target[:16]}... - {members_with}/{total_members} "
                    f"hive members already have channels ({hive_coverage_pct:.0%})",
                    level='debug'
                )
                continue

            # Phase 6.2: Get quality score for the target
            quality_score = 0.5  # Default neutral
            quality_confidence = 0.0
            quality_recommendation = "neutral"

            if self.quality_scorer:
                quality_result = self.quality_scorer.calculate_score(
                    target, days=QUALITY_SCORE_DAYS
                )
                quality_score = quality_result.overall_score
                quality_confidence = quality_result.confidence
                quality_recommendation = quality_result.recommendation

                # Filter out low-quality targets unless explicitly included
                if not include_low_quality:
                    # Skip targets with 'avoid' recommendation
                    if quality_recommendation == "avoid":
                        self._log(
                            f"Skipping {target[:16]}... - quality='avoid' "
                            f"(score={quality_score:.2f}, confidence={quality_confidence:.2f})",
                            level='debug'
                        )
                        continue

                    # Skip targets below minimum score with sufficient confidence
                    if quality_confidence >= 0.3 and quality_score < MIN_QUALITY_SCORE:
                        self._log(
                            f"Skipping {target[:16]}... - low quality score "
                            f"({quality_score:.2f} < {MIN_QUALITY_SCORE})",
                            level='debug'
                        )
                        continue

            # Calculate base score: higher capacity + lower Hive share = more attractive
            # Score = capacity_btc * (1 - hive_share)
            capacity_btc = public_capacity / 100_000_000
            base_score = capacity_btc * (1 - result.hive_share_pct)

            # Phase 7: Apply competition discount
            competition_factor, competition_level = self._calculate_competition_score(target)
            adjusted_score = base_score * competition_factor

            if competition_level in ["high", "very_high"]:
                self._log(
                    f"Discounting {target[:16]}... - high competition "
                    f"({self._get_target_channel_count(target)} channels, -{int((1-competition_factor)*100)}%)",
                    level='debug'
                )

            # Phase 7: Apply bottleneck bonus
            is_bottleneck = self._is_bottleneck_peer(target)
            if is_bottleneck:
                adjusted_score *= BOTTLENECK_BONUS_MULTIPLIER
                self._log(
                    f"Boosting {target[:16]}... - bottleneck peer (+50%)",
                    level='debug'
                )

            # Physarum/Slime mold: Boost high-value corridors
            # Prioritize routes where "nutrients" (fees) flow abundantly
            corridor_bonus, corridor_tier = self._get_corridor_value_bonus(target)
            if corridor_bonus > 1.0:
                adjusted_score *= corridor_bonus
                self._log(
                    f"Boosting {target[:16]}... - {corridor_tier} value corridor "
                    f"(+{int((corridor_bonus-1)*100)}%)",
                    level='debug'
                )

            # Phase 6.2: Factor in quality score
            # Combined score = adjusted_score * quality_multiplier
            # Quality multiplier ranges from 0.5 (avoid) to 1.5 (excellent)
            if quality_confidence > 0.3:
                # Quality data is meaningful - apply multiplier
                quality_multiplier = 0.5 + quality_score  # 0.5 to 1.5
            else:
                # Low confidence - use neutral multiplier
                quality_multiplier = 1.0

            combined_score = adjusted_score * quality_multiplier

            underserved.append(UnderservedResult(
                target=target,
                public_capacity_sats=public_capacity,
                hive_share_pct=result.hive_share_pct,
                score=combined_score,
                quality_score=quality_score,
                quality_confidence=quality_confidence,
                quality_recommendation=quality_recommendation,
            ))

        # Sort by combined score (highest first)
        underserved.sort(key=lambda r: r.score, reverse=True)
        return underserved

    def _get_local_onchain_balance(self) -> int:
        """
        Get local confirmed onchain balance.

        Returns:
            Confirmed balance in satoshis, or 0 on error
        """
        if not self.plugin:
            return 0

        try:
            funds = self.plugin.rpc.listfunds()
            outputs = funds.get('outputs', [])

            confirmed_sats = 0
            for output in outputs:
                # Only count confirmed outputs
                if output.get('status') == 'confirmed':
                    # Handle both msat and satoshi formats
                    amount = output.get('amount_msat')
                    if amount is not None:
                        if isinstance(amount, int):
                            confirmed_sats += amount // 1000
                        elif isinstance(amount, str) and amount.endswith('msat'):
                            confirmed_sats += int(amount[:-4]) // 1000
                    else:
                        # Fallback to value field
                        confirmed_sats += output.get('value', 0)

            return confirmed_sats
        except RpcError as e:
            self._log(f"listfunds RPC failed: {e}", level='warn')
            return 0
        except Exception as e:
            self._log(f"Error getting onchain balance: {e}", level='warn')
            return 0

    def _get_target_channel_count(self, target: str) -> int:
        """
        Get the number of channels a target node has.

        Uses the network cache to count unique channel partners.

        Args:
            target: Target node pubkey

        Returns:
            Number of channels the target has
        """
        if target not in self._network_cache:
            return 0

        # Count unique partners (each channel has source/destination)
        partners = set()
        for channel in self._network_cache.get(target, []):
            # Add both ends of each channel
            if channel.source == target:
                partners.add(channel.destination)
            else:
                partners.add(channel.source)

        return len(partners)

    def _get_avg_fee_rate(self) -> int:
        """
        Get average fee rate from cl-revenue-ops configuration.

        Queries the bridge for fee configuration and returns the midpoint
        of the configured fee range. Falls back to 500 ppm if unavailable.

        Returns:
            Fee rate in ppm (midpoint of configured range, or 500 default)
        """
        if not self.bridge:
            return 500

        try:
            fee_config = self.bridge.get_fee_config()
            if fee_config and 'midpoint_ppm' in fee_config:
                return fee_config['midpoint_ppm']
        except Exception:
            pass

        return 500  # Default if unavailable

    def _has_pending_intent(self, target: str) -> bool:
        """
        Check if there's already a pending intent for this target.

        Args:
            target: Target pubkey to check

        Returns:
            True if a pending intent exists, False otherwise
        """
        if not self.db:
            return False

        pending = self.db.get_pending_intents()
        for intent in pending:
            if intent.get('target') == target and intent.get('status') == 'pending':
                return True
        return False

    # =========================================================================
    # EXPANSION PROPOSALS
    # =========================================================================

    def _propose_expansions(self, cfg, run_id: str) -> List[Dict]:
        """
        Evaluate underserved targets and log expansion recommendations.

        Checks feerate gate, gets scored targets, and logs the top
        MAX_EXPANSIONS_PER_CYCLE recommendations as planner decisions.
        Does not open channels — cl-hive is hint-only.

        Returns:
            List of decision records.
        """
        decisions = []

        # Feerate gate: block expansions when on-chain fees are too high
        max_feerate = getattr(cfg, 'max_expansion_feerate_perkb', 5000)
        if max_feerate != 0 and self.plugin:
            try:
                feerates = self.plugin.rpc.feerates("perkb")
                opening = feerates.get("perkb", {}).get("opening")
                if opening is not None and opening > max_feerate:
                    self._log(
                        f"Expansion blocked: feerate {opening} > max {max_feerate} sat/kB",
                        level='info'
                    )
                    self.db.log_planner_action(
                        action_type='expansion_blocked',
                        result='feerate_too_high',
                        details={
                            'current_feerate': opening,
                            'max_feerate': max_feerate,
                            'run_id': run_id,
                        }
                    )
                    return decisions
            except Exception as e:
                self._log(f"Feerate check failed: {e}", level='debug')

        # Get scored underserved targets
        try:
            targets = self.get_underserved_targets(cfg)
        except Exception as e:
            self._log(f"Underserved target analysis failed: {e}", level='warn')
            return decisions

        if not targets:
            return decisions

        # Evaluate top targets (up to MAX_EXPANSIONS_PER_CYCLE)
        proposed = 0
        for target_result in targets:
            if proposed >= MAX_EXPANSIONS_PER_CYCLE:
                break

            # Skip targets with pending intents
            if self._has_pending_intent(target_result.target):
                continue

            # Skip ignored (saturated) targets
            if target_result.target in self._ignored_peers:
                continue

            try:
                rec = self.get_expansion_recommendation(target_result.target, cfg)
            except Exception:
                continue

            if rec.recommendation_type != "open_channel":
                continue

            # Quality gate
            if target_result.quality_score < MIN_QUALITY_SCORE and target_result.quality_recommendation == "avoid":
                self._log(
                    f"Expansion skipped for {target_result.target[:16]}: "
                    f"quality {target_result.quality_score:.2f} < {MIN_QUALITY_SCORE}",
                    level='debug'
                )
                continue

            decision = {
                'action_type': 'expansion_proposed',
                'target': target_result.target,
                'score': round(rec.score, 4),
                'reasoning': rec.reasoning,
                'competition_level': rec.competition_level,
                'hive_coverage_pct': round(rec.hive_coverage_pct * 100, 1),
                'quality_score': round(target_result.quality_score, 3),
            }
            decisions.append(decision)

            self.db.log_planner_action(
                action_type='expansion_proposed',
                result='proposed',
                target=target_result.target,
                details={
                    'score': round(rec.score, 4),
                    'reasoning': rec.reasoning,
                    'competition_level': rec.competition_level,
                    'hive_coverage_pct': round(rec.hive_coverage_pct * 100, 1),
                    'network_channels': rec.network_channels,
                    'quality_score': round(target_result.quality_score, 3),
                    'run_id': run_id,
                }
            )

            self._log(
                f"Expansion proposed: {target_result.target[:16]}... "
                f"(score={rec.score:.1f}, competition={rec.competition_level}, "
                f"coverage={rec.hive_coverage_pct:.0%})",
                level='info'
            )
            proposed += 1

        return decisions

    # =========================================================================
    # RUN CYCLE
    # =========================================================================

    def run_cycle(self, cfg, *, shutdown_event=None, now=None, run_id=None) -> List[Dict]:
        """
        Execute one planning cycle.

        This is the main entry point called by the planner_loop thread.
        No sleeping inside this method - caller handles timing.

        Args:
            cfg: Config snapshot (use config.snapshot() at cycle start)
            shutdown_event: Threading event to check for shutdown
            now: Current timestamp (for testing)
            run_id: Unique identifier for this cycle

        Returns:
            List of decision records for testing
        """
        if shutdown_event and shutdown_event.is_set():
            return []

        if now is None:
            now = int(time.time())
        if run_id is None:
            run_id = secrets.token_hex(8)

        self._log(f"Starting planner cycle (run_id={run_id})")
        decisions = []

        try:
            # Refresh network cache first
            if not self._refresh_network_cache(force=True):
                self._log("Network cache refresh failed, skipping cycle", level='warn')
                self.db.log_planner_action(
                    action_type='cycle',
                    result='failed',
                    details={'reason': 'cache_refresh_failed', 'run_id': run_id}
                )
                return []

            # Enforce saturation limits (Guard mechanism)
            saturation_decisions = self._enforce_saturation(cfg, run_id)
            decisions.extend(saturation_decisions)

            # Release over-ignored peers (best effort)
            release_decisions = self._release_saturation(cfg, run_id)
            decisions.extend(release_decisions)

            # Propose expansions to underserved targets
            expansion_decisions = self._propose_expansions(cfg, run_id)
            decisions.extend(expansion_decisions)

            self._log(f"Planner cycle complete (run_id={run_id}): {len(decisions)} decisions")
            self.db.log_planner_action(
                action_type='cycle',
                result='completed',
                details={
                    'decisions_count': len(decisions),
                    'run_id': run_id
                }
            )

        except Exception as e:
            self._log(f"Planner cycle error: {e}", level='warn')
            self.db.log_planner_action(
                action_type='cycle',
                result='error',
                details={'error': str(e), 'run_id': run_id}
            )

        return decisions

    # =========================================================================
    # STATISTICS
    # =========================================================================

    def get_planner_stats(self) -> Dict[str, Any]:
        """Get current planner statistics."""
        return {
            'network_cache_size': len(self._network_cache),
            'network_cache_age_seconds': int(time.time()) - self._network_cache_time,
            'ignored_peers_count': len(self._ignored_peers),
            'ignored_peers': list(self._ignored_peers)[:10],  # Limit for display
            'max_ignores_per_cycle': MAX_IGNORES_PER_CYCLE,
            'saturation_release_threshold_pct': SATURATION_RELEASE_THRESHOLD_PCT,
            'min_target_capacity_sats': MIN_TARGET_CAPACITY_SATS,
        }
