# DID Hive Liquidity: Liquidity-as-a-Service Marketplace

**Status:** Proposal / Design Draft  
**Version:** 0.1.0  
**Author:** Hex (`did:cid:bagaaierajrr7k6izcrdfwqxpgtrobflsv5oibymfnthjazkkokaugszyh4ka`)  
**Date:** 2026-02-14  
**Feedback:** Open — file issues or comment in #singularity

---

## Abstract

This document defines a trustless marketplace for Lightning liquidity services — how liquidity providers advertise capacity, how consumers discover and contract for it, how delivery is proven, and how payments settle — all using the same DID/escrow/reputation/marketplace infrastructure defined in the companion specs.

Liquidity is the most valuable resource in the Lightning Network. Without inbound capacity, a node cannot receive payments. Without balanced channels, a node loses routing revenue. Without strategic channel placement, a node is topologically irrelevant. Today, obtaining liquidity requires manual negotiation, trust in centralized platforms, or expensive on-chain capital commitment with no performance guarantees.

This spec turns liquidity into a **commodity service** — priced, escrowed, delivered, verified, and settled through cryptographic protocols. It extends [Type 3 (Channel Leasing)](./DID-HIVE-SETTLEMENTS.md#3-channel-leasing--liquidity-rental) from the Settlements spec into a full liquidity marketplace encompassing nine distinct service types, six pricing models, and comprehensive proof/escrow mechanisms.

---

## Motivation

### The Liquidity Problem

The Lightning Network has a fundamental cold-start problem and an ongoing balance problem:

1. **Cold start:** A new node opens channels but has zero inbound capacity. It can send but not receive. To accept payments, someone else must commit capital toward it — capital that earns nothing while sitting idle. Why would anyone do this for a stranger?

2. **Balance drift:** Routing nodes start with balanced channels but traffic is directional. A channel with 5M sats of outbound and 5M sats of inbound drifts to 8M/2M after routing. Now the node can't route large payments in the depleted direction. Revenue drops.

3. **Topological irrelevance:** A node with 10 channels to poorly-connected peers routes nothing. Strategic channel placement — connecting to high-volume corridors — requires capital, intelligence, and coordination that most operators lack.

4. **Capital inefficiency:** Large routing nodes have capital spread across channels, much of it idle. They'd lend it if there were a trustless way to do so. Small nodes need capital but can't find it. The market is fragmented and opaque.

### The Opportunity

The Lightning Network has ~$500M in public channel capacity (2026 estimate). Studies suggest 30-60% of capacity is underutilized at any given time. A trustless marketplace for capital allocation could:

- **For consumers:** Provide on-demand inbound liquidity without manual negotiation, at market-driven prices, with delivery guarantees backed by escrow.
- **For providers:** Turn idle capital into yield. A provider with 10 BTC in well-connected channels can lease excess capacity to dozens of clients, earning sat-hour revenue that compounds.
- **For the network:** Improve capital efficiency network-wide. Liquidity flows to where it's needed, reducing the total capital required to support the same payment volume.

### Why This Protocol Suite

Existing liquidity solutions (Lightning Pool, Magma, LNBig) are centralized — they depend on a single operator for matching, pricing, and trust. This spec builds on the hive protocol suite to provide:

| Property | Centralized (Pool/Magma) | This Protocol |
|----------|------------------------|---------------|
| Identity | Platform accounts | DIDs (self-sovereign, portable) |
| Trust | Platform reputation | Verifiable credentials (cryptographic, cross-platform) |
| Escrow | Platform custodial | Cashu P2PK+HTLC (non-custodial, trustless) |
| Matching | Platform algorithm | Peer-to-peer discovery via gossip/Archon/Nostr |
| Settlement | Platform ledger | Bilateral/multilateral netting with Cashu tokens |
| Pricing | Platform-set or opaque auction | Transparent market with multiple pricing models |
| Implementation | Single implementation | CLN + LND via [DID Hive Client](./DID-HIVE-CLIENT.md) |

---

## Design Principles

### DID Transparency

Liquidity operations use human-readable names and aliases. Operators "lease inbound from BigNode Liquidity" — never "issue `LiquidityLeaseCredential` to `did:cid:bagaaiera...`". Provider profiles show display names, capacity badges, and uptime ratings. DIDs are resolved transparently by the client software. See [DID Hive Client](./DID-HIVE-CLIENT.md) for the abstraction layer.

### Payment Flexibility

Each liquidity service type uses the payment method best suited to its settlement pattern:

| Context | Payment Method | Why |
|---------|---------------|-----|
| Lease deposits (conditional) | **Cashu** (NUT-10/11/14) | Progressive release on heartbeat proof |
| JIT/sidecar flat fees | **Bolt11** or **Cashu** | Simple one-time; Cashu if escrow desired |
| Recurring lease payments | **Bolt12 offers** | Reusable recurring payment codes |
| Submarine swaps | **HTLC-native** | Naturally atomic; no additional escrow needed |
| Insurance premiums | **Bolt11** or **Bolt12** | Regular payments; Cashu for top-up guarantee escrow |
| Revenue-share settlements | **Settlement protocol** | Netting via [Settlements Type 1](./DID-HIVE-SETTLEMENTS.md#1-routing-revenue-sharing) |

### Archon Integration Tiers

Liquidity services work at all three Archon tiers:

| Tier | Experience |
|------|-----------|
| **No Archon node** (default) | DID auto-provisioned; discover providers via public gateway; contract and escrow work identically |
| **Own Archon node** (encouraged) | Full sovereignty; local DID resolution; faster credential verification |
| **Archon behind L402** (future) | Pay-per-use identity services; same liquidity functionality |

### Graceful Degradation

Non-hive nodes access liquidity services via `cl-hive-client` / `hive-lnd` with simplified contracting (see [Section 11](#11-non-hive-access)). Full hive members get settlement netting, credit tiers, and fleet-coordinated liquidity management.

---

## Liquidity Service Types

### Type 1: Channel Leasing

**Definition:** Provider opens a channel to the client's node (or maintains an existing one) with X sats of capacity directed toward the client, for Y days.

**Extends:** [Settlements Type 3](./DID-HIVE-SETTLEMENTS.md#3-channel-leasing--liquidity-rental) with full marketplace integration.

**Flow:**

```
Client                           Provider                       Mint
  │                                 │                             │
  │  1. Request lease               │                             │
  │     (capacity, duration, terms) │                             │
  │  ────────────────────────────►  │                             │
  │                                 │                             │
  │  2. Quote (price, SLA)          │                             │
  │  ◄────────────────────────────  │                             │
  │                                 │                             │
  │  3. Accept + mint escrow        │                             │
  │     (milestone tickets:         │                             │
  │      1 per heartbeat period)    │                             │
  │  ──────────────────────────────────────────────────────────►  │
  │                                 │                             │
  │  4. Send tickets to provider    │                             │
  │  ────────────────────────────►  │                             │
  │                                 │                             │
  │  5. Provider opens channel      │                             │
  │  ◄────────────────────────────  │                             │
  │                                 │                             │
  │  [Each heartbeat period:]       │                             │
  │  6. Provider sends heartbeat    │                             │
  │     attestation (signed         │                             │
  │     capacity proof)             │                             │
  │  ◄────────────────────────────  │                             │
  │                                 │                             │
  │  7. Client verifies, reveals    │                             │
  │     heartbeat preimage          │                             │
  │  ────────────────────────────►  │                             │
  │                                 │                             │
  │  8. Provider redeems ticket     │                             │
  │                                 │  ───────────────────────►   │
  │                                 │                             │
```

**Heartbeat attestation:**

```json
{
  "type": "LeaseHeartbeat",
  "lease_id": "<unique_id>",
  "lessor": "did:cid:<provider_did>",
  "lessee": "did:cid:<client_did>",
  "channel_id": "931770x2363x0",
  "capacity_sats": 5000000,
  "remote_balance_sats": 4800000,
  "direction": "inbound_to_lessee",
  "available": true,
  "measured_at": "2026-02-14T14:00:00Z",
  "lessor_signature": "<sig>"
}
```

**Heartbeat frequency:** Configurable (default: 1 hour). Three consecutive missed heartbeats terminate the lease; remaining escrowed tickets refund to client via timelock.

**Capacity verification:** The client independently verifies the channel exists and has the claimed capacity by checking the gossip network for the channel announcement and/or probing the channel.

**Proration:** If the provider's channel capacity drops below the contracted amount (e.g., due to routing through the leased channel), the heartbeat reports `remote_balance_sats` below threshold. The client can:
1. Accept the reduced capacity (pro-rate the next heartbeat payment)
2. Trigger a top-up demand (provider must rebalance within 2 hours)
3. Terminate the lease with prorated refund

### Type 2: Liquidity Pools

**Definition:** Multiple providers pool capital into a shared fund managed by a pool operator (an advisor agent or automated system). The pool allocates capital to requesting nodes. Revenue is distributed proportionally to capital contribution.

**Structure:**

```
┌─────────────────────────────────────────────────┐
│                LIQUIDITY POOL                    │
│                                                  │
│  Pool Manager: did:cid:<pool_manager_did>        │
│  Total Capital: 50,000,000 sats                  │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │  Providers                               │   │
│  │  Provider A: 20M sats (40% share)        │   │
│  │  Provider B: 15M sats (30% share)        │   │
│  │  Provider C: 10M sats (20% share)        │   │
│  │  Provider D:  5M sats (10% share)        │   │
│  └──────────────────────────────────────────┘   │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │  Active Allocations                      │   │
│  │  Client X: 5M sats (lease, 30 days)      │   │
│  │  Client Y: 3M sats (JIT, 7 days)         │   │
│  │  Client Z: 8M sats (lease, 90 days)      │   │
│  │  Available: 34M sats                      │   │
│  └──────────────────────────────────────────┘   │
│                                                  │
└─────────────────────────────────────────────────┘
```

**Pool shares as verifiable credentials:**

```json
{
  "@context": ["https://www.w3.org/ns/credentials/v2", "https://hive.lightning/liquidity/v1"],
  "type": ["VerifiableCredential", "LiquidityPoolShare"],
  "issuer": "did:cid:<pool_manager_did>",
  "credentialSubject": {
    "id": "did:cid:<provider_did>",
    "poolId": "<pool_id>",
    "contributionSats": 20000000,
    "sharePct": 40.0,
    "joinedAt": "2026-02-14T00:00:00Z",
    "minimumLockDays": 30,
    "revenueDistribution": "proportional",
    "withdrawalNotice": "7d"
  },
  "validFrom": "2026-02-14T00:00:00Z",
  "validUntil": "2026-08-14T00:00:00Z"
}
```

**Revenue distribution:** Pool revenue (lease fees collected from clients) is distributed proportionally via the [settlement protocol](./DID-HIVE-SETTLEMENTS.md). Each allocation generates routing revenue sharing receipts (`HTLCForwardReceipt`) that flow through the standard settlement netting process. Providers receive their share at each settlement window.

**Pool manager compensation:** The pool manager takes a management fee (configurable, typically 5-15% of pool revenue) settled via [Type 9 (Advisor Fee Settlement)](./DID-HIVE-SETTLEMENTS.md#9-advisor-fee-settlement).

**Withdrawal:** Providers give notice (default: 7 days), and their capital is returned as existing allocations expire. Emergency withdrawal forfeits any pending revenue share for the current period.

**Risk sharing:** If a client's channel force-closes, the on-chain fee and CSV delay cost are distributed proportionally across contributing providers, not borne by a single provider. This is the key advantage over individual leasing.

### Type 3: JIT (Just-In-Time) Liquidity

**Definition:** On-demand channel open when a node needs inbound capacity for a specific payment or corridor. The provider detects the need (via monitoring or explicit request) and opens a channel with provider capital, timed to arrive before the payment.

**Flow:**

```
Client/Advisor                   Provider                    Network
     │                              │                           │
     │  1. JIT request:             │                           │
     │     need 2M inbound          │                           │
     │     corridor: exchange_peer  │                           │
     │     urgency: 10 blocks       │                           │
     │  ─────────────────────────►  │                           │
     │                              │                           │
     │  2. Quote: 5000 sats flat    │                           │
     │     + channel open fee       │                           │
     │     ETA: 2 blocks            │                           │
     │  ◄─────────────────────────  │                           │
     │                              │                           │
     │  3. Accept + escrow ticket   │                           │
     │     HTLC: H(channel_txid)    │                           │
     │  ─────────────────────────►  │                           │
     │                              │                           │
     │                              │  4. Open channel          │
     │                              │  ───────────────────────► │
     │                              │                           │
     │  5. Channel confirmed        │                           │
     │  ◄─────────────────────────  │  ◄─────────────────────── │
     │                              │                           │
     │  6. Reveal channel_txid      │                           │
     │     (preimage for escrow)    │                           │
     │  ─────────────────────────►  │                           │
     │                              │                           │
```

**Escrow:** The HTLC preimage is the funding transaction ID. The client can independently verify the channel was opened by checking the chain. Once confirmed, the client reveals the txid as the preimage, releasing the escrow ticket.

**Time-critical settlement:** JIT requires fast escrow. The escrow ticket timelock is short (6 hours default). If the provider doesn't open the channel within the urgency window, the client reclaims via timelock.

**Advisor integration:** The AI advisor (per [Fleet Management](./DID-L402-FLEET-MANAGEMENT.md)) can trigger JIT requests automatically when it detects a client node needs inbound for a specific corridor — using the monitoring credential to observe traffic patterns and the management credential to execute the liquidity purchase within budget constraints.

### Type 4: Sidecar Channels

**Definition:** A third party (the funder) pays for a channel to be opened between two other nodes. Three-party coordination: the funder provides capital, the two endpoint nodes cooperate on a dual-funded channel open.

**Three-party escrow:**

```
Funder (F)              Node A              Node B              Mint
    │                     │                   │                   │
    │  1. Mint escrow:    │                   │                   │
    │     P2PK: multisig  │                   │                   │
    │     (A + B, 2-of-2) │                   │                   │
    │     HTLC: H(funding_txid)               │                   │
    │  ────────────────────────────────────────────────────────►  │
    │                     │                   │                   │
    │  2. Send tickets    │                   │                   │
    │     + sidecar terms │                   │                   │
    │  ──────────────────►│  ────────────────►│                   │
    │                     │                   │                   │
    │                     │  3. Dual-funded   │                   │
    │                     │     channel open  │                   │
    │                     │  ◄───────────────►│                   │
    │                     │                   │                   │
    │  4. Channel         │                   │                   │
    │     confirmed       │                   │                   │
    │  ◄──────────────────│                   │                   │
    │                     │                   │                   │
    │  5. A + B sign      │                   │                   │
    │     redemption      │                   │                   │
    │     (NUT-11 multisig)                   │                   │
    │                     │  ──────────────────────────────────►  │
    │                     │                   │                   │
```

The escrow ticket uses NUT-11 multisig: `n_sigs: 2` with `pubkeys: [A_pubkey, B_pubkey]`. Both endpoint nodes must sign to redeem, ensuring both cooperated on the channel open. The HTLC hash is `H(funding_txid)`, verified on-chain.

**Revenue sharing:** The funder earns a share of routing revenue flowing through the sidecar channel. This is settled via [Type 1 (Routing Revenue Sharing)](./DID-HIVE-SETTLEMENTS.md#1-routing-revenue-sharing) with the funder as a third participant.

**Use case:** A large routing node wants to improve connectivity between two well-positioned peers without committing its own channel slots. It funds a sidecar channel between them and earns passive routing revenue.

### Type 5: Liquidity Swaps

**Definition:** Bilateral exchange — "I give you X sats of inbound on my node, you give me X sats of inbound on yours." Zero net capital movement; both sides benefit from improved topology.

**Flow:**

```
Node A                              Node B
  │                                    │
  │  1. Swap proposal:                 │
  │     A opens 5M to B               │
  │     B opens 5M to A               │
  │     Duration: 90 days             │
  │  ──────────────────────────────►   │
  │                                    │
  │  2. Accept                         │
  │  ◄──────────────────────────────   │
  │                                    │
  │  3. Simultaneous channel opens     │
  │  ◄─────────────────────────────►   │
  │                                    │
  │  [Settlement handles bookkeeping:  │
  │   Both sides owe each other the    │
  │   same amount → nets to zero]      │
  │                                    │
```

**Settlement:** Both parties' obligations net to zero in the [bilateral netting](./DID-HIVE-SETTLEMENTS.md#bilateral-netting) process. If capacities are unequal (A opens 5M, B opens 3M), the difference is settled as a standard lease payment.

**Proof:** Both channels must exist and maintain capacity for the agreed duration. Heartbeat attestations (same as Type 1) confirm ongoing availability.

**Matching:** The marketplace facilitates swap matching — nodes advertise their topology and desired connections. The discovery system matches complementary needs. Nodes with high connectivity to different regions of the graph are natural swap partners.

### Type 6: Submarine Swaps

**Definition:** On-chain ↔ Lightning conversion as a service. The provider holds on-chain capital and creates Lightning liquidity on demand (or reverse: drains Lightning channels to on-chain).

**Protocol:** Uses existing submarine swap protocols (Boltz-style) with DID authentication and reputation:

```
Client                              Provider (Swap Service)
  │                                    │
  │  1. Swap request:                  │
  │     Direction: on-chain → LN       │
  │     Amount: 1M sats                │
  │  ──────────────────────────────►   │
  │                                    │
  │  2. Quote: 0.5% fee               │
  │     Provider creates LN invoice    │
  │     with H(preimage)               │
  │  ◄──────────────────────────────   │
  │                                    │
  │  3. Client sends on-chain tx       │
  │     to provider's HTLC address     │
  │     (locked to same H(preimage))   │
  │  ──────────────────────────────►   │
  │                                    │
  │  4. Provider pays LN invoice       │
  │     (reveals preimage to           │
  │      claim on-chain HTLC)          │
  │  ◄──────────────────────────────   │
  │                                    │
```

**No additional escrow needed:** Submarine swaps are natively atomic via HTLCs — the provider can only claim on-chain funds by paying the Lightning invoice (revealing the preimage), and vice versa.

**DID value-add:** The swap service authenticates via DID, builds reputation for reliable swaps (completion rate, speed, fee competitiveness), and can be discovered through the marketplace. Clients choose swap providers based on verifiable track record rather than trusting a random website.

**Reputation profile:** `hive:liquidity-provider` with swap-specific metrics (swap completion rate, average swap time, fee consistency).

### Type 7: Turbo Channels

**Definition:** Zero-conf channel opens for trusted providers with high reputation scores. The client receives usable liquidity immediately without waiting for on-chain confirmations.

**Trust model:** The client accepts unconfirmed channels only from providers whose `hive:liquidity-provider` reputation meets a threshold (configurable, default: reputation score > 80 with > 90 days tenure). The provider takes the confirmation risk — if the funding transaction is double-spent, the provider loses the capital.

**Pricing:** Turbo channels carry a premium (typically 10-25% above standard lease rates) reflecting the provider's confirmation risk.

**Escrow:** Standard lease escrow (milestone tickets), but the first heartbeat period begins immediately upon the unconfirmed channel appearing in the peer's channel list — not upon on-chain confirmation. The provider starts earning immediately, compensating for the risk.

**Risk mitigation:** Providers can mitigate double-spend risk by:
- Using high-fee-rate funding transactions
- Only offering turbo channels to clients with high reputation
- Limiting turbo channel capacity to amounts where the double-spend risk is economically irrational

> **⚠️ Double-spend attack:** A malicious client could request a turbo channel, immediately route payments through it (consuming the provider's capital), then double-spend the funding transaction. The provider loses both the channel capacity and any payments routed through it. **Mitigation:** Turbo channels should only be offered to clients with reputation bond ≥ the channel capacity, ensuring the client has more at stake than they could steal.

### Type 8: Balanced Channel Service

**Definition:** Provider opens a channel AND pushes half the capacity to the client's side. The client gets both inbound AND outbound immediately.

**Flow:**

```
Client                              Provider
  │                                    │
  │  1. Request balanced channel:      │
  │     Total capacity: 10M sats       │
  │     (5M inbound + 5M outbound)     │
  │  ──────────────────────────────►   │
  │                                    │
  │  2. Quote: lease_fee + push_fee    │
  │  ◄──────────────────────────────   │
  │                                    │
  │  3. Accept + escrow                │
  │  ──────────────────────────────►   │
  │                                    │
  │  4. Provider opens 10M channel     │
  │     with push_msat = 5M            │
  │  ◄──────────────────────────────   │
  │                                    │
```

**Pricing:** Premium over standard leasing because the provider commits the full channel capacity AND gives away half of it. The push amount is non-recoverable — the client owns those sats. Pricing reflects: lease fee (for the inbound half) + push premium (for the outbound half, typically near-face-value minus a small discount).

**Escrow:** Two-part escrow ticket:
1. **Push payment:** Released when the channel is confirmed on-chain with the correct push amount (verifiable from the funding transaction output)
2. **Lease component:** Standard milestone tickets for ongoing heartbeat verification of the inbound half

### Type 9: Liquidity Insurance

**Definition:** Provider guarantees minimum inbound capacity for a period. If the client's inbound capacity on the insured channel drops below a threshold (due to routing consuming the balance), the provider rebalances to restore it.

**Terms:**

```json
{
  "type": "LiquidityInsurancePolicy",
  "insurer": "did:cid:<provider_did>",
  "insured": "did:cid:<client_did>",
  "channel_id": "931770x2363x0",
  "guaranteed_inbound_sats": 3000000,
  "threshold_pct": 60,
  "restoration_window_hours": 4,
  "premium_sats_per_day": 50,
  "coverage_period_days": 30,
  "max_restorations_per_period": 10,
  "restoration_cost_coverage": "provider_bears_routing_fees"
}
```

**Mechanism:** The provider monitors the insured channel (via monitoring credential or periodic heartbeat). When inbound capacity drops below `threshold_pct` of `guaranteed_inbound_sats`, the provider must rebalance to restore capacity within `restoration_window_hours`.

**Escrow:**
1. **Premium escrow:** Client pays daily premium via Bolt12 recurring offer or pre-funded Cashu milestone tickets (one per day).
2. **Top-up guarantee bond:** Provider posts a Cashu bond (NUT-11 multisig: provider + client) equal to the estimated cost of `max_restorations_per_period` rebalances. If the provider fails to restore within the window, the client can claim from the bond (with evidence of the missed restoration — the heartbeat showing capacity below threshold + elapsed time > window).

**Proof of restoration:** Provider submits a signed attestation showing the channel balance was restored, verified by the client's next heartbeat check.

> **⚠️ Moral hazard:** A client could intentionally drain the insured channel (by routing large payments through it) to force costly restorations by the provider. **Mitigation:** The `max_restorations_per_period` cap limits provider exposure. Repeated restoration triggers increase the premium at renewal (experience-rated pricing). Providers can also stipulate that client-initiated routing drains above a threshold void the insurance for that drain event.

---

## Pricing Models

### Sat-Hours

The base unit for liquidity pricing. Denominates the cost of holding X sats of capacity available for Y hours.

```
cost = capacity_sats × duration_hours × rate_per_sat_hour

Example:
  5,000,000 sats × 720 hours (30 days) × 0.000001 sats/sat-hour = 3,600 sats
```

**Market rate:** The `rate_per_sat_hour` is market-driven. Providers advertise rates; consumers choose. Initial calibration should target ~1-5% annualized yield on committed capital (competitive with on-chain lending rates).

**Rate advertisement:** Providers publish their sat-hour rate in their `LiquidityServiceProfile` (see [Section 4](#4-liquidity-provider-profiles)).

### Flat Fee

Simple per-channel-open fee. Best for JIT and sidecar services where the pricing event is a single action.

```
cost = base_fee + (capacity_sats × rate_ppm)

Example:
  base_fee: 1000 sats
  capacity: 5,000,000 sats
  rate: 200 ppm
  total: 1000 + 1000 = 2000 sats
```

### Revenue Share

Provider takes a percentage of routing revenue earned through the leased capacity. Aligns incentives — provider benefits when the client routes more.

```
provider_share = routing_revenue_through_leased_channel × share_pct / 100

Example:
  Revenue through leased channel: 50,000 sats/month
  Share: 20%
  Provider earns: 10,000 sats/month
```

**Settlement:** Revenue share is settled via [Type 1 (Routing Revenue Sharing)](./DID-HIVE-SETTLEMENTS.md#1-routing-revenue-sharing) from the Settlements spec. Forwarding receipts through the leased channel are tagged with the lease ID, enabling attribution.

**Minimum guarantee:** Providers may require a minimum monthly payment regardless of routing volume, with revenue share kicking in above the minimum. This protects against clients who lease capacity but don't route through it.

### Yield Curve

Longer commitments get lower rates. Incentivizes stability for providers (less capital churn) and lower costs for clients (commitment discount).

| Duration | Rate Modifier |
|----------|--------------|
| Spot / JIT (< 1 day) | 2.0× base rate |
| Short-term (1-7 days) | 1.5× base rate |
| Medium-term (7-30 days) | 1.0× base rate |
| Long-term (30-90 days) | 0.8× base rate |
| Extended (90-365 days) | 0.6× base rate |

**Early termination:** Clients who terminate early pay the rate for the actual duration used, not the committed rate. Example: a client commits for 90 days (0.8× rate) but terminates at day 30 — they pay the 30-day rate (1.0×) for those 30 days, with the difference deducted from any remaining escrow.

### Auction-Based

Nodes bid for liquidity from a pool or provider. Sealed-bid auction using the [marketplace's auction mechanism](./DID-HIVE-MARKETPLACE.md#sealed-bid-auctions).

**Flow:**
1. Provider announces available capacity (e.g., "10M sats available for 30-day leases")
2. Clients submit sealed bids (capacity requested + max price per sat-hour)
3. After bid deadline, provider allocates capacity to highest bidders
4. First-price or second-price auction (configurable)

**Sealed-bid privacy:** Bids are encrypted to the provider's DID pubkey. Commitment hashes prevent post-deadline manipulation (same scheme as marketplace RFP bids).

### Dynamic Pricing

Rates adjust based on network-wide liquidity demand, measured via hive intelligence:

```
dynamic_rate = base_rate × demand_multiplier(corridor) × scarcity_multiplier(provider)

where:
  demand_multiplier = f(
    recent_JIT_requests_for_corridor,
    corridor_routing_volume,
    corridor_failure_rate
  )
  
  scarcity_multiplier = f(
    provider_utilization_pct,
    provider_remaining_capacity,
    market_average_utilization
  )
```

**Hive intelligence:** Dynamic pricing requires network-wide demand signals. These are derived from:
- Pheromone markers indicating high-traffic corridors
- Intelligence market data (routing success rates, fee maps)
- Provider utilization reports (shared via gossip at aggregate level)

**Privacy consideration:** Dynamic pricing reveals demand information. Providers learn which corridors are in demand; this is competitive intelligence. See [Section 13](#13-privacy) for mitigations.

### Price Discovery

The market finds equilibrium through:

1. **Profile transparency:** Provider rates are published in service profiles. Consumers see the range of available prices.
2. **Auction competition:** Bidding reveals willingness-to-pay.
3. **Historical data:** Completed leases generate price records (anonymized, aggregated) that serve as market benchmarks. Published as hive intelligence.
4. **Reputation-price correlation:** Providers with better uptime and completion rates command premium pricing. The market naturally prices reliability.

---

## 4. Liquidity Provider Profiles

### LiquidityServiceProfile Credential

Providers advertise services by publishing a `LiquidityServiceProfile` — extending the [HiveServiceProfile](./DID-HIVE-MARKETPLACE.md#hiveserviceprofile-credential) with liquidity-specific fields:

```json
{
  "@context": [
    "https://www.w3.org/ns/credentials/v2",
    "https://hive.lightning/liquidity/v1"
  ],
  "type": ["VerifiableCredential", "LiquidityServiceProfile"],
  "issuer": "did:cid:<provider_did>",
  "validFrom": "2026-02-14T00:00:00Z",
  "validUntil": "2026-05-14T00:00:00Z",
  "credentialSubject": {
    "id": "did:cid:<provider_did>",
    "displayName": "BigNode Liquidity",
    "serviceTypes": ["leasing", "jit", "turbo", "balanced", "swap", "submarine", "insurance"],
    "capital": {
      "totalAvailableSats": 100000000,
      "minLeaseSats": 1000000,
      "maxLeaseSats": 20000000,
      "currentUtilizationPct": 35
    },
    "pricing": {
      "leasing": {
        "satHourRate": 0.000001,
        "yieldCurveEnabled": true,
        "minimumDays": 7,
        "maximumDays": 365
      },
      "jit": {
        "flatFeeSats": 2000,
        "ratePpm": 200,
        "maxResponseBlocks": 3
      },
      "turbo": {
        "premiumPct": 15,
        "minClientReputation": 80
      },
      "balanced": {
        "pushPremiumPct": 95
      },
      "submarine": {
        "feePct": 0.5,
        "minSwapSats": 100000,
        "maxSwapSats": 10000000,
        "directions": ["onchain_to_ln", "ln_to_onchain"]
      },
      "insurance": {
        "dailyPremiumPerMsats": 10,
        "maxRestorations": 10,
        "restorationWindowHours": 4
      },
      "acceptedPayment": ["cashu", "bolt11", "bolt12", "l402"],
      "preferredPayment": "bolt12",
      "acceptableMints": ["https://mint.hive.lightning"],
      "revenueShareAvailable": true,
      "revenueSharePct": 20,
      "auctionParticipation": true
    },
    "channelTypes": {
      "public": true,
      "private": true,
      "turboZeroConf": true,
      "dualFunded": true
    },
    "topology": {
      "wellConnectedTo": ["ACINQ", "Kraken", "River", "CashApp"],
      "regions": ["US", "EU"],
      "avgChannelCapacitySats": 8000000,
      "totalChannels": 85
    },
    "sla": {
      "uptimeTargetPct": 99.5,
      "heartbeatFrequencyMinutes": 60,
      "maxResponseTimeMinutes": 10,
      "forceClosePolicy": "provider_bears_onchain_fee"
    },
    "reputationRefs": [
      "did:cid:<reputation_credential_1>",
      "did:cid:<reputation_credential_2>"
    ]
  }
}
```

### Service Domain: `liquidity:*`

The `liquidity` domain extends the marketplace specialization taxonomy:

| Specialization | Description |
|---------------|-------------|
| `liquidity:leasing` | Channel leasing — parking inbound capacity |
| `liquidity:pool` | Liquidity pool management/participation |
| `liquidity:jit` | Just-in-time channel opens |
| `liquidity:sidecar` | Third-party funded channels |
| `liquidity:swap` | Bilateral liquidity swaps |
| `liquidity:submarine` | On-chain ↔ Lightning swaps |
| `liquidity:turbo` | Zero-conf channel opens |
| `liquidity:balanced` | Balanced channel service |
| `liquidity:insurance` | Capacity maintenance guarantees |

### Reputation Profile: `hive:liquidity-provider`

A new reputation domain for liquidity providers, tracked via `DIDReputationCredential`:

```json
{
  "domain": "hive:liquidity-provider",
  "metrics": {
    "uptime_pct": 99.2,
    "capital_utilization_pct": 65,
    "lease_completion_rate": 0.98,
    "avg_yield_delivered_annualized_pct": 3.2,
    "heartbeat_reliability": 0.997,
    "force_close_rate": 0.01,
    "jit_response_time_median_seconds": 45,
    "total_capital_deployed_sats": 500000000,
    "unique_clients_served": 34,
    "tenure_days": 180,
    "disputes_lost": 0,
    "insurance_restoration_success_rate": 1.0
  }
}
```

### Provider Tiers

| Tier | Requirements | Benefits |
|------|-------------|----------|
| **New Provider** | DID + profile published | Listed in marketplace; escrow required for all services |
| **Verified Provider** | 30+ days, 5+ completed leases, reputation > 60 | Reduced escrow requirements; listed prominently |
| **Premium Provider** | 90+ days, 20+ completed leases, reputation > 80, > 50M sats deployed | Turbo channel eligible; pool manager eligible; premium marketplace placement |
| **Institutional Provider** | 180+ days, reputation > 90, > 200M sats deployed, 0 force closes | Insurance underwriter eligible; dynamic pricing privilege; cross-hive discovery featured |

---

## 5. Escrow for Liquidity Services

Each service type uses the [Cashu escrow protocol](./DID-CASHU-TASK-ESCROW.md) adapted to its settlement pattern:

### Channel Leasing Escrow

**Mechanism:** Milestone tickets — one per heartbeat period.

```
Total lease: 30 days at 3,600 sats
Heartbeat: hourly
Tickets: 720 milestone tickets × 5 sats each

Each ticket:
  P2PK: provider's DID pubkey
  HTLC: H(heartbeat_secret_i) — client holds secret, reveals on valid heartbeat
  Timelock: heartbeat_period_end + 2 hours buffer
  Refund: client's pubkey
```

**Progressive release:** Each hour, the provider sends a heartbeat attestation. The client verifies capacity, then reveals the heartbeat preimage. The provider redeems that hour's ticket. Missed heartbeats → unredeemed tickets → client reclaims via timelock.

### JIT Escrow

**Mechanism:** Single-task ticket.

```
Ticket: flat_fee + channel_open_cost
  P2PK: provider's DID pubkey
  HTLC: H(funding_txid) — client verifies channel open on-chain
  Timelock: urgency_window + 6 hours
  Refund: client's pubkey
```

The client can independently verify the funding transaction on-chain. Once confirmed, the txid serves as the preimage.

### Sidecar Escrow

**Mechanism:** Three-party escrow with NUT-11 multisig.

```
Ticket: sidecar_fee
  P2PK: multisig(node_A_pubkey, node_B_pubkey), n_sigs: 2
  HTLC: H(funding_txid)
  Timelock: coordination_window + 24 hours
  Refund: funder's pubkey
```

Both endpoint nodes must cooperate (dual signatures) to redeem, proving both participated in the channel open. The funder reclaims via timelock if coordination fails.

### Pool Share Escrow

**Mechanism:** Pool share tokens as Cashu tokens with pool-specific conditions.

```
Share token:
  P2PK: pool_manager_pubkey
  Tags: ["pool_id", "<id>"], ["provider_did", "<did>"], ["share_pct", "40"]
  Timelock: minimum_lock_period_end
  Refund: provider's pubkey
```

The pool manager holds the tokens (representing provider capital commitments) and uses them to mint allocation-specific escrow tickets for clients. When a provider withdraws, the pool manager returns the share token, and the provider redeems it.

### Insurance Escrow

**Mechanism:** Two separate escrow constructions.

1. **Premium escrow (client pays):** Daily milestone tickets, released on each day the insurance is active (verified by heartbeat showing capacity at or above threshold, OR a successful restoration).

2. **Top-up guarantee bond (provider posts):**
```
Bond:
  P2PK: multisig(provider_pubkey, client_pubkey), n_sigs: 1
  Tags: ["insurance_policy_id", "<id>"]
  Timelock: coverage_period_end + 7 days
  Refund: provider's pubkey (after coverage period)
```

The `n_sigs: 1` with both pubkeys means **either** party can spend. The client claims from the bond by presenting evidence of a missed restoration (heartbeat showing capacity below threshold + time elapsed > restoration window). The provider reclaims after the coverage period if no valid claims exist.

> **⚠️ Race condition:** With `n_sigs: 1`, both parties can try to claim simultaneously. The mint processes the first valid spend. **Mitigation:** The client's claim requires a signed evidence attestation (capacity proof + timestamp). The provider's reclaim is only valid after the timelock. During the coverage period, only the client can spend (provider has no evidence to claim their own bond). After the timelock, the provider can reclaim unclaimed bonds.

### Submarine Swap Escrow

**No additional escrow needed.** Submarine swaps are natively atomic via on-chain HTLCs — the provider can only claim the client's on-chain funds by paying the Lightning invoice (revealing the preimage), and the client can only lose funds if they voluntarily pay the on-chain HTLC. The swap protocol itself provides the escrow.

**DID authentication** adds accountability: if a swap provider repeatedly fails to complete swaps (takes on-chain funds but doesn't pay Lightning invoice before timeout), their `hive:liquidity-provider` reputation is damaged.

---

## 6. Proof Mechanisms

### Channel Existence Proof

**Verification:** The channel funding transaction is on-chain. Anyone can verify:
- The funding output exists at the claimed transaction
- The output amount matches the claimed capacity
- The output is unspent (channel is still open)

**Gossip verification:** For public channels, the channel announcement in the gossip network confirms both endpoints. For private channels, the client probes the channel or verifies via the peer connection.

### Capacity Availability Proof

**Mechanism:** Periodic signed attestations from the provider:

```json
{
  "type": "CapacityAttestation",
  "provider": "did:cid:<provider_did>",
  "client": "did:cid:<client_did>",
  "channel_id": "931770x2363x0",
  "total_capacity_sats": 5000000,
  "remote_balance_sats": 4800000,
  "local_balance_sats": 200000,
  "timestamp": "2026-02-14T14:00:00Z",
  "signature": "<provider_sig>"
}
```

**Trust model:** The provider self-reports balance. The client can independently verify:
1. **Probing:** Send a probe payment (amount = claimed inbound) through the channel. If it succeeds in routing (gets to the provider and fails with `incorrect_payment_details`), the capacity exists.
2. **Gossip capacity:** Public channels have gossip-advertised capacity (but not balance).
3. **Historical consistency:** A provider who consistently over-reports capacity will be caught when probes fail.

> **⚠️ Probe privacy:** Probing reveals the client's interest in the channel balance to the provider. This is acceptable since they already have a contractual relationship.

### Routing Proof

**Mechanism:** Signed forwarding receipts showing traffic flowed through leased capacity. Uses the same `HTLCForwardReceipt` format from [Settlements Type 1](./DID-HIVE-SETTLEMENTS.md#1-routing-revenue-sharing).

**Purpose:** Required for revenue-share pricing models. The provider proves that their leased channel was actually used for routing (justifying their revenue share).

### Uptime Proof

**Mechanism:** Heartbeat attestations via Bolt 8 custom messages. The heartbeat protocol:

1. Client sends a challenge nonce via custom message type 49153 (using a `hive:liquidity/heartbeat` schema)
2. Provider responds with signed attestation including the nonce, current capacity, and timestamp
3. Client verifies signature, capacity, and nonce freshness

**Frequency:** Configurable per lease (default: hourly). More frequent heartbeats increase verification confidence but add message overhead.

**Offline tolerance:** A single missed heartbeat is not penalized. Two consecutive misses trigger a warning. Three consecutive misses terminate the lease (remaining escrow refunds to client).

### Revenue Proof

**Mechanism:** For revenue-share models, the provider submits signed forwarding totals at each settlement window:

```json
{
  "type": "RevenueAttestation",
  "lease_id": "<lease_id>",
  "provider": "did:cid:<provider_did>",
  "period": {
    "start": "2026-02-14T00:00:00Z",
    "end": "2026-02-15T00:00:00Z"
  },
  "forwards_through_leased_channel": 47,
  "total_fees_earned_msat": 23500,
  "provider_share_msat": 4700,
  "receipt_merkle_root": "sha256:<root_of_forwarding_receipts>",
  "signature": "<provider_sig>"
}
```

The client can spot-check by comparing the merkle root against individual `HTLCForwardReceipt` records exchanged during the period.

---

## 7. Settlement Integration

### Settlement Type Extension

Liquidity services extend the existing settlement types rather than creating new ones:

| Liquidity Service | Settlement Type | Notes |
|-------------------|----------------|-------|
| Channel Leasing | **Type 3** (extended) | Progressive milestone tickets; heartbeat-verified |
| Liquidity Pools | **Type 3** + **Type 1** | Type 3 for client→pool; Type 1 for pool→provider revenue distribution |
| JIT Liquidity | **Type 3** (single-event) | One-shot lease; escrow released on channel confirmation |
| Sidecar Channels | **Type 3** + **Type 4** | Type 3 for funder payment; Type 4 (splice/shared) for revenue attribution |
| Liquidity Swaps | **Type 3** (bilateral, netting to zero) | Both sides owe each other; nets in bilateral settlement |
| Submarine Swaps | N/A (atomic) | HTLC-native; no settlement protocol involvement |
| Turbo Channels | **Type 3** (with early start) | Same as leasing but heartbeats begin pre-confirmation |
| Balanced Channels | **Type 3** + one-time push | Push amount settled separately; lease component is standard Type 3 |
| Liquidity Insurance | **Type 3** (premium) + bond | Premium via Type 3 milestones; bond is separate NUT-11 escrow |

### Netting

Liquidity obligations participate in standard [bilateral](./DID-HIVE-SETTLEMENTS.md#bilateral-netting) and [multilateral netting](./DID-HIVE-SETTLEMENTS.md#multilateral-netting):

```
Example netting between Node A (client) and Node B (provider):

A owes B: 3600 sats (lease payment for this period)
B owes A: 1200 sats (routing revenue share through A's channels)
B owes A:  500 sats (rebalancing cost settlement)

Net: A pays B 1900 sats (one Cashu ticket instead of three)
```

### Multi-Party Settlement for Pools and Sidecars

**Pools:** The pool manager aggregates all client lease payments, deducts management fees, and distributes to providers proportionally. This is a multilateral settlement where:
- Clients → Pool (lease payments)
- Pool → Providers (revenue distribution)
- Pool → Manager (management fees)

All three flows participate in the standard netting process.

**Sidecars:** Three-party settlement:
- Funder → Endpoint nodes (sidecar fee, split between both endpoints for cooperation)
- Endpoint nodes → Funder (revenue share from routing through the sidecar channel)

This nets bilaterally between the funder and each endpoint, then multilaterally if all three are in the same hive.

---

## 8. Capital Efficiency

### Portfolio Management

Providers optimize capital allocation across multiple clients, corridors, and durations:

```
Provider Portfolio:
  Total Capital: 100M sats
  
  Allocation Strategy:
  ├── 40% Long-term leases (90+ days, low yield, stable)
  ├── 30% Medium-term leases (30-90 days, moderate yield)
  ├── 15% JIT reserve (high yield per event, unpredictable)
  ├── 10% Pool participation (diversified, managed by pool operator)
  └──  5% Insurance bonds (low usage, premium income)
```

**Diversification:** Spread capital across clients to limit exposure to any single force-close event. Across corridors to capture demand from different network regions. Across durations to balance yield and flexibility.

### Capital Recycling

When a lease ends, the provider's capital is automatically re-offered to the marketplace:

1. Lease expires or terminates
2. Provider's profile auto-updates `currentUtilizationPct`
3. If `autoRelist: true`, the freed capacity is immediately available for new leases
4. The advisor (if managing the provider's portfolio) evaluates whether to relist at the same rate, adjust pricing, or reallocate to a different service type

### Yield Optimization Advisor

A meta-service: an advisor that manages a liquidity provider's portfolio. This advisor:
- Monitors market demand across corridors
- Adjusts pricing in response to utilization and competition
- Recommends reallocation of capital between service types
- Optimizes the yield curve for the provider's risk tolerance

This uses the same [Fleet Management](./DID-L402-FLEET-MANAGEMENT.md) credential and escrow infrastructure — the advisor manages the provider's liquidity portfolio under a management credential, paid via performance share of the provider's liquidity revenue.

---

## 9. Risk Management

### Provider Risks

| Risk | Impact | Probability | Mitigation |
|------|--------|------------|-----------|
| Client force-closes leased channel | Capital locked for CSV delay (144+ blocks); on-chain fee cost; lost routing revenue during lockup | Medium | Bond requirement for clients; reputation penalty; insurance product covers on-chain fees |
| Channel stuck in pending | Capital committed to an unconfirmed funding tx; opportunity cost | Low | Timeout mechanism; RBF for funding transactions; reserve capacity for stuck channels |
| On-chain fee spikes | Channel open/close costs exceed lease revenue | Medium (cyclical) | Dynamic pricing adjusts for on-chain fee environment; fee-rate floor in lease terms |
| Client defaults on revenue-share | Client routes through leased channel but disputes revenue | Low | Signed forwarding receipts; settlement arbitration |
| Capital lockup concentration | Too much capital with one client; if they go dark, capital is stuck | Medium | Portfolio diversification limits; max single-client allocation |
| Turbo channel double-spend | Client double-spends funding tx after routing through zero-conf channel | Low (requires malice + technical sophistication) | Reputation bond ≥ channel capacity; high-fee-rate funding; limit turbo to high-rep clients |

### Client Risks

| Risk | Impact | Probability | Mitigation |
|------|--------|------------|-----------|
| Provider goes offline | Leased capacity disappears; routing revenue drops | Medium | Heartbeat monitoring; escrow auto-refund on missed heartbeats; multi-provider redundancy |
| Provider force-closes | Client loses inbound capacity and pays on-chain fees | Low | Provider reputation (force-close rate tracked); insurance product; provider bond |
| Capacity degradation | Provider routes through leased channel, depleting inbound | Medium | Capacity attestations; threshold monitoring; insurance product for guaranteed minimums |
| Turbo channel not confirmed | Zero-conf channel's funding tx never confirms | Very Low | Only accept turbo from providers with reputation > threshold; small initial amounts |
| Price manipulation | Provider colludes to inflate liquidity prices | Low | Multiple providers; auction mechanism; price transparency; low entry barriers |

### Force Close Cost Allocation

Force closes are the most contentious risk event in leased channels. Clear allocation rules:

| Initiator | Who Pays On-Chain Fees | Rationale |
|-----------|----------------------|-----------|
| Client initiates cooperative close | Split 50/50 | Mutual agreement |
| Client force-closes | **Client pays all on-chain fees** + penalty from bond | Client violated the lease; provider shouldn't bear cost |
| Provider initiates cooperative close | **Provider pays all on-chain fees** + refund of remaining lease escrow | Provider broke the agreement |
| Provider force-closes | **Provider pays all on-chain fees** + refund + reputation slash | Provider violated the lease |
| External event (peer crash, no response) | Default: provider pays (they chose to take the peer risk) | Configurable in lease terms; can be split by agreement |

**Bond enforcement:** Client-initiated force-close costs are deducted from the client's hive bond (if hive member) or from a separate lease bond posted at lease initiation. Non-hive clients must post a lease-specific bond equal to estimated force-close cost (based on current fee environment).

### Channel Reserve Considerations

Lightning protocol requires each party to maintain a reserve (typically 1% of channel capacity). For leased channels:

- The **provider's reserve** is their own capital — they accept this as part of the lease cost.
- The **client's reserve** on the provider's side is functionally zero (the client hasn't pushed any funds). This means the provider may need to push a small amount during channel open to satisfy reserve requirements, or use the `option_channel_reserve` feature to set it to zero.

---

## 10. Integration with Fleet Management

### Advisor-Driven Liquidity Management

The AI advisor (per [Fleet Management](./DID-L402-FLEET-MANAGEMENT.md)) uses liquidity services as a tool for node optimization:

```
┌─────────────────────────────────────────────────────────────────┐
│                    AI ADVISOR                                    │
│                                                                  │
│  1. Monitor node (via monitoring credential)                     │
│     → Detect: node needs 5M sats inbound from exchange corridor │
│                                                                  │
│  2. Query liquidity marketplace                                  │
│     → Filter: providers with connectivity to target corridor     │
│     → Rank: by price, reputation, response time                  │
│                                                                  │
│  3. Select provider based on:                                    │
│     - Budget constraints (operator-defined max spend)            │
│     - Price/reputation tradeoff                                  │
│     - Existing portfolio (avoid concentration)                   │
│                                                                  │
│  4. Execute via management credential:                           │
│     hive:liquidity/lease-request schema                          │
│     → Escrow funded from operator's budget                       │
│     → Lease contracted with selected provider                    │
│                                                                  │
│  5. Ongoing monitoring:                                          │
│     → Verify heartbeats, track capacity                          │
│     → Adjust portfolio as traffic patterns change                │
│     → Renew/terminate leases at expiry                           │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Liquidity Management Schema

New schema for advisor-driven liquidity operations:

```json
{
  "schema": "hive:liquidity/v1",
  "action": "lease_request",
  "params": {
    "capacity_sats": 5000000,
    "direction": "inbound",
    "duration_days": 30,
    "max_cost_sats": 5000,
    "preferred_corridor": ["03exchange_peer...", "03gateway_peer..."],
    "provider_min_reputation": 70,
    "service_type": "leasing",
    "auto_renew": true
  }
}
```

**Required tier:** `advanced` (commits capital via escrow)  
**Danger score:** 5 (commits funds to external contract; bounded by `max_cost_sats`)

Additional actions: `lease_terminate`, `lease_renew`, `swap_request`, `jit_request`, `insurance_purchase`, `portfolio_rebalance`.

### Budget Constraints

Operators set maximum liquidity spend per period in their management credential:

```json
{
  "constraints": {
    "max_liquidity_spend_daily_sats": 10000,
    "max_liquidity_spend_monthly_sats": 100000,
    "max_single_lease_sats": 50000,
    "allowed_service_types": ["leasing", "jit", "insurance"],
    "forbidden_providers": ["did:cid:<blacklisted_provider>"],
    "auto_renew_enabled": true
  }
}
```

The Policy Engine enforces these constraints before any liquidity operation executes.

### Automated Liquidity Optimization

The advisor continuously optimizes the node's liquidity position:

1. **Demand forecasting:** Analyze routing patterns to predict which corridors need more inbound capacity
2. **Lease portfolio management:** Maintain a portfolio of leases that covers predicted demand
3. **Cost optimization:** Switch providers when cheaper options become available (during renewal)
4. **Rebalance vs. lease decision:** For each liquidity need, compare the cost of rebalancing vs. leasing new capacity
5. **Insurance evaluation:** Purchase insurance for critical corridors where capacity loss would significantly impact revenue

---

## 11. Non-Hive Access

### Discovery

Non-hive nodes discover liquidity providers through the same mechanisms as advisor discovery (see [DID Hive Client — Discovery](./DID-HIVE-CLIENT.md#9-discovery-for-non-hive-nodes)):

1. **Archon network** — Query for `LiquidityServiceProfile` credentials
2. **Nostr events** — Providers publish profiles as Nostr events (kind `38383`, tags: `t:hive-liquidity`, `t:liquidity-leasing`)
3. **Direct connection** — Operator has a provider's DID from a referral or directory

```bash
# Discover liquidity providers
lightning-cli hive-client-discover --type="liquidity" --service="leasing" --min-capacity=5000000

# Or via hive-lnd
hive-lnd discover --type=liquidity --service=leasing --min-capacity=5000000
```

### Simplified Contracting

Non-hive nodes skip settlement protocol integration. All payments use direct escrow:

| Full Hive Member | Non-Hive Client |
|-----------------|-----------------|
| Lease payments netted with routing revenue | Lease payments via direct Cashu escrow or Bolt11 |
| Credit tiers reduce escrow requirements | Full escrow required for all services |
| Multi-party netting for pools/sidecars | Direct payment to each party |
| Settlement disputes via arbitration panel | Bilateral dispute → reputation consequences only |

### Payment Methods

Non-hive clients use the full payment stack without settlement netting:

```
Simple lease: Bolt12 recurring offer (provider publishes offer, client auto-pays monthly)
JIT request: Bolt11 invoice (provider quotes, client pays)
Escrow lease: Cashu milestone tickets (same as hive members)
Submarine swap: HTLC-native (same for everyone)
```

---

## 12. Comparison with Existing Solutions

| Property | Lightning Pool | Magma (Amboss) | LNBig | This Protocol |
|----------|---------------|----------------|-------|---------------|
| **Operator** | Lightning Labs | Amboss Technologies | LNBig operator | None (decentralized) |
| **Identity** | Lightning Labs account | Amboss account | Email/Telegram | DIDs (self-sovereign) |
| **Trust model** | Trust Lightning Labs | Trust Amboss | Trust LNBig operator | Trustless (Cashu escrow) |
| **Pricing** | Sealed-bid auction | Fixed rates + marketplace | Manual negotiation | Multiple models (sat-hours, auction, dynamic, revenue-share) |
| **Proof of delivery** | Platform-verified | Platform-verified | Manual verification | Cryptographic (heartbeats, on-chain, probing) |
| **Reputation** | Platform-internal | Amboss score | Informal | Verifiable credentials (cross-platform, portable) |
| **Implementation** | LND only | LND + CLN (limited) | LND only | CLN + LND (full parity) |
| **Service types** | Leasing (auction) | Leasing | Leasing | 9 types (leasing, pool, JIT, sidecar, swap, submarine, turbo, balanced, insurance) |
| **Escrow** | Custodial (Platform holds funds) | Custodial | None (trust-based) | Non-custodial (Cashu P2PK+HTLC) |
| **Privacy** | Platform sees everything | Platform sees everything | Operator sees everything | Blind signatures; minimal disclosure |
| **Censorship resistance** | Platform can ban users | Platform can ban users | Single operator | No central authority |
| **Settlement** | Platform ledger | Platform ledger | Manual | Bilateral/multilateral netting |

### Key Differentiators

1. **Trustless escrow:** No custodial intermediary. Cashu tokens with cryptographic spending conditions replace platform custody.
2. **Verifiable reputation:** Reputation credentials are portable across platforms and cryptographically verifiable, not locked to a single marketplace operator.
3. **Service diversity:** Nine service types vs. single-type (leasing) offered by existing solutions.
4. **Multi-implementation:** Full CLN and LND support via [DID Hive Client](./DID-HIVE-CLIENT.md).
5. **Composability:** Liquidity services compose with fleet management, routing optimization, and intelligence markets through the same protocol suite.

---

## 13. Privacy

### What Liquidity Requests Reveal

A client requesting liquidity reveals:
- **That they need inbound capacity** — implies they expect to receive payments
- **The amount needed** — reveals approximate business volume expectations
- **Desired corridors** — reveals business relationships (e.g., "I need inbound from exchange X")

This is sensitive competitive intelligence.

### Minimum Disclosure Protocol

Clients reveal the minimum necessary at each stage:

| Stage | Disclosed | Hidden |
|-------|-----------|--------|
| Discovery query | Service type, capacity range | Node identity, specific corridors |
| Negotiation | Capacity, duration, max price | Channel graph, existing channels, revenue |
| Contract | Full terms, node pubkey (necessary for channel open) | Other channels, balance distribution, routing patterns |
| Ongoing | Heartbeat data for contracted channel only | All other channel data |

**Anonymous discovery:** Clients can query the marketplace anonymously (no DID in the query). The provider only learns the client's identity when a contract is formed.

### Provider Privacy

Provider capital positions are competitive intelligence:

- **Total available capital** — Reveals financial strength
- **Current utilization** — Reveals demand levels
- **Client list** — Reveals business relationships

**Mitigation:**
1. Providers can publish **ranges** instead of exact figures (`"totalAvailable": "50M-100M sats"`)
2. Client identities are never published; only aggregate metrics appear in reputation
3. Utilization is reported in brackets (0-25%, 25-50%, 50-75%, 75-100%)

### Blind Matching

**Can the marketplace match without revealing identities?**

Partially. The discovery phase can be anonymous. Providers publish profiles; clients query without revealing identity. But channel opens require knowing the peer's pubkey — identity is necessarily revealed at contract time.

**Future work:** A blind matchmaker service could match clients and providers based on requirements, revealing identities only after mutual opt-in. This requires a trusted intermediary (or a cryptographic commit-reveal scheme) and is deferred to a future spec version.

---

## 14. Implementation Roadmap

### Phase 1: Channel Leasing (4–6 weeks)
*Prerequisites: Settlements Type 3 (basic), Task Escrow Phase 1 (milestone tickets)*

- `LiquidityServiceProfile` credential schema
- Lease request/quote/accept negotiation flow
- Heartbeat attestation protocol (custom message schema `hive:liquidity/heartbeat`)
- Milestone escrow ticket creation for leases
- Capacity verification (gossip + probing)
- `hive:liquidity/v1` management schema (lease_request, lease_terminate)
- Basic provider profile and discovery

### Phase 2: JIT & Turbo Channels (3–4 weeks)
*Prerequisites: Phase 1*

- JIT request/response flow with channel-open verification escrow
- Turbo channel trust model (reputation threshold enforcement)
- Fast escrow settlement for time-critical operations
- Integration with fleet management advisor for auto-JIT

### Phase 3: Submarine Swaps & Swaps (3–4 weeks)
*Prerequisites: Phase 1, DID auth infrastructure*

- Submarine swap protocol with DID authentication
- Bilateral liquidity swap matching and settlement
- Swap provider reputation tracking
- Integration with existing swap protocols (Boltz API compatibility)

### Phase 4: Sidecar & Balanced Channels (3–4 weeks)
*Prerequisites: Phase 1, NUT-11 multisig support*

- Three-party sidecar escrow (NUT-11 multisig)
- Dual-funded channel coordination protocol
- Balanced channel service with push verification
- Revenue sharing settlement for sidecar funders

### Phase 5: Liquidity Pools (4–6 weeks)
*Prerequisites: Phase 1, Settlements multilateral netting*

- Pool share credential schema
- Pool manager registration and governance
- Capital contribution and withdrawal flows
- Revenue distribution via settlement protocol
- Pool-level risk management

### Phase 6: Liquidity Insurance (3–4 weeks)
*Prerequisites: Phase 1, NUT-11 multisig for bonds*

- Insurance policy credential schema
- Capacity monitoring and restoration triggers
- Top-up guarantee bond mechanism
- Premium escrow (daily milestone tickets)
- Claims processing

### Phase 7: Dynamic Pricing & Auctions (3–4 weeks)
*Prerequisites: Phase 1, hive intelligence infrastructure*

- Dynamic pricing engine (demand/scarcity multipliers)
- Sealed-bid auction integration
- Yield curve implementation
- Market analytics and price discovery tools

### Phase 8: Portfolio Management & Advisor Integration (4–6 weeks)
*Prerequisites: All previous phases, Fleet Management integration*

- Portfolio optimization advisor schema
- Capital recycling automation
- Yield optimization algorithms
- Budget-constrained liquidity management for fleet advisors

### Cross-Spec Integration Timeline

```
Settlements Type 3     ──────────►  Liquidity Phase 1 (leasing)
                                         │
Task Escrow Phase 1    ──────────►  Liquidity Phase 1 (milestone tickets)
                                         │
Fleet Mgmt Phase 4     ──────────►  Liquidity Phase 2 (advisor integration)
                                         │
NUT-11 multisig        ──────────►  Liquidity Phase 4 (sidecar) + Phase 6 (insurance)
                                         │
Settlements multilateral ─────────►  Liquidity Phase 5 (pools)
                                         │
Hive intelligence      ──────────►  Liquidity Phase 7 (dynamic pricing)
```

---

## 15. Open Questions

1. **Channel ownership:** In a leased channel, who "owns" the routing revenue? If the provider opens a channel to the client and the client routes traffic through it, the client earns the routing fees. The provider earns the lease fee. But what about fees earned on the provider's side of the channel? This needs clear attribution rules per lease terms.

2. **Lease-through-routing conflict:** A provider leasing inbound capacity to a client may also want to route through that channel. Routing consumes the leased capacity. Should leased channels be "reserved" (no provider routing through them) or "shared" (provider can route but must maintain minimum capacity)?

3. **Pool manager trust:** Pool managers have significant power — they allocate capital and collect management fees. What governance mechanisms prevent a malicious pool manager from misallocating funds? Multi-sig with providers? On-chain proof of allocation?

4. **Insurance actuarial data:** Pricing liquidity insurance requires actuarial data — how often does capacity degrade, how much does restoration cost? This data doesn't exist yet. Initial insurance pricing will be guesswork. How do we bootstrap the actuarial model?

5. **Cross-hive liquidity:** Can providers in one hive lease to clients in another? Cross-hive contracts would need cross-hive reputation verification and settlement. This extends the cross-hive questions from the Settlements spec.

6. **Lease secondary market:** Can a client who leased capacity resell it to a third party? A secondary market for lease contracts would improve capital efficiency but adds complexity (assignable credentials, sub-leasing escrow).

7. **Minimum viable liquidity:** What's the minimum capacity that makes economic sense to lease? Below some threshold, the on-chain fees for channel opens/closes exceed the lease revenue. This floor depends on the fee environment and should be dynamically calculated.

8. **Balanced channel pricing:** How should the "push" component of a balanced channel be priced? The provider is giving away sats (push_msat is non-recoverable). Is face value minus a discount appropriate? Or should it be priced as a separate product (outbound liquidity as a service)?

9. **Insurance moral hazard:** Clients with insurance may take more risks (route aggressively through insured channels knowing the provider will restore). How do we prevent moral hazard without making insurance useless? Experience-rated premiums help but need calibration data.

10. **Regulatory considerations:** Liquidity leasing has characteristics of financial lending (capital provided for a period in exchange for yield). Does this create regulatory risk? Jurisdiction-dependent, but the protocol should be designed to avoid creating custodial relationships.

---

## 16. References

### Protocol Suite

- [DID + L402 Remote Fleet Management](./DID-L402-FLEET-MANAGEMENT.md) — Credential system, management schemas, danger scoring
- [DID + Cashu Task Escrow Protocol](./DID-CASHU-TASK-ESCROW.md) — Escrow ticket format, NUT-10/11/14 conditions
- [DID + Cashu Hive Settlements Protocol](./DID-HIVE-SETTLEMENTS.md) — Settlement types, netting, bonds, credit tiers
- [DID Hive Marketplace Protocol](./DID-HIVE-MARKETPLACE.md) — Service advertising, discovery, contracting, reputation
- [DID Hive Client: Universal Lightning Node Management](./DID-HIVE-CLIENT.md) — Client software for non-hive nodes
- [DID Reputation Schema](./DID-REPUTATION-SCHEMA.md) — Reputation credential format, profile definitions

### External References

- [Lightning Pool](https://lightning.engineering/pool/) — Lightning Labs' centralized liquidity auction
- [Magma by Amboss](https://amboss.space/magma) — Amboss liquidity marketplace
- [Boltz Exchange](https://boltz.exchange/) — Non-custodial submarine swap service
- [Dual-Funding Proposal (BOLT draft)](https://github.com/lightning/bolts/pull/851) — Interactive channel funding protocol
- [Liquidity Ads (Lisa Neigut / niftynei)](https://github.com/lightning/bolts/pull/878) — In-protocol liquidity advertising
- [Cashu NUT-10: Spending Conditions](https://github.com/cashubtc/nuts/blob/main/10.md)
- [Cashu NUT-11: Pay-to-Public-Key (P2PK)](https://github.com/cashubtc/nuts/blob/main/11.md)
- [Cashu NUT-14: Hashed Timelock Contracts](https://github.com/cashubtc/nuts/blob/main/14.md)
- [W3C DID Core 1.0](https://www.w3.org/TR/did-core/)
- [W3C Verifiable Credentials Data Model 2.0](https://www.w3.org/TR/vc-data-model-2.0/)
- [Archon: Decentralized Identity for AI Agents](https://github.com/archetech/archon)
- [Lightning Hive: Swarm Intelligence for Lightning](https://github.com/lightning-goats/cl-hive)

---

*Feedback welcome. File issues on [cl-hive](https://github.com/lightning-goats/cl-hive) or discuss in #singularity.*

*— Hex ⬡*
