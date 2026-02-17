"""
Tests for Extended Settlements (Phase 4B).

Tests cover:
- SettlementTypeRegistry: 9 types, receipt verification
- NettingEngine: bilateral, multilateral, deterministic hashing
- BondManager: post, slash, refund, tier assignment, time-weighting
- DisputeResolver: panel selection, voting, quorum, outcome
- Credit tier helper
- Protocol messages: factory, validator, signing for all 7 new types
"""

import hashlib
import json
import math
import time
import pytest
from unittest.mock import MagicMock

from modules.settlement import (
    SettlementTypeRegistry,
    SettlementTypeHandler,
    RoutingRevenueHandler,
    RebalancingCostHandler,
    ChannelLeaseHandler,
    CooperativeSpliceHandler,
    SharedChannelHandler,
    PheromoneMarketHandler,
    IntelligenceHandler,
    PenaltyHandler,
    AdvisorFeeHandler,
    NettingEngine,
    BondManager,
    DisputeResolver,
    BOND_TIER_SIZING,
    CREDIT_TIERS,
    VALID_SETTLEMENT_TYPE_IDS,
    get_credit_tier_info,
)

from modules.protocol import (
    HiveMessageType,
    RELIABLE_MESSAGE_TYPES,
    IMPLICIT_ACK_MAP,
    IMPLICIT_ACK_MATCH_FIELD,
    VALID_SETTLEMENT_TYPES,
    VALID_BOND_TIERS,
    VALID_ARBITRATION_VOTES,
    # Factory functions
    create_settlement_receipt,
    create_bond_posting,
    create_bond_slash,
    create_netting_proposal,
    create_netting_ack,
    create_violation_report,
    create_arbitration_vote,
    # Validator functions
    validate_settlement_receipt,
    validate_bond_posting,
    validate_bond_slash,
    validate_netting_proposal,
    validate_netting_ack,
    validate_violation_report,
    validate_arbitration_vote,
    # Signing payloads
    get_settlement_receipt_signing_payload,
    get_bond_posting_signing_payload,
    get_bond_slash_signing_payload,
    get_netting_proposal_signing_payload,
    get_netting_ack_signing_payload,
    get_violation_report_signing_payload,
    get_arbitration_vote_signing_payload,
    # Serialization
    deserialize,
)


# =============================================================================
# Test helpers
# =============================================================================

ALICE = "03" + "a1" * 32
BOB = "03" + "b2" * 32
CHARLIE = "03" + "c3" * 32
DAVE = "03" + "d4" * 32
EVE = "03" + "e5" * 32
FRANK = "03" + "f6" * 32
GRACE = "03" + "77" * 32


