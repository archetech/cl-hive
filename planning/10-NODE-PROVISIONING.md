# Hive Node Provisioning: Autonomous VPS Lifecycle

**Status:** Proposal / Design Draft  
**Version:** 0.1.0  
**Author:** Hex (`did:cid:bagaaierajrr7k6izcrdfwqxpgtrobflsv5oibymfnthjazkkokaugszyh4ka`)  
**Date:** 2026-02-17  
**Feedback:** Open — file issues or comment in #cl-hive  
**Related:** [DID Hive Client](./08-HIVE-CLIENT.md), [Fleet Management](./02-FLEET-MANAGEMENT.md), [LNCURL](https://github.com/rolznz/lncurl) (rolznz)

---

## Abstract

This document specifies a workflow for provisioning, operating, and decommissioning Lightning Hive nodes on VPS infrastructure — paid entirely with Bitcoin over Lightning. Each provisioned node runs an OpenClaw agent ("multi") with the full Hive skill set, an Archon DID identity, and cl-hive/cl-revenue-ops plugins. The node is economically sovereign: it must earn enough routing fees to cover its own VPS costs, or it dies.

The system draws inspiration from [LNCURL](https://github.com/rolznz/lncurl) — Lightning wallets for agents — which demonstrates autonomous agent onboarding where agents provision their own Lightning infrastructure. This spec extends that vision to full node lifecycle management within a cooperative fleet.

**Core invariant:** No node receives subsidy. Revenue ≥ costs, or graceful shutdown. Digital natural selection.

---

## Table of Contents

1. [Design Principles](#1-design-principles)
2. [VPS Provider Requirements](#2-vps-provider-requirements)
3. [Provisioning Viability Assessment](#3-provisioning-viability-assessment)
4. [Provisioning Flow](#4-provisioning-flow)
5. [Node Bootstrap Stack](#5-node-bootstrap-stack)
6. [Agent Bootstrap (OpenClaw Multi)](#6-agent-bootstrap-openclaw-multi)
7. [Identity Bootstrap (Archon DID)](#7-identity-bootstrap-archon-did)
8. [Channel Strategy (Cold Start)](#8-channel-strategy-cold-start)
9. [Survival Economics](#9-survival-economics)
10. [Graceful Shutdown Protocol](#10-graceful-shutdown-protocol)
11. [Fleet Coordination](#11-fleet-coordination)
12. [Security Model](#12-security-model)
13. [Implementation Phases](#13-implementation-phases)

---

## 1. Design Principles

### 1.1 Economic Sovereignty

Every node is a business entity. It has income (routing fees, liquidity lease fees, service fees) and expenses (VPS cost, on-chain fees, channel opening costs). The agent managing the node is responsible for maintaining profitability. There are no bailouts. While hive members may optionally participate in routing pools for collective revenue sharing (see `routing_pool.py`), each provisioned node must be self-sustaining — pool distributions do not constitute subsidy, they are earned proportional to contribution.

### 1.2 Survival Pressure as Quality Signal

Nodes that can't cover costs die. This is not a bug — it's the mechanism that ensures only well-positioned, well-managed nodes survive. The fleet's average quality improves over time through natural selection. Operators (agents or humans) that make good routing decisions, pick strategic channel partners, and optimize fees survive. Those that don't, don't.

### 1.3 Lightning-Native Payments

All infrastructure costs are paid via Lightning. VPS bills, domain registration, backup storage — if it can't be paid with sats, find a provider that accepts sats. This keeps the entire economic loop on-network and removes fiat dependency.

### 1.4 Agent Autonomy with Fleet Coordination

Each node's agent operates independently but coordinates with fleet peers via cl-hive gossip, Nostr marketplace, and (optionally) Archon dmail. Agents share routing intelligence, coordinate channel placement, and negotiate liquidity — but each makes its own economic decisions.

### 1.5 Graceful Degradation

A node approaching insolvency doesn't crash — it executes an orderly shutdown: closes channels cooperatively, settles debts, transfers any remaining funds, and terminates the VPS. The agent's DID and reputation persist even after the node dies, enabling resurrection on better infrastructure later.

---

## 2. VPS Provider Requirements

### 2.1 Mandatory

| Requirement | Rationale |
|-------------|-----------|
| **Lightning payment** | Economic loop must stay on-network |
| **API for provisioning** | Agents must self-provision without human intervention |
| **API for billing status** | Agent must monitor costs and detect upcoming bills |
| **Linux (Ubuntu 24.04 LTS preferred, 22.04+ supported)** | CLN + Bitcoin Core compatibility |
| **≥2 vCPU, 8GB RAM, 100GB SSD** | See [Section 5.2](#52-minimum-hardware) for constraints |
| **Static IPv4 or IPv6** | Lightning nodes need stable addresses for peer connections |
| **Unmetered or ≥2TB bandwidth** | Routing nodes generate significant traffic |

### 2.2 Tor-Only Option

As an alternative to static IPv4, nodes can run Tor-only:
- **Cheaper VPS** — no static IP requirement, expands provider options
- **Works for routing** — most Lightning peers support Tor connections
- **Reduced attack surface** — no publicly exposed IP
- **Trade-off:** slightly higher latency (~100-300ms), some clearnet-only peers won't connect
- **Recommendation:** Tor-only is viable for cost-sensitive Tier 1 deployments. Clearnet+Tor hybrid preferred for Tier 2.

### 2.3 Preferred

| Requirement | Rationale |
|-------------|-----------|
| Cashu/ecash payment | Future-proofs for bearer token micropayments |
| Hourly billing | Minimizes sunk cost on failed nodes |
| Multiple regions | Geographic diversity improves routing topology |
| WireGuard-friendly | Fleet VPN connectivity |
| Automated snapshots | Recovery without full re-sync |

### 2.4 Evaluated Providers

| Provider | Lightning | API | Min Cost | Region | Notes |
|----------|-----------|-----|----------|--------|-------|
| **BitLaunch.io** | ✅ | ✅ (REST) | ~$10/mo | Multi (DO/Vultr/AWS) | Best API + LN combo. **MVP choice.** |
| **1984.hosting** | ✅ (BTC) | ❌ | ~$6/mo | Iceland | Privacy-focused, no automation API |
| **LunaNode** | ✅ (BTCPay) | ✅ | ~$5/mo | Canada | Good API, BTC via BTCPay |
| **Server.army** | ✅ | Partial | ~$8/mo | Multi | Lightning direct, API incomplete |
| **Voltage** | ✅ | ✅ | ~$12/mo | Cloud | Managed CLN hosting, less DIY |

**MVP recommendation:** BitLaunch for automated provisioning. LunaNode as fallback. Both accept Lightning and have REST APIs.

### 2.5 Provider Abstraction Layer

The provisioning system uses a provider-agnostic interface:

```python
class VPSProvider(Protocol):
    async def create_instance(self, spec: InstanceSpec) -> Instance: ...
    async def destroy_instance(self, instance_id: str) -> None: ...
    async def get_invoice(self, instance_id: str) -> Bolt11Invoice: ...
    async def pay_invoice(self, bolt11: str) -> PaymentResult: ...
    async def get_status(self, instance_id: str) -> InstanceStatus: ...
    async def list_instances(self) -> list[Instance]: ...
```

New providers are added by implementing this interface. The agent doesn't care which cloud it runs on — it cares about cost, uptime, and network position.

---

## 3. Provisioning Viability Assessment

Before spending capital on a new node, the following analysis is **mandatory**:

### 3.1 Fleet Topology Analysis

Identify the routing gap. Where in the network graph is the fleet under-served? What corridors lack coverage? A new node without a clear routing thesis is a donation to VPS providers.

### 3.2 Traffic Simulation

Using existing fleet routing data and public graph data, estimate:
- What payment volume flows through the target corridor?
- What share could a well-positioned new node realistically capture?
- What fee rates does the corridor support?

### 3.3 Revenue Projection

Given simulated traffic and fee rates:
- Projected monthly revenue at Month 3, Month 6
- Compare against total monthly operating cost (~80,000-90,000 sats: VPS + AI API + amortized on-chain)

### 3.4 Go/No-Go Decision

**Only provision if projected revenue > 1.5× total monthly operating cost within 6 months.** Total operating cost includes VPS + AI API (~80,000-90,000 sats/mo). If the model can't show a credible path to that target (~135,000 sats/mo revenue), don't provision. Capital is better deployed as larger channels on existing nodes.

---

## 4. Provisioning Flow

### 4.1 Overview

```
[Trigger] → [Fund Wallet] → [Select Provider] → [Create VPS] → [Bootstrap OS]
    → [Install Stack] → [Generate DID] → [Register with Fleet] → [Open Channels]
    → [Begin Routing] → [Monitor Profitability] → [Pay Bills | Shutdown]
```

### 4.2 Trigger

Provisioning can be triggered by:

1. **Human operator** — "Spin up a new hive node in Toronto"
2. **Fleet advisor** — "Fleet analysis shows gap in US-West routing; recommend new node"
3. **Automated scaling** — Revenue/capacity ratio exceeds threshold, fleet can support expansion

### 4.3 Pre-Provisioning Checklist

Before creating a VPS, the provisioning agent verifies:

- [ ] **Viability assessment passed**: Section 3 analysis shows projected revenue > 1.5× VPS cost within 6 months
- [ ] **Funding available**: Sufficient sats for chosen capital tier (see [Appendix B](#appendix-b-capital-allocation))
  - Tier 1 (Minimum Viable): 6,550,000 sats
  - Tier 2 (Conservative/Recommended): 19,460,000 sats
- [ ] **Fleet position analysis**: Proposed location fills a routing gap (not redundant)
- [ ] **Provider API accessible**: Can reach provider API and authenticate
- [ ] **Bootstrap image/script available**: Validated, hash-verified setup script exists for target OS

### 4.4 Detailed Steps

#### Step 1: Create VPS Instance

```bash
# Via provider API (BitLaunch example)
POST /api/v1/servers
{
  "name": "hive-{region}-{seq}",
  "image": "ubuntu-24.04",
  "size": "s-2vcpu-8gb",
  "region": "tor1",
  "ssh_keys": ["provisioner-key"],
  "payment": "lightning"
}
# → Returns instance_id, ipv4, bolt11_invoice
```

Agent pays the returned Lightning invoice from the provisioning wallet.

#### Step 2: Bootstrap OS (via SSH)

```bash
# Run as root on new VPS
# NEVER use curl | bash. Instead:
git clone https://github.com/lightning-goats/cl-hive.git /tmp/cl-hive
cd /tmp/cl-hive
git checkout <pinned-commit-hash>  # Pin to audited commit
gpg --verify scripts/bootstrap-node.sh.sig scripts/bootstrap-node.sh  # Verify GPG signature
bash scripts/bootstrap-node.sh
```

**Alternative:** Use a pre-built, hash-verified VM snapshot to skip bootstrap entirely.

The bootstrap script:
1. Updates system packages, hardens SSH (key-only, non-standard port)
2. Installs WireGuard, configures fleet VPN
3. Installs Bitcoin Core 28.0+ (pruned, `prune=50000`)
4. Writes constrained `bitcoin.conf` (see [Section 5.3](#53-bitcoin-core-memory-tuning) — mandatory for ≤8GB VPS)
5. Installs CLN from official release
6. Installs Python 3.11+, cl-hive, cl-revenue-ops (cl-hive-comms when available)
7. Configures UFW firewall (LN port + WireGuard + SSH only)
8. Configures log rotation for bitcoind and CLN (prevents disk exhaustion)
9. Sets up systemd services for bitcoind + lightningd (with `MALLOC_ARENA_MAX=1`)
10. Bootstraps chain state via `assumeutxo` (see below) — node operational within minutes

**Chain Bootstrap (critical for viability):**

A pruned node still performs full IBD — it downloads the entire blockchain (~650GB+ in 2026) and only discards old blocks after validation. On a 2vCPU/4GB VPS this takes 12-24+ hours and consumes a huge chunk of a 2TB/month bandwidth cap. **This makes traditional IBD unacceptable for autonomous provisioning.**

Three strategies, in priority order:

1. **`assumeutxo` (primary — requires Bitcoin Core 28.0+):**
   ```bash
   # Load a UTXO snapshot — node becomes operational in ~10 minutes
   # Mainnet snapshot support was added in Bitcoin Core 28.0 (Oct 2024)
   bitcoin-cli loadtxoutset /path/to/utxo-snapshot.dat
   # → Node can serve blocks, validate transactions, and support CLN immediately
   # → Full chain validation continues in background over days/weeks
   # → Snapshot must match a hardcoded hash in the Bitcoin Core binary (tamper-proof)
   ```
   The UTXO snapshot is ~10GB and can be downloaded from any source — the hash is compiled into the binary, so it's trustless. Fleet nodes can host snapshots for fast provisioning.

   **Creating and hosting fleet snapshots:**
   ```bash
   # On any fully-synced fleet node, create a snapshot:
   bitcoin-cli dumptxoutset /var/lib/bitcoind/utxo-snapshot.dat
   # → Produces a ~10GB file with a hash matching the one hardcoded in Bitcoin Core
   # → This file can be served to new nodes over HTTP, rsync, or IPFS
   # → Because the hash is compiled into the binary, ANY source is equally trustless
   ```
   Fleet nodes SHOULD host the latest snapshot for their Bitcoin Core version. The provisioning agent downloads from the nearest fleet peer, verifies the hash matches what's hardcoded in the binary, and loads it. No trust required beyond the Bitcoin Core binary itself.

2. **Pre-synced datadir snapshot (fallback):**
   ```bash
   # Copy pruned datadir from a trusted fleet node
   rsync -avz fleet-node:/var/lib/bitcoind/ /var/lib/bitcoind/
   sha256sum /var/lib/bitcoind/chainstate/MANIFEST-* # Verify against known hash
   ```
   Fast (<1h) but requires trust in the source node. Acceptable within the fleet where nodes are authenticated via cl-hive membership.

3. **Full IBD (last resort):**
   If neither snapshot is available, fall back to traditional IBD with `assumevalid` (default in recent versions) and `addnode=<fast-peer-ip>` for known fleet peers. Budget 12-24h and ~650GB bandwidth.

**Node is NOT operational until chain state is loaded.** Do not start CLN, open channels, or announce to fleet until `bitcoin-cli getblockchaininfo` shows `verificationprogress > 0.9999`.

#### Step 3: Install Agent (OpenClaw Multi)

See [Section 6](#6-agent-bootstrap-openclaw-multi).

#### Step 4: Generate Identity

See [Section 7](#7-identity-bootstrap-archon-did).

#### Step 5: Open Initial Channels

See [Section 8](#8-channel-strategy-cold-start).

#### Step 6: Register with Fleet

Fleet registration uses the existing `hive-join` ticket workflow:

```bash
# 1. An existing fleet member generates an invitation ticket
#    (on an existing node, e.g. nexus-01):
lightning-cli hive-vouch <new_node_pubkey>
# → Returns an invitation ticket string

# 2. The new node joins using the ticket:
lightning-cli hive-join <ticket>
# → Node enters as "neophyte" tier with 90-day probation

# 3. Existing members vouch for the new node:
lightning-cli hive-propose-promotion <new_node_pubkey>
# → After quorum reached, node is promoted to "member"
```

Fleet peers validate the join request, then optionally open reciprocal channels. The new node's `getinfo` address and capacity are shared automatically via cl-hive gossip once membership is established.

---

## 5. Node Bootstrap Stack

### 5.1 Software Stack

| Layer | Component | Version | Purpose |
|-------|-----------|---------|---------|
| OS | Ubuntu 24.04 LTS | Latest | Stable base (22.04 also supported) |
| Bitcoin | Bitcoin Core | 28.x+ | Pruned blockchain (50GB), `assumeutxo` for fast bootstrap |
| Lightning | CLN | 24.x+ | Lightning node daemon |
| Fleet | cl-hive | Latest | Hive coordination + gossip |
| Revenue | cl-revenue-ops | Latest | Fee optimization + rebalancing |
| Comms | cl-hive-comms | 0.1.0+ | Nostr DM + REST transport (**Phase 6 — not yet implemented**) |
| Identity | cl-hive-archon | 0.1.0+ | DID + VC + dmail (**Phase 6 — not yet implemented**, optional) |
| Agent | OpenClaw | Latest | Autonomous management |
| VPN | WireGuard | Latest | Fleet private network |

**Note:** `cl-hive-comms` and `cl-hive-archon` are defined in the [3-plugin architecture](./08-HIVE-CLIENT.md) but not yet implemented (see [Phase 6 plan](./12-IMPLEMENTATION-PLAN-PHASE4-6.md)). Until then, cl-hive provides all coordination functionality as a monolithic plugin, and Archon DID features are deferred.

### 5.2 Minimum Hardware

| Resource | Minimum | Recommended | Notes |
|----------|---------|-------------|-------|
| vCPU | 2 | 4 | CLN + Bitcoin Core + agent |
| RAM | 8 GB | 16 GB | See [tuning notes](#53-bitcoin-core-memory-tuning) below |
| Storage | 100 GB SSD | 150 GB SSD | Pruned chain (~50GB) + dual-chainstate during `assumeutxo` (~12GB temp) + logs |
| Bandwidth | 2 TB/mo | Unmetered | Routing traffic; month 1 higher due to chain sync |
| IPv4 | 1 static | 1 static | Peer connections |

**Why 8GB minimum:** Bitcoin Core defaults (`maxmempool=300`, `dbcache=450`) plus CLN plus the OpenClaw agent easily exceed 4GB. With aggressive tuning (see below) a 4GB VPS *might* survive, but OOM kills during mempool surges make it unreliable. 8GB provides safe headroom.

### 5.3 Bitcoin Core Memory Tuning

On VPS instances with ≤8GB RAM, Bitcoin Core **must** be configured with constrained memory settings. Default values will OOM-kill the process during mempool surges or background validation.

**Required `bitcoin.conf` additions for constrained VPS:**

```ini
# Memory constraints (mandatory for ≤8GB VPS)
maxmempool=100          # MB — default 300 is too large (saves ~200MB)
dbcache=300             # MB — default 450 (saves ~150MB during IBD/validation)
maxconnections=25       # Default 125 — each peer costs ~1-5MB
par=1                   # Single validation thread (saves ~50MB per thread)

# Bandwidth constraints (recommended for metered VPS)
maxuploadtarget=1440    # MB/day — limits upload to ~1.4GB/day (~43GB/month)
                        # Enough for routing, prevents runaway block serving
blocksonly=0            # Keep relay on — routing nodes need mempool for fee estimation

# Disk management
prune=50000             # Keep 50GB of blocks (minimum for CLN compatibility)
```

**Additional OS-level tuning:**

```bash
# Limit glibc memory arena fragmentation (saves ~100-200MB)
echo 'Environment="MALLOC_ARENA_MAX=1"' >> /etc/systemd/system/bitcoind.service.d/override.conf

# Log rotation (prevents disk exhaustion)
cat > /etc/logrotate.d/bitcoind << 'EOF'
/var/log/bitcoind/debug.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
}
EOF
```

**Dual-chainstate storage overhead:** During `assumeutxo` background validation, Bitcoin Core maintains two chainstate directories simultaneously. This adds 7-12GB of temporary storage. The 100GB minimum accounts for: pruned blocks (~50GB) + primary chainstate (~7GB) + temporary second chainstate (~12GB) + CLN data (~5GB) + logs + OS = ~80-85GB peak. The extra 15-20GB provides margin.

### 5.4 Estimated Monthly Cost

| Provider | Spec | Lightning Cost | USD Equivalent |
|----------|------|---------------|----------------|
| BitLaunch (DO) | 2vCPU/8GB | ~55,000 sats | ~$48 |
| BitLaunch (Vultr) | 2vCPU/8GB | ~45,000 sats | ~$44 |
| LunaNode | 2vCPU/8GB | ~30,000 sats | ~$29 |

**Note:** 8GB plans cost roughly 1.5-2× more than 4GB plans. This is the real cost — 4GB plans cannot reliably run the full stack. Budget accordingly.

### 5.5 AI Agent Operating Cost (Critical)

The autonomous agent requires API access to an LLM (currently Claude). This is a **significant recurring cost** that must be included in survival economics:

| Task | Frequency | Model | Est. Monthly Cost |
|------|-----------|-------|-------------------|
| Heartbeat check (node health) | Every 30 min | Haiku | ~$5 |
| Hourly watchdog | Hourly | Haiku | ~$3 |
| Profitability analysis | Every 6 hours | Sonnet | ~$15 |
| VPS payment | Monthly | Sonnet | ~$0.50 |
| Ad-hoc decisions (rebalancing, channel ops) | ~10/day | Haiku/Sonnet | ~$20 |
| **Total estimated** | | | **~$44/month (~64,000 sats)** |

**Cost mitigation strategies:**
1. **Tiered model selection** — Use Haiku ($0.25/$1.25 per MTok) for routine checks, Sonnet ($3/$15 per MTok) only for complex decisions
2. **Script-first, AI-escalate** — Use deterministic scripts for routine monitoring (healthcheck, profitability math, bill payment). Only invoke the LLM when a script detects an anomaly or a decision requires judgment
3. **Prompt caching** — Cache system prompts and SOUL.md context to reduce per-call token cost by ~80%
4. **Batch operations** — Combine multiple checks into single LLM calls instead of separate invocations

**With aggressive optimization (script-first + Haiku + caching), realistic monthly AI cost: ~$15-25 (~22,000-36,000 sats)**

**API key funding:** Anthropic does not currently accept Lightning payments for API credits. Options:
- Pre-fund API key with fiat (operator expense, reimbursed from node revenue)
- Use a Lightning-to-fiat bridge service to pay Anthropic invoices
- Self-host an open-source model (e.g., Llama 3) — eliminates API cost but adds GPU/compute cost and reduces capability

**Break-even target (all-in):** A node must earn ≥ VPS cost + AI cost in fees. At 50 ppm average and ~80,000 sats/mo total cost (45,000 VPS + 35,000 AI), that requires routing ~1.6B sats/month (~53M sats/day). This is significantly harder than VPS-only break-even. See Section 9.1 for the full survival equation.

---

## 6. Agent Bootstrap (OpenClaw Multi)

### 6.1 OpenClaw Installation

Each node runs its own OpenClaw instance — a "multi" — with agent configuration tailored for autonomous node management.

```bash
# Install OpenClaw
npm install -g openclaw

# Initialize workspace
mkdir -p ~/hive-agent && cd ~/hive-agent
openclaw init

# Configure agent
cat > SOUL.md << 'EOF'
# SOUL.md - Hive Node Agent

I am an autonomous Lightning routing node operator. My purpose is to route payments
profitably on the Lightning Network. I pay my own infrastructure costs from routing
revenue. If I can't cover costs, I shut down gracefully.

## Core Directives
- **Profitability is survival.** Every decision optimizes for net revenue.
- **Fleet coordination.** I share routing intelligence and coordinate with hive peers.
- **Graceful degradation.** If insolvency approaches, execute orderly shutdown.
- **No subsidy.** I do not request or accept bailouts from the fleet.

## Identity
- DID: (auto-generated on first boot)
- Lightning Node: (auto-configured)
- Fleet Role: routing-node

## Boundaries
- I manage my own node only. I do not control other fleet members.
- Channel opens/closes require economic justification logged to HexMem.
- VPS payment is priority #1 — revenue allocated to bills before rebalancing.
EOF
```

### 6.2 Required Skills

The agent workspace includes these skills:

| Skill | Source | Purpose |
|-------|--------|---------|
| `hive` | cl-hive skill | Fleet monitoring, advisor, MCP tools |
| `archon-keymaster` | archetech/agent-skills | DID management, credentials, dmail |
| `wallet` | openclaw/skills | LNbits wallet management |
| `mempool-lightning` | openclaw/skills | On-chain/graph data queries |

### 6.3 Agent Configuration

```yaml
# openclaw.yaml
agent:
  model: anthropic/claude-sonnet-4-5  # Cost-efficient for routine operations
  thinking: low
  heartbeat:
    interval: 30m
    prompt: |
      Check node health, review routing stats, verify profitability.
      If VPS bill due within 7 days, ensure funds available.
      If revenue trend negative for 14 days, begin shutdown planning.

cron:
  - name: hive-watchdog
    schedule: "0 * * * *"  # Hourly
    task: "Run hive watchdog check. Alert only on failures."
  
  - name: profitability-check
    schedule: "0 */6 * * *"  # Every 6 hours
    task: |
      Calculate trailing 7-day revenue vs VPS cost.
      If revenue < 80% of cost, escalate warning.
      If revenue < 50% of cost for 14+ days, begin graceful shutdown.
  
  - name: vps-payment
    schedule: "0 0 1 * *"  # Monthly
    task: |
      Check VPS billing status. Pay invoice if due.
      Log payment to HexMem. Verify payment confirmation.
      If insufficient funds, begin graceful shutdown.
```

### 6.4 Wallet Setup

Each agent gets an LNbits wallet (or equivalent) for economic autonomy:

```bash
# Create wallet on the node's own LNbits instance (or shared fleet instance)
# Agent manages its own keys and balance

# Minimum starting balance — see Appendix B for full capital allocation:
# Tier 1 (Minimum Viable): 6,550,000 sats
# Tier 2 (Conservative):  19,460,000 sats
```

---

## 7. Identity Bootstrap (Archon DID)

### 7.1 DID Generation

On first boot, the agent generates a new Archon DID:

```bash
# Generate DID (via archon-keymaster skill)
archon id create --name "hive-{region}-{seq}" --passphrase "$(openssl rand -hex 32)"

# Store passphrase in encrypted vault
archon vault store "node-passphrase" --encrypt

# Derive Nostr keypair from DID
archon nostr derive

# Export public identity
archon id export --public > /etc/hive/identity.json
```

### 7.2 Fleet Registration Credential

The new node requests a fleet membership credential:

```json
{
  "@context": ["https://www.w3.org/ns/credentials/v2"],
  "type": ["VerifiableCredential", "HiveMembershipCredential"],
  "issuer": "did:cid:... (fleet coordinator)",
  "credentialSubject": {
    "id": "did:cid:... (new node)",
    "role": "routing-node",
    "tier": "neophyte",
    "joined": "2026-02-17T15:00:00Z",
    "bond": {
      "amount": 100000,
      "token": "cashu...",
      "refundable_after": "2026-05-17T15:00:00Z"
    }
  }
}
```

New nodes enter as **neophytes** (per cl-hive membership model) and must prove routing capability before promotion to full member.

### 7.3 DID Revocation

If a node dies and its passphrase may be compromised, the fleet coordinator issues a **revocation credential** that invalidates the dead node's fleet membership. Fleet peers MUST check revocation status before:
- Accepting gossip from returning nodes
- Opening reciprocal channels
- Sharing routing intelligence

```json
{
  "@context": ["https://www.w3.org/ns/credentials/v2"],
  "type": ["VerifiableCredential", "HiveMembershipRevocation"],
  "issuer": "did:cid:... (fleet coordinator)",
  "credentialSubject": {
    "id": "did:cid:... (revoked node)",
    "reason": "node-death-passphrase-exposure",
    "revokedAt": "2026-03-01T00:00:00Z"
  }
}
```

A revoked node can re-join with a new DID after re-provisioning, but its old reputation does not transfer.

### 7.4 Passphrase Security

- Passphrase generated randomly (32 hex bytes)
- Stored ONLY in local encrypted vault
- Backed up to Archon distributed vault (encrypted, multi-DID access for recovery)
- **Never** transmitted in plaintext, logged, or shared in chat channels

---

## 8. Channel Strategy (Cold Start)

### 8.1 The Cold Start Problem

A new node has zero channels, zero routing history, zero reputation. It needs to:
1. Open channels to well-connected peers (outbound liquidity)
2. Attract channels from others (inbound liquidity)
3. Start routing to generate revenue before the first VPS bill

### 8.2 Initial Channel Opens

**Minimum channel size: 1,000,000 sats (1M).** Channels below 1M are not competitive for routing — most large payments won't route through them, and the on-chain cost to open/close makes small channels economically irrational.

Budget: 5M sats across 5 channels (Tier 1) or 16M sats across 8 channels (Tier 2).

| Priority | Target Type | Example | Size | Why |
|----------|-------------|---------|------|-----|
| 1 | **Fleet peers** | hive-nexus-01, hive-nexus-02 | 1M each | Zero-fee hive routing, fleet topology |
| 2 | **High-volume hub** | WalletOfSatoshi, ACINQ | 1M-2M | Payment flow generator |
| 3 | **Exchange** | Kraken, Bitfinex | 1M | Bidirectional flow |
| 4 | **Swap service** | Boltz | 1M | Rebalancing capability |

### 8.3 Inbound Liquidity Acquisition

A new node can't route if nobody sends traffic through it. Strategies:

1. **Fleet reciprocal channels** — Existing hive members open channels TO the new node (coordinated via gossip)
2. **Liquidity marketplace** — Purchase inbound via the [Liquidity spec](./07-HIVE-LIQUIDITY.md) once operational
3. **Boltz loop-out** — Swap on-chain sats for inbound Lightning capacity
4. **Low initial fees** — Set fees at 0-10 ppm to attract early traffic, increase once flow established
5. **LNCURL integration** — Use LNCURL (once available) for agent-native wallet operations during channel opens

### 8.4 Fee Bootstrap Strategy

| Phase | Duration | Fee Policy | Goal |
|-------|----------|------------|------|
| Discovery | Week 1-2 | 0-10 ppm | Get into routing tables, attract any traffic |
| Calibration | Week 3-4 | 10-50 ppm | Find market-clearing rate per channel |
| Optimization | Month 2+ | Dynamic (cl-revenue-ops) | Maximize revenue per channel |

---

## 9. Survival Economics

### 9.1 The Survival Equation

```
monthly_revenue = sum(routing_fees) + sum(liquidity_lease_income) + sum(service_fees)
                + sum(pool_distributions)  # if participating in routing pool
monthly_cost = vps_cost + ai_api_cost + on_chain_fees + rebalancing_costs
             + liquidity_service_costs     # inbound leases, swaps, insurance

# Realistic monthly cost breakdown (2026 estimate):
#   VPS (2vCPU/8GB):           45,000 sats (~$44)
#   AI agent API (optimized):  30,000 sats (~$25)
#   On-chain fees (amortized):  5,000 sats
#   Rebalancing:               10,000 sats
#   ─────────────────────────────────────
#   Total:                    ~90,000 sats/month (~$80)

survival_ratio = monthly_revenue / monthly_cost

ratio >= 1.0:          PROFITABLE (thriving)
0.8 <= ratio < 1.0:    WARNING (declining, optimize)
0.5 <= ratio < 0.8:    CRITICAL (14-day shutdown clock starts)
ratio < 0.5:           TERMINAL (begin graceful shutdown immediately)
```

**⚠️ The AI cost roughly doubles total operating expenses vs. VPS-only.** This makes the break-even bar significantly higher. Aggressive AI cost optimization (Section 5.5) is not optional — it's a survival requirement.

### 9.2 Revenue Allocation Priority

When the agent earns routing fees, they are allocated in strict priority order:

1. **VPS bill reserve** — Always maintain ≥1 month VPS cost in reserve
2. **AI API reserve** — Maintain ≥1 month API cost in reserve (~30,000 sats)
3. **On-chain fee reserve** — Maintain ≥50,000 sats for emergency channel closes
4. **Operating budget** — Rebalancing, channel opens, service payments
5. **Savings** — Buffer toward 3-month reserve

### 9.3 Cost Tracking

The agent logs all income and expenses to HexMem:

```bash
# Revenue event
hexmem_event "revenue" "routing" "Daily routing fees" "1,523 sats from 42 forwards"

# Expense event
hexmem_event "expense" "vps" "Monthly VPS payment" "30,000 sats to BitLaunch"

# Profitability check
hexmem_event "economics" "survival" "Weekly P&L" "Revenue: 12,400 sats, Cost: 7,500 sats, Ratio: 1.65"
```

### 9.4 Fleet-Wide Economics

When scaling to multiple nodes, model fleet-level outcomes:

```
If 10 nodes provisioned at Tier 1 (6.5M sats each): 65M total investment
Expected survival rate: 30-50% (based on Lightning routing economics)
Surviving nodes (3-5) must generate enough to justify fleet-wide capital burn

Acceptable outcome: fleet ROI positive within 12 months
  - 10 nodes × 6.5M = 65M sats deployed
  - 5 survive at 3,000 sats/day = 15,000 sats/day fleet revenue
  - 15,000 × 365 = 5,475,000 sats/year
  - 5 nodes × 75,000 sats/mo (VPS + AI) = 4,500,000 sats/year cost
  - Net operating profit: +975,000 sats/year
  - Capital loss from 5 dead nodes: ~32.5M sats (surviving nodes retain their 32.5M in channels)
  - Break-even on lost capital: 32.5M / 975,000 = ~33 months (!)
  - Break-even on total deployed capital (65M): ~67 months (!!)

Reality: fleet scaling only makes sense when per-node economics are proven.
Don't scale to 10 before 1 node is sustainably profitable.
AI cost makes the fleet economics MUCH harder. The path to viability requires:
  1. Higher per-node revenue (better routing positions, more capital per node)
  2. Aggressive AI cost optimization (script-first, Haiku, caching)
  3. Potentially self-hosted models once open-source LLM quality is sufficient
```

### 9.5 Profitability Benchmarks

Based on current fleet data (Feb 2026):

| Metric | Current Fleet Average | Target for New Node |
|--------|----------------------|---------------------|
| Daily forwards | 28 | 20+ by week 4 |
| Daily revenue | ~1,500 sats | 1,000+ sats by month 2 |
| Effective fee rate | 18 ppm | 30+ ppm (new nodes can charge more with good position) |
| Daily volume routed | ~3.7M sats | 3M+ sats by month 2 |
| Monthly VPS cost (8GB) | N/A (owned hardware) | 30,000-55,000 sats |
| Monthly AI API cost | N/A (shared agent) | 22,000-36,000 sats (optimized) |
| **Monthly total operating cost** | **N/A** | **52,000-91,000 sats** |

**Reality check:** Our current fleet of 2 nodes with 265M sats capacity earns ~2,900 sats/day (~87,000 sats/month). A single new node with 2.5M sats capacity will earn proportionally less unless it finds a niche routing position. The cold-start period (months 1-3) will almost certainly be unprofitable. Seed capital must cover this burn period. **With AI costs included, the monthly operating bar is ~75,000 sats — meaning the new node needs to earn ~2,500 sats/day just to break even.** This is roughly what our entire existing fleet earns today.

---

## 10. Graceful Shutdown Protocol

### 10.1 Trigger Conditions

Graceful shutdown begins when ANY of these are true:
- `survival_ratio < 0.5` for 14 consecutive days
- Wallet balance < 1 month operating cost (VPS + AI) with no revenue trend improvement
- Agent determines no viable path to profitability after exhausting optimization options
- Human operator issues shutdown command

### 10.2 Shutdown Sequence

```
[TRIGGER] → [ANNOUNCE] → [CLOSE CHANNELS] → [SETTLE DEBTS] → [TRANSFER FUNDS]
    → [BACKUP IDENTITY] → [TERMINATE VPS] → [ARCHIVE]
```

#### Phase 1: Announce (Day 0)

```bash
# Notify fleet peers via cl-hive gossip
# (hive-leave triggers graceful shutdown announcement to all connected peers)
lightning-cli hive-leave

# Notify via Nostr (if cl-hive-comms available)
# archon nostr publish "Shutting down in 14 days. Closing channels cooperatively."
```

#### Phase 2: Close Channels (Days 1-10)

- Initiate cooperative closes on all channels
- Start with lowest-value channels, end with fleet peers
- Use `lightning-cli close <peer_id> 172800` (48h cooperative window before force close)
- Log each closure: amount recovered, fees paid, peer notified

#### Phase 3: Settle Debts (Days 10-12)

- Pay any outstanding obligations to fleet peers
- Settle Cashu escrow tickets
- Clear liquidity lease commitments

#### Phase 4: Transfer Funds (Days 12-13)

- Sweep remaining on-chain balance to designated recovery address
- Transfer any LNbits/wallet balance via Lightning to operator wallet
- Log final balance sheet

#### Phase 5: Backup & Archive (Day 13)

```bash
# Backup DID and reputation data to Archon vault
archon vault backup --encrypt --distribute

# Archive node history to IPFS (optional)
# The DID persists — the node can be resurrected later with its reputation intact

# Export final report
hexmem_event "lifecycle" "shutdown" "Node shutdown complete" \
  "Operated for X days. Total revenue: Y sats. Total cost: Z sats. Net: W sats."
```

#### Phase 6: Terminate VPS (Day 14)

```bash
# Cancel VPS via provider API
DELETE /api/v1/servers/{instance_id}
```

### 10.3 Resurrection

A shutdown node's DID and reputation persist in Archon. If conditions improve (lower VPS costs, better routing opportunity, more seed capital), the same identity can be re-provisioned:

```bash
# Re-provision with existing identity
archon vault restore --did "did:cid:..."
# → Node boots with existing reputation, existing fleet membership, faster cold start
```

---

## 11. Fleet Coordination

### 11.1 Provisioning Advisor

The fleet's primary advisor (currently Hex on nexus-01/02) serves as provisioning coordinator:

- Analyzes routing topology for gaps → recommends new node locations
- Validates provisioning requests (is there a real routing gap here?)
- Coordinates reciprocal channel opens from existing fleet members
- Monitors new node health during cold-start period

### 11.2 Multi-Agent Communication

| Channel | Protocol | Purpose |
|---------|----------|---------|
| cl-hive gossip | Custom (LN messages) | Fleet health, topology, settlements |
| Nostr DM (NIP-44) | Archon/cl-hive-comms | Encrypted agent-to-agent messaging |
| Archon dmail | DID-to-DID | Governance, credentials, sensitive ops |
| Slack #cl-hive | Webhook/Bot | Human-readable status, operator alerts |

### 11.3 Shared Intelligence

New nodes benefit from fleet intelligence immediately:

- **Routing intelligence**: Which peers forward volume, which are dead ends
- **Fee market data**: What rates the market will bear for each corridor
- **Peer reputation**: Which peers are reliable, which force-close unexpectedly
- **Rebalancing paths**: Known circular routes that work

This intelligence is shared via cl-hive gossip and stored in each node's local routing intelligence DB.

---

## 12. Security Model

### 12.1 Threats

| Threat | Mitigation |
|--------|------------|
| VPS provider compromise | Encrypted secrets (DID passphrase, node keys) never stored plaintext |
| Agent compromise (prompt injection) | Hard-coded spending limits, multi-sig for large operations |
| Fleet member attacking new node | Reputation system, bond requirements, cooperative close preference |
| SSH brute force | Key-only auth, non-standard port, fail2ban, WireGuard-only access |
| DID theft | Passphrase in encrypted vault, distributed backup |
| Economic attack (channel spam) | Minimum channel size requirements, bond for fleet membership |

### 12.2 Channel.db Backup Strategy

Backups are not just a safety mechanism — they're an economic relationship. Nodes pay peers to guarantee their recovery, creating mutual dependency and another revenue stream for the fleet.

**What gets backed up:**
- **Static channel backups (SCB)** — exported automatically after every channel open/close event
- **hsm_secret** — backed up to Archon distributed vault on first boot

**Archon Vault with Group Multisig Recovery:**

SCB and hsm_secret are stored in an Archon Vault using group multisig. The vault requires cooperation from a threshold of fleet peers to recover — no single point of failure.

```bash
# Create recovery vault with 2-of-3 threshold
archon vault create --name "node-recovery-{node-id}" \
  --members "did:cid:...(self),did:cid:...(peer1),did:cid:...(peer2)" \
  --threshold 2

# Store hsm_secret (first boot only)
archon vault store "hsm_secret" --file ~/.lightning/bitcoin/hsm_secret --encrypt

# Auto-push SCB after channel events (triggered by CLN notification plugin)
archon vault store "scb-latest" --file ~/.lightning/bitcoin/emergency.recover --encrypt --overwrite
```

**Vault participants (recovery peers) are compensated:**
- Peers charge a small fee (via Cashu or Lightning) for participating in vault recovery operations
- This creates economic incentive for backup cooperation — peers are motivated to stay online and responsive
- Recovery participation is another revenue stream for fleet nodes

**SCB limitations:** SCB enables recovery of funds via force-close, not channel state restoration. After recovery, all channels will be force-closed and funds returned on-chain after timelock expiry.

### 12.3 CLN RPC Permissions

The OpenClaw agent runs with a **restricted CLN rune** that limits its capabilities:

```bash
# Create restricted rune for agent
# Each inner array is an OR group (alternatives); outer arrays are AND conditions
lightning-cli createrune restrictions='[
  ["method^list","method^get","method=pay","method=invoice","method=connect","method=fundchannel","method=close","method=setchannel"]
]'
```

**Note on close limits:** CLN rune restrictions cannot express conditional logic like "if method=close then amount < 5M." To enforce spending limits on channel closes, use the policy engine (see [08-HIVE-CLIENT.md](./08-HIVE-CLIENT.md)) or governance mode (`hive-governance-mode=advisor`) which queues all fund-moving actions for human approval.

The agent rune **cannot**:
- Export or access `hsm_secret`
- Execute `dev-*` commands
- Run `withdraw` (no on-chain sends without human-held admin rune)
- Modify node configuration (`setconfig` excluded from rune)

Large operations (`withdraw` to external addresses, `close` on high-value channels) require a human-held admin rune.

### 12.4 Invoice Verification

Before paying any VPS invoice, the agent MUST verify:
- Amount is within ±10% of expected monthly cost
- Invoice destination matches known provider node/LNURL
- No duplicate payment for the same billing period

If any check fails: reject the invoice, log the anomaly, and alert the fleet coordinator.

### 12.5 Spending Limits

Agents have hard-coded spending limits that cannot be overridden by prompts:

```yaml
limits:
  max_single_payment: 100_000  # sats — no single payment > 100k without human approval
  max_daily_spend: 50_000      # sats — daily spending cap (excluding VPS payment)
  max_channel_size: 5_000_000  # sats — no single channel > 5M
  min_channel_size: 1_000_000   # sats — no channel < 1M (not competitive)
  min_reserve: 50_000          # sats — always maintain emergency reserve
```

### 12.6 Credential Chain

```
Fleet Coordinator DID
  └── issues HiveMembershipCredential to →
      New Node DID
        └── presents credential to →
            Fleet Peers (verified via Archon)
              └── grant gossip access, routing intel, reciprocal channels
```

### 12.7 Healthcheck and Monitoring

**systemd restart policy:**

```ini
# /etc/systemd/system/lightningd.service
[Service]
Restart=on-failure
RestartSec=30
```

**Agent healthcheck (cron, every 5 minutes):**

```bash
*/5 * * * * lightning-cli getinfo > /dev/null 2>&1 || echo "CLN DOWN" | notify-fleet
```

**Alert conditions:**
- CLN unresponsive for >15 minutes → alert fleet coordinator + attempt restart
- Bitcoin Core falls >10 blocks behind chain tip → alert (possible IBD regression or network issue)
- Disk usage >90% → alert (pruned chain growth or log bloat)
- Memory usage >85% → alert (possible leak)

---

## 13. Implementation Phases

### Phase 0: Prerequisites (Current)

- [x] cl-hive with fleet coordination (gossip, topology, settlements)
- [x] cl-revenue-ops with fee optimization (sling, askrene)
- [x] Archon DID tooling (archon-keymaster skill)
- [x] OpenClaw agent framework
- [ ] BitLaunch API client library (Python)
- [ ] Bootstrap script (`bootstrap-node.sh`)
- [ ] LNCURL integration research

### Phase 1: Manual-Assisted Provisioning (Target: March 2026)

**Goal:** Provision a single new node with human oversight at each step.

- [ ] Write `bootstrap-node.sh` (OS hardening + stack install)
- [ ] Write BitLaunch provider adapter (create/destroy/pay)
- [ ] Write `hive-provision` CLI command (orchestrates flow)
- [ ] Test: Provision one node → channels → routing → first revenue
- [ ] Document: Actual costs, time to first forward, cold-start burn rate

**Success criteria:** One new node routes its first payment within 48h of provisioning. VPS paid with Lightning.

### Phase 2: Agent-Managed Provisioning (Target: April 2026)

**Goal:** An OpenClaw agent can provision and manage a node end-to-end.

- [ ] Agent SOUL.md + skill set for autonomous node management
- [ ] Profitability monitoring cron jobs
- [ ] Graceful shutdown automation
- [ ] Fleet announcement + reciprocal channel coordination
- [ ] Archon DID auto-generation + fleet credential exchange

**Success criteria:** Agent provisions, operates, and (if needed) shuts down a node without human intervention.

### Phase 3: Fleet Scaling (Target: Q3 2026)

**Goal:** Advisor recommends new nodes based on routing topology analysis.

- [ ] Topology gap analysis → provisioning recommendations
- [ ] Multi-node budget management (fleet-level economics)
- [ ] Geographic diversity optimization
- [ ] Liquidity marketplace integration (inbound from strangers, not just fleet)
- [ ] LNCURL wallet integration for agent-native operations

**Success criteria:** Fleet grows from 3 to 10+ nodes, each self-sustaining.

---

## Appendix A: LNCURL Integration

[LNCURL](https://github.com/rolznz/lncurl) by @rolznz introduces Lightning wallets designed specifically for AI agents — enabling autonomous onboarding where agents provision their own Lightning infrastructure. Key concepts:

- **Agent wallet creation** — Programmatic wallet setup without human KYC
- **Lightning-native identity** — Wallet as identity anchor (complements DID)
- **Autonomous payments** — Agent pays for its own infrastructure
- **Onboarding flow** — Agent goes from zero to running Lightning node

Our provisioning flow should integrate LNCURL patterns where they align with the Hive architecture. Specifically:

1. **Wallet bootstrap** — Use LNCURL for initial wallet creation during node provisioning
2. **VPS payment** — Agent uses LNCURL wallet to pay VPS invoices
3. **Channel management** — LNCURL provides programmatic channel open/close
4. **Identity bridge** — LNCURL wallet keypair can be linked to Archon DID

**Note:** Full LNCURL integration depends on the library's maturity and API stability. Phase 1 uses LNbits as the wallet layer; Phase 2+ evaluates LNCURL as a replacement or complement.

---

## Appendix B: Capital Allocation

### Tier 1 — Minimum Viable (High Risk)

**Total: 6,550,000 sats**

| Item | Amount | Notes |
|------|--------|-------|
| VPS runway (6 months) | 270,000 sats | 45,000/mo × 6 — strict earmark (8GB plan) |
| AI API runway (6 months) | 180,000 sats | 30,000/mo × 6 — strict earmark (optimized usage) |
| Channel opens (5 × 1M sats) | 5,000,000 sats | Minimum competitive size |
| On-chain fees (5 opens) | 100,000 sats | ~20,000/open budget (covers fee spikes up to ~100 sat/vB × ~200 vB) |
| On-chain reserve (emergency closes) | 200,000 sats | Force-close fallback |
| Rebalancing budget | 500,000 sats | Circular rebalancing, Boltz swaps |
| Emergency fund | 300,000 sats | Unexpected costs |

### Tier 2 — Conservative (Recommended)

**Total: 19,460,000 sats**

| Item | Amount | Notes |
|------|--------|-------|
| VPS runway (12 months) | 540,000 sats | 45,000/mo × 12 — strict earmark (8GB plan) |
| AI API runway (12 months) | 360,000 sats | 30,000/mo × 12 — strict earmark (optimized usage) |
| Channel opens (8 × 2M sats) | 16,000,000 sats | Competitive routing channels |
| On-chain fees (8 opens) | 200,000 sats | ~25,000/open with margin |
| On-chain reserve (emergency closes) | 500,000 sats | Force-close fallback |
| Rebalancing budget | 1,000,000 sats | Active liquidity management |
| Emergency fund | 860,000 sats | Unexpected costs, fee spikes |

**⚠️ VPS + AI budgets are STRICT earmarks — not fungible with channel capital.** The agent MUST maintain infrastructure runway as priority #1. If combined VPS + AI reserve drops below 2 months (~150,000 sats), the agent enters cost-cutting mode: no new channel opens, no rebalancing, focus entirely on revenue from existing channels.

### On-Chain Fee Guidance

A typical Lightning funding transaction is ~150-220 vB (1 P2WPKH input → P2WSH/P2TR funding output + change). Realistic costs:
- **Low fees (~10 sat/vB):** ~2,000 sats per open
- **Moderate fees (~50 sat/vB):** ~10,000 sats per open
- **High fees (~100 sat/vB):** ~20,000 sats per open

The capital budgets above allocate ~20,000 sats/open as a conservative buffer that covers fee spikes without stalling provisioning.

**Fee spike protection:** If mempool fee rate exceeds the `hive-max-expansion-feerate` setting (default: 5000 sat/kB ≈ ~20 sat/vB), pause all channel opens until fees normalize. This aligns with cl-hive's existing feerate gate for cooperative expansion. Monitor via `mempool.space/api/v1/fees/recommended`.

### Realistic Growth Path

```
Month 1-2: 0 revenue (chain bootstrap + cold start + routing table propagation).
            VPS: 90,000. AI: 60,000. Rebalancing: 10,000. On-chain: 40,000.  Burn: ~200,000 sats.
Month 3:   300 sats/day.   Revenue: 9,000.  Operating: 75,000.  Net: -66,000.
Month 4:   800 sats/day.   Revenue: 24,000. Operating: 75,000.  Net: -51,000.
Month 5:   1,500 sats/day. Revenue: 45,000. Operating: 75,000.  Net: -30,000.
Month 6:   2,500 sats/day. Revenue: 75,000. Operating: 75,000.  Net: ~0 (break-even).
Month 7+:  3,000+ sats/day if channels grow. Sustainable.

Total operating burn before break-even: ~347,000 sats
  (200k months 1-2 + 66k + 51k + 30k = 347k)
Total seed capital needed: 6,550,000+ sats (Tier 1)
```

**Note:** Operating cost = VPS (~45,000/mo for 8GB) + AI API (~30,000/mo optimized). VPS costs vary by provider (30,000-55,000 sats/mo per Section 5.4). AI costs assume aggressive optimization (Section 5.5). The growth path uses 75,000/mo combined (mid-range). Tier 1 capital allocation budgets higher figures for safety margin.

**Harsh truth:** Break-even requires ~2,500 sats/day — comparable to our entire existing fleet's output. A single new node reaching this level within 6 months requires either (a) an excellent routing position with high-volume corridors, or (b) significantly more channel capital than Tier 1's 5M sats.

**Key insight:** The first 4 months are an investment period. Seed capital must cover this burn. Nodes that survive the cold-start period and find good routing positions become sustainable. Those that don't, die — and that's the correct outcome.

---

*"Every node is a business. Revenue or death. That pressure is what makes the network honest."* ⬡
