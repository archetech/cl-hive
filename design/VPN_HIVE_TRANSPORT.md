# VPN Hive Transport Design

## Overview

This feature allows hive communication to be routed exclusively through a WireGuard VPN,
providing a private, low-latency network for hive gossip while maintaining public
Lightning channels over Tor/clearnet.

## Use Cases

1. **Private Fleet Management**: Corporate/organization running multiple nodes
2. **Geographic Distribution**: Nodes across data centers with private interconnect
3. **Security Isolation**: Hive coordination separate from public Lightning traffic
4. **Latency Optimization**: VPN often faster than Tor for time-sensitive gossip

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    HIVE NETWORK                              │
│                                                              │
│  ┌─────────────┐     WireGuard VPN      ┌─────────────┐     │
│  │   alice     │◄────(10.8.0.0/24)─────►│    bob      │     │
│  │ 10.8.0.1    │                        │ 10.8.0.2    │     │
│  │             │    Hive Gossip Only    │             │     │
│  └──────┬──────┘                        └──────┬──────┘     │
│         │                                      │            │
│         │  VPN Hive Gossip                    │            │
│         ▼                                      ▼            │
│  ┌─────────────┐                        ┌─────────────┐     │
│  │   carol     │◄────(10.8.0.0/24)─────►│  (future)   │     │
│  │ 10.8.0.3    │                        │ 10.8.0.N    │     │
│  └─────────────┘                        └─────────────┘     │
│                                                              │
└─────────────────────────────────────────────────────────────┘
         │                    │                    │
         │ Tor/Clearnet       │                    │
         ▼                    ▼                    ▼
┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│  External   │      │  External   │      │  External   │
│   Peers     │      │   Peers     │      │   Peers     │
│ (LND, etc)  │      │ (LND, etc)  │      │ (LND, etc)  │
└─────────────┘      └─────────────┘      └─────────────┘
```

## Configuration

### cl-hive.conf Options

```ini
# =============================================================================
# VPN TRANSPORT CONFIGURATION
# =============================================================================

# Transport mode for hive communication
# Options:
#   any      - Accept hive gossip from any interface (default)
#   vpn-only - Only accept hive gossip from VPN interface
#   vpn-preferred - Prefer VPN, fall back to any
hive-transport-mode=vpn-only

# VPN subnet(s) for hive peers (CIDR notation)
# Multiple subnets can be comma-separated
# Used to identify if a connection comes from VPN
hive-vpn-subnets=10.8.0.0/24

# Bind address for hive-only listener (optional)
# If set, creates additional bind on this VPN IP for hive traffic
hive-vpn-bind=10.8.0.1:9736

# Require VPN for specific hive message types
# Options: all, gossip, intent, sync
# Example: gossip,intent (only these require VPN)
hive-vpn-required-messages=all

# VPN peer mapping (pubkey to VPN address)
# Format: pubkey@vpn-ip:port (one per line or comma-separated)
# If set, hive will connect to these addresses for VPN peers
hive-vpn-peers=02abc123...@10.8.0.2:9735,03def456...@10.8.0.3:9735
```

### Environment Variables (Docker)

```bash
# In docker-compose.yml or .env
HIVE_TRANSPORT_MODE=vpn-only
HIVE_VPN_SUBNETS=10.8.0.0/24
HIVE_VPN_BIND=10.8.0.1:9736
HIVE_VPN_PEERS=02abc...@10.8.0.2:9735,03def...@10.8.0.3:9735
```

## Implementation

### New Module: `modules/vpn_transport.py`

```python
"""
VPN Transport Module for cl-hive.

Manages VPN-based communication for hive gossip, providing:
- VPN subnet detection
- Peer address resolution (VPN vs clearnet)
- Transport policy enforcement
- Connection routing decisions
"""

import ipaddress
import socket
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple


class TransportMode(Enum):
    """Hive transport modes."""
    ANY = "any"              # Accept from any interface
    VPN_ONLY = "vpn-only"    # VPN required for hive gossip
    VPN_PREFERRED = "vpn-preferred"  # Prefer VPN, allow fallback


@dataclass
class VPNPeerMapping:
    """Maps a node pubkey to its VPN address."""
    pubkey: str
    vpn_ip: str
    vpn_port: int = 9735

    @property
    def vpn_address(self) -> str:
        return f"{self.vpn_ip}:{self.vpn_port}"


