# Implementation Plan: Phase 7 - Swarm Intelligence Enhancement

**Date**: January 2025
**Status**: Planning
**Based On**: SWARM_INTELLIGENCE_RESEARCH_2025.md deep analysis

---

## Executive Summary

This plan addresses gaps identified between the Swarm Intelligence Research Report and current cl-hive implementation. After deep analysis of all modules, the hive has **strong implementation** of core swarm intelligence principles, but several opportunities remain to enhance alpha and yield.

---

## Current Implementation Status

### Fully Implemented (90-100%)

| Feature | Module | Status | Notes |
|---------|--------|--------|-------|
| **Stigmergic Fee Coordination** | `fee_coordination.py` | 95% | RouteMarker, pheromone deposit/evaporation, 24h half-life |
| **Adaptive Fee Evaporation** | `fee_coordination.py` | 95% | HistoricalResponseCurve, regime detection, BASE_EVAPORATION_RATE=0.2 |
| **Collective Defense (Mycelium)** | `fee_coordination.py` | 90% | MyceliumDefenseSystem, drain detection, DRAIN_RATIO_THRESHOLD=5.0 |
| **Flow Corridor Ownership** | `fee_coordination.py` | 95% | FlowCorridorManager, primary/secondary designation |
| **Internal Competition Detection** | `liquidity_coordinator.py` | 90% | detect_internal_competition(), bottleneck peer detection |
| **NNLB Health Sharing** | `fee_intelligence.py`, `health_aggregator.py` | 90% | 4-tier system, budget multipliers |
| **Redundancy Analysis** | `channel_rationalization.py` | 95% | MAX_HEALTHY_REDUNDANCY=2, stigmergic ownership markers |
| **Cooperative Expansion** | `cooperative_expansion.py` | 90% | Election system, redundancy checks, fallback support |
| **Velocity Prediction** | `yield_metrics.py` | 85% | Balance velocity extrapolation, critical velocity alerts |

### Partially Implemented (40-70%)

| Feature | Module | Status | Gap |
|---------|--------|--------|-----|
| **Physarum Channel Lifecycle** | `strategic_positioning.py` | 60% | Advisory only, no auto-triggers. Thresholds exist (STRENGTHEN_FLOW_THRESHOLD=0.02, ATROPHY_FLOW_THRESHOLD=0.001) but actions require human approval |
| **Splice Coordination** | `splice_coordinator.py` | 70% | Safety checks implemented, but no splice execution integration |
| **Route Value Analysis** | `strategic_positioning.py` | 65% | RouteValueAnalyzer exists but isn't used to drive automatic decisions |

### Not Implemented (0-30%)

| Feature | Research Reference | Priority | Complexity |
|---------|-------------------|----------|------------|
| **Anticipatory Liquidity** | Alpha 5 | HIGH | Medium |
| **Fleet-Based LSP** | Alpha 4, Niche 3 | LOW | High |
| **Time-of-Day Patterns** | Part of Alpha 5 | HIGH | Medium |
| **Channel Factory Coordination** | Section 3.4 | LOW | Very High |
| **RCA-SI Adaptive Consensus** | Section 1.1 | MEDIUM | High |

---

## Gap Analysis

### Gap 1: Anticipatory Liquidity (0% Implemented)

**Research Recommendation (Alpha 5)**:
> "Predict liquidity needs before they occur... Identify patterns (time-of-day, day-of-week)... Pre-position liquidity before demand spikes"

**Current State**:
- `yield_metrics.py` tracks velocity but only as point-in-time extrapolation
- No time-of-day or day-of-week pattern recognition
- Rebalancing is reactive, not predictive

**Gap Impact**: Reactive rebalancing pays urgency premiums. Anticipatory positioning could reduce costs by 30-50%.

---

### Gap 2: Physarum Auto-Triggers (40% Gap)

**Research Recommendation (Alpha 2)**:
> "Implement flow-based channel evolution... High flow → strengthen (splice-in), Low flow → atrophy (close)"

**Current State**:
- `PhysarumChannelManager` exists with correct thresholds
- `get_channel_lifecycle_recommendation()` returns advisory output
- No automatic splice-in triggers
- No automatic close triggers
- Human must manually approve all lifecycle changes

**Gap Impact**: Manual process delays optimization. Channels sit stagnant or underperforming for weeks.

---

### Gap 3: Time Pattern Recognition (0% Implemented)

**Missing Feature**: No analysis of temporal patterns in routing volume.

**Example Pattern**:
- Weekend mornings: High European retail flow
- US business hours: Higher institutional flow
- Monthly salary days: Predictable spikes

**Gap Impact**: Fees and liquidity positioning are static, missing time-based optimization.

---

### Gap 4: Fleet LSP (0% Implemented)

