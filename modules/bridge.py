"""
Integration Bridge Module for cl-hive.

Implements the "Paranoid" Bridge pattern with Circuit Breaker for
safe integration with external plugins (cl-revenue-ops).

Circuit Breaker Pattern:
- CLOSED: Normal operation, requests pass through
- OPEN: Fail fast, no requests sent (dependency is down)
- HALF_OPEN: Probe mode, single test request to check recovery

This prevents cascading failures when a dependency hangs or crashes.

Author: Lightning Goats Team
"""

import json
import math
import re
import shutil
import subprocess
import threading
import time
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from pyln.client import RpcError
# =============================================================================
# CONSTANTS
# =============================================================================

# Circuit Breaker thresholds
MAX_FAILURES = 3          # Consecutive failures before opening circuit
RESET_TIMEOUT = 60        # Seconds to wait before probing (OPEN -> HALF_OPEN)
RPC_TIMEOUT = 5           # Timeout for RPC calls (seconds)
HALF_OPEN_SUCCESS_THRESHOLD = 3  # Consecutive successes needed to close circuit (Issue #10)

# Minimum required version of cl-revenue-ops
MIN_REVENUE_OPS_VERSION = (1, 4, 0)

# Startup retry configuration (Issue: plugin startup race condition)
STARTUP_RETRY_ATTEMPTS = 5        # Number of retries for cl-revenue-ops detection
STARTUP_RETRY_BASE_DELAY = 1.0    # Base delay in seconds (doubles each retry)



# =============================================================================
# ENUMS
# =============================================================================

