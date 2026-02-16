"""
Revenue Predictor for Lightning Hive Fleet

Predicts expected revenue for different fee/balance configurations using
historical channel_history data from the advisor database.

Model: Log-linear regression with hand-crafted features.
Training data: channel_history records with forward_count > 0.

Key method: predict_optimal_fee(channel_features) -> (optimal_fee, expected_revenue)

Dependencies: standard library + numpy only.
"""

import json
import logging
import math
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

logger = logging.getLogger("revenue_predictor")


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class ChannelFeatures:
    """Features for a single channel at a point in time."""
    channel_id: str
    node_name: str
    fee_ppm: float
    balance_ratio: float  # local/capacity, 0-1
    capacity_sats: int
    forward_count: int  # recent forwards
    fees_earned_sats: int
    channel_age_days: float
    time_since_last_forward_hours: float
    peer_channel_count: int  # how many channels the peer has (if known)
    hour_of_day: int
    day_of_week: int

    def to_feature_vector(self) -> List[float]:
        """Convert to numerical feature vector for the model."""
        log_fee = math.log1p(self.fee_ppm)
        log_cap = math.log1p(self.capacity_sats)
        log_age = math.log1p(self.channel_age_days)
        log_tslf = math.log1p(self.time_since_last_forward_hours)
        log_peer_ch = math.log1p(self.peer_channel_count)

        # Balance quality: distance from ideal 0.5 (0 = perfect, 0.5 = worst)
        balance_quality = 1.0 - 2.0 * abs(self.balance_ratio - 0.5)

        # Interaction terms
        fee_x_balance = log_fee * self.balance_ratio
        cap_x_balance = log_cap * balance_quality

        return [
            1.0,  # bias
            log_fee,
            self.balance_ratio,
            balance_quality,
            log_cap,
            log_age,
            log_tslf,
            log_peer_ch,
            fee_x_balance,
            cap_x_balance,
            float(self.hour_of_day) / 24.0,
            float(self.day_of_week) / 7.0,
        ]


@dataclass
class FeeRecommendation:
    """Recommendation from the revenue predictor."""
    channel_id: str
    node_name: str
    current_fee_ppm: int
    optimal_fee_ppm: int
    expected_forwards_per_day: float
    expected_revenue_per_day: float  # sats
    confidence: float  # 0-1
    fee_curve: List[Dict[str, float]]  # [{fee_ppm, expected_revenue}]
    reasoning: str


@dataclass
class ChannelCluster:
    """A cluster of channels with similar behavior."""
    cluster_id: int
    label: str  # e.g. "high-cap active", "stagnant small"
    channel_ids: List[str]
    avg_fee_ppm: float
    avg_balance_ratio: float
    avg_capacity: float
    avg_forwards_per_day: float
    avg_revenue_per_day: float
    recommended_strategy: str


@dataclass
class TemporalPattern:
    """Time-based routing pattern for a channel."""
    channel_id: str
    node_name: str
    hourly_forward_rate: Dict[int, float]  # hour -> avg forwards
    daily_forward_rate: Dict[int, float]  # day_of_week -> avg forwards
    peak_hours: List[int]
    low_hours: List[int]
    peak_days: List[int]
    pattern_strength: float  # 0-1, how strong the temporal pattern is


# =============================================================================
# Revenue Predictor
# =============================================================================

