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

## 4. IMPORTANT: CLBoss Integration Limitation

**Discovery Date:** 2026-01-10

The threat model and mitigations above assume `clboss-ignore` and `clboss-unignore` commands exist. **They do not exist in CLBoss v0.15.1.**

### What CLBoss Actually Has:
- `clboss-ignore-onchain`: Ignore addresses for on-chain sweeps (different purpose)
- `clboss-unmanage`: Stop managing fees for a peer (used by cl-revenue-ops)
- `clboss-manage`: Resume managing fees for a peer

### What This Means:
- The saturation detection works correctly (hive_share calculation)
- But we **cannot** tell CLBoss to avoid opening channels to saturated targets
- CLBoss will still auto-open channels to any peer it deems profitable

### Current Solution: Intent Lock Protocol
The Hive uses the Intent Lock Protocol for channel coordination instead:
1. **ANNOUNCE**: Node broadcasts HIVE_INTENT with (type, target, initiator, timestamp)
2. **WAIT**: Hold for 60 seconds
3. **COMMIT**: If no conflicts, proceed with action
4. **TIE-BREAKER**: Lowest lexicographical pubkey wins conflicts

This prevents thundering herd (multiple hive nodes opening to same target) but does NOT prevent CLBoss from acting independently.

### Future Options:
1. **Patch CLBoss** (RECOMMENDED): Add `clboss-ignore` / `clboss-unignore` commands upstream
   - Submit PR to https://github.com/ZmnSCPxj/clboss
   - New commands would accept peer_id and prevent auto-opening to that peer
   - This is the cleanest solution

2. **Hook fundchannel**: NOT POSSIBLE
   - Core Lightning only provides `openchannel` hook for INCOMING channels
   - No hook exists for intercepting outgoing fundchannel commands
   - See: https://docs.corelightning.org/docs/hooks

3. **Accept Limitation** (CURRENT): Document that CLBoss may open channels independently
   - Intent Lock Protocol prevents intra-Hive conflicts
   - CLBoss may open channels to saturated targets independently
   - This is acceptable for Phase 6 MVP

## 5. Conclusion
The Planner is safe to deploy **ONLY IF** expansions are gated by default (`ADVISOR` mode) and gossip data is validated against public channel state.

**Note:** The `clboss-ignore` mitigation in T2.1 is not currently functional due to CLBoss limitations. The Hive relies on Intent Lock Protocol for coordination, which mitigates intra-Hive thundering herd but not independent CLBoss actions.
