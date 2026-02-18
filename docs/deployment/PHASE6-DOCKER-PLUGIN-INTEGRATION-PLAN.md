# Phase 6 Docker Plugin Integration Plan

**Status:** Planning-only (do not enable until Phase 6 gates pass)  
**Last Updated:** 2026-02-17

---

## 1. Goal

Prepare Docker deployment to support the future 3-plugin stack without changing current production behavior.

Current production behavior remains:
- `cl-hive`
- `cl-revenue-ops`
- existing required dependencies (CLBOSS, Sling, c-lightning-REST)

---

## 2. Non-Goals (Until Unblocked)

- No extraction of runtime code from `cl-hive.py` yet.
- No default enabling of `cl-hive-comms` or `cl-hive-archon`.
- No change to current production startup order.

---

## 3. Planned Container Changes

When Phase 6 starts, update Docker image and entrypoint in this order.

### Step 1: Image support for new repos
- Add build args:
  - `CL_HIVE_COMMS_VERSION`
  - `CL_HIVE_ARCHON_VERSION`
- Clone plugin repos into image:
  - `/opt/cl-hive-comms`
  - `/opt/cl-hive-archon`
- Symlink plugin entrypoints into `/root/.lightning/plugins/`:
  - `cl-hive-comms.py`
  - `cl-hive-archon.py`

### Step 2: Optional enable flags
- Add env flags (default `false` initially):
  - `HIVE_COMMS_ENABLED=false`
  - `HIVE_ARCHON_ENABLED=false`
- Keep `cl-hive` and `cl-revenue-ops` startup unchanged.

### Step 3: Startup order
If flags are enabled, start plugins in strict order:
1. `cl-hive-comms`
2. `cl-hive-archon` (only if comms active)
3. `cl-revenue-ops`
4. `cl-hive`

### Step 4: Health checks
- Extend startup verification to assert enabled plugins appear in `plugin list`.
- Fail fast if `HIVE_ARCHON_ENABLED=true` but `cl-hive-comms` is not active.

---

## 4. Compose and Env Plan

Planned `.env` additions:
- `HIVE_COMMS_ENABLED`
- `HIVE_ARCHON_ENABLED`
- `CL_HIVE_COMMS_VERSION`
- `CL_HIVE_ARCHON_VERSION`

Planned `docker-compose` behavior:
- Defaults keep both new plugins disabled.
- Operator can opt-in per environment.
- Build override can mount local checkouts:
  - `~/bin/cl-hive-comms:/opt/cl-hive-comms:ro`
  - `~/bin/cl-hive-archon:/opt/cl-hive-archon:ro`

---

## 5. Rollout and Rollback

### Canary rollout
1. Build image with new plugin binaries present but disabled.
2. Deploy to one node with defaults.
3. Enable `HIVE_COMMS_ENABLED=true` on canary only.
4. Enable `HIVE_ARCHON_ENABLED=true` only after comms stability.

### Rollback
- Immediate: set new flags back to `false` and restart container.
- If needed: roll back image tag to previous stable release.
- No schema migration expected for this stage; rollback remains low risk.

---

## 6. Validation Checklist

- `docker-compose config` validates with new env vars.
- Container starts clean with both new flags disabled.
- Enabling `HIVE_COMMS_ENABLED` starts only comms plugin.
- Enabling both flags starts comms then archon in order.
- Existing `cl-hive` workflows remain unchanged when flags are disabled.

---

## 7. Change Control

Do not merge Docker enablement PRs until:
- Phase 6 readiness gates in `docs/planning/13-PHASE6-READINESS-GATED-PLAN.md` are green.
- Manual non-docker install document is validated end-to-end.

