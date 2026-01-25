"""
Anticipatory Liquidity Module (Phase 7.1)

Predicts liquidity needs before they occur using temporal pattern recognition.
Like mycelium nutrient pre-positioning - move resources to where they'll be
needed before the demand spike.

Key Features:
- Time-of-day pattern detection (hour 0-23)
- Day-of-week pattern detection (Mon-Sun)
- Predictive rebalancing recommendations
- Fleet-wide coordination to avoid competing for same routes

This module is INFORMATION ONLY - it recommends actions but doesn't execute.
Actual rebalancing is done by cl-revenue-ops based on these predictions.

Author: Lightning Goats Team
"""

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .database import HiveDatabase


# =============================================================================
# CONSTANTS
# =============================================================================

# Pattern detection settings
PATTERN_WINDOW_DAYS = 14              # Days of history to analyze
MIN_PATTERN_SAMPLES = 10              # Minimum observations for confidence
PATTERN_CONFIDENCE_THRESHOLD = 0.60   # Minimum confidence to act on pattern
PATTERN_STRENGTH_THRESHOLD = 1.3      # 30% above average = significant pattern

# Prediction settings
PREDICTION_HORIZONS = [6, 12, 24]     # Hours to look ahead
DEFAULT_PREDICTION_HOURS = 12         # Default prediction window

# Urgency thresholds
URGENT_HOURS_THRESHOLD = 6            # <6 hours = urgent
PREEMPTIVE_HOURS_THRESHOLD = 24       # 6-24 hours = preemptive window
DEPLETION_PCT_THRESHOLD = 0.20        # <20% local = depletion risk
SATURATION_PCT_THRESHOLD = 0.80       # >80% local = saturation risk

# Fleet coordination
MAX_PREDICTIONS_PER_CHANNEL = 5       # Max predictions cached per channel
PREDICTION_STALE_HOURS = 1            # Refresh predictions hourly


# =============================================================================
# ENUMS
# =============================================================================

class FlowDirection(Enum):
    """Direction of liquidity flow."""
    INBOUND = "inbound"      # Receiving liquidity
    OUTBOUND = "outbound"    # Losing liquidity
    BALANCED = "balanced"    # Roughly equal


class PredictionUrgency(Enum):
    """Urgency level for rebalancing action."""
    CRITICAL = "critical"      # <6 hours to depletion
    URGENT = "urgent"          # 6-12 hours
    PREEMPTIVE = "preemptive"  # 12-24 hours (ideal window)
    LOW = "low"                # >24 hours
    NONE = "none"              # No action needed


class RecommendedAction(Enum):
    """Recommended action based on prediction."""
    PREEMPTIVE_REBALANCE = "preemptive_rebalance"
    FEE_ADJUSTMENT = "fee_adjustment"
    MONITOR = "monitor"
    NO_ACTION = "no_action"


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class TemporalPattern:
    """
    Detected time-based flow pattern.

    Patterns indicate when a channel typically experiences high inbound
    or outbound flow, enabling predictive positioning.
    """
    channel_id: str
    hour_of_day: int              # 0-23 (None if day pattern)
    day_of_week: Optional[int]    # 0-6 (Mon-Sun), None if hour-only
    direction: FlowDirection
    intensity: float              # Relative intensity (1.0 = average)
    confidence: float             # Pattern reliability (0.0-1.0)
    samples: int                  # Number of observations
    avg_flow_sats: int            # Average flow in this window
    detected_at: int = 0         # Timestamp of detection

    def __post_init__(self):
        if self.detected_at == 0:
            self.detected_at = int(time.time())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "channel_id": self.channel_id,
            "hour_of_day": self.hour_of_day,
            "day_of_week": self.day_of_week,
            "day_name": self._day_name(),
            "direction": self.direction.value,
            "intensity": round(self.intensity, 2),
            "confidence": round(self.confidence, 2),
            "samples": self.samples,
            "avg_flow_sats": self.avg_flow_sats,
            "detected_at": self.detected_at
        }

    def _day_name(self) -> Optional[str]:
        """Get day name from day_of_week."""
        if self.day_of_week is None:
            return None
        days = ["Monday", "Tuesday", "Wednesday", "Thursday",
                "Friday", "Saturday", "Sunday"]
        return days[self.day_of_week] if 0 <= self.day_of_week <= 6 else None


@dataclass
class LiquidityPrediction:
    """
    Prediction of future liquidity state for a channel.

    Used to recommend preemptive rebalancing before depletion or saturation.
    """
    channel_id: str
    peer_id: str
    current_local_pct: float
    predicted_local_pct: float
    hours_ahead: int
    velocity_pct_per_hour: float

    # Risk assessment
    depletion_risk: float         # 0.0-1.0, higher = more likely to deplete
    saturation_risk: float        # 0.0-1.0, higher = more likely to saturate
    hours_to_critical: Optional[float]  # Hours until depletion/saturation

    # Recommendation
    recommended_action: RecommendedAction
    urgency: PredictionUrgency
    confidence: float

    # Pattern match
    pattern_match: Optional[str]  # Name of matched pattern
    pattern_intensity: float = 1.0

    # Timestamps
    predicted_at: int = 0

    def __post_init__(self):
        if self.predicted_at == 0:
            self.predicted_at = int(time.time())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "channel_id": self.channel_id,
            "peer_id": self.peer_id,
            "current_local_pct": round(self.current_local_pct, 3),
            "predicted_local_pct": round(self.predicted_local_pct, 3),
            "hours_ahead": self.hours_ahead,
            "velocity_pct_per_hour": round(self.velocity_pct_per_hour, 4),
            "depletion_risk": round(self.depletion_risk, 2),
            "saturation_risk": round(self.saturation_risk, 2),
            "hours_to_critical": round(self.hours_to_critical, 1) if self.hours_to_critical else None,
            "recommended_action": self.recommended_action.value,
            "urgency": self.urgency.value,
            "confidence": round(self.confidence, 2),
            "pattern_match": self.pattern_match,
            "pattern_intensity": round(self.pattern_intensity, 2),
            "predicted_at": self.predicted_at
        }


