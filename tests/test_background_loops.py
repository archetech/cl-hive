from types import SimpleNamespace
from unittest.mock import MagicMock

from modules import background_loops
from modules import protocol_handlers
from modules.protocol import (
    HiveMessageType,
    create_close_proposal,
    create_positioning_proposal,
    deserialize,
)


PEER_A = "02" + "a" * 64
PEER_B = "02" + "b" * 64
PEER_C = "02" + "c" * 64
PEER_D = "02" + "d" * 64


def _make_plugin() -> MagicMock:
    plugin = MagicMock()
    plugin.rpc.signmessage.return_value = {"signature": "sig"}
    plugin.log = MagicMock()
    return plugin


def _payload_from_message(message_bytes):
    msg_type, payload = deserialize(message_bytes)
    assert msg_type is not None
    assert payload is not None
    return payload


def test_broadcast_our_positioning_proposals_uses_current_shareable_shape(monkeypatch):
    plugin = _make_plugin()
    sent_messages = []

    monkeypatch.setattr(background_loops, "plugin", plugin, raising=False)
    monkeypatch.setattr(background_loops, "database", object(), raising=False)
    monkeypatch.setattr(background_loops, "our_pubkey", PEER_A, raising=False)
    monkeypatch.setattr(
        background_loops,
        "strategic_positioning_mgr",
        SimpleNamespace(
            get_shareable_positioning_recommendations=lambda max_recommendations: [
                {
                    "target_peer_id": PEER_B,
                    "recommended_member": PEER_C,
                    "priority_tier": "high",
                    "target_capacity_sats": 5_000_000,
                    "reason": "high value corridor",
                    "value_score": 0.42,
                }
            ]
        ),
        raising=False,
    )

    def fake_broadcast_member_message(*, message_bytes, **kwargs):
        sent_messages.append(message_bytes)
        return {"queued": 1, "sent": 0}

    monkeypatch.setattr(
        background_loops.protocol_handlers,
        "_broadcast_member_message",
        fake_broadcast_member_message,
    )

    background_loops._broadcast_our_positioning_proposals()

    assert len(sent_messages) == 1
    msg_type, payload = deserialize(sent_messages[0])
    assert msg_type == HiveMessageType.POSITIONING_PROPOSAL
    assert payload["target_peer_id"] == PEER_B
    assert payload["recommended_member"] == PEER_C
    assert payload["priority_tier"] == "high"
    assert payload["target_capacity_sats"] == 5_000_000
    assert payload["value_score"] == 0.42


def test_broadcast_our_close_proposals_uses_current_shareable_shape(monkeypatch):
    plugin = _make_plugin()
    sent_messages = []

    monkeypatch.setattr(background_loops, "plugin", plugin, raising=False)
    monkeypatch.setattr(background_loops, "database", object(), raising=False)
    monkeypatch.setattr(background_loops, "our_pubkey", PEER_A, raising=False)
    monkeypatch.setattr(
        background_loops,
        "rationalization_mgr",
        SimpleNamespace(
            get_shareable_close_recommendations=lambda max_recommendations: [
                {
                    "member_id": PEER_B,
                    "peer_id": PEER_C,
                    "channel_id": "123x1x1",
                    "owner_id": PEER_D,
                    "reason": "redundant coverage",
                    "freed_capacity_sats": 7_000_000,
                    "member_marker_strength": 0.12,
                    "owner_marker_strength": 0.91,
                }
            ]
        ),
        raising=False,
    )

    def fake_broadcast_member_message(*, message_bytes, **kwargs):
        sent_messages.append(message_bytes)
        return {"queued": 1, "sent": 0}

    monkeypatch.setattr(
        background_loops.protocol_handlers,
        "_broadcast_member_message",
        fake_broadcast_member_message,
    )

    background_loops._broadcast_our_close_proposals()

    assert len(sent_messages) == 1
    msg_type, payload = deserialize(sent_messages[0])
    assert msg_type == HiveMessageType.CLOSE_PROPOSAL
    assert payload["member_id"] == PEER_B
    assert payload["peer_id"] == PEER_C
    assert payload["channel_id"] == "123x1x1"
    assert payload["owner_id"] == PEER_D
    assert payload["freed_capacity_sats"] == 7_000_000
    assert payload["member_marker_strength"] == 0.12
    assert payload["owner_marker_strength"] == 0.91


def test_handle_positioning_proposal_logs_current_target_key(monkeypatch):
    plugin = _make_plugin()
    plugin.rpc.checkmessage.return_value = {"verified": True, "pubkey": PEER_A}
    database = MagicMock()
    database.get_member.return_value = {"tier": "member"}
    database.is_banned.return_value = False

    message_bytes = create_positioning_proposal(
        target_peer_id=PEER_B,
        recommended_member=PEER_C,
        priority_tier="high",
        target_capacity_sats=5_000_000,
        reason="high value corridor",
        value_score=0.42,
        rpc=plugin.rpc,
        our_pubkey=PEER_A,
    )
    payload = _payload_from_message(message_bytes)

    monkeypatch.setattr(protocol_handlers, "database", database, raising=False)
    monkeypatch.setattr(
        protocol_handlers,
        "strategic_positioning_mgr",
        MagicMock(receive_positioning_proposal_from_fleet=MagicMock(return_value=True)),
        raising=False,
    )
    monkeypatch.setattr(protocol_handlers, "_should_process_message", lambda payload: True)
    monkeypatch.setattr(
        protocol_handlers,
        "_check_timestamp_freshness",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(protocol_handlers, "_is_relayed_message", lambda payload: False)
    monkeypatch.setattr(protocol_handlers, "_relay_message", lambda *args, **kwargs: None)

    result = protocol_handlers.handle_positioning_proposal(PEER_A, payload, plugin)

    assert result == {"result": "continue"}
    logged = [
        call.args[0]
        for call in plugin.log.call_args_list
        if call.args and "Stored positioning proposal" in call.args[0]
    ]
    assert logged
    assert PEER_B[:16] in logged[0]


def test_handle_close_proposal_logs_current_target_keys(monkeypatch):
    plugin = _make_plugin()
    plugin.rpc.checkmessage.return_value = {"verified": True, "pubkey": PEER_A}
    database = MagicMock()
    database.get_member.return_value = {"tier": "member"}
    database.is_banned.return_value = False

    message_bytes = create_close_proposal(
        member_id=PEER_B,
        peer_id=PEER_C,
        channel_id="123x1x1",
        owner_id=PEER_D,
        reason="redundant coverage",
        freed_capacity_sats=7_000_000,
        member_marker_strength=0.12,
        owner_marker_strength=0.91,
        rpc=plugin.rpc,
        our_pubkey=PEER_A,
    )
    payload = _payload_from_message(message_bytes)

    monkeypatch.setattr(protocol_handlers, "database", database, raising=False)
    monkeypatch.setattr(
        protocol_handlers,
        "rationalization_mgr",
        MagicMock(receive_close_proposal_from_fleet=MagicMock(return_value=True)),
        raising=False,
    )
    monkeypatch.setattr(
        protocol_handlers,
        "_check_timestamp_freshness",
        lambda *args, **kwargs: True,
    )

    result = protocol_handlers.handle_close_proposal(PEER_A, payload, plugin)

    assert result == {"result": "continue"}
    logged = [
        call.args[0]
        for call in plugin.log.call_args_list
        if call.args and "Stored close proposal" in call.args[0]
    ]
    assert logged
    assert PEER_B[:16] in logged[0]
    assert PEER_C[:16] in logged[0]
