"""Tests for Phase 6 injected packet parsing helpers."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.phase6_ingest import coerce_hive_message_type, parse_injected_hive_packet
from modules.protocol import HiveMessageType, serialize


def test_coerce_hive_message_type_accepts_name_and_int():
    assert coerce_hive_message_type("gossip") == HiveMessageType.GOSSIP
    assert coerce_hive_message_type("HiveMessageType.GOSSIP") == HiveMessageType.GOSSIP
    assert coerce_hive_message_type(int(HiveMessageType.GOSSIP)) == HiveMessageType.GOSSIP


def test_parse_injected_packet_with_canonical_envelope():
    packet = {
        "sender": "02" + "a" * 64,
        "type": int(HiveMessageType.HELLO),
        "version": 1,
        "payload": {"ticket": "abc"},
    }
    peer_id, msg_type, payload = parse_injected_hive_packet(packet)
    assert peer_id.startswith("02")
    assert msg_type == HiveMessageType.HELLO
    assert payload["ticket"] == "abc"
    assert payload["_envelope_version"] == 1


def test_parse_injected_packet_with_msg_type_aliases():
    packet = {
        "sender": "peer1",
        "msg_type": "intent",
        "msg_payload": {"request_id": "abcd"},
    }
    peer_id, msg_type, payload = parse_injected_hive_packet(packet)
    assert peer_id == "peer1"
    assert msg_type == HiveMessageType.INTENT
    assert payload["request_id"] == "abcd"


def test_parse_injected_packet_with_raw_hex_wire_message():
    wire = serialize(HiveMessageType.GOSSIP, {"sender": "peer2", "state_hash": "deadbeef"})
    packet = {"sender": "peer2", "raw_plaintext": wire.hex()}
    peer_id, msg_type, payload = parse_injected_hive_packet(packet)
    assert peer_id == "peer2"
    assert msg_type == HiveMessageType.GOSSIP
    assert payload["state_hash"] == "deadbeef"


def test_parse_injected_packet_with_raw_json_envelope_string():
    envelope = {
        "type": int(HiveMessageType.STATE_HASH),
        "version": 1,
        "payload": {"sender": "peer3", "hash": "cafebabe"},
    }
    packet = {"sender": "peer3", "raw_plaintext": json.dumps(envelope)}
    peer_id, msg_type, payload = parse_injected_hive_packet(packet)
    assert peer_id == "peer3"
    assert msg_type == HiveMessageType.STATE_HASH
    assert payload["hash"] == "cafebabe"


def test_parse_injected_packet_returns_none_for_unrecognized_payload():
    peer_id, msg_type, payload = parse_injected_hive_packet({"sender": "peer4", "foo": "bar"})
    assert peer_id == "peer4"
    assert msg_type is None
    assert payload is None
