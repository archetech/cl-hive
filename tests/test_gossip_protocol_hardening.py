"""
Hardening tests for the strict v2 gossip/state-sync protocol helpers.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import modules.protocol as protocol


def test_full_sync_states_hash_v2_changes_when_state_contents_change():
    base_states = [
        {
            "peer_id": "02" + "a" * 64,
            "version": 1,
            "timestamp": 1711200100,
            "topology": ["03" + "b" * 64, "02" + "c" * 64],
            "addresses": ["10.0.0.1:9735"],
            "capabilities": ["mcf"],
        }
    ]
    changed_states = [
        {
            "peer_id": "02" + "a" * 64,
            "version": 1,
            "timestamp": 1711200100,
            "topology": ["03" + "b" * 64, "02" + "c" * 64],
            "addresses": ["10.0.0.2:9735"],
            "capabilities": ["mcf"],
        }
    ]

    base = protocol.compute_full_sync_states_hash_v2(base_states)
    changed = protocol.compute_full_sync_states_hash_v2(changed_states)

    assert base != changed


def test_full_sync_states_hash_v2_fails_closed_on_malformed_state():
    base_states = [
        {
            "peer_id": "02" + "a" * 64,
            "version": 1,
            "timestamp": 1711200100,
            "topology": ["03" + "b" * 64],
            "addresses": ["10.0.0.1:9735"],
            "capabilities": ["mcf"],
        }
    ]
    malformed_states = base_states + [123]

    base = protocol.compute_full_sync_states_hash_v2(base_states)

    assert base
    with pytest.raises(ValueError):
        protocol.compute_full_sync_states_hash_v2(malformed_states)


def test_full_sync_members_hash_v2_fails_closed_on_malformed_member():
    base_members = [
        {
            "peer_id": "02" + "a" * 64,
            "tier": "member",
            "joined_at": 1711200200,
            "addresses": ["10.0.0.1:9735"],
            "capabilities": ["mcf"],
        }
    ]
    malformed_members = base_members + [123]

    base = protocol.compute_full_sync_members_hash_v2(base_members)

    assert base
    with pytest.raises(ValueError):
        protocol.compute_full_sync_members_hash_v2(malformed_members)


def test_full_sync_signing_payload_v2_fails_closed_on_malformed_content():
    payload = {
        "sender_id": "02" + "f" * 64,
        "fleet_hash": "9" * 64,
        "timestamp": 1711200300,
        "states": [
            {
                "peer_id": "02" + "a" * 64,
                "version": 1,
                "timestamp": 1711200100,
                "topology": ["03" + "b" * 64],
                "addresses": ["10.0.0.1:9735"],
                "capabilities": ["mcf"],
            }
        ],
        "members": [
            {
                "peer_id": "02" + "c" * 64,
                "tier": "member",
                "joined_at": 1711200200,
                "addresses": ["10.0.0.2:9735"],
                "capabilities": ["mcf"],
            }
        ],
    }
    malformed_payload = dict(
        payload,
        states=payload["states"] + [123],
        members=payload["members"] + [123],
    )

    base = protocol.get_full_sync_signing_payload_v2(payload)

    assert base
    with pytest.raises(ValueError):
        protocol.get_full_sync_signing_payload_v2(malformed_payload)


def test_full_sync_v2_helpers_are_order_insensitive_for_normalized_rows():
    payload = {
        "sender_id": "02" + "f" * 64,
        "fleet_hash": "9" * 64,
        "timestamp": 1711200300,
        "states": [
            {
                "peer_id": "02" + "a" * 64,
                "version": 1,
                "timestamp": 1711200100,
                "topology": ["03" + "b" * 64, "02" + "c" * 64],
                "addresses": ["10.0.0.2:9735", "10.0.0.1:9735"],
                "capabilities": ["beta", "alpha"],
            },
            {
                "peer_id": "02" + "d" * 64,
                "version": 2,
                "timestamp": 1711200200,
                "topology": ["03" + "e" * 64],
                "addresses": ["10.0.0.4:9735", "10.0.0.3:9735"],
                "capabilities": ["mcf"],
            },
        ],
        "members": [
            {
                "peer_id": "02" + "c" * 64,
                "tier": "member",
                "joined_at": 1711200200,
                "addresses": ["10.0.0.2:9735", "10.0.0.1:9735"],
                "capabilities": ["mcf", "alpha"],
            },
            {
                "peer_id": "02" + "b" * 64,
                "tier": "admin",
                "joined_at": 1711200100,
                "addresses": ["10.0.0.4:9735", "10.0.0.3:9735"],
                "capabilities": ["beta"],
            },
        ],
    }
    reordered_payload = dict(
        payload,
        states=list(reversed(payload["states"])),
        members=list(reversed(payload["members"])),
    )

    assert protocol.compute_full_sync_states_hash_v2(payload["states"]) == protocol.compute_full_sync_states_hash_v2(reordered_payload["states"])
    assert protocol.compute_full_sync_members_hash_v2(payload["members"]) == protocol.compute_full_sync_members_hash_v2(reordered_payload["members"])
    assert protocol.get_full_sync_signing_payload_v2(payload) == protocol.get_full_sync_signing_payload_v2(reordered_payload)


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"_envelope_version": 2}, True),
        ({"_envelope_version": 1}, False),
        ({}, False),
    ],
)
def test_state_sync_messages_require_envelope_version_2(payload, expected):
    assert protocol.is_strict_state_sync_payload(payload) is expected


def test_state_sync_messages_reject_non_dict_payloads():
    assert protocol.is_strict_state_sync_payload(None) is False
    assert protocol.is_strict_state_sync_payload("not a payload") is False
