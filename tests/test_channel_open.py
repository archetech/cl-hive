"""Tests for fundchannel and multifundchannel channel opening helpers."""

from unittest.mock import MagicMock

import pytest

from modules.rpc_commands import _batch_open_channels, _open_channel


class TestOpenChannel:
    def test_open_channel_success(self):
        rpc = MagicMock()
        rpc.call.return_value = {"channel_id": "chan123", "txid": "tx456"}

        result = _open_channel(rpc, "02abc123", 1_000_000)

        assert result == {"channel_id": "chan123", "txid": "tx456"}
        rpc.call.assert_called_once_with(
            "fundchannel",
            {"id": "02abc123", "amount": 1_000_000, "feerate": "normal", "announce": True},
        )

    def test_open_channel_failure(self):
        rpc = MagicMock()
        rpc.call.side_effect = RuntimeError("fundchannel failed")

        with pytest.raises(RuntimeError, match="fundchannel failed"):
            _open_channel(rpc, "02abc123", 500_000)

    def test_parameters_passed_through(self):
        rpc = MagicMock()
        rpc.call.return_value = {"channel_id": "c1", "txid": "t1"}

        _open_channel(rpc, "02abc123", 500_000, feerate="urgent", announce=False)

        rpc.call.assert_called_once_with(
            "fundchannel",
            {"id": "02abc123", "amount": 500_000, "feerate": "urgent", "announce": False},
        )

    def test_log_fn_called(self):
        rpc = MagicMock()
        rpc.call.return_value = {"channel_id": "c1", "txid": "t1"}
        log_fn = MagicMock()

        _open_channel(rpc, "02abc123", 500_000, log_fn=log_fn)

        assert log_fn.call_count == 1
        assert "opening channel" in log_fn.call_args[0][0].lower()

    def test_no_log_fn(self):
        rpc = MagicMock()
        rpc.call.return_value = {"channel_id": "c1", "txid": "t1"}

        result = _open_channel(rpc, "02abc123", 500_000, log_fn=None)

        assert result["channel_id"] == "c1"


class TestBatchOpenChannels:
    def test_batch_open_all_succeed(self):
        rpc = MagicMock()
        rpc.call.return_value = {
            "channel_ids": ["chan1", "chan2"],
            "txid": "tx123",
            "failed": [],
        }
        targets = [{"id": "02a", "amount": 100_000}, {"id": "02b", "amount": 200_000}]

        result = _batch_open_channels(rpc, targets)

        assert result["txid"] == "tx123"
        assert result["channel_ids"] == ["chan1", "chan2"]
        assert result["failed"] == []
        assert result["results_by_id"]["02a"]["channel_id"] == "chan1"
        assert result["results_by_id"]["02b"]["channel_id"] == "chan2"

    def test_batch_open_partial_failure(self):
        rpc = MagicMock()
        rpc.call.return_value = {
            "channel_ids": ["chan1"],
            "txid": "tx123",
            "failed": [{"id": "02b", "error": "peer rejected"}],
        }
        targets = [{"id": "02a", "amount": 100_000}, {"id": "02b", "amount": 200_000}]

        result = _batch_open_channels(rpc, targets)

        assert result["results_by_id"]["02a"]["status"] == "opened"
        assert result["results_by_id"]["02a"]["channel_id"] == "chan1"
        assert result["results_by_id"]["02b"]["status"] == "failed"
        assert result["results_by_id"]["02b"]["error"] == "peer rejected"

    def test_batch_open_single_target(self):
        rpc = MagicMock()
        rpc.call.return_value = {
            "channel_ids": ["chan1"],
            "txid": "tx123",
            "failed": [],
        }

        result = _batch_open_channels(rpc, [{"id": "02a", "amount": 100_000}])

        assert result["results_by_id"]["02a"]["channel_id"] == "chan1"

    def test_batch_parameters_passed_through(self):
        rpc = MagicMock()
        rpc.call.return_value = {"channel_ids": [], "txid": "tx123", "failed": []}
        targets = [{"id": "02a", "amount": 100_000}]

        _batch_open_channels(rpc, targets, feerate="urgent", announce=False)

        rpc.call.assert_called_once_with(
            "multifundchannel",
            {
                "destinations": targets,
                "feerate": "urgent",
                "announce": False,
                "minchannels": 1,
            },
        )
