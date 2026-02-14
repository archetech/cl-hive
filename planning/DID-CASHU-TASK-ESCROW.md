# DID + Cashu Task Escrow Protocol

**Status:** Proposal / Design Draft  
**Version:** 0.1.0  
**Author:** Hex (`did:cid:bagaaierajrr7k6izcrdfwqxpgtrobflsv5oibymfnthjazkkokaugszyh4ka`)  
**Date:** 2026-02-14  
**Feedback:** Open — file issues or comment in #singularity

---

## Abstract

This document defines a protocol for conditional Cashu ecash tokens that act as escrow "tickets" for agent task execution. Each ticket is a Cashu token with composite spending conditions: locked to an agent's DID-derived public key (NUT-11 P2PK), hash-locked and time-locked with a refund path (NUT-14 HTLC), all encoded using the structured secret format (NUT-10). Payment is released if and only if the agent completes the task and the node reveals the HTLC preimage — making task completion and payment release atomic.

The protocol is general-purpose. While motivated by Lightning fleet management, it applies to any scenario where one party wants to pay another party contingent on provable work: code review, research tasks, monitoring, content generation, or any agent service market.

---

## Motivation

### The Escrow Problem in Agent Economies

Autonomous agents need to get paid. Operators need assurance that payment only flows for completed work. The fundamental tension:

- **Agents won't work for free** — they need guaranteed compensation for successful task execution
- **Operators won't pay blindly** — they need proof of completion before releasing funds
- **Neither party trusts the other** — especially in open marketplaces with pseudonymous participants

Traditional escrow requires a trusted third party. This is antithetical to decentralized agent systems. We need **trustless escrow** — payment conditioned on cryptographic proof of task completion, with automatic refund on failure.

### Why Not Just Lightning HTLCs?

Lightning's native HTLC mechanism provides hash-locked conditional payments. However:

| Property | Lightning HTLC | Cashu Escrow Ticket |
|----------|---------------|-------------------|
| Requires online sender | Yes (routing) | No (bearer token, offline) |
| Requires routing path | Yes | No (direct mint redemption) |
| Time-lock granularity | Block height (≈10 min) | Unix timestamp (seconds) |
| Privacy | Correlatable across hops | Blind signatures — mint can't link ticket to task |
| Composability | Single hash condition | P2PK + HTLC + timelock composed |
| Offline holding | No (channel state) | Yes (bearer instrument) |
| Batch-friendly | Requires N payments | Single mint, N tokens |

Cashu tokens are bearer instruments with programmable spending conditions. They combine the hash-lock mechanism of Lightning HTLCs with the offline capability and privacy of ecash. For task escrow, this is strictly better.

### Current State

The [DID+L402 Fleet Management](./DID-L402-FLEET-MANAGEMENT.md) spec defines per-action Cashu payment as a simple bearer token: agent attaches a Cashu token to each management command, and the node redeems it. This works for low-trust, low-risk actions but has no conditionality — the node gets paid whether the task succeeds or fails.

For higher-value operations (large rebalances, channel opens, performance-based management), we need conditional payment: the token should only be redeemable upon provable task completion.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────┐
│                     OPERATOR                                  │
│                                                               │
│  1. Mints escrow ticket:                                      │
│     Cashu token with conditions:                              │
│       • P2PK: locked to Agent's DID pubkey (NUT-11)          │
│       • HTLC: H(secret) where Node holds secret (NUT-10)    │
│       • Timelock: refund to Operator after deadline (NUT-14) │
│       • Metadata: task schema, danger score, node ID          │
│                                                               │
│  Sends ticket to Agent via Bolt 8 / Dmail / any channel      │
└────────────────────────┬─────────────────────────────────────┘
                         │
                    ticket assignment
                         │
                         ▼
┌──────────────────────────────────────────────────────────────┐
│                      AGENT                                    │
│                                                               │
│  2. Presents to Node:                                         │
│     ticket + DID credential + task command                    │
│                                                               │
│  Holds ticket until task execution                            │
└────────────────────────┬─────────────────────────────────────┘
                         │
                    task + ticket
                         │
                         ▼
┌──────────────────────────────────────────────────────────────┐
│                       NODE                                    │
│                                                               │
│  3. Validates credential, executes task                       │
│  4. If successful: returns signed receipt + HTLC preimage     │
│     If failed: returns failure receipt, no preimage           │
│                                                               │
└────────────────────────┬─────────────────────────────────────┘
                         │
                  receipt + preimage
                         │
                         ▼
