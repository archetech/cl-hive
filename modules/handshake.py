"""
Handshake module for cl-hive

Implements the PKI-based authentication protocol:
- Genesis: Create a new Hive as the founding Member
- Manifest: Create and verify capability attestations
- Challenge-Response: Prove identity via HSM signatures

Crypto Strategy:
    Uses Core Lightning's signmessage/checkmessage RPCs.
    Keys never leave the HSM. No external crypto libraries required.

Join Flow (Channel-as-Proof-of-Stake):
    A node with a channel to any hive member can join:
    1. A -> B (HELLO): Candidate announces pubkey
    2. B stores a pending request for A (awaiting hive-approve)
    3. Operator runs hive-approve <peer_id>
    4. B -> A (CHALLENGE): Member sends random Nonce
    5. A -> B (ATTEST): Candidate sends signed Manifest + Nonce
    6. B -> A (WELCOME): New member joins as member
"""

import json
import threading
import time
import hashlib
import secrets
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, asdict


# =============================================================================
# CONSTANTS
# =============================================================================

# Nonce size in bytes (32 bytes = 64 hex chars)
NONCE_SIZE = 32

# Challenge time-to-live in seconds
CHALLENGE_TTL_SECONDS = 300

# Cap to prevent unbounded pending challenge growth
MAX_PENDING_CHALLENGES = 1000

# Cap to prevent unbounded pending request / outbound hello growth
MAX_PENDING_REQUESTS = 1000
MAX_OUTBOUND_HELLOS = 1000

# SECURITY (Issue #11): Per-peer rate limit for challenge generation
CHALLENGE_RATE_LIMIT_SECONDS = 10  # Minimum seconds between challenges per peer

# Plugin version for manifest
PLUGIN_VERSION = "cl-hive v2.2.6"

# Default pending request expiry (24 hours)
PENDING_REQUEST_MAX_AGE = 86400


@dataclass
class Manifest:
    """
    Capability manifest structure.
    
    A node creates this to prove its identity and capabilities
    during the handshake process.
    """
    pubkey: str             # Node's public key
    version: str            # Plugin version
    features: list          # Supported features
    timestamp: int          # Creation timestamp
    nonce: str              # Challenge nonce being responded to
    
    def to_json(self) -> str:
        """Serialize to JSON for signing."""
        return json.dumps(asdict(self), sort_keys=True, separators=(',', ':'))


# =============================================================================
# REQUIREMENT FLAGS (Bitmask)
# =============================================================================

class Requirements:
    """Feature requirement bitmask values."""
    NONE = 0
    SPLICE = 1 << 0         # Node must support splicing
    DUAL_FUND = 1 << 1      # Node must support dual-funded channels
    ANCHOR = 1 << 2         # Node must support anchor outputs
    ONION_MSG = 1 << 3      # Node must support onion messages


# =============================================================================
# HANDSHAKE MANAGER
# =============================================================================

