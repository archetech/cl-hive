"""
Tests for VPN Transport Module.

Tests VPN subnet detection, transport policy enforcement,
and peer connection tracking.
"""

import pytest
from modules.vpn_transport import (
    VPNTransportManager,
    TransportMode,
    MessageRequirement,
    VPNPeerMapping,
    VPNConnectionInfo
)


class TestVPNPeerMapping:
    """Tests for VPNPeerMapping dataclass."""

    def test_vpn_address_property(self):
        """Test vpn_address returns correct format."""
        mapping = VPNPeerMapping(
            pubkey="02abc123",
            vpn_ip="10.8.0.2",
            vpn_port=9735
        )
        assert mapping.vpn_address == "10.8.0.2:9735"

    def test_vpn_address_custom_port(self):
        """Test vpn_address with custom port."""
        mapping = VPNPeerMapping(
            pubkey="02abc123",
            vpn_ip="10.8.0.2",
            vpn_port=9736
        )
        assert mapping.vpn_address == "10.8.0.2:9736"

    def test_to_dict(self):
        """Test serialization to dictionary."""
        mapping = VPNPeerMapping(
            pubkey="02abc123",
            vpn_ip="10.8.0.2",
            vpn_port=9735
        )
        d = mapping.to_dict()
        assert d["pubkey"] == "02abc123"
        assert d["vpn_ip"] == "10.8.0.2"
        assert d["vpn_port"] == 9735
        assert d["vpn_address"] == "10.8.0.2:9735"
        assert "added_at" in d


class TestVPNTransportConfiguration:
    """Tests for VPN transport configuration."""

    def test_default_mode_is_any(self):
        """Test default transport mode is ANY."""
        mgr = VPNTransportManager()
        assert mgr._mode == TransportMode.ANY

    def test_configure_vpn_only_mode(self):
        """Test configuring vpn-only mode."""
        mgr = VPNTransportManager()
        result = mgr.configure(mode="vpn-only", vpn_subnets="10.8.0.0/24")
        assert result["mode"] == "vpn-only"
        assert mgr._mode == TransportMode.VPN_ONLY

    def test_configure_vpn_preferred_mode(self):
        """Test configuring vpn-preferred mode."""
        mgr = VPNTransportManager()
        result = mgr.configure(mode="vpn-preferred", vpn_subnets="10.8.0.0/24")
        assert result["mode"] == "vpn-preferred"
        assert mgr._mode == TransportMode.VPN_PREFERRED

    def test_configure_invalid_mode_defaults_to_any(self):
        """Test invalid mode defaults to any."""
        mgr = VPNTransportManager()
        result = mgr.configure(mode="invalid-mode")
        assert result["mode"] == "any"
        assert len(result["warnings"]) > 0

    def test_configure_vpn_subnets(self):
        """Test configuring VPN subnets."""
        mgr = VPNTransportManager()
        result = mgr.configure(
            vpn_subnets="10.8.0.0/24, 192.168.100.0/24"
        )
        assert len(result["subnets"]) == 2
        assert "10.8.0.0/24" in result["subnets"]
        assert "192.168.100.0/24" in result["subnets"]

    def test_configure_invalid_subnet(self):
        """Test invalid subnet generates warning."""
        mgr = VPNTransportManager()
        result = mgr.configure(vpn_subnets="invalid-subnet")
        assert len(result["subnets"]) == 0
        assert len(result["warnings"]) > 0

    def test_configure_vpn_peers(self):
        """Test configuring VPN peer mappings."""
        mgr = VPNTransportManager()
        result = mgr.configure(
            vpn_peers="02abc123@10.8.0.2:9735,03def456@10.8.0.3:9736"
        )
        assert result["peers"] == 2
        assert "02abc123" in mgr._vpn_peers
        assert mgr._vpn_peers["02abc123"].vpn_ip == "10.8.0.2"
        assert mgr._vpn_peers["03def456"].vpn_port == 9736

    def test_configure_vpn_bind(self):
        """Test configuring VPN bind address."""
        mgr = VPNTransportManager()
        result = mgr.configure(vpn_bind="10.8.0.1:9735")
        assert result["bind"] == "10.8.0.1:9735"
        assert mgr._vpn_bind == ("10.8.0.1", 9735)


