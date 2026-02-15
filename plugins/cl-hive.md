# cl-hive: Hive Coordination Plugin

**Status:** Design Document  
**Version:** 0.1.0  
**Author:** Hex (`did:cid:bagaaierajrr7k6izcrdfwqxpgtrobflsv5oibymfnthjazkkokaugszyh4ka`)  
**Date:** 2026-02-15  
**Source Specs:** [DID-HIVE-CLIENT](../planning/DID-HIVE-CLIENT.md), [DID-HIVE-SETTLEMENTS](../planning/DID-HIVE-SETTLEMENTS.md), [DID-HIVE-MARKETPLACE](../planning/DID-HIVE-MARKETPLACE.md), [DID-HIVE-LIQUIDITY](../planning/DID-HIVE-LIQUIDITY.md)

---

## Overview

`cl-hive` is the **full hive coordination plugin** that transforms a Lightning node from an independent client into a cooperative fleet member. It adds gossip-based intelligence, topology planning, fee coordination, settlement netting, and fleet-wide rebalancing — capabilities that emerge only when multiple nodes cooperate as a swarm.

**Requires:** `cl-hive-comms`  
**Recommended:** `cl-hive-archon` (for full DID identity)

This plugin is for operators who want the benefits of fleet coordination: 97% cheaper rebalancing via intra-hive paths, pheromone-based routing intelligence, settlement netting that reduces payment overhead, and cooperative topology planning. It requires posting a bond (50k–500k sats) as economic commitment.

---

## Relationship to Other Plugins

```
┌──────────────────────────────────────────────────────┐
│              ➤ cl-hive (coordination) ◄                │
│  Gossip, topology, settlements, fleet advisor         │
│  Requires: cl-hive-comms                              │
│  Recommended: cl-hive-archon                          │
├──────────────────────────────────────────────────────┤
│                    cl-hive-archon (identity)           │
│  DID generation, credentials, dmail, vault            │
│  Requires: cl-hive-comms                              │
├──────────────────────────────────────────────────────┤
│                    cl-hive-comms (transport)           │
│  Nostr DM + REST/rune transport, marketplace,         │
│  payment, policy engine                               │
│  Standalone                                           │
└──────────────────────────────────────────────────────┘
```

| Plugin | Relationship |
|--------|-------------|
| **cl-hive-comms** | **Required.** cl-hive registers gossip message handlers and settlement schemas with cl-hive-comms' transport abstraction. Uses cl-hive-comms' Payment Manager for settlement payments. |
| **cl-hive-archon** | **Recommended.** DID identity for hive PKI handshakes, credential-based governance, vault backup. Without it, hive membership uses Nostr identity only (reduced trust). |

### What cl-hive Adds Beyond Client-Only

| Feature | cl-hive-comms only | + cl-hive |
|---------|-------------------|-----------|
| Advisor management | ✓ (direct escrow) | ✓ (+ settlement netting) |
| Liquidity marketplace | ✓ (direct contracts) | ✓ (+ fleet-coordinated liquidity) |
| Fee optimization | Via advisor | Via advisor + fleet intelligence |
| Rebalancing | Via advisor (public routes) | Via advisor + 97% cheaper intra-hive paths |
| Discovery | Nostr + Archon + directories | + Hive gossip (fastest, highest trust) |
| Settlement | Direct Cashu escrow per-action | Netting (bilateral + multilateral), credit tiers |
| Intelligence market | Buy from advisor only | Full market (buy/sell routing intelligence) |
| Gossip participation | ✗ | ✓ (pheromone markers, stigmergic routing) |
| Topology planning | ✗ | ✓ (MCF optimization, cooperative splicing) |
| Governance | ✗ | ✓ (vote on hive parameters) |
| Bond requirement | None | 50k–500k sats (recoverable) |

---

## PKI Handshakes & Hive Membership

### Joining a Hive

