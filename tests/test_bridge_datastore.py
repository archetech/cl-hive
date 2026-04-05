"""
Test Suite for cl-hive Bridge datastore-first reads.

Tests the _read_datastore_json helper, datastore-first profitability,
dashboard, and fee-bounds methods on HiveBridge (Bridge).

Author: Lightning Goats Team
"""

import json
import time

import pytest
from unittest.mock import Mock, MagicMock

# Add parent to path for imports
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Create a mock RpcError class (must be done before importing modules)
class MockRpcError(Exception):
    """Mock RpcError to match pyln.client.RpcError behavior."""
    pass

# Mock pyln.client before importing modules
mock_pyln = MagicMock()
mock_pyln.Plugin = MagicMock
mock_pyln.RpcError = MockRpcError
sys.modules['pyln'] = mock_pyln
sys.modules['pyln.client'] = mock_pyln

from modules.bridge import Bridge, BridgeStatus


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def mock_rpc():
    """Create a mock RPC proxy."""
    return MagicMock()


@pytest.fixture
def mock_plugin():
    """Create a mock plugin for logging."""
    plugin = Mock()
    plugin.log = Mock()
    return plugin


@pytest.fixture
def bridge(mock_rpc, mock_plugin):
    """Create a Bridge instance with mocks, status set to ENABLED."""
    b = Bridge(mock_rpc, mock_plugin)
    b._status = BridgeStatus.ENABLED
    return b


def _ds_response(data: dict) -> dict:
    """Build a listdatastore response wrapping *data* as a JSON string."""
    return {"datastore": [{"string": json.dumps(data)}]}


def _fresh_ts() -> int:
    """Return a timestamp that is definitely fresh (now)."""
    return int(time.time())


def _stale_ts(max_age: int) -> int:
    """Return a timestamp older than *max_age* seconds."""
    return int(time.time()) - max_age - 60


# =============================================================================
# _read_datastore_json TESTS
# =============================================================================

class TestReadDatastoreJson:
    """Tests for the _read_datastore_json helper."""

    def test_returns_fresh_data(self, bridge, mock_rpc):
        """Fresh timestamp within max_age returns the parsed data."""
        payload = {"timestamp": _fresh_ts(), "foo": "bar"}
        mock_rpc.listdatastore.return_value = _ds_response(payload)

        result = bridge._read_datastore_json(["revenue", "test"], max_age_seconds=300)

        assert result is not None
        assert result["foo"] == "bar"

    def test_rejects_stale_data(self, bridge, mock_rpc):
        """Old timestamp beyond max_age returns None."""
        payload = {"timestamp": _stale_ts(300), "foo": "bar"}
        mock_rpc.listdatastore.return_value = _ds_response(payload)

        result = bridge._read_datastore_json(["revenue", "test"], max_age_seconds=300)

        assert result is None

    def test_returns_none_on_empty(self, bridge, mock_rpc):
        """Empty datastore (no entries) returns None."""
        mock_rpc.listdatastore.return_value = {"datastore": []}

        result = bridge._read_datastore_json(["revenue", "test"], max_age_seconds=300)

        assert result is None

    def test_returns_none_on_exception(self, bridge, mock_rpc):
        """RPC failure returns None (no exception raised)."""
        mock_rpc.listdatastore.side_effect = Exception("RPC down")

        result = bridge._read_datastore_json(["revenue", "test"], max_age_seconds=300)

        assert result is None

    def test_returns_none_on_missing_timestamp(self, bridge, mock_rpc):
        """Entry without a timestamp field is treated as infinitely stale."""
        payload = {"foo": "bar"}  # no "timestamp" key
        mock_rpc.listdatastore.return_value = _ds_response(payload)

        result = bridge._read_datastore_json(["revenue", "test"], max_age_seconds=300)

        assert result is None


# =============================================================================
# get_profitability DATASTORE TESTS
# =============================================================================

class TestProfitabilityFromDatastore:
    """Tests for datastore-first profitability reads."""

    def test_prefers_datastore(self, bridge, mock_rpc):
        """Fresh datastore data is returned without calling safe_call/RPC."""
        payload = {"timestamp": _fresh_ts(), "channels": [{"id": "abc", "roi": 0.05}]}
        mock_rpc.listdatastore.return_value = _ds_response(payload)

        result = bridge.get_profitability()

        assert result is not None
        assert result["channels"][0]["id"] == "abc"
        # safe_call uses rpc.call — should NOT have been invoked
        mock_rpc.call.assert_not_called()

    def test_falls_back_to_rpc_when_stale(self, bridge, mock_rpc):
        """Stale datastore triggers cross-plugin RPC fallback."""
        stale_payload = {"timestamp": _stale_ts(600), "channels": []}
        mock_rpc.listdatastore.return_value = _ds_response(stale_payload)
        mock_rpc.call.return_value = {"channels": [{"id": "xyz", "roi": 0.1}]}

        result = bridge.get_profitability()

        assert result is not None
        assert result["channels"][0]["id"] == "xyz"
        mock_rpc.call.assert_called()


# =============================================================================
# get_dashboard DATASTORE TESTS
# =============================================================================

class TestDashboardFromDatastore:
    """Tests for datastore-first dashboard reads."""

    def test_prefers_datastore(self, bridge, mock_rpc):
        """Fresh datastore data is returned without cross-plugin RPC."""
        payload = {"timestamp": _fresh_ts(), "revenue_sats": 4200, "costs_sats": 100}
        mock_rpc.listdatastore.return_value = _ds_response(payload)

        result = bridge.get_dashboard()

        assert result is not None
        assert result["revenue_sats"] == 4200
        mock_rpc.call.assert_not_called()


# =============================================================================
# get_fee_config fee-bounds DATASTORE TESTS
# =============================================================================

class TestFeeBoundsFromDatastore:
    """Tests for fee-bounds datastore short-circuit in get_fee_config."""

    def test_short_circuits_before_status_read(self, bridge, mock_rpc):
        """fee-bounds datastore key available returns immediately.

        Neither the revenue-status datastore read nor the RPC fallback
        should be reached.
        """
        payload = {
            "timestamp": _fresh_ts(),
            "min_fee_ppm": 25,
            "max_fee_ppm": 1200,
            "mid_fee_ppm": 612,
        }
        mock_rpc.listdatastore.return_value = _ds_response(payload)

        result = bridge.get_fee_config()

        assert result == {"min_fee_ppm": 25, "max_fee_ppm": 1200, "midpoint_ppm": 612}
        # Only 1 listdatastore call (fee-bounds); revenue-status never read
        assert mock_rpc.listdatastore.call_count == 1
        mock_rpc.call.assert_not_called()


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
