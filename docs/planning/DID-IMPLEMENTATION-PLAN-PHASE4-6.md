# DID Ecosystem — Phases 4-6 Implementation Plan

## Context

This document covers the advanced phases of the DID ecosystem that require external Python libraries beyond `pyln-client`. It builds on Phases 1-3 (see `DID-IMPLEMENTATION-PLAN.md`) which deliver the credential foundation, management schemas, danger scoring, and credential exchange protocol using only CLN HSM crypto.

**Prerequisites**: Phases 1-3 must be deployed and validated before starting Phase 4.

**New external dependencies introduced**:
- Phase 4: Cashu Python SDK (NUT-10/11/14)
- Phase 5: Nostr Python library (NIP-44 encryption, WebSocket relay client)
- Phase 6: No new deps (architectural refactor into 3 plugins)

---

## Phase 4: Cashu Task Escrow + Extended Settlements

**Goal**: Trustless conditional payments via Cashu ecash tokens, 9 settlement types extending the existing `settlement.py`, bond system, credit tiers, and dispute resolution.

### Phase 4A: Cashu Escrow Foundation (3-4 weeks)

#### New file: `modules/cashu_escrow.py`

```python
class CashuEscrowManager:
    """Cashu NUT-10/11/14 escrow ticket management."""

    MAX_ACTIVE_TICKETS = 500
    MAX_TICKET_ROWS = 50_000
    SECRET_RETENTION_DAYS = 90

    def __init__(self, database, plugin, rpc=None, our_pubkey="",
                 acceptable_mints=None):
```

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
- `get_pricing(danger_score, reputation_tier)` → dynamic pricing based on DID-L402 spec

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
    secret_hex TEXT NOT NULL,           -- HTLC preimage (encrypted at rest)
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

---

### Phase 4B: Extended Settlements (4-6 weeks)

#### Modifications to `modules/settlement.py`

Extend the existing settlement module with 8 additional settlement types beyond the current routing revenue sharing.

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

**New receipt types** (added to `protocol.py`):

| Message | ID | Purpose |
|---------|------|---------|
| `SETTLEMENT_RECEIPT` | 32891 | Generic signed receipt for any settlement type |
| `BOND_POSTING` | 32893 | Announce bond deposit |
| `BOND_SLASH` | 32895 | Announce bond forfeiture |
| `NETTING_PROPOSAL` | 32897 | Bilateral/multilateral netting proposal |
| `NETTING_ACK` | 32899 | Acknowledge netting computation |
| `VIOLATION_REPORT` | 32901 | Report policy violation |
| `ARBITRATION_VOTE` | 32903 | Cast arbitration vote |

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
        """Net obligations between two peers. Returns single net payment."""

    def multilateral_net(self, obligations, window_id):
        """Multilateral netting across all peers. Minimizes total payments."""
        # Uses cycle detection in obligation graph
        # Reduces N² obligations to ≤N payments
```

#### Dispute resolution

Arbitration panel selection:
```python
def select_panel(dispute_id, block_hash, eligible_members):
    """Deterministic panel selection using stake-weighted randomness."""
    seed = sha256(dispute_id + block_hash)
    weights = {m: m.bond * sqrt(m.tenure_days) for m in eligible_members}
    return weighted_sample(seed, weights, k=min(7, len(eligible_members)))
```

Panel sizes: 7 members (5-of-7 majority) for >=15 eligible, 5 members (3-of-5) for 10-14, 3 members (2-of-3) for 5-9, bilateral negotiation for <5.

---

## Phase 5: Nostr Transport + Marketplace + Liquidity

**Goal**: Public marketplace layer using Nostr for discovery, NIP-44 encrypted DMs for management command transport, and a 9-service liquidity marketplace.

### Phase 5A: Nostr Transport Layer (3-4 weeks)

#### New file: `modules/nostr_transport.py`

```python
class NostrTransport:
    """Nostr WebSocket relay client with NIP-44 encryption."""

    DEFAULT_RELAYS = [
        "wss://nos.lol",
        "wss://relay.damus.io",
    ]
    SEARCH_RELAYS = ["wss://relay.nostr.band"]
    PROFILE_RELAYS = ["wss://purplepag.es"]

    MAX_RELAY_CONNECTIONS = 8
    RECONNECT_BACKOFF_MAX = 300  # 5 min max backoff

    def __init__(self, plugin, privkey_hex=None):
