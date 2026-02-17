"""
Tests for Phase 3: DID Credential Exchange Protocol.

Tests cover:
- Management credential protocol messages (create/validate/signing payload)
- Management credential gossip handlers (present/revoke)
- Auto-issue node credentials from peer state data
- Rebroadcast own credentials to fleet
- Planner reputation integration
- Membership reputation integration
- Settlement reputation metadata
- Idempotency entries for MGMT messages
"""

import json
import time
import uuid
import pytest
from unittest.mock import MagicMock, patch, call
from dataclasses import dataclass

from modules.protocol import (
    HiveMessageType,
    RELIABLE_MESSAGE_TYPES,
    # MGMT credential protocol functions
    create_mgmt_credential_present,
    validate_mgmt_credential_present,
    get_mgmt_credential_present_signing_payload,
    create_mgmt_credential_revoke,
    validate_mgmt_credential_revoke,
    get_mgmt_credential_revoke_signing_payload,
    # Existing DID functions for rebroadcast tests
    create_did_credential_present,
    VALID_MGMT_TIERS,
    MAX_MGMT_ALLOWED_SCHEMAS_LEN,
    MAX_MGMT_CONSTRAINTS_LEN,
    MAX_REVOCATION_REASON_LEN,
)

from modules.idempotency import EVENT_ID_FIELDS, generate_event_id

from modules.management_schemas import (
    ManagementSchemaRegistry,
    ManagementCredential,
    MAX_MANAGEMENT_CREDENTIALS,
)

from modules.did_credentials import (
    DIDCredentialManager,
    CREDENTIAL_PROFILES,
)


# =============================================================================
# Test helpers
# =============================================================================

ALICE_PUBKEY = "03" + "a1" * 32  # 66 hex chars
BOB_PUBKEY = "03" + "b2" * 32
CHARLIE_PUBKEY = "03" + "c3" * 32
DAVE_PUBKEY = "03" + "d4" * 32


def _make_mgmt_credential_dict(**overrides):
    """Create a valid management credential dict for protocol testing."""
    cred = {
        "credential_id": str(uuid.uuid4()),
        "issuer_id": ALICE_PUBKEY,
        "agent_id": BOB_PUBKEY,
        "node_id": CHARLIE_PUBKEY,
        "tier": "standard",
        "allowed_schemas": ["hive:fee-policy/*", "hive:monitor/*"],
        "constraints": {"max_fee_change_pct": 20},
        "valid_from": int(time.time()) - 86400,
        "valid_until": int(time.time()) + 86400 * 90,
        "signature": "zbase32signature",
    }
    cred.update(overrides)
    return cred


def _make_mgmt_present_payload(**cred_overrides):
    """Create a valid MGMT_CREDENTIAL_PRESENT payload."""
    return {
        "sender_id": ALICE_PUBKEY,
        "event_id": str(uuid.uuid4()),
        "timestamp": int(time.time()),
        "credential": _make_mgmt_credential_dict(**cred_overrides),
    }


class MockDatabase:
    """Mock database for management credential tests."""

    def __init__(self):
        self.mgmt_credentials = {}
        self.mgmt_credential_count = 0

    def store_management_credential(self, credential_id, issuer_id, agent_id,
                                     node_id, tier, allowed_schemas_json,
                                     constraints_json, valid_from, valid_until,
                                     signature):
        if self.mgmt_credential_count >= MAX_MANAGEMENT_CREDENTIALS:
            return False
        self.mgmt_credentials[credential_id] = {
            "credential_id": credential_id,
            "issuer_id": issuer_id,
            "agent_id": agent_id,
            "node_id": node_id,
            "tier": tier,
            "allowed_schemas_json": allowed_schemas_json,
            "constraints_json": constraints_json,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "signature": signature,
            "revoked_at": None,
        }
        self.mgmt_credential_count += 1
        return True

    def get_management_credential(self, credential_id):
        return self.mgmt_credentials.get(credential_id)

    def count_management_credentials(self):
        return self.mgmt_credential_count

    def revoke_management_credential(self, credential_id, timestamp):
        cred = self.mgmt_credentials.get(credential_id)
        if cred:
            cred["revoked_at"] = timestamp
            return True
        return False

    def get_management_credentials(self, agent_id=None, node_id=None):
        return list(self.mgmt_credentials.values())


