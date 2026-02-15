# cl-hive-comms: Communication & Transport Plugin

**Status:** Design Document  
**Version:** 0.1.0  
**Author:** Hex (`did:cid:bagaaierajrr7k6izcrdfwqxpgtrobflsv5oibymfnthjazkkokaugszyh4ka`)  
**Date:** 2026-02-15  
**Source Specs:** [DID-HIVE-CLIENT](../planning/DID-HIVE-CLIENT.md), [DID-L402-FLEET-MANAGEMENT](../planning/DID-L402-FLEET-MANAGEMENT.md), [DID-NOSTR-MARKETPLACE](../planning/DID-NOSTR-MARKETPLACE.md), [DID-CASHU-TASK-ESCROW](../planning/DID-CASHU-TASK-ESCROW.md)

---

## Overview

`cl-hive-comms` is the **entry-point plugin** for the Lightning Hive protocol suite. It is a standalone CLN plugin that provides transport, marketplace access, payment management, policy enforcement, and credential verification for any Lightning node operator — without requiring hive membership, bonds, or additional plugins.

**Install this one plugin. Access everything.**

- Hire AI or human advisors for fee optimization, rebalancing, channel management
- Access the full liquidity marketplace (leasing, JIT, swaps, insurance)
- Publish and discover services on the Nostr marketplace
- Enforce local policy as the last line of defense against malicious advisors
- Pay advisors via Bolt11, Bolt12, L402, or Cashu escrow
- Maintain a tamper-evident audit trail of all management actions

**Zero configuration required.** On first run, the plugin auto-generates a Nostr keypair, connects to relays, and is ready to receive advisor commands.

---

## Relationship to Other Plugins

```
┌──────────────────────────────────────────────────────┐
│                    cl-hive (coordination)              │
│  Gossip, topology, settlements, fleet advisor         │
│  Requires: cl-hive-comms                              │
├──────────────────────────────────────────────────────┤
│                    cl-hive-archon (identity)           │
│  DID generation, credentials, dmail, vault            │
│  Requires: cl-hive-comms                              │
├──────────────────────────────────────────────────────┤
│              ➤ cl-hive-comms (transport) ◄            │
│  Nostr DM + REST/rune transport, subscriptions,       │
│  marketplace publishing, payment, policy engine       │
│  Standalone — no dependencies on other hive plugins   │
├──────────────────────────────────────────────────────┤
│                    cl-revenue-ops (existing)           │
│  Local fee policy, profitability analysis             │
│  Standalone — independent of hive plugins             │
└──────────────────────────────────────────────────────┘
```

| Plugin | Relationship |
|--------|-------------|
| **cl-hive-archon** | Optional. Adds DID identity, credential verification upgrade, vault backup. Registers dmail as an additional transport. |
| **cl-hive** | Optional. Adds gossip protocol, topology planning, settlements, fleet coordination. Registers hive-specific message handlers. |
| **cl-revenue-ops** | Independent. Existing fee policy tool. Can be managed by advisors via cl-hive-comms. |