```

**Key methods**:
- `connect(relay_urls)` → establish WebSocket connections to relays
- `publish(event)` → sign and publish to >=3 relays
- `subscribe(filters, callback)` → subscribe to event kinds with filters
- `send_dm(recipient_pubkey, plaintext)` → NIP-44 encrypt and publish
- `receive_dm(callback)` → decrypt incoming NIP-44 DMs
- `close()` → graceful disconnect

**Nostr keypair management**:
- Auto-generate secp256k1 keypair on first run, persist in DB
- If `cl-hive-archon` installed later, bind DID to Nostr pubkey
- Until then, Nostr pubkey serves as identity

#### New DB table

```sql
CREATE TABLE IF NOT EXISTS nostr_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Stores: privkey (encrypted), pubkey, relay_list, last_event_ids
```

### Phase 5B: Advisor Marketplace (4-5 weeks)

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

**Nostr event kinds — Advisor services (38380-38389)**:

| Kind | Type | Content |
|------|------|---------|
| 38380 | Advisor Service Profile | Self-issued VC with capabilities, pricing, availability |
| 38381 | Advisor Service Offer | Specific engagement offer with terms |
| 38382 | Advisor RFP | Node requesting advisor services |
| 38383 | Contract Confirmation | Immutable dual-signed contract record |
| 38384 | Heartbeat Attestation | Ongoing engagement status |
| 38385 | Reputation Summary | Aggregated advisor reputation |

**Service specializations** (from DID-HIVE-MARKETPLACE):
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
    scope TEXT NOT NULL,
    flat_fee_sats INTEGER NOT NULL,
    start_at INTEGER NOT NULL,
    end_at INTEGER NOT NULL,
    evaluation_json TEXT,              -- metrics at trial end
    outcome TEXT,                      -- pass/fail/extended
    FOREIGN KEY (contract_id) REFERENCES marketplace_contracts(contract_id)
);
```

Row caps: `MAX_MARKETPLACE_PROFILE_ROWS = 5_000`, `MAX_MARKETPLACE_CONTRACT_ROWS = 10_000`.

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

### Phase 5C: Liquidity Marketplace (5-6 weeks)

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
| `hive-liquidity-status` | View active leases |
| `hive-liquidity-terminate` | Terminate a lease |

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

### Phase 6A: `cl-hive-comms` plugin (4-6 weeks)

#### New file: `cl-hive-comms.py`

The lightweight client entry point. Contains:

| Component | Responsibility |
|-----------|---------------|
| **Schema Handler** | Receive management commands via Nostr DM or REST/rune, dispatch to CLN RPC, return signed receipts |
| **Transport Abstraction** | Pluggable interface: Nostr DM (NIP-44), REST/rune. Future: Bolt 8, Archon Dmail |
| **Payment Manager** | Bolt11 (per-action), Bolt12 (subscription), L402 (API), Cashu (escrow) |
| **Policy Engine** | Operator's last defense: presets (conservative/moderate/aggressive), custom rules, protected channels, quiet hours |
| **Receipt Store** | Append-only hash-chained dual-signed SQLite log |
| **Marketplace Client** | Publish/subscribe to kinds 38380+/38900+ |

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

### Phase 6B: `cl-hive-archon` plugin (3-4 weeks)

#### New file: `cl-hive-archon.py`

Adds DID identity layer on top of `cl-hive-comms`:

| Component | Responsibility |
|-----------|---------------|
| **DID Provisioning** | Auto-generate `did:cid:*` via public Archon gateway or local node |
| **DID-Nostr Binding** | Attestation credential linking DID to Nostr pubkey |
| **Credential Manager** | Issue, verify, present, revoke DID credentials |
| **Dmail Transport** | Register Archon Dmail as transport option in comms |
| **Vault Backup** | Archon group vault for DID wallet, credentials, receipt chain, Cashu tokens |
| **Shamir Recovery** | k-of-n threshold recovery for distributed trust |

**Sovereignty tiers**:

| Tier | Setup | DID Resolution | Trust Level |
|------|-------|---------------|-------------|
| No Archon (default) | Zero — auto-provision via public gateway | Remote | Minimal |
| Own Archon node | Docker compose | Local (self-sovereign) | Full |
| L402-gated Archon | Public gatekeeper | Remote (paid) | Moderate |

### Phase 6C: Refactor existing `cl-hive.py` (3-4 weeks)

Extract modules that belong in `cl-hive-comms` or `cl-hive-archon`:
- Move Nostr transport → `cl-hive-comms`
- Move DID credential management → `cl-hive-archon`
- Move management schema handling → `cl-hive-comms`
- Keep gossip, topology, settlements, governance in `cl-hive`
- `cl-hive` detects presence of `cl-hive-comms` and `cl-hive-archon` via plugin list

**Migration path for existing nodes**:
1. Existing hive members: no changes needed (cl-hive continues to work as monolith)
2. New non-hive nodes: install `cl-hive-comms` only
3. Upgrade path: `cl-hive-comms` → add `cl-hive-archon` → add `cl-hive` → `hive-join --bond=50000`

---

## Files Summary (All Phases)

### Phase 4: Cashu Escrow + Extended Settlements

| File | Type | Changes |
|------|------|---------|
| **NEW** `modules/cashu_escrow.py` | New | CashuEscrowManager, ticket types, pricing |
| `modules/settlement.py` | Modify | 8 new settlement types, netting engine, bond system |
| `modules/database.py` | Modify | 6 new tables, ~25 new methods |
| `modules/protocol.py` | Modify | 7 new message types (32891-32903) |
| `modules/rpc_commands.py` | Modify | ~10 new handler functions |
| `cl-hive.py` | Modify | Import, init, dispatch, settlement_loop updates |
| **NEW** `tests/test_cashu_escrow.py` | New | Ticket creation, validation, redemption, refund |
| **NEW** `tests/test_extended_settlements.py` | New | 9 types, netting, bonds, disputes |