class MockDIDDatabase(MockDatabase):
    """Extended mock for DID credential auto-issue tests."""

    def __init__(self):
        super().__init__()
        self.did_credentials = {}
        self.did_credential_count = 0
        self.members = {}
        self.reputation_cache = {}

    def store_did_credential(self, credential_id, issuer_id, subject_id, domain,
                              period_start, period_end, metrics_json, outcome,
                              evidence_json, signature, issued_at, expires_at,
                              received_from):
        self.did_credentials[credential_id] = {
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
            "received_from": received_from,
        }
        self.did_credential_count += 1
        return True

    def get_did_credential(self, credential_id):
        return self.did_credentials.get(credential_id)

    def get_did_credentials_for_subject(self, subject_id, domain=None, limit=100):
        results = []
        for c in self.did_credentials.values():
            if c["subject_id"] == subject_id:
                if domain and c["domain"] != domain:
                    continue
                results.append(c)
        return results[:limit]

    def get_did_credentials_by_issuer(self, issuer_id, subject_id=None, limit=100):
        results = []
        for c in self.did_credentials.values():
            if c["issuer_id"] == issuer_id:
                if subject_id and c["subject_id"] != subject_id:
                    continue
                results.append(c)
        return sorted(results, key=lambda x: x.get("issued_at", 0), reverse=True)[:limit]

    def count_did_credentials(self):
        return self.did_credential_count

    def count_did_credentials_for_subject(self, subject_id):
        return sum(1 for c in self.did_credentials.values()
                   if c["subject_id"] == subject_id)

    def get_all_members(self):
        return list(self.members.values())

    def get_member(self, peer_id):
        return self.members.get(peer_id)

    def store_did_reputation_cache(self, subject_id, domain, score, tier,
                                    confidence, credential_count, issuer_count,
                                    components_json):
        self.reputation_cache[(subject_id, domain)] = {
            "subject_id": subject_id, "domain": domain, "score": score,
            "tier": tier, "confidence": confidence,
            "credential_count": credential_count, "issuer_count": issuer_count,
            "components_json": components_json,
            "computed_at": int(time.time()),
        }
        return True

    def get_did_reputation_cache(self, subject_id, domain=None):
        return self.reputation_cache.get((subject_id, domain or "_all"))

    def get_stale_did_reputation_cache(self, before_ts, limit=50):
        return []

    def cleanup_expired_did_credentials(self, before_ts):
        return 0

    def revoke_did_credential(self, credential_id, reason, timestamp):
        cred = self.did_credentials.get(credential_id)
        if cred:
            cred["revoked_at"] = timestamp
            cred["revocation_reason"] = reason
            return True
        return False


# =============================================================================
# Test MGMT credential protocol messages
# =============================================================================

