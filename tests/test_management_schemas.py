"""
Tests for Management Schema Module (Phase 2 - DID Ecosystem).

Tests cover:
- Schema registry: 15 categories, actions, danger scores
- DangerScore dataclass: 5 dimensions, total calculation
- Command validation against schema definitions
- Tier hierarchy and authorization checks
- Management credential lifecycle: issue, revoke, list
- Receipt recording
- Pricing calculation
- Schema matching with wildcards
"""

import json
import time
import uuid
import pytest
from unittest.mock import MagicMock

from modules.management_schemas import (
    DangerScore,
    SchemaAction,
    SchemaCategory,
    ManagementCredential,
    ManagementReceipt,
    ManagementSchemaRegistry,
    SCHEMA_REGISTRY,
    TIER_HIERARCHY,
    VALID_TIERS,
    MAX_MANAGEMENT_CREDENTIALS,
    MAX_MANAGEMENT_RECEIPTS,
    BASE_PRICE_PER_DANGER_POINT,
    TIER_PRICING_MULTIPLIERS,
    get_credential_signing_payload,
    _schema_matches,
    _is_valid_pubkey,
)


# =============================================================================
# Test helpers
# =============================================================================

ALICE_PUBKEY = "03" + "a1" * 32  # 66 hex chars
BOB_PUBKEY = "03" + "b2" * 32
CHARLIE_PUBKEY = "03" + "c3" * 32


class MockDatabase:
    """Mock database with management credential/receipt methods."""

    def __init__(self):
        self.credentials = {}
        self.receipts = {}

    def store_management_credential(self, credential_id, issuer_id, agent_id,
                                     node_id, tier, allowed_schemas_json,
                                     constraints_json, valid_from, valid_until,
                                     signature):
        self.credentials[credential_id] = {
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
            "created_at": int(time.time()),
        }
        return True

    def get_management_credential(self, credential_id):
        return self.credentials.get(credential_id)

    def get_management_credentials(self, agent_id=None, node_id=None,
                                    limit=100):
        results = []
        for c in self.credentials.values():
            if agent_id and c["agent_id"] != agent_id:
                continue
            if node_id and c["node_id"] != node_id:
                continue
            results.append(c)
        return results[:limit]

    def revoke_management_credential(self, credential_id, revoked_at):
        if credential_id in self.credentials:
            self.credentials[credential_id]["revoked_at"] = revoked_at
            return True
        return False

    def count_management_credentials(self):
        return len(self.credentials)

    def store_management_receipt(self, receipt_id, credential_id, schema_id,
                                  action, params_json, danger_score,
                                  result_json, state_hash_before,
                                  state_hash_after, executed_at,
                                  executor_signature):
        self.receipts[receipt_id] = {
            "receipt_id": receipt_id,
            "credential_id": credential_id,
            "schema_id": schema_id,
            "action": action,
            "params_json": params_json,
            "danger_score": danger_score,
            "result_json": result_json,
            "state_hash_before": state_hash_before,
            "state_hash_after": state_hash_after,
            "executed_at": executed_at,
            "executor_signature": executor_signature,
        }
        return True

    def get_management_receipts(self, credential_id, limit=100):
        results = [r for r in self.receipts.values()
                   if r["credential_id"] == credential_id]
        return results[:limit]


def _make_registry(our_pubkey=ALICE_PUBKEY):
    """Create a ManagementSchemaRegistry with mock DB and RPC."""
    db = MockDatabase()
    plugin = MagicMock()
    rpc = MagicMock()
    rpc.signmessage.return_value = {"zbase": "fakesig123"}
    registry = ManagementSchemaRegistry(
        database=db,
        plugin=plugin,
        rpc=rpc,
        our_pubkey=our_pubkey,
    )
    return registry, db


# =============================================================================
# DangerScore Tests
# =============================================================================

class TestDangerScore:
    def test_total_is_max_of_dimensions(self):
        ds = DangerScore(1, 5, 3, 2, 4)
        assert ds.total == 5

    def test_total_all_equal(self):
        ds = DangerScore(7, 7, 7, 7, 7)
        assert ds.total == 7

    def test_total_single_high(self):
        ds = DangerScore(1, 1, 1, 1, 10)
        assert ds.total == 10

    def test_to_dict(self):
        ds = DangerScore(2, 3, 4, 5, 6)
        d = ds.to_dict()
        assert d["reversibility"] == 2
        assert d["financial_exposure"] == 3
        assert d["time_sensitivity"] == 4
        assert d["blast_radius"] == 5
        assert d["recovery_difficulty"] == 6
        assert d["total"] == 6

    def test_frozen(self):
        ds = DangerScore(1, 1, 1, 1, 1)
        with pytest.raises(AttributeError):
            ds.reversibility = 5

    def test_minimum_danger(self):
        ds = DangerScore(1, 1, 1, 1, 1)
        assert ds.total == 1

    def test_maximum_danger(self):
        ds = DangerScore(10, 10, 10, 10, 10)
        assert ds.total == 10


# =============================================================================
# Schema Registry Tests
# =============================================================================