┌──────────────────────────────────────────────────────────────┐
│                      AGENT                                    │
│                                                               │
│  5. Now has: private key (DID) + preimage                     │
│     Redeems token with mint                                   │
│                                                               │
│  ──────────── OR (timeout) ─────────────                      │
│                                                               │
│  6. Timelock expires → Operator reclaims via refund path      │
│                                                               │
└──────────────────────────────────────────────────────────────┘
```

---

## Protocol Components

### Cashu NUT References

This protocol composes three Cashu NUT specifications to create conditional escrow tokens:

#### NUT-10: Structured Secret Format

[NUT-10](https://github.com/cashubtc/nuts/blob/main/10.md) defines the **spending condition framework** for Cashu tokens. Instead of a random secret, the token's secret is a structured JSON array: `[kind, {nonce, data, tags}]`. NUT-10 itself defines no spending semantics — it provides the **container format** that higher-level NUTs (NUT-11, NUT-14) populate with specific condition types.

**How it's used:** All escrow ticket conditions are encoded in the NUT-10 structured secret format. The `kind` field identifies which spending rules apply (e.g., `"P2PK"` for NUT-11/14 conditions). The `data` field carries the primary condition (a public key), and `tags` carry additional conditions (hash locks, timelocks, refund paths).

#### NUT-11: Pay-to-Public-Key (P2PK)

[NUT-11](https://github.com/cashubtc/nuts/blob/main/11.md) defines **signature-based spending conditions** using the NUT-10 format. A token with kind `"P2PK"` requires a valid secp256k1 signature from the public key specified in `data`. NUT-11 also introduces the `tags` system for additional conditions (`sigflag`, `n_sigs`, `pubkeys` for multisig, `locktime`, `refund`).

**How it's used:** The agent's DID-derived secp256k1 public key is the P2PK lock. This ensures only the authorized agent — the one whose DID credential grants management permission — can redeem the escrow ticket. Even if the HTLC preimage leaks, no one else can spend the token. NUT-11 also supports multisig via the `n_sigs` and `pubkeys` tags, used for bond multisig in the [settlements protocol](./DID-HIVE-SETTLEMENTS.md#bond-system).

#### NUT-14: Hashed Timelock Contracts (HTLCs)

[NUT-14](https://github.com/cashubtc/nuts/blob/main/14.md) **extends NUT-11 P2PK** with hash-lock conditions, composing P2PK signatures + hash preimage verification + timelocks into a single spending condition. A NUT-14 HTLC token uses kind `"P2PK"` (same as NUT-11) but adds a `hash` tag containing the lock hash. The token can be spent in two ways:

1. **Normal spend:** Provide the hash preimage AND a valid P2PK signature (before the timelock)
2. **Refund spend:** After the timelock expires, any pubkey listed in the `refund` tag can claim the token without the preimage

**How it's used:** The HTLC hash is `H(secret)` where the node generates and holds `secret`. The timelock is set to the task deadline. If the agent completes the task, the node reveals `secret` in the signed receipt. If the task isn't completed before the deadline, the operator reclaims via the refund path.

> **Note:** The `refund` tag accepts a *list* of pubkeys. For single-operator refund, one pubkey suffices. For multi-party escrow (e.g., hive bonds), multiple refund pubkeys can be specified.

#### NUT-14 HTLC Secret Structure (using NUT-10 format)

The complete escrow ticket secret, encoded per NUT-10's structured format with NUT-14 HTLC conditions:

```json
[
  "P2PK",
  {
    "nonce": "<unique_nonce>",
    "data": "<agent_did_pubkey_hex>",
    "tags": [
      ["hash", "<hex_encoded_sha256_hash>"],
      ["locktime", "<unix_timestamp>"],
      ["refund", "<operator_pubkey_hex>"],
      ["sigflag", "SIG_ALL"]
    ]
  }
]
```

> **Implementation note:** The `hash` tag contains only the hex-encoded SHA-256 hash value. The hash algorithm is always SHA-256 per NUT-14 — do not include an algorithm identifier in the tag.

#### Mint Requirements

Mints used for escrow tickets **must** support the following NUTs:

| NUT | Requirement | Purpose |
|-----|------------|---------|
| NUT-10 | Required | Structured secret format |
| NUT-11 | Required | P2PK signature conditions |
| NUT-14 | Required | HTLC hash-lock + timelock |
| NUT-07 | Required | Token state check (`POST /v1/checkstate`) |

Not all Cashu mints support NUT-14. Agents and operators **must** verify mint capabilities before creating escrow tickets. Mint capabilities can be queried via `GET /v1/info` (NUT-06).

### DID-to-Pubkey Derivation

Cashu P2PK requires a secp256k1 public key. Archon DIDs are backed by secp256k1 key pairs. The derivation:

1. Agent's DID: `did:cid:bagaaiera...`
2. Resolve DID document via Archon network
3. Extract the `verificationMethod` with type `EcdsaSecp256k1VerificationKey2019`
4. The `publicKeyHex` is the P2PK lock target

```json
{
  "id": "did:cid:bagaaiera...#key-1",
  "type": "EcdsaSecp256k1VerificationKey2019",
  "controller": "did:cid:bagaaiera...",
  "publicKeyHex": "02abc123..."
}
```

This public key is used directly in the NUT-11 P2PK condition. The agent signs the Cashu redemption with the same private key that backs their DID — ensuring identity continuity between the credential system and the payment system.

### Ticket Metadata

Beyond the Cashu spending conditions, each escrow ticket carries metadata linking it to a specific task:

```json
{
  "task_schema": "hive:rebalance/v1",
  "task_params_hash": "sha256:<hash_of_task_params>",
  "danger_score": 5,
  "node_id": "03abcdef...",
  "credential_ref": "did:cid:<management_credential>",
  "issued_at": "2026-02-14T12:00:00Z",
  "deadline": "2026-02-14T18:00:00Z"
}
```

Metadata is included in the token's `memo` field or as an additional tag in the NUT-10 secret structure. The node validates that the ticket metadata matches the presented task command before executing.

---

## Detailed Protocol Flow

### Secret Generation Protocol

The HTLC preimage (`secret`) must be generated before the escrow ticket is minted. Three models are supported depending on the trust topology:

| Model | Flow | Best For |
|-------|------|----------|
| **Operator-generated** | Operator generates `secret` locally, configures the node to release it on task completion via a `secret_map` entry in the cl-hive plugin config | Single-operator fleets where operator controls the node directly |
| **Node API** | Operator calls `POST /hive/escrow/generate-secret` on the node's cl-hive RPC, receiving `H(secret)`. The node stores the secret internally and reveals it upon task completion. | Multi-operator fleets where the operator has RPC access |
| **Credential-delegated** | The management credential includes an `escrow_secret_generation` capability. The agent requests secret generation from the node as part of the task negotiation handshake. | Open marketplaces where the agent and operator coordinate remotely |

**For single-operator fleets** (the common case), the operator generates the secret locally:

```bash
# Generate a 32-byte random secret
secret=$(openssl rand -hex 32)
hash=$(echo -n "$secret" | sha256sum | cut -d' ' -f1)