```bash
# 1. Ensure cl-hive-comms is running (and optionally cl-hive-archon)
lightning-cli plugin start /path/to/cl_hive.py

# 2. Join a hive and post bond
lightning-cli hive-join --bond=50000

# 3. Existing advisor relationships continue unchanged
lightning-cli hive-client-status  # same advisors, same credentials
```

### PKI Handshake

The existing hive PKI handshake is extended for the settlement protocol:

1. Node key exchange
2. DID credential presentation (if cl-hive-archon installed) or Nostr key presentation
3. Hive membership verification
4. **Bond status attestation** (current bond amount, last slash, tenure)
5. **Settlement preference negotiation:**
   - Preferred settlement window
   - Acceptable Cashu mints
   - Credit tier assertion + supporting reputation credentials
6. **Initial credit line establishment**

### Bond Requirements

Bond size scales with privileges:

| Privilege Level | Minimum Bond | Access |
|----------------|-------------|--------|
| **Observer** | 0 sats | Read-only gossip, no settlement |
| **Basic routing** | 50,000 sats | Revenue sharing (no intelligence) |
| **Full member** | 150,000 sats | All settlements, pheromone market, intelligence |
| **Liquidity provider** | 300,000 sats | Channel leasing, splice participation, premium pheromone |
| **Founding member** | 500,000 sats | Governance voting, arbitration eligibility, highest credit |

**Bond structure:** A Cashu token with NUT-11 multisig spending conditions. Locked to a hive multisig key (e.g., 3-of-5 founding members). Slashing requires quorum agreement with evidence. Bond is recoverable (minus any slashing) on hive exit after a 7-day hold period.

**Dynamic bond floor:** Effective minimum scales with hive size to prevent sybil attacks:

```
effective_minimum(tier) = max(base_minimum(tier), median_bond(existing_members) × 0.5)
```

**Time-weighted staking:** Bond effectiveness increases with tenure:

```
effective_bond(node) = bond_amount × min(1.0, tenure_days / 180)
```

---

## Gossip Protocol

### Stigmergic Markers (Pheromone Routing Intelligence)

The hive uses a bio-inspired stigmergic signaling system. Nodes deposit "pheromone markers" on routes based on observed routing success/failure, creating emergent routing intelligence.

**Marker types:**

| Marker | Purpose | Decay Rate |
|--------|---------|-----------|
| `route_preference` | Signals successful routing corridors | Moderate |
| `settlement_pending` | Flags paths with unsettled obligations | Fast |
| `credit_available` | Advertises available credit on a path | Moderate |
| `bond_healthy` | Signals healthy bonds along path | Slow |
| `dispute_active` | Warns of settlement disputes | Persists until resolved |

Pheromone markers carry settlement metadata:

```json
{
  "type": "pheromone_marker",
  "marker_type": "route_preference",
  "path": ["03abc...", "03def...", "03ghi..."],
  "strength": 0.85,
  "decay_rate": 0.02,
  "settlement_metadata": {
    "revenue_share_model": "proportional",
    "settlement_window": "daily",
    "credit_tiers": { "03abc...": "trusted", "03def...": "recognized" }
  }
}
```

### Gossip Message Types

| Message Type | Content | Propagation |
|-------------|---------|-------------|
| `service_profile_announce` | `HiveServiceProfile` credential | Broadcast (full hive) |
| `service_discovery_query` | Filter criteria for advisor/liquidity search | Broadcast |
| `service_discovery_response` | Matching profile references | Direct reply |
| `settlement_summary` | Net obligation summary | Bilateral only |
| `netting_proposal` | Multilateral netting proposal | All participants |
| `netting_ack` | Agreement to netting result | All participants |
| `bond_posting` | New bond or renewal announcement | Broadcast |
| `violation_report` | Policy violation with evidence | Broadcast |
| `arbitration_vote` | Panel member's dispute vote | Panel + parties |
| `pheromone_marker` | Stigmergic routing signal | Broadcast |

---

## Topology Planning (The Gardner)

### MCF Optimization

The Gardner uses Min-Cost Flow (MCF) optimization to plan optimal channel topology across the hive:

- **Channel open suggestions** — Identifies valuable peers and recommends channel sizes
- **Channel close recommendations** — Flags underperforming channels for rationalization
- **Cooperative splicing** — Coordinates multi-party splice transactions for channel resizing
- **Load balancing** — Distributes routing across the fleet to equalize utilization

### Cooperative Splicing

Multiple hive members participate in splice transactions — adding or removing funds from channels:

```json
{
  "type": "SpliceReceipt",
  "channel_id": "931770x2363x0",
  "splice_txid": "abc123...",
  "participants": [
    { "did": "did:cid:<node_a>", "contribution_sats": 2000000, "share_pct": 40 },
    { "did": "did:cid:<node_b>", "contribution_sats": 3000000, "share_pct": 60 }
  ],
  "new_capacity_sats": 5000000
}
```

Revenue share from spliced channels is proportional to contribution, settled via the standard settlement protocol.

---

## Settlement Protocol

### Settlement Types

Nine settlement types, all using the same netting and escrow infrastructure:

| Type | Description | Proof Mechanism |
|------|-------------|-----------------|
| **1. Routing Revenue Sharing** | Revenue split based on forwarding contribution | Signed `HTLCForwardReceipt` chain |
| **2. Rebalancing Cost** | Compensation for liquidity used in rebalances | Signed `RebalanceReceipt` |
| **3. Channel Leasing** | Lease payments for inbound capacity | Periodic `LeaseHeartbeat` attestations |
| **4. Cooperative Splicing** | Revenue share from multi-party channels | `SpliceReceipt` + on-chain tx |
| **5. Shared Channel Opens** | Revenue from co-funded channels | `SharedChannelReceipt` + funding tx |
| **6. Pheromone Market** | Payment for route advertising | `PheromoneReceipt` + forward receipts |
| **7. Intelligence Sharing** | Payment for routing intelligence data | `IntelligenceReceipt` + correlation |
| **8. Penalty** | Slashing for policy violations | `ViolationReport` + quorum sigs |
| **9. Advisor Fees** | Performance bonuses, subscriptions, multi-operator billing | `AdvisorFeeReceipt` + management receipts |

### Netting

Before creating Cashu tickets, obligations are netted to minimize token volume.

**Bilateral netting:**

```
net_obligation(A→B) = Σ(A owes B) - Σ(B owes A)
If > 0: A pays B. If < 0: B pays A. If = 0: No settlement.
```

**Multilateral netting** (for hives with many members):

```
Given N nodes with bilateral net obligations:
  Compute net position for each node
  Net receivers get paid; net payers pay
  Minimum payments = max(|receivers|, |payers|) - 1
```

Example: 5 bilateral obligations net to 3 payments.

### Settlement Windows

| Mode | Window | Best For |
|------|--------|---------|
| **Real-time micro** | Per-event | Low-trust relationships |
| **Hourly batch** | 1 hour | Active routing |
| **Daily batch** | 24 hours | Standard members |
| **Weekly batch** | 7 days | Highly trusted, high-volume |

Settlement mode is negotiated during PKI handshake and adjusted based on credit tier.

### Credit & Trust Tiers

| Tier | Requirements | Credit Line | Settlement Window |
|------|-------------|------------|-------------------|
| **Newcomer** | Bond posted, no history | 0 sats | Per-event |
| **Recognized** | 30+ days, 0 disputes, rep > 60 | 10,000 sats | Hourly |
| **Trusted** | 90+ days, ≤1 dispute, rep > 75 | 50,000 sats | Daily |
| **Senior** | 180+ days, 0 recent disputes, rep > 85 | 200,000 sats | Weekly |
| **Founding** | Genesis or governance-approved | 1,000,000 sats | Weekly |

Credit lines mean obligations accumulate before escrow is required:

```
If accumulated_obligations < credit_line:
  No escrow — settle at window end
Else:
  Excess escrowed immediately via Cashu
```

### Dispute Resolution

