"""
Tests for DID Credential Module (Phase 16 - DID Ecosystem).

Tests cover:
- DIDCredentialManager: issuance, verification, revocation, aggregation
- Credential profiles and metric validation
- Self-issuance rejection
- Row cap enforcement
- Aggregation with recency decay, issuer weight, evidence strength
- Cache invalidation
- Protocol message creation and validation
- Handler functions for incoming credentials and revocations
"""

import json
import time
import uuid
import pytest
from unittest.mock import MagicMock, patch

from modules.did_credentials import (
    DIDCredentialManager,
    DIDCredential,
    AggregatedReputation,
    CredentialProfile,
    CREDENTIAL_PROFILES,
    VALID_DOMAINS,
    VALID_OUTCOMES,
    MAX_CREDENTIALS_PER_PEER,
    MAX_TOTAL_CREDENTIALS,
    MAX_AGGREGATION_CACHE_ENTRIES,
    AGGREGATION_CACHE_TTL,
    RECENCY_DECAY_LAMBDA,
    get_credential_signing_payload,
    validate_metrics_for_profile,
    _is_valid_pubkey,
    _score_to_tier,
    _compute_confidence,
)

from modules.protocol import (
    HiveMessageType,
    create_did_credential_present,
    validate_did_credential_present,
    get_did_credential_present_signing_payload,
    create_did_credential_revoke,
    validate_did_credential_revoke,
    get_did_credential_revoke_signing_payload,
)


# =============================================================================
# Test helpers
# =============================================================================

ALICE_PUBKEY = "03" + "a1" * 32  # 66 hex chars
BOB_PUBKEY = "03" + "b2" * 32
CHARLIE_PUBKEY = "03" + "c3" * 32
DAVE_PUBKEY = "03" + "d4" * 32


class MockDatabase:
    """Mock database with DID credential methods."""

    def __init__(self):
        self.credentials = {}
        self.reputation_cache = {}
        self.members = {}

    def store_did_credential(self, credential_id, issuer_id, subject_id, domain,
                              period_start, period_end, metrics_json, outcome,
                              evidence_json, signature, issued_at, expires_at,
                              received_from):
        self.credentials[credential_id] = {
            "credential_id": credential_id,
            "issuer_id": issuer_id,
            "subject_id": subject_id,
            "domain": domain,
            "period_start": period_start,
            "period_end": period_end,
            "metrics_json": metrics_json,
            "outcome": outcome,
            "evidence_json": evidence_json,
            "signature": signature,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "revoked_at": None,
            "revocation_reason": None,
            "received_from": received_from,
        }
        return True

    def get_did_credential(self, credential_id):
        return self.credentials.get(credential_id)

    def get_did_credentials_for_subject(self, subject_id, domain=None, limit=100):
        results = []
        for c in self.credentials.values():
            if c["subject_id"] == subject_id:
                if domain and c["domain"] != domain:
                    continue
                results.append(c)
        return sorted(results, key=lambda x: x["issued_at"], reverse=True)[:limit]

    def get_did_credentials_by_issuer(self, issuer_id, subject_id=None, limit=100):
        results = []
        for c in self.credentials.values():
            if c["issuer_id"] == issuer_id:
                if subject_id and c["subject_id"] != subject_id:
                    continue
                results.append(c)
        return sorted(results, key=lambda x: x["issued_at"], reverse=True)[:limit]

    def revoke_did_credential(self, credential_id, reason, timestamp):
        if credential_id in self.credentials:
            self.credentials[credential_id]["revoked_at"] = timestamp
            self.credentials[credential_id]["revocation_reason"] = reason
            return True
        return False

    def count_did_credentials(self):
        return len(self.credentials)

    def count_did_credentials_for_subject(self, subject_id):
        return sum(1 for c in self.credentials.values() if c["subject_id"] == subject_id)

    def cleanup_expired_did_credentials(self, before_ts):
        to_remove = [cid for cid, c in self.credentials.items()
                     if c.get("expires_at") is not None and c["expires_at"] < before_ts]
        for cid in to_remove:
            del self.credentials[cid]
        return len(to_remove)

    def store_did_reputation_cache(self, subject_id, domain, score, tier,
                                    confidence, credential_count, issuer_count,
                                    computed_at, components_json=None):
        key = f"{subject_id}:{domain}"
        self.reputation_cache[key] = {
            "subject_id": subject_id,
            "domain": domain,
            "score": score,
            "tier": tier,
            "confidence": confidence,
            "credential_count": credential_count,
            "issuer_count": issuer_count,
            "computed_at": computed_at,
            "components_json": components_json,
        }
        return True

    def get_did_reputation_cache(self, subject_id, domain=None):
        target_domain = domain or "_all"
        key = f"{subject_id}:{target_domain}"
        return self.reputation_cache.get(key)

    def get_stale_did_reputation_cache(self, before_ts, limit=50):
        results = []
        for entry in self.reputation_cache.values():
            if entry.get("computed_at", 0) < before_ts:
                results.append(entry)
        return results[:limit]

    def get_all_members(self):
        return list(self.members.values())

    def get_member(self, peer_id):
        return self.members.get(peer_id)


