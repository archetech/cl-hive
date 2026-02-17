"""
Management Schema Module (Phase 2 - DID Ecosystem)

Implements the 15 management schema categories with danger scoring engine
and schema-based command validation. This is the framework that management
credentials and future escrow will use.

Responsibilities:
- Schema registry with 15 categories of node management operations
- Danger scoring engine (5 dimensions, each 1-10)
- Command validation against schema definitions
- Management credential data model (operator → agent permission)
- Pricing calculation based on danger score and reputation tier

Security:
- Management credentials signed via CLN signmessage (zbase32)
- Danger scores are pre-computed and immutable per action
- Higher danger actions require higher permission tiers
- All management actions produce signed receipts
"""

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# --- Constants ---

MAX_MANAGEMENT_CREDENTIALS = 1_000
MAX_MANAGEMENT_RECEIPTS = 100_000
MAX_ALLOWED_SCHEMAS_LEN = 4096
MAX_CONSTRAINTS_LEN = 4096
MAX_MGMT_CREDENTIAL_PRESENTS_PER_PEER_PER_HOUR = 20
MAX_MGMT_CREDENTIAL_REVOKES_PER_PEER_PER_HOUR = 10

VALID_TIERS = frozenset(["monitor", "standard", "advanced", "admin"])

# Base pricing per danger point (sats) — used for future escrow integration
BASE_PRICE_PER_DANGER_POINT = 100

# Reputation discount factors
TIER_PRICING_MULTIPLIERS = {
    "newcomer": 1.5,
    "recognized": 1.0,
    "trusted": 0.8,
    "senior": 0.6,
}


# --- Dataclasses ---

@dataclass(frozen=True)
class DangerScore:
    """
    Multi-dimensional danger assessment for a management action.

    Each dimension is scored 1-10:
    - 1 = minimal risk
    - 10 = maximum risk

    The overall danger score is the max of all dimensions (not the sum),
    because a single catastrophic dimension makes the action dangerous
    regardless of how safe the other dimensions are.
    """
    reversibility: int       # 1=instant undo, 10=irreversible
    financial_exposure: int  # 1=0 sats, 10=>10M sats at risk
    time_sensitivity: int    # 1=no compounding, 10=permanent damage
    blast_radius: int        # 1=single metric, 10=entire fleet
    recovery_difficulty: int  # 1=trivial, 10=unrecoverable

    def __post_init__(self):
        for field_name in ['reversibility', 'financial_exposure', 'time_sensitivity', 'blast_radius', 'recovery_difficulty']:
            val = getattr(self, field_name)
            if not isinstance(val, int) or val < 1 or val > 10:
                raise ValueError(f"DangerScore.{field_name} must be int in [1, 10], got {val}")

    @property
    def total(self) -> int:
        """Overall danger score (max of dimensions)."""
        return max(self.reversibility, self.financial_exposure,
                   self.time_sensitivity, self.blast_radius,
                   self.recovery_difficulty)

    def to_dict(self) -> Dict[str, int]:
        return {
            "reversibility": self.reversibility,
            "financial_exposure": self.financial_exposure,
            "time_sensitivity": self.time_sensitivity,
            "blast_radius": self.blast_radius,
            "recovery_difficulty": self.recovery_difficulty,
            "total": self.total,
        }


@dataclass(frozen=True)
class SchemaAction:
    """Definition of a single action within a management schema."""
    danger: DangerScore
    required_tier: str          # monitor/standard/advanced/admin
    description: str = ""
    parameters: Dict[str, type] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "danger": self.danger.to_dict(),
            "required_tier": self.required_tier,
            "description": self.description,
            "parameters": {k: v.__name__ for k, v in self.parameters.items()},
        }


@dataclass(frozen=True)
class SchemaCategory:
    """Definition of a management schema category."""
    schema_id: str
    name: str
    description: str
    danger_range: Tuple[int, int]  # (min, max) danger across actions
    actions: Dict[str, SchemaAction]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_id": self.schema_id,
            "name": self.name,
            "description": self.description,
            "danger_range": list(self.danger_range),
            "actions": {k: v.to_dict() for k, v in self.actions.items()},
            "action_count": len(self.actions),
        }


@dataclass(frozen=True)
class ManagementCredential:
    """
    HiveManagementCredential — operator grants agent permission to manage.

    Data model only in Phase 2 — no L402/Cashu payment gating yet.
    Frozen to prevent post-issuance mutation of signed fields.
    """
    credential_id: str
    issuer_id: str          # node operator pubkey
    agent_id: str           # agent/advisor pubkey
    node_id: str            # managed node pubkey
    tier: str               # monitor/standard/advanced/admin
    allowed_schemas: tuple  # e.g. ("hive:fee-policy/*", "hive:monitor/*")
    # NOTE: constraints are advisory metadata, not enforced at authorization time
    constraints: str        # JSON string of constraints (frozen-compatible)
    valid_from: int         # epoch
    valid_until: int        # epoch
    signature: str = ""     # operator's HSM signature
    revoked_at: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        constraints = self.constraints
        if isinstance(constraints, str):
            try:
                constraints = json.loads(constraints)
            except (json.JSONDecodeError, TypeError):
                constraints = {}
        return {
            "credential_id": self.credential_id,
            "issuer_id": self.issuer_id,
            "agent_id": self.agent_id,
            "node_id": self.node_id,
            "tier": self.tier,
            "allowed_schemas": list(self.allowed_schemas),
            "constraints": constraints,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "signature": self.signature,
            "revoked_at": self.revoked_at,
        }


