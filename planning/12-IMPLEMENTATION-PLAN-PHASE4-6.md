# DID Ecosystem — Phases 4-6 Implementation Plan

## Context

This document covers the advanced phases of the DID ecosystem that require external Python libraries beyond `pyln-client`. It builds on Phases 1-3 (see [11-IMPLEMENTATION-PLAN.md](./11-IMPLEMENTATION-PLAN.md)) which deliver the credential foundation, management schemas, danger scoring, and credential exchange protocol using only CLN HSM crypto.

**Prerequisites**: Phases 1-3 must be deployed and validated before starting Phase 4.

**New external dependencies introduced**:
- Phase 4: Cashu Python SDK (NUT-10/11/14)
- Phase 5: Nostr Python library (NIP-44 encryption, WebSocket relay client)
- Phase 6: No new deps (architectural refactor into 3 plugins)

**Relationship to other specs**:
- [09-ARCHON-INTEGRATION.md](./09-ARCHON-INTEGRATION.md): Phase 6B (`cl-hive-archon`) is where Archon DID provisioning, `did:cid:*` binding, Dmail transport, and governance tier upgrades are wired in. Until then, CLN HSM + Nostr keypair serve as identity.
- [10-NODE-PROVISIONING.md](./10-NODE-PROVISIONING.md): Provisioned nodes are prime consumers of marketplace services (Phase 5B) and liquidity services (Phase 5C). The provisioning system's capital allocation model (6.18M–18.56M sats) informs bond amounts and credit tier thresholds in Phase 4B.

---

## Phase 4: Cashu Task Escrow + Extended Settlements

**Goal**: Trustless conditional payments via Cashu ecash tokens, 8 additional settlement types extending the existing `settlement.py`, bond system, credit tiers, and dispute resolution.

### Phase 4A: Cashu Escrow Foundation

#### New file: `modules/cashu_escrow.py`

```python
class CashuEscrowManager:
    """Cashu NUT-10/11/14 escrow ticket management."""

    MAX_ACTIVE_TICKETS = 500
    MAX_ESCROW_TICKET_ROWS = 50_000
    MAX_ESCROW_SECRET_ROWS = 50_000
    MAX_ESCROW_RECEIPT_ROWS = 100_000
    SECRET_RETENTION_DAYS = 90

    # Rate limits for mint HTTP calls (circuit breaker pattern)
    MINT_REQUEST_TIMEOUT = 10  # seconds
    MINT_MAX_RETRIES = 3
    MINT_CIRCUIT_BREAKER_THRESHOLD = 5   # failures before opening
    MINT_CIRCUIT_BREAKER_RESET = 60      # seconds in OPEN before HALF_OPEN
    MINT_HALF_OPEN_SUCCESS_THRESHOLD = 3 # successes in HALF_OPEN before CLOSED

    def __init__(self, database, plugin, rpc=None, our_pubkey="",
                 acceptable_mints=None):
```

**Acceptable mints configuration**: The `acceptable_mints` parameter is a list of mint URLs loaded from CLN plugin option `hive-cashu-mints` (comma-separated). If not configured, defaults to an empty list and escrow creation is disabled until at least one mint is configured. Example: `hive-cashu-mints=https://mint.example.com,https://mint2.example.com`.

**Threading model for mint HTTP calls**: All Cashu mint API calls (`POST /v1/checkstate`, `POST /v1/mint`, `POST /v1/swap`, etc.) are executed via `concurrent.futures.ThreadPoolExecutor(max_workers=2)` to avoid blocking the CLN event loop. Each call goes through a `MintCircuitBreaker` (same pattern as `bridge.py` `CircuitBreaker`): CLOSED → OPEN (after 5 failures) → HALF_OPEN (after 60s). Failed mints are logged and the ticket remains in `pending` status for retry on next cycle.

**Escrow token structure** (NUT-10 structured secret):
```json
["P2PK", {
  "nonce": "<unique>",
  "data": "<agent_did_pubkey_hex>",
  "tags": [
    ["hash", "<sha256_of_secret>"],
    ["locktime", "<unix_timestamp>"],
    ["refund", "<operator_pubkey_hex>"],
    ["sigflag", "SIG_ALL"]
  ]
}]
```

**Ticket types**:

| Type | Structure | Use Case |
|------|-----------|----------|
| Single-task | 1 token: P2PK + HTLC + timelock + refund | Individual management commands |
| Batch | N tokens: same P2PK, different HTLC hashes | Sequential task lists |
| Milestone | M tokens of increasing value, checkpoint secrets | Large multi-step operations |
| Performance | Base token + bonus token (separate conditions) | Aligned-incentive compensation |

**Key methods**:
- `create_ticket(agent_id, task_schema, danger_score, amount_sats, mint_url)` → mint escrow token with conditions
- `validate_ticket(token)` → check mint NUT support, verify conditions, pre-flight `POST /v1/checkstate`
- `generate_secret(task_id)` → create and persist HTLC secret for task
- `reveal_secret(task_id)` → return preimage on task completion
- `redeem_ticket(token, preimage, agent_privkey)` → redeem with mint
- `check_refund_eligible(token)` → check if timelock has passed for operator reclaim
- `get_pricing(danger_score, reputation_tier)` → dynamic pricing based on [02-FLEET-MANAGEMENT.md](./02-FLEET-MANAGEMENT.md)
- `cleanup_expired_tickets()` → mark expired tickets, attempt refund via timelock path
- `retry_pending_operations()` → retry failed mint operations (create/redeem) for tickets in `pending` status, respecting circuit breaker state per mint
- `prune_old_secrets()` → delete revealed secrets older than `SECRET_RETENTION_DAYS` (90 days) from `escrow_secrets`
- `get_mint_status(mint_url)` → return circuit breaker state for a mint

**Danger-to-pricing mapping**:

| Danger | Base Cost | Escrow Window | Reputation Modifier |
|--------|-----------|---------------|---------------------|
| 1-2 | 0-5 sats | 1 hour | Novice 1.5x, Proven 0.5x |
| 3-4 | 5-25 sats | 2-6 hours | Novice 1.5x, Proven 0.5x |
| 5-6 | 25-100 sats | 6-24 hours | Novice 1.5x, Proven 0.5x |
| 7-8 | 100-500 sats | 24-72 hours | Novice 1.5x, Proven 0.5x |
| 9-10 | 500+ sats | 72+ hours | Novice 1.5x, Proven 0.5x |

#### New DB tables

```sql
CREATE TABLE IF NOT EXISTS escrow_tickets (
    ticket_id TEXT PRIMARY KEY,
    ticket_type TEXT NOT NULL,          -- single/batch/milestone/performance
    agent_id TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    mint_url TEXT NOT NULL,
    amount_sats INTEGER NOT NULL,
    token_json TEXT NOT NULL,           -- serialized Cashu token
    htlc_hash TEXT NOT NULL,            -- H(secret)
    timelock INTEGER NOT NULL,          -- refund deadline
    danger_score INTEGER NOT NULL,
    schema_id TEXT,
    action TEXT,
    status TEXT NOT NULL DEFAULT 'active', -- active/redeemed/refunded/expired
    created_at INTEGER NOT NULL,
    redeemed_at INTEGER,
    refunded_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_escrow_agent ON escrow_tickets(agent_id, status);
CREATE INDEX IF NOT EXISTS idx_escrow_status ON escrow_tickets(status, timelock);

CREATE TABLE IF NOT EXISTS escrow_secrets (
    task_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    secret_hex TEXT NOT NULL,           -- HTLC preimage (see encryption note below)
    hash_hex TEXT NOT NULL,             -- H(secret) for verification
    revealed_at INTEGER,
    FOREIGN KEY (ticket_id) REFERENCES escrow_tickets(ticket_id)
);

CREATE TABLE IF NOT EXISTS escrow_receipts (
    receipt_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    schema_id TEXT NOT NULL,
    action TEXT NOT NULL,
    params_json TEXT NOT NULL,
    result_json TEXT,
    success INTEGER NOT NULL,           -- 0=failed, 1=success
    preimage_revealed INTEGER NOT NULL DEFAULT 0,
    agent_signature TEXT,
    node_signature TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (ticket_id) REFERENCES escrow_tickets(ticket_id)
);
CREATE INDEX IF NOT EXISTS idx_escrow_receipt_ticket ON escrow_receipts(ticket_id);
```

