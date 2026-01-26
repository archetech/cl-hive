"""
Tests for the relay module.

Tests TTL-based message relay with deduplication for non-mesh topologies.
"""

import pytest
import time
from modules.relay import (
    RelayManager, MessageDeduplicator, RelayMetadata,
    DEFAULT_TTL, MAX_RELAY_PATH_LENGTH
)


class TestMessageDeduplicator:
    """Tests for MessageDeduplicator."""

    def test_first_message_not_duplicate(self):
        """First time seeing a message should return False (not duplicate)."""
        dedup = MessageDeduplicator()
        assert dedup.is_duplicate("msg1") is False

    def test_second_message_is_duplicate(self):
        """Second time seeing same message should return True."""
        dedup = MessageDeduplicator()
        dedup.mark_seen("msg1")
        assert dedup.is_duplicate("msg1") is True

    def test_check_and_mark_returns_true_first_time(self):
        """check_and_mark should return True first time (should process)."""
        dedup = MessageDeduplicator()
        assert dedup.check_and_mark("msg1") is True

    def test_check_and_mark_returns_false_second_time(self):
        """check_and_mark should return False second time (duplicate)."""
        dedup = MessageDeduplicator()
        dedup.check_and_mark("msg1")
        assert dedup.check_and_mark("msg1") is False

    def test_different_messages_not_duplicates(self):
        """Different message IDs should not be considered duplicates."""
        dedup = MessageDeduplicator()
        dedup.mark_seen("msg1")
        assert dedup.is_duplicate("msg2") is False

    def test_stats(self):
        """Stats should show correct counts."""
        dedup = MessageDeduplicator()
        dedup.mark_seen("msg1")
        dedup.mark_seen("msg2")
        stats = dedup.stats()
        assert stats["cached_messages"] == 2


class TestRelayMetadata:
    """Tests for RelayMetadata dataclass."""

    def test_to_dict(self):
        """Should serialize to dict correctly."""
        meta = RelayMetadata(
            msg_id="abc123",
            ttl=3,
            relay_path=["node1", "node2"],
            origin="node1",
            origin_ts=1234567890
        )
        d = meta.to_dict()
        assert d["msg_id"] == "abc123"
        assert d["ttl"] == 3
        assert d["relay_path"] == ["node1", "node2"]
        assert d["origin"] == "node1"

    def test_from_dict(self):
        """Should deserialize from dict correctly."""
        d = {
            "msg_id": "abc123",
            "ttl": 2,
            "relay_path": ["node1"],
            "origin": "node1",
            "origin_ts": 1234567890
        }
        meta = RelayMetadata.from_dict(d)
        assert meta.msg_id == "abc123"
        assert meta.ttl == 2
        assert meta.relay_path == ["node1"]