class VPNTransportManager:
    """
    Manages VPN transport policy for hive communication.

    Responsibilities:
    - Detect if peer connection is via VPN
    - Enforce transport policy for hive messages
    - Resolve peer addresses for VPN routing
    - Track VPN connectivity status
    """

    def __init__(self, plugin=None, config=None):
        self.plugin = plugin
        self.config = config

        # Transport mode
        self._mode: TransportMode = TransportMode.ANY

        # VPN subnets for detection
        self._vpn_subnets: List[ipaddress.IPv4Network] = []

        # Peer to VPN address mapping
        self._vpn_peers: Dict[str, VPNPeerMapping] = {}

        # Track which peers are connected via VPN
        self._vpn_connected_peers: Set[str] = set()

        # VPN bind address (optional)
        self._vpn_bind: Optional[Tuple[str, int]] = None

    def configure(self,
                  mode: str = "any",
                  vpn_subnets: str = "",
                  vpn_bind: str = "",
                  vpn_peers: str = "") -> None:
        """
        Configure VPN transport settings.

        Args:
            mode: Transport mode (any, vpn-only, vpn-preferred)
            vpn_subnets: Comma-separated CIDR subnets
            vpn_bind: VPN bind address (ip:port)
            vpn_peers: Comma-separated pubkey@ip:port mappings
        """
        # Parse mode
        try:
            self._mode = TransportMode(mode.lower())
        except ValueError:
            self._log(f"Invalid transport mode '{mode}', using 'any'", level='warn')
            self._mode = TransportMode.ANY

        # Parse VPN subnets
        self._vpn_subnets = []
        if vpn_subnets:
            for subnet in vpn_subnets.split(','):
                subnet = subnet.strip()
                if subnet:
                    try:
                        self._vpn_subnets.append(ipaddress.IPv4Network(subnet))
                    except ValueError as e:
                        self._log(f"Invalid VPN subnet '{subnet}': {e}", level='warn')

        # Parse VPN bind
        self._vpn_bind = None
        if vpn_bind:
            try:
                ip, port = vpn_bind.rsplit(':', 1)
                self._vpn_bind = (ip, int(port))
            except ValueError:
                self._log(f"Invalid VPN bind '{vpn_bind}'", level='warn')

        # Parse peer mappings
        self._vpn_peers = {}
        if vpn_peers:
            for mapping in vpn_peers.split(','):
                mapping = mapping.strip()
                if '@' in mapping:
                    try:
                        pubkey, addr = mapping.split('@', 1)
                        ip, port = addr.rsplit(':', 1) if ':' in addr else (addr, '9735')
                        self._vpn_peers[pubkey] = VPNPeerMapping(
                            pubkey=pubkey,
                            vpn_ip=ip,
                            vpn_port=int(port)
                        )
                    except ValueError:
                        self._log(f"Invalid VPN peer mapping '{mapping}'", level='warn')

        self._log(f"VPN transport configured: mode={self._mode.value}, "
                  f"subnets={len(self._vpn_subnets)}, peers={len(self._vpn_peers)}")

    def is_vpn_address(self, ip_address: str) -> bool:
        """
        Check if an IP address is within VPN subnets.

        Args:
            ip_address: IP address to check

        Returns:
            True if address is in VPN subnet
        """
        if not self._vpn_subnets:
            return False

        try:
            ip = ipaddress.IPv4Address(ip_address)
            return any(ip in subnet for subnet in self._vpn_subnets)
        except ValueError:
            return False

    def should_accept_hive_message(self,
                                    peer_id: str,
                                    peer_address: Optional[str] = None) -> Tuple[bool, str]:
        """
        Check if a hive message should be accepted based on transport policy.

        Args:
            peer_id: Node pubkey of the peer
            peer_address: Optional peer IP address

        Returns:
            Tuple of (accept: bool, reason: str)
        """
        if self._mode == TransportMode.ANY:
            return (True, "any transport allowed")

        # Check if peer is connected via VPN
        is_vpn = peer_id in self._vpn_connected_peers

        if peer_address and not is_vpn:
            is_vpn = self.is_vpn_address(peer_address)
            if is_vpn:
                self._vpn_connected_peers.add(peer_id)

        if self._mode == TransportMode.VPN_ONLY:
            if is_vpn:
                return (True, "vpn transport verified")
            else:
                return (False, "vpn-only mode: non-VPN connection rejected")

        if self._mode == TransportMode.VPN_PREFERRED:
            if is_vpn:
                return (True, "vpn transport (preferred)")
            else:
                return (True, "vpn-preferred: allowing non-VPN fallback")

        return (True, "transport check passed")

    def get_vpn_address(self, peer_id: str) -> Optional[str]:
        """
        Get the VPN address for a peer if configured.

        Args:
            peer_id: Node pubkey

        Returns:
            VPN address string (ip:port) or None
        """
        mapping = self._vpn_peers.get(peer_id)
        return mapping.vpn_address if mapping else None

    def on_peer_connected(self, peer_id: str, address: Optional[str] = None) -> None:
        """
        Handle peer connection event.

        Args:
            peer_id: Connected peer's pubkey
            address: Connection address if known
        """
        if address and self.is_vpn_address(address):
            self._vpn_connected_peers.add(peer_id)
            self._log(f"Peer {peer_id[:16]}... connected via VPN ({address})")

    def on_peer_disconnected(self, peer_id: str) -> None:
        """Handle peer disconnection."""
        self._vpn_connected_peers.discard(peer_id)

    def get_vpn_status(self) -> Dict:
        """
        Get VPN transport status.

        Returns:
            Status dictionary
        """
        return {
            "mode": self._mode.value,
            "vpn_subnets": [str(s) for s in self._vpn_subnets],
            "vpn_bind": f"{self._vpn_bind[0]}:{self._vpn_bind[1]}" if self._vpn_bind else None,
            "configured_peers": len(self._vpn_peers),
            "vpn_connected_peers": list(self._vpn_connected_peers),
            "vpn_peer_mappings": {
                k: v.vpn_address for k, v in self._vpn_peers.items()
            }
        }

    def _log(self, message: str, level: str = 'info') -> None:
        """Log with optional plugin reference."""
        if self.plugin:
            self.plugin.log(f"vpn-transport: {message}", level=level)
