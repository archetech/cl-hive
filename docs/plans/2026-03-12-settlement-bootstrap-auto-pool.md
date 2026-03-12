# Settlement Bootstrap Auto Pool Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make weekly routing-pool settlement automatic on the existing settlement cadence, backfill missed weeks oldest-first, allow settlement readiness with `1/2` votes in a two-member hive, and surface why settlement proposals do not get auto-voted.

**Architecture:** Keep orchestration in the existing hourly `settlement_loop()`, but extract a testable helper for routing-pool backlog handling. Add a tiny persistence layer for cleared pool weeks so zero-revenue or no-contribution periods do not retry forever, keep settlement quorum logic inside `SettlementManager`, and expose structured rejection reasons from `verify_and_vote()` so protocol handlers and the loop can log the real failure mode.

**Tech Stack:** Python, pytest, Core Lightning plugin RPC, sqlite-backed `HiveDatabase`, background threads in `background_loops.py`

---

### Task 1: Add Pool Settlement Marker Persistence

**Files:**
- Modify: `modules/database.py`
- Test: `tests/test_routing_pool.py`

**Step 1: Write the failing test**

Add a focused round-trip test for cleared pool weeks.

```python
def test_pool_settlement_marker_round_trip(database, mock_plugin):
    assert database.get_pool_settlement_marker("2026-W01") is None

    marked = database.mark_pool_period_cleared(
        period="2026-W01",
        reason="zero_total_revenue",
    )

    assert marked is True
    marker = database.get_pool_settlement_marker("2026-W01")
    assert marker["period"] == "2026-W01"
    assert marker["reason"] == "zero_total_revenue"
```

Add an idempotency test too.

```python
def test_pool_settlement_marker_is_idempotent(database, mock_plugin):
    assert database.mark_pool_period_cleared("2026-W01", "zero_total_revenue") is True
    assert database.mark_pool_period_cleared("2026-W01", "zero_total_revenue") is False
```

**Step 2: Run test to verify it fails**

Run: `/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_routing_pool.py -k pool_settlement_marker -v`

Expected: `AttributeError` or `sqlite3` failure because the marker table/helpers do not exist yet.

**Step 3: Write minimal implementation**

Add a dedicated table and helpers in `modules/database.py`.

```python
conn.execute("""
    CREATE TABLE IF NOT EXISTS pool_settlement_markers (
        period TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        reason TEXT,
        settled_at INTEGER NOT NULL
    )
""")

def mark_pool_period_cleared(self, period: str, reason: str) -> bool:
    normalized_period = self._normalize_pool_period(period)
    try:
        self._get_connection().execute(
            """
            INSERT INTO pool_settlement_markers (period, status, reason, settled_at)
            VALUES (?, 'cleared', ?, ?)
            """,
            (normalized_period, reason, int(time.time())),
        )
        return True
    except sqlite3.IntegrityError:
        return False

def get_pool_settlement_marker(self, period: str) -> Optional[Dict[str, Any]]:
    normalized_period = self._normalize_pool_period(period)
    row = self._get_connection().execute(
        "SELECT * FROM pool_settlement_markers WHERE period = ?",
        (normalized_period,),
    ).fetchone()
    return dict(row) if row else None
```

Also add a helper for backlog discovery.

```python
def get_pool_candidate_periods_up_to(self, max_period: str) -> List[str]:
    # Union of weeks derived from pool_revenue.recorded_at plus stored
    # pool_contributions/pool_distributions periods, normalized and sorted.
```

**Step 4: Run test to verify it passes**

Run: `/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_routing_pool.py -k pool_settlement_marker -v`

Expected: PASS

**Step 5: Commit**

```bash
git add modules/database.py tests/test_routing_pool.py
git commit -m "feat: persist routing pool settlement markers"
```

### Task 2: Add Settlement Bootstrap Quorum and Rejection Reasons

**Files:**
- Modify: `modules/settlement.py`
- Test: `tests/test_distributed_settlement.py`

**Step 1: Write the failing tests**

Add bootstrap quorum coverage.

```python
def test_quorum_reached_with_one_of_two_members(
    settlement_manager, mock_database
):
    mock_database.get_settlement_ready_votes.return_value = [
        {"voter_peer_id": "peer_a"},
    ]
    mock_database.get_all_members.return_value = [
        {"peer_id": "peer_a"},
        {"peer_id": "peer_b"},
    ]
    mock_database.get_settlement_proposal.return_value = {
        "proposal_id": "test_proposal",
        "status": "pending",
    }

    result = settlement_manager.check_quorum_and_mark_ready(
        proposal_id="test_proposal",
        member_count=2,
    )

    assert result is True
```

Add rejection-reason coverage.