@dataclass
class FleetAnticipation:
    """
    Fleet-wide anticipatory positioning recommendation.

    Coordinates predictions across members to avoid competing for
    the same rebalance routes.
    """
    target_peer: str
    members_predicting_depletion: List[str]
    members_predicting_saturation: List[str]
    recommended_coordinator: str      # Member best positioned to act
    total_predicted_demand_sats: int
    coordination_window_hours: int
    recommendation: str
    timestamp: int = 0

    def __post_init__(self):
        if self.timestamp == 0:
            self.timestamp = int(time.time())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "target_peer": self.target_peer[:16] + "..." if len(self.target_peer) > 16 else self.target_peer,
            "members_predicting_depletion": len(self.members_predicting_depletion),
            "members_predicting_saturation": len(self.members_predicting_saturation),
            "recommended_coordinator": self.recommended_coordinator[:16] + "..." if self.recommended_coordinator else None,
            "total_predicted_demand_sats": self.total_predicted_demand_sats,
            "coordination_window_hours": self.coordination_window_hours,
            "recommendation": self.recommendation,
            "timestamp": self.timestamp
        }


@dataclass
class HourlyFlowSample:
    """Single hourly flow observation for pattern building."""
    channel_id: str
    hour: int               # 0-23
    day_of_week: int        # 0-6
    inbound_sats: int
    outbound_sats: int
    net_flow_sats: int      # inbound - outbound (positive = receiving)
    timestamp: int


# =============================================================================
# ANTICIPATORY LIQUIDITY MANAGER
# =============================================================================

