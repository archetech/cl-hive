"""Tests for liquidity-aware expansion proposal gate."""
import json
import sqlite3
import time


def _create_test_db():
    """Create in-memory DB with pending_actions table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE pending_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            proposed_at INTEGER NOT NULL,
            expires_at INTEGER,
            status TEXT DEFAULT 'pending',
            rejection_reason TEXT
        )
    """)
    return conn


def _insert_pending_action(conn, action_type, payload, status='pending',
                           expires_at=None):
    """Insert a test pending action."""
    now = int(time.time())
    if expires_at is None:
        expires_at = now + 86400
    conn.execute(
        "INSERT INTO pending_actions (action_type, payload, proposed_at, expires_at, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (action_type, json.dumps(payload), now, expires_at, status),
    )
    conn.commit()


def test_pending_total_empty():
    """No pending actions returns 0."""
    from modules.database import _get_pending_channel_open_total_sql
    conn = _create_test_db()
    assert _get_pending_channel_open_total_sql(conn) == 0


def test_pending_total_sums_correctly():
    """Two pending channel_open proposals sum their sizes."""
    from modules.database import _get_pending_channel_open_total_sql
    conn = _create_test_db()
    _insert_pending_action(conn, 'channel_open', {'proposed_size_sats': 1_000_000})
    _insert_pending_action(conn, 'channel_open', {'proposed_size_sats': 2_000_000})
    assert _get_pending_channel_open_total_sql(conn) == 3_000_000


def test_pending_total_excludes_expired():
    """Expired proposals are not counted."""
    from modules.database import _get_pending_channel_open_total_sql
    conn = _create_test_db()
    past = int(time.time()) - 3600
    _insert_pending_action(conn, 'channel_open', {'proposed_size_sats': 1_000_000},
                           expires_at=past)
    _insert_pending_action(conn, 'channel_open', {'proposed_size_sats': 500_000})
    assert _get_pending_channel_open_total_sql(conn) == 500_000


def test_pending_total_excludes_non_pending():
    """Approved/rejected/executed actions are not counted."""
    from modules.database import _get_pending_channel_open_total_sql
    conn = _create_test_db()
    _insert_pending_action(conn, 'channel_open', {'proposed_size_sats': 1_000_000},
                           status='approved')
    _insert_pending_action(conn, 'channel_open', {'proposed_size_sats': 2_000_000},
                           status='rejected')
    _insert_pending_action(conn, 'channel_open', {'proposed_size_sats': 500_000},
                           status='pending')
    assert _get_pending_channel_open_total_sql(conn) == 500_000


def test_pending_total_fallback_to_channel_size_sats():
    """Falls back to channel_size_sats when proposed_size_sats is missing."""
    from modules.database import _get_pending_channel_open_total_sql
    conn = _create_test_db()
    _insert_pending_action(conn, 'channel_open', {'channel_size_sats': 3_000_000})
    assert _get_pending_channel_open_total_sql(conn) == 3_000_000


def test_pending_total_ignores_non_channel_open():
    """Non-channel_open actions are not counted."""
    from modules.database import _get_pending_channel_open_total_sql
    conn = _create_test_db()
    _insert_pending_action(conn, 'ban', {'amount_sats': 999_999})
    _insert_pending_action(conn, 'channel_open', {'proposed_size_sats': 1_000_000})
    assert _get_pending_channel_open_total_sql(conn) == 1_000_000


def test_expansion_blocked_by_pending():
    """Planner skips expansion when pending proposals exhaust available budget."""
    from unittest.mock import MagicMock, patch
    from modules.planner import Planner, UnderservedResult

    mock_plugin = MagicMock()
    mock_db = MagicMock()
    mock_state_mgr = MagicMock()
    mock_bridge = MagicMock()
    mock_intent_mgr = MagicMock()
    planner = Planner(mock_state_mgr, mock_db, mock_bridge,
                      plugin=mock_plugin, intent_manager=mock_intent_mgr)

    # Setup: 2M daily budget, 10M onchain, 20% reserve = 8M spendable
    # max_per_channel = 2M * 0.5 = 1M
    # gross_available = min(2M, 8M, 1M) = 1M
    # pending = 1M from existing proposal
    # net available = 1M - 1M = 0 < min_channel_size (1M) -> BLOCKED
    mock_config = MagicMock()
    mock_config.failsafe_budget_per_day = 2_000_000
    mock_config.budget_reserve_pct = 0.20
    mock_config.budget_max_per_channel_pct = 0.50
    mock_config.planner_enable_expansions = True
    mock_config.planner_min_channel_sats = 1_000_000
    mock_config.planner_max_channel_sats = 50_000_000
    mock_config.planner_default_channel_sats = 5_000_000
    mock_config.planner_max_active_channels = 50
    mock_config.max_expansion_feerate_perkb = 5000
    mock_config.governance_mode = 'advisor'
    mock_config.market_share_cap = 0.20
    mock_config.planner_safety_reserve_sats = 500_000
    mock_config.planner_fee_buffer_sats = 100_000
    mock_config.rejection_cooldown_seconds = 86400

    mock_db.get_available_budget.return_value = 2_000_000
    mock_db.get_pending_channel_open_total.return_value = 1_000_000
    mock_db.get_pending_intents.return_value = []

    mock_plugin.rpc.listfunds.return_value = {
        'outputs': [{'status': 'confirmed', 'amount_msat': 10_000_000_000}]
    }
    mock_plugin.rpc.feerates.return_value = {
        'perkb': {'opening': 1000}
    }

    with patch.object(planner, '_should_pause_expansions_globally', return_value=(False, '')), \
         patch.object(planner, 'compute_node_summary', return_value={}), \
         patch.object(planner, 'get_underserved_targets') as mock_targets, \
         patch.object(planner, '_should_skip_target', return_value=(False, '')):
        mock_targets.return_value = [
            UnderservedResult(
                target='02' + 'a' * 64,
                public_capacity_sats=200_000_000,
                hive_share_pct=0.02,
                score=2.0,
            )
        ]
        decisions = planner._propose_expansion(mock_config, 'test-gate')

    assert len(decisions) == 1
    assert decisions[0]['action'] == 'expansion_skipped'
    assert decisions[0]['reason'] == 'insufficient_budget'
