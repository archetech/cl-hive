"""
Tests for Issue #28: Multi-node crypto integration test

Verifies that signmessage output from one node can be verified by another
using checkmessage. This is critical for the Hive PKI authentication.

Test Coverage:
- Cross-node signature verification
- Full ticket verification flow (Genesis → Invite → Join)
- Manifest/nonce challenge-response protocol

Requirements:
- Requires either:
  1. Two running CLN nodes on regtest (for integration mode)
  2. Falls back to mock-based verification (for unit test mode)

Author: Lightning Goats Team
"""

import pytest
import json
import time
import secrets
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.handshake import (
    HandshakeManager, Ticket, Manifest, Requirements,
    NONCE_SIZE, CHALLENGE_TTL_SECONDS
)

# Try to import pyln for integration tests
try:
    from pyln.client import Plugin
    from pyln.testing.fixtures import *  # noqa: F401, F403
    PYLN_AVAILABLE = True
except ImportError:
    PYLN_AVAILABLE = False


# =============================================================================
# MOCK RPC IMPLEMENTATION
# =============================================================================

class MockSignature:
    """
    Mock signature implementation for testing.

    In production, signatures come from CLN's HSM via signmessage/checkmessage.
    This mock simulates the same behavior deterministically.
    """

    @staticmethod
    def sign(message: str, pubkey: str) -> str:
        """Generate a deterministic mock signature."""
        import hashlib
        # Create a deterministic "signature" based on message + pubkey
        data = f"{pubkey}:{message}".encode()
        sig_hash = hashlib.sha256(data).hexdigest()
        return f"mock_sig_{sig_hash[:32]}"

    @staticmethod
    def verify(message: str, signature: str, expected_pubkey: str) -> dict:
        """Verify a mock signature."""
        expected_sig = MockSignature.sign(message, expected_pubkey)

        if signature == expected_sig:
            return {"verified": True, "pubkey": expected_pubkey}

        # Check if it was signed by a different key
        # In mock mode, we can't determine the actual signer if verification fails
        return {"verified": False}


class MockRpcProxy:
    """
    Mock RPC proxy that simulates CLN signmessage/checkmessage.

    Uses deterministic signatures for testing without HSM.
    """

    def __init__(self, our_pubkey: str):
        self._our_pubkey = our_pubkey
        self._known_signatures = {}  # Store signer info for verification

    def getinfo(self) -> dict:
        return {"id": self._our_pubkey}

    def signmessage(self, message: str) -> dict:
        """Sign a message with our key."""
        signature = MockSignature.sign(message, self._our_pubkey)
        # Store the signer so checkmessage can find it
        self._known_signatures[signature] = self._our_pubkey
        return {"signature": signature, "zbase": signature}

    def checkmessage(self, message: str, signature: str, pubkey: str = None) -> dict:
        """
        Verify a signature.

        If pubkey is provided, verify against that specific key.
        Otherwise, try to determine the signer.
        """
        if pubkey:
            result = MockSignature.verify(message, signature, pubkey)
            return result

        # Try to find the signer from our records
        if signature in self._known_signatures:
            signer = self._known_signatures[signature]
            result = MockSignature.verify(message, signature, signer)
            return result

        return {"verified": False}


class SharedSignatureRegistry:
    """
    Shared registry for cross-node signature verification.

    In the real world, signatures are mathematically verifiable by anyone.
    This simulates that by sharing signature → signer mappings.
    """
    _signatures = {}

    @classmethod
    def register(cls, signature: str, signer: str):
        cls._signatures[signature] = signer

    @classmethod
    def get_signer(cls, signature: str) -> str:
        return cls._signatures.get(signature)

    @classmethod
    def clear(cls):
        cls._signatures.clear()


