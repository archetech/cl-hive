# cl-hive.py Monolith Decomposition

**Date**: 2026-03-07
**Status**: Approved

## Problem

cl-hive.py is 21,260 lines — a monolith containing infrastructure classes,
protocol handlers, 13 background loops, 170+ RPC wrappers, config registration,
and initialization. This makes it hard to navigate, debug, and reason about.

## Architecture

Surgical extraction of implementation logic into 5 new modules. cl-hive.py
retains plugin wiring (init, hooks, dispatch, @plugin.method decorators).
Zero behavioral changes — pure structural refactor.

Follows the existing `rpc_commands.py` pattern: handler functions receive
dependencies as parameters, no global state in extracted modules, one-way
dependency flow (cl-hive.py → modules).

## New Modules

### 1. `modules/plugin_options.py` (~300 lines)

- `register_options(plugin)` — all `plugin.add_option()` calls
- `RateLimiter` class
- Config parsing helpers: `_parse_setconfig_value()`, `_parse_bool()`
- Called from cl-hive.py before `plugin.run()`

### 2. `modules/rpc_pool.py` (~670 lines)

- `RpcLockTimeoutError` exception class
- `RpcPool` class (subprocess-isolated timeout-safe RPC execution)
- `RpcPoolProxy` class (method forwarding wrapper)
- Self-contained — depends only on stdlib (subprocess, threading, queue)
- cl-hive.py creates pool in `init()` and passes to modules

### 3. `modules/log_writer.py` (~250 lines)

- `BatchedLogWriter` class
- Reduces write_lock contention via batched stdout flushing
- Self-contained — wraps plugin.log
- cl-hive.py creates in `init()` and monkey-patches plugin.log

### 4. `modules/protocol_handlers.py` (~3,500 lines)

All `handle_*` functions extracted from cl-hive.py:

| Handler | Protocol Phase |
|---------|---------------|
| `handle_hello`, `handle_challenge`, `handle_attest`, `handle_welcome` | Handshake |
| `handle_gossip`, `handle_state_hash`, `handle_full_sync` | State sync |
| `_apply_membership_sync`, `_create_membership_payload`, `_create_signed_full_sync_msg`, `_create_signed_state_hash_msg` | Membership helpers |
| `validate_and_apply_ban`, `handle_member_left` | Ban/leave |
| Promotion request/vote handlers | Promotion |
| `handle_expansion_nominate`, `handle_expansion_elect`, `handle_expansion_decline` | Expansion |
| `handle_settlement_offer_broadcast`, settlement ACK handlers | Settlement |
| `handle_mcf_needs_broadcast`, `handle_mcf_solution_broadcast`, `handle_mcf_assignment_ack`, `handle_mcf_completion_report` | MCF |
| `handle_peer_available` | Availability |
| `handle_msg_ack`, retransmission logic | Reliable delivery |

Each handler receives dependencies via keyword arguments:

```python
def handle_hello(peer_id, payload, *, db, state_manager, handshake,
                 membership, gossip, plugin_log):
    ...
```

`_dispatch_hive_message()` stays in cl-hive.py but delegates to these functions.

### 5. `modules/background_loops.py` (~1,900 lines)

All 13 `*_loop` daemon thread functions:

| Loop | Interval |
|------|----------|
| `gossip_loop` | 5 min heartbeat |
| `membership_maintenance_loop` | Periodic |
| `planner_loop` | Config-driven |
| `intent_monitor_loop` | Periodic |
| `fee_intelligence_loop` | Config-driven |
| `settlement_loop` | Period-based |
| `mcf_optimization_loop` | Config-driven |
| `outbox_retry_loop` | Exponential backoff |
| `did_maintenance_loop` | Companion-only |
| `escrow_maintenance_loop` | Companion-only |
| `marketplace_maintenance_loop` | Companion-only |
| `liquidity_maintenance_loop` | Companion-only |

Each loop receives dependencies via keyword arguments:

```python
def gossip_loop(*, shutdown_event, db, state_manager, gossip,
                config, plugin_log, submit_message_fn):
    while not shutdown_event.is_set():
        ...
```

cl-hive.py spawns threads that call these functions with bound kwargs.

## What Stays in cl-hive.py (~8-9K lines)

- Imports + plugin object creation
- `init()` function (module wiring + thread spawning)
- Hook handlers: `peer_connected`, `custmsg`, `connect`, `disconnect`, `forward_event`
- `_dispatch_hive_message()` — routing only, delegates to protocol_handlers
- 170+ `@plugin.method` thin wrappers (required for pyln-client registration)
- `_submit_hive_message()` and message submission helpers
- `__main__` entry point

## Protocol Cleanup (Minor)

- Remove dead `INTENT_ACK` message type (0 callers, 0 handlers)
- Add missing `BAN` message receive handler (currently send-only)

## Migration Order

Each extraction is independently committable and bisect-friendly:

1. `plugin_options.py` — lowest risk, pure config registration
2. `rpc_pool.py` — self-contained, no external dependencies
3. `log_writer.py` — self-contained, no external dependencies
4. `protocol_handlers.py` — largest extraction, most dependency wiring
5. `background_loops.py` — final extraction, depends on patterns from step 4

## Testing

- All 1,340+ existing tests must pass unchanged after each commit
- No new tests needed — behavior is identical
- Each extraction verified by full test suite run

## Risk

Low. Pure structural refactor with no behavioral changes. Each step is
independently reversible via `git revert`.

## Estimated Impact

| Metric | Before | After |
|--------|--------|-------|
| cl-hive.py lines | 21,260 | ~8,600 |
| New module files | 0 | 5 |
| Total module count | 47 | 52 |
| Behavioral changes | — | Zero |