**Secret encryption at rest**: The `secret_hex` column in `escrow_secrets` is encrypted using the node's HSM-derived key. Encryption: `signmessage("escrow_key_derivation")` produces a deterministic key; XOR the secret with the first 32 bytes of this signature. This is symmetric, deterministic, and requires no new dependencies. The key is derived once at startup and held in memory only.

Row caps: `MAX_ESCROW_TICKET_ROWS = 50_000`, `MAX_ESCROW_SECRET_ROWS = 50_000`, `MAX_ESCROW_RECEIPT_ROWS = 100_000`.

#### External dependency: Cashu Python SDK

```python
# Required mint capabilities (checked at startup):
# - NUT-10: Structured secret format
# - NUT-11: P2PK signature conditions
# - NUT-14: HTLC hash-lock + timelock
# - NUT-07: Token state check (POST /v1/checkstate)

# DID-to-pubkey derivation (until Archon integration):
# Use CLN node pubkey as the P2PK lock key
# Agent's CLN pubkey serves as their DID-derived secp256k1 key
```

#### New RPC commands

| Command | Description |
|---------|-------------|
| `hive-escrow-create` | Create escrow ticket for a task |
| `hive-escrow-list` | List active escrow tickets |
| `hive-escrow-redeem` | Redeem a ticket (agent side) |
| `hive-escrow-refund` | Reclaim expired ticket (operator side) |
| `hive-escrow-receipt` | Get signed receipt for a completed task |

#### Background loop: `escrow_maintenance_loop`

```python
def escrow_maintenance_loop():
    """15-minute maintenance cycle for escrow ticket lifecycle."""
    shutdown_event.wait(30)  # startup delay
    while not shutdown_event.is_set():
        try:
            if not database or not cashu_escrow_mgr:
                shutdown_event.wait(900)
                continue
            # 1. Check for expired tickets → attempt timelock refund
            cashu_escrow_mgr.cleanup_expired_tickets()
            # 2. Retry failed mint operations (circuit breaker permitting)
            cashu_escrow_mgr.retry_pending_operations()
            # 3. Prune old secrets beyond SECRET_RETENTION_DAYS
            cashu_escrow_mgr.prune_old_secrets()
        except Exception as e:
            plugin.log(f"cl-hive: escrow_maintenance error: {e}", level='error')
        shutdown_event.wait(900)  # 15 min cycle
```

---

### Phase 4B: Extended Settlements

#### Modifications to `modules/settlement.py`

Extend the existing settlement module with 8 additional settlement types beyond the current routing revenue sharing. **Note**: This creates tight coupling between `settlement.py` and several other modules (`cashu_escrow.py`, `did_credentials.py`). To manage this, the extended settlement types are implemented as a `SettlementTypeRegistry` class within `settlement.py` that accepts injected dependencies rather than importing them directly. Each settlement type is a `SettlementTypeHandler` with `calculate()`, `verify_receipt()`, and `execute()` methods.

**9 settlement types**:

| # | Type | Formula | Proof |
|---|------|---------|-------|
| 1 | Routing Revenue | `share = total_fee × contribution / Σcontributions` | `HTLCForwardReceipt` chain |
| 2 | Rebalancing Cost | `cost = fees_through_B + liquidity_cost + risk_premium` | `RebalanceReceipt` dual-signed |
| 3 | Channel Leasing | `cost = capacity × rate_ppm × duration / 365` | `LeaseHeartbeat` attestations |
| 4 | Cooperative Splice | `share = contribution / total_capacity_after_splice` | On-chain splice tx + `SpliceReceipt` |
| 5 | Shared Channel Open | Same as Type 4 for new channels | Funding tx inputs + `SharedChannelReceipt` |
| 6 | Pheromone Market | `cost = base_fee + priority × multiplier` | Pay-for-performance HTLC |
| 7 | Intelligence Sharing | `cost = base_fee + freshness_premium × recency` | 70/30 base/bonus split |
| 8 | Penalty | `penalty = base × severity × repeat_multiplier` | N/2+1 quorum confirmation |
| 9 | Advisor Fee | `bonus = max(0, revenue_delta) × share_pct` | `AdvisorFeeReceipt` dual-signed |

**New protocol messages** (added to `protocol.py`):

| Message | ID | Purpose | Rate Limit |
|---------|------|---------|------------|
| `SETTLEMENT_RECEIPT` | 32891 | Generic signed receipt for any settlement type | 30/peer/hour |
| `BOND_POSTING` | 32893 | Announce bond deposit | 5/peer/hour |
| `BOND_SLASH` | 32895 | Announce bond forfeiture | 5/peer/hour |
| `NETTING_PROPOSAL` | 32897 | Bilateral/multilateral netting proposal | 10/peer/hour |
| `NETTING_ACK` | 32899 | Acknowledge netting computation | 10/peer/hour |
| `VIOLATION_REPORT` | 32901 | Report policy violation | 5/peer/hour |
| `ARBITRATION_VOTE` | 32903 | Cast arbitration vote | 5/peer/hour |

All 7 message types added to `RELIABLE_MESSAGE_TYPES`. Rate limits enforced per-peer via sliding window.

`NETTING_ACK` (32899) is a direct response to `NETTING_PROPOSAL` (32897), so add to `IMPLICIT_ACK_MAP`: `32899: 32897` with `IMPLICIT_ACK_MATCH_FIELD[32899] = "window_id"`. This allows the outbox to match netting acknowledgements to their proposals.

Factory functions follow the same pattern as Phase 1-3: `create_*()` returns unsigned serialized bytes with a `str(uuid.uuid4())` event_id. Signing payloads use `json.dumps(..., sort_keys=True, separators=(',',':'))` for deterministic serialization.

**Handler security chain for BOND_SLASH** (critical — involves fund forfeiture):

```
handle_bond_slash(peer_id, payload, plugin):
    1. Dedup (proto_events)
    2. Rate limit check
    3. Timestamp freshness (±300s)
    4. Membership verification (sender must be admin or panel member)
    5. Identity binding
    6. Verify dispute_id references a resolved dispute with outcome='upheld'
    7. Verify slash_amount <= bond.amount_sats - bond.slashed_amount
    8. Verify panel vote quorum (N/2+1 votes for 'upheld')
    9. Verify each panel vote signature individually
    10. Apply slash → update bond → broadcast confirmation
```

All other Phase 4B handlers follow the standard 10-step security chain from Phase 3.

#### Bond system

