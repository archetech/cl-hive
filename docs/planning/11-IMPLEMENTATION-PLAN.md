# DID Ecosystem — Phased Implementation Plan

## Context

8 DID specification documents in `docs/planning/` define a decentralized identity, reputation, marketplace, and settlement ecosystem for cl-hive. These specs depend on the Archon DID infrastructure (`@didcid/keymaster`, Gatekeeper) which is a Node.js ecosystem tool not yet integrated. The practical approach is to build the Python data models, credential logic, and protocol layer first using CLN's existing HSM crypto (`signmessage`/`checkmessage`), then wire in Archon integration later.

**Dependency order**: Reputation Schema → Fleet Management Schemas → Cashu Task Escrow → Marketplace → Nostr Transport + Settlements → Liquidity → Client (3-plugin split).

**This plan covers Phases 1-3** (the foundation layers that can be built with zero new external dependencies). Phases 4-5 (Cashu/Nostr) require external libraries and will be planned separately once the foundation is deployed.

---

## Phase 1: DID Credential Foundation

**Goal**: Data models, DB storage, credential issuance/verification via CLN HSM, reputation aggregation, RPC commands.

### New file: `modules/did_credentials.py`

Core `DIDCredentialManager` class following the `SettlementManager` pattern:

```python
class DIDCredentialManager:
    """DID credential issuance, verification, storage, and aggregation."""

    MAX_CREDENTIALS_PER_PEER = 100
    MAX_TOTAL_CREDENTIALS = 10_000
    AGGREGATION_CACHE_TTL = 3600  # 1 hour
    RECENCY_DECAY_LAMBDA = 0.01  # half-life ~69 days

    def __init__(self, database, plugin, rpc=None, our_pubkey=""):
```

**Key classes/dataclasses**:

| Class | Purpose |
|-------|---------|
| `DIDCredential` | Single credential: issuer, subject, domain, period, metrics, outcome, evidence, signature |
| `AggregatedReputation` | Cached aggregation for a subject: domain, score (0-100), confidence, tier, component scores |
| `CredentialProfile` | Profile definition (one of 4 domains): required metrics, valid ranges, evidence types |

**4 credential profiles** (hardcoded, not DB-driven):

| Domain | Subject | Issuer | Key Metrics |
|--------|---------|--------|-------------|
| `hive:advisor` | Fleet advisor | Node operator | `revenue_delta_pct`, `actions_taken`, `uptime_pct`, `channels_managed` |
| `hive:node` | Lightning node | Peer node | `routing_reliability`, `uptime`, `htlc_success_rate`, `avg_fee_ppm` |
| `hive:client` | Node operator | Advisor | `payment_timeliness`, `sla_reasonableness`, `communication_quality` |
| `agent:general` | AI agent | Task delegator | `task_completion_rate`, `accuracy`, `response_time_ms`, `tasks_evaluated` |

**Aggregation algorithm**:
- `score = Σ(credential_weight × metric_score)` where `credential_weight = issuer_weight × recency_factor × evidence_strength`
- Issuer weight: 1.0 default, up to 3.0 for issuers with open channels to subject (proof-of-stake)
- Recency factor: `e^(-λ × age_days)` with λ=0.01
- Evidence strength: ×0.3 (no evidence), ×0.7 (1-5 refs), ×1.0 (5+ signed receipts)
- Self-issuance rejected (`issuer == subject`)
- Output: 0-100 score → tier: Newcomer (0-59), Recognized (60-74), Trusted (75-84), Senior (85-100)

**Methods**:
- `issue_credential(subject_id, domain, metrics, outcome, evidence, rpc)` → sign with HSM, store, return credential
- `verify_credential(credential)` → check signature, expiry, self-issuance, schema
- `revoke_credential(credential_id, reason)` → mark revoked, broadcast
- `aggregate_reputation(subject_id, domain=None)` → weighted aggregation with caching
- `get_credit_tier(subject_id)` → Newcomer/Recognized/Trusted/Senior
- `handle_credential_present(peer_id, payload, rpc)` → validate incoming credential gossip
- `handle_credential_revoke(peer_id, payload, rpc)` → process revocation
- `cleanup_expired()` → remove expired credentials, refresh stale aggregations