class CrossNodeMockRpcProxy(MockRpcProxy):
    """
    Mock RPC that uses a shared registry for cross-node verification.
    """

    def signmessage(self, message: str) -> dict:
        signature = MockSignature.sign(message, self._our_pubkey)
        # Register globally so other nodes can verify
        SharedSignatureRegistry.register(signature, self._our_pubkey)
        return {"signature": signature, "zbase": signature}

    def checkmessage(self, message: str, signature: str, pubkey: str = None) -> dict:
        """
        Verify a signature.

        Uses shared registry to find the actual signer.
        """
        # First, check if we're verifying against a specific pubkey
        if pubkey:
            result = MockSignature.verify(message, signature, pubkey)
            return result

        # Try to find the signer from shared registry
        signer = SharedSignatureRegistry.get_signer(signature)
        if signer:
            result = MockSignature.verify(message, signature, signer)
            return result

        return {"verified": False}


# =============================================================================
# MOCK DATABASE
# =============================================================================

class MockDatabase:
    """In-memory mock database for testing."""

    def __init__(self):
        self._members = {}

    def add_member(self, peer_id: str, tier: str = 'member',
                   joined_at: int = None, promoted_at: int = None) -> bool:
        if peer_id in self._members:
            return False
        self._members[peer_id] = {
            'peer_id': peer_id,
            'tier': tier,
            'joined_at': joined_at or int(time.time()),
            'promoted_at': promoted_at,
            'metadata': '{}'
        }
        return True

    def get_member(self, peer_id: str):
        return self._members.get(peer_id)

    def update_member(self, peer_id: str, **kwargs):
        if peer_id not in self._members:
            return False
        self._members[peer_id].update(kwargs)
        return True

    def clear(self):
        self._members.clear()


class MockPlugin:
    """Mock plugin for logging."""

    def __init__(self):
        self.logs = []

    def log(self, message: str, level: str = 'info'):
        self.logs.append((level, message))


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture(autouse=True)
def clear_registry():
    """Clear shared signature registry before each test."""
    SharedSignatureRegistry.clear()
    yield
    SharedSignatureRegistry.clear()


@pytest.fixture
def node_a_pubkey():
    """Generate a consistent pubkey for Node A."""
    return "02" + "a" * 64


@pytest.fixture
def node_b_pubkey():
    """Generate a consistent pubkey for Node B."""
    return "02" + "b" * 64


@pytest.fixture
def node_a_rpc(node_a_pubkey):
    """Create mock RPC for Node A."""
    return CrossNodeMockRpcProxy(node_a_pubkey)


@pytest.fixture
def node_b_rpc(node_b_pubkey):
    """Create mock RPC for Node B."""
    return CrossNodeMockRpcProxy(node_b_pubkey)


@pytest.fixture
def node_a_db():
    """Create mock database for Node A."""
    return MockDatabase()


@pytest.fixture
def node_b_db():
    """Create mock database for Node B."""
    return MockDatabase()


@pytest.fixture
def node_a_plugin():
    """Create mock plugin for Node A."""
    return MockPlugin()


@pytest.fixture
def node_b_plugin():
    """Create mock plugin for Node B."""
    return MockPlugin()


@pytest.fixture
def node_a_handshake(node_a_rpc, node_a_db, node_a_plugin):
    """Create HandshakeManager for Node A."""
    return HandshakeManager(node_a_rpc, node_a_db, node_a_plugin)


@pytest.fixture
def node_b_handshake(node_b_rpc, node_b_db, node_b_plugin):
    """Create HandshakeManager for Node B."""
    return HandshakeManager(node_b_rpc, node_b_db, node_b_plugin)


# =============================================================================
# CROSS-NODE SIGNATURE VERIFICATION TESTS
# =============================================================================