```sql
CREATE TABLE IF NOT EXISTS settlement_bonds (
    bond_id TEXT PRIMARY KEY,
    peer_id TEXT NOT NULL,
    amount_sats INTEGER NOT NULL,
    token_json TEXT,                -- Cashu token (NUT-11 3-of-5 multisig)
    posted_at INTEGER NOT NULL,
    timelock INTEGER NOT NULL,      -- 6-month refund path
    tier TEXT NOT NULL,             -- observer/basic/full/liquidity/founding
    slashed_amount INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active', -- active/slashed/refunded
    UNIQUE(peer_id)
);

CREATE TABLE IF NOT EXISTS settlement_obligations (
    obligation_id TEXT PRIMARY KEY,
    settlement_type INTEGER NOT NULL, -- 1-9
    from_peer TEXT NOT NULL,
    to_peer TEXT NOT NULL,
    amount_sats INTEGER NOT NULL,
    window_id TEXT NOT NULL,        -- settlement window identifier
    receipt_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending', -- pending/netted/settled/disputed
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obligation_window ON settlement_obligations(window_id, status);
CREATE INDEX IF NOT EXISTS idx_obligation_peers ON settlement_obligations(from_peer, to_peer);

CREATE TABLE IF NOT EXISTS settlement_disputes (
    dispute_id TEXT PRIMARY KEY,
    obligation_id TEXT NOT NULL,
    filing_peer TEXT NOT NULL,
    respondent_peer TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    panel_members_json TEXT,        -- selected arbitration panel
    votes_json TEXT,                -- panel votes
    outcome TEXT,                   -- upheld/rejected/partial
    slash_amount INTEGER DEFAULT 0,
    filed_at INTEGER NOT NULL,
    resolved_at INTEGER,
    FOREIGN KEY (obligation_id) REFERENCES settlement_obligations(obligation_id)
);
```

Row caps: `MAX_SETTLEMENT_BOND_ROWS = 1_000`, `MAX_SETTLEMENT_OBLIGATION_ROWS = 100_000`, `MAX_SETTLEMENT_DISPUTE_ROWS = 10_000`.

#### Credit tier integration

Uses `did_credential_mgr.get_credit_tier()` from Phase 1 to determine settlement terms:

| Tier | Credit Line | Settlement Window | Escrow Model |
|------|-------------|-------------------|--------------|
| Newcomer (0-59) | 0 sats | Per-event | Pre-paid escrow |
| Recognized (60-74) | 10,000 sats | Hourly batch | Escrow above credit line |
| Trusted (75-84) | 50,000 sats | Daily batch | Bilateral netting |
| Senior (85-100) | 200,000 sats | Weekly batch | Multilateral netting |

#### Netting engine

```python
class NettingEngine:
    """Bilateral and multilateral obligation netting."""

    def bilateral_net(self, peer_a, peer_b, window_id):
        """Net obligations between two peers. Returns single net payment.
        Uses deterministic JSON serialization (sort_keys=True, separators=(',',':'))
        for obligation hashing to ensure all parties compute identical net amounts."""

    def multilateral_net(self, obligations, window_id):
        """Multilateral netting across all peers. Minimizes total payments.
        Uses cycle detection in obligation graph.
        Reduces N² obligations to ≤N payments.
        All intermediate computations use integer sats (no floats) to avoid
        rounding disagreements between peers."""
```

#### Dispute resolution

Arbitration panel selection:
```python
def select_panel(dispute_id, block_hash, eligible_members):
    """Deterministic panel selection using stake-weighted randomness.

    block_hash: obtained from CLN 'getinfo' response field 'blockheight',
    then 'getblock' via bitcoin-cli (or CLN's 'getchaininfo' if available).
    Uses the block hash at the height when the dispute was filed.
    This ensures all nodes select the same panel deterministically.

    tenure_days: computed from hive_members.joined_at to dispute filing time.
    bond: from settlement_bonds.amount_sats for the member.
    Members without bonds (tenure_days used alone) get weight = sqrt(tenure_days).
    """
    seed = sha256(dispute_id + block_hash)
    weights = {m: (m.bond or 0) + sqrt(m.tenure_days) for m in eligible_members}
    return weighted_sample(seed, weights, k=min(7, len(eligible_members)))
```

Panel sizes: 7 members (5-of-7 majority) for >=15 eligible, 5 members (3-of-5) for 10-14, 3 members (2-of-3) for 5-9, bilateral negotiation for <5.

---

## Phase 5: Nostr Transport + Marketplace + Liquidity

**Goal**: Public marketplace layer using Nostr for discovery, NIP-44 encrypted DMs for management command transport, and a 9-service liquidity marketplace.

### Phase 5A: Nostr Transport Layer

#### New file: `modules/nostr_transport.py`

```python
class NostrTransport:
    """Nostr WebSocket relay client with NIP-44 encryption.

    Threading model: Nostr WebSocket connections run in a dedicated daemon thread
    with its own asyncio event loop (asyncio.new_event_loop()). The CLN plugin's
    synchronous code communicates with the Nostr thread via thread-safe queues:
    - _outbound_queue: CLN thread → Nostr thread (events to publish)
    - _inbound_queue: Nostr thread → CLN thread (received events)
    The Nostr thread's event loop manages all WebSocket connections via asyncio.
    CLN dispatch reads _inbound_queue in the existing message processing flow.
    """

    DEFAULT_RELAYS = [
        "wss://nos.lol",
        "wss://relay.damus.io",
    ]
    SEARCH_RELAYS = ["wss://relay.nostr.band"]
    PROFILE_RELAYS = ["wss://purplepag.es"]

    MAX_RELAY_CONNECTIONS = 8
    RECONNECT_BACKOFF_MAX = 300  # 5 min max backoff

    def __init__(self, plugin, database, privkey_hex=None):
```

**Key methods**:
- `start()` → spawn daemon thread with asyncio event loop, connect to relays
- `stop()` → signal shutdown, join thread with timeout
- `publish(event)` → queue event for signing and publishing to >=3 relays
- `subscribe(filters, callback)` → subscribe to event kinds with filters
- `send_dm(recipient_pubkey, plaintext)` → NIP-44 encrypt and queue for publish
- `receive_dm(callback)` → register callback for decrypted incoming NIP-44 DMs
- `get_status()` → return connection status for all relays

**Nostr keypair management**:
- Auto-generate secp256k1 keypair on first run using `coincurve` library
- Store in `nostr_state` table with encryption (same HSM-derived key pattern as `escrow_secrets`)
- The Nostr keypair is **separate** from the CLN node keypair — Nostr uses schnorr signatures (BIP-340) while CLN uses ECDSA. They cannot share keys directly.
- If `cl-hive-archon` installed later, a `DID_NOSTR_BINDING` attestation links the Nostr pubkey to the DID and CLN pubkey.
- Until then, Nostr pubkey serves as marketplace identity, with CLN pubkey cross-referenced in the Nostr profile event.

#### New DB table

```sql
CREATE TABLE IF NOT EXISTS nostr_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL              -- encrypted for sensitive keys (privkey)
);
-- Stores: privkey (encrypted), pubkey, relay_list, last_event_ids
-- This is a bounded KV store: max 100 keys enforced in application code.
-- Keys are prefixed: 'config:', 'relay:', 'event:' for namespacing.
```

Row cap: `MAX_NOSTR_STATE_ROWS = 100` (bounded KV store, not unbounded growth).

### Phase 5B: Advisor Marketplace

#### New file: `modules/marketplace.py`

```python
class MarketplaceManager:
    """Advisor marketplace — profiles, discovery, contracting, trials."""

    MAX_CACHED_PROFILES = 500
    PROFILE_STALE_DAYS = 90
    MAX_ACTIVE_TRIALS = 2
    TRIAL_COOLDOWN_DAYS = 14

    def __init__(self, database, plugin, nostr_transport, did_credential_mgr,
                 management_schema_registry, cashu_escrow_mgr):
```