class RevenuePredictor:
    """
    Predicts expected revenue for different fee/balance configurations.
    
    Uses log-linear regression trained on historical channel_history data.
    Model predicts log(1 + forwards_per_day) and log(1 + revenue_per_day).
    """

    # Fee levels to evaluate when finding optimal
    FEE_LEVELS = [25, 50, 100, 150, 200, 300, 500, 750, 1000, 1500, 2000, 2500]

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = str(Path.home() / ".lightning" / "advisor.db")
        self.db_path = db_path
        
        # Model weights (trained via least squares)
        self._forward_weights: Optional[List[float]] = None
        self._revenue_weights: Optional[List[float]] = None
        self._training_samples: int = 0
        self._last_trained: float = 0
        self._training_stats: Dict[str, Any] = {}
        
        # Channel cluster cache
        self._clusters: Optional[List[ChannelCluster]] = None
        self._cluster_assignments: Dict[str, int] = {}

    @contextmanager
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # =========================================================================
    # Training
    # =========================================================================

    def train(self, min_samples: int = 50) -> Dict[str, Any]:
        """
        Train the model on historical channel_history data.
        
        Returns training statistics.
        """
        logger.info("Training revenue predictor...")
        
        # Gather training data: aggregate per-channel-per-day
        training_data = self._gather_training_data()
        
        if len(training_data) < min_samples:
            logger.warning(f"Only {len(training_data)} samples, need {min_samples}")
            return {
                "status": "insufficient_data",
                "samples": len(training_data),
                "min_required": min_samples
            }

        # Build feature matrix and targets
        X = []
        y_forwards = []
        y_revenue = []
        
        for row in training_data:
            features = row["features"].to_feature_vector()
            X.append(features)
            y_forwards.append(math.log1p(row["forwards_per_day"]))
            y_revenue.append(math.log1p(row["revenue_per_day"]))

        if HAS_NUMPY:
            X_arr = np.array(X)
            y_fwd = np.array(y_forwards)
            y_rev = np.array(y_revenue)
            
            # Ridge regression (L2 regularization)
            lambda_reg = 1.0
            XtX = X_arr.T @ X_arr + lambda_reg * np.eye(X_arr.shape[1])
            
            self._forward_weights = [float(x) for x in np.linalg.solve(XtX, X_arr.T @ y_fwd)]
            self._revenue_weights = [float(x) for x in np.linalg.solve(XtX, X_arr.T @ y_rev)]
            
            # R² scores
            y_fwd_pred = X_arr @ np.array(self._forward_weights)
            y_rev_pred = X_arr @ np.array(self._revenue_weights)
            
            ss_res_fwd = np.sum((y_fwd - y_fwd_pred) ** 2)
            ss_tot_fwd = np.sum((y_fwd - np.mean(y_fwd)) ** 2)
            r2_fwd = float(1 - ss_res_fwd / ss_tot_fwd) if ss_tot_fwd > 0 else 0.0
            
            ss_res_rev = np.sum((y_rev - y_rev_pred) ** 2)
            ss_tot_rev = np.sum((y_rev - np.mean(y_rev)) ** 2)
            r2_rev = float(1 - ss_res_rev / ss_tot_rev) if ss_tot_rev > 0 else 0.0
        else:
            # Fallback: simple averages per fee bucket
            self._forward_weights = self._train_simple(X, y_forwards)
            self._revenue_weights = self._train_simple(X, y_revenue)
            r2_fwd = 0.0
            r2_rev = 0.0

        self._training_samples = len(training_data)
        self._last_trained = time.time()
        self._training_stats = {
            "status": "trained",
            "samples": len(training_data),
            "features": len(X[0]),
            "r2_forwards": round(r2_fwd, 4),
            "r2_revenue": round(r2_rev, 4),
            "trained_at": datetime.now().isoformat(),
            "has_numpy": HAS_NUMPY
        }
        
        logger.info(f"Trained on {len(training_data)} samples. "
                     f"R²(fwd)={r2_fwd:.3f}, R²(rev)={r2_rev:.3f}")
        
        # Also build clusters
        self._build_clusters(training_data)
        
        return self._training_stats

    def _train_simple(self, X: List[List[float]], y: List[float]) -> List[float]:
        """Fallback training without numpy - uses mean prediction."""
        n_features = len(X[0])
        weights = [0.0] * n_features
        weights[0] = sum(y) / len(y) if y else 0  # bias = mean
        return weights

    def _gather_training_data(self) -> List[Dict]:
        """
        Gather training data from channel_history.
        
        Aggregates per channel per 6-hour window (matching advisor cycle).
        """
        training_data = []
        
        with self._get_conn() as conn:
            # Get per-channel aggregated data grouped by ~6h windows
            rows = conn.execute("""
                SELECT 
                    channel_id, node_name,
                    AVG(fee_ppm) as avg_fee,
                    AVG(balance_ratio) as avg_balance,
                    AVG(capacity_sats) as avg_capacity,
                    SUM(forward_count) as total_forwards,
                    SUM(fees_earned_sats) as total_fees,
                    MIN(timestamp) as first_ts,
                    MAX(timestamp) as last_ts,
                    COUNT(*) as num_readings,
                    -- Group into 6h windows
                    CAST(timestamp / 21600 AS INT) as time_window
                FROM channel_history
                WHERE capacity_sats > 0
                GROUP BY channel_id, node_name, time_window
                HAVING num_readings >= 1
            """).fetchall()

            # Get channel first-seen times for age calculation
            channel_first_seen = {}
            first_seen_rows = conn.execute("""
                SELECT channel_id, node_name, MIN(timestamp) as first_ts
                FROM channel_history
                GROUP BY channel_id, node_name
            """).fetchall()
            for r in first_seen_rows:
                channel_first_seen[(r['channel_id'], r['node_name'])] = r['first_ts']
            
            for row in rows:
                first_ts = channel_first_seen.get(
                    (row['channel_id'], row['node_name']), row['first_ts']
                )
                age_days = (row['last_ts'] - first_ts) / 86400.0
                
                # Time window is 6h, scale to per-day
                window_hours = max(1, (row['last_ts'] - row['first_ts']) / 3600.0) if row['num_readings'] > 1 else 6.0
                forwards_per_day = (row['total_forwards'] or 0) * 24.0 / max(window_hours, 1)
                revenue_per_day = (row['total_fees'] or 0) * 24.0 / max(window_hours, 1)
                
                dt = datetime.fromtimestamp(row['first_ts'])
                
                features = ChannelFeatures(
                    channel_id=row['channel_id'],
                    node_name=row['node_name'],
                    fee_ppm=row['avg_fee'] or 0,
                    balance_ratio=row['avg_balance'] or 0,
                    capacity_sats=int(row['avg_capacity'] or 0),
                    forward_count=row['total_forwards'] or 0,
                    fees_earned_sats=row['total_fees'] or 0,
                    channel_age_days=max(0, age_days),
                    time_since_last_forward_hours=0,  # Not available in aggregate
                    peer_channel_count=0,  # Not in this table
                    hour_of_day=dt.hour,
                    day_of_week=dt.weekday(),
                )
                
                training_data.append({
                    "features": features,
                    "forwards_per_day": forwards_per_day,
                    "revenue_per_day": revenue_per_day,
                })
        
        return training_data

    # =========================================================================
    # Prediction
    # =========================================================================

    def _predict_raw(self, features: ChannelFeatures, 
                     weights: List[float]) -> float:
        """Make a raw prediction (log-space)."""
        x = features.to_feature_vector()
        pred = sum(w * xi for w, xi in zip(weights, x))
        return pred

    def predict_forwards_per_day(self, features: ChannelFeatures) -> float:
        """Predict expected forwards per day."""
        if not self._forward_weights:
            return 0.0
        raw = self._predict_raw(features, self._forward_weights)
        return max(0, math.expm1(raw))

    def predict_revenue_per_day(self, features: ChannelFeatures) -> float:
        """Predict expected revenue per day in sats."""
        if not self._revenue_weights:
            return 0.0
        raw = self._predict_raw(features, self._revenue_weights)
        return max(0, math.expm1(raw))

    def predict_optimal_fee(
        self,
        channel_id: str,
        node_name: str,
        current_fee_ppm: int = None,
        balance_ratio: float = None,
        capacity_sats: int = None,
        channel_age_days: float = None,
    ) -> FeeRecommendation:
        """
        Predict optimal fee for a channel by evaluating multiple fee levels.
        
        Fetches current channel state from DB if params not provided.
        Returns the fee that maximizes expected revenue.
        """
        # Auto-train if needed
        if not self._forward_weights:
            self.train()
        
        # Get current state from DB if not provided
        if any(v is None for v in [current_fee_ppm, balance_ratio, capacity_sats]):
            state = self._get_latest_channel_state(channel_id, node_name)
            if state:
                current_fee_ppm = current_fee_ppm if current_fee_ppm is not None else state.get('fee_ppm', 100)
                balance_ratio = balance_ratio if balance_ratio is not None else state.get('balance_ratio', 0.5)
                capacity_sats = capacity_sats if capacity_sats is not None else state.get('capacity_sats', 5000000)
                channel_age_days = channel_age_days if channel_age_days is not None else 30
            else:
                # Defaults
                current_fee_ppm = current_fee_ppm if current_fee_ppm is not None else 100
                balance_ratio = balance_ratio if balance_ratio is not None else 0.5
                capacity_sats = capacity_sats if capacity_sats is not None else 5000000
                channel_age_days = channel_age_days if channel_age_days is not None else 30

        now = datetime.now()
        
        # Evaluate each fee level
        fee_curve = []
        best_fee = current_fee_ppm
        best_revenue = 0.0
        best_forwards = 0.0
        
        for fee in self.FEE_LEVELS:
            features = ChannelFeatures(
                channel_id=channel_id,
                node_name=node_name,
                fee_ppm=fee,
                balance_ratio=balance_ratio,
                capacity_sats=capacity_sats,
                forward_count=0,
                fees_earned_sats=0,
                channel_age_days=channel_age_days,
                time_since_last_forward_hours=0,
                peer_channel_count=0,
                hour_of_day=now.hour,
                day_of_week=now.weekday(),
            )
            
            fwd = self.predict_forwards_per_day(features)
            rev = self.predict_revenue_per_day(features)
            
            fee_curve.append({
                "fee_ppm": fee,
                "expected_forwards_per_day": round(fwd, 3),
                "expected_revenue_per_day": round(rev, 3),
            })
            
            if rev > best_revenue:
                best_revenue = rev
                best_fee = fee
                best_forwards = fwd

        # If model R² is very low, fall back to Bayesian posteriors
        r2 = self._training_stats.get("r2_revenue", 0)
        if r2 < 0.1 and self._forward_weights:
            posteriors = self.bayesian_fee_posterior(channel_id, node_name)
            # Use posterior mean as primary signal
            best_post_fee = None
            best_post_mean = -1
            for fee_level, post in posteriors.items():
                if post.get("observations", 0) > 0 and post["mean"] > best_post_mean:
                    best_post_mean = post["mean"]
                    best_post_fee = fee_level
            if best_post_fee is not None:
                best_fee = best_post_fee
                best_revenue = best_post_mean
                # Estimate forwards: revenue_per_day / (fee_ppm / 1e6) / avg_forward_size
                # Simplified: if we earn X sats/day at Y ppm, rough forward count ~ X / (Y * avg_capacity * 1e-6)
                # Use simple heuristic: low revenue = low forwards
                best_forwards = max(0.001, best_post_mean * 0.1)  # ~0.1 forwards per sat/day as rough proxy

        # Confidence based on training quality and data availability
        confidence = self._calculate_confidence(channel_id, node_name)

        # Generate reasoning
        if best_fee > current_fee_ppm * 1.5:
            reasoning = f"Model suggests significantly higher fee ({best_fee} vs {current_fee_ppm} ppm). Channel may be underpriced."
        elif best_fee < current_fee_ppm * 0.5:
            reasoning = f"Model suggests lower fee ({best_fee} vs {current_fee_ppm} ppm). Current fee may be suppressing volume."
        elif best_revenue < 1.0:
            reasoning = f"Low expected revenue ({best_revenue:.1f} sats/day) at any fee level. Channel may need rebalancing or different strategy."
        else:
            reasoning = f"Optimal fee ~{best_fee} ppm, expected {best_revenue:.1f} sats/day revenue."

        return FeeRecommendation(
            channel_id=channel_id,
            node_name=node_name,
            current_fee_ppm=current_fee_ppm,
            optimal_fee_ppm=best_fee,
            expected_forwards_per_day=round(best_forwards, 3),
            expected_revenue_per_day=round(best_revenue, 3),
            confidence=confidence,
            fee_curve=fee_curve,
            reasoning=reasoning,
        )

    def estimate_rebalance_benefit(self, channel_id: str, node_name: str,
                                    target_ratio: float = 0.5) -> Dict:
        """
        Estimate revenue gain from rebalancing a channel to target_ratio.

        Uses historical data: find periods when this channel had good balance
        and compare revenue vs periods with poor balance.

        Returns dict with estimated benefit, max rebalance cost, and reasoning.
        """
        with self._get_conn() as conn:
            cutoff = int((datetime.now() - timedelta(days=30)).timestamp())

            rows = conn.execute("""
                SELECT balance_ratio, fees_earned_sats, forward_count,
                       timestamp
                FROM channel_history
                WHERE channel_id = ? AND node_name = ?
                  AND timestamp > ?
                ORDER BY timestamp
            """, (channel_id, node_name, cutoff)).fetchall()

            if not rows:
                return {
                    "channel_id": channel_id,
                    "current_ratio": None,
                    "target_ratio": target_ratio,
                    "estimated_daily_revenue_current": 0,
                    "estimated_daily_revenue_target": 0,
                    "estimated_weekly_gain": 0,
                    "max_rebalance_cost": 0,
                    "confidence": 0.1,
                    "reasoning": "No historical data for this channel. Cannot estimate benefit."
                }

            # Current state
            latest = dict(rows[-1])
            current_ratio = latest.get('balance_ratio')
            if current_ratio is None:
                current_ratio = 0.5

            # Bucket by balance quality: "good" (0.3-0.7) vs "poor" (<0.2 or >0.8)
            good_rev = []
            poor_rev = []
            for r in rows:
                br = r['balance_ratio'] if r['balance_ratio'] is not None else 0.5
                rev = r['fees_earned_sats'] or 0
                if 0.3 <= br <= 0.7:
                    good_rev.append(rev)
                elif br < 0.2 or br > 0.8:
                    poor_rev.append(rev)

            # Compute averages per 6h window
            good_avg = sum(good_rev) / len(good_rev) if good_rev else 0
            poor_avg = sum(poor_rev) / len(poor_rev) if poor_rev else 0

            # Extrapolate to 7 days (4 windows/day * 7 days = 28 windows)
            daily_good = good_avg * 4
            daily_poor = poor_avg * 4
            weekly_gain = (good_avg - poor_avg) * 28

            # Max rebalance cost = 20% of estimated weekly gain
            max_cost = max(0, int(weekly_gain * 0.2))

            # Confidence based on data
            data_points = len(good_rev) + len(poor_rev)
            if data_points >= 50:
                confidence = 0.7
            elif data_points >= 20:
                confidence = 0.5
            elif data_points >= 5:
                confidence = 0.3
            else:
                confidence = 0.15

            # Adjust confidence down if no good-balance periods observed
            if not good_rev:
                confidence *= 0.5
                reasoning = (
                    f"Channel has never been well-balanced (0.3-0.7) in the last 30 days. "
                    f"Currently at {current_ratio:.0%}. Rebalancing could help but we have no "
                    f"revenue data from balanced periods to estimate benefit."
                )
            elif weekly_gain <= 0:
                reasoning = (
                    f"Historical data shows no revenue improvement when balanced vs imbalanced. "
                    f"Good-balance avg: {good_avg:.1f} sats/6h, Poor-balance avg: {poor_avg:.1f} sats/6h. "
                    f"Rebalancing this channel may not improve revenue."
                )
            else:
                reasoning = (
                    f"When balanced (0.3-0.7), this channel earns ~{daily_good:.1f} sats/day vs "
                    f"~{daily_poor:.1f} sats/day when imbalanced. Estimated weekly gain: {weekly_gain:.0f} sats. "
                    f"Worth spending up to {max_cost} sats on rebalancing."
                )

            return {
                "channel_id": channel_id,
                "current_ratio": round(current_ratio, 3),
                "target_ratio": target_ratio,
                "estimated_daily_revenue_current": round(daily_poor if (current_ratio < 0.2 or current_ratio > 0.8) else daily_good, 2),
                "estimated_daily_revenue_target": round(daily_good, 2),
                "estimated_weekly_gain": round(max(0, weekly_gain), 2),
                "max_rebalance_cost": max_cost,
                "confidence": round(confidence, 2),
                "reasoning": reasoning,
            }

    def get_mab_recommendation(self, channel_id: str, node_name: str) -> Dict:
        """
        Get next fee to try for a stagnant channel using multi-armed bandit.

        Wraps bayesian_fee_posterior into a single actionable recommendation.
        Returns the fee level with highest UCB that hasn't been tried,
        or the best-performing fee if all have been tried.
        """
        posteriors = self.bayesian_fee_posterior(channel_id, node_name)

        if not posteriors:
            return {
                "channel_id": channel_id,
                "recommended_fee_ppm": 50,
                "strategy": "explore",
                "confidence": 0.2,
                "reasoning": "No posterior data available. Starting with moderate fee of 50 ppm."
            }

        # Find fee with highest UCB (exploration-exploitation balance)
        best_ucb_fee = None
        best_ucb = -float('inf')
        best_mean_fee = None
        best_mean = -float('inf')
        untried_fees = []

        for fee, post in posteriors.items():
            ucb = post.get("ucb", 0)
            mean = post.get("mean", 0)
            obs = post.get("observations", 0)

            if obs == 0:
                untried_fees.append(int(fee))

            if ucb > best_ucb:
                best_ucb = ucb
                best_ucb_fee = int(fee)

            if mean > best_mean and obs > 0:
                best_mean = mean
                best_mean_fee = int(fee)

        if untried_fees:
            # Prioritize middle-range untried fees (min 25 ppm per safety constraints)
            preferred_order = [25, 50, 100, 200, 500, 1000, 2000]
            for pf in preferred_order:
                if pf in untried_fees:
                    recommended = pf
                    break
            else:
                recommended = untried_fees[0]
            strategy = "explore"
            reasoning = (
                f"Fee levels {untried_fees} have never been tried. "
                f"Recommending {recommended} ppm to explore. "
                f"UCB analysis favors {best_ucb_fee} ppm."
            )
        elif best_mean_fee and best_mean > 0:
            recommended = best_mean_fee
            strategy = "exploit"
            reasoning = (
                f"All fee levels tested. Best performer: {best_mean_fee} ppm "
                f"(avg revenue {best_mean:.2f} sats/day). Recommending exploitation."
            )
        else:
            recommended = best_ucb_fee or 50
            strategy = "explore"
            reasoning = (
                f"All fee levels tested but none produced revenue. "
                f"UCB suggests {best_ucb_fee} ppm. Channel may need rebalancing first."
            )

        return {
            "channel_id": channel_id,
            "recommended_fee_ppm": recommended,
            "strategy": strategy,
            "ucb_best_fee": best_ucb_fee,
            "mean_best_fee": best_mean_fee,
            "untried_fees": untried_fees,
            "confidence": 0.3 if strategy == "explore" else 0.6,
            "reasoning": reasoning,
            "posteriors_summary": {
                str(k): {"mean": round(v.get("mean", 0), 2), "obs": v.get("observations", 0)}
                for k, v in posteriors.items()
            },
        }

    def _get_latest_channel_state(self, channel_id: str, node_name: str) -> Optional[Dict]:
        """Get most recent channel state from DB."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM channel_history
                WHERE channel_id = ? AND node_name = ?
                ORDER BY timestamp DESC LIMIT 1
            """, (channel_id, node_name)).fetchone()
            return dict(row) if row else None

    def _calculate_confidence(self, channel_id: str, node_name: str) -> float:
        """Calculate prediction confidence for a channel."""
        if not self._forward_weights:
            return 0.1
        
        base = 0.3  # Base confidence from having a trained model
        
        # Bonus for training quality
        r2 = self._training_stats.get("r2_revenue", 0)
        base += r2 * 0.3  # Up to 0.3 bonus
        
        # Bonus for having data on this specific channel
        with self._get_conn() as conn:
            count = conn.execute("""
                SELECT COUNT(*) as cnt FROM channel_history
                WHERE channel_id = ? AND node_name = ?
            """, (channel_id, node_name)).fetchone()['cnt']
        
        if count > 50:
            base += 0.2
        elif count > 20:
            base += 0.1
        elif count > 5:
            base += 0.05
        
        return min(0.9, base)

    # =========================================================================
    # Bayesian Fee Optimization
    # =========================================================================

    def bayesian_fee_posterior(
        self,
        channel_id: str,
        node_name: str,
        fee_levels: List[int] = None,
    ) -> Dict[int, Dict[str, float]]:
        """
        Compute Bayesian posterior distribution of revenue per fee level.
        
        Uses historical data as observations and a conjugate prior.
        Returns posterior mean and variance for each fee level.
        
        This is essentially a multi-armed bandit with Gaussian rewards.
        """
        if fee_levels is None:
            fee_levels = [25, 50, 100, 200, 500, 1000, 2000]
        
        # Prior: mean=0.5 sats/day, variance=100 (vague)
        prior_mean = 0.5
        prior_var = 100.0
        
        posteriors = {}

        with self._get_conn() as conn:
            # First pass: collect observation counts per fee level
            fee_observations = {}
            fee_stats = {}
            for fee in fee_levels:
                low = int(fee * 0.7)
                high = int(fee * 1.3)

                rows = conn.execute("""
                    SELECT fees_earned_sats, forward_count,
                           (MAX(timestamp) - MIN(timestamp)) as window_secs
                    FROM channel_history
                    WHERE channel_id = ? AND node_name = ?
                      AND fee_ppm BETWEEN ? AND ?
                    GROUP BY CAST(timestamp / 21600 AS INT)
                    HAVING window_secs > 0 OR COUNT(*) = 1
                """, (channel_id, node_name, low, high)).fetchall()

                observations = []
                for r in rows:
                    window_h = max(6, (r['window_secs'] or 21600) / 3600)
                    rev_per_day = (r['fees_earned_sats'] or 0) * 24.0 / window_h
                    observations.append(rev_per_day)

                fee_observations[fee] = observations

            # Total observations across all fee levels for this channel
            channel_total_obs = sum(len(obs) for obs in fee_observations.values())

            # Second pass: compute posteriors with correct UCB
            for fee in fee_levels:
                observations = fee_observations[fee]
                n = len(observations)
                if n == 0:
                    posteriors[fee] = {
                        "mean": prior_mean,
                        "variance": prior_var,
                        "observations": 0,
                        "ucb": prior_mean + math.sqrt(2 * prior_var),  # Optimistic
                    }
                else:
                    obs_mean = sum(observations) / n
                    obs_var = max(1.0, sum((x - obs_mean)**2 for x in observations) / n)

                    # Bayesian update (conjugate normal)
                    post_var = 1.0 / (1.0 / prior_var + n / obs_var)
                    post_mean = post_var * (prior_mean / prior_var + n * obs_mean / obs_var)

                    # UCB: use channel-level total observations as denominator
                    ucb = post_mean + math.sqrt(2 * post_var * math.log(max(2, channel_total_obs)) / max(1, n))

                    posteriors[fee] = {
                        "mean": round(post_mean, 3),
                        "variance": round(post_var, 3),
                        "observations": n,
                        "ucb": round(ucb, 3),
                    }
        
        return posteriors

    # =========================================================================
    # Channel Clustering
    # =========================================================================

    def _build_clusters(self, training_data: List[Dict]) -> None:
        """
        Build channel clusters using simple k-means-like approach.
        
        Clusters channels by: capacity, forward rate, balance, fee level.
        """
        if not training_data:
            return
        
        # Aggregate per-channel
        channel_agg: Dict[str, Dict] = {}
        for row in training_data:
            f = row["features"]
            key = f"{f.node_name}|{f.channel_id}"
            if key not in channel_agg:
                channel_agg[key] = {
                    "channel_id": f.channel_id,
                    "node_name": f.node_name,
                    "fees": [], "balances": [], "caps": [],
                    "fwds": [], "revs": [],
                }
            channel_agg[key]["fees"].append(f.fee_ppm)
            channel_agg[key]["balances"].append(f.balance_ratio)
            channel_agg[key]["caps"].append(f.capacity_sats)
            channel_agg[key]["fwds"].append(row["forwards_per_day"])
            channel_agg[key]["revs"].append(row["revenue_per_day"])
        
        # Create feature vectors for clustering
        channels = []
        for key, data in channel_agg.items():
            avg_fee = sum(data["fees"]) / len(data["fees"])
            avg_bal = sum(data["balances"]) / len(data["balances"])
            avg_cap = sum(data["caps"]) / len(data["caps"])
            avg_fwd = sum(data["fwds"]) / len(data["fwds"])
            avg_rev = sum(data["revs"]) / len(data["revs"])
            
            channels.append({
                "key": key,
                "channel_id": data["channel_id"],
                "node_name": data["node_name"],
                "vec": [
                    math.log1p(avg_cap) / 20,  # Normalize
                    avg_bal,
                    math.log1p(avg_fee) / 10,
                    math.log1p(avg_fwd) / 5,
                ],
                "avg_fee": avg_fee,
                "avg_balance": avg_bal,
                "avg_cap": avg_cap,
                "avg_fwd": avg_fwd,
                "avg_rev": avg_rev,
            })
        
        if len(channels) < 4:
            self._clusters = []
            return
        
        # Simple k-means with k=4
        k = min(4, len(channels))
        clusters = self._kmeans(channels, k)
        
        self._clusters = []
        self._cluster_assignments = {}
        
        labels = [
            "high-volume earners",
            "balanced moderate",
            "stagnant/imbalanced",
            "low-fee explorers",
        ]
        
        for i, members in enumerate(clusters):
            if not members:
                continue
            
            avg_fee = sum(m["avg_fee"] for m in members) / len(members)
            avg_bal = sum(m["avg_balance"] for m in members) / len(members)
            avg_cap = sum(m["avg_cap"] for m in members) / len(members)
            avg_fwd = sum(m["avg_fwd"] for m in members) / len(members)
            avg_rev = sum(m["avg_rev"] for m in members) / len(members)
            
            # Determine strategy based on cluster characteristics
            if avg_fwd > 5:
                strategy = "Protect and optimize: fine-tune fees, ensure balance stays healthy"
                label = "high-volume earners"
            elif avg_bal > 0.85 or avg_bal < 0.15:
                strategy = "Rebalance urgently, then explore lower fees to attract flow"
                label = "stagnant/imbalanced"
            elif avg_fwd < 0.5:
                strategy = "Aggressive fee exploration (MAB): try 25, 50, 100, 200, 500 ppm"
                label = "stagnant low-flow"
            else:
                strategy = "Moderate fee adjustment, monitor for improvement"
                label = "balanced moderate"
            
            channel_ids = [m["channel_id"] for m in members]
            
            cluster = ChannelCluster(
                cluster_id=i,
                label=label,
                channel_ids=channel_ids,
                avg_fee_ppm=round(avg_fee, 1),
                avg_balance_ratio=round(avg_bal, 3),
                avg_capacity=round(avg_cap),
                avg_forwards_per_day=round(avg_fwd, 3),
                avg_revenue_per_day=round(avg_rev, 3),
                recommended_strategy=strategy,
            )
            self._clusters.append(cluster)
            
            for m in members:
                self._cluster_assignments[m["key"]] = i

    def _kmeans(self, items: List[Dict], k: int, max_iter: int = 20) -> List[List[Dict]]:
        """Simple k-means clustering."""
        import random
        
        # Initialize centroids randomly
        centroids = [items[i]["vec"][:] for i in random.sample(range(len(items)), k)]
        
        clusters = [[] for _ in range(k)]
        
        for _ in range(max_iter):
            clusters = [[] for _ in range(k)]
            
            # Assign
            for item in items:
                dists = [sum((a - b)**2 for a, b in zip(item["vec"], c)) for c in centroids]
                best = dists.index(min(dists))
                clusters[best].append(item)
            
            # Update centroids
            new_centroids = []
            for i, cluster in enumerate(clusters):
                if cluster:
                    dim = len(cluster[0]["vec"])
                    new_c = [sum(m["vec"][d] for m in cluster) / len(cluster) for d in range(dim)]
                    new_centroids.append(new_c)
                else:
                    new_centroids.append(centroids[i])
            
            if new_centroids == centroids:
                break
            centroids = new_centroids
        
        return clusters

    def get_clusters(self) -> List[ChannelCluster]:
        """Get channel clusters. Trains model if needed."""
        if self._clusters is None:
            self.train()
        return self._clusters or []

    # =========================================================================
    # Temporal Patterns
    # =========================================================================

    def get_temporal_patterns(
        self,
        channel_id: str,
        node_name: str,
        days: int = 14,
    ) -> Optional[TemporalPattern]:
        """
        Analyze time-of-day and day-of-week routing patterns.
        """
        with self._get_conn() as conn:
            cutoff = int((datetime.now() - timedelta(days=days)).timestamp())
            
            rows = conn.execute("""
                SELECT timestamp, forward_count, fees_earned_sats
                FROM channel_history
                WHERE channel_id = ? AND node_name = ?
                  AND timestamp > ?
                ORDER BY timestamp
            """, (channel_id, node_name, cutoff)).fetchall()
            
            if len(rows) < 10:
                return None
            
            # Aggregate by hour and day
            hourly: Dict[int, List[float]] = {h: [] for h in range(24)}
            daily: Dict[int, List[float]] = {d: [] for d in range(7)}
            
            for row in rows:
                dt = datetime.fromtimestamp(row['timestamp'])
                fwd = row['forward_count'] or 0
                hourly[dt.hour].append(fwd)
                daily[dt.weekday()].append(fwd)
            
            # Calculate averages
            hourly_avg = {}
            for h, vals in hourly.items():
                hourly_avg[h] = sum(vals) / len(vals) if vals else 0
            
            daily_avg = {}
            for d, vals in daily.items():
                daily_avg[d] = sum(vals) / len(vals) if vals else 0
            
            # Find peaks and lows
            overall_avg = sum(hourly_avg.values()) / max(1, sum(1 for v in hourly_avg.values() if v > 0))
            
            peak_hours = [h for h, v in hourly_avg.items() if v > overall_avg * 1.3 and v > 0]
            low_hours = [h for h, v in hourly_avg.items() if v < overall_avg * 0.5 or v == 0]
            
            daily_overall = sum(daily_avg.values()) / max(1, sum(1 for v in daily_avg.values() if v > 0))
            peak_days = [d for d, v in daily_avg.items() if v > daily_overall * 1.2 and v > 0]
            
            # Pattern strength: coefficient of variation
            all_hourly = [v for v in hourly_avg.values() if v > 0]
            if all_hourly and len(all_hourly) > 1:
                mean_h = sum(all_hourly) / len(all_hourly)
                std_h = math.sqrt(sum((v - mean_h)**2 for v in all_hourly) / len(all_hourly))
                pattern_strength = min(1.0, std_h / max(mean_h, 0.01))
            else:
                pattern_strength = 0.0
            
            return TemporalPattern(
                channel_id=channel_id,
                node_name=node_name,
                hourly_forward_rate=hourly_avg,
                daily_forward_rate=daily_avg,
                peak_hours=sorted(peak_hours),
                low_hours=sorted(low_hours),
                peak_days=sorted(peak_days),
                pattern_strength=round(pattern_strength, 3),
            )

    # =========================================================================
    # Learning Engine Integration
    # =========================================================================

    def get_insights(self) -> Dict[str, Any]:
        """
        Get a summary of everything the predictor has learned.
        For use by the MCP learning_engine_insights tool.
        """
        insights = {
            "model_status": "trained" if self._forward_weights else "untrained",
            "training_stats": self._training_stats,
            "cluster_count": len(self._clusters) if self._clusters else 0,
            "clusters": [],
        }
        
        if self._clusters:
            for c in self._clusters:
                insights["clusters"].append({
                    "id": c.cluster_id,
                    "label": c.label,
                    "channels": len(c.channel_ids),
                    "avg_fee": c.avg_fee_ppm,
                    "avg_fwd_per_day": c.avg_forwards_per_day,
                    "avg_rev_per_day": c.avg_revenue_per_day,
                    "strategy": c.recommended_strategy,
                })
        
        # Top/bottom channels by predicted revenue
        if self._forward_weights:
            insights["feature_names"] = [
                "bias", "log_fee", "balance_ratio", "balance_quality",
                "log_capacity", "log_age", "log_time_since_fwd",
                "log_peer_channels", "fee_x_balance", "cap_x_balance",
                "hour_norm", "day_norm",
            ]
            insights["forward_weights"] = [round(w, 4) for w in self._forward_weights]
            if self._revenue_weights:
                insights["revenue_weights"] = [round(w, 4) for w in self._revenue_weights]
        
        return insights

    def get_training_stats(self) -> Dict[str, Any]:
        """Get training statistics."""
        return self._training_stats


# =============================================================================
# Module-level singleton
# =============================================================================

_predictor: Optional[RevenuePredictor] = None

def get_predictor(db_path: str = None) -> RevenuePredictor:
    """Get or create the singleton predictor instance."""
    global _predictor
    if _predictor is None:
        _predictor = RevenuePredictor(db_path)
    return _predictor
