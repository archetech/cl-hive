"""Tests for dual-funded channel open with single-funded fallback."""

import pytest
from unittest.mock import MagicMock, call

from modules.rpc_commands import _open_channel, _MAX_V2_UPDATE_ROUNDS


class TestDualFundSuccess:
    """Test successful dual-funded (v2) channel open."""

    def test_dual_fund_success(self):
        rpc = MagicMock()
        rpc.call.side_effect = self._v2_success_side_effect

        result = _open_channel(rpc, "02abc123", 1_000_000)

        assert result["funding_type"] == "dual-funded"
        assert result["channel_id"] == "chan123"
        assert result["txid"] == "tx456"

        # Verify v2 flow was called in order
        called_methods = [c[0][0] for c in rpc.call.call_args_list]
        assert called_methods == [
            "fundpsbt",
            "openchannel_init",
            "openchannel_update",
            "signpsbt",
            "openchannel_signed",
        ]

    def _v2_success_side_effect(self, method, params=None):
        if method == "fundpsbt":
            return {"psbt": "psbt_data"}
        elif method == "openchannel_init":
            return {"channel_id": "chan123", "psbt": "init_psbt"}
        elif method == "openchannel_update":
            return {"psbt": "updated_psbt", "commitments_secured": True}
        elif method == "signpsbt":
            return {"signed_psbt": "signed_psbt_data"}
        elif method == "openchannel_signed":
            return {"channel_id": "chan123", "txid": "tx456"}
        raise ValueError(f"Unexpected RPC call: {method}")


class TestDualFundFallback:
    """Test fallback to single-funded when v2 fails."""

    def test_dual_fund_fails_falls_back(self):
        """openchannel_init raises -> unreserveinputs -> fundchannel fallback."""
        rpc = MagicMock()

        def side_effect(method, params=None):
            if method == "fundpsbt":
                return {"psbt": "psbt_data"}
            elif method == "openchannel_init":
                raise Exception("Peer does not support option_dual_fund")
            elif method == "unreserveinputs":
                return {}
            elif method == "fundchannel":
                return {"channel_id": "chan_v1", "txid": "tx_v1"}
            raise ValueError(f"Unexpected: {method}")

        rpc.call.side_effect = side_effect

        result = _open_channel(rpc, "02abc123", 500_000)

        assert result["funding_type"] == "single-funded"
        assert result["channel_id"] == "chan_v1"
        assert result["txid"] == "tx_v1"

        # unreserveinputs should be called (psbt was created), no abort (no channel_id)
        called_methods = [c[0][0] for c in rpc.call.call_args_list]
        assert "unreserveinputs" in called_methods
        assert "openchannel_abort" not in called_methods
        assert "fundchannel" in called_methods

    def test_dual_fund_update_fails_aborts(self):
        """openchannel_init succeeds, update fails -> abort + unreserve -> fallback."""
        rpc = MagicMock()

        def side_effect(method, params=None):
            if method == "fundpsbt":
                return {"psbt": "psbt_data"}
            elif method == "openchannel_init":
                return {"channel_id": "chan_v2", "psbt": "init_psbt"}
            elif method == "openchannel_update":
                raise Exception("Negotiation failed")
            elif method == "openchannel_abort":
                return {}
            elif method == "unreserveinputs":
                return {}
            elif method == "fundchannel":
                return {"channel_id": "chan_v1", "txid": "tx_v1"}
            raise ValueError(f"Unexpected: {method}")

        rpc.call.side_effect = side_effect

        result = _open_channel(rpc, "02abc123", 500_000)

        assert result["funding_type"] == "single-funded"

        called_methods = [c[0][0] for c in rpc.call.call_args_list]
        assert "openchannel_abort" in called_methods
        assert "unreserveinputs" in called_methods
        assert "fundchannel" in called_methods

    def test_dual_fund_update_max_rounds(self):
        """commitments_secured never true -> aborts after max rounds -> fallback."""
        rpc = MagicMock()
        update_count = 0

        def side_effect(method, params=None):
            nonlocal update_count
            if method == "fundpsbt":
                return {"psbt": "psbt_data"}
            elif method == "openchannel_init":
                return {"channel_id": "chan_v2", "psbt": "init_psbt"}
            elif method == "openchannel_update":
                update_count += 1
                return {"psbt": f"updated_{update_count}", "commitments_secured": False}
            elif method == "openchannel_abort":
                return {}
            elif method == "unreserveinputs":
                return {}
            elif method == "fundchannel":
                return {"channel_id": "chan_v1", "txid": "tx_v1"}
            raise ValueError(f"Unexpected: {method}")

        rpc.call.side_effect = side_effect

        result = _open_channel(rpc, "02abc123", 500_000)

        assert result["funding_type"] == "single-funded"
        assert update_count == _MAX_V2_UPDATE_ROUNDS

        called_methods = [c[0][0] for c in rpc.call.call_args_list]
        assert "openchannel_abort" in called_methods
        assert "fundchannel" in called_methods

    def test_dual_fund_sign_fails_aborts(self):
        """signpsbt fails -> abort + unreserve -> fallback."""
        rpc = MagicMock()

        def side_effect(method, params=None):
            if method == "fundpsbt":
                return {"psbt": "psbt_data"}
            elif method == "openchannel_init":
                return {"channel_id": "chan_v2", "psbt": "init_psbt"}
            elif method == "openchannel_update":
                return {"psbt": "updated_psbt", "commitments_secured": True}
            elif method == "signpsbt":
                raise Exception("Signing failed")
            elif method == "openchannel_abort":
                return {}
            elif method == "unreserveinputs":
                return {}
            elif method == "fundchannel":
                return {"channel_id": "chan_v1", "txid": "tx_v1"}
            raise ValueError(f"Unexpected: {method}")

        rpc.call.side_effect = side_effect

        result = _open_channel(rpc, "02abc123", 500_000)

        assert result["funding_type"] == "single-funded"

        called_methods = [c[0][0] for c in rpc.call.call_args_list]
        assert "openchannel_abort" in called_methods
        assert "unreserveinputs" in called_methods
        assert "fundchannel" in called_methods

    def test_fundpsbt_fails_goes_straight_to_single(self):
        """fundpsbt raises -> no abort needed -> fundchannel."""
        rpc = MagicMock()

        def side_effect(method, params=None):
            if method == "fundpsbt":
                raise Exception("Insufficient funds for PSBT")
            elif method == "fundchannel":
                return {"channel_id": "chan_v1", "txid": "tx_v1"}
            raise ValueError(f"Unexpected: {method}")

        rpc.call.side_effect = side_effect

        result = _open_channel(rpc, "02abc123", 500_000)

        assert result["funding_type"] == "single-funded"

        # No abort or unreserve since neither psbt nor channel_id was set
        called_methods = [c[0][0] for c in rpc.call.call_args_list]
        assert "openchannel_abort" not in called_methods
        assert "unreserveinputs" not in called_methods
        assert "fundchannel" in called_methods