**Key methods**:
- `discover_advisors(criteria)` → search cached profiles matching criteria (specialization, min_reputation, price range), return ranked list
- `publish_profile(profile)` → publish own advisor profile to Nostr relays (kind 38380)
- `propose_contract(advisor_did, node_id, scope, tier, pricing)` → send contract proposal via NIP-44 DM
- `accept_contract(contract_id)` → accept proposal, publish contract confirmation (kind 38383)
- `start_trial(contract_id)` → transition contract to trial status, create escrow ticket
- `evaluate_trial(contract_id)` → evaluate trial metrics against thresholds, return pass/fail/extended
- `terminate_contract(contract_id, reason)` → terminate contract, revoke management credential
- `cleanup_stale_profiles()` → expire profiles older than `PROFILE_STALE_DAYS` (90 days)
- `evaluate_expired_trials()` → auto-evaluate trials past their `end_at` deadline
- `check_contract_renewals()` → notify operator of contracts expiring within `notice_days`
- `republish_profile()` → re-publish own profile to Nostr (every 4h, tracked via timestamp)

**Nostr event kinds — Advisor services (38380-38389)**:

| Kind | Type | Content |
|------|------|---------|
| 38380 | Advisor Service Profile | Self-issued VC with capabilities, pricing, availability |
| 38381 | Advisor Service Offer | Specific engagement offer with terms |
| 38382 | Advisor RFP | Node requesting advisor services |
| 38383 | Contract Confirmation | Immutable dual-signed contract record |
| 38384 | Heartbeat Attestation | Ongoing engagement status |
| 38385 | Reputation Summary | Aggregated advisor reputation |

**Note**: Marketplace communication is Nostr-only — no new `protocol.py` message types are needed for Phase 5B. All marketplace events are published to Nostr relays and discovered there. Hive members may additionally gossip marketplace profile summaries via existing gossip mechanisms, but this is optional caching, not a new protocol message.

**Service specializations** (from [04-HIVE-MARKETPLACE.md](./04-HIVE-MARKETPLACE.md)):
- `fee-optimization`, `high-volume-routing`, `rebalancing`, `expansion-planning`
- `emergency-response`, `splice-management`, `full-stack`, `monitoring-only`
- `liquidity-services`

**Contract lifecycle**:

```
Discovery → Proposal → Negotiation (NIP-44 DM) → Trial → Evaluation → Full Contract → Renewal/Exit
```

**Trial protections**:
- Max 2 concurrent trials per node
- 14-day cooldown between trials with different advisors (same scope)
- Graduated pricing: 1st trial standard, 2nd at 2x, 3rd+ at 3x within 90 days
- Trial evaluation: `actions_taken >= 10`, `uptime_pct >= 95`, `revenue_delta >= -5%`
- **Trial sequence tracking**: Each trial increments a `sequence_number` per (node_id, scope) pair, stored in `marketplace_trials`. The graduated pricing multiplier is computed from `SELECT COUNT(*) FROM marketplace_trials WHERE node_id=? AND scope=? AND start_at > ?` (90-day window).

**Multi-advisor conflict resolution**:
- Scope isolation via `allowed_schemas` in management credentials
- Indirect conflict detection: `conflict_score(action_A, action_B)` based on schema interaction, temporal proximity, channel overlap
- Action cooldown (default 300s) prevents rapid conflicting changes
- Escalation to operator when conflict score exceeds threshold

**Ranking algorithm**:
```
match_score = 0.35 × reputation + 0.25 × capability_match + 0.15 × specialization
            + 0.10 × price_fit + 0.10 × availability + 0.05 × freshness
```

#### New DB tables

```sql
CREATE TABLE IF NOT EXISTS marketplace_profiles (
    advisor_did TEXT PRIMARY KEY,
    profile_json TEXT NOT NULL,         -- full HiveServiceProfile VC
    nostr_pubkey TEXT,
    version TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,    -- primary/secondary/experimental
    pricing_json TEXT NOT NULL,
    reputation_score INTEGER DEFAULT 0,
    last_seen INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'gossip' -- gossip/nostr/archon
);
CREATE INDEX IF NOT EXISTS idx_mp_reputation ON marketplace_profiles(reputation_score DESC);

CREATE TABLE IF NOT EXISTS marketplace_contracts (
    contract_id TEXT PRIMARY KEY,
    advisor_did TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed', -- proposed/trial/active/terminated
    tier TEXT NOT NULL,
    scope_json TEXT NOT NULL,           -- allowed schemas and constraints
    pricing_json TEXT NOT NULL,
    sla_json TEXT,
    trial_start INTEGER,
    trial_end INTEGER,
    contract_start INTEGER,
    contract_end INTEGER,
    auto_renew INTEGER NOT NULL DEFAULT 0,
    notice_days INTEGER NOT NULL DEFAULT 7,
    created_at INTEGER NOT NULL,
    terminated_at INTEGER,
    termination_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_contract_advisor ON marketplace_contracts(advisor_did, status);
CREATE INDEX IF NOT EXISTS idx_contract_status ON marketplace_contracts(status);

CREATE TABLE IF NOT EXISTS marketplace_trials (
    trial_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    advisor_did TEXT NOT NULL,
    node_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    sequence_number INTEGER NOT NULL DEFAULT 1, -- per (node_id, scope) for graduated pricing
    flat_fee_sats INTEGER NOT NULL,
    start_at INTEGER NOT NULL,
    end_at INTEGER NOT NULL,
    evaluation_json TEXT,              -- metrics at trial end
    outcome TEXT,                      -- pass/fail/extended
    FOREIGN KEY (contract_id) REFERENCES marketplace_contracts(contract_id)
);
CREATE INDEX IF NOT EXISTS idx_trial_node_scope ON marketplace_trials(node_id, scope, start_at);
```

Row caps: `MAX_MARKETPLACE_PROFILE_ROWS = 5_000`, `MAX_MARKETPLACE_CONTRACT_ROWS = 10_000`, `MAX_MARKETPLACE_TRIAL_ROWS = 10_000`.

#### New RPC commands

| Command | Description |
|---------|-------------|
| `hive-marketplace-discover` | Search for advisors matching criteria |
| `hive-marketplace-profile` | View/publish own advisor profile |
| `hive-marketplace-propose` | Propose contract to an advisor |
| `hive-marketplace-accept` | Accept a contract proposal |
| `hive-marketplace-trial` | Start/evaluate a trial period |
| `hive-marketplace-terminate` | Terminate a contract |
| `hive-marketplace-status` | View active contracts and their status |

#### Background loop: `marketplace_maintenance_loop`

```python
def marketplace_maintenance_loop():
    """1-hour maintenance cycle for marketplace state."""
    shutdown_event.wait(30)  # startup delay
    while not shutdown_event.is_set():
        try:
            if not database or not marketplace_mgr:
                shutdown_event.wait(3600)
                continue
            # 1. Expire stale profiles (>PROFILE_STALE_DAYS)
            marketplace_mgr.cleanup_stale_profiles()
            # 2. Check trial deadlines → auto-evaluate expired trials
            marketplace_mgr.evaluate_expired_trials()
            # 3. Check contract renewals → notify operator of upcoming expirations
            marketplace_mgr.check_contract_renewals()
            # 4. Republish own profile to Nostr (every 4h)
            marketplace_mgr.republish_profile()
        except Exception as e:
            plugin.log(f"cl-hive: marketplace_maintenance error: {e}", level='error')
        shutdown_event.wait(3600)  # 1 hour cycle
```