class TestSchemaRegistry:
    def test_has_15_schemas(self):
        assert len(SCHEMA_REGISTRY) == 15

    def test_all_schema_ids_valid(self):
        for schema_id in SCHEMA_REGISTRY:
            assert schema_id.startswith("hive:")
            assert "/v1" in schema_id

    def test_all_schemas_have_actions(self):
        for schema_id, cat in SCHEMA_REGISTRY.items():
            assert len(cat.actions) > 0, f"{schema_id} has no actions"

    def test_all_actions_have_danger_scores(self):
        for schema_id, cat in SCHEMA_REGISTRY.items():
            for action_name, action in cat.actions.items():
                assert isinstance(action.danger, DangerScore)
                assert 1 <= action.danger.total <= 10

    def test_all_actions_have_valid_tiers(self):
        for schema_id, cat in SCHEMA_REGISTRY.items():
            for action_name, action in cat.actions.items():
                assert action.required_tier in VALID_TIERS, \
                    f"{schema_id}/{action_name} has invalid tier: {action.required_tier}"

    def test_danger_ranges_match_actions(self):
        """Verify that each schema's danger_range covers all its actions."""
        for schema_id, cat in SCHEMA_REGISTRY.items():
            actual_min = min(a.danger.total for a in cat.actions.values())
            actual_max = max(a.danger.total for a in cat.actions.values())
            assert actual_min >= cat.danger_range[0], \
                f"{schema_id}: actual min {actual_min} < declared min {cat.danger_range[0]}"
            assert actual_max <= cat.danger_range[1], \
                f"{schema_id}: actual max {actual_max} > declared max {cat.danger_range[1]}"

    def test_monitor_schema_is_low_danger(self):
        monitor = SCHEMA_REGISTRY["hive:monitor/v1"]
        for action in monitor.actions.values():
            assert action.danger.total <= 2
            assert action.required_tier == "monitor"

    def test_channel_close_all_is_max_danger(self):
        channel = SCHEMA_REGISTRY["hive:channel/v1"]
        close_all = channel.actions["close_all"]
        assert close_all.danger.total == 10
        assert close_all.required_tier == "admin"

    def test_set_bulk_requires_advanced(self):
        """set_bulk should require advanced tier (H6 fix)."""
        fee = SCHEMA_REGISTRY["hive:fee-policy/v1"]
        assert fee.actions["set_bulk"].required_tier == "advanced"

    def test_circular_rebalance_requires_advanced(self):
        """circular_rebalance should require advanced tier (H6 fix)."""
        rebalance = SCHEMA_REGISTRY["hive:rebalance/v1"]
        assert rebalance.actions["circular_rebalance"].required_tier == "advanced"

    def test_backup_restore_is_max_danger(self):
        backup = SCHEMA_REGISTRY["hive:backup/v1"]
        restore = backup.actions["restore"]
        assert restore.danger.total == 10
        assert restore.required_tier == "admin"

    def test_schema_to_dict(self):
        monitor = SCHEMA_REGISTRY["hive:monitor/v1"]
        d = monitor.to_dict()
        assert d["schema_id"] == "hive:monitor/v1"
        assert d["name"] == "Monitoring & Read-Only"
        assert "actions" in d
        assert d["action_count"] == len(monitor.actions)

    def test_action_to_dict(self):
        fee = SCHEMA_REGISTRY["hive:fee-policy/v1"]
        action = fee.actions["set_single"]
        d = action.to_dict()
        assert "danger" in d
        assert "required_tier" in d
        assert "parameters" in d


# =============================================================================
# Schema Action Tests
# =============================================================================

class TestSchemaAction:
    def test_action_with_parameters(self):
        action = SchemaAction(
            danger=DangerScore(1, 1, 1, 1, 1),
            required_tier="monitor",
            parameters={"key": str, "value": int},
        )
        assert action.parameters == {"key": str, "value": int}

    def test_action_without_parameters(self):
        action = SchemaAction(
            danger=DangerScore(1, 1, 1, 1, 1),
            required_tier="monitor",
        )
        assert action.parameters == {}


# =============================================================================
# Tier Hierarchy Tests
# =============================================================================

class TestTierHierarchy:
    def test_monitor_lowest(self):
        assert TIER_HIERARCHY["monitor"] == 0

    def test_admin_highest(self):
        assert TIER_HIERARCHY["admin"] == 3

    def test_ordering(self):
        assert TIER_HIERARCHY["monitor"] < TIER_HIERARCHY["standard"]
        assert TIER_HIERARCHY["standard"] < TIER_HIERARCHY["advanced"]
        assert TIER_HIERARCHY["advanced"] < TIER_HIERARCHY["admin"]

    def test_all_tiers_present(self):
        for tier in VALID_TIERS:
            assert tier in TIER_HIERARCHY


# =============================================================================
# Schema Matching Tests
# =============================================================================

class TestSchemaMatching:
    def test_exact_match(self):
        assert _schema_matches("hive:fee-policy/v1", "hive:fee-policy/v1")

    def test_exact_mismatch(self):
        assert not _schema_matches("hive:fee-policy/v1", "hive:monitor/v1")

    def test_wildcard_all(self):
        assert _schema_matches("*", "hive:fee-policy/v1")
        assert _schema_matches("*", "hive:monitor/v1")

    def test_prefix_wildcard(self):
        assert _schema_matches("hive:fee-policy/*", "hive:fee-policy/v1")
        assert _schema_matches("hive:fee-policy/*", "hive:fee-policy/v2")

    def test_prefix_wildcard_no_match(self):
        assert not _schema_matches("hive:fee-policy/*", "hive:monitor/v1")

    def test_prefix_wildcard_boundary(self):
        """Ensure prefix wildcard doesn't match cross-category (C3 fix)."""
        assert not _schema_matches("hive:fee-policy/*", "hive:fee-policy-extended/v1")
        assert _schema_matches("hive:fee-policy/*", "hive:fee-policy/v2")

    def test_empty_pattern(self):
        assert not _schema_matches("", "hive:fee-policy/v1")


# =============================================================================
# ManagementSchemaRegistry Tests
# =============================================================================