class TestCrossNodeSignatureVerification:
    """Test that signatures from one node can be verified by another."""

    def test_basic_cross_node_signature(self, node_a_rpc, node_b_rpc, node_a_pubkey):
        """Node B should be able to verify a signature created by Node A."""
        message = "Hello from Node A"

        # Node A signs the message
        sig_result = node_a_rpc.signmessage(message)
        signature = sig_result['signature']

        # Node B verifies the signature
        verify_result = node_b_rpc.checkmessage(message, signature, node_a_pubkey)

        assert verify_result['verified'] is True
        assert verify_result['pubkey'] == node_a_pubkey

    def test_signature_pubkey_mismatch(self, node_a_rpc, node_b_rpc,
                                        node_a_pubkey, node_b_pubkey):
        """Signature verification should fail if pubkey doesn't match signer."""
        message = "Hello from Node A"

        # Node A signs
        sig_result = node_a_rpc.signmessage(message)
        signature = sig_result['signature']

        # Node B tries to verify with wrong pubkey (its own)
        verify_result = node_b_rpc.checkmessage(message, signature, node_b_pubkey)

        assert verify_result['verified'] is False

    def test_modified_message_fails_verification(self, node_a_rpc, node_b_rpc,
                                                   node_a_pubkey):
        """Signature should be invalid if message is modified."""
        original_message = "Original message"
        modified_message = "Modified message"

        # Node A signs the original
        sig_result = node_a_rpc.signmessage(original_message)
        signature = sig_result['signature']

        # Node B tries to verify with modified message
        verify_result = node_b_rpc.checkmessage(modified_message, signature, node_a_pubkey)

        assert verify_result['verified'] is False

    def test_json_message_signature(self, node_a_rpc, node_b_rpc, node_a_pubkey):
        """Test signing and verifying JSON-structured messages (like tickets)."""
        ticket_data = {
            "admin_pubkey": node_a_pubkey,
            "hive_id": "test_hive",
            "requirements": 0,
            "issued_at": int(time.time()),
            "expires_at": int(time.time()) + 3600
        }

        # Serialize consistently (important for signature verification)
        message = json.dumps(ticket_data, sort_keys=True, separators=(',', ':'))

        # Node A signs
        sig_result = node_a_rpc.signmessage(message)
        signature = sig_result['signature']

        # Node B verifies
        verify_result = node_b_rpc.checkmessage(message, signature, node_a_pubkey)

        assert verify_result['verified'] is True


# =============================================================================
# TICKET VERIFICATION FLOW TESTS
# =============================================================================

class TestTicketVerificationFlow:
    """Test the full Genesis → Invite → Join ticket flow."""

    def test_genesis_creates_valid_ticket(self, node_a_handshake, node_a_pubkey):
        """Genesis should create a self-signed, verifiable ticket."""
        result = node_a_handshake.genesis(hive_id="test_hive")

        assert result['status'] == 'genesis_complete'
        assert result['hive_id'] == 'test_hive'
        assert result['admin_pubkey'] == node_a_pubkey

        # Verify the genesis ticket
        is_valid, ticket, error = node_a_handshake.verify_ticket(result['genesis_ticket'])
        assert is_valid is True
        assert ticket.admin_pubkey == node_a_pubkey
        assert ticket.hive_id == 'test_hive'

    def test_admin_can_generate_invite_ticket(self, node_a_handshake):
        """Admin should be able to generate invite tickets."""
        # First, become admin via genesis
        node_a_handshake.genesis(hive_id="test_hive")

        # Generate invite ticket
        invite_b64 = node_a_handshake.generate_invite_ticket(valid_hours=24)

        # Verify the invite ticket
        is_valid, ticket, error = node_a_handshake.verify_ticket(invite_b64)
        assert is_valid is True
        assert ticket.hive_id == 'test_hive'

    def test_node_b_can_verify_node_a_ticket(self, node_a_handshake, node_b_handshake,
                                              node_a_pubkey, node_b_db):
        """Node B should be able to verify a ticket issued by Node A."""
        # Node A creates Hive and generates invite
        node_a_handshake.genesis(hive_id="cross_node_hive")
        invite_b64 = node_a_handshake.generate_invite_ticket(valid_hours=24)

        # Node B needs to know Node A is a member (simulating gossip/sync)
        node_b_db.add_member(node_a_pubkey, tier='member')

        # Node B verifies the ticket
        is_valid, ticket, error = node_b_handshake.verify_ticket(invite_b64)

        assert is_valid is True, f"Verification failed: {error}"
        assert ticket.admin_pubkey == node_a_pubkey
        assert ticket.hive_id == 'cross_node_hive'

    def test_expired_ticket_rejected(self, node_a_handshake, node_b_handshake,
                                      node_a_pubkey, node_b_db):
        """Expired tickets should be rejected."""
        # Node A creates Hive
        node_a_handshake.genesis(hive_id="expiry_test")

        # Generate a ticket that expires in -1 hours (already expired)
        # We need to manually create an expired ticket
        ticket_data = {
            "admin_pubkey": node_a_pubkey,
            "hive_id": "expiry_test",
            "requirements": Requirements.NONE,
            "issued_at": int(time.time()) - 7200,  # 2 hours ago
            "expires_at": int(time.time()) - 3600,  # 1 hour ago (expired)
        }

        ticket_json = json.dumps(ticket_data, sort_keys=True, separators=(',', ':'))
        sig_result = node_a_handshake.rpc.signmessage(ticket_json)

        expired_ticket = Ticket(**ticket_data, signature=sig_result['signature'])
        invite_b64 = expired_ticket.to_base64()

        # Node B tries to verify
        node_b_db.add_member(node_a_pubkey, tier='member')
        is_valid, ticket, error = node_b_handshake.verify_ticket(invite_b64)

        assert is_valid is False
        assert "expired" in error.lower()


