"""
Tests for Cashu Escrow Module (Phase 4A).

Tests cover:
- MintCircuitBreaker: state transitions, availability, stats
- CashuEscrowManager: ticket creation, validation, pricing, secrets, receipts
- Secret encryption/decryption round-trip
- Ticket lifecycle: create -> active -> redeemed/refunded/expired
- Row cap enforcement
- Circuit breaker integration with mint calls
"""

import hashlib
import json
import os
import time
import concurrent.futures
import pytest
from unittest.mock import MagicMock, patch

from modules.cashu_escrow import (
    CashuEscrowManager,
    MintCircuitBreaker,
    MintCircuitState,
    VALID_TICKET_TYPES,
    VALID_TICKET_STATUSES,
    DANGER_PRICING_TABLE,
    REP_MODIFIER,
)


# =============================================================================
# Test helpers
# =============================================================================

ALICE_PUBKEY = "03" + "a1" * 32
BOB_PUBKEY = "03" + "b2" * 32
MINT_URL = "https://mint.example.com"


class MockDatabase:
    """Mock database for escrow operations."""

    def __init__(self):
        self.tickets = {}
        self.secrets = {}
        self.receipts = {}

    def store_escrow_ticket(self, ticket_id, ticket_type, agent_id, operator_id,
                            mint_url, amount_sats, token_json, htlc_hash,
                            timelock, danger_score, schema_id, action,
                            status, created_at):
        self.tickets[ticket_id] = {
            "ticket_id": ticket_id, "ticket_type": ticket_type,
            "agent_id": agent_id, "operator_id": operator_id,
            "mint_url": mint_url, "amount_sats": amount_sats,
            "token_json": token_json, "htlc_hash": htlc_hash,
            "timelock": timelock, "danger_score": danger_score,
            "schema_id": schema_id, "action": action,
            "status": status, "created_at": created_at,
            "redeemed_at": None, "refunded_at": None,
        }
        return True

    def get_escrow_ticket(self, ticket_id):
        return self.tickets.get(ticket_id)

    def list_escrow_tickets(self, agent_id=None, status=None, limit=100):
        result = []
        for t in self.tickets.values():
            if agent_id and t["agent_id"] != agent_id:
                continue
            if status and t["status"] != status:
                continue
            result.append(t)
        return result[:limit]

    def update_escrow_ticket_status(self, ticket_id, status, timestamp, expected_status=None):
        if ticket_id in self.tickets:
            if expected_status is not None and self.tickets[ticket_id]["status"] != expected_status:
                return False
            self.tickets[ticket_id]["status"] = status
            if status == "redeemed":
                self.tickets[ticket_id]["redeemed_at"] = timestamp
            elif status == "refunded":
                self.tickets[ticket_id]["refunded_at"] = timestamp
            return True
        return False

    def count_escrow_tickets(self):
        return len(self.tickets)

    def store_escrow_secret(self, task_id, ticket_id, secret_hex, hash_hex):
        self.secrets[task_id] = {
            "task_id": task_id, "ticket_id": ticket_id,
            "secret_hex": secret_hex, "hash_hex": hash_hex,
            "revealed_at": None,
        }
        return True

    def get_escrow_secret(self, task_id):
        return self.secrets.get(task_id)

    def get_escrow_secret_by_ticket(self, ticket_id):
        for s in self.secrets.values():
            if s["ticket_id"] == ticket_id:
                return s
        return None

    def reveal_escrow_secret(self, task_id, timestamp):
        if task_id in self.secrets:
            self.secrets[task_id]["revealed_at"] = timestamp
            return True
        return False

    def count_escrow_secrets(self):
        return len(self.secrets)

    def prune_escrow_secrets(self, before_ts):
        to_delete = [k for k, v in self.secrets.items()
                     if v["revealed_at"] and v["revealed_at"] < before_ts]
        for k in to_delete:
            del self.secrets[k]
        return len(to_delete)

    def store_escrow_receipt(self, receipt_id, ticket_id, schema_id, action,
                             params_json, result_json, success,
                             preimage_revealed, node_signature, created_at,
                             agent_signature=None):
        self.receipts[receipt_id] = {
            "receipt_id": receipt_id, "ticket_id": ticket_id,
            "schema_id": schema_id, "action": action,
            "params_json": params_json, "result_json": result_json,
            "success": success, "preimage_revealed": preimage_revealed,
            "agent_signature": agent_signature, "node_signature": node_signature,
            "created_at": created_at,
        }
        return True

    def get_escrow_receipts(self, ticket_id, limit=100):
        return [r for r in self.receipts.values() if r["ticket_id"] == ticket_id][:limit]

    def count_escrow_receipts(self):
        return len(self.receipts)


