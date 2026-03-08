# cl-hive.py Monolith Decomposition Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extract ~12,600 lines from the 21,260-line cl-hive.py monolith into 5 focused modules with zero behavioral changes.

**Architecture:** Move implementation logic into new modules using a `globals().update(deps)` injection pattern. Handler and loop functions are moved verbatim — no code changes to function bodies. cl-hive.py calls `init_*()` during startup to inject dependency references into each module's namespace, so moved functions reference the same variable names they always did.

**Tech Stack:** Python 3.10+, pyln-client, pytest

**Design doc:** `docs/plans/2026-03-07-cl-hive-monolith-decomposition-design.md`

---

### Task 1: Extract plugin_options.py

**Files:**
- Create: `modules/plugin_options.py`
- Modify: `cl-hive.py`

**What moves:**
- `_parse_bool()` function (lines 1098-1104)
- `RateLimiter` class (lines 961-1095)
- All `plugin.add_option()` calls (lines 1230-1469)
- `OPTION_TO_CONFIG_MAP` dict (lines 1485-1512)
- `VPN_OPTIONS` set (lines 1515-1521)
- `_parse_setconfig_value()` function (lines 1524-1535)

**Step 1: Create modules/plugin_options.py**

```python
"""
Plugin option registration and config parsing for cl-hive.

Extracted from cl-hive.py to reduce monolith size.
"""

import time
from typing import Any


class RateLimiter:
    # ... MOVE ENTIRE CLASS FROM cl-hive.py lines 961-1095 ...
    pass


def _parse_bool(value: Any, default: bool = False) -> bool:
    # ... MOVE FROM cl-hive.py lines 1098-1104 ...
    pass


def _parse_setconfig_value(value: Any, target_type: type) -> Any:
    # ... MOVE FROM cl-hive.py lines 1524-1535 ...
    pass


# Mapping from plugin option names to HiveConfig attribute names
OPTION_TO_CONFIG_MAP = {
    # ... MOVE FROM cl-hive.py lines 1485-1512 ...
}

VPN_OPTIONS = {
    # ... MOVE FROM cl-hive.py lines 1515-1521 ...
}


def register_options(plugin):
    """Register all hive-* plugin options. Call before plugin.run()."""
    # ... MOVE ALL plugin.add_option() CALLS FROM cl-hive.py lines 1230-1469 ...
    pass
```

**Step 2: Update cl-hive.py**

Replace the moved code with imports:

```python
from modules.plugin_options import (
    RateLimiter, _parse_bool, _parse_setconfig_value,
    OPTION_TO_CONFIG_MAP, VPN_OPTIONS, register_options,
)

# Before plugin.run() at the bottom of the file:
register_options(plugin)
```

Remove the original function/class definitions and option registration block from cl-hive.py.

**Step 3: Run tests**

```bash
cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q
```

Expected: All tests pass (no behavioral change).

**Step 4: Commit**

```bash
git add modules/plugin_options.py cl-hive.py
git commit -m "refactor: extract plugin_options.py from cl-hive.py monolith

Move RateLimiter, _parse_bool, _parse_setconfig_value, option
registration, OPTION_TO_CONFIG_MAP, and VPN_OPTIONS to new module.
~600 lines extracted. Zero behavioral changes."
```

---

### Task 2: Extract rpc_pool.py

**Files:**
- Create: `modules/rpc_pool.py`
- Modify: `cl-hive.py`

**What moves:**
- `RpcLockTimeoutError` class (lines 280-290)
- `RpcPool` class (lines 300-636)
- `RpcPoolProxy` class (lines 639-713)

**Step 1: Create modules/rpc_pool.py**

```python
"""
RPC Pool with subprocess isolation for timeout-safe RPC execution.

Extracted from cl-hive.py to reduce monolith size.
"""

import json
import multiprocessing
import os
import queue
import sys
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional


class RpcLockTimeoutError(TimeoutError):
    # ... MOVE FROM cl-hive.py lines 280-290 ...
    pass


class RpcPool:
    # ... MOVE ENTIRE CLASS FROM cl-hive.py lines 300-636 ...
    pass


class RpcPoolProxy:
    # ... MOVE ENTIRE CLASS FROM cl-hive.py lines 639-713 ...
    pass
```

