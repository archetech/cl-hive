"""
Tests for settlement database integrity guards.
"""

from unittest.mock import MagicMock

from modules.database import HiveDatabase


def _make_db(tmp_path):
    plugin = MagicMock()
    db = HiveDatabase(str(tmp_path / "settlement_integrity.db"), plugin)
    db.initialize()
    return db


def test_ready_vote_rejects_unknown_proposal(tmp_path):
    db = _make_db(tmp_path)
    ok = db.add_settlement_ready_vote(
        proposal_id="unknown",
        voter_peer_id="02" + "a" * 64,
        data_hash="f" * 64,
        signature="sig",
    )
    assert ok is False


def test_execution_rejects_unknown_proposal(tmp_path):
    db = _make_db(tmp_path)
    ok = db.add_settlement_execution(
        proposal_id="unknown",
        executor_peer_id="02" + "a" * 64,
        signature="sig",
        payment_hash="p",
        amount_paid_sats=1,
        plan_hash="e" * 64,
    )
    assert ok is False


def test_ready_vote_and_execution_accept_known_proposal(tmp_path):
    db = _make_db(tmp_path)
    created = db.add_settlement_proposal(
        proposal_id="known-proposal",
        period="2026-08",
        proposer_peer_id="02" + "b" * 64,
        data_hash="d" * 64,
        total_fees_sats=100,
        member_count=2,
        plan_hash="e" * 64,
    )
    assert created is True

    vote_ok = db.add_settlement_ready_vote(
        proposal_id="known-proposal",
        voter_peer_id="02" + "a" * 64,
        data_hash="d" * 64,
        signature="sig",
    )
    exec_ok = db.add_settlement_execution(
        proposal_id="known-proposal",
        executor_peer_id="02" + "a" * 64,
        signature="sig",
        payment_hash="p",
        amount_paid_sats=1,
        plan_hash="e" * 64,
    )

    assert vote_ok is True
    assert exec_ok is True
