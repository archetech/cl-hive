# Phase 6: Strict Companion Dependency Enforcement

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Change cl-hive's companion plugin model from **optional graceful degradation** to **mandatory hard dependency**, so cl-hive refuses to enable fleet functionality without `cl-hive-comms` and `cl-hive-archon`.

**Architecture:** Three tasks: (1) add config-gated enforcement to startup, (2) remove LocalIdentity fallback path, (3) add comprehensive tests for the new failure modes. Each task is independently reviewable.

**Tech Stack:** Python 3, pytest, Core Lightning plugin framework (pyln-client), SQLite

---

## Current State (What Already Exists)

All transport and identity bridges are fully implemented:

| Component | File | Status |
|-----------|------|--------|
| `ExternalCommsTransport` | `modules/nostr_transport.py:55-203` | Production-ready, circuit breaker |
| `InternalNostrTransport` | `modules/nostr_transport.py:206-218` | Already a dead stub (raises RuntimeError) |
| `RemoteArchonIdentity` | `modules/identity_adapter.py:65-127` | Production-ready, circuit breaker |
| `LocalIdentity` | `modules/identity_adapter.py:30-62` | Active fallback (CLN HSM delegation) |
| `_detect_phase6_optional_plugins()` | `cl-hive.py:1538-1589` | Detects both plugins at startup |
| `hive-inject-packet` RPC | `cl-hive.py:~13910` | Receives packets from cl-hive-comms |
| `hive-comms-send-dm` | `cl-hive-comms.py` | Already exposed by comms plugin |
| `hive-comms-publish-event` | `cl-hive-comms.py` | Already exposed by comms plugin |
| `hive-archon-sign-message` | `cl-hive-archon.py` | Already exposed by archon plugin |
| `hive-archon-status` | `cl-hive-archon.py` | Already exposed by archon plugin |

**The only real delta is enforcement policy.** No new bridges, RPCs, or transport code needed.

---

## Design Decisions

### D1: Config-gated, not compile-time removal

A new boolean option `hive-require-companions` (default: `false`) controls enforcement. This preserves backward compatibility for existing deployments and allows operators to opt in to strict mode.

**Rationale:** Existing nodes running cl-hive standalone would break on upgrade. A config flag lets operators migrate at their own pace.

### D2: Keep LocalIdentity as circuit-breaker fallback

`LocalIdentity` is 32 lines that delegate to CLN's HSM — it generates no keys and stores nothing. When `hive-require-companions=true` AND archon is active, `RemoteArchonIdentity` is used. But `LocalIdentity` remains available as the emergency fallback if archon's circuit breaker trips.

**Rationale:** `RemoteArchonIdentity.sign_message()` already returns `""` when the circuit opens (line 78-79). Callers that need a signature get silent failures. Falling through to CLN HSM signing is safer than returning empty signatures.

### D3: BOLT 8 P2P transport is never gated

cl-hive's primary communication is BOLT 8 custom messages via `sendcustommsg` (37 call sites). This works peer-to-peer over existing Lightning connections with no dependency on companion plugins. Only **Nostr transport** (marketplace, global reach, advisor DMs) requires `cl-hive-comms`.

The strict mode gates: Nostr transport, DID credentials, management schemas, marketplace, liquidity marketplace, cashu escrow, and archon-delegated signing. It does NOT gate: gossip, handshake, intents, settlements, contribution tracking, or any other BOLT 8 protocol message.

### D4: Retry-with-backoff for plugin detection

CLN doesn't guarantee plugin initialization order. When `hive-require-companions=true`, the detection function retries up to 3 times with 2-second intervals before declaring failure. This handles the common case where companion plugins are simply slower to initialize.

---

## Task 1: Add Startup Enforcement Gate

Add config option and enforcement logic to `cl-hive.py` initialization.

**Files:**
- Modify: `cl-hive.py` (plugin options + init function)

**Step 1: Register the new config option**

Add to the plugin options block (near line ~170, with the other `plugin.add_option` calls):

```python
plugin.add_option(
    'hive-require-companions',
    'false',
    'Require cl-hive-comms and cl-hive-archon to be active. '
    'When true, cl-hive disables fleet features if either companion is missing.',
    opt_type='bool',
)
```

