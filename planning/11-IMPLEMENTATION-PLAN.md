# DID Ecosystem — Phased Implementation Plan (Phases 1-3)

## Context

12 specification documents in `docs/planning/` (see [00-INDEX.md](./00-INDEX.md)) define a decentralized identity, reputation, marketplace, and settlement ecosystem for cl-hive. These specs depend on the Archon DID infrastructure (`@didcid/keymaster`, Gatekeeper) which is a Node.js ecosystem tool not yet integrated. The practical approach is to build the Python data models, credential logic, and protocol layer first using CLN's existing HSM crypto (`signmessage`/`checkmessage`), then wire in Archon integration later (see [09-ARCHON-INTEGRATION.md](./09-ARCHON-INTEGRATION.md) for the integration plan and governance tier model).

**Dependency order**: [01-REPUTATION-SCHEMA](./01-REPUTATION-SCHEMA.md) → [02-FLEET-MANAGEMENT](./02-FLEET-MANAGEMENT.md) Schemas → [03-CASHU-TASK-ESCROW](./03-CASHU-TASK-ESCROW.md) → [04-HIVE-MARKETPLACE](./04-HIVE-MARKETPLACE.md) → [05-NOSTR-MARKETPLACE](./05-NOSTR-MARKETPLACE.md) + [06-HIVE-SETTLEMENTS](./06-HIVE-SETTLEMENTS.md) → [07-HIVE-LIQUIDITY](./07-HIVE-LIQUIDITY.md) → [08-HIVE-CLIENT](./08-HIVE-CLIENT.md) (3-plugin split).

**This plan covers Phases 1-3** (the foundation layers that can be built with zero new external dependencies). Phases 4-6 (Cashu/Nostr/plugin split) require external libraries and are planned in [12-IMPLEMENTATION-PLAN-PHASE4-6.md](./12-IMPLEMENTATION-PLAN-PHASE4-6.md).

**Relationship to Archon (09) and Node Provisioning (10)**:
- [09-ARCHON-INTEGRATION.md](./09-ARCHON-INTEGRATION.md): Defines the optional Archon DID integration layer and tiered participation model (Basic → Governance). Phases 1-3 implement the credential foundation using CLN HSM, enabling a clean migration path to Archon `did:cid:*` identifiers later. The `governance_tier` column defined in 09 will be added to `hive_members` in Phase 3 integration.
- [10-NODE-PROVISIONING.md](./10-NODE-PROVISIONING.md): Defines autonomous VPS lifecycle management. Provisioned nodes will consume reputation credentials (Phase 1) and management credentials (Phase 2) to establish trust, and will use the credential exchange protocol (Phase 3) to participate in the fleet reputation system. The provisioning system's "Revenue ≥ costs or graceful shutdown" invariant can use reputation scores as a signal for node health.

---

## Phase 1: DID Credential Foundation

**Goal**: Data models, DB storage, credential issuance/verification via CLN HSM, reputation aggregation, RPC commands.

### New file: `modules/did_credentials.py`

Core `DIDCredentialManager` class following the `SettlementManager` pattern:

```python
class DIDCredentialManager:
    """DID credential issuance, verification, storage, and aggregation."""

    MAX_CREDENTIALS_PER_PEER = 100
    MAX_CREDENTIAL_ROWS = 50_000       # DB row cap
    MAX_REPUTATION_CACHE_ROWS = 10_000 # DB row cap for aggregation cache
    AGGREGATION_CACHE_TTL = 3600       # 1 hour
    RECENCY_DECAY_LAMBDA = 0.01        # half-life ~69 days

    # Rate limits for incoming protocol messages
    MAX_CREDENTIAL_PRESENTS_PER_PEER_PER_HOUR = 20
    MAX_CREDENTIAL_REVOKES_PER_PEER_PER_HOUR = 10

    def __init__(self, database, plugin, rpc=None, our_pubkey=""):
```

**Key classes/dataclasses**:

| Class | Purpose |
|-------|---------|
| `DIDCredential` | Single credential: issuer, subject, domain, period, metrics, outcome, evidence, signature |
| `AggregatedReputation` | Cached aggregation for a subject: domain, score (0-100), confidence, tier, component scores |
| `CreditTierResult` | Result of `get_credit_tier()`: tier (str), score (int), confidence (str), credential_count (int) |
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
- Issuer weight: 1.0 default, up to 3.0 for issuers with open channels to subject (proof-of-stake). **For credentials received from remote peers**, issuer weight is verified by checking our local `listpeers` / `listchannels` for the claimed issuer↔subject channel relationship. If the channel cannot be verified locally, issuer weight falls back to 1.0.
- Recency factor: `e^(-λ × age_days)` with λ=0.01
- Evidence strength: ×0.3 (no evidence), ×0.7 (1-5 refs), ×1.0 (5+ signed receipts). The `evidence_json` field must be a JSON array of objects; non-array values are rejected during validation.
- Self-issuance rejected (`issuer == subject`)
- Output: 0-100 score → tier: Newcomer (0-59), Recognized (60-74), Trusted (75-84), Senior (85-100)

**Methods**:
- `issue_credential(subject_id, domain, metrics, outcome, evidence, rpc)` → sign with HSM, store, return credential
- `verify_credential(credential)` → check signature, expiry, self-issuance, schema
- `revoke_credential(credential_id, reason)` → mark revoked, broadcast
- `aggregate_reputation(subject_id, domain=None)` → weighted aggregation with caching
- `get_credit_tier(subject_id)` → returns `CreditTierResult(tier, score, confidence, credential_count)` — never just a string
- `handle_credential_present(peer_id, payload, rpc)` → validate incoming credential gossip (see security chain below)
- `handle_credential_revoke(peer_id, payload, rpc)` → process revocation
- `cleanup_expired()` → remove expired credentials, refresh stale aggregations
- `refresh_stale_aggregations()` → recompute cache entries older than `AGGREGATION_CACHE_TTL`
- `auto_issue_node_credentials(rpc)` → issue `hive:node` credentials for peers with sufficient forwarding history (from `contribution.py`)
- `rebroadcast_own_credentials(rpc)` → re-gossip our issued credentials to hive members (every 4 hours, tracked via `_last_rebroadcast` timestamp)

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
    outcome TEXT NOT NULL,                -- 'renew', 'revoke', 'neutral' (no DEFAULT — force explicit)
    evidence_json TEXT,                   -- JSON array of evidence refs (validated as array)
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

**New `HiveDatabase` methods**: `store_credential()`, `get_credentials_for_subject(subject_id, domain=None, limit=100)`, `get_credential(credential_id)`, `revoke_credential(credential_id, reason, timestamp)`, `count_credentials()`, `count_credentials_by_issuer(issuer_id)`, `store_reputation_cache(subject_id, domain, score, tier, ...)`, `get_reputation_cache(subject_id, domain=None)`, `cleanup_expired_credentials(before_ts)`, `count_reputation_cache_rows()`.

Row caps: `MAX_CREDENTIAL_ROWS = 50_000` (checked before insert in `store_credential()`), `MAX_REPUTATION_CACHE_ROWS = 10_000` (checked before insert in `store_reputation_cache()`). On cap violation: return `False` from the insert method and log at `warn` level (matching existing pattern in `database.py` e.g. `store_contribution()`).

### New protocol messages (in `protocol.py`)

| Type | ID | Purpose | Reliable? |
|------|----|---------|-----------|
| `DID_CREDENTIAL_PRESENT` | 32883 | Gossip a credential to hive members | Yes |
| `DID_CREDENTIAL_REVOKE` | 32885 | Announce credential revocation | Yes |

Both types added to `RELIABLE_MESSAGE_TYPES` frozenset. These are broadcast messages (not request-response pairs), so they are **not** added to `IMPLICIT_ACK_MAP` — they use generic `MSG_ACK` for reliable delivery confirmation.

Factory functions: `create_did_credential_present(...)`, `validate_did_credential_present(payload)`, `get_did_credential_present_signing_payload(payload)`. Same pattern for revoke. Factory functions return **unsigned serialized bytes** — the `event_id` field is a UUID (`str(uuid.uuid4())`), generated by the factory function and used for idempotency dedup via `proto_events`. Signature verification happens in the handler functions via `rpc.checkmessage()`, not in the factory.

