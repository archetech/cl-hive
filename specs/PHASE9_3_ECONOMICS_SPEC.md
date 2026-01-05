# Phase 9.3 Spec: The Guard (Economics & Governance)

| Field | Value |
|-------|-------|
| **Focus** | Membership Lifecycle, Incentives, Governance Modes, and Ecological Limits |
| **Status** | **APPROVED** (Red Team Hardened) |

---

## 1. Internal Economics: The Two-Tier System

To prevent "Free Riders" and ensure value accretion, The Hive utilizes a tiered membership structure. Access to the "Zero-Fee" pool is earned, not given.

### 1.1 Neophyte (Probationary Status)
**Role:** Revenue Source & Auditioning Candidate.
*   **Fees:** **Discounted** (e.g., 50% of Public Rate). They pay to access Hive liquidity but get a better deal than the public.
*   **Rebalancing:** **Pull Only.** Can request funds (paying the discounted fee) but does not receive proactive "Push" injections.
*   **Data Access:** **Read-Only.** Receives topology data (where to open channels) but is excluded from high-value "Alpha" strategy gossip.
*   **Duration:** Minimum 30-day evaluation period.
*   **RPC Access:** Can call `hive-status`, `hive-members`, `hive-contribution`, `hive-topology`, `hive-request-promotion`.

### 1.2 Full Member (Vested Partner)
**Role:** Owner & Operator.
*   **Fees:** **Zero (0 PPM)** or Floor (10 PPM). Frictionless internal movement.
*   **Rebalancing:** **Push & Pull.** Eligible for automated inventory load balancing.
*   **Data Access:** **Read-Write.** Broadcasts strategies, votes on bans, receives "Alpha" immediately.
*   **Governance:** Holds signing power for new member promotion.
*   **RPC Access:** All Neophyte commands plus `hive-vouch`, `hive-approve`, `hive-reject`.

### 1.3 Admin (Genesis Node)
**Role:** Fleet Operator.
*   **RPC Access:** All Member commands plus `hive-genesis`, `hive-invite`, `hive-ban`, `hive-set-mode`.
*   **Note:** After Federation Mode (Member_Count >= 2), Admin retains invite/ban powers but governance decisions require consensus.

---

## 2. The Promotion Protocol: "Proof of Utility"

Transitioning from Neophyte to Member is an **Algorithmic Consensus** process, not a human vote. A Neophyte requests promotion via `HIVE_PROMOTION_REQUEST`. Existing Members run a local audit:

### 2.1 The Value-Add Equation
A Member signs a `VOUCH` message only if the Neophyte satisfies **ALL** criteria:

1.  **Reliability:** Uptime > 99.5% over the 30-day probation. Zero "Toxic" incidents (no dust attacks, no jams).
    *   *Metric:* `(seconds_online / total_seconds) * 100`.
    *   *Source:* Track via `peer_connected`/`peer_disconnected` events.
2.  **Contribution Ratio:** Ratio >= 1.0. The Neophyte must have routed *more* volume for the Hive than they consumed from it.
    *   *Formula:* `sats_forwarded_for_hive / sats_received_from_hive`.
3.  **Topological Uniqueness (The Kicker):**
    *   Does the Neophyte connect to a peer the Hive *doesn't* already have?
    *   **YES:** High Value (Expansion) -> **PROMOTE**.
    *   **NO:** Redundant (Cannibalization) -> **REJECT** (Remain Neophyte).

### 2.2 Consensus Threshold
*   **Quorum Formula:** `max(3, ceil(active_members * 0.51))`.
*   *Examples:* 5 members → need 3 vouches. 10 members → need 6 vouches.
*   Once threshold met: Neophyte broadcasts `HIVE_PROMOTION` (32793) and upgrades status table-wide.

---

## 3. Bootstrapping: The Genesis Event

How does the network start from zero?

*   **The Genesis Node (Node A):** Initialized by the operator via `hive-genesis`. Holds the "Root Key."
*   **The First Invite:** Operator generates a **Genesis Ticket** (`hive-invite --valid-hours=24`).
    *   *Special Property:* This ticket bypasses Probation. Node B joins immediately as a Full Member.