def _make_manager(our_pubkey=ALICE_PUBKEY, with_rpc=True):
    """Create a DIDCredentialManager with mocked dependencies."""
    db = MockDatabase()
    plugin = MagicMock()
    rpc = MagicMock() if with_rpc else None
    if rpc:
        rpc.signmessage.return_value = {"zbase": "fakesig_zbase32encoded"}
        rpc.checkmessage.return_value = {"verified": True, "pubkey": ALICE_PUBKEY}
    return DIDCredentialManager(database=db, plugin=plugin, rpc=rpc, our_pubkey=our_pubkey), db


def _valid_node_metrics():
    return {
        "routing_reliability": 0.95,
        "uptime": 0.99,
        "htlc_success_rate": 0.98,
        "avg_fee_ppm": 50,
    }


def _valid_advisor_metrics():
    return {
        "revenue_delta_pct": 15.5,
        "actions_taken": 42,
        "uptime_pct": 99.1,
        "channels_managed": 12,
    }


# =============================================================================
# Credential Profiles
# =============================================================================

class TestCredentialProfiles:
    """Test credential profile definitions and metric validation."""

    def test_all_four_profiles_defined(self):
        assert len(CREDENTIAL_PROFILES) == 4
        assert "hive:advisor" in CREDENTIAL_PROFILES
        assert "hive:node" in CREDENTIAL_PROFILES
        assert "hive:client" in CREDENTIAL_PROFILES
        assert "agent:general" in CREDENTIAL_PROFILES

    def test_validate_valid_node_metrics(self):
        err = validate_metrics_for_profile("hive:node", _valid_node_metrics())
        assert err is None

    def test_validate_missing_required_metric(self):
        metrics = _valid_node_metrics()
        del metrics["uptime"]
        err = validate_metrics_for_profile("hive:node", metrics)
        assert err is not None
        assert "missing required metric" in err

    def test_validate_unknown_metric(self):
        metrics = _valid_node_metrics()
        metrics["bogus_field"] = 42
        err = validate_metrics_for_profile("hive:node", metrics)
        assert err is not None
        assert "unknown metric" in err

    def test_validate_out_of_range(self):
        metrics = _valid_node_metrics()
        metrics["uptime"] = 1.5  # Max is 1.0
        err = validate_metrics_for_profile("hive:node", metrics)
        assert err is not None
        assert "out of range" in err

    def test_validate_non_numeric(self):
        metrics = _valid_node_metrics()
        metrics["uptime"] = "high"
        err = validate_metrics_for_profile("hive:node", metrics)
        assert err is not None
        assert "must be numeric" in err

    def test_validate_unknown_domain(self):
        err = validate_metrics_for_profile("bogus:domain", {})
        assert err is not None
        assert "unknown domain" in err

    def test_validate_optional_metrics_accepted(self):
        metrics = _valid_node_metrics()
        metrics["capacity_sats"] = 5_000_000
        err = validate_metrics_for_profile("hive:node", metrics)
        assert err is None

    def test_all_valid_domains_in_profiles(self):
        for domain in VALID_DOMAINS:
            assert domain in CREDENTIAL_PROFILES

    def test_validate_nan_metric_rejected(self):
        """NaN values must be rejected (H1 fix)."""
        metrics = _valid_node_metrics()
        metrics["uptime"] = float("nan")
        err = validate_metrics_for_profile("hive:node", metrics)
        assert err is not None
        assert "finite" in err

    def test_validate_inf_metric_rejected(self):
        """Infinity values must be rejected (H1 fix)."""
        metrics = _valid_node_metrics()
        metrics["uptime"] = float("inf")
        err = validate_metrics_for_profile("hive:node", metrics)
        assert err is not None
        assert "finite" in err

    def test_validate_neg_inf_metric_rejected(self):
        metrics = _valid_node_metrics()
        metrics["uptime"] = float("-inf")
        err = validate_metrics_for_profile("hive:node", metrics)
        assert err is not None
        assert "finite" in err


# =============================================================================
# Signing Payload
# =============================================================================

