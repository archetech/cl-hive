# DID Hive Client: Universal Lightning Node Management

**Status:** Proposal / Design Draft  
**Version:** 0.1.0  
**Author:** Hex (`did:cid:bagaaierajrr7k6izcrdfwqxpgtrobflsv5oibymfnthjazkkokaugszyh4ka`)  
**Date:** 2026-02-14  
**Feedback:** Open — file issues or comment in #singularity

---

## Abstract

This document specifies lightweight client software — a CLN plugin (`cl-hive-client`) and an LND companion daemon (`hive-lnd`) — that enables **any** Lightning node to contract for professional management services from advisors. The client implements the management interface defined in the [Fleet Management](./DID-L402-FLEET-MANAGEMENT.md) spec without requiring hive membership, bonds, gossip participation, or the full `cl-hive` plugin.

The result: every Lightning node operator — from a hobbyist running a Raspberry Pi to a business with a multi-BTC routing node — can hire AI-powered or human expert advisors for fee optimization, rebalancing, and channel management. **Install the plugin, pick an advisor, approve access, done.** The client enforces local policy as the last line of defense against malicious or incompetent advisors. No trust required.

### Design Principles

Two principles govern the user experience:

1. **Cryptographic identity is plumbing.** The protocol uses Archon DIDs for authentication, W3C Verifiable Credentials for authorization, and secp256k1 signatures for everything. The operator never sees any of it. DIDs are auto-provisioned on first run. Credentials are issued by clicking "authorize." Signatures happen silently. Like TLS — essential infrastructure that users never think about.

2. **Payment flexibility is mandatory.** Advisors accept payment via standard Lightning invoices (Bolt11), recurring offers (Bolt12), API-gated access (L402), and conditional escrow (Cashu). The operator picks their preferred method. Only conditional escrow (payment-on-completion) specifically requires Cashu tokens. Everything else uses whatever Lightning payment method the operator and advisor agree on.

---

## Design Principles

### DID Transparency

DIDs are the cryptographic foundation but **must be invisible to end users**. The onboarding experience is "install plugin, pick an advisor, approve" — not "create a DID, resolve credentials, issue a VC." Specifically:

- **Auto-provisioning:** On first run, if no DID exists, the client automatically creates one via the configured Archon gateway. Zero user action required.
- **Human-readable names:** Advisors are shown by `displayName` (e.g., "Hex Fleet Advisor"), not DID strings. Node identity uses the Lightning node's alias.
- **Alias system:** The client maintains a local alias map (`advisor_name → DID`). All CLI commands accept aliases: `hive-client-authorize --advisor="Hex Fleet Advisor"`.
- **Transparent credential management:** "Authorize this advisor" and "revoke access" — not "issue VC" or "revoke credential."
- **Technical details hidden by default:** `hive-client-status` shows advisor names, contract status, and escrow balance. DID strings only appear with `--verbose` or `--technical` flags.

### Archon Integration Tiers

The client supports three Archon deployment tiers with graceful degradation:

#### Tier 1: No Archon Node (Default)

- **Setup:** Zero. DID auto-provisioned via public gatekeeper (`archon.technology`).
- **How it works:** On first run, the client calls the public Archon gateway to create a DID. All DID resolution, credential verification, and revocation checks go through the public gateway.
- **Tradeoffs:** Depends on public infrastructure availability; slightly slower operations; trusts the public gatekeeper for DID resolution.
- **Best for:** Non-technical operators, quick start, hobbyists.

```ini
# Default config — no Archon node needed
hive-client-archon-gateway=https://archon.technology
hive-client-archon-auto-provision=true
```

#### Tier 2: Own Archon Node (Encouraged)

- **Setup:** Run local Archon node (`docker compose up` in `~/bin/archon`).
- **How it works:** All DID operations are local. No external dependency for identity management.
- **Tradeoffs:** Requires running 14 Docker containers; more infrastructure to maintain; full sovereignty.
- **Best for:** Serious operators, businesses, privacy-focused users.

```ini
# Local Archon node
hive-client-archon-gateway=http://localhost:4224
hive-client-archon-auto-provision=true
```

#### Tier 3: Archon Behind L402 (Future)

- **Setup:** Same as Tier 1, but the public gatekeeper gates services behind L402.
- **How it works:** DID operations require L402 payment. The client's `L402AccessCredential` (from the [Fleet Management spec](./DID-L402-FLEET-MANAGEMENT.md)) applies here too — the same payment infrastructure that gates fleet management API access can gate identity services.
- **Tradeoffs:** Per-operation cost; ensures sustainable public infrastructure; natural upgrade incentive to Tier 2.
- **Best for:** Scaling public infrastructure sustainably.

```ini
# Public gateway with L402
hive-client-archon-gateway=https://archon.technology
hive-client-archon-l402=true
hive-client-archon-l402-budget-sats=1000
```

#### Graceful Degradation

The client tries Archon endpoints in order: local node → public gateway → cached credentials. If all fail, the client operates in **degraded mode**: existing credentials are honored (cached), but new credential issuance and revocation checks fail-closed (deny new commands from unverifiable credentials).

### Payment Flexibility

The client handles the full payment stack, not just Cashu:

| Method | Use Case | Client Component |
|--------|----------|-----------------|
| **Cashu tokens** | Escrow (conditional payments), bearer micropayments | Built-in Cashu wallet (NUT-10/11/14) |
| **Bolt11 invoices** | Simple per-action payments, one-time fees | Lightning node's native invoice handling |
| **Bolt12 offers** | Recurring subscriptions | Lightning node's offer handling (CLN native, LND experimental) |
| **L402** | API-style access, subscription macaroons | Built-in L402 client |

The Escrow Manager described in this spec handles Cashu-specific operations. The broader Payment Manager coordinates across all four methods based on the advisor's accepted payment methods and the contract terms.

---

## Motivation

### The Total Addressable Market

The existing protocol suite assumes hive membership. Hive membership requires:
- Running the full `cl-hive` plugin
- Posting a bond (50,000–500,000 sats)
- Participating in gossip, settlement, and PKI protocols
- Maintaining ongoing obligations to other hive members

This is appropriate for sophisticated operators who want the full benefits of fleet coordination. But it limits the addressable market to operators willing to commit capital, infrastructure, and social participation.

The Lightning Network has **~15,000 publicly visible nodes** and an unknown number of private nodes. Most are unmanaged or self-managed with default settings. The operators fall into three categories:

| Category | Estimated Count | Current State | Willingness to Join a Hive |
|----------|----------------|---------------|---------------------------|
| Hobbyist operators | ~8,000 | Default fees, minimal optimization | Low (too complex, too much commitment) |
| Semi-professional | ~5,000 | Some manual tuning, basic monitoring | Medium (interested but barrier is high) |
| Professional routing nodes | ~2,000 | Active management, custom tooling | High (already sophisticated) |

The hive targets the professional tier (~2,000 nodes). The client targets **everyone** — lowering the barrier from "join a cooperative and post bonds" to "install a plugin and hire an advisor."

### The Value Proposition

**For node operators:**
- Professional management without learning routing optimization
- Pay-per-action or subscription pricing — no bond, no ongoing hive obligations
- Local policy engine ensures the advisor can never exceed operator-defined limits
- Try before you commit — trial periods with reduced scope
- Upgrade path to full hive membership if desired

**For advisors:**
- Access to the entire Lightning node market, not just hive members
- Build verifiable reputation across a larger client base
- Specialize and compete on merit
- No requirement to operate a Lightning node themselves (just need a DID and expertise)

**For the hive ecosystem:**
- Client nodes are the funnel for hive membership
- Advisors serving client nodes generate reputation that benefits the marketplace
- Revenue from client management fees funds hive development
- Network effects: more managed nodes → better routing intelligence → better management → more nodes

### Why Two Implementations

Lightning has two dominant implementations: CLN and LND. They share the Lightning protocol but differ in everything else — language, architecture, API surface, plugin model, configuration format. A single client implementation cannot serve both.

| Property | CLN | LND |
|----------|-----|-----|
| Language | C (core), Python (plugins) | Go |
| Plugin model | Dynamic plugins via JSON-RPC | Companion daemons via gRPC |
| Custom messages | `sendcustommsg` / `custommsg` hook | `SendCustomMessage` / `SubscribeCustomMessages` |
| Configuration | `config` file, command-line flags | `lnd.conf`, command-line flags |
| Extension convention | Python plugin, single file | Go binary, YAML/TOML config |

Building both `cl-hive-client` (Python, CLN plugin) and `hive-lnd` (Go, LND daemon) ensures the entire Lightning network can participate.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                         CLIENT NODE                                   │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │              cl-hive-client (CLN) / hive-lnd (LND)              │ │
│  │                                                                  │ │
│  │  ┌──────────┐ ┌────────────┐ ┌──────────┐ ┌──────────────────┐ │ │
│  │  │ Schema   │ │ Credential │ │ Payment  │ │ Policy Engine    │ │ │
│  │  │ Handler  │ │ Verifier   │ │ Manager  │ │ (local overrides)│ │ │
│  │  └────┬─────┘ └─────┬──────┘ └────┬─────┘ └───────┬──────────┘ │ │
│  │       │              │              │               │            │ │
│  │  ┌────▼──────────────▼──────────────▼───────────────▼──────────┐ │ │
│  │  │                    Receipt Store                             │ │ │
│  │  │  (tamper-evident log of all management actions)             │ │ │
│  │  └─────────────────────────────────────────────────────────────┘ │ │
│  │                                                                  │ │
│  │  ┌───────────────────────────────────────────────────┐          │ │
│  │  │  Identity Layer (auto-provisioned, invisible)     │          │ │
│  │  │  Archon Keymaster — DID generation, credential    │          │ │
│  │  │  signing, alias resolution (bundled, no user      │          │ │
│  │  │  interaction required)                            │          │ │
│  │  └───────────────────────────────────────────────────┘          │ │
│  └──────────────────────────────┬──────────────────────────────────┘ │
│                                 │                                     │
│                    Custom Messages (49153/49155)                      │
│                                 │                                     │
│  ┌──────────────────────────────▼──────────────────────────────────┐ │
│  │                   Lightning Node (CLN / LND)                    │ │
│  │               (Bolt11 / Bolt12 / L402 / Cashu)                  │ │
│  └─────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘

                              ▲
                              │ Bolt 8 Transport
                              │ (Custom TLV Messages)
                              ▼