**Research Recommendation (Alpha 4, Niche 3)**:
> "Offer LSP services as a fleet... LSPS1/LSPS2 at fleet level... Load balancing based on current capacity/position"

**Current State**: No LSP functionality.

**Gap Impact**: Missing a significant revenue stream and market differentiator. However, this is complex and lower priority.

---

## Implementation Plan

### Phase 7.1: Anticipatory Liquidity (2-3 weeks)

**Goal**: Predict liquidity needs 6-24 hours ahead using historical patterns.

#### New Module: `modules/anticipatory_liquidity.py`

```python
# Key Components:

class TemporalPattern:
    """Detected time-based flow pattern."""
    hour_of_day: int        # 0-23
    day_of_week: int        # 0-6 (Mon-Sun)
    direction: str          # "inbound" | "outbound"
    intensity: float        # 0.0-1.0
    confidence: float       # Pattern reliability
    samples: int            # Number of observations

class AnticipatoryLiquidityManager:
    """
    Predicts liquidity needs before they occur.

    Like mycelium nutrient pre-positioning - move resources
    to where they'll be needed before the demand.
    """

    # Detection window
    PATTERN_WINDOW_DAYS = 14           # Days of history to analyze
    MIN_PATTERN_SAMPLES = 10           # Minimum observations for confidence
    PATTERN_CONFIDENCE_THRESHOLD = 0.6 # Minimum confidence to act

    # Prediction horizon
    PREDICTION_HOURS = [6, 12, 24]     # Look-ahead windows

    def detect_temporal_patterns(self, channel_id: str) -> List[TemporalPattern]:
        """Analyze forward history to detect time-based patterns."""

    def predict_liquidity_need(
        self,
        channel_id: str,
        hours_ahead: int = 12
    ) -> Dict[str, Any]:
        """
        Predict liquidity state N hours from now.

        Returns:
            {
                "predicted_local_pct": 0.25,
                "predicted_need": "inbound",
                "confidence": 0.75,
                "pattern_match": "weekday_morning_drain",
                "recommended_action": "preemptive_rebalance",
                "optimal_timing_hours": 6
            }
        """

    def get_fleet_anticipatory_positions(self) -> List[Dict]:
        """
        Get recommended positions for entire fleet based on predictions.

        Coordinates so members don't all rebalance to same target.
        """
```

#### Integration Points:
- `yield_metrics.py`: Feed forward history data
- `liquidity_coordinator.py`: Share predictions across fleet
- `hive_bridge.py` (cl-revenue-ops): New methods for prediction queries

---

### Phase 7.2: Physarum Auto-Triggers (1-2 weeks)

**Goal**: Enable automatic lifecycle actions with configurable thresholds and governance approval.

#### Enhancements to `modules/strategic_positioning.py`

```python
# New Constants
AUTO_STRENGTHEN_ENABLED = True     # Config: hive-physarum-auto-strengthen
AUTO_ATROPHY_ENABLED = False       # Config: hive-physarum-auto-atrophy (default off)
MIN_AUTO_STRENGTHEN_FLOW = 0.025   # 2.5% flow intensity minimum
MAX_AUTO_STRENGTHEN_PER_DAY = 2    # Rate limit

# New Method
def execute_physarum_cycle(self) -> Dict[str, Any]:
    """
    Execute one Physarum optimization cycle.

    Called by background loop. Respects governance mode:
    - advisor: Queue actions to pending_actions
    - failsafe: Only execute if within budget

    Slime mold algorithm:
    1. Channels with flow > STRENGTHEN_FLOW_THRESHOLD → splice-in
    2. Channels with flow < ATROPHY_FLOW_THRESHOLD & age > 180d → close
    3. Channels in between → maintain (adjust fees only)
    """

def auto_strengthen(self, channel_id: str, recommended_sats: int):
    """
    Automatically splice-in capacity to high-flow channel.

    Safety checks:
    - On-chain balance sufficient
    - Daily limit not exceeded
    - Channel already exists (not new open)
    - Flow sustained for MIN_SUSTAIN_PERIODS
    """

def auto_atrophy(self, channel_id: str):
    """
    Automatically close low-flow channel.

    Safety checks:
    - Channel age > MIN_CHANNEL_AGE_FOR_ATROPHY_DAYS
    - No recent routing activity
    - Fleet coverage check via splice_coordinator
    - Not a hive member channel
    """
```

#### Governance Integration:
- Auto-strengthen: Queue as pending_action in advisor mode
- Auto-atrophy: Always queue as pending_action (never auto-execute closes)
- Rate limits: MAX_AUTO_STRENGTHEN_PER_DAY, MAX_AUTO_ATROPHY_PER_WEEK

---

### Phase 7.3: Planner Enhancement (See clever-rolling-dragonfly.md)