@dataclass
class ManagementReceipt:
    """Signed receipt of a management action execution."""
    receipt_id: str
    credential_id: str
    schema_id: str
    action: str
    params: Dict[str, Any]
    danger_score: int
    result: Optional[Dict[str, Any]] = None
    state_hash_before: Optional[str] = None
    state_hash_after: Optional[str] = None
    executed_at: int = 0
    executor_signature: str = ""


# --- Schema Definitions (15 categories) ---

SCHEMA_REGISTRY: Dict[str, SchemaCategory] = {
    "hive:monitor/v1": SchemaCategory(
        schema_id="hive:monitor/v1",
        name="Monitoring & Read-Only",
        description="Read-only operations: node status, channel info, routing stats",
        danger_range=(1, 2),
        actions={
            "get_info": SchemaAction(
                danger=DangerScore(1, 1, 1, 1, 1),
                required_tier="monitor",
                description="Get node info (getinfo)",
                parameters={"format": str},
            ),
            "list_channels": SchemaAction(
                danger=DangerScore(1, 1, 1, 1, 1),
                required_tier="monitor",
                description="List channels with balances",
            ),
            "list_forwards": SchemaAction(
                danger=DangerScore(1, 1, 1, 1, 1),
                required_tier="monitor",
                description="List forwarding history",
                parameters={"status": str, "limit": int},
            ),
            "get_balance": SchemaAction(
                danger=DangerScore(1, 1, 1, 1, 1),
                required_tier="monitor",
                description="Get on-chain and channel balances",
            ),
            "list_peers": SchemaAction(
                danger=DangerScore(1, 1, 1, 1, 1),
                required_tier="monitor",
                description="List connected peers",
            ),
        },
    ),
    "hive:fee-policy/v1": SchemaCategory(
        schema_id="hive:fee-policy/v1",
        name="Fee Management",
        description="Set and adjust channel fee policies",
        danger_range=(2, 5),
        actions={
            "set_single": SchemaAction(
                danger=DangerScore(2, 2, 2, 1, 1),
                required_tier="standard",
                description="Set fee on a single channel",
                parameters={"channel_id": str, "base_msat": int, "fee_ppm": int},
            ),
            "set_bulk": SchemaAction(
                danger=DangerScore(3, 4, 3, 5, 2),
                required_tier="advanced",
                description="Set fees on multiple channels at once",
                parameters={"channels": list, "policy": dict},
            ),
            "set_anchor": SchemaAction(
                danger=DangerScore(2, 2, 2, 1, 1),
                required_tier="standard",
                description="Set anchor fee rate for a channel",
                parameters={"channel_id": str, "target_fee_ppm": int, "reason": str},
            ),
        },
    ),
    "hive:htlc-policy/v1": SchemaCategory(
        schema_id="hive:htlc-policy/v1",
        name="HTLC Policy",
        description="Configure HTLC size limits and CLTV deltas",
        danger_range=(2, 5),
        actions={
            "set_htlc_limits": SchemaAction(
                danger=DangerScore(3, 3, 2, 2, 2),
                required_tier="standard",
                description="Set min/max HTLC size for a channel",
                parameters={"channel_id": str, "htlc_minimum_msat": int, "htlc_maximum_msat": int},
            ),
            "set_cltv_delta": SchemaAction(
                danger=DangerScore(3, 2, 4, 2, 3),
                required_tier="standard",
                description="Set CLTV expiry delta",
                parameters={"channel_id": str, "cltv_expiry_delta": int},
            ),
        },
    ),
    "hive:forwarding/v1": SchemaCategory(
        schema_id="hive:forwarding/v1",
        name="Forwarding Policy",
        description="Control forwarding behavior and routing hints",
        danger_range=(2, 6),
        actions={
            "disable_channel": SchemaAction(
                danger=DangerScore(4, 3, 4, 2, 2),
                required_tier="standard",
                description="Disable forwarding on a channel",
                parameters={"channel_id": str, "reason": str},
            ),
            "enable_channel": SchemaAction(
                danger=DangerScore(2, 1, 1, 1, 1),
                required_tier="standard",
                description="Re-enable forwarding on a channel",
                parameters={"channel_id": str},
            ),
            "set_routing_hints": SchemaAction(
                danger=DangerScore(3, 2, 3, 3, 2),
                required_tier="advanced",
                description="Set routing hints for invoice generation",
                parameters={"hints": list},
            ),
        },
    ),
    "hive:rebalance/v1": SchemaCategory(
        schema_id="hive:rebalance/v1",
        name="Liquidity Management",
        description="Rebalancing operations and liquidity movement",
        danger_range=(3, 6),
        actions={
            "circular_rebalance": SchemaAction(
                danger=DangerScore(4, 5, 3, 2, 3),
                required_tier="advanced",
                description="Circular rebalance between channels",
                parameters={"from_channel": str, "to_channel": str, "amount_sats": int, "max_fee_ppm": int},
            ),
            "swap_out": SchemaAction(
                danger=DangerScore(5, 6, 3, 2, 4),
                required_tier="advanced",
                description="Swap Lightning to on-chain (loop out)",
                parameters={"amount_sats": int, "address": str},
            ),
            "swap_in": SchemaAction(
                danger=DangerScore(4, 5, 3, 2, 3),
                required_tier="advanced",
                description="Swap on-chain to Lightning (loop in)",
                parameters={"amount_sats": int},
            ),
        },
    ),
    "hive:channel/v1": SchemaCategory(
        schema_id="hive:channel/v1",
        name="Channel Lifecycle",
        description="Open and close Lightning channels",
        danger_range=(5, 10),
        actions={
            "open": SchemaAction(
                danger=DangerScore(7, 8, 5, 3, 6),
                required_tier="advanced",
                description="Open a new channel",
                parameters={"peer_id": str, "amount_sats": int, "push_msat": int},
            ),
            "close_cooperative": SchemaAction(
                danger=DangerScore(6, 7, 4, 2, 5),
                required_tier="advanced",
                description="Cooperatively close a channel",
                parameters={"channel_id": str, "destination": str},
            ),
            "close_force": SchemaAction(
                danger=DangerScore(9, 9, 8, 3, 8),
                required_tier="admin",
                description="Force close a channel (last resort)",
                parameters={"channel_id": str},
            ),
            "close_all": SchemaAction(
                danger=DangerScore(10, 10, 9, 10, 9),
                required_tier="admin",
                description="Close all channels (emergency only)",
                parameters={"destination": str},
            ),
        },
    ),
    "hive:splice/v1": SchemaCategory(
        schema_id="hive:splice/v1",
        name="Splicing",
        description="Splice in/out to resize channels without closing",
        danger_range=(5, 7),
        actions={
            "splice_in": SchemaAction(
                danger=DangerScore(5, 6, 4, 2, 4),
                required_tier="advanced",
                description="Splice in (add funds to channel)",
                parameters={"channel_id": str, "amount_sats": int},
            ),
            "splice_out": SchemaAction(
                danger=DangerScore(6, 7, 4, 2, 5),
                required_tier="advanced",
                description="Splice out (remove funds from channel)",
                parameters={"channel_id": str, "amount_sats": int, "destination": str},
            ),
        },
    ),
    "hive:peer/v1": SchemaCategory(
        schema_id="hive:peer/v1",
        name="Peer Management",
        description="Connect/disconnect peers",
        danger_range=(2, 5),
        actions={
            "connect": SchemaAction(
                danger=DangerScore(2, 1, 1, 1, 1),
                required_tier="standard",
                description="Connect to a peer",
                parameters={"peer_id": str, "host": str, "port": int},
            ),
            "disconnect": SchemaAction(
                danger=DangerScore(3, 2, 3, 2, 2),
                required_tier="standard",
                description="Disconnect from a peer",
                parameters={"peer_id": str},
            ),
        },
    ),
    "hive:payment/v1": SchemaCategory(
        schema_id="hive:payment/v1",
        name="Payments & Invoicing",
        description="Create invoices and send payments",
        danger_range=(1, 6),
        actions={
            "create_invoice": SchemaAction(
                danger=DangerScore(1, 1, 1, 1, 1),
                required_tier="monitor",
                description="Create a Lightning invoice",
                parameters={"amount_msat": int, "label": str, "description": str},
            ),
            "pay": SchemaAction(
                danger=DangerScore(5, 6, 3, 1, 4),
                required_tier="advanced",
                description="Pay a Lightning invoice",
                parameters={"bolt11": str, "max_fee_ppm": int},
            ),
            "keysend": SchemaAction(
                danger=DangerScore(5, 6, 3, 1, 4),
                required_tier="advanced",
                description="Send a keysend payment",
                parameters={"destination": str, "amount_msat": int},
            ),
        },
    ),
    "hive:wallet/v1": SchemaCategory(
        schema_id="hive:wallet/v1",
        name="Wallet & On-Chain",
        description="On-chain wallet operations",
        danger_range=(1, 9),
        actions={
            "list_funds": SchemaAction(
                danger=DangerScore(1, 1, 1, 1, 1),
                required_tier="monitor",
                description="List on-chain and channel funds",
            ),
            "new_address": SchemaAction(
                danger=DangerScore(1, 1, 1, 1, 1),
                required_tier="standard",
                description="Generate a new on-chain address",
                parameters={"type": str},
            ),
            "withdraw": SchemaAction(
                danger=DangerScore(8, 9, 5, 1, 8),
                required_tier="admin",
                description="Withdraw on-chain funds to external address",
                parameters={"destination": str, "amount_sats": int, "feerate": str},
            ),
        },
    ),
    "hive:plugin/v1": SchemaCategory(
        schema_id="hive:plugin/v1",
        name="Plugin Management",
        description="Start/stop/list plugins",
        danger_range=(1, 9),
        actions={
            "list_plugins": SchemaAction(
                danger=DangerScore(1, 1, 1, 1, 1),
                required_tier="monitor",
                description="List installed plugins",
            ),
            "start_plugin": SchemaAction(
                danger=DangerScore(7, 5, 5, 7, 7),
                required_tier="admin",
                description="Start a plugin",
                parameters={"path": str},
            ),
            "stop_plugin": SchemaAction(
                danger=DangerScore(7, 5, 5, 7, 7),
                required_tier="admin",
                description="Stop a plugin",
                parameters={"plugin_name": str},
            ),
        },
    ),
    "hive:config/v1": SchemaCategory(
        schema_id="hive:config/v1",
        name="Node Configuration",
        description="Read and modify node configuration",
        danger_range=(1, 7),
        actions={
            "get_config": SchemaAction(
                danger=DangerScore(1, 1, 1, 1, 1),
                required_tier="monitor",
                description="Get current configuration values",
                parameters={"key": str},
            ),
            "set_config": SchemaAction(
                danger=DangerScore(5, 3, 5, 5, 5),
                required_tier="admin",
                description="Set a configuration value",
                parameters={"key": str, "value": str},
            ),
        },
    ),
    "hive:backup/v1": SchemaCategory(
        schema_id="hive:backup/v1",
        name="Backup Operations",
        description="Create and manage backups",
        danger_range=(1, 10),
        actions={
            "export_scb": SchemaAction(
                danger=DangerScore(1, 1, 1, 1, 1),
                required_tier="standard",
                description="Export static channel backup",
            ),
            "verify_backup": SchemaAction(
                danger=DangerScore(1, 1, 1, 1, 1),
                required_tier="monitor",
                description="Verify backup integrity",
                parameters={"backup_path": str},
            ),
            "restore": SchemaAction(
                danger=DangerScore(10, 10, 10, 10, 10),
                required_tier="admin",
                description="Restore from backup (DANGEROUS — triggers force-close of all channels)",
                parameters={"backup_path": str},
            ),
        },
    ),
    "hive:emergency/v1": SchemaCategory(
        schema_id="hive:emergency/v1",
        name="Emergency Operations",
        description="Emergency actions for node recovery",
        danger_range=(3, 10),
        actions={
            "stop_node": SchemaAction(
                danger=DangerScore(8, 6, 7, 3, 6),
                required_tier="admin",
                description="Gracefully stop the Lightning node",
            ),
            "emergency_close_all": SchemaAction(
                danger=DangerScore(10, 10, 9, 10, 9),
                required_tier="admin",
                description="Emergency close all channels and stop",
                parameters={"destination": str},
            ),
            "ban_peer": SchemaAction(
                danger=DangerScore(4, 3, 3, 2, 3),
                required_tier="advanced",
                description="Ban a malicious peer",
                parameters={"peer_id": str, "reason": str},
            ),
        },
    ),
    "hive:htlc-mgmt/v1": SchemaCategory(
        schema_id="hive:htlc-mgmt/v1",
        name="HTLC Management",
        description="Manage in-flight HTLCs",
        danger_range=(1, 8),
        actions={
            "list_htlcs": SchemaAction(
                danger=DangerScore(1, 1, 1, 1, 1),
                required_tier="monitor",
                description="List in-flight HTLCs",
            ),
            "settle_htlc": SchemaAction(
                danger=DangerScore(5, 6, 5, 2, 5),
                required_tier="advanced",
                description="Manually settle an HTLC",
                parameters={"htlc_id": str, "preimage": str},
            ),
            "fail_htlc": SchemaAction(
                danger=DangerScore(5, 6, 5, 2, 5),
                required_tier="advanced",
                description="Manually fail an HTLC",
                parameters={"htlc_id": str, "reason": str},
            ),
        },
    ),
}