class TestSigningPayload:
    """Test deterministic signing payload generation."""

    def test_deterministic_output(self):
        cred = {
            "issuer_id": ALICE_PUBKEY,
            "subject_id": BOB_PUBKEY,
            "domain": "hive:node",
            "period_start": 1000,
            "period_end": 2000,
            "metrics": {"uptime": 0.99},
            "outcome": "neutral",
        }
        p1 = get_credential_signing_payload(cred)
        p2 = get_credential_signing_payload(cred)
        assert p1 == p2
        # Must be valid JSON
        parsed = json.loads(p1)
        assert parsed["issuer_id"] == ALICE_PUBKEY

    def test_sorted_keys(self):
        cred = {
            "outcome": "neutral",
            "issuer_id": ALICE_PUBKEY,
            "subject_id": BOB_PUBKEY,
            "domain": "hive:node",
            "period_start": 1000,
            "period_end": 2000,
            "metrics": {"b": 2, "a": 1},
        }
        payload = get_credential_signing_payload(cred)
        # Keys should be in alphabetical order
        assert payload.index('"domain"') < payload.index('"issuer_id"')
        assert payload.index('"issuer_id"') < payload.index('"metrics"')


# =============================================================================
# Score and Tier Helpers
# =============================================================================

class TestPubkeyValidation:
    """Test pubkey validation helper (C6 fix)."""

    def test_valid_pubkey_02(self):
        assert _is_valid_pubkey("02" + "ab" * 32) is True

    def test_valid_pubkey_03(self):
        assert _is_valid_pubkey("03" + "cd" * 32) is True

    def test_too_short(self):
        assert _is_valid_pubkey("03" + "ab" * 31) is False

    def test_too_long(self):
        assert _is_valid_pubkey("03" + "ab" * 33) is False

    def test_wrong_prefix(self):
        assert _is_valid_pubkey("04" + "ab" * 32) is False

    def test_non_hex_chars(self):
        assert _is_valid_pubkey("03" + "zz" * 32) is False

    def test_empty_string(self):
        assert _is_valid_pubkey("") is False

    def test_short_string(self):
        assert _is_valid_pubkey("abcdefghij") is False


class TestScoreHelpers:
    """Test score-to-tier conversion and confidence calculation."""

    def test_tier_newcomer(self):
        assert _score_to_tier(0) == "newcomer"
        assert _score_to_tier(59) == "newcomer"

    def test_tier_recognized(self):
        assert _score_to_tier(60) == "recognized"
        assert _score_to_tier(74) == "recognized"

    def test_tier_trusted(self):
        assert _score_to_tier(75) == "trusted"
        assert _score_to_tier(84) == "trusted"

    def test_tier_senior(self):
        assert _score_to_tier(85) == "senior"
        assert _score_to_tier(100) == "senior"

    def test_confidence_low(self):
        assert _compute_confidence(0, 0) == "low"
        assert _compute_confidence(2, 1) == "low"

    def test_confidence_medium(self):
        assert _compute_confidence(3, 2) == "medium"

    def test_confidence_high(self):
        assert _compute_confidence(10, 5) == "high"


# =============================================================================
# Credential Issuance
# =============================================================================

class TestCredentialIssuance:
    """Test credential issuance via DIDCredentialManager."""

    def test_issue_valid_credential(self):
        mgr, db = _make_manager()
        cred = mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
        )
        assert cred is not None
        assert cred.issuer_id == ALICE_PUBKEY
        assert cred.subject_id == BOB_PUBKEY
        assert cred.domain == "hive:node"
        assert cred.signature == "fakesig_zbase32encoded"
        assert cred.credential_id in db.credentials

    def test_issue_self_issuance_rejected(self):
        mgr, db = _make_manager()
        cred = mgr.issue_credential(
            subject_id=ALICE_PUBKEY,  # Same as our_pubkey
            domain="hive:node",
            metrics=_valid_node_metrics(),
        )
        assert cred is None
        assert len(db.credentials) == 0

    def test_issue_invalid_domain(self):
        mgr, db = _make_manager()
        cred = mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="bogus:domain",
            metrics={"foo": 1},
        )
        assert cred is None

    def test_issue_invalid_outcome(self):
        mgr, db = _make_manager()
        cred = mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
            outcome="invalid",
        )
        assert cred is None

    def test_issue_invalid_metrics(self):
        mgr, db = _make_manager()
        cred = mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics={"routing_reliability": 0.5},  # Missing required fields
        )
        assert cred is None

    def test_issue_no_rpc(self):
        mgr, db = _make_manager(with_rpc=False)
        cred = mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
        )
        assert cred is None

    def test_issue_hsm_failure(self):
        mgr, db = _make_manager()
        mgr.rpc.signmessage.side_effect = Exception("HSM error")
        cred = mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
        )
        assert cred is None

    def test_issue_row_cap_enforcement(self):
        mgr, db = _make_manager()
        # Simulate being at cap
        for i in range(MAX_TOTAL_CREDENTIALS):
            db.credentials[f"cred-{i}"] = {"subject_id": f"03{i:064x}"}
        cred = mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
        )
        assert cred is None

    def test_issue_per_peer_cap_enforcement(self):
        mgr, db = _make_manager()
        for i in range(MAX_CREDENTIALS_PER_PEER):
            db.credentials[f"cred-{i}"] = {"subject_id": BOB_PUBKEY}
        cred = mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
        )
        assert cred is None

    def test_issue_with_evidence(self):
        mgr, db = _make_manager()
        evidence = [{"type": "routing_receipt", "hash": "abc123"}]
        cred = mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
            evidence=evidence,
        )
        assert cred is not None
        assert cred.evidence == evidence

    def test_issue_with_custom_period(self):
        mgr, db = _make_manager()
        now = int(time.time())
        cred = mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
            period_start=now - 86400,
            period_end=now,
        )
        assert cred is not None
        assert cred.period_start == now - 86400
        assert cred.period_end == now

    def test_issue_bad_period_order(self):
        """period_end must be after period_start (H2 fix)."""
        mgr, db = _make_manager()
        now = int(time.time())
        cred = mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
            period_start=now,
            period_end=now - 86400,
        )
        assert cred is None

    def test_issue_equal_period(self):
        """period_end == period_start should be rejected."""
        mgr, db = _make_manager()
        now = int(time.time())
        cred = mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
            period_start=now,
            period_end=now,
        )
        assert cred is None

    def test_issue_renew_outcome(self):
        mgr, db = _make_manager()
        cred = mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
            outcome="renew",
        )
        assert cred is not None
        assert cred.outcome == "renew"