**Already Planned**: The plan at `/home/sat/.claude/plans/clever-rolling-dragonfly.md` addresses:
- Peer network competition scoring
- Hive coverage diversity check
- Splice recommendations instead of new opens
- Integration with cooperation modules

**Priority**: Implement this plan after Phase 7.1 and 7.2.

---

### Phase 7.4: Time-Based Fee Optimization (1-2 weeks)

**Goal**: Adjust fees based on time-of-day patterns.

#### Enhancements to `modules/fee_coordination.py`

```python
# New Constants
TIME_FEE_ADJUSTMENT_ENABLED = True  # Config: hive-time-fee-enabled
TIME_FEE_MAX_ADJUSTMENT_PCT = 0.25  # ±25% from base fee

class TimeBasedFeeAdjuster:
    """
    Adjusts fees based on detected temporal patterns.

    Like circadian rhythms in nature - different behavior at different times.
    """

    def get_time_adjustment(
        self,
        channel_id: str,
        base_fee: int
    ) -> Tuple[int, str]:
        """
        Get time-adjusted fee.

        Returns:
            (adjusted_fee, reason)

        Example:
            (275, "Peak European hours +10%")
            (225, "Low-activity period -10%")
        """

    def detect_peak_hours(self, channel_id: str) -> List[int]:
        """
        Detect peak routing hours for a channel.

        Returns list of hours (0-23) with above-average volume.
        """
```

---

## Implementation Priority

| Phase | Feature | Priority | Est. Time | Dependencies |
|-------|---------|----------|-----------|--------------|
| 7.1 | Anticipatory Liquidity | HIGH | 2-3 weeks | None |
| 7.2 | Physarum Auto-Triggers | HIGH | 1-2 weeks | None |
| 7.3 | Planner Enhancement | MEDIUM | 1 week | Existing plan |
| 7.4 | Time-Based Fees | MEDIUM | 1-2 weeks | 7.1 |
| 7.5 | Fleet LSP | LOW | 4-6 weeks | All above |

---

## Files to Create/Modify

### New Files
| File | Description | Lines Est. |
|------|-------------|------------|
| `modules/anticipatory_liquidity.py` | Temporal pattern detection, prediction | ~600 |

### Modified Files
| File | Changes | Lines Est. |
|------|---------|------------|
| `modules/strategic_positioning.py` | Auto-trigger methods | +150 |
| `modules/fee_coordination.py` | Time-based adjustments | +200 |
| `modules/planner.py` | Competition/coverage scoring | +200 |
| `cl-hive.py` | New config options, loop integration | +50 |
| `tools/mcp-hive-server.py` | New MCP tools | +100 |

### cl-revenue-ops Integration
| File | Changes |
|------|---------|
| `modules/hive_bridge.py` | New query methods for anticipatory, time-based |
| `modules/fee_controller.py` | Use time-based fee recommendations |
| `modules/rebalancer.py` | Use anticipatory predictions |

---

## Success Metrics

### Phase 7 Targets

| Metric | Current | Target | Measurement |
|--------|---------|--------|-------------|
| Rebalancing Cost | 100% (baseline) | 70% (-30%) | advisor_db costs |
| Channel Utilization | ~35% average | ~50% average | Volume/Capacity |
| Physarum Actions | 0/month (manual) | 5-10/month (auto) | pending_actions count |
| Time-Pattern Coverage | 0% | 80% channels | Pattern confidence |
| Fleet ROC | 0.17% | 0.5%+ | revenue_dashboard |

---

## Risk Mitigation

### Auto-Action Risks

1. **Incorrect Splice-In**: Rate limit to 2/day, require sustained flow
2. **Incorrect Close**: Always queue for human approval, never auto-execute
3. **Time Pattern Noise**: Require MIN_PATTERN_SAMPLES=10 and confidence>0.6
4. **Fee Whiplash**: MAX_TIME_ADJUSTMENT_PCT=25%, smooth transitions

### Safety Constraints

- All auto-actions respect governance mode
- Auto-atrophy NEVER executes without human approval
- Time-based fees bounded to ±25% of base
- Anticipatory rebalancing respects normal cost limits

---

## Conclusion

The hive has a strong foundation with 90%+ implementation of core swarm intelligence features. The remaining gaps center on:

1. **Temporal awareness**: No time-of-day/week pattern recognition
2. **Automation**: Advisory-only mode for Physarum lifecycle
3. **Predictive positioning**: Reactive rather than anticipatory

Addressing these gaps could improve:
- Rebalancing efficiency by 30%+
- Channel utilization by 15%+
- Overall fleet ROC from 0.17% toward 0.5%+

The implementation is incremental and maintains the hive's safety-first approach through governance integration and rate limits.