class AnticipatoryLiquidityManager:
    """
    Predicts liquidity needs before they occur.

    Like mycelium nutrient pre-positioning - move resources to where
    they'll be needed before the demand spike.

    Key capabilities:
    1. Temporal pattern detection (hour/day cycles)
    2. Flow velocity prediction
    3. Preemptive rebalancing recommendations
    4. Fleet-wide coordination to avoid competition

    Usage:
        manager = AnticipatoryLiquidityManager(database, plugin)

        # Detect patterns from history
        patterns = manager.detect_patterns(channel_id)

        # Get prediction for next 12 hours
        prediction = manager.predict_liquidity(channel_id, hours=12)

        # Get fleet-wide recommendations
        fleet_recs = manager.get_fleet_recommendations()
    """

    def __init__(
        self,
        database: 'HiveDatabase',
        plugin=None,
        state_manager=None,
        our_id: str = None
    ):
        """
        Initialize the AnticipatoryLiquidityManager.

        Args:
            database: HiveDatabase instance for pattern storage
            plugin: Plugin instance for RPC and logging
            state_manager: StateManager for fleet state queries
            our_id: Our node's pubkey
        """
        self.database = database
        self.plugin = plugin
        self.state_manager = state_manager
        self.our_id = our_id

        # In-memory caches
        self._pattern_cache: Dict[str, List[TemporalPattern]] = {}
        self._prediction_cache: Dict[str, LiquidityPrediction] = {}
        self._flow_history: Dict[str, List[HourlyFlowSample]] = defaultdict(list)

        # Cache timestamps
        self._pattern_cache_time: Dict[str, int] = {}
        self._last_analysis_time: int = 0

    def _log(self, message: str, level: str = "debug") -> None:
        """Log a message if plugin is available."""
        if self.plugin:
            self.plugin.log(f"ANTICIPATORY: {message}", level=level)

    def _get_our_id(self) -> str:
        """Get our node's pubkey."""
        if self.our_id:
            return self.our_id
        if self.plugin:
            try:
                return self.plugin.rpc.getinfo().get("id", "")
            except Exception:
                pass
        return ""

    # =========================================================================
    # FLOW DATA RECORDING
    # =========================================================================

    def record_flow_sample(
        self,
        channel_id: str,
        inbound_sats: int,
        outbound_sats: int,
        timestamp: int = None
    ) -> None:
        """
        Record a flow observation for pattern building.

        Should be called periodically (e.g., hourly) to build flow history.

        Args:
            channel_id: Channel SCID
            inbound_sats: Satoshis received in this period
            outbound_sats: Satoshis sent in this period
            timestamp: Observation timestamp (defaults to now)
        """
        ts = timestamp or int(time.time())
        dt = datetime.fromtimestamp(ts)

        sample = HourlyFlowSample(
            channel_id=channel_id,
            hour=dt.hour,
            day_of_week=dt.weekday(),
            inbound_sats=inbound_sats,
            outbound_sats=outbound_sats,
            net_flow_sats=inbound_sats - outbound_sats,
            timestamp=ts
        )

        # Add to in-memory history
        self._flow_history[channel_id].append(sample)

        # Trim old samples (keep PATTERN_WINDOW_DAYS)
        cutoff = ts - (PATTERN_WINDOW_DAYS * 24 * 3600)
        self._flow_history[channel_id] = [
            s for s in self._flow_history[channel_id]
            if s.timestamp > cutoff
        ]

        # Persist to database
        self._persist_flow_sample(sample)

    def _persist_flow_sample(self, sample: HourlyFlowSample) -> None:
        """Persist flow sample to database."""
        try:
            self.database.record_flow_sample(
                channel_id=sample.channel_id,
                hour=sample.hour,
                day_of_week=sample.day_of_week,
                inbound_sats=sample.inbound_sats,
                outbound_sats=sample.outbound_sats,
                net_flow_sats=sample.net_flow_sats,
                timestamp=sample.timestamp
            )
        except Exception as e:
            self._log(f"Failed to persist flow sample: {e}", level="debug")

    def load_flow_history(self, channel_id: str) -> List[HourlyFlowSample]:
        """
        Load flow history from database.

        Args:
            channel_id: Channel SCID

        Returns:
            List of historical flow samples
        """
        try:
            rows = self.database.get_flow_samples(
                channel_id=channel_id,
                days=PATTERN_WINDOW_DAYS
            )

            samples = []
            for row in rows:
                samples.append(HourlyFlowSample(
                    channel_id=row["channel_id"],
                    hour=row["hour"],
                    day_of_week=row["day_of_week"],
                    inbound_sats=row["inbound_sats"],
                    outbound_sats=row["outbound_sats"],
                    net_flow_sats=row["net_flow_sats"],
                    timestamp=row["timestamp"]
                ))

            # Update in-memory cache
            self._flow_history[channel_id] = samples
            return samples

        except Exception as e:
            self._log(f"Failed to load flow history: {e}", level="debug")
            return self._flow_history.get(channel_id, [])

    # =========================================================================
    # PATTERN DETECTION
    # =========================================================================

    def detect_patterns(
        self,
        channel_id: str,
        force_refresh: bool = False
    ) -> List[TemporalPattern]:
        """
        Detect temporal patterns in channel flow.

        Analyzes historical flow data to find recurring patterns by:
        - Hour of day (e.g., "high outbound 14:00-17:00 UTC")
        - Day of week (e.g., "high inbound on weekends")
        - Combined patterns (e.g., "Monday mornings drain")

        Args:
            channel_id: Channel SCID
            force_refresh: Force recalculation even if cached

        Returns:
            List of detected TemporalPattern objects
        """
        now = int(time.time())

        # Check cache
        if not force_refresh and channel_id in self._pattern_cache:
            cache_age = now - self._pattern_cache_time.get(channel_id, 0)
            if cache_age < PREDICTION_STALE_HOURS * 3600:
                return self._pattern_cache[channel_id]

        # Load history
        samples = self.load_flow_history(channel_id)
        if len(samples) < MIN_PATTERN_SAMPLES:
            self._log(
                f"Insufficient samples for {channel_id[:12]}... "
                f"({len(samples)} < {MIN_PATTERN_SAMPLES})",
                level="debug"
            )
            return []

        patterns = []

        # Detect hourly patterns
        hourly_patterns = self._detect_hourly_patterns(channel_id, samples)
        patterns.extend(hourly_patterns)

        # Detect daily patterns
        daily_patterns = self._detect_daily_patterns(channel_id, samples)
        patterns.extend(daily_patterns)

        # Detect combined patterns (specific hours on specific days)
        combined_patterns = self._detect_combined_patterns(channel_id, samples)
        patterns.extend(combined_patterns)

        # Cache results
        self._pattern_cache[channel_id] = patterns
        self._pattern_cache_time[channel_id] = now

        self._log(
            f"Detected {len(patterns)} patterns for {channel_id[:12]}... "
            f"from {len(samples)} samples",
            level="debug"
        )

        return patterns

    def _detect_hourly_patterns(
        self,
        channel_id: str,
        samples: List[HourlyFlowSample]
    ) -> List[TemporalPattern]:
        """
        Detect hour-of-day patterns.

        Identifies hours with significantly above-average flow in either direction.
        """
        patterns = []

        # Group by hour
        hourly_flows: Dict[int, List[int]] = defaultdict(list)
        for sample in samples:
            hourly_flows[sample.hour].append(sample.net_flow_sats)

        # Calculate overall average
        all_flows = [s.net_flow_sats for s in samples]
        if not all_flows:
            return patterns

        overall_avg = sum(abs(f) for f in all_flows) / len(all_flows)
        if overall_avg == 0:
            return patterns

        # Find significant deviations
        for hour, flows in hourly_flows.items():
            if len(flows) < 3:  # Need at least 3 samples per hour
                continue

            avg_flow = sum(flows) / len(flows)
            avg_magnitude = sum(abs(f) for f in flows) / len(flows)

            # Determine direction
            if avg_flow > 0:
                direction = FlowDirection.INBOUND
            elif avg_flow < 0:
                direction = FlowDirection.OUTBOUND
            else:
                direction = FlowDirection.BALANCED

            # Calculate intensity (relative to overall)
            intensity = avg_magnitude / overall_avg if overall_avg > 0 else 1.0

            # Calculate confidence based on consistency
            if avg_magnitude > 0:
                consistency = 1.0 - (
                    sum(abs(f - avg_flow) for f in flows) /
                    (len(flows) * avg_magnitude)
                )
            else:
                consistency = 0.0

            confidence = min(1.0, max(0.0, consistency * (len(flows) / MIN_PATTERN_SAMPLES)))

            # Only keep significant patterns
            if intensity >= PATTERN_STRENGTH_THRESHOLD and confidence >= PATTERN_CONFIDENCE_THRESHOLD:
                patterns.append(TemporalPattern(
                    channel_id=channel_id,
                    hour_of_day=hour,
                    day_of_week=None,  # All days
                    direction=direction,
                    intensity=intensity,
                    confidence=confidence,
                    samples=len(flows),
                    avg_flow_sats=int(abs(avg_flow))
                ))

        return patterns

    def _detect_daily_patterns(
        self,
        channel_id: str,
        samples: List[HourlyFlowSample]
    ) -> List[TemporalPattern]:
        """
        Detect day-of-week patterns.

        Identifies days with significantly different flow than average.
        """
        patterns = []

        # Group by day of week
        daily_flows: Dict[int, List[int]] = defaultdict(list)
        for sample in samples:
            daily_flows[sample.day_of_week].append(sample.net_flow_sats)

        # Calculate overall average
        all_flows = [s.net_flow_sats for s in samples]
        if not all_flows:
            return patterns

        overall_avg = sum(abs(f) for f in all_flows) / len(all_flows)
        if overall_avg == 0:
            return patterns

        # Find significant deviations
        for day, flows in daily_flows.items():
            if len(flows) < 5:  # Need at least 5 samples per day
                continue

            avg_flow = sum(flows) / len(flows)
            avg_magnitude = sum(abs(f) for f in flows) / len(flows)

            # Determine direction
            if avg_flow > 0:
                direction = FlowDirection.INBOUND
            elif avg_flow < 0:
                direction = FlowDirection.OUTBOUND
            else:
                direction = FlowDirection.BALANCED

            # Calculate intensity
            intensity = avg_magnitude / overall_avg if overall_avg > 0 else 1.0

            # Calculate confidence
            if avg_magnitude > 0:
                consistency = 1.0 - (
                    sum(abs(f - avg_flow) for f in flows) /
                    (len(flows) * avg_magnitude)
                )
            else:
                consistency = 0.0

            confidence = min(1.0, max(0.0, consistency * (len(flows) / 20)))

            # Only keep significant patterns
            if intensity >= PATTERN_STRENGTH_THRESHOLD and confidence >= PATTERN_CONFIDENCE_THRESHOLD:
                patterns.append(TemporalPattern(
                    channel_id=channel_id,
                    hour_of_day=None,  # All hours
                    day_of_week=day,
                    direction=direction,
                    intensity=intensity,
                    confidence=confidence,
                    samples=len(flows),
                    avg_flow_sats=int(abs(avg_flow))
                ))

        return patterns

    def _detect_combined_patterns(
        self,
        channel_id: str,
        samples: List[HourlyFlowSample]
    ) -> List[TemporalPattern]:
        """
        Detect combined hour+day patterns.

        Identifies specific time slots (e.g., "Monday 9am") with strong patterns.
        """
        patterns = []

        # Group by (day, hour)
        slot_flows: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        for sample in samples:
            key = (sample.day_of_week, sample.hour)
            slot_flows[key].append(sample.net_flow_sats)

        # Calculate overall average
        all_flows = [s.net_flow_sats for s in samples]
        if not all_flows:
            return patterns

        overall_avg = sum(abs(f) for f in all_flows) / len(all_flows)
        if overall_avg == 0:
            return patterns

        # Find significant deviations (need at least 2 samples per slot)
        for (day, hour), flows in slot_flows.items():
            if len(flows) < 2:
                continue

            avg_flow = sum(flows) / len(flows)
            avg_magnitude = sum(abs(f) for f in flows) / len(flows)

            # Determine direction
            if avg_flow > 0:
                direction = FlowDirection.INBOUND
            elif avg_flow < 0:
                direction = FlowDirection.OUTBOUND
            else:
                continue  # Skip balanced slots

            # Calculate intensity (must be significantly higher)
            intensity = avg_magnitude / overall_avg if overall_avg > 0 else 1.0

            # Combined patterns need higher threshold
            if intensity < PATTERN_STRENGTH_THRESHOLD * 1.5:
                continue

            # Confidence is lower due to fewer samples
            confidence = min(0.8, len(flows) / 4)  # Cap at 0.8 for combined

            patterns.append(TemporalPattern(
                channel_id=channel_id,
                hour_of_day=hour,
                day_of_week=day,
                direction=direction,
                intensity=intensity,
                confidence=confidence,
                samples=len(flows),
                avg_flow_sats=int(abs(avg_flow))
            ))

        return patterns

    # =========================================================================
    # PREDICTION
    # =========================================================================

    def predict_liquidity(
        self,
        channel_id: str,
        hours_ahead: int = DEFAULT_PREDICTION_HOURS,
        current_local_pct: float = None,
        capacity_sats: int = None,
        peer_id: str = None
    ) -> Optional[LiquidityPrediction]:
        """
        Predict liquidity state N hours from now.

        Combines:
        1. Current velocity (point-in-time extrapolation)
        2. Temporal patterns (historical cycles)
        3. Recent trend analysis

        Args:
            channel_id: Channel SCID
            hours_ahead: Hours to predict ahead
            current_local_pct: Current local balance percentage (0.0-1.0)
            capacity_sats: Channel capacity in satoshis
            peer_id: Peer pubkey

        Returns:
            LiquidityPrediction or None if insufficient data
        """
        # Get current state if not provided
        if current_local_pct is None or capacity_sats is None:
            channel_info = self._get_channel_info(channel_id)
            if not channel_info:
                return None
            current_local_pct = channel_info.get("local_pct", 0.5)
            capacity_sats = channel_info.get("capacity_sats", 0)
            peer_id = peer_id or channel_info.get("peer_id", "")

        # Get patterns
        patterns = self.detect_patterns(channel_id)

        # Find matching pattern for prediction window
        target_time = datetime.fromtimestamp(time.time() + hours_ahead * 3600)
        target_hour = target_time.hour
        target_day = target_time.weekday()

        matched_pattern = self._find_best_pattern_match(
            patterns, target_hour, target_day
        )

        # Calculate base velocity from recent samples
        base_velocity = self._calculate_velocity(channel_id, capacity_sats)

        # Adjust velocity based on pattern
        if matched_pattern and matched_pattern.confidence >= PATTERN_CONFIDENCE_THRESHOLD:
            # Pattern indicates stronger flow expected
            if matched_pattern.direction == FlowDirection.OUTBOUND:
                adjusted_velocity = base_velocity - (
                    matched_pattern.intensity * abs(base_velocity) * 0.5
                )
            elif matched_pattern.direction == FlowDirection.INBOUND:
                adjusted_velocity = base_velocity + (
                    matched_pattern.intensity * abs(base_velocity) * 0.5
                )
            else:
                adjusted_velocity = base_velocity

            pattern_name = self._pattern_name(matched_pattern)
            pattern_intensity = matched_pattern.intensity
            confidence = matched_pattern.confidence
        else:
            adjusted_velocity = base_velocity
            pattern_name = None
            pattern_intensity = 1.0
            confidence = 0.5  # Lower confidence without pattern match

        # Project forward
        predicted_local_pct = current_local_pct + (adjusted_velocity * hours_ahead)
        predicted_local_pct = max(0.0, min(1.0, predicted_local_pct))

        # Calculate risks
        depletion_risk = self._calculate_depletion_risk(
            current_local_pct, predicted_local_pct, adjusted_velocity
        )
        saturation_risk = self._calculate_saturation_risk(
            current_local_pct, predicted_local_pct, adjusted_velocity
        )

        # Calculate hours to critical
        hours_to_critical = self._hours_to_critical(
            current_local_pct, adjusted_velocity
        )

        # Determine urgency and action
        urgency = self._determine_urgency(hours_to_critical, depletion_risk, saturation_risk)
        action = self._determine_action(urgency, depletion_risk, saturation_risk)

        prediction = LiquidityPrediction(
            channel_id=channel_id,
            peer_id=peer_id or "",
            current_local_pct=current_local_pct,
            predicted_local_pct=predicted_local_pct,
            hours_ahead=hours_ahead,
            velocity_pct_per_hour=adjusted_velocity,
            depletion_risk=depletion_risk,
            saturation_risk=saturation_risk,
            hours_to_critical=hours_to_critical,
            recommended_action=action,
            urgency=urgency,
            confidence=confidence,
            pattern_match=pattern_name,
            pattern_intensity=pattern_intensity
        )

        # Cache prediction
        self._prediction_cache[channel_id] = prediction

        return prediction

    def _find_best_pattern_match(
        self,
        patterns: List[TemporalPattern],
        target_hour: int,
        target_day: int
    ) -> Optional[TemporalPattern]:
        """
        Find the best matching pattern for a target time.

        Priority:
        1. Exact hour+day match
        2. Hour match (any day)
        3. Day match (any hour)
        """
        best_match = None
        best_score = 0

        for pattern in patterns:
            score = 0

            # Check hour match
            if pattern.hour_of_day is not None:
                if pattern.hour_of_day == target_hour:
                    score += 2
                else:
                    continue  # Hour specified but doesn't match

            # Check day match
            if pattern.day_of_week is not None:
                if pattern.day_of_week == target_day:
                    score += 1
                else:
                    continue  # Day specified but doesn't match

            # Weight by confidence
            weighted_score = score * pattern.confidence

            if weighted_score > best_score:
                best_score = weighted_score
                best_match = pattern

        return best_match

    def _calculate_velocity(
        self,
        channel_id: str,
        capacity_sats: int
    ) -> float:
        """
        Calculate balance velocity (% change per hour) from recent samples.
        """
        samples = self._flow_history.get(channel_id, [])
        if len(samples) < 2 or capacity_sats == 0:
            return 0.0

        # Use last 24 hours of samples
        cutoff = int(time.time()) - 24 * 3600
        recent = [s for s in samples if s.timestamp > cutoff]

        if len(recent) < 2:
            return 0.0

        # Calculate net flow
        total_net = sum(s.net_flow_sats for s in recent)
        hours = (recent[-1].timestamp - recent[0].timestamp) / 3600

        if hours == 0:
            return 0.0

        # Convert to percentage per hour
        flow_per_hour = total_net / hours
        velocity_pct = flow_per_hour / capacity_sats

        return velocity_pct

    def _calculate_depletion_risk(
        self,
        current_pct: float,
        predicted_pct: float,
        velocity: float
    ) -> float:
        """Calculate risk of channel depletion (0.0-1.0)."""
        # Base risk from current level
        if current_pct <= DEPLETION_PCT_THRESHOLD:
            base_risk = 0.8
        elif current_pct <= DEPLETION_PCT_THRESHOLD * 1.5:
            base_risk = 0.5
        elif current_pct <= DEPLETION_PCT_THRESHOLD * 2:
            base_risk = 0.2
        else:
            base_risk = 0.0

        # Velocity risk (negative velocity = depleting)
        if velocity < -0.01:  # >1% per hour outbound
            velocity_risk = 0.8
        elif velocity < -0.005:
            velocity_risk = 0.5
        elif velocity < 0:
            velocity_risk = 0.2
        else:
            velocity_risk = 0.0

        # Predicted state risk
        if predicted_pct <= DEPLETION_PCT_THRESHOLD:
            predicted_risk = 0.9
        elif predicted_pct <= DEPLETION_PCT_THRESHOLD * 1.5:
            predicted_risk = 0.5
        else:
            predicted_risk = 0.1

        # Combine risks
        combined = max(base_risk, velocity_risk * 0.8, predicted_risk * 0.7)
        return min(1.0, combined)

    def _calculate_saturation_risk(
        self,
        current_pct: float,
        predicted_pct: float,
        velocity: float
    ) -> float:
        """Calculate risk of channel saturation (0.0-1.0)."""
        # Base risk from current level
        if current_pct >= SATURATION_PCT_THRESHOLD:
            base_risk = 0.8
        elif current_pct >= SATURATION_PCT_THRESHOLD - 0.1:
            base_risk = 0.5
        elif current_pct >= SATURATION_PCT_THRESHOLD - 0.2:
            base_risk = 0.2
        else:
            base_risk = 0.0

        # Velocity risk (positive velocity = saturating)
        if velocity > 0.01:  # >1% per hour inbound
            velocity_risk = 0.8
        elif velocity > 0.005:
            velocity_risk = 0.5
        elif velocity > 0:
            velocity_risk = 0.2
        else:
            velocity_risk = 0.0

        # Predicted state risk
        if predicted_pct >= SATURATION_PCT_THRESHOLD:
            predicted_risk = 0.9
        elif predicted_pct >= SATURATION_PCT_THRESHOLD - 0.1:
            predicted_risk = 0.5
        else:
            predicted_risk = 0.1

        # Combine risks
        combined = max(base_risk, velocity_risk * 0.8, predicted_risk * 0.7)
        return min(1.0, combined)

    def _hours_to_critical(
        self,
        current_pct: float,
        velocity: float
    ) -> Optional[float]:
        """Calculate hours until depletion or saturation."""
        if velocity == 0:
            return None

        if velocity < 0:
            # Depleting - hours until DEPLETION_PCT_THRESHOLD
            pct_to_threshold = current_pct - DEPLETION_PCT_THRESHOLD
            if pct_to_threshold <= 0:
                return 0  # Already critical
            hours = pct_to_threshold / abs(velocity)
        else:
            # Saturating - hours until SATURATION_PCT_THRESHOLD
            pct_to_threshold = SATURATION_PCT_THRESHOLD - current_pct
            if pct_to_threshold <= 0:
                return 0  # Already critical
            hours = pct_to_threshold / velocity

        return max(0, hours)

    def _determine_urgency(
        self,
        hours_to_critical: Optional[float],
        depletion_risk: float,
        saturation_risk: float
    ) -> PredictionUrgency:
        """Determine urgency level from prediction."""
        max_risk = max(depletion_risk, saturation_risk)

        if hours_to_critical is not None:
            if hours_to_critical <= 0:
                return PredictionUrgency.CRITICAL
            elif hours_to_critical <= URGENT_HOURS_THRESHOLD:
                return PredictionUrgency.CRITICAL if max_risk > 0.7 else PredictionUrgency.URGENT
            elif hours_to_critical <= 12:
                return PredictionUrgency.URGENT if max_risk > 0.5 else PredictionUrgency.PREEMPTIVE
            elif hours_to_critical <= PREEMPTIVE_HOURS_THRESHOLD:
                return PredictionUrgency.PREEMPTIVE if max_risk > 0.3 else PredictionUrgency.LOW

        if max_risk > 0.7:
            return PredictionUrgency.URGENT
        elif max_risk > 0.4:
            return PredictionUrgency.PREEMPTIVE
        elif max_risk > 0.2:
            return PredictionUrgency.LOW

        return PredictionUrgency.NONE

    def _determine_action(
        self,
        urgency: PredictionUrgency,
        depletion_risk: float,
        saturation_risk: float
    ) -> RecommendedAction:
        """Determine recommended action from urgency and risks."""
        if urgency == PredictionUrgency.NONE:
            return RecommendedAction.NO_ACTION

        if urgency in [PredictionUrgency.CRITICAL, PredictionUrgency.URGENT]:
            return RecommendedAction.PREEMPTIVE_REBALANCE

        if urgency == PredictionUrgency.PREEMPTIVE:
            if depletion_risk > 0.5 or saturation_risk > 0.5:
                return RecommendedAction.PREEMPTIVE_REBALANCE
            else:
                return RecommendedAction.FEE_ADJUSTMENT

        return RecommendedAction.MONITOR

    def _pattern_name(self, pattern: TemporalPattern) -> str:
        """Generate human-readable pattern name."""
        parts = []

        if pattern.day_of_week is not None:
            days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            parts.append(days[pattern.day_of_week])

        if pattern.hour_of_day is not None:
            parts.append(f"{pattern.hour_of_day:02d}:00")

        direction = "drain" if pattern.direction == FlowDirection.OUTBOUND else "inflow"
        parts.append(direction)

        return "_".join(parts) if parts else "unknown"

    def _get_channel_info(self, channel_id: str) -> Optional[Dict]:
        """Get channel info from RPC."""
        if not self.plugin:
            return None

        try:
            channels = self.plugin.rpc.listpeerchannels()
            for ch in channels.get("channels", []):
                scid = ch.get("short_channel_id")
                if scid == channel_id:
                    total = ch.get("total_msat", 0)
                    if isinstance(total, str):
                        total = int(total.replace("msat", ""))
                    total_sats = total // 1000

                    local = ch.get("to_us_msat", 0)
                    if isinstance(local, str):
                        local = int(local.replace("msat", ""))
                    local_sats = local // 1000

                    return {
                        "channel_id": scid,
                        "peer_id": ch.get("peer_id", ""),
                        "capacity_sats": total_sats,
                        "local_sats": local_sats,
                        "local_pct": local_sats / total_sats if total_sats > 0 else 0.5
                    }
        except Exception as e:
            self._log(f"Failed to get channel info: {e}", level="debug")

        return None

    # =========================================================================
    # FLEET COORDINATION
    # =========================================================================

    def get_all_predictions(
        self,
        hours_ahead: int = DEFAULT_PREDICTION_HOURS,
        min_risk: float = 0.3
    ) -> List[LiquidityPrediction]:
        """
        Get predictions for all channels.

        Args:
            hours_ahead: Hours to predict ahead
            min_risk: Minimum depletion/saturation risk to include

        Returns:
            List of predictions with risk >= min_risk
        """
        predictions = []

        if not self.plugin:
            return predictions

        try:
            channels = self.plugin.rpc.listpeerchannels()
            for ch in channels.get("channels", []):
                scid = ch.get("short_channel_id")
                if not scid:
                    continue

                # Skip non-normal channels
                if ch.get("state") != "CHANNELD_NORMAL":
                    continue

                pred = self.predict_liquidity(scid, hours_ahead=hours_ahead)
                if pred:
                    max_risk = max(pred.depletion_risk, pred.saturation_risk)
                    if max_risk >= min_risk:
                        predictions.append(pred)

        except Exception as e:
            self._log(f"Failed to get all predictions: {e}", level="debug")

        # Sort by risk
        predictions.sort(
            key=lambda p: max(p.depletion_risk, p.saturation_risk),
            reverse=True
        )

        return predictions

    def get_fleet_recommendations(self) -> List[FleetAnticipation]:
        """
        Get fleet-wide anticipatory positioning recommendations.

        Coordinates predictions across members to avoid competing
        for the same rebalance routes.

        Returns:
            List of FleetAnticipation recommendations
        """
        if not self.state_manager:
            return []

        recommendations = []

        try:
            # Get our predictions
            our_predictions = self.get_all_predictions(min_risk=0.4)

            # Group by peer
            peer_predictions: Dict[str, List[LiquidityPrediction]] = defaultdict(list)
            for pred in our_predictions:
                peer_predictions[pred.peer_id].append(pred)

            # For each peer, check if other members also predict issues
            all_states = self.state_manager.get_all_peer_states()

            for peer_id, preds in peer_predictions.items():
                members_depleting = []
                members_saturating = []

                # Check our predictions
                for pred in preds:
                    if pred.depletion_risk > 0.5:
                        members_depleting.append(self._get_our_id())
                    if pred.saturation_risk > 0.5:
                        members_saturating.append(self._get_our_id())

                # Check other members (from shared state)
                for state in all_states:
                    # Would need liquidity state to include predictions
                    # For now, check if they have channels to same peer
                    topology = getattr(state, 'topology', []) or []
                    if peer_id in topology:
                        # They have a channel to this peer too
                        # Could be competing for rebalance
                        pass

                if members_depleting or members_saturating:
                    # Determine recommended coordinator
                    # Prefer member with most capacity to this peer
                    coordinator = self._get_our_id()  # Default to us

                    total_demand = sum(
                        int(p.current_local_pct * 1_000_000)  # Rough estimate
                        for p in preds
                        if p.depletion_risk > 0.5
                    )

                    recommendations.append(FleetAnticipation(
                        target_peer=peer_id,
                        members_predicting_depletion=members_depleting,
                        members_predicting_saturation=members_saturating,
                        recommended_coordinator=coordinator,
                        total_predicted_demand_sats=total_demand,
                        coordination_window_hours=12,
                        recommendation=self._fleet_recommendation(
                            len(members_depleting), len(members_saturating)
                        )
                    ))

        except Exception as e:
            self._log(f"Failed to get fleet recommendations: {e}", level="debug")

        return recommendations

    def _fleet_recommendation(
        self,
        depleting_count: int,
        saturating_count: int
    ) -> str:
        """Generate fleet coordination recommendation."""
        if depleting_count > 1 and saturating_count > 1:
            return "Multiple members depleting AND saturating - internal rebalance opportunity"
        elif depleting_count > 1:
            return "Multiple members depleting - coordinate to avoid competing for inbound"
        elif saturating_count > 1:
            return "Multiple members saturating - coordinate to avoid competing for outbound"
        elif depleting_count == 1:
            return "Single member depleting - proceed with preemptive rebalance"
        elif saturating_count == 1:
            return "Single member saturating - lower fees to attract outbound"
        else:
            return "Monitor situation"

    # =========================================================================
    # STATUS / DIAGNOSTICS
    # =========================================================================

    def get_status(self) -> Dict[str, Any]:
        """Get manager status for diagnostics."""
        return {
            "active": True,
            "channels_with_patterns": len(self._pattern_cache),
            "channels_with_predictions": len(self._prediction_cache),
            "total_flow_samples": sum(len(s) for s in self._flow_history.values()),
            "pattern_window_days": PATTERN_WINDOW_DAYS,
            "prediction_stale_hours": PREDICTION_STALE_HOURS,
            "min_pattern_samples": MIN_PATTERN_SAMPLES,
            "confidence_threshold": PATTERN_CONFIDENCE_THRESHOLD
        }

    def get_patterns_summary(self) -> Dict[str, Any]:
        """Get summary of detected patterns across all channels."""
        all_patterns = []
        for channel_id, patterns in self._pattern_cache.items():
            for p in patterns:
                all_patterns.append(p.to_dict())

        # Group by type
        hourly = [p for p in all_patterns if p["hour_of_day"] is not None and p["day_of_week"] is None]
        daily = [p for p in all_patterns if p["hour_of_day"] is None and p["day_of_week"] is not None]
        combined = [p for p in all_patterns if p["hour_of_day"] is not None and p["day_of_week"] is not None]

        return {
            "total_patterns": len(all_patterns),
            "hourly_patterns": len(hourly),
            "daily_patterns": len(daily),
            "combined_patterns": len(combined),
            "patterns": all_patterns[:20]  # Limit for display
        }

    # =========================================================================
    # FLEET INTELLIGENCE SHARING (Phase 14)
    # =========================================================================

    def get_shareable_patterns(
        self,
        min_confidence: float = 0.6,
        min_samples: int = 10,
        exclude_peer_ids: Optional[set] = None,
        max_patterns: int = 500
    ) -> List[Dict[str, Any]]:
        """
        Get temporal patterns suitable for sharing with fleet.

        Only shares patterns with sufficient confidence and samples.

        Args:
            min_confidence: Minimum pattern confidence to share
            min_samples: Minimum samples required
            exclude_peer_ids: Set of peer IDs to exclude
            max_patterns: Maximum number of patterns to return

        Returns:
            List of pattern dicts ready for serialization
        """
        exclude_peer_ids = exclude_peer_ids or set()
        shareable = []

        for channel_id, patterns in self._pattern_cache.items():
            # Get peer_id for this channel (if we have mapping)
            peer_id = self._channel_peer_map.get(channel_id) if hasattr(self, '_channel_peer_map') else None
            if not peer_id:
                continue

            # Skip hive members
            if peer_id in exclude_peer_ids:
                continue

            for p in patterns:
                if p.confidence < min_confidence:
                    continue
                if p.samples < min_samples:
                    continue

                shareable.append({
                    "peer_id": peer_id,
                    "channel_id": channel_id,
                    "hour_of_day": p.hour_of_day if p.hour_of_day is not None else -1,
                    "day_of_week": p.day_of_week if p.day_of_week is not None else -1,
                    "direction": p.direction.value,
                    "intensity": round(p.intensity, 3),
                    "confidence": round(p.confidence, 3),
                    "samples": p.samples
                })

        # Sort by confidence descending
        shareable.sort(key=lambda x: -x["confidence"])

        return shareable[:max_patterns]

    def set_channel_peer_mapping(self, channel_id: str, peer_id: str) -> None:
        """Set the mapping from channel_id to peer_id for sharing."""
        if not hasattr(self, '_channel_peer_map'):
            self._channel_peer_map: Dict[str, str] = {}
        self._channel_peer_map[channel_id] = peer_id

    def update_channel_peer_mappings(self, channels: List[Dict[str, Any]]) -> None:
        """Update channel-to-peer mappings from a list of channel info."""
        if not hasattr(self, '_channel_peer_map'):
            self._channel_peer_map: Dict[str, str] = {}
        for ch in channels:
            channel_id = ch.get("short_channel_id")
            peer_id = ch.get("peer_id")
            if channel_id and peer_id:
                self._channel_peer_map[channel_id] = peer_id

    def receive_pattern_from_fleet(
        self,
        reporter_id: str,
        pattern_data: Dict[str, Any]
    ) -> bool:
        """
        Receive a temporal pattern from another fleet member.

        Stores remote patterns for use in coordinated liquidity positioning.

        Args:
            reporter_id: The fleet member who reported this
            pattern_data: Dict with peer_id, hour_of_day, direction, etc.

        Returns:
            True if stored successfully
        """
        peer_id = pattern_data.get("peer_id")
        if not peer_id:
            return False

        # Initialize remote patterns storage if needed
        if not hasattr(self, "_remote_patterns"):
            self._remote_patterns: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        hour = pattern_data.get("hour_of_day", -1)
        day = pattern_data.get("day_of_week", -1)

        entry = {
            "reporter_id": reporter_id,
            "hour_of_day": hour if hour >= 0 else None,
            "day_of_week": day if day >= 0 else None,
            "direction": pattern_data.get("direction", "balanced"),
            "intensity": pattern_data.get("intensity", 0),
            "confidence": pattern_data.get("confidence", 0),
            "samples": pattern_data.get("samples", 0),
            "timestamp": time.time()
        }

        self._remote_patterns[peer_id].append(entry)

        # Keep only recent patterns per peer (last 50)
        if len(self._remote_patterns[peer_id]) > 50:
            self._remote_patterns[peer_id] = self._remote_patterns[peer_id][-50:]

        return True

    def get_fleet_patterns_for_peer(self, peer_id: str) -> List[Dict[str, Any]]:
        """
        Get fleet-reported patterns for a specific peer.

        Aggregates patterns from multiple reporters for consensus view.

        Args:
            peer_id: External peer to get patterns for

        Returns:
            List of aggregated pattern data
        """
        if not hasattr(self, "_remote_patterns"):
            return []

        patterns = self._remote_patterns.get(peer_id, [])
        if not patterns:
            return []

        # Filter to recent patterns (last 7 days)
        now = time.time()
        recent = [p for p in patterns if now - p.get("timestamp", 0) < 7 * 86400]

        return recent

    def cleanup_old_remote_patterns(self, max_age_days: float = 7) -> int:
        """Remove old remote pattern data."""
        if not hasattr(self, "_remote_patterns"):
            return 0

        cutoff = time.time() - (max_age_days * 86400)
        cleaned = 0

        for peer_id in list(self._remote_patterns.keys()):
            before = len(self._remote_patterns[peer_id])
            self._remote_patterns[peer_id] = [
                p for p in self._remote_patterns[peer_id]
                if p.get("timestamp", 0) > cutoff
            ]
            cleaned += before - len(self._remote_patterns[peer_id])

            if not self._remote_patterns[peer_id]:
                del self._remote_patterns[peer_id]

        return cleaned
