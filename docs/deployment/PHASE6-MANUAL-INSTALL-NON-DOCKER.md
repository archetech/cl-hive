# Phase 6 Manual Install Plan (Non-Docker Members)

**Status:** Planning-only runbook (do not execute until Phase 6 gates pass)  
**Audience:** Existing `cl-hive` members running direct/non-docker installations  
**Last Updated:** 2026-02-17

---

## 1. Purpose

Provide a safe manual upgrade path for existing non-docker nodes when the Phase 6 split is released:
- `cl-hive-comms` (new)
- `cl-hive-archon` (new, optional)
- `cl-hive` (existing coordination plugin)

This document is intentionally staged as a runbook before implementation to reduce migration risk.

---

## 2. Target Local Layout

Expected local checkouts under `~/bin`:
- `~/bin/cl-hive`
- `~/bin/cl_revenue_ops`
- `~/bin/cl-hive-comms`
- `~/bin/cl-hive-archon`

This aligns with current operator convention used for `cl-hive` and `cl_revenue_ops`.

---

## 3. Preflight Checklist (Before Any Upgrade)

1. Confirm current plugin status:
   - `lightning-cli plugin list`
2. Confirm full test baseline on release branch:
   - `python3 -m pytest tests -q`
3. Back up CLN data and plugin DBs.
4. Confirm rollback window and maintenance window.

Do not proceed if any preflight item fails.

---

## 4. Planned Install Order

When Phase 6 is approved for execution:

1. Install `cl-hive-comms` first.
2. Optionally install `cl-hive-archon` second.
3. Keep `cl-hive` enabled for hive-member functionality.
4. Validate plugin interoperability after each step.

Rationale:
- `cl-hive-comms` is the transport and client entry point.
- `cl-hive-archon` depends on comms.
- `cl-hive` should detect and cooperate with sibling plugins.

---

## 5. Planned lightningd Config Pattern

Example plugin lines (future state):

```ini
plugin=/home/sat/bin/cl_revenue_ops/cl-revenue-ops.py
plugin=/home/sat/bin/cl-hive-comms/cl-hive-comms.py
plugin=/home/sat/bin/cl-hive-archon/cl-hive-archon.py
plugin=/home/sat/bin/cl-hive/cl-hive.py
```

If running without Archon:

```ini
plugin=/home/sat/bin/cl_revenue_ops/cl-revenue-ops.py
plugin=/home/sat/bin/cl-hive-comms/cl-hive-comms.py
plugin=/home/sat/bin/cl-hive/cl-hive.py
```

---

## 6. Validation Steps (Future Execution)

After each plugin enablement:

1. Verify plugin list:
   - `lightning-cli plugin list`
2. Verify baseline RPCs:
   - `lightning-cli hive-status`
3. Verify comms/client RPC availability:
   - `lightning-cli help | grep hive-client`
4. If archon enabled, verify identity RPC availability:
   - `lightning-cli help | grep hive-archon`
5. Confirm logs show no cyclic startup failures.

---

## 7. Rollback Procedure (Manual)

If issues appear:

1. Stop `lightningd`.
2. Remove or comment new plugin lines.
3. Restart with prior plugin set (`cl-hive` + `cl_revenue_ops`).
4. Restore DB backup only if required by incident response.

Keep rollback under change window and capture logs for postmortem.

---

## 8. Compatibility Expectations

- Existing monolith path must continue to work.
- New plugins are additive until migration is completed.
- No forced migration for existing members during initial releases.

---

## 9. Operator Communication Plan

Before execution release:

1. Publish migration announcement with exact release tags.
2. Publish known-good config examples per deployment mode.
3. Publish rollback guidance and support channel.
4. Provide canary feedback window before broad rollout.