class TestRegistryQueries:
    def test_list_schemas(self):
        reg, _ = _make_registry()
        schemas = reg.list_schemas()
        assert len(schemas) == 15
        assert "hive:monitor/v1" in schemas

    def test_get_schema(self):
        reg, _ = _make_registry()
        cat = reg.get_schema("hive:fee-policy/v1")
        assert cat is not None
        assert cat.schema_id == "hive:fee-policy/v1"

    def test_get_schema_not_found(self):
        reg, _ = _make_registry()
        assert reg.get_schema("hive:nonexistent/v1") is None

    def test_get_action(self):
        reg, _ = _make_registry()
        action = reg.get_action("hive:fee-policy/v1", "set_single")
        assert action is not None
        assert action.required_tier == "standard"

    def test_get_action_not_found(self):
        reg, _ = _make_registry()
        assert reg.get_action("hive:fee-policy/v1", "nonexistent") is None
        assert reg.get_action("hive:nonexistent/v1", "set_single") is None

    def test_get_danger_score(self):
        reg, _ = _make_registry()
        ds = reg.get_danger_score("hive:channel/v1", "close_force")
        assert ds is not None
        assert ds.total >= 8

    def test_get_danger_score_not_found(self):
        reg, _ = _make_registry()
        assert reg.get_danger_score("hive:channel/v1", "nonexistent") is None

    def test_get_required_tier(self):
        reg, _ = _make_registry()
        assert reg.get_required_tier("hive:monitor/v1", "get_info") == "monitor"
        assert reg.get_required_tier("hive:channel/v1", "close_force") == "admin"

    def test_get_required_tier_not_found(self):
        reg, _ = _make_registry()
        assert reg.get_required_tier("hive:nonexistent/v1", "x") is None


# =============================================================================
# Command Validation Tests
# =============================================================================

class TestCommandValidation:
    def test_valid_command(self):
        reg, _ = _make_registry()
        ok, reason = reg.validate_command("hive:fee-policy/v1", "set_single",
                                           {"channel_id": "abc", "base_msat": 1000, "fee_ppm": 50})
        assert ok
        assert reason == "valid"

    def test_valid_command_no_params(self):
        reg, _ = _make_registry()
        ok, reason = reg.validate_command("hive:monitor/v1", "get_balance")
        assert ok

    def test_unknown_schema(self):
        reg, _ = _make_registry()
        ok, reason = reg.validate_command("hive:nonexistent/v1", "x")
        assert not ok
        assert "unknown schema" in reason

    def test_unknown_action(self):
        reg, _ = _make_registry()
        ok, reason = reg.validate_command("hive:fee-policy/v1", "nonexistent")
        assert not ok
        assert "unknown action" in reason

    def test_wrong_param_type(self):
        reg, _ = _make_registry()
        ok, reason = reg.validate_command("hive:fee-policy/v1", "set_single",
                                           {"channel_id": 123})  # should be str
        assert not ok
        assert "must be str" in reason

    def test_extra_params_rejected(self):
        """Extra parameters not in the schema are rejected."""
        reg, _ = _make_registry()
        ok, reason = reg.validate_command("hive:fee-policy/v1", "set_single",
                                           {"channel_id": "abc", "extra": True})
        assert not ok
        assert "unexpected parameters" in reason

    def test_missing_params_allowed(self):
        """Missing parameters are allowed (optional by design)."""
        reg, _ = _make_registry()
        ok, reason = reg.validate_command("hive:fee-policy/v1", "set_single",
                                           {"channel_id": "abc"})
        assert ok


# =============================================================================
# Authorization Tests
# =============================================================================

class TestAuthorization:
    def _make_credential(self, tier="standard", schemas=None,
                          valid_from=None, valid_until=None, revoked=False):
        now = int(time.time())
        return ManagementCredential(
            credential_id=str(uuid.uuid4()),
            issuer_id=ALICE_PUBKEY,
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier=tier,
            allowed_schemas=tuple(schemas or ["hive:fee-policy/*", "hive:monitor/*"]),
            constraints="{}",
            valid_from=valid_from or (now - 3600),
            valid_until=valid_until or (now + 86400),
            signature="fakesig",
            revoked_at=now if revoked else None,
        )

    def test_authorized(self):
        reg, _ = _make_registry()
        cred = self._make_credential(tier="standard")
        ok, reason = reg.check_authorization(cred, "hive:fee-policy/v1", "set_single")
        assert ok
        assert reason == "authorized"

    def test_revoked_credential(self):
        reg, _ = _make_registry()
        cred = self._make_credential(revoked=True)
        ok, reason = reg.check_authorization(cred, "hive:fee-policy/v1", "set_single")
        assert not ok
        assert "revoked" in reason

    def test_expired_credential(self):
        reg, _ = _make_registry()
        now = int(time.time())
        cred = self._make_credential(valid_until=now - 3600)
        ok, reason = reg.check_authorization(cred, "hive:fee-policy/v1", "set_single")
        assert not ok
        assert "expired" in reason

    def test_not_yet_valid(self):
        reg, _ = _make_registry()
        now = int(time.time())
        cred = self._make_credential(valid_from=now + 3600)
        ok, reason = reg.check_authorization(cred, "hive:fee-policy/v1", "set_single")
        assert not ok
        assert "not yet valid" in reason

    def test_insufficient_tier(self):
        reg, _ = _make_registry()
        cred = self._make_credential(tier="monitor", schemas=["*"])
        ok, reason = reg.check_authorization(cred, "hive:fee-policy/v1", "set_single")
        assert not ok
        assert "insufficient" in reason

    def test_schema_not_in_allowlist(self):
        reg, _ = _make_registry()
        cred = self._make_credential(tier="admin", schemas=["hive:monitor/*"])
        ok, reason = reg.check_authorization(cred, "hive:channel/v1", "open")
        assert not ok
        assert "not in credential allowlist" in reason

    def test_wildcard_schema_allows_all(self):
        reg, _ = _make_registry()
        cred = self._make_credential(tier="admin", schemas=["*"])
        ok, reason = reg.check_authorization(cred, "hive:channel/v1", "close_force")
        assert ok

    def test_higher_tier_allows_lower(self):
        """Admin tier should authorize standard-required actions."""
        reg, _ = _make_registry()
        cred = self._make_credential(tier="admin", schemas=["*"])
        ok, reason = reg.check_authorization(cred, "hive:fee-policy/v1", "set_single")
        assert ok

    def test_unknown_action_denied(self):
        reg, _ = _make_registry()
        cred = self._make_credential(tier="admin", schemas=["*"])
        ok, reason = reg.check_authorization(cred, "hive:fee-policy/v1", "nonexistent")
        assert not ok


# =============================================================================
# Pricing Tests
# =============================================================================