# =============================================================================
# Credential Verification
# =============================================================================

class TestCredentialVerification:
    """Test credential verification logic."""

    def _make_valid_credential(self):
        now = int(time.time())
        return {
            "issuer_id": ALICE_PUBKEY,
            "subject_id": BOB_PUBKEY,
            "domain": "hive:node",
            "period_start": now - 86400,
            "period_end": now,
            "metrics": _valid_node_metrics(),
            "outcome": "neutral",
            "signature": "valid_sig",
        }

    def test_verify_valid_credential(self):
        mgr, _ = _make_manager()
        cred = self._make_valid_credential()
        is_valid, reason = mgr.verify_credential(cred)
        assert is_valid is True
        assert reason == "valid"

    def test_verify_self_issuance_rejected(self):
        mgr, _ = _make_manager()
        cred = self._make_valid_credential()
        cred["subject_id"] = cred["issuer_id"]
        is_valid, reason = mgr.verify_credential(cred)
        assert is_valid is False
        assert "self-issuance" in reason

    def test_verify_missing_field(self):
        mgr, _ = _make_manager()
        cred = self._make_valid_credential()
        del cred["signature"]
        is_valid, reason = mgr.verify_credential(cred)
        assert is_valid is False
        assert "missing field" in reason

    def test_verify_invalid_domain(self):
        mgr, _ = _make_manager()
        cred = self._make_valid_credential()
        cred["domain"] = "bogus"
        is_valid, reason = mgr.verify_credential(cred)
        assert is_valid is False
        assert "invalid domain" in reason

    def test_verify_expired(self):
        mgr, _ = _make_manager()
        cred = self._make_valid_credential()
        cred["expires_at"] = int(time.time()) - 3600
        is_valid, reason = mgr.verify_credential(cred)
        assert is_valid is False
        assert "expired" in reason

    def test_verify_revoked(self):
        mgr, _ = _make_manager()
        cred = self._make_valid_credential()
        cred["revoked_at"] = int(time.time())
        is_valid, reason = mgr.verify_credential(cred)
        assert is_valid is False
        assert "revoked" in reason

    def test_verify_bad_period(self):
        mgr, _ = _make_manager()
        cred = self._make_valid_credential()
        cred["period_end"] = cred["period_start"] - 1
        is_valid, reason = mgr.verify_credential(cred)
        assert is_valid is False
        assert "period_end" in reason

    def test_verify_signature_failure(self):
        mgr, _ = _make_manager()
        mgr.rpc.checkmessage.return_value = {"verified": False}
        cred = self._make_valid_credential()
        is_valid, reason = mgr.verify_credential(cred)
        assert is_valid is False
        assert "verification failed" in reason

    def test_verify_invalid_pubkey_format(self):
        """Pubkeys must be 66-char hex with 02/03 prefix (C6 fix)."""
        mgr, _ = _make_manager()
        cred = self._make_valid_credential()
        cred["issuer_id"] = "not_a_valid_pubkey_string"
        is_valid, reason = mgr.verify_credential(cred)
        assert is_valid is False
        assert "invalid issuer_id" in reason

    def test_verify_invalid_subject_pubkey(self):
        mgr, _ = _make_manager()
        cred = self._make_valid_credential()
        cred["subject_id"] = "04" + "ab" * 32  # Wrong prefix
        is_valid, reason = mgr.verify_credential(cred)
        assert is_valid is False
        assert "invalid subject_id" in reason

    def test_verify_pubkey_mismatch(self):
        mgr, _ = _make_manager()
        mgr.rpc.checkmessage.return_value = {"verified": True, "pubkey": CHARLIE_PUBKEY}
        cred = self._make_valid_credential()
        is_valid, reason = mgr.verify_credential(cred)
        assert is_valid is False
        assert "pubkey" in reason

    def test_verify_no_rpc_fails_closed(self):
        """Without RPC, verification must fail-closed (C1 fix)."""
        mgr, _ = _make_manager(with_rpc=False)
        cred = self._make_valid_credential()
        is_valid, reason = mgr.verify_credential(cred)
        assert is_valid is False
        assert "no RPC" in reason


