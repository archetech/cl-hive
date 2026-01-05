# Phase 9.2 Spec: The Brain (Logic & State)

| Field | Value |
|-------|-------|
| **Focus** | State Synchronization, Conflict Resolution, Anti-Entropy |
| **Status** | **APPROVED** (Red Team Hardened) |

---

## 1. Shared State Management
Nodes maintain a local `HiveMap` representing the fleet.

### 1.1 State Hash Algorithm
To ensure deterministic comparison across nodes, the State Hash is calculated as:

```
SHA256( JSON.stringify( sort_by_peer_id( [ {peer_id, version, timestamp}, ... ] ) ) )
```

**Rules:**
*   Only essential metadata is hashed (not full state) to detect drift.
*   Array MUST be sorted lexicographically by `peer_id` before serialization.
*   JSON serialization MUST use consistent key ordering (sorted keys).
*   Used for Anti-Entropy checks on `peer_connected` events.

### 1.2 Threshold Gossiping
To prevent bandwidth exhaustion, nodes do NOT broadcast every satoshi change.
*   **Trigger:** Broadcast `HIVE_GOSSIP` only if:
    *   Available Capacity changes by > **10%**.
    *   Fee Policy changes.
    *   Peer Status changes (Ban/Unban).
    *   **Heartbeat:** Force broadcast every **300 seconds** if no other updates.

### 1.3 Anti-Entropy Protocol
On `peer_connected` event:
1.  Send `HIVE_STATE_HASH` with local fleet hash.
2.  Compare received hash from peer.
3.  If mismatch → Request `HIVE_FULL_SYNC`.
4.  Merge received state (version-based conflict resolution).

## 2. The "Intent Lock" Protocol (Deterministic Tie-Breaking)
**Problem:** Node A and Node B both decide to open a channel to "Kraken" at the same time.
**Solution:** The Announce-Wait-Commit pattern.

### 2.1 Supported Intent Types
| Type | Description | Conflict Scope |
| :--- | :--- | :--- |
| `channel_open` | Opening a channel to an external peer | Same target pubkey |
| `rebalance` | Large circular rebalance affecting fleet liquidity | Overlapping channel set |
| `ban_peer` | Proposing a ban (requires consensus) | Same target pubkey |

### 2.2 The Flow
1.  **Decision:** Node A decides to open to Target X.
2.  **Announce:** Node A broadcasts `HIVE_INTENT { type: "channel_open", target: X, initiator: A, timestamp: T }`.
3.  **Hold Period:** Node A waits **60 seconds**. It listens for conflicting intents.
4.  **Resolution:**
    *   **Scenario 1 (Silence):** No conflicting messages received. **Action:** Commit (Open Channel).
    *   **Scenario 2 (Conflict):** Node B broadcasts an Intent for Target X during the hold period.
        *   **Tie-Breaker:** Compare `Node_A_Pubkey` vs `Node_B_Pubkey` (lexicographic).
        *   **Winner:** Lowest Lexicographical Pubkey proceeds.
        *   **Loser:** Highest Pubkey broadcasts `HIVE_INTENT_ABORT` and recalculates.

### 2.3 Timer Management
*   **Monitor Loop:** Background thread runs every **5 seconds**.
*   **Commit Condition:** `now > intent.timestamp + 60s` AND `status == 'pending'`.
*   **Cleanup:** Stale intents (> 1 hour) are purged from the database.
*   **Abort Handling:** On receiving `HIVE_INTENT_ABORT`, update remote intent status in DB.

## 3. The Hive Planner (Topology Logic)
The "Gardner" algorithm runs hourly to optimize the graph.
*   **Anti-Overlap:** If `Total_Hive_Capacity(Peer_Y) > Target_Saturation`, issue `clboss-ignore Peer_Y` to all nodes *except* the ones already connected.
*   **Coverage Expansion:** Identify high-yield peers with 0 Hive connections. Assign the node with the most idle on-chain capital to initiate the `HIVE_INTENT` process.