class TestVPNSubnetDetection:
    """Tests for VPN subnet detection."""

    def test_is_vpn_address_in_subnet(self):
        """Test IP in VPN subnet returns True."""
        mgr = VPNTransportManager()
        mgr.configure(vpn_subnets="10.8.0.0/24")
        assert mgr.is_vpn_address("10.8.0.5") == True
        assert mgr.is_vpn_address("10.8.0.254") == True

    def test_is_vpn_address_not_in_subnet(self):
        """Test IP not in VPN subnet returns False."""
        mgr = VPNTransportManager()
        mgr.configure(vpn_subnets="10.8.0.0/24")
        assert mgr.is_vpn_address("10.8.1.5") == False
        assert mgr.is_vpn_address("192.168.1.1") == False
        assert mgr.is_vpn_address("8.8.8.8") == False

    def test_is_vpn_address_multiple_subnets(self):
        """Test detection with multiple subnets."""
        mgr = VPNTransportManager()
        mgr.configure(vpn_subnets="10.8.0.0/24,192.168.100.0/24")
        assert mgr.is_vpn_address("10.8.0.5") == True
        assert mgr.is_vpn_address("192.168.100.50") == True
        assert mgr.is_vpn_address("192.168.1.1") == False

    def test_is_vpn_address_no_subnets(self):
        """Test returns False when no subnets configured."""
        mgr = VPNTransportManager()
        mgr.configure()
        assert mgr.is_vpn_address("10.8.0.5") == False

    def test_is_vpn_address_invalid_ip(self):
        """Test invalid IP returns False."""
        mgr = VPNTransportManager()
        mgr.configure(vpn_subnets="10.8.0.0/24")
        assert mgr.is_vpn_address("invalid") == False
        assert mgr.is_vpn_address("") == False
        assert mgr.is_vpn_address("10.8.0.999") == False


class TestIPExtraction:
    """Tests for IP address extraction."""

    def test_extract_ip_from_ipv4_port(self):
        """Test extracting IP from ip:port format."""
        mgr = VPNTransportManager()
        assert mgr.extract_ip_from_address("10.8.0.5:9735") == "10.8.0.5"

    def test_extract_ip_from_bare_ip(self):
        """Test extracting bare IP address."""
        mgr = VPNTransportManager()
        assert mgr.extract_ip_from_address("10.8.0.5") == "10.8.0.5"

    def test_extract_ip_from_ipv6_brackets(self):
        """Test extracting IPv6 from [ip]:port format."""
        mgr = VPNTransportManager()
        result = mgr.extract_ip_from_address("[::1]:9735")
        assert result == "::1"

    def test_extract_ip_none_for_hostname(self):
        """Test returns None for hostname."""
        mgr = VPNTransportManager()
        assert mgr.extract_ip_from_address("node.example.com:9735") is None

    def test_extract_ip_empty_string(self):
        """Test returns None for empty string."""
        mgr = VPNTransportManager()
        assert mgr.extract_ip_from_address("") is None
        assert mgr.extract_ip_from_address(None) is None


class TestTransportPolicy:
    """Tests for transport policy enforcement."""

    def test_any_mode_accepts_all(self):
        """Test ANY mode accepts all messages."""
        mgr = VPNTransportManager()
        mgr.configure(mode="any")

        accept, reason = mgr.should_accept_hive_message("peer1", "GOSSIP")
        assert accept == True
        assert "any transport allowed" in reason

    def test_vpn_only_rejects_non_vpn(self):
        """Test VPN_ONLY mode rejects non-VPN connections."""
        mgr = VPNTransportManager()
        mgr.configure(mode="vpn-only", vpn_subnets="10.8.0.0/24")

        # Peer not connected via VPN
        accept, reason = mgr.should_accept_hive_message(
            "peer1", "GOSSIP", peer_address="1.2.3.4:9735"
        )
        assert accept == False
        assert "vpn-only" in reason.lower()

    def test_vpn_only_accepts_vpn_connection(self):
        """Test VPN_ONLY mode accepts VPN connections."""
        mgr = VPNTransportManager()
        mgr.configure(mode="vpn-only", vpn_subnets="10.8.0.0/24")

        # Peer connected via VPN
        accept, reason = mgr.should_accept_hive_message(
            "peer1", "GOSSIP", peer_address="10.8.0.5:9735"
        )
        assert accept == True
        assert "vpn" in reason.lower()

    def test_vpn_preferred_allows_fallback(self):
        """Test VPN_PREFERRED mode allows non-VPN fallback."""
        mgr = VPNTransportManager()
        mgr.configure(mode="vpn-preferred", vpn_subnets="10.8.0.0/24")

        # Non-VPN connection
        accept, reason = mgr.should_accept_hive_message(
            "peer1", "GOSSIP", peer_address="1.2.3.4:9735"
        )
        assert accept == True
        assert "fallback" in reason.lower()

    def test_vpn_connection_caching(self):
        """Test VPN connection status is cached."""
        mgr = VPNTransportManager()
        mgr.configure(mode="vpn-only", vpn_subnets="10.8.0.0/24")

        # First connection via VPN
        mgr.on_peer_connected("peer1", "10.8.0.5:9735")

        # Subsequent message without address should still work
        accept, _ = mgr.should_accept_hive_message("peer1", "GOSSIP")
        assert accept == True

    def test_message_requirement_filtering(self):
        """Test message type requirement filtering."""
        mgr = VPNTransportManager()
        mgr.configure(
            mode="vpn-only",
            vpn_subnets="10.8.0.0/24",
            required_messages="gossip"  # Only gossip requires VPN
        )

        # INTENT message should be allowed without VPN
        accept, _ = mgr.should_accept_hive_message(
            "peer1", "INTENT", peer_address="1.2.3.4:9735"
        )
        assert accept == True

        # GOSSIP message should be rejected
        accept, _ = mgr.should_accept_hive_message(
            "peer1", "GOSSIP", peer_address="1.2.3.4:9735"
        )
        assert accept == False