```

### Integration Points

#### 1. Plugin Initialization (`cl-hive.py`)

```python
# Add to plugin options
plugin.add_option(
    name="hive-transport-mode",
    default="any",
    description="Hive transport mode: any, vpn-only, vpn-preferred"
)
plugin.add_option(
    name="hive-vpn-subnets",
    default="",
    description="VPN subnets for hive peers (CIDR, comma-separated)"
)
plugin.add_option(
    name="hive-vpn-bind",
    default="",
    description="VPN bind address for hive traffic (ip:port)"
)
plugin.add_option(
    name="hive-vpn-peers",
    default="",
    description="VPN peer mappings (pubkey@ip:port, comma-separated)"
)

# Initialize in init()
vpn_transport = VPNTransportManager(plugin=plugin)
vpn_transport.configure(
    mode=plugin.get_option("hive-transport-mode"),
    vpn_subnets=plugin.get_option("hive-vpn-subnets"),
    vpn_bind=plugin.get_option("hive-vpn-bind"),
    vpn_peers=plugin.get_option("hive-vpn-peers")
)
```

#### 2. Message Reception (`handle_custommsg`)

```python
@plugin.hook("custommsg")
def handle_custommsg(peer_id, payload, plugin, **kwargs):
    """Handle custom messages including Hive protocol."""
    # ... existing parsing ...

    # Check VPN transport policy for hive messages
    if vpn_transport and msg_type.startswith("HIVE"):
        accept, reason = vpn_transport.should_accept_hive_message(
            peer_id=peer_id,
            peer_address=kwargs.get('peer_address')  # If available
        )
        if not accept:
            plugin.log(f"Rejected hive message from {peer_id[:16]}...: {reason}")
            return {"result": "continue"}

    # ... continue with message handling ...
```

#### 3. Peer Connection Hook

```python
@plugin.subscribe("connect")
def on_peer_connected(**kwargs):
    peer_id = kwargs.get('id')
    # Extract peer address from connection info
    peer_address = extract_peer_address(peer_id)  # Implementation needed

    if vpn_transport:
        vpn_transport.on_peer_connected(peer_id, peer_address)

    # ... existing member check and state_hash sending ...
```

#### 4. New RPC Command

```python
@plugin.method("hive-vpn-status")
def hive_vpn_status(plugin: Plugin):
    """Get VPN transport status."""
    if not vpn_transport:
        return {"error": "VPN transport not initialized"}
    return vpn_transport.get_vpn_status()
```

### Address Resolution

Getting the peer's IP address from CLN requires some work:

```python
def get_peer_address(rpc, peer_id: str) -> Optional[str]:
    """
    Get the IP address of a connected peer.

    Args:
        rpc: Lightning RPC client
        peer_id: Node pubkey

    Returns:
        IP address or None
    """
    try:
        peers = rpc.listpeers(id=peer_id)
        if peers and peers.get('peers'):
            peer = peers['peers'][0]
            # Check netaddr for connection info
            if 'netaddr' in peer and peer['netaddr']:
                # netaddr format: "ip:port" or "[ipv6]:port"
                addr = peer['netaddr'][0]
                # Extract IP from address
                if addr.startswith('['):
                    # IPv6
                    ip = addr[1:addr.rindex(']')]
                else:
                    # IPv4
                    ip = addr.rsplit(':', 1)[0]
                return ip
    except Exception:
        pass
    return None
