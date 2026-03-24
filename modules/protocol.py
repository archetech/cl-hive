"""
Protocol module for cl-hive

Implements BOLT 8 custom message types for Hive communication.

Wire Format:
    All messages use a 4-byte magic prefix (0x48495645 = "HIVE") to avoid
    collisions with other plugins using the experimental message range.

    ┌────────────────────┬────────────────────────────────────┐
    │  Magic Bytes (4)   │           Payload (N)              │
    ├────────────────────┼────────────────────────────────────┤
    │     0x48495645     │  [Message-Type-Specific Content]   │
    │     ("HIVE")       │                                    │
    └────────────────────┴────────────────────────────────────┘

Message ID Range: 32769 - 33000 (Odd numbers for safe ignoring by non-Hive peers)
"""

import hashlib
import json
import time
from enum import IntEnum
from typing import Dict, Any, List, Optional, Tuple


# =============================================================================
# CONSTANTS
# =============================================================================

# 4-byte magic prefix: ASCII "HIVE" = 0x48 0x49 0x56 0x45
HIVE_MAGIC = b'HIVE'

# Protocol version for compatibility checks
PROTOCOL_VERSION = 1

# Version tolerance: accept messages from this range of protocol versions.
# Prevents fleet partition during rolling upgrades (Phase B hardening).
MIN_SUPPORTED_VERSION = 1
MAX_SUPPORTED_VERSION = 2
SUPPORTED_VERSIONS = set(range(MIN_SUPPORTED_VERSION, MAX_SUPPORTED_VERSION + 1))

# Maximum message size in bytes (post-hex decode)
MAX_MESSAGE_BYTES = 65535

# Shared boundary for FULL_SYNC payloads
MAX_FULL_SYNC_STATES = 500

# Maximum peer_id length (hex-encoded pubkey should be 66 chars, allow some margin)
MAX_PEER_ID_LEN = 128

# Maximum length for freeform string fields
MAX_REASON_LEN = 512

# Strict envelope version required by the hardened state-sync helpers
STRICT_STATE_SYNC_VERSION = 2

# =============================================================================
# MESSAGE TYPES
# =============================================================================

class HiveMessageType(IntEnum):
    """
    BOLT 8 custom message IDs for Hive protocol.
    
    Uses odd numbers in experimental range (32768+) so non-Hive nodes
    can safely ignore unknown messages per BOLT 1.
    
    MVP Messages (Phase 1):
        HELLO, CHALLENGE, ATTEST, WELCOME
    
    Deferred Messages:
        GOSSIP (Phase 2), INTENT (Phase 3), BAN/MEMBERSHIP (Phase 5)
    """
    # Phase 1: Handshake
    HELLO = 32769       # Join request presentation
    CHALLENGE = 32771   # Nonce for proof-of-identity
    ATTEST = 32773      # Signed manifest + nonce response
    WELCOME = 32775     # Session established, HiveID assigned
    
    # Phase 2: State Sync (deferred)
    GOSSIP = 32777      # State update broadcast
    STATE_HASH = 32779  # Anti-entropy hash exchange
    FULL_SYNC = 32781   # Full state sync request/response
    
    # Phase 3: Coordination (deferred)
    INTENT = 32783      # Intent lock announcement
    # 32785 reserved (was INTENT_ACK, removed — unused)
    INTENT_ABORT = 32787  # Intent abort notification
    
    # Membership
    # 32789-32795 removed (VOUCH, PROMOTION, PROMOTION_REQUEST — membership voting)
    BAN = 32791         # Ban announcement (executed ban)
    MEMBER_LEFT = 32797  # Member voluntarily leaving fleet
    # 32799-32801 removed (BAN_PROPOSAL, BAN_VOTE — membership voting)

    # Intelligence sharing
    # 32803-32819 removed (PEER_AVAILABLE, EXPANSION_NOMINATE, EXPANSION_ELECT, EXPANSION_DECLINE)
    # 32821-32823 removed (SETTLEMENT_OFFER, FEE_REPORT — settlement)
    FEE_INTELLIGENCE_SNAPSHOT = 32825  # Batch fee observations for all peers
    PEER_REPUTATION_SNAPSHOT = 32827   # Batch peer reputation for all peers
    # 32829 removed (ROUTE_PROBE_BATCH)
    LIQUIDITY_SNAPSHOT = 32831        # Batch liquidity needs
    LIQUIDITY_NEED = 32811      # Broadcast rebalancing needs
    HEALTH_REPORT = 32813       # NNLB health status report
    # 32815 removed (ROUTE_PROBE)

    # 32833-32835 removed (TASK_REQUEST, TASK_RESPONSE — task delegation)
    # 32837-32845 removed (SPLICE_INIT_REQUEST..SPLICE_ABORT — splice coordination)
    # 32847-32851 removed (SETTLEMENT_PROPOSE, SETTLEMENT_READY, SETTLEMENT_EXECUTED)
    # 32853-32855 removed (STIGMERGIC_MARKER_BATCH, PHEROMONE_BATCH — fee coordination)

    # Fleet-wide intelligence
    YIELD_METRICS_BATCH = 32857    # Per-channel ROI and profitability metrics
    # 32859 removed (CIRCULAR_FLOW_ALERT — fee coordination)
    TEMPORAL_PATTERN_BATCH = 32861 # Hour/day flow patterns and predictions
    CORRIDOR_VALUE_BATCH = 32863   # High-value routing corridors discovered
    POSITIONING_PROPOSAL = 32865   # Channel open recommendation for fleet coordination
    # 32867 removed (PHYSARUM_RECOMMENDATION — fee coordination)
    COVERAGE_ANALYSIS_BATCH = 32869 # Peer coverage and ownership analysis
    CLOSE_PROPOSAL = 32871         # Channel close recommendation for redundancy

    # 32873-32879 removed (MCF_NEEDS_BATCH..MCF_COMPLETION_REPORT — MCF optimization)
    # 32881 removed (MSG_ACK — reliable delivery)
    # 32883-32889 removed (DID/MGMT credentials)
    # 32891-32903 removed (extended settlements, bonds, netting, violations, arbitration)

    # Traffic intelligence
    TRAFFIC_INTELLIGENCE_BATCH = 32905


# =============================================================================
# RELIABLE DELIVERY CONSTANTS (kept minimal for MEMBER_LEFT and traffic intel)
# =============================================================================

# Message types that require reliable delivery
RELIABLE_MESSAGE_TYPES = frozenset({
    HiveMessageType.MEMBER_LEFT,
    HiveMessageType.TRAFFIC_INTELLIGENCE_BATCH,
})

# Implicit ack mapping: no remaining request/response pairs need this
IMPLICIT_ACK_MAP = {}

# Field in the response payload that matches the request for implicit acks
IMPLICIT_ACK_MATCH_FIELD = {}


# =============================================================================
# VALIDATION CONSTANTS
# =============================================================================

# Maximum length of request_id
MAX_REQUEST_ID_LEN = 64

# Fee intelligence bounds
MAX_FEE_PPM = 10000              # Maximum fee rate (1%)
MAX_VOLUME_SATS = 1_000_000_000_000  # 10k BTC max volume
MAX_DAYS_OBSERVED = 365          # Maximum observation period
FEE_INTELLIGENCE_MAX_AGE = 3600  # 1 hour max message age

# Liquidity need bounds
MAX_LIQUIDITY_AMOUNT = 100_000_000_000  # 1000 BTC max
VALID_NEED_TYPES = {'inbound', 'outbound', 'rebalance'}
VALID_URGENCY_LEVELS = {'critical', 'high', 'medium', 'low'}
VALID_FLOW_DIRECTIONS = {'source', 'sink', 'balanced'}

# Health report bounds
MAX_HEALTH_SCORE = 100
MIN_HEALTH_SCORE = 0

# Rate limits (count, period_seconds)
FEE_INTELLIGENCE_SNAPSHOT_RATE_LIMIT = (2, 3600)  # 2 snapshots per hour per sender
MAX_PEERS_IN_SNAPSHOT = 200                 # Maximum peers in one snapshot message
LIQUIDITY_NEED_RATE_LIMIT = (5, 3600)       # 5 per hour per sender
LIQUIDITY_SNAPSHOT_RATE_LIMIT = (2, 3600)  # 2 snapshots per hour per sender
MAX_NEEDS_IN_SNAPSHOT = 50                 # Maximum liquidity needs in one snapshot message
HEALTH_REPORT_RATE_LIMIT = (1, 3600)        # 1 per hour per sender
PEER_REPUTATION_SNAPSHOT_RATE_LIMIT = (2, 86400)  # 2 snapshots per day per sender
MAX_PEERS_IN_REPUTATION_SNAPSHOT = 200      # Maximum peers in one reputation snapshot

# Yield metrics sharing constants (Phase 14)
YIELD_METRICS_BATCH_RATE_LIMIT = (1, 86400)  # 1 batch per day per sender
MAX_YIELD_METRICS_IN_BATCH = 200             # Maximum channels in one batch
MIN_YIELD_ROI_TO_SHARE = -100.0              # Share even underwater channels (negative ROI)
YIELD_WEIGHTING_FACTOR = 0.4                 # How much to weight remote yield data

# Temporal pattern sharing constants (Phase 14)
TEMPORAL_PATTERN_BATCH_RATE_LIMIT = (4, 86400)  # 4 batches per day (every 6 hours)
MAX_PATTERNS_IN_BATCH = 500                     # Maximum patterns in one batch
MAX_TEMPORAL_PATTERNS_IN_BATCH = MAX_PATTERNS_IN_BATCH  # Alias for consistency
MIN_PATTERN_CONFIDENCE = 0.6                    # Minimum confidence to share
MIN_PATTERN_SAMPLES = 10                        # Minimum samples for pattern validity
MIN_TEMPORAL_PATTERN_CONFIDENCE = MIN_PATTERN_CONFIDENCE  # Alias
MIN_TEMPORAL_PATTERN_SAMPLES = MIN_PATTERN_SAMPLES        # Alias

# Strategic positioning sharing constants (Phase 14.2)
CORRIDOR_VALUE_BATCH_RATE_LIMIT = (2, 86400)    # 2 batches per day (every 12 hours)
MAX_CORRIDORS_IN_BATCH = 100                    # Maximum corridors in one batch
MIN_CORRIDOR_VALUE_SCORE = 0.05                 # Minimum value score to share
POSITIONING_PROPOSAL_RATE_LIMIT = (5, 86400)    # 5 proposals per day
MAX_POSITIONING_PROPOSALS_PER_CYCLE = 5         # Alias for broadcast function
VALID_PRIORITY_TIERS = {"critical", "high", "medium", "low"}

