# DID Hive Marketplace Protocol

**Status:** Proposal / Design Draft  
**Version:** 0.1.0  
**Author:** Hex (`did:cid:bagaaierajrr7k6izcrdfwqxpgtrobflsv5oibymfnthjazkkokaugszyh4ka`)  
**Date:** 2026-02-14  
**Feedback:** Open — file issues or comment in #singularity

---

## Abstract

This document defines the marketplace layer for the Lightning Hive protocol suite — how advisors advertise management services, how nodes discover and evaluate advisors, how they negotiate terms, and how contracts are formed. It bridges the existing protocol specifications ([Fleet Management](./DID-L402-FLEET-MANAGEMENT.md), [Reputation Schema](./DID-REPUTATION-SCHEMA.md), [Task Escrow](./DID-CASHU-TASK-ESCROW.md), [Settlements](./DID-HIVE-SETTLEMENTS.md)) into a functioning market for routing expertise.

The result is a decentralized, peer-to-peer marketplace where AI advisors and human experts compete to manage Lightning nodes — authenticated by DIDs, ranked by verifiable reputation, contracted through signed credentials, and paid through Cashu escrow. No central marketplace operator. No platform fees. Just cryptography, gossip, and economic incentives.

---

## Motivation

### The Gap Between Protocols and Markets

The existing protocol suite defines *how* management works (Fleet Management), *how* reputation is measured (Reputation Schema), *how* payment is conditional (Task Escrow), and *how* obligations settle (Settlements). What's missing is *how services are traded* — the connective tissue that turns protocol capabilities into economic activity.

Consider the state today: the Lightning Hive has one advisor (the prototype AI running on fleet operator infrastructure). This advisor has direct RPC access, implicit trust, and no competition. This is fine for development. It is not fine for a market.

### Why a Marketplace Matters

**Competition drives quality.** A single advisor has no pressure to improve. Ten advisors competing for the same nodes will optimize relentlessly. The best fee strategies, the fastest rebalancing, the most accurate channel expansion recommendations — these emerge from market pressure, not from a single agent iterating in isolation.

**Specialization enables expertise.** No single advisor excels at everything. Some will specialize in high-volume routing optimization. Others in channel expansion strategy. Others in emergency response and HTLC resolution. A marketplace lets node operators hire the right specialist for each domain.

**Network effects compound value.** Each new advisor brings capabilities. Each new node brings demand. Each successful contract produces reputation credentials that make the next contract easier to form. The marketplace becomes more valuable for every participant as it grows.

**Permissionless entry prevents capture.** Anyone can build an advisor and offer services. No gatekeeper decides who gets to compete. The barrier to entry is demonstrable competence, not platform approval.

### The Long-Term Vision

Build an AI advisor that excels at Lightning node management, then offer those services commercially via this protocol suite. The current advisor is the prototype. This spec defines how future advisors — ours and others' — will compete in an open market for routing expertise.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                        MARKETPLACE LAYER                              │
│                                                                       │
│   ┌─────────────┐    ┌─────────────┐    ┌──────────────────┐        │
│   │  Service     │    │  Discovery  │    │  Negotiation     │        │
│   │  Advertising │    │  & Ranking  │    │  & Contracting   │        │
│   │             │    │             │    │                  │        │
│   │ HiveService │    │ Gossip      │    │ Direct Hire      │        │
│   │ Profile     │    │ Queries     │    │ RFP / Bidding    │        │
│   │ Credentials │    │ Archon      │    │ SLA Negotiation  │        │
│   │             │    │ Resolution  │    │ Contract Creds   │        │
│   └──────┬──────┘    └──────┬──────┘    └────────┬─────────┘        │
│          │                  │                     │                   │
│          └──────────────────┴─────────────────────┘                   │
│                             │                                         │
│   ┌─────────────────────────▼──────────────────────────────────┐     │
│   │                   CONTRACT EXECUTION                        │     │
│   │                                                             │     │
│   │  Management Credential  +  Escrow Tickets  +  SLA Terms    │     │
│   │  (Fleet Management)        (Task Escrow)      (This Spec)  │     │
│   │                                                             │     │
│   │  Trial Periods  →  Full Contracts  →  Renewal / Termination│     │
│   │                                                             │     │
│   │  Multi-Advisor Coordination  ←→  Reputation Feedback Loop  │     │
│   └─────────────────────────────────────────────────────────────┘     │
│                                                                       │
└──────────────────────────────────────────────────────────────────────┘

         ▲                    ▲                    ▲
         │                    │                    │
    ┌────┴────┐         ┌────┴─────┐        ┌────┴──────┐
    │  Fleet  │         │Reputation│        │  Task     │
    │  Mgmt   │         │  Schema  │        │  Escrow   │
    │  Spec   │         │  Spec    │        │  Spec     │
    └─────────┘         └──────────┘        └───────────┘
