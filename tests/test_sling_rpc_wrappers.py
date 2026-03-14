"""Regression tests for thin Sling RPC wrappers in cl-hive.py."""

import importlib.util
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class MockRpcError(Exception):
    """Lightweight stand-in for pyln.client.RpcError."""

    def __init__(self, method, payload, error):
        self.method = method
        self.payload = payload
        self.error = error
        super().__init__(
            f"RPC call failed: method: {method}, payload: {payload}, error: {error}"
        )


def _load_cl_hive_module():
    """Import cl-hive.py under a lightweight pyln.client stub."""

    class DummyPlugin:
        def __init__(self, *args, **kwargs):
            self.rpc = None
            self.log = lambda *a, **k: None
            self.write_lock = None
            self.stdout = None

        def method(self, *args, **kwargs):
            def decorator(fn):
                return fn

            return decorator

        hook = method
        subscribe = method
        init = method

        def add_option(self, *args, **kwargs):
            return None

        def run(self):
            return None

        def __getattr__(self, name):
            def no_op(*args, **kwargs):
                return None

            return no_op

    mock_pyln_client = types.SimpleNamespace(Plugin=DummyPlugin, RpcError=MockRpcError)
    sys.modules["pyln"] = types.SimpleNamespace(client=mock_pyln_client)
    sys.modules["pyln.client"] = mock_pyln_client

    module_path = REPO_ROOT / "cl-hive.py"
    spec = importlib.util.spec_from_file_location(
        f"cl_hive_sling_rpc_test_{time.time_ns()}",
        module_path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSlingDeleteJobWrapper:
    def test_delete_all_missing_jobs_file_is_noop(self):
        """First-run delete-all should treat a missing jobs file as empty state."""
        cl_hive = _load_cl_hive_module()
        plugin = MagicMock()
        plugin.log = MagicMock()
        plugin.rpc = MagicMock()
        plugin.rpc.call.side_effect = MockRpcError(
            "sling-deletejob",
            {"job": "all"},
            {"code": -32700, "data": None, "message": "No such file or directory (os error 2)"},
        )

        result = cl_hive.hive_sling_deletejob(plugin, job="all")

        assert result == {
            "status": "noop",
            "job": "all",
            "deleted": 0,
            "message": "sling jobs store not initialized",
        }
        plugin.log.assert_called_once()

    def test_delete_specific_job_missing_file_still_raises(self):
        """Narrow the no-op behavior to delete-all only."""
        cl_hive = _load_cl_hive_module()
        plugin = MagicMock()
        plugin.log = MagicMock()
        plugin.rpc = MagicMock()
        plugin.rpc.call.side_effect = MockRpcError(
            "sling-deletejob",
            {"job": "job-123"},
            {"code": -32700, "data": None, "message": "No such file or directory (os error 2)"},
        )

        with pytest.raises(MockRpcError):
            cl_hive.hive_sling_deletejob(plugin, job="job-123")

    def test_delete_all_unrelated_rpc_error_still_raises(self):
        """Do not hide non-filesystem Sling failures."""
        cl_hive = _load_cl_hive_module()
        plugin = MagicMock()
        plugin.log = MagicMock()
        plugin.rpc = MagicMock()
        plugin.rpc.call.side_effect = MockRpcError(
            "sling-deletejob",
            {"job": "all"},
            {"code": -1, "data": None, "message": "permission denied"},
        )

        with pytest.raises(MockRpcError):
            cl_hive.hive_sling_deletejob(plugin, job="all")