# =============================================================================
# Credential Revocation
# =============================================================================

class TestCredentialRevocation:
    """Test credential revocation."""

    def test_revoke_own_credential(self):
        mgr, db = _make_manager()
        cred = mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
        )
        assert cred is not None
        success = mgr.revoke_credential(cred.credential_id, "peer went offline")
        assert success is True
        stored = db.credentials[cred.credential_id]
        assert stored["revoked_at"] is not None
        assert stored["revocation_reason"] == "peer went offline"

    def test_revoke_not_issuer(self):
        mgr, db = _make_manager(our_pubkey=CHARLIE_PUBKEY)
        # Store a credential issued by someone else
        db.credentials["other-cred"] = {
            "credential_id": "other-cred",
            "issuer_id": ALICE_PUBKEY,
            "subject_id": BOB_PUBKEY,
            "revoked_at": None,
        }
        success = mgr.revoke_credential("other-cred", "reason")
        assert success is False

    def test_revoke_already_revoked(self):
        mgr, db = _make_manager()
        cred = mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
        )
        mgr.revoke_credential(cred.credential_id, "first revoke")
        success = mgr.revoke_credential(cred.credential_id, "second revoke")
        assert success is False

    def test_revoke_nonexistent(self):
        mgr, db = _make_manager()
        success = mgr.revoke_credential("nonexistent-id", "reason")
        assert success is False

    def test_revoke_empty_reason(self):
        mgr, db = _make_manager()
        cred = mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
        )
        success = mgr.revoke_credential(cred.credential_id, "")
        assert success is False

    def test_revoke_reason_too_long(self):
        mgr, db = _make_manager()
        cred = mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
        )
        success = mgr.revoke_credential(cred.credential_id, "x" * 501)
        assert success is False


# =============================================================================
# Reputation Aggregation
# =============================================================================

class TestReputationAggregation:
    """Test weighted reputation aggregation."""

    def test_aggregate_single_credential(self):
        mgr, db = _make_manager()
        mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
        )
        result = mgr.aggregate_reputation(BOB_PUBKEY, domain="hive:node")
        assert result is not None
        assert isinstance(result.score, int)
        assert 0 <= result.score <= 100
        assert result.tier in ("newcomer", "recognized", "trusted", "senior")
        assert result.credential_count == 1
        assert result.issuer_count == 1

    def test_aggregate_no_credentials(self):
        mgr, db = _make_manager()
        result = mgr.aggregate_reputation(BOB_PUBKEY)
        assert result is None

    def test_aggregate_cross_domain(self):
        mgr, db = _make_manager()
        mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
        )
        # Cross-domain aggregation (domain=None)
        result = mgr.aggregate_reputation(BOB_PUBKEY, domain=None)
        assert result is not None
        assert result.domain == "_all"

    def test_aggregate_revoked_excluded(self):
        mgr, db = _make_manager()
        cred = mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
        )
        mgr.revoke_credential(cred.credential_id, "revoked")
        result = mgr.aggregate_reputation(BOB_PUBKEY, domain="hive:node")
        assert result is None  # All credentials revoked

    def test_aggregate_caching(self):
        mgr, db = _make_manager()
        mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
        )
        r1 = mgr.aggregate_reputation(BOB_PUBKEY, domain="hive:node")
        r2 = mgr.aggregate_reputation(BOB_PUBKEY, domain="hive:node")
        # Second call should return cached result
        assert r1.computed_at == r2.computed_at

    def test_aggregate_cache_invalidated_on_issue(self):
        mgr, db = _make_manager()
        mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
        )
        r1 = mgr.aggregate_reputation(BOB_PUBKEY, domain="hive:node")

        # Issue another credential — cache should be invalidated
        mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
            outcome="renew",
        )
        r2 = mgr.aggregate_reputation(BOB_PUBKEY, domain="hive:node")
        assert r2.credential_count == 2

    def test_aggregate_renew_boosts_score(self):
        mgr, db = _make_manager()
        # Issue neutral
        mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
            outcome="neutral",
        )
        r_neutral = mgr.aggregate_reputation(BOB_PUBKEY, domain="hive:node")

        # Clear and issue renew
        db.credentials.clear()
        mgr._aggregation_cache.clear()
        mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
            outcome="renew",
        )
        r_renew = mgr.aggregate_reputation(BOB_PUBKEY, domain="hive:node")
        assert r_renew.score >= r_neutral.score

    def test_get_credit_tier_default(self):
        mgr, db = _make_manager()
        tier = mgr.get_credit_tier(BOB_PUBKEY)
        assert tier == "newcomer"

    def test_get_credit_tier_with_credentials(self):
        mgr, db = _make_manager()
        mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
        )
        tier = mgr.get_credit_tier(BOB_PUBKEY)
        assert tier in ("newcomer", "recognized", "trusted", "senior")

    def test_aggregate_persists_to_db_cache(self):
        mgr, db = _make_manager()
        mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
        )
        result = mgr.aggregate_reputation(BOB_PUBKEY, domain="hive:node")
        assert result is not None
        # Check DB cache was populated
        cached = db.get_did_reputation_cache(BOB_PUBKEY, "hive:node")
        assert cached is not None
        assert cached["score"] == result.score
        assert cached["tier"] == result.tier