**Step 2: Update cl-hive.py**

```python
from modules.rpc_pool import RpcLockTimeoutError, RpcPool, RpcPoolProxy
```

Remove original class definitions. Keep the global `_rpc_pool` variable and its initialization in `init()`.

**Step 3: Run tests**

```bash
cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q
```

**Step 4: Commit**

```bash
git add modules/rpc_pool.py cl-hive.py
git commit -m "refactor: extract rpc_pool.py from cl-hive.py monolith

Move RpcLockTimeoutError, RpcPool, RpcPoolProxy to new module.
~440 lines extracted. Zero behavioral changes."
```

---

### Task 3: Extract log_writer.py

**Files:**
- Create: `modules/log_writer.py`
- Modify: `cl-hive.py`

**What moves:**
- `BatchedLogWriter` class (lines 727-805)

**Step 1: Create modules/log_writer.py**

```python
"""
Batched log writer to reduce write_lock contention.

Extracted from cl-hive.py to reduce monolith size.
"""

import queue
import threading
import time
from typing import Optional


class BatchedLogWriter:
    # ... MOVE ENTIRE CLASS FROM cl-hive.py lines 727-805 ...
    pass
```

**Step 2: Update cl-hive.py**

```python
from modules.log_writer import BatchedLogWriter
```

Remove original class definition. Keep instantiation in `init()`.

**Step 3: Run tests**

```bash
cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q
```

**Step 4: Commit**

```bash
git add modules/log_writer.py cl-hive.py
git commit -m "refactor: extract log_writer.py from cl-hive.py monolith

Move BatchedLogWriter to new module. ~80 lines extracted.
Zero behavioral changes."
```

---

### Task 4: Extract protocol_handlers.py

This is the largest extraction (~72 handler functions, ~8,000+ lines).

**Files:**
- Create: `modules/protocol_handlers.py`
- Modify: `cl-hive.py`

**Key pattern — dependency injection via globals().update():**

Handler functions currently access module-level globals in cl-hive.py (e.g. `database`, `membership_mgr`, `plugin`). To avoid rewriting every function body, we inject the same variable names into the new module's namespace.

**Step 1: Create modules/protocol_handlers.py**

