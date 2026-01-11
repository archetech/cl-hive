# Phase 6 Threat Model: The Thundering Gardner

**Date:** 2026-01-08
**Author:** Red Team Lead (AI)
**Status:** DRAFT

## 1. Overview
Phase 6 introduces automated capital allocation (Expansion) and inhibition (Guard/Ignore). This shifts `cl-hive` from a passive coordination layer to an active management layer. This document analyzes the security risks introduced by the **Planner** module.

## 2. Threat Analysis

### 2.1 Threat: Runaway Ignore (Denial of Service)
*   **Attack Vector:** A compromised or malicious Hive member broadcasts fake `GOSSIP` messages claiming they have huge capacity to *every* major node on the Lightning Network.
*   **Mechanism:**
    1.  Attacker sends gossip: "I have 100 BTC capacity to Node A, Node B, Node C..."
    2.  Honest nodes calculate `hive_share(Node A)`.
    3.  `hive_share` spikes > 20% (Saturation Threshold).
    4.  Honest nodes trigger `clboss-ignore Node A`.
    5.  **Result:** CLBoss stops managing channels to all top-tier nodes. The node's profitability collapses.
*   **Risk Level:** **HIGH**
*   **Mitigation:**
    *   **Capacity Verification:** Do not trust Gossip capacity blindly. Verify against public `listchannels`. If a peer claims capacity > public capacity, cap it or reject the gossip.
    *   **Ignore Cap:** Limit `enforce_saturation_limits` to ignoring max 5 new peers per cycle.
    *   **Manual Override:** Ensure `clboss-unignore` works manually even if Planner tries to re-ignore.

### 2.2 Threat: Sybil Liquidity Drain (Capital Exhaustion)
*   **Attack Vector:** Attacker creates a new node (Sybil) and manipulates metrics to look "Underserved".
*   **Mechanism:**
    1.  Attacker opens large public channels to their Sybil node (self-funded) to boost "Total Network Capacity" (denominator).
    2.  Attacker ensures 0 Hive capacity to Sybil (numerator).
    3.  `hive_share` = 0%. Target is flagged "Underserved".
    4.  Hive Planner proposes expansion.
    5.  Honest Hive node opens a 5M sat channel to Attacker.
    6.  Attacker drains funds via submarine swap or circular payment, then closes channel.
*   **Risk Level:** **MEDIUM**
*   **Mitigation:**
    *   **Min Capacity Threshold:** Only consider targets with > 1 BTC public capacity (already in plan).
    *   **Age Check:** Only consider targets that have been in the graph for > 30 days (requires historical data or heuristics).
    *   **Governance Mode:** Run in `ADVISOR` mode initially. Operator must manually approve expansions.

### 2.3 Threat: Intent Storms (Network Spam)
*   **Attack Vector:** A bug in `planner_loop` causes it to run every second instead of every hour, or the "Pending Intent" check fails.
*   **Mechanism:**
    1.  Planner sees target X is underserved.
    2.  Planner proposes Intent.
    3.  Loop repeats immediately.
    4.  Planner proposes Intent again (because previous one is not yet committed).
    5.  **Result:** Network flooded with `HIVE_INTENT` messages.
*   **Risk Level:** **MEDIUM**
*   **Mitigation:**
    *   **Hard Timer:** Use `time.sleep()` or `threading.Event.wait()` with a hardcoded minimum (e.g., `max(config_interval, 300)`).
    *   **State Check:** Explicitly check `database.get_pending_intents()` before proposing.
    *   **Rate Limit:** Enforce `MAX_INTENTS_PER_CYCLE = 1`.

## 3. Recommendations for Lead Developer

1.  **Trust but Verify:** In `_calculate_hive_share`, clamp reported peer capacity to the maximum seen in `listchannels` for that pair.
2.  **Safety Valve:** Add a config option `hive-planner-enable-expansions` (default `false`). Force users to opt-in to automated channel opening.
3.  **Circuit Breaker:** If the Planner ignores > 10 peers in a single cycle, abort the cycle and log an error "Mass Saturation Detected".

## 4. CLBoss Integration (ksedgwic/clboss fork)

**Updated:** 2026-01-10

The threat model mitigations require preventing CLBoss from opening channels to saturated targets.
This is **SOLVED** using the ksedgwic/clboss fork which provides `clboss-unmanage` with management tags.

### CLBoss Management Tags (ksedgwic/clboss)
- `open`: Channel opening - **used by cl-hive for saturation control**
- `close`: Channel closing
- `lnfee`: Fee management - **used by cl-revenue-ops**
- `balance`: Rebalancing - **used by cl-revenue-ops**

### How Saturation Control Works
1. Hive detects target saturation (hive_share > 20%)
2. Planner calls `clboss-unmanage <peer_id> open`
3. CLBoss stops auto-opening channels to that peer
4. When saturation drops below 15%, call `clboss-manage <peer_id> open`

### Coordination with cl-revenue-ops
- cl-hive manages the `open` tag (channel opening to saturated targets)
- cl-revenue-ops manages `lnfee` and `balance` tags (fees and rebalancing)
- Both plugins use the same `clboss-unmanage` / `clboss-manage` commands

### Intent Lock Protocol (Complementary)
The Intent Lock Protocol complements CLBoss control:
1. **ANNOUNCE**: Node broadcasts HIVE_INTENT with (type, target, initiator, timestamp)
2. **WAIT**: Hold for 60 seconds
3. **COMMIT**: If no conflicts, proceed with action
4. **TIE-BREAKER**: Lowest lexicographical pubkey wins conflicts

This prevents thundering herd for Hive-initiated actions. CLBoss control prevents
CLBoss-initiated actions to saturated targets.

### Required CLBoss Version
- Must use ksedgwic/clboss fork: https://github.com/ksedgwic/clboss
- The upstream ZmnSCPxj/clboss lacks peer-level unmanage with `open` tag

## 5. Conclusion
The Planner is safe to deploy **ONLY IF** expansions are gated by default (`ADVISOR` mode) and gossip data is validated against public channel state.

All threat mitigations are functional with the ksedgwic/clboss fork.