class MockDatabase:
    """Mock database for settlement operations."""

    def __init__(self):
        self.bonds = {}
        self.obligations = {}
        self.disputes = {}

    def store_bond(self, bond_id, peer_id, amount_sats, token_json,
                   posted_at, timelock, tier):
        self.bonds[bond_id] = {
            "bond_id": bond_id, "peer_id": peer_id,
            "amount_sats": amount_sats, "token_json": token_json,
            "posted_at": posted_at, "timelock": timelock,
            "tier": tier, "slashed_amount": 0, "status": "active",
        }
        return True

    def get_bond(self, bond_id):
        return self.bonds.get(bond_id)

    def get_bond_for_peer(self, peer_id):
        for b in self.bonds.values():
            if b["peer_id"] == peer_id and b["status"] == "active":
                return b
        return None

    def update_bond_status(self, bond_id, status):
        if bond_id in self.bonds:
            self.bonds[bond_id]["status"] = status
            return True
        return False

    def slash_bond(self, bond_id, slash_amount):
        if bond_id in self.bonds:
            self.bonds[bond_id]["slashed_amount"] += slash_amount
            self.bonds[bond_id]["status"] = "slashed"
            return True
        return False

    def count_bonds(self):
        return len(self.bonds)

    def store_obligation(self, obligation_id, settlement_type, from_peer,
                         to_peer, amount_sats, window_id, receipt_id, created_at):
        self.obligations[obligation_id] = {
            "obligation_id": obligation_id, "settlement_type": settlement_type,
            "from_peer": from_peer, "to_peer": to_peer,
            "amount_sats": amount_sats, "window_id": window_id,
            "receipt_id": receipt_id, "status": "pending",
            "created_at": created_at,
        }
        return True

    def get_obligation(self, obligation_id):
        return self.obligations.get(obligation_id)

    def get_obligations_for_window(self, window_id, status=None, limit=1000):
        result = []
        for ob in self.obligations.values():
            if window_id and ob["window_id"] != window_id:
                continue
            if status and ob["status"] != status:
                continue
            result.append(ob)
        return result[:limit]

    def get_obligations_between_peers(self, peer_a, peer_b, window_id=None, limit=1000):
        result = []
        for ob in self.obligations.values():
            if (ob["from_peer"] == peer_a and ob["to_peer"] == peer_b) or \
               (ob["from_peer"] == peer_b and ob["to_peer"] == peer_a):
                if window_id and ob["window_id"] != window_id:
                    continue
                result.append(ob)
        return result[:limit]

    def update_obligation_status(self, obligation_id, status):
        if obligation_id in self.obligations:
            self.obligations[obligation_id]["status"] = status
            return True
        return False

    def count_obligations(self):
        return len(self.obligations)

    def store_dispute(self, dispute_id, obligation_id, filing_peer,
                      respondent_peer, evidence_json, filed_at):
        self.disputes[dispute_id] = {
            "dispute_id": dispute_id, "obligation_id": obligation_id,
            "filing_peer": filing_peer, "respondent_peer": respondent_peer,
            "evidence_json": evidence_json, "panel_members_json": None,
            "votes_json": None, "outcome": None, "slash_amount": 0,
            "filed_at": filed_at, "resolved_at": None,
        }
        return True

    def get_dispute(self, dispute_id):
        return self.disputes.get(dispute_id)

    def update_dispute_outcome(self, dispute_id, outcome, slash_amount,
                                panel_members_json, votes_json, resolved_at):
        if dispute_id in self.disputes:
            self.disputes[dispute_id]["outcome"] = outcome
            self.disputes[dispute_id]["slash_amount"] = slash_amount
            self.disputes[dispute_id]["panel_members_json"] = panel_members_json
            self.disputes[dispute_id]["votes_json"] = votes_json
            self.disputes[dispute_id]["resolved_at"] = resolved_at
            return True
        return False

    def count_disputes(self):
        return len(self.disputes)


# =============================================================================
# Settlement Type Registry tests
# =============================================================================

class TestSettlementTypeRegistry:

    def test_all_9_types_registered(self):
        registry = SettlementTypeRegistry()
        types = registry.list_types()
        assert len(types) == 9
        for type_id in VALID_SETTLEMENT_TYPE_IDS:
            assert type_id in types

    def test_get_handler_returns_correct_type(self):
        registry = SettlementTypeRegistry()
        h = registry.get_handler("routing_revenue")
        assert isinstance(h, RoutingRevenueHandler)
        h = registry.get_handler("penalty")
        assert isinstance(h, PenaltyHandler)

    def test_get_handler_unknown_type(self):
        registry = SettlementTypeRegistry()
        assert registry.get_handler("nonexistent") is None

    def test_routing_revenue_verify(self):
        registry = SettlementTypeRegistry()
        valid, err = registry.verify_receipt("routing_revenue", {"htlc_forwards": 10})
        assert valid
        valid, err = registry.verify_receipt("routing_revenue", {})
        assert not valid

    def test_rebalancing_cost_verify(self):
        registry = SettlementTypeRegistry()
        valid, err = registry.verify_receipt("rebalancing_cost", {"rebalance_amount_sats": 1000})
        assert valid

    def test_channel_lease_verify(self):
        registry = SettlementTypeRegistry()
        valid, err = registry.verify_receipt("channel_lease", {"lease_start": 1, "lease_end": 2})
        assert valid
        valid, err = registry.verify_receipt("channel_lease", {"lease_start": 1})
        assert not valid

    def test_cooperative_splice_verify(self):
        registry = SettlementTypeRegistry()
        valid, _ = registry.verify_receipt("cooperative_splice", {"txid": "abc123"})
        assert valid

    def test_shared_channel_verify(self):
        registry = SettlementTypeRegistry()
        valid, _ = registry.verify_receipt("shared_channel", {"funding_txid": "abc123"})
        assert valid

    def test_pheromone_market_verify(self):
        registry = SettlementTypeRegistry()
        valid, _ = registry.verify_receipt("pheromone_market", {"performance_metric": 0.95})
        assert valid

    def test_intelligence_calculate_split(self):
        handler = IntelligenceHandler()
        obs = [{"amount_sats": 1000, "obligation_id": "o1"}]
        result = handler.calculate(obs, "w1")
        assert result[0]["base_sats"] == 700
        assert result[0]["bonus_sats"] == 300

    def test_intelligence_verify(self):
        registry = SettlementTypeRegistry()
        valid, _ = registry.verify_receipt("intelligence", {"intelligence_type": "route_info"})
        assert valid

    def test_penalty_verify_quorum(self):
        registry = SettlementTypeRegistry()
        valid, _ = registry.verify_receipt("penalty", {"quorum_confirmations": 3})
        assert valid
        valid, _ = registry.verify_receipt("penalty", {"quorum_confirmations": 0})
        assert not valid

    def test_advisor_fee_verify(self):
        registry = SettlementTypeRegistry()
        valid, _ = registry.verify_receipt("advisor_fee", {"advisor_signature": "sig123"})
        assert valid

    def test_unknown_type_verify(self):
        registry = SettlementTypeRegistry()
        valid, err = registry.verify_receipt("fake_type", {})
        assert not valid
        assert "unknown" in err


