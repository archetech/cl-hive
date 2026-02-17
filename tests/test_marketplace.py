"""Tests for Phase 5B marketplace manager."""

import json
import time
from unittest.mock import MagicMock

import pytest

from modules.database import HiveDatabase
from modules.marketplace import MarketplaceManager
from modules.nostr_transport import NostrTransport


@pytest.fixture
def mock_plugin():
    plugin = MagicMock()
    plugin.log = MagicMock()
    plugin.rpc = MagicMock()
    plugin.rpc.signmessage.return_value = {"zbase": "marketplace-test-sig"}
    return plugin


@pytest.fixture
def database(mock_plugin, tmp_path):
    db = HiveDatabase(str(tmp_path / "test_marketplace.db"), mock_plugin)
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
    return MarketplaceManager(
        database=database,
        plugin=mock_plugin,
        nostr_transport=transport,
        did_credential_mgr=None,
        management_schema_registry=None,
        cashu_escrow_mgr=None,
    )


def test_publish_and_discover_profile(manager):
    profile = {
        "advisor_did": "did:cid:advisor1",
        "specializations": ["fee-optimization", "rebalancing"],
        "capabilities": {"primary": ["fee-optimization"]},
        "pricing": {"model": "flat", "amount_sats": 1000},
        "reputation_score": 80,
    }
    result = manager.publish_profile(profile)
    assert result["ok"] is True

    discovered = manager.discover_advisors({"specialization": "fee-optimization", "min_reputation": 50})
    assert len(discovered) == 1
    assert discovered[0]["advisor_did"] == "did:cid:advisor1"


def test_contract_proposal_and_accept(manager):
    proposal = manager.propose_contract(
        advisor_did="did:cid:advisor1",
        node_id="02" + "aa" * 32,
        scope={"scope": "fee-policy"},
        tier="standard",
        pricing={"model": "flat", "amount_sats": 500},
    )
    assert proposal["ok"] is True
    contract_id = proposal["contract_id"]

    accepted = manager.accept_contract(contract_id)
    assert accepted["ok"] is True
    assert accepted["contract_id"] == contract_id


def test_trial_start_and_evaluate_pass(manager, database):
    proposal = manager.propose_contract(
        advisor_did="did:cid:advisor2",
        node_id="02" + "bb" * 32,
        scope={"scope": "monitor"},
        tier="standard",
        pricing={"model": "flat"},
    )
    contract_id = proposal["contract_id"]
    manager.accept_contract(contract_id)

    trial = manager.start_trial(contract_id, duration_days=1, flat_fee_sats=200)
    assert trial["ok"] is True
    assert trial["sequence_number"] == 1

    result = manager.evaluate_trial(
        contract_id,
        {"actions_taken": 12, "uptime_pct": 99, "revenue_delta": 1.5},
    )
    assert result["ok"] is True
    assert result["outcome"] == "pass"

    conn = database._get_connection()
    row = conn.execute(
        "SELECT status FROM marketplace_contracts WHERE contract_id = ?",
        (contract_id,),
    ).fetchone()
    assert row["status"] == "active"


def test_trial_cooldown_enforced(manager):
    node_id = "02" + "cc" * 32
    p1 = manager.propose_contract(
        advisor_did="did:cid:advisor3",
        node_id=node_id,
        scope={"scope": "rebalance"},
        tier="standard",
        pricing={},
    )
    manager.accept_contract(p1["contract_id"])
    first = manager.start_trial(p1["contract_id"], duration_days=1)
    assert first["ok"] is True

    p2 = manager.propose_contract(
        advisor_did="did:cid:advisor4",
        node_id=node_id,
        scope={"scope": "rebalance"},
        tier="standard",
        pricing={},
    )
    manager.accept_contract(p2["contract_id"])
    second = manager.start_trial(p2["contract_id"], duration_days=1)
    assert "error" in second
    assert "cooldown" in second["error"]


def test_cleanup_stale_profiles(manager, database):
    now = int(time.time())
    conn = database._get_connection()
    conn.execute(
        "INSERT INTO marketplace_profiles (advisor_did, profile_json, nostr_pubkey, version, capabilities_json, "
        "pricing_json, reputation_score, last_seen, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "did:cid:stale",
            json.dumps({"advisor_did": "did:cid:stale"}),
            "",
            "1",
            "{}",
            "{}",
            10,
            now - (95 * 86400),
            "nostr",
        ),
    )
    deleted = manager.cleanup_stale_profiles()
    assert deleted == 1