### New DB tables (in `database.py` `initialize()`)

```sql
-- DID credentials received from peers or issued locally
CREATE TABLE IF NOT EXISTS did_credentials (
    credential_id TEXT PRIMARY KEY,       -- UUID
    issuer_id TEXT NOT NULL,              -- pubkey of issuer
    subject_id TEXT NOT NULL,             -- pubkey of subject
    domain TEXT NOT NULL,                 -- 'hive:advisor', 'hive:node', etc.
    period_start INTEGER NOT NULL,        -- epoch
    period_end INTEGER NOT NULL,          -- epoch
    metrics_json TEXT NOT NULL,           -- JSON: domain-specific metrics
    outcome TEXT NOT NULL DEFAULT 'neutral', -- 'renew', 'revoke', 'neutral'
    evidence_json TEXT,                   -- JSON array of evidence refs
    signature TEXT NOT NULL,              -- zbase signature from issuer
    issued_at INTEGER NOT NULL,
    expires_at INTEGER,
    revoked_at INTEGER,
    revocation_reason TEXT,
    received_from TEXT,                   -- peer_id we received this from (NULL = local)
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_did_cred_subject ON did_credentials(subject_id, domain);
CREATE INDEX IF NOT EXISTS idx_did_cred_issuer ON did_credentials(issuer_id);
CREATE INDEX IF NOT EXISTS idx_did_cred_domain ON did_credentials(domain, issued_at);

-- Cached aggregated reputation scores (recomputed periodically)
CREATE TABLE IF NOT EXISTS did_reputation_cache (
    subject_id TEXT NOT NULL,
    domain TEXT NOT NULL,                 -- domain or '_all' for cross-domain
    score INTEGER NOT NULL DEFAULT 50,    -- 0-100
    tier TEXT NOT NULL DEFAULT 'newcomer', -- newcomer/recognized/trusted/senior
    confidence TEXT NOT NULL DEFAULT 'low', -- low/medium/high
    credential_count INTEGER NOT NULL DEFAULT 0,
    issuer_count INTEGER NOT NULL DEFAULT 0,
    computed_at INTEGER NOT NULL,
    components_json TEXT,                 -- JSON breakdown of score components
    PRIMARY KEY (subject_id, domain)
);
```

**New `HiveDatabase` methods**: `store_credential()`, `get_credentials_for_subject(subject_id, domain=None, limit=100)`, `get_credential(credential_id)`, `revoke_credential(credential_id, reason, timestamp)`, `count_credentials()`, `store_reputation_cache(subject_id, domain, score, tier, ...)`, `get_reputation_cache(subject_id, domain=None)`, `cleanup_expired_credentials(before_ts)`, `count_credentials_by_issuer(issuer_id)`.

Row cap: `MAX_DID_CREDENTIAL_ROWS = 50_000` checked before insert.

### New protocol messages (in `protocol.py`)

| Type | ID | Purpose | Reliable? |
|------|----|---------|-----------|
| `DID_CREDENTIAL_PRESENT` | 32883 | Gossip a credential to hive members | Yes |
| `DID_CREDENTIAL_REVOKE` | 32885 | Announce credential revocation | Yes |

Factory functions: `create_did_credential_present(...)`, `validate_did_credential_present(payload)`, `get_did_credential_present_signing_payload(payload)`. Same pattern for revoke.

Signing payload for credentials: `json.dumps({"issuer_id":..., "subject_id":..., "domain":..., "period_start":..., "period_end":..., "metrics":..., "outcome":...}, sort_keys=True)` — deterministic JSON for reproducible signatures.

### New RPC commands

| Command | Handler | Permission | Description |
|---------|---------|------------|-------------|
| `hive-did-issue` | `did_issue_credential(ctx, subject_id, domain, metrics_json, outcome, evidence_json)` | member | Issue a credential for a subject |
| `hive-did-list` | `did_list_credentials(ctx, subject_id, domain, issuer_id)` | any | List credentials (filtered) |
| `hive-did-revoke` | `did_revoke_credential(ctx, credential_id, reason)` | member | Revoke a credential we issued |
| `hive-did-reputation` | `did_get_reputation(ctx, subject_id, domain)` | any | Get aggregated reputation score |
| `hive-did-profiles` | `did_list_profiles(ctx)` | any | List supported credential profiles |