class TestPricing:
    def test_basic_pricing(self):
        reg, _ = _make_registry()
        ds = DangerScore(1, 1, 1, 1, 1)  # total=1
        price = reg.get_pricing(ds, "newcomer")
        assert price == int(1 * BASE_PRICE_PER_DANGER_POINT * 1.5)

    def test_higher_danger_higher_price(self):
        reg, _ = _make_registry()
        ds_low = DangerScore(1, 1, 1, 1, 1)
        ds_high = DangerScore(10, 10, 10, 10, 10)
        price_low = reg.get_pricing(ds_low, "newcomer")
        price_high = reg.get_pricing(ds_high, "newcomer")
        assert price_high > price_low

    def test_better_reputation_discount(self):
        reg, _ = _make_registry()
        ds = DangerScore(5, 5, 5, 5, 5)
        price_newcomer = reg.get_pricing(ds, "newcomer")
        price_senior = reg.get_pricing(ds, "senior")
        assert price_senior < price_newcomer

    def test_minimum_price_is_1(self):
        reg, _ = _make_registry()
        ds = DangerScore(1, 1, 1, 1, 1)
        price = reg.get_pricing(ds, "senior")
        assert price >= 1

    def test_all_tier_multipliers(self):
        reg, _ = _make_registry()
        ds = DangerScore(5, 5, 5, 5, 5)
        prices = {}
        for tier in TIER_PRICING_MULTIPLIERS:
            prices[tier] = reg.get_pricing(ds, tier)
        # newcomer > recognized > trusted > senior
        assert prices["newcomer"] > prices["recognized"]
        assert prices["recognized"] > prices["trusted"]
        assert prices["trusted"] > prices["senior"]


# =============================================================================
# Credential Issuance Tests
# =============================================================================

class TestCredentialIssuance:
    def test_issue_credential(self):
        reg, db = _make_registry()
        cred = reg.issue_credential(
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier="standard",
            allowed_schemas=["hive:fee-policy/*"],
            constraints={"max_fee_ppm": 1000},
        )
        assert cred is not None
        assert cred.issuer_id == ALICE_PUBKEY
        assert cred.agent_id == BOB_PUBKEY
        assert cred.tier == "standard"
        assert cred.signature == "fakesig123"
        assert len(db.credentials) == 1

    def test_issue_rejects_self(self):
        reg, db = _make_registry()
        cred = reg.issue_credential(
            agent_id=ALICE_PUBKEY,  # same as our_pubkey
            node_id=ALICE_PUBKEY,
            tier="standard",
            allowed_schemas=["*"],
            constraints={},
        )
        assert cred is None
        assert len(db.credentials) == 0

    def test_issue_rejects_invalid_tier(self):
        reg, db = _make_registry()
        cred = reg.issue_credential(
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier="superadmin",
            allowed_schemas=["*"],
            constraints={},
        )
        assert cred is None

    def test_issue_rejects_empty_schemas(self):
        reg, db = _make_registry()
        cred = reg.issue_credential(
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier="standard",
            allowed_schemas=[],
            constraints={},
        )
        assert cred is None

    def test_issue_rejects_empty_agent(self):
        reg, db = _make_registry()
        cred = reg.issue_credential(
            agent_id="",
            node_id=ALICE_PUBKEY,
            tier="standard",
            allowed_schemas=["*"],
            constraints={},
        )
        assert cred is None

    def test_issue_no_rpc(self):
        db = MockDatabase()
        plugin = MagicMock()
        reg = ManagementSchemaRegistry(db, plugin, rpc=None, our_pubkey=ALICE_PUBKEY)
        cred = reg.issue_credential(
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier="standard",
            allowed_schemas=["*"],
            constraints={},
        )
        assert cred is None

    def test_issue_hsm_failure(self):
        reg, db = _make_registry()
        reg.rpc.signmessage.side_effect = Exception("HSM unavailable")
        cred = reg.issue_credential(
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier="standard",
            allowed_schemas=["*"],
            constraints={},
        )
        assert cred is None

    def test_issue_valid_days(self):
        reg, db = _make_registry()
        cred = reg.issue_credential(
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier="monitor",
            allowed_schemas=["hive:monitor/*"],
            constraints={},
            valid_days=30,
        )
        assert cred is not None
        # valid_until should be ~30 days from now
        assert cred.valid_until - cred.valid_from == 30 * 86400

    def test_issue_rejects_zero_valid_days(self):
        """valid_days must be > 0 (H4 fix)."""
        reg, db = _make_registry()
        cred = reg.issue_credential(
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier="standard",
            allowed_schemas=["*"],
            constraints={},
            valid_days=0,
        )
        assert cred is None

    def test_issue_rejects_negative_valid_days(self):
        reg, db = _make_registry()
        cred = reg.issue_credential(
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier="standard",
            allowed_schemas=["*"],
            constraints={},
            valid_days=-1,
        )
        assert cred is None

    def test_issue_rejects_oversized_schemas(self):
        """allowed_schemas JSON must be within size limit (H5 fix)."""
        reg, db = _make_registry()
        # Create a schema list that exceeds MAX_ALLOWED_SCHEMAS_LEN
        huge_schemas = [f"hive:schema-{i}/v1" for i in range(500)]
        cred = reg.issue_credential(
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier="standard",
            allowed_schemas=huge_schemas,
            constraints={},
        )
        assert cred is None

    def test_issue_rejects_oversized_constraints(self):
        """constraints JSON must be within size limit (H5 fix)."""
        reg, db = _make_registry()
        huge_constraints = {f"key_{i}": "x" * 100 for i in range(100)}
        cred = reg.issue_credential(
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier="standard",
            allowed_schemas=["*"],
            constraints=huge_constraints,
        )
        assert cred is None

    def test_issue_credential_is_frozen(self):
        """Issued credential should be immutable (C4 fix)."""
        reg, db = _make_registry()
        cred = reg.issue_credential(
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier="standard",
            allowed_schemas=["hive:fee-policy/*"],
            constraints={"max_fee_ppm": 1000},
        )
        assert cred is not None
        with pytest.raises(AttributeError):
            cred.tier = "admin"

    def test_issue_row_cap(self):
        reg, db = _make_registry()
        # Fill to cap
        for i in range(MAX_MANAGEMENT_CREDENTIALS):
            db.credentials[f"cred-{i}"] = {"credential_id": f"cred-{i}"}
        cred = reg.issue_credential(
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier="standard",
            allowed_schemas=["*"],
            constraints={},
        )
        assert cred is None


