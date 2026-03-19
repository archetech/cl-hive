"""
Tests for the Governance module (Simplified).

Tests cover:
- RecommendationLogger: Logging recommendations to plugin and database
- Recommendation dataclass
"""

import time
import pytest
from unittest.mock import MagicMock

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.governance import RecommendationLogger, Recommendation


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def mock_database():
    """Create a mock database."""
    db = MagicMock()
    return db


@pytest.fixture
def mock_plugin():
    """Create a mock plugin."""
    return MagicMock()


@pytest.fixture
def logger(mock_database, mock_plugin):
    """Create a RecommendationLogger instance."""
    return RecommendationLogger(database=mock_database, plugin=mock_plugin)


# =============================================================================
# RECOMMENDATION LOGGER TESTS
# =============================================================================

class TestRecommendationLogger:
    """Tests for the simplified RecommendationLogger."""

    def test_log_recommendation_returns_recommendation(self, logger):
        """log_recommendation should return a Recommendation object."""
        rec = logger.log_recommendation(
            action_type='channel_open',
            target='02' + 'a' * 64,
            context={'amount_sats': 1_000_000},
        )

        assert isinstance(rec, Recommendation)
        assert rec.action_type == 'channel_open'
        assert rec.target == '02' + 'a' * 64
        assert rec.context == {'amount_sats': 1_000_000}
        assert isinstance(rec.timestamp, int)

    def test_log_recommendation_persists_to_database(self, logger, mock_database):
        """log_recommendation should persist via log_planner_action."""
        logger.log_recommendation(
            action_type='channel_open',
            target='02' + 'b' * 64,
            context={'amount_sats': 500_000},
        )

        mock_database.log_planner_action.assert_called_once_with(
            action_type='channel_open',
            result='recommended',
            target='02' + 'b' * 64,
            details={'amount_sats': 500_000},
        )

    def test_log_recommendation_logs_to_plugin(self, logger, mock_plugin):
        """log_recommendation should log to plugin output."""
        logger.log_recommendation(
            action_type='rebalance',
            target='02' + 'c' * 64,
            context={'reason': 'inventory_low'},
        )

        mock_plugin.log.assert_called_once()
        call_args = mock_plugin.log.call_args
        assert 'Recommendation' in call_args[0][0]
        assert 'rebalance' in call_args[0][0]

    def test_log_recommendation_handles_db_failure(self, logger, mock_database, mock_plugin):
        """log_recommendation should handle database errors gracefully."""
        mock_database.log_planner_action.side_effect = Exception("DB error")

        # Should not raise
        rec = logger.log_recommendation(
            action_type='channel_open',
            target='02' + 'd' * 64,
            context={},
        )

        assert rec is not None
        assert rec.action_type == 'channel_open'
        # Should have logged warning
        assert mock_plugin.log.call_count == 2  # recommendation + warning

    def test_log_recommendation_no_database(self, mock_plugin):
        """log_recommendation should work without database."""
        logger = RecommendationLogger(database=None, plugin=mock_plugin)

        rec = logger.log_recommendation(
            action_type='close_channel',
            target='02' + 'e' * 64,
        )

        assert rec is not None
        assert rec.action_type == 'close_channel'
        assert rec.context == {}

    def test_log_recommendation_no_plugin(self, mock_database):
        """log_recommendation should work without plugin."""
        logger = RecommendationLogger(database=mock_database, plugin=None)

        rec = logger.log_recommendation(
            action_type='channel_open',
            target='02' + 'f' * 64,
            context={'amount_sats': 1_000_000},
        )

        assert rec is not None
        mock_database.log_planner_action.assert_called_once()

    def test_log_recommendation_default_context(self, logger):
        """log_recommendation should default context to empty dict."""
        rec = logger.log_recommendation(
            action_type='channel_open',
            target='02' + 'g' * 64,
        )

        assert rec.context == {}


# =============================================================================
# RECOMMENDATION DATACLASS TESTS
# =============================================================================

class TestRecommendation:
    """Tests for the Recommendation dataclass."""

    def test_recommendation_fields(self):
        """Recommendation should have expected fields."""
        rec = Recommendation(
            action_type='channel_open',
            target='02abc123',
            context={'hive_share': 0.05, 'capacity': 1_000_000},
            timestamp=1234567890,
        )

        assert rec.action_type == 'channel_open'
        assert rec.target == '02abc123'
        assert rec.context['hive_share'] == 0.05
        assert rec.timestamp == 1234567890