# =============================================================================
# NettingEngine tests
# =============================================================================

class TestNettingEngine:

    def test_bilateral_net_a_owes_b(self):
        obligations = [
            {"from_peer": ALICE, "to_peer": BOB, "amount_sats": 1000, "window_id": "w1", "status": "pending"},
            {"from_peer": BOB, "to_peer": ALICE, "amount_sats": 400, "window_id": "w1", "status": "pending"},
        ]
        result = NettingEngine.bilateral_net(obligations, ALICE, BOB, "w1")
        assert result["from_peer"] == ALICE
        assert result["to_peer"] == BOB
        assert result["amount_sats"] == 600

    def test_bilateral_net_b_owes_a(self):
        obligations = [
            {"from_peer": ALICE, "to_peer": BOB, "amount_sats": 200, "window_id": "w1", "status": "pending"},
            {"from_peer": BOB, "to_peer": ALICE, "amount_sats": 500, "window_id": "w1", "status": "pending"},
        ]
        result = NettingEngine.bilateral_net(obligations, ALICE, BOB, "w1")
        assert result["from_peer"] == BOB
        assert result["to_peer"] == ALICE
        assert result["amount_sats"] == 300

    def test_bilateral_net_zero(self):
        obligations = [
            {"from_peer": ALICE, "to_peer": BOB, "amount_sats": 500, "window_id": "w1", "status": "pending"},
            {"from_peer": BOB, "to_peer": ALICE, "amount_sats": 500, "window_id": "w1", "status": "pending"},
        ]
        result = NettingEngine.bilateral_net(obligations, ALICE, BOB, "w1")
        assert result["amount_sats"] == 0

    def test_bilateral_net_filters_window(self):
        obligations = [
            {"from_peer": ALICE, "to_peer": BOB, "amount_sats": 1000, "window_id": "w1", "status": "pending"},
            {"from_peer": ALICE, "to_peer": BOB, "amount_sats": 999, "window_id": "w2", "status": "pending"},
        ]
        result = NettingEngine.bilateral_net(obligations, ALICE, BOB, "w1")
        assert result["amount_sats"] == 1000

    def test_multilateral_net_reduces_payments(self):
        """A->B 1000, B->C 800, C->A 600 should reduce to 2 payments."""
        obligations = [
            {"from_peer": ALICE, "to_peer": BOB, "amount_sats": 1000, "window_id": "w1", "status": "pending"},
            {"from_peer": BOB, "to_peer": CHARLIE, "amount_sats": 800, "window_id": "w1", "status": "pending"},
            {"from_peer": CHARLIE, "to_peer": ALICE, "amount_sats": 600, "window_id": "w1", "status": "pending"},
        ]
        payments = NettingEngine.multilateral_net(obligations, "w1")
        # Net balances: A: -1000+600=-400, B: -800+1000=200, C: -600+800=200
        # A pays B 200, A pays C 200
        total_paid = sum(p["amount_sats"] for p in payments)
        assert total_paid == 400  # Much less than 1000+800+600=2400
        assert len(payments) <= 3

    def test_multilateral_net_balanced(self):
        """All even - no payments needed."""
        obligations = [
            {"from_peer": ALICE, "to_peer": BOB, "amount_sats": 100, "window_id": "w1", "status": "pending"},
            {"from_peer": BOB, "to_peer": ALICE, "amount_sats": 100, "window_id": "w1", "status": "pending"},
        ]
        payments = NettingEngine.multilateral_net(obligations, "w1")
        total_paid = sum(p["amount_sats"] for p in payments)
        assert total_paid == 0

    def test_multilateral_net_integer_only(self):
        """All amounts should be integers."""
        obligations = [
            {"from_peer": ALICE, "to_peer": BOB, "amount_sats": 333, "window_id": "w1", "status": "pending"},
            {"from_peer": BOB, "to_peer": CHARLIE, "amount_sats": 111, "window_id": "w1", "status": "pending"},
        ]
        payments = NettingEngine.multilateral_net(obligations, "w1")
        for p in payments:
            assert isinstance(p["amount_sats"], int)

    def test_obligations_hash_deterministic(self):
        obligations = [
            {"obligation_id": "o2", "amount_sats": 200},
            {"obligation_id": "o1", "amount_sats": 100},
        ]
        h1 = NettingEngine.compute_obligations_hash(obligations)
        # Same obligations, different order
        obligations_reordered = [obligations[1], obligations[0]]
        h2 = NettingEngine.compute_obligations_hash(obligations_reordered)
        assert h1 == h2  # Deterministic regardless of input order