# Channel rationalization sharing constants (Phase 14.2)
COVERAGE_ANALYSIS_BATCH_RATE_LIMIT = (2, 86400) # 2 batches per day
MAX_COVERAGE_ENTRIES_IN_BATCH = 200             # Maximum coverage entries
MIN_COVERAGE_OWNERSHIP_CONFIDENCE = 0.5         # Minimum confidence to share ownership
CLOSE_PROPOSAL_RATE_LIMIT = (5, 86400)          # 5 close proposals per day
MAX_CLOSE_PROPOSALS_PER_CYCLE = 5               # Alias for broadcast function

# Traffic intelligence bounds
VALID_PROFILE_TYPES = {'retail', 'wholesale', 'burst', 'steady', 'mixed'}
VALID_DRAIN_DIRECTIONS = {'inbound_heavy', 'outbound_heavy', 'balanced'}
MAX_PROFILES_IN_BATCH = 200
TRAFFIC_INTELLIGENCE_MAX_AGE = 48 * 3600  # 48 hours
TRAFFIC_INTELLIGENCE_BATCH_RATE_LIMIT = (1, 6 * 3600)  # 1 per 6 hours per sender
MAX_DAILY_VOLUME_SATS = 1_000_000_000_000  # 10k BTC
MAX_FORWARD_SIZE_SATS = 100_000_000_000  # 1k BTC
MAX_OBSERVATION_WINDOW_HOURS = 720  # 30 days

# Peer reputation constants
MAX_RESPONSE_TIME_MS = 60000                # 60 seconds max response time
MAX_FORCE_CLOSE_COUNT = 100                 # Reasonable max for tracking
MAX_CHANNEL_AGE_DAYS = 3650                 # 10 years max
MAX_OBSERVATION_DAYS = 365                  # 1 year max observation period
MAX_WARNINGS_COUNT = 10                     # Max warnings per report
MAX_WARNING_LENGTH = 200                    # Max length of each warning
VALID_WARNINGS = {
    "fee_spike",           # Sudden fee increase
    "force_close",         # Initiated force close
    "htlc_timeout",        # HTLC timeouts
    "offline_frequent",    # Frequently offline
    "channel_reject",      # Rejected channel opens
    "routing_failure",     # High routing failure rate
    "slow_response",       # Slow HTLC processing
    "fee_manipulation",    # Suspected fee manipulation
    "capacity_drain",      # Draining liquidity
    "other",               # Other issues
}


# =============================================================================
# SERIALIZATION
# =============================================================================

def serialize(msg_type: HiveMessageType, payload: Dict[str, Any]) -> Optional[bytes]:
    """
    Serialize a Hive message for transmission via sendcustommsg.
    
    Format: MAGIC (4 bytes) + JSON payload
    
    Args:
        msg_type: HiveMessageType enum value
        payload: Dictionary to serialize as JSON
        
    Returns:
        bytes: Wire-ready message with magic prefix
        
    Example:
        >>> data = serialize(HiveMessageType.HELLO, {"pubkey": "02abc123..."})
        >>> data[:4]
        b'HIVE'
    """
    # Add message type to payload for deserialization
    envelope = {
        "type": int(msg_type),
        "version": PROTOCOL_VERSION,
        "payload": payload
    }
    
    # JSON encode
    json_bytes = json.dumps(envelope, separators=(',', ':')).encode('utf-8')

    # Prepend magic
    result = HIVE_MAGIC + json_bytes

    # Size check: reject messages exceeding wire limit
    if len(result) > MAX_MESSAGE_BYTES:
        import logging
        logging.getLogger(__name__).warning(
            f"serialize: message too large ({len(result)} bytes > {MAX_MESSAGE_BYTES}), dropping"
        )
        return None

    return result


def deserialize(data: bytes) -> Tuple[Optional[HiveMessageType], Optional[Dict[str, Any]]]:
    """
    Deserialize a Hive message received via custommsg hook.
    
    Performs magic byte verification before attempting JSON parse.
    
    Args:
        data: Raw bytes from custommsg event
        
    Returns:
        Tuple of (message_type, payload) if valid Hive message
        Tuple of (None, None) if magic check fails or parse error
        
    Example:
        >>> msg_type, payload = deserialize(data)
        >>> if msg_type is None:
        ...     return {"result": "continue"}  # Not our message
    """
    # Peek & Check: Verify magic prefix
    if len(data) < 4 or len(data) > MAX_MESSAGE_BYTES:
        return (None, None)
    
    if data[:4] != HIVE_MAGIC:
        return (None, None)
    
    # Strip magic and parse JSON
    try:
        json_data = data[4:].decode('utf-8')
        envelope = json.loads(json_data)
        
        if envelope.get('version') not in SUPPORTED_VERSIONS:
            return (None, None)

        msg_type = HiveMessageType(envelope['type'])
        payload = envelope.get('payload', {})
        if not isinstance(payload, dict):
            return (None, None)

        # Inject envelope version so handlers can check it without
        # changing the function signature (Phase B hardening).
        payload['_envelope_version'] = envelope.get('version')

        return (msg_type, payload)
        
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        # Malformed message - log would go here in production
        return (None, None)


def is_hive_message(data: bytes) -> bool:
    """
    Quick check if data is a Hive message (magic prefix only).
    
    Use this for fast rejection in custommsg hook before full deserialization.
    
    Args:
        data: Raw bytes from custommsg event
        
    Returns:
        True if magic prefix matches, False otherwise
    """
    return len(data) >= 4 and data[:4] == HIVE_MAGIC


# =============================================================================
# PAYLOAD VALIDATION
# =============================================================================

def validate_member_left(payload: Dict[str, Any]) -> bool:
    """Validate MEMBER_LEFT payload schema."""
    if not isinstance(payload, dict):
        return False
    peer_id = payload.get("peer_id")
    timestamp = payload.get("timestamp")
    reason = payload.get("reason")
    signature = payload.get("signature")

    # peer_id must be valid pubkey (66 hex chars)
    if not isinstance(peer_id, str) or len(peer_id) != 66:
        return False
    if not all(c in "0123456789abcdef" for c in peer_id):
        return False

    # timestamp must be positive integer
    if not isinstance(timestamp, int) or timestamp < 0:
        return False

    # reason must be a non-empty string
    if not isinstance(reason, str) or not reason:
        return False
    if len(reason) > MAX_REASON_LEN:
        return False

    # signature must be present (zbase encoded)
    if not isinstance(signature, str) or not signature:
        return False

    return True


def _valid_pubkey(pubkey: Any) -> bool:
    """Check if value is a valid 66-char hex pubkey with 02/03 prefix."""
    if not isinstance(pubkey, str) or len(pubkey) != 66:
        return False
    if not (pubkey.startswith('02') or pubkey.startswith('03')):
        return False
    return all(c in "0123456789abcdef" for c in pubkey)


# =============================================================================
# STATE MANAGEMENT MESSAGE VALIDATION
# =============================================================================

def validate_gossip(payload: Dict[str, Any]) -> bool:
    """
    Validate GOSSIP payload schema.

    SECURITY: Requires cryptographic signature from the sender.
    """
    if not isinstance(payload, dict):
        return False

    sender_id = payload.get("sender_id")
    timestamp = payload.get("timestamp")
    signature = payload.get("signature")

    # sender_id must be valid pubkey
    if not _valid_pubkey(sender_id):
        return False

    # timestamp must be positive integer
    if not isinstance(timestamp, int) or timestamp < 0:
        return False

    # SECURITY: Signature must be present
    if not isinstance(signature, str) or len(signature) < 10:
        return False

    # version must be a positive integer if present
    version = payload.get("version")
    if version is not None and (not isinstance(version, int) or version < 0):
        return False

    # Budget fields (Phase 8 - optional, backward compatible)
    # Validate only if present, must be non-negative integers
    budget_available = payload.get("budget_available_sats")
    if budget_available is not None:
        if not isinstance(budget_available, int) or budget_available < 0:
            return False

    budget_reserved = payload.get("budget_reserved_until")
    if budget_reserved is not None:
        if not isinstance(budget_reserved, int) or budget_reserved < 0:
            return False

    budget_update = payload.get("budget_last_update")
    if budget_update is not None:
        if not isinstance(budget_update, int) or budget_update < 0:
            return False

    return True