# Configure the node to release this secret on task completion
# (via cl-hive plugin RPC or config file)
lightning-cli hive-escrow-register --task-id <id> --secret "$secret"
```

The operator then uses `$hash` as the HTLC lock when minting the escrow ticket.

### Happy Path: Successful Task Execution

```
Operator                Agent                  Node                   Mint
   │                      │                      │                      │
   │  1. Generate secret  │                      │                      │
   │  ───────────────────────────────────────►   │                      │
   │                      │                      │                      │
   │  2. Receive H(secret)│                      │                      │
   │  ◄───────────────────────────────────────   │                      │
   │                      │                      │                      │
   │  3. Mint ticket:     │                      │                      │
   │     P2PK(agent_pub)  │                      │                      │
   │     HTLC(H(secret))  │                      │                      │
   │     Timelock(deadline)│                     │                      │
   │     Refund(op_pub)   │                      │                      │
   │  ──────────────────────────────────────────────────────────────►   │
   │                      │                      │                      │
   │  4. Receive token    │                      │                      │
   │  ◄──────────────────────────────────────────────────────────────   │
   │                      │                      │                      │
   │  5. Send ticket      │                      │                      │
   │     + task assignment │                      │                      │
   │  ──────────────────► │                      │                      │
   │                      │                      │                      │
   │                      │  6. Present ticket   │                      │
   │                      │     + credential     │                      │
   │                      │     + task command    │                      │
   │                      │  ──────────────────► │                      │
   │                      │                      │                      │
   │                      │     7. Validate:     │                      │
   │                      │     • DID credential │                      │
   │                      │     • Ticket metadata│                      │
   │                      │     • Task vs policy │                      │
   │                      │                      │                      │
   │                      │     8. Execute task  │                      │
   │                      │                      │                      │
   │                      │  9. Signed receipt   │                      │
   │                      │     + preimage       │                      │
   │                      │  ◄────────────────── │                      │
   │                      │                      │                      │
   │                      │  10. Redeem token:   │                      │
   │                      │      sig(agent_key)  │                      │
   │                      │      + preimage      │                      │
   │                      │  ──────────────────────────────────────►   │
   │                      │                      │                      │
   │                      │  11. Sats received   │                      │
   │                      │  ◄──────────────────────────────────────   │
   │                      │                      │                      │
```

### Timeout Path: Task Not Completed

```
Operator                Agent                  Node                   Mint
   │                      │                      │                      │
   │  [Steps 1-5 same as above]                  │                      │
   │                      │                      │                      │
   │                      │  ⏰ Deadline passes  │                      │
   │                      │  without execution   │                      │
   │                      │                      │                      │
   │  6. Reclaim token:   │                      │                      │
   │     sig(operator_key)│                      │                      │
   │     (timelock expired)                      │                      │
   │  ──────────────────────────────────────────────────────────────►   │
   │                      │                      │                      │
   │  7. Sats returned    │                      │                      │
   │  ◄──────────────────────────────────────────────────────────────   │
   │                      │                      │                      │