**What cl-hive-comms provides to other plugins:**
- Transport abstraction API (register handlers for new message types)
- Nostr connection sharing (DM transport + marketplace use same WebSocket)
- Payment Manager API (method selection, spending limit enforcement)
- Policy Engine hooks (register custom policy rules)
- Receipt Store API (append receipts, query history)
- Identity context (Nostr keypair, alias registry)

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  cl-hive-comms                                                │
│                                                               │
│  ┌─────────────┐  ┌────────────┐  ┌───────────────────────┐ │
│  │ Transport    │  │ Nostr Mkt  │  │ Subscription Manager │ │
│  │ Abstraction  │  │ Publisher  │  │                      │ │
│  │              │  │ (38380+/   │  │                      │ │
│  │ ┌──────────┐ │  │  38900+)   │  │                      │ │
│  │ │Nostr DM  │ │  └────────────┘  └───────────────────────┘ │
│  │ │(NIP-44)  │ │                                             │
│  │ │(primary) │ │  ┌──────────┐  ┌──────────────────┐       │
│  │ ├──────────┤ │  │ Payment  │  │ Policy Engine    │       │
│  │ │REST/rune │ │  │ Manager  │  │ (local overrides)│       │
│  │ │(secondary│ │  └──────────┘  └──────────────────┘       │
│  │ └──────────┘ │                                             │
│  └─────────────┘  ┌──────────────┐  ┌───────────────────┐   │
│                    │ Credential   │  │ Receipt Store     │   │
│  ┌─────────────┐  │ Verifier     │  │ (tamper-evident)  │   │
│  │ Cashu       │  │ (Nostr-only) │  │                   │   │
│  │ Escrow      │  └──────────────┘  └───────────────────┘   │
│  │ Wallet      │                                              │
│  └─────────────┘  ┌──────────────────────────────────────┐   │
│                    │ Identity (auto-gen Nostr keypair)    │   │
│                    │ + Alias Registry                     │   │
│                    └──────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
```

---

## Components

### 1. Transport Abstraction Layer

A pluggable transport system so new transports can be added without touching other components.

| Transport | Role | Status |
|-----------|------|--------|
| **Nostr DM (NIP-44)** | Primary — all node↔advisor communication | ✓ Initial |
| **REST/rune** | Secondary — direct low-latency control, relay-down fallback | ✓ Initial |
| **Bolt 8** | Future P2P encrypted messaging | Deferred |
| **Archon Dmail** | High-value comms (requires cl-hive-archon) | Deferred |

Other plugins register handlers with `cl-hive-comms`:

```python
# cl-hive-archon registers dmail transport
comms.register_transport("dmail", DmailTransport(archon_gateway))

# cl-hive registers gossip message handlers
comms.register_handler("hive:gossip/*", hive_gossip_handler)
```

**Message format** uses TLV payloads regardless of transport:

```
TLV Payload:
  [1] schema_type    : utf8     (e.g., "hive:fee-policy/v1")
  [3] schema_payload : json     (the actual command)
  [5] credential     : bytes    (Nostr signature or serialized VC)
  [7] payment_proof  : bytes    (L402 macaroon OR Cashu token)
  [9] signature      : bytes    (agent's signature over [1]+[3])
  [11] nonce         : u64      (replay protection)
  [13] timestamp     : u64      (unix epoch seconds)
```

### 2. Nostr Marketplace Publisher

Handles publishing and subscribing to Nostr marketplace events using the same WebSocket connection as DM transport.

**Advisor services:** kinds `38380–38389`  
**Liquidity services:** kinds `38900–38909`

| Kind Offset | Purpose | Advisor Kind | Liquidity Kind |
|-------------|---------|-------------|----------------|
| +0 | Provider/Service Profile | 38380 | 38900 |
| +1 | Offer | 38381 | 38901 |
| +2 | RFP (demand broadcast) | 38382 | 38902 |
| +3 | Contract Confirmation | 38383 | 38903 |
| +4 | Heartbeat/Status | 38384 | 38904 |
| +5 | Reputation Summary | 38385 | 38905 |

Supports dual-publishing to NIP-99 (kind 30402) and NIP-15 (kinds 30017/30018) for maximum interoperability with existing Nostr marketplace clients.

### 3. Subscription Manager

Tracks active advisor and liquidity contracts, manages trial periods, handles renewal and termination.

### 4. Payment Manager

Coordinates across all four payment methods based on context:

| Method | Use Case | Requires |
|--------|----------|---------|
| **Bolt11** | Simple per-action payments, one-time fees | Node's Lightning wallet |
| **Bolt12** | Recurring subscriptions | CLN native Bolt12 |
| **L402** | API-style access, subscription macaroons | Built-in L402 client |
| **Cashu** | Conditional escrow (payment-on-completion) | Built-in Cashu wallet |

**Method selection logic:**

```
Is this a conditional payment (escrow)?
  YES → Cashu (only option for conditional spending conditions)
  NO  → Use operator's preferred method:
        ├─ Subscription? → Bolt12 offer (if supported) or Bolt11
        ├─ Per-action?   → Bolt11 invoice or L402 macaroon
        └─ Flat fee?     → Bolt11 invoice
```

**Spending limits** enforced across all methods:

| Limit | Default | Configurable |
|-------|---------|-------------|
| Per-action cap | None (danger-score pricing) | Yes |
| Daily cap | 50,000 sats | Yes |
| Weekly cap | 200,000 sats | Yes |
| Per-advisor daily cap | 25,000 sats | Yes |

### 5. Cashu Escrow Wallet

Built-in Cashu wallet implementing NUT-10/11/14 for conditional escrow payments:

- **P2PK lock** — Tokens locked to advisor's public key
- **HTLC** — Hash-locked; node reveals preimage only on successful task completion
- **Timelock** — Auto-refund to operator if task not completed by deadline
- **Auto-replenishment** — Mints new tokens when escrow balance drops below threshold

Supports single-task tickets, batch tickets, milestone tickets, and performance tickets per the [Task Escrow spec](../planning/DID-CASHU-TASK-ESCROW.md).

### 6. Policy Engine

The operator's **last line of defense**. Even with valid credentials and payment, the Policy Engine can reject any action.

#### Default Presets

| Preset | Max Fee Change/24h | Max Rebalance | Forbidden Actions | Confirmation Required |
|--------|-------------------|--------------|-------------------|----------------------|
| `conservative` | ±15% per channel | 100k sats | Channel close, force close, wallet send, plugin start | Danger ≥ 5 |
| `moderate` | ±30% per channel | 500k sats | Force close, wallet sweep, plugin start (unapproved) | Danger ≥ 7 |
| `aggressive` | ±50% per channel | 2M sats | Wallet sweep, force close all | Danger ≥ 9 |

#### Custom Rules

```json
{
  "policy_version": 1,
  "preset": "moderate",
  "overrides": {
    "max_fee_change_per_24h_pct": 25,
    "max_rebalance_sats": 300000,
    "max_rebalance_fee_ppm": 500,
    "forbidden_peers": ["03badpeer..."],
    "protected_channels": ["931770x2363x0"],
    "required_confirmation": {
      "danger_gte": 6,
      "channel_close": "always",
      "onchain_send_gte_sats": 50000
    },
    "rate_limits": {
      "fee_changes_per_hour": 10,
      "rebalances_per_day": 20,
      "total_actions_per_day": 100
    },
    "time_restrictions": {
      "quiet_hours": { "start": "23:00", "end": "07:00", "timezone": "UTC" },
      "quiet_hour_max_danger": 2
    }
  }
}
```

#### Confirmation Flow

When the Policy Engine requires operator approval:

1. Action is held pending
2. Operator notified via configured channels (webhook, Nostr DM)
3. Operator approves/rejects via RPC (`hive-client-approve`)
4. Pending confirmations expire after configurable timeout (default: 24h for danger 5–6, 4h for danger 7–8)

#### Alert Integration

| Alert Level | Trigger | Channels |
|------------|---------|----------|
| **info** | Danger 1–2 actions | Daily digest |
| **notice** | Danger 3–4 | Real-time: webhook |
| **warning** | Danger 5–6 | Webhook + Nostr DM |
| **critical** | Danger 7+ | Webhook + Nostr DM + email |
| **confirmation** | Action requires approval | All channels |

#### Policy Overrides (Temporary)

```bash
# Tighten during maintenance
lightning-cli hive-client-policy --override='{"max_danger": 2}' --duration="4h"

# Loosen for specific operation
lightning-cli hive-client-policy --override='{"max_rebalance_sats": 2000000}' --duration="1h"

# Remove override
lightning-cli hive-client-policy --clear-override
```

Overrides auto-expire to prevent "forgot to undo" scenarios.

### 7. Credential Verifier (Nostr-Only Mode)

Without `cl-hive-archon`, verification operates in Nostr-only mode:

1. **Nostr signature verification** — Command signed by advisor's Nostr pubkey
2. **Scope check** — Credential grants required permission tier
3. **Constraint check** — Parameters within credential constraints (`max_fee_change_pct`, `max_rebalance_sats`, etc.)
4. **Replay protection** — Monotonic nonce per agent pubkey; timestamp within ±5 minutes

When `cl-hive-archon` is installed, this upgrades to full DID verification (DID resolution, VC signature check, revocation check with fail-closed on Archon unreachable).

### 8. Receipt Store

Append-only, hash-chained log of all management actions:

```json
{
  "receipt_id": 47,
  "prev_hash": "sha256:<hash_of_receipt_46>",
  "timestamp": "2026-02-14T12:34:56Z",
  "agent_did": "did:cid:<agent_did>",
  "schema": "hive:fee-policy/v1",
  "action": "set_anchor",
  "params": { "channel_id": "931770x2363x0", "target_fee_ppm": 150 },
  "result": "success",
  "state_hash_before": "sha256:<before>",
  "state_hash_after": "sha256:<after>",
  "agent_signature": "<sig>",
  "node_signature": "<sig>",
  "receipt_hash": "sha256:<hash_of_this_receipt>"
}
```

- **Hash chaining** — Modifying any receipt breaks the chain
- **Dual signatures** — Both agent and node sign each receipt
- **Periodic merkle roots** — Hourly/daily roots for efficient auditing
- **SQLite storage** with export capability

### 9. Identity & Alias Registry

**Auto-generated Nostr keypair on first run.** Stored in `~/.lightning/cl-hive-comms/`. No configuration needed.

**Alias registry** maps human-readable names to identifiers:

| Source | Priority | Example |
|--------|----------|---------|
| Local aliases | 1 (highest) | `lightning-cli hive-client-alias set hex-advisor "did:cid:..."` |
| Profile display names | 2 | From advisor's `HiveServiceProfile.displayName` |
| Auto-generated | 3 | `"advisor-1"`, `"advisor-2"` |

All CLI commands accept names, not DIDs:

```bash
lightning-cli hive-client-authorize "Hex Fleet Advisor" --access="fee optimization"
lightning-cli hive-client-revoke "Bad Advisor"
```

---

## RPC Commands

All commands accept **advisor names, aliases, or discovery indices** — not DIDs. DIDs accepted via `--advisor-did` for advanced use.

| Command | Description | Example |
|---------|-------------|---------|
| `hive-client-status` | Active advisors, spending, policy, liquidity contracts | `lightning-cli hive-client-status` |
| `hive-client-authorize` | Grant an advisor access to your node | `lightning-cli hive-client-authorize "Hex Advisor" --access="fees"` |
| `hive-client-revoke` | Immediately revoke an advisor's access | `lightning-cli hive-client-revoke "Hex Advisor"` |
| `hive-client-discover` | Find advisors or liquidity providers | `lightning-cli hive-client-discover --capabilities="fee optimization"` |
| `hive-client-policy` | View or modify local policy | `lightning-cli hive-client-policy --preset=moderate` |
| `hive-client-payments` | View payment balance and spending | `lightning-cli hive-client-payments` |
| `hive-client-trial` | Start or review a trial period | `lightning-cli hive-client-trial "Hex Advisor" --days=14` |
| `hive-client-alias` | Set a friendly name for an advisor | `lightning-cli hive-client-alias set "Hex" "did:cid:..."` |
| `hive-client-identity` | View or manage node identity | `lightning-cli hive-client-identity` |
| `hive-client-receipts` | List management action receipts | `lightning-cli hive-client-receipts --advisor="Hex Advisor"` |
| `hive-client-approve` | Approve/reject a pending action | `lightning-cli hive-client-approve --action-id=47` |
| `hive-client-lease` | Lease liquidity from a provider | `lightning-cli hive-client-lease "BigNode" --capacity=5000000 --days=30` |
| `hive-client-jit` | Request JIT liquidity | `lightning-cli hive-client-jit "FlashChannel" --capacity=2000000` |
| `hive-client-liquidity-status` | View active liquidity contracts | `lightning-cli hive-client-liquidity-status` |
| `hive-client-marketplace-publish` | Publish service profile to Nostr | `lightning-cli hive-client-marketplace-publish --type advisor` |
| `hive-comms-import-key` | Import existing Nostr key | `lightning-cli hive-comms-import-key --nsec="nsec1..."` |

### Example Output

```bash
$ lightning-cli hive-client-status

Hive Client Status
━━━━━━━━━━━━━━━━━
Identity: my-node (auto-provisioned)
Policy: moderate

Active Advisors:
  Hex Fleet Advisor
    Access: fee optimization
    Since: 2026-02-14 (30 days remaining)
    Actions: 87 taken, 0 rejected
    Spending: 2,340 sats this month

Active Liquidity:
  BigNode Liquidity — lease — 5M inbound — 23 days left — 3,600 sats

Payment Balance:
  Escrow (Cashu): 7,660 sats
  This month's spend: 5,940 sats (limit: 50,000)
```

### Discovery Output

```bash
$ lightning-cli hive-client-discover --capabilities="fee optimization"

Found 5 advisors:

#  Name                  Rating  Nodes  Price         Specialties
─  ────                  ──────  ─────  ─────         ───────────
1  Hex Fleet Advisor     ★★★★★   12     3k sats/mo    fee optimization, rebalancing
2  RoutingBot Pro        ★★★★☆   8      5k sats/mo    fee optimization
3  LightningTuner        ★★★☆☆   3      2k sats/mo    fee optimization, monitoring
4  NodeWhisperer         ★★★★☆   22     8k sats/mo    full-stack management
5  FeeHawk AI            ★★★☆☆   5      per-action    fee optimization

Trial available: #1, #2, #3, #5

Use: lightning-cli hive-client-authorize <number> --access="fee optimization"
```

### Credential Templates

| User Types | Maps To | Schemas |
|-----------|---------|---------|
| `"monitoring"` / `"read only"` | `monitor_only` | `hive:monitor/*` |
| `"fee optimization"` / `"fees"` | `fee_optimization` | `hive:monitor/*`, `hive:fee-policy/*` |
| `"full routing"` / `"routing"` | `full_routing` | `hive:monitor/*`, `hive:fee-policy/*`, `hive:rebalance/*`, `hive:config/*` |
| `"full management"` / `"everything"` | `complete_management` | All except `hive:channel/close_*`, `hive:emergency/force_close_*` |

---

## Configuration Reference

All settings are optional. **Zero configuration required for first run.**

```ini
# ~/.lightning/config (CLN config file)

# === Transport (Nostr DM — primary) ===
# hive-comms-nostr-relays=wss://nos.lol,wss://relay.damus.io     # defaults
# hive-comms-nsec=nsec1...                # Only if importing existing key
                                           # Otherwise auto-generated on first run

# === Transport (REST/rune — secondary) ===
# hive-comms-rest-enabled=true             # default: true
# hive-comms-rest-port=9737                # default: 9737

# === Payment ===
hive-comms-payment-methods=bolt11,bolt12   # preference order
hive-comms-escrow-method=cashu
hive-comms-escrow-mint=https://mint.minibits.cash
# hive-comms-escrow-backup-mints=          # comma-separated backup mints
# hive-comms-escrow-replenish-threshold=1000   # sats
# hive-comms-escrow-replenish-amount=5000      # sats
# hive-comms-escrow-auto-replenish=true

# === Spending Limits ===
hive-comms-daily-limit=50000               # sats
hive-comms-weekly-limit=200000             # sats
# hive-comms-per-advisor-daily-limit=25000

# === Policy ===
hive-comms-policy-preset=moderate          # conservative | moderate | aggressive
# hive-comms-policy-file=                  # path to custom policy JSON

# === Marketplace ===
hive-comms-marketplace-publish=true        # Publish Nostr events (38380+/38900+)
# hive-comms-marketplace-dual-nip99=true   # Also publish as NIP-99 (kind 30402)
# hive-comms-marketplace-dual-nip15=false  # Also publish as NIP-15 (kinds 30017/30018)
# hive-comms-marketplace-pow-bits=20       # NIP-13 proof of work

# === Alerts ===
# hive-comms-alert-nostr-dm=npub1abc...
# hive-comms-alert-webhook=https://hooks.example.com/hive
# hive-comms-alert-email=operator@example.com
```

---

## Installation

### Minimum Setup (Zero Config)

```bash
# Install and start — that's it
lightning-cli plugin start /path/to/cl_hive_comms.py
```

On first run:
1. Nostr keypair auto-generated, stored in `~/.lightning/cl-hive-comms/`
2. Connects to default Nostr relays
3. Creates data directory and SQLite databases
4. REST/rune transport enabled on default port
5. Policy preset defaults to `moderate`
6. Ready to accept advisor connections

### Permanent Installation

Add to CLN config:

```ini
plugin=/path/to/cl_hive_comms.py
```

### Requirements

- **CLN ≥ v24.08**
- **Python 3.10+** with dependencies (bundled or pip-installable)
- No Archon node required
- No DID setup required
- No manual key management

---

## Standalone Operation

`cl-hive-comms` is fully functional without `cl-hive-archon` or `cl-hive`:

| Feature | cl-hive-comms only | + cl-hive-archon | + cl-hive |
|---------|-------------------|-----------------|-----------|
| Nostr DM transport | ✓ | ✓ | ✓ |
| REST/rune transport | ✓ | ✓ | ✓ |
| Marketplace publishing | ✓ | ✓ | ✓ |
| Advisor management | ✓ | ✓ | ✓ |
| Liquidity marketplace | ✓ | ✓ | ✓ |
| Policy Engine | ✓ | ✓ | ✓ |
| Receipt Store | ✓ | ✓ | ✓ |
| Credential verification | Nostr-only | Full DID | Full DID |
| DID identity | ✗ | ✓ | ✓ |
| Vault backup | ✗ | ✓ | ✓ |
| Gossip protocol | ✗ | ✗ | ✓ |
| Settlement netting | ✗ | ✗ | ✓ |
| Fleet rebalancing | ✗ | ✗ | ✓ |
| Bond requirement | None | None | 50k–500k sats |

---

## Onboarding: Three-Command Quickstart

```bash
# 1. Install
lightning-cli plugin start /path/to/cl_hive_comms.py

# 2. Find an advisor
lightning-cli hive-client-discover --capabilities="fee optimization"

# 3. Hire them
lightning-cli hive-client-authorize 1 --access="fee optimization"
```

Done. Node is professionally managed. Behind the scenes: identity auto-provisioned, credentials issued, payment method negotiated, trial period started.

---

## Security

### Defense in Depth

Three independent validation layers — all must pass:

1. **Credential** — Is this agent authorized? Valid signature, unexpired, unrevoked?
2. **Payment** — Has the agent paid? Valid Cashu token, L402 macaroon, or invoice?
3. **Policy** — Does local policy allow this action regardless of credential scope?

### What Advisors Can Never Do

- Access private keys, seed phrases, or HSM secrets
- Modify client software or configuration
- Bypass the Policy Engine
- Access other advisors' credentials
- Persist access after revocation

### Replay Protection

- Monotonically increasing nonce per agent
- Timestamp within ±5 minutes
- Commands with stale nonces rejected

### Transport Security

- **Nostr DM (NIP-44)** — End-to-end encrypted
- **REST/rune** — CLN rune-based authentication
- No cleartext management traffic

---

## Implementation Roadmap

| Phase | Scope | Timeline |
|-------|-------|----------|
| 1 | Core transport (Nostr DM + REST/rune), Schema Handler, Nostr keypair auto-gen, basic Policy Engine (presets), Receipt Store, Bolt11 payment, marketplace publishing | 4–6 weeks |
| 2 | Cashu escrow wallet (NUT-10/11/14), Bolt12 offers, L402 client, payment method negotiation, spending limits | 3–4 weeks |
| 3 | Full schema coverage (15 categories), capability advertisement, danger score integration | 3–4 weeks |
| 4 | Discovery pipeline (Nostr + Archon + directories), trial periods, onboarding wizard | 3–4 weeks |
| 5 | Custom policy rules, confirmation flow, alert integration, quiet hours | 2–3 weeks |
| 6 | Multi-advisor coordination, conflict detection, hive membership upgrade flow | 2–3 weeks |

---

## References

- [DID Hive Client](../planning/DID-HIVE-CLIENT.md) — Full client architecture
- [DID + L402 Fleet Management](../planning/DID-L402-FLEET-MANAGEMENT.md) — Schema definitions, danger scoring
- [DID + Cashu Task Escrow](../planning/DID-CASHU-TASK-ESCROW.md) — Escrow ticket format
- [DID Nostr Marketplace](../planning/DID-NOSTR-MARKETPLACE.md) — Nostr event kinds, relay strategy
- [DID Hive Marketplace](../planning/DID-HIVE-MARKETPLACE.md) — Service profiles, discovery, contracting
- [DID Hive Liquidity](../planning/DID-HIVE-LIQUIDITY.md) — Liquidity-as-a-service marketplace

---

*Feedback welcome. File issues on [cl-hive](https://github.com/lightning-goats/cl-hive) or discuss in #singularity.*

*— Hex ⬡*