class TestMgmtProtocolMessages:
    """Tests for MGMT_CREDENTIAL_PRESENT/REVOKE protocol functions."""

    def test_message_types_defined(self):
        assert HiveMessageType.MGMT_CREDENTIAL_PRESENT == 32887
        assert HiveMessageType.MGMT_CREDENTIAL_REVOKE == 32889

    def test_reliable_delivery(self):
        assert HiveMessageType.MGMT_CREDENTIAL_PRESENT in RELIABLE_MESSAGE_TYPES
        assert HiveMessageType.MGMT_CREDENTIAL_REVOKE in RELIABLE_MESSAGE_TYPES

    def test_valid_tiers(self):
        assert VALID_MGMT_TIERS == frozenset(["monitor", "standard", "advanced", "admin"])

    # --- create_mgmt_credential_present ---

    def test_create_present(self):
        cred = _make_mgmt_credential_dict()
        msg = create_mgmt_credential_present(
            sender_id=ALICE_PUBKEY,
            credential=cred,
            event_id="test-event",
            timestamp=1000,
        )
        assert isinstance(msg, bytes)
        assert len(msg) > 0

    def test_create_present_auto_fills(self):
        """Auto-generates event_id and timestamp if not provided."""
        cred = _make_mgmt_credential_dict()
        msg = create_mgmt_credential_present(sender_id=ALICE_PUBKEY, credential=cred)
        assert isinstance(msg, bytes)

    # --- validate_mgmt_credential_present ---

    def test_validate_present_valid(self):
        payload = _make_mgmt_present_payload()
        assert validate_mgmt_credential_present(payload) is True

    def test_validate_present_missing_sender(self):
        payload = _make_mgmt_present_payload()
        del payload["sender_id"]
        assert validate_mgmt_credential_present(payload) is False

    def test_validate_present_bad_sender(self):
        payload = _make_mgmt_present_payload()
        payload["sender_id"] = "not-a-pubkey"
        assert validate_mgmt_credential_present(payload) is False

    def test_validate_present_missing_event_id(self):
        payload = _make_mgmt_present_payload()
        del payload["event_id"]
        assert validate_mgmt_credential_present(payload) is False

    def test_validate_present_bad_timestamp(self):
        payload = _make_mgmt_present_payload()
        payload["timestamp"] = -1
        assert validate_mgmt_credential_present(payload) is False

    def test_validate_present_missing_credential(self):
        payload = _make_mgmt_present_payload()
        del payload["credential"]
        assert validate_mgmt_credential_present(payload) is False

    def test_validate_present_bad_credential_id(self):
        payload = _make_mgmt_present_payload()
        payload["credential"]["credential_id"] = ""
        assert validate_mgmt_credential_present(payload) is False

    def test_validate_present_long_credential_id(self):
        payload = _make_mgmt_present_payload()
        payload["credential"]["credential_id"] = "x" * 65
        assert validate_mgmt_credential_present(payload) is False

    def test_validate_present_bad_issuer(self):
        payload = _make_mgmt_present_payload()
        payload["credential"]["issuer_id"] = "bad"
        assert validate_mgmt_credential_present(payload) is False

    def test_validate_present_bad_agent(self):
        payload = _make_mgmt_present_payload()
        payload["credential"]["agent_id"] = "bad"
        assert validate_mgmt_credential_present(payload) is False

    def test_validate_present_bad_node(self):
        payload = _make_mgmt_present_payload()
        payload["credential"]["node_id"] = "bad"
        assert validate_mgmt_credential_present(payload) is False

    def test_validate_present_bad_tier(self):
        payload = _make_mgmt_present_payload()
        payload["credential"]["tier"] = "superadmin"
        assert validate_mgmt_credential_present(payload) is False

    def test_validate_present_bad_schemas_type(self):
        payload = _make_mgmt_present_payload()
        payload["credential"]["allowed_schemas"] = "not-a-list"
        assert validate_mgmt_credential_present(payload) is False

    def test_validate_present_empty_schema_entry(self):
        payload = _make_mgmt_present_payload()
        payload["credential"]["allowed_schemas"] = ["hive:fee-policy/*", ""]
        assert validate_mgmt_credential_present(payload) is False

    def test_validate_present_oversized_schemas(self):
        payload = _make_mgmt_present_payload()
        payload["credential"]["allowed_schemas"] = ["x" * 100] * 50  # Large
        assert validate_mgmt_credential_present(payload) is False

    def test_validate_present_oversized_constraints(self):
        payload = _make_mgmt_present_payload()
        payload["credential"]["constraints"] = {"key": "x" * 5000}
        assert validate_mgmt_credential_present(payload) is False

    def test_validate_present_bad_validity(self):
        payload = _make_mgmt_present_payload()
        payload["credential"]["valid_until"] = payload["credential"]["valid_from"]
        assert validate_mgmt_credential_present(payload) is False

    def test_validate_present_missing_signature(self):
        payload = _make_mgmt_present_payload()
        payload["credential"]["signature"] = ""
        assert validate_mgmt_credential_present(payload) is False

    def test_validate_present_missing_required_field(self):
        for field in ["credential_id", "issuer_id", "agent_id", "node_id",
                      "tier", "allowed_schemas", "constraints",
                      "valid_from", "valid_until", "signature"]:
            payload = _make_mgmt_present_payload()
            del payload["credential"][field]
            assert validate_mgmt_credential_present(payload) is False, f"Missing {field} should fail"

    # --- signing payload ---

    def test_signing_payload_deterministic(self):
        payload = _make_mgmt_present_payload()
        p1 = get_mgmt_credential_present_signing_payload(payload)
        p2 = get_mgmt_credential_present_signing_payload(payload)
        assert p1 == p2

    def test_signing_payload_sorted_keys(self):
        payload = _make_mgmt_present_payload()
        sp = get_mgmt_credential_present_signing_payload(payload)
        parsed = json.loads(sp)
        assert list(parsed.keys()) == sorted(parsed.keys())

    def test_signing_payload_includes_all_fields(self):
        payload = _make_mgmt_present_payload()
        sp = get_mgmt_credential_present_signing_payload(payload)
        parsed = json.loads(sp)
        for field in ["credential_id", "issuer_id", "agent_id", "node_id",
                      "tier", "allowed_schemas", "constraints",
                      "valid_from", "valid_until"]:
            assert field in parsed

    # --- create/validate mgmt_credential_revoke ---

    def test_create_revoke(self):
        msg = create_mgmt_credential_revoke(
            sender_id=ALICE_PUBKEY,
            credential_id="test-cred-id",
            issuer_id=ALICE_PUBKEY,
            reason="expired",
            signature="zbase32sig",
            event_id="test-event",
            timestamp=1000,
        )
        assert isinstance(msg, bytes)

    def test_validate_revoke_valid(self):
        payload = {
            "sender_id": ALICE_PUBKEY,
            "event_id": str(uuid.uuid4()),
            "timestamp": int(time.time()),
            "credential_id": "test-cred-id",
            "issuer_id": ALICE_PUBKEY,
            "reason": "no longer needed",
            "signature": "zbase32sig",
        }
        assert validate_mgmt_credential_revoke(payload) is True

    def test_validate_revoke_missing_reason(self):
        payload = {
            "sender_id": ALICE_PUBKEY,
            "event_id": str(uuid.uuid4()),
            "timestamp": int(time.time()),
            "credential_id": "test-cred-id",
            "issuer_id": ALICE_PUBKEY,
            "reason": "",
            "signature": "zbase32sig",
        }
        assert validate_mgmt_credential_revoke(payload) is False

    def test_validate_revoke_long_reason(self):
        payload = {
            "sender_id": ALICE_PUBKEY,
            "event_id": str(uuid.uuid4()),
            "timestamp": int(time.time()),
            "credential_id": "test-cred-id",
            "issuer_id": ALICE_PUBKEY,
            "reason": "x" * (MAX_REVOCATION_REASON_LEN + 1),
            "signature": "zbase32sig",
        }
        assert validate_mgmt_credential_revoke(payload) is False

    def test_revoke_signing_payload(self):
        sp = get_mgmt_credential_revoke_signing_payload("cred-id", "test reason")
        parsed = json.loads(sp)
        assert parsed["credential_id"] == "cred-id"
        assert parsed["action"] == "mgmt_revoke"
        assert parsed["reason"] == "test reason"


# =============================================================================
# Test idempotency entries for MGMT messages
# =============================================================================

