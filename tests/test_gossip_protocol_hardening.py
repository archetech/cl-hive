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