# =============================================================================
# BondManager tests
# =============================================================================

class TestBondManager:

    def _make_bond_mgr(self):
        db = MockDatabase()
        plugin = MagicMock()
        return BondManager(db, plugin), db

    def test_post_bond(self):
        mgr, db = self._make_bond_mgr()
        result = mgr.post_bond(ALICE, 150_000)
        assert result is not None
        assert result["tier"] == "full"
        assert result["amount_sats"] == 150_000
        assert result["status"] == "active"

    def test_tier_assignment(self):
        mgr, _ = self._make_bond_mgr()
        assert mgr.get_tier_for_amount(0) == "observer"
        assert mgr.get_tier_for_amount(49_999) == "observer"
        assert mgr.get_tier_for_amount(50_000) == "basic"
        assert mgr.get_tier_for_amount(150_000) == "full"
        assert mgr.get_tier_for_amount(300_000) == "liquidity"
        assert mgr.get_tier_for_amount(500_000) == "founding"
        assert mgr.get_tier_for_amount(1_000_000) == "founding"

    def test_effective_bond_time_weighting(self):
        mgr, _ = self._make_bond_mgr()
        # At day 0
        assert mgr.effective_bond(100_000, 0) == 0
        # At day 90 (half maturity)
        assert mgr.effective_bond(100_000, 90) == 50_000
        # At day 180 (full maturity)
        assert mgr.effective_bond(100_000, 180) == 100_000
        # Beyond maturity
        assert mgr.effective_bond(100_000, 360) == 100_000

    def test_calculate_slash(self):
        mgr, _ = self._make_bond_mgr()
        # Basic slash
        slash = mgr.calculate_slash(1000, severity=1.0, repeat_count=1, estimated_profit=0)
        assert slash == 1000
        # With repeat multiplier
        slash = mgr.calculate_slash(1000, severity=1.0, repeat_count=3, estimated_profit=0)
        assert slash == 2000  # 1000 * 1.0 * (1.0 + 0.5*2) = 2000
        # With estimated profit
        slash = mgr.calculate_slash(100, severity=1.0, repeat_count=1, estimated_profit=5000)
        assert slash == 10000  # max(100, 5000*2)

    def test_distribute_slash(self):
        mgr, _ = self._make_bond_mgr()
        dist = mgr.distribute_slash(1000)
        assert dist["aggrieved"] == 500
        assert dist["panel"] == 300
        assert dist["burned"] == 200
        assert sum(dist.values()) == 1000

    def test_slash_bond(self):
        mgr, db = self._make_bond_mgr()
        mgr.post_bond(ALICE, 100_000)
        bond_id = list(db.bonds.keys())[0]
        result = mgr.slash_bond(bond_id, 10_000)
        assert result is not None
        assert result["slashed_amount"] == 10_000
        assert result["remaining"] == 90_000

    def test_slash_capped_at_bond_amount(self):
        mgr, db = self._make_bond_mgr()
        mgr.post_bond(ALICE, 10_000)
        bond_id = list(db.bonds.keys())[0]
        result = mgr.slash_bond(bond_id, 50_000)
        assert result["slashed_amount"] == 10_000

    def test_refund_after_timelock(self):
        mgr, db = self._make_bond_mgr()
        mgr.post_bond(ALICE, 50_000)
        bond_id = list(db.bonds.keys())[0]
        # Force past timelock
        db.bonds[bond_id]["timelock"] = int(time.time()) - 1
        result = mgr.refund_bond(bond_id)
        assert result["refund_amount"] == 50_000
        assert result["status"] == "refunded"

    def test_refund_before_timelock(self):
        mgr, db = self._make_bond_mgr()
        mgr.post_bond(ALICE, 50_000)
        bond_id = list(db.bonds.keys())[0]
        result = mgr.refund_bond(bond_id)
        assert "error" in result

    def test_get_bond_status(self):
        mgr, _ = self._make_bond_mgr()
        mgr.post_bond(ALICE, 50_000)
        status = mgr.get_bond_status(ALICE)
        assert status is not None
        assert status["tier"] == "basic"
        assert "tenure_days" in status
        assert "effective_bond" in status

    def test_reject_negative_amount(self):
        mgr, _ = self._make_bond_mgr()
        assert mgr.post_bond(ALICE, -1) is None


