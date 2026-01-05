# Phase 9 Proposal: "The Hive"
**Distributed Swarm Intelligence & Virtual Centrality**

| Field | Value |
|-------|-------|
| **Target Version** | v2.0.0 |
| **Architecture** | **Agent-Based Swarm (Distributed State)** |
| **Authentication** | Public Key Infrastructure (PKI) |
| **Objective** | Create a self-organizing "Super-Node" from a fleet of independent peers. |
| **Status** | **Tentatively Approved for development** |

---

## 1. Executive Summary

**"The Hive"** is a protocol that allows independent Lightning nodes to function as a single, distributed organism.

It pivots from the "Central Bank" model of the deprecated LDS system to a **"Meritocratic Federation"**. Instead of a central controller, The Hive utilizes **Swarm Intelligence**. Each node acts as an autonomous agent: observing the shared state of the fleet, making independent decisions to maximize the fleet's total surface area, and synchronizing actions via the **Intent Lock Protocol** to prevent resource conflicts.

The result is **Virtual Centrality**: A fleet of 5 small nodes achieves the routing efficiency, fault tolerance, and market dominance of a single massive whale node, while remaining 100% non-custodial and voluntary.

---

## 2. Strategic Pivot: Solving the LDS Pitfalls

| Issue | The LDS Failure Mode | The Hive Solution |
| :--- | :--- | :--- |
| **Custody** | **High Risk.** Operator holds keys for LPs. Regulated as Money Transmission. | **Solved.** LPs run their own nodes/keys. The Hive is just a communication protocol between them. |
| **Liability** | **High.** If the central node is hacked, all LP funds are lost. | **Solved.** Funds are distributed. A hack on one node does not compromise the others. |
| **Solvency** | **Fragile.** "Runs on the bank" could lock up the central node. | **Robust.** There is no central bank. Nodes trade liquidity bilaterally via standard Lightning channels. |
| **Regulation** | **Security.** "Investment contract" via pooled profits. | **Trade Agreement.** "Preferential Routing" between independent peers. |

---

## 3. The Core Loop: Observe, Orient, Decide, Act

The Hive operates on a continuous OODA loop running locally on every member node. There is no central server.

### 3.1 Observe (Gossip State)
Nodes broadcast compressed heartbeat messages via Custom Messages (BOLT 8 encrypted).
*   **Topology:** "I am connected to [Binance, River, ACINQ]."
*   **Liquidity:** "I have 50M sats outbound capacity available."
*   **Reputation:** "Peer X is toxic (high failure rate)."
*   **Opportunities:** "Peer Y is high-yield (hidden gem)."

### 3.2 Orient (Global Context)
Before taking action, a node contextualizes its local view against the Hive's state.
*   *Local View:* "I should open a channel to Binance."
*   *Hive View:* "Node A already has 10 BTC to Binance. The fleet is saturated."
*   *Adjustment:* "I will `clboss-ignore` Binance to prevent capital duplication."

### 3.3 Decide (Autonomous Optimization)
The node calculates the highest-value action for itself and the Fleet.
*   **Surface Area Expansion:** "The Hive has 0 connections to Kraken. I have spare capital. I will connect to Kraken."
*   **Load Balancing:** "Node A is empty. I am full. I will push liquidity to Node A."

### 3.4 Act & Share (Conflict Resolution)
The node executes the action and **immediately** broadcasts a "Lock" message.
*   **Action:** `fundchannel` to Kraken.
*   **Broadcast:** `HIVE_ACTION: OPENING [Kraken_Pubkey]`.
*   **Effect:** Other nodes see this lock and abort their own attempts to open to Kraken, preventing "Race Conditions" where two nodes waste fees opening redundant channels simultaneously.

---

## 4. Alpha Capabilities (The "Unfair Advantages")

### 4.1 Zero-Cost Capital Teleportation
**The Mechanism:** Fleet members whitelist each other for **0-Fee Routing**.
**The Result:** Capital becomes "super-fluid." Liquidity can instantly move to whichever node has the highest demand without friction cost.

### 4.2 Inventory Load Balancing ("Push" Rebalancing)
**The Mechanism:** Proactive "Push." Node A (Surplus) proactively routes funds to Node B (Deficit) *before* Node B runs dry.
**The Result:** Zero downtime for high-demand channels.

### 4.3 The "Borg" Defense (Distributed Immunity)
**The Mechanism:** Shared `ignored_peers` list. If Node A detects a "Dust Attack" or "HTLC Jamming" from Peer X, it broadcasts a **Signed Ban**. All Hive members immediately blacklist Peer X.

