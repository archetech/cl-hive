"""Tests for fundchannel and multifundchannel channel opening helpers."""

from unittest.mock import MagicMock, call

import pytest

from modules.rpc_commands import (
    _batch_open_channels,
    _open_channel,
    _peer_supports_dual_fund,
)


class TestOpenChannel:
    def test_open_channel_success(self):
        rpc = MagicMock()
        rpc.call.return_value = {"channel_id": "chan123", "txid": "tx456"}

        result = _open_channel(rpc, "02abc123", 1_000_000)

        assert result == {"channel_id": "chan123", "txid": "tx456", "dual_funded": False}
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


class TestPeerSupportsDualFund:
    """Tests for _peer_supports_dual_fund() feature-bit detection."""

    def test_bit_28_set(self):
        rpc = MagicMock()
        features = hex(1 << 28)[2:]  # even bit
        rpc.call.return_value = {"peers": [{"features": features}]}
        assert _peer_supports_dual_fund(rpc, "02abc") is True

    def test_bit_29_set(self):
        rpc = MagicMock()
        features = hex(1 << 29)[2:]  # odd bit
        rpc.call.return_value = {"peers": [{"features": features}]}
        assert _peer_supports_dual_fund(rpc, "02abc") is True

    def test_neither_bit_set(self):
        rpc = MagicMock()
        features = hex(1 << 12)[2:]  # some other bit
        rpc.call.return_value = {"peers": [{"features": features}]}
        assert _peer_supports_dual_fund(rpc, "02abc") is False

    def test_empty_peers(self):
        rpc = MagicMock()
        rpc.call.return_value = {"peers": []}
        assert _peer_supports_dual_fund(rpc, "02abc") is False

    def test_rpc_error(self):
        rpc = MagicMock()
        rpc.call.side_effect = RuntimeError("connection lost")
        assert _peer_supports_dual_fund(rpc, "02abc") is False

    def test_missing_features_field(self):
        rpc = MagicMock()
        rpc.call.return_value = {"peers": [{"id": "02abc"}]}
        assert _peer_supports_dual_fund(rpc, "02abc") is False


class TestOpenChannelDualFund:
    """Tests for dual-fund path in _open_channel()."""

    def test_request_amt_zero_skips_feature_check(self):
        rpc = MagicMock()
        rpc.call.return_value = {"channel_id": "c1", "txid": "t1"}

        result = _open_channel(rpc, "02abc", 1_000_000, request_amt=0)

        # Only one rpc.call: fundchannel (no listpeers)
        rpc.call.assert_called_once_with(
            "fundchannel",
            {"id": "02abc", "amount": 1_000_000, "feerate": "normal", "announce": True},
        )
        assert result["dual_funded"] is False

    def test_request_amt_with_supporting_peer(self):
        rpc = MagicMock()
        features = hex(1 << 28)[2:]

        def mock_call(method, params=None):
            if method == "listpeers":
                return {"peers": [{"features": features}]}
            return {"channel_id": "c1", "txid": "t1"}

        rpc.call.side_effect = mock_call

        result = _open_channel(rpc, "02abc", 1_000_000, request_amt=500_000)

        assert result["dual_funded"] is True
        # fundchannel should include request_amt
        fund_call = [c for c in rpc.call.call_args_list if c[0][0] == "fundchannel"][0]
        assert fund_call[0][1]["request_amt"] == 500_000

    def test_request_amt_with_non_supporting_peer(self):
        rpc = MagicMock()

        def mock_call(method, params=None):
            if method == "listpeers":
                return {"peers": [{"features": hex(1 << 12)[2:]}]}
            return {"channel_id": "c1", "txid": "t1"}

        rpc.call.side_effect = mock_call

        result = _open_channel(rpc, "02abc", 1_000_000, request_amt=500_000)

        assert result["dual_funded"] is False
        fund_call = [c for c in rpc.call.call_args_list if c[0][0] == "fundchannel"][0]
        assert "request_amt" not in fund_call[0][1]

    def test_dual_fund_failure_falls_back(self):
        rpc = MagicMock()
        features = hex(1 << 28)[2:]
        call_count = {"n": 0}

        def mock_call(method, params=None):
            if method == "listpeers":
                return {"peers": [{"features": features}]}
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("dual-fund negotiation failed")
            return {"channel_id": "c1", "txid": "t1"}

        rpc.call.side_effect = mock_call

        result = _open_channel(rpc, "02abc", 1_000_000, request_amt=500_000)

        assert result["dual_funded"] is False
        # Second fundchannel call should not have request_amt
        fund_calls = [c for c in rpc.call.call_args_list if c[0][0] == "fundchannel"]
        assert len(fund_calls) == 2
        assert "request_amt" in fund_calls[0][0][1]
        assert "request_amt" not in fund_calls[1][0][1]

    def test_both_v2_and_v1_fail(self):
        rpc = MagicMock()
        features = hex(1 << 28)[2:]

        def mock_call(method, params=None):
            if method == "listpeers":
                return {"peers": [{"features": features}]}
            raise RuntimeError("fundchannel failed")

        rpc.call.side_effect = mock_call

        with pytest.raises(RuntimeError, match="fundchannel failed"):
            _open_channel(rpc, "02abc", 1_000_000, request_amt=500_000)