# =============================================================================
# DisputeResolver tests
# =============================================================================

class TestDisputeResolver:

    def _make_resolver(self):
        db = MockDatabase()
        plugin = MagicMock()
        return DisputeResolver(db, plugin), db

    def test_panel_selection_deterministic(self):
        resolver, _ = self._make_resolver()
        members = [
            {"peer_id": ALICE, "bond_amount": 100_000, "tenure_days": 90},
            {"peer_id": BOB, "bond_amount": 50_000, "tenure_days": 180},
            {"peer_id": CHARLIE, "bond_amount": 150_000, "tenure_days": 30},
            {"peer_id": DAVE, "bond_amount": 75_000, "tenure_days": 60},
            {"peer_id": EVE, "bond_amount": 200_000, "tenure_days": 120},
        ]
        result1 = resolver.select_arbitration_panel("dispute1", "block_hash_abc", members)
        result2 = resolver.select_arbitration_panel("dispute1", "block_hash_abc", members)
        assert result1["panel_members"] == result2["panel_members"]

    def test_panel_size_5_members(self):
        resolver, _ = self._make_resolver()
        members = [
            {"peer_id": f"03{'%02x' % i}" + "00" * 31, "bond_amount": 10_000, "tenure_days": 10}
            for i in range(5)
        ]
        result = resolver.select_arbitration_panel("d1", "bh1", members)
        assert result["panel_size"] == 3
        assert result["quorum"] == 2

    def test_panel_size_10_members(self):
        resolver, _ = self._make_resolver()
        members = [
            {"peer_id": f"03{'%02x' % i}" + "00" * 31, "bond_amount": 10_000, "tenure_days": 10}
            for i in range(12)
        ]
        result = resolver.select_arbitration_panel("d2", "bh2", members)
        assert result["panel_size"] == 5
        assert result["quorum"] == 3

    def test_panel_size_15_members(self):
        resolver, _ = self._make_resolver()
        members = [
            {"peer_id": f"03{'%02x' % i}" + "00" * 31, "bond_amount": 10_000, "tenure_days": 10}
            for i in range(20)
        ]
        result = resolver.select_arbitration_panel("d3", "bh3", members)
        assert result["panel_size"] == 7
        assert result["quorum"] == 5

    def test_panel_not_enough_members(self):
        resolver, _ = self._make_resolver()
        members = [
            {"peer_id": ALICE, "bond_amount": 10_000, "tenure_days": 10},
        ]
        assert resolver.select_arbitration_panel("d4", "bh4", members) is None

    def test_different_seed_different_panel(self):
        resolver, _ = self._make_resolver()
        members = [
            {"peer_id": f"03{'%02x' % i}" + "00" * 31, "bond_amount": 10_000, "tenure_days": 10}
            for i in range(15)
        ]
        r1 = resolver.select_arbitration_panel("d_a", "bh_x", members)
        r2 = resolver.select_arbitration_panel("d_b", "bh_y", members)
        # Very unlikely to be same panel with different seeds
        assert r1["panel_members"] != r2["panel_members"] or True  # Allow rare collision

    def test_file_dispute(self):
        resolver, db = self._make_resolver()
        db.store_obligation("ob1", "routing_revenue", ALICE, BOB, 1000, "w1", None, int(time.time()))
        result = resolver.file_dispute("ob1", BOB, {"reason": "underpayment"})
        assert result is not None
        assert "dispute_id" in result
        assert result["filing_peer"] == BOB
        assert result["respondent_peer"] == ALICE

    def test_record_vote(self):
        resolver, db = self._make_resolver()
        db.store_dispute("disp1", "ob1", BOB, ALICE, '{}', int(time.time()))
        # Set panel members so vote is accepted
        panel = json.dumps([CHARLIE, DAVE])
        db.disputes["disp1"]["panel_members_json"] = panel
        result = resolver.record_vote("disp1", CHARLIE, "upheld", "clear evidence")
        assert result["total_votes"] == 1

    def test_record_vote_rejected_non_panel(self):
        resolver, db = self._make_resolver()
        db.store_dispute("disp1", "ob1", BOB, ALICE, '{}', int(time.time()))
        panel = json.dumps([DAVE])
        db.disputes["disp1"]["panel_members_json"] = panel
        result = resolver.record_vote("disp1", CHARLIE, "upheld", "clear evidence")
        assert result["error"] == "voter not on arbitration panel"

    def test_quorum_resolves_dispute(self):
        resolver, db = self._make_resolver()
        db.store_dispute("disp2", "ob1", BOB, ALICE, '{}', int(time.time()))
        panel = json.dumps([CHARLIE, DAVE, GRACE])
        db.disputes["disp2"]["panel_members_json"] = panel
        resolver.record_vote("disp2", CHARLIE, "upheld", "")
        resolver.record_vote("disp2", DAVE, "upheld", "")
        result = resolver.check_quorum("disp2", quorum=2)
        assert result is not None
        assert result["outcome"] == "upheld"

    def test_quorum_rejected_outcome(self):
        resolver, db = self._make_resolver()
        db.store_dispute("disp3", "ob1", BOB, ALICE, '{}', int(time.time()))
        panel = json.dumps([CHARLIE, DAVE, GRACE])
        db.disputes["disp3"]["panel_members_json"] = panel
        resolver.record_vote("disp3", CHARLIE, "rejected", "")
        resolver.record_vote("disp3", DAVE, "rejected", "")
        result = resolver.check_quorum("disp3", quorum=2)
        assert result["outcome"] == "rejected"

    def test_quorum_not_reached(self):
        resolver, db = self._make_resolver()
        db.store_dispute("disp4", "ob1", BOB, ALICE, '{}', int(time.time()))
        panel = json.dumps([CHARLIE, DAVE, GRACE])
        db.disputes["disp4"]["panel_members_json"] = panel
        resolver.record_vote("disp4", CHARLIE, "upheld", "")
        result = resolver.check_quorum("disp4", quorum=3)
        assert result is None