### Wiring in `cl-hive.py`

1. Import `DIDCredentialManager` from `modules.did_credentials`
2. Declare `did_credential_mgr: Optional[DIDCredentialManager] = None` global
3. Initialize in `init()` after database, pass `database, plugin, rpc, our_pubkey`
4. Add `did_credential_mgr` field to `HiveContext` in `rpc_commands.py`
5. Add dispatch entries for `DID_CREDENTIAL_PRESENT` and `DID_CREDENTIAL_REVOKE` in `_dispatch_hive_message()`
6. Add `did_maintenance_loop` background thread: cleanup expired credentials, refresh stale aggregation cache (runs every 30 min)

### MCP server

Add `hive-did-issue`, `hive-did-list`, `hive-did-revoke`, `hive-did-reputation`, `hive-did-profiles` to `_check_method_allowed()` in `tools/mcp-hive-server.py`.

---

## Phase 2: Management Schemas + Danger Scoring

**Goal**: Define the 15 management schema categories, implement the danger scoring engine, and add schema-based command validation. This is the framework that management credentials and escrow will use.

### New file: `modules/management_schemas.py`

```python
class ManagementSchemaRegistry:
    """Registry of management schema categories with danger scoring."""
```

**15 schema categories** (each a dataclass):

| # | Schema ID | Category | Danger Range |
|---|-----------|----------|-------------|
| 1 | `hive:monitor/v1` | Monitoring & Read-Only | 1-2 |
| 2 | `hive:fee-policy/v1` | Fee Management | 2-5 |
| 3 | `hive:htlc-policy/v1` | HTLC Policy | 2-5 |
| 4 | `hive:forwarding/v1` | Forwarding Policy | 2-6 |
| 5 | `hive:rebalance/v1` | Liquidity Management | 3-6 |
| 6 | `hive:channel/v1` | Channel Lifecycle | 5-10 |
| 7 | `hive:splice/v1` | Splicing | 5-7 |
| 8 | `hive:peer/v1` | Peer Management | 2-5 |
| 9 | `hive:payment/v1` | Payments & Invoicing | 1-6 |
| 10 | `hive:wallet/v1` | Wallet & On-Chain | 1-9 |
| 11 | `hive:plugin/v1` | Plugin Management | 1-9 |
| 12 | `hive:config/v1` | Node Configuration | 1-7 |
| 13 | `hive:backup/v1` | Backup Operations | 1-10 |
| 14 | `hive:emergency/v1` | Emergency Operations | 3-10 |
| 15 | `hive:htlc-mgmt/v1` | HTLC Management | 2-8 |

**Danger scoring engine** — 5 dimensions, each 1-10:

```python
@dataclass(frozen=True)
class DangerScore:
    reversibility: int      # 1=instant undo, 10=irreversible
    financial_exposure: int  # 1=0 sats, 10=>10M sats
    time_sensitivity: int    # 1=no compounding, 10=permanent
    blast_radius: int        # 1=single metric, 10=entire fleet
    recovery_difficulty: int # 1=trivial, 10=unrecoverable

    @property
    def total(self) -> int:
        """Overall danger score (max of dimensions, not sum)."""
        return max(self.reversibility, self.financial_exposure,
                   self.time_sensitivity, self.blast_radius,
                   self.recovery_difficulty)
```

**Schema action definitions**: Each action within a schema has a pre-computed `DangerScore` and required permission tier:

```python
SCHEMA_ACTIONS = {
    "hive:fee-policy/v1": {
        "set_anchor": SchemaAction(
            danger=DangerScore(2, 2, 2, 1, 1),  # total=2
            required_tier="standard",
            parameters={"channel_id": str, "target_fee_ppm": int, "reason": str},
        ),
        "set_bulk": SchemaAction(
            danger=DangerScore(3, 4, 3, 5, 2),  # total=5
            required_tier="standard",
            parameters={"channels": list, "policy": dict},
        ),
    },
    # ... 15 schemas × N actions each
}
```