class TestPeerManagement:
    """Tests for VPN peer management."""

    def test_add_vpn_peer(self):
        """Test adding a VPN peer."""
        mgr = VPNTransportManager()
        mgr.configure()

        success = mgr.add_vpn_peer("02abc123", "10.8.0.2", 9735)
        assert success == True
        assert "02abc123" in mgr._vpn_peers
        assert mgr.get_vpn_address("02abc123") == "10.8.0.2:9735"

    def test_remove_vpn_peer(self):
        """Test removing a VPN peer."""
        mgr = VPNTransportManager()
        mgr.configure(vpn_peers="02abc123@10.8.0.2:9735")

        success = mgr.remove_vpn_peer("02abc123")
        assert success == True
        assert "02abc123" not in mgr._vpn_peers

    def test_remove_nonexistent_peer(self):
        """Test removing nonexistent peer returns False."""
        mgr = VPNTransportManager()
        mgr.configure()

        success = mgr.remove_vpn_peer("nonexistent")
        assert success == False

    def test_get_vpn_address(self):
        """Test getting VPN address for peer."""
        mgr = VPNTransportManager()
        mgr.configure(vpn_peers="02abc123@10.8.0.2:9735")

        assert mgr.get_vpn_address("02abc123") == "10.8.0.2:9735"
        assert mgr.get_vpn_address("unknown") is None


class TestConnectionTracking:
    """Tests for connection tracking."""

    def test_on_peer_connected_vpn(self):
        """Test tracking VPN peer connection."""
        mgr = VPNTransportManager()
        mgr.configure(vpn_subnets="10.8.0.0/24")

        result = mgr.on_peer_connected("peer1", "10.8.0.5:9735")
        assert result["connected_via_vpn"] == True
        assert mgr._stats["vpn_connections"] == 1

    def test_on_peer_connected_non_vpn(self):
        """Test tracking non-VPN peer connection."""
        mgr = VPNTransportManager()
        mgr.configure(vpn_subnets="10.8.0.0/24")

        result = mgr.on_peer_connected("peer1", "1.2.3.4:9735")
        assert result["connected_via_vpn"] == False
        assert mgr._stats["non_vpn_connections"] == 1

    def test_on_peer_disconnected(self):
        """Test peer disconnection clears VPN status."""
        mgr = VPNTransportManager()
        mgr.configure(vpn_subnets="10.8.0.0/24")

        mgr.on_peer_connected("peer1", "10.8.0.5:9735")
        assert mgr._peer_connections["peer1"].connected_via_vpn == True

        mgr.on_peer_disconnected("peer1")
        assert mgr._peer_connections["peer1"].connected_via_vpn == False


class TestStatus:
    """Tests for status reporting."""

    def test_get_status(self):
        """Test getting full status."""
        mgr = VPNTransportManager()
        mgr.configure(
            mode="vpn-only",
            vpn_subnets="10.8.0.0/24",
            vpn_peers="02abc@10.8.0.2:9735"
        )

        status = mgr.get_status()
        assert status["configured"] == True
        assert status["mode"] == "vpn-only"
        assert "10.8.0.0/24" in status["vpn_subnets"]
        assert status["configured_peers"] == 1

    def test_is_enabled(self):
        """Test is_enabled returns correct value."""
        mgr = VPNTransportManager()

        mgr.configure(mode="any")
        assert mgr.is_enabled() == False

        mgr.configure(mode="vpn-only")
        assert mgr.is_enabled() == True

        mgr.configure(mode="vpn-preferred")
        assert mgr.is_enabled() == True

    def test_statistics_tracking(self):
        """Test statistics are tracked correctly."""
        mgr = VPNTransportManager()
        mgr.configure(mode="vpn-only", vpn_subnets="10.8.0.0/24")

        # Simulate accepted and rejected messages
        mgr.on_peer_connected("peer1", "10.8.0.5:9735")
        mgr.should_accept_hive_message("peer1", "GOSSIP")

        mgr.should_accept_hive_message("peer2", "GOSSIP", "1.2.3.4:9735")

        status = mgr.get_status()
        assert status["statistics"]["messages_accepted"] >= 1
        assert status["statistics"]["messages_rejected"] >= 1

    def test_reset_statistics(self):
        """Test resetting statistics."""
        mgr = VPNTransportManager()
        mgr.configure(mode="any")

        mgr.should_accept_hive_message("peer1", "GOSSIP")
        mgr.should_accept_hive_message("peer2", "GOSSIP")

        old_stats = mgr.reset_statistics()
        assert old_stats["messages_accepted"] == 2

        new_status = mgr.get_status()
        assert new_status["statistics"]["messages_accepted"] == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