# =============================================================================
# Credential Revocation Tests
# =============================================================================

class TestCredentialRevocation:
    def test_revoke_credential(self):
        reg, db = _make_registry()
        cred = reg.issue_credential(
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier="standard",
            allowed_schemas=["*"],
            constraints={},
        )
        assert cred is not None
        success = reg.revoke_credential(cred.credential_id)
        assert success
        stored = db.credentials[cred.credential_id]
        assert stored["revoked_at"] is not None

    def test_revoke_nonexistent(self):
        reg, db = _make_registry()
        success = reg.revoke_credential("nonexistent-id")
        assert not success

    def test_revoke_not_issuer(self):
        reg, db = _make_registry(our_pubkey=ALICE_PUBKEY)
        # Manually store a credential with different issuer
        db.credentials["foreign-cred"] = {
            "credential_id": "foreign-cred",
            "issuer_id": CHARLIE_PUBKEY,
            "revoked_at": None,
        }
        success = reg.revoke_credential("foreign-cred")
        assert not success

    def test_revoke_already_revoked(self):
        reg, db = _make_registry()
        cred = reg.issue_credential(
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier="standard",
            allowed_schemas=["*"],
            constraints={},
        )
        reg.revoke_credential(cred.credential_id)
        # Second revoke should fail
        success = reg.revoke_credential(cred.credential_id)
        assert not success


# =============================================================================
# Credential List Tests
# =============================================================================

class TestCredentialList:
    def test_list_all(self):
        reg, db = _make_registry()
        reg.issue_credential(BOB_PUBKEY, ALICE_PUBKEY, "standard", ["*"], {})
        reg.issue_credential(CHARLIE_PUBKEY, ALICE_PUBKEY, "monitor", ["hive:monitor/*"], {})
        creds = reg.list_credentials()
        assert len(creds) == 2

    def test_list_by_agent(self):
        reg, db = _make_registry()
        reg.issue_credential(BOB_PUBKEY, ALICE_PUBKEY, "standard", ["*"], {})
        reg.issue_credential(CHARLIE_PUBKEY, ALICE_PUBKEY, "monitor", ["hive:monitor/*"], {})
        creds = reg.list_credentials(agent_id=BOB_PUBKEY)
        assert len(creds) == 1
        assert creds[0]["agent_id"] == BOB_PUBKEY

    def test_list_by_node(self):
        reg, db = _make_registry()
        reg.issue_credential(BOB_PUBKEY, ALICE_PUBKEY, "standard", ["*"], {})
        creds = reg.list_credentials(node_id=ALICE_PUBKEY)
        assert len(creds) == 1


# =============================================================================
# Receipt Recording Tests
# =============================================================================

class TestReceiptRecording:
    def test_record_receipt(self):
        reg, db = _make_registry()
        cred = reg.issue_credential(BOB_PUBKEY, ALICE_PUBKEY, "standard", ["*"], {})
        receipt_id = reg.record_receipt(
            credential_id=cred.credential_id,
            schema_id="hive:fee-policy/v1",
            action="set_single",
            params={"channel_id": "abc", "fee_ppm": 50},
            result={"success": True},
        )
        assert receipt_id is not None
        assert len(db.receipts) == 1
        receipt = db.receipts[receipt_id]
        assert receipt["schema_id"] == "hive:fee-policy/v1"
        assert receipt["danger_score"] == 2  # set_single max dimension

    def test_record_receipt_unknown_action(self):
        reg, db = _make_registry()
        receipt_id = reg.record_receipt(
            credential_id="cred-123",
            schema_id="hive:nonexistent/v1",
            action="x",
            params={},
        )
        assert receipt_id is None

    def test_record_receipt_no_rpc(self):
        """Receipt recording refuses to store unsigned receipts when RPC is None."""
        db = MockDatabase()
        plugin = MagicMock()
        reg = ManagementSchemaRegistry(db, plugin, rpc=None, our_pubkey=ALICE_PUBKEY)
        # Pre-populate a credential so the existence check passes
        db.credentials["cred-123"] = {
            "credential_id": "cred-123",
            "issuer_id": ALICE_PUBKEY,
            "agent_id": BOB_PUBKEY,
            "node_id": ALICE_PUBKEY,
            "tier": "monitor",
            "allowed_schemas_json": '["*"]',
            "constraints_json": "{}",
            "valid_from": int(time.time()),
            "valid_until": int(time.time()) + 86400,
            "signature": "fakesig",
            "revoked_at": None,
            "created_at": int(time.time()),
        }
        # Without RPC, receipt recording should return None (refuse unsigned)
        receipt_id = reg.record_receipt(
            credential_id="cred-123",
            schema_id="hive:monitor/v1",
            action="get_info",
            params={"format": "json"},
        )
        assert receipt_id is None

    def test_receipt_with_state_hashes(self):
        reg, db = _make_registry()
        # Pre-populate a credential so the existence check passes
        db.credentials["cred-123"] = {
            "credential_id": "cred-123",
            "issuer_id": ALICE_PUBKEY,
            "agent_id": BOB_PUBKEY,
            "node_id": ALICE_PUBKEY,
            "tier": "standard",
            "allowed_schemas_json": '["*"]',
            "constraints_json": "{}",
            "valid_from": int(time.time()),
            "valid_until": int(time.time()) + 86400,
            "signature": "fakesig",
            "revoked_at": None,
            "created_at": int(time.time()),
        }
        receipt_id = reg.record_receipt(
            credential_id="cred-123",
            schema_id="hive:fee-policy/v1",
            action="set_single",
            params={"channel_id": "abc"},
            state_hash_before="abc123",
            state_hash_after="def456",
        )
        assert receipt_id is not None
        receipt = db.receipts[receipt_id]
        assert receipt["state_hash_before"] == "abc123"
        assert receipt["state_hash_after"] == "def456"