### Phase 5C: Liquidity Marketplace

#### New file: `modules/liquidity_marketplace.py`

```python
class LiquidityMarketplaceManager:
    """9-service liquidity marketplace with Nostr discovery."""

    MAX_ACTIVE_LEASES = 50
    MAX_ACTIVE_OFFERS = 200
    HEARTBEAT_MISS_THRESHOLD = 3  # consecutive misses terminate lease

    def __init__(self, database, plugin, nostr_transport, cashu_escrow_mgr,
                 settlement_mgr, did_credential_mgr):
```

**Key methods**:
- `discover_offers(service_type, min_capacity, max_rate)` → search cached offers matching criteria
- `publish_offer(service_type, capacity, duration, pricing)` → publish offer to Nostr (kind 38901)
- `accept_offer(offer_id)` → accept offer, create lease, mint escrow tickets
- `send_heartbeat(lease_id)` → create and publish heartbeat attestation (kind 38904)
- `verify_heartbeat(lease_id, heartbeat)` → verify heartbeat, reveal preimage if valid
- `check_heartbeat_deadlines()` → increment `missed_heartbeats` for overdue leases
- `terminate_dead_leases()` → terminate leases exceeding `HEARTBEAT_MISS_THRESHOLD` (3 misses)
- `expire_stale_offers()` → mark offers past their `expires_at` as expired
- `republish_offers()` → re-publish active offers to Nostr (every 2h, tracked via timestamp)
- `get_lease_status(lease_id)` → return lease details with heartbeat history

**9 liquidity service types**:

| # | Service | Escrow Model | Pricing Model |
|---|---------|-------------|---------------|
| 1 | Channel Leasing | Milestone (per heartbeat) | Sat-hours or yield curve |
| 2 | Liquidity Pools | Pool share VCs | Revenue share |
| 3 | JIT Liquidity | Single ticket (preimage = funding txid) | Flat fee |
| 4 | Sidecar Channels | 3-party NUT-11 2-of-2 multisig | Flat fee |
| 5 | Liquidity Swaps | Nets to zero (bilateral settlement) | No cost (mutual benefit) |
| 6 | Submarine Swaps | Native HTLC (no extra escrow) | Flat fee + on-chain fee |
| 7 | Turbo Channels | Single ticket (premium rate) | Sat-hours + 10-25% premium |
| 8 | Balanced Channels | Two-part: push + lease milestones | Sat-hours |
| 9 | Liquidity Insurance | Daily premium + provider bond | Daily premium rate |

**Nostr event kinds — Liquidity services (38900-38909)**:

| Kind | Type | Content |
|------|------|---------|
| 38900 | Provider Profile | Self-issued VC with capacity, rates, services |
| 38901 | Capacity Offer | Specific liquidity offer with terms |
| 38902 | Liquidity RFP | Node requesting liquidity |
| 38903 | Contract Confirmation | Immutable dual-signed lease/service record |
| 38904 | Lease Heartbeat | Ongoing capacity attestation |
| 38905 | Provider Reputation Summary | Aggregated provider reputation |

**Note**: Like Phase 5B, liquidity marketplace communication is Nostr-only — no new `protocol.py` message types. Lease heartbeats between hive members may optionally piggyback on existing gossip messages for redundancy, but the canonical heartbeat is a Nostr event.

**Lease lifecycle** (canonical example — Channel Leasing):
```
1. Client discovers offer (38901) or publishes RFP (38902)
2. NIP-44 DM negotiation → quote
3. Client mints milestone escrow tickets (1 per heartbeat period)
4. Provider opens channel
5. Each period: provider sends LeaseHeartbeat → client verifies → reveals preimage
6. Provider redeems period ticket from mint
7. 3 consecutive missed heartbeats → lease terminated → remaining tickets refund via timelock
```

**Heartbeat rate limiting**: Heartbeats are rate-limited to 1 per `heartbeat_interval` (default 3600s) per lease. Heartbeats arriving faster than `heartbeat_interval * 0.5` are silently dropped. This prevents heartbeat flooding while allowing reasonable clock drift.

**6 pricing models**:

| Model | Formula | Use Case |
|-------|---------|----------|
| Sat-hours | `capacity × hours × rate_per_sat_hour` | Channel leasing (base) |
| Flat fee | `base + capacity × rate_ppm` | JIT, sidecar, one-shot |
| Revenue share | `% of routing revenue through leased channel` | Aligned incentives |
| Yield curve | Duration discounts: spot 2x, 7d 1.5x, 30d 1x, 90d 0.8x, 365d 0.6x | Long-term leases |
| Auction | Sealed-bid for capacity blocks | High-demand corridors |
| Dynamic | `base × demand_multiplier × scarcity_multiplier` | Real-time pricing |

#### New DB tables

```sql
CREATE TABLE IF NOT EXISTS liquidity_offers (
    offer_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    service_type INTEGER NOT NULL,     -- 1-9
    capacity_sats INTEGER NOT NULL,
    duration_hours INTEGER,
    pricing_model TEXT NOT NULL,
    rate_json TEXT NOT NULL,
    min_reputation INTEGER DEFAULT 0,
    nostr_event_id TEXT,
    status TEXT NOT NULL DEFAULT 'active', -- active/filled/expired/withdrawn
    created_at INTEGER NOT NULL,
    expires_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_liq_offer_type ON liquidity_offers(service_type, status);

CREATE TABLE IF NOT EXISTS liquidity_leases (
    lease_id TEXT PRIMARY KEY,
    offer_id TEXT,
    provider_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    service_type INTEGER NOT NULL,
    channel_id TEXT,
    capacity_sats INTEGER NOT NULL,
    start_at INTEGER NOT NULL,
    end_at INTEGER NOT NULL,
    heartbeat_interval INTEGER NOT NULL DEFAULT 3600,
    last_heartbeat INTEGER,
    missed_heartbeats INTEGER NOT NULL DEFAULT 0,
    total_paid_sats INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active', -- active/completed/terminated
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lease_status ON liquidity_leases(status);
CREATE INDEX IF NOT EXISTS idx_lease_provider ON liquidity_leases(provider_id);

CREATE TABLE IF NOT EXISTS liquidity_heartbeats (
    heartbeat_id TEXT PRIMARY KEY,
    lease_id TEXT NOT NULL,
    period_number INTEGER NOT NULL,
    channel_id TEXT NOT NULL,
    capacity_sats INTEGER NOT NULL,
    remote_balance_sats INTEGER NOT NULL,
    provider_signature TEXT NOT NULL,
    client_verified INTEGER NOT NULL DEFAULT 0,
    preimage_revealed INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (lease_id) REFERENCES liquidity_leases(lease_id)
);
CREATE INDEX IF NOT EXISTS idx_heartbeat_lease ON liquidity_heartbeats(lease_id, period_number);
```

Row caps: `MAX_LIQUIDITY_OFFER_ROWS = 10_000`, `MAX_LIQUIDITY_LEASE_ROWS = 10_000`, `MAX_HEARTBEAT_ROWS = 500_000`.

#### Nostr spam resistance (4 layers)

1. **NIP-13 Proof of Work**: Profiles/offers >= 20 bits, contracts >= 16 bits, heartbeats >= 12 bits
2. **DID bond verification**: Events with `did-nostr-proof` tag prioritized
3. **Relay-side rate limiting**: Profiles 1/hr, offers 10/hr, RFPs 5/hr, heartbeats 1/10min
4. **Client-side trust scoring**: DID binding +50, PoW +1/bit, reputation +30, contracts +20