class TestMgmtIdempotency:
    """Tests for MGMT_CREDENTIAL idempotency event ID generation."""

    def test_mgmt_present_in_event_id_fields(self):
        assert "MGMT_CREDENTIAL_PRESENT" in EVENT_ID_FIELDS
        assert EVENT_ID_FIELDS["MGMT_CREDENTIAL_PRESENT"] == ["event_id"]

    def test_mgmt_revoke_in_event_id_fields(self):
        assert "MGMT_CREDENTIAL_REVOKE" in EVENT_ID_FIELDS
        assert EVENT_ID_FIELDS["MGMT_CREDENTIAL_REVOKE"] == ["credential_id", "issuer_id"]

    def test_mgmt_present_generates_event_id(self):
        payload = {"event_id": "test-uuid-123"}
        eid = generate_event_id("MGMT_CREDENTIAL_PRESENT", payload)
        assert eid is not None
        assert len(eid) == 32

    def test_mgmt_revoke_generates_event_id(self):
        payload = {"credential_id": "cred-123", "issuer_id": ALICE_PUBKEY}
        eid = generate_event_id("MGMT_CREDENTIAL_REVOKE", payload)
        assert eid is not None
        assert len(eid) == 32

    def test_mgmt_revoke_deterministic(self):
        payload = {"credential_id": "cred-123", "issuer_id": ALICE_PUBKEY}
        eid1 = generate_event_id("MGMT_CREDENTIAL_REVOKE", payload)
        eid2 = generate_event_id("MGMT_CREDENTIAL_REVOKE", payload)
        assert eid1 == eid2

    def test_mgmt_revoke_different_for_different_creds(self):
        p1 = {"credential_id": "cred-1", "issuer_id": ALICE_PUBKEY}
        p2 = {"credential_id": "cred-2", "issuer_id": ALICE_PUBKEY}
        assert generate_event_id("MGMT_CREDENTIAL_REVOKE", p1) != \
               generate_event_id("MGMT_CREDENTIAL_REVOKE", p2)


# =============================================================================
# Test MGMT credential gossip handlers
# =============================================================================

class TestMgmtCredentialPresentHandler:
    """Tests for ManagementSchemaRegistry.handle_mgmt_credential_present."""

    def _make_registry(self, db=None):
        db = db or MockDatabase()
        rpc = MagicMock()
        rpc.checkmessage.return_value = {
            "verified": True,
            "pubkey": ALICE_PUBKEY,
        }
        registry = ManagementSchemaRegistry(
            database=db, plugin=MagicMock(), rpc=rpc, our_pubkey=BOB_PUBKEY,
        )
        return registry, db, rpc

    def test_valid_credential_stored(self):
        registry, db, rpc = self._make_registry()
        payload = _make_mgmt_present_payload()
        result = registry.handle_mgmt_credential_present(ALICE_PUBKEY, payload)
        assert result is True
        cred_id = payload["credential"]["credential_id"]
        assert cred_id in db.mgmt_credentials

    def test_missing_credential_dict(self):
        registry, _, _ = self._make_registry()
        result = registry.handle_mgmt_credential_present(ALICE_PUBKEY, {})
        assert result is False

    def test_missing_credential_id(self):
        registry, _, _ = self._make_registry()
        payload = _make_mgmt_present_payload()
        del payload["credential"]["credential_id"]
        result = registry.handle_mgmt_credential_present(ALICE_PUBKEY, payload)
        assert result is False

    def test_invalid_tier(self):
        registry, _, _ = self._make_registry()
        payload = _make_mgmt_present_payload(tier="superadmin")
        result = registry.handle_mgmt_credential_present(ALICE_PUBKEY, payload)
        assert result is False

    def test_invalid_validity_period(self):
        registry, _, _ = self._make_registry()
        now = int(time.time())
        payload = _make_mgmt_present_payload(valid_from=now, valid_until=now - 1)
        result = registry.handle_mgmt_credential_present(ALICE_PUBKEY, payload)
        assert result is False

    def test_missing_signature_rejected(self):
        registry, _, _ = self._make_registry()
        payload = _make_mgmt_present_payload(signature="")
        result = registry.handle_mgmt_credential_present(ALICE_PUBKEY, payload)
        assert result is False

    def test_no_rpc_rejected(self):
        db = MockDatabase()
        registry = ManagementSchemaRegistry(
            database=db, plugin=MagicMock(), rpc=None, our_pubkey=BOB_PUBKEY,
        )
        payload = _make_mgmt_present_payload()
        result = registry.handle_mgmt_credential_present(ALICE_PUBKEY, payload)
        assert result is False

    def test_signature_verification_failed(self):
        registry, _, rpc = self._make_registry()
        rpc.checkmessage.return_value = {"verified": False, "pubkey": ALICE_PUBKEY}
        payload = _make_mgmt_present_payload()
        result = registry.handle_mgmt_credential_present(ALICE_PUBKEY, payload)
        assert result is False

    def test_signature_pubkey_mismatch(self):
        registry, _, rpc = self._make_registry()
        rpc.checkmessage.return_value = {"verified": True, "pubkey": DAVE_PUBKEY}
        payload = _make_mgmt_present_payload()
        result = registry.handle_mgmt_credential_present(ALICE_PUBKEY, payload)
        assert result is False

    def test_idempotent_duplicate(self):
        registry, db, _ = self._make_registry()
        payload = _make_mgmt_present_payload()
        result1 = registry.handle_mgmt_credential_present(ALICE_PUBKEY, payload)
        result2 = registry.handle_mgmt_credential_present(ALICE_PUBKEY, payload)
        assert result1 is True
        assert result2 is True  # Idempotent
        assert db.mgmt_credential_count == 1

    def test_row_cap_enforcement(self):
        db = MockDatabase()
        db.mgmt_credential_count = MAX_MANAGEMENT_CREDENTIALS
        registry, _, _ = self._make_registry(db)
        payload = _make_mgmt_present_payload()
        result = registry.handle_mgmt_credential_present(ALICE_PUBKEY, payload)
        assert result is False

    def test_checkmessage_exception(self):
        registry, _, rpc = self._make_registry()
        rpc.checkmessage.side_effect = Exception("RPC error")
        payload = _make_mgmt_present_payload()
        result = registry.handle_mgmt_credential_present(ALICE_PUBKEY, payload)
        assert result is False