```python
"""
Protocol message handlers for cl-hive.

All handle_* functions and their helpers, extracted from cl-hive.py.
Dependencies are injected via init_protocol_handlers() which populates
this module's globals — so handler code references the same variable
names it always did (database, membership_mgr, plugin, etc.).
"""

import json
import time
import threading
import traceback
from typing import Any, Dict, List, Optional, Tuple


def init_protocol_handlers(deps: dict):
    """Inject dependency references into this module's namespace.

    Called once from cl-hive.py init() after all managers are created.
    Handler functions then reference e.g. `database`, `membership_mgr`
    as module-level names — same as when they lived in cl-hive.py.
    """
    globals().update(deps)


# --- Handshake handlers ---

def handle_hello(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 2919-3004 ...
    pass

def handle_challenge(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 3007-3044 ...
    pass

def handle_attest(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 3047-3223 ...
    pass

def handle_welcome(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 3226-3319 ...
    pass

# --- State sync handlers ---

def handle_gossip(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 3321-3418 ...
    pass

def handle_state_hash(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 3421-3496 ...
    pass

def handle_full_sync(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 3499-3587 ...
    pass

# --- Membership helpers ---

def _apply_membership_sync(members_list, sender_id, plugin):
    # ... MOVE FROM cl-hive.py lines 3590-3681 ...
    pass

def _create_membership_payload():
    # ... MOVE FROM cl-hive.py lines 3684-3717 ...
    pass

def _create_signed_full_sync_msg():
    # ... MOVE FROM cl-hive.py lines 3720-3750 ...
    pass

def _create_signed_state_hash_msg():
    # ... MOVE FROM cl-hive.py lines 3753-3782 ...
    pass

def _create_signed_gossip_msg(capacity_sats, available_sats, fee_policy, topology, prev_hash):
    # ... MOVE FROM cl-hive.py lines 3860-3908 ...
    pass

def _get_our_addresses():
    # ... MOVE FROM cl-hive.py lines 3785-3806 ...
    pass

# --- Peer lifecycle helpers ---

def _handle_peer_connected(peer_id, member):
    # ... MOVE FROM cl-hive.py lines 3971-4008 ...
    pass

def _handle_forward_event(forward_event):
    # ... MOVE FROM cl-hive.py lines 4055-4106 ...
    pass

# --- Intent handlers ---

def handle_intent(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 4461-4547 ...
    pass

def handle_intent_abort(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 4549-4950 ...
    pass

# --- MSG ACK handler ---

def handle_msg_ack(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 4952-4988 ...
    pass

# --- DID credential handlers ---

def handle_did_credential_present(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 4990-5063 ...
    pass

def handle_did_credential_revoke(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 5066-5139 ...
    pass

def handle_mgmt_credential_present(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 5142-5215 ...
    pass

def handle_mgmt_credential_revoke(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 5218-5291 ...
    pass

# --- Settlement handlers ---

def handle_settlement_receipt(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 5461-5520 ...
    pass

def handle_bond_posting(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 5523-5553 ...
    pass

def handle_bond_slash(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 5556-5696 ...
    pass

def handle_netting_proposal(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 5699-5745 ...
    pass

def handle_netting_ack(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 5748-5794 ...
    pass

def handle_violation_report(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 5797-5833 ...
    pass

def handle_arbitration_vote(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 5836-5890 ...
    pass

# --- Promotion/Vouch handlers ---

def handle_promotion_request(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 6470-6565 ...
    pass

def handle_vouch(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 6567-6708 ...
    pass

def handle_promotion(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 6710-6818 ...
    pass

# --- Membership change handlers ---

def handle_member_left(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 6820-6920 ...
    pass

def handle_ban_proposal(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 6923-7033 ...
    pass

def handle_ban_vote(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 7036-7250 ...
    pass

# --- Peer availability ---

def handle_peer_available(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 7253-7497 ...
    pass

# --- Expansion handlers ---

def handle_expansion_nominate(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 8012-8089 ...
    pass

def handle_expansion_elect(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 8092-8212 ...
    pass

def handle_expansion_decline(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 8215-8351 ...
    pass

# --- Fee intelligence handlers ---

def handle_fee_intelligence_snapshot(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 8354-8426 ...
    pass

def handle_health_report(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 8429-8496 ...
    pass

def handle_liquidity_need(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 8499-8565 ...
    pass

def handle_liquidity_snapshot(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 8568-8636 ...
    pass

def handle_route_probe(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 8639-8707 ...
    pass

def handle_route_probe_batch(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 8710-8780 ...
    pass

def handle_peer_reputation_snapshot(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 8783-8850 ...
    pass

# --- Stigmergic/pheromone handlers ---

def handle_stigmergic_marker_batch(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 8853-8950 ...
    pass

def handle_pheromone_batch(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 8953-9045 ...
    pass

# --- Fleet intelligence handlers ---

def handle_yield_metrics_batch(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 9048-9136 ...
    pass

def handle_circular_flow_alert(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 9139-9221 ...
    pass

def handle_temporal_pattern_batch(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 9224-9316 ...
    pass

# --- Strategic positioning handlers ---

def handle_corridor_value_batch(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 9319-9406 ...
    pass

def handle_positioning_proposal(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 9409-9488 ...
    pass

def handle_physarum_recommendation(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 9491-9572 ...
    pass

def handle_coverage_analysis_batch(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 9575-9662 ...
    pass

def handle_close_proposal(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 9665-9727 ...
    pass

# --- Settlement offer handlers ---

def handle_settlement_offer(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 9730-9797 ...
    pass

def handle_fee_report(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 9800-9935 ...
    pass

def handle_settlement_propose(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 9938-10072 ...
    pass

def handle_settlement_ready(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 10075-10185 ...
    pass

def handle_settlement_executed(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 10188-10304 ...
    pass

# --- Task handlers ---

def handle_task_request(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 10307-10376 ...
    pass

def handle_task_response(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 10379-10451 ...
    pass

# --- Splice handlers ---

def handle_splice_init_request(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 10454-10521 ...
    pass

def handle_splice_init_response(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 10524-10593 ...
    pass

def handle_splice_update(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 10596-10655 ...
    pass

def handle_splice_signed(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 10657-10720 ...
    pass

def handle_splice_abort(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 10723-10786 ...
    pass

# --- MCF handlers ---

def handle_mcf_needs_batch(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 10789-10866 ...
    pass

def handle_mcf_solution_broadcast(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 10869-10960 ...
    pass

def handle_mcf_assignment_ack(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 10963-11026 ...
    pass

def handle_mcf_completion_report(peer_id, payload, plugin):
    # ... MOVE FROM cl-hive.py lines 11029-11260 ...
    pass
```