#### New RPC commands

| Command | Description |
|---------|-------------|
| `hive-liquidity-discover` | Search for liquidity offers |
| `hive-liquidity-offer` | Publish a liquidity offer |
| `hive-liquidity-request` | Request liquidity (publish RFP) |
| `hive-liquidity-lease` | Accept an offer and start a lease |
| `hive-liquidity-heartbeat` | Send/verify lease heartbeat |
| `hive-liquidity-lease-status` | View active leases (**renamed** from `hive-liquidity-status` to avoid conflict with existing RPC command at cl-hive.py:13982) |
| `hive-liquidity-terminate` | Terminate a lease |

#### Background loop: `liquidity_maintenance_loop`

```python
def liquidity_maintenance_loop():
    """10-minute maintenance cycle for liquidity lease lifecycle."""
    shutdown_event.wait(30)  # startup delay
    while not shutdown_event.is_set():
        try:
            if not database or not liquidity_mgr:
                shutdown_event.wait(600)
                continue
            # 1. Check heartbeat deadlines → increment missed_heartbeats
            liquidity_mgr.check_heartbeat_deadlines()
            # 2. Terminate leases with >= HEARTBEAT_MISS_THRESHOLD consecutive misses
            liquidity_mgr.terminate_dead_leases()
            # 3. Expire old offers
            liquidity_mgr.expire_stale_offers()
            # 4. Republish active offers to Nostr (every 2h)
            liquidity_mgr.republish_offers()
        except Exception as e:
            plugin.log(f"cl-hive: liquidity_maintenance error: {e}", level='error')
        shutdown_event.wait(600)  # 10 min cycle
```

---

## Wiring: Phase 4-5 in `cl-hive.py`

### HiveContext additions

Add the following fields to `HiveContext` in `rpc_commands.py` (extending Phase 1-3 additions):

| Field | Type | Phase | Initialized After |
|-------|------|-------|-------------------|
| `cashu_escrow_mgr` | `Optional[CashuEscrowManager]` | 4A | `did_credential_mgr` |
| `nostr_transport` | `Optional[NostrTransport]` | 5A | `cashu_escrow_mgr` |
| `marketplace_mgr` | `Optional[MarketplaceManager]` | 5B | `nostr_transport` |
| `liquidity_mgr` | `Optional[LiquidityMarketplaceManager]` | 5C | `marketplace_mgr` |

### Initialization order in `init()`

```python
# Phase 4A: Cashu escrow (after did_credential_mgr)
cashu_escrow_mgr = CashuEscrowManager(
    database, plugin, rpc, our_pubkey,
    acceptable_mints=plugin.get_option('hive-cashu-mints', '').split(',')
)

# Phase 4B: Extended settlement types (extend existing settlement_mgr)
settlement_mgr.register_extended_types(cashu_escrow_mgr, did_credential_mgr)

# Phase 5A: Nostr transport (start daemon thread)
nostr_transport = NostrTransport(plugin, database)
nostr_transport.start()

# Phase 5B: Marketplace (after nostr + escrow + credentials)
marketplace_mgr = MarketplaceManager(
    database, plugin, nostr_transport, did_credential_mgr,
    management_schema_registry, cashu_escrow_mgr
)

# Phase 5C: Liquidity marketplace (after marketplace + settlements)
liquidity_mgr = LiquidityMarketplaceManager(
    database, plugin, nostr_transport, cashu_escrow_mgr,
    settlement_mgr, did_credential_mgr
)
```

### Shutdown additions

```python
# In shutdown handler, before database close:
if nostr_transport:
    nostr_transport.stop()  # signal WebSocket thread shutdown, join with 5s timeout
```

### Dispatch additions

Add dispatch entries in `_dispatch_hive_message()` for all 7 Phase 4B protocol message types (32891-32903).

---

## Phase 6: Client Plugin Architecture (3-plugin split)

**Goal**: Refactor from monolithic `cl-hive.py` into 3 independently installable CLN plugins, enabling non-hive nodes to hire advisors and access liquidity without full hive membership.

### Architecture

```
Standalone (any node):
  cl-hive-comms ← Entry point: transport, schema handler, policy engine

Add DID identity:
  cl-hive-archon ← DID provisioning, credential verification, vault backup
    └── requires: cl-hive-comms

Full hive membership:
  cl-hive ← Gossip, topology, settlements, governance
    └── requires: cl-hive-comms
```

A fourth plugin, `cl-revenue-ops`, remains standalone and independent.

### Database architecture for 3-plugin split

**Shared database with per-plugin namespacing**: All three plugins share a single SQLite database file (`hive.sqlite3`) with WAL mode. Table ownership is namespaced:
- `cl-hive-comms` owns: `nostr_state`, `management_receipts`, `marketplace_*`, `liquidity_*`
- `cl-hive-archon` owns: `did_credentials`, `did_reputation_cache`, `archon_*`
- `cl-hive` owns: all existing tables plus `settlement_*`, `escrow_*`

Each plugin creates only its own tables in `initialize()`. Cross-plugin data access uses read-only queries (never writes to tables owned by other plugins). This avoids the complexity of IPC for data sharing while maintaining clear ownership boundaries.

**Migration from monolithic**: When upgrading from monolith to 3-plugin, the existing database is reused as-is. No migration needed — the new plugins simply create any missing tables they own.

### Phase 6A: `cl-hive-comms` plugin

#### New file: `cl-hive-comms.py`

The lightweight client entry point. Contains:

| Component | Responsibility | Source Module |
|-----------|---------------|---------------|
| **Schema Handler** | Receive management commands via Nostr DM or REST/rune, dispatch to CLN RPC, return signed receipts | `modules/management_schemas.py` |
| **Transport Abstraction** | Pluggable interface: Nostr DM (NIP-44), REST/rune. Future: Bolt 8, Archon Dmail | `modules/nostr_transport.py` |
| **Payment Manager** | Bolt11 (per-action), Bolt12 (subscription), L402 (API), Cashu (escrow) | `modules/cashu_escrow.py` |
| **Policy Engine** | Operator's last defense: presets (conservative/moderate/aggressive), custom rules, protected channels, quiet hours | NEW: `modules/policy_engine.py` |
| **Receipt Store** | Append-only hash-chained dual-signed SQLite log | `management_receipts` table |
| **Marketplace Client** | Publish/subscribe to kinds 38380+/38900+ | `modules/marketplace.py`, `modules/liquidity_marketplace.py` |

**Module dependencies for cl-hive-comms**:
- `modules/management_schemas.py` (Phase 2)
- `modules/nostr_transport.py` (Phase 5A)
- `modules/cashu_escrow.py` (Phase 4A)
- `modules/marketplace.py` (Phase 5B)
- `modules/liquidity_marketplace.py` (Phase 5C)
- `modules/config.py` (existing)
- `modules/database.py` (existing, creates only its own tables)
- NEW: `modules/policy_engine.py` (operator policy rules — see specification below)

#### New file: `modules/policy_engine.py`

```python
class PolicyEngine:
    """Operator's last-defense policy layer for management commands.

    Evaluates every incoming management command against operator-defined
    rules before execution. This is the final gate after credential
    verification and danger scoring.
    """

    PRESETS = {
        "conservative": {"max_danger": 4, "quiet_hours": True, "require_confirmation_above": 3},
        "moderate": {"max_danger": 6, "quiet_hours": False, "require_confirmation_above": 5},
        "aggressive": {"max_danger": 8, "quiet_hours": False, "require_confirmation_above": 7},
    }

    def __init__(self, database, plugin, preset="moderate"):
```