**Step 2: Add retry-capable detection**

Replace the single-shot detection call with a retry wrapper. Add near the existing `_detect_phase6_optional_plugins` function (~line 1538):

```python
def _detect_required_plugins(plugin_obj: Plugin, max_retries: int = 3, retry_delay: float = 2.0) -> Dict[str, Any]:
    """
    Detect companion plugins with retry for startup race conditions.

    CLN plugins initialize concurrently, so companion plugins may not appear
    in the plugin list immediately. Retries with backoff before giving up.
    """
    for attempt in range(max_retries):
        result = _detect_phase6_optional_plugins(plugin_obj)
        comms_active = result.get("cl_hive_comms", {}).get("active", False)
        archon_active = result.get("cl_hive_archon", {}).get("active", False)

        if comms_active and archon_active:
            return result

        if attempt < max_retries - 1:
            plugin_obj.log(
                f"cl-hive: companion plugins not yet detected (attempt {attempt + 1}/{max_retries}), "
                f"retrying in {retry_delay}s... "
                f"[comms={'active' if comms_active else 'missing'}, "
                f"archon={'active' if archon_active else 'missing'}]",
                level='info'
            )
            shutdown_event.wait(retry_delay)
            if shutdown_event.is_set():
                break

    return result
```

**Step 3: Wire enforcement into init**

In the init function, after the existing detection call (~line 1860), add the enforcement gate:

```python
require_companions = plugin.get_option('hive-require-companions')

if require_companions:
    phase6_optional_plugins = _detect_required_plugins(plugin, max_retries=3, retry_delay=2.0)
else:
    phase6_optional_plugins = _detect_phase6_optional_plugins(plugin)

# ... existing code that reads _comms_active / _archon_active ...

if require_companions and not _companion_stack_active:
    missing = []
    if not _comms_active:
        missing.append("cl-hive-comms")
    if not _archon_active:
        missing.append("cl-hive-archon")
    plugin.log(
        f"cl-hive: CRITICAL — hive-require-companions=true but missing: {', '.join(missing)}. "
        f"Fleet features disabled. Install missing plugins or set hive-require-companions=false.",
        level='error'
    )
    # Set a global flag that RPC commands check to return early
    global companions_required_but_missing
    companions_required_but_missing = True
```

**Step 4: Gate feature initialization**

The existing `_companion_stack_active` flag already gates DID, schemas, cashu, marketplace, and liquidity. Add the new flag to gate the remaining optional features:

```python
# Near line 2429, replace the comms-only check:
if _comms_active:
    nostr_transport = ExternalCommsTransport(plugin=plugin)
    # ... existing code ...
elif require_companions:
    # Don't log the "optional" message when companions are required
    nostr_transport = None
    plugin.log("cl-hive: Nostr transport unavailable (required companion cl-hive-comms missing)", level='error')
else:
    # ... existing fallback logging ...
```

**Step 5: Add global variable declaration**

Near the other global declarations (~line 830):

```python
companions_required_but_missing: bool = False
```

---

## Task 2: Add Identity Fallback Chain

When `hive-require-companions=true` and archon is active, use `RemoteArchonIdentity` as primary with `LocalIdentity` as circuit-breaker fallback.

**Files:**
- Modify: `modules/identity_adapter.py`

**Step 1: Add FallbackIdentity wrapper**

Add after the existing `RemoteArchonIdentity` class:

```python
class FallbackIdentity(IdentityInterface):
    """Tries RemoteArchonIdentity first, falls back to LocalIdentity on circuit open.

    Used when hive-require-companions=true to prefer archon signing but
    maintain availability via CLN HSM when archon is temporarily down.
    """

    def __init__(self, primary: RemoteArchonIdentity, fallback: LocalIdentity, plugin=None):
        self._primary = primary
        self._fallback = fallback
        self._plugin = plugin

    def sign_message(self, message: str) -> str:
        sig = self._primary.sign_message(message)
        if sig:
            return sig
        # Primary failed (circuit open or RPC error) — use local HSM
        if self._plugin:
            self._plugin.log("cl-hive: archon signing failed, falling back to local HSM", level="warn")
        return self._fallback.sign_message(message)

    def check_message(self, message: str, signature: str, pubkey: str = "") -> bool:
        # Always local — doesn't need secrets
        return self._primary.check_message(message, signature, pubkey)

    def get_info(self) -> Dict[str, Any]:
        primary_info = self._primary.get_info()
        return {
            **primary_info,
            "mode": "fallback",
            "primary_mode": "remote",
            "fallback_mode": "local",
        }
```