Signing payload for credentials: `json.dumps({"issuer_id":..., "subject_id":..., "domain":..., "period_start":..., "period_end":..., "metrics":..., "outcome":...}, sort_keys=True, separators=(',',':'))` — deterministic JSON for reproducible signatures. The `separators` parameter ensures no whitespace variation across implementations.

**Rate limiting**: All incoming DID protocol messages are rate-limited per peer using an in-memory sliding-window tracker stored in `DIDCredentialManager._rate_limiters` (dict keyed by `(sender_id, message_type)`, protected by `threading.Lock()`). Stale sender entries are evicted when dict size exceeds 1000. Limits: 20 presents/peer/hour, 10 revokes/peer/hour. Exceeding the limit logs at `warn` level and drops the message silently (no error response that could be used for probing).

**Relay scope**: After storing a credential, relay it to all connected hive members. Credentials are immutable once issued, so no TTL limit is needed — relay once per peer. Revocations are broadcast to all connected members immediately (same pattern as `ban_proposal`).

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
4. Add `did_credential_mgr` field to `HiveContext` in `rpc_commands.py` (also add the currently missing `settlement_mgr` field)
5. Add dispatch entries for `DID_CREDENTIAL_PRESENT` and `DID_CREDENTIAL_REVOKE` in `_dispatch_hive_message()`
6. Add `did_maintenance_loop` background thread: cleanup expired credentials, refresh stale aggregation cache (runs every 30 min)
7. Add thin `@plugin.method()` wrappers in `cl-hive.py` for all 5 RPC commands

### MCP server

Add the following to `_check_method_allowed()` in `tools/mcp-hive-server.py`:
- Phase 1: `hive-did-issue`, `hive-did-list`, `hive-did-revoke`, `hive-did-reputation`, `hive-did-profiles`
- Phase 2: `hive-schema-list`, `hive-schema-validate`, `hive-mgmt-credential-issue`, `hive-mgmt-credential-list`, `hive-mgmt-credential-revoke`

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

**Danger-to-approval mapping**: The `DangerScore.total` maps to an approval level that determines how the action is processed:

| Danger Total | Approval Level | Behavior |
|-------------|----------------|----------|
| 1-3 | `auto` | Execute immediately if credential allows |
| 4-6 | `queue` | Queue to `pending_actions` for operator review |
| 7-8 | `confirm` | Require explicit operator confirmation (interactive) |
| 9-10 | `multisig` | Require N/2+1 admin confirmations |

This mapping is checked by `get_approval_level(danger_score)` and used by the handler to route commands through the appropriate governance path.

**Key methods**:
- `validate_command(schema_id, action, params)` → validate params against schema definition
- `get_danger_score(schema_id, action)` → return DangerScore
- `get_required_tier(schema_id, action)` → "monitor"/"standard"/"advanced"/"admin"
- `get_approval_level(danger_score)` → "auto"/"queue"/"confirm"/"multisig" (based on DangerScore.total)
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

Row caps: `MAX_MANAGEMENT_CREDENTIAL_ROWS = 1_000`, `MAX_MANAGEMENT_RECEIPT_ROWS = 100_000`. On cap violation: return `False` from the insert method and log at `warn` level (matching existing pattern in `database.py`).

### Wiring in `cl-hive.py` (Phase 2)

1. Import `ManagementSchemaRegistry` from `modules.management_schemas`
2. Declare `management_schema_registry: Optional[ManagementSchemaRegistry] = None` global
3. Initialize in `init()` after `did_credential_mgr`, pass `database, plugin`
4. Add `management_schema_registry` field to `HiveContext` in `rpc_commands.py`
5. Add thin `@plugin.method()` wrappers in `cl-hive.py` for all 5 Phase 2 RPC commands

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

Rate limits: 10 presents/peer/hour, 5 revokes/peer/hour (same sliding-window pattern as Phase 1 messages).

### Handler security chain (in `cl-hive.py`)

All 4 new protocol message handlers follow the same 10-step security chain:

```
handle_did_credential_present(peer_id, payload, plugin):
    1. Dedup (proto_events)
    2. Rate limit check (per-peer sliding window)
    3. Timestamp freshness check (±300s)
    4. Membership verification (sender must be a hive member)
    5. Identity binding (peer_id == sender claimed in payload)
    6. Schema validation (domain is one of the 4 known profiles)
    7. Signature verification (checkmessage via RPC) — if `valid=False`, log at `warn` and drop; on RPC error (e.g. timeout), log at `warn` and return (do not crash)
    8. Self-issuance rejection (issuer != subject)
    9. Row cap check → store credential
    10. Update aggregation cache → relay to other members

handle_did_credential_revoke(peer_id, payload, plugin):
    Steps 1-5 same as above
    6. Verify revocation is for a credential we have stored
    7. Verify revoker == original issuer (only issuers can revoke)
    8. Signature verification of revocation message
    9. Mark credential as revoked (set revoked_at, revocation_reason)
    10. Relay revocation to other members

handle_mgmt_credential_present(peer_id, payload, plugin):
    Same 10-step chain as handle_did_credential_present

handle_mgmt_credential_revoke(peer_id, payload, plugin):
    Same chain as handle_did_credential_revoke, additionally:
    6b. Immediately invalidate any active sessions using this credential
```

### Integration with existing modules

**`planner.py`**: Before proposing expansion to a target, check `did_credential_mgr.get_credit_tier(target)`. Prefer targets with Recognized+ tier. Log reputation score in `hive_planner_log`.

**`membership.py`**: During auto-promotion evaluation, incorporate `hive:node` reputation from peer credentials as supplementary signal (not sole criterion — existing forwarding/uptime metrics remain primary). Add `governance_tier` column to `hive_members` table per [09-ARCHON-INTEGRATION.md](./09-ARCHON-INTEGRATION.md): `ALTER TABLE hive_members ADD COLUMN governance_tier TEXT NOT NULL DEFAULT 'basic'` (values: `basic`, `governance`).

**`settlement.py`**: Reputation tier determines settlement terms. Newcomer: full escrow required. Senior: extended credit lines. Store tier alongside settlement proposal.

### Background loop: `did_maintenance_loop`

```python
def did_maintenance_loop():
    """30-minute maintenance cycle for DID credential system."""
    # Startup delay: let node stabilize before maintenance work
    shutdown_event.wait(30)
    while not shutdown_event.is_set():
        try:
            if not database or not did_credential_mgr:
                shutdown_event.wait(1800)
                continue
            snap = config.snapshot()
            # 1. Cleanup expired credentials (remove expired_at < now)
            did_credential_mgr.cleanup_expired()
            # 2. Refresh stale aggregation cache entries (older than AGGREGATION_CACHE_TTL)
            did_credential_mgr.refresh_stale_aggregations()
            # 3. Auto-issue hive:node credentials for peers we have data on
            #    (forwarding stats from contribution.py, uptime from state_manager)
            #    Rate-limited: max 10 auto-issuances per cycle
            did_credential_mgr.auto_issue_node_credentials(rpc)
            # 4. Rebroadcast our credentials periodically (every 4h)
            #    Tracked via _last_rebroadcast timestamp to avoid redundant sends
            did_credential_mgr.rebroadcast_own_credentials(rpc)
        except Exception as e:
            plugin.log(f"cl-hive: did_maintenance error: {e}", level='error')
        shutdown_event.wait(1800)  # 30 min cycle
```

---

## HSM → DID Migration Path

Phases 1-3 use CLN's `signmessage`/`checkmessage` for all credential signatures. This produces zbase-encoded signatures over the lightning message prefix (`"Lightning Signed Message:"` + payload).

When Archon integration is deployed (see [09-ARCHON-INTEGRATION.md](./09-ARCHON-INTEGRATION.md)), the migration path is:

1. **Dual-signature period**: New credentials carry both a CLN HSM zbase signature and an Archon DID signature. Verifiers accept either.
2. **DID-to-pubkey binding**: A one-time `DID_BINDING_ATTESTATION` credential links the node's CLN pubkey to its `did:cid:*` identifier. This credential is signed by the CLN HSM and registered with the Archon gateway.
3. **Credential format upgrade**: Once all hive members support DID verification, new credentials are issued as W3C Verifiable Credentials (VC 2.0 JSON-LD) with DID signatures only. Old credentials remain valid until expiry.
4. **HSM sunset**: After a configurable migration window (default: 180 days), HSM-only credentials are no longer accepted for new issuance. Existing stored credentials retain their HSM signatures.