class TestMgmtCredentialRevokeHandler:
    """Tests for ManagementSchemaRegistry.handle_mgmt_credential_revoke."""

    def _make_registry_with_cred(self):
        db = MockDatabase()
        rpc = MagicMock()
        rpc.checkmessage.return_value = {
            "verified": True,
            "pubkey": ALICE_PUBKEY,
        }
        registry = ManagementSchemaRegistry(
            database=db, plugin=MagicMock(), rpc=rpc, our_pubkey=BOB_PUBKEY,
        )
        # Pre-store a credential
        cred_id = "test-cred-for-revoke"
        db.store_management_credential(
            credential_id=cred_id, issuer_id=ALICE_PUBKEY,
            agent_id=BOB_PUBKEY, node_id=CHARLIE_PUBKEY,
            tier="standard",
            allowed_schemas_json='["hive:fee-policy/*"]',
            constraints_json="{}",
            valid_from=int(time.time()) - 86400,
            valid_until=int(time.time()) + 86400 * 90,
            signature="zbase32sig",
        )
        return registry, db, rpc, cred_id

    def test_valid_revocation(self):
        registry, db, rpc, cred_id = self._make_registry_with_cred()
        payload = {
            "credential_id": cred_id,
            "issuer_id": ALICE_PUBKEY,
            "reason": "expired",
            "signature": "revoke-sig",
        }
        result = registry.handle_mgmt_credential_revoke(ALICE_PUBKEY, payload)
        assert result is True
        assert db.mgmt_credentials[cred_id]["revoked_at"] is not None

    def test_missing_credential_id(self):
        registry, _, _, _ = self._make_registry_with_cred()
        payload = {"reason": "test", "issuer_id": ALICE_PUBKEY, "signature": "sig"}
        result = registry.handle_mgmt_credential_revoke(ALICE_PUBKEY, payload)
        assert result is False

    def test_bad_reason(self):
        registry, _, _, cred_id = self._make_registry_with_cred()
        payload = {
            "credential_id": cred_id,
            "issuer_id": ALICE_PUBKEY,
            "reason": "",
            "signature": "sig",
        }
        result = registry.handle_mgmt_credential_revoke(ALICE_PUBKEY, payload)
        assert result is False

    def test_long_reason(self):
        registry, _, _, cred_id = self._make_registry_with_cred()
        payload = {
            "credential_id": cred_id,
            "issuer_id": ALICE_PUBKEY,
            "reason": "x" * 501,
            "signature": "sig",
        }
        result = registry.handle_mgmt_credential_revoke(ALICE_PUBKEY, payload)
        assert result is False

    def test_credential_not_found(self):
        registry, _, _, _ = self._make_registry_with_cred()
        payload = {
            "credential_id": "nonexistent",
            "issuer_id": ALICE_PUBKEY,
            "reason": "test",
            "signature": "sig",
        }
        result = registry.handle_mgmt_credential_revoke(ALICE_PUBKEY, payload)
        assert result is False

    def test_issuer_mismatch(self):
        registry, _, _, cred_id = self._make_registry_with_cred()
        payload = {
            "credential_id": cred_id,
            "issuer_id": DAVE_PUBKEY,
            "reason": "test",
            "signature": "sig",
        }
        result = registry.handle_mgmt_credential_revoke(ALICE_PUBKEY, payload)
        assert result is False

    def test_already_revoked_idempotent(self):
        registry, db, _, cred_id = self._make_registry_with_cred()
        db.mgmt_credentials[cred_id]["revoked_at"] = int(time.time())
        payload = {
            "credential_id": cred_id,
            "issuer_id": ALICE_PUBKEY,
            "reason": "test",
            "signature": "sig",
        }
        result = registry.handle_mgmt_credential_revoke(ALICE_PUBKEY, payload)
        assert result is True

    def test_missing_signature(self):
        registry, _, _, cred_id = self._make_registry_with_cred()
        payload = {
            "credential_id": cred_id,
            "issuer_id": ALICE_PUBKEY,
            "reason": "test",
            "signature": "",
        }
        result = registry.handle_mgmt_credential_revoke(ALICE_PUBKEY, payload)
        assert result is False

    def test_no_rpc(self):
        db = MockDatabase()
        db.store_management_credential(
            credential_id="cred-1", issuer_id=ALICE_PUBKEY,
            agent_id=BOB_PUBKEY, node_id=CHARLIE_PUBKEY,
            tier="standard", allowed_schemas_json='["*"]',
            constraints_json="{}", valid_from=0, valid_until=99999999999,
            signature="sig",
        )
        registry = ManagementSchemaRegistry(
            database=db, plugin=MagicMock(), rpc=None, our_pubkey=BOB_PUBKEY,
        )
        payload = {
            "credential_id": "cred-1",
            "issuer_id": ALICE_PUBKEY,
            "reason": "test",
            "signature": "sig",
        }
        result = registry.handle_mgmt_credential_revoke(ALICE_PUBKEY, payload)
        assert result is False

    def test_sig_verification_failed(self):
        registry, _, rpc, cred_id = self._make_registry_with_cred()
        rpc.checkmessage.return_value = {"verified": False}
        payload = {
            "credential_id": cred_id,
            "issuer_id": ALICE_PUBKEY,
            "reason": "test",
            "signature": "bad-sig",
        }
        result = registry.handle_mgmt_credential_revoke(ALICE_PUBKEY, payload)
        assert result is False


