"""Tests for the member-broadcast gateway transport core."""

import importlib.util
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modules.protocol import deserialize, serialize, HiveMessageType
from modules.relay import RelayManager


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

    mock_pyln_client = types.SimpleNamespace(Plugin=DummyPlugin, RpcError=Exception)
    sys.modules["pyln"] = types.SimpleNamespace(client=mock_pyln_client)
    sys.modules["pyln.client"] = mock_pyln_client

    module_path = REPO_ROOT / "cl-hive.py"
    spec = importlib.util.spec_from_file_location(
        f"cl_hive_gateway_test_{time.time_ns()}",
        module_path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _decode_last_sendcustommsg(sent_messages):
    """Decode the last captured custom message payload."""
    method, params = sent_messages[-1]
    assert method == "sendcustommsg"
    msg_type, payload = deserialize(bytes.fromhex(params["msg"]))
    return msg_type, payload


class TestMemberBroadcastGateway:
    def _configure_module(self, member_ids=None, banned_peer_ids=None):
        cl_hive = _load_cl_hive_module()
        our_pubkey = "02" + "a" * 64
        sent_messages = []
        banned_peer_ids = set(banned_peer_ids or [])

        if member_ids is None:
            member_ids = ["02" + "b" * 64]

        plugin = MagicMock()
        plugin.log = MagicMock()
        plugin.rpc = MagicMock()
        plugin.rpc.call.side_effect = lambda method, params: (
            sent_messages.append((method, params)) or {"result": "ok"}
        )

        cl_hive.plugin = plugin
        cl_hive.our_pubkey = our_pubkey
        cl_hive.database = MagicMock()
        cl_hive.database.get_all_members.return_value = [
            {"peer_id": peer_id, "tier": "member"} for peer_id in member_ids
        ]
        cl_hive.database.is_banned.side_effect = lambda peer_id: peer_id in banned_peer_ids
        cl_hive.relay_mgr = RelayManager(
            our_pubkey=our_pubkey,
            send_message=lambda peer_id, msg: True,
            get_members=lambda: member_ids,
            log=lambda *args, **kwargs: None,
        )

        shutdown_event = threading.Event()
        cl_hive.shutdown_event = shutdown_event

        # Sync module globals into protocol_handlers (moved handler code reads from there)
        cl_hive.protocol_handlers.init_protocol_handlers({
            'plugin': plugin,
            'our_pubkey': our_pubkey,
            'database': cl_hive.database,
            'relay_mgr': cl_hive.relay_mgr,
            'shutdown_event': shutdown_event,
        })

        # Sync module globals into background_loops (moved loop code reads from there)
        cl_hive.background_loops.init_background_loops({
            'plugin': plugin,
            'our_pubkey': our_pubkey,
            'database': cl_hive.database,
            'shutdown_event': shutdown_event,
        })

        return cl_hive, our_pubkey, sent_messages

    def test_gateway_adds_relay_metadata_for_payload_input(self):
        cl_hive, our_pubkey, sent_messages = self._configure_module()

        result = cl_hive.protocol_handlers._broadcast_member_message(
            msg_type=HiveMessageType.PHEROMONE_BATCH,
            payload={
                "reporter_id": our_pubkey,
                "timestamp": int(time.time()),
                "signature": "sig",
                "pheromones": [],
            },
            reliability="direct",
            failure_policy="best_effort",
            log_label="pheromone_batch",
        )

        msg_type, payload = _decode_last_sendcustommsg(sent_messages)
        assert msg_type == HiveMessageType.PHEROMONE_BATCH
        assert payload["_relay"]["origin"] == our_pubkey
        assert payload["_relay"]["relay_path"] == [our_pubkey]
        assert result["ok"] is True
        assert result["mode"] == "direct"
        assert result["policy"] == "best_effort"

    def test_gateway_normalizes_bytes_input_before_send(self):
        cl_hive, our_pubkey, sent_messages = self._configure_module()

        raw_msg = serialize(
            HiveMessageType.GOSSIP,
            {"sender_id": our_pubkey, "timestamp": int(time.time()), "signature": "sig"},
        )

        result = cl_hive.protocol_handlers._broadcast_member_message(
            message_bytes=raw_msg,
            reliability="direct",
            failure_policy="best_effort",
            log_label="gossip",
        )

        msg_type, payload = _decode_last_sendcustommsg(sent_messages)
        assert msg_type == HiveMessageType.GOSSIP
        assert payload["_relay"]["origin"] == our_pubkey
        assert payload["_relay"]["relay_path"] == [our_pubkey]
        assert result["ok"] is True
        assert result["sent"] == 1

    def test_gateway_preserves_existing_relay_metadata_for_bytes_input(self):
        cl_hive, _our_pubkey, _sent_messages = self._configure_module()
        raw_payload = {
            "sender_id": "02" + "f" * 64,
            "timestamp": int(time.time()),
            "signature": "sig",
            "_relay": {
                "origin": "02" + "e" * 64,
                "relay_path": ["02" + "e" * 64, "02" + "f" * 64],
                "ttl": 2,
                "msg_id": "fixed-relay-id",
                "origin_ts": 111,
            },
        }
        raw_msg = serialize(HiveMessageType.GOSSIP, raw_payload)

        msg_type, payload, _normalized = cl_hive.protocol_handlers._normalize_member_broadcast_bytes(
            message_bytes=raw_msg,
        )

        assert msg_type == HiveMessageType.GOSSIP
        assert payload["_relay"] == raw_payload["_relay"]

    def test_gateway_rejects_fail_closed_direct_policy(self):
        cl_hive, our_pubkey, _sent_messages = self._configure_module()

        with pytest.raises(ValueError, match="fail_closed broadcasts must use reliable delivery"):
            cl_hive.protocol_handlers._broadcast_member_message(
                msg_type=HiveMessageType.GOSSIP,
                payload={"sender_id": our_pubkey, "timestamp": int(time.time())},
                reliability="direct",
                failure_policy="fail_closed",
                log_label="gossip",
            )

    @pytest.mark.parametrize(
        ("payload", "message_bytes", "expected_error"),
        [
            (None, None, "exactly one of payload or message_bytes is required"),
            (
                {"sender_id": "02" + "a" * 64, "timestamp": 1},
                serialize(HiveMessageType.GOSSIP, {"sender_id": "02" + "a" * 64, "timestamp": 1}),
                "exactly one of payload or message_bytes is required",
            ),
        ],
    )
    def test_normalize_member_broadcast_bytes_requires_exactly_one_input(
        self, payload, message_bytes, expected_error
    ):
        cl_hive = _load_cl_hive_module()

        with pytest.raises(ValueError, match=expected_error):
            cl_hive.protocol_handlers._normalize_member_broadcast_bytes(
                msg_type=HiveMessageType.GOSSIP,
                payload=payload,
                message_bytes=message_bytes,
            )

    def test_gateway_rejects_unknown_reliability(self):
        cl_hive, our_pubkey, _sent_messages = self._configure_module()

        with pytest.raises(ValueError, match="unsupported reliability"):
            cl_hive.protocol_handlers._broadcast_member_message(
                msg_type=HiveMessageType.GOSSIP,
                payload={"sender_id": our_pubkey, "timestamp": int(time.time())},
                reliability="eventual",
                failure_policy="best_effort",
                log_label="gossip",
            )

    def test_gateway_rejects_unknown_failure_policy(self):
        cl_hive, our_pubkey, _sent_messages = self._configure_module()

        with pytest.raises(ValueError, match="unsupported failure_policy"):
            cl_hive.protocol_handlers._broadcast_member_message(
                msg_type=HiveMessageType.GOSSIP,
                payload={"sender_id": our_pubkey, "timestamp": int(time.time())},
                reliability="direct",
                failure_policy="drop_on_error",
                log_label="gossip",
            )

    def test_gateway_normalizes_explicit_targets(self):
        allowed_peer = "02" + "b" * 64
        banned_peer = "02" + "c" * 64
        non_member_peer = "02" + "d" * 64
        cl_hive, our_pubkey, sent_messages = self._configure_module(
            member_ids=[allowed_peer, banned_peer],
            banned_peer_ids=[banned_peer],
        )

        result = cl_hive.protocol_handlers._broadcast_member_message(
            msg_type=HiveMessageType.GOSSIP,
            payload={"sender_id": our_pubkey, "timestamp": int(time.time()), "signature": "sig"},
            reliability="direct",
            failure_policy="best_effort",
            targets=[allowed_peer, our_pubkey, banned_peer, allowed_peer, banned_peer, non_member_peer],
            log_label="gossip",
        )

        assert result["attempted"] == 1
        assert result["sent"] == 1
        assert result["failed"] == 0
        assert sent_messages == [
            (
                "sendcustommsg",
                {
                    "node_id": allowed_peer,
                    "msg": sent_messages[0][1]["msg"],
                },
            )
        ]

    def test_gateway_best_effort_reliable_falls_back_to_direct_without_outbox(self):
        cl_hive, our_pubkey, sent_messages = self._configure_module()
        cl_hive.outbox_mgr = None
        cl_hive.protocol_handlers.outbox_mgr = None

        result = cl_hive.protocol_handlers._broadcast_member_message(
            msg_type=HiveMessageType.GOSSIP,
            payload={"sender_id": our_pubkey, "timestamp": int(time.time()), "signature": "sig"},
            reliability="reliable",
            failure_policy="best_effort",
            log_label="gossip",
        )

        assert result["ok"] is True
        assert result["attempted"] == 1
        assert result["queued"] == 0
        assert result["sent"] == 1
        assert result["failed"] == 0
        assert sent_messages[-1][0] == "sendcustommsg"

    def test_gateway_best_effort_reliable_falls_back_to_direct_on_enqueue_failure(self):
        cl_hive, our_pubkey, sent_messages = self._configure_module()
        outbox_mock = MagicMock()
        outbox_mock.enqueue.side_effect = RuntimeError("queue unavailable")
        cl_hive.outbox_mgr = outbox_mock
        cl_hive.protocol_handlers.outbox_mgr = outbox_mock

        result = cl_hive.protocol_handlers._broadcast_member_message(
            msg_type=HiveMessageType.GOSSIP,
            payload={"sender_id": our_pubkey, "timestamp": int(time.time()), "signature": "sig"},
            reliability="reliable",
            failure_policy="best_effort",
            log_label="gossip",
        )

        assert result["ok"] is True
        assert result["queued"] == 0
        assert result["sent"] == 1
        assert result["failed"] == 0
        assert result["mode"] == "direct"
        assert sent_messages[-1][0] == "sendcustommsg"

    def test_gateway_fail_closed_reliable_does_not_fallback_without_outbox(self):
        cl_hive, our_pubkey, sent_messages = self._configure_module()
        cl_hive.outbox_mgr = None
        cl_hive.protocol_handlers.outbox_mgr = None

        result = cl_hive.protocol_handlers._broadcast_member_message(
            msg_type=HiveMessageType.GOSSIP,
            payload={"sender_id": our_pubkey, "timestamp": int(time.time()), "signature": "sig"},
            reliability="reliable",
            failure_policy="fail_closed",
            log_label="gossip",
        )

        assert result["ok"] is False
        assert result["attempted"] == 1
        assert result["queued"] == 0
        assert result["sent"] == 0
        assert result["failed"] == 1
        assert sent_messages == []

    def test_gateway_fail_closed_reliable_reports_partial_enqueue_failure(self):
        member_ids = ["02" + "b" * 64, "02" + "c" * 64]
        cl_hive, our_pubkey, _sent_messages = self._configure_module(member_ids=member_ids)
        outbox_mock = MagicMock()
        outbox_mock.enqueue.return_value = 1
        cl_hive.outbox_mgr = outbox_mock
        cl_hive.protocol_handlers.outbox_mgr = outbox_mock

        result = cl_hive.protocol_handlers._broadcast_member_message(
            msg_type=HiveMessageType.FULL_SYNC,
            payload={"sender_id": our_pubkey, "timestamp": int(time.time()), "signature": "sig"},
            reliability="reliable",
            failure_policy="fail_closed",
            log_label="full_sync",
        )
        assert result["ok"] is False
        assert result["attempted"] == 2
        assert result["queued"] == 1
        assert result["failed"] == 1
        assert result["mode"] == "reliable"
        assert result["policy"] == "fail_closed"

    def test_gateway_reliable_enqueue_uses_explicit_msg_id(self):
        cl_hive, our_pubkey, _sent_messages = self._configure_module()
        outbox_mock = MagicMock()
        outbox_mock.enqueue.return_value = 1
        cl_hive.outbox_mgr = outbox_mock
        cl_hive.protocol_handlers.outbox_mgr = outbox_mock

        result = cl_hive.protocol_handlers._broadcast_member_message(
            msg_type=HiveMessageType.FULL_SYNC,
            payload={"sender_id": our_pubkey, "timestamp": int(time.time()), "signature": "sig"},
            reliability="reliable",
            failure_policy="best_effort",
            msg_id="fixed-message-id",
            log_label="full_sync",
        )

        assert result["ok"] is True
        cl_hive.outbox_mgr.enqueue.assert_called_once()
        assert cl_hive.outbox_mgr.enqueue.call_args.args[0] == "fixed-message-id"

    def test_broadcast_to_members_delegates_to_gateway(self):
        cl_hive, our_pubkey, _sent_messages = self._configure_module()
        raw_msg = serialize(
            HiveMessageType.GOSSIP,
            {"sender_id": our_pubkey, "timestamp": int(time.time()), "signature": "sig"},
        )
        cl_hive.protocol_handlers._broadcast_member_message = MagicMock(return_value={"sent": 3})

        sent = cl_hive.protocol_handlers._broadcast_to_members(raw_msg)

        assert sent == 3
        cl_hive.protocol_handlers._broadcast_member_message.assert_called_once_with(
            message_bytes=raw_msg,
            reliability="direct",
            failure_policy="best_effort",
            log_label="broadcast_to_members",
        )

    def test_broadcast_to_members_logs_failed_delivery(self):
        cl_hive, our_pubkey, _sent_messages = self._configure_module()
        raw_msg = serialize(
            HiveMessageType.GOSSIP,
            {"sender_id": our_pubkey, "timestamp": int(time.time()), "signature": "sig"},
        )
        cl_hive.protocol_handlers._broadcast_member_message = MagicMock(
            return_value={"sent": 0, "failed": 1, "attempted": 1, "ok": False}
        )

        sent = cl_hive.protocol_handlers._broadcast_to_members(raw_msg)

        assert sent == 0
        cl_hive.plugin.log.assert_called()
        assert "broadcast_to_members incomplete" in cl_hive.plugin.log.call_args.args[0]

    def test_reliable_broadcast_delegates_to_gateway(self):
        cl_hive, our_pubkey, _sent_messages = self._configure_module()
        payload = {"sender_id": our_pubkey, "timestamp": int(time.time()), "signature": "sig"}
        cl_hive.protocol_handlers._broadcast_member_message = MagicMock(return_value={"ok": True})

        cl_hive.protocol_handlers._reliable_broadcast(
            HiveMessageType.FULL_SYNC,
            payload,
            msg_id="fixed-message-id",
        )

        cl_hive.protocol_handlers._broadcast_member_message.assert_called_once_with(
            msg_type=HiveMessageType.FULL_SYNC,
            payload=payload,
            reliability="reliable",
            failure_policy="best_effort",
            msg_id="fixed-message-id",
            log_label="reliable_broadcast",
        )

    def test_reliable_broadcast_logs_failed_delivery(self):
        cl_hive, our_pubkey, _sent_messages = self._configure_module()
        payload = {"sender_id": our_pubkey, "timestamp": int(time.time()), "signature": "sig"}
        cl_hive.protocol_handlers._broadcast_member_message = MagicMock(
            return_value={"ok": True, "failed": 1, "attempted": 2, "queued": 0, "sent": 1}
        )

        cl_hive.protocol_handlers._reliable_broadcast(
            HiveMessageType.FULL_SYNC,
            payload,
            msg_id="fixed-message-id",
        )

        cl_hive.plugin.log.assert_called()
        assert "reliable_broadcast incomplete" in cl_hive.plugin.log.call_args.args[0]

    def test_full_sync_uses_reliable_fail_closed_gateway(self):
        cl_hive, _our_pubkey, _sent_messages = self._configure_module()
        cl_hive.gossip_mgr = object()
        cl_hive.protocol_handlers.gossip_mgr = object()
        full_sync_msg = serialize(
            HiveMessageType.FULL_SYNC,
            {"sender_id": "02" + "a" * 64, "timestamp": int(time.time()), "signature": "sig"},
        )
        cl_hive.protocol_handlers._create_signed_full_sync_msg = MagicMock(return_value=full_sync_msg)
        cl_hive.protocol_handlers._broadcast_member_message = MagicMock(return_value={"ok": True, "queued": 1})

        cl_hive.protocol_handlers._broadcast_full_sync_to_members(cl_hive.plugin)

        cl_hive.protocol_handlers._broadcast_member_message.assert_called_once_with(
            message_bytes=full_sync_msg,
            reliability="reliable",
            failure_policy="fail_closed",
            log_label="full_sync",
        )

    def test_promotion_vote_uses_reliable_fail_closed_gateway(self):
        cl_hive, our_pubkey, _sent_messages = self._configure_module()
        cl_hive.membership_mgr = MagicMock()
        cl_hive.protocol_handlers.membership_mgr = MagicMock()
        cl_hive.membership_mgr.build_vouch_message.return_value = "canonical-vouch"
        cl_hive.plugin.rpc.signmessage.return_value = {"zbase": "signed-vouch"}
        cl_hive.protocol_handlers._broadcast_member_message = MagicMock(return_value={"ok": True, "queued": 1})
        target_peer_id = "02" + "d" * 64

        result = cl_hive.protocol_handlers._broadcast_promotion_vote(target_peer_id, our_pubkey)

        assert result is True
        cl_hive.protocol_handlers._broadcast_member_message.assert_called_once()
        call_kwargs = cl_hive.protocol_handlers._broadcast_member_message.call_args.kwargs
        assert call_kwargs["msg_type"] == HiveMessageType.VOUCH
        assert call_kwargs["reliability"] == "reliable"
        assert call_kwargs["failure_policy"] == "fail_closed"
        assert call_kwargs["log_label"] == "promotion_vote"
        assert call_kwargs["payload"]["target_pubkey"] == target_peer_id
        assert call_kwargs["payload"]["voucher_pubkey"] == our_pubkey

    def test_mcf_solution_uses_reliable_fail_closed_gateway(self):
        cl_hive, our_pubkey, _sent_messages = self._configure_module()
        assignment = MagicMock()
        assignment.to_dict.return_value = {"source": "a", "dest": "b", "amount_sats": 123}
        solution = types.SimpleNamespace(
            assignments=[assignment],
            total_flow_sats=123,
            total_cost_sats=4,
            unmet_demand_sats=0,
            iterations=2,
        )
        mcf_msg = serialize(
            HiveMessageType.MCF_SOLUTION_BROADCAST,
            {"sender_id": our_pubkey, "timestamp": int(time.time()), "signature": "sig"},
        )
        protocol_module = sys.modules["modules.protocol"]
        original_factory = protocol_module.create_mcf_solution_broadcast
        protocol_module.create_mcf_solution_broadcast = MagicMock(return_value=mcf_msg)
        cl_hive.protocol_handlers._broadcast_member_message = MagicMock(return_value={"ok": True, "queued": 1})

        try:
            cl_hive.background_loops._broadcast_mcf_solution(solution)
        finally:
            protocol_module.create_mcf_solution_broadcast = original_factory

        cl_hive.protocol_handlers._broadcast_member_message.assert_called_once_with(
            message_bytes=mcf_msg,
            reliability="reliable",
            failure_policy="fail_closed",
            log_label="mcf_solution",
        )

    def test_circular_flow_alerts_use_best_effort_reliable_gateway(self):
        cl_hive, our_pubkey, _sent_messages = self._configure_module()
        cl_hive.cost_reduction_mgr = MagicMock()
        cl_hive.background_loops.cost_reduction_mgr = cl_hive.cost_reduction_mgr
        cl_hive.protocol_handlers.cost_reduction_mgr = MagicMock()
        cl_hive.cost_reduction_mgr.circular_detector.get_shareable_circular_flows.return_value = [
            {
                "members_involved": [our_pubkey],
                "total_amount_sats": 1000,
                "total_cost_sats": 12,
                "cycle_count": 1,
                "detection_window_hours": 6,
                "recommendation": "avoid",
            }
        ]
        alert_msg = serialize(
            HiveMessageType.CIRCULAR_FLOW_ALERT,
            {"sender_id": our_pubkey, "timestamp": int(time.time()), "signature": "sig"},
        )
        protocol_module = sys.modules["modules.protocol"]
        original_factory = protocol_module.create_circular_flow_alert
        protocol_module.create_circular_flow_alert = MagicMock(return_value=alert_msg)
        cl_hive.protocol_handlers._broadcast_member_message = MagicMock(return_value={"ok": True, "queued": 1})

        try:
            cl_hive.background_loops._broadcast_circular_flow_alerts()
        finally:
            protocol_module.create_circular_flow_alert = original_factory

        cl_hive.protocol_handlers._broadcast_member_message.assert_called_once_with(
            message_bytes=alert_msg,
            reliability="reliable",
            failure_policy="best_effort",
            log_label="circular_flow_alert",
        )

    def test_positioning_proposals_use_best_effort_reliable_gateway(self):
        cl_hive, our_pubkey, _sent_messages = self._configure_module()
        cl_hive.strategic_positioning_mgr = MagicMock()
        cl_hive.background_loops.strategic_positioning_mgr = cl_hive.strategic_positioning_mgr
        cl_hive.protocol_handlers.strategic_positioning_mgr = MagicMock()
        cl_hive.strategic_positioning_mgr.get_shareable_positioning_recommendations.return_value = [
            {
                "target_pubkey": "02" + "d" * 64,
                "target_alias": "Target",
                "reason": "exchange",
                "score": 0.8,
                "suggested_amount_sats": 250000,
                "priority": "high",
            }
        ]
        proposal_msg = serialize(
            HiveMessageType.POSITIONING_PROPOSAL,
            {"sender_id": our_pubkey, "timestamp": int(time.time()), "signature": "sig"},
        )
        protocol_module = sys.modules["modules.protocol"]
        original_factory = protocol_module.create_positioning_proposal
        protocol_module.create_positioning_proposal = MagicMock(return_value=proposal_msg)
        cl_hive.protocol_handlers._broadcast_member_message = MagicMock(return_value={"ok": True, "queued": 1})

        try:
            cl_hive.background_loops._broadcast_our_positioning_proposals()
        finally:
            protocol_module.create_positioning_proposal = original_factory

        cl_hive.protocol_handlers._broadcast_member_message.assert_called_once_with(
            message_bytes=proposal_msg,
            reliability="reliable",
            failure_policy="best_effort",
            log_label="positioning_proposal",
        )

    def test_close_proposals_use_best_effort_reliable_gateway(self):
        cl_hive, our_pubkey, _sent_messages = self._configure_module()
        cl_hive.rationalization_mgr = MagicMock()
        cl_hive.background_loops.rationalization_mgr = cl_hive.rationalization_mgr
        cl_hive.protocol_handlers.rationalization_mgr = MagicMock()
        cl_hive.rationalization_mgr.get_shareable_close_recommendations.return_value = [
            {
                "target_member": "02" + "d" * 64,
                "target_peer": "02" + "e" * 64,
                "reason": "redundant",
                "our_routing_share": 0.2,
                "their_routing_share": 0.8,
                "suggested_action": "close",
            }
        ]
        close_msg = serialize(
            HiveMessageType.CLOSE_PROPOSAL,
            {"sender_id": our_pubkey, "timestamp": int(time.time()), "signature": "sig"},
        )
        protocol_module = sys.modules["modules.protocol"]
        original_factory = protocol_module.create_close_proposal
        protocol_module.create_close_proposal = MagicMock(return_value=close_msg)
        cl_hive.protocol_handlers._broadcast_member_message = MagicMock(return_value={"ok": True, "queued": 1})

        try:
            cl_hive.background_loops._broadcast_our_close_proposals()
        finally:
            protocol_module.create_close_proposal = original_factory

        cl_hive.protocol_handlers._broadcast_member_message.assert_called_once_with(
            message_bytes=close_msg,
            reliability="reliable",
            failure_policy="best_effort",
            log_label="close_proposal",
        )

    def test_fee_intelligence_uses_best_effort_direct_gateway(self):
        cl_hive, our_pubkey, _sent_messages = self._configure_module()
        cl_hive.fee_intel_mgr = MagicMock()
        cl_hive.background_loops.fee_intel_mgr = cl_hive.fee_intel_mgr
        cl_hive.plugin.rpc.listfunds.return_value = {
            "channels": [
                {
                    "state": "CHANNELD_NORMAL",
                    "peer_id": "02" + "d" * 64,
                    "short_channel_id": "1x1x1",
                    "amount_msat": 1_000_000,
                    "our_amount_msat": 600_000,
                }
            ]
        }
        cl_hive.plugin.rpc.listpeerchannels.return_value = {"channels": []}
        cl_hive.plugin.rpc.listforwards.return_value = {"forwards": []}
        fee_msg = serialize(
            HiveMessageType.FEE_INTELLIGENCE_SNAPSHOT,
            {"sender_id": our_pubkey, "timestamp": int(time.time()), "signature": "sig"},
        )
        cl_hive.fee_intel_mgr.create_fee_intelligence_snapshot_message.return_value = fee_msg
        cl_hive.protocol_handlers._broadcast_member_message = MagicMock(return_value={"ok": True, "sent": 1})

        cl_hive.background_loops._broadcast_our_fee_intelligence()

        cl_hive.protocol_handlers._broadcast_member_message.assert_called_once_with(
            message_bytes=fee_msg,
            reliability="direct",
            failure_policy="best_effort",
            log_label="fee_intelligence",
        )

    def test_temporal_patterns_use_best_effort_direct_gateway(self):
        cl_hive, our_pubkey, _sent_messages = self._configure_module()
        cl_hive.anticipatory_liquidity_mgr = MagicMock()
        cl_hive.background_loops.anticipatory_liquidity_mgr = cl_hive.anticipatory_liquidity_mgr
        cl_hive.protocol_handlers.anticipatory_liquidity_mgr = MagicMock()
        cl_hive.anticipatory_liquidity_mgr.get_shareable_patterns.return_value = [
            {
                "peer_id": "02" + "d" * 64,
                "peak_hours": [10, 11],
                "low_hours": [2, 3],
                "pattern_strength": 0.7,
            }
        ]
        pattern_msg = serialize(
            HiveMessageType.TEMPORAL_PATTERN_BATCH,
            {"sender_id": our_pubkey, "timestamp": int(time.time()), "signature": "sig"},
        )
        protocol_module = sys.modules["modules.protocol"]
        original_factory = protocol_module.create_temporal_pattern_batch
        protocol_module.create_temporal_pattern_batch = MagicMock(return_value=pattern_msg)
        cl_hive.protocol_handlers._broadcast_member_message = MagicMock(return_value={"ok": True, "sent": 1})

        try:
            cl_hive.background_loops._broadcast_our_temporal_patterns()
        finally:
            protocol_module.create_temporal_pattern_batch = original_factory

        cl_hive.protocol_handlers._broadcast_member_message.assert_called_once_with(
            message_bytes=pattern_msg,
            reliability="direct",
            failure_policy="best_effort",
            log_label="temporal_patterns",
        )

    def test_fee_report_uses_best_effort_reliable_gateway(self):
        cl_hive, our_pubkey, _sent_messages = self._configure_module()
        cl_hive.plugin.rpc.signmessage.return_value = {"zbase": "fee-report-sig"}
        fee_report_msg = serialize(
            HiveMessageType.FEE_REPORT,
            {"peer_id": our_pubkey, "timestamp": int(time.time()), "signature": "sig"},
        )
        protocol_module = sys.modules["modules.protocol"]
        original_factory = protocol_module.create_fee_report
        protocol_module.create_fee_report = MagicMock(return_value=fee_report_msg)
        cl_hive.protocol_handlers._broadcast_member_message = MagicMock(return_value={"ok": True, "queued": 1, "sent": 0})

        try:
            cl_hive.protocol_handlers._broadcast_fee_report(21, 3, 100, 200, rebalance_costs=5)
        finally:
            protocol_module.create_fee_report = original_factory

        cl_hive.protocol_handlers._broadcast_member_message.assert_called_once_with(
            message_bytes=fee_report_msg,
            reliability="reliable",
            failure_policy="best_effort",
            log_label="fee_report",
        )

    def test_intent_abort_uses_best_effort_reliable_gateway(self):
        cl_hive, our_pubkey, _sent_messages = self._configure_module()
        intent_mock = MagicMock()
        intent_mock.our_pubkey = our_pubkey
        cl_hive.intent_mgr = intent_mock
        cl_hive.protocol_handlers.intent_mgr = intent_mock
        cl_hive.plugin.rpc.signmessage.return_value = {"zbase": "abort-sig"}
        cl_hive.protocol_handlers._broadcast_member_message = MagicMock(return_value={"ok": True, "queued": 1})

        cl_hive.protocol_handlers.broadcast_intent_abort("02" + "d" * 64, "splice")

        cl_hive.protocol_handlers._broadcast_member_message.assert_called_once()
        call_kwargs = cl_hive.protocol_handlers._broadcast_member_message.call_args.kwargs
        assert call_kwargs["msg_type"] == HiveMessageType.INTENT_ABORT
        assert call_kwargs["reliability"] == "reliable"
        assert call_kwargs["failure_policy"] == "best_effort"
        assert call_kwargs["log_label"] == "intent_abort"
        assert call_kwargs["payload"]["target"] == "02" + "d" * 64

    def test_coverage_analysis_uses_best_effort_direct_gateway(self):
        cl_hive, our_pubkey, _sent_messages = self._configure_module()
        cl_hive.rationalization_mgr = MagicMock()
        cl_hive.background_loops.rationalization_mgr = cl_hive.rationalization_mgr
        cl_hive.protocol_handlers.rationalization_mgr = MagicMock()
        cl_hive.rationalization_mgr.get_shareable_coverage_analysis.return_value = [
            {
                "peer_id": "02" + "d" * 64,
                "owner_id": our_pubkey,
                "ownership_confidence": 0.8,
            }
        ]
        coverage_msg = serialize(
            HiveMessageType.COVERAGE_ANALYSIS_BATCH,
            {"sender_id": our_pubkey, "timestamp": int(time.time()), "signature": "sig"},
        )
        protocol_module = sys.modules["modules.protocol"]
        original_factory = protocol_module.create_coverage_analysis_batch
        protocol_module.create_coverage_analysis_batch = MagicMock(return_value=coverage_msg)
        cl_hive.protocol_handlers._broadcast_member_message = MagicMock(return_value={"ok": True, "sent": 1})

        try:
            cl_hive.background_loops._broadcast_our_coverage_analysis()
        finally:
            protocol_module.create_coverage_analysis_batch = original_factory

        cl_hive.protocol_handlers._broadcast_member_message.assert_called_once_with(
            message_bytes=coverage_msg,
            reliability="direct",
            failure_policy="best_effort",
            log_label="coverage_analysis",
        )
