# Settlement Reporting Audit - 2026-02-23

## Executive Summary

The distributed settlement system has five critical bugs preventing proper fee pooling and distribution among hive fleet members. This audit identifies root causes and provides fixes.

## Observed Issues

1. **nexus-01** (managed node) shows 0 fees_earned and 0 forward_count in settlement proposals, despite actively routing
2. **cyber-hornet-1** (external member) shows all zeros (no fees, no forwards, no uptime)
3. Only **nexus-02** shows any data (885 sats earned, 10 forwards)
4. **Uptime field is 0 for ALL members** (not being tracked)
5. No evidence of actual settlement payments being executed (proposals reach "ready" but expire)

---

## Bug #1: Local Node Uptime Never Tracked

### Root Cause
The local node (our_pubkey) never records its own presence data in the `peer_presence` table. Presence is only updated for REMOTE peers via:
- `on_peer_connected` hook (line 3738)
- `on_peer_disconnected` hook (line 3787)
- `handle_handshake_complete` (line 2972)

The `sync_uptime_from_presence()` function only calculates uptime for members who have entries in `peer_presence`. Since the local node has no presence entry, it gets 0% uptime.

### Impact
- Local node shows 0% uptime in all settlement calculations
- Fair share algorithm undervalues local node contribution (10% weight is uptime)

### Fix Location
`cl-hive.py` in `init()` function, after line 1838 (where startup uptime sync occurs)

### Fix Code
```python
# Initialize local node presence on startup (settlement uptime tracking)
if our_pubkey:
    database.update_presence(our_pubkey, is_online=True, now_ts=int(time.time()), window_seconds=30 * 86400)
```

---

## Bug #2: Remote Member Uptime Depends on Seeing Connections

### Root Cause
For external members like cyber-hornet-1, uptime is only tracked when they connect/disconnect TO the local node. If:
- They're already connected at startup but presence table is empty
- Connection events were missed
- The member joined recently with no presence history

...they will show 0% uptime.

### Impact
- New members or members who rarely reconnect show 0% uptime
- Settlement fair shares are incorrect

### Fix
On startup, enumerate all currently connected peers who are hive members and initialize their presence if missing.

---

## Bug #3: Local Fee Report Not Saved Below Threshold

### Root Cause
The `_update_and_broadcast_fees()` function (line 3872) only saves fee reports to the database when the broadcast threshold is met:
- `FEE_BROADCAST_MIN_SATS = 10` (minimum cumulative fee change)
- `FEE_BROADCAST_MIN_INTERVAL = 30` (minimum seconds between broadcasts)

If a node has low traffic or the accumulation hasn't crossed the threshold, `database.save_fee_report()` is never called.

### Critical Path
```
forward_event → _update_and_broadcast_fees() → (threshold check) → _broadcast_fee_report() → database.save_fee_report()
```

If thresholds aren't met, save_fee_report is skipped entirely.

### Impact
- Low-traffic nodes have no fee_reports entries
- Settlement calculations show 0 fees for active routing nodes
- nexus-01 showing 0 fees despite routing activity

### Fix
Save fee report to database on every update, independent of broadcast threshold. The broadcast threshold should only control gossip, not local persistence.

---

## Bug #4: Period String Calculation Edge Case

### Root Cause
Fee reports use `SettlementManager.get_period_string(period_start)` to determine the YYYY-WW period. If `period_start` is from the previous week (due to period initialization timing), the report is stored under the wrong period.

### Example
- Node started routing on Sunday 23:55 UTC
- period_start = Sunday timestamp
- Monday 00:01 UTC: settlement proposal created for new week
- Fee report from Sunday is stored under previous week's period
- Settlement calculation finds no fee report for current period

### Impact
- Fee reports appear missing for current settlement period
- Timing-dependent data loss

### Fix
Always use `get_period_string(time.time())` for saving local fee reports, not `get_period_string(period_start)`.

---

## Bug #5: Settlement Execution Blocked in Advisor Mode

### Root Cause
The settlement loop (line 11488) checks governance mode before executing settlements:
```python
if governance_mode != "failsafe":
    # Queue settlement execution as a pending action for approval
    database.add_pending_action(...)
```

In advisor mode (default), settlements are queued to `pending_actions` but:
1. There's no automated approval mechanism
2. MCP tools for approval exist but require manual intervention
3. Pending actions expire after a timeout
4. Settlement proposals also expire (typically 24-48 hours)

### Impact
- Settlement proposals reach "ready" status (quorum achieved)
- No payments are executed
- Proposals expire before anyone approves the pending actions
- Fleet never actually settles

### Fix Options
1. **Auto-approve settlements that reached quorum** - settlements are member-voted consensus decisions, not unilateral actions
2. **Reduce settlement action approval burden** - treat as "low danger" action
3. **Create periodic reminder for pending settlement approvals**

---

## Bug #6: Missing BOLT12 Offers Prevent Settlement

### Root Cause
`execute_our_settlement()` (line 1498) requires a BOLT12 offer for each recipient:
```python
offer = self.get_offer(to_peer)
if not offer:
    self.plugin.log(f"SETTLEMENT: Missing BOLT12 offer for receiver {to_peer[:16]}...")
    return None
```

If any receiver hasn't registered a BOLT12 offer, the entire settlement for the payer fails.

### Impact
- Members who haven't registered offers block settlements
- No partial settlement possible

### Observation
This may explain why cyber-hornet-1 shows all zeros - they may not have a BOLT12 offer registered.

---

## Summary Table

| Bug | Severity | Fix Difficulty | Impact |
|-----|----------|---------------|--------|
| #1 Local node uptime | High | Easy | Incorrect fair shares |
| #2 Remote uptime init | Medium | Easy | Incorrect fair shares |
| #3 Fee report threshold | Critical | Easy | Missing fee data |
| #4 Period edge case | Medium | Easy | Data loss at period boundary |
| #5 Advisor mode blocks | Critical | Medium | No settlements execute |
| #6 Missing BOLT12 offers | High | N/A (design) | Settlement failures |

---

## Recommended Fix Priority

1. **Immediate**: Fix #3 (fee report threshold) - saves data correctly
2. **Immediate**: Fix #1 (local uptime) - accurate fair shares
3. **Soon**: Fix #5 (advisor mode) - enable settlement execution
4. **Soon**: Fix #2 (remote uptime init) - accurate remote member data
5. **Later**: Fix #4 (period edge) - edge case handling

---

## Test Recommendations

1. Add test for local node presence initialization
2. Add test for fee report saving independent of broadcast threshold
3. Add test for settlement execution in advisor mode
4. Add integration test for end-to-end settlement flow
5. Add test for period boundary handling

---

## Files Modified

- `cl-hive.py`: Lines 1838, 3872-3946
- `modules/settlement.py`: Lines 1049-1127 (gather_contributions_from_gossip)
