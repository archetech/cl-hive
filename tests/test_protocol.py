"""
Unit tests for cl-hive protocol layer.

Tests:
1. Magic Byte Verification - Non-HIVE messages are ignored
2. Round Trip - Serialize -> Deserialize preserves data
3. Message Types - All MVP message types are handled
4. Serialization round-trip tests

Run with: pytest tests/test_protocol.py -v
"""

import pytest
import time
import json
from unittest.mock import Mock, MagicMock

# Import modules under test
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import modules.protocol as protocol

from modules.protocol import (
    HIVE_MAGIC,
    HiveMessageType,
    PROTOCOL_VERSION,
    serialize,
    deserialize,
    is_hive_message,
    create_hello,
    create_challenge,
    create_attest,
    create_welcome
)

from modules.handshake import (
    Manifest,
    Requirements,
    NONCE_SIZE
)


# =============================================================================
# MAGIC BYTE TESTS
# =============================================================================

class TestMagicBytes:
    """Test magic byte verification (Peek & Check)."""
    
    def test_valid_magic_prefix(self):
        """Messages with HIVE magic should be recognized."""
        data = HIVE_MAGIC + b'{"type":32769}'
        assert is_hive_message(data) is True
    
    def test_invalid_magic_prefix(self):
        """Messages without HIVE magic should be rejected."""
        data = b'FAKE{"type":32769}'
        assert is_hive_message(data) is False
    
    def test_empty_message(self):
        """Empty messages should be rejected."""
        assert is_hive_message(b'') is False
    
    def test_short_message(self):
        """Messages shorter than 4 bytes should be rejected."""
        assert is_hive_message(b'HIV') is False
        assert is_hive_message(b'HI') is False
        assert is_hive_message(b'H') is False
    
    def test_only_magic_no_payload(self):
        """Message with only magic but no payload should still pass magic check."""
        assert is_hive_message(HIVE_MAGIC) is True
    
    def test_other_plugin_message(self):
        """Messages from other plugins should be passed through."""
        # Simulate a message from another plugin using experimental range
        other_plugin_msg = b'BOLT' + b'{"type":32800}'
        assert is_hive_message(other_plugin_msg) is False


# =============================================================================
# SERIALIZATION ROUND-TRIP TESTS
# =============================================================================

class TestSerialization:
    """Test serialize/deserialize round-trip."""
    
    def test_hello_round_trip(self):
        """HELLO message should survive serialize -> deserialize."""
        original_payload = {"pubkey": "02abcdef1234", "protocol_version": 1}

        data = serialize(HiveMessageType.HELLO, original_payload)
        msg_type, payload = deserialize(data)

        assert msg_type == HiveMessageType.HELLO
        assert payload['pubkey'] == original_payload['pubkey']
        assert payload['protocol_version'] == original_payload['protocol_version']
    
    def test_challenge_round_trip(self):
        """CHALLENGE message should survive serialize -> deserialize."""
        original_payload = {"nonce": "a" * 64, "hive_id": "hive_12345"}
        
        data = serialize(HiveMessageType.CHALLENGE, original_payload)
        msg_type, payload = deserialize(data)
        
        assert msg_type == HiveMessageType.CHALLENGE
        assert payload['nonce'] == original_payload['nonce']
        assert payload['hive_id'] == original_payload['hive_id']
    
    def test_attest_round_trip(self):
        """ATTEST message should survive serialize -> deserialize."""
        original_payload = {
            "pubkey": "02" + "a" * 64,
            "version": "cl-hive v0.1.0",
            "features": ["splice", "dual-fund"],
            "nonce_signature": "sig1",
            "manifest_signature": "sig2"
        }
        
        data = serialize(HiveMessageType.ATTEST, original_payload)
        msg_type, payload = deserialize(data)
        
        assert msg_type == HiveMessageType.ATTEST
        assert payload['pubkey'] == original_payload['pubkey']
        assert payload['features'] == original_payload['features']
    
    def test_welcome_round_trip(self):
        """WELCOME message should survive serialize -> deserialize."""
        original_payload = {
            "hive_id": "hive_test",
            "tier": "member",
            "member_count": 5,
            "state_hash": "0" * 64
        }

        data = serialize(HiveMessageType.WELCOME, original_payload)
        msg_type, payload = deserialize(data)

        assert msg_type == HiveMessageType.WELCOME
        assert payload['tier'] == "member"
        assert payload['member_count'] == 5
    
    def test_complex_payload(self):
        """Complex nested payloads should serialize correctly."""
        original_payload = {
            "simple": "string",
            "number": 12345,
            "float": 3.14159,
            "nested": {"key": "value", "list": [1, 2, 3]},
            "unicode": "こんにちは",
        }
        
        data = serialize(HiveMessageType.HELLO, original_payload)
        msg_type, payload = deserialize(data)
        
        assert payload['nested']['key'] == "value"
        assert payload['nested']['list'] == [1, 2, 3]
        assert payload['unicode'] == "こんにちは"
    
    def test_deserialize_invalid_json(self):
        """Invalid JSON after magic should return None."""
        data = HIVE_MAGIC + b'not valid json'
        msg_type, payload = deserialize(data)
        
        assert msg_type is None
        assert payload is None
    
    def test_deserialize_missing_type(self):
        """JSON without 'type' field should return None."""
        data = HIVE_MAGIC + b'{"payload": "data"}'
        msg_type, payload = deserialize(data)
        
        assert msg_type is None
        assert payload is None