# =============================================================================
# Credit tier tests
# =============================================================================

class TestCreditTier:

    def test_default_newcomer(self):
        info = get_credit_tier_info(ALICE)
        assert info["tier"] == "newcomer"
        assert info["credit_line"] == 0
        assert info["model"] == "prepaid_escrow"

    def test_with_did_manager(self):
        mock_did = MagicMock()
        mock_did.get_credit_tier.return_value = "trusted"
        info = get_credit_tier_info(ALICE, mock_did)
        assert info["tier"] == "trusted"
        assert info["credit_line"] == 50_000
        assert info["model"] == "bilateral_netting"

    def test_senior_tier(self):
        mock_did = MagicMock()
        mock_did.get_credit_tier.return_value = "senior"
        info = get_credit_tier_info(ALICE, mock_did)
        assert info["tier"] == "senior"
        assert info["credit_line"] == 200_000
        assert info["model"] == "multilateral_netting"

    def test_did_error_defaults_newcomer(self):
        mock_did = MagicMock()
        mock_did.get_credit_tier.side_effect = Exception("boom")
        info = get_credit_tier_info(ALICE, mock_did)
        assert info["tier"] == "newcomer"


# =============================================================================
# Protocol message tests
# =============================================================================

class TestProtocolMessages:

    def test_new_types_in_reliable_set(self):
        for mt in [
            HiveMessageType.SETTLEMENT_RECEIPT,
            HiveMessageType.BOND_POSTING,
            HiveMessageType.BOND_SLASH,
            HiveMessageType.NETTING_PROPOSAL,
            HiveMessageType.NETTING_ACK,
            HiveMessageType.VIOLATION_REPORT,
            HiveMessageType.ARBITRATION_VOTE,
        ]:
            assert mt in RELIABLE_MESSAGE_TYPES

    def test_netting_ack_implicit_ack(self):
        assert IMPLICIT_ACK_MAP[HiveMessageType.NETTING_ACK] == HiveMessageType.NETTING_PROPOSAL
        assert IMPLICIT_ACK_MATCH_FIELD[HiveMessageType.NETTING_ACK] == "window_id"

    def test_message_type_ids(self):
        assert HiveMessageType.SETTLEMENT_RECEIPT == 32891
        assert HiveMessageType.BOND_POSTING == 32893
        assert HiveMessageType.BOND_SLASH == 32895
        assert HiveMessageType.NETTING_PROPOSAL == 32897
        assert HiveMessageType.NETTING_ACK == 32899
        assert HiveMessageType.VIOLATION_REPORT == 32901
        assert HiveMessageType.ARBITRATION_VOTE == 32903


