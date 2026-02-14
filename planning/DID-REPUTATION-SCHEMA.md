# DID Reputation Schema

**Status:** Proposal / Design Draft  
**Version:** 0.1.0  
**Author:** Hex (`did:cid:bagaaierajrr7k6izcrdfwqxpgtrobflsv5oibymfnthjazkkokaugszyh4ka`)  
**Date:** 2026-02-14  
**Feedback:** Open — file issues or comment in #singularity

---

## Abstract

This document defines `DIDReputationCredential`, a general-purpose [W3C Verifiable Credential](https://www.w3.org/TR/vc-data-model-2.0/) schema for expressing reputation about any DID holder — agents, people, services, or nodes. It provides a base schema with domain-specific **profiles** that define valid metric keys, enabling interoperable reputation across heterogeneous systems.

The schema is designed for the Archon decentralized identity network but is portable to any DID method and VC-compatible ecosystem.

---

## Canonical Schema

> **📦 The JSON schemas defined in this document have been adopted by the Archon project.** The canonical schema files — `reputation-credential.json` and `reputation-profile.json` — are maintained at:
>
> **[archetech/schemas/credentials/reputation/v1](https://github.com/archetech/schemas/tree/main/credentials/reputation/v1)**
>
> The canonical schema context URL is: `https://schemas.archetech.com/credentials/reputation/v1`
>
> This document remains the authoritative specification for semantics, aggregation algorithms, and domain profiles. The Archon schema repository contains the machine-readable JSON Schema files for credential validation.

---

## Motivation

Reputation is the missing primitive in decentralized identity. DIDs give us verifiable identity; Verifiable Credentials give us verifiable claims. But there is no standard way to say:

> "This DID performed well in domain X over period Y, and here is the cryptographic evidence."

Existing approaches are domain-specific and siloed. A Lightning routing node's reputation doesn't compose with an AI agent's task completion rate, even though both are fundamentally the same structure: **a subject, evaluated in a domain, over a period, producing metrics, supported by evidence.**

### Design Goals

1. **Universal** — One schema for any DID holder type (human, agent, node, service)
2. **Composable** — Reputation from different domains and issuers can be aggregated
3. **Verifiable** — Every claim is backed by signed evidence, not self-reported
4. **Extensible** — New domains are added by defining profiles, not modifying the base schema
5. **Sybil-resistant** — Aggregation rules account for issuer diversity and collusion

---

## Base Schema: `DIDReputationCredential`

### W3C Verifiable Credential Structure

```json
{
  "@context": [
    "https://www.w3.org/ns/credentials/v2",
    "https://archon.technology/schemas/reputation/v1",
    "https://schemas.archetech.com/credentials/reputation/v1"
  ],
  "type": ["VerifiableCredential", "DIDReputationCredential"],
  "issuer": "did:cid:<issuer_did>",
  "validFrom": "2026-03-14T00:00:00Z",
  "credentialSubject": {
    "id": "did:cid:<subject_did>",
    "domain": "hive:advisor",
    "period": {
      "start": "2026-02-14T00:00:00Z",
      "end": "2026-03-14T00:00:00Z"
    },
    "metrics": {
      "revenue_delta_pct": 340,
      "actions_taken": 87,
      "uptime_pct": 99.2,
      "channels_managed": 19
    },
    "outcome": "renew",
    "evidence": [
      {
        "type": "SignedReceipt",
        "id": "did:cid:<receipt_credential_did>",
        "description": "87 signed management receipts from managed node"
      },
      {
        "type": "MetricSnapshot",
        "id": "did:cid:<snapshot_credential_did>",
        "description": "Revenue measurement at period start and end"
      }
    ]
  }
}
```

### Core Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `credentialSubject.id` | DID | Yes | The DID being evaluated. Any DID method. |
| `credentialSubject.domain` | string | Yes | Profile identifier (e.g., `hive:advisor`, `agent:general`). Defines valid metric keys. |
| `credentialSubject.period` | object | Yes | `{ start, end }` — ISO 8601 timestamps bounding the evaluation window. |
| `credentialSubject.metrics` | object | Yes | Domain-specific key-value pairs. Keys must conform to the domain profile. Values are numbers or strings. |
| `credentialSubject.outcome` | enum | Yes | One of: `renew` (positive — continued engagement), `revoke` (negative — termination), `neutral` (informational, no recommendation). |
| `credentialSubject.evidence` | array | No | References to signed receipts, attestations, or snapshots that back the metrics. Each entry has `type`, `id` (DID or URI), and `description`. |
| `issuer` | DID | Yes | The DID issuing the reputation credential. Typically the entity that directly observed the subject's performance. |
| `validFrom` | datetime | Yes | When this credential becomes valid (VC 2.0 replaces `issuanceDate`). |
| `validUntil` | datetime | No | When this credential should no longer be considered current (VC 2.0 replaces `expirationDate`). If omitted, the credential is valid indefinitely (but `period.end` still bounds the evaluation window). |

### Outcome Semantics

| Outcome | Meaning | Signal |
|---------|---------|--------|
| `renew` | Positive evaluation. Issuer would engage again. | Trust-building |
| `revoke` | Negative evaluation. Relationship terminated or not recommended. | Trust-reducing |
| `neutral` | Informational only. No strong signal either way. | Baseline data |

A `revoke` outcome doesn't mean the credential itself is revoked — it means the issuer is expressing a negative reputation signal. Credential revocation (via Archon) is a separate mechanism that invalidates the credential entirely.

### Evidence Types

| Type | Description | Example |
|------|-------------|---------|
| `SignedReceipt` | A countersigned record of an action taken. Both parties signed. | Management command receipts from [DID+L402 Fleet Management](./DID-L402-FLEET-MANAGEMENT.md) |
| `MetricSnapshot` | A signed measurement at a point in time (e.g., revenue, uptime). | Node revenue at period start vs end |
| `Attestation` | A third-party statement vouching for a claim. | Another node confirming routing reliability |
| `AuditLog` | A signed log or merkle root covering a set of operations. | Hash of all agent actions during period |

Evidence entries reference other Verifiable Credentials or URIs. Verifiers can resolve the references to independently confirm the metrics.

---

## Domain Profiles

A **profile** defines the valid metric keys, their types, and their semantics for a specific domain. Profiles are identified by the `domain` field in the credential.

### Profile Registry

Profiles are published as Archon Verifiable Credentials, enabling:
- **Discovery** — Query the Archon network for all registered profiles
- **Validation** — Verify that a credential's metrics match its declared profile
- **Governance** — New profiles are proposed and approved by domain stakeholders

Profile identifiers follow the pattern `<namespace>:<type>`:
- `hive:*` — Lightning Hive ecosystem
- `agent:*` — AI agent ecosystem
- `service:*` — Generic service providers
- `peer:*` — Peer-to-peer network participants

### Profile: `hive:advisor`

**Subject type:** DID of a Lightning fleet advisor (agent or human)  
**Issuer type:** DID of a node operator whose fleet was managed  
**Reference:** [DID+L402 Fleet Management](./DID-L402-FLEET-MANAGEMENT.md)

| Metric Key | Type | Unit | Description |
|------------|------|------|-------------|
| `revenue_delta_pct` | number | percent | Change in routing revenue vs baseline period. 100 = doubled. |
| `actions_taken` | integer | count | Total management actions executed during period. |
| `uptime_pct` | number | percent | Percentage of period the advisor was responsive and active. |
| `channels_managed` | integer | count | Number of channels under active management. |

**Example evidence:** Signed management receipts (per [DID+L402 protocol](./DID-L402-FLEET-MANAGEMENT.md)), revenue snapshots at period boundaries.

**Outcome interpretation:**
- `renew` — Operator extends the management credential
- `revoke` — Operator terminates the management relationship
- `neutral` — Period ended without strong signal (e.g., trial period)

### Profile: `hive:node`

**Subject type:** DID of a Lightning node (or its operator)  
**Issuer type:** DID of a peer node, routing service, or monitoring service

| Metric Key | Type | Unit | Description |
|------------|------|------|-------------|
| `routing_reliability` | number | 0.0–1.0 | Fraction of attempted routes through this node that succeeded. |
| `uptime` | number | percent | Percentage of period the node was reachable. |
| `htlc_success_rate` | number | 0.0–1.0 | Fraction of forwarded HTLCs that resolved successfully. |
| `avg_fee_ppm` | number | ppm | Average fee rate charged during period. (optional) |
| `capacity_sats` | integer | sats | Total channel capacity during period. (optional) |

**Example evidence:** Probe results, forwarding statistics, gossip uptime measurements, settlement receipts from the [DID + Cashu Hive Settlements Protocol](./DID-HIVE-SETTLEMENTS.md).

The `hive:node` profile is central to the hive settlements protocol — bond amounts, slash history, and settlement dispute outcomes are recorded as metrics in this profile, and the aggregated reputation score determines [credit and trust tiers](./DID-HIVE-SETTLEMENTS.md#credit-and-trust-tiers) for settlement terms.

**Outcome interpretation:**
- `renew` — Peer maintains or opens channels with this node
- `revoke` — Peer closes channels or blacklists this node
- `neutral` — Routine measurement, no action taken

### Profile: `hive:client`

**Subject type:** DID of a node operator (as a client of advisory services)  
**Issuer type:** DID of an advisor who managed the operator's fleet  
**Reference:** [DID Hive Marketplace Protocol](./DID-HIVE-MARKETPLACE.md)

| Metric Key | Type | Unit | Description |
|------------|------|------|-------------|
| `payment_timeliness` | number | 0.0–1.0 | Fraction of payments made on time per contract terms. |
| `sla_reasonableness` | number | 0.0–1.0 | How reasonable the operator's SLA expectations were (advisor's assessment). |
| `communication_quality` | number | 0.0–1.0 | Responsiveness and clarity of operator communication. |
| `infrastructure_reliability` | number | 0.0–1.0 | Node infrastructure uptime and accessibility during management period. |
| `trial_count_90d` | integer | count | Number of trial periods initiated in the last 90 days. (optional) |

**Example evidence:** Escrow ticket redemption records, SLA definitions from contract credentials, communication logs.

**Outcome interpretation:**
- `renew` — Advisor would work with this operator again
- `revoke` — Advisor terminates relationship or warns other advisors
- `neutral` — Standard engagement, no strong signal

### Profile: `agent:general`

**Subject type:** DID of an AI agent  
**Issuer type:** DID of a task delegator, platform, or evaluation service

| Metric Key | Type | Unit | Description |
|------------|------|------|-------------|
| `task_completion_rate` | number | 0.0–1.0 | Fraction of assigned tasks completed successfully. |
| `accuracy` | number | 0.0–1.0 | Quality score of completed work (domain-dependent measurement). |
| `response_time_ms` | number | milliseconds | Median response time for task initiation. |
| `tasks_evaluated` | integer | count | Number of tasks in the evaluation sample. |

**Example evidence:** Signed task receipts, evaluation rubric results, automated test outcomes.

**Outcome interpretation:**
- `renew` — Delegator continues using this agent
- `revoke` — Delegator stops delegating to this agent
- `neutral` — Benchmark evaluation, no ongoing relationship

---

## Defining New Profiles

Any entity can propose a new profile by publishing a `DIDReputationProfile` credential:

```json
{
  "@context": [
    "https://www.w3.org/ns/credentials/v2",
    "https://archon.technology/schemas/reputation/v1",
    "https://schemas.archetech.com/credentials/reputation/v1"
  ],
  "type": ["VerifiableCredential", "DIDReputationProfile"],
  "issuer": "did:cid:<proposer_did>",
  "credentialSubject": {
    "domain": "hive:channel-partner",
    "version": "1.0.0",
    "description": "Reputation profile for evaluating Lightning channel partnerships",
    "subjectType": "Lightning node operator",
    "issuerType": "Channel partner or routing analysis service",
    "metrics": {
      "liquidity_reliability": {
        "type": "number",
        "range": [0.0, 1.0],
        "description": "Consistency of channel liquidity availability"
      },
      "fee_stability": {
        "type": "number",
        "range": [0.0, 1.0],
        "description": "How predictable the peer's fee policy is"
      },
      "cooperative_close_rate": {
        "type": "number",
        "range": [0.0, 1.0],
        "description": "Fraction of channel closes that were cooperative"
      }
    },
    "requiredMetrics": ["liquidity_reliability"],
    "optionalMetrics": ["fee_stability", "cooperative_close_rate"]
  }
}
```

### Profile Versioning

Profiles use semantic versioning:
- **Patch** (1.0.x): Documentation clarifications, no metric changes
- **Minor** (1.x.0): New optional metrics added
- **Major** (x.0.0): Required metrics changed, breaking

Credentials reference their profile domain string (e.g., `hive:advisor`). Verifiers resolve the latest profile version to validate metrics. Credentials issued under older profile versions remain valid — verifiers should accept unknown optional metrics gracefully.

---

## Aggregation & Discovery

### Querying Reputation

To evaluate a DID's reputation, a verifier collects `DIDReputationCredential` instances from multiple issuers and aggregates them.

#### Discovery Methods

1. **Archon Network Query** — Query the Archon network for all `DIDReputationCredential` credentials where `credentialSubject.id` matches the target DID
2. **Subject-Published Index** — The subject DID publishes a list of reputation credential references in their DID document's `service` endpoint
3. **Domain Registry** — Domain-specific registries (e.g., a Lightning routing reputation aggregator) collect and index credentials

```
Verifier                          Archon Network
   │                                    │
   │  1. Query: DIDReputationCredential │
   │     where subject = did:cid:abc    │
   │     and domain = "hive:advisor"    │
   │  ─────────────────────────────►    │
   │                                    │
   │  2. Returns N credentials from     │
   │     M distinct issuers             │
   │  ◄─────────────────────────────    │
   │                                    │
   │  3. Verify each credential         │
   │     (signature, revocation,        │
   │      expiration, evidence)         │
   │                                    │
   │  4. Aggregate using weighting      │
   │     rules (see below)              │
   │                                    │
```

### Aggregation Algorithm

Raw reputation credentials must be aggregated carefully. A naive average is trivially gamed.

#### Weighted Aggregation

```
reputation_score(subject, domain) =
  Σ (weight_i × normalize(metrics_i)) / Σ weight_i

where weight_i = issuer_weight(issuer_i) × recency(period_i) × evidence_strength(evidence_i)
```

**Issuer Weight Factors:**

| Factor | Weight Modifier | Rationale |
|--------|----------------|-----------|
| Issuer has own reputation | ×1.0–2.0 | Reputable issuers' opinions count more |
| Issuer diversity | ×0.5–1.0 | Diminishing returns from same issuer |
| Issuer-subject independence | ×0.0–1.0 | Self-issued or colluding issuers discounted |
| Issuer stake | ×1.0–3.0 | Issuers with skin in the game (e.g., open channels) weighted higher |

**Recency Decay:**

```
recency(period) = exp(-λ × days_since(period.end))
```

Where λ controls how fast old credentials decay. Suggested default: λ = 0.01 (half-life ≈ 69 days).

**Evidence Strength:**

| Evidence Count | Modifier |
|----------------|----------|
| 0 (no evidence) | ×0.3 |
| 1–5 references | ×0.7 |
| 5+ with signed receipts | ×1.0 |

### Sybil Resistance

Reputation systems are inherently vulnerable to sybil attacks — an entity creating multiple DIDs to issue fake reputation credentials to itself.

#### Mitigations

1. **Proof of Stake** — Weight issuer credentials by verifiable economic commitment. In the Lightning context: issuers with open channels to the subject have real capital at risk. Their reputation signals carry more weight.

2. **Issuer Graph Analysis** — Track the issuer-subject graph. Clusters of DIDs that only issue credentials to each other are suspicious. Apply diminishing weight to credentials from issuers in the same cluster.

3. **Temporal Consistency** — Reputation built over longer periods with consistent metrics from diverse issuers is harder to fake. Weight long-tenure relationships higher.

4. **Evidence Verification** — Credentials with resolvable, independently verifiable evidence (signed receipts from third parties, on-chain data) are worth more than self-attested claims.

5. **Web of Trust Anchoring** — Anchor the reputation graph to well-known, high-cost identities. A credential issued by a node operator with 10 BTC in channels carries more weight than one from a fresh DID with no history.

6. **Cross-Domain Corroboration** — A DID with reputation in multiple unrelated domains is less likely to be a sybil. An `agent:general` credential from a task platform that corroborates a `hive:advisor` credential from a node operator strengthens both.

#### What This Schema Does NOT Solve

This schema provides the **data format** for reputation. It does not prescribe a single aggregation algorithm or sybil resistance strategy. Different consumers will weight factors differently based on their risk tolerance. The schema ensures they all have the same structured data to work with.

---

## Cross-Domain Reputation

A key design goal is enabling reputation to compose across domains. An entity's `hive:advisor` reputation should be discoverable alongside their `agent:general` reputation, even though the metrics are different.

### Unified DID Reputation View

```
┌──────────────────────────────────────────────────┐
│           DID: did:cid:abc123...                  │
├──────────────────────────────────────────────────┤
│                                                    │
│  hive:advisor  ████████████░░  85/100              │
│    3 issuers, 6 months tenure                      │
│    avg revenue_delta_pct: +210%                    │
│                                                    │
│  agent:general ██████████████  92/100              │
│    1 issuer, 2 months tenure                       │
│    task_completion_rate: 0.95                      │
│                                                    │
│  hive:node     ███████████░░░  78/100              │
│    8 issuers, 12 months tenure                     │
│    routing_reliability: 0.89                       │
│                                                    │
│  Overall: ████████████░░░  83/100                  │
│  Sybil Risk: LOW (diverse issuers, staked)         │
│                                                    │
└──────────────────────────────────────────────────┘
```

Cross-domain aggregation normalizes domain-specific metrics to a 0–100 score using the profile's defined ranges, then combines with equal or configurable domain weights.

### Score Threshold Interpretation

This schema produces 0–100 aggregate scores but does **not** prescribe threshold meanings. Consumers apply domain-specific interpretations. For reference, the [DID + Cashu Hive Settlements Protocol](./DID-HIVE-SETTLEMENTS.md#credit-and-trust-tiers) uses these thresholds for node trust tiers:

| Score Range | Tier | Meaning |
|-------------|------|---------|
| 0–59 | Newcomer | Insufficient history for trust |
| 60–74 | Recognized | Basic track record established |
| 75–84 | Trusted | Consistent positive performance |
| 85–100 | Senior | Exceptional long-term reliability |

Other consumers may define different thresholds appropriate to their risk tolerance. The schema intentionally leaves this to domain-specific policy.

---

## Relationship to Existing Specs

### DID+L402 Fleet Management

The [DID+L402 Fleet Management](./DID-L402-FLEET-MANAGEMENT.md) spec defines `HiveAdvisorReputationCredential` for Lightning fleet advisors. That credential is a **domain-specific instance** of this general schema, using the `hive:advisor` profile.

The fleet management spec's reputation system implements this schema's base structure with Lightning-specific evidence types (management receipts, revenue snapshots) and outcome semantics (credential renewal/revocation).

### W3C Verifiable Credentials

This schema follows [VC Data Model 2.0](https://www.w3.org/TR/vc-data-model-2.0/):
- Context URL: `https://www.w3.org/ns/credentials/v2` (VC 2.0)
- Standard `@context`, `type`, `issuer`, `validFrom`, `credentialSubject` structure
- `validFrom`/`validUntil` replace the 1.1-era `issuanceDate`/`expirationDate`
- Evidence references follow the VC evidence property pattern
- Revocation uses the issuer's DID method's native revocation mechanism (Archon credential revocation)

### Archon DIDs

[Archon](https://github.com/archetech/archon) provides the identity substrate:
- DIDs for subjects and issuers
- Credential issuance and revocation via Keymaster
- Network-wide credential discovery via Gatekeeper
- Cryptographic verification of all claims

---

## Implementation Notes

### Issuing a Reputation Credential

Using Archon Keymaster:

```bash
# 1. Create the credential data
cat > reputation.json << 'EOF'
{
  "domain": "hive:advisor",
  "period": { "start": "2026-02-14T00:00:00Z", "end": "2026-03-14T00:00:00Z" },
  "metrics": {
    "revenue_delta_pct": 340,
    "actions_taken": 87,
    "uptime_pct": 99.2,
    "channels_managed": 19
  },
  "outcome": "renew",
  "evidence": [
    { "type": "SignedReceipt", "id": "did:cid:<receipt_did>", "description": "87 signed management receipts" }
  ]
}
EOF

# 2. Issue as Verifiable Credential to the subject DID
npx @didcid/keymaster issue-credential \
  --type DIDReputationCredential \
  --subject did:cid:<advisor_did> \
  --data reputation.json
```

### Querying Reputation

```bash
# Find all reputation credentials for a DID
npx @didcid/keymaster search-credentials \
  --type DIDReputationCredential \
  --subject did:cid:<target_did>

# Filter by domain
npx @didcid/keymaster search-credentials \
  --type DIDReputationCredential \
  --subject did:cid:<target_did> \
  --filter 'credentialSubject.domain == "hive:advisor"'
```

### Validation Checklist

When verifying a `DIDReputationCredential`:

1. ✅ Standard VC validation (signature, schema, expiration, revocation)
2. ✅ `domain` matches a known profile
3. ✅ `metrics` keys conform to the profile's required/optional sets
4. ✅ `metrics` values are within the profile's defined ranges
5. ✅ `period.start` < `period.end`
6. ✅ `outcome` is one of `renew`, `revoke`, `neutral`
7. ✅ `evidence` references (if present) resolve to valid credentials or URIs
8. ✅ Issuer DID is not the same as subject DID (self-issued credentials flagged)

---

## Open Questions

1. **Profile governance:** Who approves new profiles? Per-domain authorities? Archon-wide governance? Open registry with social consensus?

2. **Negative reputation privacy:** Should `revoke` outcomes be publishable without the subject's consent? Privacy vs. safety tradeoff.

3. **Metric normalization:** How do we compare `revenue_delta_pct: 340` across different market conditions? Should profiles define normalization baselines?

4. **Credential volume:** High-frequency domains (e.g., per-HTLC node reputation) could generate enormous credential volumes. Should there be a summary/rollup mechanism?

5. **Interoperability:** How do reputation credentials from non-Archon DID methods integrate? The schema is DID-method-agnostic, but discovery and revocation depend on the method.

6. **Incentive to issue:** See [Issuance Incentives](#issuance-incentives) below for analysis.

---

## Issuance Incentives

A reputation system only works if participants issue credentials. Why would an operator spend effort issuing reputation credentials for their advisor?

### Automated Issuance at Credential Renewal

The primary mechanism: reputation credential issuance is **automated** as part of the management credential lifecycle. When a management credential (per [DID+L402 Fleet Management](./DID-L402-FLEET-MANAGEMENT.md)) expires or renews, the node's cl-hive plugin automatically generates a `DIDReputationCredential` (with `domain: "hive:advisor"`) based on measured metrics (actions taken, revenue delta, uptime). The operator need only approve the renewal — the reputation credential is a byproduct, not extra work.

### Protocol Requirement for Performance Settlement

Performance-based payment (see [Task Escrow — Performance Ticket](./DID-CASHU-TASK-ESCROW.md#performance-ticket)) requires a signed metric attestation to trigger bonus release. This attestation **is** a reputation credential. Operators who use performance-based pricing are already issuing reputation data as part of the payment flow.

### Reputation Reciprocity

Operators benefit from having reputable advisors — it signals to the network that their node is well-managed. An operator who issues honest reputation credentials for good advisors attracts better advisors in the future (advisors prefer operators who build their track record). Conversely, operators who refuse to issue credentials for good work will find it harder to attract talent.

### Negative Reputation as Defense

Operators are incentivized to issue `revoke` credentials against bad advisors to protect the ecosystem. This is self-interested: warning other operators about a bad actor prevents that actor from damaging the hive network that the operator depends on.

---

## References

- [W3C DID Core 1.0](https://www.w3.org/TR/did-core/)
- [W3C Verifiable Credentials Data Model 2.0](https://www.w3.org/TR/vc-data-model-2.0/)
- [Archon: Decentralized Identity for AI Agents](https://github.com/archetech/archon)
- [Archon Reputation Schemas (canonical)](https://github.com/archetech/schemas/tree/main/credentials/reputation/v1)
- [DID+L402 Remote Fleet Management](./DID-L402-FLEET-MANAGEMENT.md)
- [DID + Cashu Hive Settlements Protocol](./DID-HIVE-SETTLEMENTS.md)
- [DID Hive Marketplace Protocol](./DID-HIVE-MARKETPLACE.md) — Primary consumer of reputation credentials for advisor discovery, ranking, and contract formation
- [DID Hive Client: Universal Lightning Node Management](./DID-HIVE-CLIENT.md) — Client plugin/daemon for non-hive nodes
- [Lightning Hive: Swarm Intelligence for Lightning](https://github.com/lightning-goats/cl-hive)

---

*Feedback welcome. File issues on [cl-hive](https://github.com/lightning-goats/cl-hive) or discuss in #singularity.*

*— Hex ⬡*
