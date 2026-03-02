"""
Goal Manager for Proactive AI Advisor

Manages measurable goals for node optimization and tracks progress.
Goals are set based on node analysis and strategy, with progress
checked each cycle and strategy adjusted if off-track.

Usage:
    from goal_manager import GoalManager, Goal

    manager = GoalManager(db)
    goals = manager.analyze_and_set_goals(node_state)
    progress = manager.check_progress(goal, current_value)
"""

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional


def _unique_suffix() -> str:
    """Short random hex suffix for ID uniqueness."""
    return os.urandom(4).hex()


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class Goal:
    """A measurable objective for the advisor to pursue."""
    goal_id: str
    goal_type: str          # "roc", "routing_volume", "channel_health", "profitability"
    target_metric: str      # e.g., "roc_pct", "underwater_pct", "daily_volume_sats"
    current_value: float
    target_value: float
    deadline_days: int
    created_at: int
    priority: int           # 1-5, higher = more important

    # Tracking
    checkpoints: List[Dict] = field(default_factory=list)  # [{timestamp, value, notes}]
    status: str = "active"  # "active", "achieved", "failed", "abandoned"

    def to_dict(self) -> Dict[str, Any]:
        """Convert goal to dictionary for storage."""
        return {
            "goal_id": self.goal_id,
            "goal_type": self.goal_type,
            "target_metric": self.target_metric,
            "current_value": self.current_value,
            "target_value": self.target_value,
            "deadline_days": self.deadline_days,
            "created_at": self.created_at,
            "priority": self.priority,
            "checkpoints": self.checkpoints,
            "status": self.status
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Goal":
        """Create goal from dictionary."""
        return cls(
            goal_id=data["goal_id"],
            goal_type=data["goal_type"],
            target_metric=data["target_metric"],
            current_value=data["current_value"],
            target_value=data["target_value"],
            deadline_days=data["deadline_days"],
            created_at=data["created_at"],
            priority=data["priority"],
            checkpoints=data.get("checkpoints", []),
            status=data.get("status", "active")
        )


@dataclass
class GoalProgress:
    """Progress tracking for a goal."""
    goal_id: str
    on_track: bool
    progress_pct: float
    days_elapsed: float
    days_remaining: float
    velocity_needed: float  # Change per day needed to meet goal
    current_velocity: float  # Actual change per day observed
    recommendation: str
    status_emoji: str


# =============================================================================
# Goal Templates
# =============================================================================

GOAL_TEMPLATES = {
    "improve_roc": {
        "goal_type": "profitability",
        "target_metric": "roc_pct",
        "description": "Improve return on capital",
        "default_deadline_days": 30,
        "priority": 5
    },
    "reduce_underwater": {
        "goal_type": "channel_health",
        "target_metric": "underwater_pct",
        "description": "Reduce percentage of underwater channels",
        "default_deadline_days": 45,
        "priority": 4
    },
    "increase_routing": {
        "goal_type": "routing_volume",
        "target_metric": "daily_forwards_sats",
        "description": "Increase daily routing volume",
        "default_deadline_days": 30,
        "priority": 3
    },
    "improve_balance": {
        "goal_type": "channel_health",
        "target_metric": "avg_balance_ratio",
        "description": "Improve average channel balance ratio toward 0.5",
        "default_deadline_days": 21,
        "priority": 3
    },
    "reduce_bleeders": {
        "goal_type": "channel_health",
        "target_metric": "bleeder_count",
        "description": "Reduce number of bleeding channels",
        "default_deadline_days": 14,
        "priority": 5
    },
    "improve_profitable_pct": {
        "goal_type": "profitability",
        "target_metric": "profitable_pct",
        "description": "Increase percentage of profitable channels",
        "default_deadline_days": 30,
        "priority": 4
    }
}


# =============================================================================
# Goal Manager
# =============================================================================

class GoalManager:
    """
    Manages advisor goals and tracks progress.

    Goals are set based on node analysis and strategy.
    Progress is checked each cycle and strategy adjusted if off-track.
    """

    def __init__(self, db):
        """
        Initialize goal manager.

        Args:
            db: AdvisorDB instance for persistence
        """
        self.db = db
        self._goals_cache: Dict[str, Goal] = {}

    def get_active_goals(self) -> List[Goal]:
        """
        Get all active goals from the database.

        Returns:
            List of active Goal objects
        """
        goals = self.db.get_goals(status="active")
        return [Goal.from_dict(g) for g in goals]

    def get_goal(self, goal_id: str) -> Optional[Goal]:
        """Get a specific goal by ID."""
        goal_data = self.db.get_goal(goal_id)
        if goal_data:
            return Goal.from_dict(goal_data)
        return None

    def analyze_and_set_goals(self, node_state: Dict[str, Any]) -> List[Goal]:
        """
        Analyze current node state and set appropriate goals.

        Called when advisor starts or when goals need refresh.

        Args:
            node_state: Dictionary containing node metrics:
                - roc_pct: Return on capital percentage
                - underwater_pct: Percentage of underwater channels
                - profitable_pct: Percentage of profitable channels
                - avg_balance_ratio: Average channel balance ratio
                - bleeder_count: Number of bleeding channels
                - daily_forwards_sats: Daily routing volume

        Returns:
            List of newly created goals
        """
        goals = []
        now = int(time.time())

        # ROC Goal: If ROC < 0.5%, set improvement target
        current_roc = node_state.get("roc_pct", 0)
        if current_roc < 0.5:
            # Double ROC or reach 0.5%, whichever is less aggressive
            target = min(0.5, current_roc * 2) if current_roc > 0.1 else 0.3
            goal = Goal(
                goal_id=f"roc_{now}_{_unique_suffix()}",
                goal_type="profitability",
                target_metric="roc_pct",
                current_value=current_roc,
                target_value=target,
                deadline_days=30,
                created_at=now,
                priority=5,  # Highest priority
                checkpoints=[],
                status="active"
            )
            goals.append(goal)

        # Underwater Goal: If >30% underwater, reduce
        underwater_pct = node_state.get("underwater_pct", 0)
        if underwater_pct > 30:
            # Reduce by 15% or to 20%, whichever is higher
            target = max(20, underwater_pct - 15)
            goal = Goal(
                goal_id=f"underwater_{now}_{_unique_suffix()}",
                goal_type="channel_health",
                target_metric="underwater_pct",
                current_value=underwater_pct,
                target_value=target,
                deadline_days=45,
                created_at=now,
                priority=4,
                checkpoints=[],
                status="active"
            )
            goals.append(goal)

        # Bleeder Goal: If >5 bleeders, reduce
        bleeder_count = node_state.get("bleeder_count", 0)
        if bleeder_count > 5:
            # Target: reduce to 3 or by half
            target = max(3, bleeder_count // 2)
            goal = Goal(
                goal_id=f"bleeders_{now}_{_unique_suffix()}",
                goal_type="channel_health",
                target_metric="bleeder_count",
                current_value=bleeder_count,
                target_value=target,
                deadline_days=14,
                created_at=now,
                priority=5,
                checkpoints=[],
                status="active"
            )
            goals.append(goal)

        # Balance Goal: If avg balance ratio far from 0.5
        avg_balance = node_state.get("avg_balance_ratio", 0.5)
        if abs(avg_balance - 0.5) > 0.15:
            # Target is always 0.5 (perfectly balanced)
            goal = Goal(
                goal_id=f"balance_{now}_{_unique_suffix()}",
                goal_type="channel_health",
                target_metric="avg_balance_ratio",
                current_value=avg_balance,
                target_value=0.5,
                deadline_days=21,
                created_at=now,
                priority=3,
                checkpoints=[],
                status="active"
            )
            goals.append(goal)

        # Profitable channels Goal: If <50% profitable, increase
        profitable_pct = node_state.get("profitable_pct", 0)
        if profitable_pct < 50:
            # Target: increase to 60% or by 20 percentage points
            target = min(60, profitable_pct + 20)
            goal = Goal(
                goal_id=f"profitable_{now}_{_unique_suffix()}",
                goal_type="profitability",
                target_metric="profitable_pct",
                current_value=profitable_pct,
                target_value=target,
                deadline_days=30,
                created_at=now,
                priority=4,
                checkpoints=[],
                status="active"
            )
            goals.append(goal)

        # Save all goals to database
        for goal in goals:
            self.db.save_goal(goal.to_dict())

        return goals

    def check_progress(self, goal: Goal, current_value: float) -> GoalProgress:
        """
        Check progress toward a goal and return status.

        Args:
            goal: The Goal to check
            current_value: Current metric value

        Returns:
            GoalProgress with status and recommendations
        """
        now = int(time.time())
        days_elapsed = (now - goal.created_at) / 86400
        days_remaining = max(0, goal.deadline_days - days_elapsed)

        # Calculate progress (handling inverse metrics like underwater_pct)
        total_change_needed = goal.target_value - goal.current_value
        change_so_far = current_value - goal.current_value

        if total_change_needed != 0:
            progress_pct = (change_so_far / total_change_needed) * 100
        else:
            progress_pct = 100.0

        # Expected progress based on time
        time_progress_pct = (days_elapsed / goal.deadline_days) * 100 if goal.deadline_days > 0 else 100.0

        # On track if actual progress is at least 80% of expected
        on_track = progress_pct >= time_progress_pct * 0.8

        # Calculate velocity
        current_velocity = change_so_far / max(1, days_elapsed)
        velocity_needed = (goal.target_value - current_value) / max(1, days_remaining) if days_remaining > 0 else 0

        # Determine recommendation and emoji
        new_status = None
        if progress_pct >= 100:
            recommendation = "Goal achieved! Consider setting a new target."
            status_emoji = "\u2705"  # checkmark
            new_status = "achieved"
        elif days_remaining <= 0:
            recommendation = "Deadline passed - goal not achieved. Analyze what went wrong."
            status_emoji = "\u274c"  # X
            new_status = "failed"
        elif on_track:
            recommendation = "On track. Continue current strategy."
            status_emoji = "\U0001f7e2"  # green circle
        elif progress_pct < time_progress_pct * 0.5:
            recommendation = "Significantly behind - increase action aggressiveness or revise target."
            status_emoji = "\U0001f534"  # red circle
        else:
            recommendation = "Slightly behind - consider strategy adjustment."
            status_emoji = "\U0001f7e1"  # yellow circle

        # Update goal status atomically - set and persist together
        if new_status:
            goal.status = new_status
            self.update_goal_status(goal.goal_id, new_status)

        return GoalProgress(
            goal_id=goal.goal_id,
            on_track=on_track,
            progress_pct=round(progress_pct, 2),
            days_elapsed=round(days_elapsed, 1),
            days_remaining=round(days_remaining, 1),
            velocity_needed=round(velocity_needed, 4),
            current_velocity=round(current_velocity, 4),
            recommendation=recommendation,
            status_emoji=status_emoji
        )

    def record_checkpoint(self, goal: Goal, current_value: float,
                          notes: str = None) -> None:
        """
        Record a checkpoint for goal progress tracking.

        Args:
            goal: The Goal to update
            current_value: Current metric value
            notes: Optional notes about the checkpoint
        """
        checkpoint = {
            "timestamp": int(time.time()),
            "value": current_value,
            "notes": notes
        }
        goal.checkpoints.append(checkpoint)

        # Update in database
        self.db.update_goal_checkpoints(goal.goal_id, goal.checkpoints)

    def update_goal_status(self, goal_id: str, status: str) -> bool:
        """
        Update goal status.

        Args:
            goal_id: Goal to update
            status: New status ("active", "achieved", "failed", "abandoned")

        Returns:
            True if updated successfully
        """
        return self.db.update_goal_status(goal_id, status)

    def create_custom_goal(
        self,
        goal_type: str,
        target_metric: str,
        current_value: float,
        target_value: float,
        deadline_days: int,
        priority: int = 3
    ) -> Goal:
        """
        Create a custom goal with specific parameters.

        Args:
            goal_type: Type of goal (profitability, routing_volume, channel_health)
            target_metric: Metric to track
            current_value: Starting value
            target_value: Target value
            deadline_days: Days to achieve goal
            priority: Priority 1-5 (default 3)

        Returns:
            Created Goal object
        """
        now = int(time.time())
        goal = Goal(
            goal_id=f"{target_metric}_{now}_{_unique_suffix()}",
            goal_type=goal_type,
            target_metric=target_metric,
            current_value=current_value,
            target_value=target_value,
            deadline_days=deadline_days,
            created_at=now,
            priority=priority,
            checkpoints=[],
            status="active"
        )
        self.db.save_goal(goal.to_dict())
        return goal

    def get_strategy_adjustments(
        self,
        goals: List[Goal],
        progress_list: List[GoalProgress]
    ) -> List[str]:
        """
        Generate strategy adjustments based on goal progress.

        Args:
            goals: List of active goals
            progress_list: List of GoalProgress for each goal

        Returns:
            List of strategy adjustment recommendations
        """
        adjustments = []

        for goal, progress in zip(goals, progress_list):
            if progress.on_track:
                continue

            if goal.goal_type == "profitability":
                if goal.target_metric == "roc_pct":
                    adjustments.append(
                        "ROC behind target: Increase fee optimization aggressiveness, "
                        "focus on high-margin channels"
                    )
                elif goal.target_metric == "profitable_pct":
                    adjustments.append(
                        "Profitable % behind: Review underwater channels for fee "
                        "adjustments or closure candidates"
                    )

            elif goal.goal_type == "channel_health":
                if goal.target_metric == "underwater_pct":
                    adjustments.append(
                        "Underwater % behind: Prioritize rebalancing underwater "
                        "channels, consider static policies for worst performers"
                    )
                elif goal.target_metric == "avg_balance_ratio":
                    adjustments.append(
                        "Balance ratio behind: Increase rebalancing frequency, "
                        "adjust fee differential between imbalanced channels"
                    )
                elif goal.target_metric == "bleeder_count":
                    adjustments.append(
                        "Bleeder count behind: Apply static fee policies to "
                        "worst bleeders, consider channel closures"
                    )

            elif goal.goal_type == "routing_volume":
                adjustments.append(
                    "Routing volume behind: Consider fee reductions on "
                    "well-balanced channels to attract flow"
                )

        return adjustments

    def get_goals_summary(self) -> Dict[str, Any]:
        """
        Get a summary of all goals and their status.

        Returns:
            Dictionary with goal statistics and details
        """
        active_goals = self.get_active_goals()

        summary = {
            "total_active": len(active_goals),
            "by_type": {},
            "by_priority": {i: 0 for i in range(1, 6)},
            "goals": []
        }

        for goal in active_goals:
            # Count by type
            if goal.goal_type not in summary["by_type"]:
                summary["by_type"][goal.goal_type] = 0
            summary["by_type"][goal.goal_type] += 1

            # Count by priority
            if goal.priority in summary["by_priority"]:
                summary["by_priority"][goal.priority] += 1

            # Add goal details
            summary["goals"].append({
                "goal_id": goal.goal_id,
                "goal_type": goal.goal_type,
                "target_metric": goal.target_metric,
                "target_value": goal.target_value,
                "deadline_days": goal.deadline_days,
                "priority": goal.priority,
                "checkpoints_count": len(goal.checkpoints)
            })

        return summary
