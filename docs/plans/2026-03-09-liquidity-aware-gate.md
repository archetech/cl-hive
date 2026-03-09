# Liquidity-Aware Proposal Gate Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prevent the topology planner from queuing expansion proposals that collectively exceed available on-chain funds.

**Architecture:** Add one database query (`get_pending_channel_open_total`) that sums `proposed_size_sats` from pending `channel_open` actions, then deduct that from the existing three-way budget calculation in `_propose_expansion()`. Zero new tables, zero new config.

**Tech Stack:** Python, SQLite (json_extract), pytest

**Design Doc:** `docs/plans/2026-03-09-liquidity-aware-gate-design.md`

---

### Task 1: Database Query — `get_pending_channel_open_total()`

**Files:**
- Modify: `modules/database.py` (add method near `get_available_budget` at ~line 4103)
- Test: `tests/test_liquidity_gate.py` (create)

**Step 1: Write the failing tests**

Create `tests/test_liquidity_gate.py`:

```python
"""Tests for liquidity-aware expansion proposal gate."""
import json
import sqlite3
import time


def _create_test_db():
    """Create in-memory DB with pending_actions table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE pending_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            proposed_at INTEGER NOT NULL,
            expires_at INTEGER,
            status TEXT DEFAULT 'pending',
            rejection_reason TEXT
        )
    """)
    return conn


def _insert_pending_action(conn, action_type, payload, status='pending',
                           expires_at=None):
    """Insert a test pending action."""
    now = int(time.time())
    if expires_at is None:
        expires_at = now + 86400  # 24h from now
    conn.execute(
        "INSERT INTO pending_actions (action_type, payload, proposed_at, expires_at, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (action_type, json.dumps(payload), now, expires_at, status),
    )
    conn.commit()


def test_pending_total_empty():
    """No pending actions returns 0."""
    from modules.database import _get_pending_channel_open_total_sql
    conn = _create_test_db()
    assert _get_pending_channel_open_total_sql(conn) == 0


def test_pending_total_sums_correctly():
    """Two pending channel_open proposals sum their sizes."""
    from modules.database import _get_pending_channel_open_total_sql
    conn = _create_test_db()
    _insert_pending_action(conn, 'channel_open', {'proposed_size_sats': 1_000_000})
    _insert_pending_action(conn, 'channel_open', {'proposed_size_sats': 2_000_000})
    assert _get_pending_channel_open_total_sql(conn) == 3_000_000


def test_pending_total_excludes_expired():
    """Expired proposals are not counted."""
    from modules.database import _get_pending_channel_open_total_sql
    conn = _create_test_db()
    past = int(time.time()) - 3600  # expired 1h ago
    _insert_pending_action(conn, 'channel_open', {'proposed_size_sats': 1_000_000},
                           expires_at=past)
    _insert_pending_action(conn, 'channel_open', {'proposed_size_sats': 500_000})
    assert _get_pending_channel_open_total_sql(conn) == 500_000


def test_pending_total_excludes_non_pending():
    """Approved/rejected/executed actions are not counted."""
    from modules.database import _get_pending_channel_open_total_sql
    conn = _create_test_db()
    _insert_pending_action(conn, 'channel_open', {'proposed_size_sats': 1_000_000},
                           status='approved')
    _insert_pending_action(conn, 'channel_open', {'proposed_size_sats': 2_000_000},
                           status='rejected')
    _insert_pending_action(conn, 'channel_open', {'proposed_size_sats': 500_000},
                           status='pending')
    assert _get_pending_channel_open_total_sql(conn) == 500_000


def test_pending_total_fallback_to_channel_size_sats():
    """Falls back to channel_size_sats when proposed_size_sats is missing."""
    from modules.database import _get_pending_channel_open_total_sql
    conn = _create_test_db()
    _insert_pending_action(conn, 'channel_open', {'channel_size_sats': 3_000_000})
    assert _get_pending_channel_open_total_sql(conn) == 3_000_000


def test_pending_total_ignores_non_channel_open():
    """Non-channel_open actions are not counted."""
    from modules.database import _get_pending_channel_open_total_sql
    conn = _create_test_db()
    _insert_pending_action(conn, 'ban', {'amount_sats': 999_999})
    _insert_pending_action(conn, 'channel_open', {'proposed_size_sats': 1_000_000})
    assert _get_pending_channel_open_total_sql(conn) == 1_000_000
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_liquidity_gate.py -v`
Expected: FAIL with `ImportError: cannot import name '_get_pending_channel_open_total_sql'`