**Key methods**:
- `evaluate(schema_id, action, params, danger_score, agent_id)` → `PolicyResult(allowed, reason, requires_confirmation)`
- `set_preset(preset_name)` → apply a preset configuration
- `add_rule(rule)` → add custom policy rule (e.g. "block channel closes on weekends")
- `remove_rule(rule_id)` → remove a custom rule
- `set_protected_channels(channel_ids)` → channels that cannot be closed by any advisor
- `set_quiet_hours(start_hour, end_hour, timezone)` → block non-monitor actions during quiet hours
- `get_policy()` → return current policy configuration
- `list_rules()` → list all active rules (preset + custom)

**Policy rule types**:
- `max_danger`: Block actions above this danger score
- `quiet_hours`: Time window where only `hive:monitor/*` actions are allowed
- `protected_channels`: Channel IDs that cannot be targeted by `hive:channel/v1` close actions
- `daily_budget_sats`: Maximum sats in management fees per day
- `require_confirmation_above`: Danger score threshold for interactive confirmation
- `blocked_schemas`: Schemas entirely blocked from remote execution

**Storage**: Policy rules stored in `nostr_state` table (bounded KV store) with `policy:` key prefix.

**CLI commands**:
- `hive-client-discover` — search for advisors/liquidity
- `hive-client-authorize` — issue management credential to an advisor
- `hive-client-revoke` — revoke advisor access
- `hive-client-receipts` — view management action log
- `hive-client-policy` — view/edit policy engine rules
- `hive-client-status` — show active advisors, contracts, spending
- `hive-client-payments` — payment history and limits
- `hive-client-trial` — manage trial periods
- `hive-client-alias` — human-readable names for advisor DIDs
- `hive-client-identity` — show/manage Nostr identity

**Schema translation** (15 categories → CLN RPC):

| Schema | CLN RPC Calls |
|--------|---------------|
| `hive:monitor/v1` | `getinfo`, `listchannels`, `listforwards`, `listpeers` |
| `hive:fee-policy/v1` | `setchannel` |
| `hive:rebalance/v1` | `pay` (circular), Boltz API (swaps) |
| `hive:channel/v1` | `fundchannel`, `close` |
| `hive:config/v1` | `setconfig` |
| `hive:emergency/v1` | `close --force`, `disconnect` |

### Phase 6B: `cl-hive-archon` plugin

#### New file: `cl-hive-archon.py`

Adds DID identity layer on top of `cl-hive-comms`. See [09-ARCHON-INTEGRATION.md](./09-ARCHON-INTEGRATION.md) for the full Archon integration spec including governance tiers, Archon Polls, and the `governance_eligible_members` view.

| Component | Responsibility | Integration Point |
|-----------|---------------|-------------------|
| **DID Provisioning** | Auto-generate `did:cid:*` via public Archon gateway or local node | HTTP API to `archon.technology` or local Docker |
| **DID-Nostr Binding** | Attestation credential linking DID to Nostr pubkey | `DID_NOSTR_BINDING` credential |
| **DID-CLN Binding** | Attestation linking DID to CLN node pubkey | `DID_BINDING_ATTESTATION` from Phase 1-3 migration path |
| **Credential Manager** | Issue, verify, present, revoke DID credentials | Replaces HSM-based credentials from Phase 1-3 |
| **Governance Tier** | Upgrade from Basic to Governance participation | `governance_tier` column from 09-ARCHON-INTEGRATION |
| **Dmail Transport** | Register Archon Dmail as transport option in comms | Pluggable transport in `cl-hive-comms` |
| **Vault Backup** | Archon group vault for DID wallet, credentials, receipt chain, Cashu tokens | Archon vault API |
| **Shamir Recovery** | k-of-n threshold recovery for distributed trust | Archon recovery API |

**CLI commands** (from [09-ARCHON-INTEGRATION.md](./09-ARCHON-INTEGRATION.md)):
- `hive-archon-provision` — provision `did:cid:*` identity via gateway
- `hive-archon-bind-nostr` — create DID-Nostr binding attestation
- `hive-archon-bind-cln` — create DID-CLN binding attestation
- `hive-archon-status` — show DID identity status, bindings, governance tier
- `hive-archon-upgrade` — upgrade from Basic to Governance tier (requires DID + bond)
- `hive-poll-create` — create a governance poll (governance tier only)
- `hive-poll-status` — view poll status and vote tally
- `hive-vote` — cast a vote on an active poll (governance tier only)
- `hive-my-votes` — list own voting history

**Module dependencies for cl-hive-archon**:
- `modules/did_credentials.py` (Phase 1)
- `modules/config.py` (existing)
- `modules/database.py` (existing, creates only its own tables)
- Requires: `cl-hive-comms` plugin installed and active

**Sovereignty tiers**:

| Tier | Setup | DID Resolution | Trust Level |
|------|-------|---------------|-------------|
| No Archon (default) | Zero — auto-provision via public gateway | Remote | Minimal |
| Own Archon node | Docker compose | Local (self-sovereign) | Full |
| L402-gated Archon | Public gatekeeper | Remote (paid) | Moderate |

### Phase 6C: Refactor existing `cl-hive.py`

Extract modules that belong in `cl-hive-comms` or `cl-hive-archon`:
- Move Nostr transport → `cl-hive-comms`
- Move DID credential management → `cl-hive-archon`
- Move management schema handling → `cl-hive-comms`
- Keep gossip, topology, settlements, governance in `cl-hive`
- `cl-hive` detects presence of `cl-hive-comms` and `cl-hive-archon` via `plugin list` RPC call (same pattern as CLBoss detection in `clboss_bridge.py`)

**Migration path for existing nodes**:
1. Existing hive members: no changes needed (cl-hive continues to work as monolith)
2. New non-hive nodes: install `cl-hive-comms` only
3. Upgrade path: `cl-hive-comms` → add `cl-hive-archon` → add `cl-hive` → `hive-join --bond=50000`

---

## MCP Server Updates (All Phases)

Add the following to `_check_method_allowed()` in `tools/mcp-hive-server.py`:

**Phase 4A (Escrow)**: `hive-escrow-create`, `hive-escrow-list`, `hive-escrow-redeem`, `hive-escrow-refund`, `hive-escrow-receipt`

**Phase 5B (Marketplace)**: `hive-marketplace-discover`, `hive-marketplace-profile`, `hive-marketplace-propose`, `hive-marketplace-accept`, `hive-marketplace-trial`, `hive-marketplace-terminate`, `hive-marketplace-status`

**Phase 5C (Liquidity)**: `hive-liquidity-discover`, `hive-liquidity-offer`, `hive-liquidity-request`, `hive-liquidity-lease`, `hive-liquidity-heartbeat`, `hive-liquidity-lease-status`, `hive-liquidity-terminate`

**Phase 6A (Client)**: `hive-client-discover`, `hive-client-authorize`, `hive-client-revoke`, `hive-client-receipts`, `hive-client-policy`, `hive-client-status`, `hive-client-payments`, `hive-client-trial`, `hive-client-alias`, `hive-client-identity`

**Phase 6B (Archon)**: `hive-archon-provision`, `hive-archon-bind-nostr`, `hive-archon-bind-cln`, `hive-archon-status`, `hive-archon-upgrade`, `hive-poll-create`, `hive-poll-status`, `hive-vote`, `hive-my-votes`

---

## Security Notes