1. **Evidence comparison** — Both nodes exchange signed receipt chains
2. **Peer arbitration** — 7-member panel (stake-weighted random selection), 5-of-7 majority
3. **Reputation consequences** — Losing party gets `neutral` or `revoke` reputation signal
4. **Bond forfeiture** — For egregious violations (fabricated receipts), supermajority can slash bond

### Penalty Enforcement

| Violation | Base Penalty | Detection |
|-----------|-------------|-----------|
| Fee undercutting | 1,000 sats × severity | Gossip observation |
| Unannounced close | 10,000 sats × severity | Channel monitoring |
| Data leakage | 50,000 sats × severity | Reporting + quorum |
| Free-riding | 5,000 sats × severity | Contribution tracking |
| Heartbeat failure | 500 + proportional | Heartbeat monitoring |

Penalties require quorum confirmation (N/2+1) before slashing.

---

## Fleet Rebalancing

### Intra-Hive Paths

Hive members route rebalances through each other's channels at minimal cost — typically 97% cheaper than public routing because:

- Zero or near-zero routing fees between members
- Pheromone markers identify optimal paths
- Coordinated liquidity means paths are available when needed
- Settlement netting means the routing fees net against other obligations

### Intent Locks

Before executing a rebalance across multiple hive nodes, the system creates an **intent lock** — a reservation of liquidity along the planned path:

```json
{
  "type": "IntentLock",
  "initiator": "did:cid:<node_a>",
  "path": ["03abc...", "03def...", "03ghi..."],
  "amount_sats": 500000,
  "direction": "a_to_c",
  "expires": "2026-02-14T13:00:00Z",
  "lock_id": "<unique_id>"
}
```

Intent locks prevent competing rebalances from consuming the same liquidity simultaneously. They expire automatically if not executed within the window.

---

## Upgrade Path: cl-hive-comms → Full Hive Member

### What Changes

| Aspect | cl-hive-comms only | + cl-hive |
|--------|-------------------|-----------|
| Software | Single plugin | Three plugins (comms + archon recommended + hive) |
| Identity | Nostr keypair | Nostr + DID + hive PKI |
| Bond | None | 50k–500k sats |
| Gossip | No participation | Full network access |
| Settlement | Direct escrow only | Netting, credit tiers |
| Fleet rebalancing | N/A | Intra-hive paths (97% savings) |
| Pheromone routing | N/A | Full stigmergic signal access |
| Intelligence market | Buy from advisor | Full buy/sell access |
| Management fees | Per-action / subscription | Discounted (fleet paths reduce costs) |

### What Stays the Same

- Same management interface (schemas, receipts)
- Same credential system
- Same escrow mechanism (Cashu tickets, same mints)
- Same advisor relationships (existing credentials remain valid)
- Same reputation history (portable across membership levels)

### Migration Process

```bash
# Starting from cl-hive-comms only:

# 1. Add DID identity (recommended before hive membership)
lightning-cli plugin start /path/to/cl_hive_archon.py
# → DID auto-provisioned, bound to existing Nostr key

# 2. Add full hive coordination
lightning-cli plugin start /path/to/cl_hive.py

# 3. Join a hive and post bond
lightning-cli hive-join --bond=50000

# 4. Existing advisor relationships continue unchanged
lightning-cli hive-client-status  # same advisors, same credentials
```

Each plugin layer adds capabilities without disrupting existing connections. The Nostr keypair from cl-hive-comms persists through the upgrade. DID binding is created automatically when cl-hive-archon is added.

### Incentives to Upgrade

| Benefit | Impact |
|---------|--------|
| Fleet rebalancing | 97% cheaper than public routing |
| Intelligence market | Buy/sell routing intelligence |
| Discounted management | Advisors pass on fleet path savings |
| Settlement netting | Reduces escrow overhead |
| Credit tiers | Long-tenure members get credit lines |
| Governance | Vote on hive parameters |

### Bond Recovery

Bond is recoverable (minus any slashing) on hive exit:

1. Broadcast intent-to-leave
2. 4-hour emergency settlement window
3. 7-day bond hold period for late claims
4. Bond released via refund path

