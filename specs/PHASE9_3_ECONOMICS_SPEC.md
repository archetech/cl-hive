# Phase 9.3 Spec: The Guard (Economics & Governance)

| Field | Value |
|-------|-------|
| **Focus** | Membership Lifecycle, Incentives, and Ecological Limits |
| **Status** | **APPROVED** |

---

## 1. Internal Economics: The Two-Tier System

To prevent "Free Riders" and ensure value accretion, The Hive utilizes a tiered membership structure. Access to the "Zero-Fee" pool is earned, not given.

### 1.1 Neophyte (Probationary Status)
**Role:** Revenue Source & Auditioning Candidate.
*   **Fees:** **Discounted** (e.g., 50% of Public Rate). They pay to access Hive liquidity but get a better deal than the public.
*   **Rebalancing:** **Pull Only.** Can request funds (paying the discounted fee) but does not receive proactive "Push" injections.
*   **Data Access:** **Read-Only.** Receives topology data (where to open channels) but is excluded from high-value "Alpha" strategy gossip.
*   **Duration:** Minimum 30-day evaluation period.

### 1.2 Full Member (Vested Partner)
**Role:** Owner & Operator.
*   **Fees:** **Zero (0 PPM)** or Floor (10 PPM). Frictionless internal movement.
*   **Rebalancing:** **Push & Pull.** Eligible for automated inventory load balancing.
*   **Data Access:** **Read-Write.** Broadcasts strategies, votes on bans, receives "Alpha" immediately.
*   **Governance:** Holds signing power for new member promotion.

---

## 2. The Promotion Protocol: "Proof of Utility"

Transitioning from Neophyte to Member is an **Algorithmic Consensus** process, not a human vote. A Neophyte requests promotion via `HIVE_PROMOTION_REQUEST`. Existing Members run a local audit:

### 2.1 The Value-Add Equation
A Member signs a `VOUCH` message only if the Neophyte satisfies **ALL** criteria:

1.  **Reliability:** Uptime > 99.5% over the 30-day probation. Zero "Toxic" incidents (no dust attacks, no jams).
2.  **Contribution Ratio:** Ratio > 1.0. The Neophyte must have routed *more* volume for the Hive than they consumed from it.
3.  **Topological Uniqueness (The Kicker):**
    *   Does the Neophyte connect to a peer the Hive *doesn't* already have?
    *   **YES:** High Value (Expansion) -> **PROMOTE**.
    *   **NO:** Redundant (Cannibalization) -> **REJECT** (Remain Neophyte).

### 2.2 Consensus Threshold
Once a Neophyte collects `VOUCH` signatures from **51%** of the active fleet (or a fixed quorum, e.g., 3 nodes for small fleets), they broadcast `HIVE_PROMOTION_PROOF` and upgrade their status table-wide.

---

## 3. Bootstrapping: The Genesis Event

How does the network start from zero?

*   **The Genesis Node (Node A):** Initialized by the operator. Holds the "Root Key."
*   **The First Invite:** Operator generates a **Genesis Ticket** (`revenue-hive-genesis-invite`).
    *   *Special Property:* This ticket bypasses Probation. Node B joins immediately as a Full Member.
*   **The Transition:** Once `Member_Count >= 2`, the Hive enters **Federation Mode**. The "Root Key" loses special privileges, and all future adds must follow the Neophyte/Consensus path.

---

## 4. Ecological Limits: "The Goldilocks Zone"

The Hive seeks **Virtual Centrality**, not Market Monopoly. Unlimited growth leads to diseconomies of scale (gossip storms) and market fragility.

### 4.1 The "Dunbar Number" (Max Node Count)
**Hard Cap:** **50 Nodes.**
*   *Rationale:* 50 well-managed nodes can cover the entire useful surface area of the Lightning Network (major exchanges, LSPs, services). Beyond 50, $N^2$ gossip overhead degrades decision speed.

### 4.2 The Market Share Cap (Anti-Monopoly)
To prevent "destroying the market" (and inviting retaliation from large hubs), the Hive self-regulates its dominance.

*   **Metric:** `Hive_Share = Hive_Capacity_To_Target / Total_Network_Capacity_To_Target`.
*   **The Guard:** If `Hive_Share > 20%` for a specific target (e.g., Kraken):
    *   **Action:** The Hive Planner **STOPS** recommending new channels to that target.
    *   **Pivot:** The Hive directs capital to *new, under-served* markets.
*   **Philosophy:** "Be a 20% partner to everyone, not a 100% threat to anyone."

---

## 5. Anti-Cheating & Enforcement

### 5.1 The "Internal Zero" Check
*   **Monitor:** Node B periodically checks Node A's channel update gossip.
*   **Violation:** If Node A charges Node B > 10 PPM (Internal Floor), Node B flags Node A as **NON-COMPLIANT**.
*   **Penalty:** Node B revokes Node A's 0-fee privileges locally (Tit-for-Tat).

### 5.2 The Contribution Ratio (Anti-Leech)
Nodes track `Ratio = Sats_Forwarded / Sats_Received`.
*   **Action:** If `Ratio < 0.5` (Peer takes 2x what they give), the Rebalancer automatically throttles "Push" operations to that peer.

---
*Specification Author: Lightning Goats Team*