**Step 2: Wire into init**

In `cl-hive.py`, replace the identity adapter selection (~line 2466-2481):

```python
# Phase 6: Identity adapter
global identity_adapter
try:
    archon_active = phase6_optional_plugins["cl_hive_archon"]["active"]
    if archon_active and require_companions:
        # Strict mode: archon primary with local HSM fallback
        remote = RemoteArchonIdentity(plugin=plugin)
        local = LocalIdentity(rpc=plugin.rpc)
        identity_adapter = FallbackIdentity(primary=remote, fallback=local, plugin=plugin)
        plugin.log("cl-hive: Using Fallback Identity (archon primary, CLN HSM fallback)")
    elif archon_active:
        identity_adapter = RemoteArchonIdentity(plugin=plugin)
        plugin.log("cl-hive: Using Remote Identity (cl-hive-archon)")
    else:
        identity_adapter = LocalIdentity(rpc=plugin.rpc)
        plugin.log(
            "cl-hive: Using Local Identity (CLN HSM); "
            "cl-hive-archon is optional for delegated signing"
        )
except Exception as e:
    identity_adapter = LocalIdentity(rpc=plugin.rpc)
    plugin.log(f"cl-hive: Identity adapter fell back to local CLN HSM signing: {e}", level='warn')
```

---

## Task 3: RPC Guard for Strict Mode

When `companions_required_but_missing=True`, RPC commands that depend on companion features should return clear error messages instead of silently failing.

**Files:**
- Modify: `modules/rpc_commands.py`

**Step 1: Add guard helper**

Add near the top of `rpc_commands.py`:

```python
def _check_companions_available(ctx: HiveContext) -> Optional[Dict[str, Any]]:
    """Returns error dict if companions are required but missing, else None."""
    if getattr(ctx, 'companions_required_but_missing', False):
        return {
            "ok": False,
            "error": "Fleet features disabled: hive-require-companions=true but "
                     "required companion plugins are not active. "
                     "Install cl-hive-comms and cl-hive-archon, or set "
                     "hive-require-companions=false.",
        }
    return None
```

**Step 2: Add `companions_required_but_missing` to HiveContext**

In the HiveContext dataclass:

```python
companions_required_but_missing: bool = False
```

**Step 3: Wire the flag into HiveContext construction**

In `cl-hive.py` where HiveContext is built, pass the global flag:

```python
companions_required_but_missing=companions_required_but_missing,
```

**Step 4: Guard companion-dependent RPCs**

Add the guard at the top of RPCs that require companion features (DID commands, marketplace, archon pass-through commands). Example:

```python
def did_issue_credential(ctx, ...):
    guard = _check_companions_available(ctx)
    if guard:
        return guard
    # ... existing implementation ...
```

Apply to: `hive-did-issue`, `hive-did-list`, `hive-did-revoke`, `hive-did-reputation`, `hive-did-profiles`, `hive-mgmt-credential-issue`, `hive-mgmt-credential-list`, `hive-mgmt-credential-revoke`, `hive-archon-*` pass-throughs, and marketplace commands.

Do NOT guard: `hive-status`, `hive-health`, `hive-members`, `hive-channels`, or any BOLT 8 protocol commands. These should always work.

---

## Task 4: Tests

**Files:**
- Modify or create: `tests/test_strict_companions.py`

**Test 1: Enforcement disabled by default**

```python
def test_default_no_enforcement():
    """hive-require-companions defaults to false — missing plugins don't disable features."""
    # Simulate init with require_companions=False, no companion plugins
    # Assert: companions_required_but_missing == False
    # Assert: LocalIdentity is selected
    # Assert: nostr_transport is None (optional, not error)
```

**Test 2: Enforcement with both plugins present**