### Secret storage
- **Escrow secrets** (`escrow_secrets.secret_hex`): Encrypted at rest using HSM-derived symmetric key (see Phase 4A)
- **Nostr private key** (`nostr_state` where `key='config:privkey'`): Encrypted at rest using same HSM-derived key pattern
- **Bond tokens** (`settlement_bonds.token_json`): Contains Cashu tokens — read-only after posting, no encryption needed (tokens are already cryptographically bound to conditions)

### Network call isolation
- **Cashu mint HTTP calls**: Isolated in `ThreadPoolExecutor(2)` with circuit breaker (Phase 4A)
- **Nostr WebSocket connections**: Isolated in dedicated daemon thread with asyncio event loop (Phase 5A)
- **Archon HTTP calls** (Phase 6B): Same `ThreadPoolExecutor` pattern as Cashu, separate circuit breaker instance

### Rate limiting summary (all new protocol messages)

| Message Type | ID | Rate Limit |
|--------------|----|------------|
| `SETTLEMENT_RECEIPT` | 32891 | 30/peer/hour |
| `BOND_POSTING` | 32893 | 5/peer/hour |
| `BOND_SLASH` | 32895 | 5/peer/hour |
| `NETTING_PROPOSAL` | 32897 | 10/peer/hour |
| `NETTING_ACK` | 32899 | 10/peer/hour |
| `VIOLATION_REPORT` | 32901 | 5/peer/hour |
| `ARBITRATION_VOTE` | 32903 | 5/peer/hour |

---

## Files Summary (All Phases)

### Phase 4: Cashu Escrow + Extended Settlements

| File | Type | Changes |
|------|------|---------|
| **NEW** `modules/cashu_escrow.py` | New | CashuEscrowManager, MintCircuitBreaker, ticket types, pricing |
| `modules/settlement.py` | Modify | SettlementTypeRegistry, 8 new settlement types, NettingEngine, bond system |
| `modules/database.py` | Modify | 6 new tables, ~25 new methods, row caps |
| `modules/protocol.py` | Modify | 7 new message types (32891-32903), rate limit constants |
| `modules/rpc_commands.py` | Modify | ~10 new handler functions |
| `cl-hive.py` | Modify | Import, init, dispatch, settlement_loop updates, escrow_maintenance_loop |
| `tools/mcp-hive-server.py` | Modify | Add 5 escrow RPC methods to allowlist |
| **NEW** `tests/test_cashu_escrow.py` | New | Ticket creation, validation, redemption, refund, circuit breaker |
| **NEW** `tests/test_extended_settlements.py` | New | 9 types, netting, bonds, disputes, panel selection |

### Phase 5: Nostr + Marketplace + Liquidity

| File | Type | Changes |
|------|------|---------|
| **NEW** `modules/nostr_transport.py` | New | Async WebSocket relay client, NIP-44, event publishing, thread-safe queues |
| **NEW** `modules/marketplace.py` | New | Advisor marketplace, contracts, trials, conflict resolution |
| **NEW** `modules/liquidity_marketplace.py` | New | 9 liquidity services, heartbeats, pricing models |
| `modules/database.py` | Modify | 7 new tables, ~30 new methods, row caps |
| `modules/rpc_commands.py` | Modify | ~14 new handler functions |
| `cl-hive.py` | Modify | Import, init, Nostr thread start/stop, marketplace_maintenance_loop, liquidity_maintenance_loop |
| `tools/mcp-hive-server.py` | Modify | Add 14 marketplace + liquidity RPC methods to allowlist |
| **NEW** `tests/test_nostr_transport.py` | New | Relay connection, DM encryption, event publishing, thread safety |
| **NEW** `tests/test_marketplace.py` | New | Discovery, contracts, trials, multi-advisor, sequence numbering |
| **NEW** `tests/test_liquidity_marketplace.py` | New | 9 services, heartbeats, lease lifecycle, rate limiting |

### Phase 6: 3-Plugin Split

| File | Type | Changes |
|------|------|---------|
| **NEW** `cl-hive-comms.py` | New | Client plugin: transport, schema, policy, payments |
| **NEW** `cl-hive-archon.py` | New | Identity plugin: DID, credentials, vault, governance tier, polls |
| **NEW** `modules/policy_engine.py` | New | Operator policy rules, presets, quiet hours, protected channels |
| `cl-hive.py` | Refactor | Extract shared code, detect sibling plugins |
| `tools/mcp-hive-server.py` | Modify | Add 10 client + 9 archon RPC methods to allowlist |
| **NEW** `tests/test_hive_comms.py` | New | Transport, schema translation, policy engine |
| **NEW** `tests/test_hive_archon.py` | New | DID provisioning, binding, vault, governance tier, polls |

---

## External Dependencies by Phase

| Phase | Library | Purpose | Install |
|-------|---------|---------|---------|
| 4 | `cashu` (Python) | NUT-10/11/14 token operations | `pip install cashu` |
| 5 | `websockets` | Nostr relay WebSocket client | `pip install websockets` |
| 5 | `coincurve` | NIP-44 encryption, Nostr event signing (schnorr/BIP-340) | `pip install coincurve` |
| 5 | `cffi` (transitive) | C FFI for secp256k1 | Installed with coincurve |
| 6 | None new | Architectural refactor only | — |

**Archon integration** (Phase 6B): Via HTTP API calls to public gateway (`archon.technology`) or local node. No Python library needed — standard `urllib.request` calls. Circuit breaker pattern same as Cashu mint calls.

---

## Verification

### Phase 4
1. Unit tests: `python3 -m pytest tests/test_cashu_escrow.py tests/test_extended_settlements.py -v`
2. Escrow round-trip: create ticket → execute task → reveal preimage → redeem
3. Netting: verify bilateral net reduces N obligations to 1 payment (integer arithmetic, no rounding)
4. Bond posting: verify tier assignment and credit line computation
5. Panel selection: verify deterministic selection given same dispute_id + block_hash
6. BOND_SLASH: verify full security chain (quorum check, vote signature verification)
7. Circuit breaker: verify mint failures trigger OPEN state and recovery via HALF_OPEN
8. Regression: all existing tests pass

### Phase 5
1. Unit tests: `python3 -m pytest tests/test_nostr_transport.py tests/test_marketplace.py tests/test_liquidity_marketplace.py -v`
2. Nostr integration: publish profile to relay → discover → NIP-44 DM negotiation
3. Threading: verify Nostr thread starts/stops cleanly, queue operations are thread-safe
4. Lease lifecycle: offer → accept → heartbeat attestations → completion
5. Trial anti-gaming: verify cooldown enforcement, concurrent limits, graduated pricing with sequence numbers
6. Heartbeat rate limiting: verify early heartbeats are dropped
7. Regression: all existing tests pass

### Phase 6
1. Unit tests: `python3 -m pytest tests/test_hive_comms.py tests/test_hive_archon.py -v`
2. Standalone test: `cl-hive-comms` operates without `cl-hive` installed
3. Upgrade test: install comms → add archon → add cl-hive → verify state preserved
4. Schema translation: all 15 categories correctly map to CLN RPC
5. Policy engine: conservative preset blocks danger > 4, aggressive allows danger ≤ 7, quiet hours block non-monitor actions
6. Protected channels: verify `hive:channel/v1` close actions are blocked for protected channel IDs
7. Governance polls: `hive-poll-create` → `hive-vote` → `hive-poll-status` shows correct tally (governance tier only)
8. Database: verify each plugin creates only its own tables, cross-plugin reads work
9. Regression: all existing tests pass
