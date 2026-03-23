"""Focused tests for durable membership tombstones."""

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.database import HiveDatabase


PEER_A = "02" + "a" * 64
PEER_B = "02" + "b" * 64


@pytest.fixture
def mock_plugin():
    plugin = MagicMock()
    plugin.log = MagicMock()
    return plugin


@pytest.fixture
def database(mock_plugin, tmp_path):
    db = HiveDatabase(str(tmp_path / "membership_tombstones.db"), mock_plugin)
    db.initialize()
    return db


def test_record_membership_tombstone_is_idempotent(database):
    ok1 = database.record_membership_tombstone(
        event_id="evt-1",
        peer_id=PEER_B,
        event="removed",
        actor_peer_id=PEER_A,
        reason="maintenance",
        timestamp=123,
        joined_at_cutoff=100,
    )
    ok2 = database.record_membership_tombstone(
        event_id="evt-1",
        peer_id=PEER_B,
        event="removed",
        actor_peer_id=PEER_A,
        reason="maintenance",
        timestamp=123,
        joined_at_cutoff=100,
    )

    assert ok1 is True
    assert ok2 is False


def test_get_membership_tombstones_returns_newest_first(database):
    database.record_membership_tombstone("evt-1", PEER_A, "left", None, "voluntary", 100, 90)
    database.record_membership_tombstone("evt-2", PEER_B, "banned", PEER_A, "spam", 200, 150)

    rows = database.get_membership_tombstones(limit=10)

    assert [row["event_id"] for row in rows] == ["evt-2", "evt-1"]
