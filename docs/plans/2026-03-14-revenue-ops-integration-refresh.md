# Revenue-Ops Integration Refresh Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Restore `cl_hive` compatibility with current `cl_revenue_ops` policy and status semantics, and refresh the MCP/operator surfaces to match the new behavior.

**Architecture:** Add a bridge compatibility shim for the new `revenue-status` shape, make MCP `revenue_policy` diagnostic-first with explicit write override, and update the highest-signal docs/prompts to reflect current autoband and status semantics. Drive the change with targeted bridge and MCP tests first.

**Tech Stack:** Python, pytest, CLN RPC wrappers, MCP tool registry/docs

---

### Task 1: Cover New Revenue Status Shape In Bridge Tests

**Files:**
- Modify: `tests/test_bridge.py`
- Modify: `modules/bridge.py`

**Step 1: Write the failing tests**

Add bridge tests for:
- `operator_controls.values.min_fee_ppm` / `max_fee_ppm`
- fallback to legacy `config.fee_range_ppm`
- missing fee bounds returns `None`

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_bridge.py -q`
Expected: FAIL on the new fee-config tests because `get_fee_config()` still only reads `config.fee_range_ppm`

**Step 3: Write minimal implementation**

Update `Bridge.get_fee_config()` to:
- read `operator_controls.values.min_fee_ppm` and `max_fee_ppm` first
- fall back to `config.fee_range_ppm`
- return `None` if neither shape is valid

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_bridge.py -q`
Expected: PASS

**Step 5: Commit**

```bash
git add tests/test_bridge.py modules/bridge.py
git commit -m "fix: support new revenue-status fee controls"
```

### Task 2: Cover MCP Revenue Policy Surface Changes

**Files:**
- Modify: `tests/test_mcp_hive_server.py`
- Modify: `tools/mcp-hive-server.py`

**Step 1: Write the failing tests**

Add MCP tests that assert:
- `revenue_policy` tool enum includes `find` and `changes`
- the tool description frames `revenue_policy` as diagnostic-first
- `handle_revenue_policy()` requires an explicit override for `set` and `delete`

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_mcp_hive_server.py -q`
Expected: FAIL because the current schema only supports `list|get|set|delete` and writes are forwarded without override gating

**Step 3: Write minimal implementation**

Update `tools/mcp-hive-server.py` to:
- expand supported actions to `list|get|find|changes|set|delete`
- add an explicit write-override argument
- require the override for `set` and `delete`
- pass `internal=True` when an internal write is intentionally allowed
- refresh the MCP descriptions for `revenue_policy` and `revenue_status`

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_mcp_hive_server.py -q`
Expected: PASS

**Step 5: Commit**

```bash
git add tests/test_mcp_hive_server.py tools/mcp-hive-server.py
git commit -m "fix: refresh revenue policy mcp surface"
```

### Task 3: Preserve Internal MCP Automation Writes

**Files:**
- Modify: `tests/test_mcp_hive_server.py`
- Modify: `tools/mcp-hive-server.py`

**Step 1: Write the failing tests**

Add targeted tests or source assertions for the MCP helper flows that still
write `revenue-policy`, especially:
- stagnant remediation
- bulk policy application

The tests should assert those internal flows pass the explicit policy-write
override instead of relying on permissive upstream behavior.

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_mcp_hive_server.py -q`
Expected: FAIL because these flows currently call `revenue-policy set` without
override

**Step 3: Write minimal implementation**

Update those helper paths to pass the explicit override flag through to
`handle_revenue_policy()` or directly to `revenue-policy` calls, whichever is
already idiomatic for that code path.

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_mcp_hive_server.py -q`
Expected: PASS

**Step 5: Commit**

```bash
git add tests/test_mcp_hive_server.py tools/mcp-hive-server.py
git commit -m "fix: preserve internal revenue policy automation"
```

### Task 4: Refresh Docs And Prompts

**Files:**
- Modify: `docs/MCP_SERVER.md`
- Modify: `MOLTY.md`
- Modify: `production.example/strategy-prompts/system_prompt.md`
- Modify: `tools/mcp-hive-server.py`

**Step 1: Write the failing checks**

Add or reuse source assertions in `tests/test_mcp_hive_server.py` that verify:
- `revenue_status` description mentions operator controls / decision state
- `revenue_policy` description no longer treats manual multipliers as the main
  autoband workflow

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_mcp_hive_server.py -q`
Expected: FAIL while old descriptions remain

**Step 3: Write minimal implementation**

Update the high-signal docs/prompts so they match current upstream semantics:
- diagnostic-first `revenue_policy`
- manual bands as fallback
- richer `revenue_status` surface

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_mcp_hive_server.py -q`
Expected: PASS

**Step 5: Commit**

```bash
git add docs/MCP_SERVER.md MOLTY.md production.example/strategy-prompts/system_prompt.md tools/mcp-hive-server.py tests/test_mcp_hive_server.py
git commit -m "docs: refresh revenue ops integration guidance"
```

### Task 5: Final Verification

**Files:**
- Verify the full worktree diff only; no planned new files

**Step 1: Run targeted verification**

Run:

```bash
python3 -m pytest tests/test_bridge.py tests/test_mcp_hive_server.py -q
```

Expected: PASS

**Step 2: Review diff**

Run:

```bash
git status --short
git diff --stat
```

Expected: Only the intended bridge, MCP, test, and doc files are changed

**Step 3: Commit final polish if needed**

```bash
git add modules/bridge.py tools/mcp-hive-server.py tests/test_bridge.py tests/test_mcp_hive_server.py docs/MCP_SERVER.md MOLTY.md production.example/strategy-prompts/system_prompt.md docs/plans/2026-03-14-revenue-ops-integration-refresh-design.md docs/plans/2026-03-14-revenue-ops-integration-refresh.md
git commit -m "fix: align cl-hive with current revenue ops surfaces"
```