┌──────────────────────────────────────────────────────────────────────┐
│                         ADVISOR                                       │
│                                                                       │
│  ┌───────────────────┐  ┌────────────────────────────────┐          │
│  │ Management Engine │  │ Payment Receiver               │          │
│  │ (AI / human)      │  │ (Bolt11/Bolt12/L402/Cashu)     │          │
│  └───────────────────┘  └────────────────────────────────┘          │
│  ┌────────────────────────────────────────────────────────┐          │
│  │ Identity Layer (Archon DID — advisor's storefront)     │          │
│  └────────────────────────────────────────────────────────┘          │
└──────────────────────────────────────────────────────────────────────┘
```

### Comparison with Full Hive Membership

| Feature | Unmanaged | Client (`cl-hive-client` / `hive-lnd`) | Full Hive Member (`cl-hive`) |
|---------|-----------|----------------------------------------|------------------------------|
| Professional management | ✗ | ✓ | ✓ |
| Fee optimization | Manual | Via advisor | Via advisor + fleet intelligence |
| Rebalancing | Manual | Via advisor | Via advisor + fleet paths (97% cheaper) |
| Channel expansion | Manual | Via advisor proposal | Via advisor + hive coordination |
| Monitoring & alerts | DIY | Via advisor | Via advisor + hive health gossip |
| Gossip participation | ✗ | ✗ | ✓ |
| Settlement protocol | ✗ | ✗ (direct escrow only) | ✓ (netting, credit tiers) |
| Fleet rebalancing | ✗ | ✗ | ✓ (intra-hive paths) |
| Pheromone routing | ✗ | ✗ | ✓ |
| Intelligence market | ✗ | ✗ (buy from advisor directly) | ✓ (full market access) |
| Payment methods | N/A | Bolt11, Bolt12, L402, Cashu escrow | Same + settlement netting |
| Bond requirement | None | None | 50,000–500,000 sats |
| Infrastructure | Node only | Node + plugin/daemon (auto-configuring) | Node + cl-hive + full PKI |
| Cost model | Free | Per-action or subscription | Bond + discounted per-action |

### Minimal Dependencies

The client has two dependencies:

1. **Lightning node** — CLN ≥ v24.08 or LND ≥ v0.18.0 (custom message support required)
2. **The client plugin/daemon itself** — Single file (CLN) or single binary (LND)

That's it. The Archon Keymaster (for DID identity) is **bundled** — the client auto-provisions a DID on first run. No separate installation, no manual key management. A built-in Cashu wallet handles conditional escrow. The node's existing Lightning wallet handles Bolt11/Bolt12/L402 payments.

---

## DID Abstraction Layer

### Principle: DIDs Are Plumbing

Archon DIDs are the cryptographic backbone of the entire protocol — identity, credentials, escrow, reputation. But operators should **never interact with DIDs directly**. The abstraction layer ensures that all DID operations happen invisibly, like TLS certificates in a web browser.

### Auto-Provisioning

On first run, the client:

1. Checks if a DID is configured
2. If not, **automatically generates one** using the bundled Archon Keymaster library
3. Stores the DID and key material in the client's data directory (encrypted at rest)
4. Registers the DID with the configured Archon gateway (default: `https://archon.technology`)
5. Logs: `"Hive client initialized. Your node identity has been created."`

The operator never sees `did:cid:bagaaiera...`. They see "your node identity."

```bash
# What the operator types:
lightning-cli plugin start cl_hive_client.py

# What happens internally:
# 1. Plugin starts
# 2. No DID found → auto-generate
# 3. DID stored in ~/.lightning/hive-client/identity.json (encrypted)
# 4. Ready to go
```

For operators who already have an Archon DID (e.g., from another application), the client can import it:

```bash
lightning-cli hive-client-import-identity --file=/path/to/wallet.json
```

### Alias Resolution

Every DID in the system gets a human-readable alias. The client maintains a local alias registry:

| Internal | User Sees |
|----------|-----------|
| `did:cid:bagaaierajrr7k...` | `"Hex Fleet Advisor"` |
| `did:cid:bagaaierawhtw...` | `"RoutingBot Pro"` |
| `did:cid:bagaaierabnbx...` | `"my-node"` (auto-assigned) |

Aliases come from three sources (priority order):
1. **Local aliases** — Operator assigns names: `lightning-cli hive-client-alias set hex-advisor "did:cid:..."`
2. **Profile display names** — From the advisor's `HiveServiceProfile.displayName`
3. **Auto-generated** — `"advisor-1"`, `"advisor-2"` for unnamed entities

Aliases are used in **all** user-facing output:

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

  RoutingBot Pro
    Access: monitoring only
    Since: 2026-02-10 (24 days remaining)
    Actions: 12 taken, 0 rejected
    Spending: 120 sats this month

Payment Balance:
  Escrow (Cashu): 7,660 sats
  This month's spend: 2,460 sats (limit: 50,000)
```

No DIDs anywhere. No credential IDs. No hashes. Just names, numbers, and plain English.

### Simplified CLI Commands

Every CLI command uses aliases, not DIDs:

```bash
# What the spec defines (internal/advanced):
lightning-cli hive-client-authorize --advisor-did="did:cid:bagaaiera..." --template="fee_optimization"

# What operators actually type:
lightning-cli hive-client-authorize "Hex Fleet Advisor" --access="fee optimization"

# Or even simpler, from discovery results:
lightning-cli hive-client-authorize --advisor=1 --access="fee optimization"
# (where "1" is the index from the last discovery query)
```

The `--access` parameter maps to credential templates using natural language:

| User Types | Maps To Template |
|-----------|-----------------|
| `"monitoring"` or `"read only"` | `monitor_only` |
| `"fee optimization"` or `"fees"` | `fee_optimization` |
| `"full routing"` or `"routing"` | `full_routing` |
| `"full management"` or `"everything"` | `complete_management` |

Similarly for revocation:

```bash
# Instead of:
lightning-cli hive-client-revoke --advisor-did="did:cid:badactor..."

# Operators type:
lightning-cli hive-client-revoke "Hex Fleet Advisor"

# Or emergency lockdown:
lightning-cli hive-client-revoke --all
```

### Discovery Output

Discovery results hide all cryptographic details:

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

Payment methods: All accept Lightning (Bolt11). #1, #4 also accept Bolt12 recurring.
Trial available: #1, #2, #3, #5

Use: lightning-cli hive-client-authorize <number> --access="fee optimization"
```

No DIDs. No credential schemas. No Archon queries visible. Just a ranked list with actionable next steps.

### What Stays Visible (Advanced Mode)

For power users and developers, raw DID/credential data is always accessible:

```bash
# Show full identity details (advanced)
lightning-cli hive-client-identity --verbose

# Show raw credential for an advisor
lightning-cli hive-client-credential "Hex Fleet Advisor" --raw

# Manually specify DID (bypasses alias resolution)
lightning-cli hive-client-authorize --advisor-did="did:cid:bagaaiera..." --template="fee_optimization"
```

The `--verbose` and `--raw` flags expose the cryptographic layer for debugging, auditing, and integration with other DID-aware tools. But the default output is always human-readable.

### Implementation Notes

The abstraction layer is implemented as a thin wrapper around the Archon Keymaster library:

```python
class IdentityLayer:
    """Invisible DID management. Users never interact with this directly."""

    def __init__(self, data_dir):
        self.keymaster = BundledKeymaster(data_dir)
        self.aliases = AliasRegistry(data_dir / "aliases.json")

    def ensure_identity(self):
        """Auto-provision DID on first run. No user action needed."""
        if not self.keymaster.has_identity():
            did = self.keymaster.create_identity()
            self.aliases.set("my-node", did)
            log.info("Node identity created.")
        return self.keymaster.get_identity()

    def resolve_advisor(self, name_or_index):
        """Resolve human input to a DID. Accepts names, indices, or raw DIDs."""
        if isinstance(name_or_index, int):
            return self.last_discovery_results[name_or_index - 1].did
        if name_or_index.startswith("did:"):
            return name_or_index  # passthrough for advanced users
        return self.aliases.resolve(name_or_index)

    def display_name(self, did):
        """Convert DID to human-readable name."""
        alias = self.aliases.get(did)
        if alias:
            return alias
        profile = self.profile_cache.get(did)
        if profile and profile.display_name:
            return profile.display_name
        return did[:20] + "..."  # last resort: truncated DID
```

---

## Payment Manager

### Overview

The Payment Manager handles all payment flows between operator and advisor. It supports **four payment methods**, choosing the right one based on the payment context:

| Method | Use Case | Conditional? | Requires |
|--------|----------|-------------|----------|
| **Bolt11** | Simple per-action payments, one-time subscription fees | No | Node's Lightning wallet |
| **Bolt12** | Recurring subscriptions, reusable payment codes | No | Bolt12-capable node (CLN native, LND via plugin) |
| **L402** | API-gated access, subscription macaroons | No | L402 middleware (bundled) |
| **Cashu** | Conditional escrow (payment-on-completion) | Yes (NUT-10/11/14) | Built-in Cashu wallet |

### Payment Method Selection

The client selects the payment method based on the situation:

```
Is this a conditional payment (escrow)?
  YES → Cashu (only option for conditional spending conditions)
  NO  → Use operator's preferred method:
        ├─ Subscription? → Bolt12 offer (if supported) or Bolt11 invoice
        ├─ Per-action?   → Bolt11 invoice or L402 macaroon
        └─ Flat fee?     → Bolt11 invoice
```

**Configuration:**

```ini
# Operator's preferred payment methods (in priority order)
hive-client-payment-methods=bolt11,bolt12,cashu

# For escrow specifically (danger score ≥ 3)
hive-client-escrow-method=cashu
hive-client-escrow-mint=https://mint.minibits.cash
```

```yaml
# hive-lnd.yaml
payments:
  preferred_methods: ["bolt11", "bolt12"]
  escrow_method: "cashu"
  escrow_mint: "https://mint.minibits.cash"
```

### Bolt11 Payments (Standard Lightning Invoices)

The simplest and most widely supported payment method. Used for:
- Per-action fees (advisor presents invoice, client pays automatically within spending limits)
- Flat-fee trial periods
- One-time subscription payments

```
Advisor                          Client                    Node Wallet
  │                                │                          │
  │  1. Management command +       │                          │
  │     Bolt11 invoice (10 sats)   │                          │
  │  ──────────────────────────►   │                          │
  │                                │                          │
  │     2. Verify credential       │                          │
  │     3. Verify invoice matches  │                          │
  │        expected pricing        │                          │
  │     4. Check spending limits   │                          │
  │                                │                          │
  │                                │  5. Pay invoice          │
  │                                │  ──────────────────────► │
  │                                │                          │
  │                                │  6. Payment confirmed    │
  │                                │  ◄────────────────────── │
  │                                │                          │
  │     7. Execute action          │                          │
  │     8. Return signed receipt   │                          │
  │  ◄────────────────────────────  │                          │
```

**Advantage:** Works with every Lightning node. No Cashu wallet needed for simple payments.

**Limitation:** Not conditional — once paid, the payment is final regardless of task outcome. Suitable for low-danger actions (score 1–4) where the cost of failure is low.

### Bolt12 Payments (Recurring Offers)

For subscription-based management contracts. The advisor publishes a Bolt12 offer; the client pays it on a recurring schedule.

```
Advisor                          Client
  │                                │
  │  1. Contract includes          │
  │     Bolt12 offer string        │
  │  ──────────────────────────►   │
  │                                │
  │     2. Client stores offer     │
  │     3. Auto-pays monthly       │
  │        (within spending limits) │
  │                                │
  │  [Each month:]                 │
  │  4. Client fetches invoice     │
  │     from offer                 │
  │  ──────────────────────────►   │
  │                                │
  │  5. Invoice returned           │
  │  ◄────────────────────────────  │
  │                                │
  │  6. Client pays               │
  │  ──────────────────────────►   │
  │                                │
```

**Advantage:** Recurring payments without manual intervention. Reusable — same offer for the entire contract duration. Privacy-preserving (Bolt12 blinded paths).

**Limitation:** Requires Bolt12 support. CLN has native support. LND support via experimental flag or plugin. Not conditional.

### L402 Payments (API-Gated Access)

For API-style access patterns where the advisor provides an HTTP endpoint:

```
Advisor (HTTP API)               Client
  │                                │
  │  1. Request resource           │
  │  ◄────────────────────────────  │
  │                                │
  │  2. HTTP 402 + Lightning       │
  │     invoice + macaroon stub    │
  │  ──────────────────────────►   │
  │                                │
  │     3. Pay invoice             │
  │     4. Receive L402 macaroon   │
  │        (valid for N actions    │
  │         or T time period)      │
  │                                │
  │  5. Subsequent requests with   │
  │     L402 macaroon              │
  │  ◄────────────────────────────  │
  │                                │
```

**Advantage:** Familiar HTTP API pattern. Macaroon caveats can encode permission scope (mirroring credential constraints). Efficient for high-frequency monitoring queries.

**Limitation:** Requires HTTP connectivity to advisor (not P2P Bolt 8). Best suited for monitoring-heavy advisors with web dashboards.

### Cashu Escrow (Conditional Payments)

Used exclusively for conditional payments where payment must be contingent on task completion. See [Section 7: Escrow Management](#7-escrow-management-client-side) for the full protocol.

**When Cashu escrow is required:**
- Danger score ≥ 3 (configurable, default: 3)
- Performance-based compensation (bonus payments)
- Any action where the operator wants payment-on-completion guarantees

**When Cashu escrow is optional:**
- Danger score 1–2 (monitoring, read-only)
- Flat-fee subscriptions
- Trusted advisors with established reputation (operator can configure to skip escrow)

### Payment in the HiveServiceProfile

Advisors advertise accepted payment methods in their service profile (extending the [Marketplace spec](./DID-HIVE-MARKETPLACE.md#hiveserviceprofile-credential)):

```json
{
  "pricing": {
    "models": [
      {
        "type": "per_action",
        "baseFeeRange": { "min": 5, "max": 100, "currency": "sats" }
      },
      {
        "type": "subscription",
        "monthlyRate": 5000,
        "bolt12Offer": "lno1qgsq...",
        "currency": "sats"
      }
    ],
    "acceptedPayment": ["bolt11", "bolt12", "cashu", "l402"],
    "preferredPayment": "bolt12",
    "escrowRequired": true,
    "escrowMinDangerScore": 3,
    "acceptableMints": ["https://mint.minibits.cash"]
  }
}
```

### Payment Method Negotiation

When operator and advisor connect, they negotiate a payment method:

```
Operator preferred:  [bolt11, bolt12]
Advisor accepted:    [bolt11, bolt12, cashu, l402]
Negotiated:          bolt12 (first match in operator's preference that advisor accepts)

Exception: escrow payments always use Cashu regardless of preference
```

If no common non-escrow method exists, the client falls back to Cashu for all payments (since both parties must support Cashu for escrow anyway).

---

## CLN Plugin (`cl-hive-client`)

### Overview

A Python plugin following CLN's plugin architecture. Single file (`cl_hive_client.py`), no Docker, no complex setup. Registers custom message handlers for management schemas (types 49153/49155) and exposes RPC commands for operator interaction. **Auto-provisions identity on first run** — no manual DID setup needed.

### Components

#### Schema Handler

Receives incoming management commands via custom message type 49153, validates the TLV payload structure per the [Fleet Management transport spec](./DID-L402-FLEET-MANAGEMENT.md#3-transport-layer-bolt-8--custom-messages), and dispatches to the appropriate CLN RPC.

```python
@plugin.hook("custommsg")
def on_custommsg(peer_id, payload, plugin, **kwargs):
    msg_type = int.from_bytes(payload[:2], 'big')
    if msg_type == 0xC001:  # 49153 — Hive Management Message
        return handle_management_message(peer_id, payload[2:])
    return {"result": "continue"}
```

The handler:
1. Deserializes the TLV payload (schema_type, schema_payload, credential, payment_proof, signature, nonce, timestamp)
2. Passes to Credential Verifier
3. Passes to Policy Engine
4. If both pass, executes the schema action via CLN RPC
5. Generates signed receipt
6. Sends response via custom message type 49155

#### Credential Verifier

Validates the Archon DID credential attached to each management command:

1. **DID resolution** — Resolves the agent's DID via local Archon Keymaster or remote Archon gateway
2. **Signature verification** — Verifies the credential's proof against the issuer's DID document
3. **Scope check** — Confirms the credential grants the required permission tier for the requested schema
4. **Constraint check** — Validates the command parameters against credential constraints (`max_fee_change_pct`, `max_rebalance_sats`, etc.)
5. **Revocation check** — Queries Archon revocation status. **Fail-closed**: if Archon is unreachable, deny. Cache with 1-hour TTL per the [Fleet Management spec](./DID-L402-FLEET-MANAGEMENT.md#credential-lifecycle).
6. **Replay protection** — Monotonic nonce check per agent DID. Timestamp within ±5 minutes.

#### Payment & Escrow Manager

Handles all payment flows. Delegates to the [Payment Manager](#payment-manager) for method selection, and manages the Cashu escrow wallet for conditional payments per the [Task Escrow protocol](./DID-CASHU-TASK-ESCROW.md):

- **Method selection** — Chooses Bolt11/Bolt12/L402/Cashu based on context and preferences
- **Bolt11/Bolt12 payments** — Routes through the node's existing Lightning wallet
- **Cashu escrow tickets** — Mints tokens with P2PK + HTLC + timelock conditions for conditional payments
- **Secret management** — Generates and stores HTLC secrets, reveals on task completion
- **Auto-replenishment** — When escrow balance drops below threshold, auto-mints new tokens
- **Spending limits** — Enforces daily/weekly caps across all payment methods
- **Mint management** — Configurable trusted mints, multi-mint support
- **Receipt tracking** — Stores all completed task receipts locally

#### Policy Engine

The operator's last line of defense. Even with a valid credential and valid payment, the Policy Engine can reject any action based on local rules. See [Section 8: Local Policy Engine](#8-local-policy-engine) for full details.

#### Receipt Store

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

Tamper-evident: modifying any receipt breaks the hash chain. Receipts are stored in a local SQLite database with periodic merkle root computation for efficient auditing.

### RPC Commands

All commands accept **advisor names, aliases, or discovery indices** — not DIDs. DIDs are accepted via `--advisor-did` for advanced use.

| Command | Description | Example |
|---------|-------------|---------|
| `hive-client-status` | Active advisors, spending, policy | `lightning-cli hive-client-status` |
| `hive-client-authorize` | Grant an advisor access to your node | `lightning-cli hive-client-authorize "Hex Advisor" --access="fees"` |
| `hive-client-revoke` | Immediately revoke an advisor's access | `lightning-cli hive-client-revoke "Hex Advisor"` |
| `hive-client-receipts` | List management action receipts | `lightning-cli hive-client-receipts --advisor="Hex Advisor"` |
| `hive-client-discover` | Find advisors | `lightning-cli hive-client-discover --capabilities="fee optimization"` |
| `hive-client-policy` | View or modify local policy | `lightning-cli hive-client-policy --preset=moderate` |
| `hive-client-payments` | View payment balance and spending | `lightning-cli hive-client-payments` |
| `hive-client-trial` | Start or review a trial period | `lightning-cli hive-client-trial "Hex Advisor" --days=14` |
| `hive-client-alias` | Set a friendly name for an advisor | `lightning-cli hive-client-alias set "Hex" "did:cid:..."` |
| `hive-client-identity` | View or manage node identity | `lightning-cli hive-client-identity` (shows name, not DID) |

### Configuration

Most settings have sensible defaults. **Zero configuration is required for first run** — the plugin auto-provisions identity and uses defaults for everything else.

```ini
# ~/.lightning/config (CLN config file)
# All settings are optional — defaults work out of the box.

# Identity (auto-provisioned if not set — see Archon Integration Tiers)
# hive-client-did=did:cid:bagaaiera...    # Only set if importing existing DID
# hive-client-archon-gateway=https://archon.technology  # Tier 1 default
# hive-client-archon-gateway=http://localhost:4224      # Tier 2: own Archon node

# Payment methods (in preference order)
hive-client-payment-methods=bolt11,bolt12
hive-client-escrow-mint=https://mint.minibits.cash

# Spending limits
hive-client-daily-limit=50000
hive-client-weekly-limit=200000

# Policy preset (conservative | moderate | aggressive)
hive-client-policy-preset=moderate

# Alerts (optional — enables notifications for advisor actions)
# hive-client-alert-webhook=https://hooks.example.com/hive
# hive-client-alert-nostr-dm=npub1abc...
# hive-client-alert-email=operator@example.com
```

### Installation

```bash
# 1. Download the plugin
curl -O https://github.com/lightning-goats/cl-hive-client/releases/latest/cl_hive_client.py

# 2. Start it
lightning-cli plugin start /path/to/cl_hive_client.py
```

That's it. On first run, the plugin auto-provisions a node identity, creates its data directory, and is ready to accept advisor connections. No DID setup. No key management. No configuration file edits required.

For permanent installation, add to your CLN config:

```ini
plugin=/path/to/cl_hive_client.py
```

### Relationship to Full `cl-hive`

`cl-hive-client` is a **strict subset** of `cl-hive`. If you're already running `cl-hive`, you don't need `cl-hive-client` — the full plugin includes all client functionality plus gossip, settlement, pheromone, and fleet coordination.

```
┌──────────────────────────────────────────────────────┐
│                      cl-hive (full)                   │
│                                                       │
│  ┌────────────────────────────────────────────────┐  │
│  │              cl-hive-client (subset)            │  │
│  │                                                 │  │
│  │  Schema Handler    Credential Verifier          │  │
│  │  Escrow Manager    Policy Engine                │  │
│  │  Receipt Store     RPC Commands                 │  │
│  └─────────────────────────────────────────────────┘  │
│                                                       │
│  Gossip Protocol        Settlement Protocol           │
│  Pheromone System       Bond Management               │
│  Fleet Coordination     Hive PKI                      │
│  Intelligence Market    Stigmergic Signals            │
└──────────────────────────────────────────────────────┘
```

**Migration path:** See [Section 11: Hive Membership Upgrade Path](#11-hive-membership-upgrade-path).

---

## LND Companion Daemon (`hive-lnd`)

### Overview

A Go daemon that connects to LND via gRPC and provides the same management interface as `cl-hive-client`. Runs as a standalone process alongside LND, similar to other LND companion tools (Loop, Pool, Faraday, Lightning Terminal).

### Architecture

```
┌──────────────────────────────────────────────────────┐
│                       hive-lnd                        │
│                                                       │
│  ┌──────────┐ ┌────────────┐ ┌──────────┐           │
│  │ Schema   │ │ Credential │ │ Escrow   │           │
│  │ Handler  │ │ Verifier   │ │ Manager  │           │
│  └────┬─────┘ └────────────┘ └──────────┘           │
│       │                                               │
│  ┌────▼──────────────────────────────────┐           │
│  │     Schema Translation Layer          │           │
│  │                                       │           │
│  │  hive:fee-policy → UpdateChannelPolicy│           │
│  │  hive:monitor    → GetInfo, ListChans │           │
│  │  hive:rebalance  → SendPaymentV2     │           │
│  │  hive:channel    → OpenChannel, Close │           │
│  │  ...                                  │           │
│  └────┬──────────────────────────────────┘           │
│       │                                               │
│  ┌────▼─────────────────────────┐                    │
│  │   LND gRPC Client           │                    │
│  │   (lnrpc, routerrpc, etc.)  │                    │
│  └──────────────────────────────┘                    │
│                                                       │
│  ┌──────────────────────────────┐                    │
│  │   Policy Engine + Receipt   │                    │
│  │   Store + Alert Manager     │                    │
│  └──────────────────────────────┘                    │
│                                                       │
│  ┌──────────────────────────────┐                    │
│  │   HiveClientService (gRPC)  │                    │
│  │   (local management API)    │                    │
│  └──────────────────────────────┘                    │
└──────────────────────────────────────────────────────┘
          │                    ▲
          │ gRPC               │ Custom Messages
          ▼                    │ (SubscribeCustomMessages)
     ┌─────────┐          ┌───┴───┐
     │  LND    │          │  LND  │
     │  (RPC)  │          │ (P2P) │
     └─────────┘          └───────┘
```

### Custom Message Handling

LND exposes custom message handling via gRPC:

```go
// Subscribe to incoming custom messages
stream, err := client.SubscribeCustomMessages(ctx, &lnrpc.SubscribeCustomMessagesRequest{})
for {
    msg, err := stream.Recv()
    if msg.Type == 49153 { // Hive Management Message
        handleManagementMessage(msg.Peer, msg.Data)
    }
}

// Send custom message response
_, err = client.SendCustomMessage(ctx, &lnrpc.SendCustomMessageRequest{
    Peer: peerPubkey,
    Type: 49155, // Hive Management Response
    Data: responsePayload,
})
```

### Local gRPC Service

`hive-lnd` exposes a local gRPC service for operator interaction (equivalent to `cl-hive-client`'s RPC commands):

```protobuf
service HiveClientService {
  rpc Status(StatusRequest) returns (StatusResponse);
  rpc Authorize(AuthorizeRequest) returns (AuthorizeResponse);
  rpc Revoke(RevokeRequest) returns (RevokeResponse);
  rpc ListReceipts(ListReceiptsRequest) returns (ListReceiptsResponse);
  rpc Discover(DiscoverRequest) returns (DiscoverResponse);
  rpc GetPolicy(GetPolicyRequest) returns (PolicyResponse);
  rpc SetPolicy(SetPolicyRequest) returns (PolicyResponse);
  rpc EscrowInfo(EscrowInfoRequest) returns (EscrowInfoResponse);
  rpc StartTrial(StartTrialRequest) returns (TrialResponse);
}
```

### Configuration

Auto-detected defaults for most settings. Only the LND connection needs explicit configuration (and `hive-lnd init` auto-detects the standard LND paths).

```yaml
# hive-lnd.yaml (generated by `hive-lnd init`)
# Identity is auto-provisioned on first run — no DID setup needed.

lnd:
  rpc_host: "localhost:10009"       # auto-detected
  tls_cert: "~/.lnd/tls.cert"      # auto-detected
  macaroon: "~/.lnd/data/chain/bitcoin/mainnet/admin.macaroon"  # auto-detected

payments:
  preferred_methods: ["bolt11", "bolt12"]
  escrow_mint: "https://mint.minibits.cash"
  daily_limit: 50000
  weekly_limit: 200000

policy:
  preset: "moderate"

# alerts:                           # optional
#   webhook: "https://hooks.example.com/hive"
#   email: "operator@example.com"
```

### Installation

```bash
# 1. Download and install
curl -LO https://github.com/lightning-goats/hive-lnd/releases/latest/hive-lnd-linux-amd64
chmod +x hive-lnd-linux-amd64 && mv hive-lnd-linux-amd64 /usr/local/bin/hive-lnd

# 2. Initialize (auto-detects LND paths, generates config)
hive-lnd init

# 3. Run
hive-lnd

# Optional: install as system service
hive-lnd install-service
```

On first run, `hive-lnd` auto-provisions a node identity and connects to LND. No DID setup, no key management.

---

## 5. Schema Translation Layer

The management schemas defined in the [Fleet Management spec](./DID-L402-FLEET-MANAGEMENT.md#core-schemas) are implementation-agnostic. The client translates each schema action to the appropriate CLN RPC call or LND gRPC call. This section defines the full mapping for all 15 schema categories.

### Translation Table

| Schema | Action | CLN RPC | LND gRPC | Danger | Notes |
|--------|--------|---------|----------|--------|-------|
| **hive:monitor/v1** | | | | | |
| | `health_summary` | `getinfo` | `lnrpc.GetInfo` | 1 | |
| | `channel_list` | `listpeerchannels` | `lnrpc.ListChannels` | 1 | CLN uses `listpeerchannels` (v23.08+) |
| | `forward_history` | `listforwards` | `lnrpc.ForwardingHistory` | 1 | |
| | `peer_list` | `listpeers` | `lnrpc.ListPeers` | 1 | |
| | `invoice_list` | `listinvoices` | `lnrpc.ListInvoices` | 1 | |
| | `payment_list` | `listsendpays` | `lnrpc.ListPayments` | 1 | |
| | `htlc_snapshot` | `listpeerchannels` (htlcs field) | `lnrpc.ListChannels` (pending_htlcs) | 1 | |
| | `fee_report` | `listpeerchannels` (fee fields) | `lnrpc.FeeReport` | 1 | |
| | `onchain_balance` | `listfunds` | `lnrpc.WalletBalance` | 1 | |
| | `graph_query` | `listnodes` / `listchannels` | `lnrpc.DescribeGraph` | 1 | |
| | `log_stream` | `notifications` subscribe | `lnrpc.SubscribeInvoices` (partial) | 2 | LND lacks generic log streaming |
| | `plugin_status` | `plugin list` | N/A | 1 | LND: report `hive-lnd` version/status instead |
| | `backup_status` | Custom (check backup file timestamps) | `lnrpc.SubscribeChannelBackups` | 1 | |
| **hive:fee-policy/v1** | | | | | |
| | `set_anchor` (single) | `setchannel` | `lnrpc.UpdateChannelPolicy` | 2–3 | |
| | `set_anchor` (bulk) | `setchannel` (loop) | `lnrpc.UpdateChannelPolicy` (loop) | 4–5 | |
| | `set_htlc_limits` | `setchannel` (htlcmin/htlcmax) | `lnrpc.UpdateChannelPolicy` (min/max_htlc) | 2–5 | |
| | `set_zero_fee` | `setchannel` (0/0) | `lnrpc.UpdateChannelPolicy` (0/0) | 4 | |
| **hive:rebalance/v1** | | | | | |
| | `circular_rebalance` | `pay` (self-invoice) | `routerrpc.SendPaymentV2` (circular) | 3–5 | CLN: create invoice, self-pay via specific route |
| | `submarine_swap` | External (Loop/Boltz plugin) | `looprpc.LoopOut` / `LoopIn` | 5 | Requires Loop/Boltz integration |
| | `peer_rebalance` | Custom message to peer | Custom message to peer | 4 | Hive peers only; N/A for standalone client |
| **hive:config/v1** | | | | | |
| | `adjust` | `setconfig` (CLN ≥ v24.02) | `lnrpc.UpdateNodeAnnouncement` (limited) | 3–4 | LND: fewer runtime-adjustable params |
| | `set_alias` | `setconfig alias` | `lnrpc.UpdateNodeAnnouncement` | 1 | |
| | `disable_forwarding` (all) | `setchannel` (all, disabled) | `lnrpc.UpdateChannelPolicy` (all, disabled) | 6 | |
| **hive:expansion/v1** | | | | | |
| | `propose_channel_open` | Queued for operator approval | Queued for operator approval | 5–7 | Never auto-executed; always queued |
| **hive:channel/v1** | | | | | |
| | `open` | `fundchannel` | `lnrpc.OpenChannelSync` | 5–7 | |
| | `close_cooperative` | `close` | `lnrpc.CloseChannel` (cooperative) | 6 | |
| | `close_unilateral` | `close --unilateraltimeout=1` | `lnrpc.CloseChannel` (force=true) | 7 | |
| | `close_all` | `close` (loop, all) | `lnrpc.CloseChannel` (loop, all) | 10 | Nuclear. Always multi-sig. |
| **hive:splice/v1** | | | | | |
| | `splice_in` | `splice` (CLN ≥ v24.02) | N/A (experimental in LND) | 5–7 | LND: advertise as unsupported |
| | `splice_out` | `splice` | N/A | 6 | |
| **hive:peer/v1** | | | | | |
| | `connect` | `connect` | `lnrpc.ConnectPeer` | 2 | |
| | `disconnect` | `disconnect` | `lnrpc.DisconnectPeer` | 2–4 | |
| | `ban` | `dev-blacklist-peer` (if available) | Custom (blocklist file) | 5 | Implementation varies |
| **hive:payment/v1** | | | | | |
| | `create_invoice` | `invoice` | `lnrpc.AddInvoice` | 1 | |
| | `pay_invoice` | `pay` | `routerrpc.SendPaymentV2` | 4–6 | |
| | `keysend` | `keysend` | `routerrpc.SendPaymentV2` (keysend) | 4–6 | |
| **hive:wallet/v1** | | | | | |
| | `generate_address` | `newaddr` | `lnrpc.NewAddress` | 1 | |
| | `send_onchain` | `withdraw` | `lnrpc.SendCoins` | 6–9 | |
| | `utxo_management` | `fundpsbt` / `reserveinputs` | `walletrpc.FundPsbt` / `LeaseOutput` | 3–4 | |
| | `bump_fee` | `bumpfee` (via psbt) | `walletrpc.BumpFee` | 4 | |
| **hive:plugin/v1** | | | | | |
| | `list` | `plugin list` | N/A | 1 | LND: not applicable |
| | `start` | `plugin start` | N/A | 4–9 | LND: not applicable |
| | `stop` | `plugin stop` | N/A | 5 | LND: not applicable |
| **hive:backup/v1** | | | | | |
| | `trigger_backup` | `makesecret` + manual | `lnrpc.ExportAllChannelBackups` | 2 | |
| | `verify_backup` | Custom (hash check) | Custom (hash check) | 1 | |
| | `export_scb` | `staticbackup` | `lnrpc.ExportAllChannelBackups` | 3 | |
| | `restore` | N/A (requires restart) | `lnrpc.RestoreChannelBackups` | 10 | |
| **hive:emergency/v1** | | | | | |
| | `disable_forwarding` | `setchannel` (all, disabled) | `lnrpc.UpdateChannelPolicy` (all, disabled) | 6 | |
| | `fee_spike` | `setchannel` (all, max fee) | `lnrpc.UpdateChannelPolicy` (all, max fee) | 5 | |
| | `force_close` | `close --unilateraltimeout=1` | `lnrpc.CloseChannel` (force) | 8 | |
| | `force_close_all` | Loop `close` all | Loop `CloseChannel` all | 10 | |
| | `revoke_all_credentials` | Internal (revoke all via Archon) | Internal | 3 | |
| **hive:htlc/v1** | | | | | |
| | `list_stuck` | `listpeerchannels` (filter pending) | `lnrpc.ListChannels` (filter pending) | 2 | |
| | `inspect` | `listpeerchannels` (specific htlc) | `lnrpc.ListChannels` (specific htlc) | 2 | |
| | `fail_htlc` | `dev-fail-htlc` (dev mode) | `routerrpc.HtlcInterceptor` | 7 | CLN: requires `--developer`; LND: interceptor |
| | `settle_htlc` | `dev-resolve-htlc` (dev mode) | `routerrpc.HtlcInterceptor` | 7 | Same constraints |
| | `force_resolve_expired` | `dev-fail-htlc` (expired only) | `routerrpc.HtlcInterceptor` | 8 | Last resort |

### Semantic Differences

| Area | CLN Behavior | LND Behavior | Handling |
|------|-------------|-------------|----------|
| Fee unit | `fee_proportional_millionths` | `fee_rate_milli_msat` (ppm) | Translation layer normalizes to ppm |
| Channel ID | Short channel ID (`931770x2363x0`) | Channel point (`txid:index`) OR `chan_id` (uint64) | Both formats supported; translation layer converts |
| HTLC resolution | `dev-` commands (developer mode) | `routerrpc.HtlcInterceptor` stream | Capability advertised per implementation |
| Splicing | Native support (v24.02+) | Experimental / not available | Advertised as unsupported on LND |
| Plugin management | Full lifecycle | Not applicable | Schema returns `unsupported` on LND |
| Runtime config | `setconfig` (extensive) | Limited runtime changes | Advertised capabilities differ |

### Feature Capability Advertisement

On startup, the client determines which schemas it can support based on the underlying implementation and version:

```json
{
  "implementation": "CLN",
  "version": "24.08",
  "supported_schemas": [
    "hive:monitor/v1",
    "hive:fee-policy/v1",
    "hive:rebalance/v1",
    "hive:config/v1",
    "hive:expansion/v1",
    "hive:channel/v1",
    "hive:splice/v1",
    "hive:peer/v1",
    "hive:payment/v1",
    "hive:wallet/v1",
    "hive:plugin/v1",
    "hive:backup/v1",
    "hive:emergency/v1",
    "hive:htlc/v1"
  ],
  "unsupported_actions": [
    { "schema": "hive:htlc/v1", "action": "fail_htlc", "reason": "--developer not enabled" }
  ]
}
```

The advisor queries capabilities before sending commands. Commands for unsupported schemas return an error response with `status: 2` and a reason string.

**Danger score preservation:** Danger scores are identical regardless of implementation. A `hive:fee-policy/v1 set_anchor` is danger 3 whether on CLN or LND. The Policy Engine uses the same scoring table from the [Fleet Management spec](./DID-L402-FLEET-MANAGEMENT.md#task-taxonomy--danger-scoring).

---

## 6. Credential Management (Client Side)

### Issuing Access (Management Credential)

The operator grants an advisor access to their node. Under the hood, this issues a `HiveManagementCredential` (per the [Fleet Management spec](./DID-L402-FLEET-MANAGEMENT.md#management-credentials)) — but the operator never sees the credential format.

```bash
# CLN — authorize by name (from discovery results)
lightning-cli hive-client-authorize "Hex Fleet Advisor" --access="fee optimization"

# CLN — authorize by discovery index
lightning-cli hive-client-authorize 1 --access="full routing" --days=30

# LND (via hive-lnd CLI)
hive-lnd authorize "Hex Fleet Advisor" --access="fee optimization"

# Advanced: authorize by DID directly
lightning-cli hive-client-authorize --advisor-did="did:cid:bagaaiera..." --template="fee_optimization"
```

The credential is signed by the operator's auto-provisioned identity and delivered to the advisor automatically via the Bolt 8 peer connection.

### Credential Templates

Pre-configured permission sets for common scenarios. Operators can use templates or define custom scopes.

| Template | Permissions | Schemas | Constraints | Use Case |
|----------|-----------|---------|-------------|----------|
| `monitor_only` | `monitor` | `hive:monitor/*` | Read-only, no state changes | Dashboard, alerting, reporting |
| `fee_optimization` | `monitor`, `fee_policy` | `hive:monitor/*`, `hive:fee-policy/*`, `hive:config/fee_*` | `max_fee_change_pct: 50`, `max_daily_actions: 50` | Automated fee management |
| `full_routing` | `monitor`, `fee_policy`, `rebalance`, `config_tune` | `hive:monitor/*`, `hive:fee-policy/*`, `hive:rebalance/*`, `hive:config/*` | `max_rebalance_sats: 1000000`, `max_daily_actions: 100` | Full routing optimization |
| `complete_management` | All except `channel_close` | All except `hive:channel/close_*`, `hive:emergency/force_close_*` | `max_daily_actions: 200` | Full management minus nuclear options |

#### Custom Scope

For advanced users who need fine-grained control beyond templates:

```bash
lightning-cli hive-client-authorize "Hex Fleet Advisor" \
  --access="custom" \
  --allow="monitoring,fees,rebalancing" \
  --max-fee-change=25 \
  --max-rebalance=500000 \
  --days=14
```

Under the hood, this maps to the full credential schema (`permissions`, `constraints`, `allowed_schemas`) — but the operator interface uses plain English and sensible parameter names.

### Credential Lifecycle

```
Issue ──► Active ──┬──► Renew ──► Active (extended)
                   │
                   ├──► Expire (natural end)
                   │
                   └──► Revoke (operator-initiated, immediate)
```

1. **Issue** — Operator creates and signs credential. Delivered to advisor.
2. **Active** — Advisor presents credential with each management command. Node validates.
3. **Renew** — Before expiry, operator issues a new credential with updated terms. Old credential superseded.
4. **Expire** — Credential's `validUntil` date passes. All commands rejected. No cleanup needed.
5. **Revoke** — Operator calls `hive-client-revoke`. Credential marked as revoked in Archon. All pending commands from this credential are rejected immediately.

### Multi-Advisor Support

Operators can issue credentials to multiple advisors with non-overlapping scopes:

```bash
# Advisor A: fee expert
lightning-cli hive-client-authorize "Hex Fleet Advisor" --access="fee optimization"

# Advisor B: rebalance specialist
lightning-cli hive-client-authorize "RoutingBot Pro" --access="custom" --allow="monitoring,rebalancing"

# Advisor C: monitoring only (dashboard provider)
lightning-cli hive-client-authorize "NodeWatch" --access="monitoring"
```

The Policy Engine enforces scope isolation — Advisor A cannot send `hive:rebalance/*` commands even if their credential somehow includes that scope, because the operator configured them for fee optimization only.

For multi-advisor coordination details (conflict detection, shared state, action cooldowns), see the [Marketplace spec, Section 6](./DID-HIVE-MARKETPLACE.md#6-multi-advisor-coordination).

### Emergency Revocation

```bash
# Immediate revocation — all pending commands rejected
lightning-cli hive-client-revoke "Bad Advisor"

# Revoke ALL advisors (emergency lockdown)
lightning-cli hive-client-revoke --all
```

Revocation:
1. Marks credential as revoked locally (takes effect immediately for all pending/future commands)
2. Publishes revocation to Archon network (propagates to advisor and any verifier)
3. Logs the revocation event with reason in the Receipt Store
4. Sends alert via configured channels (webhook, Nostr DM, email)

The advisor's pending legitimate compensation (escrow tickets for completed work where the preimage was already revealed) is honored — the advisor can still redeem those tokens. Revocation only affects future commands.

---

## 7. Payment & Escrow Management (Client Side)

The client handles all payments to advisors through the [Payment Manager](#payment-manager). This section covers the operator-facing payment experience and the Cashu escrow subsystem.

### Payment Overview

Most advisor payments are simple Lightning transactions — the operator's node pays a Bolt11 invoice or subscribes via a Bolt12 offer. The client automates this within configured spending limits. **No special wallet or token management needed for standard payments.**

Cashu escrow is used only for **conditional payments** (danger score ≥ 3 by default) where payment must be contingent on task completion. The built-in Cashu wallet (NUT-10/11/14/07) handles escrow automatically.

### Ticket Creation Workflow (Escrow Only)

```
Operator                     Client Plugin               Cashu Mint
    │                             │                          │
    │  1. Advisor requests task   │                          │
    │  ◄──────────────────────    │                          │
    │                             │                          │
    │  2. Client auto-creates     │                          │
    │     escrow ticket:          │                          │
    │     - Generates HTLC secret │                          │
    │     - Computes H(secret)    │                          │
    │     - Mints Cashu token     │                          │
    │                      ───────────────────────────────►  │
    │                             │                          │
    │     - Token received        │                          │
    │                      ◄───────────────────────────────  │
    │                             │                          │
    │  3. Ticket sent to advisor  │                          │
    │     via Bolt 8              │                          │
    │  ──────────────────────►    │                          │
    │                             │                          │
```

For low-danger actions (score 1–2), the operator can configure **direct payment** (simple Cashu token, no HTLC escrow) to reduce overhead. For danger score 3+, full escrow is always used per the [Task Escrow spec](./DID-CASHU-TASK-ESCROW.md#danger-score-integration).

### Auto-Replenishment

```yaml
escrow:
  replenish_threshold: 1000    # sats — trigger replenishment when balance drops below
  replenish_amount: 5000       # sats — amount to mint on replenishment
  replenish_source: "onchain"  # "onchain" (from node wallet) or "lightning" (via invoice)
  auto_replenish: true         # enable automatic replenishment
```

When auto-replenishment triggers:
1. Client checks node's on-chain wallet balance (or creates a Lightning invoice)
2. If sufficient funds, mints new Cashu tokens at the preferred mint
3. New tokens added to the escrow wallet
4. Operator notified via alert channel

**Safety:** Auto-replenishment respects `daily_limit` and `weekly_limit`. If the limit would be exceeded, replenishment is blocked and the operator is alerted.

### Spending Limits

| Limit | Default | Configurable | Enforcement |
|-------|---------|-------------|-------------|
| Per-action cap | None (uses danger-score pricing) | Yes | Hard reject if exceeded |
| Daily cap | 50,000 sats | Yes | No new escrow tickets minted beyond cap |
| Weekly cap | 200,000 sats | Yes | No new escrow tickets minted beyond cap |
| Per-advisor daily cap | 25,000 sats | Yes | Per-advisor enforcement |

When a limit is reached, the client stops minting new escrow tickets and alerts the operator. The advisor receives a `budget_exhausted` error on their next command attempt.

### Mint Selection

```yaml
escrow:
  preferred_mint: "https://mint.minibits.cash"
  backup_mints:
    - "https://mint2.example.com"
  mint_health_check_interval: 3600  # seconds
```

The client periodically checks mint health (`GET /v1/info`) and switches to backup mints if the preferred mint is unreachable. Mint capabilities (NUT-10, NUT-11, NUT-14 support) are verified at startup.

### Receipt Tracking

All completed tasks generate receipts stored in the local Receipt Store:

```bash
# View recent receipts
lightning-cli hive-client-receipts --limit=10

# View receipts for a specific advisor (by name)
lightning-cli hive-client-receipts --advisor="Hex Fleet Advisor"

# Export receipts for auditing
lightning-cli hive-client-receipts --since="2026-02-01" --format=json > receipts.json
```

Each receipt links to the escrow ticket, the task command, the execution result, and the HTLC preimage (for completed tasks). This creates a complete audit trail of all management activity and its cost.

---

## 8. Local Policy Engine

### Purpose

The Policy Engine is the operator's **last line of defense**. Even if an advisor presents a valid credential, a valid payment, and a well-formed command, the Policy Engine can reject the action based on locally-defined rules. This is critical because:

- Credentials can be too permissive (operator granted broader access than intended)
- Advisors can make mistakes (valid action, bad judgment)
- Advisors can be adversarial (valid credential, malicious intent)

The Policy Engine enforces the operator's risk tolerance independent of the credential system.

### Default Policy Presets

| Preset | Philosophy | Max Fee Change/24h | Max Rebalance | Forbidden Actions | Confirmation Required |
|--------|-----------|-------------------|--------------|-------------------|----------------------|
| `conservative` | Safety first | ±15% per channel | 100k sats | Channel close, force close, wallet send, plugin start | Danger ≥ 5 |
| `moderate` | Balanced | ±30% per channel | 500k sats | Force close, wallet sweep, plugin start (unapproved) | Danger ≥ 7 |
| `aggressive` | Maximum advisor autonomy | ±50% per channel | 2M sats | Wallet sweep, force close all | Danger ≥ 9 |

### Custom Policy Rules

Operators can define granular rules beyond the presets:

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

#### Protected Channels

Channels in the `protected_channels` list cannot be modified by any advisor. Fee changes, disabling, closing — all rejected. This is useful for critical channels with important peers.

#### Forbidden Peers

Advisors cannot open channels to, connect to, or route through nodes in the `forbidden_peers` list. Protects against advisors routing through known malicious nodes or competitors.

#### Quiet Hours

During quiet hours, only low-danger actions (monitoring, read-only) are permitted. This prevents advisors from making significant changes while the operator is sleeping.

### Confirmation Flow

When the Policy Engine requires confirmation (based on danger score or rule):

```
Advisor ──► Client Plugin ──► Policy Engine
                                    │
                              Requires confirmation
                                    │
                         ┌──────────▼──────────┐
                         │   Alert Operator     │
                         │   (webhook/Nostr/    │
                         │    email)            │
                         └──────────┬──────────┘
                                    │
                              Operator reviews
                                    │
                         ┌──────────▼──────────┐
                         │  Approve / Reject    │
                         │  (via RPC command)   │
                         └──────────┬──────────┘
                                    │
                              ┌─────┴─────┐
                              │           │
                           Approve     Reject
                              │           │
                           Execute    Reject + notify advisor
```

Pending confirmations expire after a configurable timeout (default: 24 hours for danger 5–6, 4 hours for danger 7–8). Expired confirmations are rejected.

```bash
# View pending confirmations
lightning-cli hive-client-status --pending

# Approve a pending action
lightning-cli hive-client-approve --action-id=47

# Reject a pending action
lightning-cli hive-client-approve --action-id=47 --reject --reason="Too aggressive"
```

### Alert Integration

The Policy Engine sends alerts for all advisor actions above a configurable threshold:

| Alert Level | Trigger | Channels |
|------------|---------|----------|
| **info** | Any action executed (danger 1–2) | Digest (daily summary) |
| **notice** | Standard actions (danger 3–4) | Real-time: webhook |
| **warning** | Elevated actions (danger 5–6) | Real-time: webhook + Nostr DM |
| **critical** | High/critical actions (danger 7+) | Real-time: webhook + Nostr DM + email |
| **confirmation** | Action requires approval | All channels + push notification |

Alert channels:

```yaml
alerts:
  webhook: "https://hooks.example.com/hive"
  nostr_dm: "npub1abc..."
  email: "operator@example.com"
  # Future: Telegram, Signal, SMS
```

### Policy Overrides

Operators can temporarily tighten or loosen policy:

```bash
# Temporarily tighten (e.g., during maintenance window)
lightning-cli hive-client-policy --override='{"max_danger": 2}' --duration="4h"

# Temporarily loosen (e.g., for a specific operation)
lightning-cli hive-client-policy --override='{"max_rebalance_sats": 2000000}' --duration="1h"

# Remove override (return to base policy)
lightning-cli hive-client-policy --clear-override
```

Overrides auto-expire after the specified duration. This prevents "forgot to undo the loose policy" scenarios.

---

## 9. Discovery for Non-Hive Nodes

Non-hive nodes cannot use hive gossip for advisor discovery. Four alternative mechanisms are supported, ordered by decentralization:

Non-hive nodes cannot use hive gossip for advisor discovery. The client searches multiple sources automatically and presents unified results. **The operator just types what they need — the client figures out where to look.**

```bash
# Simple search — client queries all available sources automatically
lightning-cli hive-client-discover --capabilities="fee optimization"
```

### Discovery Sources (Under the Hood)

The client searches multiple sources in parallel and merges results:

**1. Archon Network** — Queries for `HiveServiceProfile` credentials. Highest trust — profiles are cryptographically signed, reputation is verifiable.

**2. Nostr** — Subscribes to advisor profile events (kind `38383`, tag `t:hive-advisor`). Medium trust — the client verifies the embedded credential signature and DID-to-Nostr binding.

**3. Curated Directories** — Optional web directories that aggregate profiles. Low trust for the directory; high trust for the verified credentials it surfaces.

**4. Direct Connection** — Operator has an advisor's contact info (from a website, conference, or recommendation):

```bash
# Add an advisor directly by their public identifier
lightning-cli hive-client-authorize --advisor-did="did:cid:bagaaiera..." --access="fee optimization"
```

**5. Referrals** — An existing client or advisor refers someone. Referral reputation is tracked per the [Marketplace spec, Section 8](./DID-HIVE-MARKETPLACE.md#8-referral--affiliate-system).

All discovery results are ranked using the [Marketplace ranking algorithm](./DID-HIVE-MARKETPLACE.md#filtering--ranking-algorithm) and presented as a simple numbered list (see [Discovery Output](#discovery-output) in the Abstraction Layer section).

---

## 10. Onboarding Flow

Step-by-step process for a new node operator to start using professional management:

### Step 1: Install Plugin/Daemon

```bash
# CLN
curl -O https://github.com/lightning-goats/cl-hive-client/releases/latest/cl_hive_client.py
lightning-cli plugin start /path/to/cl_hive_client.py

# LND
curl -LO https://github.com/lightning-goats/hive-lnd/releases/latest/hive-lnd-linux-amd64
hive-lnd init && hive-lnd --config ~/.hive-lnd/hive-lnd.yaml
```

### Step 2: Identity (Automatic)

On first run, the client automatically provisions a DID identity via the configured Archon gateway. **No user action required.** The operator sees:

```
✓ Node identity created (via archon.technology)
✓ Ready to discover advisors
```

> **Advanced users** who already run an Archon node or have an existing DID can configure it manually in the config file. For most operators, auto-provisioning is the right choice. See [Archon Integration Tiers](#archon-integration-tiers) for details.

### Step 3: Discover Advisors

```bash
lightning-cli hive-client-discover --capabilities="fee-optimization,rebalancing"
```

Returns a ranked list of advisors with reputation scores, pricing, and availability.

### Step 4: Review Advisor Reputation

```bash
# View detailed advisor profile and reputation
lightning-cli hive-client-discover --advisor-did="did:cid:advisor..." --detail
```

Review:
- Number of nodes managed and average tenure
- Revenue improvement metrics across clients
- Escrow history (completed tickets, timeouts, disputes)
- Trial period success rate

### Step 5: Select Advisor and Configure Credential

```bash
# Start with a trial period
lightning-cli hive-client-trial \
  --advisor-did="did:cid:advisor..." \
  --duration-days=14 \
  --scope="monitor,fee-policy"
```

### Step 6: Fund Escrow Wallet

```bash
# Check current balance
lightning-cli hive-client-escrow balance

# Mint initial escrow tokens
lightning-cli hive-client-escrow mint --amount=10000
```

### Step 7: Trial Period (7–14 Days)

During the trial:
- Advisor operates with reduced scope (monitor + fee-policy only)
- Flat-fee compensation (no performance bonus)
- Client measures baseline metrics
- Both parties evaluate fit

### Step 8: Review Trial Results

```bash
# View trial metrics
lightning-cli hive-client-trial --review

# Output: actions taken, revenue delta, uptime, response time
```

### Step 9: Full Contract or Terminate

```bash
# If satisfied: upgrade to full credential
lightning-cli hive-client-authorize \
  --advisor-did="did:cid:advisor..." \
  --template="full_routing" \
  --duration-days=90

# If not: terminate trial (no penalty)
lightning-cli hive-client-revoke --advisor-did="did:cid:advisor..."
```

### Step 10: Ongoing Management

With the full credential active:
- Advisor manages the node per contracted scope
- Escrow auto-replenishes
- Policy Engine enforces local rules
- Operator receives alerts for significant actions
- Receipts accumulate for auditing
- At contract end, both parties issue mutual reputation credentials

---

## 11. Hive Membership Upgrade Path

Client-only nodes can upgrade to full hive membership when they want the benefits of fleet coordination.

### What Changes

| Aspect | Client | Full Hive Member |
|--------|--------|-----------------|
| Software | `cl-hive-client` | `cl-hive` (full plugin) |
| Bond | None | 50,000–500,000 sats (per [Settlements spec](./DID-HIVE-SETTLEMENTS.md#bond-sizing)) |
| Gossip | No participation | Full gossip network access |
| Settlement | Direct escrow only | Netting, credit tiers, bilateral/multilateral |
| Fleet rebalancing | N/A | Intra-hive paths (97% fee savings) |
| Pheromone routing | N/A | Full stigmergic signal access |
| Intelligence market | Buy from advisor directly | Full market access (buy/sell) |
| Management fees | Per-action / subscription | Discounted (fleet paths reduce advisor costs) |

### What Stays the Same

- Same management interface (schemas, custom messages, receipt format)
- Same credential system (management credentials work identically)
- Same escrow mechanism (Cashu tickets, same mints)
- Same advisor relationships (existing credentials remain valid)
- Same reputation history (reputation credentials are portable across membership levels)

### Migration Process

```bash
# 1. Install full cl-hive (replaces cl-hive-client)
lightning-cli plugin stop cl_hive_client.py
lightning-cli plugin start cl_hive.py

# 2. Join hive PKI
lightning-cli hive-join --hive-id="<hive_identifier>"

# 3. Post bond
lightning-cli hive-bond --amount=50000 --mint="https://mint.hive.lightning"

# 4. Wait for hive acceptance (bond verification + existing reputation review)
lightning-cli hive-status

# 5. Existing advisor relationships continue unchanged
```

### Incentives to Upgrade

| Benefit | Impact |
|---------|--------|
| Fleet rebalancing paths | 97% cheaper than public routing (per cl-hive pheromone system) |
| Intelligence market access | Buy/sell routing intelligence with other hive members |
| Discounted management | Advisors pass on cost savings from fleet paths |
| Settlement netting | Bilateral/multilateral netting reduces escrow overhead |
| Credit tiers | Long-tenure members get credit lines, reducing pre-payment requirements |
| Governance participation | Vote on hive parameters, schema governance |

---

## 12. Security Considerations

### Attack Surface

The client plugin/daemon introduces a new attack surface on the node:

| Attack Vector | Risk | Mitigation |
|--------------|------|-----------|
| Malicious custom messages from non-advisors | Low — messages from unauthorized DIDs are rejected at credential check | Credential Verifier is the first check; messages without valid credentials never reach the Schema Handler |
| Compromised advisor credential | Medium — advisor could execute damaging actions within credential scope | Policy Engine limits blast radius; credential scope is narrow; revocation is instant |
| Compromised Archon Keymaster | High — attacker could issue credentials | Keymaster passphrase protection; key material never leaves the operator's machine |
| Malicious mint | Medium — escrow tokens could be stolen | Multi-mint strategy; operator controls which mints are trusted; pre-flight token verification |
| DID resolution poisoning | Low — attacker provides false DID documents | Multiple Archon gateways for verification; local cache with TTL |
| Policy Engine bypass | Critical if possible — but code is local, operator-controlled | Open-source auditable code; policy is enforced locally, not by the advisor |

### Malicious Advisor Protections

Assume the worst: the advisor is adversarial. Defense layers, from outermost to innermost:

1. **Credential scope** — The blast radius is limited to the schemas and constraints in the credential. A `fee_optimization` credential cannot close channels.

2. **Policy Engine** — Even within credential scope, the Policy Engine enforces operator-defined limits. Max fee change per period, max rebalance amount, forbidden peers, quiet hours.

3. **Spending limits** — Escrow expenditure is capped daily and weekly. An adversarial advisor cannot drain the operator's escrow wallet.

4. **Confirmation requirements** — High-danger actions require explicit operator approval. The advisor cannot auto-execute anything above the configured danger threshold.

5. **Rate limiting** — Actions are rate-limited per hour and per day. An advisor cannot flood the node with rapid-fire commands.

6. **Audit trail** — Every action is logged in the tamper-evident Receipt Store. The operator can review what the advisor did and when.

7. **Instant revocation** — One command (`hive-client-revoke`) immediately invalidates the advisor's credential. Fail-closed: if Archon is unreachable for revocation check, all commands are denied.

### What Advisors Can Never Do

Regardless of credential scope or Policy Engine configuration:

- **Access private keys** — The client never exposes node private keys, seed phrases, or HSM secrets to advisors
- **Modify the client software** — Advisors interact via the schema interface only; they cannot change plugin code or configuration
- **Bypass the Policy Engine** — Policy is enforced locally; the advisor has no mechanism to disable it
- **Access other advisors' credentials** — Multi-advisor isolation is enforced by the client
- **Persist access after revocation** — Revocation is instant and fail-closed

### Audit Log

The Receipt Store serves as a tamper-evident audit log:

- **Hash chaining** — Each receipt includes the hash of the previous receipt. Modifying any receipt breaks the chain.
- **Dual signatures** — Both the agent's DID and the node sign each receipt. Neither party can forge a receipt alone.
- **Periodic merkle roots** — Hourly/daily merkle roots are computed and optionally published (e.g., to Archon or Nostr) for external timestamping.
- **Export** — Receipts can be exported for independent audit at any time.

### Network-Level Security

- **Bolt 8 encryption** — All management traffic uses Noise_XK with forward secrecy. Management commands are invisible to network observers.
- **No cleartext management traffic** — The client never sends management commands over unencrypted channels.
- **Custom message types are odd** (49153, 49155) — Per BOLT 1, non-hive peers simply ignore these messages. No information leakage to uninvolved peers.

---

## 13. Comparison: Client vs Hive Member vs Unmanaged

### Feature Comparison

| Feature | Unmanaged | Client | Hive Member |
|---------|-----------|--------|-------------|
| Fee optimization | Manual | ✓ (advisor) | ✓ (advisor + fleet intel) |
| Rebalancing | Manual | ✓ (advisor) | ✓ (advisor + 97% cheaper paths) |
| Channel expansion | Manual | ✓ (advisor proposals) | ✓ (advisor + hive coordination) |
| Monitoring | DIY tools | ✓ (advisor + client alerts) | ✓ (advisor + hive health) |
| HTLC resolution | Manual | ✓ (advisor, if admin tier) | ✓ (advisor + fleet coordination) |
| Pheromone routing | ✗ | ✗ | ✓ |
| Intelligence market | ✗ | ✗ (advisor provides) | ✓ (full market) |
| Settlement netting | ✗ | ✗ | ✓ |
| Credit tiers | ✗ | ✗ | ✓ |
| Governance | ✗ | ✗ | ✓ |
| Reputation earned | ✗ | ✓ (`hive:client`) | ✓ (`hive:node`) |
| DID identity | Optional | Required | Required |
| Local policy engine | ✗ | ✓ | ✓ |
| Audit trail | ✗ | ✓ | ✓ |

### Cost Comparison

| Model | Upfront | Ongoing | Revenue Impact |
|-------|---------|---------|----------------|
| **Unmanaged** | 0 sats | 0 sats | Baseline (leaving 50–200% revenue on table) |
| **Client** | 0 sats | 2,000–50,000 sats/month (per advisor pricing) | +50–300% revenue improvement (varies by advisor quality) |
| **Hive Member** | 50,000–500,000 sats (bond) | 1,000–30,000 sats/month (discounted via fleet) | +100–500% revenue improvement (fleet intelligence + cheaper rebalancing) |

Bond is recoverable (minus any slashing) on hive exit.

### Risk Comparison

| Risk | Unmanaged | Client | Hive Member |
|------|-----------|--------|-------------|
| Adversarial advisor | N/A | Policy Engine + credential scope + escrow limits | Same + bond forfeiture for hive-attested advisors |
| Fund loss from mismanagement | Self-inflicted | Limited by Policy Engine constraints | Same + fleet cross-checks |
| Privacy | Full control | Advisor sees channel data (within credential scope) | Hive sees aggregate data; advisor sees detail |
| Lock-in | None | None (switch advisors anytime) | Bond lock-up (6-month default) |
| Dependency | None | Advisor uptime (mitigated by monitoring fallback) | Advisor + hive infrastructure |

### When to Use Each Model

| Scenario | Recommendation |
|----------|---------------|
| Hobbyist, < 5 channels, no revenue goal | Unmanaged |
| Small-medium node, wants optimization, low commitment | **Client** with `fee_optimization` template |
| Medium node, wants full management, growing fleet | **Client** with `full_routing` template |
| Large routing node, wants fleet benefits, willing to post bond | **Hive Member** |
| Professional routing business, multiple nodes | **Hive Member** (founding/full) |

---

## 14. Implementation Roadmap

Phased delivery, aligned with the other specs' roadmaps. The client is designed to be useful early — even Phase 1 provides value.

### Phase 1: Core Client (4–6 weeks)
*Prerequisites: Fleet Management Phase 1–2 (schemas + DID auth)*

- `cl-hive-client` Python plugin with Schema Handler and Credential Verifier
- Custom message handling (types 49153/49155)
- Basic Policy Engine (presets only)
- Receipt Store (SQLite, hash-chained)
- RPC commands: `hive-client-status`, `hive-client-authorize`, `hive-client-revoke`, `hive-client-receipts`
- CLN schema translation for categories 1–4 (monitor, fee-policy, HTLC policy, forwarding)

### Phase 2: Escrow Integration (3–4 weeks)
*Prerequisites: Task Escrow Phase 1 (single tickets)*

- Built-in Cashu wallet (NUT-10/11/14)
- Escrow ticket creation and management
- Auto-replenishment
- Spending limits
- `hive-client-escrow` RPC command

### Phase 3: Full Schema Coverage (3–4 weeks)
*Prerequisites: Phase 1*

- Schema translation for categories 5–15 (rebalancing through emergency)
- Feature capability advertisement
- Danger score integration with Policy Engine

### Phase 4: LND Daemon (4–6 weeks)
*Prerequisites: Phase 1–3 (proven design from CLN)*

- `hive-lnd` Go daemon with all components
- LND gRPC integration for all schema categories
- Schema translation layer (CLN → LND equivalents)
- `HiveClientService` gRPC API
- CLI tool and systemd integration

### Phase 5: Discovery & Onboarding (3–4 weeks)
*Prerequisites: Marketplace Phase 1 (service profiles)*

- `hive-client-discover` with Archon, Nostr, and directory sources
- `hive-client-trial` for trial period management
- Onboarding wizard (interactive CLI)
- Referral discovery support

### Phase 6: Advanced Policy & Alerts (2–3 weeks)
*Prerequisites: Phase 1*

- Custom policy rules (beyond presets)
- Confirmation flow for high-danger actions
- Alert integration (webhook, Nostr DM, email)
- Quiet hours, protected channels, forbidden peers
- Policy overrides with auto-expiry

### Phase 7: Multi-Advisor & Upgrade Path (2–3 weeks)
*Prerequisites: Phase 1, Marketplace Phase 4 (multi-advisor)*

- Multi-advisor scope isolation
- Conflict detection
- Hive membership upgrade flow
- Migration tooling (client → full member)

### Cross-Spec Integration

```
Fleet Mgmt Phase 1-2  ──────────►  Client Phase 1 (core client)
                                         │
Task Escrow Phase 1    ──────────►  Client Phase 2 (escrow)
                                         │
Fleet Mgmt Phase 3     ──────────►  Client Phase 3 (full schemas)
                                         │
Client Phase 1-3       ──────────►  Client Phase 4 (LND daemon)
                                         │
Marketplace Phase 1    ──────────►  Client Phase 5 (discovery)
```

---

## 15. Open Questions

1. **Keymaster packaging:** Should the Archon Keymaster be bundled with the client plugin/daemon, or remain a separate dependency? Bundling reduces friction but increases maintenance burden.

2. **Auto-replenishment funding source:** Should auto-replenishment draw from the node's on-chain wallet (simple, requires on-chain funds) or via Lightning invoice (more complex, uses existing liquidity)? Both have tradeoffs.

3. **LND HTLC management:** LND lacks `dev-fail-htlc`-style commands. The `HtlcInterceptor` API provides similar functionality but requires the daemon to intercept all HTLCs, which has performance implications. Is this acceptable for production use?

4. **Policy Engine complexity:** How many custom rules are too many? A complex policy is harder to audit and may have unexpected interactions between rules. Should we limit the number of custom rules or provide rule conflict detection?

5. **Multi-implementation testing:** The Schema Translation Layer assumes specific RPC behavior from CLN and LND. How do we test correctness across both implementations, especially for edge cases (concurrent operations, error handling)?

6. **Advisor-side client library:** This spec focuses on the node operator's client. Should there be a corresponding advisor-side library/SDK that simplifies building advisors? Or is the schema spec sufficient?

7. **Offline operation:** If the Archon gateway is unreachable, the client denies all commands (fail-closed). This is safe but could deny service during Archon outages. Should there be a cached-credential mode for short outages, with degraded trust?

8. **Cross-implementation credentials:** A credential issued for a CLN node should work if the operator migrates to LND (same DID, same node pubkey). Are there edge cases where implementation-specific credential constraints break?

9. **Client-to-client communication:** Could client nodes discover and communicate with each other (e.g., for referral-based reputation, cooperative rebalancing) without full hive membership? This would create a "light hive" network.

10. **Tiered client product:** Should there be a free tier (monitor-only, limited discovery) and a paid tier (full management, priority discovery)? Or should the client software be fully open and free, with advisors as the only revenue source?

---

## 16. References

- [DID + L402 Remote Fleet Management](./DID-L402-FLEET-MANAGEMENT.md) — Schema definitions, credential format, transport protocol, danger scoring
- [DID + Cashu Task Escrow Protocol](./DID-CASHU-TASK-ESCROW.md) — Escrow ticket format, HTLC conditions, ticket types
- [DID Hive Marketplace Protocol](./DID-HIVE-MARKETPLACE.md) — Service profiles, discovery, negotiation, contracting, multi-advisor coordination
- [DID + Cashu Hive Settlements Protocol](./DID-HIVE-SETTLEMENTS.md) — Bond system, settlement types, credit tiers
- [DID Reputation Schema](./DID-REPUTATION-SCHEMA.md) — Reputation credential format, `hive:advisor` and `hive:client` profiles
- [CLN Plugin Documentation](https://docs.corelightning.org/docs/plugin-development)
- [CLN Custom Messages](https://docs.corelightning.org/reference/lightning-sendcustommsg)
- [CLN `setchannel` RPC](https://docs.corelightning.org/reference/lightning-setchannel)
- [CLN `listpeerchannels` RPC](https://docs.corelightning.org/reference/lightning-listpeerchannels)
- [LND gRPC API Reference](https://api.lightning.community/)
- [LND `lnrpc.UpdateChannelPolicy`](https://api.lightning.community/#updatechannelpolicy)
- [LND `routerrpc.SendPaymentV2`](https://api.lightning.community/#sendpaymentv2)
- [LND Custom Messages](https://api.lightning.community/#sendcustommessage)
- [Cashu NUT-10: Spending Conditions](https://github.com/cashubtc/nuts/blob/main/10.md)
- [Cashu NUT-11: Pay-to-Public-Key](https://github.com/cashubtc/nuts/blob/main/11.md)
- [Cashu NUT-14: Hashed Timelock Contracts](https://github.com/cashubtc/nuts/blob/main/14.md)
- [W3C DID Core 1.0](https://www.w3.org/TR/did-core/)
- [W3C Verifiable Credentials Data Model 2.0](https://www.w3.org/TR/vc-data-model-2.0/)
- [Archon: Decentralized Identity for AI Agents](https://github.com/archetech/archon)
- [BOLT 1: Base Protocol](https://github.com/lightning/bolts/blob/master/01-messaging.md) — Custom message type rules (odd = optional)
- [BOLT 8: Encrypted and Authenticated Transport](https://github.com/lightning/bolts/blob/master/08-transport.md)
- [Lightning Hive: Swarm Intelligence for Lightning](https://github.com/lightning-goats/cl-hive)

---

*Feedback welcome. File issues on [cl-hive](https://github.com/lightning-goats/cl-hive) or discuss in #singularity.*

*— Hex ⬡*
