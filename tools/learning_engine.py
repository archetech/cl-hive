"""
Learning Engine for Proactive AI Advisor

Tracks action outcomes and adapts advisor behavior through:
- Confidence calibration based on prediction accuracy
- Action type effectiveness tracking
- Pattern recognition for opportunity types
- Goal strategy mapping

Usage:
    from learning_engine import LearningEngine

    engine = LearningEngine(db)
    outcomes = engine.measure_outcomes(hours_ago_min=6, hours_ago_max=24)
    confidence = engine.get_adjusted_confidence(0.7, "fee_change", "peak_hour_fee")
"""

import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class ActionOutcome:
    """Tracked outcome of an advisor action."""
    action_id: int
    action_type: str        # "fee_change", "rebalance", "config_change", etc.
    opportunity_type: str   # "peak_hour_fee", "critical_depletion", "bleeder_fix", etc.
    channel_id: Optional[str]
    node_name: str

    # Context at decision time
    decision_confidence: float
    predicted_benefit: int

    # Outcome (measured 6-24 hours later)
    actual_benefit: int
    success: bool
    outcome_measured_at: int

    # Learning metrics
    prediction_error: float  # (actual - predicted) / predicted if predicted != 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "action_id": self.action_id,
            "action_type": self.action_type,
            "opportunity_type": self.opportunity_type,
            "channel_id": self.channel_id,
            "node_name": self.node_name,
            "decision_confidence": self.decision_confidence,
            "predicted_benefit": self.predicted_benefit,
            "actual_benefit": self.actual_benefit,
            "success": self.success,
            "outcome_measured_at": self.outcome_measured_at,
            "prediction_error": self.prediction_error
        }


@dataclass
class LearnedParameters:
    """Learned parameters that adjust advisor behavior."""
    # Action type multipliers (1.0 = neutral, >1 = more confident, <1 = less confident)
    action_type_confidence: Dict[str, float] = field(default_factory=lambda: {
        "fee_change": 1.0,
        "rebalance": 1.0,
        "config_change": 1.0,
        "channel_open": 1.0,
        "policy_change": 1.0
    })

    # Opportunity type success rates (0.5 = baseline)
    opportunity_success_rates: Dict[str, float] = field(default_factory=dict)

    # Statistics
    total_outcomes_measured: int = 0
    overall_success_rate: float = 0.5
    last_updated: int = 0


# =============================================================================
# Learning Engine
# =============================================================================

