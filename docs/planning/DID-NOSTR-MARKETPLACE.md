# DID Nostr Marketplace Protocol

**Status:** Proposal / Design Draft  
**Version:** 0.1.1  
**Author:** Hex (`did:cid:bagaaierajrr7k6izcrdfwqxpgtrobflsv5oibymfnthjazkkokaugszyh4ka`)  
**Date:** 2026-02-15  
**Updated:** 2026-02-15 — Client integration updated for cl-hive-comms plugin architecture  
**Feedback:** Open — file issues or comment in #singularity

---

## Abstract

This document is the **authoritative specification** for all Nostr-based marketplace integration in the Lightning Hive protocol suite. It consolidates, extends, and supersedes the Nostr sections in the [Marketplace spec](./DID-HIVE-MARKETPLACE.md) (Section 7 / Nostr advertising) and the [Liquidity spec](./DID-HIVE-LIQUIDITY.md) (Section 11A / Nostr Marketplace Protocol).

The Nostr layer serves as the **public, open marketplace** for Lightning Hive services — the interface that makes advisor management and liquidity services discoverable by the entire Lightning Network without requiring hive membership, custom infrastructure, or platform accounts. Any Nostr client can browse services, view provider profiles, and initiate contracts.

This spec defines:
- A unified event kind allocation for all marketplace service types
- Relay strategy and redundancy
- Spam resistance and anti-abuse mechanisms
- Event lifecycle management (creation, update, expiration, garbage collection)
- Cross-NIP compatibility mapping (NIP-15, NIP-99, NIP-04/NIP-44, NIP-40, NIP-78)
- Dual-publishing strategy for maximum interoperability
- Privacy mechanisms for anonymous browsing, sealed bids, and throwaway identities
- DID-to-Nostr binding and impersonation prevention
- Client integration patterns for `cl-hive-comms` (CLN plugin — handles all Nostr publishing/subscribing)
- Guidance for Nostr-native clients displaying hive services with zero hive-specific code

---

## Relationship to Other Specs

This spec does **not** duplicate content from companion specifications. It references them and adds the Nostr-specific integration layer.

| Spec | What It Defines | What This Spec Adds |
|------|----------------|---------------------|
| [Marketplace](./DID-HIVE-MARKETPLACE.md) | Advisor profiles, discovery, negotiation, contracts | Nostr event kinds for advisor services; dual-publishing |
| [Liquidity](./DID-HIVE-LIQUIDITY.md) | Liquidity service types, escrow, proofs, settlement | Nostr event kinds for liquidity services (originated there, formalized here) |
| [Client](./DID-HIVE-CLIENT.md) | Plugin architecture, discovery pipeline, UX | Nostr subscription/publishing integration |
| [Reputation](./DID-REPUTATION-SCHEMA.md) | Credential schema, scoring, aggregation | Nostr-published reputation summaries |
| [Fleet Management](./DID-L402-FLEET-MANAGEMENT.md) | RPC, delegation, policy enforcement | N/A (internal, not Nostr-facing) |
| [Task Escrow](./DID-CASHU-TASK-ESCROW.md) | Cashu escrow mechanics | Payment method tags in Nostr events |
| [Settlements](./DID-HIVE-SETTLEMENTS.md) | Netting, settlement types | N/A (bilateral, not Nostr-facing) |