class TestParameterPassthrough:
    """Test that parameters are correctly forwarded."""

    def test_feerate_passed_through(self):
        """Verify feerate param reaches both fundpsbt and fundchannel."""
        rpc = MagicMock()

        def side_effect(method, params=None):
            if method == "fundpsbt":
                raise Exception("Force fallback")
            elif method == "fundchannel":
                return {"channel_id": "c1", "txid": "t1"}
            raise ValueError(f"Unexpected: {method}")

        rpc.call.side_effect = side_effect

        _open_channel(rpc, "02abc123", 500_000, feerate="urgent")

        # Check fundpsbt was called with the feerate
        fundpsbt_call = rpc.call.call_args_list[0]
        assert fundpsbt_call[0][1]["feerate"] == "urgent"

        # Check fundchannel was called with the feerate
        fundchannel_call = rpc.call.call_args_list[1]
        assert fundchannel_call[0][1]["feerate"] == "urgent"

    def test_announce_passed_through(self):
        """Verify announce param reaches both openchannel_init and fundchannel."""
        rpc = MagicMock()

        def side_effect(method, params=None):
            if method == "fundpsbt":
                return {"psbt": "psbt_data"}
            elif method == "openchannel_init":
                raise Exception("Force fallback")
            elif method == "unreserveinputs":
                return {}
            elif method == "fundchannel":
                return {"channel_id": "c1", "txid": "t1"}
            raise ValueError(f"Unexpected: {method}")

        rpc.call.side_effect = side_effect

        _open_channel(rpc, "02abc123", 500_000, announce=False)

        # Check openchannel_init was called with announce=False
        init_call = rpc.call.call_args_list[1]
        assert init_call[0][1]["announce"] is False

        # Check fundchannel was called with announce=False
        fundchannel_call = [c for c in rpc.call.call_args_list if c[0][0] == "fundchannel"][0]
        assert fundchannel_call[0][1]["announce"] is False


class TestLogging:
    """Test that log_fn is called appropriately."""

    def test_log_fn_called_on_v2_success(self):
        log_fn = MagicMock()
        rpc = MagicMock()

        def side_effect(method, params=None):
            if method == "fundpsbt":
                return {"psbt": "psbt_data"}
            elif method == "openchannel_init":
                return {"channel_id": "chan123", "psbt": "init_psbt"}
            elif method == "openchannel_update":
                return {"psbt": "updated_psbt", "commitments_secured": True}
            elif method == "signpsbt":
                return {"signed_psbt": "signed_psbt_data"}
            elif method == "openchannel_signed":
                return {"channel_id": "chan123", "txid": "tx456"}
            raise ValueError(f"Unexpected: {method}")

        rpc.call.side_effect = side_effect

        _open_channel(rpc, "02abc123", 500_000, log_fn=log_fn)

        assert log_fn.call_count >= 2
        # First log: attempting dual-funded
        assert "dual-funded" in log_fn.call_args_list[0][0][0].lower() or \
               "Dual-funded" in log_fn.call_args_list[0][0][0]

    def test_log_fn_called_on_fallback(self):
        log_fn = MagicMock()
        rpc = MagicMock()

        def side_effect(method, params=None):
            if method == "fundpsbt":
                raise Exception("No funds")
            elif method == "fundchannel":
                return {"channel_id": "c1", "txid": "t1"}
            raise ValueError(f"Unexpected: {method}")

        rpc.call.side_effect = side_effect

        _open_channel(rpc, "02abc123", 500_000, log_fn=log_fn)

        log_messages = [c[0][0] for c in log_fn.call_args_list]
        # Should have: attempt, fallback message, single-funded message
        assert any("failed" in m.lower() or "falling back" in m.lower() for m in log_messages)
        assert any("single-funded" in m.lower() for m in log_messages)

    def test_no_log_fn_does_not_crash(self):
        """Passing log_fn=None should not raise."""
        rpc = MagicMock()

        def side_effect(method, params=None):
            if method == "fundpsbt":
                raise Exception("No funds")
            elif method == "fundchannel":
                return {"channel_id": "c1", "txid": "t1"}
            raise ValueError(f"Unexpected: {method}")

        rpc.call.side_effect = side_effect

        result = _open_channel(rpc, "02abc123", 500_000, log_fn=None)
        assert result["funding_type"] == "single-funded"
