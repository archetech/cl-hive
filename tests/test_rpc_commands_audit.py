"""
Tests for RPC command fixes from audit 2026-02-10.

Tests cover:
- Status capability fields
"""

import pytest
import time
import json
from unittest.mock import MagicMock
from dataclasses import dataclass

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.database import HiveDatabase
from modules.rpc_commands import (
    HiveContext,
    check_permission,
    status as rpc_status,
)


@pytest.fixture
def mock_plugin():
    plugin = MagicMock()
    plugin.log = MagicMock()
    return plugin


@pytest.fixture
def database(mock_plugin, tmp_path):
    db_path = str(tmp_path / "test_rpc_audit.db")
    db = HiveDatabase(db_path, mock_plugin)
    db.initialize()
    return db


def _make_ctx(database, pubkey, tier='member', rationalization_mgr=None):
    """Create HiveContext with a member of the given tier."""
    now = int(time.time())
    conn = database._get_connection()

    # Ensure the member exists
    existing = conn.execute(
        "SELECT peer_id FROM hive_members WHERE peer_id = ?", (pubkey,)
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO hive_members (peer_id, tier, joined_at) VALUES (?, ?, ?)",
            (pubkey, tier, now)
        )

    return HiveContext(
        database=database,
        config=MagicMock(),
        safe_plugin=MagicMock(),
        our_pubkey=pubkey,
        rationalization_mgr=rationalization_mgr,
        log=MagicMock(),
    )


class TestStatusCapabilityFields:
    def test_status_includes_transport_and_signing_capabilities(self, database):
        pubkey = "02" + "dd" * 32
        ctx = _make_ctx(database, pubkey, tier='member')
        ctx.config.max_members = 50
        ctx.config.market_share_cap_pct = 0.20
        ctx.comms_active = True
        ctx.archon_active = False
        ctx.signing_backend = "cln-hsm"

        result = rpc_status(ctx)

        assert result["comms_active"] is True
        assert result["archon_active"] is False
        assert result["signing_backend"] == "cln-hsm"