# =============================================================================
# MANIFEST/NONCE CHALLENGE-RESPONSE TESTS
# =============================================================================

class TestChallengeResponseProtocol:
    """Test the manifest/nonce challenge-response protocol."""

    def test_generate_challenge_returns_nonce(self, node_a_handshake, node_b_pubkey):
        """Generating a challenge should return a random nonce."""
        nonce = node_a_handshake.generate_challenge(node_b_pubkey, Requirements.NONE)

        assert len(nonce) == NONCE_SIZE * 2  # Hex string (32 bytes = 64 hex chars)
        assert all(c in '0123456789abcdef' for c in nonce.lower())

    def test_manifest_includes_nonce(self, node_b_handshake):
        """Manifest should include the challenge nonce."""
        nonce = secrets.token_hex(NONCE_SIZE)

        manifest_data = node_b_handshake.create_manifest(nonce)

        assert 'manifest' in manifest_data
        assert manifest_data['manifest']['nonce'] == nonce
        assert 'nonce_signature' in manifest_data
        assert 'manifest_signature' in manifest_data

    def test_manifest_nonce_signature_verifiable(self, node_a_handshake, node_b_handshake,
                                                   node_b_pubkey, node_b_rpc):
        """Node A should be able to verify Node B's nonce signature."""
        # Node A creates challenge
        nonce = secrets.token_hex(NONCE_SIZE)

        # Node B creates manifest with that nonce
        manifest_data = node_b_handshake.create_manifest(nonce)

        # Node A verifies the nonce signature
        nonce_sig = manifest_data['nonce_signature']

        # Use Node A's RPC to verify (simulates cross-node verification)
        verify_result = node_a_handshake.rpc.checkmessage(nonce, nonce_sig, node_b_pubkey)

        assert verify_result['verified'] is True

    def test_manifest_signature_verifiable(self, node_a_handshake, node_b_handshake,
                                            node_b_pubkey):
        """Node A should be able to verify Node B's manifest signature."""
        nonce = secrets.token_hex(NONCE_SIZE)

        # Node B creates manifest
        manifest_data = node_b_handshake.create_manifest(nonce)

        # Reconstruct manifest JSON (must match exactly)
        manifest_obj = Manifest(**manifest_data['manifest'])
        manifest_json = manifest_obj.to_json()

        # Node A verifies manifest signature
        manifest_sig = manifest_data['manifest_signature']
        verify_result = node_a_handshake.rpc.checkmessage(manifest_json, manifest_sig, node_b_pubkey)

        assert verify_result['verified'] is True

    def test_full_challenge_response_flow(self, node_a_handshake, node_b_handshake,
                                           node_a_pubkey, node_b_pubkey,
                                           node_a_db, node_b_db):
        """Test the complete challenge-response flow between two nodes."""
        # Setup: Node A is admin of a Hive
        node_a_handshake.genesis(hive_id="challenge_test")
        invite_b64 = node_a_handshake.generate_invite_ticket()

        # Node B presents ticket (HELLO)
        # Node B would decode the ticket and present it
        ticket = Ticket.from_base64(invite_b64)

        # Node A creates challenge (CHALLENGE)
        nonce = node_a_handshake.generate_challenge(node_b_pubkey, ticket.requirements)

        # Node B creates manifest (ATTEST)
        manifest_data = node_b_handshake.create_manifest(nonce)

        # Node A verifies both signatures
        # 1. Verify nonce signature
        nonce_verify = node_a_handshake.rpc.checkmessage(
            nonce,
            manifest_data['nonce_signature'],
            node_b_pubkey
        )
        assert nonce_verify['verified'] is True

        # 2. Verify manifest signature
        manifest_obj = Manifest(**manifest_data['manifest'])
        manifest_verify = node_a_handshake.rpc.checkmessage(
            manifest_obj.to_json(),
            manifest_data['manifest_signature'],
            node_b_pubkey
        )
        assert manifest_verify['verified'] is True

        # 3. Verify nonce in manifest matches challenge
        assert manifest_data['manifest']['nonce'] == nonce

        # All verifications passed - Node B is authenticated


