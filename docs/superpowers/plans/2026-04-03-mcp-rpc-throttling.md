# MCP RPC Throttling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce the existing per-node semaphore (max 5 concurrent RPCs) universally by moving it into `NodeConnection.call()`, so all 17+ MCP handler codepaths are throttled without any handler changes.

**Architecture:** The semaphore moves from `HiveFleet._node_semaphores` (only used in `call_all()`) into `NodeConnection._semaphore` (used in every `call()`). The dispatch logic inside `call()` is extracted to `_call_dispatch()`, and `call()` wraps it with the semaphore. `HiveFleet` injects the semaphore at node construction time and `call_all()` drops its redundant wrapping.

**Tech Stack:** Python 3.12+, asyncio, httpx, dataclasses

**Spec:** `docs/superpowers/specs/2026-04-03-mcp-rpc-throttling-design.md`

---

### Task 1: Restore source files to `cl-hive/tools/`

**Files:**
- Create: `tools/mcp-hive-server.py` (restored from pinned rev `15ba8b2e`)
- Create: `tools/advisor_db.py` (restored from pinned rev `15ba8b2e`)

- [ ] **Step 1: Restore mcp-hive-server.py from pinned revision**

```bash
cd /home/sat/bin/cl-hive
git show 15ba8b2e96d08d39cf2226ff84c62524d6ba4245:tools/mcp-hive-server.py > tools/mcp-hive-server.py
```

- [ ] **Step 2: Restore advisor_db.py from pinned revision**

```bash
git show 15ba8b2e96d08d39cf2226ff84c62524d6ba4245:tools/advisor_db.py > tools/advisor_db.py
```

- [ ] **Step 3: Verify both files are present and parseable**

```bash
python3 -c "import ast; ast.parse(open('tools/mcp-hive-server.py').read()); print('mcp-hive-server.py: OK')"
python3 -c "import ast; ast.parse(open('tools/advisor_db.py').read()); print('advisor_db.py: OK')"
```

Expected: Both print OK.

- [ ] **Step 4: Commit restoration**

```bash
git add tools/mcp-hive-server.py tools/advisor_db.py
git commit -m "restore: MCP server source files from pinned revision 15ba8b2e"
```

---

### Task 2: Add semaphore field to `NodeConnection` and extract `_call_dispatch()`

**Files:**
- Modify: `tools/mcp-hive-server.py:58` (import line)
- Modify: `tools/mcp-hive-server.py:313-406` (NodeConnection class)

- [ ] **Step 1: Add `field` to the dataclass import**

In `tools/mcp-hive-server.py`, line 58, change:

```python
from dataclasses import dataclass
```

to:

```python
from dataclasses import dataclass, field
```

- [ ] **Step 2: Add `_semaphore` field to `NodeConnection` dataclass**

After the `omit_network_flag` field (line 326 in the original), add:

```python
    _semaphore: Optional[asyncio.Semaphore] = field(default=None, repr=False)
```

The full field list becomes:

```python
@dataclass
class NodeConnection:
    """Connection to a CLN node via REST API or Docker exec (for Polar)."""
    name: str
    rest_url: str = ""
    rune: str = ""
    ca_cert: Optional[str] = None
    client: Optional[httpx.AsyncClient] = None
    # Polar/Docker mode
    docker_container: Optional[str] = None
    lightning_dir: str = "/home/clightning/.lightning"
    network: str = "regtest"
    omit_network_flag: bool = False
    _semaphore: Optional[asyncio.Semaphore] = field(default=None, repr=False)
```

- [ ] **Step 3: Extract dispatch logic into `_call_dispatch()`**

Replace the existing `call()` method with two methods. The current REST/Docker dispatch logic moves to `_call_dispatch()`, and `call()` wraps it with the semaphore:

```python
    async def call(self, method: str, params: Dict = None) -> Dict:
        """Call a CLN RPC method via REST or docker exec.

        When a semaphore is attached, at most N calls proceed concurrently
        per node — all others queue transparently.
        """
        if not _check_method_allowed(method):
            return {"error": f"Method '{method}' not in allowlist"}

        if self._semaphore:
            async with self._semaphore:
                return await self._call_dispatch(method, params)
        return await self._call_dispatch(method, params)

    async def _call_dispatch(self, method: str, params: Dict = None) -> Dict:
        """Internal dispatch — REST or Docker exec."""
        # Docker exec mode (for Polar)
        if self.docker_container:
            return await self._call_docker(method, params)

        # REST mode
        if not self.client:
            await self.connect()

        try:
            response = await self.client.post(
                f"/v1/{method}",
                json=params or {}
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            body = {}
            try:
                body = e.response.json()
            except Exception:
                body = {"error": e.response.text.strip()} if e.response.text else {}
            error_msg = (
                body.get("message")
                or body.get("error")
                or str(e)
                or f"HTTP {e.response.status_code} from {self.name}"
            )
            logger.error(f"RPC error on {self.name}: {error_msg}")
            return {"error": error_msg, "details": body}
        except httpx.HTTPError as e:
            error_msg = str(e) or f"{type(e).__name__} connecting to {self.name}"
            logger.error(f"RPC error on {self.name}: {error_msg}")
            return {"error": error_msg}
```