**Step 3: Write minimal implementation**

Add to `modules/database.py` after the `get_available_budget` method (~line 4103). Follow the existing pattern of standalone SQL functions (like `_reserve_budget_atomic` in cl-revenue-ops or `_revenue_by_size_bucket_sql`):

```python
def _get_pending_channel_open_total_sql(conn) -> int:
    """Sum proposed_size_sats from all active pending channel_open actions.

    Uses json_extract to read the size from the payload JSON.
    Falls back to channel_size_sats if proposed_size_sats is absent.
    Excludes expired and non-pending actions.
    """
    now = int(time.time())
    row = conn.execute("""
        SELECT COALESCE(SUM(
            COALESCE(
                json_extract(payload, '$.proposed_size_sats'),
                json_extract(payload, '$.channel_size_sats'),
                0
            )
        ), 0) AS total
        FROM pending_actions
        WHERE action_type = 'channel_open'
          AND status = 'pending'
          AND (expires_at IS NULL OR expires_at > ?)
    """, (now,)).fetchone()
    return int(row[0] if row else 0)
```

Then add the instance method on the `Database` class (near `get_available_budget`):

```python
    def get_pending_channel_open_total(self) -> int:
        """Sum of proposed_size_sats from all pending channel_open actions."""
        conn = self._get_connection()
        return _get_pending_channel_open_total_sql(conn)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_liquidity_gate.py -v`
Expected: 6 passed

**Step 5: Commit**

```bash
git add tests/test_liquidity_gate.py modules/database.py
git commit -m "feat: add get_pending_channel_open_total query for liquidity gate"
```

---

### Task 2: Planner Integration — Deduct Pending from Available Budget

**Files:**
- Modify: `modules/planner.py:2177-2203` (budget calculation in `_propose_expansion`)
- Modify: `tests/test_liquidity_gate.py` (add integration test)

**Step 1: Write the failing test**

Add to `tests/test_liquidity_gate.py`:

```python
def test_expansion_blocked_by_pending():
    """Planner skips expansion when pending proposals exhaust available budget."""
    from unittest.mock import MagicMock, patch
    from modules.planner import TopologyPlanner

    mock_plugin = MagicMock()
    mock_db = MagicMock()
    planner = TopologyPlanner(mock_plugin, mock_db)

    # Setup: 2M daily budget, 10M onchain, 20% reserve = 8M spendable
    # max_per_channel = 2M * 0.5 = 1M
    # available = min(2M, 8M, 1M) = 1M
    # pending = 1M from existing proposal
    # net available = 1M - 1M = 0 < min_channel_size (1M)
    mock_config = MagicMock()
    mock_config.failsafe_budget_per_day = 2_000_000
    mock_config.budget_reserve_pct = 0.20
    mock_config.budget_max_per_channel_pct = 0.50
    mock_config.planner_enable_expansions = True
    mock_config.planner_min_channel_sats = 1_000_000
    mock_config.planner_max_channel_sats = 50_000_000
    mock_config.planner_default_channel_sats = 5_000_000
    mock_config.planner_max_active_channels = 50
    mock_config.max_expansion_feerate_perkb = 5000
    mock_config.governance_mode = 'advisor'
    mock_config.market_share_cap = 0.20

    mock_db.get_available_budget.return_value = 2_000_000
    mock_db.get_pending_channel_open_total.return_value = 1_000_000
    mock_db.get_pending_intents.return_value = []

    mock_plugin.rpc.listfunds.return_value = {
        'outputs': [{'status': 'confirmed', 'amount_msat': 10_000_000_000}]
    }
    mock_plugin.rpc.feerates.return_value = {
        'perkb': {'opening': 1000}
    }

    from modules.planner import UnderservedResult
    with patch.object(planner, 'get_underserved_targets') as mock_targets, \
         patch.object(planner, '_get_node_summary', return_value={}):
        mock_targets.return_value = [
            UnderservedResult(
                target='02' + 'a' * 64,
                public_capacity_sats=200_000_000,
                hive_share_pct=0.02,
                score=2.0,
            )
        ]
        decisions = planner._propose_expansion(mock_config, 'test-gate')

    assert len(decisions) == 1
    assert decisions[0]['action'] == 'expansion_skipped'
    assert decisions[0]['reason'] == 'insufficient_budget'
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_liquidity_gate.py::test_expansion_blocked_by_pending -v`
Expected: FAIL (planner doesn't call `get_pending_channel_open_total` yet, so budget looks sufficient)

**Step 3: Modify the planner budget calculation**

In `modules/planner.py`, replace the budget calculation block (~lines 2177-2203):

Find this code:
```python
        daily_remaining = self.db.get_available_budget(daily_budget)
        spendable_onchain = int(onchain_balance * (1.0 - budget_reserve_pct))
        max_per_channel = int(daily_budget * budget_max_per_channel_pct)

        available_budget = min(daily_remaining, spendable_onchain, max_per_channel)

        if available_budget < min_channel_size:
            self._log(
                f"Skipping expansion to {selected_target.target[:16]}... - "
                f"insufficient budget ({available_budget:,} < {min_channel_size:,} min). "
                f"daily_remaining={daily_remaining:,}, spendable={spendable_onchain:,}, "
                f"max_per_channel={max_per_channel:,}",
                level='info'
            )
```

Replace with:
```python
        daily_remaining = self.db.get_available_budget(daily_budget)
        spendable_onchain = int(onchain_balance * (1.0 - budget_reserve_pct))
        max_per_channel = int(daily_budget * budget_max_per_channel_pct)

        pending_committed = self.db.get_pending_channel_open_total()
        gross_available = min(daily_remaining, spendable_onchain, max_per_channel)
        available_budget = max(0, gross_available - pending_committed)

        if available_budget < min_channel_size:
            self._log(
                f"Skipping expansion to {selected_target.target[:16]}... - "
                f"insufficient budget ({available_budget:,} < {min_channel_size:,} min). "
                f"gross={gross_available:,}, pending_committed={pending_committed:,}, "
                f"daily_remaining={daily_remaining:,}, spendable={spendable_onchain:,}, "
                f"max_per_channel={max_per_channel:,}",
                level='info'
            )
```

**Step 4: Run all tests to verify they pass**

Run: `python3 -m pytest tests/test_liquidity_gate.py -v`
Expected: 7 passed

Then run the existing planner tests to check for regressions (they mock `get_available_budget` but not `get_pending_channel_open_total`, so we need to ensure the mock's default `MagicMock()` return value doesn't break things — it returns a MagicMock object, not an int). If existing tests fail, add `mock_database.get_pending_channel_open_total.return_value = 0` to the test fixtures.

Run: `python3 -m pytest tests/test_planner.py -v`
Expected: All existing tests pass (may need mock fix — see above)

**Step 5: Commit**

```bash
git add modules/planner.py tests/test_liquidity_gate.py
git commit -m "feat: deduct pending proposals from expansion budget gate"
```

---

### Task 3: Regression Fix & Final Validation

**Files:**
- Possibly modify: `tests/test_planner.py` (add mock default if needed)

**Step 1: Run full test suite**

Run: `python3 -m pytest tests/ -v`

If existing planner tests fail because `get_pending_channel_open_total` returns a MagicMock instead of int, add to the test fixture or setUp:

```python
mock_database.get_pending_channel_open_total.return_value = 0
```

**Step 2: Run full test suite again**

Run: `python3 -m pytest tests/ -v`
Expected: All tests pass

**Step 3: Commit if any fixes were needed**

```bash
git add tests/test_planner.py
git commit -m "test: add pending_channel_open_total mock default for existing planner tests"
```