# =============================================================================
# MESSAGE HELPER TESTS
# =============================================================================

class TestMessageHelpers:
    """Test convenience functions for creating messages."""
    
    def test_create_hello(self):
        """create_hello should produce valid HELLO message."""
        pubkey = "02" + "a" * 64
        data = create_hello(pubkey)

        assert data[:4] == HIVE_MAGIC
        msg_type, payload = deserialize(data)
        assert msg_type == HiveMessageType.HELLO
        assert payload['pubkey'] == pubkey
        assert payload['protocol_version'] == PROTOCOL_VERSION
    
    def test_create_challenge(self):
        """create_challenge should produce valid CHALLENGE message."""
        nonce = "deadbeef" * 8
        data = create_challenge(nonce, "hive_abc")
        
        msg_type, payload = deserialize(data)
        assert msg_type == HiveMessageType.CHALLENGE
        assert payload['nonce'] == nonce
        assert payload['hive_id'] == "hive_abc"
    
    def test_create_attest(self):
        """create_attest should produce valid ATTEST message."""
        manifest = {
            "pubkey": "02" + "a" * 64,
            "version": "v1.0",
            "features": ["splice"],
            "timestamp": 1234567890,
            "nonce": "deadbeef" * 8
        }
        data = create_attest(
            pubkey="02" + "a" * 64,
            version="v1.0",
            features=["splice"],
            nonce_signature="nsig",
            manifest_signature="msig",
            manifest=manifest
        )
        
        msg_type, payload = deserialize(data)
        assert msg_type == HiveMessageType.ATTEST
        assert "splice" in payload['features']
    
    def test_create_welcome(self):
        """create_welcome should produce valid WELCOME message."""
        data = create_welcome("hive_xyz", "member", 10, "hash123")
        
        msg_type, payload = deserialize(data)
        assert msg_type == HiveMessageType.WELCOME
        assert payload['tier'] == "member"
        assert payload['member_count'] == 10


# =============================================================================
# REQUIREMENTS BITMASK TESTS
# =============================================================================

class TestRequirements:
    """Test feature requirement bitmasks."""
    
    def test_no_requirements(self):
        """NONE should be zero."""
        assert Requirements.NONE == 0
    
    def test_single_requirement(self):
        """Single requirements should be powers of 2."""
        assert Requirements.SPLICE == 1
        assert Requirements.DUAL_FUND == 2
        assert Requirements.ANCHOR == 4
        assert Requirements.ONION_MSG == 8
    
    def test_combined_requirements(self):
        """Combined requirements should use bitwise OR."""
        combined = Requirements.SPLICE | Requirements.DUAL_FUND
        
        assert combined & Requirements.SPLICE
        assert combined & Requirements.DUAL_FUND
        assert not (combined & Requirements.ANCHOR)
    
    def test_all_requirements(self):
        """All requirements combined."""
        all_reqs = (Requirements.SPLICE | Requirements.DUAL_FUND | 
                    Requirements.ANCHOR | Requirements.ONION_MSG)
        
        assert all_reqs == 15  # 1 + 2 + 4 + 8


# =============================================================================
# MANIFEST TESTS
# =============================================================================

class TestManifest:
    """Test manifest structure."""
    
    def test_manifest_to_json(self):
        """Manifest JSON should be deterministic (sorted keys)."""
        manifest = Manifest(
            pubkey="02" + "f" * 64,
            version="v1.0",
            features=["splice", "dual-fund"],
            timestamp=1234567890,
            nonce="abc123"
        )
        
        json1 = manifest.to_json()
        json2 = manifest.to_json()
        
        assert json1 == json2  # Deterministic
        
        parsed = json.loads(json1)
        assert parsed['pubkey'] == manifest.pubkey
        assert parsed['features'] == manifest.features


# =============================================================================
# EDGE CASES
# =============================================================================