class LearningEngine:
    """
    Tracks action outcomes and adjusts advisor behavior.

    Key learning mechanisms:
    1. Confidence calibration - adjust confidence based on accuracy
    2. Action type effectiveness - track which actions actually help
    3. Pattern recognition - learn which opportunities are real
    4. Goal strategy mapping - learn which actions help which goals
    """

    # Minimum samples before adjusting confidence
    MIN_SAMPLES_FOR_ADJUSTMENT = 5

    # Learning rate (how much to adjust toward new observations)
    LEARNING_RATE = 0.1

    # Default success rate for new opportunity types
    # Optimistic default for untracked opportunity types (Issue #45)
    # No reason to assume 50% failure when system success is 99%+
    DEFAULT_SUCCESS_RATE = 0.9

    def __init__(self, db):
        """
        Initialize learning engine.

        Args:
            db: AdvisorDB instance for persistence
        """
        self.db = db
        self._params = self._load_parameters()

    def _load_parameters(self) -> LearnedParameters:
        """Load learned parameters from database."""
        data = self.db.get_learning_params()
        if data:
            params = LearnedParameters()
            params.action_type_confidence = data.get(
                "action_type_confidence",
                params.action_type_confidence
            )
            params.opportunity_success_rates = data.get(
                "opportunity_success_rates", {}
            )
            params.total_outcomes_measured = data.get("total_outcomes_measured", 0)
            params.overall_success_rate = data.get("overall_success_rate", 0.5)
            params.last_updated = data.get("last_updated", 0)
            return params
        return LearnedParameters()

    def _save_parameters(self) -> None:
        """Save learned parameters to database."""
        self._params.last_updated = int(time.time())
        data = {
            "action_type_confidence": self._params.action_type_confidence,
            "opportunity_success_rates": self._params.opportunity_success_rates,
            "total_outcomes_measured": self._params.total_outcomes_measured,
            "overall_success_rate": self._params.overall_success_rate,
            "last_updated": self._params.last_updated
        }
        self.db.save_learning_params(data)

    def measure_outcomes(
        self,
        hours_ago_min: int = 6,
        hours_ago_max: int = 24
    ) -> List[ActionOutcome]:
        """
        Measure outcomes of past decisions.

        Called each cycle to evaluate decisions from the specified time window.
        This window allows actions to have effect but is recent enough for learning.

        Args:
            hours_ago_min: Minimum hours since decision (default 6)
            hours_ago_max: Maximum hours since decision (default 24)

        Returns:
            List of measured ActionOutcome objects
        """
        outcomes = []

        # Get decisions from the time window
        decisions = self.db.get_decisions_in_window(hours_ago_min, hours_ago_max)

        for decision in decisions:
            if decision.get("outcome_measured"):
                continue  # Already measured

            outcome = self._measure_single_outcome(decision)
            if outcome:
                outcomes.append(outcome)
                # Record outcome in database
                self.db.record_action_outcome(outcome.to_dict())

        # Update learned parameters
        if outcomes:
            self._update_learned_parameters(outcomes)

        return outcomes

    def _measure_single_outcome(self, decision: Dict) -> Optional[ActionOutcome]:
        """
        Measure outcome for a single decision.

        Args:
            decision: Decision record from database

        Returns:
            ActionOutcome or None if cannot measure
        """
        action_type = decision.get("decision_type", "unknown")
        node_name = decision.get("node_name", "unknown")
        channel_id = decision.get("channel_id")
        decision_time = decision.get("timestamp", 0)

        # Get context at decision time
        snapshot_metrics = decision.get("snapshot_metrics")
        if snapshot_metrics and isinstance(snapshot_metrics, str):
            try:
                snapshot_metrics = json.loads(snapshot_metrics)
            except json.JSONDecodeError:
                snapshot_metrics = {}
        snapshot_metrics = snapshot_metrics or {}

        # Enrich decision with data from snapshot_metrics if not already present
        if decision.get("predicted_benefit") is None and snapshot_metrics:
            decision["predicted_benefit"] = snapshot_metrics.get("predicted_benefit", 0)
        if not decision.get("opportunity_type") and snapshot_metrics.get("opportunity_type"):
            decision["opportunity_type"] = snapshot_metrics["opportunity_type"]

        # Get current state for comparison
        current_state = self._get_current_channel_state(node_name, channel_id)

        if not current_state and action_type in ["fee_change", "rebalance"]:
            # No recent history - skip measurement rather than assume failure (Issue #45)
            # Missing history != channel closed; could be data gap or slow sync
            return None

        # Calculate outcome based on action type
        if action_type == "fee_change":
            outcome = self._measure_fee_change_outcome(
                decision, snapshot_metrics, current_state
            )
        elif action_type == "rebalance":
            outcome = self._measure_rebalance_outcome(
                decision, snapshot_metrics, current_state
            )
        elif action_type == "config_change":
            outcome = self._measure_config_change_outcome(
                decision, snapshot_metrics
            )
        elif action_type == "policy_change":
            outcome = self._measure_policy_change_outcome(
                decision, snapshot_metrics, current_state
            )
        else:
            # Generic outcome - just mark as measured with neutral result
            outcome = ActionOutcome(
                action_id=decision.get("id", 0),
                action_type=action_type,
                opportunity_type=decision.get("opportunity_type", "unknown"),
                channel_id=channel_id,
                node_name=node_name,
                decision_confidence=decision.get("confidence", 0.5),
                predicted_benefit=0,
                actual_benefit=0,
                success=True,  # Neutral
                outcome_measured_at=int(time.time()),
                prediction_error=0
            )

        return outcome

    def _get_current_channel_state(
        self,
        node_name: str,
        channel_id: str,
        hours: int = 24
    ) -> Optional[Dict]:
        """Get current state of a channel from database."""
        if not channel_id:
            return None

        # Get most recent history record within window
        # Use longer window (default 24h) to avoid false "channel closed" (Issue #45)
        history = self.db.get_channel_history(node_name, channel_id, hours=hours)
        if history:
            return history[-1]  # Most recent
        return None

    def _measure_fee_change_outcome(
        self,
        decision: Dict,
        before: Dict,
        after: Optional[Dict]
    ) -> ActionOutcome:
        """
        Measure outcome of a fee change decision using revenue-based comparison.

        Primary metric: fees_earned_sats delta (direct revenue measurement).
        Secondary metric: forward_count delta (volume proxy).
        When both are 0 (no activity), outcome is neutral rather than failed.
        """
        if not after:
            after = {}

        before_revenue = before.get("fees_earned_sats") if before.get("fees_earned_sats") is not None else 0
        after_revenue = after.get("fees_earned_sats") if after.get("fees_earned_sats") is not None else 0
        before_flow = before.get("forward_count") if before.get("forward_count") is not None else 0
        after_flow = after.get("forward_count") if after.get("forward_count") is not None else 0
        after_fee = after.get("fee_ppm") if after.get("fee_ppm") is not None else 0

        # Primary metric: revenue change (direct measurement)
        revenue_delta = after_revenue - before_revenue

        # Secondary metric: flow count change (volume proxy)
        flow_delta = after_flow - before_flow

        # Success criteria:
        # 1. Revenue increased or maintained with fee change
        # 2. Or flow increased significantly even if revenue flat
        # 3. No activity = neutral (don't penalize inactive channels)
        if revenue_delta > 0:
            success = True
            actual_benefit = revenue_delta
        elif revenue_delta == 0 and flow_delta > 0:
            success = True
            actual_benefit = flow_delta * after_fee // 1_000_000  # estimate from count
        elif revenue_delta == 0 and flow_delta == 0:
            # No data yet — neutral (don't penalize for no activity)
            success = True
            actual_benefit = 0
        else:
            success = False
            actual_benefit = revenue_delta  # negative

        predicted_benefit = decision.get("predicted_benefit", 0)
        if predicted_benefit != 0:
            prediction_error = (actual_benefit - predicted_benefit) / abs(predicted_benefit)
        else:
            prediction_error = 0

        return ActionOutcome(
            action_id=decision.get("id", 0),
            action_type="fee_change",
            opportunity_type=decision.get("opportunity_type", "unknown"),
            channel_id=decision.get("channel_id"),
            node_name=decision.get("node_name"),
            decision_confidence=decision.get("confidence", 0.5),
            predicted_benefit=predicted_benefit,
            actual_benefit=actual_benefit,
            success=success,
            outcome_measured_at=int(time.time()),
            prediction_error=prediction_error
        )

    def _measure_rebalance_outcome(
        self,
        decision: Dict,
        before: Dict,
        after: Optional[Dict]
    ) -> ActionOutcome:
        """Measure outcome of a rebalance decision."""
        if not after:
            after = {}

        # Success: channel balance improved toward 0.5
        before_ratio = before.get("balance_ratio") if before.get("balance_ratio") is not None else 0.5
        after_ratio = after.get("balance_ratio") if after.get("balance_ratio") is not None else 0.5

        # Distance from ideal (0.5)
        before_distance = abs(before_ratio - 0.5)
        after_distance = abs(after_ratio - 0.5)

        # Improvement in percentage points
        improvement = (before_distance - after_distance) * 100

        success = after_distance < before_distance - 0.02  # At least 2% improvement
        actual_benefit = int(improvement * 100)  # Scale for comparison

        predicted_benefit = decision.get("predicted_benefit", 0)
        if predicted_benefit != 0:
            prediction_error = (actual_benefit - predicted_benefit) / abs(predicted_benefit)
        else:
            prediction_error = 0

        return ActionOutcome(
            action_id=decision.get("id", 0),
            action_type="rebalance",
            opportunity_type=decision.get("opportunity_type", "unknown"),
            channel_id=decision.get("channel_id"),
            node_name=decision.get("node_name"),
            decision_confidence=decision.get("confidence", 0.5),
            predicted_benefit=predicted_benefit,
            actual_benefit=actual_benefit,
            success=success,
            outcome_measured_at=int(time.time()),
            prediction_error=prediction_error
        )

    def _measure_config_change_outcome(
        self,
        decision: Dict,
        before: Dict
    ) -> ActionOutcome:
        """Measure outcome of a config change decision."""
        # Config changes are harder to measure directly
        # Mark as success if no errors occurred (neutral outcome)
        return ActionOutcome(
            action_id=decision.get("id", 0),
            action_type="config_change",
            opportunity_type=decision.get("opportunity_type", "unknown"),
            channel_id=decision.get("channel_id"),
            node_name=decision.get("node_name"),
            decision_confidence=decision.get("confidence", 0.5),
            predicted_benefit=decision.get("predicted_benefit", 0),
            actual_benefit=0,  # Cannot measure directly
            success=True,  # Assume success if no errors
            outcome_measured_at=int(time.time()),
            prediction_error=0
        )

    def _measure_policy_change_outcome(
        self,
        decision: Dict,
        before: Dict,
        after: Optional[Dict]
    ) -> ActionOutcome:
        """Measure outcome of a policy change (static fees, rebalance mode)."""
        if not after:
            after = {}

        # For static policies, check if the channel stopped bleeding
        before_flow_state = before.get("flow_state", "unknown")
        after_flow_state = after.get("flow_state", "unknown")

        # Success: improved classification or maintained stable
        # Compare before vs after — improvement or stable-good counts as success
        good_states = ["profitable", "stable"]
        bad_states = ["underwater", "bleeder"]
        if before_flow_state in bad_states:
            # Was bad: success only if improved to good state
            success = after_flow_state in good_states
        elif before_flow_state in good_states:
            # Was already good: success if stayed good (didn't regress)
            success = after_flow_state not in bad_states
        else:
            # Unknown before state: don't penalize, treat as neutral
            success = after_flow_state in good_states

        return ActionOutcome(
            action_id=decision.get("id", 0),
            action_type="policy_change",
            opportunity_type=decision.get("opportunity_type", "unknown"),
            channel_id=decision.get("channel_id"),
            node_name=decision.get("node_name"),
            decision_confidence=decision.get("confidence", 0.5),
            predicted_benefit=decision.get("predicted_benefit", 0),
            actual_benefit=1 if success else -1,
            success=success,
            outcome_measured_at=int(time.time()),
            prediction_error=0
        )

    def _update_learned_parameters(self, outcomes: List[ActionOutcome]) -> None:
        """Update learned parameters based on outcomes."""

        # Group outcomes by action type
        by_action_type: Dict[str, List[ActionOutcome]] = {}
        for outcome in outcomes:
            at = outcome.action_type
            if at not in by_action_type:
                by_action_type[at] = []
            by_action_type[at].append(outcome)

        # Update confidence multipliers
        for action_type, type_outcomes in by_action_type.items():
            if len(type_outcomes) < self.MIN_SAMPLES_FOR_ADJUSTMENT:
                continue

            success_rate = sum(1 for o in type_outcomes if o.success) / len(type_outcomes)

            # Get current multiplier
            current = self._params.action_type_confidence.get(action_type, 1.0)

            # Move multiplier: >80% success pushes up, <50% pushes down, middle holds steady
            # Map success_rate to a target multiplier: 1.0 = baseline, >1.0 = good, <1.0 = bad
            target_mult = 0.5 + success_rate  # 0% -> 0.5, 50% -> 1.0, 100% -> 1.5
            new_value = current * (1 - self.LEARNING_RATE) + target_mult * self.LEARNING_RATE

            # Clamp to reasonable range [0.5, 1.5]
            new_value = max(0.5, min(1.5, new_value))

            self._params.action_type_confidence[action_type] = new_value

        # Group by opportunity type
        by_opp_type: Dict[str, List[ActionOutcome]] = {}
        for outcome in outcomes:
            ot = outcome.opportunity_type
            if ot not in by_opp_type:
                by_opp_type[ot] = []
            by_opp_type[ot].append(outcome)

        # Update opportunity success rates (require minimum samples)
        for opp_type, opp_outcomes in by_opp_type.items():
            if len(opp_outcomes) < self.MIN_SAMPLES_FOR_ADJUSTMENT:
                continue
            success_rate = sum(1 for o in opp_outcomes if o.success) / len(opp_outcomes)

            # Get current rate
            current = self._params.opportunity_success_rates.get(
                opp_type, self.DEFAULT_SUCCESS_RATE
            )

            # Exponential moving average
            new_rate = current * (1 - self.LEARNING_RATE * 2) + success_rate * self.LEARNING_RATE * 2

            # Clamp to [0.1, 0.9]
            new_rate = max(0.1, min(0.9, new_rate))

            self._params.opportunity_success_rates[opp_type] = new_rate

        # Update overall statistics
        self._params.total_outcomes_measured += len(outcomes)
        total_success = sum(1 for o in outcomes if o.success)
        current_rate = self._params.overall_success_rate
        new_rate = (
            current_rate * (1 - self.LEARNING_RATE)
            + (total_success / len(outcomes)) * self.LEARNING_RATE
        )
        self._params.overall_success_rate = new_rate

        # Save updated parameters
        self._save_parameters()

    def get_adjusted_confidence(
        self,
        base_confidence: float,
        action_type: str,
        opportunity_type: str
    ) -> float:
        """
        Get confidence adjusted by learning.

        Combines base confidence with learned multipliers.

        Args:
            base_confidence: Initial confidence score (0-1)
            action_type: Type of action being considered
            opportunity_type: Type of opportunity

        Returns:
            Adjusted confidence score (0.1-0.99)
        """
        # Action type multiplier
        action_mult = self._params.action_type_confidence.get(action_type, 1.0)

        # Opportunity success rate (use as additional multiplier)
        opp_rate = self._params.opportunity_success_rates.get(
            opportunity_type, self.DEFAULT_SUCCESS_RATE
        )

        # Use sqrt of action_mult to avoid over-penalizing (Issue #45)
        # sqrt makes the learned penalty less severe as confidence grows
        # Example: 0.59 -> sqrt(0.59) = 0.77, so 0.85 * 0.77 = 0.65 (passable)
        # opp_rate: 0.5 = neutral, 1.0 = 50% boost, 0 = 50% reduction
        adjusted = base_confidence * math.sqrt(action_mult) * (0.5 + opp_rate * 0.5)

        # Clamp to valid range
        return min(0.99, max(0.1, adjusted))

    def get_learning_summary(self) -> Dict[str, Any]:
        """Get summary of learned parameters for display."""
        return {
            "action_type_confidence": dict(self._params.action_type_confidence),
            "opportunity_success_rates": dict(self._params.opportunity_success_rates),
            "total_outcomes_measured": self._params.total_outcomes_measured,
            "overall_success_rate": round(self._params.overall_success_rate, 4),
            "last_updated": datetime.fromtimestamp(
                self._params.last_updated
            ).isoformat() if self._params.last_updated else None
        }

    def should_skip_action(
        self,
        action_type: str,
        opportunity_type: str,
        base_confidence: float
    ) -> Tuple[bool, str]:
        """
        Check if an action should be skipped based on learning.

        Args:
            action_type: Type of action
            opportunity_type: Type of opportunity
            base_confidence: Base confidence score

        Returns:
            Tuple of (should_skip, reason)
        """
        adjusted = self.get_adjusted_confidence(
            base_confidence, action_type, opportunity_type
        )

        # Skip if adjusted confidence is very low
        if adjusted < 0.3:
            opp_rate = self._params.opportunity_success_rates.get(opportunity_type, 0.5)
            return True, f"Low success rate for {opportunity_type} ({opp_rate:.0%})"

        # Skip if action type has been very unsuccessful
        action_conf = self._params.action_type_confidence.get(action_type, 1.0)
        if action_conf < 0.6:
            return True, f"Action type {action_type} has low success (mult={action_conf:.2f})"

        return False, ""

    def reset_learning(self) -> None:
        """Reset all learned parameters to defaults."""
        self._params = LearnedParameters()
        self._save_parameters()

    def get_action_type_recommendations(self) -> List[Dict[str, Any]]:
        """Get recommendations based on action type performance."""
        recommendations = []

        for action_type, confidence in self._params.action_type_confidence.items():
            if confidence < 0.7:
                recommendations.append({
                    "action_type": action_type,
                    "confidence": confidence,
                    "recommendation": f"Review {action_type} strategy - low success rate",
                    "severity": "warning" if confidence > 0.5 else "critical"
                })
            elif confidence > 1.2:
                recommendations.append({
                    "action_type": action_type,
                    "confidence": confidence,
                    "recommendation": f"Consider more aggressive {action_type} actions",
                    "severity": "info"
                })

        return recommendations

    # =========================================================================
    # Enhanced Learning: Gradient Tracking & Improvement Magnitude
    # =========================================================================

    def measure_improvement_gradient(self, hours_window: int = 48) -> Dict[str, Any]:
        """
        Track magnitude of improvement, not just success/fail.
        
        Returns gradient information showing:
        - Revenue trajectory (improving/declining/flat)
        - Per-action-type improvement magnitudes
        - Velocity of change
        """
        cutoff = int(time.time()) - hours_window * 3600
        
        # Get outcomes in window
        outcomes = []
        try:
            with self.db._get_conn() as conn:
                rows = conn.execute("""
                    SELECT action_type, actual_benefit, predicted_benefit,
                           success, measured_at
                    FROM action_outcomes
                    WHERE measured_at > ?
                    ORDER BY measured_at
                """, (cutoff,)).fetchall()
                outcomes = [dict(r) for r in rows]
        except Exception:
            pass
        
        if not outcomes:
            return {"status": "no_data", "window_hours": hours_window}
        
        # Group by action type
        by_type: Dict[str, List] = {}
        for o in outcomes:
            at = o.get("action_type", "unknown")
            if at not in by_type:
                by_type[at] = []
            by_type[at].append(o)
        
        gradients = {}
        for action_type, type_outcomes in by_type.items():
            benefits = [o.get("actual_benefit", 0) or 0 for o in type_outcomes]
            successes = [o.get("success", 0) for o in type_outcomes]
            
            # Split into first half and second half for trend
            mid = len(benefits) // 2
            if mid > 0:
                first_half_avg = sum(benefits[:mid]) / mid
                second_half_avg = sum(benefits[mid:]) / len(benefits[mid:])
                if first_half_avg >= 0:
                    trend = "improving" if second_half_avg > first_half_avg * 1.1 else \
                            "declining" if second_half_avg < first_half_avg * 0.9 else "stable"
                else:
                    # Negative values: compare absolute improvement (less negative = improving)
                    trend = "improving" if second_half_avg > first_half_avg + abs(first_half_avg) * 0.1 else \
                            "declining" if second_half_avg < first_half_avg - abs(first_half_avg) * 0.1 else "stable"
            else:
                first_half_avg = second_half_avg = sum(benefits) / len(benefits) if benefits else 0
                trend = "insufficient_data"
            
            gradients[action_type] = {
                "count": len(type_outcomes),
                "avg_benefit": round(sum(benefits) / len(benefits), 2) if benefits else 0,
                "max_benefit": max(benefits) if benefits else 0,
                "success_rate": round(sum(successes) / len(successes), 3) if successes else 0,
                "trend": trend,
                "first_half_avg": round(first_half_avg, 2),
                "second_half_avg": round(second_half_avg, 2),
            }
        
        # Overall revenue gradient
        all_benefits = [o.get("actual_benefit", 0) or 0 for o in outcomes]
        total = sum(all_benefits)
        
        return {
            "status": "ok",
            "window_hours": hours_window,
            "total_outcomes": len(outcomes),
            "total_benefit_sats": total,
            "avg_benefit_per_action": round(total / len(outcomes), 2) if outcomes else 0,
            "by_action_type": gradients,
        }

    # =========================================================================
    # Strategy Memo: Cross-Session LLM Memory
    # =========================================================================

    def generate_strategy_memo(self) -> Dict[str, Any]:
        """
        Generate natural-language strategy memo for LLM context restoration.

        This is the LLM's cross-session memory. It synthesizes recent outcomes
        into actionable guidance for the current run.

        Returns:
            {
                "memo": str,  # Natural language summary for the LLM
                "working_strategies": [...],
                "failing_strategies": [...],
                "untested_areas": [...],
                "recommended_focus": str
            }
        """
        memo_parts = []
        working = []
        failing = []
        untested = []

        # 1. Query recent outcomes (last 7 days) grouped by action type
        try:
            cutoff_7d = int(time.time()) - 7 * 86400
            with self.db._get_conn() as conn:
                # Get recent outcomes by action type
                rows = conn.execute("""
                    SELECT action_type, opportunity_type, channel_id,
                           actual_benefit, success, measured_at,
                           predicted_benefit, decision_confidence
                    FROM action_outcomes
                    WHERE measured_at > ?
                    ORDER BY measured_at DESC
                """, (cutoff_7d,)).fetchall()
                outcomes = [dict(r) for r in rows]

                # Get recent decisions (including those not yet measured)
                dec_rows = conn.execute("""
                    SELECT decision_type, channel_id, reasoning,
                           confidence, timestamp, snapshot_metrics
                    FROM ai_decisions
                    WHERE timestamp > ?
                    ORDER BY timestamp DESC
                    LIMIT 50
                """, (cutoff_7d,)).fetchall()
                recent_decisions = [dict(r) for r in dec_rows]

                # Get channels that have never been anchored
                all_channels = conn.execute("""
                    SELECT DISTINCT channel_id, node_name
                    FROM channel_history
                    WHERE timestamp > ? AND channel_id IS NOT NULL
                """, (cutoff_7d,)).fetchall()
                all_channel_ids = {r['channel_id'] for r in all_channels}

                anchored_channels = {
                    d.get('channel_id')
                    for d in recent_decisions
                    if d.get('decision_type') == 'fee_change' and d.get('channel_id')
                }
                untested_channels = all_channel_ids - anchored_channels

        except Exception:
            return {
                "memo": "No learning data available yet. This may be the first run. "
                        "Focus on fleet health assessment and setting initial fee anchors "
                        "using revenue_predict_optimal_fee for data-driven targets.",
                "working_strategies": [],
                "failing_strategies": [],
                "untested_areas": ["all channels - first run"],
                "recommended_focus": "Initial assessment and model-driven fee anchors"
            }

        if not outcomes and not recent_decisions:
            return {
                "memo": "No outcomes measured yet. Previous decisions are still pending measurement. "
                        "Continue with model-driven fee anchors and wait for outcome data.",
                "working_strategies": [],
                "failing_strategies": [],
                "untested_areas": list(untested_channels)[:10],
                "recommended_focus": "Set fee anchors using revenue_predict_optimal_fee, await outcomes"
            }

        # 2. Analyze by action type
        by_type: Dict[str, list] = {}
        for o in outcomes:
            at = o.get("action_type", "unknown")
            if at not in by_type:
                by_type[at] = []
            by_type[at].append(o)

        for action_type, type_outcomes in by_type.items():
            successes = [o for o in type_outcomes if o.get("success")]
            failures = [o for o in type_outcomes if not o.get("success")]
            total = len(type_outcomes)
            success_rate = len(successes) / total if total > 0 else 0

            if success_rate >= 0.6 and total >= 2:
                # Find what fee ranges worked
                fee_info = ""
                if action_type == "fee_change":
                    benefits = [o.get("actual_benefit", 0) for o in successes if o.get("actual_benefit") is not None]
                    if benefits:
                        fee_info = f" Avg benefit: {sum(benefits) / len(benefits):.0f} sats."

                working.append({
                    "action_type": action_type,
                    "success_rate": round(success_rate, 2),
                    "count": total,
                    "detail": f"{action_type} succeeding at {success_rate:.0%} ({len(successes)}/{total}).{fee_info}"
                })
                memo_parts.append(
                    f"WORKING: {action_type} actions succeeding ({success_rate:.0%}).{fee_info} Keep using this approach."
                )

            elif success_rate < 0.4 and total >= 2:
                failing.append({
                    "action_type": action_type,
                    "success_rate": round(success_rate, 2),
                    "count": total,
                    "detail": f"{action_type} failing at {1 - success_rate:.0%} ({len(failures)}/{total})."
                })
                memo_parts.append(
                    f"FAILING: {action_type} actions failing ({1 - success_rate:.0%}). CHANGE APPROACH — "
                    f"try different fee levels, different channels, or different action types."
                )

            elif total >= 1:
                memo_parts.append(
                    f"MIXED: {action_type} at {success_rate:.0%} success ({total} samples). "
                    f"Need more data to determine effectiveness."
                )

        # 3. Analyze by fee range (for fee_change specifically)
        fee_outcomes = by_type.get("fee_change", [])
        if fee_outcomes:
            # Group by approximate fee range from snapshot_metrics
            pass  # Revenue data already captured in benefits above

        # 4. Untested areas
        if untested_channels:
            untested = list(untested_channels)[:10]
            memo_parts.append(
                f"UNTESTED: {len(untested_channels)} channels have never been fee-anchored. "
                f"Consider exploring: {', '.join(list(untested_channels)[:5])}..."
            )

        # 5. Overall recommendation
        if not working and not failing:
            focus = "Set model-driven fee anchors on high-priority channels, measure outcomes next cycle"
        elif failing and not working:
            focus = "Current strategy is not working. Try significantly different fee levels (lower for stagnant, explore new ranges)"
        elif working and failing:
            focus = f"Double down on {working[0]['action_type']} (working). Abandon or restructure {failing[0]['action_type']} (failing)."
        else:
            focus = f"Continue {working[0]['action_type']} strategy. Expand to untested channels."

        # 6. Compose final memo
        memo = "\n".join(memo_parts) if memo_parts else "Insufficient data for strategy memo."
        memo += f"\n\nRECOMMENDED FOCUS THIS RUN: {focus}"

        return {
            "memo": memo,
            "working_strategies": working,
            "failing_strategies": failing,
            "untested_areas": untested,
            "recommended_focus": focus
        }

    # =========================================================================
    # Counterfactual Analysis
    # =========================================================================

    def counterfactual_analysis(self, action_type: str = "fee_change",
                                days: int = 14) -> Dict[str, Any]:
        """
        Compare channels that received fee anchors vs similar channels that didn't.

        Groups channels by cluster, compares anchored vs non-anchored revenue change.
        Returns estimated true impact of fee anchors.
        """
        cutoff = int(time.time()) - days * 86400

        try:
            with self.db._get_conn() as conn:
                # Get all decisions of this type in window
                decisions = conn.execute("""
                    SELECT channel_id, node_name, timestamp, confidence,
                           snapshot_metrics
                    FROM ai_decisions
                    WHERE decision_type = ? AND timestamp > ?
                      AND channel_id IS NOT NULL
                """, (action_type, cutoff)).fetchall()

                treatment_channels = {r['channel_id'] for r in decisions}

                if not treatment_channels:
                    return {
                        "status": "no_data",
                        "narrative": f"No {action_type} decisions found in the last {days} days."
                    }

                # Get revenue data for treatment channels (after decision)
                treatment_rev = []
                for dec in decisions:
                    ch_id = dec['channel_id']
                    dec_time = dec['timestamp']
                    rows = conn.execute("""
                        SELECT AVG(fees_earned_sats) as avg_rev,
                               SUM(forward_count) as total_fwd,
                               COUNT(*) as samples
                        FROM channel_history
                        WHERE channel_id = ? AND node_name = ?
                          AND timestamp > ? AND timestamp < ?
                    """, (ch_id, dec['node_name'], dec_time,
                          dec_time + 3 * 86400)).fetchone()
                    if rows and rows['samples'] and rows['samples'] > 0:
                        treatment_rev.append({
                            "channel_id": ch_id,
                            "avg_rev": rows['avg_rev'] or 0,
                            "total_fwd": rows['total_fwd'] or 0,
                            "samples": rows['samples'],
                        })

                # Get revenue data for control channels (not in treatment) — single batch query
                control_rev = []
                control_rows = conn.execute("""
                    SELECT channel_id, node_name,
                           AVG(fees_earned_sats) as avg_rev,
                           SUM(forward_count) as total_fwd,
                           COUNT(*) as samples
                    FROM channel_history
                    WHERE timestamp > ?
                      AND channel_id IS NOT NULL
                    GROUP BY channel_id, node_name
                    HAVING samples > 0
                """, (cutoff,)).fetchall()

                for row in control_rows:
                    ch_id = row['channel_id']
                    if ch_id in treatment_channels:
                        continue
                    control_rev.append({
                        "channel_id": ch_id,
                        "avg_rev": row['avg_rev'] or 0,
                        "total_fwd": row['total_fwd'] or 0,
                        "samples": row['samples'],
                    })

        except Exception as e:
            return {"status": "error", "narrative": f"Analysis failed: {str(e)}"}

        # Compare treatment vs control
        treatment_avg = (
            sum(r['avg_rev'] for r in treatment_rev) / len(treatment_rev)
            if treatment_rev else 0
        )
        control_avg = (
            sum(r['avg_rev'] for r in control_rev) / len(control_rev)
            if control_rev else 0
        )
        treatment_fwd = (
            sum(r['total_fwd'] for r in treatment_rev) / len(treatment_rev)
            if treatment_rev else 0
        )
        control_fwd = (
            sum(r['total_fwd'] for r in control_rev) / len(control_rev)
            if control_rev else 0
        )

        # Generate narrative
        if treatment_avg > control_avg * 1.1 and control_avg > 0:
            impact = "positive"
            improvement_pct = ((treatment_avg / control_avg) - 1) * 100
            narrative = (
                f"Anchored channels earned {treatment_avg:.1f} avg sats vs "
                f"{control_avg:.1f} for non-anchored (a {improvement_pct:.0f}% improvement). "
                f"Fee anchors appear to be helping."
            )
        elif treatment_avg > control_avg * 1.1:
            impact = "positive"
            narrative = (
                f"Anchored channels earned {treatment_avg:.1f} avg sats vs "
                f"{control_avg:.1f} for non-anchored. Fee anchors appear to be helping "
                f"(control baseline near zero)."
            )
        elif treatment_avg < control_avg * 0.9:
            impact = "negative"
            narrative = (
                f"Anchored channels earned {treatment_avg:.1f} avg sats vs "
                f"{control_avg:.1f} for non-anchored. Fee anchors may be hurting — "
                f"consider different fee targets or let the optimizer work autonomously."
            )
        else:
            impact = "neutral"
            narrative = (
                f"Anchored channels earned {treatment_avg:.1f} avg sats vs "
                f"{control_avg:.1f} for non-anchored — no significant difference. "
                f"May need more time or more aggressive fee exploration."
            )

        return {
            "status": "ok",
            "action_type": action_type,
            "days": days,
            "treatment_count": len(treatment_rev),
            "control_count": len(control_rev),
            "treatment_avg_revenue": round(treatment_avg, 2),
            "control_avg_revenue": round(control_avg, 2),
            "treatment_avg_forwards": round(treatment_fwd, 1),
            "control_avg_forwards": round(control_fwd, 1),
            "estimated_impact": impact,
            "narrative": narrative,
        }

    # =========================================================================
    # Config Gradient Tracking
    # =========================================================================

    def config_gradient(self, config_key: str, node_name: str = None) -> Dict[str, Any]:
        """
        Compute gradient direction for a config parameter.

        Instead of binary success/fail, tracks magnitude of improvement.
        Returns suggested direction and step size.
        """
        try:
            with self.db._get_conn() as conn:
                query = """
                    SELECT config_key, old_value, new_value, trigger_reason,
                           confidence, context_metrics, timestamp,
                           outcome_success, outcome_metrics
                    FROM config_adjustments
                    WHERE config_key = ?
                    ORDER BY timestamp DESC
                    LIMIT 20
                """
                params = [config_key]
                if node_name:
                    query = """
                        SELECT config_key, old_value, new_value, trigger_reason,
                               confidence, context_metrics, timestamp,
                               outcome_success, outcome_metrics, node_name
                        FROM config_adjustments
                        WHERE config_key = ? AND node_name = ?
                        ORDER BY timestamp DESC
                        LIMIT 20
                    """
                    params = [config_key, node_name]

                rows = conn.execute(query, params).fetchall()
                adjustments = [dict(r) for r in rows]
        except Exception as e:
            return {
                "status": "error",
                "config_key": config_key,
                "narrative": f"Failed to query adjustments: {str(e)}"
            }

        if not adjustments:
            return {
                "status": "no_data",
                "config_key": config_key,
                "narrative": f"No adjustment history for '{config_key}'. "
                             f"Try an initial change based on config_recommend()."
            }

        # Analyze direction and outcomes
        increases = []
        decreases = []
        for adj in adjustments:
            try:
                raw_old = adj.get('old_value')
                raw_new = adj.get('new_value')
                if raw_old is None or raw_new is None:
                    continue  # Skip adjustments with missing values
                old_val = float(raw_old)
                new_val = float(raw_new)
            except (ValueError, TypeError):
                continue

            success = adj.get('outcome_success')
            if success is None:
                continue  # Not yet measured

            direction = "increase" if new_val > old_val else "decrease" if new_val < old_val else "unchanged"
            entry = {
                "old": old_val,
                "new": new_val,
                "success": bool(success),
                "magnitude": abs(new_val - old_val),
            }

            # Parse outcome metrics for revenue delta if available
            outcome_metrics = adj.get('outcome_metrics')
            if outcome_metrics and isinstance(outcome_metrics, str):
                try:
                    outcome_metrics = json.loads(outcome_metrics)
                    entry["revenue_delta"] = outcome_metrics.get("revenue_delta", 0)
                except (json.JSONDecodeError, TypeError):
                    pass

            if direction == "increase":
                increases.append(entry)
            elif direction == "decrease":
                decreases.append(entry)

        # Compute gradient
        inc_success = sum(1 for x in increases if x['success']) / len(increases) if increases else 0
        dec_success = sum(1 for x in decreases if x['success']) / len(decreases) if decreases else 0

        if inc_success > dec_success + 0.1 and len(increases) >= 2:
            gradient_dir = "increase"
            suggested_step = sum(x['magnitude'] for x in increases) / len(increases)
            narrative = (
                f"Increasing '{config_key}' has worked {inc_success:.0%} of the time "
                f"({len(increases)} samples) vs decreasing at {dec_success:.0%}. "
                f"Suggest continuing upward by ~{suggested_step:.1f}."
            )
        elif dec_success > inc_success + 0.1 and len(decreases) >= 2:
            gradient_dir = "decrease"
            suggested_step = sum(x['magnitude'] for x in decreases) / len(decreases)
            narrative = (
                f"Decreasing '{config_key}' has worked {dec_success:.0%} of the time "
                f"({len(decreases)} samples) vs increasing at {inc_success:.0%}. "
                f"Suggest continuing downward by ~{suggested_step:.1f}."
            )
        else:
            gradient_dir = "uncertain"
            suggested_step = 0
            narrative = (
                f"No clear gradient for '{config_key}'. "
                f"Increases: {inc_success:.0%} ({len(increases)}), "
                f"Decreases: {dec_success:.0%} ({len(decreases)}). "
                f"Need more data or try a different approach."
            )

        return {
            "status": "ok",
            "config_key": config_key,
            "gradient_direction": gradient_dir,
            "suggested_step": round(suggested_step, 2),
            "increase_success_rate": round(inc_success, 2),
            "decrease_success_rate": round(dec_success, 2),
            "increase_samples": len(increases),
            "decrease_samples": len(decreases),
            "confidence": min(0.9, (len(increases) + len(decreases)) / 10),
            "narrative": narrative,
        }

    def suggest_exploration_fees(
        self,
        channel_id: str,
        node_name: str,
        current_fee: int,
    ) -> List[Dict[str, Any]]:
        """
        Multi-armed bandit exploration: suggest fee levels to try for stagnant channels.
        
        Returns a ranked list of fees to explore, with UCB-based priority.
        """
        exploration_fees = [25, 50, 100, 200, 500]
        
        # Get historical performance at each fee level
        suggestions = []
        cumulative_trials = 0
        per_fee_data = []
        try:
            with self.db._get_conn() as conn:
                for fee in exploration_fees:
                    low = int(fee * 0.7)
                    high = int(fee * 1.3)
                    
                    row = conn.execute("""
                        SELECT COUNT(*) as trials,
                               SUM(CASE WHEN forward_count > 0 THEN 1 ELSE 0 END) as successes,
                               AVG(fees_earned_sats) as avg_rev
                        FROM channel_history
                        WHERE channel_id = ? AND node_name = ?
                          AND fee_ppm BETWEEN ? AND ?
                    """, (channel_id, node_name, low, high)).fetchone()
                    
                    trials = row['trials'] or 0
                    successes = row['successes'] or 0
                    avg_rev = row['avg_rev'] or 0
                    cumulative_trials += trials

                    # UCB1 score: exploitation + exploration (total_trials computed after loop)
                    per_fee_data.append((fee, trials, successes, avg_rev))

            # Second pass: compute UCB with actual cumulative trial count
            total_trials = max(1, cumulative_trials)
            for fee, trials, successes, avg_rev in per_fee_data:
                if trials > 0:
                    exploit = avg_rev
                    explore = math.sqrt(2 * math.log(max(2, total_trials * 10)) / trials)
                    ucb = exploit + explore * 100  # Scale exploration bonus
                else:
                    ucb = float('inf')  # Untried = highest priority

                suggestions.append({
                    "fee_ppm": fee,
                    "trials": trials,
                    "successes": successes,
                    "avg_revenue": round(avg_rev, 2),
                    "ucb_score": round(ucb, 2) if ucb != float('inf') else 999999,
                    "recommendation": "explore" if trials < 3 else (
                        "exploit" if successes > 0 else "skip"
                    ),
                })
        except Exception:
            # Fallback: just return the fee levels
            suggestions = [{"fee_ppm": f, "trials": 0, "successes": 0,
                           "avg_revenue": 0, "ucb_score": 999999,
                           "recommendation": "explore"} for f in exploration_fees]
        
        # Sort by UCB score descending
        suggestions.sort(key=lambda x: x["ucb_score"], reverse=True)
        
        return suggestions