```python
def test_verify_and_vote_records_hash_mismatch_reason(
    settlement_manager, mock_database, mock_state_manager, mock_rpc
):
    proposal = {
        "proposal_id": "test_proposal_123",
        "period": "2024-05",
        "data_hash": "wrong_hash_" + "x" * 54,
        "plan_hash": "y" * 64,
        "total_fees_sats": 18000,
        "member_count": 3,
    }

    vote = settlement_manager.verify_and_vote(
        proposal=proposal,
        our_peer_id="02" + "a" * 64,
        state_manager=mock_state_manager,
        rpc=mock_rpc,
    )

    assert vote is None
    assert settlement_manager.last_verify_and_vote_reason["reason"] == "hash_mismatch"
```

Add at least one more reason test for `already_voted` or `expired`.

**Step 2: Run test to verify it fails**

Run: `/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_distributed_settlement.py -k "one_of_two_members or records_hash_mismatch_reason or already_voted_reason" -v`

Expected: FAIL because quorum remains `2/2` and no rejection-reason state exists.

**Step 3: Write minimal implementation**

In `SettlementManager`, add a stored reason payload and a small helper.

```python
def _set_verify_and_vote_reason(self, reason: str, proposal_id: str, period: str, **extra) -> None:
    self.last_verify_and_vote_reason = {
        "reason": reason,
        "proposal_id": proposal_id,
        "period": period,
        **extra,
    }
```

Use it on every early-return path in `verify_and_vote()`.

```python
if db_proposal and db_proposal.get("expires_at", 0) < int(time.time()):
    self._set_verify_and_vote_reason("expired", proposal_id, period)
    return None
```

Keep the public return type unchanged: success still returns the vote dict, rejection still returns `None`.

Then narrow settlement bootstrap quorum inside `check_quorum_and_mark_ready()`.

```python
def _settlement_quorum_needed(self, active_count: int) -> int:
    if active_count == 2:
        return 1
    return (active_count // 2) + 1
```

**Step 4: Run test to verify it passes**

Run: `/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_distributed_settlement.py -k "one_of_two_members or records_hash_mismatch_reason or already_voted_reason" -v`

Expected: PASS

**Step 5: Commit**

```bash
git add modules/settlement.py tests/test_distributed_settlement.py
git commit -m "feat: add settlement bootstrap quorum and vote diagnostics"
```

### Task 3: Extract and Implement Automatic Pool Backlog Settlement

**Files:**
- Modify: `modules/background_loops.py`
- Modify: `modules/database.py`
- Test: `tests/test_routing_settlement_bugfixes.py`

**Step 1: Write the failing tests**

Add direct unit tests for a new helper instead of testing the infinite loop.

```python
def test_auto_finalize_pool_backlog_settles_oldest_unsettled_period_first(
    mock_db, mock_plugin
):
    routing_pool = MagicMock()
    settlement_mgr = MagicMock()
    settlement_mgr.get_previous_period.return_value = "2026-W10"
    mock_db.get_pool_candidate_periods_up_to.return_value = ["2026-W08", "2026-W09", "2026-W10"]
    mock_db.get_pool_distributions.side_effect = [[], [{"period": "2026-W09"}], []]
    mock_db.get_pool_settlement_marker.return_value = None
    mock_db.get_pool_revenue.return_value = {"total_sats": 5000}
    mock_db.get_pool_contributions.return_value = [{"member_id": PEER_A, "pool_share": 1.0}]

    settled = background_loops._auto_finalize_pool_backlog(
        routing_pool=routing_pool,
        settlement_mgr=settlement_mgr,
        database=mock_db,
        plugin=mock_plugin,
    )

    assert settled == "2026-W08"
    routing_pool.settle_period.assert_called_once_with("2026-W08")
```

Add empty-period coverage.

```python
def test_auto_finalize_pool_backlog_marks_zero_revenue_period_cleared(
    mock_db, mock_plugin
):
    routing_pool = MagicMock()
    settlement_mgr = MagicMock()
    settlement_mgr.get_previous_period.return_value = "2026-W10"
    mock_db.get_pool_candidate_periods_up_to.return_value = ["2026-W09"]
    mock_db.get_pool_distributions.return_value = []
    mock_db.get_pool_settlement_marker.return_value = None
    mock_db.get_pool_revenue.return_value = {"total_sats": 0}
    mock_db.get_pool_contributions.return_value = [{"member_id": PEER_A, "pool_share": 1.0}]

    settled = background_loops._auto_finalize_pool_backlog(
        routing_pool=routing_pool,
        settlement_mgr=settlement_mgr,
        database=mock_db,
        plugin=mock_plugin,
    )

    assert settled == "2026-W09"
    mock_db.mark_pool_period_cleared.assert_called_once_with("2026-W09", "zero_total_revenue")
```

Add current-week safety coverage by ensuring `get_previous_period()` is the ceiling.

**Step 2: Run test to verify it fails**