- [ ] **Step 4: Verify file still parses**

```bash
python3 -c "import ast; ast.parse(open('tools/mcp-hive-server.py').read()); print('OK')"
```

Expected: OK

- [ ] **Step 5: Commit**

```bash
git add tools/mcp-hive-server.py
git commit -m "feat: add semaphore field to NodeConnection and extract _call_dispatch"
```

---

### Task 3: Inject semaphore from `HiveFleet` and simplify `call_all()`

**Files:**
- Modify: `tools/mcp-hive-server.py:464-567` (HiveFleet class)

- [ ] **Step 1: Update `HiveFleet.__init__` — remove `_node_semaphores` dict**

Replace:

```python
    def __init__(self):
        self.nodes: Dict[str, NodeConnection] = {}
        self._node_semaphores: Dict[str, asyncio.Semaphore] = {}
        self._max_concurrent_per_node = 5
```

With:

```python
    def __init__(self):
        self.nodes: Dict[str, NodeConnection] = {}
        self._max_concurrent_per_node = 5
```

- [ ] **Step 2: Update `load_config()` — inject semaphore into NodeConnection**

In the Docker mode branch, change:

```python
                node = NodeConnection(
                    name=node_config["name"],
                    docker_container=node_config.get("docker_container"),
                    lightning_dir=node_config.get("lightning_dir", global_lightning_dir),
                    network=node_config.get("network", global_network),
                    omit_network_flag=bool(node_config.get("omit_network_flag", False))
                )
```

To:

```python
                sem = asyncio.Semaphore(self._max_concurrent_per_node)
                node = NodeConnection(
                    name=node_config["name"],
                    docker_container=node_config.get("docker_container"),
                    lightning_dir=node_config.get("lightning_dir", global_lightning_dir),
                    network=node_config.get("network", global_network),
                    omit_network_flag=bool(node_config.get("omit_network_flag", False)),
                    _semaphore=sem,
                )
```

In the REST mode branch, change:

```python
                node = NodeConnection(
                    name=node_config["name"],
                    rest_url=node_config.get("rest_url"),
                    rune=node_config.get("rune"),
                    ca_cert=node_config.get("ca_cert")
                )
```

To:

```python
                sem = asyncio.Semaphore(self._max_concurrent_per_node)
                node = NodeConnection(
                    name=node_config["name"],
                    rest_url=node_config.get("rest_url"),
                    rune=node_config.get("rune"),
                    ca_cert=node_config.get("ca_cert"),
                    _semaphore=sem,
                )
```

Remove the old semaphore storage line:

```python
            self._node_semaphores[node.name] = asyncio.Semaphore(self._max_concurrent_per_node)
```

This line is deleted entirely — the semaphore now lives inside the node.

- [ ] **Step 3: Simplify `call_all()` — remove redundant semaphore wrapping**

Replace the entire `call_all()` method:

```python
    async def call_all(self, method: str, params: Dict = None, timeout: float = 30.0) -> Dict[str, Any]:
        """Call an RPC method on all nodes in parallel."""
        async def call_with_timeout(name: str, node: NodeConnection) -> tuple:
            sem = self._node_semaphores.get(name)
            try:
                if sem:
                    async with sem:
                        result = await asyncio.wait_for(node.call(method, params), timeout=timeout)
                else:
                    result = await asyncio.wait_for(node.call(method, params), timeout=timeout)
                return (name, result)
            except asyncio.TimeoutError:
                logger.warning(f"Timeout calling {method} on {name}")
                return (name, {"error": f"Timeout after {timeout}s"})
            except Exception as e:
                logger.error(f"Error calling {method} on {name}: {e}")
                return (name, {"error": str(e) or f"{type(e).__name__} calling {method}"})

        tasks = [call_with_timeout(name, node) for name, node in self.nodes.items()]
        results_list = await asyncio.gather(*tasks)
        return dict(results_list)
```

With:

```python
    async def call_all(self, method: str, params: Dict = None, timeout: float = 30.0) -> Dict[str, Any]:
        """Call an RPC method on all nodes in parallel.

        Per-node concurrency is enforced inside NodeConnection.call().
        """
        async def call_with_timeout(name: str, node: NodeConnection) -> tuple:
            try:
                result = await asyncio.wait_for(
                    node.call(method, params), timeout=timeout
                )
                return (name, result)
            except asyncio.TimeoutError:
                logger.warning(f"Timeout calling {method} on {name}")
                return (name, {"error": f"Timeout after {timeout}s"})
            except Exception as e:
                logger.error(f"Error calling {method} on {name}: {e}")
                return (name, {"error": str(e) or f"{type(e).__name__} calling {method}"})

        tasks = [call_with_timeout(name, node) for name, node in self.nodes.items()]
        results_list = await asyncio.gather(*tasks)
        return dict(results_list)
```

- [ ] **Step 4: Verify file still parses**

```bash
python3 -c "import ast; ast.parse(open('tools/mcp-hive-server.py').read()); print('OK')"
```

Expected: OK

- [ ] **Step 5: Commit**

```bash
git add tools/mcp-hive-server.py
git commit -m "feat: enforce per-node RPC semaphore universally via NodeConnection.call()"
```

---

### Task 4: Update compat launcher to use restored source

**Files:**
- Modify: `../cl_revenue_ops/tools/hive_mcp_compat.py:20` (DEFAULT_SOURCE_REV)

This task updates after the cl-hive changes are committed and pushed, so the compat launcher reads the restored file with throttling built in.

- [ ] **Step 1: Push cl-hive changes**

```bash
cd /home/sat/bin/cl-hive
git push
```

- [ ] **Step 2: Get the new HEAD commit hash**

```bash
git rev-parse HEAD
```

Capture the output — this is the new source revision.

- [ ] **Step 3: Update DEFAULT_SOURCE_REV in hive_mcp_compat.py**

In `/home/sat/bin/cl_revenue_ops/tools/hive_mcp_compat.py`, line 20, change:

```python
DEFAULT_SOURCE_REV = "15ba8b2e96d08d39cf2226ff84c62524d6ba4245"
```

To:

```python
DEFAULT_SOURCE_REV = "<new-commit-hash>"
```

(Replace `<new-commit-hash>` with the hash from step 2.)

- [ ] **Step 4: Clear the cached MCP server so it regenerates on next start**

```bash
rm -f /tmp/hive-mcp-compat/mcp-hive-server.py
rm -f /tmp/hive-mcp-compat/advisor_db.py
```

- [ ] **Step 5: Verify compat launcher still works**

```bash
cd /home/sat/bin/cl_revenue_ops
python3 -c "
from tools.hive_mcp_compat import patch_observability_source, DEFAULT_CL_HIVE_PATH, DEFAULT_SOURCE_REV, SOURCE_FILES
from tools.hive_mcp_compat import _read_git_file
src = _read_git_file(DEFAULT_CL_HIVE_PATH, DEFAULT_SOURCE_REV, 'tools/mcp-hive-server.py')
patched = patch_observability_source(src)
import ast; ast.parse(patched); print('Patched source parses OK')
print(f'Source lines: {len(src.splitlines())}')
print(f'Patched lines: {len(patched.splitlines())}')
# Verify semaphore is present
assert '_semaphore' in patched, '_semaphore field missing'
assert '_call_dispatch' in patched, '_call_dispatch method missing'
print('Throttling code present: OK')
"
```

Expected: All checks pass.

- [ ] **Step 6: Commit and push**

```bash
cd /home/sat/bin/cl_revenue_ops
git add tools/hive_mcp_compat.py
git commit -m "chore: update MCP source rev to include RPC throttling"
git push
```

---

### Task 5: Smoke test the running MCP server

- [ ] **Step 1: Restart the MCP server**

The Claude Code session needs to be restarted (or the MCP server process killed) so it regenerates from the new source. After restart, verify the server is running:

```bash
ps aux | grep hive_mcp_compat | grep -v grep
```

Expected: One process running.

- [ ] **Step 2: Invoke concurrent MCP tools to verify throttling works**

Use the MCP tools to call `hive_status` and `revenue_status` — both should return without timeout. If they were timing out before due to RPC stacking, this confirms the fix.

- [ ] **Step 3: Run cl-hive test suite**

```bash
cd /home/sat/bin/cl-hive
.venv/bin/python -m pytest tests/ -x -q --tb=short
```

Expected: All tests pass (813+).

- [ ] **Step 4: Run cl-revenue-ops test suite**

```bash
cd /home/sat/bin/cl_revenue_ops
python3 -m pytest tests/ -x -q --tb=short
```

Expected: All tests pass (906+).