**Step 2: Update cl-hive.py init()**

After all managers are created in `init()`, inject dependencies:

```python
from modules import protocol_handlers

# In init(), after all managers and globals are set up:
protocol_handlers.init_protocol_handlers({
    'plugin': plugin,
    'database': database,
    'state_mgr': state_mgr,
    'membership_mgr': membership_mgr,
    'handshake_mgr': handshake_mgr,
    'gossip_mgr': gossip_mgr,
    'intent_mgr': intent_mgr,
    'bridge': bridge,
    'fee_intel_mgr': fee_intel_mgr,
    'fee_coord_mgr': fee_coord_mgr,
    'settlement_mgr': settlement_mgr,
    'mcf_solver': mcf_solver,
    'liquidity_coord': liquidity_coord,
    'anticipatory_mgr': anticipatory_mgr,
    'cost_reducer': cost_reducer,
    'routing_intel_mgr': routing_intel_mgr,
    'planner': planner,
    'cooperative_expansion': cooperative_expansion,
    'channel_rationalization_mgr': channel_rationalization_mgr,
    'strategic_positioning_mgr': strategic_positioning_mgr,
    'quality_scorer': quality_scorer,
    'health_aggregator': health_aggregator,
    'peer_reputation_mgr': peer_reputation_mgr,
    'contribution_mgr': contribution_mgr,
    'yield_metrics_mgr': yield_metrics_mgr,
    'splice_mgr': splice_mgr,
    'splice_coordinator': splice_coordinator,
    'outbox_mgr': outbox_mgr,
    'task_mgr': task_mgr,
    'relay': relay,
    'governance': governance,
    'budget_mgr': budget_mgr,
    'network_metrics': network_metrics,
    'vpn_transport': vpn_transport,
    'config': config,
    'shutdown_event': shutdown_event,
    '_submit_hive_message': _submit_hive_message,
    # Optional companion managers (may be None):
    'did_credential_mgr': did_credential_mgr,
    'management_schemas_mgr': management_schemas_mgr,
    'cashu_escrow_mgr': cashu_escrow_mgr,
    'marketplace_mgr': marketplace_mgr,
    'liquidity_marketplace_mgr': liquidity_marketplace_mgr,
    'nostr_transport': nostr_transport,
    'identity_adapter': identity_adapter,
    # Infrastructure:
    '_rpc_pool': _rpc_pool,
    '_thread_pool': _thread_pool,
})
```

**Step 3: Update _dispatch_hive_message()**

Change dispatch to call `protocol_handlers.handle_*` instead of local functions:

```python
def _dispatch_hive_message(peer_id, msg_type, payload):
    from modules import protocol_handlers as ph
    handlers = {
        HiveMessageType.HELLO: ph.handle_hello,
        HiveMessageType.CHALLENGE: ph.handle_challenge,
        HiveMessageType.ATTEST: ph.handle_attest,
        # ... all message type → handler mappings ...
    }
    handler = handlers.get(msg_type)
    if handler:
        return handler(peer_id, payload, plugin)
```

**Important:** Identify ALL global variable names referenced by handlers. The implementer MUST:
1. Read each handler function being moved
2. Note every module-level variable it references
3. Ensure that variable is included in the deps dict passed to `init_protocol_handlers()`

If a handler references a variable not in the deps dict, it will raise `NameError` at runtime.

**Step 4: Run tests**

```bash
cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q
```

Expected: All tests pass.

**Step 5: Commit**

```bash
git add modules/protocol_handlers.py cl-hive.py
git commit -m "refactor: extract protocol_handlers.py from cl-hive.py monolith

Move 72 handle_* functions and helpers to new module. Dependencies
injected via init_protocol_handlers(). ~8,000 lines extracted.
Zero behavioral changes."
```

---

### Task 5: Extract background_loops.py

**Files:**
- Create: `modules/background_loops.py`
- Modify: `cl-hive.py`

**What moves:** All 12 `*_loop` functions.

**Step 1: Create modules/background_loops.py**

Same `globals().update(deps)` pattern as protocol_handlers:

```python
"""
Background loop functions for cl-hive daemon threads.

Extracted from cl-hive.py to reduce monolith size.
Dependencies are injected via init_background_loops().
"""

import time
import threading
import traceback
from typing import Any, Dict, Optional


def init_background_loops(deps: dict):
    """Inject dependency references into this module's namespace."""
    globals().update(deps)


# --- Core protocol loops ---

def gossip_loop():
    # ... MOVE FROM cl-hive.py lines 12511-12629 ...
    pass

def membership_maintenance_loop():
    # ... MOVE FROM cl-hive.py lines 11382-11522 ...
    pass

def planner_loop():
    # ... MOVE FROM cl-hive.py lines 11525-11611 ...
    pass

def intent_monitor_loop():
    # ... MOVE FROM cl-hive.py lines 11263-11285 ...
    pass

# --- Fee & intelligence loops ---

def fee_intelligence_loop():
    # ... MOVE FROM cl-hive.py lines 11614-11951 ...
    pass

# --- Settlement & MCF loops ---

def settlement_loop():
    # ... MOVE FROM cl-hive.py lines 11954-12345 ...
    pass

def mcf_optimization_loop():
    # ... MOVE FROM cl-hive.py lines 12631-12684 ...
    pass

# --- Maintenance loops ---

def outbox_retry_loop():
    # ... MOVE FROM cl-hive.py lines 5962-5988 ...
    pass

def did_maintenance_loop():
    # ... MOVE FROM cl-hive.py lines 5294-5337 ...
    pass

def escrow_maintenance_loop():
    # ... MOVE FROM cl-hive.py lines 5893-5919 ...
    pass

def marketplace_maintenance_loop():
    # ... MOVE FROM cl-hive.py lines 5922-5939 ...
    pass

def liquidity_maintenance_loop():
    # ... MOVE FROM cl-hive.py lines 5942-5959 ...
    pass
```

**Step 2: Update cl-hive.py init()**

```python
from modules import background_loops

# In init(), after protocol_handlers init:
background_loops.init_background_loops({
    # Same deps dict as protocol_handlers, plus any loop-specific refs
    'plugin': plugin,
    'database': database,
    'config': config,
    'shutdown_event': shutdown_event,
    'gossip_mgr': gossip_mgr,
    'membership_mgr': membership_mgr,
    'planner': planner,
    'intent_mgr': intent_mgr,
    'fee_intel_mgr': fee_intel_mgr,
    'settlement_mgr': settlement_mgr,
    'mcf_solver': mcf_solver,
    'outbox_mgr': outbox_mgr,
    'did_credential_mgr': did_credential_mgr,
    'cashu_escrow_mgr': cashu_escrow_mgr,
    'marketplace_mgr': marketplace_mgr,
    'liquidity_marketplace_mgr': liquidity_marketplace_mgr,
    # ... all globals referenced by loop functions ...
})

# Update thread spawning to use new module:
threading.Thread(target=background_loops.gossip_loop, daemon=True).start()
threading.Thread(target=background_loops.membership_maintenance_loop, daemon=True).start()
# ... etc for all 12 loops ...
```