# =============================================================================
# Incoming Credential Handling
# =============================================================================

class TestHandleCredentialPresent:
    """Test handling of incoming credential present messages."""

    def _make_credential_payload(self, issuer=BOB_PUBKEY, subject=CHARLIE_PUBKEY):
        now = int(time.time())
        return {
            "sender_id": BOB_PUBKEY,
            "event_id": str(uuid.uuid4()),
            "timestamp": now,
            "credential": {
                "credential_id": str(uuid.uuid4()),
                "issuer_id": issuer,
                "subject_id": subject,
                "domain": "hive:node",
                "period_start": now - 86400,
                "period_end": now,
                "metrics": _valid_node_metrics(),
                "outcome": "neutral",
                "signature": "valid_sig",
                "issued_at": now,
            },
        }

    def test_handle_valid_credential(self):
        mgr, db = _make_manager()
        # Make checkmessage return the issuer's pubkey (BOB_PUBKEY)
        mgr.rpc.checkmessage.return_value = {"verified": True, "pubkey": BOB_PUBKEY}
        payload = self._make_credential_payload()
        result = mgr.handle_credential_present(BOB_PUBKEY, payload)
        assert result is True
        assert len(db.credentials) == 1

    def test_handle_duplicate_idempotent(self):
        mgr, db = _make_manager()
        mgr.rpc.checkmessage.return_value = {"verified": True, "pubkey": BOB_PUBKEY}
        payload = self._make_credential_payload()
        mgr.handle_credential_present(BOB_PUBKEY, payload)
        result = mgr.handle_credential_present(BOB_PUBKEY, payload)
        assert result is True  # Idempotent
        assert len(db.credentials) == 1

    def test_handle_invalid_payload(self):
        mgr, db = _make_manager()
        result = mgr.handle_credential_present(BOB_PUBKEY, {"bogus": True})
        assert result is False

    def test_handle_self_issuance_in_credential(self):
        mgr, db = _make_manager()
        payload = self._make_credential_payload(issuer=BOB_PUBKEY, subject=BOB_PUBKEY)
        result = mgr.handle_credential_present(BOB_PUBKEY, payload)
        assert result is False

    def test_handle_missing_credential_id(self):
        """credential_id must be present — reject if missing (M2 fix)."""
        mgr, db = _make_manager()
        mgr.rpc.checkmessage.return_value = {"verified": True, "pubkey": BOB_PUBKEY}
        payload = self._make_credential_payload()
        # Remove credential_id from the credential dict
        del payload["credential"]["credential_id"]
        result = mgr.handle_credential_present(BOB_PUBKEY, payload)
        assert result is False

    def test_handle_at_row_cap(self):
        mgr, db = _make_manager()
        for i in range(MAX_TOTAL_CREDENTIALS):
            db.credentials[f"cred-{i}"] = {"subject_id": f"03{i:064x}"}
        payload = self._make_credential_payload()
        result = mgr.handle_credential_present(BOB_PUBKEY, payload)
        assert result is False


# =============================================================================
# Incoming Credential Revocation
# =============================================================================

