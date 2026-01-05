# Phase 9.2 Spec: The Brain (Logic & State)

## 1. Shared State Management
Nodes maintain a local `HiveMap` representing the fleet.

### 1.1 Threshold Gossiping
To prevent bandwidth exhaustion, nodes do NOT broadcast every satoshi change.
*   **Trigger:** Broadcast `HIVE_GOSSIP` only if:
    *   Available Capacity changes by > **10%**.
    *   Fee Policy changes.
    *   Peer Status changes (Ban/Unban).

## 2. The "Intent Lock" Protocol (Deterministic Tie-Breaking)
**Problem:** Node A and Node B both decide to open a channel to "Kraken" at the same time.
**Solution:** The Announce-Wait-Commit pattern.

### 2.1 The Flow
1.  **Decision:** Node A decides to open to Target X.
2.  **Announce:** Node A broadcasts `HIVE_INTENT { target: X, timestamp: T, node: A }`.
3.  **Hold Period:** Node A waits **60 seconds**. It listens for conflicting intents.
4.  **Resolution:**
    *   **Scenario 1 (Silence):** No conflicting messages received. **Action:** Commit (Open Channel).
    *   **Scenario 2 (Conflict):** Node B broadcasts an Intent for Target X during the hold period.
        *   **Tie-Breaker:** Compare `Node_A_Pubkey` vs `Node_B_Pubkey`.
        *   **Winner:** Lowest Lexicographical Pubkey proceeds.
        *   **Loser:** Highest Pubkey aborts and recalculates.

## 3. The Hive Planner (Topology Logic)
The "Gardner" algorithm runs hourly to optimize the graph.
*   **Anti-Overlap:** If `Total_Hive_Capacity(Peer_Y) > Target_Saturation`, issue `clboss-ignore Peer_Y` to all nodes *except* the ones already connected.
*   **Coverage Expansion:** Identify high-yield peers with 0 Hive connections. Assign the node with the most idle on-chain capital to initiate the `HIVE_INTENT` process.