class TestEdgeCases:
    """Test edge cases and error handling."""
    
    def test_deserialize_empty_payload(self):
        """Empty payload after magic should handle gracefully."""
        data = HIVE_MAGIC + b''
        msg_type, payload = deserialize(data)
        
        assert msg_type is None
    
    def test_deserialize_invalid_message_type(self):
        """Unknown message type should raise ValueError (caught internally)."""
        # Message type 99999 doesn't exist
        data = HIVE_MAGIC + b'{"type": 99999, "version": 1, "payload": {}}'
        msg_type, payload = deserialize(data)
        
        assert msg_type is None
    
    def test_serialize_special_characters(self):
        """Special characters in payload should be handled."""
        payload = {
            "quotes": 'He said "hello"',
            "newlines": "line1\nline2",
            "backslash": "path\\to\\file",
            "emoji": "🐝⚡"
        }
        
        data = serialize(HiveMessageType.HELLO, payload)
        msg_type, result = deserialize(data)
        
        assert result['emoji'] == "🐝⚡"
        assert result['quotes'] == 'He said "hello"'


class TestSerializeNoneReturn:
    """M-4: Test serialize() returns None for oversized messages."""

    def test_oversized_payload_returns_none(self):
        """Messages exceeding MAX_MESSAGE_BYTES should return None."""
        from modules.protocol import MAX_MESSAGE_BYTES
        # Create a payload large enough to exceed the limit
        huge_payload = {"data": "x" * (MAX_MESSAGE_BYTES + 1000)}
        result = serialize(HiveMessageType.HELLO, huge_payload)
        assert result is None

    def test_normal_payload_returns_bytes(self):
        """Normal-sized messages should return bytes."""
        result = serialize(HiveMessageType.HELLO, {"pubkey": "02" + "aa" * 32})
        assert result is not None
        assert isinstance(result, bytes)

    def test_create_hello_oversized_pubkey(self):
        """create_hello with enormous pubkey should return None."""
        from modules.protocol import MAX_MESSAGE_BYTES
        # A normal pubkey is fine
        normal = create_hello("02" + "aa" * 32)
        assert normal is not None

        # A ridiculously large pubkey should make the message too big
        huge = create_hello("x" * MAX_MESSAGE_BYTES)
        assert huge is None

    def test_callers_handle_none(self):
        """Verify None result doesn't crash .hex() callers."""
        result = serialize(HiveMessageType.HELLO, {"data": "x" * 100000})
        if result is None:
            # This is the pattern callers should use
            assert True
        else:
            # Normal case - can call .hex()
            assert isinstance(result.hex(), str)