### Phase 5: Nostr + Marketplace + Liquidity

| File | Type | Changes |
|------|------|---------|
| **NEW** `modules/nostr_transport.py` | New | WebSocket relay client, NIP-44, event publishing |
| **NEW** `modules/marketplace.py` | New | Advisor marketplace, contracts, trials, conflict resolution |
| **NEW** `modules/liquidity_marketplace.py` | New | 9 liquidity services, heartbeats, pricing models |
| `modules/database.py` | Modify | 7 new tables, ~30 new methods |
| `modules/protocol.py` | Modify | Marketplace gossip message types |
| `modules/rpc_commands.py` | Modify | ~15 new handler functions |
| `cl-hive.py` | Modify | Import, init, Nostr connection, marketplace loops |
| **NEW** `tests/test_nostr_transport.py` | New | Relay connection, DM encryption, event publishing |
| **NEW** `tests/test_marketplace.py` | New | Discovery, contracts, trials, multi-advisor |
| **NEW** `tests/test_liquidity_marketplace.py` | New | 9 services, heartbeats, lease lifecycle |

### Phase 6: 3-Plugin Split

| File | Type | Changes |
|------|------|---------|
| **NEW** `cl-hive-comms.py` | New | Client plugin: transport, schema, policy, payments |
| **NEW** `cl-hive-archon.py` | New | Identity plugin: DID, credentials, vault |
| `cl-hive.py` | Refactor | Extract shared code, detect sibling plugins |
| **NEW** `tests/test_hive_comms.py` | New | Transport, schema translation, policy engine |
| **NEW** `tests/test_hive_archon.py` | New | DID provisioning, binding, vault |

---

## External Dependencies by Phase

| Phase | Library | Purpose | Install |
|-------|---------|---------|---------|
| 4 | `cashu` (Python) | NUT-10/11/14 token operations | `pip install cashu` |
| 5 | `websockets` | Nostr relay WebSocket client | `pip install websockets` |
| 5 | `secp256k1` or `coincurve` | NIP-44 encryption, Nostr event signing | `pip install coincurve` |
| 5 | `cffi` (transitive) | C FFI for secp256k1 | Installed with coincurve |
| 6 | None new | Architectural refactor only | — |

**Archon integration** (all phases): Via HTTP API calls to public gateway (`archon.technology`) or local node. No Python library needed — standard `urllib` or subprocess calls to `npx @didcid/keymaster`.

---

## Verification

### Phase 4
1. Unit tests: `python3 -m pytest tests/test_cashu_escrow.py tests/test_extended_settlements.py -v`
2. Escrow round-trip: create ticket → execute task → reveal preimage → redeem
3. Netting: verify bilateral net reduces N obligations to 1 payment
4. Bond posting: verify tier assignment and credit line computation
5. Regression: all existing tests pass

### Phase 5
1. Unit tests: `python3 -m pytest tests/test_nostr_transport.py tests/test_marketplace.py tests/test_liquidity_marketplace.py -v`
2. Nostr integration: publish profile to relay → discover → NIP-44 DM negotiation
3. Lease lifecycle: offer → accept → heartbeat attestations → completion
4. Trial anti-gaming: verify cooldown enforcement, concurrent limits, graduated pricing
5. Regression: all existing tests pass

### Phase 6
1. Unit tests: `python3 -m pytest tests/test_hive_comms.py tests/test_hive_archon.py -v`
2. Standalone test: `cl-hive-comms` operates without `cl-hive` installed
3. Upgrade test: install comms → add archon → add cl-hive → verify state preserved
4. Schema translation: all 15 categories correctly map to CLN RPC
5. Policy engine: conservative preset blocks danger > 4, aggressive allows danger ≤ 7
6. Regression: all existing tests pass

---

## Timeline Estimate

| Phase | Duration | Dependencies |
|-------|----------|-------------|
| 4A: Cashu Escrow | 3-4 weeks | Phases 1-3 complete, `cashu` pip package |
| 4B: Extended Settlements | 4-6 weeks | Phase 4A complete |
| 5A: Nostr Transport | 3-4 weeks | `websockets` + `coincurve` pip packages |
| 5B: Advisor Marketplace | 4-5 weeks | Phase 5A + Phase 4A complete |
| 5C: Liquidity Marketplace | 5-6 weeks | Phase 5B + Phase 4B complete |
| 6A: cl-hive-comms | 4-6 weeks | Phase 5A complete |
| 6B: cl-hive-archon | 3-4 weeks | Phase 6A complete |
| 6C: Refactor cl-hive | 3-4 weeks | Phase 6A + 6B complete |

Phases 4 and 5A can run in parallel. Total estimated: 6-9 months for all phases.