### 4.4 Coordinated Graph Mapping
**The Mechanism:** The Hive Planner algorithms direct nodes to unique targets, maximizing the fleet's total network surface area rather than overlapping on the same few hubs.

---

## 5. Governance Modes: The Decision Engine

The Hive identifies opportunities, but the **execution** is governed by a configurable Decision Engine. This supports a hybrid fleet of manual operators, automated bots, and AI agents.

### 5.1 Mode A: Advisor (Default)
**"Human in the Loop"**
*   **Behavior:** The Hive calculates the optimal move but **does not execute it**.
*   **Action:** Records proposal. Triggers notification (Webhook). Operator approves via RPC `revenue-hive-approve`.

### 5.2 Mode B: Autonomous (The Swarm)
**"Algorithmic Execution"**
*   **Behavior:** The node executes the action immediately, provided it passes strict **Safety Constraints** (Budget Caps, Rate Limits, Confidence Thresholds).

### 5.3 Mode C: Oracle (AI / External API)
**"The Quant Strategy"**
*   **Behavior:** The node delegates the final decision to an external intelligence.
*   **Flow:** Node sends a `Decision Packet` (JSON) to a configured API endpoint (e.g., an LLM or ML model). The API replies `APPROVE` or `DENY`.

---

## 6. Membership & Growth

The Hive is designed to grow organically but safely, utilizing a two-tier system to vet new nodes.

### 6.1 Tiers
*   **Neophyte (Probationary):** Revenue Source & Candidate. They pay discounted fees (e.g., 50% market rate) to access Hive liquidity. Read-Only access to topology data. Minimum 30-day evaluation.
*   **Full Member (Vested):** Partner. They enjoy 0-fee internal routing, "Push" rebalancing, and Full Read-Write access to strategy gossip and governance.

### 6.2 "Proof of Utility" (Promotion)
New members are not voted in by humans; they are promoted by algorithms. A Member node signs a `VOUCH` message only if the Neophyte satisfies the **Value-Add Equation**:
1.  **Reliability:** >99.5% Uptime, Zero Toxic Incidents.
2.  **Contribution:** Ratio > 1.0 (Routed more for the Hive than consumed).
3.  **Unique Topology:** Connects to a peer the Hive does *not* already have.

### 6.3 Ecological Limits
To prevent centralization risks and market retaliation:
*   **Dunbar Cap:** Max ~50 Nodes per Hive (prevents gossip storms).
*   **Market Share Cap:** Max 20% of public liquidity to any single target (e.g., Kraken). If exceeded, the Hive stops opening channels to that target.

---

## 7. Anti-Cheating: Behavioral Integrity & Verification

Since we cannot verify source code on remote nodes (Zero Trust), The Hive uses **Behavioral Verification** to enforce rules.

### 7.1 The "Gossip Truth" Check (Anti-Bait-and-Switch)
**Threat:** Node A claims 0-fees internally but broadcasts high fees publicly.
**Defense:** Honest nodes verify the public **Lightning Gossip**. If `Gossip_Fee > Agreed_Fee`, Node A is flagged Non-Compliant.

### 7.2 The Contribution Ratio (Anti-Leech)
**Threat:** Node A drains fleet liquidity but refuses to route for others.
**Defense:** **Algorithmic Tit-for-Tat.**
Nodes track `Ratio = Sats_Forwarded / Sats_Received`. Nodes with low ratios are automatically throttled by the Rebalancer.

### 7.3 Active Probing (Anti-Black-Hole)
**Threat:** Node A claims false capacity to attract traffic.
**Defense:** Nodes periodically route small self-payments through peers. Failures result in Reputation slashing.

---

## 8. Detailed Specifications

This proposal is supported by three detailed technical specifications:

| Component | Spec Document | Focus |
|-----------|---------------|-------|
| **Protocol** | [`PHASE9_1_PROTOCOL_SPEC.md`](./PHASE9_1_PROTOCOL_SPEC.md) | PKI Handshake, Message IDs, Manifests. |
| **Logic** | [`PHASE9_2_LOGIC_SPEC.md`](./PHASE9_2_LOGIC_SPEC.md) | Intent Locks, State Map, Threshold Gossip. |
| **Economics** | [`PHASE9_3_ECONOMICS_SPEC.md`](./PHASE9_3_ECONOMICS_SPEC.md) | Incentives, Lifecycle, Consensus Banning. |

---
*Specification Author: Lightning Goats Team*  
*Architecture: Distributed Agent Model*
