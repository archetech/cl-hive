"""
Focused tests for settlement proposal diagnostics at protocol boundaries.
"""

import sys
import time
import types
from unittest.mock import MagicMock

if "pyln.client" not in sys.modules:
    pyln_module = types.ModuleType("pyln")
    pyln_client_module = types.ModuleType("pyln.client")

    class _Plugin:
        pass

    class _RpcError(Exception):
        pass

    pyln_client_module.Plugin = _Plugin
    pyln_client_module.RpcError = _RpcError
    pyln_module.client = pyln_client_module
    sys.modules.setdefault("pyln", pyln_module)
    sys.modules["pyln.client"] = pyln_client_module

import modules.protocol as protocol
from modules import background_loops, protocol_handlers


PEER_A = "02" + "a" * 64
PEER_B = "02" + "b" * 64


def test_pyln_client_stub_supports_unit_imports():
    assert "pyln.client" in sys.modules
    assert hasattr(sys.modules["pyln.client"], "Plugin")
    assert hasattr(sys.modules["pyln.client"], "RpcError")
    assert hasattr(protocol_handlers, "handle_settlement_propose")
    assert hasattr(background_loops, "_process_pending_settlement_proposals_once")


def _make_plugin():
    plugin = MagicMock()
    plugin.log = MagicMock()
    plugin.rpc = MagicMock()
    plugin.rpc.call.return_value = {"verified": True}
    return plugin


def _make_proposal_payload():
    return {
        "proposal_id": "test_proposal_123",
        "period": "2026-W09",
        "proposer_peer_id": PEER_B,
        "data_hash": "d" * 64,
        "plan_hash": "p" * 64,
        "total_fees_sats": 1000,
        "member_count": 2,
        "contributions": [],
        "timestamp": int(time.time()),
        "signature": "sig",
    }


def test_handle_settlement_propose_logs_verify_rejection_reason(monkeypatch):
    plugin = _make_plugin()
    database = MagicMock()
    database.get_member.return_value = {"peer_id": PEER_B, "tier": "member"}
    database.is_banned.return_value = False
    database.get_settlement_proposal_by_period.return_value = None
    settlement_mgr = MagicMock()
    settlement_mgr.verify_and_vote.return_value = None
    settlement_mgr.last_verify_and_vote_reason = {
        "reason": "hash_mismatch",
        "proposal_id": "test_proposal_123",
        "period": "2026-W09",
    }
    state_manager = MagicMock()

    protocol_handlers.init_protocol_handlers({
        "plugin": plugin,
        "database": database,
        "state_manager": state_manager,
        "settlement_mgr": settlement_mgr,
        "our_pubkey": PEER_A,
    })

    monkeypatch.setattr(protocol, "validate_settlement_propose", lambda payload: True)
    monkeypatch.setattr(protocol_handlers, "_should_process_message", lambda payload: True)
    monkeypatch.setattr(protocol_handlers, "_check_timestamp_freshness", lambda *args, **kwargs: True)
    monkeypatch.setattr(protocol_handlers, "_validate_relay_sender", lambda *args, **kwargs: True)
    monkeypatch.setattr(protocol_handlers, "check_and_record", lambda *args, **kwargs: (True, "evt-1"))
    monkeypatch.setattr(protocol_handlers, "_emit_ack", lambda *args, **kwargs: None)
    monkeypatch.setattr(protocol_handlers, "_relay_message", lambda *args, **kwargs: None)
    monkeypatch.setattr(protocol_handlers, "_reliable_broadcast", lambda *args, **kwargs: None)

    result = protocol_handlers.handle_settlement_propose(
        peer_id=PEER_B,
        payload=_make_proposal_payload(),
        plugin=plugin,
    )

    assert result == {"result": "continue"}
    plugin.log.assert_any_call(
        "SETTLEMENT: Proposal test_proposal_12... not voted locally (reason=hash_mismatch, period=2026-W09)",
        level="info",
    )


def test_pending_proposal_processing_logs_verify_rejection_reason(monkeypatch):
    plugin = _make_plugin()
    database = MagicMock()
    database.get_pending_settlement_proposals.return_value = [
        {
            "proposal_id": "test_proposal_123",
            "period": "2026-W09",
            "member_count": 2,
        }
    ]
    database.has_voted_settlement.return_value = False
    settlement_mgr = MagicMock()
    settlement_mgr.verify_and_vote.return_value = None
    settlement_mgr.last_verify_and_vote_reason = {
        "reason": "hash_mismatch",
        "proposal_id": "test_proposal_123",
        "period": "2026-W09",
    }

    monkeypatch.setattr(background_loops.protocol_handlers, "_broadcast_to_members", MagicMock())

    background_loops._process_pending_settlement_proposals_once(
        settlement_mgr=settlement_mgr,
        database=database,
        state_manager=MagicMock(),
        plugin=plugin,
        our_pubkey=PEER_A,
    )

    plugin.log.assert_any_call(
        "SETTLEMENT: Proposal test_proposal_12... not voted locally (reason=hash_mismatch, period=2026-W09)",
        level="info",
    )