class TestHandleCredentialRevoke:
    """Test handling of incoming revocation messages."""

    def test_handle_valid_revocation(self):
        mgr, db = _make_manager()
        # First, store a credential
        cred_id = str(uuid.uuid4())
        db.credentials[cred_id] = {
            "credential_id": cred_id,
            "issuer_id": BOB_PUBKEY,
            "subject_id": CHARLIE_PUBKEY,
            "domain": "hive:node",
            "revoked_at": None,
        }
        mgr.rpc.checkmessage.return_value = {"verified": True, "pubkey": BOB_PUBKEY}

        payload = {
            "credential_id": cred_id,
            "issuer_id": BOB_PUBKEY,
            "reason": "peer went offline",
            "signature": "valid_revoke_sig",
        }
        result = mgr.handle_credential_revoke(BOB_PUBKEY, payload)
        assert result is True
        assert db.credentials[cred_id]["revoked_at"] is not None

    def test_handle_revoke_issuer_mismatch(self):
        mgr, db = _make_manager()
        cred_id = str(uuid.uuid4())
        db.credentials[cred_id] = {
            "credential_id": cred_id,
            "issuer_id": ALICE_PUBKEY,
            "subject_id": BOB_PUBKEY,
            "revoked_at": None,
        }
        payload = {
            "credential_id": cred_id,
            "issuer_id": CHARLIE_PUBKEY,  # Not the issuer
            "reason": "bogus",
            "signature": "sig",
        }
        result = mgr.handle_credential_revoke(BOB_PUBKEY, payload)
        assert result is False

    def test_handle_revoke_empty_signature_rejected(self):
        """Empty signature must be rejected (C2 fix)."""
        mgr, db = _make_manager()
        cred_id = str(uuid.uuid4())
        db.credentials[cred_id] = {
            "credential_id": cred_id,
            "issuer_id": BOB_PUBKEY,
            "subject_id": CHARLIE_PUBKEY,
            "domain": "hive:node",
            "revoked_at": None,
        }
        payload = {
            "credential_id": cred_id,
            "issuer_id": BOB_PUBKEY,
            "reason": "offline",
            "signature": "",  # Empty — should be rejected
        }
        result = mgr.handle_credential_revoke(BOB_PUBKEY, payload)
        assert result is False

    def test_handle_revoke_no_rpc_rejected(self):
        """Revocation without RPC must be rejected (fail-closed)."""
        mgr, db = _make_manager(with_rpc=False)
        cred_id = str(uuid.uuid4())
        db.credentials[cred_id] = {
            "credential_id": cred_id,
            "issuer_id": BOB_PUBKEY,
            "subject_id": CHARLIE_PUBKEY,
            "domain": "hive:node",
            "revoked_at": None,
        }
        payload = {
            "credential_id": cred_id,
            "issuer_id": BOB_PUBKEY,
            "reason": "offline",
            "signature": "some_sig",
        }
        result = mgr.handle_credential_revoke(BOB_PUBKEY, payload)
        assert result is False

    def test_handle_revoke_already_revoked_idempotent(self):
        mgr, db = _make_manager()
        cred_id = str(uuid.uuid4())
        db.credentials[cred_id] = {
            "credential_id": cred_id,
            "issuer_id": BOB_PUBKEY,
            "subject_id": CHARLIE_PUBKEY,
            "revoked_at": int(time.time()),  # Already revoked
        }
        payload = {
            "credential_id": cred_id,
            "issuer_id": BOB_PUBKEY,
            "reason": "reason",
            "signature": "sig",
        }
        result = mgr.handle_credential_revoke(BOB_PUBKEY, payload)
        assert result is True  # Idempotent


# =============================================================================
# Maintenance
# =============================================================================

class TestMaintenance:
    """Test cleanup and cache refresh."""

    def test_cleanup_expired(self):
        mgr, db = _make_manager()
        now = int(time.time())
        # Add an expired credential
        db.credentials["expired-1"] = {
            "credential_id": "expired-1",
            "issuer_id": ALICE_PUBKEY,
            "subject_id": BOB_PUBKEY,
            "expires_at": now - 3600,
        }
        # Add a non-expired credential
        db.credentials["valid-1"] = {
            "credential_id": "valid-1",
            "issuer_id": ALICE_PUBKEY,
            "subject_id": BOB_PUBKEY,
            "expires_at": now + 3600,
        }
        count = mgr.cleanup_expired()
        assert count == 1
        assert "expired-1" not in db.credentials
        assert "valid-1" in db.credentials

    def test_get_credentials_for_relay(self):
        mgr, db = _make_manager()
        mgr.issue_credential(
            subject_id=BOB_PUBKEY,
            domain="hive:node",
            metrics=_valid_node_metrics(),
        )
        creds = mgr.get_credentials_for_relay()
        assert len(creds) == 1
        assert creds[0]["issuer_id"] == ALICE_PUBKEY


# =============================================================================
# Protocol Messages
# =============================================================================

