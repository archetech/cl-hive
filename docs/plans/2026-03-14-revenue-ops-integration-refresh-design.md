# Revenue-Ops Integration Refresh Design

**Date**: 2026-03-14
**Status**: Approved

## Problem

Recent `cl_revenue_ops` changes broke or outdated several `cl_hive`
integration points:

- `revenue-status` no longer exposes fee bounds under `config.fee_range_ppm`
- `revenue-policy` is now diagnostic-first for normal callers, with tactical
  writes requiring explicit internal or admin override
- manual policy fee multipliers are no longer the primary autoband mechanism;
  learned auto bands now take precedence when available
- `revenue-status` now exposes richer operator/debug surfaces that `cl_hive`
  docs and MCP descriptions do not reflect

The result is one real bridge compatibility bug, one real MCP behavior bug, and
several operator-facing surfaces that steer users toward stale workflows.

## Goals

- Restore compatibility with current `cl_revenue_ops` `revenue-status`
- Keep internal `cl_hive` orchestration working with current `revenue-policy`
  semantics
- Refresh the MCP `revenue_policy` and `revenue_status` surfaces to match
  current upstream behavior
- Update the highest-signal docs and prompts so agents stop recommending stale
  autoband and policy workflows

## Non-Goals

- No large version-gating framework for mixed old/new `cl_revenue_ops`
  deployments
- No removal of the `revenue_policy` MCP tool
- No broad documentation sweep beyond the MCP server, MCP docs, and primary
  operator prompts

## Chosen Approach

Use a conservative compatibility shim plus operator-surface refresh.

### Bridge compatibility

Update `Bridge.get_fee_config()` to read the current `revenue-status` shape
first:

- `operator_controls.values.min_fee_ppm`
- `operator_controls.values.max_fee_ppm`

If those are missing, fall back to the legacy
`config.fee_range_ppm = [min, max]` structure. This keeps `cl_hive`
compatible with both current and older `cl_revenue_ops` releases while fixing
planner fee inference immediately.

### MCP policy semantics

Keep the `revenue_policy` MCP tool, but make it explicitly diagnostic-first.

- Add read-only `find` and `changes` actions
- Keep `list` and `get`
- Keep `set` and `delete`, but require an explicit override flag from the MCP
  caller before forwarding them
- When write override is present, forward that intent to `cl_revenue_ops` via
  `internal=True`

This preserves deliberate automation while matching upstream’s
"no tactical writes by default" guard.

### Internal automation

Any `cl_hive` automation that still writes `revenue-policy` as an internal
orchestration step should pass the same explicit override itself. Known write
paths in the MCP server include stagnant remediation and bulk policy
application. The low-level hive membership sync path is already correct.

### Operator/docs refresh

Update the MCP tool descriptions and the highest-signal docs/prompts to reflect
current upstream behavior:

- manual fee multipliers are fallback bands, not the primary autoband workflow
- `revenue_status` surfaces operator controls and decision/debug state
- `revenue_policy` is primarily diagnostic, with writes as explicit override

## Files In Scope

### Code

- `modules/bridge.py`
- `tools/mcp-hive-server.py`
- `tests/test_bridge.py`
- `tests/test_mcp_hive_server.py`

### Docs and prompts

- `docs/MCP_SERVER.md`
- `MOLTY.md`
- `production.example/strategy-prompts/system_prompt.md`

## Testing Strategy

### Bridge tests

Add targeted tests covering:

- current `revenue-status` shape using `operator_controls.values`
- fallback to legacy `config.fee_range_ppm`
- graceful `None` when fee bounds are unavailable

### MCP server tests

Extend MCP server coverage to assert:

- the `revenue_policy` schema exposes `find` and `changes`
- writes require an explicit override
- internal helper flows that still mutate policy pass the override
- tool descriptions no longer describe manual multipliers as the primary
  autoband path

If import-based handler testing is practical, prefer it. Otherwise add precise
source-structure assertions matching the repo’s existing MCP test style.

### Verification

At minimum, rerun:

- `pytest tests/test_bridge.py tests/test_mcp_hive_server.py -q`

If the handler changes affect adjacent revenue-ops flows, extend verification to
those targeted tests before completion.

## Risks

- Making MCP writes permissive again would undo upstream’s policy guard, so
  write access must stay explicit
- The MCP server tests are currently mixed between behavior and source
  inspection, so the test additions should stay tight and avoid fragile
  overreach
