"""
Tests for Boltz RPC command handlers in modules.rpc_commands.
"""

import time
from unittest.mock import MagicMock

import pytest

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.database import HiveDatabase
from modules.rpc_commands import HiveContext, boltz_status, boltz_swap_in, boltz_swap_out


@pytest.fixture
def mock_plugin():
    plugin = MagicMock()
    plugin.log = MagicMock()
    return plugin


@pytest.fixture
def database(mock_plugin, tmp_path):
    db_path = str(tmp_path / "test_rpc_boltz.db")
    db = HiveDatabase(db_path, mock_plugin)
    db.initialize()
    return db


def _ctx(database, pubkey: str, tier: str, boltz_client=None):
    database.add_member(pubkey, tier=tier, joined_at=int(time.time()))
    return HiveContext(
        database=database,
        config=MagicMock(),
        safe_plugin=MagicMock(),
        our_pubkey=pubkey,
        boltz_client=boltz_client,
        log=MagicMock(),
    )


def test_boltz_status_without_client(database):
    pubkey = "02" + "aa" * 32
    ctx = _ctx(database, pubkey, tier="member", boltz_client=None)

    result = boltz_status(ctx)
    assert result["enabled"] is False
    assert result["available"] is False
    assert "not initialized" in result["error"]


def test_boltz_swap_in_denies_neophyte(database):
    pubkey = "02" + "bb" * 32
    client = MagicMock()
    ctx = _ctx(database, pubkey, tier="neophyte", boltz_client=client)

    result = boltz_swap_in(ctx, amount_sats=10_000, dry_run=True)
    assert result["error"] == "permission_denied"
    client.quote_submarine.assert_not_called()


def test_boltz_swap_in_dry_run_quotes(database):
    pubkey = "02" + "cc" * 32
    client = MagicMock()
    client.quote_submarine.return_value = {"ok": True, "result": {"fee": 123}}
    ctx = _ctx(database, pubkey, tier="member", boltz_client=client)

    result = boltz_swap_in(ctx, amount_sats=25_000, currency="btc", dry_run=True)

    assert result["status"] == "quote"
    assert result["swap_type"] == "submarine"
    assert result["amount_sats"] == 25_000
    assert result["quote"]["fee"] == 123
    client.quote_submarine.assert_called_once_with(amount_sats=25_000, currency="btc")


def test_boltz_swap_in_execute(database):
    pubkey = "02" + "dd" * 32
    client = MagicMock()
    client.create_swap_in.return_value = {"ok": True, "result": {"id": "swap-in-1"}}
    ctx = _ctx(database, pubkey, tier="member", boltz_client=client)

    result = boltz_swap_in(
        ctx,
        amount_sats=30_000,
        currency="lbtc",
        invoice="lnbc1invoice",
        from_wallet="wallet-a",
        refund_address="bc1refund",
        external_pay=True,
        dry_run=False,
    )

    assert result["status"] == "created"
    assert result["swap_type"] == "submarine"
    assert result["swap"]["id"] == "swap-in-1"
    client.create_swap_in.assert_called_once_with(
        amount_sats=30_000,
        currency="lbtc",
        invoice="lnbc1invoice",
        from_wallet="wallet-a",
        refund_address="bc1refund",
        external_pay=True,
    )


def test_boltz_swap_out_dry_run_quote_error(database):
    pubkey = "02" + "ee" * 32
    client = MagicMock()
    client.quote_reverse.return_value = {"ok": False, "error": "quote failed"}
    ctx = _ctx(database, pubkey, tier="member", boltz_client=client)

    result = boltz_swap_out(ctx, amount_sats=50_000, dry_run=True)
    assert result["error"] == "quote failed"
    assert "details" in result


def test_boltz_swap_out_execute(database):
    pubkey = "02" + "ff" * 32
    client = MagicMock()
    client.create_swap_out.return_value = {"ok": True, "result": {"id": "swap-out-1"}}
    ctx = _ctx(database, pubkey, tier="member", boltz_client=client)

    result = boltz_swap_out(
        ctx,
        amount_sats=45_000,
        currency="btc",
        address="bc1dest",
        to_wallet="wallet-b",
        external_pay=True,
        no_zero_conf=True,
        description="outbound",
        routing_fee_limit_ppm=1200,
        chan_ids=["101x1x1"],
        dry_run=False,
    )

    assert result["status"] == "created"
    assert result["swap_type"] == "reverse"
    assert result["swap"]["id"] == "swap-out-1"
    client.create_swap_out.assert_called_once_with(
        amount_sats=45_000,
        currency="btc",
        address="bc1dest",
        to_wallet="wallet-b",
        external_pay=True,
        no_zero_conf=True,
        description="outbound",
        routing_fee_limit_ppm=1200,
        chan_ids=["101x1x1"],
    )