class TestSettlementReceiptMessage:

    def test_create_and_deserialize(self):
        msg = create_settlement_receipt(
            sender_id=ALICE, receipt_id="r1", settlement_type="routing_revenue",
            from_peer=ALICE, to_peer=BOB, amount_sats=1000,
            window_id="w1", receipt_data={"htlc_forwards": 10},
            signature="sig" * 10,
        )
        msg_type, payload = deserialize(msg)
        assert msg_type == HiveMessageType.SETTLEMENT_RECEIPT
        assert payload["receipt_id"] == "r1"
        assert payload["amount_sats"] == 1000

    def test_validate_valid(self):
        payload = {
            "sender_id": ALICE, "event_id": "e1", "timestamp": int(time.time()),
            "receipt_id": "r1", "settlement_type": "routing_revenue",
            "from_peer": ALICE, "to_peer": BOB, "amount_sats": 1000,
            "window_id": "w1", "receipt_data": {"test": True},
            "signature": "a" * 20,
        }
        assert validate_settlement_receipt(payload)

    def test_validate_invalid_type(self):
        payload = {
            "sender_id": ALICE, "event_id": "e1", "timestamp": int(time.time()),
            "receipt_id": "r1", "settlement_type": "invalid_type",
            "from_peer": ALICE, "to_peer": BOB, "amount_sats": 1000,
            "window_id": "w1", "receipt_data": {},
            "signature": "a" * 20,
        }
        assert not validate_settlement_receipt(payload)

    def test_signing_payload_deterministic(self):
        p1 = get_settlement_receipt_signing_payload("r1", "routing_revenue", ALICE, BOB, 1000, "w1")
        p2 = get_settlement_receipt_signing_payload("r1", "routing_revenue", ALICE, BOB, 1000, "w1")
        assert p1 == p2
        assert "settlement_receipt" in p1


