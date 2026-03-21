"""
Tests for database integrity fixes from audit 2026-02-10.

Tests cover:
- H-9: sync_uptime_from_presence JOIN-based query
- M-11: update_presence TOCTOU prevention
- M-12: log_planner_action transaction atomicity
"""

import pytest
import time
import threading
import sqlite3
from unittest.mock import MagicMock

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.database import HiveDatabase


@pytest.fixture
def mock_plugin():
    plugin = MagicMock()
    plugin.log = MagicMock()
    return plugin


@pytest.fixture
def database(mock_plugin, tmp_path):
    db_path = str(tmp_path / "test_audit.db")
    db = HiveDatabase(db_path, mock_plugin)
    db.initialize()
    return db


class TestUpdatePresenceTransaction:
    """M-11: Test update_presence TOCTOU prevention."""

    def test_insert_new_presence(self, database):
        """First call should insert."""
        now = int(time.time())
        database.update_presence("peer_a", True, now, 86400)
        result = database.get_presence("peer_a")
        assert result is not None
        assert result['peer_id'] == 'peer_a'
        assert result['is_online'] == 1

    def test_update_existing_presence(self, database):
        """Second call should update, not duplicate."""
        now = int(time.time())
        database.update_presence("peer_a", True, now, 86400)
        database.update_presence("peer_a", False, now + 100, 86400)

        result = database.get_presence("peer_a")
        assert result['is_online'] == 0
        assert result['online_seconds_rolling'] == 100

        # Verify no duplicate rows
        conn = database._get_connection()
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM peer_presence WHERE peer_id = ?",
            ("peer_a",)
        ).fetchone()
        assert count['cnt'] == 1

    def test_concurrent_presence_inserts(self, database):
        """No duplicate rows under concurrent inserts."""
        now = int(time.time())
        errors = []

        def insert_presence(peer_id):
            try:
                database.update_presence(peer_id, True, now, 86400)
            except Exception as e:
                errors.append(str(e))

        # Concurrent inserts for different peers should be fine
        threads = [
            threading.Thread(target=insert_presence, args=(f"peer_{i}",))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert errors == []

        # Verify exactly 10 rows
        conn = database._get_connection()
        count = conn.execute("SELECT COUNT(*) as cnt FROM peer_presence").fetchone()
        assert count['cnt'] == 10


class TestLogPlannerActionTransaction:
    """M-12: Test log_planner_action transaction."""

    def test_ring_buffer_cap(self, database):
        """Verify ring buffer cap holds."""
        # Set a small cap for testing
        original_cap = database.MAX_PLANNER_LOG_ROWS
        database.MAX_PLANNER_LOG_ROWS = 20

        try:
            # Insert more than cap
            for i in range(25):
                database.log_planner_action(
                    action_type="test",
                    result="success",
                    target=f"target_{i}",
                    details={"iteration": i}
                )

            conn = database._get_connection()
            count = conn.execute("SELECT COUNT(*) as cnt FROM hive_planner_log").fetchone()
            # After 20 rows, 10% (2) are pruned before inserting next
            # So we should have <= 20 rows
            assert count['cnt'] <= 20
        finally:
            database.MAX_PLANNER_LOG_ROWS = original_cap

    def test_basic_logging(self, database):
        """Test basic planner log insertion."""
        database.log_planner_action(
            action_type="expansion",
            result="proposed",
            target="02" + "aa" * 32,
            details={"reason": "underserved"}
        )
        logs = database.get_planner_logs(limit=1)
        assert len(logs) == 1
        assert logs[0]['action_type'] == 'expansion'
        assert logs[0]['result'] == 'proposed'


class TestSyncUptimeFromPresence:
    """H-9: Test JOIN-based uptime calculation."""

    def test_correct_uptime_calculation(self, database):
        """Verify correct uptime from presence data."""
        now = int(time.time())
        conn = database._get_connection()

        # Add a member
        conn.execute(
            "INSERT INTO hive_members (peer_id, tier, joined_at) VALUES (?, ?, ?)",
            ("peer_a", "member", now - 86400)
        )

        # Add presence: online for 50% of window
        window = 1000
        conn.execute(
            "INSERT INTO peer_presence (peer_id, last_change_ts, is_online, "
            "online_seconds_rolling, window_start_ts) VALUES (?, ?, ?, ?, ?)",
            ("peer_a", now - 100, 0, 500, now - window)
        )

        updated = database.sync_uptime_from_presence(window_seconds=window)
        assert updated == 1

        # Check uptime
        member = conn.execute(
            "SELECT uptime_pct FROM hive_members WHERE peer_id = ?",
            ("peer_a",)
        ).fetchone()
        assert member['uptime_pct'] == pytest.approx(0.5, abs=0.05)

    def test_online_member_gets_credit(self, database):
        """Currently online members get credit for time since last change."""
        now = int(time.time())
        conn = database._get_connection()

        conn.execute(
            "INSERT INTO hive_members (peer_id, tier, joined_at) VALUES (?, ?, ?)",
            ("peer_b", "member", now - 86400)
        )
        # Online since window start
        window = 1000
        conn.execute(
            "INSERT INTO peer_presence (peer_id, last_change_ts, is_online, "
            "online_seconds_rolling, window_start_ts) VALUES (?, ?, ?, ?, ?)",
            ("peer_b", now - window, 1, 0, now - window)
        )

        updated = database.sync_uptime_from_presence(window_seconds=window)
        assert updated == 1

        member = conn.execute(
            "SELECT uptime_pct FROM hive_members WHERE peer_id = ?",
            ("peer_b",)
        ).fetchone()
        # Should be ~100% since online for the entire window
        assert member['uptime_pct'] == pytest.approx(1.0, abs=0.05)

    def test_no_presence_data_skipped(self, database):
        """Members without presence data are skipped."""
        now = int(time.time())
        conn = database._get_connection()

        conn.execute(
            "INSERT INTO hive_members (peer_id, tier, joined_at) VALUES (?, ?, ?)",
            ("peer_c", "member", now - 86400)
        )

        updated = database.sync_uptime_from_presence()
        assert updated == 0