class CircuitState(Enum):
    """Circuit Breaker states."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Fail fast mode
    HALF_OPEN = "half_open"  # Probe mode


class BridgeStatus(Enum):
    """Overall bridge status."""
    ENABLED = "enabled"
    DISABLED = "disabled"
    DEGRADED = "degraded"


# =============================================================================
# EXCEPTIONS
# =============================================================================

class CircuitOpenError(Exception):
    """Raised when Circuit Breaker is OPEN and blocking requests."""
    pass


class BridgeDisabledError(Exception):
    """Raised when Bridge is disabled due to missing dependency."""
    pass


class VersionMismatchError(Exception):
    """Raised when dependency version is incompatible."""
    pass


# =============================================================================
# CIRCUIT BREAKER
# =============================================================================

class CircuitBreaker:
    """
    Implements the Circuit Breaker pattern for RPC calls.
    
    State transitions:
    - CLOSED -> OPEN: After MAX_FAILURES consecutive failures
    - OPEN -> HALF_OPEN: After RESET_TIMEOUT seconds
    - HALF_OPEN -> CLOSED: On successful probe
    - HALF_OPEN -> OPEN: On probe failure
    """
    
    def __init__(self, name: str, max_failures: int = MAX_FAILURES,
                 reset_timeout: int = RESET_TIMEOUT,
                 half_open_success_threshold: int = HALF_OPEN_SUCCESS_THRESHOLD):
        """
        Initialize Circuit Breaker.

        Args:
            name: Identifier for logging
            max_failures: Failures before opening circuit
            reset_timeout: Seconds before probing
            half_open_success_threshold: Consecutive successes needed in HALF_OPEN
        """
        self.name = name
        self.max_failures = max_failures
        self.reset_timeout = reset_timeout
        self.half_open_success_threshold = half_open_success_threshold

        self._lock = threading.RLock()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._half_open_success_count = 0  # Track consecutive successes in HALF_OPEN
        self._last_failure_time = 0
        self._last_success_time = 0
    
    @property
    def state(self) -> CircuitState:
        """Get current state, checking for automatic transitions."""
        with self._lock:
            if self._state == CircuitState.OPEN:
                now = int(time.time())
                if now - self._last_failure_time >= self.reset_timeout:
                    self._state = CircuitState.HALF_OPEN
            return self._state

    def is_available(self) -> bool:
        """Check if requests can be made (not OPEN)."""
        return self.state != CircuitState.OPEN

    def record_success(self) -> None:
        """
        Record a successful call.

        SECURITY (Issue #10): In HALF_OPEN state, require multiple consecutive
        successes before fully closing the circuit to prevent rapid flapping
        with unstable dependencies.
        """
        with self._lock:
            self._failure_count = 0
            self._last_success_time = int(time.time())

            if self._state == CircuitState.HALF_OPEN:
                self._half_open_success_count += 1
                if self._half_open_success_count >= self.half_open_success_threshold:
                    self._state = CircuitState.CLOSED
                    self._half_open_success_count = 0
            else:
                self._half_open_success_count = 0

    def record_failure(self) -> None:
        """Record a failed call."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = int(time.time())

            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._half_open_success_count = 0
            elif self._failure_count >= self.max_failures:
                self._state = CircuitState.OPEN

    def reset(self) -> None:
        """Reset circuit breaker to initial state."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._half_open_success_count = 0
            self._last_failure_time = 0

    def get_stats(self) -> Dict[str, Any]:
        """Get circuit breaker statistics."""
        with self._lock:
            return {
                "name": self.name,
                "state": self.state.value,
                "failure_count": self._failure_count,
                "max_failures": self.max_failures,
                "reset_timeout": self.reset_timeout,
                "last_failure_ago": int(time.time()) - self._last_failure_time if self._last_failure_time else None,
                "last_success_ago": int(time.time()) - self._last_success_time if self._last_success_time else None
            }


# =============================================================================
# BRIDGE CLASS
# =============================================================================

class Bridge:
    """
    Integration Bridge for cl-hive to external plugins.
    
    Provides "Paranoid" error handling for calls to:
    - cl-revenue-ops: Fee strategy and rebalancing

    Thread Safety:
    - Uses the thread-safe RPC proxy from cl-hive.py
    - Circuit breaker state is simple integers (thread-safe for reads)
    """
    
    def __init__(self, rpc, plugin=None):
        """
        Initialize the Bridge.
        
        Args:
            rpc: Thread-safe RPC proxy
            plugin: Optional plugin reference for logging
        """
        self.rpc = rpc
        self.plugin = plugin
        
        # Status tracking
        self._status = BridgeStatus.DISABLED
        self._revenue_ops_version: Optional[str] = None
        self._rpc_socket_path = self._resolve_rpc_socket()
        self._use_subprocess = bool(
            self._rpc_socket_path and shutil.which("lightning-cli")
        )
        if not self._use_subprocess:
            self._log(
                "Bridge RPC timeout disabled: lightning-cli or rpc socket unavailable",
                level="warn"
            )
        
        # Circuit breaker for revenue-ops integration
        self._revenue_ops_cb = CircuitBreaker("revenue-ops")


    def _resolve_rpc_socket(self) -> Optional[str]:
        """Resolve the Core Lightning RPC socket path if available."""
        # Check direct attribute access (not __getattr__ magic methods).
        # LightningRpc.__getattr__ turns any attribute into an RPC call,
        # so hasattr() alone is unreliable — use type(obj).__dict__ checks
        # and wrap calls in try/except to avoid spurious RPC calls.
        try:
            # Check instance/class dict directly to avoid __getattr__
            rpc_type = type(self.rpc)
            if "get_socket_path" in dir(rpc_type) or "get_socket_path" in getattr(self.rpc, "__dict__", {}):
                path = self.rpc.get_socket_path()
                if isinstance(path, str) and path:
                    return path
        except Exception:
            pass
        try:
            if "socket_path" in getattr(self.rpc, "__dict__", {}):
                path = self.rpc.__dict__["socket_path"]
                if isinstance(path, str) and path:
                    return path
            # Also check class-level descriptor/property
            if hasattr(type(self.rpc), "socket_path"):
                path = self.rpc.socket_path
                if isinstance(path, str) and path:
                    return path
        except Exception:
            pass
        try:
            rpc_inner = getattr(self.rpc, "_rpc", None)
            if rpc_inner is not None:
                inner_path = getattr(rpc_inner, "socket_path", None)
                if isinstance(inner_path, str) and inner_path:
                    return inner_path
        except Exception:
            pass
        return None
    
    def _log(self, msg: str, level: str = "info") -> None:
        """Log a message if plugin is available."""
        if self.plugin:
            self.plugin.log(f"[Bridge] {msg}", level=level)
    
    # =========================================================================
    # INITIALIZATION & FEATURE DETECTION
    # =========================================================================
    
    def initialize(self) -> BridgeStatus:
        """
        Detect available integrations and verify versions.

        Should be called once during plugin startup. Uses exponential backoff
        retry to handle plugin startup race conditions where cl-revenue-ops
        may not be fully initialized yet.

        Returns:
            BridgeStatus indicating availability
        """
        revenue_ops_ok = False

        # Retry with exponential backoff to handle startup race condition
        for attempt in range(STARTUP_RETRY_ATTEMPTS):
            revenue_ops_ok = self._detect_revenue_ops()
            if revenue_ops_ok:
                break

            # Don't sleep after the last attempt
            if attempt < STARTUP_RETRY_ATTEMPTS - 1:
                delay = STARTUP_RETRY_BASE_DELAY * (2 ** attempt)
                self._log(
                    f"cl-revenue-ops not ready, retry {attempt + 1}/{STARTUP_RETRY_ATTEMPTS} "
                    f"in {delay:.1f}s",
                    level='debug'
                )
                time.sleep(delay)

        if revenue_ops_ok:
            self._status = BridgeStatus.ENABLED
            self._log(f"Bridge enabled: cl-revenue-ops {self._revenue_ops_version}")
        else:
            self._status = BridgeStatus.DISABLED
            self._log("Bridge disabled: cl-revenue-ops not available", level='warn')

        return self._status

    def reinitialize(self) -> BridgeStatus:
        """
        Re-attempt bridge initialization if currently disabled.

        Useful for recovering from startup race conditions or after
        cl-revenue-ops is installed/restarted.

        Returns:
            BridgeStatus indicating new availability
        """
        if self._status == BridgeStatus.ENABLED:
            self._log("Bridge already enabled, skipping reinitialize")
            return self._status

        self._log("Attempting bridge re-initialization")
        return self.initialize()

    def _detect_revenue_ops(self) -> bool:
        """
        Detect cl-revenue-ops plugin and verify version.
        
        Returns:
            True if cl-revenue-ops is available and compatible
        """
        try:
            # Check plugin is loaded
            plugins = self.rpc.plugin("list")
            
            revenue_ops_active = False
            for p in plugins.get('plugins', []):
                if 'cl-revenue-ops' in p.get('name', ''):
                    revenue_ops_active = p.get('active', False)
                    break
            
            if not revenue_ops_active:
                self._log("cl-revenue-ops plugin not found or not active")
                return False
            
            # Check version
            status = self.rpc.call("revenue-status")
            version_str = status.get("version", "0.0.0")
            self._revenue_ops_version = version_str
            
            # Parse version
            version_tuple = self._parse_version(version_str)
            if version_tuple < MIN_REVENUE_OPS_VERSION:
                self._log(
                    f"cl-revenue-ops version {version_str} < required {MIN_REVENUE_OPS_VERSION}",
                    level='warn'
                )
                return False
            
            self._revenue_ops_cb.record_success()
            return True
            
        except Exception as e:
            self._log(f"Failed to detect cl-revenue-ops: {e}", level='warn')
            self._revenue_ops_cb.record_failure()
            return False
    
    def _parse_version(self, version_str: str) -> Tuple[int, int, int]:
        """
        Parse version string to tuple.
        
        Args:
            version_str: Version like "v1.4.0" or "1.4.0"
            
        Returns:
            Tuple of (major, minor, patch)
        """
        # Strip leading 'v' if present
        version_str = version_str.lstrip('v')
        
        # Extract numbers
        match = re.match(r'(\d+)\.(\d+)\.?(\d*)', version_str)
        if match:
            major = int(match.group(1))
            minor = int(match.group(2))
            patch = int(match.group(3)) if match.group(3) else 0
            return (major, minor, patch)
        
        self._log(f"Could not parse version string: {version_str}", level="debug")
        return (0, 0, 0)
    
    # =========================================================================
    # SAFE CALL WRAPPER
    # =========================================================================

    def _call_via_lightning_cli(self, method: str, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Execute an RPC call via lightning-cli with a hard timeout.

        Uses -k (keyword args) format to properly pass named parameters
        to CLN methods.
        """
        if not self._rpc_socket_path:
            raise BridgeDisabledError("RPC socket path unavailable")

        # Use -k for keyword args format: key=value
        cmd = ["lightning-cli", "--rpc-file", self._rpc_socket_path, "-k", method]
        if payload:
            for key, value in payload.items():
                # Handle different value types
                if value is None:
                    continue
                elif isinstance(value, bool):
                    cmd.append(f"{key}={str(value).lower()}")
                elif isinstance(value, (dict, list)):
                    cmd.append(f"{key}={json.dumps(value, separators=(',', ':'))}")
                elif isinstance(value, float):
                    if math.isnan(value) or math.isinf(value):
                        raise ValueError(f"Non-finite float for key {key}")
                    cmd.append(f"{key}={value}")
                elif isinstance(value, (int, str)):
                    if isinstance(value, str) and any(c in value for c in '\x00\n\r'):
                        raise ValueError(f"Invalid characters in string value for key {key}")
                    cmd.append(f"{key}={value}")
                else:
                    raise ValueError(f"Unsupported payload type for key {key}: {type(value).__name__}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=RPC_TIMEOUT,
            check=False
        )

        if result.returncode != 0:
            err_msg = (result.stderr or result.stdout or "").strip()
            raise RpcError(method, payload or {}, err_msg or "RPC error")

        output = result.stdout.strip()
        if not output:
            return {}

        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise RpcError(method, payload or {}, f"Invalid JSON response: {exc}")

    def _call_direct(self, method: str, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Execute an RPC call directly via the RPC proxy.

        Note: relies on RpcPoolProxy timeout (30s) when installed.
        If called with raw RPC before proxy install, falls back to
        subprocess path which has explicit RPC_TIMEOUT enforcement.
        """
        if payload:
            return self.rpc.call(method, payload)
        return self.rpc.call(method)

    def safe_call(self, method: str, payload: Dict = None,
                  circuit_breaker: CircuitBreaker = None) -> Dict[str, Any]:
        """
        Execute an RPC call with Circuit Breaker protection.
        
        Args:
            method: RPC method name
            payload: Optional payload dict
            circuit_breaker: Which circuit breaker to use
            
        Returns:
            RPC response
            
        Raises:
            CircuitOpenError: If circuit is open
            BridgeDisabledError: If bridge is disabled
        """
        if self._status == BridgeStatus.DISABLED:
            raise BridgeDisabledError("Bridge is disabled")
        
        cb = circuit_breaker or self._revenue_ops_cb
        
        if not cb.is_available():
            raise CircuitOpenError(f"Circuit {cb.name} is OPEN")
        
        try:
            if self._use_subprocess:
                result = self._call_via_lightning_cli(method, payload)
            else:
                result = self._call_direct(method, payload)

            cb.record_success()
            return result
        except subprocess.TimeoutExpired:
            cb.record_failure()
            self._log(
                f"RPC call {method} timed out after {RPC_TIMEOUT}s",
                level='warn'
            )
            raise TimeoutError(f"RPC call {method} timed out after {RPC_TIMEOUT}s") from None
        except RpcError as e:
            cb.record_failure()
            self._log(f"RPC call {method} failed: {e}", level='warn')
            raise
        except TimeoutError as e:
            # Direct RPC path (no subprocess) can raise built-in TimeoutError.
            # Count it so the circuit breaker still protects degraded mode.
            cb.record_failure()
            self._log(f"RPC call {method} timed out: {e}", level='warn')
            raise
        except Exception as e:
            cb.record_failure()
            self._log(f"RPC call {method} failed: {e}", level='warn')
            raise
    
    # =========================================================================
    # DATASTORE HELPERS
    # =========================================================================

    def _read_datastore_json(self, key: list, max_age_seconds: int) -> Optional[Dict[str, Any]]:
        """Read and validate a JSON datastore entry.

        Returns parsed dict if entry exists and timestamp is within max_age_seconds.
        Returns None if missing, stale, or on any error.
        """
        try:
            import json as _json
            ds = self.rpc.listdatastore(key=key)
            entries = ds.get("datastore", [])
            if not entries:
                return None
            data = _json.loads(entries[0].get("string", "{}"))
            age = int(time.time()) - data.get("timestamp", 0)
            if age > max_age_seconds:
                return None
            return data
        except Exception:
            return None

    # =========================================================================
    # REVENUE-OPS INTEGRATION (read-only queries)
    # =========================================================================

    def get_fee_config(self) -> Optional[Dict[str, Any]]:
        """
        Get fee configuration from cl-revenue-ops.

        Prefers dedicated fee-bounds datastore key (fastest). Falls back to
        CLN datastore revenue-status, then cross-plugin RPC.

        Returns:
            Dict with min/max fee bounds and midpoint, or None if unavailable
        """
        if self._status == BridgeStatus.DISABLED:
            return None

        # Priority 0: Dedicated fee-bounds key (simplest, most reliable)
        fb = self._read_datastore_json(["revenue", "fee-bounds"], max_age_seconds=120)
        if fb is not None:
            min_fee = fb.get("min_fee_ppm", 0)
            max_fee = fb.get("max_fee_ppm", 5000)
            mid_fee = fb.get("mid_fee_ppm", (min_fee + max_fee) // 2)
            return {"min_fee_ppm": min_fee, "max_fee_ppm": max_fee, "midpoint_ppm": mid_fee}

        try:
            # Priority 1: Read from CLN datastore (fast, no cross-plugin RPC)
            result = None
            try:
                import json as _json
                ds = self.rpc.listdatastore(key=["revenue", "status"])
                entries = ds.get("datastore", [])
                if entries:
                    data_str = entries[0].get("string", "")
                    if data_str:
                        result = _json.loads(data_str)
            except Exception:
                pass

            # Priority 2: Fall back to cross-plugin RPC
            if result is None:
                result = self.safe_call("revenue-status")

            operator_values = (
                result.get("operator_controls", {}).get("values", {})
                if isinstance(result, dict)
                else {}
            )
            min_fee = operator_values.get("min_fee_ppm")
            max_fee = operator_values.get("max_fee_ppm")

            if min_fee is not None and max_fee is not None:
                min_fee = int(min_fee)
                max_fee = int(max_fee)
                return {
                    "min_fee_ppm": min_fee,
                    "max_fee_ppm": max_fee,
                    "midpoint_ppm": (min_fee + max_fee) // 2,
                }

            config = result.get("config", {}) if isinstance(result, dict) else {}
            fee_range = config.get("fee_range_ppm")
            if isinstance(fee_range, list) and len(fee_range) == 2:
                min_fee = int(fee_range[0])
                max_fee = int(fee_range[1])
                return {
                    "min_fee_ppm": min_fee,
                    "max_fee_ppm": max_fee,
                    "midpoint_ppm": (min_fee + max_fee) // 2,
                }
            return None
        except Exception:
            return None

    def get_profitability(self) -> Optional[Dict[str, Any]]:
        """
        Get channel profitability data from cl-revenue-ops.

        Priority 1: Read from datastore (fast, no cross-plugin RPC)
        Priority 2: Cross-plugin RPC fallback

        Returns:
            Dict with per-channel profitability analysis, or None if unavailable
        """
        if self._status == BridgeStatus.DISABLED:
            return None

        # Priority 1: Datastore (10 min staleness = 2x the 5-min write cycle)
        ds_data = self._read_datastore_json(
            ["revenue", "profitability-summary"], max_age_seconds=600
        )
        if ds_data is not None:
            return ds_data

        # Priority 2: Cross-plugin RPC fallback
        try:
            result = self.safe_call("revenue-profitability")
            if isinstance(result, dict) and "error" not in result:
                return result
            return None
        except Exception:
            return None

    def get_dashboard(self, window_days: int = 30) -> Optional[Dict[str, Any]]:
        """
        Get financial dashboard from cl-revenue-ops.

        Priority 1: Read from datastore (fast, no cross-plugin RPC)
        Priority 2: Cross-plugin RPC fallback

        Returns:
            Dict with 30-day P&L snapshot, or None if unavailable
        """
        if self._status == BridgeStatus.DISABLED:
            return None

        # Priority 1: Datastore (10 min staleness = 2x the 5-min write cycle)
        ds_data = self._read_datastore_json(
            ["revenue", "dashboard"], max_age_seconds=600
        )
        if ds_data is not None:
            return ds_data

        # Priority 2: Cross-plugin RPC fallback
        try:
            result = self.safe_call("revenue-dashboard", window_days=window_days)
            if isinstance(result, dict) and "error" not in result:
                return result
            return None
        except Exception:
            return None

    # =========================================================================
    # STATUS & STATISTICS
    # =========================================================================

    @property
    def status(self) -> BridgeStatus:
        """Get current bridge status."""
        return self._status
    
    def get_stats(self) -> Dict[str, Any]:
        """Get bridge statistics."""
        return {
            "status": self._status.value,
            "revenue_ops": {
                "version": self._revenue_ops_version,
                "circuit_breaker": self._revenue_ops_cb.get_stats()
            },
        }