**Key methods**:
- `validate_command(schema_id, action, params)` → validate params against schema definition
- `get_danger_score(schema_id, action)` → return DangerScore
- `get_required_tier(schema_id, action)` → "monitor"/"standard"/"advanced"/"admin"
- `get_pricing(danger_score, reputation_tier)` → sats (for future escrow integration)
- `list_schemas()` → all registered schemas with their actions

**Management credential structure** (data model only — no L402/Cashu yet):

```python
@dataclass
class ManagementCredential:
    """HiveManagementCredential — operator grants agent permission to manage."""
    credential_id: str
    issuer_id: str          # node operator pubkey
    agent_id: str           # agent/advisor pubkey
    node_id: str            # managed node pubkey
    tier: str               # monitor/standard/advanced/admin
    allowed_schemas: List[str]  # e.g. ["hive:fee-policy/*", "hive:monitor/*"]
    constraints: Dict       # max_fee_change_pct, max_rebalance_sats, max_daily_actions
    valid_from: int         # epoch
    valid_until: int        # epoch
    signature: str          # operator's HSM signature
```

### New DB tables

```sql
CREATE TABLE IF NOT EXISTS management_credentials (
    credential_id TEXT PRIMARY KEY,
    issuer_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'monitor',
    allowed_schemas_json TEXT NOT NULL,
    constraints_json TEXT NOT NULL,
    valid_from INTEGER NOT NULL,
    valid_until INTEGER NOT NULL,
    signature TEXT NOT NULL,
    revoked_at INTEGER,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_mgmt_cred_agent ON management_credentials(agent_id);
CREATE INDEX IF NOT EXISTS idx_mgmt_cred_node ON management_credentials(node_id);

CREATE TABLE IF NOT EXISTS management_receipts (
    receipt_id TEXT PRIMARY KEY,
    credential_id TEXT NOT NULL,
    schema_id TEXT NOT NULL,
    action TEXT NOT NULL,
    params_json TEXT NOT NULL,
    danger_score INTEGER NOT NULL,
    result_json TEXT,
    state_hash_before TEXT,
    state_hash_after TEXT,
    executed_at INTEGER NOT NULL,
    executor_signature TEXT NOT NULL,
    FOREIGN KEY (credential_id) REFERENCES management_credentials(credential_id)
);
CREATE INDEX IF NOT EXISTS idx_mgmt_receipt_cred ON management_receipts(credential_id);
```

Row caps: `MAX_MANAGEMENT_CREDENTIAL_ROWS = 1_000`, `MAX_MANAGEMENT_RECEIPT_ROWS = 100_000`.

### New RPC commands

| Command | Description |
|---------|-------------|
| `hive-schema-list` | List all management schemas with actions and danger scores |
| `hive-schema-validate` | Validate a command against schema (dry run) |
| `hive-mgmt-credential-issue` | Issue management credential for an agent |
| `hive-mgmt-credential-list` | List management credentials |
| `hive-mgmt-credential-revoke` | Revoke a management credential |

---

## Phase 3: Credential Exchange Protocol

**Goal**: Gossip DID credentials and management credentials between hive members. Integrate with existing membership/planner for reputation-weighted decisions.

### Protocol messages

| Type | ID | Purpose | Reliable? |
|------|----|---------|-----------|
| `MGMT_CREDENTIAL_PRESENT` | 32887 | Share a management credential with hive | Yes |
| `MGMT_CREDENTIAL_REVOKE` | 32889 | Announce management credential revocation | Yes |

### Handler functions (in `cl-hive.py`)

```
handle_did_credential_present(peer_id, payload, plugin):
    1. Dedup (proto_events)
    2. Timestamp freshness check (±300s)
    3. Membership verification
    4. Identity binding (peer_id == sender claimed in payload)
    5. Schema validation
    6. Signature verification (checkmessage)
    7. Self-issuance rejection
    8. Store credential
    9. Update aggregation cache
    10. Relay to other members
```