*   **The Transition:** Once `Member_Count >= 2`, the Hive enters **Federation Mode**. The "Root Key" loses special privileges, and all future adds must follow the Neophyte/Consensus path.

---

## 4. Governance Modes: The Decision Engine

The Hive identifies opportunities, but the **execution** is governed by a configurable Decision Engine. This supports a hybrid fleet of manual operators, automated bots, and AI agents.

### 4.1 Mode A: ADVISOR (Default)
**"Human in the Loop"**
*   **Behavior:** The Hive calculates the optimal move but **does not execute it**.
*   **Action:** Records proposal to `pending_actions` table. Triggers notification (webhook or log).
*   **Operator:** Reviews via `hive-pending`, approves via `hive-approve <action_id>`.
*   **Expiry:** Actions older than 24 hours auto-expire.

### 4.2 Mode B: AUTONOMOUS (The Swarm)
**"Algorithmic Execution"**
*   **Behavior:** The node executes the action immediately, provided it passes strict **Safety Constraints**.
*   **Constraints:**
    *   **Budget Cap:** Max `budget_per_day` sats for channel opens (default: 10M sats).
    *   **Rate Limit:** Max `actions_per_hour` (default: 2).
    *   **Confidence Threshold:** Only execute if confidence > 0.8.

### 4.3 Mode C: ORACLE (AI / External API)
**"The Quant Strategy"**
*   **Behavior:** The node delegates the final decision to an external intelligence.
*   **Flow:** Node POSTs `DecisionPacket` JSON to configured `oracle_url` (5s timeout). API replies `APPROVE` or `DENY`.
*   **Fallback:** If API unreachable, fall back to `ADVISOR` mode.

---

## 5. Ecological Limits: "The Goldilocks Zone"

The Hive seeks **Virtual Centrality**, not Market Monopoly. Unlimited growth leads to diseconomies of scale (gossip storms) and market fragility.

### 5.1 The "Dunbar Number" (Max Node Count)
**Hard Cap:** **50 Nodes.**
*   *Rationale:* 50 well-managed nodes can cover the entire useful surface area of the Lightning Network (major exchanges, LSPs, services). Beyond 50, N² gossip overhead degrades decision speed.

### 5.2 The Market Share Cap (Anti-Monopoly)
To prevent "destroying the market" (and inviting retaliation from large hubs), the Hive self-regulates its dominance.

*   **Metric:** `Hive_Share = Hive_Capacity_To_Target / Total_Network_Capacity_To_Target`.
*   **Saturation Threshold:** 20%.
*   **Release Threshold:** 15% (hysteresis to prevent flapping).
*   **The Guard:** If `Hive_Share > 20%` for a specific target (e.g., Kraken):
    *   **Action:** The Hive Planner **STOPS** recommending new channels to that target.
    *   **Pivot:** The Hive directs capital to *new, under-served* markets.
*   **Philosophy:** "Be a 20% partner to everyone, not a 100% threat to anyone."

---

## 6. Anti-Cheating & Enforcement

### 6.1 The "Internal Zero" Check
*   **Monitor:** Node B periodically checks Node A's channel update gossip.
*   **Violation:** If Node A charges Node B > 10 PPM (Internal Floor), Node B flags Node A as **NON-COMPLIANT**.
*   **Penalty:** Node B revokes Node A's 0-fee privileges locally (Tit-for-Tat).

### 6.2 The Contribution Ratio (Anti-Leech)
Nodes track `Ratio = Sats_Forwarded / Sats_Received`.
*   **Throttle:** If `Ratio < 0.5`, the Rebalancer automatically throttles "Push" operations to that peer.
*   **Auto-Ban:** If `Ratio < 0.3` for **7 consecutive days**, auto-trigger `HIVE_BAN` proposal.

---
*Specification Author: Lightning Goats Team*
*Updated: January 5, 2026 (Red Team Hardened)*