# =============================================================================
# Signing Payload Tests
# =============================================================================

class TestSigningPayload:
    def test_deterministic(self):
        cred = {
            "credential_id": "test-cred-123",
            "issuer_id": ALICE_PUBKEY,
            "agent_id": BOB_PUBKEY,
            "node_id": ALICE_PUBKEY,
            "tier": "standard",
            "allowed_schemas": ["hive:fee-policy/*"],
            "constraints": {"max_fee_ppm": 1000},
            "valid_from": 1000000,
            "valid_until": 2000000,
        }
        p1 = get_credential_signing_payload(cred)
        p2 = get_credential_signing_payload(cred)
        assert p1 == p2

    def test_includes_credential_id(self):
        """Signing payload must include credential_id (M3 fix)."""
        cred = {
            "credential_id": "unique-id-abc",
            "issuer_id": ALICE_PUBKEY,
            "agent_id": BOB_PUBKEY,
            "node_id": ALICE_PUBKEY,
            "tier": "standard",
            "allowed_schemas": ["*"],
            "constraints": {},
            "valid_from": 1000000,
            "valid_until": 2000000,
        }
        payload = get_credential_signing_payload(cred)
        parsed = json.loads(payload)
        assert "credential_id" in parsed
        assert parsed["credential_id"] == "unique-id-abc"

    def test_different_fields_different_payload(self):
        cred1 = {
            "credential_id": "cred-1",
            "issuer_id": ALICE_PUBKEY,
            "agent_id": BOB_PUBKEY,
            "node_id": ALICE_PUBKEY,
            "tier": "standard",
            "allowed_schemas": ["*"],
            "constraints": {},
            "valid_from": 1000000,
            "valid_until": 2000000,
        }
        cred2 = dict(cred1)
        cred2["tier"] = "admin"
        assert get_credential_signing_payload(cred1) != get_credential_signing_payload(cred2)

    def test_sorted_keys(self):
        payload = get_credential_signing_payload({
            "credential_id": "cred-123",
            "valid_until": 2000000,
            "valid_from": 1000000,
            "tier": "standard",
            "node_id": ALICE_PUBKEY,
            "issuer_id": ALICE_PUBKEY,
            "constraints": {},
            "allowed_schemas": ["*"],
            "agent_id": BOB_PUBKEY,
        })
        parsed = json.loads(payload)
        keys = list(parsed.keys())
        assert keys == sorted(keys)


# =============================================================================
# ManagementCredential Dataclass Tests
# =============================================================================

class TestManagementCredential:
    def test_to_dict(self):
        now = int(time.time())
        cred = ManagementCredential(
            credential_id="test-id",
            issuer_id=ALICE_PUBKEY,
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier="standard",
            allowed_schemas=("hive:fee-policy/*",),
            constraints='{"max_fee_ppm": 1000}',
            valid_from=now,
            valid_until=now + 86400,
            signature="sig123",
        )
        d = cred.to_dict()
        assert d["credential_id"] == "test-id"
        assert d["tier"] == "standard"
        assert d["revoked_at"] is None
        assert d["allowed_schemas"] == ["hive:fee-policy/*"]
        assert d["constraints"] == {"max_fee_ppm": 1000}

    def test_frozen_immutable(self):
        """ManagementCredential should be frozen (C4 fix)."""
        now = int(time.time())
        cred = ManagementCredential(
            credential_id="test-id",
            issuer_id=ALICE_PUBKEY,
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier="standard",
            allowed_schemas=("*",),
            constraints="{}",
            valid_from=now,
            valid_until=now + 86400,
            signature="sig123",
        )
        with pytest.raises(AttributeError):
            cred.signature = "tampered"


# =============================================================================
# RPC Handler Tests
# =============================================================================

class TestRPCHandlers:
    """Test the RPC handler functions from rpc_commands.py."""

    def _make_context(self):
        reg, db = _make_registry()
        from modules.rpc_commands import HiveContext
        ctx = MagicMock(spec=HiveContext)
        ctx.management_schema_registry = reg
        ctx.our_pubkey = ALICE_PUBKEY
        # Provide database mock so check_permission succeeds
        ctx.database = MagicMock()
        ctx.database.get_member.return_value = {"peer_id": ALICE_PUBKEY, "tier": "member"}
        return ctx, reg, db

    def test_schema_list_handler(self):
        from modules.rpc_commands import schema_list
        ctx, _, _ = self._make_context()
        result = schema_list(ctx)
        assert "schemas" in result
        assert result["count"] == 15

    def test_schema_validate_handler(self):
        from modules.rpc_commands import schema_validate
        ctx, _, _ = self._make_context()
        result = schema_validate(ctx, "hive:fee-policy/v1", "set_single")
        assert result["valid"]
        assert "danger" in result

    def test_schema_validate_invalid(self):
        from modules.rpc_commands import schema_validate
        ctx, _, _ = self._make_context()
        result = schema_validate(ctx, "hive:nonexistent/v1", "x")
        assert not result["valid"]

    def test_mgmt_credential_issue_handler(self):
        from modules.rpc_commands import mgmt_credential_issue
        ctx, _, _ = self._make_context()
        result = mgmt_credential_issue(
            ctx, BOB_PUBKEY, "standard",
            json.dumps(["hive:fee-policy/*"]),
        )
        assert "credential" in result
        assert result["credential"]["tier"] == "standard"

    def test_mgmt_credential_issue_invalid_json(self):
        from modules.rpc_commands import mgmt_credential_issue
        ctx, _, _ = self._make_context()
        result = mgmt_credential_issue(ctx, BOB_PUBKEY, "standard", "not-json")
        assert "error" in result

    def test_mgmt_credential_list_handler(self):
        from modules.rpc_commands import mgmt_credential_list, mgmt_credential_issue
        ctx, _, _ = self._make_context()
        mgmt_credential_issue(ctx, BOB_PUBKEY, "standard", json.dumps(["*"]))
        result = mgmt_credential_list(ctx)
        assert result["count"] == 1

    def test_mgmt_credential_revoke_handler(self):
        from modules.rpc_commands import mgmt_credential_revoke, mgmt_credential_issue
        ctx, _, _ = self._make_context()
        issued = mgmt_credential_issue(ctx, BOB_PUBKEY, "standard", json.dumps(["*"]))
        cred_id = issued["credential"]["credential_id"]
        result = mgmt_credential_revoke(ctx, cred_id)
        assert result["revoked"]

    def test_handlers_no_registry(self):
        from modules.rpc_commands import schema_list, schema_validate
        ctx = MagicMock()
        ctx.management_schema_registry = None
        result = schema_list(ctx)
        assert "error" in result
        result = schema_validate(ctx, "x", "y")
        assert "error" in result

    def test_schema_validate_params_json_not_dict(self):
        """params_json that decodes to non-dict should be rejected (P2-M-2)."""
        from modules.rpc_commands import schema_validate
        ctx, _, _ = self._make_context()
        # JSON list instead of object
        result = schema_validate(ctx, "hive:fee-policy/v1", "set_single",
                                  params_json='["not", "a", "dict"]')
        assert "error" in result
        assert "object" in result["error"]

    def test_schema_validate_params_json_string(self):
        """params_json that decodes to a string should be rejected (P2-M-2)."""
        from modules.rpc_commands import schema_validate
        ctx, _, _ = self._make_context()
        result = schema_validate(ctx, "hive:fee-policy/v1", "set_single",
                                  params_json='"just a string"')
        assert "error" in result
        assert "object" in result["error"]