# --- Tier hierarchy ---

TIER_HIERARCHY = {
    "monitor": 0,
    "standard": 1,
    "advanced": 2,
    "admin": 3,
}


# --- Helper Functions ---

def get_credential_signing_payload(credential: Dict[str, Any]) -> str:
    """Build deterministic JSON string for management credential signing."""
    signing_data = {
        "credential_id": credential.get("credential_id", ""),
        "issuer_id": credential.get("issuer_id", ""),
        "agent_id": credential.get("agent_id", ""),
        "node_id": credential.get("node_id", ""),
        "tier": credential.get("tier", ""),
        "allowed_schemas": credential.get("allowed_schemas", []),
        "constraints": credential.get("constraints", {}),
        "valid_from": credential.get("valid_from", 0),
        "valid_until": credential.get("valid_until", 0),
    }
    return json.dumps(signing_data, sort_keys=True, separators=(',', ':'))


def _schema_matches(pattern: str, schema_id: str) -> bool:
    """Check if a schema pattern matches a schema_id. Supports wildcard '*'."""
    if pattern == "*":
        return True
    if pattern.endswith("/*"):
        prefix = pattern[:-2]  # e.g. "hive:fee-policy" from "hive:fee-policy/*"
        # Require exact category match: prefix must be followed by "/" in schema_id
        return schema_id.startswith(prefix + "/")
    return pattern == schema_id