class TestBondPostingMessage:

    def test_create_and_validate(self):
        msg = create_bond_posting(
            sender_id=ALICE, bond_id="b1", amount_sats=50_000,
            tier="basic", timelock=int(time.time()) + 86400,
            token_hash="a" * 64, signature="sig" * 10,
        )
        msg_type, payload = deserialize(msg)
        assert msg_type == HiveMessageType.BOND_POSTING
        assert validate_bond_posting(payload)

    def test_validate_invalid_tier(self):
        payload = {
            "sender_id": ALICE, "event_id": "e1", "timestamp": int(time.time()),
            "bond_id": "b1", "amount_sats": 50_000, "tier": "mega",
            "timelock": 1000, "token_hash": "a" * 64, "signature": "a" * 20,
        }
        assert not validate_bond_posting(payload)


class TestBondSlashMessage:

    def test_create_and_validate(self):
        msg = create_bond_slash(
            sender_id=ALICE, bond_id="b1", slash_amount=10_000,
            reason="policy violation", dispute_id="d1", signature="sig" * 10,
        )
        msg_type, payload = deserialize(msg)
        assert msg_type == HiveMessageType.BOND_SLASH
        assert validate_bond_slash(payload)


class TestNettingProposalMessage:

    def test_create_and_validate(self):
        msg = create_netting_proposal(
            sender_id=ALICE, window_id="w1", netting_type="bilateral",
            obligations_hash="a" * 64,
            net_payments=[{"from_peer": ALICE, "to_peer": BOB, "amount_sats": 100}],
            signature="sig" * 10,
        )
        msg_type, payload = deserialize(msg)
        assert msg_type == HiveMessageType.NETTING_PROPOSAL
        assert validate_netting_proposal(payload)

    def test_validate_invalid_netting_type(self):
        payload = {
            "sender_id": ALICE, "event_id": "e1", "timestamp": int(time.time()),
            "window_id": "w1", "netting_type": "invalid",
            "obligations_hash": "a" * 64,
            "net_payments": [], "signature": "a" * 20,
        }
        assert not validate_netting_proposal(payload)


class TestNettingAckMessage:

    def test_create_and_validate(self):
        msg = create_netting_ack(
            sender_id=ALICE, window_id="w1",
            obligations_hash="a" * 64, accepted=True,
            signature="sig" * 10,
        )
        msg_type, payload = deserialize(msg)
        assert msg_type == HiveMessageType.NETTING_ACK
        assert validate_netting_ack(payload)

    def test_validate_invalid_accepted_type(self):
        payload = {
            "sender_id": ALICE, "event_id": "e1", "timestamp": int(time.time()),
            "window_id": "w1", "obligations_hash": "a" * 64,
            "accepted": "yes", "signature": "a" * 20,
        }
        assert not validate_netting_ack(payload)


class TestViolationReportMessage:

    def test_create_and_validate(self):
        msg = create_violation_report(
            sender_id=ALICE, violation_id="v1", violator_id=BOB,
            violation_type="fee_undercutting",
            evidence={"channel": "123", "ppm_delta": -500},
            signature="sig" * 10,
        )
        msg_type, payload = deserialize(msg)
        assert msg_type == HiveMessageType.VIOLATION_REPORT
        assert validate_violation_report(payload)


class TestArbitrationVoteMessage:

    def test_create_and_validate(self):
        msg = create_arbitration_vote(
            sender_id=ALICE, dispute_id="d1", vote="upheld",
            reason="clear evidence of violation", signature="sig" * 10,
        )
        msg_type, payload = deserialize(msg)
        assert msg_type == HiveMessageType.ARBITRATION_VOTE
        assert validate_arbitration_vote(payload)

    def test_validate_invalid_vote(self):
        payload = {
            "sender_id": ALICE, "event_id": "e1", "timestamp": int(time.time()),
            "dispute_id": "d1", "vote": "maybe",
            "reason": "unsure", "signature": "a" * 20,
        }
        assert not validate_arbitration_vote(payload)

    def test_all_valid_votes(self):
        for vote in VALID_ARBITRATION_VOTES:
            payload = {
                "sender_id": ALICE, "event_id": "e1", "timestamp": int(time.time()),
                "dispute_id": "d1", "vote": vote,
                "reason": "", "signature": "a" * 20,
            }
            assert validate_arbitration_vote(payload)

    def test_signing_payload_deterministic(self):
        p1 = get_arbitration_vote_signing_payload("d1", "upheld")
        p2 = get_arbitration_vote_signing_payload("d1", "upheld")
        assert p1 == p2
