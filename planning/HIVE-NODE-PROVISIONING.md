# Hive Node Provisioning: Autonomous VPS Lifecycle

**Status:** Proposal / Design Draft  
**Version:** 0.1.0  
**Author:** Hex (`did:cid:bagaaierajrr7k6izcrdfwqxpgtrobflsv5oibymfnthjazkkokaugszyh4ka`)  
**Date:** 2026-02-17  
**Feedback:** Open — file issues or comment in #cl-hive  
**Related:** [DID Hive Client](./DID-HIVE-CLIENT.md), [Fleet Management](./DID-L402-FLEET-MANAGEMENT.md), [LNCURL](https://github.com/niclas9/lncurl) (rolznz)

---

## Abstract

This document specifies a workflow for provisioning, operating, and decommissioning Lightning Hive nodes on VPS infrastructure — paid entirely with Bitcoin over Lightning. Each provisioned node runs an OpenClaw agent ("multi") with the full Hive skill set, an Archon DID identity, and cl-hive/cl-revenue-ops plugins. The node is economically sovereign: it must earn enough routing fees to cover its own VPS costs, or it dies.

The system draws inspiration from [LNCURL](https://x.com/rolznz/status/2023428008602980548) — Lightning wallets for agents — which demonstrates autonomous agent onboarding where agents provision their own Lightning infrastructure. This spec extends that vision to full node lifecycle management within a cooperative fleet.

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

Every node is a business entity. It has income (routing fees, liquidity lease fees, service fees) and expenses (VPS cost, on-chain fees, channel opening costs). The agent managing the node is responsible for maintaining profitability. There is no fleet treasury, no bailouts, no shared revenue pool.

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
| **Linux (Ubuntu 22.04+)** | CLN + Bitcoin Core compatibility |
| **≥2 vCPU, 4GB RAM, 80GB SSD** | Minimum for pruned Bitcoin Core + CLN |
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
- Compare against monthly VPS cost (25,000-30,000 sats)

### 3.4 Go/No-Go Decision

**Only provision if projected revenue > 1.5× monthly VPS cost within 6 months.** If the model can't show a credible path to that target, don't provision. Capital is better deployed as larger channels on existing nodes.

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
  - Tier 1 (Minimum Viable): 6,180,000 sats
  - Tier 2 (Conservative/Recommended): 18,560,000 sats
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
  "image": "ubuntu-22.04",
  "size": "s-2vcpu-4gb",
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
3. Installs Bitcoin Core (pruned, `prune=50000`)
4. Installs CLN from official release
5. Installs Python 3.11+, cl-hive, cl-revenue-ops, cl-hive-comms
6. Configures UFW firewall (LN port + WireGuard + SSH only)
7. Sets up systemd services for bitcoind + lightningd
8. Waits for Bitcoin IBD to complete (pruned: ~4-8 hours on good hardware)

**IBD Optimization:**
- Bitcoin Core uses `-assumevalid` by default (recent versions) — no need to set manually
- Add `addnode=<fast-peer-ip>` for known fast peers in the fleet to speed sync
- Consider pre-synced pruned snapshots (with hash verification via `sha256sum`) to reduce IBD from 4-8h to <1h
- **Node is NOT operational until IBD completes.** Do not open channels or announce to fleet until fully synced

#### Step 3: Install Agent (OpenClaw Multi)

See [Section 6](#6-agent-bootstrap-openclaw-multi).

#### Step 4: Generate Identity

See [Section 7](#7-identity-bootstrap-archon-did).

#### Step 5: Open Initial Channels

See [Section 8](#8-channel-strategy-cold-start).

#### Step 6: Register with Fleet

```bash
# Agent announces itself to the fleet via cl-hive gossip
lightning-cli hive-announce \
  --did "did:cid:..." \
  --address "{ipv4}:9735" \
  --capacity "{initial_capacity}" \
  --region "{datacenter_region}"
```

Fleet peers validate the announcement, optionally open reciprocal channels.

---

## 5. Node Bootstrap Stack

### 5.1 Software Stack

| Layer | Component | Version | Purpose |
|-------|-----------|---------|---------|
| OS | Ubuntu 22.04 LTS | Latest | Stable base |
| Bitcoin | Bitcoin Core | 27.x+ | Pruned blockchain (50GB) |
| Lightning | CLN | 24.x+ | Lightning node daemon |
| Fleet | cl-hive | 2.7.0+ | Hive coordination + gossip |
| Revenue | cl-revenue-ops | 2.7.0+ | Fee optimization + rebalancing |
| Comms | cl-hive-comms | 0.1.0+ | Nostr DM + REST transport |
| Identity | cl-hive-archon | 0.1.0+ | DID + VC + dmail (optional) |
| Agent | OpenClaw | Latest | Autonomous management |
| VPN | WireGuard | Latest | Fleet private network |

### 5.2 Minimum Hardware

| Resource | Minimum | Recommended | Notes |
|----------|---------|-------------|-------|
| vCPU | 2 | 4 | CLN + Bitcoin Core + agent |
| RAM | 4 GB | 8 GB | Bitcoin Core mempool + CLN |
| Storage | 80 GB SSD | 120 GB SSD | Pruned chain (~50GB) + logs |
| Bandwidth | 2 TB/mo | Unmetered | Routing traffic |
| IPv4 | 1 static | 1 static | Peer connections |

### 5.3 Estimated Monthly Cost

| Provider | Spec | Lightning Cost | USD Equivalent |
|----------|------|---------------|----------------|
| BitLaunch (DO) | 2vCPU/4GB | ~30,000 sats | ~$29 |
| BitLaunch (Vultr) | 2vCPU/4GB | ~25,000 sats | ~$24 |
| LunaNode | 2vCPU/4GB | ~15,000 sats | ~$15 |

**Break-even target:** A node must route enough to earn ≥ its monthly VPS cost in fees. At 50 ppm average and 30,000 sats/mo cost, that requires routing ~600M sats/month (~20M sats/day). Achievable for a well-positioned node with 5+ balanced channels of ≥1M sats each.

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
# Tier 1 (Minimum Viable): 6,180,000 sats
# Tier 2 (Conservative):  18,560,000 sats
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
  "@context": ["https://www.w3.org/2018/credentials/v1"],
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
  "@context": ["https://www.w3.org/2018/credentials/v1"],
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
2. **Liquidity marketplace** — Purchase inbound via the [Liquidity spec](./DID-HIVE-LIQUIDITY.md) once operational
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
monthly_cost = vps_cost + on_chain_fees + rebalancing_costs

survival_ratio = monthly_revenue / monthly_cost

if survival_ratio >= 1.0: PROFITABLE (thriving)
if survival_ratio >= 0.8: WARNING (declining, optimize)
if survival_ratio >= 0.5: CRITICAL (14-day shutdown clock starts)
if survival_ratio < 0.5:  TERMINAL (begin graceful shutdown immediately)
```

### 9.2 Revenue Allocation Priority

When the agent earns routing fees, they are allocated in strict priority order:

1. **VPS bill reserve** — Always maintain ≥1 month VPS cost in reserve
2. **On-chain fee reserve** — Maintain ≥50,000 sats for emergency channel closes
3. **Operating budget** — Rebalancing, channel opens, service payments
4. **Savings** — Buffer toward 3-month reserve

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
If 10 nodes provisioned at Tier 1 (6M sats each): 60M total investment
Expected survival rate: 30-50% (based on Lightning routing economics)
Surviving nodes (3-5) must generate enough to justify fleet-wide capital burn

Acceptable outcome: fleet ROI positive within 12 months
  - 10 nodes × 6M = 60M sats deployed
  - 5 survive at 2,500 sats/day = 12,500 sats/day fleet revenue
  - 12,500 × 365 = 4,562,500 sats/year
  - 5 nodes × 30,000 sats/mo VPS = 1,800,000 sats/year cost
  - Net: +2,762,500 sats/year (but 30M sats lost to failed nodes)
  - Break-even on total investment: ~22 months

Reality: fleet scaling only makes sense when per-node economics are proven.
Don't scale to 10 before 1 node is sustainably profitable.
```

### 9.5 Profitability Benchmarks

Based on current fleet data (Feb 2026):

| Metric | Current Fleet Average | Target for New Node |
|--------|----------------------|---------------------|
| Daily forwards | 28 | 20+ by week 4 |
| Daily revenue | ~1,500 sats | 1,000+ sats by month 2 |
| Effective fee rate | 18 ppm | 30+ ppm (new nodes can charge more with good position) |
| Daily volume routed | ~3.7M sats | 3M+ sats by month 2 |
| Monthly VPS cost | N/A (owned hardware) | 15,000-30,000 sats |

**Reality check:** Our current fleet of 2 nodes with 265M sats capacity earns ~2,900 sats/day. A single new node with 2.5M sats capacity will earn proportionally less unless it finds a niche routing position. The cold-start period (months 1-3) will almost certainly be unprofitable. Seed capital must cover this burn period.

---

## 10. Graceful Shutdown Protocol

### 10.1 Trigger Conditions

Graceful shutdown begins when ANY of these are true:
- `survival_ratio < 0.5` for 14 consecutive days
- Wallet balance < 1 month VPS cost with no revenue trend improvement
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
lightning-cli hive-announce --type "shutdown" --reason "economic" --timeline "14d"

# Notify via Nostr
archon nostr publish "Shutting down in 14 days. Closing channels cooperatively."
```

#### Phase 2: Close Channels (Days 1-10)

- Initiate cooperative closes on all channels
- Start with lowest-value channels, end with fleet peers
- Use `close --unilateraltimeout 172800` (48h cooperative window before force close)
- Log each closure: amount recovered, fees paid, peer notified

#### Phase 3: Settle Debts (Days 10-12)

- Pay any outstanding obligations to fleet peers
- Settle Cashu escrow tickets
- Clear liquidity lease commitments

#### Phase 4: Transfer Funds (Days 12-13)

- Sweep remaining on-chain balance to designated recovery address
- Transfer any LNbits/wallet balance via Lightning to fleet treasury or operator wallet
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
lightning-cli createrune restrictions='[
  ["method^list|method^get|method=pay|method=invoice|method=connect|method=fundchannel|method=close"],
  ["method/close&pnameamountsat<5000000"]
]'
```

The agent rune **cannot**:
- Export or access `hsm_secret`
- Execute `dev-*` commands
- Close channels above the spending limit without human approval
- Modify node configuration

Large operations (channel closes > 5M sats, `withdraw` to external addresses) require a human-held admin rune.

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

- [x] cl-hive v2.7.0 with fleet coordination
- [x] cl-revenue-ops v2.7.0 with fee optimization
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

[LNCURL](https://x.com/rolznz/status/2023428008602980548) by @rolznz introduces Lightning wallets designed specifically for AI agents — enabling autonomous onboarding where agents provision their own Lightning infrastructure. Key concepts:

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

**Total: 6,180,000 sats**

| Item | Amount | Notes |
|------|--------|-------|
| VPS runway (6 months) | 180,000 sats | 30,000/mo × 6 — strict earmark |
| Channel opens (5 × 1M sats) | 5,000,000 sats | Minimum competitive size |
| On-chain fees (5 opens) | 100,000 sats | ~20,000/open at moderate fees (~10 sat/vB, ~200 vB) |
| On-chain reserve (emergency closes) | 200,000 sats | Force-close fallback |
| Rebalancing budget | 500,000 sats | Circular rebalancing, Boltz swaps |
| Emergency fund | 200,000 sats | Unexpected costs |

### Tier 2 — Conservative (Recommended)

**Total: 18,560,000 sats**

| Item | Amount | Notes |
|------|--------|-------|
| VPS runway (12 months) | 360,000 sats | 30,000/mo × 12 — strict earmark |
| Channel opens (8 × 2M sats) | 16,000,000 sats | Competitive routing channels |
| On-chain fees (8 opens) | 200,000 sats | ~25,000/open with margin |
| On-chain reserve (emergency closes) | 500,000 sats | Force-close fallback |
| Rebalancing budget | 1,000,000 sats | Active liquidity management |
| Emergency fund | 500,000 sats | Unexpected costs, fee spikes |

**⚠️ VPS budget is a STRICT earmark — not fungible with channel capital.** The agent MUST maintain VPS runway as priority #1. If VPS reserve drops below 2 months (60,000 sats), the agent enters cost-cutting mode: no new channel opens, no rebalancing, focus entirely on revenue from existing channels.

### On-Chain Fee Guidance

Realistic channel open cost: **~20,000 sats** at moderate fees (~10 sat/vB, ~200 vB per funding transaction). The old estimate of ~5,000 sats per open was unrealistically low.

**Fee spike protection:** If mempool fee rate exceeds 50 sat/vB, pause all channel opens until fees normalize. Monitor via `mempool.space/api/v1/fees/recommended`.

### Realistic Growth Path

```
Month 1-2: 0 revenue (IBD + cold start + routing table propagation). Burn: 50,000 sats.
Month 3:   300 sats/day.   Revenue: 9,000.  VPS: 25,000.  Net: -16,000.
Month 4:   800 sats/day.   Revenue: 24,000. VPS: 25,000.  Net: -1,000.
Month 5:   1,500 sats/day. Revenue: 45,000. VPS: 25,000.  Net: +20,000.
Month 6+:  2,500+ sats/day if channels grow. Sustainable.

Total burn before break-even: ~120,000 sats
Total seed capital needed: 6,180,000+ sats (Tier 1)
```

**Key insight:** The first 4 months are an investment period. Seed capital must cover this burn. Nodes that survive the cold-start period and find good routing positions become sustainable. Those that don't, die — and that's the correct outcome.

---

*"Every node is a business. Revenue or death. That pressure is what makes the network honest."* ⬡