**Step 3: Run tests**

```bash
cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q
```

**Step 4: Commit**

```bash
git add modules/background_loops.py cl-hive.py
git commit -m "refactor: extract background_loops.py from cl-hive.py monolith

Move 12 *_loop daemon thread functions to new module. Dependencies
injected via init_background_loops(). ~2,500 lines extracted.
Zero behavioral changes."
```

---

### Task 6: Protocol cleanup

**Files:**
- Modify: `modules/protocol.py`
- Modify: `modules/protocol_handlers.py` (or `cl-hive.py` if BAN handler stays)

**Step 1: Remove dead INTENT_ACK message type**

In `modules/protocol.py`, remove `INTENT_ACK = 32785` from the `HiveMessageType` enum.
Grep the entire codebase first to confirm zero references:

```bash
cd /home/sat/bin/cl-hive && grep -r "INTENT_ACK" --include="*.py" .
```

Expected: Only the enum definition in protocol.py.

**Step 2: Add BAN message receive handler**

The BAN message (sent at line 7241) has no receive handler in dispatch. Either:
- Add a `handle_ban()` handler that processes incoming ban notifications, OR
- Add a comment in dispatch documenting why BAN is intentionally send-only

Investigate the BAN send path to determine the correct approach. If BAN is a broadcast-only notification (informational, no action needed on receive), document it. If peers should act on it, implement the handler.

**Step 3: Run tests**

```bash
cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q
```

**Step 4: Commit**

```bash
git add modules/protocol.py modules/protocol_handlers.py
git commit -m "fix: remove dead INTENT_ACK message type, resolve BAN handler gap"
```

---

### Task 7: Final verification

**Step 1: Run full test suite**

```bash
cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -v 2>&1 | tail -20
```

All 1,340+ tests must pass.

**Step 2: Verify line count reduction**

```bash
wc -l cl-hive.py
```

Expected: ~8,000-9,000 lines (down from 21,260).

**Step 3: Verify new modules exist**

```bash
wc -l modules/plugin_options.py modules/rpc_pool.py modules/log_writer.py modules/protocol_handlers.py modules/background_loops.py
```

**Step 4: Commit summary (if any final fixups needed)**

```bash
git log --oneline -7
```

Should show 5-6 clean commits, one per extraction + cleanup.

---

## Implementation Notes

### The globals().update() Pattern

This is the safest refactoring approach for extracting functions from a monolith:
- **Zero code changes** in moved function bodies
- **Same variable names** — just in a different module's namespace
- **Fully reversible** — move functions back and remove the init call
- **Well-known pattern** for large Python application decomposition

The tradeoff is implicit dependencies (not visible in function signatures). This is acceptable for a first-pass extraction. A future cleanup could add explicit parameters if desired.

### Critical Risk: Missing Dependencies

The `init_*_handlers(deps)` dict MUST include every global variable that any moved function references. If a variable is missing, the function will raise `NameError` at runtime — but **only when that specific code path executes**, which may not be covered by tests.

**Mitigation:** Before committing each extraction:
1. Grep the moved code for all bare name references
2. Cross-reference against the deps dict
3. Add any missing entries

### pyln-client Constraint

All `@plugin.method()` and `@plugin.hook()` decorators MUST remain in cl-hive.py. These decorators register handlers with the Plugin object at import time, before `plugin.run()` is called. They cannot be moved to other modules without changing the registration pattern.
