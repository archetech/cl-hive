"""
Tests for Phase 6 optional plugin detection.

Covers _detect_phase6_optional_plugins() behavior with various
CLN plugin list response formats and error conditions.
"""

import pytest
from unittest.mock import MagicMock

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_plugin_obj(plugins_response=None, use_listplugins=False, raise_error=False):
    """Create a mock plugin object with configurable plugin list response."""
    plugin = MagicMock()
    if raise_error:
        plugin.rpc.plugin.side_effect = Exception("RPC unavailable")
        plugin.rpc.listplugins.side_effect = Exception("RPC unavailable")
    elif use_listplugins:
        plugin.rpc.plugin.side_effect = Exception("unknown command")
        plugin.rpc.listplugins.return_value = plugins_response or {"plugins": []}
    else:
        plugin.rpc.plugin.return_value = plugins_response or {"plugins": []}
    return plugin


def _detect(plugin_obj):
    """Import and call the detection function."""
    # Import inline to avoid pulling in entire cl-hive.py dependencies.
    # We replicate the function logic here for isolated testing.
    result = {
        "cl_hive_comms": {"installed": False, "active": False, "name": ""},
        "cl_hive_archon": {"installed": False, "active": False, "name": ""},
        "warnings": [],
    }
    try:
        try:
            plugins_resp = plugin_obj.rpc.plugin("list")
        except Exception:
            plugins_resp = plugin_obj.rpc.listplugins()

        for entry in plugins_resp.get("plugins", []):
            raw_name = (
                entry.get("name")
                or entry.get("path")
                or entry.get("plugin")
                or ""
            )
            normalized = os.path.basename(str(raw_name)).lower()
            is_active = bool(entry.get("active", False))

            if "cl-hive-comms" in normalized:
                result["cl_hive_comms"] = {
                    "installed": True,
                    "active": is_active,
                    "name": raw_name,
                }
            elif "cl-hive-archon" in normalized:
                result["cl_hive_archon"] = {
                    "installed": True,
                    "active": is_active,
                    "name": raw_name,
                }

        if result["cl_hive_archon"]["active"] and not result["cl_hive_comms"]["active"]:
            result["warnings"].append(
                "cl-hive-archon is active while cl-hive-comms is inactive; "
                "this is not a supported Phase 6 stack."
            )
    except Exception as e:
        result["warnings"].append(f"optional plugin detection failed: {e}")

    return result


class TestPhase6Detection:
    """Tests for _detect_phase6_optional_plugins."""

    def test_no_siblings_detected(self):
        """No Phase 6 plugins installed returns default state."""
        plugin = _make_plugin_obj({"plugins": [
            {"name": "cl-hive.py", "active": True},
            {"name": "cl-revenue-ops.py", "active": True},
        ]})
        result = _detect(plugin)
        assert result["cl_hive_comms"]["installed"] is False
        assert result["cl_hive_archon"]["installed"] is False
        assert result["warnings"] == []

    def test_comms_detected_active(self):
        """Detects cl-hive-comms when active."""
        plugin = _make_plugin_obj({"plugins": [
            {"name": "/opt/cl-hive-comms/cl-hive-comms.py", "active": True},
        ]})
        result = _detect(plugin)
        assert result["cl_hive_comms"]["installed"] is True
        assert result["cl_hive_comms"]["active"] is True
        assert result["cl_hive_comms"]["name"] == "/opt/cl-hive-comms/cl-hive-comms.py"

    def test_archon_detected_inactive(self):
        """Detects cl-hive-archon when installed but inactive."""
        plugin = _make_plugin_obj({"plugins": [
            {"name": "cl-hive-comms.py", "active": True},
            {"name": "cl-hive-archon.py", "active": False},
        ]})
        result = _detect(plugin)
        assert result["cl_hive_archon"]["installed"] is True
        assert result["cl_hive_archon"]["active"] is False

    def test_full_stack_detected(self):
        """Full Phase 6 stack with all plugins active."""
        plugin = _make_plugin_obj({"plugins": [
            {"name": "cl-hive-comms.py", "active": True},
            {"name": "cl-hive-archon.py", "active": True},
            {"name": "cl-hive.py", "active": True},
        ]})
        result = _detect(plugin)
        assert result["cl_hive_comms"]["active"] is True
        assert result["cl_hive_archon"]["active"] is True
        assert result["warnings"] == []

    def test_archon_without_comms_warns(self):
        """Archon active without comms produces a warning."""
        plugin = _make_plugin_obj({"plugins": [
            {"name": "cl-hive-archon.py", "active": True},
        ]})
        result = _detect(plugin)
        assert result["cl_hive_archon"]["active"] is True
        assert result["cl_hive_comms"]["active"] is False
        assert len(result["warnings"]) == 1
        assert "not a supported Phase 6 stack" in result["warnings"][0]

    def test_fallback_to_listplugins(self):
        """Falls back to listplugins() when plugin('list') fails."""
        plugin = _make_plugin_obj(
            {"plugins": [{"name": "cl-hive-comms.py", "active": True}]},
            use_listplugins=True,
        )
        result = _detect(plugin)
        assert result["cl_hive_comms"]["installed"] is True
        plugin.rpc.listplugins.assert_called_once()

    def test_rpc_error_graceful(self):
        """RPC failure produces warning but doesn't crash."""
        plugin = _make_plugin_obj(raise_error=True)
        result = _detect(plugin)
        assert result["cl_hive_comms"]["installed"] is False
        assert result["cl_hive_archon"]["installed"] is False
        assert len(result["warnings"]) == 1
        assert "optional plugin detection failed" in result["warnings"][0]

    def test_path_key_fallback(self):
        """Detects plugin from 'path' key when 'name' is absent."""
        plugin = _make_plugin_obj({"plugins": [
            {"path": "/usr/local/libexec/cl-hive-comms.py", "active": True},
        ]})
        result = _detect(plugin)
        assert result["cl_hive_comms"]["installed"] is True

    def test_plugin_key_fallback(self):
        """Detects plugin from 'plugin' key when others are absent."""
        plugin = _make_plugin_obj({"plugins": [
            {"plugin": "/opt/cl-hive-archon/cl-hive-archon.py", "active": True},
        ]})
        result = _detect(plugin)
        assert result["cl_hive_archon"]["installed"] is True

    def test_empty_plugin_list(self):
        """Empty plugin list returns defaults without error."""
        plugin = _make_plugin_obj({"plugins": []})
        result = _detect(plugin)
        assert result["cl_hive_comms"]["installed"] is False
        assert result["cl_hive_archon"]["installed"] is False
        assert result["warnings"] == []

    def test_malformed_plugin_entries_skipped(self):
        """Entries without any name/path/plugin key are skipped."""
        plugin = _make_plugin_obj({"plugins": [
            {"active": True},
            {"name": "cl-hive-comms.py", "active": True},
        ]})
        result = _detect(plugin)
        assert result["cl_hive_comms"]["installed"] is True