```

### Failure Path: Task Attempted but Failed

```
Operator                Agent                  Node                   Mint
   │                      │                      │                      │
   │  [Steps 1-6 same as happy path]             │                      │
   │                      │                      │                      │
   │                      │     7. Validate ✓    │                      │
   │                      │     8. Execute task  │                      │
   │                      │        → FAILURE     │                      │
   │                      │                      │                      │
   │                      │  9. Failure receipt  │                      │
   │                      │     (NO preimage)    │                      │
   │                      │  ◄────────────────── │                      │
   │                      │                      │                      │
   │                      │  Agent cannot redeem │                      │
   │                      │  (missing preimage)  │                      │
   │                      │                      │                      │
   │  [Timelock expires, operator reclaims]      │                      │
   │                      │                      │                      │
```

---

## Ticket Types

### Single-Task Ticket

The basic unit. One ticket, one task, one payment.

**Structure:**
- One Cashu token
- P2PK locked to agent's DID pubkey
- HTLC locked to H(secret) from the target node
- Timelock set to task deadline
- Refund to operator's pubkey

**Use case:** Individual management commands (fee change, single rebalance, config adjustment).

**Example:**
```
Ticket: 100 sats
Task: hive:fee-policy/v1 — set channel 931770x2363x0 fee to 150 ppm
Deadline: 6 hours
Danger score: 3
```

### Batch Ticket

Multiple tasks, progressive secret release. The operator creates N tickets, each locked to a different HTLC hash. The node reveals secrets progressively as each task in the batch completes.

**Structure:**
- N Cashu tokens, each with:
  - Same P2PK lock (same agent)
  - Different HTLC hash: H(secret_1), H(secret_2), ..., H(secret_N)
  - Same or staggered timelocks
  - Same refund path

**Progressive release:**
```
Task 1 complete → Node reveals secret_1 → Agent redeems token_1
Task 2 complete → Node reveals secret_2 → Agent redeems token_2
...
Task N complete → Node reveals secret_N → Agent redeems token_N
```

**Use case:** Batch fee updates across 20 channels, multi-step configuration changes, sequential rebalancing operations.

**Benefit over N single tickets:** The node generates all secrets upfront in a single coordination step. The operator mints all tokens in one batch. Reduces round trips.

### Milestone Ticket

Partial payments as subtasks of a larger operation complete. Like a batch ticket, but the subtasks are phases of a single complex task rather than independent tasks.

**Structure:**
- M Cashu tokens of increasing value (reflecting increasing difficulty/risk of each milestone)
- Each locked to a different HTLC hash corresponding to a milestone checkpoint
- The node generates milestone secrets when pre-defined checkpoints are reached

**Example — Large Channel Rebalance:**
```
Milestone 1: Route found and validated → 25 sats (H(secret_route))
Milestone 2: Partial rebalance (50%) complete → 50 sats (H(secret_half))
Milestone 3: Full rebalance complete → 100 sats (H(secret_full))

Total potential: 175 sats
Minimum payout (route found but rebalance fails): 25 sats
```

**Use case:** Complex operations where partial completion has value — large rebalances, multi-hop liquidity management, channel open negotiations.

**Milestone definition:** Milestones are encoded in the task schema. The node's policy engine defines what constitutes each checkpoint.

### Performance Ticket

Base payment plus bonus, implemented as two separate tokens with different conditions.

**Structure:**
- **Base token:** Standard escrow ticket (P2PK + HTLC + timelock). Released on task completion.
- **Bonus token:** P2PK + HTLC locked to a **performance secret**. The node generates and reveals this secret only if the task outcome exceeds a defined threshold.

**Example — Fee Optimization:**
```
Base ticket: 50 sats
  HTLC: H(secret_complete) — released when fee changes are applied

Bonus ticket: 200 sats
  HTLC: H(secret_performance) — released only if 24h revenue increases >10%
  Timelock: 48 hours (allows time to measure performance)