# =============================================================================
# Test auto-issue node credentials
# =============================================================================

@dataclass
class MockPeerState:
    """Mock HivePeerState for auto-issue tests."""
    peer_id: str = ""
    last_update: int = 0
    capacity_sats: int = 1_000_000
    fees_forward_count: int = 50
    fee_policy: dict = None

    def __post_init__(self):
        if self.fee_policy is None:
            self.fee_policy = {"fee_ppm": 100}


class TestAutoIssueNodeCredentials:
    """Tests for DIDCredentialManager.auto_issue_node_credentials."""

    def _make_mgr(self):
        db = MockDIDDatabase()
        rpc = MagicMock()
        rpc.signmessage.return_value = {"zbase": "auto-issue-sig"}
        mgr = DIDCredentialManager(
            database=db, plugin=MagicMock(), rpc=rpc, our_pubkey=ALICE_PUBKEY,
        )
        return mgr, db, rpc

    def test_issues_for_active_peer(self):
        mgr, db, _ = self._make_mgr()
        now = int(time.time())
        state_mgr = MagicMock()
        state_mgr.get_all_peer_states.return_value = {
            BOB_PUBKEY: MockPeerState(peer_id=BOB_PUBKEY, last_update=now - 300),
        }
        count = mgr.auto_issue_node_credentials(state_manager=state_mgr)
        assert count == 1
        assert db.did_credential_count == 1

    def test_skips_self(self):
        mgr, db, _ = self._make_mgr()
        now = int(time.time())
        state_mgr = MagicMock()
        state_mgr.get_all_peer_states.return_value = {
            ALICE_PUBKEY: MockPeerState(peer_id=ALICE_PUBKEY, last_update=now - 300),
        }
        count = mgr.auto_issue_node_credentials(state_manager=state_mgr)
        assert count == 0

    def test_skips_recent_credential(self):
        mgr, db, _ = self._make_mgr()
        now = int(time.time())
        # Pre-store a recent credential
        db.store_did_credential(
            credential_id="existing", issuer_id=ALICE_PUBKEY,
            subject_id=BOB_PUBKEY, domain="hive:node",
            period_start=now - 86400, period_end=now,
            metrics_json='{"routing_reliability":0.9}', outcome="neutral",
            evidence_json=None, signature="sig",
            issued_at=now - 3600,  # 1 hour ago (within 7-day interval)
            expires_at=now + 86400 * 90, received_from=None,
        )
        state_mgr = MagicMock()
        state_mgr.get_all_peer_states.return_value = {
            BOB_PUBKEY: MockPeerState(peer_id=BOB_PUBKEY, last_update=now - 300),
        }
        count = mgr.auto_issue_node_credentials(state_manager=state_mgr)
        assert count == 0  # Skipped due to recent credential

    def test_no_state_manager_returns_zero(self):
        mgr, _, _ = self._make_mgr()
        count = mgr.auto_issue_node_credentials(state_manager=None)
        assert count == 0

    def test_no_rpc_returns_zero(self):
        db = MockDIDDatabase()
        mgr = DIDCredentialManager(
            database=db, plugin=MagicMock(), rpc=None, our_pubkey=ALICE_PUBKEY,
        )
        state_mgr = MagicMock()
        state_mgr.get_all_peer_states.return_value = {}
        count = mgr.auto_issue_node_credentials(state_manager=state_mgr)
        assert count == 0

    def test_broadcasts_when_fn_provided(self):
        mgr, _, _ = self._make_mgr()
        now = int(time.time())
        state_mgr = MagicMock()
        state_mgr.get_all_peer_states.return_value = {
            BOB_PUBKEY: MockPeerState(peer_id=BOB_PUBKEY, last_update=now - 300),
        }
        broadcast_fn = MagicMock()
        mgr.auto_issue_node_credentials(
            state_manager=state_mgr, broadcast_fn=broadcast_fn,
        )
        broadcast_fn.assert_called_once()

    def test_stale_peer_low_uptime(self):
        mgr, db, _ = self._make_mgr()
        now = int(time.time())
        state_mgr = MagicMock()
        # Peer not updated in > 1 day → low uptime
        state_mgr.get_all_peer_states.return_value = {
            BOB_PUBKEY: MockPeerState(
                peer_id=BOB_PUBKEY, last_update=now - 100000,
            ),
        }
        count = mgr.auto_issue_node_credentials(state_manager=state_mgr)
        assert count == 1
        cred = list(db.did_credentials.values())[0]
        metrics = json.loads(cred["metrics_json"])
        assert metrics["uptime"] == 0.3  # Low uptime for stale peer

    def test_with_contribution_tracker(self):
        mgr, db, _ = self._make_mgr()
        now = int(time.time())
        contrib = MagicMock()
        contrib.get_contribution_stats.return_value = {
            "forwarded": 1000, "received": 500, "ratio": 2.0,
        }
        state_mgr = MagicMock()
        state_mgr.get_all_peer_states.return_value = {
            BOB_PUBKEY: MockPeerState(peer_id=BOB_PUBKEY, last_update=now - 300),
        }
        count = mgr.auto_issue_node_credentials(
            state_manager=state_mgr, contribution_tracker=contrib,
        )
        assert count == 1
        cred = list(db.did_credentials.values())[0]
        metrics = json.loads(cred["metrics_json"])
        assert metrics["routing_reliability"] > 0.5