# --- Main Registry ---

class ManagementSchemaRegistry:
    """
    Registry of management schema categories with danger scoring.

    Provides command validation, danger assessment, tier enforcement,
    and management credential lifecycle management.
    """

    def __init__(self, database, plugin, rpc=None, our_pubkey=""):
        self.db = database
        self.plugin = plugin
        self.rpc = rpc
        self.our_pubkey = our_pubkey
        self._rate_limiters: Dict[tuple, List[int]] = {}
        self._rate_lock = threading.Lock()

    def _log(self, msg: str, level: str = "info"):
        try:
            self.plugin.log(f"cl-hive: management_schemas: {msg}", level=level)
        except Exception:
            pass

    def _check_rate_limit(self, peer_id: str, message_type: str, max_per_hour: int) -> bool:
        """Per-peer sliding-window rate limit."""
        now = int(time.time())
        cutoff = now - 3600
        key = (peer_id, message_type)

        with self._rate_lock:
            timestamps = self._rate_limiters.get(key, [])
            timestamps = [ts for ts in timestamps if ts > cutoff]
            if len(timestamps) >= max_per_hour:
                self._rate_limiters[key] = timestamps
                return False

            timestamps.append(now)
            self._rate_limiters[key] = timestamps

            if len(self._rate_limiters) > 1000:
                stale_keys = [
                    k for k, vals in self._rate_limiters.items()
                    if not vals or vals[-1] <= cutoff
                ]
                for k in stale_keys:
                    self._rate_limiters.pop(k, None)

        return True

    # --- Schema Queries ---

    def list_schemas(self) -> Dict[str, Dict[str, Any]]:
        """List all registered schemas with their actions."""
        return {sid: cat.to_dict() for sid, cat in SCHEMA_REGISTRY.items()}

    def get_schema(self, schema_id: str) -> Optional[SchemaCategory]:
        """Get a schema category by ID."""
        return SCHEMA_REGISTRY.get(schema_id)

    def get_action(self, schema_id: str, action: str) -> Optional[SchemaAction]:
        """Get a specific action within a schema."""
        cat = SCHEMA_REGISTRY.get(schema_id)
        if cat:
            return cat.actions.get(action)
        return None

    def get_danger_score(self, schema_id: str, action: str) -> Optional[DangerScore]:
        """Get the danger score for a specific schema action."""
        sa = self.get_action(schema_id, action)
        return sa.danger if sa else None

    def get_required_tier(self, schema_id: str, action: str) -> Optional[str]:
        """Get the required permission tier for a schema action."""
        sa = self.get_action(schema_id, action)
        return sa.required_tier if sa else None

    # --- Command Validation ---

    def validate_command(
        self, schema_id: str, action: str, params: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, str]:
        """
        Validate a command against its schema definition (dry run).

        Returns:
            (is_valid, reason) tuple
        """
        cat = SCHEMA_REGISTRY.get(schema_id)
        if not cat:
            return False, f"unknown schema: {schema_id}"

        sa = cat.actions.get(action)
        if not sa:
            return False, f"unknown action '{action}' in schema {schema_id}"

        # Validate parameters if the action defines them
        if sa.parameters and params:
            for param_name, param_type in sa.parameters.items():
                # Parameters are optional — only validate if provided
                if param_name in params:
                    value = params[param_name]
                    if not isinstance(value, param_type):
                        return False, f"parameter '{param_name}' must be {param_type.__name__}, got {type(value).__name__}"

        # For dangerous actions (danger >= 5), require all defined parameters
        if sa.danger and sa.danger.total >= 5 and sa.parameters:
            if not params:
                return False, f"high-danger action '{action}' requires parameters: {list(sa.parameters.keys())}"
            missing = [p for p in sa.parameters if p not in params]
            if missing:
                return False, f"high-danger action '{action}' missing required parameters: {missing}"

        return True, "valid"

    # --- Credential Authorization ---

    def check_authorization(
        self,
        credential: ManagementCredential,
        schema_id: str,
        action: str,
    ) -> Tuple[bool, str]:
        """
        Check if a management credential authorizes a specific action.

        Validates tier, schema allowlist, and expiry. Does NOT verify the
        credential signature — callers must verify the signature via
        checkmessage before calling this method.

        Returns:
            (authorized, reason)
        """
        now = int(time.time())

        # Check revocation
        if credential.revoked_at is not None:
            return False, "credential revoked"

        # Check expiry
        if credential.valid_until < now:
            return False, "credential expired"

        if credential.valid_from > now:
            return False, "credential not yet valid"

        # Check tier
        required_tier = self.get_required_tier(schema_id, action)
        if not required_tier:
            return False, f"unknown action {schema_id}/{action}"

        cred_level = TIER_HIERARCHY.get(credential.tier, -1)
        required_level = TIER_HIERARCHY.get(required_tier, 99)
        if cred_level < required_level:
            return False, f"credential tier '{credential.tier}' insufficient, requires '{required_tier}'"

        # Check schema allowlist
        allowed = any(
            _schema_matches(pattern, schema_id)
            for pattern in credential.allowed_schemas
        )
        if not allowed:
            return False, f"schema {schema_id} not in credential allowlist"

        return True, "authorized"

    # --- Pricing ---

    def get_pricing(self, danger_score: DangerScore, reputation_tier: str = "newcomer") -> int:
        """
        Calculate price in sats for an action based on danger and reputation.

        Higher danger = higher price. Better reputation = discount.
        """
        base = danger_score.total * BASE_PRICE_PER_DANGER_POINT
        multiplier = TIER_PRICING_MULTIPLIERS.get(reputation_tier, 1.5)
        return max(1, int(base * multiplier))

    # --- Management Credential Lifecycle ---

    def issue_credential(
        self,
        agent_id: str,
        node_id: str,
        tier: str,
        allowed_schemas: List[str],
        constraints: Dict[str, Any],
        valid_days: int = 90,
    ) -> Optional[ManagementCredential]:
        """
        Issue a management credential from our node to an agent.

        Args:
            agent_id: Agent/advisor pubkey
            node_id: Managed node pubkey (usually our_pubkey)
            tier: Permission tier (monitor/standard/advanced/admin)
            allowed_schemas: Schema patterns the agent can use
            constraints: Operational constraints (limits)
            valid_days: Credential validity period in days (must be > 0)

        Returns:
            ManagementCredential on success, None on failure
        """
        if not self.rpc or not self.our_pubkey:
            self._log("cannot issue: no RPC or pubkey", "warn")
            return None

        if tier not in VALID_TIERS:
            self._log(f"invalid tier: {tier}", "warn")
            return None

        if not allowed_schemas:
            self._log("allowed_schemas cannot be empty", "warn")
            return None

        for schema_pattern in allowed_schemas:
            if schema_pattern == "*":
                continue
            if schema_pattern.endswith("/*"):
                prefix = schema_pattern[:-2]
                if not any(sid.startswith(prefix + "/") for sid in SCHEMA_REGISTRY):
                    self._log(f"allowed_schemas pattern '{schema_pattern}' matches no known schemas", "warn")
                    return None
            elif schema_pattern not in SCHEMA_REGISTRY:
                self._log(f"allowed_schemas entry '{schema_pattern}' is not a known schema", "warn")
                return None

        if not isinstance(valid_days, int) or valid_days <= 0:
            self._log(f"invalid valid_days: {valid_days}", "warn")
            return None

        if valid_days > 730:  # 2 years max
            self._log(f"valid_days {valid_days} exceeds max 730", "warn")
            return None

        if not agent_id or agent_id == self.our_pubkey:
            self._log("cannot issue credential to self", "warn")
            return None

        # Enforce size limits on serialized fields
        schemas_json = json.dumps(allowed_schemas)
        constraints_json = json.dumps(constraints)
        if len(schemas_json) > MAX_ALLOWED_SCHEMAS_LEN:
            self._log(f"allowed_schemas too large ({len(schemas_json)} > {MAX_ALLOWED_SCHEMAS_LEN})", "warn")
            return None
        if len(constraints_json) > MAX_CONSTRAINTS_LEN:
            self._log(f"constraints too large ({len(constraints_json)} > {MAX_CONSTRAINTS_LEN})", "warn")
            return None

        # Check row cap
        count = self.db.count_management_credentials()
        if count >= MAX_MANAGEMENT_CREDENTIALS:
            self._log(f"management credentials at cap ({MAX_MANAGEMENT_CREDENTIALS})", "warn")
            return None

        now = int(time.time())
        credential_id = str(uuid.uuid4())

        # Build signing payload before constructing frozen credential
        signing_data = {
            "credential_id": credential_id,
            "issuer_id": self.our_pubkey,
            "agent_id": agent_id,
            "node_id": node_id,
            "tier": tier,
            "allowed_schemas": allowed_schemas,
            "constraints": constraints,
            "valid_from": now,
            "valid_until": now + (valid_days * 86400),
        }
        signing_payload = get_credential_signing_payload(signing_data)

        # Sign with HSM
        try:
            result = self.rpc.signmessage(signing_payload)
            signature = result.get("zbase", "") if isinstance(result, dict) else str(result)
        except Exception as e:
            self._log(f"HSM signing failed: {e}", "error")
            return None

        if not signature:
            self._log("HSM returned empty signature", "error")
            return None

        # Construct frozen credential with signature
        cred = ManagementCredential(
            credential_id=credential_id,
            issuer_id=self.our_pubkey,
            agent_id=agent_id,
            node_id=node_id,
            tier=tier,
            allowed_schemas=tuple(allowed_schemas),
            constraints=constraints_json,
            valid_from=now,
            valid_until=now + (valid_days * 86400),
            signature=signature,
        )

        # Store
        stored = self.db.store_management_credential(
            credential_id=cred.credential_id,
            issuer_id=cred.issuer_id,
            agent_id=cred.agent_id,
            node_id=cred.node_id,
            tier=cred.tier,
            allowed_schemas_json=schemas_json,
            constraints_json=constraints_json,
            valid_from=cred.valid_from,
            valid_until=cred.valid_until,
            signature=cred.signature,
        )

        if not stored:
            self._log("failed to store management credential", "error")
            return None

        self._log(f"issued mgmt credential {credential_id[:8]}... for agent {agent_id[:16]}... tier={tier}")
        return cred

    def revoke_credential(self, credential_id: str) -> bool:
        """Revoke a management credential we issued."""
        cred = self.db.get_management_credential(credential_id)
        if not cred:
            self._log(f"credential {credential_id[:8]}... not found", "warn")
            return False

        if cred.get("issuer_id") != self.our_pubkey:
            self._log("cannot revoke: not the issuer", "warn")
            return False

        if cred.get("revoked_at") is not None:
            self._log(f"credential {credential_id[:8]}... already revoked", "warn")
            return False

        now = int(time.time())
        success = self.db.revoke_management_credential(credential_id, now)
        if success:
            self._log(f"revoked mgmt credential {credential_id[:8]}...")
        return success

    def list_credentials(
        self, agent_id: Optional[str] = None, node_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """List management credentials with optional filters."""
        return self.db.get_management_credentials(agent_id=agent_id, node_id=node_id)

    # --- Receipt Recording ---

    def record_receipt(
        self,
        credential_id: str,
        schema_id: str,
        action: str,
        params: Dict[str, Any],
        result: Optional[Dict[str, Any]] = None,
        state_hash_before: Optional[str] = None,
        state_hash_after: Optional[str] = None,
    ) -> Optional[str]:
        """
        Record a management action receipt.

        Returns receipt_id on success, None on failure.
        """
        if self.db:
            cred = self.db.get_management_credential(credential_id)
            if not cred:
                self._log(f"receipt references non-existent credential: {credential_id[:16]}...", "warn")
                return None
            if cred.get('revoked_at'):
                self._log(f"receipt references revoked credential: {credential_id[:16]}...", "warn")
                return None

        danger = self.get_danger_score(schema_id, action)
        if not danger:
            return None

        receipt_id = str(uuid.uuid4())
        now = int(time.time())

        # Sign the receipt
        signature = ""
        if self.rpc:
            receipt_payload = json.dumps({
                "receipt_id": receipt_id,
                "credential_id": credential_id,
                "schema_id": schema_id,
                "action": action,
                "danger_score": danger.total,
                "executed_at": now,
            }, sort_keys=True, separators=(',', ':'))
            try:
                sig_result = self.rpc.signmessage(receipt_payload)
                signature = sig_result.get("zbase", "") if isinstance(sig_result, dict) else str(sig_result)
            except Exception as e:
                self._log(f"receipt signing failed: {e}", "warn")
                return None  # Don't store unsigned receipts

        stored = self.db.store_management_receipt(
            receipt_id=receipt_id,
            credential_id=credential_id,
            schema_id=schema_id,
            action=action,
            params_json=json.dumps(params),
            danger_score=danger.total,
            result_json=json.dumps(result) if result else None,
            state_hash_before=state_hash_before,
            state_hash_after=state_hash_after,
            executed_at=now,
            executor_signature=signature,
        )

        return receipt_id if stored else None

    # --- Protocol Gossip Handlers ---

    def handle_mgmt_credential_present(
        self, peer_id: str, payload: dict
    ) -> bool:
        """
        Handle an incoming MGMT_CREDENTIAL_PRESENT message.

        Validates credential structure, verifies issuer signature,
        stores if new, and returns True if accepted.
        """
        credential = payload.get("credential")
        if not isinstance(credential, dict):
            self._log("invalid mgmt_credential_present: missing credential dict", "warn")
            return False

        if not self._check_rate_limit(
            peer_id,
            "mgmt_credential_present",
            MAX_MGMT_CREDENTIAL_PRESENTS_PER_PEER_PER_HOUR,
        ):
            self._log(f"rate limit exceeded for mgmt credential presents from {peer_id[:16]}...", "warn")
            return False

        # Extract fields
        credential_id = credential.get("credential_id")
        if not credential_id or not isinstance(credential_id, str):
            self._log("mgmt_credential_present: missing credential_id", "warn")
            return False

        issuer_id = credential.get("issuer_id", "")
        agent_id = credential.get("agent_id", "")
        node_id = credential.get("node_id", "")
        tier = credential.get("tier", "")
        allowed_schemas = credential.get("allowed_schemas", [])
        constraints = credential.get("constraints", {})
        valid_from = credential.get("valid_from", 0)
        valid_until = credential.get("valid_until", 0)
        signature = credential.get("signature", "")

        # Basic field validation
        if tier not in VALID_TIERS:
            self._log(f"mgmt_credential_present: invalid tier {tier!r}", "warn")
            return False

        if not isinstance(allowed_schemas, list) or not allowed_schemas:
            self._log("mgmt_credential_present: bad allowed_schemas", "warn")
            return False

        if not isinstance(valid_from, int) or not isinstance(valid_until, int):
            self._log("mgmt_credential_present: bad validity period", "warn")
            return False

        if valid_until <= valid_from:
            self._log("mgmt_credential_present: valid_until <= valid_from", "warn")
            return False

        now = int(time.time())
        if valid_until < now:
            self._log(f"rejecting expired management credential from {peer_id[:16]}...", "info")
            return False

        # Self-issuance of management credential: issuer == agent is not
        # inherently invalid (operator can credential their own agent),
        # but issuer == node_id is also fine. No self-issuance rejection here.

        # Verify issuer signature (fail-closed)
        if not signature:
            self._log("mgmt_credential_present: missing signature", "warn")
            return False

        if not self.rpc:
            self._log("mgmt_credential_present: no RPC for sig verification", "warn")
            return False

        # Build signing payload matching get_credential_signing_payload()
        constraints_for_payload = constraints
        if isinstance(constraints_for_payload, str):
            try:
                constraints_for_payload = json.loads(constraints_for_payload)
            except (json.JSONDecodeError, TypeError):
                constraints_for_payload = {}

        signing_data = {
            "credential_id": credential_id,
            "issuer_id": issuer_id,
            "agent_id": agent_id,
            "node_id": node_id,
            "tier": tier,
            "allowed_schemas": allowed_schemas,
            "constraints": constraints_for_payload,
            "valid_from": valid_from,
            "valid_until": valid_until,
        }
        signing_payload = json.dumps(signing_data, sort_keys=True, separators=(',', ':'))

        try:
            result = self.rpc.checkmessage(signing_payload, signature, issuer_id)
            if not isinstance(result, dict):
                self._log("mgmt_credential_present: unexpected checkmessage response type", "warn")
                return False
            if not result.get("verified", False):
                self._log("mgmt_credential_present: signature verification failed", "warn")
                return False
            if not result.get("pubkey", "") or result.get("pubkey", "") != issuer_id:
                self._log("mgmt_credential_present: signature pubkey mismatch", "warn")
                return False
        except Exception as e:
            self._log(f"mgmt_credential_present: checkmessage error: {e}", "warn")
            return False

        # Check row cap
        count = self.db.count_management_credentials()
        if count >= MAX_MANAGEMENT_CREDENTIALS:
            self._log("mgmt credential store at cap, rejecting", "warn")
            return False

        # Content-level dedup: already have this credential?
        existing = self.db.get_management_credential(credential_id)
        if existing:
            return True  # Idempotent

        # Serialize for storage
        allowed_schemas_json = json.dumps(allowed_schemas)
        constraints_json = (
            constraints if isinstance(constraints, str)
            else json.dumps(constraints)
        )

        stored = self.db.store_management_credential(
            credential_id=credential_id,
            issuer_id=issuer_id,
            agent_id=agent_id,
            node_id=node_id,
            tier=tier,
            allowed_schemas_json=allowed_schemas_json,
            constraints_json=constraints_json,
            valid_from=valid_from,
            valid_until=valid_until,
            signature=signature,
        )

        if stored:
            self._log(f"stored mgmt credential {credential_id[:8]}... from {peer_id[:16]}...")

        return stored

    def handle_mgmt_credential_revoke(
        self, peer_id: str, payload: dict
    ) -> bool:
        """
        Handle an incoming MGMT_CREDENTIAL_REVOKE message.

        Verifies issuer signature and marks credential as revoked.
        """
        credential_id = payload.get("credential_id")
        reason = payload.get("reason", "")
        issuer_id = payload.get("issuer_id", "")
        signature = payload.get("signature", "")

        if not self._check_rate_limit(
            peer_id,
            "mgmt_credential_revoke",
            MAX_MGMT_CREDENTIAL_REVOKES_PER_PEER_PER_HOUR,
        ):
            self._log(f"rate limit exceeded for mgmt credential revokes from {peer_id[:16]}...", "warn")
            return False

        if not credential_id or not isinstance(credential_id, str):
            self._log("invalid mgmt_credential_revoke: missing credential_id", "warn")
            return False

        if not reason or len(reason) > 500:
            self._log("invalid mgmt_credential_revoke: bad reason", "warn")
            return False

        # Fetch credential
        cred = self.db.get_management_credential(credential_id)
        if not cred:
            self._log(f"mgmt revoke: credential {credential_id[:8]}... not found", "debug")
            return False

        # Verify issuer matches
        if cred.get("issuer_id") != issuer_id:
            self._log(f"mgmt revoke: issuer mismatch for {credential_id[:8]}...", "warn")
            return False

        # Already revoked?
        if cred.get("revoked_at") is not None:
            return True  # Idempotent

        # Verify revocation signature (fail-closed)
        if not signature:
            self._log("mgmt revoke: missing signature", "warn")
            return False
        if not self.rpc:
            self._log("mgmt revoke: no RPC for signature verification", "warn")
            return False

        revoke_payload = json.dumps({
            "credential_id": credential_id,
            "action": "mgmt_revoke",
            "reason": reason,
        }, sort_keys=True, separators=(',', ':'))

        try:
            result = self.rpc.checkmessage(revoke_payload, signature, issuer_id)
            if not isinstance(result, dict):
                self._log("mgmt revoke: unexpected checkmessage response type", "warn")
                return False
            if not result.get("verified", False):
                self._log("mgmt revoke: signature verification failed", "warn")
                return False
            if not result.get("pubkey", "") or result.get("pubkey", "") != issuer_id:
                self._log("mgmt revoke: signature pubkey mismatch", "warn")
                return False
        except Exception as e:
            self._log(f"mgmt revoke: checkmessage error: {e}", "warn")
            return False

        now = int(time.time())
        success = self.db.revoke_management_credential(credential_id, now)

        if success:
            self._log(f"processed mgmt revocation for {credential_id[:8]}...")

        return success