# =============================================================================
# Gossip Protocol Handler Tests (P2-L-4)
# =============================================================================

class TestGossipHandlers:
    """Test the gossip/protocol handlers in management_schemas.py."""

    def _make_valid_credential_payload(self, issuer_id=ALICE_PUBKEY,
                                        agent_id=BOB_PUBKEY,
                                        node_id=ALICE_PUBKEY):
        """Build a valid MGMT_CREDENTIAL_PRESENT payload."""
        now = int(time.time())
        return {
            "credential": {
                "credential_id": str(uuid.uuid4()),
                "issuer_id": issuer_id,
                "agent_id": agent_id,
                "node_id": node_id,
                "tier": "standard",
                "allowed_schemas": ["hive:fee-policy/*"],
                "constraints": {"max_fee_ppm": 1000},
                "valid_from": now - 3600,
                "valid_until": now + 86400,
                "signature": "valid_signature_zbase32",
            }
        }

    def _make_registry_with_checkmessage(self, our_pubkey=CHARLIE_PUBKEY):
        """Create a registry with RPC that passes checkmessage verification."""
        db = MockDatabase()
        plugin = MagicMock()
        rpc = MagicMock()
        rpc.signmessage.return_value = {"zbase": "fakesig123"}
        registry = ManagementSchemaRegistry(
            database=db,
            plugin=plugin,
            rpc=rpc,
            our_pubkey=our_pubkey,
        )
        return registry, db, rpc

    def test_valid_credential_gossip_accepted(self):
        """A properly formed and signed credential should be accepted."""
        reg, db, rpc = self._make_registry_with_checkmessage()
        payload = self._make_valid_credential_payload()
        issuer_id = payload["credential"]["issuer_id"]

        # Mock checkmessage to return verified
        rpc.checkmessage.return_value = {"verified": True, "pubkey": issuer_id}

        result = reg.handle_mgmt_credential_present(BOB_PUBKEY, payload)
        assert result is True
        assert len(db.credentials) == 1

    def test_reject_invalid_agent_id_pubkey(self):
        """Credentials with invalid agent_id pubkey should be rejected (P2-M-3)."""
        reg, db, rpc = self._make_registry_with_checkmessage()
        payload = self._make_valid_credential_payload(agent_id="not_a_valid_pubkey")

        result = reg.handle_mgmt_credential_present(BOB_PUBKEY, payload)
        assert result is False
        assert len(db.credentials) == 0

    def test_reject_invalid_node_id_pubkey(self):
        """Credentials with invalid node_id pubkey should be rejected (P2-M-3)."""
        reg, db, rpc = self._make_registry_with_checkmessage()
        payload = self._make_valid_credential_payload(node_id="04" + "aa" * 32)

        result = reg.handle_mgmt_credential_present(BOB_PUBKEY, payload)
        assert result is False
        assert len(db.credentials) == 0

    def test_reject_invalid_issuer_id_pubkey(self):
        """Credentials with invalid issuer_id pubkey should be rejected (P2-M-3)."""
        reg, db, rpc = self._make_registry_with_checkmessage()
        payload = self._make_valid_credential_payload(issuer_id="short")

        result = reg.handle_mgmt_credential_present(BOB_PUBKEY, payload)
        assert result is False
        assert len(db.credentials) == 0

    def test_reject_oversized_allowed_schemas(self):
        """allowed_schemas with >100 entries should be rejected (P2-L-1)."""
        reg, db, rpc = self._make_registry_with_checkmessage()
        payload = self._make_valid_credential_payload()
        payload["credential"]["allowed_schemas"] = [f"hive:schema-{i}/v1" for i in range(101)]

        result = reg.handle_mgmt_credential_present(BOB_PUBKEY, payload)
        assert result is False
        assert len(db.credentials) == 0

    def test_reject_oversized_constraints(self):
        """constraints with >50 keys should be rejected (P2-L-1)."""
        reg, db, rpc = self._make_registry_with_checkmessage()
        payload = self._make_valid_credential_payload()
        payload["credential"]["constraints"] = {f"key_{i}": i for i in range(51)}

        result = reg.handle_mgmt_credential_present(BOB_PUBKEY, payload)
        assert result is False
        assert len(db.credentials) == 0

    def test_reject_non_string_allowed_schemas_entries(self):
        """allowed_schemas containing non-string entries should be rejected (P2-L-2)."""
        reg, db, rpc = self._make_registry_with_checkmessage()
        payload = self._make_valid_credential_payload()
        payload["credential"]["allowed_schemas"] = ["hive:fee-policy/*", 42, True]

        result = reg.handle_mgmt_credential_present(BOB_PUBKEY, payload)
        assert result is False
        assert len(db.credentials) == 0

    def test_reject_long_credential_id(self):
        """credential_id longer than 128 chars should be rejected (P2-L-3)."""
        reg, db, rpc = self._make_registry_with_checkmessage()
        payload = self._make_valid_credential_payload()
        payload["credential"]["credential_id"] = "x" * 129

        result = reg.handle_mgmt_credential_present(BOB_PUBKEY, payload)
        assert result is False
        assert len(db.credentials) == 0

    def test_reject_long_credential_id_in_revoke(self):
        """credential_id longer than 128 chars should be rejected in revoke (P2-L-3)."""
        reg, db, rpc = self._make_registry_with_checkmessage()
        payload = {
            "credential_id": "x" * 129,
            "reason": "test revocation",
            "issuer_id": ALICE_PUBKEY,
            "signature": "fakesig",
        }
        result = reg.handle_mgmt_credential_revoke(BOB_PUBKEY, payload)
        assert result is False

    def test_exactly_100_allowed_schemas_accepted(self):
        """Exactly 100 allowed_schemas should be accepted."""
        reg, db, rpc = self._make_registry_with_checkmessage()
        payload = self._make_valid_credential_payload()
        payload["credential"]["allowed_schemas"] = [f"hive:schema-{i}/v1" for i in range(100)]
        issuer_id = payload["credential"]["issuer_id"]
        rpc.checkmessage.return_value = {"verified": True, "pubkey": issuer_id}

        result = reg.handle_mgmt_credential_present(BOB_PUBKEY, payload)
        assert result is True

    def test_exactly_50_constraints_accepted(self):
        """Exactly 50 constraint keys should be accepted."""
        reg, db, rpc = self._make_registry_with_checkmessage()
        payload = self._make_valid_credential_payload()
        payload["credential"]["constraints"] = {f"key_{i}": i for i in range(50)}
        issuer_id = payload["credential"]["issuer_id"]
        rpc.checkmessage.return_value = {"verified": True, "pubkey": issuer_id}

        result = reg.handle_mgmt_credential_present(BOB_PUBKEY, payload)
        assert result is True