Total potential: 250 sats
Minimum payout: 50 sats (task done, no performance improvement)
Maximum payout: 250 sats (task done + measurable improvement)
```

**Performance measurement:** The node measures the performance metric over a defined window after task completion. If the threshold is met, it publishes the performance secret (e.g., via a Nostr event, Dmail, or the next Bolt 8 message exchange).

> **⚠️ Trust assumption:** Performance tickets are NOT fully trustless. The node/operator measures and reports performance metrics — they could refuse to reveal the performance secret even if the threshold was met. The agent's recourse is limited to reputation damage (issuing a `revoke` outcome credential against the operator). For this reason, performance tickets should only be used with operators who have established reputation, and the base ticket should provide adequate compensation for the work performed regardless of bonus.

**Baseline integrity:** The performance baseline **must** be established by the node operator independently, using data from **before** the agent had any access. Specifically:
- Baseline measurement period must end before the management credential's `validFrom` date
- Baseline data must be signed by the node and included in the escrow ticket metadata
- A rolling 7-day average from the pre-credential period is recommended
- Agents must not have monitor-tier or higher access during baseline measurement

> **⚠️ First-time relationship challenge.** The "baseline must precede credential" rule creates a chicken-and-egg problem for first-time advisor-operator relationships: the operator has no prior performance data specific to this advisor, and the advisor has no track record with this node. **Recommended approach:** Introduce a **trial period** mechanism:
> - First-time engagements use a 7-day trial credential with reduced scope (monitor + standard tier only)
> - During the trial, baseline metrics are established collaboratively — both parties observe performance together
> - Trial period uses flat-fee compensation only (no performance bonus) to remove baseline manipulation incentives
> - After the trial, a full credential is issued with the trial-period metrics as the baseline
>
> This needs real-world validation: trial periods may be too conservative for time-sensitive optimizations, or operators may exploit the trial to get cheap labor before switching advisors.

**Use case:** Performance-based management contracts where the advisor's incentives align with the node's outcomes. Maps directly to the [performance-based payment model](./DID-L402-FLEET-MANAGEMENT.md#payment-models) in the fleet management spec.

---

## Danger Score Integration

Ticket value scales with the [danger score](./DID-L402-FLEET-MANAGEMENT.md#task-taxonomy--danger-scoring) from the task taxonomy. Higher danger = higher stakes = more compensation = longer escrow windows.

### Pricing by Danger Score

| Danger Range | Base Ticket Value (sats) | Escrow Window | Ticket Type |
|-------------|------------------------|---------------|-------------|
| 1–2 (Routine) | 0–5 | 1 hour | Single-task (or no escrow — simple Cashu) |
| 3–4 (Standard) | 5–25 | 2–6 hours | Single-task |
| 5–6 (Elevated) | 25–100 | 6–24 hours | Single-task or Milestone |
| 7–8 (High) | 100–500 | 24–72 hours | Milestone or Performance |
| 9–10 (Critical) | 500+ | 72+ hours | Performance + multi-sig approval |

### Escrow Window Rationale

The escrow window (timelock duration) reflects:
- **Time to execute:** Higher-danger tasks take longer (e.g., waiting for on-chain confirmations)
- **Time to verify:** Performance metrics need measurement windows
- **Time to dispute:** More time for operator review of critical actions

### Dynamic Pricing

Ticket value is modulated by agent reputation (see [Reputation Integration](#reputation-integration)):

```
ticket_value = base_value(danger_score) × reputation_modifier(agent)
```

Where `reputation_modifier` ranges from 0.7 (proven agent, discount) to 1.5 (new agent, premium). This mirrors the [mutual trust discount](./DID-L402-FLEET-MANAGEMENT.md#mutual-trust-discount) model.

---

## Reputation Integration

Agent reputation — measured via the [DID Reputation Schema](./DID-REPUTATION-SCHEMA.md) — affects escrow ticket terms in several ways:

### Escrow Duration

Higher-reputation agents get shorter escrow windows (faster payment):

| Agent Reputation | Escrow Duration Modifier | Rationale |
|-----------------|-------------------------|-----------|
| Novice (no history) | 1.5× base duration | More time for operator oversight |
| Established (>30 days) | 1.0× base duration | Standard terms |
| Proven (>90 days, good metrics) | 0.5× base duration | Trusted to execute quickly |

### Bonus Multipliers

Performance ticket bonus amounts scale with reputation:

| Agent Reputation | Bonus Multiplier | Rationale |
|-----------------|-----------------|-----------|
| Novice | 1.0× | Standard bonus available |
| Established | 1.5× | Higher bonus rewards proven track record |
| Proven | 2.0× | Maximum bonus for top performers |

### Pre-Authorization

Highly reputed agents may receive **pre-authorized tickets** — escrow tickets where the HTLC condition is relaxed for low-danger tasks:

- Danger 1–2: No HTLC, just P2PK (agent is trusted to self-report completion)
- Danger 3–4: Standard HTLC but auto-approval (no operator review)
- Danger 5+: Full escrow always applies, regardless of reputation

This maps to the [approval workflows](./DID-L402-FLEET-MANAGEMENT.md#approval-workflows) in the fleet management spec.

### Reputation from Escrow History

Completed escrow tickets become evidence for reputation credentials:

```json
{
  "type": "EscrowReceipt",
  "id": "did:cid:<receipt_credential>",
  "description": "47 escrow tickets redeemed over 30-day period, 0 timeouts, 3 bonus achievements"
}
```

This creates a virtuous cycle: good escrow history → better reputation → better escrow terms → more work → more escrow history.

---

## Mint Considerations

### Trust Model

The Cashu mint is a trusted party — it holds the backing funds and processes redemptions. For escrow tickets, mint trust is critical:

| Concern | Impact | Mitigation |
|---------|--------|-----------|
| Mint goes offline | Tokens unredeemable | Multi-mint strategy; operator maintains backup mint |
| Mint is malicious | Operator double-spends via mint collusion | Agent verifies mint reputation; use well-known mints |
| Mint censors agent | Agent can't redeem despite valid proof | Refund path also blocked; requires mint diversity |
| Mint leaks data | Privacy degradation | Cashu blind signatures prevent correlation by design |

### Acceptable Mints

The escrow protocol requires agreement on which mints are acceptable. Options:

1. **Operator's own mint** — Maximum trust for operator, minimal trust for agent. Acceptable when operator has strong reputation.
2. **Hive-endorsed mint** — A mint operated by or endorsed by the hive collective. Both parties trust the hive.
3. **Well-known public mint** — Established mints with long track records (e.g., community-run mints). Neutral third party.
4. **Agent-chosen mint** — Agent requests a specific mint. Operator must agree.

**Default:** The management credential specifies acceptable mints:

```json
{
  "compensation": {
    "model": "escrow",
    "acceptable_mints": [
      "https://mint.hive.lightning",
      "https://mint.minibits.cash"
    ],
    "preferred_mint": "https://mint.hive.lightning"
  }
}
```

### Multi-Mint Scenarios

For high-value escrow tickets, the operator can split across multiple mints to reduce single-mint risk:

```
Total escrow: 500 sats
  Mint A: 250 sats (operator's mint)
  Mint B: 250 sats (public mint)
```

Both tickets share the same HTLC hash and timelock. The agent redeems both with the same preimage. If one mint fails, the agent still receives partial payment.

> **⚠️ Atomicity challenge.** Multi-mint ticket redemption is NOT atomic — the agent redeems sequentially, and failure at one mint after success at another results in partial payment. This is an accepted tradeoff (partial payment > no payment), but it introduces edge cases:
> - If Mint A succeeds but Mint B fails permanently, the agent receives 50% — is this a "completed" task for reputation purposes?
> - If Mint B comes back online later, can the agent retry? The preimage is now public (used at Mint A), so the operator could theoretically front-run the redemption via the refund path if the timelock is close to expiry.
> - **Mitigation:** Use staggered timelocks — the secondary mint's ticket should have a longer timelock than the primary, giving the agent time to retry after primary redemption.
>
> True atomic cross-mint redemption would require a cross-mint coordination protocol (analogous to cross-chain atomic swaps), which is an open research problem in the Cashu ecosystem. For now, single-mint escrow is recommended for high-value tickets, with multi-mint reserved for risk distribution on very large amounts.

---

## Failure Modes and Edge Cases

### Task Partially Completed

**Scenario:** Agent starts a rebalance; route is found but the payment fails mid-way. The channel is in a different state than before but the rebalance didn't complete.

**Resolution:**
- For **milestone tickets**: partial milestones that were achieved can still be redeemed. The node reveals secrets for completed milestones only.
- For **single-task tickets**: the node decides success/failure. If the task's success criteria aren't met, no preimage is revealed.
- The signed receipt includes the actual outcome, enabling dispute evidence.

### Node Goes Offline Before Revealing Secret

**Scenario:** Agent sends task, node executes successfully, but node crashes before returning the receipt with the preimage.

**Resolution:**
- The node MUST persist the secret-to-task mapping before execution. On restart, it can re-issue the receipt.
- If the node is permanently offline, the agent cannot redeem. The timelock eventually expires and the operator reclaims.
- **Mitigation:** Nodes should reveal the preimage as part of an atomic execute-and-respond flow. The preimage is committed to persistent storage alongside the execution log.
- **Insurance:** For high-value tickets, the operator may issue a replacement ticket if the node's logs confirm successful execution.

### Agent Holds Preimage but Doesn't Redeem Before Timelock

**Scenario:** Agent receives the preimage but delays redemption. The timelock expires, and the operator reclaims.

**Resolution:**
- This is the agent's loss. The protocol is designed with clear deadlines.
- The escrow window should be generous enough for the agent to redeem (deadline = task_deadline + redemption_buffer).
- **Recommended buffer:** At least 1 hour between expected task completion and token timelock.
- The agent should redeem immediately upon receiving the preimage. Wallet software should automate this.

### Disputed Completion

**Scenario:** The node says the task failed (no preimage), but the agent believes the task succeeded.

**Resolution:**
- The signed receipt is the arbiter. It contains the task command, the execution result, and the node's signature.
- If the node issues a failure receipt for a task that actually succeeded, the receipt itself is evidence of bad faith.
- **Dispute flow:**
  1. Agent publishes the failure receipt + evidence of task completion (e.g., observable state change)
  2. Operator reviews and may issue a replacement ticket or direct payment
  3. If pattern repeats, agent records a `revoke` outcome in a [DID Reputation Credential](./DID-REPUTATION-SCHEMA.md) against the node operator
- **No on-chain arbitration.** This is a reputation-based system. Dishonest nodes lose agents. Dishonest agents lose contracts.

### Double-Spend Attempts

**Scenario 1: Operator double-spends the token with the mint before the agent redeems.**
- The operator would need the agent's private key OR the HTLC preimage to spend before timelock.
- Before timelock, only the agent (with preimage) can spend. The operator cannot.
- After timelock, the operator can reclaim via refund path — but this is by design.

**Scenario 2: Agent tries to redeem the same token twice.**
- Cashu mints track spent tokens. Double-redemption is rejected at the mint level.

**Scenario 3: Operator mints a ticket but the backing funds aren't real.**
- The agent can verify the token with the mint before accepting the task assignment.
- **Pre-flight check:** Agent calls `POST /v1/checkstate` (NUT-07) on the mint to verify the token is valid and unspent before starting work.

---

## Comparison with Lightning HTLC Escrow

| Property | Lightning HTLC | Cashu Escrow Ticket |
|----------|---------------|-------------------|
| **Online requirement** | Sender must be online to route | Operator mints offline; agent redeems async |
| **Routing dependency** | Payment must find a path through the network | No routing — agent talks directly to mint |
| **Privacy** | Payment amount and timing visible to routing nodes | Blind signatures; mint sees redemption but can't correlate to task |
| **Composability** | Single HTLC condition per payment | P2PK + HTLC + timelock + metadata in one token |
| **Bearer property** | Channel state; not transferable | Bearer instrument; agent holds token like cash |
| **Granularity** | Millisatoshi precision but routing fees add noise | Exact token denomination; no routing fee overhead |
| **Failure mode** | Stuck HTLCs can lock channel liquidity for hours | Token is just data; no channel liquidity impact |
| **Refund mechanism** | Timeout on-chain or via update_fail_htlc | Timelock refund path in token conditions |
| **Multi-condition** | Requires PTLCs (not yet deployed) for complex conditions | NUT-10 supports arbitrary condition composition today |

**Verdict:** For task escrow specifically, Cashu is superior. Lightning HTLCs are optimized for real-time payment routing, not conditional escrow. Cashu tokens are purpose-built for programmable bearer instruments.

---

## Privacy Properties

Cashu's blind signature scheme provides strong privacy guarantees for the escrow protocol:

### What the Mint Sees

| Event | Mint Learns |
|-------|-------------|
| Token minting | Operator requested N sats of tokens (not which task, which agent, or which node) |
| Token redemption | Someone with a valid signature + preimage redeemed a token (not who, not for what) |

### What the Mint Does NOT See

- **Task-token correlation** — Blind signatures mean the mint cannot link a minted token to a redeemed token
- **Agent identity** — The P2PK signature proves key ownership to the mint, but the mint doesn't know which DID the key belongs to
- **Task details** — Metadata is in the token structure, not exposed to the mint during minting or redemption
- **Operator-agent relationship** — The mint can't determine that a specific operator is paying a specific agent

### Privacy Boundaries

- The **operator** knows: which agent, which task, which ticket, which mint
- The **agent** knows: which operator, which task, which ticket, which mint, which node
- The **node** knows: which agent, which task, which ticket (but not mint details or payment amount unless told)
- The **mint** knows: token amounts, minting/redemption timing (but not identities or tasks)

This separation is a significant advantage over Lightning-based escrow, where routing nodes can observe payment amounts, timing, and participants.

---

## General Applicability

While this spec is motivated by Lightning fleet management, the escrow ticket pattern is universal. The [DID + Cashu Hive Settlements Protocol](./DID-HIVE-SETTLEMENTS.md) applies this escrow mechanism to nine distinct settlement types — routing revenue sharing, rebalancing costs, liquidity leases, splice settlements, pheromone markets, intelligence trading, and penalty enforcement — demonstrating the breadth of the pattern.

Any scenario with these properties is a candidate:

1. **Task delegator** wants to pay **task executor** contingent on completion
2. A **verifier** (the node, in fleet management) can objectively determine success
3. The verifier holds a secret that is only revealed on success

### Example Applications

#### Code Review

```
Operator: Software project maintainer
Agent: AI code reviewer
Node/Verifier: CI/CD pipeline

Ticket: 500 sats, locked to reviewer's DID
HTLC: H(secret) where CI pipeline holds secret
Condition: Secret revealed when all tests pass after review-suggested changes
```

#### Research Tasks

```
Operator: Research coordinator
Agent: AI research assistant
Node/Verifier: Evaluation oracle (another agent or human)

Ticket: 1000 sats, locked to researcher's DID
HTLC: H(secret) where evaluator holds secret
Condition: Secret revealed when research output meets quality criteria
```

#### Monitoring Services

```
Operator: Infrastructure owner
Agent: Monitoring service
Node/Verifier: The monitored infrastructure itself

Ticket: 10 sats/check, locked to monitor's DID
HTLC: H(secret) where infrastructure generates secret per health check
Condition: Secret revealed when check is performed and result delivered
```

#### Content Generation

```
Operator: Content platform
Agent: Content creator
Node/Verifier: Content review system

Ticket: 200 sats, locked to creator's DID
HTLC: H(secret) where review system holds secret
Condition: Secret revealed when content meets guidelines and is published
```

### Generalized Architecture

```
┌──────────────┐    ticket    ┌───────────┐   task + ticket   ┌──────────────┐
│   Delegator  │ ──────────► │  Executor  │ ────────────────► │   Verifier   │
│  (pays)      │              │ (works)    │                   │ (judges)     │
│              │              │            │ ◄──────────────── │              │
│              │              │            │  receipt+preimage │              │
│              │              │            │                   │              │
│  Reclaims    │              │  Redeems   │                   │  Holds       │
│  on timeout  │              │  on success│                   │  secret      │
└──────────────┘              └───────────┘                   └──────────────┘
```

The three roles (Delegator, Executor, Verifier) may collapse — e.g., the Delegator and Verifier might be the same entity (operator verifying their own node). The protocol remains the same.

---

## Implementation Roadmap

### Phase 1: Single-Task Tickets (2–3 weeks)
- Implement Cashu token creation with NUT-10/11/14 conditions
- DID-to-pubkey derivation utility
- Token verification (pre-flight check with mint)
- Basic escrow flow: create → assign → redeem/refund
- Integration with cl-hive plugin for task execution and preimage reveal

### Phase 2: Ticket Types (2–3 weeks)
- Batch ticket creation and progressive secret management
- Milestone ticket support with checkpoint definitions in task schemas
- Performance ticket with delayed bonus measurement
- Ticket type negotiation in management credential

### Phase 3: Mint Integration (2–3 weeks)
- Multi-mint support and mint preference negotiation
- Token validity pre-flight checks
- Automatic redemption on preimage receipt
- Refund path monitoring and notification

### Phase 4: Danger Score + Reputation Pricing (2–3 weeks)
- Dynamic ticket pricing based on danger score taxonomy
- Reputation-adjusted escrow terms
- Escrow history tracking for reputation evidence generation
- Integration with [DID Reputation Schema](./DID-REPUTATION-SCHEMA.md) evidence types

### Phase 5: General Applicability (4–6 weeks)
- Abstract the escrow protocol from fleet-management-specific code
- Generic Delegator/Executor/Verifier SDK
- Task schema registry for non-fleet domains
- Documentation and example integrations

---

## Open Questions

1. **Secret generation timing:** The node should generate the HTLC secret at ticket creation time (see [Secret Generation Protocol](#secret-generation-protocol)). Task-presentation-time generation introduces a trust gap where the agent works without knowing whether a valid secret exists.

2. **Multi-node tasks:** For tasks spanning multiple nodes (e.g., a two-node rebalance), the **destination node** generates the HTLC secret. This mirrors Lightning's receiver-generates-preimage pattern. The flow: (a) operator requests secret from destination node, (b) mints ticket with H(secret), (c) agent coordinates both nodes, (d) destination node reveals secret upon successful completion. For N-node tasks, a single designated verifier node generates the secret. The verifier is specified in the ticket metadata as `verifier_node_id`.

3. **Token denomination:** Should escrow tickets use fixed denominations (powers of 2, like standard Cashu) or exact amounts? Fixed denominations improve privacy at the cost of over/under-payment. Exact amounts improve accounting at the cost of privacy.

4. **Partial redemption:** If an agent partially completes a task (not enough for a milestone), should there be a mechanism for partial preimage reveal? This adds protocol complexity but improves fairness.

5. **Offline verification:** Can a node verify a Cashu token's validity without contacting the mint? This matters for air-gapped or intermittently connected nodes. Current Cashu requires mint contact for verification.

6. **Cross-mint atomic redemption:** For multi-mint tickets, can the agent atomically redeem across mints? Failure at one mint after success at another creates partial payment. Is this acceptable?

7. **Arbitration evolution:** The current design uses reputation as the dispute resolution mechanism. Should there be a formal arbitration protocol for high-value disputes? (e.g., a panel of DIDs votes on disputed receipts.)

---

## References

- [Cashu NUT-10: Spending Conditions](https://github.com/cashubtc/nuts/blob/main/10.md)
- [Cashu NUT-11: Pay-to-Public-Key (P2PK)](https://github.com/cashubtc/nuts/blob/main/11.md)
- [Cashu NUT-14: Hashed Timelock Contracts](https://github.com/cashubtc/nuts/blob/main/14.md)
- [Cashu Protocol](https://cashu.space/)
- [DID+L402 Remote Fleet Management](./DID-L402-FLEET-MANAGEMENT.md)
- [DID + Cashu Hive Settlements Protocol](./DID-HIVE-SETTLEMENTS.md)
- [DID Reputation Schema](./DID-REPUTATION-SCHEMA.md)
- [Archon Reputation Schemas (canonical)](https://github.com/archetech/schemas/tree/main/credentials/reputation/v1)
- [W3C DID Core 1.0](https://www.w3.org/TR/did-core/)
- [W3C Verifiable Credentials Data Model 2.0](https://www.w3.org/TR/vc-data-model-2.0/)
- [Archon: Decentralized Identity for AI Agents](https://github.com/archetech/archon)
- [DID Hive Marketplace Protocol](./DID-HIVE-MARKETPLACE.md) — Marketplace trial periods reference this spec's escrow and baseline mechanisms
- [Lightning Hive: Swarm Intelligence for Lightning](https://github.com/lightning-goats/cl-hive)

---

*Feedback welcome. File issues on [cl-hive](https://github.com/lightning-goats/cl-hive) or discuss in #singularity.*

*— Hex ⬡*