---

## Emergency Exit Protocol

### Voluntary Exit

1. **Broadcast intent-to-leave** — Signed `EmergencyExit` message
2. **4-hour settlement window** — All pending obligations netted and settled
3. **7-day bond hold** — Window for late-arriving claims
4. **Bond release** — Full bond returned minus any slashing
5. **Reputation recorded** — Exit event logged (not penalized)

### Involuntary Exit (Node Disappears)

1. Detected via 3+ consecutive missed heartbeats
2. 48-hour grace period to return
3. After 48h: obligations settled from bond
4. Remaining bond held for 7-day claim window

---

## Configuration Reference

```ini
# ~/.lightning/config

# === Hive Membership ===
# hive-bond-amount=50000                # sats to post as bond
# hive-settlement-window=daily          # per-event | hourly | daily | weekly
# hive-settlement-mints=https://mint.minibits.cash

# === Gossip ===
# hive-gossip-interval=60               # seconds between gossip rounds
# hive-pheromone-decay=0.02             # pheromone decay rate

# === Topology ===
# hive-mcf-interval=3600                # seconds between MCF runs
# hive-auto-suggest-channels=true       # suggest channel opens/closes

# === Intelligence ===
# hive-intelligence-share=true          # contribute routing data to market
# hive-intelligence-buy=true            # purchase routing intelligence

# === Rebalancing ===
# hive-fleet-rebalance=true             # use intra-hive paths
# hive-intent-lock-timeout=300          # seconds before intent locks expire
```

---

## Installation

```bash
# Requires cl-hive-comms (and recommended: cl-hive-archon)
lightning-cli plugin start /path/to/cl_hive.py

# Join the hive
lightning-cli hive-join --bond=50000
```

For permanent installation:

```ini
plugin=/path/to/cl_hive_comms.py
plugin=/path/to/cl_hive_archon.py
plugin=/path/to/cl_hive.py
```

### Requirements

- **cl-hive-comms** running
- **cl-hive-archon** recommended (for DID-based PKI)
- Bond funds available in node wallet
- Network connectivity to other hive members

---

## Implementation Roadmap

| Phase | Scope | Timeline |
|-------|-------|----------|
| 1 | PKI handshake, bond posting, basic gossip, membership management | 4–6 weeks |
| 2 | Settlement receipt infrastructure (all 9 types), bilateral netting | 4–6 weeks |
| 3 | Pheromone markers, stigmergic routing integration | 3–4 weeks |
| 4 | MCF topology planning, channel suggestions, cooperative splicing | 4–6 weeks |
| 5 | Credit tiers, multilateral netting, settlement windows | 3–4 weeks |
| 6 | Intelligence market, pheromone market | 4–6 weeks |
| 7 | Dispute resolution, penalty enforcement, bond slashing | 3–4 weeks |
| 8 | Fleet rebalancing, intent locks, emergency exit | 3–4 weeks |

---

## References

- [DID Hive Client](../planning/DID-HIVE-CLIENT.md) — Plugin architecture, upgrade path (Section 11)
- [DID + Cashu Hive Settlements](../planning/DID-HIVE-SETTLEMENTS.md) — Full settlement protocol, bond system, credit tiers, netting, disputes
- [DID Hive Marketplace](../planning/DID-HIVE-MARKETPLACE.md) — Gossip-based discovery, multi-advisor coordination
- [DID Hive Liquidity](../planning/DID-HIVE-LIQUIDITY.md) — Fleet-coordinated liquidity, pools, JIT
- [DID + L402 Fleet Management](../planning/DID-L402-FLEET-MANAGEMENT.md) — Schema definitions, danger scoring
- [DID + Cashu Task Escrow](../planning/DID-CASHU-TASK-ESCROW.md) — Escrow ticket format

---

*Feedback welcome. File issues on [cl-hive](https://github.com/lightning-goats/cl-hive) or discuss in #singularity.*

*— Hex ⬡*