# =============================================================================
# Valid Days > 730 Rejection Test (P2-L-5)
# =============================================================================

class TestValidDaysLimit:
    """Test that credentials with valid_days > 730 are rejected."""

    def test_issue_rejects_valid_days_over_730(self):
        """valid_days > 730 (2 years) should be rejected (P2-L-5)."""
        reg, db = _make_registry()
        cred = reg.issue_credential(
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier="standard",
            allowed_schemas=["*"],
            constraints={},
            valid_days=731,
        )
        assert cred is None
        assert len(db.credentials) == 0

    def test_issue_accepts_valid_days_exactly_730(self):
        """valid_days == 730 should be accepted."""
        reg, db = _make_registry()
        cred = reg.issue_credential(
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier="standard",
            allowed_schemas=["*"],
            constraints={},
            valid_days=730,
        )
        assert cred is not None
        assert cred.valid_until - cred.valid_from == 730 * 86400

    def test_issue_rejects_valid_days_very_large(self):
        """Extremely large valid_days should be rejected."""
        reg, db = _make_registry()
        cred = reg.issue_credential(
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier="standard",
            allowed_schemas=["*"],
            constraints={},
            valid_days=10000,
        )
        assert cred is None


# =============================================================================
# Receipt Signing Malformed Response Test (P2-M-1)
# =============================================================================

class TestReceiptSigningMalformed:
    """Test that malformed HSM responses don't produce empty-signature receipts."""

    def test_receipt_rejects_empty_signature_from_malformed_response(self):
        """If signmessage returns malformed response with no 'zbase', reject (P2-M-1)."""
        reg, db = _make_registry()
        # Issue a credential first
        cred = reg.issue_credential(
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier="standard",
            allowed_schemas=["*"],
            constraints={},
        )
        assert cred is not None

        # Now make signmessage return a malformed response (no 'zbase' key)
        reg.rpc.signmessage.return_value = {"unexpected_key": "value"}

        receipt_id = reg.record_receipt(
            credential_id=cred.credential_id,
            schema_id="hive:fee-policy/v1",
            action="set_single",
            params={"channel_id": "abc", "fee_ppm": 50},
        )
        assert receipt_id is None
        assert len(db.receipts) == 0

    def test_receipt_rejects_none_signature(self):
        """If signmessage returns dict with zbase=None, reject."""
        reg, db = _make_registry()
        cred = reg.issue_credential(
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier="standard",
            allowed_schemas=["*"],
            constraints={},
        )
        assert cred is not None

        reg.rpc.signmessage.return_value = {"zbase": None}

        receipt_id = reg.record_receipt(
            credential_id=cred.credential_id,
            schema_id="hive:fee-policy/v1",
            action="set_single",
            params={"channel_id": "abc", "fee_ppm": 50},
        )
        assert receipt_id is None

    def test_receipt_accepts_valid_signature(self):
        """Normal signmessage response with valid zbase should succeed."""
        reg, db = _make_registry()
        cred = reg.issue_credential(
            agent_id=BOB_PUBKEY,
            node_id=ALICE_PUBKEY,
            tier="standard",
            allowed_schemas=["*"],
            constraints={},
        )
        assert cred is not None

        # signmessage still returns valid signature from _make_registry setup
        receipt_id = reg.record_receipt(
            credential_id=cred.credential_id,
            schema_id="hive:fee-policy/v1",
            action="set_single",
            params={"channel_id": "abc", "fee_ppm": 50},
        )
        assert receipt_id is not None
        assert len(db.receipts) == 1