The `CredentialProfile` dataclass includes a `signature_type` field (`"hsm"` or `"did"` or `"dual"`) to track which regime each credential was issued under.

---

## Files Modified Summary

| File | Phase | Changes |
|------|-------|---------|
| **NEW** `modules/did_credentials.py` | 1 | DIDCredentialManager, credential profiles, aggregation, CreditTierResult |
| **NEW** `modules/management_schemas.py` | 2 | Schema registry, danger scoring, ManagementCredential |
| `modules/database.py` | 1-2 | 4 new tables, ~17 new methods, row caps (50K credentials, 10K cache, 1K mgmt creds, 100K receipts) |
| `modules/protocol.py` | 1, 3 | 4 new message types (32883-32889), factory/validation functions, rate limit constants |
| `modules/rpc_commands.py` | 1-2 | `did_credential_mgr` + `management_schema_registry` + `settlement_mgr` on HiveContext, ~10 handler functions |
| `cl-hive.py` | 1-3 | Import, init, dispatch entries, background loop, RPC wrappers, rate limiting |
| `tools/mcp-hive-server.py` | 1-2 | Add 10 new RPC methods to allowlist |
| **NEW** `tests/test_did_credentials.py` | 1 | Credential issuance, verification, aggregation, revocation, CreditTierResult |
| **NEW** `tests/test_management_schemas.py` | 2 | Schema validation, danger scoring, credential checks |
| **NEW** `tests/test_did_protocol.py` | 3 | Protocol message handling, relay, idempotency, rate limiting |

---

## Verification

1. **Unit tests**: `python3 -m pytest tests/test_did_credentials.py tests/test_management_schemas.py tests/test_did_protocol.py -v`
2. **Regression**: `python3 -m pytest tests/ -v` (all existing tests must pass)
3. **RPC smoke test**: `lightning-cli hive-did-profiles`, `lightning-cli hive-schema-list`
4. **Integration**: Issue credential via `hive-did-issue`, verify it appears in `hive-did-list`, check reputation via `hive-did-reputation`
5. **Rate limiting**: Verify that exceeding 20 presents/peer/hour results in silent drop
6. **Backwards compatibility**: Nodes without DID support must still participate in hive normally (all DID features are additive, never blocking)
7. **Migration prep**: Verify `CreditTierResult` includes all fields needed by settlement/planner integrations

---

## What's Deferred (Phases 4-6)

See [12-IMPLEMENTATION-PLAN-PHASE4-6.md](./12-IMPLEMENTATION-PLAN-PHASE4-6.md) for the complete plan.

| Phase | Spec | Requires |
|-------|------|----------|
| 4A | [03-CASHU-TASK-ESCROW](./03-CASHU-TASK-ESCROW.md) | Cashu Python SDK (NUT-10/11/14), mint integration |
| 4B | [06-HIVE-SETTLEMENTS](./06-HIVE-SETTLEMENTS.md) (extended) | Extends existing settlement.py with 8 new types |
| 5A | Nostr Transport | Nostr Python library (NIP-44), relay connections |
| 5B | [04-HIVE-MARKETPLACE](./04-HIVE-MARKETPLACE.md) + [05-NOSTR-MARKETPLACE](./05-NOSTR-MARKETPLACE.md) | Nostr transport + escrow |
| 5C | [07-HIVE-LIQUIDITY](./07-HIVE-LIQUIDITY.md) | Marketplace + settlements |
| 6 | [08-HIVE-CLIENT](./08-HIVE-CLIENT.md) | 3-plugin split (cl-hive-comms, cl-hive-archon, cl-hive) |

These require external Python libraries not currently in the dependency set. They will be planned once Phases 1-3 are deployed and validated.

**Node Provisioning** ([10-NODE-PROVISIONING.md](./10-NODE-PROVISIONING.md)) is operational infrastructure that runs alongside all phases. Provisioned nodes consume credentials from Phase 1 onward.