Same pattern for revoke and management credential messages.

### Integration with existing modules

**`planner.py`**: Before proposing expansion to a target, check `did_credential_mgr.get_credit_tier(target)`. Prefer targets with Recognized+ tier. Log reputation score in `hive_planner_log`.

**`membership.py`**: During auto-promotion evaluation, incorporate `hive:node` reputation from peer credentials as supplementary signal (not sole criterion — existing forwarding/uptime metrics remain primary).

**`settlement.py`**: Reputation tier determines settlement terms. Newcomer: full escrow required. Senior: extended credit lines. Store tier alongside settlement proposal.

### Background loop: `did_maintenance_loop`

```python
def did_maintenance_loop():
    while not shutdown_event.is_set():
        try:
            snap = config.snapshot()
            # 1. Cleanup expired credentials
            did_credential_mgr.cleanup_expired()
            # 2. Refresh stale aggregation cache entries
            did_credential_mgr.refresh_stale_aggregations()
            # 3. Auto-issue hive:node credentials for peers we have data on
            #    (forwarding stats from contribution.py, uptime from state_manager)
            did_credential_mgr.auto_issue_node_credentials(rpc)
            # 4. Rebroadcast our credentials periodically (every 4h)
            did_credential_mgr.rebroadcast_own_credentials(rpc)
        except Exception as e:
            plugin.log(f"cl-hive: did_maintenance error: {e}", level='error')
        shutdown_event.wait(1800)  # 30 min cycle
```

---

## Files Modified Summary

| File | Phase | Changes |
|------|-------|---------|
| **NEW** `modules/did_credentials.py` | 1 | DIDCredentialManager, credential profiles, aggregation |
| **NEW** `modules/management_schemas.py` | 2 | Schema registry, danger scoring, ManagementCredential |
| `modules/database.py` | 1-2 | 4 new tables, ~15 new methods, row caps |
| `modules/protocol.py` | 1, 3 | 4 new message types (32883-32889), factory/validation functions |
| `modules/rpc_commands.py` | 1-2 | `did_credential_mgr` + `management_schema_registry` on HiveContext, ~10 handler functions |
| `cl-hive.py` | 1-3 | Import, init, dispatch entries, background loop, RPC wrappers |
| `tools/mcp-hive-server.py` | 1-2 | Add new RPC methods to allowlist |
| **NEW** `tests/test_did_credentials.py` | 1 | Credential issuance, verification, aggregation, revocation |
| **NEW** `tests/test_management_schemas.py` | 2 | Schema validation, danger scoring, credential checks |
| **NEW** `tests/test_did_protocol.py` | 3 | Protocol message handling, relay, idempotency |

---

## Verification

1. **Unit tests**: `python3 -m pytest tests/test_did_credentials.py tests/test_management_schemas.py tests/test_did_protocol.py -v`
2. **Regression**: `python3 -m pytest tests/ -v` (all 1749+ existing tests must pass)
3. **RPC smoke test**: `lightning-cli hive-did-profiles`, `lightning-cli hive-schema-list`
4. **Integration**: Issue credential via `hive-did-issue`, verify it appears in `hive-did-list`, check reputation via `hive-did-reputation`
5. **Backwards compatibility**: Nodes without DID support must still participate in hive normally (all DID features are additive, never blocking)

---

## What's Deferred (Phases 4-5, planned separately)

| Phase | Spec | Requires |
|-------|------|----------|
| 4 | DID-CASHU-TASK-ESCROW | Cashu Python SDK (NUT-10/11/14), mint integration |
| 4 | DID-HIVE-SETTLEMENTS (extended) | Extends existing settlement.py with 9 new types |
| 5 | DID-NOSTR-MARKETPLACE | Nostr Python library (NIP-44), relay connections |
| 5 | DID-HIVE-LIQUIDITY | Depends on settlements + escrow |
| 6 | DID-HIVE-CLIENT | 3-plugin split (cl-hive-comms, cl-hive-archon, cl-hive) |

These require external Python libraries not currently in the dependency set. They will be planned once Phases 1-3 are deployed and validated.