# =============================================================================
# Test rebroadcast own credentials
# =============================================================================

class TestRebroadcastOwnCredentials:
    """Tests for DIDCredentialManager.rebroadcast_own_credentials."""

    def _make_mgr_with_creds(self):
        db = MockDIDDatabase()
        rpc = MagicMock()
        mgr = DIDCredentialManager(
            database=db, plugin=MagicMock(), rpc=rpc, our_pubkey=ALICE_PUBKEY,
        )
        now = int(time.time())
        # Store 2 credentials issued by us
        for i in range(2):
            db.store_did_credential(
                credential_id=f"cred-{i}",
                issuer_id=ALICE_PUBKEY,
                subject_id=BOB_PUBKEY,
                domain="hive:node",
                period_start=now - 86400,
                period_end=now,
                metrics_json='{"routing_reliability":0.9,"uptime":0.95,"htlc_success_rate":0.88,"avg_fee_ppm":100}',
                outcome="neutral",
                evidence_json=None,
                signature="sig",
                issued_at=now - 3600,
                expires_at=now + 86400 * 90,
                received_from=None,  # Issued by us
            )
        return mgr, db

    def test_rebroadcasts_own_creds(self):
        mgr, _ = self._make_mgr_with_creds()
        broadcast_fn = MagicMock()
        count = mgr.rebroadcast_own_credentials(broadcast_fn=broadcast_fn)
        assert count == 2
        assert broadcast_fn.call_count == 2

    def test_no_broadcast_fn_returns_zero(self):
        mgr, _ = self._make_mgr_with_creds()
        count = mgr.rebroadcast_own_credentials(broadcast_fn=None)
        assert count == 0

    def test_no_pubkey_returns_zero(self):
        db = MockDIDDatabase()
        mgr = DIDCredentialManager(
            database=db, plugin=MagicMock(), rpc=None, our_pubkey="",
        )
        broadcast_fn = MagicMock()
        count = mgr.rebroadcast_own_credentials(broadcast_fn=broadcast_fn)
        assert count == 0

    def test_skips_revoked(self):
        mgr, db = self._make_mgr_with_creds()
        # Revoke one
        db.did_credentials["cred-0"]["revoked_at"] = int(time.time())
        broadcast_fn = MagicMock()
        count = mgr.rebroadcast_own_credentials(broadcast_fn=broadcast_fn)
        assert count == 1

    def test_skips_expired(self):
        mgr, db = self._make_mgr_with_creds()
        # Expire one
        db.did_credentials["cred-0"]["expires_at"] = int(time.time()) - 1
        broadcast_fn = MagicMock()
        count = mgr.rebroadcast_own_credentials(broadcast_fn=broadcast_fn)
        assert count == 1


# =============================================================================
# Test planner reputation integration
# =============================================================================

class TestPlannerReputationIntegration:
    """Tests for reputation tier in planner expansion scoring."""

    def test_underserved_result_has_reputation_tier(self):
        from modules.planner import UnderservedResult
        result = UnderservedResult(
            target=BOB_PUBKEY,
            public_capacity_sats=1_000_000,
            hive_share_pct=0.05,
            score=1.0,
            reputation_tier="trusted",
        )
        assert result.reputation_tier == "trusted"

    def test_underserved_result_default_newcomer(self):
        from modules.planner import UnderservedResult
        result = UnderservedResult(
            target=BOB_PUBKEY,
            public_capacity_sats=1_000_000,
            hive_share_pct=0.05,
            score=1.0,
        )
        assert result.reputation_tier == "newcomer"

    def test_planner_has_did_credential_mgr_attr(self):
        from modules.planner import Planner
        # Minimal init
        planner = Planner(
            state_manager=MagicMock(),
            database=MagicMock(),
            bridge=MagicMock(),
            clboss_bridge=MagicMock(),
        )
        assert hasattr(planner, 'did_credential_mgr')
        assert planner.did_credential_mgr is None


# =============================================================================
# Test membership reputation integration
# =============================================================================

