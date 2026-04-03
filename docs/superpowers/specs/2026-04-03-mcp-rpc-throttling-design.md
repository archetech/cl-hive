# MCP RPC Throttling — Universal Semaphore Enforcement

**Date:** 2026-04-03
**Status:** Approved
**Scope:** cl-hive MCP server (`tools/mcp-hive-server.py`) and compat launcher (`cl_revenue_ops/tools/hive_mcp_compat.py`)

## Problem

The MCP hive server fires RPC calls to Core Lightning without effective concurrency control. A per-node semaphore (max 5 concurrent) exists in `HiveFleet` but is only enforced in `call_all()`. The remaining 17+ handler functions call `node.call()` directly and bypass the semaphore entirely. Fleet-wide endpoints and advisor cycles can blast 30+ concurrent RPCs at the lightning daemon, causing timeouts that cascade into stale cached state (e.g. the `no_channel_balance_data` issue fixed earlier today).

## Solution

Move semaphore enforcement into `NodeConnection.call()` so every RPC from every codepath is automatically throttled to 5 concurrent per node.

## Changes

### 1. `NodeConnection` — add semaphore field

Add an optional `_semaphore: Optional[asyncio.Semaphore]` field to the dataclass, defaulting to `None`. When present, `call()` acquires it before dispatching the RPC.

```python
@dataclass
class NodeConnection:
    name: str
    rest_url: str = ""
    rune: str = ""
    ca_cert: Optional[str] = None
    client: Optional[httpx.AsyncClient] = None
    docker_container: Optional[str] = None
    lightning_dir: str = "/home/clightning/.lightning"
    network: str = "regtest"
    omit_network_flag: bool = False
    _semaphore: Optional[asyncio.Semaphore] = None
```

### 2. `NodeConnection.call()` — wrap dispatch in semaphore

The method already handles REST and Docker modes. Wrap the dispatch (after the allowlist check) in `async with self._semaphore:` when the semaphore is present:

```python
async def call(self, method: str, params: Dict = None) -> Dict:
    if not _check_method_allowed(method):
        return {"error": f"Method '{method}' not in allowlist"}

    if self._semaphore:
        async with self._semaphore:
            return await self._call_dispatch(method, params)
    return await self._call_dispatch(method, params)
```

Extract the current REST/Docker dispatch logic into a private `_call_dispatch()` method to keep `call()` clean.

### 3. `HiveFleet.load_config()` — inject semaphore into NodeConnection

Pass the semaphore at construction instead of storing it in `_node_semaphores`:

```python
sem = asyncio.Semaphore(self._max_concurrent_per_node)
node = NodeConnection(name=..., ..., _semaphore=sem)
self.nodes[node.name] = node
```

Remove `self._node_semaphores` dict — it's no longer needed.

### 4. `HiveFleet.call_all()` — drop redundant semaphore wrapping

The `call_with_timeout` closure currently acquires the semaphore before calling `node.call()`. Since `call()` now handles this internally, `call_all()` simplifies to just the `asyncio.wait_for` timeout wrapper:

```python
async def call_all(self, method, params=None, timeout=30.0):
    async def call_with_timeout(name, node):
        try:
            result = await asyncio.wait_for(node.call(method, params), timeout=timeout)
            return (name, result)
        except asyncio.TimeoutError:
            return (name, {"error": f"Timeout after {timeout}s"})
        except Exception as e:
            return (name, {"error": str(e) or f"{type(e).__name__} calling {method}"})

    tasks = [call_with_timeout(n, nd) for n, nd in self.nodes.items()]
    return dict(await asyncio.gather(*tasks))
```

### 5. Source file restoration

Restore `tools/mcp-hive-server.py` from the pinned git revision (`15ba8b2e`) into `cl-hive/tools/`, apply the changes above, and update `hive_mcp_compat.py` to point `DEFAULT_SOURCE_REV` at the new commit containing the restored and patched file.

## What does NOT change

- Handler code — no modifications to any of the 17+ tool handlers
- Concurrency limit — stays at 5 per node
- httpx client configuration — no pool tuning
- Config surface — no new env vars or options
- `_call_docker()` — internal dispatch unchanged

## Behavior

Before: A tool call like `handle_hive_node_diagnostic` fires 4 parallel RPCs (`getinfo`, `listpeerchannels`, `listforwards`, `revenue-status`) that all hit lightningd simultaneously. If another tool call arrives concurrently, that's 8 RPCs at once, unbounded.

After: The same 4 RPCs still fire concurrently via `asyncio.gather`, but the semaphore gates them. If 5 are already in flight, the 6th waits until one completes. Handlers don't notice — they still use `asyncio.gather` and get their results when all complete. The only observable effect is that under heavy load, individual RPCs may wait slightly longer, but the node won't be overwhelmed.

## Testing

- Start the MCP server, invoke `revenue_status` and `hive_status` concurrently — verify both return without timeout
- Run cl-hive test suite — no regressions expected since tests don't exercise the MCP server directly
- Manual: watch lightning daemon logs under load to confirm RPC calls are no longer stacking
