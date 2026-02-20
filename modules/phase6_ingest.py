"""
Phase 6 injected-packet parsing helpers.

These helpers normalize payloads forwarded from cl-hive-comms into
Hive protocol tuples that cl-hive can dispatch through existing handlers.
"""

import json
from typing import Any, Dict, Optional, Tuple

from modules.protocol import HiveMessageType, deserialize


def coerce_hive_message_type(value: Any) -> Optional[HiveMessageType]:
    """Best-effort conversion from mixed type identifiers to HiveMessageType."""
    if isinstance(value, HiveMessageType):
        return value

    if isinstance(value, int):
        try:
            return HiveMessageType(value)
        except Exception:
            return None

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None

        try:
            return HiveMessageType(int(raw))
        except Exception:
            pass

        # Accept names like "gossip" or "HiveMessageType.GOSSIP"
        name = raw.split(".")[-1].upper()
        try:
            return HiveMessageType[name]
        except Exception:
            return None

    return None


def parse_injected_hive_packet(
    packet: Dict[str, Any],
) -> Tuple[str, Optional[HiveMessageType], Optional[Dict[str, Any]]]:
    """
    Parse an injected packet from comms into (peer_id, msg_type, msg_payload).

    Supported forms:
    1) {"type": <int|name>, "version": <int>, "payload": {...}, "sender": "..."}
    2) {"msg_type": <int|name>, "msg_payload": {...}, "sender": "..."}
    3) {"raw_plaintext": "<hex or json envelope>", "sender": "..."}
    """
    if not isinstance(packet, dict):
        return "", None, None

    peer_id = str(packet.get("sender") or packet.get("peer_id") or packet.get("pubkey") or "")

    # Canonical envelope from protocol.serialize() JSON form
    if "type" in packet and isinstance(packet.get("payload"), dict):
        msg_type = coerce_hive_message_type(packet.get("type"))
        if msg_type is not None:
            msg_payload = dict(packet.get("payload") or {})
            version = packet.get("version")
            if isinstance(version, int):
                msg_payload["_envelope_version"] = version
            return peer_id, msg_type, msg_payload

    # Explicit aliases
    msg_type_raw = (
        packet.get("msg_type")
        or packet.get("message_type")
        or packet.get("hive_message_type")
    )
    msg_payload_raw = packet.get("msg_payload")
    if msg_payload_raw is None:
        msg_payload_raw = packet.get("message_payload")
    if msg_payload_raw is None and isinstance(packet.get("payload"), dict):
        msg_payload_raw = packet.get("payload")

    msg_type = coerce_hive_message_type(msg_type_raw)
    if msg_type is not None and isinstance(msg_payload_raw, dict):
        return peer_id, msg_type, dict(msg_payload_raw)

    # Raw transport path (used when comms receives non-JSON plaintext)
    raw_plaintext = packet.get("raw_plaintext")
    if isinstance(raw_plaintext, str) and raw_plaintext:
        # If raw plaintext is itself JSON, recurse on parsed object
        try:
            parsed = json.loads(raw_plaintext)
            if isinstance(parsed, dict):
                if "sender" not in parsed and peer_id:
                    parsed["sender"] = peer_id
                return parse_injected_hive_packet(parsed)
        except Exception:
            pass

        data = None
        try:
            data = bytes.fromhex(raw_plaintext)
        except Exception:
            if raw_plaintext.startswith("HIVE"):
                data = raw_plaintext.encode("utf-8")

        if data is not None:
            msg_type, msg_payload = deserialize(data)
            if msg_type is not None and isinstance(msg_payload, dict):
                return peer_id, msg_type, msg_payload

    return peer_id, None, None
