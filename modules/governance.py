"""
Governance Module for cl-hive (Simplified)

In the trusted-fleet model, governance is simple:
- Recommendations are generated, logged, and optionally pushed to
  cl-revenue-ops via bridge.
- A single enable/disable flag controls whether recommendations are acted on.
- No advisor/failsafe modes, no pending_actions queue, no budget gating.

All strategic decisions (channel opens, closes, rebalancing) are logged
as recommendations.  The planner decides whether to act directly.
"""

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class Recommendation:
    """A logged recommendation from the planning engine."""
    action_type: str          # 'channel_open', 'channel_close', 'rebalance', etc.
    target: str               # Target peer pubkey
    context: Dict[str, Any]   # Additional context (capacity, sizing, reason)
    timestamp: int            # Unix timestamp


class RecommendationLogger:
    """
    Logs planning recommendations for audit and optional bridge relay.

    Replaces the old DecisionEngine.  No modes, no budget tracking,
    no rate limiting -- just structured logging.
    """

    def __init__(self, database=None, plugin=None):
        self.db = database
        self.plugin = plugin

    def _log(self, msg: str, level: str = 'info') -> None:
        if self.plugin:
            self.plugin.log(f"[Governance] {msg}", level=level)

    def log_recommendation(
        self,
        action_type: str,
        target: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Recommendation:
        """
        Record a recommendation.

        Logs to plugin output and persists to planner_log via database
        if available.

        Returns:
            The Recommendation object that was logged.
        """
        rec = Recommendation(
            action_type=action_type,
            target=target,
            context=context or {},
            timestamp=int(time.time()),
        )

        self._log(
            f"Recommendation: {action_type} target={target[:16]}... "
            f"context={context}"
        )

        # Persist to planner_log table if database available
        if self.db:
            try:
                self.db.log_planner_action(
                    action_type=action_type,
                    result='recommended',
                    target=target,
                    details=context or {},
                )
            except Exception as e:
                self._log(f"Failed to persist recommendation: {e}", level='warn')

        return rec