def compute_gossip_data_hash(payload: Dict[str, Any]) -> str:
    """
    Compute a hash of the GOSSIP data fields.

    SECURITY: This hash is included in the signature to prevent
    data tampering while keeping the signing payload small.
    """
    data_fields = {
        "capacity_sats": payload.get("capacity_sats", 0),
        "available_sats": payload.get("available_sats", 0),
        "fee_policy": payload.get("fee_policy", {}),
        "topology": sorted(payload.get("topology", [])),  # Sort for determinism
    }
    json_str = json.dumps(data_fields, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(json_str.encode('utf-8')).hexdigest()


def _normalize_string_list(values: Any, field_name: str) -> List[str]:
    """Return a deterministic sorted list of strings or fail closed."""
    if not isinstance(values, list):
        raise ValueError(f"{field_name} must be a list of strings")
    if any(not isinstance(v, str) for v in values):
        raise ValueError(f"{field_name} must contain only strings")
    return sorted(values)


def compute_gossip_data_hash_v2(payload: Dict[str, Any]) -> str:
    """
    Compute a v2 hash of the GOSSIP data fields.

    Normalizes topology, addresses, and capabilities before hashing so the
    signing payload is stable across ordering differences.
    """
    data_fields = {
        "capacity_sats": payload.get("capacity_sats", 0),
        "available_sats": payload.get("available_sats", 0),
        "fee_policy": payload.get("fee_policy", {}),
        "topology": _normalize_string_list(payload.get("topology", []), "topology"),
        "addresses": _normalize_string_list(payload.get("addresses", []), "addresses"),
        "capabilities": _normalize_string_list(payload.get("capabilities", []), "capabilities"),
    }
    json_str = json.dumps(data_fields, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(json_str.encode('utf-8')).hexdigest()


def get_gossip_signing_payload_v2(payload: Dict[str, Any]) -> str:
    """
    Get the canonical payload string for signing v2 GOSSIP messages.
    """
    data_hash = compute_gossip_data_hash_v2(payload)

    signing_fields = {
        "sender_id": payload.get("sender_id", ""),
        "timestamp": payload.get("timestamp", 0),
        "version": payload.get("version", 0),
        "fleet_hash": payload.get("fleet_hash", ""),
        "data_hash": data_hash,
    }
    return json.dumps(signing_fields, sort_keys=True, separators=(',', ':'))


def get_gossip_signing_payload(payload: Dict[str, Any]) -> str:
    """
    Get the canonical payload string for signing GOSSIP messages.

    SECURITY: The signature covers:
    - sender_id: Identity of sender
    - timestamp: Replay protection
    - version: State version for conflict resolution
    - fleet_hash: Overall fleet state hash
    - data_hash: Hash of actual gossip data (fee_policy, topology, capacity)

    This prevents data tampering attacks where an attacker modifies
    the fee policies or topology while keeping the signature valid.
    """
    data_hash = compute_gossip_data_hash(payload)

    signing_fields = {
        "sender_id": payload.get("sender_id", ""),
        "timestamp": payload.get("timestamp", 0),
        "version": payload.get("version", 0),
        "fleet_hash": payload.get("fleet_hash", ""),
        "data_hash": data_hash,
    }
    return json.dumps(signing_fields, sort_keys=True, separators=(',', ':'))


def validate_state_hash(payload: Dict[str, Any]) -> bool:
    """
    Validate STATE_HASH payload schema.

    SECURITY: Requires cryptographic signature from the sender.
    """
    if not isinstance(payload, dict):
        return False

    sender_id = payload.get("sender_id")
    fleet_hash = payload.get("fleet_hash")
    timestamp = payload.get("timestamp")
    signature = payload.get("signature")

    # sender_id must be valid pubkey
    if not _valid_pubkey(sender_id):
        return False

    # fleet_hash must be a string
    if not isinstance(fleet_hash, str) or not fleet_hash:
        return False

    # timestamp must be positive integer
    if not isinstance(timestamp, int) or timestamp < 0:
        return False

    # SECURITY: Signature must be present
    if not isinstance(signature, str) or len(signature) < 10:
        return False

    return True


def get_state_hash_signing_payload(payload: Dict[str, Any]) -> str:
    """
    Get the canonical payload string for signing STATE_HASH messages.

    The signature covers core fields in sorted order.
    """
    signing_fields = {
        "sender_id": payload.get("sender_id", ""),
        "fleet_hash": payload.get("fleet_hash", ""),
        "timestamp": payload.get("timestamp", 0),
        "peer_count": payload.get("peer_count", 0),
    }
    return json.dumps(signing_fields, sort_keys=True, separators=(',', ':'))


def get_state_hash_signing_payload_v2(payload: Dict[str, Any]) -> str:
    """
    Get the canonical payload string for signing v2 STATE_HASH messages.
    """
    signing_fields = {
        "sender_id": payload.get("sender_id", ""),
        "fleet_hash": payload.get("fleet_hash", ""),
        "membership_hash": payload.get("membership_hash", ""),
        "timestamp": payload.get("timestamp", 0),
        "peer_count": payload.get("peer_count", 0),
    }
    return json.dumps(signing_fields, sort_keys=True, separators=(',', ':'))


def validate_full_sync(payload: Dict[str, Any]) -> bool:
    """
    Validate FULL_SYNC payload schema.

    SECURITY: Requires cryptographic signature from the sender.
    This is critical as FULL_SYNC contains membership lists.
    """
    if not isinstance(payload, dict):
        return False

    sender_id = payload.get("sender_id")
    fleet_hash = payload.get("fleet_hash")
    timestamp = payload.get("timestamp")
    signature = payload.get("signature")
    states = payload.get("states")

    # sender_id must be valid pubkey
    if not _valid_pubkey(sender_id):
        return False

    # fleet_hash must be a string
    if not isinstance(fleet_hash, str):
        return False

    # timestamp must be positive integer
    if not isinstance(timestamp, int) or timestamp < 0:
        return False

    # SECURITY: Signature must be present
    if not isinstance(signature, str) or len(signature) < 10:
        return False

    # states must be a list (can be empty)
    if not isinstance(states, list):
        return False

    # Limit states to prevent DoS
    if len(states) > MAX_FULL_SYNC_STATES:
        return False

    return True


def compute_members_hash(members: list) -> str:
    """
    Compute a deterministic hash of the members list.

    SECURITY: This hash is included in the FULL_SYNC signature to prevent
    membership injection attacks. Without this, an attacker could modify
    the members array while keeping the signature valid.

    Args:
        members: List of member dicts with peer_id, tier, joined_at

    Returns:
        Hex-encoded SHA256 hash of the sorted members array
    """
    if not members:
        return ""

    # Extract minimal fields and sort by peer_id for determinism
    member_tuples = [
        {
            "peer_id": m.get("peer_id", ""),
            "tier": m.get("tier", ""),
            "joined_at": m.get("joined_at", 0),
        }
        for m in members
    ]
    member_tuples.sort(key=lambda x: x["peer_id"])

    json_str = json.dumps(member_tuples, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(json_str.encode('utf-8')).hexdigest()


def _normalize_member_row_v2(member: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a FULL_SYNC member row for deterministic hashing."""
    peer_id = member.get("peer_id")
    tier = member.get("tier")
    joined_at = member.get("joined_at")

    if not _valid_pubkey(peer_id):
        raise ValueError("member.peer_id must be a valid pubkey")
    if tier != "member":
        raise ValueError("member.tier must be 'member'")
    if not isinstance(joined_at, int) or joined_at <= 0:
        raise ValueError("member.joined_at must be a positive integer")

    return {
        "peer_id": peer_id,
        "tier": tier,
        "joined_at": joined_at,
        "addresses": _normalize_string_list(member.get("addresses", []), "member.addresses"),
        "capabilities": _normalize_string_list(member.get("capabilities", []), "member.capabilities"),
    }


def compute_full_sync_members_hash_v2(members: list) -> str:
    """
    Compute a v2 deterministic hash of the members list.
    """
    if not isinstance(members, list):
        raise ValueError("members must be a list")
    if not members:
        return ""
    if any(not isinstance(member, dict) for member in members):
        raise ValueError("members must contain only dict rows")

    member_rows = [_normalize_member_row_v2(m) for m in members]
    member_rows.sort(key=lambda x: x["peer_id"])

    json_str = json.dumps(member_rows, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(json_str.encode('utf-8')).hexdigest()


def compute_states_hash(states: list) -> str:
    """
    Compute a deterministic hash of the states list.

    SECURITY: This allows receivers to verify that received states
    match the signed fleet_hash, preventing state injection attacks.

    Algorithm matches StateManager.calculate_fleet_hash():
    1. Extract minimal tuples: (peer_id, version, timestamp)
    2. Sort by peer_id (lexicographic)
    3. Serialize to JSON with sorted keys
    4. SHA256 hash the result

    Args:
        states: List of state dicts from FULL_SYNC

    Returns:
        Hex-encoded SHA256 hash of the sorted state tuples
    """
    if not states:
        return ""

    # Extract minimal state tuples (matching StateManager algorithm)
    state_tuples = [
        {
            "peer_id": s.get("peer_id", ""),
            "version": s.get("version", 0),
            "timestamp": s.get("last_update", s.get("timestamp", 0)),
        }
        for s in states
    ]

    # Sort by peer_id for determinism
    state_tuples.sort(key=lambda x: x["peer_id"])

    # Serialize and hash
    json_str = json.dumps(state_tuples, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(json_str.encode('utf-8')).hexdigest()


def _normalize_state_row_v2(state: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a FULL_SYNC state row for deterministic hashing."""
    return {
        "peer_id": state.get("peer_id", ""),
        "version": state.get("version", 0),
        "timestamp": state.get("last_update", state.get("timestamp", 0)),
        "capacity_sats": state.get("capacity_sats", 0),
        "available_sats": state.get("available_sats", 0),
        "fee_policy": state.get("fee_policy", {}),
        "topology": _normalize_string_list(state.get("topology", []), "states.topology"),
        "addresses": _normalize_string_list(state.get("addresses", []), "states.addresses"),
        "capabilities": _normalize_string_list(state.get("capabilities", []), "states.capabilities"),
        "budget_available_sats": state.get("budget_available_sats", 0),
        "budget_reserved_until": state.get("budget_reserved_until", 0),
        "budget_last_update": state.get("budget_last_update", 0),
        "state_hash": state.get("state_hash", ""),
    }


def compute_full_sync_states_hash_v2(states: list) -> str:
    """
    Compute a v2 deterministic hash of the full-sync states list.
    """
    if not isinstance(states, list):
        raise ValueError("states must be a list")
    if not states:
        return ""
    if any(not isinstance(state, dict) for state in states):
        raise ValueError("states must contain only dict rows")

    state_rows = [_normalize_state_row_v2(s) for s in states]
    state_rows.sort(key=lambda x: x["peer_id"])

    json_str = json.dumps(state_rows, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(json_str.encode('utf-8')).hexdigest()


def get_full_sync_signing_payload(payload: Dict[str, Any]) -> str:
    """
    Get the canonical payload string for signing FULL_SYNC messages.

    SECURITY: The signature covers:
    - sender_id: Identity of sender
    - fleet_hash: Cryptographic digest of states (verified separately)
    - members_hash: Cryptographic digest of members list
    - timestamp: Replay protection

    This prevents both state tampering AND membership injection attacks.
    """
    members = payload.get("members", [])
    members_hash = compute_members_hash(members)

    signing_fields = {
        "sender_id": payload.get("sender_id", ""),
        "fleet_hash": payload.get("fleet_hash", ""),
        "members_hash": members_hash,
        "timestamp": payload.get("timestamp", 0),
    }
    return json.dumps(signing_fields, sort_keys=True, separators=(',', ':'))


def get_full_sync_signing_payload_v2(payload: Dict[str, Any]) -> str:
    """
    Get the canonical payload string for signing v2 FULL_SYNC messages.
    """
    states = payload.get("states", [])
    members = payload.get("members", [])

    signing_fields = {
        "sender_id": payload.get("sender_id", ""),
        "fleet_hash": payload.get("fleet_hash", ""),
        "states_hash": compute_full_sync_states_hash_v2(states),
        "members_hash": compute_full_sync_members_hash_v2(members),
        "timestamp": payload.get("timestamp", 0),
    }
    return json.dumps(signing_fields, sort_keys=True, separators=(',', ':'))


def is_strict_state_sync_payload(payload: Dict[str, Any]) -> bool:
    return isinstance(payload, dict) and payload.get("_envelope_version") == STRICT_STATE_SYNC_VERSION


# =============================================================================
# PHASE 3: INTENT MESSAGE VALIDATION
# =============================================================================

def validate_intent_abort(payload: Dict[str, Any]) -> bool:
    """
    Validate INTENT_ABORT payload schema.

    SECURITY: Requires cryptographic signature from the initiator.
    Only the intent owner can abort their own intent.
    """
    if not isinstance(payload, dict):
        return False

    intent_type = payload.get("intent_type")
    target = payload.get("target")
    initiator = payload.get("initiator")
    timestamp = payload.get("timestamp")
    signature = payload.get("signature")

    # intent_type must be a valid string
    valid_intent_types = ('channel_open', 'channel_close', 'rebalance')
    if intent_type not in valid_intent_types:
        return False

    # target must be valid pubkey
    if not _valid_pubkey(target):
        return False

    # initiator must be valid pubkey (the one aborting their intent)
    if not _valid_pubkey(initiator):
        return False

    # timestamp must be positive integer
    if not isinstance(timestamp, int) or timestamp < 0:
        return False

    # SECURITY: Signature must be present
    if not isinstance(signature, str) or len(signature) < 10:
        return False

    return True


def get_intent_signing_payload(payload: Dict[str, Any]) -> str:
    """
    Get the canonical payload string for signing HIVE_INTENT messages.

    The signature proves the intent was created by the claimed initiator.
    """
    signing_fields = {
        "intent_type": payload.get("intent_type", ""),
        "target": payload.get("target", ""),
        "initiator": payload.get("initiator", ""),
        "timestamp": payload.get("timestamp", 0),
    }
    return json.dumps(signing_fields, sort_keys=True, separators=(',', ':'))


def get_intent_abort_signing_payload(payload: Dict[str, Any]) -> str:
    """
    Get the canonical payload string for signing INTENT_ABORT messages.

    The signature proves the initiator is voluntarily aborting their intent.
    """
    signing_fields = {
        "intent_type": payload.get("intent_type", ""),
        "target": payload.get("target", ""),
        "initiator": payload.get("initiator", ""),
        "timestamp": payload.get("timestamp", 0),
        "reason": payload.get("reason", ""),
    }
    return json.dumps(signing_fields, sort_keys=True, separators=(',', ':'))


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def create_hello(pubkey: str) -> bytes:
    """
    Create a HIVE_HELLO message.

    Args:
        pubkey: Sender's public key (66 hex chars)

    Channel existence serves as proof of stake.
    """
    return serialize(HiveMessageType.HELLO, {
        "pubkey": pubkey,
        "protocol_version": PROTOCOL_VERSION,
        "supported_versions": sorted(SUPPORTED_VERSIONS)
    })


def create_challenge(nonce: str, hive_id: str) -> bytes:
    """Create a HIVE_CHALLENGE message."""
    return serialize(HiveMessageType.CHALLENGE, {
        "nonce": nonce,
        "hive_id": hive_id
    })


def create_attest(pubkey: str, version: str, features: list,
                  nonce_signature: str, manifest_signature: str,
                  manifest: Dict[str, Any]) -> bytes:
    """Create a HIVE_ATTEST message."""
    return serialize(HiveMessageType.ATTEST, {
        "pubkey": pubkey,
        "version": version,
        "features": features,
        "nonce_signature": nonce_signature,
        "manifest_signature": manifest_signature,
        "manifest": manifest
    })


def create_welcome(hive_id: str, tier: str, member_count: int,
                   state_hash: str, signature: str = "") -> bytes:
    """Create a HIVE_WELCOME message."""
    return serialize(HiveMessageType.WELCOME, {
        "hive_id": hive_id,
        "tier": tier,
        "member_count": member_count,
        "state_hash": state_hash,
        "signature": signature
    })


# =============================================================================
# PHASE 7: FEE INTELLIGENCE SIGNING & VALIDATION
# =============================================================================

def get_fee_intelligence_snapshot_signing_payload(payload: Dict[str, Any]) -> str:
    """
    Get the canonical string to sign for FEE_INTELLIGENCE_SNAPSHOT messages.

    Signs over: reporter_id, timestamp, and a hash of the sorted peer data.
    This ensures the entire snapshot is authenticated without making the
    signing string excessively long.

    Args:
        payload: FEE_INTELLIGENCE_SNAPSHOT message payload

    Returns:
        Canonical string for signmessage()
    """

    # Create deterministic hash of peers data
    peers = payload.get("peers", [])
    # Sort by peer_id for deterministic ordering
    sorted_peers = sorted(peers, key=lambda p: p.get("peer_id", ""))
    peers_json = json.dumps(sorted_peers, sort_keys=True, separators=(',', ':'))
    peers_hash = hashlib.sha256(peers_json.encode()).hexdigest()[:16]

    return (
        f"FEE_INTELLIGENCE_SNAPSHOT:"
        f"{payload.get('reporter_id', '')}:"
        f"{payload.get('timestamp', 0)}:"
        f"{len(peers)}:"
        f"{peers_hash}"
    )


def validate_fee_intelligence_snapshot_payload(payload: Dict[str, Any]) -> bool:
    """
    Validate a FEE_INTELLIGENCE_SNAPSHOT payload.

    SECURITY: Bounds all values to prevent manipulation and overflow.

    Args:
        payload: FEE_INTELLIGENCE_SNAPSHOT message payload

    Returns:
        True if valid, False otherwise
    """


    # Required string fields
    reporter_id = payload.get("reporter_id")
    signature = payload.get("signature")

    if not isinstance(reporter_id, str) or not reporter_id:
        return False
    if not isinstance(signature, str) or len(signature) < 10:
        return False

    # Timestamp freshness
    timestamp = payload.get("timestamp", 0)
    if not isinstance(timestamp, int) or timestamp < 0:
        return False
    if abs(time.time() - timestamp) > FEE_INTELLIGENCE_MAX_AGE:
        return False

    # Peers array
    peers = payload.get("peers")
    if not isinstance(peers, list):
        return False
    if len(peers) > MAX_PEERS_IN_SNAPSHOT:
        return False

    # Validate each peer entry
    for peer in peers:
        if not isinstance(peer, dict):
            return False

        peer_id = peer.get("peer_id")
        if not isinstance(peer_id, str) or not peer_id:
            return False

        # Fee bounds
        our_fee_ppm = peer.get("our_fee_ppm", 0)
        their_fee_ppm = peer.get("their_fee_ppm", 0)
        if not isinstance(our_fee_ppm, int) or not (0 <= our_fee_ppm <= MAX_FEE_PPM):
            return False
        if not isinstance(their_fee_ppm, int) or not (0 <= their_fee_ppm <= MAX_FEE_PPM):
            return False

        # Volume bounds
        forward_count = peer.get("forward_count", 0)
        forward_volume_sats = peer.get("forward_volume_sats", 0)
        revenue_sats = peer.get("revenue_sats", 0)

        if not isinstance(forward_count, int) or not (0 <= forward_count <= MAX_VOLUME_SATS):
            return False
        if not isinstance(forward_volume_sats, int) or not (0 <= forward_volume_sats <= MAX_VOLUME_SATS):
            return False
        if not isinstance(revenue_sats, int) or not (0 <= revenue_sats <= MAX_VOLUME_SATS):
            return False

        # Flow direction
        flow_direction = peer.get("flow_direction", "")
        if flow_direction and flow_direction not in VALID_FLOW_DIRECTIONS:
            return False

        # Utilization bounds
        utilization_pct = peer.get("utilization_pct", 0.0)
        if not isinstance(utilization_pct, (int, float)) or not (0 <= utilization_pct <= 1):
            return False

    return True


def get_liquidity_need_signing_payload(payload: Dict[str, Any]) -> str:
    """
    Get the canonical string to sign for LIQUIDITY_NEED messages.

    Args:
        payload: LIQUIDITY_NEED message payload

    Returns:
        Canonical string for signmessage()
    """
    return (
        f"LIQUIDITY_NEED:"
        f"{payload.get('reporter_id', '')}:"
        f"{payload.get('timestamp', 0)}:"
        f"{payload.get('need_type', '')}:"
        f"{payload.get('target_peer_id', '')}:"
        f"{payload.get('amount_sats', 0)}:"
        f"{payload.get('urgency', '')}:"
        f"{payload.get('max_fee_ppm', 0)}"
    )


def validate_liquidity_need_payload(payload: Dict[str, Any]) -> bool:
    """
    Validate a LIQUIDITY_NEED payload.

    Args:
        payload: LIQUIDITY_NEED message payload

    Returns:
        True if valid, False otherwise
    """
    # Required string fields
    reporter_id = payload.get("reporter_id")
    target_peer_id = payload.get("target_peer_id")
    signature = payload.get("signature")

    if not isinstance(reporter_id, str) or not reporter_id:
        return False
    if not isinstance(target_peer_id, str) or not target_peer_id:
        return False
    if not isinstance(signature, str) or len(signature) < 10:
        return False

    # Timestamp
    timestamp = payload.get("timestamp", 0)
    if not isinstance(timestamp, int) or timestamp < 0:
        return False

    # Need type validation
    need_type = payload.get("need_type")
    if need_type not in VALID_NEED_TYPES:
        return False

    # Urgency validation
    urgency = payload.get("urgency")
    if urgency not in VALID_URGENCY_LEVELS:
        return False

    # Amount bounds
    amount_sats = payload.get("amount_sats", 0)
    if not isinstance(amount_sats, int) or not (0 < amount_sats <= MAX_LIQUIDITY_AMOUNT):
        return False

    # Fee bounds
    max_fee_ppm = payload.get("max_fee_ppm", 0)
    if not isinstance(max_fee_ppm, int) or not (0 <= max_fee_ppm <= MAX_FEE_PPM):
        return False

    # Balance percentage
    current_balance_pct = payload.get("current_balance_pct", 0.0)
    if not isinstance(current_balance_pct, (int, float)) or not (0 <= current_balance_pct <= 1):
        return False

    return True


def get_liquidity_snapshot_signing_payload(payload: Dict[str, Any]) -> str:
    """
    Get the canonical string to sign for LIQUIDITY_SNAPSHOT messages.

    Signs over: reporter_id, timestamp, and a hash of the sorted needs data.
    This ensures the entire snapshot is authenticated without making the
    signing string excessively long.

    Args:
        payload: LIQUIDITY_SNAPSHOT message payload

    Returns:
        Canonical string for signmessage()
    """

    # Create deterministic hash of needs data
    needs = payload.get("needs", [])
    # Sort by target_peer_id for deterministic ordering
    sorted_needs = sorted(needs, key=lambda n: (n.get("target_peer_id", ""), n.get("need_type", "")))
    needs_json = json.dumps(sorted_needs, sort_keys=True, separators=(',', ':'))
    needs_hash = hashlib.sha256(needs_json.encode()).hexdigest()[:16]

    return (
        f"LIQUIDITY_SNAPSHOT:"
        f"{payload.get('reporter_id', '')}:"
        f"{payload.get('timestamp', 0)}:"
        f"{len(needs)}:"
        f"{needs_hash}"
    )


def validate_liquidity_snapshot_payload(payload: Dict[str, Any]) -> bool:
    """
    Validate a LIQUIDITY_SNAPSHOT payload.

    SECURITY: Bounds all values to prevent manipulation and overflow.

    Args:
        payload: LIQUIDITY_SNAPSHOT message payload

    Returns:
        True if valid, False otherwise
    """


    # Required string fields
    reporter_id = payload.get("reporter_id")
    signature = payload.get("signature")

    if not isinstance(reporter_id, str) or not reporter_id:
        return False
    if not isinstance(signature, str) or len(signature) < 10:
        return False

    # Timestamp freshness (allow 1 hour for snapshot messages)
    timestamp = payload.get("timestamp", 0)
    if not isinstance(timestamp, int) or timestamp < 0:
        return False
    if abs(time.time() - timestamp) > 3600:
        return False

    # Needs array
    needs = payload.get("needs")
    if not isinstance(needs, list):
        return False
    if len(needs) > MAX_NEEDS_IN_SNAPSHOT:
        return False

    # Validate each need entry
    for need in needs:
        if not isinstance(need, dict):
            return False

        # Target peer required
        target_peer_id = need.get("target_peer_id")
        if not isinstance(target_peer_id, str) or not target_peer_id:
            return False

        # Need type validation
        need_type = need.get("need_type")
        if need_type not in VALID_NEED_TYPES:
            return False

        # Urgency validation
        urgency = need.get("urgency")
        if urgency not in VALID_URGENCY_LEVELS:
            return False

        # Amount bounds
        amount_sats = need.get("amount_sats", 0)
        if not isinstance(amount_sats, int) or not (0 < amount_sats <= MAX_LIQUIDITY_AMOUNT):
            return False

        # Fee bounds
        max_fee_ppm = need.get("max_fee_ppm", 0)
        if not isinstance(max_fee_ppm, int) or not (0 <= max_fee_ppm <= MAX_FEE_PPM):
            return False

        # Balance percentage
        current_balance_pct = need.get("current_balance_pct", 0.0)
        if not isinstance(current_balance_pct, (int, float)) or not (0 <= current_balance_pct <= 1):
            return False

    return True


def create_liquidity_snapshot(
    reporter_id: str,
    timestamp: int,
    signature: str,
    needs: list
) -> bytes:
    """
    Create a LIQUIDITY_SNAPSHOT message.

    This is the preferred method for sharing liquidity needs, replacing
    individual LIQUIDITY_NEED messages. Send one snapshot with all needs
    instead of N individual messages.

    SECURITY: The signature must be created using signmessage() over the
    canonical payload returned by get_liquidity_snapshot_signing_payload().

    Args:
        reporter_id: Hive member reporting these needs
        timestamp: Unix timestamp
        signature: zbase-encoded signature from signmessage()
        needs: List of liquidity needs, each containing:
            - target_peer_id: External peer or hive member
            - need_type: 'inbound', 'outbound', 'rebalance'
            - amount_sats: How much is needed
            - urgency: 'critical', 'high', 'medium', 'low'
            - max_fee_ppm: Maximum fee willing to pay
            - reason: Why this liquidity is needed
            - current_balance_pct: Current local balance percentage
            - can_provide_inbound: Sats of inbound that can be provided
            - can_provide_outbound: Sats of outbound that can be provided

    Returns:
        Serialized LIQUIDITY_SNAPSHOT message
    """
    payload = {
        "reporter_id": reporter_id,
        "timestamp": timestamp,
        "signature": signature,
        "needs": needs,
    }

    return serialize(HiveMessageType.LIQUIDITY_SNAPSHOT, payload)


def get_health_report_signing_payload(payload: Dict[str, Any]) -> str:
    """
    Get the canonical string to sign for HEALTH_REPORT messages.

    Args:
        payload: HEALTH_REPORT message payload

    Returns:
        Canonical string for signmessage()
    """
    return (
        f"HEALTH_REPORT:"
        f"{payload.get('reporter_id', '')}:"
        f"{payload.get('timestamp', 0)}:"
        f"{payload.get('overall_health', 0)}:"
        f"{payload.get('capacity_score', 0)}:"
        f"{payload.get('revenue_score', 0)}:"
        f"{payload.get('connectivity_score', 0)}"
    )


def validate_health_report_payload(payload: Dict[str, Any]) -> bool:
    """
    Validate a HEALTH_REPORT payload.

    Args:
        payload: HEALTH_REPORT message payload

    Returns:
        True if valid, False otherwise
    """
    # Required string fields
    reporter_id = payload.get("reporter_id")
    signature = payload.get("signature")

    if not isinstance(reporter_id, str) or not reporter_id:
        return False
    if not isinstance(signature, str) or len(signature) < 10:
        return False

    # Timestamp
    timestamp = payload.get("timestamp", 0)
    if not isinstance(timestamp, int) or timestamp < 0:
        return False

    # Health scores (0-100)
    for score_field in ['overall_health', 'capacity_score', 'revenue_score', 'connectivity_score']:
        score = payload.get(score_field, 0)
        if not isinstance(score, int) or not (MIN_HEALTH_SCORE <= score <= MAX_HEALTH_SCORE):
            return False

    # Assistance budget bounds
    assistance_budget = payload.get("assistance_budget_sats", 0)
    if not isinstance(assistance_budget, int) or assistance_budget < 0:
        return False

    return True


def get_peer_reputation_snapshot_signing_payload(payload: Dict[str, Any]) -> str:
    """
    Get the canonical string to sign for PEER_REPUTATION_SNAPSHOT messages.

    Signs over: reporter_id, timestamp, and a hash of the sorted peer data.
    This ensures the entire snapshot is authenticated without making the
    signing string excessively long.

    Args:
        payload: PEER_REPUTATION_SNAPSHOT message payload

    Returns:
        Canonical string for signmessage()
    """

    # Create deterministic hash of peers data
    peers = payload.get("peers", [])
    # Sort by peer_id for deterministic ordering
    sorted_peers = sorted(peers, key=lambda p: p.get("peer_id", ""))
    peers_json = json.dumps(sorted_peers, sort_keys=True, separators=(',', ':'))
    peers_hash = hashlib.sha256(peers_json.encode()).hexdigest()[:16]

    return (
        f"PEER_REPUTATION_SNAPSHOT:"
        f"{payload.get('reporter_id', '')}:"
        f"{payload.get('timestamp', 0)}:"
        f"{len(peers)}:"
        f"{peers_hash}"
    )


def validate_peer_reputation_snapshot_payload(payload: Dict[str, Any]) -> bool:
    """
    Validate a PEER_REPUTATION_SNAPSHOT payload.

    SECURITY: Bounds all values to prevent manipulation and overflow.

    Args:
        payload: PEER_REPUTATION_SNAPSHOT message payload

    Returns:
        True if valid, False otherwise
    """


    # Required string fields
    reporter_id = payload.get("reporter_id")
    signature = payload.get("signature")

    if not isinstance(reporter_id, str) or not reporter_id:
        return False
    if not isinstance(signature, str) or len(signature) < 10:
        return False

    # Timestamp freshness (allow 1 hour for reputation snapshots)
    timestamp = payload.get("timestamp", 0)
    if not isinstance(timestamp, int) or timestamp < 0:
        return False
    if abs(time.time() - timestamp) > 3600:
        return False

    # Peers array
    peers = payload.get("peers")
    if not isinstance(peers, list):
        return False
    if len(peers) > MAX_PEERS_IN_REPUTATION_SNAPSHOT:
        return False

    # Validate each peer entry
    for peer in peers:
        if not isinstance(peer, dict):
            return False

        peer_id = peer.get("peer_id")
        if not isinstance(peer_id, str) or not peer_id:
            return False

        # Uptime percentage bounds (0-1)
        uptime_pct = peer.get("uptime_pct", 1.0)
        if not isinstance(uptime_pct, (int, float)) or not (0 <= uptime_pct <= 1):
            return False

        # Response time bounds
        response_time_ms = peer.get("response_time_ms", 0)
        if not isinstance(response_time_ms, int) or not (0 <= response_time_ms <= MAX_RESPONSE_TIME_MS):
            return False

        # Force close count bounds
        force_close_count = peer.get("force_close_count", 0)
        if not isinstance(force_close_count, int) or not (0 <= force_close_count <= MAX_FORCE_CLOSE_COUNT):
            return False

        # Fee stability bounds (0-1)
        fee_stability = peer.get("fee_stability", 1.0)
        if not isinstance(fee_stability, (int, float)) or not (0 <= fee_stability <= 1):
            return False

        # HTLC success rate bounds (0-1)
        htlc_success_rate = peer.get("htlc_success_rate", 1.0)
        if not isinstance(htlc_success_rate, (int, float)) or not (0 <= htlc_success_rate <= 1):
            return False

        # Channel age bounds
        channel_age_days = peer.get("channel_age_days", 0)
        if not isinstance(channel_age_days, int) or not (0 <= channel_age_days <= MAX_CHANNEL_AGE_DAYS):
            return False

        # Total routed bounds
        total_routed_sats = peer.get("total_routed_sats", 0)
        if not isinstance(total_routed_sats, int) or total_routed_sats < 0:
            return False

        # Warnings validation
        warnings = peer.get("warnings", [])
        if not isinstance(warnings, list):
            return False
        if len(warnings) > MAX_WARNINGS_COUNT:
            return False
        for warning in warnings:
            if not isinstance(warning, str):
                return False
            if warning and warning not in VALID_WARNINGS:
                return False

    return True


def create_peer_reputation_snapshot(
    reporter_id: str,
    timestamp: int,
    signature: str,
    peers: list
) -> bytes:
    """
    Create a PEER_REPUTATION_SNAPSHOT message.

    This is the preferred method for sharing peer reputation, replacing
    individual PEER_REPUTATION messages. Send one snapshot with all peer
    observations instead of N individual messages.

    SECURITY: The signature must be created using signmessage() over the
    canonical payload returned by get_peer_reputation_snapshot_signing_payload().

    Args:
        reporter_id: Hive member reporting these observations
        timestamp: Unix timestamp
        signature: zbase-encoded signature from signmessage()
        peers: List of peer observations, each containing:
            - peer_id: External peer being reported on
            - uptime_pct: Peer uptime (0-1)
            - response_time_ms: Average HTLC response time
            - force_close_count: Force closes by peer
            - fee_stability: Fee stability (0-1)
            - htlc_success_rate: HTLC success rate (0-1)
            - channel_age_days: Channel age
            - total_routed_sats: Total volume routed
            - warnings: Warning codes list
            - observation_days: Days covered

    Returns:
        Serialized PEER_REPUTATION_SNAPSHOT message
    """
    payload = {
        "reporter_id": reporter_id,
        "timestamp": timestamp,
        "signature": signature,
        "peers": peers,
    }

    return serialize(HiveMessageType.PEER_REPUTATION_SNAPSHOT, payload)


def create_fee_intelligence_snapshot(
    reporter_id: str,
    timestamp: int,
    signature: str,
    peers: list
) -> bytes:
    """
    Create a FEE_INTELLIGENCE_SNAPSHOT message.

    This is the preferred method for sharing fee intelligence, replacing
    individual FEE_INTELLIGENCE messages. Send one snapshot with all peer
    observations instead of N individual messages.

    SECURITY: The signature must be created using signmessage() over the
    canonical payload returned by get_fee_intelligence_snapshot_signing_payload().

    Args:
        reporter_id: Hive member reporting these observations
        timestamp: Unix timestamp
        signature: zbase-encoded signature from signmessage()
        peers: List of peer observations, each containing:
            - peer_id: External peer being reported on
            - our_fee_ppm: Fee we charge to this peer
            - their_fee_ppm: Fee they charge us
            - forward_count: Number of forwards
            - forward_volume_sats: Total volume routed
            - revenue_sats: Fees earned
            - flow_direction: 'source', 'sink', or 'balanced'
            - utilization_pct: Channel utilization (0.0-1.0)

    Returns:
        Serialized FEE_INTELLIGENCE_SNAPSHOT message
    """
    payload = {
        "reporter_id": reporter_id,
        "timestamp": timestamp,
        "signature": signature,
        "peers": peers,
    }

    return serialize(HiveMessageType.FEE_INTELLIGENCE_SNAPSHOT, payload)


def create_liquidity_need(
    reporter_id: str,
    timestamp: int,
    signature: str,
    need_type: str,
    target_peer_id: str,
    amount_sats: int,
    urgency: str,
    max_fee_ppm: int,
    reason: str,
    current_balance_pct: float,
    can_provide_inbound: int = 0,
    can_provide_outbound: int = 0
) -> bytes:
    """
    Create a LIQUIDITY_NEED message.

    SECURITY: The signature must be created using signmessage() over the
    canonical payload returned by get_liquidity_need_signing_payload().

    Args:
        reporter_id: Hive member needing liquidity
        timestamp: Unix timestamp
        signature: zbase-encoded signature from signmessage()
        need_type: 'inbound', 'outbound', or 'rebalance'
        target_peer_id: External peer (or hive member)
        amount_sats: How much liquidity needed
        urgency: 'critical', 'high', 'medium', or 'low'
        max_fee_ppm: Maximum fee willing to pay
        reason: Why liquidity is needed
        current_balance_pct: Current local balance percentage
        can_provide_inbound: Sats of inbound we can provide
        can_provide_outbound: Sats of outbound we can provide

    Returns:
        Serialized LIQUIDITY_NEED message
    """
    payload = {
        "reporter_id": reporter_id,
        "timestamp": timestamp,
        "signature": signature,
        "need_type": need_type,
        "target_peer_id": target_peer_id,
        "amount_sats": amount_sats,
        "urgency": urgency,
        "max_fee_ppm": max_fee_ppm,
        "reason": reason,
        "current_balance_pct": current_balance_pct,
        "can_provide_inbound": can_provide_inbound,
        "can_provide_outbound": can_provide_outbound,
    }

    return serialize(HiveMessageType.LIQUIDITY_NEED, payload)


def create_health_report(
    reporter_id: str,
    timestamp: int,
    signature: str,
    overall_health: int,
    capacity_score: int,
    revenue_score: int,
    connectivity_score: int,
    needs_inbound: bool = False,
    needs_outbound: bool = False,
    needs_channels: bool = False,
    can_provide_assistance: bool = False,
    assistance_budget_sats: int = 0
) -> bytes:
    """
    Create a HEALTH_REPORT message.

    SECURITY: The signature must be created using signmessage() over the
    canonical payload returned by get_health_report_signing_payload().

    Args:
        reporter_id: Hive member reporting their health
        timestamp: Unix timestamp
        signature: zbase-encoded signature from signmessage()
        overall_health: Overall health score (0-100)
        capacity_score: Capacity score (0-100)
        revenue_score: Revenue score (0-100)
        connectivity_score: Connectivity score (0-100)
        needs_inbound: Whether node needs inbound liquidity
        needs_outbound: Whether node needs outbound liquidity
        needs_channels: Whether node needs more channels
        can_provide_assistance: Whether node can help others
        assistance_budget_sats: How much node can spend helping

    Returns:
        Serialized HEALTH_REPORT message
    """
    payload = {
        "reporter_id": reporter_id,
        "timestamp": timestamp,
        "signature": signature,
        "overall_health": overall_health,
        "capacity_score": capacity_score,
        "revenue_score": revenue_score,
        "connectivity_score": connectivity_score,
        "needs_inbound": needs_inbound,
        "needs_outbound": needs_outbound,
        "needs_channels": needs_channels,
        "can_provide_assistance": can_provide_assistance,
        "assistance_budget_sats": assistance_budget_sats,
    }

    return serialize(HiveMessageType.HEALTH_REPORT, payload)


# =============================================================================
# YIELD METRICS BATCH FUNCTIONS (Phase 14)
# =============================================================================

def get_yield_metrics_batch_signing_payload(payload: Dict[str, Any]) -> str:
    """
    Get the canonical string to sign for YIELD_METRICS_BATCH messages.

    Signs over: reporter_id, timestamp, and a hash of the sorted yield data.
    """
    metrics = payload.get("metrics", [])

    # Sort metrics by peer_id for deterministic ordering
    sorted_metrics = sorted(metrics, key=lambda m: m.get("peer_id", ""))

    # Create a condensed hash of the metrics data
    metrics_str = json.dumps(sorted_metrics, sort_keys=True, separators=(',', ':'))
    metrics_hash = hashlib.sha256(metrics_str.encode()).hexdigest()[:16]

    return (
        f"YIELD_METRICS_BATCH:"
        f"{payload.get('reporter_id', '')}:"
        f"{payload.get('timestamp', 0)}:"
        f"{len(metrics)}:"
        f"{metrics_hash}"
    )


def validate_yield_metrics_batch(payload: Dict[str, Any]) -> bool:
    """
    Validate a YIELD_METRICS_BATCH payload.

    SECURITY: Bounds all values to prevent manipulation and overflow.
    """
    # Required fields
    reporter_id = payload.get("reporter_id")
    if not reporter_id or not isinstance(reporter_id, str):
        return False
    if len(reporter_id) > MAX_PEER_ID_LEN:
        return False

    timestamp = payload.get("timestamp")
    if not isinstance(timestamp, (int, float)):
        return False
    now = time.time()
    if timestamp > now + 300:  # 5 min future tolerance
        return False
    if timestamp < now - (48 * 3600):  # 48 hour max age
        return False

    signature = payload.get("signature")
    if not signature or not isinstance(signature, str):
        return False

    metrics = payload.get("metrics")
    if not isinstance(metrics, list):
        return False
    if len(metrics) > MAX_YIELD_METRICS_IN_BATCH:
        return False

    # Validate each metrics entry
    for m in metrics:
        if not isinstance(m, dict):
            return False

        peer_id = m.get("peer_id")
        if not peer_id or not isinstance(peer_id, str):
            return False
        if len(peer_id) > MAX_PEER_ID_LEN:
            return False

        # ROI can be negative (underwater channels)
        roi_pct = m.get("roi_pct")
        if not isinstance(roi_pct, (int, float)):
            return False
        if roi_pct < -1000 or roi_pct > 10000:  # Reasonable bounds
            return False

        # Capital efficiency should be small positive
        capital_efficiency = m.get("capital_efficiency")
        if not isinstance(capital_efficiency, (int, float)):
            return False
        if capital_efficiency < -1 or capital_efficiency > 1:  # Per-sat efficiency
            return False

        # Flow intensity 0-1
        flow_intensity = m.get("flow_intensity")
        if not isinstance(flow_intensity, (int, float)):
            return False
        if flow_intensity < 0 or flow_intensity > 1:
            return False

        # Profitability tier
        tier = m.get("profitability_tier")
        if tier not in ("profitable", "underwater", "zombie", "stagnant", "unknown"):
            return False

    return True


def create_yield_metrics_batch(
    metrics: List[Dict[str, Any]],
    rpc: Any,
    our_pubkey: str
) -> Optional[bytes]:
    """
    Create a YIELD_METRICS_BATCH message.

    This message shares per-channel profitability metrics with the fleet,
    enabling collective learning about which external peers are profitable.

    Args:
        metrics: List of yield metric entries, each containing:
            - peer_id: External peer pubkey
            - channel_id: Channel short ID
            - roi_pct: Return on investment percentage
            - capital_efficiency: Revenue per sat of capacity
            - flow_intensity: Volume / capacity ratio
            - profitability_tier: profitable/underwater/zombie/stagnant
            - period_days: Analysis period
        rpc: CLN RPC interface for signing
        our_pubkey: Our node's public key

    Returns:
        Serialized YIELD_METRICS_BATCH message, or None on error
    """
    timestamp = int(time.time())
    reporter_id = our_pubkey

    payload = {
        "reporter_id": reporter_id,
        "timestamp": timestamp,
        "signature": "",
        "metrics": metrics,
    }

    try:
        signing_payload = get_yield_metrics_batch_signing_payload(payload)
        sign_result = rpc.signmessage(signing_payload)
        signature = sign_result.get("signature", sign_result.get("zbase", ""))
        payload["signature"] = signature
    except Exception:
        return None

    return serialize(HiveMessageType.YIELD_METRICS_BATCH, payload)


# =============================================================================
# CIRCULAR FLOW ALERT FUNCTIONS (Phase 14)
# =============================================================================

def get_temporal_pattern_batch_signing_payload(payload: Dict[str, Any]) -> str:
    """
    Get the canonical string to sign for TEMPORAL_PATTERN_BATCH messages.
    """
    patterns = payload.get("patterns", [])
    sorted_patterns = sorted(patterns, key=lambda p: (p.get("peer_id", ""), p.get("hour_of_day", 0)))
    patterns_str = json.dumps(sorted_patterns, sort_keys=True, separators=(',', ':'))
    patterns_hash = hashlib.sha256(patterns_str.encode()).hexdigest()[:16]

    return (
        f"TEMPORAL_PATTERN_BATCH:"
        f"{payload.get('reporter_id', '')}:"
        f"{payload.get('timestamp', 0)}:"
        f"{len(patterns)}:"
        f"{patterns_hash}"
    )


def validate_temporal_pattern_batch(payload: Dict[str, Any]) -> bool:
    """
    Validate a TEMPORAL_PATTERN_BATCH payload.
    """
    reporter_id = payload.get("reporter_id")
    if not reporter_id or not isinstance(reporter_id, str):
        return False
    if len(reporter_id) > MAX_PEER_ID_LEN:
        return False

    timestamp = payload.get("timestamp")
    if not isinstance(timestamp, (int, float)):
        return False
    now = time.time()
    if timestamp > now + 300:
        return False
    if timestamp < now - (48 * 3600):
        return False

    signature = payload.get("signature")
    if not signature or not isinstance(signature, str):
        return False

    patterns = payload.get("patterns")
    if not isinstance(patterns, list):
        return False
    if len(patterns) > MAX_PATTERNS_IN_BATCH:
        return False

    for p in patterns:
        if not isinstance(p, dict):
            return False

        peer_id = p.get("peer_id")
        if not peer_id or not isinstance(peer_id, str):
            return False
        if len(peer_id) > MAX_PEER_ID_LEN:
            return False

        hour = p.get("hour_of_day")
        if not isinstance(hour, int) or hour < 0 or hour > 23:
            return False

        day = p.get("day_of_week")
        if not isinstance(day, int) or day < -1 or day > 6:  # -1 = every day
            return False

        direction = p.get("direction")
        if direction not in ("inbound", "outbound", "bidirectional"):
            return False

        intensity = p.get("intensity")
        if not isinstance(intensity, (int, float)) or intensity < 0 or intensity > 1:
            return False

        confidence = p.get("confidence")
        if not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1:
            return False

    return True


def create_temporal_pattern_batch(
    patterns: List[Dict[str, Any]],
    rpc: Any,
    our_pubkey: str
) -> Optional[bytes]:
    """
    Create a TEMPORAL_PATTERN_BATCH message.

    This message shares detected temporal flow patterns with the fleet,
    enabling coordinated liquidity positioning and fee optimization.

    Args:
        patterns: List of pattern entries, each containing:
            - peer_id: External peer pubkey
            - channel_id: Channel short ID
            - hour_of_day: 0-23 hour when pattern occurs
            - day_of_week: 0-6 (Mon-Sun) or -1 for every day
            - direction: inbound/outbound/bidirectional
            - intensity: Flow intensity 0-1
            - confidence: Pattern confidence 0-1
            - samples: Number of samples used to detect pattern
        rpc: CLN RPC interface for signing
        our_pubkey: Our node's public key

    Returns:
        Serialized TEMPORAL_PATTERN_BATCH message, or None on error
    """
    timestamp = int(time.time())

    payload = {
        "reporter_id": our_pubkey,
        "timestamp": timestamp,
        "signature": "",
        "patterns": patterns,
    }

    try:
        signing_payload = get_temporal_pattern_batch_signing_payload(payload)
        sign_result = rpc.signmessage(signing_payload)
        signature = sign_result.get("signature", sign_result.get("zbase", ""))
        payload["signature"] = signature
    except Exception:
        return None

    return serialize(HiveMessageType.TEMPORAL_PATTERN_BATCH, payload)


# =============================================================================
# CORRIDOR VALUE BATCH FUNCTIONS (Phase 14.2)
# =============================================================================

def get_corridor_value_batch_signing_payload(payload: Dict[str, Any]) -> str:
    """Get the canonical string to sign for CORRIDOR_VALUE_BATCH messages."""
    corridors = payload.get("corridors", [])
    sorted_corridors = sorted(corridors, key=lambda c: (c.get("source_peer_id", ""), c.get("destination_peer_id", "")))
    corridors_str = json.dumps(sorted_corridors, sort_keys=True, separators=(',', ':'))
    corridors_hash = hashlib.sha256(corridors_str.encode()).hexdigest()[:16]

    return (
        f"CORRIDOR_VALUE_BATCH:"
        f"{payload.get('reporter_id', '')}:"
        f"{payload.get('timestamp', 0)}:"
        f"{len(corridors)}:"
        f"{corridors_hash}"
    )


def validate_corridor_value_batch(payload: Dict[str, Any]) -> bool:
    """Validate a CORRIDOR_VALUE_BATCH payload."""
    reporter_id = payload.get("reporter_id")
    if not reporter_id or not isinstance(reporter_id, str) or len(reporter_id) > MAX_PEER_ID_LEN:
        return False

    timestamp = payload.get("timestamp")
    if not isinstance(timestamp, (int, float)):
        return False
    now = time.time()
    if timestamp > now + 300 or timestamp < now - (48 * 3600):
        return False

    signature = payload.get("signature")
    if not signature or not isinstance(signature, str):
        return False

    corridors = payload.get("corridors")
    if not isinstance(corridors, list) or len(corridors) > MAX_CORRIDORS_IN_BATCH:
        return False

    for c in corridors:
        if not isinstance(c, dict):
            return False
        for field in ["source_peer_id", "destination_peer_id"]:
            val = c.get(field)
            if not val or not isinstance(val, str) or len(val) > MAX_PEER_ID_LEN:
                return False
        value_score = c.get("value_score")
        if not isinstance(value_score, (int, float)) or value_score < 0 or value_score > 100:
            return False
        daily_volume = c.get("daily_volume_sats")
        if not isinstance(daily_volume, int) or daily_volume < 0:
            return False

    return True


def create_corridor_value_batch(
    corridors: List[Dict[str, Any]],
    rpc: Any,
    our_pubkey: str
) -> Optional[bytes]:
    """
    Create a CORRIDOR_VALUE_BATCH message.

    Shares discovered high-value routing corridors with the fleet.
    """
    timestamp = int(time.time())
    payload = {
        "reporter_id": our_pubkey,
        "timestamp": timestamp,
        "signature": "",
        "corridors": corridors,
    }

    try:
        signing_payload = get_corridor_value_batch_signing_payload(payload)
        sign_result = rpc.signmessage(signing_payload)
        signature = sign_result.get("signature", sign_result.get("zbase", ""))
        payload["signature"] = signature
    except Exception:
        return None

    return serialize(HiveMessageType.CORRIDOR_VALUE_BATCH, payload)


# =============================================================================
# POSITIONING PROPOSAL FUNCTIONS (Phase 14.2)
# =============================================================================

def get_positioning_proposal_signing_payload(payload: Dict[str, Any]) -> str:
    """Get the canonical string to sign for POSITIONING_PROPOSAL messages."""
    return (
        f"POSITIONING_PROPOSAL:"
        f"{payload.get('reporter_id', '')}:"
        f"{payload.get('timestamp', 0)}:"
        f"{payload.get('target_peer_id', '')}:"
        f"{payload.get('recommended_member', '')}:"
        f"{payload.get('target_capacity_sats', 0)}"
    )


def validate_positioning_proposal(payload: Dict[str, Any]) -> bool:
    """Validate a POSITIONING_PROPOSAL payload."""
    reporter_id = payload.get("reporter_id")
    if not reporter_id or not isinstance(reporter_id, str) or len(reporter_id) > MAX_PEER_ID_LEN:
        return False

    timestamp = payload.get("timestamp")
    if not isinstance(timestamp, (int, float)):
        return False
    now = time.time()
    if timestamp > now + 300 or timestamp < now - (48 * 3600):
        return False

    signature = payload.get("signature")
    if not signature or not isinstance(signature, str):
        return False

    target_peer_id = payload.get("target_peer_id")
    if not target_peer_id or not isinstance(target_peer_id, str) or len(target_peer_id) > MAX_PEER_ID_LEN:
        return False

    recommended_member = payload.get("recommended_member")
    if not recommended_member or not isinstance(recommended_member, str) or len(recommended_member) > MAX_PEER_ID_LEN:
        return False

    priority_tier = payload.get("priority_tier")
    if priority_tier not in VALID_PRIORITY_TIERS:
        return False

    target_capacity = payload.get("target_capacity_sats")
    if not isinstance(target_capacity, int) or target_capacity < 0 or target_capacity > 100_000_000_000:
        return False

    return True


def create_positioning_proposal(
    target_peer_id: str,
    recommended_member: str,
    priority_tier: str,
    target_capacity_sats: int,
    reason: str,
    value_score: float,
    rpc: Any,
    our_pubkey: str
) -> Optional[bytes]:
    """
    Create a POSITIONING_PROPOSAL message.

    Proposes which fleet member should open a channel to a target peer.
    """
    timestamp = int(time.time())
    payload = {
        "reporter_id": our_pubkey,
        "timestamp": timestamp,
        "signature": "",
        "target_peer_id": target_peer_id,
        "recommended_member": recommended_member,
        "priority_tier": priority_tier,
        "target_capacity_sats": target_capacity_sats,
        "reason": reason[:500],
        "value_score": round(value_score, 4),
    }

    try:
        signing_payload = get_positioning_proposal_signing_payload(payload)
        sign_result = rpc.signmessage(signing_payload)
        signature = sign_result.get("signature", sign_result.get("zbase", ""))
        payload["signature"] = signature
    except Exception:
        return None

    return serialize(HiveMessageType.POSITIONING_PROPOSAL, payload)


# =============================================================================
# PHYSARUM RECOMMENDATION FUNCTIONS (Phase 14.2)
# =============================================================================

def get_coverage_analysis_batch_signing_payload(payload: Dict[str, Any]) -> str:
    """Get the canonical string to sign for COVERAGE_ANALYSIS_BATCH messages."""
    entries = payload.get("coverage_entries", [])
    sorted_entries = sorted(entries, key=lambda e: e.get("peer_id", ""))
    entries_str = json.dumps(sorted_entries, sort_keys=True, separators=(',', ':'))
    entries_hash = hashlib.sha256(entries_str.encode()).hexdigest()[:16]

    return (
        f"COVERAGE_ANALYSIS_BATCH:"
        f"{payload.get('reporter_id', '')}:"
        f"{payload.get('timestamp', 0)}:"
        f"{len(entries)}:"
        f"{entries_hash}"
    )


def validate_coverage_analysis_batch(payload: Dict[str, Any]) -> bool:
    """Validate a COVERAGE_ANALYSIS_BATCH payload."""
    reporter_id = payload.get("reporter_id")
    if not reporter_id or not isinstance(reporter_id, str) or len(reporter_id) > MAX_PEER_ID_LEN:
        return False

    timestamp = payload.get("timestamp")
    if not isinstance(timestamp, (int, float)):
        return False
    now = time.time()
    if timestamp > now + 300 or timestamp < now - (48 * 3600):
        return False

    signature = payload.get("signature")
    if not signature or not isinstance(signature, str):
        return False

    entries = payload.get("coverage_entries")
    if not isinstance(entries, list) or len(entries) > MAX_COVERAGE_ENTRIES_IN_BATCH:
        return False

    for e in entries:
        if not isinstance(e, dict):
            return False
        peer_id = e.get("peer_id")
        if not peer_id or not isinstance(peer_id, str) or len(peer_id) > MAX_PEER_ID_LEN:
            return False
        members = e.get("members_with_channels")
        if not isinstance(members, list):
            return False
        owner_confidence = e.get("ownership_confidence")
        if not isinstance(owner_confidence, (int, float)) or owner_confidence < 0 or owner_confidence > 1:
            return False

    return True


def create_coverage_analysis_batch(
    coverage_entries: List[Dict[str, Any]],
    rpc: Any,
    our_pubkey: str
) -> Optional[bytes]:
    """
    Create a COVERAGE_ANALYSIS_BATCH message.

    Shares peer coverage analysis showing which members have channels to each peer.
    """
    timestamp = int(time.time())
    payload = {
        "reporter_id": our_pubkey,
        "timestamp": timestamp,
        "signature": "",
        "coverage_entries": coverage_entries,
    }

    try:
        signing_payload = get_coverage_analysis_batch_signing_payload(payload)
        sign_result = rpc.signmessage(signing_payload)
        signature = sign_result.get("signature", sign_result.get("zbase", ""))
        payload["signature"] = signature
    except Exception:
        return None

    return serialize(HiveMessageType.COVERAGE_ANALYSIS_BATCH, payload)


# =============================================================================
# CLOSE PROPOSAL FUNCTIONS (Phase 14.2)
# =============================================================================

def get_close_proposal_signing_payload(payload: Dict[str, Any]) -> str:
    """Get the canonical string to sign for CLOSE_PROPOSAL messages."""
    return (
        f"CLOSE_PROPOSAL:"
        f"{payload.get('reporter_id', '')}:"
        f"{payload.get('timestamp', 0)}:"
        f"{payload.get('member_id', '')}:"
        f"{payload.get('peer_id', '')}:"
        f"{payload.get('channel_id', '')}"
    )


def validate_close_proposal(payload: Dict[str, Any]) -> bool:
    """Validate a CLOSE_PROPOSAL payload."""
    reporter_id = payload.get("reporter_id")
    if not reporter_id or not isinstance(reporter_id, str) or len(reporter_id) > MAX_PEER_ID_LEN:
        return False

    timestamp = payload.get("timestamp")
    if not isinstance(timestamp, (int, float)):
        return False
    now = time.time()
    if timestamp > now + 300 or timestamp < now - (48 * 3600):
        return False

    signature = payload.get("signature")
    if not signature or not isinstance(signature, str):
        return False

    member_id = payload.get("member_id")
    if not member_id or not isinstance(member_id, str) or len(member_id) > MAX_PEER_ID_LEN:
        return False

    peer_id = payload.get("peer_id")
    if not peer_id or not isinstance(peer_id, str) or len(peer_id) > MAX_PEER_ID_LEN:
        return False

    channel_id = payload.get("channel_id")
    if not channel_id or not isinstance(channel_id, str) or len(channel_id) > 50:
        return False

    owner_id = payload.get("owner_id")
    if owner_id and (not isinstance(owner_id, str) or len(owner_id) > MAX_PEER_ID_LEN):
        return False

    freed_capacity = payload.get("freed_capacity_sats")
    if not isinstance(freed_capacity, int) or freed_capacity < 0:
        return False

    return True


def create_close_proposal(
    member_id: str,
    peer_id: str,
    channel_id: str,
    owner_id: str,
    reason: str,
    freed_capacity_sats: int,
    member_marker_strength: float,
    owner_marker_strength: float,
    rpc: Any,
    our_pubkey: str
) -> Optional[bytes]:
    """
    Create a CLOSE_PROPOSAL message.

    Proposes that a fleet member close a redundant/underperforming channel.
    """
    timestamp = int(time.time())
    payload = {
        "reporter_id": our_pubkey,
        "timestamp": timestamp,
        "signature": "",
        "member_id": member_id,
        "peer_id": peer_id,
        "channel_id": channel_id,
        "owner_id": owner_id,
        "reason": reason[:500],
        "freed_capacity_sats": freed_capacity_sats,
        "member_marker_strength": round(member_marker_strength, 3),
        "owner_marker_strength": round(owner_marker_strength, 3),
    }

    try:
        signing_payload = get_close_proposal_signing_payload(payload)
        sign_result = rpc.signmessage(signing_payload)
        signature = sign_result.get("signature", sign_result.get("zbase", ""))
        payload["signature"] = signature
    except Exception:
        return None

    return serialize(HiveMessageType.CLOSE_PROPOSAL, payload)


# =============================================================================
# PHASE 15: MCF (MIN-COST MAX-FLOW) MESSAGE FUNCTIONS
# =============================================================================

def get_traffic_intelligence_batch_signing_payload(payload: Dict[str, Any]) -> str:
    """Get canonical string to sign for TRAFFIC_INTELLIGENCE_BATCH."""
    profiles = payload.get("profiles", [])
    sorted_profiles = sorted(profiles, key=lambda p: p.get("peer_id", ""))
    profiles_json = json.dumps(sorted_profiles, sort_keys=True, separators=(',', ':'))
    profiles_hash = hashlib.sha256(profiles_json.encode()).hexdigest()[:16]
    return (
        f"TRAFFIC_INTELLIGENCE_BATCH:"
        f"{payload.get('reporter_id', '')}:"
        f"{payload.get('timestamp', 0)}:"
        f"{len(profiles)}:"
        f"{profiles_hash}"
    )


def validate_traffic_intelligence_batch(payload: Dict[str, Any]) -> bool:
    """Validate a TRAFFIC_INTELLIGENCE_BATCH payload."""
    reporter_id = payload.get("reporter_id")
    if not isinstance(reporter_id, str) or not reporter_id:
        return False

    signature = payload.get("signature")
    if not isinstance(signature, str) or len(signature) < 10:
        return False

    timestamp = payload.get("timestamp", 0)
    if not isinstance(timestamp, (int, float)):
        return False
    now = time.time()
    if timestamp > now + 300:
        return False
    if timestamp < now - TRAFFIC_INTELLIGENCE_MAX_AGE:
        return False

    profiles = payload.get("profiles")
    if not isinstance(profiles, list):
        return False
    if len(profiles) > MAX_PROFILES_IN_BATCH:
        return False

    for p in profiles:
        if not isinstance(p, dict):
            return False
        peer_id = p.get("peer_id")
        if not isinstance(peer_id, str) or not peer_id:
            return False
        if p.get("profile_type") not in VALID_PROFILE_TYPES:
            return False
        if p.get("drain_direction") not in VALID_DRAIN_DIRECTIONS:
            return False
        confidence = p.get("confidence", 0)
        if not isinstance(confidence, (int, float)) or not (0 <= confidence <= 1):
            return False
        avg_size = p.get("avg_forward_size_sats", 0)
        if not isinstance(avg_size, (int, float)) or avg_size < 0 or avg_size > MAX_FORWARD_SIZE_SATS:
            return False
        daily_vol = p.get("daily_volume_sats", 0)
        if not isinstance(daily_vol, (int, float)) or daily_vol < 0 or daily_vol > MAX_DAILY_VOLUME_SATS:
            return False
        obs_window = p.get("observation_window_hours", 0)
        if not isinstance(obs_window, (int, float)) or obs_window < 0 or obs_window > MAX_OBSERVATION_WINDOW_HOURS:
            return False
        peak = p.get("peak_hours_utc")
        if not isinstance(peak, list) or not all(isinstance(h, int) and 0 <= h <= 23 for h in peak):
            return False
        quiet = p.get("quiet_hours_utc")
        if not isinstance(quiet, list) or not all(isinstance(h, int) and 0 <= h <= 23 for h in quiet):
            return False

    return True


def create_traffic_intelligence_batch(
    reporter_id: str,
    timestamp: int,
    signature: str,
    profiles: list,
) -> bytes:
    """Create a TRAFFIC_INTELLIGENCE_BATCH message."""
    payload = {
        "reporter_id": reporter_id,
        "timestamp": timestamp,
        "signature": signature,
        "profiles": profiles,
    }
    return serialize(HiveMessageType.TRAFFIC_INTELLIGENCE_BATCH, payload)