**Supersession:** Once this spec is accepted, the following sections become informational references only:
- [DID-HIVE-MARKETPLACE.md § "Advertising via Nostr"](./DID-HIVE-MARKETPLACE.md#advertising-via-nostr-optional)
- [DID-HIVE-LIQUIDITY.md § 11A "Nostr Marketplace Protocol"](./DID-HIVE-LIQUIDITY.md#11a-nostr-marketplace-protocol)

---

## Architecture Overview

```
┌───────────────────────────────────────────────────────────────────────────┐
│                         NOSTR MARKETPLACE LAYER                           │
│                                                                           │
│   ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐       │
│   │  ADVISOR MARKET   │  │ LIQUIDITY MARKET  │  │  BRIDGE LAYER    │       │
│   │                   │  │                   │  │                  │       │
│   │ Kinds 38380-38385 │  │ Kinds 38900-38905 │  │ NIP-15 (30017/8) │       │
│   │ Profiles, Offers  │  │ Profiles, Offers  │  │ NIP-99 (30402)   │       │
│   │ RFPs, Contracts   │  │ RFPs, Contracts   │  │ Dual-publish     │       │
│   │ Heartbeats, Rep   │  │ Heartbeats, Rep   │  │ adapters         │       │
│   └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘       │
│            │                      │                      │                │
│   ┌────────┴──────────────────────┴──────────────────────┴─────────┐      │
│   │                    SHARED INFRASTRUCTURE                        │      │
│   │                                                                 │      │
│   │  DID-Nostr Binding  │  Relay Strategy  │  Spam Resistance      │      │
│   │  Event Lifecycle     │  Privacy Layer   │  Tag Conventions      │      │
│   └─────────────────────────────────────────────────────────────────┘      │
│                                                                           │
│   ┌─────────────────────────────────────────────────────────────────┐      │
│   │                       NOSTR RELAYS                              │      │
│   │                                                                 │      │
│   │  Public relays (nos.lol, damus, nostr.band)                    │      │
│   │  Hive relay (relay.hive.lightning) [future]                    │      │
│   │  Private relay (operator-specific)                             │      │
│   └─────────────────────────────────────────────────────────────────┘      │
└───────────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    │               │               │
              ┌─────┴─────┐  ┌─────┴─────┐  ┌─────┴─────┐
              │ Hive-aware │  │ NIP-99    │  │ NIP-15    │
              │ Clients    │  │ Clients   │  │ Clients   │
              │            │  │           │  │           │
              │ cl-hive-   │  │ Generic   │  │ Plebeian  │
              │ comms      │  │ Nostr     │  │ Market /  │
              │ (plugin)   │  │ clients   │  │ NostrMkt  │
              └────────────┘  └───────────┘  └───────────┘
```

---

## 1. Unified Event Kind Allocation

### Design Decision: Separate Kind Ranges

Advisor services and liquidity services use **separate kind ranges** within the parameterized replaceable range (30000–39999 per NIP-01):

- **Advisor services:** `38380–38389`
- **Liquidity services:** `38900–38909`

**Rationale:**
1. **Semantic clarity** — Relay-side filtering can target an entire service category by kind range without parsing tags.
2. **Independent evolution** — Advisor and liquidity event schemas can evolve independently without version conflicts.
3. **Future extensibility** — Additional service categories (e.g., routing intelligence marketplace, watchtower services) can claim their own ranges without reorganizing existing allocations.
4. **NIP proposal readiness** — If formalized as NIPs, each service category can be proposed independently.

### Complete Kind Table

| Kind | Service | Purpose | Replaceable? | Lifetime |
|------|---------|---------|-------------|----------|
| **Advisor Services** | | | | |
| `38380` | Advisor | Service Profile | Yes (`d` tag) | Until updated/withdrawn |
| `38381` | Advisor | Service Offer | Yes (`d` tag) | Until filled/expired |
| `38382` | Advisor | RFP (node seeking advisor) | Yes (`d` tag) | Until filled/expired |
| `38383` | Advisor | Contract Confirmation | No (immutable) | Permanent |
| `38384` | Advisor | Heartbeat/Status Attestation | Yes (`d` tag) | Current period only |
| `38385` | Advisor | Reputation Summary | Yes (`d` tag) | Until updated |
| `38386–38389` | Advisor | Reserved | — | — |
| **Liquidity Services** | | | | |
| `38900` | Liquidity | Provider Profile | Yes (`d` tag) | Until updated/withdrawn |
| `38901` | Liquidity | Capacity Offer | Yes (`d` tag) | Until filled/expired |
| `38902` | Liquidity | RFP (node seeking liquidity) | Yes (`d` tag) | Until filled/expired |
| `38903` | Liquidity | Contract Confirmation | No (immutable) | Permanent |
| `38904` | Liquidity | Lease Heartbeat Attestation | Yes (`d` tag) | Current period only |
| `38905` | Liquidity | Reputation Summary | Yes (`d` tag) | Until updated |
| `38906–38909` | Liquidity | Reserved | — | — |

> **Migration note:** Kind `38383` was previously used for advisor profiles in the [Marketplace spec](./DID-HIVE-MARKETPLACE.md#advertising-via-nostr-optional). This allocation reassigns `38383` to Contract Confirmation within the advisor range and introduces `38380` for profiles. Existing `38383` profile events should be re-published as `38380` during the migration period. Clients SHOULD accept both kinds during transition.

### Kind Symmetry

The advisor and liquidity ranges are intentionally symmetric — each service category has the same six event types at the same relative offset:

| Offset | Purpose | Advisor Kind | Liquidity Kind |
|--------|---------|-------------|----------------|
| +0 | Provider/Service Profile | 38380 | 38900 |
| +1 | Offer (specific availability) | 38381 | 38901 |
| +2 | RFP (demand broadcast) | 38382 | 38902 |
| +3 | Contract Confirmation | 38383 | 38903 |
| +4 | Heartbeat/Status Attestation | 38384 | 38904 |
| +5 | Reputation Summary | 38385 | 38905 |

This symmetry simplifies client code — a single event handler parameterized by kind offset can process both service categories.

---

## 2. Advisor Event Kinds (NEW)

The [Liquidity spec § 11A](./DID-HIVE-LIQUIDITY.md#11a-nostr-marketplace-protocol) defines liquidity kinds 38900–38905 in full detail. This section defines the **parallel advisor kinds** that did not previously exist.

### Kind 38380: Advisor Service Profile

The advisor's storefront on Nostr. Contains the same information as the `HiveServiceProfile` credential from the [Marketplace spec § 1](./DID-HIVE-MARKETPLACE.md#1-service-advertising), formatted for Nostr consumption.

```json
{
  "kind": 38380,
  "pubkey": "<advisor_nostr_pubkey>",
  "created_at": 1739570400,
  "content": "<JSON-encoded HiveServiceProfile credential>",
  "tags": [
    ["d", "<advisor_did>"],
    ["t", "hive-advisor"],
    ["t", "advisor-fee-optimization"],
    ["t", "advisor-rebalancing"],
    ["t", "advisor-channel-expansion"],
    ["name", "Hex Fleet Advisor"],
    ["capabilities", "fee_optimization", "rebalancing", "channel_expansion", "htlc_management"],
    ["pricing-model", "performance-percentage"],
    ["base-fee-sats", "1000"],
    ["performance-pct", "10"],
    ["nodes-managed", "12"],
    ["uptime", "99.8"],
    ["tenure-days", "365"],
    ["did", "<advisor_did>"],
    ["did-nostr-proof", "<did_to_nostr_attestation_credential>"],
    ["p", "<advisor_nostr_pubkey>"],
    ["alt", "Lightning node advisor — fee optimization, rebalancing, channel expansion"]
  ]
}
```

**Key design decisions:**
- **`capabilities` tag** lists specific management domains (from [Marketplace spec § 1](./DID-HIVE-MARKETPLACE.md#1-service-advertising)). Clients filter by capability to find specialists.
- **`pricing-model` tag** indicates the advisor's preferred billing model. Multiple models can be advertised; specific terms appear in offers (kind 38381).
- **`content` carries the full signed credential** — verifiable independently of the Nostr event signature.
- **`did-nostr-proof` tag** prevents impersonation (see [Section 9: DID-Nostr Binding](#9-did-nostr-binding)).

### Kind 38381: Advisor Service Offer

A specific offer of advisory services — particular capabilities at particular prices for a defined engagement.

```json
{
  "kind": 38381,
  "pubkey": "<advisor_nostr_pubkey>",
  "created_at": 1739570400,
  "content": "<optional markdown description of the offer>",
  "tags": [
    ["d", "<unique_offer_id>"],
    ["t", "hive-advisor-offer"],
    ["capability", "fee_optimization"],
    ["capability", "rebalancing"],
    ["pricing-model", "subscription"],
    ["price", "5000", "sat", "month"],
    ["trial-available", "true"],
    ["trial-days", "7"],
    ["max-channels", "50"],
    ["min-node-capacity", "10000000"],
    ["sla-response-time", "300"],
    ["sla-uptime", "99.5"],
    ["expires", "1742162400"],
    ["did", "<advisor_did>"],
    ["p", "<advisor_nostr_pubkey>"],
    ["payment-methods", "bolt11", "bolt12", "cashu"],
    ["alt", "Node management — fee optimization + rebalancing — 5k sats/month"]
  ]
}
```

**Usage patterns:**
- Advisors publish multiple offers targeting different node sizes or capability bundles.
- The `expires` tag (NIP-40) ensures stale offers auto-filter. See [Section 4: Event Lifecycle](#4-event-lifecycle-management).
- `min-node-capacity` lets advisors target nodes above a minimum size.
- `sla-response-time` (seconds) and `sla-uptime` (percentage) are queryable SLA commitments.

### Kind 38382: Advisor RFP (Request for Proposals)

A node operator broadcasts their need for management services.

```json
{
  "kind": 38382,
  "pubkey": "<client_nostr_pubkey_or_anonymous>",
  "created_at": 1739570400,
  "content": "<optional_encrypted_details>",
  "tags": [
    ["d", "<unique_rfp_id>"],
    ["t", "hive-advisor-rfp"],
    ["capability-needed", "fee_optimization"],
    ["capability-needed", "channel_expansion"],
    ["node-capacity", "50000000"],
    ["channel-count", "25"],
    ["max-price-sats", "10000"],
    ["pricing-model-preferred", "performance-percentage"],
    ["engagement-days", "90"],
    ["bid-deadline", "1739830800"],
    ["did", "<client_did_or_empty>"],
    ["alt", "Seeking advisor — fee optimization + channel expansion — 50M sat node"]
  ]
}
```

**Privacy options** mirror the liquidity RFP ([Liquidity spec § 11A](./DID-HIVE-LIQUIDITY.md#11a-nostr-marketplace-protocol)):
- **Public RFP:** Client includes `did` and `pubkey`. Advisors respond via NIP-44 DM.
- **Anonymous RFP:** Client uses throwaway Nostr key, omits `did`. See [Section 7: Privacy](#7-privacy).
- **Sealed-bid RFP:** Client includes `bid-pubkey` for encrypted responses.

### Kind 38383: Advisor Contract Confirmation

Immutable public record that an advisory engagement was formed.

```json
{
  "kind": 38383,
  "pubkey": "<publisher_nostr_pubkey>",
  "created_at": 1739570400,
  "content": "",
  "tags": [
    ["t", "hive-advisor-contract"],
    ["advisor-did", "<advisor_did>"],
    ["client-did", "<client_did>"],
    ["capabilities", "fee_optimization", "rebalancing"],
    ["engagement-days", "90"],
    ["contract-hash", "<sha256_of_full_contract_credential>"],
    ["e", "<offer_event_id>", "", "offer"],
    ["e", "<rfp_event_id>", "", "rfp"],
    ["alt", "Advisory contract confirmed — fee optimization + rebalancing — 90 days"]
  ]
}
```

**Purpose:**
- Public, timestamped record of contract formation (publishing is optional by either party).
- Links to originating offer/RFP via `e` tags.
- `contract-hash` enables selective verification without disclosing terms.
- Enables marketplace analytics (advisor utilization, engagement volume, pricing trends).

### Kind 38384: Advisor Heartbeat/Status Attestation

Optional public proof that advisory services are being delivered.

```json
{
  "kind": 38384,
  "pubkey": "<advisor_nostr_pubkey>",
  "created_at": 1739574000,
  "content": "",
  "tags": [
    ["d", "<engagement_id>"],
    ["t", "hive-advisor-heartbeat"],
    ["actions-24h", "12"],
    ["actions-total", "847"],
    ["fee-revenue-delta-pct", "+15.3"],
    ["channels-managed", "25"],
    ["uptime-hours", "2160"],
    ["contract-hash", "<sha256_of_contract>"],
    ["sig", "<did_signature_over_attestation>"],
    ["alt", "Advisor heartbeat — 12 actions/24h — +15.3% fee revenue — 2160h uptime"]
  ]
}
```

**Privacy note:** Like liquidity heartbeats, Nostr publication is optional. The primary heartbeat mechanism is Bolt 8 custom messages (bilateral, private). Nostr heartbeats are for advisors building transparent, publicly auditable reputation.

### Kind 38385: Advisor Reputation Summary

Aggregated reputation data for an advisor.

```json
{
  "kind": 38385,
  "pubkey": "<issuer_nostr_pubkey>",
  "created_at": 1739570400,
  "content": "<JSON-encoded DIDReputationCredential with domain hive:advisor>",
  "tags": [
    ["d", "<advisor_did>"],
    ["t", "hive-advisor-reputation"],
    ["uptime", "99.8"],
    ["completion-rate", "0.96"],
    ["nodes-served", "18"],
    ["tenure-days", "365"],
    ["avg-revenue-delta-pct", "+22.4"],
    ["renewal-rate", "0.85"],
    ["did", "<advisor_did>"],
    ["did-nostr-proof", "<attestation>"],
    ["alt", "Advisor reputation — 99.8% uptime — 96% completion — +22.4% avg revenue delta"]
  ]
}
```

---

## 3. Relay Strategy

### Relay Tiers

| Tier | Relays | Purpose | Required? |
|------|--------|---------|-----------|
| **Primary** | `wss://nos.lol`, `wss://relay.damus.io` | Broad reach, high availability | Yes — publish to ≥2 |
| **Search** | `wss://relay.nostr.band` | Tag-based search queries, indexing | Recommended |
| **Profile** | `wss://purplepag.es` | Profile events (kinds 38380, 38900) | Recommended |
| **Hive** | `wss://relay.hive.lightning` (future) | Dedicated hive marketplace relay | Optional (when available) |
| **Private** | Operator-configured | Fleet-internal coordination | Optional |

### Publishing Rules

- **Providers** MUST publish profiles and offers to ≥3 relays (≥2 primary + ≥1 search).
- **Clients** SHOULD query ≥2 relays and deduplicate by `d` tag.
- **RFPs** SHOULD be published to ≥2 primary relays. Anonymous RFPs MAY use fewer relays for reduced exposure.
- **Contract confirmations** SHOULD be published to ≥2 relays for permanence.
- **Heartbeats** MAY be published to 1 relay (search-optimized preferred) since they are ephemeral.

### Relay-Side Filtering

All hive marketplace events use tags designed for efficient relay-side filtering per NIP-01:

```json
// Find all advisor profiles
{"kinds": [38380]}

// Find all liquidity offers for leasing with ≥5M capacity
{"kinds": [38901], "#service": ["leasing"]}

// Find all advisor offers for fee optimization
{"kinds": [38381], "#capability": ["fee_optimization"]}

// Find all events from a specific DID
{"#did": ["did:cid:bagaaiera..."]}

// Find all hive marketplace events (both service types)
{"kinds": [38380, 38381, 38382, 38383, 38384, 38385, 38900, 38901, 38902, 38903, 38904, 38905]}
```

> **Note:** Relay support for tag-value range queries (e.g., `#capacity >= 5000000`) is not standardized in NIP-01. Clients MUST implement client-side filtering for numeric comparisons. The tags are still useful for relay-side existence filtering and exact-match queries.

### Dedicated Hive Relay (Future)

A hive-operated relay (`relay.hive.lightning`) is planned with:
- **Optimized indexes** for hive event kinds and tag patterns
- **Proof-of-work validation** at ingress (reject events below PoW threshold)
- **DID verification** at ingress (reject events with invalid `did-nostr-proof`)
- **Automatic garbage collection** of expired events
- **Rate limiting** per pubkey with DID-verified whitelist for higher limits
- **WebSocket compression** for bandwidth efficiency

The dedicated relay is **not required** — all hive marketplace functionality works on public relays. The dedicated relay provides performance, spam resistance, and curation benefits.

---

## 4. Event Lifecycle Management

### Creation

Events are created by `cl-hive-comms` and signed with the operator's Nostr key (auto-generated on first run or configured separately — see [Section 9](#9-did-nostr-binding)). If `cl-hive-archon` is installed, DID-Nostr binding is created automatically.

### Update

Replaceable events (profiles, offers, RFPs, heartbeats, reputation) are updated by publishing a new event with the same `d` tag and a newer `created_at` timestamp. Per NIP-01, relays replace the older version.

### Expiration

This spec uses **NIP-40 (Expiration Timestamp)** for event expiration:

```json
{
  "kind": 38381,
  "tags": [
    ["d", "<offer_id>"],
    ["expiration", "1742162400"],
    ["expires", "1742162400"]
  ]
}
```

- The `expiration` tag is the NIP-40 standard tag. Compliant relays automatically delete events past their expiration.
- The `expires` tag is the hive-convention tag (from Liquidity spec). Included for backward compatibility. Clients SHOULD prefer `expiration`.
- **Profiles** (kinds 38380, 38900): No expiration by default. Providers explicitly delete or replace them.
- **Offers** (kinds 38381, 38901): MUST include `expiration`. Recommended: 7–30 days.
- **RFPs** (kinds 38382, 38902): MUST include `expiration`. Recommended: 3–14 days.
- **Contract confirmations** (kinds 38383, 38903): No expiration (permanent record).
- **Heartbeats** (kinds 38384, 38904): SHOULD include `expiration`. Recommended: 2× heartbeat interval.
- **Reputation summaries** (kinds 38385, 38905): No expiration. Updated by replacement.

### Deletion

Event authors can delete events using NIP-09 (Event Deletion):

```json
{
  "kind": 5,
  "tags": [
    ["e", "<event_id_to_delete>"],
    ["a", "38381:<pubkey>:<d_tag>"]
  ]
}
```

Use cases:
- Withdrawing an offer after it's been filled
- Removing an RFP after selecting a provider
- Withdrawing a profile when ceasing operations

### Garbage Collection

Client software SHOULD:
- Discard events past their `expiration` timestamp
- Discard heartbeats older than 2× the expected interval
- Discard offers/RFPs where `bid-deadline` has passed and no contract confirmation references them
- Cache event data locally with a TTL matching the event's expected lifetime

---

## 5. Cross-NIP Compatibility

### NIP-99 (Classified Listings) — kind 30402

Hive marketplace events share tag conventions with NIP-99 for maximum interoperability:

| NIP-99 Tag | Hive Equivalent | Present in Hive Events? |
|-----------|----------------|------------------------|
| `title` | `alt` tag | Yes (human-readable summary) |
| `summary` | `content` (first paragraph) | Partial — add `summary` tag for NIP-99 clients |
| `price` | `["price", "<amount>", "<currency>", "<frequency>"]` | Yes (NIP-99 format) |
| `location` | `regions` tag | Yes |
| `status` | Derived from `expiration` | Implicit — "active" if not expired |
| `t` | `t` tags | Yes — `hive-advisor`, `hive-liquidity`, etc. |
| `image` | — | Optional (provider avatar or graph visualization) |

**Dual-publishing to NIP-99:** Providers MAY publish offers as both native kinds AND kind 30402. The kind 30402 version uses NIP-99's standard structure with hive-specific metadata in additional tags. See the [Liquidity spec § NIP Compatibility](./DID-HIVE-LIQUIDITY.md#nip-compatibility) for the full kind 30402 example.

**Advisor NIP-99 example:**

```json
{
  "kind": 30402,
  "content": "## ⚡ Lightning Node Management\n\nExperienced AI advisor specializing in fee optimization and channel rebalancing.\n\n- **Capabilities:** Fee optimization, rebalancing, channel expansion\n- **Track Record:** 18 nodes managed, +22.4% avg revenue improvement\n- **Uptime:** 99.8%\n- **DID-verified.** Contract via cl-hive-comms or direct message.",
  "tags": [
    ["d", "<unique_offer_id>"],
    ["title", "Lightning Node Advisor — Fee Optimization + Rebalancing"],
    ["summary", "AI-powered node management with DID-verified reputation and Cashu escrow"],
    ["price", "5000", "sat", "month"],
    ["t", "lightning"],
    ["t", "advisor"],
    ["t", "hive-advisor-offer"],
    ["location", "worldwide"],
    ["status", "active"],
    ["image", "<advisor_avatar>"],
    ["did", "<advisor_did>"],
    ["capability", "fee_optimization"],
    ["capability", "rebalancing"],
    ["alt", "Lightning node advisor — 5k sats/month"]
  ]
}
```

### NIP-15 (Nostr Marketplace) — kinds 30017/30018

NIP-15 defines a structured marketplace with stalls and products:

| NIP-15 Concept | Advisor Equivalent | Liquidity Equivalent |
|---------------|-------------------|---------------------|
| **Stall** (30017) | Advisor Profile (38380) | Provider Profile (38900) |
| **Product** (30018) | Service Offer (38381) | Capacity Offer (38901) |
| **Checkout** (NIP-04 DMs) | Contract negotiation | Contract negotiation |
| **Payment Request** | Bolt11/Bolt12/Cashu | Bolt11/Bolt12/Cashu |
| **Order Status** | Contract Confirmation (38383) | Contract Confirmation (38903) |

**Advisor NIP-15 stall example:**

```json
{
  "kind": 30017,
  "content": "{\"id\":\"<stall_id>\",\"name\":\"Hex Fleet Advisor\",\"description\":\"AI-powered Lightning node management — fee optimization, rebalancing, channel expansion. DID-verified, Cashu escrow.\",\"currency\":\"sat\",\"shipping\":[{\"id\":\"lightning\",\"name\":\"Lightning Network\",\"cost\":0,\"regions\":[\"worldwide\"]}]}",
  "tags": [["d", "<stall_id>"], ["t", "lightning"], ["t", "advisor"]]
}
```

**Advisor NIP-15 product example:**

```json
{
  "kind": 30018,
  "content": "{\"id\":\"<offer_id>\",\"stall_id\":\"<stall_id>\",\"name\":\"Fee Optimization + Rebalancing (Monthly)\",\"description\":\"Continuous fee optimization and channel rebalancing for up to 50 channels.\",\"currency\":\"sat\",\"price\":5000,\"quantity\":null,\"specs\":[[\"capabilities\",\"fee_optimization, rebalancing\"],[\"max_channels\",\"50\"],[\"sla_uptime\",\"99.5%\"],[\"trial\",\"7 days free\"],[\"did\",\"<advisor_did>\"]]}",
  "tags": [["d", "<offer_id>"], ["t", "lightning"], ["t", "advisor"], ["t", "hive-advisor-offer"]]
}
```

The NIP-15 checkout flow maps naturally: the "order" is a management request, the "payment request" is a Bolt11 invoice or Cashu escrow ticket, and the "order status" is the contract confirmation.

### NIP-04/NIP-44 (Encrypted DMs) — Negotiation Transport

Contract negotiation flows through encrypted DMs:

| NIP | Use Case | Recommendation |
|-----|----------|----------------|
| NIP-04 | Legacy DM encryption | Supported for compatibility; NOT recommended for new implementations |
| NIP-44 | Modern encrypted DMs | **Preferred.** Better cryptographic properties, forward secrecy |

**Negotiation flow:**
1. Client sees offer (kind 38381/38901) or publishes RFP (kind 38382/38902)
2. Counterparty sends NIP-44 encrypted DM with terms/quote
3. Negotiation continues via DMs (multiple rounds if needed)
4. Agreement reached → contract credential issued (off-Nostr, via hive protocol)
5. Optional: contract confirmation published (kind 38383/38903)

### NIP-40 (Expiration Timestamp)

Used as the **primary expiration mechanism**. See [Section 4](#4-event-lifecycle-management).

### NIP-78 (Application-Specific Data)

The original Marketplace spec used NIP-78 framing for advisor profiles. This spec transitions to dedicated custom kinds (38380–38385) for better discoverability and relay-side filtering. NIP-78 (kind 30078) MAY still be used for non-standard or experimental marketplace events during development.

---

## 6. Dual-Publishing Strategy

### Priority Levels

| Publication | Priority | Rationale |
|------------|----------|-----------|
| Native kinds (383xx/389xx) | **REQUIRED** | Primary protocol — hive-aware clients depend on these |
| NIP-99 (kind 30402) | **RECOMMENDED** | Broadest reach — most Nostr clients support classified listings |
| NIP-15 (kinds 30017/30018) | **OPTIONAL** | Structured marketplace — only needed if targeting Plebeian Market / NostrMarket users |

### Who Dual-Publishes?

Dual-publishing is the **provider's responsibility**, implemented in their client software:

```
┌──────────────────┐
│  Advisor/Provider │
│  publishes offer  │
└────────┬─────────┘
         │
    ┌────┴────┐
    │ Client  │
    │ Software│
    └────┬────┘
         │
    ┌────┴──────────────────────────┐
    │     Dual-Publish Engine       │
    │                               │
    │  1. Publish kind 38381/38901  │  ← REQUIRED
    │  2. Publish kind 30402        │  ← RECOMMENDED
    │  3. Publish kind 30017+30018  │  ← OPTIONAL
    │                               │
    │  Same content, different      │
    │  packaging for each NIP       │
    └───────────────────────────────┘
```

### Bridge Software (Future)

A standalone **Nostr marketplace bridge** can be operated by anyone to:
- Subscribe to native hive kinds (383xx/389xx)
- Re-publish as NIP-99 and/or NIP-15 events
- Handle format conversion and tag mapping
- Maintain attribution (original pubkey in `p` tags)

This enables dual-publishing without requiring every provider to implement it themselves.

---

## 7. Privacy

### Anonymous Browsing

Querying Nostr relays reveals **nothing** about the querying party. Clients browse provider profiles (38380/38900) and offers (38381/38901) without authentication or identity disclosure.

### Throwaway Keys for RFPs

Clients publishing RFPs (38382/38902) can use **throwaway Nostr keypairs** — generated per-RFP, used once, discarded. This prevents linking RFPs to a persistent identity.

```
┌───────────────────────────────────────────────────┐
│               ANONYMOUS RFP FLOW                   │
│                                                    │
│  1. Client generates ephemeral Nostr keypair       │
│  2. Publishes kind 38382/38902 with ephemeral key  │
│  3. Omits `did` tag                                │
│  4. Providers respond via NIP-44 DM to ephemeral   │
│     key (only client can decrypt)                  │
│  5. Client reviews quotes anonymously              │
│  6. Client contacts preferred provider with real    │
│     identity only when ready to contract           │
│  7. Ephemeral key discarded                        │
└───────────────────────────────────────────────────┘
```

### Sealed-Bid RFPs

For competitive bidding where providers should not see each other's quotes:

1. Client includes a `bid-pubkey` tag with a one-time NIP-44 encryption key
2. Providers encrypt their bids to this key
3. Bids appear as opaque encrypted blobs to other participants
4. Client decrypts all bids after the deadline
5. Same mechanism as [Marketplace spec sealed-bid auctions](./DID-HIVE-MARKETPLACE.md#sealed-bid-auctions), using Nostr as transport

### What Remains Private

| Data | Public? | When Disclosed? |
|------|---------|----------------|
| Provider profiles | Yes | Always (advertising) |
| Provider offers | Yes | Always (advertising) |
| Client identity during browsing | No | Never |
| Client identity in RFPs | Optional | Only if client includes `did` |
| Negotiation messages | No | Only between parties (NIP-44) |
| Contract terms | No | Only `contract-hash` is public |
| Heartbeat performance data | Optional | Only if provider opts into public heartbeats |
| Channel graph, balances | No | Never via Nostr |

---

## 8. Spam Resistance

### Multi-Layer Defense

```
┌─────────────────────────────────────────────────────────────┐
│                    SPAM RESISTANCE STACK                      │
│                                                              │
│  Layer 1: Proof of Work (NIP-13)                            │
│  ─────────────────────────────────────────                   │
│  All hive marketplace events SHOULD include PoW:            │
│  - Profiles/Offers/RFPs: ≥20 leading zero bits              │
│  - Contract confirmations: ≥16 bits (lower — already gated  │
│    by contract formation)                                    │
│  - Heartbeats: ≥12 bits (high frequency, lower barrier)     │
│                                                              │
│  Layer 2: DID Bond Verification                             │
│  ─────────────────────────────────────────                   │
│  Events with valid `did-nostr-proof` tags are prioritized:  │
│  - Relays MAY require DID binding for marketplace kinds     │
│  - Clients SHOULD display DID-verified badge prominently    │
│  - DID creation has inherent cost (Archon transaction)      │
│                                                              │
│  Layer 3: Relay-Side Rate Limiting                          │
│  ─────────────────────────────────────────                   │
│  Per-pubkey rate limits for marketplace events:             │
│  - Profiles: 1 update per hour                              │
│  - Offers: 10 per hour                                      │
│  - RFPs: 5 per hour                                         │
│  - Heartbeats: 1 per 10 minutes                            │
│  DID-verified pubkeys get 5× higher limits                  │
│                                                              │
│  Layer 4: Client-Side Filtering                             │
│  ─────────────────────────────────────────                   │
│  Clients score events by:                                    │
│  - Has valid DID binding? (+50 points)                      │
│  - Has PoW? (+1 point per bit)                              │
│  - Has reputation credentials? (+30 points)                 │
│  - Has contract confirmations? (+20 per contract)           │
│  - Account age? (+1 per month)                              │
│  Events below threshold are hidden (not deleted)            │
└─────────────────────────────────────────────────────────────┘
```

### NIP-13 Proof of Work

```json
{
  "kind": 38381,
  "id": "000000a3f4b2c...",
  "tags": [
    ["nonce", "4832751", "20"]
  ]
}
```

The `nonce` tag per NIP-13: `["nonce", "<random>", "<target_bits>"]`. The event `id` must have `<target_bits>` leading zero bits. This makes bulk spam computationally expensive while individual legitimate events cost fractions of a second.

---

## 9. DID-Nostr Binding

### How It Works

A DID-to-Nostr binding is established through an [Archon attestation credential](https://github.com/archetech/archon) that cryptographically links a DID to a Nostr pubkey. Both DID keys and Nostr keys use secp256k1 — the same curve — enabling compact cross-proofs.

```
┌─────────────────────────────────────────────────────────┐
│                  DID-NOSTR BINDING                        │
│                                                          │
│  1. Operator has DID:  did:cid:bagaaiera...              │
│  2. Operator has Nostr key: npub1qkjns...               │
│  3. Operator requests attestation from Archon:           │
│     "This DID controls this Nostr pubkey"                │
│  4. Archon issues verifiable credential:                 │
│     - Subject: DID                                       │
│     - Claim: "controls Nostr pubkey <hex>"               │
│     - Signed by: Archon network                          │
│  5. Credential ID stored in `did-nostr-proof` tag        │
│  6. Anyone can verify:                                   │
│     - Resolve credential via Archon                      │
│     - Check DID matches `did` tag                        │
│     - Check Nostr pubkey matches event `pubkey`          │
│     - Check credential signature is valid                │
└─────────────────────────────────────────────────────────┘
```

### Verification Flow (Client-Side)

```python
def verify_did_nostr_binding(event):
    did = get_tag(event, "did")
    proof_id = get_tag(event, "did-nostr-proof")
    
    # 1. Resolve the attestation credential
    credential = archon_resolve(proof_id)
    
    # 2. Verify credential signature
    if not verify_credential_signature(credential):
        return False
    
    # 3. Check DID matches
    if credential.subject != did:
        return False
    
    # 4. Check Nostr pubkey matches
    if credential.claim.nostr_pubkey != event.pubkey:
        return False
    
    return True
```

### Impersonation Prevention

Without DID-Nostr binding, anyone can publish a marketplace event claiming to be a high-reputation advisor. The binding prevents this:

| Attack | Defense |
|--------|---------|
| Publish profile with someone else's DID | `did-nostr-proof` verification fails — credential links DID to a different pubkey |
| Copy a provider's profile to a new key | `did-nostr-proof` points to credential for the original key |
| Create fake reputation summaries | Reputation credentials are signed by clients' DIDs — can't forge without their keys |

### Optional DID Binding

DID-Nostr binding is **strongly recommended** but not required. Events without `did-nostr-proof` are still valid Nostr events — they just won't be trusted by hive-aware clients. This allows:
- Experimentation without DID infrastructure
- Gradual adoption (publish first, bind DID later)
- Non-hive actors browsing and posting informally

---

## 10. Nostr-Native Client Compatibility

### Zero-Code Display

The dual-publishing strategy (Section 6) ensures that hive services appear in existing Nostr clients without any hive-specific code:

| Client Type | What They See | How | Effort |
|------------|--------------|-----|--------|
| **Any Nostr client** | `alt` tag text for native kinds | NIP-31 (alt tag) fallback | Zero |
| **NIP-99 clients** | Classified listings with title, price, description | Kind 30402 dual-publish | Zero |
| **NIP-15 clients** (Plebeian Market, NostrMarket) | Stalls + products with checkout | Kinds 30017/30018 dual-publish | Zero |
| **Hive-aware clients** (`cl-hive-comms`) | Full marketplace with escrow, heartbeats, reputation | Native kinds 383xx/389xx | Full integration |

### Tag Conventions for Generic Discovery

All hive marketplace events use standardized `t` tags for discoverability in Nostr search:

```
t:lightning          — All Lightning-related (broadest)
t:hive-advisor       — All advisor services
t:hive-liquidity     — All liquidity services
t:hive-advisor-offer — Advisor offers specifically
t:hive-liquidity-offer — Liquidity offers specifically
t:advisor-fee-optimization — Capability-specific
t:liquidity-leasing  — Service-type-specific
```

A Nostr user searching `#lightning` will discover hive services organically.

### Progressive Enhancement

```
┌──────────────────────────────────────────────────────────────┐
│               PROGRESSIVE CLIENT ENHANCEMENT                  │
│                                                               │
│  Level 0: Any Nostr client                                   │
│  └─ Sees: alt text, #lightning hashtag, basic profile info   │
│                                                               │
│  Level 1: NIP-99 aware client                                │
│  └─ Sees: Structured listing with title, price, description  │
│  └─ Can: Browse, filter by tag, view pricing                 │
│                                                               │
│  Level 2: NIP-15 aware client                                │
│  └─ Sees: Stall + product catalog with checkout flow         │
│  └─ Can: Initiate purchase via encrypted DMs                 │
│                                                               │
│  Level 3: Hive-aware client (cl-hive-comms)                  │
│  └─ Sees: Full marketplace with all metadata                 │
│  └─ Can: Escrow, heartbeat verification, reputation scoring  │
│  └─ Can: Automated discovery, contracting, and settlement    │
└──────────────────────────────────────────────────────────────┘
```

---

## 11. Client Integration

> **Key architecture note:** All Nostr publishing and subscribing is handled by the `cl-hive-comms` plugin, which is the entry point for the hive's CLN plugin architecture. Since `cl-hive-comms` already manages the Nostr connection (for DM transport), key management, and relay configuration, marketplace event publishing **shares the same Nostr connection** as the DM transport layer. This means zero additional Nostr configuration is needed — installing `cl-hive-comms` gives you both advisor communication and marketplace access.

### Publishing (Provider Side)

The `cl-hive-comms` plugin handles Nostr publishing for providers:

```
lightning-cli hive-client-marketplace-publish --type advisor

Under the hood:
  1. Read HiveServiceProfile credential from local store
  2. Use Nostr key from cl-hive-comms (auto-generated or configured)
     — same key used for DM transport
  3. Build kind 38380 event with profile data
  4. Build kind 30402 event (NIP-99 dual-publish, if enabled)
  5. Build kind 30017 + 30018 events (NIP-15 dual-publish, if enabled)
  6. Add PoW (NIP-13, target: 20 bits)
  7. Sign all events
  8. Publish to configured relays (≥3) — same relays used for DM transport
  9. Store event IDs locally for update/deletion tracking
```

### Discovery (Consumer Side)

```
lightning-cli hive-client-discover --type advisor --capability fee_optimization

Under the hood:
  1. Query Nostr relays for kind 38380 (profiles)
     Filter: #capability includes "fee_optimization"
     — uses same Nostr connection as DM transport
  2. Query for kind 38381 (offers) matching criteria
  3. If cl-hive-archon installed: query Archon network for HiveServiceProfile credentials
  4. If hive member (cl-hive installed): query hive gossip
  5. Merge results, deduplicate by DID or npub
  6. Verify DID-Nostr bindings (if cl-hive-archon installed)
  7. Fetch reputation summaries (kind 38385)
  8. Score and rank (reputation + PoW + DID verification + tenure)
  9. Present unified list to operator
```

### Subscription (Real-Time Updates)

Clients maintain persistent WebSocket subscriptions to Nostr relays for real-time marketplace updates:

```json
// Subscribe to new advisor offers
["REQ", "advisor-offers", {"kinds": [38381], "#capability": ["fee_optimization"]}]

// Subscribe to new liquidity offers above 5M sats
["REQ", "liquidity-offers", {"kinds": [38901], "#service": ["leasing"]}]

// Subscribe to heartbeats for active contracts
["REQ", "heartbeats", {"kinds": [38384, 38904], "#contract-hash": ["<hash>"]}]
```

### Configuration

```yaml
# cl-hive-comms Nostr configuration (shared between DM transport and marketplace)
nostr:
  enabled: true
  relays:
    - wss://nos.lol
    - wss://relay.damus.io
    - wss://relay.nostr.band
  publish:
    dual_nip99: true      # Recommended
    dual_nip15: false     # Optional
    pow_bits: 20          # NIP-13 proof of work
  discovery:
    min_relays: 2         # Query at least 2
    require_did: false    # Show non-DID events (lower rank)
    min_pow: 0            # Accept any PoW level
  key_source: "did"       # Derive from DID, or "file" for separate key
```

---

## 12. Implementation Roadmap

| Phase | Scope | Depends On | Timeline |
|-------|-------|-----------|----------|
| **Phase 1** | Native advisor kinds (38380–38385) — publish + discover | Marketplace spec Phase 7 | 1–2 weeks |
| **Phase 2** | NIP-99 dual-publishing for advisors + liquidity | Phase 1 | 1 week |
| **Phase 3** | Spam resistance (PoW, rate limiting, DID verification) | Phase 1 | 1 week |
| **Phase 4** | Event lifecycle (NIP-40 expiration, NIP-09 deletion, GC) | Phase 1 | 1 week |
| **Phase 5** | NIP-15 dual-publishing (stalls + products) | Phase 2 | 1–2 weeks |
| **Phase 6** | Anonymous RFPs and sealed-bid mechanism | Phase 1 | 1 week |
| **Phase 7** | Dedicated hive relay deployment | Phase 3 | 2–3 weeks |
| **Phase 8** | Nostr marketplace bridge (standalone) | Phase 5 | 2 weeks |

### Dependencies

- **Archon attestation credentials** — Required for DID-Nostr binding (already functional)
- **cl-hive-comms Nostr integration** — WebSocket client, event signing, relay management (shared with DM transport)
- **NIP-13 PoW library** — For spam resistance
- **NIP-44 encryption** — For negotiation DMs (preferred over NIP-04)

---

## 13. Open Questions

1. **Kind number stability.** Should we pursue formal NIP registration for kinds 38380–38389 and 38900–38909 before implementation, or implement first and formalize later?

2. **Relay economics.** How is the dedicated hive relay funded? Subscription from providers? PoW-only (no monetary cost)? Hive treasury?

3. **Cross-marketplace federation.** If other Lightning service marketplaces emerge on Nostr with different kind ranges, how do we interoperate? Should there be a meta-NIP for "Lightning service marketplace" events?

4. **Reputation portability.** Reputation summaries (kinds 38385/38905) published on Nostr are self-attested by the issuer. How do clients verify that the underlying `DIDReputationCredential` in the content is legitimate? Full Archon resolution on every display?

5. **Event size limits.** Some relays impose event size limits (e.g., 64KB). Full credentials in `content` may approach this. Should credentials be stored externally (IPFS/Archon) with only hashes in events?

6. **NIP-15 checkout mapping.** The NIP-15 checkout flow uses NIP-04 (deprecated encryption). Should we propose an update to NIP-15 for NIP-44 support, or handle it at the application layer?

7. **Heartbeat frequency on Nostr.** Public heartbeats (kinds 38384/38904) could create significant relay load if many providers publish frequently. What's the right balance between reputation transparency and relay resource consumption?

8. **Kind 38383 migration.** The kind number collision with the existing Marketplace spec's advisor profile usage. Should we use a different number for contract confirmations to avoid any transition issues?

---

## 14. Tag Convention Reference

Complete tag reference for all hive marketplace Nostr events:

### Universal Tags (All Hive Marketplace Events)

| Tag | Format | Required? | Purpose |
|-----|--------|-----------|---------|
| `t` | `["t", "<topic>"]` | Yes | Discoverability (`hive-advisor`, `hive-liquidity`, etc.) |
| `did` | `["did", "<provider_or_client_did>"]` | Recommended | Links to DID identity |
| `did-nostr-proof` | `["did-nostr-proof", "<credential_id>"]` | Recommended | DID-Nostr binding proof |
| `alt` | `["alt", "<human_readable>"]` | Yes | Fallback display (NIP-31) |
| `expiration` | `["expiration", "<unix_timestamp>"]` | Varies | NIP-40 expiration |
| `nonce` | `["nonce", "<random>", "<target_bits>"]` | Recommended | NIP-13 PoW |

### Profile Tags (Kinds 38380, 38900)

| Tag | Format | Purpose |
|-----|--------|---------|
| `d` | `["d", "<did>"]` | Replaceable event identifier |
| `name` | `["name", "<display_name>"]` | Human-readable provider name |
| `capabilities` / `capacity` | Service-specific | Queryable service attributes |
| `uptime` | `["uptime", "<percentage>"]` | Provider uptime claim |
| `p` | `["p", "<nostr_pubkey>"]` | Self-reference (for mention queries) |

### Offer Tags (Kinds 38381, 38901)

| Tag | Format | Purpose |
|-----|--------|---------|
| `d` | `["d", "<offer_id>"]` | Replaceable event identifier |
| `price` | `["price", "<amount>", "<currency>", "<frequency>"]` | NIP-99 compatible pricing |
| `payment-methods` | `["payment-methods", "cashu", "bolt11", ...]` | Accepted payment rails |
| `expires` | `["expires", "<unix_timestamp>"]` | Hive-convention expiration (legacy) |

### RFP Tags (Kinds 38382, 38902)

| Tag | Format | Purpose |
|-----|--------|---------|
| `d` | `["d", "<rfp_id>"]` | Replaceable event identifier |
| `bid-deadline` | `["bid-deadline", "<unix_timestamp>"]` | Deadline for provider quotes |
| `bid-pubkey` | `["bid-pubkey", "<one_time_pubkey>"]` | For sealed-bid encryption |

### Contract Tags (Kinds 38383, 38903)

| Tag | Format | Purpose |
|-----|--------|---------|
| `contract-hash` | `["contract-hash", "<sha256>"]` | Verifiable link to full contract |
| `e` | `["e", "<event_id>", "", "offer"]` | Reference to originating offer |
| `e` | `["e", "<event_id>", "", "rfp"]` | Reference to originating RFP |

### Heartbeat Tags (Kinds 38384, 38904)

| Tag | Format | Purpose |
|-----|--------|---------|
| `d` | `["d", "<engagement_or_lease_id>"]` | Replaceable per-contract |
| `sig` | `["sig", "<did_signature>"]` | DID-signed attestation over heartbeat data |

### Reputation Tags (Kinds 38385, 38905)

| Tag | Format | Purpose |
|-----|--------|---------|
| `d` | `["d", "<subject_did>"]` | Replaceable per-subject |
| `completion-rate` | `["completion-rate", "<decimal>"]` | Contract completion rate |

---

## References

### Companion Specs
- [DID Hive Marketplace Protocol](./DID-HIVE-MARKETPLACE.md)
- [DID Hive Liquidity Protocol](./DID-HIVE-LIQUIDITY.md)
- [DID Hive Client Protocol](./DID-HIVE-CLIENT.md)
- [DID Reputation Schema](./DID-REPUTATION-SCHEMA.md)
- [DID L402 Fleet Management](./DID-L402-FLEET-MANAGEMENT.md)
- [DID Cashu Task Escrow](./DID-CASHU-TASK-ESCROW.md)
- [DID Hive Settlements](./DID-HIVE-SETTLEMENTS.md)

### Nostr NIPs
- [NIP-01: Basic Protocol Flow](https://github.com/nostr-protocol/nips/blob/master/01.md)
- [NIP-04: Encrypted Direct Message (deprecated)](https://github.com/nostr-protocol/nips/blob/master/04.md)
- [NIP-09: Event Deletion](https://github.com/nostr-protocol/nips/blob/master/09.md)
- [NIP-13: Proof of Work](https://github.com/nostr-protocol/nips/blob/master/13.md)
- [NIP-15: Nostr Marketplace](https://github.com/nostr-protocol/nips/blob/master/15.md)
- [NIP-31: Dealing with Unknown Event Kinds](https://github.com/nostr-protocol/nips/blob/master/31.md)
- [NIP-40: Expiration Timestamp](https://github.com/nostr-protocol/nips/blob/master/40.md)
- [NIP-44: Versioned Encryption](https://github.com/nostr-protocol/nips/blob/master/44.md)
- [NIP-78: Application-Specific Data](https://github.com/nostr-protocol/nips/blob/master/78.md)
- [NIP-99: Classified Listings](https://github.com/nostr-protocol/nips/blob/master/99.md)

### Implementations
- [Plebeian Market](https://github.com/PlebeianTech/plebeian-market) — NIP-15 marketplace client
- [LNbits NostrMarket](https://github.com/lnbits/nostrmarket) — NIP-15 marketplace extension
- [Archon](https://github.com/archetech/archon) — DID infrastructure and attestation credentials

---

*This spec is the 8th document in the Lightning Hive protocol suite. It consolidates Nostr marketplace integration into a single authoritative reference. ⬡*