class TestMembershipReputationIntegration:
    """Tests for reputation as promotion signal."""

    def _make_membership_mgr(self, peer_id=None):
        from modules.membership import MembershipManager, MembershipTier
        now = int(time.time())
        pid = peer_id or BOB_PUBKEY

        db = MagicMock()
        db.get_presence.return_value = {
            "online_seconds_rolling": 86000,
            "last_change_ts": now - 100,
            "window_start_ts": now - 86400,
            "is_online": True,
        }

        config = MagicMock()
        config.probation_days = 90
        config.min_uptime_pct = 95.0
        config.min_contribution_ratio = 1.0
        config.min_unique_peers = 1

        contrib_mgr = MagicMock()
        contrib_mgr.get_contribution_stats.return_value = {
            "forwarded": 100, "received": 50, "ratio": 2.0,
        }

        mgr = MembershipManager(
            db=db,
            state_manager=MagicMock(),
            contribution_mgr=contrib_mgr,
            bridge=MagicMock(),
            config=config,
            plugin=MagicMock(),
        )
        return mgr, db, MembershipTier

    def test_has_did_credential_mgr_attr(self):
        mgr, _, _ = self._make_membership_mgr()
        assert hasattr(mgr, 'did_credential_mgr')
        assert mgr.did_credential_mgr is None

    @patch.object(
        __import__('modules.membership', fromlist=['MembershipManager']).MembershipManager,
        '_get_hive_centrality_metrics',
        return_value={"hive_centrality": 0.2, "hive_peer_count": 1,
                      "hive_reachability": 0.5, "rebalance_hub_score": 0.0},
    )
    @patch.object(
        __import__('modules.membership', fromlist=['MembershipManager']).MembershipManager,
        'get_unique_peers',
        return_value=["peer1", "peer2"],
    )
    @patch.object(
        __import__('modules.membership', fromlist=['MembershipManager']).MembershipManager,
        'is_probation_complete',
        return_value=True,
    )
    def test_evaluate_includes_reputation_tier(self, mock_prob, mock_peers, mock_cent):
        mgr, db, MembershipTier = self._make_membership_mgr()
        now = int(time.time())
        db.get_member.return_value = {
            "peer_id": BOB_PUBKEY,
            "tier": MembershipTier.NEOPHYTE.value,
            "joined_at": now - 100 * 86400,
            "uptime_pct": 0.99,
        }
        did_mgr = MagicMock()
        did_mgr.get_credit_tier.return_value = "trusted"
        mgr.did_credential_mgr = did_mgr

        result = mgr.evaluate_promotion(BOB_PUBKEY)
        assert "reputation_tier" in result
        assert result["reputation_tier"] == "trusted"

    @patch.object(
        __import__('modules.membership', fromlist=['MembershipManager']).MembershipManager,
        '_get_hive_centrality_metrics',
        return_value={"hive_centrality": 0.2, "hive_peer_count": 1,
                      "hive_reachability": 0.5, "rebalance_hub_score": 0.0},
    )
    @patch.object(
        __import__('modules.membership', fromlist=['MembershipManager']).MembershipManager,
        'get_unique_peers',
        return_value=["peer1"],
    )
    @patch.object(
        __import__('modules.membership', fromlist=['MembershipManager']).MembershipManager,
        'is_probation_complete',
        return_value=False,
    )
    def test_reputation_fast_track(self, mock_prob, mock_peers, mock_cent):
        """Trusted/senior reputation enables fast-track promotion."""
        mgr, db, MembershipTier = self._make_membership_mgr()
        now = int(time.time())
        db.get_member.return_value = {
            "peer_id": BOB_PUBKEY,
            "tier": MembershipTier.NEOPHYTE.value,
            "joined_at": now - 35 * 86400,  # 35 days (past 30-day fast-track min)
            "uptime_pct": 0.99,
        }
        # Low centrality (0.2) — would NOT qualify for centrality fast-track
        did_mgr = MagicMock()
        did_mgr.get_credit_tier.return_value = "trusted"
        mgr.did_credential_mgr = did_mgr

        result = mgr.evaluate_promotion(BOB_PUBKEY)
        fast_track = result.get("fast_track", {})
        assert fast_track.get("eligible") is True
        assert fast_track.get("reason") == "reputation_trusted"

    @patch.object(
        __import__('modules.membership', fromlist=['MembershipManager']).MembershipManager,
        '_get_hive_centrality_metrics',
        return_value={"hive_centrality": 0.2, "hive_peer_count": 1,
                      "hive_reachability": 0.5, "rebalance_hub_score": 0.0},
    )
    @patch.object(
        __import__('modules.membership', fromlist=['MembershipManager']).MembershipManager,
        'get_unique_peers',
        return_value=["peer1"],
    )
    @patch.object(
        __import__('modules.membership', fromlist=['MembershipManager']).MembershipManager,
        'is_probation_complete',
        return_value=False,
    )
    def test_newcomer_no_fast_track(self, mock_prob, mock_peers, mock_cent):
        """Newcomer reputation doesn't enable fast-track."""
        mgr, db, MembershipTier = self._make_membership_mgr()
        now = int(time.time())
        db.get_member.return_value = {
            "peer_id": BOB_PUBKEY,
            "tier": MembershipTier.NEOPHYTE.value,
            "joined_at": now - 35 * 86400,
            "uptime_pct": 0.99,
        }
        did_mgr = MagicMock()
        did_mgr.get_credit_tier.return_value = "newcomer"
        mgr.did_credential_mgr = did_mgr

        result = mgr.evaluate_promotion(BOB_PUBKEY)
        fast_track = result.get("fast_track", {})
        # Without centrality, newcomer should not be fast-tracked
        assert fast_track.get("eligible") is not True or fast_track.get("reason") is None


# =============================================================================
# Test settlement reputation integration
# =============================================================================

class TestSettlementReputationIntegration:
    """Tests for reputation tier in settlement data."""

    def test_settlement_mgr_has_did_credential_mgr_attr(self):
        from modules.settlement import SettlementManager
        mgr = SettlementManager(
            database=MagicMock(), plugin=MagicMock(), rpc=MagicMock(),
        )
        assert hasattr(mgr, 'did_credential_mgr')
        assert mgr.did_credential_mgr is None