```

## Security Considerations

### 1. VPN Subnet Validation
- Only accept configured VPN subnets
- Reject RFC1918 addresses unless explicitly in subnet list
- Log all rejected connections for audit

### 2. Peer Identity Verification
- VPN doesn't replace Lightning peer authentication
- Pubkey verification still required
- VPN is additional transport security layer

### 3. Message Integrity
- Hive messages already signed/verified
- VPN adds encryption in transit
- Defense in depth

### 4. Configuration Security
- VPN peer mappings should be distributed securely
- Consider encrypted config file for sensitive data
- Rotate VPN keys periodically

## Testing Plan

### Unit Tests

```python
# tests/test_vpn_transport.py

def test_vpn_subnet_detection():
    """Test IP address VPN subnet detection."""
    mgr = VPNTransportManager()
    mgr.configure(vpn_subnets="10.8.0.0/24,192.168.100.0/24")

    assert mgr.is_vpn_address("10.8.0.5") == True
    assert mgr.is_vpn_address("10.8.1.5") == False
    assert mgr.is_vpn_address("192.168.100.50") == True
    assert mgr.is_vpn_address("8.8.8.8") == False

def test_vpn_only_mode():
    """Test VPN-only transport mode."""
    mgr = VPNTransportManager()
    mgr.configure(mode="vpn-only", vpn_subnets="10.8.0.0/24")

    # Mark peer as VPN connected
    mgr.on_peer_connected("peer1", "10.8.0.5")

    accept, _ = mgr.should_accept_hive_message("peer1")
    assert accept == True

    accept, _ = mgr.should_accept_hive_message("peer2", "1.2.3.4")
    assert accept == False

def test_peer_vpn_mapping():
    """Test peer to VPN address mapping."""
    mgr = VPNTransportManager()
    mgr.configure(vpn_peers="02abc@10.8.0.2:9735,03def@10.8.0.3:9736")

    assert mgr.get_vpn_address("02abc") == "10.8.0.2:9735"
    assert mgr.get_vpn_address("03def") == "10.8.0.3:9736"
    assert mgr.get_vpn_address("unknown") == None
```

### Integration Tests (Polar)

```bash
# Test VPN transport with simulated network
./test.sh vpn-transport 1

# Tests:
# 1. Configure VPN subnets on all hive nodes
# 2. Verify hive gossip only accepted from VPN range
# 3. Test fallback behavior in vpn-preferred mode
# 4. Verify external peers still work over clearnet
```

## Migration Path

### Phase 1: Optional Feature (v0.2.0)
- Add VPN transport module
- Default mode: `any` (no change to existing behavior)
- Document configuration options

### Phase 2: Enhanced Detection (v0.3.0)
- Add automatic VPN interface detection
- Improve peer address resolution
- Add VPN health monitoring

### Phase 3: Advanced Features (v0.4.0)
- Multi-VPN support (different VPNs for different peer groups)
- Dynamic VPN peer discovery
- VPN failover handling

## Example Deployment

### Docker Compose with VPN

```yaml
# docker-compose.hive-vpn.yml
version: '3.8'

services:
  alice:
    image: cl-hive-node:latest
    environment:
      - WIREGUARD_ENABLED=true
      - WG_ADDRESS=10.8.0.1/24
      - HIVE_TRANSPORT_MODE=vpn-only
      - HIVE_VPN_SUBNETS=10.8.0.0/24
      - HIVE_VPN_PEERS=02bob...@10.8.0.2:9735,03carol...@10.8.0.3:9735
    # ... other config

  bob:
    image: cl-hive-node:latest
    environment:
      - WIREGUARD_ENABLED=true
      - WG_ADDRESS=10.8.0.2/24
      - HIVE_TRANSPORT_MODE=vpn-only
      - HIVE_VPN_SUBNETS=10.8.0.0/24
      - HIVE_VPN_PEERS=02alice...@10.8.0.1:9735,03carol...@10.8.0.3:9735
    # ... other config

  carol:
    image: cl-hive-node:latest
    environment:
      - WIREGUARD_ENABLED=true
      - WG_ADDRESS=10.8.0.3/24
      - HIVE_TRANSPORT_MODE=vpn-only
      - HIVE_VPN_SUBNETS=10.8.0.0/24
      - HIVE_VPN_PEERS=02alice...@10.8.0.1:9735,02bob...@10.8.0.2:9735
    # ... other config
```

## Open Questions

1. **Should VPN transport be hive-wide or per-member configurable?**
   - Current design: Per-node configuration
   - Alternative: Hive-level policy in genesis

2. **How to handle VPN failover?**
   - Automatic fallback to Tor?
   - Alert and pause gossip?
   - Configurable behavior?

3. **Should we support multiple VPN interfaces?**
   - Different VPNs for different regions?
   - Backup VPN tunnels?

4. **Discovery mechanism for VPN peers?**
   - Static configuration (current design)
   - DNS-based discovery?
   - Hive gossip for VPN address exchange?