Run: `/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_routing_settlement_bugfixes.py -k "auto_finalize_pool_backlog" -v`

Expected: FAIL because the helper does not exist yet.

**Step 3: Write minimal implementation**

Extract a helper from `settlement_loop()`.

```python
def _auto_finalize_pool_backlog(routing_pool, settlement_mgr, database, plugin):
    previous_period = settlement_mgr.get_previous_period()
    for period in database.get_pool_candidate_periods_up_to(previous_period):
        if database.get_pool_distributions(period):
            continue
        if database.get_pool_settlement_marker(period):
            continue

        contributions = database.get_pool_contributions(period)
        if not contributions:
            routing_pool.snapshot_contributions(period)
            contributions = database.get_pool_contributions(period)

        total_revenue = database.get_pool_revenue(period=period).get("total_sats", 0)
        if total_revenue == 0:
            database.mark_pool_period_cleared(period, "zero_total_revenue")
            return period
        if not contributions:
            database.mark_pool_period_cleared(period, "no_contributions")
            return period

        routing_pool.settle_period(period)
        return period
    return None
```

Wire it into `settlement_loop()` ahead of proposal creation and keep the helper to one backlog period per cycle.

**Step 4: Run test to verify it passes**

Run: `/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_routing_settlement_bugfixes.py -k "auto_finalize_pool_backlog" -v`

Expected: PASS

**Step 5: Commit**

```bash
git add modules/background_loops.py modules/database.py tests/test_routing_settlement_bugfixes.py
git commit -m "feat: auto-settle routing pool backlog on cadence"
```

### Task 4: Add Settlement Proposal Diagnostics at the Protocol Boundary

**Files:**
- Modify: `modules/protocol_handlers.py`
- Modify: `modules/background_loops.py`
- Test: `tests/test_settlement_protocol_handlers.py`

**Step 1: Write the failing tests**

Create a focused handler test file.

```python
def test_handle_settlement_propose_logs_verify_rejection_reason(monkeypatch):
    plugin = MagicMock()
    settlement_mgr = MagicMock()
    settlement_mgr.verify_and_vote.return_value = None
    settlement_mgr.last_verify_and_vote_reason = {
        "reason": "hash_mismatch",
        "proposal_id": "test_proposal_123",
        "period": "2026-W09",
    }

    # patch protocol_handlers globals: settlement_mgr, database, state_manager, our_pubkey
    # patch signature verification and member lookups to succeed

    result = protocol_handlers.handle_settlement_propose(peer_id=PEER_B, payload=payload, plugin=plugin)

    assert result == {"result": "continue"}
    plugin.log.assert_any_call(
        "SETTLEMENT: Proposal test_proposal_12... not voted locally (reason=hash_mismatch, period=2026-W09)",
        level="info",
    )
```

Add loop-side logging coverage too if you factor the log message into a helper.

**Step 2: Run test to verify it fails**

Run: `/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_settlement_protocol_handlers.py -v`

Expected: FAIL because the new test file and log path do not exist yet.

**Step 3: Write minimal implementation**

After `verify_and_vote()` returns `None`, log the stored rejection reason instead of silently continuing.

```python
if vote:
    ...
else:
    reason = getattr(settlement_mgr, "last_verify_and_vote_reason", None) or {}
    plugin.log(
        f"SETTLEMENT: Proposal {proposal_id[:12]}... not voted locally "
        f"(reason={reason.get('reason', 'unknown')}, period={period})",
        level="info",
    )
```

Mirror the same pattern in `settlement_loop()` step 3 when processing pending proposals so silent local failures become visible there too.

**Step 4: Run test to verify it passes**

Run: `/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_settlement_protocol_handlers.py -v`

Expected: PASS

**Step 5: Commit**

```bash
git add modules/protocol_handlers.py modules/background_loops.py tests/test_settlement_protocol_handlers.py
git commit -m "feat: log settlement auto-vote rejection reasons"
```

### Task 5: Final Verification

**Files:**
- Verify only

**Step 1: Run the targeted settlement regression suite**

Run:

```bash
/home/sat/bin/cl-hive/.venv/bin/python -m pytest \
  tests/test_distributed_settlement.py \
  tests/test_routing_pool.py \
  tests/test_routing_settlement_bugfixes.py \
  tests/test_settlement_protocol_handlers.py \
  tests/test_protocol.py \
  tests/test_outbox.py \
  tests/test_outbox_7_fixes.py -q
```

Expected: PASS

**Step 2: Attempt the repo-wide baseline**

Run: `/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/ -q`

Expected: Ideally PASS. If it still stalls after reproducing the same planning behavior, record that explicitly as a preexisting suite/runtime issue rather than claiming a full pass.

**Step 3: Commit verification-only updates if needed**

```bash
git status --short
```

If verification did not require code/doc edits, no commit is needed here.