class TestRelayManager:
    """Tests for RelayManager."""

    @pytest.fixture
    def relay_mgr(self):
        """Create a RelayManager for testing."""
        sent_messages = []

        def send_message(peer_id: str, msg_bytes: bytes) -> bool:
            sent_messages.append((peer_id, msg_bytes))
            return True

        def get_members():
            return ["node1", "node2", "node3"]

        mgr = RelayManager(
            our_pubkey="node0",
            send_message=send_message,
            get_members=get_members
        )
        mgr._sent_messages = sent_messages  # For test inspection
        return mgr

    def test_generate_msg_id_consistent(self, relay_mgr):
        """Same payload should generate same msg_id."""
        payload = {"data": "test", "value": 123}
        id1 = relay_mgr.generate_msg_id(payload)
        id2 = relay_mgr.generate_msg_id(payload)
        assert id1 == id2

    def test_generate_msg_id_ignores_relay_metadata(self, relay_mgr):
        """msg_id should be same regardless of relay metadata."""
        payload1 = {"data": "test"}
        payload2 = {"data": "test", "_relay": {"ttl": 2}}
        assert relay_mgr.generate_msg_id(payload1) == relay_mgr.generate_msg_id(payload2)

    def test_prepare_for_broadcast_adds_relay_metadata(self, relay_mgr):
        """prepare_for_broadcast should add _relay metadata."""
        payload = {"data": "test"}
        result = relay_mgr.prepare_for_broadcast(payload)
        assert "_relay" in result
        assert result["_relay"]["ttl"] == DEFAULT_TTL
        assert result["_relay"]["origin"] == "node0"
        assert "node0" in result["_relay"]["relay_path"]

    def test_should_process_returns_true_first_time(self, relay_mgr):
        """First time seeing message should return True."""
        payload = {"data": "test", "_relay": {"msg_id": "unique123"}}
        assert relay_mgr.should_process(payload) is True

    def test_should_process_returns_false_for_duplicate(self, relay_mgr):
        """Duplicate message should return False."""
        payload = {"data": "test", "_relay": {"msg_id": "unique123"}}
        relay_mgr.should_process(payload)
        assert relay_mgr.should_process(payload) is False

    def test_should_relay_with_ttl_zero(self, relay_mgr):
        """Should not relay when TTL is 0."""
        payload = {"data": "test", "_relay": {"ttl": 0}}
        assert relay_mgr.should_relay(payload) is False

    def test_should_relay_with_positive_ttl(self, relay_mgr):
        """Should relay when TTL is positive."""
        payload = {"data": "test", "_relay": {"ttl": 2}}
        assert relay_mgr.should_relay(payload) is True

    def test_prepare_for_relay_decrements_ttl(self, relay_mgr):
        """prepare_for_relay should decrement TTL."""
        payload = {
            "data": "test",
            "_relay": {"msg_id": "abc", "ttl": 3, "relay_path": ["sender"]}
        }
        result = relay_mgr.prepare_for_relay(payload, "sender")
        assert result["_relay"]["ttl"] == 2

    def test_prepare_for_relay_adds_to_path(self, relay_mgr):
        """prepare_for_relay should add us to relay_path."""
        payload = {
            "data": "test",
            "_relay": {"msg_id": "abc", "ttl": 3, "relay_path": ["sender"]}
        }
        result = relay_mgr.prepare_for_relay(payload, "sender")
        assert "node0" in result["_relay"]["relay_path"]

    def test_prepare_for_relay_returns_none_at_ttl_one(self, relay_mgr):
        """prepare_for_relay should return None when TTL would become 0."""
        payload = {
            "data": "test",
            "_relay": {"msg_id": "abc", "ttl": 1, "relay_path": ["sender"]}
        }
        result = relay_mgr.prepare_for_relay(payload, "sender")
        assert result is None

    def test_relay_excludes_sender(self, relay_mgr):
        """Relay should not send back to sender."""
        payload = relay_mgr.prepare_for_broadcast({"data": "test"}, ttl=3)

        def encode(p):
            return b"encoded"

        # Simulate receiving from node1
        relay_mgr.relay(payload, "node1", encode)

        # Check that node1 is not in recipients
        recipients = [peer for peer, _ in relay_mgr._sent_messages]
        assert "node1" not in recipients

    def test_relay_excludes_self(self, relay_mgr):
        """Relay should not send to ourselves."""
        payload = relay_mgr.prepare_for_broadcast({"data": "test"}, ttl=3)

        def encode(p):
            return b"encoded"

        relay_mgr.relay(payload, "node1", encode)

        recipients = [peer for peer, _ in relay_mgr._sent_messages]
        assert "node0" not in recipients

    def test_relay_excludes_nodes_in_path(self, relay_mgr):
        """Relay should not send to nodes already in relay_path."""
        payload = {
            "data": "test",
            "_relay": {
                "msg_id": "abc",
                "ttl": 3,
                "relay_path": ["origin", "node2"],  # node2 already saw it
                "origin": "origin"
            }
        }

        def encode(p):
            return b"encoded"

        relay_mgr.relay(payload, "node2", encode)

        recipients = [peer for peer, _ in relay_mgr._sent_messages]
        assert "node2" not in recipients

    def test_stats(self, relay_mgr):
        """Stats should show relay metrics."""
        stats = relay_mgr.stats()
        assert "messages_processed" in stats
        assert "messages_relayed" in stats
        assert "dedup" in stats