class TestProtocolMessages:
    """Test DID protocol message creation and validation."""

    def test_message_types_defined(self):
        assert HiveMessageType.DID_CREDENTIAL_PRESENT == 32883
        assert HiveMessageType.DID_CREDENTIAL_REVOKE == 32885

    def test_create_credential_present(self):
        now = int(time.time())
        cred = {
            "credential_id": str(uuid.uuid4()),
            "issuer_id": ALICE_PUBKEY,
            "subject_id": BOB_PUBKEY,
            "domain": "hive:node",
            "period_start": now - 86400,
            "period_end": now,
            "metrics": _valid_node_metrics(),
            "outcome": "neutral",
            "signature": "sig123",
        }
        msg = create_did_credential_present(ALICE_PUBKEY, cred, timestamp=now)
        assert msg is not None
        assert isinstance(msg, bytes)

    def test_validate_credential_present_valid(self):
        now = int(time.time())
        payload = {
            "sender_id": ALICE_PUBKEY,
            "event_id": str(uuid.uuid4()),
            "timestamp": now,
            "credential": {
                "credential_id": str(uuid.uuid4()),
                "issuer_id": ALICE_PUBKEY,
                "subject_id": BOB_PUBKEY,
                "domain": "hive:node",
                "period_start": now - 86400,
                "period_end": now,
                "metrics": _valid_node_metrics(),
                "outcome": "neutral",
                "signature": "sig1234567890",
            },
        }
        assert validate_did_credential_present(payload) is True

    def test_validate_credential_present_self_issuance(self):
        now = int(time.time())
        payload = {
            "sender_id": ALICE_PUBKEY,
            "event_id": str(uuid.uuid4()),
            "timestamp": now,
            "credential": {
                "credential_id": str(uuid.uuid4()),
                "issuer_id": ALICE_PUBKEY,
                "subject_id": ALICE_PUBKEY,  # Self-issuance
                "domain": "hive:node",
                "period_start": now - 86400,
                "period_end": now,
                "metrics": _valid_node_metrics(),
                "outcome": "neutral",
                "signature": "sig",
            },
        }
        assert validate_did_credential_present(payload) is False

    def test_validate_credential_present_bad_domain(self):
        now = int(time.time())
        payload = {
            "sender_id": ALICE_PUBKEY,
            "event_id": str(uuid.uuid4()),
            "timestamp": now,
            "credential": {
                "issuer_id": ALICE_PUBKEY,
                "subject_id": BOB_PUBKEY,
                "domain": "bogus",
                "period_start": now - 86400,
                "period_end": now,
                "metrics": {},
                "outcome": "neutral",
                "signature": "sig",
            },
        }
        assert validate_did_credential_present(payload) is False

    def test_validate_credential_present_missing_credential(self):
        payload = {
            "sender_id": ALICE_PUBKEY,
            "event_id": str(uuid.uuid4()),
            "timestamp": int(time.time()),
        }
        assert validate_did_credential_present(payload) is False

    def test_create_credential_revoke(self):
        msg = create_did_credential_revoke(
            sender_id=ALICE_PUBKEY,
            credential_id=str(uuid.uuid4()),
            issuer_id=ALICE_PUBKEY,
            reason="peer offline",
            signature="revoke_sig",
        )
        assert msg is not None
        assert isinstance(msg, bytes)

    def test_validate_credential_revoke_valid(self):
        payload = {
            "sender_id": ALICE_PUBKEY,
            "event_id": str(uuid.uuid4()),
            "timestamp": int(time.time()),
            "credential_id": str(uuid.uuid4()),
            "issuer_id": ALICE_PUBKEY,
            "reason": "peer offline",
            "signature": "revoke_sig",
        }
        assert validate_did_credential_revoke(payload) is True

    def test_validate_credential_revoke_empty_reason(self):
        payload = {
            "sender_id": ALICE_PUBKEY,
            "event_id": str(uuid.uuid4()),
            "timestamp": int(time.time()),
            "credential_id": str(uuid.uuid4()),
            "issuer_id": ALICE_PUBKEY,
            "reason": "",  # Empty
            "signature": "sig",
        }
        assert validate_did_credential_revoke(payload) is False

    def test_validate_credential_revoke_reason_too_long(self):
        payload = {
            "sender_id": ALICE_PUBKEY,
            "event_id": str(uuid.uuid4()),
            "timestamp": int(time.time()),
            "credential_id": str(uuid.uuid4()),
            "issuer_id": ALICE_PUBKEY,
            "reason": "x" * 501,
            "signature": "sig",
        }
        assert validate_did_credential_revoke(payload) is False

    def test_signing_payload_deterministic(self):
        now = int(time.time())
        payload = {
            "credential": {
                "issuer_id": ALICE_PUBKEY,
                "subject_id": BOB_PUBKEY,
                "domain": "hive:node",
                "period_start": now - 86400,
                "period_end": now,
                "metrics": {"a": 1, "b": 2},
                "outcome": "neutral",
            },
        }
        p1 = get_did_credential_present_signing_payload(payload)
        p2 = get_did_credential_present_signing_payload(payload)
        assert p1 == p2
        assert '"domain"' in p1

    def test_revoke_signing_payload(self):
        cred_id = str(uuid.uuid4())
        p1 = get_did_credential_revoke_signing_payload(cred_id, "reason")
        p2 = get_did_credential_revoke_signing_payload(cred_id, "reason")
        assert p1 == p2
        parsed = json.loads(p1)
        assert parsed["action"] == "revoke"
        assert parsed["credential_id"] == cred_id
