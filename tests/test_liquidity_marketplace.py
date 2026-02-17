"""Tests for Phase 5C liquidity marketplace manager."""

import time
from unittest.mock import MagicMock

import pytest

from modules.database import HiveDatabase
from modules.liquidity_marketplace import LiquidityMarketplaceManager
from modules.nostr_transport import NostrTransport


@pytest.fixture
def mock_plugin():
    plugin = MagicMock()
    plugin.log = MagicMock()
    plugin.rpc = MagicMock()
    plugin.rpc.signmessage.return_value = {"zbase": "liquidity-test-sig"}
    return plugin


@pytest.fixture
def database(mock_plugin, tmp_path):
    db = HiveDatabase(str(tmp_path / "test_liquidity.db"), mock_plugin)
    db.initialize()
    return db


@pytest.fixture
def transport(mock_plugin, database):
    t = NostrTransport(mock_plugin, database)
    t.start()
    yield t
    t.stop()


@pytest.fixture
def manager(mock_plugin, database, transport):
    return LiquidityMarketplaceManager(
        database=database,
        plugin=mock_plugin,
        nostr_transport=transport,
        cashu_escrow_mgr=None,
        settlement_mgr=None,
        did_credential_mgr=None,
    )


def test_publish_discover_offer(manager):
    published = manager.publish_offer(
        provider_id="02" + "11" * 32,
        service_type=1,
        capacity_sats=5_000_000,
        duration_hours=24,
        pricing_model="sat-hours",
        rate={"rate_ppm": 100},
    )
    assert published["ok"] is True
    offers = manager.discover_offers(service_type=1, min_capacity=1_000_000, max_rate=200)
    assert len(offers) == 1
    assert offers[0]["offer_id"] == published["offer_id"]


def test_accept_offer_and_create_lease(manager):
    offer = manager.publish_offer(
        provider_id="02" + "22" * 32,
        service_type=2,
        capacity_sats=2_000_000,
        duration_hours=12,
        pricing_model="flat",
        rate={"rate_ppm": 200},
    )
    lease = manager.accept_offer(
        offer_id=offer["offer_id"],
        client_id="03" + "33" * 32,
        heartbeat_interval=600,
    )
    assert lease["ok"] is True
    status = manager.get_lease_status(lease["lease_id"])
    assert status["lease"]["status"] == "active"
    assert status["lease"]["offer_id"] == offer["offer_id"]


def test_send_and_verify_heartbeat(manager):
    offer = manager.publish_offer(
        provider_id="02" + "44" * 32,
        service_type=1,
        capacity_sats=1_500_000,
        duration_hours=6,
        pricing_model="sat-hours",
        rate={"rate_ppm": 90},
    )
    lease = manager.accept_offer(offer["offer_id"], client_id="03" + "55" * 32, heartbeat_interval=300)
    hb = manager.send_heartbeat(
        lease_id=lease["lease_id"],
        channel_id="123x1x0",
        remote_balance_sats=500_000,
    )
    assert hb["ok"] is True
    verify = manager.verify_heartbeat(lease["lease_id"], hb["heartbeat_id"])
    assert verify["ok"] is True

    status = manager.get_lease_status(lease["lease_id"])
    assert len(status["heartbeats"]) == 1
    assert status["heartbeats"][0]["client_verified"] == 1


def test_heartbeat_rate_limit(manager):
    offer = manager.publish_offer(
        provider_id="02" + "66" * 32,
        service_type=3,
        capacity_sats=3_000_000,
        duration_hours=6,
        pricing_model="flat",
        rate={"rate_ppm": 120},
    )
    lease = manager.accept_offer(offer["offer_id"], client_id="03" + "77" * 32, heartbeat_interval=3600)
    first = manager.send_heartbeat(
        lease_id=lease["lease_id"],
        channel_id="123x2x0",
        remote_balance_sats=100_000,
    )
    assert first["ok"] is True
    second = manager.send_heartbeat(
        lease_id=lease["lease_id"],
        channel_id="123x2x0",
        remote_balance_sats=100_000,
    )
    assert "error" in second
    assert "rate-limited" in second["error"]


def test_terminate_dead_leases(manager, database):
    now = int(time.time())
    conn = database._get_connection()
    conn.execute(
        "INSERT INTO liquidity_leases (lease_id, provider_id, client_id, service_type, capacity_sats, start_at, "
        "end_at, heartbeat_interval, last_heartbeat, missed_heartbeats, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "lease-dead",
            "02" + "88" * 32,
            "03" + "99" * 32,
            1,
            1_000_000,
            now - 7200,
            now + 7200,
            300,
            now - 3600,
            3,
            "active",
            now - 7200,
        ),
    )
    terminated = manager.terminate_dead_leases()
    assert terminated == 1
    row = conn.execute("SELECT status FROM liquidity_leases WHERE lease_id = 'lease-dead'").fetchone()
    assert row["status"] == "terminated"