```python
def test_strict_mode_both_present():
    """With require-companions=true and both plugins active, full stack initializes."""
    # Mock plugin list returning both cl-hive-comms and cl-hive-archon as active
    # Assert: companions_required_but_missing == False
    # Assert: FallbackIdentity is selected
    # Assert: ExternalCommsTransport is instantiated
```

**Test 3: Enforcement with missing comms**

```python
def test_strict_mode_missing_comms():
    """With require-companions=true and comms missing, fleet features disabled."""
    # Mock plugin list returning only cl-hive-archon
    # Assert: companions_required_but_missing == True
    # Assert: nostr_transport is None
    # Assert: DID/marketplace/cashu managers are None
```

**Test 4: Enforcement with missing archon**

```python
def test_strict_mode_missing_archon():
    """With require-companions=true and archon missing, fleet features disabled."""
    # Mock plugin list returning only cl-hive-comms
    # Assert: companions_required_but_missing == True
```

**Test 5: Retry detection handles slow startup**

```python
def test_retry_detection_finds_late_plugins():
    """Retry loop succeeds when plugins appear on second attempt."""
    # First call to _detect_phase6_optional_plugins returns both inactive
    # Second call returns both active
    # Assert: _detect_required_plugins succeeds on attempt 2
    # Assert: companions_required_but_missing == False
```

**Test 6: FallbackIdentity chain**

```python
def test_fallback_identity_prefers_archon():
    """FallbackIdentity uses archon when available."""
    # Mock archon returning valid signature
    # Assert: FallbackIdentity.sign_message returns archon signature

def test_fallback_identity_uses_local_on_circuit_open():
    """FallbackIdentity falls through to local HSM when archon circuit opens."""
    # Mock archon returning "" (circuit open)
    # Mock local HSM returning valid signature
    # Assert: FallbackIdentity.sign_message returns local signature
```

**Test 7: RPC guard returns error**

```python
def test_rpc_guard_blocks_when_companions_missing():
    """DID RPCs return clear error when companions required but missing."""
    ctx = HiveContext(companions_required_but_missing=True, ...)
    result = did_issue_credential(ctx, ...)
    assert result["ok"] is False
    assert "hive-require-companions" in result["error"]
```

**Test 8: RPC guard passes when companions present**

```python
def test_rpc_guard_passes_when_companions_present():
    """DID RPCs work normally when companions are active."""
    ctx = HiveContext(companions_required_but_missing=False, ...)
    # Assert: no guard error returned
```

---

## Verification Checklist

1. **Default behavior unchanged:** Run `python3 -m pytest tests/` with `hive-require-companions` unset. All existing tests pass. No behavioral changes.
2. **Strict mode + full stack:** Set `hive-require-companions=true`, mock both plugins active. Verify FallbackIdentity selected, ExternalCommsTransport active, all features enabled.
3. **Strict mode + missing plugin:** Set `hive-require-companions=true`, mock one plugin missing. Verify CRITICAL log, `companions_required_but_missing=True`, DID/marketplace RPCs return guard error, BOLT 8 messaging unaffected.
4. **Retry succeeds:** Verify `_detect_required_plugins` finds plugins that appear on retry attempt 2 of 3.
5. **Archon circuit breaker fallback:** Trip the archon circuit breaker. Verify FallbackIdentity falls through to CLN HSM signing without error.
6. **No BOLT 8 impact:** Verify `sendcustommsg`, gossip, handshake, intents, settlements all work regardless of strict mode or companion status.

---

## Migration Guide

### For existing standalone operators

No action needed. `hive-require-companions` defaults to `false`. Upgrade cl-hive and behavior is identical to before.

### To opt into strict mode

1. Install and activate `cl-hive-comms` and `cl-hive-archon` alongside cl-hive
2. Verify all three plugins are active: `lightning-cli plugin list`
3. Add to CLN config: `hive-require-companions=true`
4. Restart CLN
5. Verify: `lightning-cli hive-status` shows `"signing_backend": "cl-hive-archon"` and `"companions_required": true`

### To revert from strict mode

1. Remove `hive-require-companions=true` from CLN config (or set to `false`)
2. Restart CLN
3. cl-hive will fall back to LocalIdentity and operate without companion plugins