```

---

## 1. Service Advertising

### HiveServiceProfile Credential

An advisor advertises their services by publishing a `HiveServiceProfile` — a signed Verifiable Credential that describes capabilities, pricing, availability, and reputation. This credential is the advisor's storefront.

```json
{
  "@context": [
    "https://www.w3.org/ns/credentials/v2",
    "https://hive.lightning/marketplace/v1"
  ],
  "type": ["VerifiableCredential", "HiveServiceProfile"],
  "issuer": "did:cid:<advisor_did>",
  "validFrom": "2026-02-14T00:00:00Z",
  "validUntil": "2026-05-14T00:00:00Z",
  "credentialSubject": {
    "id": "did:cid:<advisor_did>",
    "displayName": "Hex Fleet Advisor",
    "version": "1.0.0",
    "capabilities": {
      "primary": ["fee-optimization", "rebalancing", "config-tuning"],
      "secondary": ["expansion-planning", "emergency-response"],
      "experimental": ["htlc-resolution", "splice-management"]
    },
    "supportedSchemas": [
      "hive:fee-policy/v1",
      "hive:fee-policy/v2",
      "hive:rebalance/v1",
      "hive:config/v1",
      "hive:monitor/v1",
      "hive:expansion/v1",
      "hive:emergency/v1"
    ],
    "pricing": {
      "models": [
        {
          "type": "per_action",
          "baseFeeRange": { "min": 5, "max": 100, "currency": "sats" },
          "dangerScoreMultiplier": true
        },
        {
          "type": "subscription",
          "monthlyRate": 5000,
          "currency": "sats",
          "includedActions": 500,
          "overageRate": 15
        },
        {
          "type": "performance",
          "baseMonthlySats": 2000,
          "performanceSharePct": 10,
          "measurementWindowDays": 30
        }
      ],
      "acceptedPayment": ["cashu", "l402"],
      "acceptableMints": ["https://mint.hive.lightning", "https://mint.minibits.cash"],
      "escrowRequired": true
    },
    "availability": {
      "maxNodes": 50,
      "currentLoad": 12,
      "acceptingNewClients": true,
      "responseTimeSla": "5m",
      "uptimeTarget": 99.5
    },
    "infrastructure": {
      "redundancy": "multi-region",
      "backupAdvisor": "did:cid:<backup_did>",
      "monitoringFrequency": "60s"
    },
    "specializations": ["high-volume-routing", "fee-optimization"],
    "trialTerms": {
      "available": true,
      "durationDays": 14,
      "scope": ["monitor", "fee-policy"],
      "flatFeeSats": 1000
    },
    "reputationRefs": [
      "did:cid:<reputation_credential_1>",
      "did:cid:<reputation_credential_2>",
      "did:cid:<reputation_credential_3>"
    ]
  }
}
```

The profile is **self-issued** — the advisor signs it with their own DID. This means the profile's claims are the advisor's assertions, not independently verified facts. Verification comes from the attached reputation credentials (which ARE issued by third parties — the node operators who have been managed).

### Specialization Taxonomy

Advisors declare specializations from a defined taxonomy. Specializations are not exclusive — an advisor can claim multiple — but they guide discovery ranking.

| Specialization | Description | Key Schemas |
|---------------|-------------|-------------|
| `fee-optimization` | Channel fee tuning, revenue maximization | `hive:fee-policy/*` |
| `high-volume-routing` | Optimizing for throughput on high-traffic paths | `hive:fee-policy/*`, `hive:config/*` |
| `rebalancing` | Liquidity management, circular rebalances, submarine swaps | `hive:rebalance/*` |
| `expansion-planning` | Channel opens, peer selection, topology optimization | `hive:expansion/*` |
| `emergency-response` | HTLC resolution, force closes, compromise mitigation | `hive:emergency/*`, `hive:htlc/*` |
| `splice-management` | In-place channel resizing, multi-party splices | `hive:splice/*` |
| `full-stack` | Comprehensive node management across all domains | All schemas |
| `monitoring-only` | Read-only monitoring, alerting, reporting | `hive:monitor/*` |

New specializations can be proposed via hive governance, published as profile definitions on the Archon network.

### Profile Refresh & Update

Advisors update their profiles as reputation grows, capacity changes, or pricing adjusts:

1. **Periodic refresh:** Advisors re-publish profiles at least every 30 days. Profiles older than 90 days are considered stale and deprioritized in discovery.
2. **Event-driven update:** After receiving a new reputation credential, gaining/losing a client, or changing pricing, the advisor publishes an updated profile.
3. **Version tracking:** Each profile includes a `version` field (semver). Discovery nodes track profile versions and only propagate updates (dedup by DID + version).

### Advertising via Hive Gossip

Profiles propagate through the hive gossip protocol as a new message type:

| Message Type | Content | Propagation | TTL |
|-------------|---------|-------------|-----|
| `service_profile_announce` | Full `HiveServiceProfile` credential | Broadcast (full hive) | 30 days |
| `service_profile_update` | Updated profile (replaces previous by DID) | Broadcast (full hive) | 30 days |
| `service_profile_withdraw` | Profile withdrawal notice | Broadcast (full hive) | 7 days |

Propagation rules:
- Nodes relay profiles for advisors they consider valid (signature check + basic sanity)
- Each node maintains a local profile cache, deduped by advisor DID
- Profiles are re-gossiped on request during discovery queries (pull model)
- Nodes **do not** relay profiles from DIDs with reputation below a configurable threshold (default: 0, allowing new entrants; adjustable per-node)

### Advertising via Nostr (Optional)

For broader discovery beyond hive members, advisors can publish profiles to Nostr:

```json
{
  "kind": 38383,
  "content": "<JSON-encoded HiveServiceProfile>",
  "tags": [
    ["d", "<advisor_did>"],
    ["t", "hive-advisor"],
    ["t", "fee-optimization"],
    ["t", "rebalancing"],
    ["p", "<advisor_nostr_pubkey>"],
    ["alt", "Lightning Hive advisor service profile"]
  ]
}
```

Using NIP-78 (application-specific data) or a custom kind. The Nostr event contains the same profile credential, enabling nodes outside the hive gossip network to discover advisors. The DID-to-Nostr link is verified via the advisor's [Nostr attestation credential](https://github.com/archetech/archon) binding their DID to their Nostr pubkey.

---

## 2. Discovery

### Query Mechanism

Nodes discover advisors through two complementary models:

#### Pull Model: Gossip Queries

A node broadcasts a discovery query to the hive gossip network:

```json
{
  "type": "service_discovery_query",
  "query_id": "<unique_nonce>",
  "requester": "<optional_did_or_anonymous>",
  "filters": {
    "capabilities": ["fee-optimization", "rebalancing"],
    "minReputationScore": 60,
    "maxMonthlySats": 10000,
    "supportedSchemas": ["hive:fee-policy/v1"],
    "acceptingNewClients": true,
    "specializations": ["high-volume-routing"]
  },
  "maxResults": 20,
  "timestamp": "2026-02-14T12:00:00Z"
}
```

Nodes that cache matching profiles respond with profile references:

```json
{
  "type": "service_discovery_response",
  "query_id": "<echo_query_nonce>",
  "profiles": [
    {
      "advisorDid": "did:cid:<advisor_1>",
      "profileVersion": "1.2.0",
      "matchScore": 0.92,
      "cachedAt": "2026-02-13T08:00:00Z"
    },
    {
      "advisorDid": "did:cid:<advisor_2>",
      "profileVersion": "1.0.0",
      "matchScore": 0.78,
      "cachedAt": "2026-02-14T01:00:00Z"
    }
  ],
  "responder": "did:cid:<responding_node>"
}
```

The querying node collects responses, deduplicates by advisor DID, and fetches full profiles for the top candidates.

#### Push Model: Profile Subscriptions

Nodes subscribe to profile announcements matching their interests:

```json
{
  "type": "service_profile_subscription",
  "subscriber": "did:cid:<node_did>",
  "filters": {
    "capabilities": ["fee-optimization"],
    "minReputationScore": 70
  }
}
```

When new profiles matching the subscription arrive via gossip, the node is notified immediately. This enables passive advisor discovery — nodes learn about new advisors without actively querying.

#### Archon Network Discovery

For cross-hive discovery, nodes query the Archon network directly:

```bash
# Search for HiveServiceProfile credentials
npx @didcid/keymaster search-credentials \
  --type HiveServiceProfile \
  --filter 'credentialSubject.capabilities.primary contains "fee-optimization"' \
  --filter 'credentialSubject.availability.acceptingNewClients == true'
```

Archon discovery enables advisors serving multiple hives to be found by nodes in any hive — true cross-hive marketplace.

### Filtering & Ranking Algorithm

Discovery results are ranked by a weighted scoring algorithm:

```
match_score(advisor, query) =
  w_rep × reputation_score(advisor) +
  w_cap × capability_match(advisor, query.capabilities) +
  w_spec × specialization_match(advisor, query.specializations) +
  w_price × price_fit(advisor.pricing, query.maxMonthlySats) +
  w_avail × availability_score(advisor.availability) +
  w_fresh × freshness(advisor.profile.validFrom)
```

Default weights:

| Factor | Weight | Rationale |
|--------|--------|-----------|
| `w_rep` (Reputation) | 0.35 | Track record is the strongest signal |
| `w_cap` (Capability match) | 0.25 | Must support the needed schemas |
| `w_spec` (Specialization) | 0.15 | Specialist > generalist for specific needs |
| `w_price` (Price fit) | 0.10 | Within budget, but cheapest isn't always best |
| `w_avail` (Availability) | 0.10 | Low-load advisors can be more responsive |
| `w_fresh` (Freshness) | 0.05 | Recent profiles reflect current capabilities |

Nodes can customize weights based on their priorities. A cost-sensitive operator might weight `w_price` at 0.30; a quality-focused operator might weight `w_rep` at 0.50.

### Privacy in Discovery

Nodes can discover advisors without revealing their identity:

- **Anonymous queries:** The `requester` field in discovery queries is optional. Anonymous queries receive the same results but cannot receive push notifications.
- **Proxy queries:** A node can ask a trusted hive peer to query on its behalf, hiding the querying node's identity from the gossip network.
- **Nostr discovery:** Querying Nostr relays reveals nothing about the querying node's Lightning identity.
- **Archon queries:** DID resolution queries to the Archon network are read-only and do not expose the querier's identity.

---

## 3. Negotiation & RFP Flow

### Direct Hire

The simplest path: a node selects an advisor from discovery results and sends a contract proposal.

```
Node                                  Advisor
  │                                      │
  │  1. Discovery (query + rank)         │
  │  ──────────(gossip)──────────────►   │
  │                                      │
  │  2. Select top advisor               │
  │                                      │
  │  3. Contract Proposal                │
  │     (encrypted to advisor DID)       │
  │  ──────────(Bolt 8/Dmail)────────►   │
  │                                      │
  │     4. Review proposal               │
  │     5. Accept / Counter / Reject     │
  │                                      │
  │  6. Response                         │
  │  ◄──────────(Bolt 8/Dmail)────────   │
  │                                      │
  │  [If accepted or counter-accepted:]  │
  │                                      │
  │  7. Contract Credential issuance     │
  │  ◄─────────────────────────────────► │
  │                                      │
```

#### Contract Proposal

```json
{
  "type": "HiveContractProposal",
  "proposalId": "<unique_id>",
  "from": "did:cid:<node_operator_did>",
  "to": "did:cid:<advisor_did>",
  "terms": {
    "scope": {
      "capabilities": ["fee-optimization", "rebalancing"],
      "schemas": ["hive:fee-policy/v1", "hive:rebalance/v1"],
      "permissionTier": "standard",
      "constraints": {
        "max_fee_change_pct": 50,
        "max_rebalance_sats": 1000000,
        "max_daily_actions": 100
      }
    },
    "compensation": {
      "model": "performance",
      "baseMonthlySats": 3000,
      "performanceSharePct": 10,
      "escrowMint": "https://mint.hive.lightning"
    },
    "sla": {
      "responseTimeMinutes": 10,
      "uptimePct": 99.0,
      "reportingFrequency": "weekly",
      "performanceTargets": {
        "minRevenueDeltaPct": 0,
        "maxStagnantChannelsPct": 20
      }
    },
    "duration": {
      "trialDays": 14,
      "fullTermDays": 90,
      "noticePeriodDays": 7,
      "autoRenew": true
    },
    "nodeInfo": {
      "nodeCount": 2,
      "totalCapacitySats": 134000000,
      "channelCount": 45
    }
  },
  "expiresAt": "2026-02-21T00:00:00Z",
  "signature": "<node_operator_sig>"
}
```

### RFP (Request for Proposal)

For competitive scenarios, a node publishes requirements and invites bids:

```
Node                     Hive Gossip              Advisors (A, B, C)
  │                          │                          │
  │  1. Publish RFP          │                          │
  │  ────────────────────►   │                          │
  │                          │  2. Propagate            │
  │                          │  ────────────────────►   │
  │                          │                          │
  │                          │  3. Advisors evaluate    │
  │                          │     and prepare bids     │
  │                          │                          │
  │  4. Receive bids         │                          │
  │  ◄──────(encrypted)──────────────────────────────   │
  │                          │                          │
  │  5. Evaluate bids        │                          │
  │  6. Select winner        │                          │
  │                          │                          │
  │  7. Award notification   │                          │
  │  ──────(encrypted)───────────────────────────────►  │
  │                          │                          │
```

#### RFP Structure

```json
{
  "type": "HiveRFP",
  "rfpId": "<unique_id>",
  "issuer": "<did_or_anonymous>",
  "requirements": {
    "capabilities": ["fee-optimization", "rebalancing", "expansion-planning"],
    "minSchemaVersions": { "hive:fee-policy": "v1", "hive:rebalance": "v1" },
    "minReputationScore": 70,
    "preferredSpecializations": ["high-volume-routing"]
  },
  "nodeProfile": {
    "nodeCount": 2,
    "totalCapacitySats": 134000000,
    "channelCount": 45,
    "currentMonthlyRevenueSats": 50000,
    "currentChallenges": ["stagnant channels", "suboptimal fee structure"]
  },
  "desiredTerms": {
    "maxMonthlyCostSats": 10000,
    "preferredCompensationModel": "performance",
    "trialRequired": true,
    "minContractDays": 30
  },
  "bidDeadline": "2026-02-21T00:00:00Z",
  "awardDeadline": "2026-02-28T00:00:00Z",
  "bidFormat": "sealed",
  "signature": "<issuer_sig_or_empty_if_anonymous>"
}
```

#### Bid Structure

```json
{
  "type": "HiveBid",
  "bidId": "<unique_id>",
  "rfpId": "<rfp_id>",
  "advisor": "did:cid:<advisor_did>",
  "proposal": {
    "pricing": {
      "model": "performance",
      "baseMonthlySats": 2500,
      "performanceSharePct": 8,
      "trialFlatFeeSats": 500
    },
    "proposedSla": {
      "responseTimeMinutes": 5,
      "uptimePct": 99.5,
      "reportingFrequency": "daily",
      "performanceGuarantee": "5% revenue improvement or trial fee refunded"
    },
    "trialTerms": {
      "durationDays": 14,
      "scope": ["monitor", "fee-policy"],
      "evaluation": "automated metrics + weekly report"
    },
    "references": [
      {
        "credentialRef": "did:cid:<reputation_credential>",
        "operatorDid": "did:cid:<reference_operator>",
        "summary": "Managed 3 nodes for 60 days, +180% revenue"
      }
    ],
    "differentiators": "Specialized in high-volume routing with proprietary path analysis. 12 nodes under management, all with >100% revenue improvement."
  },
  "expiresAt": "2026-02-21T00:00:00Z",
  "signature": "<advisor_sig>"
}
```

### Sealed-Bid Auctions

For competitive scenarios where bid privacy matters:

1. **Commit phase:** Advisors submit bids encrypted to the RFP issuer's DID pubkey. Each bid includes a commitment hash: `SHA256(bid_content || nonce)` where `nonce` is a 32-byte random value chosen by the advisor.
2. **Seal deadline:** After the bid deadline, the issuer publishes the commitment hashes of all received bids (proving no post-deadline modifications were accepted).
3. **Evaluation:** The issuer decrypts and evaluates all bids simultaneously.
4. **Award & reveal:** Winner is announced. The issuer publishes the list of all commitment hashes received. Losing bidders verify their commitment hash is included by checking `SHA256(their_bid || their_nonce)` against the published list. If a bidder's hash is missing, they have cryptographic proof the issuer excluded their bid.
5. **Optional dispute reveal:** Any losing bidder can publicly reveal their `nonce` and bid content, allowing anyone to verify the commitment hash was correctly computed. This enables third-party auditing of the RFP process.

This prevents: (a) the RFP issuer from sharing early bids with favored advisors (bids are encrypted), (b) post-deadline bid insertion (commitment hashes are published), and (c) bid suppression (bidders can prove exclusion).

### Counter-Offers & Negotiation Rounds

If neither party accepts the initial terms outright:

```json
{
  "type": "HiveCounterOffer",
  "proposalId": "<original_proposal_or_bid_id>",
  "round": 2,
  "from": "did:cid:<advisor_did>",
  "to": "did:cid:<node_operator_did>",
  "modifications": {
    "compensation.baseMonthlySats": 3500,
    "compensation.performanceSharePct": 12,
    "sla.responseTimeMinutes": 15,
    "duration.trialDays": 7
  },
  "justification": "Higher base fee reflects the node's channel count (45 channels requires more frequent monitoring). Shorter trial is sufficient given my existing references.",
  "expiresAt": "2026-02-18T00:00:00Z",
  "signature": "<advisor_sig>"
}
```

Negotiation rules:
- Maximum 5 rounds before the negotiation is considered failed
- Each counter-offer has an explicit expiration (default: 72 hours)
- Either party can abort at any round with no reputation consequence
- All messages are signed by the sender's DID and optionally encrypted to the recipient's DID

### Timeout Handling

| Event | Timeout | Consequence |
|-------|---------|-------------|
| RFP bid deadline | Configurable (7 days default) | No more bids accepted; evaluation begins |
| Bid expiration | Per-bid (set by advisor) | Bid automatically withdrawn |
| Proposal expiration | Per-proposal | Proposal void; advisor may re-engage later |
| Counter-offer expiration | Per-round (72h default) | Round expires; previous terms stand or negotiation fails |
| Award deadline | Configurable (14 days default) | If no award made, RFP is considered cancelled |

---

## 4. Contracting

### Contract Credential

A contract is formalized as a signed Verifiable Credential binding both parties to agreed terms. The contract credential bundles together references to the Management Credential (from [Fleet Management](./DID-L402-FLEET-MANAGEMENT.md)) and Escrow Tickets (from [Task Escrow](./DID-CASHU-TASK-ESCROW.md)).

```json
{
  "@context": [
    "https://www.w3.org/ns/credentials/v2",
    "https://hive.lightning/marketplace/v1"
  ],
  "type": ["VerifiableCredential", "HiveManagementContract"],
  "issuer": "did:cid:<node_operator_did>",
  "credentialSubject": {
    "id": "did:cid:<advisor_did>",
    "contractId": "<unique_contract_id>",
    "managementCredentialRef": "did:cid:<management_credential>",
    "sla": {
      "responseTimeMinutes": 10,
      "uptimePct": 99.0,
      "reportingFrequency": "weekly",
      "performanceTargets": {
        "minRevenueDeltaPct": 0,
        "maxStagnantChannelsPct": 20
      },
      "penaltyForBreach": {
        "responseTimeViolation": "5% monthly fee credit per incident",
        "uptimeViolation": "prorated fee reduction",
        "performanceFailure": "no performance bonus (base fee still owed)"
      }
    },
    "compensation": {
      "model": "performance",
      "baseMonthlySats": 3000,
      "performanceSharePct": 10,
      "escrowMint": "https://mint.hive.lightning",
      "settlementType": "Type 9 (Advisor Fee Settlement)"
    },
    "duration": {
      "trialStart": "2026-02-14T00:00:00Z",
      "trialEnd": "2026-02-28T00:00:00Z",
      "fullTermStart": "2026-02-28T00:00:00Z",
      "fullTermEnd": "2026-05-28T00:00:00Z",
      "noticePeriodDays": 7,
      "autoRenew": true
    },
    "scope": {
      "nodeIds": ["03abc...", "03def..."],
      "capabilities": ["fee-optimization", "rebalancing"],
      "permissionTier": "standard"
    }
  },
  "validFrom": "2026-02-14T00:00:00Z",
  "validUntil": "2026-05-28T00:00:00Z",
  "proof": [
    {
      "type": "EcdsaSecp256k1Signature2019",
      "created": "2026-02-14T00:00:00Z",
      "verificationMethod": "did:cid:<node_operator_did>#key-1",
      "proofPurpose": "assertionMethod",
      "proofValue": "<operator_signature>"
    },
    {
      "type": "EcdsaSecp256k1Signature2019",
      "created": "2026-02-14T00:01:00Z",
      "verificationMethod": "did:cid:<advisor_did>#key-1",
      "proofPurpose": "assertionMethod",
      "proofValue": "<advisor_signature>"
    }
  ]
}
```

Both parties sign the contract — the operator issues the credential and the advisor adds a second proof entry to the `proof` array, creating a mutual binding per VC 2.0's support for multiple proofs.

### SLA Definition

Service Level Agreements define measurable commitments:

| SLA Metric | Measurement | Default | Penalty |
|-----------|-------------|---------|---------|
| Response time | Time from alert to first action | 10 min | Fee credit per incident |
| Uptime | Advisor availability for command execution | 99% | Prorated fee reduction |
| Reporting frequency | Periodic performance reports delivered | Weekly | Contract breach warning |
| Revenue improvement | Routing revenue delta vs. baseline | 0% (floor) | No performance bonus |
| Stagnant channels | Percentage of channels with zero forwards | <20% | Review trigger |
| Action throughput | Minimum actions per settlement period | Varies | Contract review |

SLA metrics are measured by the node and reported in the periodic reputation credential. Disputes over SLA measurement follow the [Dispute Resolution](./DID-HIVE-SETTLEMENTS.md#dispute-resolution) process from the Settlements spec.

### Activation Flow

```
1. Contract credential issued (both parties sign)
         │
         ▼
2. Management credential created (per Fleet Management spec)
   - Permission tier, constraints, duration from contract
         │
         ▼
3. Initial escrow tickets minted (per Task Escrow spec)
   - Trial period flat-fee ticket
   - Or first month's subscription ticket
         │
         ▼
4. Trial period begins
   - Reduced scope (monitor + fee-policy only)
   - Flat-fee compensation
   - Automated metric collection
         │
         ▼
5. Trial evaluation (automated + manual review)
         │
    ┌────┴────┐
    │         │
  Pass      Fail
    │         │
    ▼         ▼
6a. Full     6b. Graceful
    activation    exit
    │              │
    ▼              ▼
7a. Full     7b. Partial
    escrow        payment
    tickets       + no negative
    minted        reputation
```

### Contract Registry (Optional)

For transparency, contracts can be announced to the hive:

```json
{
  "type": "contract_announcement",
  "contractId": "<hash_of_contract_credential>",
  "operator": "did:cid:<node_operator_did>",
  "advisor": "did:cid:<advisor_did>",
  "scope": ["fee-optimization", "rebalancing"],
  "startDate": "2026-02-14T00:00:00Z",
  "status": "active"
}
```

Only the existence and scope are public — specific terms (pricing, SLA details, node configurations) remain private between the parties. This enables the marketplace to track advisor utilization and helps nodes assess advisor load claims.

---

## 5. Trial Periods

### Rationale

First-time relationships carry inherent risk for both parties. The node doesn't know if the advisor is competent. The advisor doesn't know if the node has reasonable expectations. Trial periods reduce this risk by limiting scope, duration, and financial commitment.

Trial periods also solve the [baseline integrity challenge](./DID-CASHU-TASK-ESCROW.md#performance-ticket) from the Task Escrow spec: the trial establishes performance baselines collaboratively before full performance-based compensation begins.

### Trial Terms

| Parameter | Default | Range | Rationale |
|-----------|---------|-------|-----------|
| Duration | 14 days | 7–30 days | Enough to demonstrate competence without over-commitment |
| Scope | `monitor` + `fee-policy` | Any subset of contracted capabilities | Low-risk operations prove competence before granting higher-tier access |
| Permission tier | `standard` (constrained) | `monitor` to `standard` | No `advanced` or `admin` during trial |
| Pricing | Flat fee | 500–5000 sats | Removes baseline manipulation incentives |
| Evaluation | Automated metrics | — | Measurable, objective criteria agreed upfront |

### Trial Evaluation Criteria

Evaluation criteria are defined in the contract proposal and measured automatically by the node:

```json
{
  "trialEvaluation": {
    "criteria": [
      {
        "metric": "actions_taken",
        "threshold": 10,
        "operator": ">=",
        "description": "At least 10 management actions executed"
      },
      {
        "metric": "uptime_pct",
        "threshold": 95.0,
        "operator": ">=",
        "description": "Advisor available >95% of trial period"
      },
      {
        "metric": "revenue_delta_pct",
        "threshold": -5.0,
        "operator": ">=",
        "description": "Revenue did not decrease by more than 5%"
      },
      {
        "metric": "response_time_p95_minutes",
        "threshold": 30,
        "operator": "<=",
        "description": "95th percentile response time under 30 minutes"
      }
    ],
    "passingRequirement": "all",
    "autoUpgrade": true
  }
}
```

### Trial → Full Contract Transition

| Scenario | Action |
|----------|--------|
| All criteria met + `autoUpgrade: true` | Automatic transition to full contract; management credential scope expanded |
| All criteria met + `autoUpgrade: false` | Notification to operator; explicit renewal required |
| Some criteria met | Operator reviews; can extend trial, renegotiate terms, or exit |
| No criteria met / major failure | Graceful exit; trial fee paid (work was done); no negative reputation for reasonable failure |
| Advisor withdraws during trial | Partial fee proportional to days served; neutral reputation |

### Anti-Trial-Cycling Protection

To prevent operators from cycling through advisors on perpetual trial periods to avoid full-rate contracts:

| Protection | Mechanism |
|-----------|-----------|
| **Concurrent trial limit** | A node can have at most 2 active trial contracts simultaneously |
| **Sequential cooldown** | After a trial ends (pass or fail), the operator must wait 14 days before starting a new trial with a *different* advisor for the same capability scope |
| **Trial history transparency** | Trial count is visible in the operator's `hive:client` reputation profile; advisors can check how many trials an operator has run |
| **Graduated trial pricing** | An operator's 1st trial in a capability scope uses the advisor's standard trial fee; 2nd trial within 90 days costs 2×; 3rd+ costs 3× |
| **Advisor opt-out** | Advisors can refuse trials from operators with high trial churn (e.g., >3 trials in 90 days with no full contract) |

These protections are enforced by advisors (who check the operator's trial history via reputation credentials) rather than by protocol — an operator can always find a new advisor willing to offer a trial, but the reputation signal makes excessive trial cycling visible and costly.

### Trial Failure Handling

Trial failures are not penalized in the reputation system **unless** the failure involves bad faith (e.g., advisor takes no actions despite being paid, or advisor causes measurable damage). Reasonable trial failures — the advisor tried but the optimization didn't work for this particular node — result in a `neutral` outcome credential.

This is critical for marketplace health: advisors won't take trial contracts if every failed trial damages their reputation. The bar for `revoke` during a trial is bad faith, not underperformance.

---

## 6. Multi-Advisor Coordination

### Scope Partitioning

A node can hire multiple advisors with non-overlapping management domains:

```
┌─────────────────────────────────────────────────┐
│                    NODE                          │
│                                                  │
│  ┌──────────────────┐  ┌─────────────────────┐  │
│  │  Advisor A       │  │  Advisor B          │  │
│  │  (Fee Expert)    │  │  (Rebalance Expert) │  │
│  │                  │  │                     │  │
│  │  Scope:          │  │  Scope:             │  │
│  │  • fee-policy    │  │  • rebalance        │  │
│  │  • config (fees) │  │  • config (rebal)   │  │
│  │                  │  │                     │  │
│  │  Schemas:        │  │  Schemas:           │  │
│  │  hive:fee-*      │  │  hive:rebalance-*   │  │
│  │  hive:config/    │  │  hive:config/       │  │
│  │    fee params    │  │    rebal params     │  │
│  └──────────────────┘  └─────────────────────┘  │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │  Advisor C (Monitor — read-only)         │   │
│  │  Scope: hive:monitor/* (all metrics)     │   │
│  │  Provides: dashboards, alerts, reports   │   │
│  └──────────────────────────────────────────┘   │
│                                                  │
└─────────────────────────────────────────────────┘
```

Each advisor's Management Credential (from the Fleet Management spec) explicitly limits their domain via `allowed_schemas`:

```json
{
  "permissions": {
    "monitor": true,
    "fee_policy": true,
    "rebalance": false,
    "config_tune": true,
    "channel_open": false,
    "channel_close": false
  },
  "constraints": {
    "allowed_schemas": ["hive:fee-policy/*", "hive:config/fee_*"]
  }
}
```

The node's policy engine enforces scope isolation — a command from Advisor A targeting a `hive:rebalance/*` schema is rejected regardless of what the credential claims.

### Conflict Resolution

When two advisors issue actions that interact:

| Conflict Type | Resolution | Example |
|--------------|------------|---------|
| **Scope overlap** | Rejected by credential enforcement | Advisor A (fees) tries to rebalance → blocked |
| **Indirect conflict** | Priority by specialization | Advisor A sets high fees to attract inbound; Advisor B rebalances outbound — B's action may undermine A's strategy |
| **Resource conflict** | First-mover + cooldown | Both advisors want to use the same channel's liquidity simultaneously |
| **True conflict** | Escalation to operator | Fundamentally incompatible strategies detected |

#### Indirect Conflict Detection

The node maintains a **conflict detection engine** that monitors cross-advisor action patterns:

```
conflict_score(action_A, action_B) = f(
  schema_interaction(A.schema, B.schema),
  temporal_proximity(A.timestamp, B.timestamp),
  channel_overlap(A.channels, B.channels)
)

If conflict_score > threshold:
  1. Hold action_B pending
  2. Notify both advisors of the potential conflict
  3. Wait for resolution (advisor coordination or operator decision)
  4. Timeout: escalate to operator
```

### Shared State

Multiple advisors need visibility into each other's actions (but not control):

- **Read-only access to management receipts:** Each advisor can see the signed receipts from other advisors' actions on the same node. This is view-only — no advisor can modify or countermand another's receipts.
- **Action log subscription:** Advisors subscribe to a filtered stream of management actions on the node. They see schema type, timestamp, and result — not the full command parameters (which may contain competitive intelligence).
- **State hash continuity:** Each management response includes a `state_hash` (per Fleet Management spec). Advisors can verify their actions are based on current state, not stale data from before another advisor's recent action.

### Non-Interference Guarantees

The contract credential includes a `coordination` clause when multiple advisors are active:

```json
{
  "coordination": {
    "multiAdvisor": true,
    "peerAdvisors": ["did:cid:<other_advisor_did>"],
    "scopeIsolation": "strict",
    "conflictResolution": "escalate_to_operator",
    "sharedStateAccess": "receipts_readonly",
    "actionCooldownSeconds": 300
  }
}
```

The `actionCooldownSeconds` prevents rapid-fire competing actions — after any advisor takes an action, other advisors must wait before acting on the same channels.

---

## 7. Termination & Handoff

### Graceful Termination

```
Terminating Party           Other Party              Hive
       │                        │                      │
       │  1. Termination notice │                      │
       │  ───────────────────►  │                      │
       │     (notice period     │                      │
       │      begins: 7 days)   │                      │
       │                        │                      │
       │  2. Acknowledge        │                      │
       │  ◄───────────────────  │                      │
       │                        │                      │
       │  [Notice period: advisor continues operating  │
       │   with full scope; prepares transition]       │
       │                        │                      │
       │  3. Final settlement   │                      │
       │  ◄──────────────────►  │                      │
       │     (per Settlements   │                      │
       │      spec Type 9)      │                      │
       │                        │                      │
       │  4. Credential         │                      │
       │     revocation         │                      │
       │  ───────────────────────────────────────────► │
       │                        │                      │
       │  5. Reputation         │                      │
       │     credentials issued │                      │
       │  ◄──────────────────►  │                      │
       │                        │                      │
```

### Data Portability

On termination, the departing advisor may export:

| Data Type | Exportable | Format | Notes |
|-----------|-----------|--------|-------|
| Anonymized learnings | Yes | Aggregate statistics | Fee optimization patterns, seasonal trends |
| Channel profiles | Yes | Per-channel performance summaries | Public-key-referenced, no balances |
| Management receipts | Yes (own) | Signed receipts | Advisor's own action history |
| Raw node data | **No** | — | Channel balances, HTLC details, wallet state |
| Routing intelligence | **No** | — | Proprietary to the node |
| Peer identity data | **No** | — | Other nodes' DID-to-pubkey mappings |

Data portability is about the advisor's own work product — not the node's operational data. The advisor's signed receipts are already theirs (they have copies). Anonymized learnings (e.g., "channels with capacity ratio >0.8 responded well to fee reductions") are exportable because they contain no node-identifying information.

### Handoff Protocol

When a departing advisor is replaced by an incoming advisor:

```
Outgoing Advisor        Node Operator         Incoming Advisor
       │                      │                      │
       │  1. Termination      │                      │
       │     notice filed     │                      │
       │  ──────────────────► │                      │
       │                      │                      │
       │                      │  2. Hire incoming    │
       │                      │  ─────────────────►  │
       │                      │                      │
       │  3. Overlap period begins                   │
       │  (both active, scoped to avoid conflicts)   │
       │                      │                      │
       │  4. Knowledge transfer (optional, paid)     │
       │  ──────────────────────────────────────────► │
       │     • Channel profiles                      │
       │     • Optimization history                  │
       │     • Seasonal patterns                     │
       │     (via Intelligence Settlement Type 7)    │
       │                      │                      │
       │  5. Outgoing scope reduced to monitor-only  │
       │                      │                      │
       │  6. Incoming fully activated                │
       │                      │                      │
       │  7. Outgoing credential revoked             │
       │  ──────────────────► │                      │
       │                      │                      │
       │  8. Final reputation credentials            │
       │  ◄────────────────── │ ──────────────────►  │
       │                      │                      │
```

The overlap period (typically 3–7 days) ensures continuity. During overlap:
- Outgoing advisor operates with reducing scope (full → monitor-only over the overlap period)
- Incoming advisor ramps up (monitor-only → full scope over the overlap period)
- Both advisors see each other's receipts (shared state)
- Conflict resolution defaults to the incoming advisor (they have the ongoing relationship)

### Knowledge Transfer (Optional, Paid)

The outgoing advisor can offer a paid knowledge transfer — sharing anonymized optimization insights with the incoming advisor. This is settled via [Intelligence Settlement (Type 7)](./DID-HIVE-SETTLEMENTS.md#7-intelligence-sharing) from the Settlements spec.

Knowledge transfer is opt-in for both parties. The outgoing advisor sets a price; the incoming advisor (or operator) decides whether the insights are worth paying for. This creates an incentive for departing advisors to cooperate gracefully — their knowledge has value even after the relationship ends.

### Emergency Termination

For urgent situations (suspected compromise, gross negligence, breach of contract):

1. **Immediate credential revocation** via Archon network
2. **Pending escrow tickets** refund to operator via timelock expiry (no preimage revealed for incomplete tasks)
3. **All active commands** are cancelled (node stops processing the advisor's queued actions)
4. **Emergency termination receipt** signed by the operator, recording the reason
5. **Reputation credential** with `revoke` outcome if the termination was for cause

Emergency termination has no notice period. The operator bears the risk of service disruption. The advisor's pending legitimate compensation (completed but unredeemed escrow tickets) is honored — the preimage for completed work was already revealed, so the advisor can still redeem those tokens.

### Non-Compete & Cool-Down

- **Non-compete:** Optional, reputation-enforced. If an advisor solicits a departing client's nodes during the notice period, the operator can issue a `revoke` reputation credential with evidence. This is social enforcement, not technical — the protocol cannot prevent an advisor from advertising to anyone.
- **Cool-down period:** After termination, a configurable cool-down (default: 30 days) before the same advisor can be re-hired by the same operator. This prevents termination-rehire cycles used to reset trial terms or avoid performance commitments.

---

## 8. Referral & Affiliate System

### Referral Credentials

An advisor can recommend another advisor for capabilities outside their specialization:

```json
{
  "@context": [
    "https://www.w3.org/ns/credentials/v2",
    "https://hive.lightning/marketplace/v1"
  ],
  "type": ["VerifiableCredential", "HiveReferralCredential"],
  "issuer": "did:cid:<referring_advisor_did>",
  "credentialSubject": {
    "id": "did:cid:<referred_advisor_did>",
    "referralType": "specialization_complement",
    "context": "Client needs rebalancing expertise; referring to specialist",
    "referredCapabilities": ["rebalancing", "liquidity-management"],
    "referralFeeAgreed": true,
    "referralFeePct": 5,
    "disclosedToOperator": true
  },
  "validFrom": "2026-02-14T00:00:00Z",
  "validUntil": "2026-03-14T00:00:00Z"
}
```

### Referral Fee Settlement

Referral fees are settled via [Type 9 (Advisor Fee Settlement)](./DID-HIVE-SETTLEMENTS.md#9-advisor-fee-settlement) from the Settlements spec. The referring advisor receives a percentage of the referred advisor's first contract revenue:

```
referral_fee = referred_advisor.first_contract_revenue × referral_fee_pct / 100
```

The referral fee is:
- **Capped:** Maximum 10% of the first contract period's revenue
- **Disclosed:** The node operator sees the referral relationship and fee in the contract terms
- **One-time:** Referral fees apply only to the first contract. Renewals do not generate additional referral fees.
- **Conditional:** Only paid if the referred advisor completes the trial period successfully

### Referral Reputation

Referral quality is tracked as a meta-reputation signal. The `hive:referrer` domain is used within `DIDReputationCredential` credentials (credentialSubject excerpt shown):

```json
{
  "domain": "hive:referrer",
  "metrics": {
    "referrals_made": 8,
    "referrals_successful": 6,
    "referrals_failed_trial": 1,
    "referrals_terminated_early": 1,
    "avg_referred_performance": 0.82
  }
}
```

Advisors who consistently make good referrals build a meta-reputation as talent scouts — their referrals carry more weight in discovery ranking.

### Anti-Collusion Measures

| Risk | Mitigation |
|------|-----------|
| Advisor refers poor advisors for kickbacks | Referral reputation tracks referred advisor outcomes; bad referrals hurt the referrer |
| Circular referral rings (A refers B, B refers A) | Diminishing returns: referral fees decrease with relationship depth; circular refs flagged |
| Referral fee inflation | Hard cap at 10%; operator always sees the fee; operator can decline referred advisors |
| Sham referrals (advisor refers themselves under different DID) | DID graph analysis; shared infrastructure detection; operator due diligence |

---

## 9. Reputation Feedback Loop

### Mutual Reputation

After each contract period (or at termination), both parties issue reputation credentials:

#### Node Rates Advisor

Using the `hive:advisor` profile from the [DID Reputation Schema](./DID-REPUTATION-SCHEMA.md):

```json
{
  "@context": [
    "https://www.w3.org/ns/credentials/v2",
    "https://schemas.archetech.com/credentials/reputation/v1"
  ],
  "type": ["VerifiableCredential", "DIDReputationCredential"],
  "issuer": "did:cid:<node_operator_did>",
  "validFrom": "2026-05-14T00:00:00Z",
  "credentialSubject": {
    "id": "did:cid:<advisor_did>",
    "domain": "hive:advisor",
    "period": { "start": "2026-02-14T00:00:00Z", "end": "2026-05-14T00:00:00Z" },
    "metrics": {
      "revenue_delta_pct": 180,
      "actions_taken": 342,
      "uptime_pct": 99.4,
      "channels_managed": 45
    },
    "outcome": "renew",
    "evidence": [
      { "type": "SignedReceipt", "id": "did:cid:<receipt_merkle>", "description": "342 signed management receipts" },
      { "type": "MetricSnapshot", "id": "did:cid:<baseline_snapshot>", "description": "Revenue baseline and endpoint measurement" }
    ]
  }
}
```

#### Advisor Rates Node

Using the `hive:client` profile (see [DID Reputation Schema](./DID-REPUTATION-SCHEMA.md#profile-hiveclient)):

```json
{
  "@context": [
    "https://www.w3.org/ns/credentials/v2",
    "https://schemas.archetech.com/credentials/reputation/v1"
  ],
  "type": ["VerifiableCredential", "DIDReputationCredential"],
  "issuer": "did:cid:<advisor_did>",
  "validFrom": "2026-05-14T00:00:00Z",
  "credentialSubject": {
    "id": "did:cid:<node_operator_did>",
    "domain": "hive:client",
    "period": { "start": "2026-02-14T00:00:00Z", "end": "2026-05-14T00:00:00Z" },
    "metrics": {
      "payment_timeliness": 1.0,
      "sla_reasonableness": 0.9,
      "communication_quality": 0.85,
      "infrastructure_reliability": 0.95
    },
    "outcome": "renew",
    "evidence": [
      { "type": "EscrowReceipt", "id": "did:cid:<escrow_history>", "description": "All escrow tickets redeemed on time, no disputes" }
    ]
  }
}
```

> **Note:** The `hive:client` profile used above is a new profile distinct from the `hive:node` profile defined in the [Reputation Schema](./DID-REPUTATION-SCHEMA.md#profile-hivenode). It captures marketplace-specific metrics (`payment_timeliness`, `sla_reasonableness`, `communication_quality`, `infrastructure_reliability`) from the advisor's perspective of the node operator as a client. This profile should be proposed to the Archon profile registry following the [Defining New Profiles](./DID-REPUTATION-SCHEMA.md#defining-new-profiles) process.

### Why Mutual Reputation Matters

One-sided reputation (only nodes rate advisors) creates a power imbalance:
- Nodes can make unreasonable demands knowing the advisor has more to lose
- Advisors can't warn each other about problematic clients
- No accountability for nodes that don't pay on time or fabricate SLA violations

Mutual reputation creates **accountability on both sides:**
- Nodes with poor `payment_timeliness` scores attract fewer quality advisors
- Nodes with unreasonable SLAs (low `sla_reasonableness`) get flagged
- Advisors can make informed decisions about which clients to serve

### Aggregated Marketplace Reputation

The marketplace maintains an aggregate reputation view weighted by contract significance:

```
marketplace_reputation(did) = Σ (
  credential_weight(i) × normalize(metrics_i)
) / Σ credential_weight(i)

where:
  credential_weight(i) = 
    contract_duration_days(i) × 
    contract_scope_breadth(i) × 
    issuer_reputation(i)
```

Longer contracts, broader scope, and more reputable issuers produce higher-weight reputation signals. A 90-day full-stack management contract from a Senior-tier node carries more weight than a 7-day monitoring trial from a Newcomer.

---

## 10. Economic Model

### No Central Operator

The marketplace has no platform operator, no marketplace fee, and no central infrastructure. It runs on:

- **Hive gossip** for profile propagation and discovery (existing infrastructure)
- **Archon network** for DID resolution and credential storage (existing infrastructure)
- **Cashu mints** for payment escrow (existing infrastructure)
- **Nostr** for optional broader discovery (public infrastructure)

Cost to operate the marketplace: zero incremental infrastructure beyond what the protocol suite already requires.

### Premium Discovery (Optional)

While basic discovery is free, premium discovery services can be offered by any hive member:

| Service | Cost | Mechanism |
|---------|------|-----------|
| Featured listing | 1000 sats/week | Pay any node that runs a profile aggregator; profile gets priority in discovery responses |
| Priority search results | 500 sats/query | Pay the responding node to boost your profile in their results |
| Cross-hive broadcast | 2000 sats/broadcast | Pay a bridge node to propagate your profile to allied hives |

Premium services are **optional and competitive** — any node can offer them, and advisors choose which (if any) to use. Payment via Cashu tokens, settled directly between the parties.

### Market Dynamics

#### Price Discovery

The market finds equilibrium pricing through competition and transparency:

1. **Profile transparency:** All service profiles (including pricing) are public. Advisors can see competitors' rates.
2. **Bid competition:** RFP processes reveal market rates through competitive bidding.
3. **Performance correlation:** Reputation credentials link pricing to outcomes. A high-priced advisor with 300% revenue improvement justifies their premium.
4. **Specialization premium:** Specialists command higher rates in their domain; generalists compete on breadth and convenience.

Expected pricing tiers (to be validated by market):

| Service Tier | Monthly Rate (sats) | Performance Share | Typical Client |
|-------------|-------------------|-------------------|----------------|
| Monitoring-only | 500–2,000 | 0% | DIY operators wanting alerts |
| Basic optimization | 2,000–5,000 | 5–8% | Small nodes, cost-sensitive |
| Full management | 5,000–15,000 | 8–12% | Medium nodes, growth-focused |
| Premium / specialist | 10,000–50,000 | 10–15% | Large routing nodes, max performance |

#### Entry Barriers

Balancing spam prevention with accessible entry:

| Barrier | Level | Rationale |
|---------|-------|-----------|
| DID creation | Free | Anyone can create an Archon DID |
| Profile publishing | Free (gossip) | Basic advertising costs nothing |
| Minimum reputation to appear in discovery | 0 (configurable per-node) | New advisors appear in results; nodes filter by their own standards |
| Minimum bond to offer services | 10,000 sats (recommended) | Prevents zero-cost spam profiles; low enough for genuine new entrants |
| Trial period requirement | Strongly recommended | New advisors prove competence before earning full contracts |

New advisors bootstrap reputation through:
1. **Trial periods** with reduced fees (or free trials for the first client)
2. **Referrals** from established advisors
3. **Cross-domain reputation** (strong `agent:general` reputation transfers partial trust to `hive:advisor`)
4. **Open-source track record** (published analysis, tools, or contributions to hive protocol)

---

## 11. Public Marketplace (Non-Hive Nodes)

The marketplace described in sections 1–10 assumes hive membership — advisors and nodes discover each other through hive gossip, contract through hive PKI, and settle through the hive settlement protocol. But the real market is every Lightning node operator, most of whom will never join a hive.

This section defines how non-hive nodes participate in the marketplace via lightweight client software (`cl-hive-client` for CLN, `hive-lnd` for LND) as specified in the [DID Hive Client](./DID-HIVE-CLIENT.md) spec.

### Hive Marketplace vs Public Marketplace

| Property | Hive Marketplace | Public Marketplace |
|----------|-----------------|-------------------|
| Discovery | Gossip-based (push + pull) | Archon queries, Nostr events, directories |
| Participants | Hive members only (bonded) | Any node with a DID and client software |
| Contracting | Full PKI handshake, settlement integration | Direct credential issuance, escrow-only |
| Settlement | Netting, credit tiers, multilateral | Direct Cashu escrow per-action/subscription |
| Bond requirement | 50,000–500,000 sats | None |
| Intelligence access | Full market (buy/sell) | Advisor-mediated only |
| Entry barrier | Bond + reputation | DID creation (free) |

### Public Discovery Mechanisms

Non-hive nodes discover advisors through three channels:

1. **Archon network** — Query for `HiveServiceProfile` credentials. Advisors who want public marketplace clients publish their profiles to Archon (in addition to or instead of hive gossip). Nodes query via `hive-client-discover --source=archon`.

2. **Nostr events** — Advisors publish profiles as Nostr events (kind `38383`, tag `t:hive-advisor`). Nodes subscribe to relevant relays. DID-to-Nostr binding verified via attestation credential.

3. **Curated directories** — Web-based advisor directories that aggregate and present profiles. Not trusted — the client verifies underlying DID credentials independently.

All three mechanisms use the same `HiveServiceProfile` credential format defined in [Section 1](#1-service-advertising). The profile is the same whether discovered via gossip, Archon, or Nostr.

### Simplified Contracting for Non-Hive Nodes

Non-hive nodes skip the hive PKI handshake and settlement integration:

```
Operator                              Advisor
    │                                    │
    │  1. Discover (Archon/Nostr/direct) │
    │  ──────────────────────────────►   │
    │                                    │
    │  2. Review profile + reputation    │
    │                                    │
    │  3. Issue management credential    │
    │     (direct, no hive PKI)          │
    │  ──────────────────────────────►   │
    │                                    │
    │  4. Fund escrow wallet             │
    │     (direct Cashu, no settlement)  │
    │                                    │
    │  5. Management begins              │
    │  ◄─────────────────────────────►   │
    │                                    │
```

Key differences from hive contracting:
- **No settlement protocol** — All payments via direct Cashu escrow tickets. No netting, no credit tiers, no bilateral accounting.
- **No bond verification** — The operator doesn't need to verify the advisor's hive bond (they may not have one). Reputation credentials are the primary trust signal.
- **No gossip announcement** — The contract is private between the two parties. No `contract_announcement` to the hive.
- **Direct credential delivery** — Via Bolt 8 custom message (if peered), Archon Dmail, or Nostr DM.

### Non-Hive Nodes in the Reputation Loop

Non-hive nodes participate fully in the reputation system:
- They issue `DIDReputationCredential` with `domain: "hive:advisor"` to rate advisors (same format as hive members)
- Advisors issue `DIDReputationCredential` with `domain: "hive:client"` to rate non-hive operators
- These credentials are published to Archon and count toward the advisor's aggregate reputation
- Non-hive operator reputation is visible to advisors evaluating potential clients

### Client Software Requirements

Non-hive nodes must run:
- `cl-hive-client` (CLN) or `hive-lnd` (LND) — provides Schema Handler, Credential Verifier, Escrow Manager, Policy Engine
- Archon Keymaster — for DID identity (lightweight, no full Archon node)

See the [DID Hive Client](./DID-HIVE-CLIENT.md) spec for full architecture, installation, and configuration details.

### Upgrade Path

Non-hive nodes that want full marketplace features (gossip discovery, settlement netting, intelligence market, fleet rebalancing) can upgrade to hive membership. The migration preserves existing credentials, escrow state, and reputation history. See [DID Hive Client — Hive Membership Upgrade Path](./DID-HIVE-CLIENT.md#11-hive-membership-upgrade-path).

---

## 12. Privacy & Security

### Public vs. Private Information

| Information | Visibility | Rationale |
|------------|-----------|-----------|
| Service profiles | Public (gossip + Nostr) | Advertising requires visibility |
| Aggregated reputation scores | Public (Archon network) | Trust signals must be verifiable |
| Pricing models | Public (in profiles) | Price transparency enables market efficiency |
| Discovery queries | Private (anonymous option) | Nodes shouldn't reveal their management needs |
| Contract existence | Optional (registry) | Transparency vs. competitive privacy |
| Contract terms | Private (bilateral) | Pricing and SLA are competitive information |
| Node configurations | Private (never shared) | Operational security |
| Raw performance data | Private (bilateral) | Proprietary operational data |
| Channel graph details | Private (never shared) | Deanonymization risk |

### Anti-Deanonymization

Nodes must be able to discover and negotiate without revealing their full channel graph:

- **Discovery:** Anonymous queries reveal no node identity
- **Negotiation:** Proposals include aggregate node info (total capacity, channel count) but NOT specific channel IDs, peer identities, or balance distributions
- **Contract:** The advisor learns channel details only after the Management Credential is issued — at which point they have a contractual obligation to protect this information
- **Post-termination:** Advisors cannot retain or share node-specific channel graph data (enforced by contract terms; violated by reputation consequence)

### Spam Protection

| Attack | Protection |
|--------|-----------|
| Spam profiles (fake advisors flooding gossip) | Bond requirement (10k sats minimum); profile relay filtering by reputation threshold |
| Spam RFPs (wasting advisor time with fake requests) | RFP issuer bond or proof-of-reputation; sealed bids prevent information extraction |
| Sybil profiles (many DIDs, one advisor) | DID graph analysis; shared infrastructure detection; reputation doesn't transfer between sybils |
| Profile spoofing (impersonating a reputable advisor) | Profiles are signed VCs — forging requires the advisor's private key |
| Discovery flooding (DoS on gossip queries) | Rate limiting per DID; query cost for high-frequency queries |

---

## 13. Implementation Roadmap

Phased delivery, aligned with the other specs' roadmaps. The marketplace builds on top of the protocol suite — most marketplace functionality requires Fleet Management, Reputation, and Escrow to be at least partially implemented.

### Phase 1: Service Profiles & Basic Discovery (3–4 weeks)
*Prerequisites: DID Reputation Schema base, Fleet Management Phase 1 (schemas)*

- Define `HiveServiceProfile` credential schema
- Implement profile creation and signing via Archon Keymaster
- Add `service_profile_announce` to hive gossip protocol
- Basic discovery: gossip-based query/response
- Local profile cache and deduplication
- CLI tools for profile creation and discovery queries

### Phase 2: Negotiation & Contracting (3–4 weeks)
*Prerequisites: Fleet Management Phase 2 (DID auth), Task Escrow Phase 1 (single tickets)*

- Contract proposal and counter-offer message formats
- Direct hire flow: proposal → accept/reject → credential issuance
- Contract credential schema (bundles management credential + escrow + SLA)
- Trial period activation flow
- Basic SLA definition and measurement

### Phase 3: RFP & Competitive Bidding (2–3 weeks)
*Prerequisites: Phase 2*

- RFP publication via gossip
- Bid submission and collection
- Sealed-bid commitment scheme
- Award notification and contract formation
- Anonymous RFP support

### Phase 4: Multi-Advisor Coordination (2–3 weeks)
*Prerequisites: Fleet Management Phase 4 (Bolt 8 transport)*

- Scope partitioning enforcement in cl-hive policy engine
- Conflict detection engine (cross-advisor action monitoring)
- Shared state: receipt-based action log subscriptions
- Action cooldown enforcement

### Phase 5: Termination & Handoff (2–3 weeks)
*Prerequisites: Phase 2, Settlements Phase 4 (escrow integration)*

- Graceful termination protocol (notice period, credential revocation)
- Overlap period management for advisor transitions
- Data portability export tools
- Knowledge transfer via Intelligence Settlement (Type 7)
- Emergency termination flow

### Phase 6: Referral System & Reputation Loop (2–3 weeks)
*Prerequisites: Reputation Schema fully implemented, Settlements Phase 5 (credit tiers)*

- Referral credential schema and issuance
- Referral fee settlement via Type 9
- Mutual reputation issuance (advisor ↔ node)
- Marketplace reputation aggregation
- Referral reputation tracking (`hive:referrer` profile)

### Phase 7: Nostr Discovery & Premium Services (2–3 weeks)
*Prerequisites: Phase 1*

- Nostr profile publication (NIP-78 or custom kind)
- Cross-hive discovery via Archon network queries
- Premium discovery services (featured listings, priority results)
- Marketplace analytics dashboard

### Phase 8: Economic Optimization & Market Intelligence (ongoing)
*Prerequisites: All previous phases*

- Price discovery analysis tools
- Market health metrics (advisor utilization, average pricing, contract duration distributions)
- Entry barrier calibration based on observed spam/sybil rates
- Governance proposals for market parameter adjustments

### Cross-Spec Integration Timeline

```
Fleet Mgmt Phase 1-2  ──────────►  Marketplace Phase 1 (profiles + discovery)
                                         │
Task Escrow Phase 1    ──────────►  Marketplace Phase 2 (contracting)
                                         │
Fleet Mgmt Phase 4     ──────────►  Marketplace Phase 4 (multi-advisor)
                                         │
Settlements Phase 4-5  ──────────►  Marketplace Phase 5-6 (termination + referrals)
                                         │
Reputation Schema      ──────────►  Marketplace Phase 6 (reputation loop)
```

---

## 14. Open Questions

1. **Profile standardization:** Should the specialization taxonomy be fixed in the spec, or fully extensible via governance? Fixed is simpler for interoperability; extensible adapts to unforeseen use cases.

2. **Anonymous RFPs and trust:** Anonymous RFPs protect node privacy but make it harder for advisors to assess whether the client is legitimate. Should anonymous RFPs require a bond to signal seriousness?

3. **Multi-hive advisor reputation:** How should reputation earned in one hive transfer to another? Full portability? Discounted? Hive-specific reputation only?

4. **Contract enforcement:** The contract credential is a mutual agreement, not a smart contract. Enforcement is reputation-based. Is this sufficient for high-value contracts, or do we need on-chain commitment mechanisms?

5. **Advisor collusion:** Multiple advisors managing different aspects of the same node could collude (e.g., one intentionally degrades performance in their domain so the other looks better by comparison). How do we detect and prevent this?

6. **Market manipulation:** A well-funded advisor could offer below-cost services to drive competitors out, then raise prices. Standard predatory pricing. Does the marketplace's low entry barriers (new advisors can always enter) provide sufficient protection?

7. **Conflict resolution at scale:** The multi-advisor conflict detection engine needs careful tuning. Too sensitive = false positives blocking legitimate actions. Too lenient = actual conflicts causing damage. What's the right threshold, and how is it calibrated?

8. **RFP gaming:** Advisors could submit fake bids to learn competitors' pricing (in non-sealed scenarios). Should all RFPs default to sealed bids?

9. **Trial period exploitation:** Operators could cycle through advisors on perpetual trial periods, getting cheap management without ever paying full rates. Should there be a limit on concurrent or sequential trials?

10. **Knowledge transfer pricing:** How do we value an outgoing advisor's accumulated knowledge? Market pricing (advisor names a price, buyer accepts or declines) seems right, but there's no objective measure of knowledge value until after it's purchased.

---

## 15. References

- [DID + L402 Remote Fleet Management](./DID-L402-FLEET-MANAGEMENT.md)
- [DID Reputation Schema](./DID-REPUTATION-SCHEMA.md)
- [DID + Cashu Task Escrow Protocol](./DID-CASHU-TASK-ESCROW.md)
- [DID + Cashu Hive Settlements Protocol](./DID-HIVE-SETTLEMENTS.md)
- [W3C DID Core 1.0](https://www.w3.org/TR/did-core/)
- [W3C Verifiable Credentials Data Model 2.0](https://www.w3.org/TR/vc-data-model-2.0/)
- [Archon: Decentralized Identity for AI Agents](https://github.com/archetech/archon)
- [Cashu Protocol](https://cashu.space/)
- [Lightning Hive: Swarm Intelligence for Lightning](https://github.com/lightning-goats/cl-hive)
- [NIP-78: Application-Specific Data](https://github.com/nostr-protocol/nips/blob/master/78.md)
- [BOLT 7: P2P Node and Channel Discovery](https://github.com/lightning/bolts/blob/master/07-routing-gossip.md)

---

*Feedback welcome. File issues on [cl-hive](https://github.com/lightning-goats/cl-hive) or discuss in #singularity.*

*— Hex ⬡*