# =============================================================================
# SECURITY TESTS
# =============================================================================

class TestCryptoSecurityProperties:
    """Test security properties of the crypto implementation."""

    def test_signature_not_transferable(self, node_a_rpc, node_b_rpc,
                                         node_a_pubkey, node_b_pubkey):
        """A signature for one message should not work for another."""
        message1 = "Transfer 100 sats to Alice"
        message2 = "Transfer 100000 sats to Mallory"

        # Node A signs message1
        sig = node_a_rpc.signmessage(message1)['signature']

        # Signature should not verify for message2
        verify = node_b_rpc.checkmessage(message2, sig, node_a_pubkey)
        assert verify['verified'] is False

    def test_signature_not_forgeable(self, node_a_rpc, node_b_rpc,
                                      node_a_pubkey, node_b_pubkey):
        """Node B should not be able to create a signature that verifies as Node A."""
        message = "Node A approves this"

        # Node B signs (as itself)
        sig = node_b_rpc.signmessage(message)['signature']

        # Should not verify as Node A
        verify = node_a_rpc.checkmessage(message, sig, node_a_pubkey)
        assert verify['verified'] is False

    def test_nonce_replay_protection(self, node_a_handshake):
        """Each challenge should have a unique nonce."""
        nonces = set()

        # Generate 100 challenges for different peers
        # (same peer would hit rate limiting)
        for i in range(100):
            peer_id = f"02{i:064x}"[:66]  # Generate unique peer IDs
            nonce = node_a_handshake.generate_challenge(peer_id, Requirements.NONE)
            nonces.add(nonce)

        # All nonces should be unique
        assert len(nonces) == 100


# =============================================================================
# INTEGRATION TEST (requires real CLN nodes)
# =============================================================================

@pytest.mark.skipif(not PYLN_AVAILABLE, reason="pyln not available")
class TestRealCLNIntegration:
    """
    Integration tests with real CLN nodes.

    These tests require a regtest environment with at least 2 CLN nodes.
    """

    @pytest.mark.skip(reason="Requires regtest CLN setup")
    def test_real_signmessage_checkmessage(self, node_factory):
        """Test signmessage/checkmessage with real CLN nodes."""
        # This would use pyln.testing.fixtures.node_factory
        # to spin up actual CLN nodes on regtest

        # Example (not runnable without proper setup):
        # l1, l2 = node_factory.get_nodes(2)
        #
        # message = "Hello from L1"
        # sig = l1.rpc.signmessage(message)['signature']
        #
        # verify = l2.rpc.checkmessage(message, sig)
        # assert verify['verified'] is True
        # assert verify['pubkey'] == l1.info['id']
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