class TestProtocolV2Helpers:
    """Focused tests for the strict v2 state-sync helpers."""

    def test_gossip_signing_payload_v2_changes_when_addresses_change(self):
        base_payload = {
            "sender_id": "02" + "a" * 64,
            "timestamp": 1711200000,
            "version": 7,
            "fleet_hash": "f" * 64,
            "capacity_sats": 1000,
            "available_sats": 500,
            "fee_policy": {"base_fee": 1000, "fee_rate": 10},
            "topology": ["03" + "b" * 64, "02" + "c" * 64],
            "addresses": ["10.0.0.1:9735"],
            "capabilities": ["mcf"],
        }
        changed_payload = dict(base_payload, addresses=["10.0.0.2:9735"])

        base = protocol.get_gossip_signing_payload_v2(base_payload)
        changed = protocol.get_gossip_signing_payload_v2(changed_payload)

        assert base != changed

    def test_gossip_signing_payload_v2_is_order_insensitive(self):
        payload = {
            "sender_id": "02" + "a" * 64,
            "timestamp": 1711200000,
            "version": 7,
            "fleet_hash": "f" * 64,
            "capacity_sats": 1000,
            "available_sats": 500,
            "fee_policy": {"base_fee": 1000, "fee_rate": 10},
            "topology": ["03" + "b" * 64, "02" + "c" * 64],
            "addresses": ["10.0.0.2:9735", "10.0.0.1:9735"],
            "capabilities": ["beta", "alpha"],
        }
        reordered = dict(
            payload,
            topology=list(reversed(payload["topology"])),
            addresses=list(reversed(payload["addresses"])),
            capabilities=list(reversed(payload["capabilities"])),
        )

        assert protocol.get_gossip_signing_payload_v2(payload) == protocol.get_gossip_signing_payload_v2(reordered)

    def test_gossip_signing_payload_v2_rejects_non_string_entries(self):
        payload = {
            "sender_id": "02" + "a" * 64,
            "timestamp": 1711200000,
            "version": 7,
            "fleet_hash": "f" * 64,
            "topology": ["03" + "b" * 64],
            "addresses": ["10.0.0.1:9735", 123],
            "capabilities": ["mcf"],
        }

        with pytest.raises(ValueError, match="addresses"):
            protocol.get_gossip_signing_payload_v2(payload)

    def test_gossip_signing_payload_v2_rejects_null_list_fields(self):
        payload = {
            "sender_id": "02" + "a" * 64,
            "timestamp": 1711200000,
            "version": 7,
            "fleet_hash": "f" * 64,
            "topology": ["03" + "b" * 64],
            "addresses": None,
            "capabilities": ["mcf"],
        }

        with pytest.raises(ValueError, match="addresses"):
            protocol.get_gossip_signing_payload_v2(payload)

    def test_state_hash_signing_payload_v2_changes_when_membership_hash_changes(self):
        base_payload = {
            "sender_id": "02" + "d" * 64,
            "fleet_hash": "e" * 64,
            "membership_hash": "1" * 64,
            "timestamp": 1711200001,
            "peer_count": 3,
        }
        changed_payload = dict(base_payload, membership_hash="2" * 64)

        base = protocol.get_state_hash_signing_payload_v2(base_payload)
        changed = protocol.get_state_hash_signing_payload_v2(changed_payload)

        assert base != changed

    def test_full_sync_members_hash_v2_is_order_insensitive(self):
        base_members = [
            {
                "peer_id": "02" + "a" * 64,
                "tier": "member",
                "joined_at": 1711200200,
                "addresses": ["10.0.0.2:9735", "10.0.0.1:9735"],
                "capabilities": ["beta", "alpha"],
            },
            {
                "peer_id": "02" + "b" * 64,
                "tier": "member",
                "joined_at": 1711200100,
                "addresses": ["10.0.0.4:9735", "10.0.0.3:9735"],
                "capabilities": ["mcf"],
            },
        ]
        reordered_members = list(reversed(base_members))

        base = protocol.compute_full_sync_members_hash_v2(base_members)
        reordered = protocol.compute_full_sync_members_hash_v2(reordered_members)

        assert base == reordered

    def test_full_sync_members_hash_v2_changes_when_address_list_contains_non_string(self):
        base_members = [
            {
                "peer_id": "02" + "a" * 64,
                "tier": "member",
                "joined_at": 1711200200,
                "addresses": ["10.0.0.1:9735"],
                "capabilities": ["mcf"],
            }
        ]
        malformed_members = [
            {
                "peer_id": "02" + "a" * 64,
                "tier": "member",
                "joined_at": 1711200200,
                "addresses": ["10.0.0.1:9735", 123],
                "capabilities": ["mcf"],
            }
        ]

        base = protocol.compute_full_sync_members_hash_v2(base_members)

        assert base
        with pytest.raises(ValueError):
            protocol.compute_full_sync_members_hash_v2(malformed_members)

    @pytest.mark.parametrize(
        "member_update, expected_error",
        [
            ({"peer_id": "not-a-pubkey"}, "member.peer_id"),
            ({"tier": "admin"}, "member.tier"),
            ({"joined_at": "invalid"}, "member.joined_at"),
        ],
    )
    def test_full_sync_members_hash_v2_rejects_malformed_member_scalars(
        self,
        member_update,
        expected_error,
    ):
        member = {
            "peer_id": "02" + "a" * 64,
            "tier": "member",
            "joined_at": 1711200200,
            "addresses": ["10.0.0.1:9735"],
            "capabilities": ["mcf"],
        }
        member.update(member_update)

        with pytest.raises(ValueError, match=expected_error):
            protocol.compute_full_sync_members_hash_v2([member])

    def test_full_sync_signing_payload_v2_is_order_insensitive(self):
        payload = {
            "sender_id": "02" + "e" * 64,
            "fleet_hash": "f" * 64,
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
                    "tier": "member",
                    "joined_at": 1711200100,
                    "addresses": ["10.0.0.4:9735", "10.0.0.3:9735"],
                    "capabilities": ["beta"],
                },
            ],
        }
        reordered = dict(
            payload,
            states=list(reversed(payload["states"])),
            members=list(reversed(payload["members"])),
        )

        base = protocol.get_full_sync_signing_payload_v2(payload)
        changed = protocol.get_full_sync_signing_payload_v2(reordered)

        assert base == changed

    @pytest.mark.parametrize(
        "field_name, field_value, expected_error",
        [
            ("states", None, "states must be a list"),
            ("states", "", "states must be a list"),
            ("members", None, "members must be a list"),
            ("members", "", "members must be a list"),
        ],
    )
    def test_full_sync_signing_payload_v2_rejects_null_or_non_list_collections(
        self,
        field_name,
        field_value,
        expected_error,
    ):
        payload = {
            "sender_id": "02" + "e" * 64,
            "fleet_hash": "f" * 64,
            "timestamp": 1711200300,
            "states": [],
            "members": [],
        }
        payload[field_name] = field_value

        with pytest.raises(ValueError, match=expected_error):
            protocol.get_full_sync_signing_payload_v2(payload)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
