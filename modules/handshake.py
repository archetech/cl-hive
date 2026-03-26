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

    def _delete_handshake_state(self, peer_id: str, state_type: str) -> None:
        """Best-effort delete of persisted handshake state."""
        if not self.db:
            return
        try:
            self.db.delete_handshake_state(peer_id, state_type)
        except Exception:
            pass
    
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
        self.expire_pending_requests()
        if len(self._pending_requests) >= MAX_PENDING_REQUESTS:
            oldest = min(self._pending_requests, key=lambda k: self._pending_requests[k]["received_at"])
            del self._pending_requests[oldest]
            self._delete_handshake_state(oldest, "pending_request")

        now = int(time.time())
        request_data = {
            "peer_id": peer_id,
            "received_at": now,
            "channel_verified": True,
        }
        self._pending_requests[peer_id] = request_data

        if self.db:
            try:
                self.db.upsert_handshake_state(
                    peer_id, "pending_request",
                    json.dumps(request_data),
                    now, now + PENDING_REQUEST_MAX_AGE
                )
            except Exception:
                pass

    def get_pending_requests(self) -> List[Dict]:
        """Return all pending join requests (memory + DB fallback)."""
        result = dict(self._pending_requests)
        if self.db:
            try:
                for row in self.db.get_all_handshake_states("pending_request"):
                    pid = row.get("peer_id")
                    if pid and pid not in result:
                        try:
                            result[pid] = json.loads(row.get("data", "{}"))
                        except (json.JSONDecodeError, TypeError):
                            pass
            except Exception:
                pass
        return list(result.values())

    def get_pending_request(self, peer_id: str) -> Optional[Dict]:
        """Return a single pending request without removing it."""
        request = self._pending_requests.get(peer_id)
        if request is not None:
            return request
        if self.db:
            try:
                row = self.db.get_handshake_state(peer_id, "pending_request")
                if row and row.get("data"):
                    return json.loads(row["data"])
            except Exception:
                pass
        return None

    def pop_pending_request(self, peer_id: str) -> Optional[Dict]:
        """Remove and return a pending request, or None if not found."""
        result = self._pending_requests.pop(peer_id, None)
        if result is None and self.db:
            try:
                row = self.db.get_handshake_state(peer_id, "pending_request")
                if row and row.get("data"):
                    result = json.loads(row["data"])
            except Exception:
                pass
        if self.db:
            try:
                self.db.delete_handshake_state(peer_id, "pending_request")
            except Exception:
                pass
        return result

    def expire_pending_requests(self, max_age_seconds: int = PENDING_REQUEST_MAX_AGE) -> int:
        """Remove pending requests older than max_age_seconds. Returns count removed."""
        now = int(time.time())
        expired = [
            pid for pid, req in self._pending_requests.items()
            if now - req["received_at"] > max_age_seconds
        ]
        for pid in expired:
            del self._pending_requests[pid]
            self._delete_handshake_state(pid, "pending_request")
        if self.db:
            try:
                self.db.cleanup_expired_handshake_states()
            except Exception:
                pass
        return len(expired)

    # =========================================================================
    # OUTBOUND JOIN TRACKING
    # =========================================================================

    def record_hello_sent(self, peer_id: str) -> None:
        """Record that we sent a HELLO to a peer (outbound join request)."""
        now = int(time.time())
        if len(self._outbound_hello_sent) >= MAX_OUTBOUND_HELLOS:
            expired = [k for k, ts in self._outbound_hello_sent.items()
                       if now - ts > PENDING_REQUEST_MAX_AGE]
            for k in expired:
                del self._outbound_hello_sent[k]
                self._delete_handshake_state(k, "outbound_hello")
            if len(self._outbound_hello_sent) >= MAX_OUTBOUND_HELLOS:
                oldest = min(self._outbound_hello_sent, key=self._outbound_hello_sent.get)
                del self._outbound_hello_sent[oldest]
                self._delete_handshake_state(oldest, "outbound_hello")

        self._outbound_hello_sent[peer_id] = now

        if self.db:
            try:
                self.db.upsert_handshake_state(
                    peer_id, "outbound_hello",
                    json.dumps({"sent_at": now}),
                    now, now + PENDING_REQUEST_MAX_AGE
                )
            except Exception:
                pass

    def has_pending_outbound_hello(self, peer_id: str, max_age_seconds: int = PENDING_REQUEST_MAX_AGE) -> bool:
        """Check if we have a pending outbound HELLO to this peer."""
        ts = self._outbound_hello_sent.get(peer_id)
        if ts is not None:
            if int(time.time()) - ts > max_age_seconds:
                del self._outbound_hello_sent[peer_id]
                self._delete_handshake_state(peer_id, "outbound_hello")
                return False
            return True
        # DB fallback (post-restart)
        if self.db:
            try:
                row = self.db.get_handshake_state(peer_id, "outbound_hello")
                if row:
                    data = json.loads(row.get("data", "{}"))
                    self._outbound_hello_sent[peer_id] = data.get("sent_at", row.get("created_at", 0))
                    return True
            except Exception:
                pass
        return False

    def clear_outbound_hello(self, peer_id: str) -> None:
        """Clear outbound HELLO tracking after join completes."""
        self._outbound_hello_sent.pop(peer_id, None)
        if self.db:
            try:
                self.db.delete_handshake_state(peer_id, "outbound_hello")
            except Exception:
                pass

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

            # Persist to DB inside lock for atomicity
            if self.db:
                try:
                    self.db.upsert_handshake_state(
                        peer_id, "pending_challenge",
                        json.dumps(self._pending_challenges[peer_id]),
                        now, now + CHALLENGE_TTL_SECONDS
                    )
                except Exception:
                    pass

            # LRU eviction if over limit
            if len(self._pending_challenges) > MAX_PENDING_CHALLENGES:
                oldest = sorted(
                    self._pending_challenges.items(),
                    key=lambda item: item[1]["issued_at"]
                )
                for key, _ in oldest[: len(self._pending_challenges) - MAX_PENDING_CHALLENGES]:
                    self._pending_challenges.pop(key, None)
                    self._delete_handshake_state(key, "pending_challenge")

            # Sweep expired challenges (TTL-based expiry)
            expired = [k for k, v in self._pending_challenges.items()
                       if now - v['issued_at'] > CHALLENGE_TTL_SECONDS]
            for k in expired:
                del self._pending_challenges[k]
                self._delete_handshake_state(k, "pending_challenge")

        return nonce

    def get_pending_challenge(self, peer_id: str) -> Optional[Dict[str, Any]]:
        """Get the pending challenge nonce for a peer."""
        with self._challenge_lock:
            challenge = self._pending_challenges.get(peer_id)
            if challenge is not None:
                now = int(time.time())
                if now - challenge["issued_at"] > CHALLENGE_TTL_SECONDS:
                    self._pending_challenges.pop(peer_id, None)
                    self._delete_handshake_state(peer_id, "pending_challenge")
                    return None
                return challenge
            # DB fallback (post-restart)
            if self.db:
                try:
                    row = self.db.get_handshake_state(peer_id, "pending_challenge")
                    if row and row.get("data"):
                        challenge = json.loads(row["data"])
                        self._pending_challenges[peer_id] = challenge
                        return challenge
                except Exception:
                    pass
            return None

    def clear_challenge(self, peer_id: str) -> None:
        """Clear the pending challenge for a peer."""
        if self.db:
            try:
                self.db.delete_handshake_state(peer_id, "pending_challenge")
            except Exception:
                pass
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