def make_mock_rpc():
    """Create a mock RPC with signmessage support."""
    rpc = MagicMock()
    rpc.signmessage.return_value = {"zbase": "test_signature_zbase32_value_for_testing"}
    rpc.checkmessage.return_value = {"verified": True, "pubkey": ALICE_PUBKEY}
    return rpc


def make_manager(acceptable_mints=None):
    """Create a CashuEscrowManager with mocked dependencies."""
    db = MockDatabase()
    plugin = MagicMock()
    rpc = make_mock_rpc()
    return CashuEscrowManager(
        database=db, plugin=plugin, rpc=rpc,
        our_pubkey=ALICE_PUBKEY,
        acceptable_mints=acceptable_mints or [MINT_URL],
    )


# =============================================================================
# MintCircuitBreaker tests
# =============================================================================

class TestMintCircuitBreaker:

    def test_initial_state_closed(self):
        cb = MintCircuitBreaker(MINT_URL)
        assert cb.state == MintCircuitState.CLOSED
        assert cb.is_available()

    def test_opens_after_failures(self):
        cb = MintCircuitBreaker(MINT_URL, max_failures=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == MintCircuitState.OPEN
        assert not cb.is_available()

    def test_half_open_after_timeout(self):
        cb = MintCircuitBreaker(MINT_URL, max_failures=2, reset_timeout=1)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == MintCircuitState.OPEN
        # Simulate timeout
        cb._last_failure_time = int(time.time()) - 2
        assert cb.state == MintCircuitState.HALF_OPEN
        assert cb.is_available()

    def test_half_open_to_closed_after_successes(self):
        cb = MintCircuitBreaker(MINT_URL, max_failures=2, reset_timeout=0,
                                half_open_success_threshold=2)
        cb.record_failure()
        cb.record_failure()
        cb._last_failure_time = 0  # force HALF_OPEN
        assert cb.state == MintCircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state == MintCircuitState.HALF_OPEN  # not enough yet
        cb.record_success()
        assert cb.state == MintCircuitState.CLOSED

    def test_half_open_to_open_on_failure(self):
        cb = MintCircuitBreaker(MINT_URL, max_failures=2, reset_timeout=9999)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == MintCircuitState.OPEN
        # Force into HALF_OPEN by backdating the failure time
        cb._last_failure_time = int(time.time()) - 10000
        assert cb.state == MintCircuitState.HALF_OPEN
        cb.record_failure()
        # Now failure time is recent, so still OPEN
        assert cb._state == MintCircuitState.OPEN

    def test_success_resets_failure_count(self):
        cb = MintCircuitBreaker(MINT_URL, max_failures=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()  # Only 1 failure now
        assert cb.state == MintCircuitState.CLOSED

    def test_reset(self):
        cb = MintCircuitBreaker(MINT_URL, max_failures=2)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == MintCircuitState.OPEN
        cb.reset()
        assert cb.state == MintCircuitState.CLOSED

    def test_get_stats(self):
        cb = MintCircuitBreaker(MINT_URL)
        stats = cb.get_stats()
        assert stats["mint_url"] == MINT_URL
        assert stats["state"] == "closed"
        assert stats["failure_count"] == 0


# =============================================================================
# CashuEscrowManager tests
# =============================================================================

class TestCashuEscrowManager:

    def test_init(self):
        mgr = make_manager()
        assert mgr.our_pubkey == ALICE_PUBKEY
        assert MINT_URL in mgr.acceptable_mints
        assert mgr._secret_key is not None

    def test_secret_encryption_roundtrip(self):
        mgr = make_manager()
        original = os.urandom(32).hex()
        task_id = "test_task_1"
        encrypted = mgr._encrypt_secret(original, task_id=task_id)
        decrypted = mgr._decrypt_secret(encrypted, task_id=task_id)
        assert decrypted == original
        assert encrypted != original  # Should be different

    def test_generate_and_reveal_secret(self):
        mgr = make_manager()
        htlc_hash = mgr.generate_secret("task1", "ticket1")
        assert htlc_hash is not None
        assert len(htlc_hash) == 64

        preimage = mgr.reveal_secret("task1", require_receipt=False)
        assert preimage is not None
        # Verify hash matches
        computed_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
        assert computed_hash == htlc_hash

    def test_generate_secret_unknown_task(self):
        mgr = make_manager()
        result = mgr.reveal_secret("nonexistent")
        assert result is None


class TestPricing:

    def test_pricing_danger_1(self):
        mgr = make_manager()
        p = mgr.get_pricing(1, "newcomer")
        assert p["danger_score"] == 1
        assert p["rep_modifier"] == 1.5
        assert p["escrow_window_seconds"] == 3600
        assert p["adjusted_sats"] >= 0

    def test_pricing_danger_5(self):
        mgr = make_manager()
        p = mgr.get_pricing(5, "trusted")
        assert p["danger_score"] == 5
        assert p["rep_modifier"] == 0.75

    def test_pricing_danger_10(self):
        mgr = make_manager()
        p = mgr.get_pricing(10, "senior")
        assert p["danger_score"] == 10
        assert p["rep_modifier"] == 0.5

    def test_pricing_clamps_danger(self):
        mgr = make_manager()
        p = mgr.get_pricing(0)
        assert p["danger_score"] == 1
        p = mgr.get_pricing(15)
        assert p["danger_score"] == 10

    def test_pricing_unknown_tier_defaults_newcomer(self):
        mgr = make_manager()
        p = mgr.get_pricing(3, "unknown_tier")
        assert p["rep_tier"] == "newcomer"

    def test_senior_lower_than_newcomer(self):
        mgr = make_manager()
        p_new = mgr.get_pricing(5, "newcomer")
        p_senior = mgr.get_pricing(5, "senior")
        assert p_senior["adjusted_sats"] <= p_new["adjusted_sats"]


class TestTicketCreation:

    def test_create_single_ticket(self):
        mgr = make_manager()
        ticket = mgr.create_ticket(
            agent_id=BOB_PUBKEY, task_id="task1",
            danger_score=3, amount_sats=100,
            mint_url=MINT_URL, ticket_type="single",
        )
        assert ticket is not None
        assert ticket["agent_id"] == BOB_PUBKEY
        assert ticket["amount_sats"] == 100
        assert ticket["status"] == "active"
        assert ticket["ticket_type"] == "single"

    def test_create_batch_ticket(self):
        mgr = make_manager()
        ticket = mgr.create_ticket(
            agent_id=BOB_PUBKEY, task_id="task2",
            danger_score=5, amount_sats=200,
            mint_url=MINT_URL, ticket_type="batch",
        )
        assert ticket is not None
        assert ticket["ticket_type"] == "batch"

    def test_create_milestone_ticket(self):
        mgr = make_manager()
        ticket = mgr.create_ticket(
            agent_id=BOB_PUBKEY, task_id="task3",
            danger_score=7, amount_sats=500,
            mint_url=MINT_URL, ticket_type="milestone",
        )
        assert ticket is not None
        assert ticket["ticket_type"] == "milestone"

    def test_create_performance_ticket(self):
        mgr = make_manager()
        ticket = mgr.create_ticket(
            agent_id=BOB_PUBKEY, task_id="task4",
            danger_score=4, amount_sats=50,
            mint_url=MINT_URL, ticket_type="performance",
        )
        assert ticket is not None
        assert ticket["ticket_type"] == "performance"

    def test_reject_invalid_ticket_type(self):
        mgr = make_manager()
        ticket = mgr.create_ticket(
            agent_id=BOB_PUBKEY, task_id="task5",
            danger_score=3, amount_sats=100,
            mint_url=MINT_URL, ticket_type="invalid",
        )
        assert ticket is None

    def test_reject_invalid_amount(self):
        mgr = make_manager()
        ticket = mgr.create_ticket(
            agent_id=BOB_PUBKEY, task_id="task6",
            danger_score=3, amount_sats=-1,
            mint_url=MINT_URL,
        )
        assert ticket is None

    def test_reject_unacceptable_mint(self):
        mgr = make_manager()
        ticket = mgr.create_ticket(
            agent_id=BOB_PUBKEY, task_id="task7",
            danger_score=3, amount_sats=100,
            mint_url="https://evil-mint.com",
        )
        assert ticket is None

    def test_reject_invalid_danger_score(self):
        mgr = make_manager()
        ticket = mgr.create_ticket(
            agent_id=BOB_PUBKEY, task_id="task8",
            danger_score=0, amount_sats=100,
            mint_url=MINT_URL,
        )
        assert ticket is None

    def test_ticket_has_htlc_hash(self):
        mgr = make_manager()
        ticket = mgr.create_ticket(
            agent_id=BOB_PUBKEY, task_id="task9",
            danger_score=3, amount_sats=100,
            mint_url=MINT_URL,
        )
        assert ticket is not None
        assert len(ticket["htlc_hash"]) == 64  # SHA256 hex

    def test_ticket_stored_in_db(self):
        mgr = make_manager()
        ticket = mgr.create_ticket(
            agent_id=BOB_PUBKEY, task_id="task10",
            danger_score=3, amount_sats=100,
            mint_url=MINT_URL,
        )
        stored = mgr.db.get_escrow_ticket(ticket["ticket_id"])
        assert stored is not None
        assert stored["agent_id"] == BOB_PUBKEY


class TestTicketValidation:

    def test_valid_token_json(self):
        mgr = make_manager()
        token = json.dumps({
            "mint": MINT_URL,
            "amount": 100,
            "ticket_type": "single",
            "conditions": {
                "nut10": {"kind": "HTLC", "data": "a" * 64},
                "nut11": {"pubkey": BOB_PUBKEY},
                "nut14": {"timelock": int(time.time()) + 3600, "refund_pubkey": ALICE_PUBKEY},
            }
        })
        valid, err = mgr.validate_ticket(token)
        assert valid
        assert err == ""

    def test_invalid_json(self):
        mgr = make_manager()
        valid, err = mgr.validate_ticket("not json")
        assert not valid
        assert "invalid JSON" in err

    def test_missing_fields(self):
        mgr = make_manager()
        valid, err = mgr.validate_ticket(json.dumps({"mint": MINT_URL}))
        assert not valid
        assert "missing field" in err

    def test_invalid_ticket_type(self):
        mgr = make_manager()
        token = json.dumps({
            "mint": MINT_URL, "amount": 100, "ticket_type": "bad",
            "conditions": {"nut10": {"kind": "HTLC", "data": "a" * 64},
                          "nut11": {"pubkey": BOB_PUBKEY},
                          "nut14": {"timelock": 1, "refund_pubkey": ALICE_PUBKEY}},
        })
        valid, err = mgr.validate_ticket(token)
        assert not valid

    def test_invalid_htlc_hash_length(self):
        mgr = make_manager()
        token = json.dumps({
            "mint": MINT_URL, "amount": 100, "ticket_type": "single",
            "conditions": {"nut10": {"kind": "HTLC", "data": "short"},
                          "nut11": {"pubkey": BOB_PUBKEY},
                          "nut14": {"timelock": 1, "refund_pubkey": ALICE_PUBKEY}},
        })
        valid, err = mgr.validate_ticket(token)
        assert not valid


class TestRedemption:

    def test_redeem_with_valid_preimage(self):
        mgr = make_manager()
        ticket = mgr.create_ticket(
            agent_id=BOB_PUBKEY, task_id="redeem_task",
            danger_score=3, amount_sats=100,
            mint_url=MINT_URL,
        )
        preimage = mgr.reveal_secret("redeem_task", require_receipt=False)
        result = mgr.redeem_ticket(ticket["ticket_id"], preimage)
        assert result["status"] == "redeemed"
        assert result["preimage_valid"]

    def test_redeem_with_invalid_preimage(self):
        mgr = make_manager()
        ticket = mgr.create_ticket(
            agent_id=BOB_PUBKEY, task_id="bad_redeem",
            danger_score=3, amount_sats=100,
            mint_url=MINT_URL,
        )
        result = mgr.redeem_ticket(ticket["ticket_id"], "00" * 32)
        assert "error" in result

    def test_redeem_nonexistent_ticket(self):
        mgr = make_manager()
        result = mgr.redeem_ticket("nonexistent", "00" * 32)
        assert "error" in result

    def test_redeem_already_redeemed(self):
        mgr = make_manager()
        ticket = mgr.create_ticket(
            agent_id=BOB_PUBKEY, task_id="double_redeem",
            danger_score=3, amount_sats=100,
            mint_url=MINT_URL,
        )
        preimage = mgr.reveal_secret("double_redeem", require_receipt=False)
        mgr.redeem_ticket(ticket["ticket_id"], preimage)
        # Try again
        result = mgr.redeem_ticket(ticket["ticket_id"], preimage)
        assert "error" in result


class TestRefund:

    def test_refund_after_timelock(self):
        mgr = make_manager()
        ticket = mgr.create_ticket(
            agent_id=BOB_PUBKEY, task_id="refund_task",
            danger_score=3, amount_sats=100,
            mint_url=MINT_URL,
        )
        # Force timelock to past
        mgr.db.tickets[ticket["ticket_id"]]["timelock"] = int(time.time()) - 1
        result = mgr.refund_ticket(ticket["ticket_id"])
        assert result["status"] == "refunded"

    def test_refund_before_timelock(self):
        mgr = make_manager()
        ticket = mgr.create_ticket(
            agent_id=BOB_PUBKEY, task_id="early_refund",
            danger_score=3, amount_sats=100,
            mint_url=MINT_URL,
        )
        result = mgr.refund_ticket(ticket["ticket_id"])
        assert "error" in result
        assert "timelock" in result["error"]


class TestReceipts:

    def test_create_receipt(self):
        mgr = make_manager()
        ticket = mgr.create_ticket(
            agent_id=BOB_PUBKEY, task_id="receipt_task",
            danger_score=3, amount_sats=100,
            mint_url=MINT_URL,
        )
        receipt = mgr.create_receipt(
            ticket_id=ticket["ticket_id"],
            schema_id="channel_management",
            action="set_fee",
            params={"fee_ppm": 100},
            result={"success": True},
            success=True,
        )
        assert receipt is not None
        assert receipt["success"]
        assert receipt["node_signature"] != ""

    def test_receipt_stored_in_db(self):
        mgr = make_manager()
        ticket = mgr.create_ticket(
            agent_id=BOB_PUBKEY, task_id="receipt_db_task",
            danger_score=3, amount_sats=100,
            mint_url=MINT_URL,
        )
        mgr.create_receipt(
            ticket_id=ticket["ticket_id"],
            schema_id="test", action="test",
            params={}, result=None, success=False,
        )
        receipts = mgr.db.get_escrow_receipts(ticket["ticket_id"])
        assert len(receipts) == 1


class TestMaintenance:

    def test_cleanup_expired_tickets(self):
        mgr = make_manager()
        ticket = mgr.create_ticket(
            agent_id=BOB_PUBKEY, task_id="expire_task",
            danger_score=1, amount_sats=5,
            mint_url=MINT_URL,
        )
        # Force past timelock
        mgr.db.tickets[ticket["ticket_id"]]["timelock"] = int(time.time()) - 1
        count = mgr.cleanup_expired_tickets()
        assert count == 1
        assert mgr.db.tickets[ticket["ticket_id"]]["status"] == "expired"

    def test_prune_old_secrets(self):
        mgr = make_manager()
        mgr.generate_secret("old_task", "old_ticket")
        mgr.reveal_secret("old_task", require_receipt=False)
        # Force old reveal time
        mgr.db.secrets["old_task"]["revealed_at"] = int(time.time()) - (91 * 86400)
        count = mgr.prune_old_secrets()
        assert count == 1

    def test_get_mint_status(self):
        mgr = make_manager()
        status = mgr.get_mint_status(MINT_URL)
        assert status["mint_url"] == MINT_URL
        assert status["state"] == "closed"


class TestMintExecutorIsolation:

    def test_mint_http_call_uses_executor(self):
        mgr = make_manager()
        future = MagicMock()
        future.result.return_value = {"states": ["UNSPENT"]}
        with patch.object(mgr._mint_executor, "submit", return_value=future) as submit:
            result = mgr._mint_http_call(
                MINT_URL, "/v1/checkstate", method="POST", body=b"{}"
            )
        assert result == {"states": ["UNSPENT"]}
        submit.assert_called_once()

    def test_mint_http_call_timeout_records_failure(self):
        mgr = make_manager()
        future = MagicMock()
        future.result.side_effect = concurrent.futures.TimeoutError()
        with patch.object(mgr._mint_executor, "submit", return_value=future):
            result = mgr._mint_http_call(
                MINT_URL, "/v1/checkstate", method="POST", body=b"{}"
            )
        assert result is None
        future.cancel.assert_called_once()
        stats = mgr.get_mint_status(MINT_URL)
        assert stats["failure_count"] == 1


class TestRowCaps:

    def test_ticket_row_cap(self):
        mgr = make_manager()
        mgr.MAX_ESCROW_TICKET_ROWS = 2
        mgr.create_ticket(BOB_PUBKEY, "t1", 3, 100, MINT_URL)
        mgr.create_ticket(BOB_PUBKEY, "t2", 3, 100, MINT_URL)
        # Third should fail
        result = mgr.create_ticket(BOB_PUBKEY, "t3", 3, 100, MINT_URL)
        assert result is None

    def test_active_ticket_limit(self):
        mgr = make_manager()
        mgr.MAX_ACTIVE_TICKETS = 1
        mgr.create_ticket(BOB_PUBKEY, "active1", 3, 100, MINT_URL)
        result = mgr.create_ticket(BOB_PUBKEY, "active2", 3, 100, MINT_URL)
        assert result is None
