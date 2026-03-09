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