class HandshakeManager:
    """
    Manages Hive authentication and session establishment.

    Handles:
    - Genesis (creating a new Hive as founding member)
    - Manifest creation and verification
    - Challenge-response protocol for identity proof
    - Pending join-request storage (awaiting hive-approve)

    Join Flow:
    Nodes join by having a channel with any existing member.
    Channel existence serves as proof of stake. An existing member
    must explicitly approve the join request via hive-approve.
    """

    def __init__(self, rpc_proxy, db, plugin):
        """
        Initialize the handshake manager.

        Args:
            rpc_proxy: ThreadSafeRpcProxy for CLN RPC calls
            db: HiveDatabase instance
            plugin: Plugin reference for logging
        """
        self.rpc = rpc_proxy
        self.db = db
        self.plugin = plugin
        self._our_pubkey: Optional[str] = None
        self._challenge_lock = threading.Lock()
        self._pending_challenges: Dict[str, Dict[str, Any]] = {}
        self._pending_requests: Dict[str, Dict] = {}
        self._outbound_hello_sent: Dict[str, int] = {}  # peer_id -> timestamp
    
    # =========================================================================
    # IDENTITY
    # =========================================================================
    
    def get_our_pubkey(self) -> str:
        """Get our node's public key (cached)."""
        if self._our_pubkey is None:
            info = self.rpc.getinfo()
            self._our_pubkey = info['id']
        return self._our_pubkey
    
    # =========================================================================
    # GENESIS
    # =========================================================================
    
    def genesis(self, hive_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Create a new Hive with this node as the founding Member.

        This bootstraps a new Hive. The founding node starts as a member
        and can approve others. Other nodes join by opening a channel to
        any existing member and awaiting approval.

        Args:
            hive_id: Optional custom Hive ID (auto-generated if not provided)

        Returns:
            Dict with genesis info (hive_id, member_pubkey, etc.)

        Raises:
            ValueError: If this node is already part of a Hive
        """
        our_pubkey = self.get_our_pubkey()

        # Check if we're already in a Hive
        existing = self.db.get_member(our_pubkey)
        if existing:
            raise ValueError(f"Already member of Hive (tier: {existing['tier']})")

        # Generate Hive ID if not provided
        if hive_id is None:
            hive_id = f"hive_{secrets.token_hex(8)}"

        now = int(time.time())

        # Store ourselves as founding member
        self.db.add_member(
            peer_id=our_pubkey,
            tier='member',
            joined_at=now,
        )

        # Store hive metadata
        self.db.update_member(
            our_pubkey,
            metadata=json.dumps({"hive_id": hive_id})
        )

        self.plugin.log(f"Genesis complete: Hive '{hive_id}' created")

        return {
            "status": "genesis_complete",
            "hive_id": hive_id,
            "member_pubkey": our_pubkey,
        }
    
    # =========================================================================
    # PENDING JOIN REQUESTS
    # =========================================================================

    def store_pending_request(self, peer_id: str) -> None:
        """Store a pending join request from a peer (awaiting hive-approve)."""
        # Expire old requests and enforce size cap
        self.expire_pending_requests()
        if len(self._pending_requests) >= MAX_PENDING_REQUESTS:
            oldest = min(self._pending_requests, key=lambda k: self._pending_requests[k]["received_at"])
            del self._pending_requests[oldest]

        self._pending_requests[peer_id] = {
            "peer_id": peer_id,
            "received_at": int(time.time()),
            "channel_verified": True,
        }

    def get_pending_requests(self) -> List[Dict]:
        """Return all pending join requests."""
        return list(self._pending_requests.values())

    def pop_pending_request(self, peer_id: str) -> Optional[Dict]:
        """Remove and return a pending request, or None if not found."""
        return self._pending_requests.pop(peer_id, None)

    def expire_pending_requests(self, max_age_seconds: int = PENDING_REQUEST_MAX_AGE) -> int:
        """Remove pending requests older than max_age_seconds. Returns count removed."""
        now = int(time.time())
        expired = [
            pid for pid, req in self._pending_requests.items()
            if now - req["received_at"] > max_age_seconds
        ]
        for pid in expired:
            del self._pending_requests[pid]
        return len(expired)

    # =========================================================================
    # OUTBOUND JOIN TRACKING
    # =========================================================================

    def record_hello_sent(self, peer_id: str) -> None:
        """Record that we sent a HELLO to a peer (outbound join request)."""
        # Expire old entries and enforce size cap
        now = int(time.time())
        if len(self._outbound_hello_sent) >= MAX_OUTBOUND_HELLOS:
            expired = [k for k, ts in self._outbound_hello_sent.items()
                       if now - ts > PENDING_REQUEST_MAX_AGE]
            for k in expired:
                del self._outbound_hello_sent[k]
            if len(self._outbound_hello_sent) >= MAX_OUTBOUND_HELLOS:
                oldest = min(self._outbound_hello_sent, key=self._outbound_hello_sent.get)
                del self._outbound_hello_sent[oldest]

        self._outbound_hello_sent[peer_id] = now

    def has_pending_outbound_hello(self, peer_id: str, max_age_seconds: int = PENDING_REQUEST_MAX_AGE) -> bool:
        """Check if we have a pending outbound HELLO to this peer."""
        ts = self._outbound_hello_sent.get(peer_id)
        if ts is None:
            return False
        if int(time.time()) - ts > max_age_seconds:
            del self._outbound_hello_sent[peer_id]
            return False
        return True

    def clear_outbound_hello(self, peer_id: str) -> None:
        """Clear outbound HELLO tracking after join completes."""
        self._outbound_hello_sent.pop(peer_id, None)

    # =========================================================================
    # MANIFEST OPERATIONS
    # =========================================================================
    
    def create_manifest(self, nonce: str, features: Optional[list] = None) -> Dict[str, Any]:
        """
        Create a signed manifest for attestation.
        
        Args:
            nonce: Challenge nonce to include
            features: List of supported features (auto-detected if None)
            
        Returns:
            Dict with manifest data and signatures
        """
        our_pubkey = self.get_our_pubkey()
        
        if features is None:
            features = self._detect_features()
        
        manifest = Manifest(
            pubkey=our_pubkey,
            version=PLUGIN_VERSION,
            features=features,
            timestamp=int(time.time()),
            nonce=nonce
        )
        
        manifest_json = manifest.to_json()
        
        # Sign both the nonce and the full manifest
        nonce_sig = self.rpc.signmessage(nonce).get('zbase', '')
        manifest_sig = self.rpc.signmessage(manifest_json).get('zbase', '')
        
        return {
            "manifest": asdict(manifest),
            "nonce_signature": nonce_sig,
            "manifest_signature": manifest_sig
        }
    
    def verify_manifest(self, manifest_data: Dict, nonce_sig: str, 
                        manifest_sig: str, expected_nonce: str) -> Tuple[bool, str]:
        """
        Verify a manifest attestation.
        
        Args:
            manifest_data: Manifest dictionary
            nonce_sig: Signature of the nonce
            manifest_sig: Signature of the manifest JSON
            expected_nonce: The nonce we challenged with
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        pubkey = manifest_data.get('pubkey')
        
        # Verify nonce matches
        if manifest_data.get('nonce') != expected_nonce:
            return (False, "Nonce mismatch")
        
        # Verify nonce signature (pass pubkey for nodes not in gossip graph)
        try:
            result = self.rpc.checkmessage(expected_nonce, nonce_sig, pubkey)
            if not result.get('verified') or result.get('pubkey') != pubkey:
                return (False, "Invalid nonce signature")
        except Exception as e:
            return (False, f"Nonce verification failed: {e}")

        # Verify manifest signature (pass pubkey for nodes not in gossip graph)
        manifest_json = json.dumps(manifest_data, sort_keys=True, separators=(',', ':'))
        try:
            result = self.rpc.checkmessage(manifest_json, manifest_sig, pubkey)
            if not result.get('verified') or result.get('pubkey') != pubkey:
                return (False, "Invalid manifest signature")
        except Exception as e:
            return (False, f"Manifest verification failed: {e}")
        
        return (True, "")
    
    # =========================================================================
    # CHALLENGE-RESPONSE
    # =========================================================================
    
    def generate_challenge(self, peer_id: str, requirements: int,
                            initial_tier: str = 'member') -> str:
        """
        Generate a challenge nonce for a peer.

        Args:
            peer_id: Peer's public key
            requirements: Bitmask of required capabilities
            initial_tier: Starting tier for new member (always 'member')

        Returns:
            Hex-encoded random nonce

        Raises:
            ValueError: If rate limit exceeded for this peer

        SECURITY (Issue #11): Per-peer rate limiting to prevent DoS via
        challenge flooding that would evict legitimate pending challenges.
        """
        now = int(time.time())

        with self._challenge_lock:
            # Check per-peer rate limit
            existing = self._pending_challenges.get(peer_id)
            if existing:
                time_since_last = now - existing["issued_at"]
                if time_since_last < CHALLENGE_RATE_LIMIT_SECONDS:
                    raise ValueError(
                        f"Rate limit exceeded: wait {CHALLENGE_RATE_LIMIT_SECONDS - time_since_last}s"
                    )

            nonce = secrets.token_hex(NONCE_SIZE)
            self._pending_challenges[peer_id] = {
                "nonce": nonce,
                "issued_at": now,
                "requirements": requirements,
                "initial_tier": initial_tier
            }

            # LRU eviction if over limit
            if len(self._pending_challenges) > MAX_PENDING_CHALLENGES:
                oldest = sorted(
                    self._pending_challenges.items(),
                    key=lambda item: item[1]["issued_at"]
                )
                for key, _ in oldest[: len(self._pending_challenges) - MAX_PENDING_CHALLENGES]:
                    self._pending_challenges.pop(key, None)

            # Sweep expired challenges (TTL-based expiry)
            expired = [k for k, v in self._pending_challenges.items()
                       if now - v['issued_at'] > CHALLENGE_TTL_SECONDS]
            for k in expired:
                del self._pending_challenges[k]

        return nonce

    def get_pending_challenge(self, peer_id: str) -> Optional[Dict[str, Any]]:
        """Get the pending challenge nonce for a peer."""
        with self._challenge_lock:
            challenge = self._pending_challenges.get(peer_id)
            if challenge is None:
                return None
            # Enforce TTL - expire stale challenges
            now = int(time.time())
            if now - challenge["issued_at"] > CHALLENGE_TTL_SECONDS:
                self._pending_challenges.pop(peer_id, None)
                return None
            return challenge

    def clear_challenge(self, peer_id: str) -> None:
        """Clear the pending challenge for a peer."""
        with self._challenge_lock:
            self._pending_challenges.pop(peer_id, None)
    
    # =========================================================================
    # FEATURE DETECTION
    # =========================================================================
    
    def _detect_features(self) -> list:
        """
        Detect supported features on this node.
        
        Returns:
            List of feature strings
        """
        features = []
        
        try:
            # Check for splice support
            config = self.rpc.listconfigs()
            if config.get('experimental-splicing'):
                features.append('splice')
            if config.get('experimental-dual-fund'):
                features.append('dual-fund')
            if config.get('experimental-onion-messages'):
                features.append('onion-msg')
        except Exception:
            pass

        # Advertise max supported protocol version (Phase B hardening)
        from modules.protocol import SUPPORTED_VERSIONS
        features.append(f'proto-v{max(SUPPORTED_VERSIONS)}')

        return features
    
    def check_requirements(self, requirements: int, features: list) -> Tuple[bool, list]:
        """
        Check if features satisfy requirements bitmask.
        
        Args:
            requirements: Bitmask of required features
            features: List of available features
            
        Returns:
            Tuple of (satisfied, missing_features)
        """
        missing = []
        
        if requirements & Requirements.SPLICE and 'splice' not in features:
            missing.append('splice')
        if requirements & Requirements.DUAL_FUND and 'dual-fund' not in features:
            missing.append('dual-fund')
        if requirements & Requirements.ANCHOR and 'anchor' not in features:
            missing.append('anchor')
        if requirements & Requirements.ONION_MSG and 'onion-msg' not in features:
            missing.append('onion-msg')
        
        return (len(missing) == 0, missing)
