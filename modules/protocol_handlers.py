"""
protocol_handlers - Protocol message handler functions for cl-hive.

This module contains all handle_* functions and their helpers that process
incoming Hive protocol messages dispatched by _dispatch_hive_message().

Dependencies are injected at startup via init_protocol_handlers() to avoid
rewriting every function body during the extraction from the cl-hive.py
monolith.
"""

import json
import secrets
import threading
import time
from typing import Dict, Optional, Any, List, Set

from pyln.client import Plugin

from modules.protocol import (
    HIVE_MAGIC, HiveMessageType,
    MAX_MESSAGE_BYTES, is_hive_message, deserialize, serialize,
    validate_member_left,
    create_challenge, create_welcome,
    validate_gossip, validate_state_hash, validate_full_sync, validate_intent_abort,
    get_gossip_signing_payload_v2,
    get_state_hash_signing_payload_v2,
    get_full_sync_signing_payload_v2,
    get_intent_signing_payload, get_intent_abort_signing_payload,
    compute_states_hash, compute_full_sync_states_hash_v2,
    compute_full_sync_members_hash_v2, compute_full_sync_membership_events_hash_v2,
    is_strict_state_sync_payload,
    STRICT_STATE_SYNC_VERSION,
)
from modules.handshake import CHALLENGE_TTL_SECONDS
from modules.state_manager import StateManager
from modules.gossip import GossipManager
from modules.intent_manager import Intent, IntentType
from modules.bridge import BridgeStatus
from modules.membership import MEMBER_TIER
from modules.idempotency import check_and_record, generate_event_id
from modules.outbox import OutboxManager

# ---------------------------------------------------------------------------
# Module-level globals -- populated by init_protocol_handlers()
# ---------------------------------------------------------------------------

plugin = None
database = None
config = None
shutdown_event = None
our_pubkey = None
handshake_mgr = None
gossip_mgr = None
state_manager = None
intent_mgr = None
contribution_mgr = None
bridge = None
relay_mgr = None
fee_intel_mgr = None
liquidity_coord = None
peer_reputation_mgr = None
yield_metrics_mgr = None
rationalization_mgr = None
strategic_positioning_mgr = None
outbox_mgr = None
traffic_intel_mgr = None
outbox = None

# Constants and locks (will be overwritten by init if they exist in main)

def init_protocol_handlers(deps: dict):
    """Inject dependency references into this module's namespace.

    Called once from cl-hive.py init() after all managers are created.
    Every key in *deps* becomes a module-level name so that the moved
    handler functions can reference the exact same variable names they
    always did.
    """
    globals().update(deps)

def handle_hello(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle HIVE_HELLO message (join request).

    A node is requesting to join the hive. Channel existence serves as
    proof of stake. The request is stored as pending, awaiting explicit
    approval via hive-approve.

    Flow:
    1. Check if we're a hive member
    2. Check if peer has a channel with us (proof of stake)
    3. Check if peer is already a member or banned
    4. Store as pending request (awaiting hive-approve)
    """
    sender_pubkey = payload.get('pubkey')
    if not sender_pubkey:
        plugin.log(f"cl-hive: HELLO from {peer_id[:16]}... missing pubkey", level='warn')
        return {"result": "continue"}

    # Verify pubkey matches peer_id (identity binding)
    if sender_pubkey != peer_id:
        plugin.log(f"cl-hive: HELLO from {peer_id[:16]}... pubkey mismatch", level='warn')
        return {"result": "continue"}

    # Check if we're a member
    our_pubkey = handshake_mgr.get_our_pubkey()
    our_member = database.get_member(our_pubkey)
    if not our_member:
        plugin.log(f"cl-hive: HELLO from {peer_id[:16]}... but we're not a member", level='debug')
        return {"result": "continue"}

    # SECURITY: Check if peer is banned (prevents ban evasion via rejoin)
    if database.is_banned(peer_id):
        plugin.log(f"cl-hive: HELLO from banned peer {peer_id[:16]}..., ignoring", level='warn')
        return {"result": "continue"}

    # Check if peer is already a member
    existing_member = database.get_member(peer_id)
    if existing_member:
        plugin.log(f"cl-hive: HELLO from {peer_id[:16]}... already a member", level='debug')
        return {"result": "continue"}

    # Check if peer has a channel with us (proof of stake)
    try:
        channels = plugin.rpc.call("listpeerchannels", {"id": peer_id})
        peer_channels = channels.get('channels', [])
        has_channel = any(
            ch.get('state') in ('CHANNELD_NORMAL', 'CHANNELD_AWAITING_LOCKIN')
            for ch in peer_channels
        )
        if not has_channel:
            plugin.log(f"cl-hive: HELLO from {peer_id[:16]}... no channel (proof of stake required)", level='debug')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: HELLO from {peer_id[:16]}... channel check failed: {e}", level='warn')
        return {"result": "continue"}

    # All checks passed — store as pending, awaiting hive-approve
    handshake_mgr.store_pending_request(peer_id)
    plugin.log(f"cl-hive: HELLO from {peer_id[:16]}... stored as pending, awaiting hive-approve")

    return {"result": "continue"}

def handle_challenge(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle HIVE_CHALLENGE message (nonce received).
    
    We received a challenge nonce - create and send attestation.
    """
    nonce = payload.get('nonce')
    hive_id = payload.get('hive_id')

    if not nonce:
        plugin.log(f"cl-hive: CHALLENGE from {peer_id[:16]}... missing nonce", level='warn')
        return {"result": "continue"}

    # SECURITY: Reject challenges from banned peers
    if database and database.is_banned(peer_id):
        plugin.log(f"cl-hive: CHALLENGE from banned peer {peer_id[:16]}..., ignoring", level='warn')
        return {"result": "continue"}

    if not handshake_mgr or not handshake_mgr.has_pending_outbound_hello(peer_id):
        plugin.log(
            f"cl-hive: CHALLENGE from {peer_id[:16]}... no pending outbound HELLO",
            level='debug'
        )
        return {"result": "continue"}
    
    # Create attestation manifest
    try:
        attest_data = handshake_mgr.create_manifest(nonce)
        
        # Build ATTEST message
        from modules.protocol import create_attest
        attest_msg = create_attest(
            pubkey=attest_data['manifest']['pubkey'],
            version=attest_data['manifest']['version'],
            features=attest_data['manifest']['features'],
            nonce_signature=attest_data['nonce_signature'],
            manifest_signature=attest_data['manifest_signature'],
            manifest=attest_data['manifest']
        )
        
        plugin.rpc.call("sendcustommsg", {
            "node_id": peer_id,
            "msg": attest_msg.hex()
        })
        plugin.log(f"cl-hive: Sent ATTEST to {peer_id[:16]}...")
        
    except Exception as e:
        plugin.log(f"cl-hive: Failed to create/send ATTEST: {e}", level='warn')
    
    return {"result": "continue"}

def handle_attest(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle HIVE_ATTEST message (manifest verification).
    
    Verify the candidate's attestation and send WELCOME if valid.
    """
    # Get the challenge we sent
    pending = handshake_mgr.get_pending_challenge(peer_id)
    if not pending:
        plugin.log(f"cl-hive: ATTEST from {peer_id[:16]}... but no pending challenge", level='warn')
        return {"result": "continue"}

    now = int(time.time())
    if now - pending["issued_at"] > CHALLENGE_TTL_SECONDS:
        handshake_mgr.clear_challenge(peer_id)
        plugin.log(f"cl-hive: ATTEST from {peer_id[:16]}... challenge expired", level='warn')
        return {"result": "continue"}

    expected_nonce = pending["nonce"]
    
    manifest_data = payload.get('manifest')
    if not isinstance(manifest_data, dict):
        plugin.log(f"cl-hive: ATTEST from {peer_id[:16]}... missing manifest", level='warn')
        handshake_mgr.clear_challenge(peer_id)
        return {"result": "continue"}

    required_fields = ["pubkey", "version", "features", "timestamp", "nonce"]
    for field in required_fields:
        if field not in manifest_data:
            plugin.log(f"cl-hive: ATTEST from {peer_id[:16]}... missing {field}", level='warn')
            handshake_mgr.clear_challenge(peer_id)
            return {"result": "continue"}

    if payload.get('pubkey') and payload.get('pubkey') != manifest_data.get('pubkey'):
        plugin.log(f"cl-hive: ATTEST from {peer_id[:16]}... pubkey mismatch", level='warn')
        handshake_mgr.clear_challenge(peer_id)
        return {"result": "continue"}
    if payload.get('version') and payload.get('version') != manifest_data.get('version'):
        plugin.log(f"cl-hive: ATTEST from {peer_id[:16]}... version mismatch", level='warn')
        handshake_mgr.clear_challenge(peer_id)
        return {"result": "continue"}
    if payload.get('features') and payload.get('features') != manifest_data.get('features'):
        plugin.log(f"cl-hive: ATTEST from {peer_id[:16]}... features mismatch", level='warn')
        handshake_mgr.clear_challenge(peer_id)
        return {"result": "continue"}

    if manifest_data.get('pubkey') != peer_id:
        plugin.log(f"cl-hive: ATTEST from {peer_id[:16]}... pubkey not bound to peer", level='warn')
        handshake_mgr.clear_challenge(peer_id)
        return {"result": "continue"}

    if not isinstance(manifest_data.get('features'), list):
        plugin.log(f"cl-hive: ATTEST from {peer_id[:16]}... invalid features", level='warn')
        handshake_mgr.clear_challenge(peer_id)
        return {"result": "continue"}
    
    nonce_sig = payload.get('nonce_signature')
    manifest_sig = payload.get('manifest_signature')
    
    if not nonce_sig or not manifest_sig:
        plugin.log(f"cl-hive: ATTEST from {peer_id[:16]}... missing signatures", level='warn')
        return {"result": "continue"}
    
    # Verify manifest
    is_valid, error = handshake_mgr.verify_manifest(
        manifest_data, nonce_sig, manifest_sig, expected_nonce
    )
    
    if not is_valid:
        plugin.log(f"cl-hive: Invalid ATTEST from {peer_id[:16]}...: {error}", level='warn')
        handshake_mgr.clear_challenge(peer_id)
        return {"result": "continue"}
    
    satisfied, missing = handshake_mgr.check_requirements(
        pending["requirements"], manifest_data.get("features", [])
    )
    if not satisfied:
        plugin.log(
            f"cl-hive: ATTEST from {peer_id[:16]}... missing requirements: {missing}",
            level='warn'
        )
        handshake_mgr.clear_challenge(peer_id)
        return {"result": "continue"}

    # SECURITY: Final ban check before adding member (prevents race with ban during handshake)
    if database.is_banned(peer_id):
        plugin.log(f"cl-hive: ATTEST from banned peer {peer_id[:16]}..., rejecting", level='warn')
        handshake_mgr.clear_challenge(peer_id)
        return {"result": "continue"}

    # Get Hive info for WELCOME
    existing_member = database.get_member(peer_id)
    members = database.get_all_members()
    hive_id = "hive"
    for m in members:
        if m.get('metadata'):
            try:
                metadata = json.loads(m['metadata'])
                hive_id = metadata.get('hive_id', 'hive')
                break
            except (json.JSONDecodeError, TypeError):
                continue

    # Calculate real state hash via StateManager
    if state_manager:
        state_hash = state_manager.calculate_fleet_hash()
    else:
        state_hash = "0" * 64

    member_count = len(members) if existing_member else len(members) + 1

    # Sign and send WELCOME with actual tier
    welcome_signing_fields = json.dumps({
        "hive_id": hive_id,
        "member_count": member_count,
        "state_hash": state_hash,
        "tier": MEMBER_TIER,
    }, sort_keys=True, separators=(',', ':'))
    welcome_sig = ""
    try:
        welcome_sig = plugin.rpc.signmessage(welcome_signing_fields).get("zbase", "")
    except Exception as e:
        plugin.log(f"cl-hive: Failed to sign WELCOME: {e}", level='warn')
        return {"result": "continue"}
    if not welcome_sig:
        plugin.log("cl-hive: Failed to sign WELCOME: empty signature", level='warn')
        return {"result": "continue"}
    welcome_msg = create_welcome(hive_id, MEMBER_TIER, member_count, state_hash, signature=welcome_sig)

    try:
        plugin.rpc.call("sendcustommsg", {
            "node_id": peer_id,
            "msg": welcome_msg.hex()
        })
        plugin.log(f"cl-hive: Sent WELCOME to {peer_id[:16]}... (new member)")
    except Exception as e:
        plugin.log(f"cl-hive: Failed to send WELCOME: {e}", level='warn')
        return {"result": "continue"}

    # Verification passed and WELCOME was delivered locally. Commit membership now.
    joined_at = int(time.time())
    newly_added = False
    if not existing_member:
        newly_added = database.add_member(
            peer_id=peer_id,
            tier=MEMBER_TIER,
            joined_at=joined_at
        )
        if not newly_added and not database.get_member(peer_id):
            plugin.log(
                f"cl-hive: Failed to activate member {peer_id[:16]}... after WELCOME delivery",
                level='warn'
            )
            return {"result": "continue"}

    if newly_added:
        database.log_membership_event("joined", peer_id)

    # Phase B: persist peer capabilities from manifest features
    manifest_features = manifest_data.get("features", [])
    database.save_peer_capabilities(peer_id, manifest_features)

    # Capture addresses from listpeers for the new member (Issue #60)
    if plugin:
        try:
            peers_info = plugin.rpc.listpeers(id=peer_id)
            if peers_info and peers_info.get('peers'):
                addrs = peers_info['peers'][0].get('netaddr', [])
                if addrs:
                    database.update_member(peer_id, addresses=json.dumps(addrs))
        except Exception as e:
            plugin.log(f"cl-hive: Failed to capture addresses for {peer_id[:16]}...: {e}", level='debug')

    # Initialize presence tracking so uptime_pct starts accumulating (Issue #59)
    # The peer is connected (they just completed the handshake), so mark online
    database.update_presence(peer_id, is_online=True, now_ts=joined_at, window_seconds=30 * 86400)

    handshake_mgr.clear_challenge(peer_id)

    # Broadcast membership update to all existing members
    _broadcast_full_sync_to_members(plugin)

    return {"result": "continue"}

def handle_welcome(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle HIVE_WELCOME message (session established).

    We've been accepted into the Hive!

    SECURITY: Requires signature to prevent spoofed WELCOME from non-hive peers.
    """
    # SECURITY: Verify signature to prevent hive-join spoofing
    signature = payload.get('signature')
    if not signature:
        plugin.log(f"cl-hive: WELCOME rejected (unsigned) from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    hive_id = payload.get('hive_id')
    state_hash = payload.get('state_hash', '')
    member_count = payload.get('member_count')
    tier = payload.get('tier')

    # Build canonical signing payload and verify
    signing_fields = json.dumps({
        "hive_id": hive_id,
        "member_count": member_count,
        "state_hash": state_hash,
        "tier": tier,
    }, sort_keys=True, separators=(',', ':'))
    try:
        verify_result = plugin.rpc.checkmessage(signing_fields, signature)
        if not verify_result.get("verified") or verify_result.get("pubkey") != peer_id:
            plugin.log(f"cl-hive: WELCOME invalid signature from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: WELCOME signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # SECURITY: Verify we actually sent a HELLO to this peer (prevents unsolicited WELCOME)
    if handshake_mgr and not handshake_mgr.has_pending_outbound_hello(peer_id):
        plugin.log(
            f"cl-hive: WELCOME rejected from {peer_id[:16]}... — no outbound HELLO sent to this peer",
            level='warn'
        )
        return {"result": "continue"}

    plugin.log(
        f"cl-hive: WELCOME received! Joined '{hive_id}' as {tier} "
        f"(Hive has {member_count} members)"
    )

    # Clear the outbound tracking now that we've joined
    if handshake_mgr:
        handshake_mgr.clear_outbound_hello(peer_id)

    # Store Hive membership info for ourselves
    if database and our_pubkey:
        now = int(time.time())
        # Start as member — single-role model, all members have equal privileges.
        database.add_member(our_pubkey, tier='member', joined_at=now)
        # Store hive_id in metadata
        database.update_member(our_pubkey, metadata=json.dumps({"hive_id": hive_id}))
        plugin.log(f"cl-hive: Stored membership (tier=member, hive_id={hive_id})")

        # Add the peer that welcomed us as member.
        database.add_member(peer_id, tier='member', joined_at=now)

    # Initiate state sync with the peer that welcomed us
    if gossip_mgr and plugin:
        state_hash_msg = _create_signed_state_hash_msg()
        if state_hash_msg:
            try:
                plugin.rpc.call("sendcustommsg", {
                    "node_id": peer_id,
                    "msg": state_hash_msg.hex()
                })
                plugin.log(f"cl-hive: STATE_HASH sent to {peer_id[:16]}... for anti-entropy sync")
            except Exception as e:
                plugin.log(f"cl-hive: Failed to send STATE_HASH to {peer_id[:16]}...: {e}", level='warn')

    return {"result": "continue"}

# =============================================================================
# PHASE 2: STATE MANAGEMENT HANDLERS
# =============================================================================

def handle_gossip(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle HIVE_GOSSIP message (state update from peer).

    Process incoming gossip and update our local state cache.
    The GossipManager handles version validation and StateManager updates.

    SECURITY: Requires cryptographic signature verification.

    RELAY: Supports multi-hop relay for non-mesh topologies.
    """
    if not gossip_mgr:
        return {"result": "continue"}

    # RELAY: Check deduplication before processing
    if not _should_process_message(payload):
        plugin.log(f"cl-hive: GOSSIP duplicate from {peer_id[:16]}..., skipping", level='debug')
        return {"result": "continue"}

    # SECURITY: Validate payload structure including signature field
    if not validate_gossip(payload):
        plugin.log(
            f"cl-hive: GOSSIP rejected from {peer_id[:16]}...: invalid payload",
            level='warn'
        )
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check (reject stale replayed messages)
    if not _check_timestamp_freshness(payload, MAX_GOSSIP_AGE_SECONDS, "GOSSIP"):
        return {"result": "continue"}

    sender_id = payload.get("sender_id")

    if not is_strict_state_sync_payload(payload):
        plugin.log(
            f"cl-hive: GOSSIP rejected from {peer_id[:16]}...: strict envelope v2 required",
            level='warn'
        )
        return {"result": "continue"}

    signature_v2 = payload.get("signature_v2")
    if not isinstance(signature_v2, str) or len(signature_v2) < 10:
        plugin.log(
            f"cl-hive: GOSSIP rejected from {peer_id[:16]}...: missing signature_v2",
            level='warn'
        )
        return {"result": "continue"}

    # SECURITY: Fast-reject ex-members before signature verification to avoid
    # graph-dependent checkmessage failures after a peer has left the hive.
    if database:
        member = database.get_member(sender_id)
        if not member:
            plugin.log(f"cl-hive: GOSSIP from non-member {sender_id[:16]}..., ignoring", level='debug')
            return {"result": "continue"}

    # SECURITY: Verify cryptographic signature
    try:
        signing_payload = get_gossip_signing_payload_v2(payload)
    except ValueError as e:
        plugin.log(
            f"cl-hive: GOSSIP rejected from {peer_id[:16]}...: invalid v2 payload ({e})",
            level='warn'
        )
        return {"result": "continue"}

    try:
        result = plugin.rpc.checkmessage(signing_payload, signature_v2, sender_id)
        if not result.get("verified") or result.get("pubkey") != sender_id:
            plugin.log(
                f"cl-hive: GOSSIP v2 signature invalid from {peer_id[:16]}...",
                level='warn'
            )
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: GOSSIP v2 signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # SECURITY: Validate sender (supports relay - peer_id may differ from sender_id)
    if not _validate_relay_sender(peer_id, sender_id, payload):
        is_relayed = _is_relayed_message(payload)
        if is_relayed:
            plugin.log(
                f"cl-hive: GOSSIP relayed by non-member {peer_id[:16]}..., ignoring",
                level='warn'
            )
        else:
            plugin.log(
                f"cl-hive: GOSSIP sender mismatch: claimed {sender_id[:16]}... but peer is {peer_id[:16]}...",
                level='warn'
            )
        return {"result": "continue"}

    # Verify original sender is a Hive member and not banned before processing
    if not database:
        return {"result": "continue"}
    member = database.get_member(sender_id)
    if not member:
        plugin.log(f"cl-hive: GOSSIP from non-member {sender_id[:16]}..., ignoring", level='warn')
        return {"result": "continue"}
    if database.is_banned(sender_id):
        plugin.log(f"cl-hive: GOSSIP from banned member {sender_id[:16]}..., ignoring", level='warn')
        return {"result": "continue"}

    accepted = gossip_mgr.process_gossip(sender_id, payload)

    if accepted:
        is_relayed = _is_relayed_message(payload)
        relay_info = " (relayed)" if is_relayed else ""
        plugin.log(f"cl-hive: GOSSIP accepted from {sender_id[:16]}...{relay_info} "
                   f"(v{payload.get('version', '?')})", level='debug')

        # Store addresses for auto-connect (Issue #38)
        addresses = payload.get("addresses", [])
        if addresses and database:
            # Store as JSON string
            import json
            database.update_member(sender_id, addresses=json.dumps(addresses))

        # Auto-connect to member if not already connected (Issue #38)
        _try_auto_connect(sender_id, addresses)

    # RELAY: Forward to other members if TTL allows
    relay_count = _relay_message(HiveMessageType.GOSSIP, payload, peer_id)
    if relay_count > 0:
        plugin.log(f"cl-hive: GOSSIP relayed to {relay_count} members", level='debug')

    return {"result": "continue"}

def handle_state_hash(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle HIVE_STATE_HASH message (anti-entropy check).

    Compare remote hash against our local state. If mismatch,
    send a FULL_SYNC with our complete state including membership.

    SECURITY: Requires cryptographic signature verification.
    """
    if not gossip_mgr or not state_manager:
        return {"result": "continue"}

    # SECURITY: Validate payload structure including signature field
    if not validate_state_hash(payload):
        plugin.log(
            f"cl-hive: STATE_HASH rejected from {peer_id[:16]}...: invalid payload",
            level='warn'
        )
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_STATE_HASH_AGE_SECONDS, "STATE_HASH"):
        return {"result": "continue"}

    sender_id = payload.get("sender_id")
    if not is_strict_state_sync_payload(payload):
        plugin.log(
            f"cl-hive: STATE_HASH rejected from {peer_id[:16]}...: strict envelope v2 required",
            level='warn'
        )
        return {"result": "continue"}

    signature_v2 = payload.get("signature_v2")
    membership_hash = payload.get("membership_hash")
    if not isinstance(signature_v2, str) or len(signature_v2) < 10:
        plugin.log(
            f"cl-hive: STATE_HASH rejected from {peer_id[:16]}...: missing signature_v2",
            level='warn'
        )
        return {"result": "continue"}
    if not isinstance(membership_hash, str) or not membership_hash:
        plugin.log(
            f"cl-hive: STATE_HASH rejected from {peer_id[:16]}...: missing membership_hash",
            level='warn'
        )
        return {"result": "continue"}

    signing_payload = get_state_hash_signing_payload_v2(payload)

    try:
        result = plugin.rpc.checkmessage(signing_payload, signature_v2, sender_id)
        if not result.get("verified") or result.get("pubkey") != sender_id:
            plugin.log(
                f"cl-hive: STATE_HASH v2 signature invalid from {peer_id[:16]}...",
                level='warn'
            )
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: STATE_HASH v2 signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # SECURITY: Verify sender identity matches peer_id
    if sender_id != peer_id:
        plugin.log(
            f"cl-hive: STATE_HASH sender mismatch: claimed {sender_id[:16]}... but peer is {peer_id[:16]}...",
            level='warn'
        )
        return {"result": "continue"}

    # SECURITY: Verify sender is a member and not banned
    if database:
        member = database.get_member(peer_id)
        if not member:
            plugin.log(f"cl-hive: STATE_HASH from non-member {peer_id[:16]}..., ignoring", level='warn')
            return {"result": "continue"}
        if database.is_banned(peer_id):
            plugin.log(f"cl-hive: STATE_HASH from banned member {peer_id[:16]}..., ignoring", level='warn')
            return {"result": "continue"}

    hashes_match = gossip_mgr.process_state_hash(peer_id, payload)

    if not hashes_match:
        # State divergence detected - send signed FULL_SYNC with membership
        plugin.log(f"cl-hive: State divergence with {peer_id[:16]}..., sending FULL_SYNC")

        full_sync_msg = _create_signed_full_sync_msg()
        if full_sync_msg:
            try:
                plugin.rpc.call("sendcustommsg", {
                    "node_id": peer_id,
                    "msg": full_sync_msg.hex()
                })
            except Exception as e:
                plugin.log(f"cl-hive: Failed to send FULL_SYNC: {e}", level='warn')

    return {"result": "continue"}

def handle_full_sync(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle HIVE_FULL_SYNC message (complete state transfer).

    Merge the received state with our local state, preferring
    higher version numbers for each peer.

    SECURITY: Requires cryptographic signature verification.
    Only accept FULL_SYNC from authenticated Hive members.
    """
    if not gossip_mgr:
        return {"result": "continue"}

    # SECURITY: Validate payload structure including signature field
    if not validate_full_sync(payload):
        plugin.log(
            f"cl-hive: FULL_SYNC rejected from {peer_id[:16]}...: invalid payload structure",
            level='warn'
        )
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_STATE_HASH_AGE_SECONDS, "FULL_SYNC"):
        return {"result": "continue"}

    sender_id = payload.get("sender_id")
    if not is_strict_state_sync_payload(payload):
        plugin.log(
            f"cl-hive: FULL_SYNC rejected from {peer_id[:16]}...: strict envelope v2 required",
            level='warn'
        )
        return {"result": "continue"}

    signature_v2 = payload.get("signature_v2")
    states_hash_v2 = payload.get("states_hash_v2")
    members_hash_v2 = payload.get("members_hash_v2")
    membership_events_hash_v2 = payload.get("membership_events_hash_v2")
    if not isinstance(signature_v2, str) or len(signature_v2) < 10:
        plugin.log(
            f"cl-hive: FULL_SYNC rejected from {peer_id[:16]}...: missing signature_v2",
            level='warn'
        )
        return {"result": "continue"}
    if (
        not isinstance(states_hash_v2, str)
        or not isinstance(members_hash_v2, str)
        or not isinstance(membership_events_hash_v2, str)
    ):
        plugin.log(
            f"cl-hive: FULL_SYNC rejected from {peer_id[:16]}...: missing v2 hashes",
            level='warn'
        )
        return {"result": "continue"}

    states = payload.get("states", [])
    members = payload.get("members", [])
    membership_events = payload.get("membership_events", [])
    try:
        computed_states_hash_v2 = compute_full_sync_states_hash_v2(states)
        computed_members_hash_v2 = compute_full_sync_members_hash_v2(members)
        computed_membership_events_hash_v2 = compute_full_sync_membership_events_hash_v2(
            membership_events
        )
    except ValueError as e:
        plugin.log(
            f"cl-hive: FULL_SYNC rejected from {peer_id[:16]}...: invalid v2 payload ({e})",
            level='warn'
        )
        return {"result": "continue"}

    if (
        states_hash_v2 != computed_states_hash_v2
        or members_hash_v2 != computed_members_hash_v2
        or membership_events_hash_v2 != computed_membership_events_hash_v2
    ):
        plugin.log(
            f"cl-hive: FULL_SYNC rejected from {peer_id[:16]}...: v2 hash mismatch",
            level='warn'
        )
        return {"result": "continue"}

    signing_payload = get_full_sync_signing_payload_v2(payload)

    try:
        result = plugin.rpc.checkmessage(signing_payload, signature_v2, sender_id)
        if not result.get("verified") or result.get("pubkey") != sender_id:
            plugin.log(
                f"cl-hive: FULL_SYNC v2 signature invalid from {peer_id[:16]}...",
                level='warn'
            )
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: FULL_SYNC v2 signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # SECURITY: Verify sender identity matches peer_id (prevent relay attacks)
    if sender_id != peer_id:
        plugin.log(
            f"cl-hive: FULL_SYNC sender mismatch: claimed {sender_id[:16]}... but peer is {peer_id[:16]}...",
            level='warn'
        )
        return {"result": "continue"}

    # SECURITY: Verify states match the signed fleet_hash (prevent state injection)
    fleet_hash = payload.get("fleet_hash", "")
    if states and fleet_hash:
        computed_hash = compute_states_hash(states)
        if computed_hash != fleet_hash:
            plugin.log(
                f"cl-hive: FULL_SYNC states hash mismatch from {peer_id[:16]}...: "
                f"computed={computed_hash[:16]}... expected={fleet_hash[:16]}...",
                level='warn'
            )
            return {"result": "continue"}

    # SECURITY: Membership check to prevent state poisoning
    if database:
        member = database.get_member(peer_id)
        if not member:
            plugin.log(
                f"cl-hive: FULL_SYNC rejected from non-member {peer_id[:16]}...",
                level='warn'
            )
            return {"result": "continue"}
        if database.is_banned(peer_id):
            plugin.log(
                f"cl-hive: FULL_SYNC rejected from banned member {peer_id[:16]}...",
                level='warn'
            )
            return {"result": "continue"}

    updated = gossip_mgr.process_full_sync(peer_id, payload)

    # Process membership list if included (Phase 5 enhancement)
    members_synced = 0
    if database and "members" in payload:
        members_synced = _apply_membership_sync(
            payload["members"],
            peer_id,
            plugin,
            membership_events=payload.get("membership_events"),
        )

    plugin.log(f"cl-hive: FULL_SYNC from {peer_id[:16]}...: {updated} states, {members_synced} members synced")

    return {"result": "continue"}

def _valid_membership_tombstone_event(event: Dict[str, Any]) -> bool:
    """Validate tombstone events carried in FULL_SYNC catch-up."""
    if not isinstance(event, dict):
        return False
    if not isinstance(event.get("event_id"), str) or not event.get("event_id"):
        return False
    if not isinstance(event.get("peer_id"), str) or not event.get("peer_id"):
        return False
    if event.get("event") not in {"banned", "left", "removed"}:
        return False
    if not isinstance(event.get("timestamp"), int) or event.get("timestamp") <= 0:
        return False
    if not isinstance(event.get("joined_at_cutoff"), int) or event.get("joined_at_cutoff") < 0:
        return False
    actor_peer_id = event.get("actor_peer_id")
    if actor_peer_id is not None and not isinstance(actor_peer_id, str):
        return False
    reason = event.get("reason")
    if reason is not None and not isinstance(reason, str):
        return False
    return True

def _apply_membership_events(events: list, sender_id: str, plugin: Plugin) -> int:
    """
    Apply membership tombstones received via FULL_SYNC catch-up.

    Removes any local member whose current membership is at or before the
    signed joined_at_cutoff. Newer rejoins are preserved.
    """
    if not database or not isinstance(events, list):
        return 0

    changed = 0
    for event in events:
        if not _valid_membership_tombstone_event(event):
            continue

        peer_id = event["peer_id"]
        event_type = event["event"]
        actor_peer_id = event.get("actor_peer_id") or sender_id
        reason = event.get("reason")
        joined_at_cutoff = int(event.get("joined_at_cutoff") or 0)

        database.record_membership_tombstone(
            event_id=event["event_id"],
            peer_id=peer_id,
            event=event_type,
            actor_peer_id=actor_peer_id,
            reason=reason,
            timestamp=event["timestamp"],
            joined_at_cutoff=joined_at_cutoff,
        )

        member = database.get_member(peer_id)
        if member and int(member.get("joined_at") or 0) <= joined_at_cutoff:
            if event_type == "banned" and not database.is_banned(peer_id):
                database.add_ban(peer_id, reason or "member_ban", actor_peer_id)
            _execute_member_removal(peer_id, reason=event_type)
            changed += 1

    return changed

def _apply_membership_sync(members_list: list, sender_id: str, plugin: Plugin,
                           membership_events: Optional[list] = None) -> int:
    """
    Apply membership list from FULL_SYNC payload.

    Only adds members we don't already know about. Does not demote
    or remove members (membership changes require proper protocol).

    Args:
        members_list: List of member dicts with peer_id, tier, joined_at
        sender_id: ID of the peer who sent this sync
        plugin: Plugin for logging

    Returns:
        Number of new members added
    """
    if not database or not isinstance(members_list, list):
        return 0

    changed = _apply_membership_events(membership_events or [], sender_id, plugin)
    added = 0
    updated = 0
    for member_info in members_list:
        if not isinstance(member_info, dict):
            continue

        member_peer_id = member_info.get("peer_id")
        if not member_peer_id or not isinstance(member_peer_id, str):
            continue

        tier = member_info.get("tier", "member")
        joined_at = member_info.get("joined_at", int(time.time()))
        addresses = member_info.get("addresses", [])

        # Validate joined_at is an integer
        if not isinstance(joined_at, int) or joined_at <= 0:
            joined_at = int(time.time())

        # Validate addresses is a list
        if not isinstance(addresses, list):
            addresses = []

        # Validate tier value (single-role model)
        if tier != "member":
            tier = "member"

        # Check if we already know this member
        existing = database.get_member(member_peer_id)
        if existing:
            existing_tier = existing.get("tier", "member")
            needs_update = False

            # Update addresses if provided and we don't have them
            if addresses:
                existing_addresses = existing.get("addresses")
                if not existing_addresses:
                    try:
                        import json
                        database.update_member(member_peer_id, addresses=json.dumps(addresses))
                        if not needs_update:
                            plugin.log(f"cl-hive: Synced addresses for {member_peer_id[:16]}...")
                    except Exception as e:
                        plugin.log(f"cl-hive: Failed to sync addresses: {e}", level='debug')

            continue  # Already have this member, done with updates

        try:
            database.add_member(
                peer_id=member_peer_id,
                tier=tier,
                joined_at=joined_at
            )
            # Store addresses if provided (Issue #38)
            if addresses:
                import json
                database.update_member(member_peer_id, addresses=json.dumps(addresses))

            added += 1
            plugin.log(f"cl-hive: Added member {member_peer_id[:16]}... ({tier}) from sync")

            # Auto-connect to new member (Issue #38)
            _try_auto_connect(member_peer_id, addresses)

        except Exception as e:
            plugin.log(f"cl-hive: Failed to add synced member: {e}", level='warn')

    if updated > 0:
        plugin.log(f"cl-hive: Membership sync: {added} added, {updated} tiers upgraded")

    return changed + added + updated

def _create_membership_payload() -> list:
    """
    Create membership list for inclusion in FULL_SYNC.

    Returns:
        List of member dicts with peer_id, tier, joined_at, addresses
    """
    if not database:
        return []

    members = database.get_all_members()
    result = []
    for m in members:
        # SECURITY: Exclude banned peers from membership list
        if database.is_banned(m["peer_id"]):
            continue
        member_dict = {
            "peer_id": m["peer_id"],
            "tier": m.get("tier", "member"),
            "joined_at": m.get("joined_at", 0)
        }
        # Include addresses if available (Issue #38)
        addresses_json = m.get("addresses")
        if addresses_json:
            try:
                import json
                member_dict["addresses"] = json.loads(addresses_json)
            except (json.JSONDecodeError, TypeError) as e:
                if plugin:
                    plugin.log(f"cl-hive: Invalid addresses JSON for {m.get('peer_id', '?')[:16]}...: {e}", level='debug')
        # For our own entry, use current addresses
        if m["peer_id"] == our_pubkey:
            member_dict["addresses"] = _get_our_addresses()
        result.append(member_dict)
    return result

def _create_signed_full_sync_msg() -> Optional[bytes]:
    """
    Create a signed FULL_SYNC message with membership.

    SECURITY: All FULL_SYNC messages must be cryptographically signed
    to prevent state poisoning attacks.

    Returns:
        Serialized and signed FULL_SYNC message, or None if signing fails
    """
    if not gossip_mgr or not plugin or not our_pubkey:
        return None

    # Create base payload
    full_sync_payload = gossip_mgr.create_full_sync_payload()
    full_sync_payload["members"] = _create_membership_payload()
    if database:
        full_sync_payload["membership_events"] = database.get_membership_tombstones(limit=200)

    # Add sender identification
    full_sync_payload["sender_id"] = our_pubkey
    full_sync_payload["timestamp"] = int(time.time())
    full_sync_payload["states_hash_v2"] = compute_full_sync_states_hash_v2(
        full_sync_payload.get("states", [])
    )
    full_sync_payload["members_hash_v2"] = compute_full_sync_members_hash_v2(
        full_sync_payload.get("members", [])
    )
    full_sync_payload["membership_events_hash_v2"] = compute_full_sync_membership_events_hash_v2(
        full_sync_payload.get("membership_events", [])
    )

    # Sign the payload using the strict v2 contract.
    signing_payload = get_full_sync_signing_payload_v2(full_sync_payload)
    try:
        sig_result = plugin.rpc.signmessage(signing_payload)
        signature = sig_result["zbase"]
        full_sync_payload["signature"] = signature
        full_sync_payload["signature_v2"] = signature
    except Exception as e:
        plugin.log(f"cl-hive: Failed to sign FULL_SYNC: {e}", level='error')
        return None

    return serialize(
        HiveMessageType.FULL_SYNC,
        full_sync_payload,
        envelope_version=STRICT_STATE_SYNC_VERSION,
    )

def _create_signed_state_hash_msg() -> Optional[bytes]:
    """
    Create a signed STATE_HASH message for anti-entropy sync.

    SECURITY: All STATE_HASH messages must be cryptographically signed
    to prevent hash manipulation attacks.

    Returns:
        Serialized and signed STATE_HASH message, or None if signing fails
    """
    if not gossip_mgr or not plugin or not our_pubkey:
        return None

    # Create base payload
    state_hash_payload = gossip_mgr.create_state_hash_payload()

    # Add sender identification and timestamp
    state_hash_payload["sender_id"] = our_pubkey
    state_hash_payload["timestamp"] = int(time.time())

    # Sign the payload using the strict v2 contract.
    signing_payload = get_state_hash_signing_payload_v2(state_hash_payload)
    try:
        sig_result = plugin.rpc.signmessage(signing_payload)
        signature = sig_result["zbase"]
        state_hash_payload["signature"] = signature
        state_hash_payload["signature_v2"] = signature
    except Exception as e:
        plugin.log(f"cl-hive: Failed to sign STATE_HASH: {e}", level='error')
        return None

    return serialize(
        HiveMessageType.STATE_HASH,
        state_hash_payload,
        envelope_version=STRICT_STATE_SYNC_VERSION,
    )

def _get_our_addresses() -> List[str]:
    """
    Get our node's connection addresses from getinfo.

    Returns:
        List of connection strings like ["1.2.3.4:9735", "xyz.onion:9735"]
    """
    if not plugin:
        return []

    try:
        info = plugin.rpc.getinfo()
        addresses = []
        for addr in info.get("address", []):
            addr_type = addr.get("type", "")
            addr_str = addr.get("address", "")
            port = addr.get("port", 9735)
            if addr_str and addr_type in ("ipv4", "ipv6", "torv3"):
                addresses.append(f"{addr_str}:{port}")
        return addresses
    except Exception:
        return []

def _is_peer_connected(peer_id: str) -> bool:
    """Check if we're already connected to a peer."""
    if not plugin:
        return False
    try:
        peers = plugin.rpc.listpeers(peer_id).get("peers", [])
        return len(peers) > 0 and peers[0].get("connected", False)
    except Exception:
        return False

def _try_auto_connect(peer_id: str, addresses: List[str]) -> bool:
    """
    Attempt to auto-connect to a hive member if not already connected.

    This enables automatic mesh formation when new members join via gossip.
    (Issue #38: Auto-connect hive members on join)

    Args:
        peer_id: The member's public key
        addresses: List of connection strings like ["1.2.3.4:9735", "xyz.onion:9735"]

    Returns:
        True if connection was established or already exists, False otherwise
    """
    if not plugin or not peer_id or peer_id == our_pubkey:
        return False

    # Skip if no addresses provided
    if not addresses:
        return False

    # Check if already connected
    if _is_peer_connected(peer_id):
        return True

    # Try each address until one succeeds
    for addr in addresses:
        try:
            connect_str = f"{peer_id}@{addr}"
            plugin.rpc.connect(connect_str)
            plugin.log(f"cl-hive: Auto-connected to hive member {peer_id[:16]}... via {addr}", level='info')
            return True
        except Exception as e:
            # Log at debug level - connection failures are common (firewalls, NAT, etc.)
            plugin.log(f"cl-hive: Auto-connect to {peer_id[:16]}... via {addr} failed: {e}", level='debug')
            continue

    return False

def _create_signed_gossip_msg(capacity_sats: int, available_sats: int,
                               fee_policy: Dict, topology: list,
                               addresses: List[str] = None) -> Optional[bytes]:
    """
    Create a signed GOSSIP message for broadcast.

    SECURITY: All GOSSIP messages must be cryptographically signed
    to prevent data tampering attacks where attackers modify fee
    policies, topology, or capacity data.

    Args:
        capacity_sats: Total Hive channel capacity
        available_sats: Available outbound liquidity
        fee_policy: Current fee policy dict
        topology: List of external peer connections
        addresses: List of our connection addresses for auto-connect

    Returns:
        Serialized and signed GOSSIP message, or None if signing fails
    """
    if not gossip_mgr or not plugin or not our_pubkey:
        return None

    # Create gossip payload using GossipManager
    gossip_payload = gossip_mgr.create_gossip_payload(
        our_pubkey=our_pubkey,
        capacity_sats=capacity_sats,
        available_sats=available_sats,
        fee_policy=fee_policy,
        topology=topology,
        addresses=addresses or [],
    )

    # Add sender identification for signature verification
    gossip_payload["sender_id"] = our_pubkey
    gossip_payload.setdefault("fleet_hash", gossip_payload.get("state_hash", ""))

    # Sign the payload using the strict v2 contract.
    signing_payload = get_gossip_signing_payload_v2(gossip_payload)
    try:
        sig_result = plugin.rpc.signmessage(signing_payload)
        signature = sig_result["zbase"]
        gossip_payload["signature"] = signature
        gossip_payload["signature_v2"] = signature
    except Exception as e:
        plugin.log(f"cl-hive: Failed to sign GOSSIP: {e}", level='error')
        return None

    return serialize(
        HiveMessageType.GOSSIP,
        gossip_payload,
        envelope_version=STRICT_STATE_SYNC_VERSION,
    )

def _broadcast_full_sync_to_members(plugin: Plugin) -> None:
    """
    Broadcast signed FULL_SYNC with membership to all existing members.

    Called after adding a new member to ensure all nodes sync.
    SECURITY: All FULL_SYNC messages are cryptographically signed.
    """
    if not database or not gossip_mgr :
        plugin.log(f"cl-hive: _broadcast_full_sync_to_members: missing deps", level='debug')
        return

    targets = _get_broadcast_targets()
    plugin.log(f"cl-hive: Broadcasting membership to {len(targets)} eligible members")

    # Create signed FULL_SYNC payload with membership
    full_sync_msg = _create_signed_full_sync_msg()
    if not full_sync_msg:
        plugin.log("cl-hive: Failed to create signed FULL_SYNC", level='error')
        return

    result = _broadcast_member_message(
        message_bytes=full_sync_msg,
        reliability="reliable",
        failure_policy="fail_closed",
        log_label="full_sync",
    )
    sent_count = result["queued"] or result["sent"]
    if not result["ok"]:
        plugin.log(
            f"cl-hive: Membership broadcast incomplete: {sent_count}/{result['attempted']} delivered",
            level='warning',
        )
        return

    plugin.log(f"cl-hive: Membership broadcast complete: {sent_count} messages sent")
def _handle_peer_connected(peer_id: str, member: Dict):
    """Process peer connection on background thread (RPC calls inside)."""
    if not database:
        return
    now = int(time.time())
    database.update_member(peer_id, last_seen=now)
    database.update_presence(peer_id, is_online=True, now_ts=now, window_seconds=30 * 86400)

    # Track VPN connection status + populate missing addresses (Issue #60)
    if plugin:
        try:
            peers = plugin.rpc.listpeers(id=peer_id)
            if peers and peers.get('peers'):
                netaddr = peers['peers'][0].get('netaddr', [])
                if netaddr:
                    if not member.get('addresses'):
                        database.update_member(peer_id, addresses=json.dumps(netaddr))
        except Exception as e:
            if plugin:
                plugin.log(f"cl-hive: Failed to update addresses for {peer_id[:16]}...: {e}", level='debug')

    if plugin:
        plugin.log(f"cl-hive: Hive member {peer_id[:16]}... connected, sending STATE_HASH")

    # Send signed STATE_HASH for anti-entropy check
    state_hash_msg = _create_signed_state_hash_msg()
    if state_hash_msg:
        try:
            plugin.rpc.call("sendcustommsg", {
                "node_id": peer_id,
                "msg": state_hash_msg.hex()
            })
        except Exception as e:
            if plugin:
                plugin.log(f"cl-hive: Failed to send STATE_HASH to {peer_id[:16]}...: {e}", level='warn')
def _parse_msat_value(value: Any) -> int:
    """
    Parse msat values from CLN notifications (int, "123msat", nested dict).
    """
    for _ in range(3):  # bounded unwrapping for nested {"msat": "..."}
        if isinstance(value, int):
            return value
        if isinstance(value, dict) and "msat" in value:
            value = value.get("msat")
            continue
        if isinstance(value, str):
            text = value.strip()
            if text.endswith("msat"):
                text = text[:-4]
            return int(text) if text.isdigit() else 0
        break
    return 0
def _handle_forward_event(forward_event: Dict):
    """Process forward event on background thread (never on IO thread)."""
    status = forward_event.get("status", "unknown")
    fee_msat = _parse_msat_value(
        forward_event.get("fee_msat", forward_event.get("fee_msatoshi", 0))
    )

    # Handle contribution tracking
    if contribution_mgr:
        try:
            contribution_mgr.handle_forward_event(forward_event)
        except Exception as e:
            if plugin:
                plugin.log(f"Forward event handling error: {e}", level="warn")


# =============================================================================
# PHASE 3: INTENT LOCK HANDLERS
# =============================================================================

def handle_intent(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle HIVE_INTENT message (remote lock request).

    When we receive an intent from another node:
    1. Record it for visibility
    2. Check for conflicts with our pending intents
    3. If conflict, apply tie-breaker (lowest pubkey wins)
    4. If we lose, abort our local intent
    """
    if not intent_mgr:
        return {"result": "continue"}

    # P3-02: Verify sender is a Hive member and not banned before processing
    if not database:
        return {"result": "continue"}
    member = database.get_member(peer_id)
    if not member:
        plugin.log(f"cl-hive: INTENT from non-member {peer_id[:16]}..., ignoring", level='warn')
        return {"result": "continue"}
    if database.is_banned(peer_id):
        plugin.log(f"cl-hive: INTENT from banned member {peer_id[:16]}..., ignoring", level='warn')
        return {"result": "continue"}

    required_fields = ["intent_type", "target", "initiator", "timestamp"]
    for field in required_fields:
        if field not in payload:
            plugin.log(f"cl-hive: INTENT from {peer_id[:16]}... missing {field}", level='warn')
            return {"result": "continue"}

    if payload.get("initiator") != peer_id:
        plugin.log(f"cl-hive: INTENT from {peer_id[:16]}... initiator mismatch", level='warn')
        return {"result": "continue"}

    # SECURITY: Verify cryptographic signature
    signature = payload.get("signature")
    if not signature:
        plugin.log(f"cl-hive: INTENT from {peer_id[:16]}... missing signature", level='warn')
        return {"result": "continue"}
    signing_payload = get_intent_signing_payload(payload)
    try:
        result = plugin.rpc.checkmessage(signing_payload, signature)
        if not result.get("verified") or result.get("pubkey") != peer_id:
            plugin.log(f"cl-hive: INTENT signature invalid from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: INTENT signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check (reject stale replayed intents)
    if not _check_timestamp_freshness(payload, MAX_INTENT_AGE_SECONDS, "INTENT"):
        return {"result": "continue"}

    if payload.get("intent_type") not in {t.value for t in IntentType}:
        plugin.log(f"cl-hive: INTENT from {peer_id[:16]}... invalid intent_type", level='warn')
        return {"result": "continue"}

    if not isinstance(payload.get("target"), str) or not payload.get("target"):
        plugin.log(f"cl-hive: INTENT from {peer_id[:16]}... invalid target", level='warn')
        return {"result": "continue"}

    # Parse the remote intent
    remote_intent = Intent.from_dict(payload)
    
    # Record for visibility
    intent_mgr.record_remote_intent(remote_intent)
    
    # Check for conflicts
    has_conflict, we_win = intent_mgr.check_conflicts(remote_intent)
    
    if has_conflict:
        if we_win:
            # We win the tie-breaker - they should abort
            plugin.log(f"cl-hive: INTENT conflict with {peer_id[:16]}..., we WIN tie-breaker")
        else:
            # We lose - abort our local intent
            plugin.log(f"cl-hive: INTENT conflict with {peer_id[:16]}..., we LOSE tie-breaker")
            intent_mgr.abort_local_intent(
                target=remote_intent.target,
                intent_type=remote_intent.intent_type
            )
            
            # Broadcast our abort
            broadcast_intent_abort(remote_intent.target, remote_intent.intent_type)
    
    return {"result": "continue"}

def handle_intent_abort(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle HIVE_INTENT_ABORT message (remote node yielding).

    Update our record to show the remote node aborted their intent.

    SECURITY: Requires cryptographic signature verification.
    Only the intent owner can abort their own intent.
    """
    if not intent_mgr:
        return {"result": "continue"}

    # SECURITY: Verify sender is a Hive member and not banned before processing
    if database:
        member = database.get_member(peer_id)
        if not member:
            plugin.log(f"cl-hive: INTENT_ABORT from non-member {peer_id[:16]}..., ignoring", level='warn')
            return {"result": "continue"}
        if database.is_banned(peer_id):
            plugin.log(f"cl-hive: INTENT_ABORT from banned member {peer_id[:16]}..., ignoring", level='warn')
            return {"result": "continue"}

    # SECURITY: Validate payload structure including signature field
    if not validate_intent_abort(payload):
        plugin.log(
            f"cl-hive: INTENT_ABORT rejected from {peer_id[:16]}...: invalid payload",
            level='warn'
        )
        return {"result": "continue"}

    intent_type = payload.get('intent_type')
    target = payload.get('target')
    initiator = payload.get('initiator')
    signature = payload.get('signature')

    # SECURITY: Verify cryptographic signature
    signing_payload = get_intent_abort_signing_payload(payload)
    try:
        result = plugin.rpc.checkmessage(signing_payload, signature)
        if not result.get("verified") or result.get("pubkey") != initiator:
            plugin.log(
                f"cl-hive: INTENT_ABORT signature invalid from {peer_id[:16]}...",
                level='warn'
            )
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: INTENT_ABORT signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # SECURITY: Verify initiator matches peer_id (only abort your own intents)
    if initiator != peer_id:
        plugin.log(
            f"cl-hive: INTENT_ABORT initiator mismatch: claimed {initiator[:16]}... but peer is {peer_id[:16]}...",
            level='warn'
        )
        return {"result": "continue"}

    intent_mgr.record_remote_abort(intent_type, target, initiator)
    plugin.log(f"cl-hive: INTENT_ABORT from {peer_id[:16]}... for {target[:16]}...")

    return {"result": "continue"}

def broadcast_intent_abort(target: str, intent_type: str) -> None:
    """
    Broadcast signed HIVE_INTENT_ABORT to all Hive members.

    Called when we lose a tie-breaker and need to yield.

    SECURITY: All INTENT_ABORT messages are cryptographically signed.
    """
    if not database or not plugin or not intent_mgr:
        return

    members = database.get_all_members()
    abort_payload = {
        'intent_type': intent_type,
        'target': target,
        'initiator': intent_mgr.our_pubkey,
        'timestamp': int(time.time()),
        'reason': 'tie_breaker_loss'
    }

    # Sign the payload
    signing_payload = get_intent_abort_signing_payload(abort_payload)
    try:
        sig_result = plugin.rpc.signmessage(signing_payload)
        abort_payload['signature'] = sig_result['zbase']
    except Exception as e:
        plugin.log(f"cl-hive: Failed to sign INTENT_ABORT: {e}", level='error')
        return

    _broadcast_member_message(
        msg_type=HiveMessageType.INTENT_ABORT,
        payload=abort_payload,
        reliability="reliable",
        failure_policy="best_effort",
        log_label="intent_abort",
    )

# =============================================================================
# PHASE 5: MEMBERSHIP PROTOCOL HELPERS
# =============================================================================

def _get_broadcast_targets() -> List[Dict[str, Any]]:
    """
    Get the list of members eligible to receive broadcasts.

    Excludes ourselves and banned peers. This is the single source of truth
    for all outbound member broadcasts — never iterate get_all_members()
    directly for sending messages.
    """
    if not database:
        return []
    return [
        m for m in database.get_all_members()
        if m.get("tier") == MEMBER_TIER
        and m["peer_id"] != our_pubkey
        and not database.is_banned(m["peer_id"])
    ]

def _normalize_member_broadcast_bytes(
    msg_type: Optional[HiveMessageType] = None,
    payload: Optional[Dict[str, Any]] = None,
    message_bytes: Optional[bytes] = None,
    relay_ttl: int = 3,
):
    """
    Normalize payload/bytes input into a relay-aware payload and serialized bytes.

    Returns:
        Tuple of (normalized_type, normalized_payload, normalized_bytes)
    """
    if (payload is None) == (message_bytes is None):
        raise ValueError("exactly one of payload or message_bytes is required")

    if payload is not None:
        if msg_type is None:
            raise ValueError("msg_type is required when payload is provided")
        normalized_type = msg_type
        normalized_payload = _prepare_broadcast_payload(dict(payload), ttl=relay_ttl)
    else:
        normalized_type, decoded_payload = deserialize(message_bytes)
        if normalized_type is None or decoded_payload is None:
            raise ValueError("message_bytes could not be deserialized")
        normalized_payload = dict(decoded_payload)
        if "_relay" not in normalized_payload:
            normalized_payload = _prepare_broadcast_payload(normalized_payload, ttl=relay_ttl)

    normalized_bytes = serialize(normalized_type, normalized_payload)
    if normalized_bytes is None:
        raise ValueError("normalized broadcast message could not be serialized")

    return normalized_type, normalized_payload, normalized_bytes

def _normalize_member_broadcast_targets(targets: Optional[List[str]] = None) -> List[str]:
    """Normalize explicit broadcast targets using the same safety filters as default broadcasts."""
    eligible_targets = [member["peer_id"] for member in _get_broadcast_targets()]
    if targets is None:
        return eligible_targets
    eligible_set = set(eligible_targets)

    normalized_targets: List[str] = []
    seen: Set[str] = set()
    for peer_id in targets:
        if not peer_id or peer_id == our_pubkey or peer_id in seen:
            continue
        if peer_id not in eligible_set:
            continue
        seen.add(peer_id)
        normalized_targets.append(peer_id)
    return normalized_targets

def _send_member_message_direct(
    target_ids: List[str],
    normalized_bytes: bytes,
    result: Dict[str, Any],
) -> Dict[str, Any]:
    """Send a member broadcast directly over sendcustommsg."""
    result["mode"] = "direct"
    if not plugin:
        result["ok"] = False
        result["failed"] = len(target_ids)
        return result

    for peer_id in target_ids:
        try:
            plugin.rpc.call("sendcustommsg", {
                "node_id": peer_id,
                "msg": normalized_bytes.hex(),
            })
            result["sent"] += 1
            shutdown_event.wait(0.02)
        except Exception as e:
            plugin.log(f"cl-hive: sendcustommsg to {peer_id[:16]}... failed: {e}", level='debug')
            result["failed"] += 1

    result["ok"] = result["failed"] == 0 if result["policy"] == "fail_closed" else True
    return result

def _broadcast_member_message(
    msg_type: Optional[HiveMessageType] = None,
    payload: Optional[Dict[str, Any]] = None,
    message_bytes: Optional[bytes] = None,
    *,
    msg_id: Optional[str] = None,
    reliability: str = "direct",
    failure_policy: str = "best_effort",
    targets: Optional[List[str]] = None,
    relay_ttl: int = 3,
    log_label: str = "member_broadcast",
) -> Dict[str, Any]:
    """
    Broadcast a message to hive members with explicit transport policy.

    Returns a result dict with:
    - ok: Whether the broadcast satisfied the requested policy
    - attempted: Target count
    - queued: Reliable enqueue count
    - sent: Direct send count
    - failed: Failures or unsatisfied targets
    - mode: direct or reliable
    - policy: best_effort or fail_closed
    """
    if reliability not in {"direct", "reliable"}:
        raise ValueError(f"unsupported reliability: {reliability}")
    if failure_policy not in {"best_effort", "fail_closed"}:
        raise ValueError(f"unsupported failure_policy: {failure_policy}")
    if failure_policy == "fail_closed" and reliability != "reliable":
        raise ValueError("fail_closed broadcasts must use reliable delivery")

    target_ids = _normalize_member_broadcast_targets(targets)

    result = {
        "ok": True,
        "attempted": len(target_ids),
        "queued": 0,
        "sent": 0,
        "failed": 0,
        "mode": reliability,
        "policy": failure_policy,
    }

    if not target_ids:
        return result

    try:
        normalized_type, normalized_payload, normalized_bytes = _normalize_member_broadcast_bytes(
            msg_type=msg_type,
            payload=payload,
            message_bytes=message_bytes,
            relay_ttl=relay_ttl,
        )
    except Exception as e:
        if plugin:
            plugin.log(f"cl-hive: {log_label} normalization failed: {e}", level="debug")
        result["ok"] = False
        result["failed"] = len(target_ids)
        return result

    if reliability == "reliable":
        if not outbox_mgr:
            if failure_policy == "best_effort":
                return _send_member_message_direct(target_ids, normalized_bytes, result)
            result["ok"] = False
            result["failed"] = len(target_ids)
            return result

        try:
            result["queued"] = outbox_mgr.enqueue(
                msg_id or generate_event_id(normalized_type.name, normalized_payload) or secrets.token_hex(16),
                normalized_type,
                normalized_payload,
                peer_ids=target_ids,
            )
        except Exception as e:
            if plugin:
                plugin.log(f"cl-hive: {log_label} outbox enqueue failed: {e}", level="debug")
            result["queued"] = 0
            if failure_policy == "best_effort":
                return _send_member_message_direct(target_ids, normalized_bytes, result)

        if result["queued"] == 0 and failure_policy == "best_effort":
            return _send_member_message_direct(target_ids, normalized_bytes, result)
        result["failed"] = max(0, len(target_ids) - result["queued"])
        result["ok"] = result["failed"] == 0 if failure_policy == "fail_closed" else True
        return result

    return _send_member_message_direct(target_ids, normalized_bytes, result)

def _broadcast_to_members(message_bytes: bytes) -> int:
    """
    Broadcast a message to all hive members (excluding ourselves and banned).

    Returns:
        Number of members the message was successfully sent to.
    """
    result = _broadcast_member_message(
        message_bytes=message_bytes,
        reliability="direct",
        failure_policy="best_effort",
        log_label="broadcast_to_members",
    )
    if plugin and result.get("failed", 0) > 0:
        plugin.log(
            f"cl-hive: broadcast_to_members incomplete: {result['sent']}/{result['attempted']} delivered",
            level="warn",
        )
    return result["sent"]

# =============================================================================
# PHASE D: RELIABLE DELIVERY HELPERS
# =============================================================================

def _outbox_send_fn(peer_id: str, msg_bytes: bytes) -> bool:
    """Send function for OutboxManager -- wraps sendcustommsg RPC."""
    if not plugin:
        return False
    try:
        plugin.rpc.call("sendcustommsg", {
            "node_id": peer_id,
            "msg": msg_bytes.hex()
        })
        return True
    except Exception:
        return False

def _outbox_get_member_ids() -> List[str]:
    """Get list of member peer_ids for OutboxManager broadcasts (excludes banned)."""
    if not database:
        return []
    return [
        m["peer_id"] for m in database.get_all_members()
        if m.get("tier") == MEMBER_TIER
        and not database.is_banned(m["peer_id"])
    ]

def _reliable_broadcast(msg_type: HiveMessageType, payload: Dict,
                         msg_id: Optional[str] = None) -> None:
    """
    Enqueue a critical message for reliable delivery to all members.

    Falls back to fire-and-forget broadcast if outbox is unavailable.
    """
    result = _broadcast_member_message(
        msg_type=msg_type,
        payload=payload,
        msg_id=msg_id,
        reliability="reliable",
        failure_policy="best_effort",
        log_label="reliable_broadcast",
    )
    if plugin and result.get("failed", 0) > 0:
        plugin.log(
            f"cl-hive: reliable_broadcast incomplete: {result['queued'] + result['sent']}/{result['attempted']} delivered",
            level="warn",
        )

def _reliable_send(msg_type: HiveMessageType, payload: Dict,
                    peer_id: str, msg_id: Optional[str] = None) -> None:
    """
    Enqueue a critical message for reliable delivery to a specific peer.

    Falls back to fire-and-forget send if outbox is unavailable.
    """
    if not msg_id:
        msg_id = generate_event_id(msg_type.name, payload) or secrets.token_hex(16)

    if outbox_mgr:
        outbox_mgr.enqueue(msg_id, msg_type, payload, peer_ids=[peer_id])
    else:
        try:
            msg_bytes = serialize(msg_type, payload)
            if msg_bytes is None:
                if plugin:
                    plugin.log(f"cl-hive: message too large, skipping send to {peer_id[:16]}", level='warning')
                return
            if plugin:
                plugin.rpc.call("sendcustommsg", {
                    "node_id": peer_id,
                    "msg": msg_bytes.hex()
                })
        except Exception as e:
            if plugin:
                plugin.log(f"cl-hive: reliable_send fallback failed for {peer_id[:16]}...: {e}", level='debug')

def _is_relayed_message(payload: Dict[str, Any]) -> bool:
    """Check if message was relayed (not direct from origin)."""
    relay_data = payload.get("_relay", {})
    relay_path = relay_data.get("relay_path", [])
    return len(relay_path) > 1

def _get_message_origin(payload: Dict[str, Any]) -> Optional[str]:
    """Get original sender of message (may differ from peer_id for relayed messages)."""
    relay_data = payload.get("_relay", {})
    return relay_data.get("origin")

def _validate_relay_sender(peer_id: str, sender_id: str, payload: Dict[str, Any]) -> bool:
    """
    Validate sender for both direct and relayed messages.

    For direct messages: sender_id must equal peer_id
    For relayed messages: sender_id must be in relay_path origin, peer_id must be a member

    Returns:
        True if sender is valid
    """
    if not database:
        return False

    if _is_relayed_message(payload):
        # Relayed message: verify peer_id is a known member (they're relaying)
        relay_peer = database.get_member(peer_id)
        if not relay_peer or relay_peer.get("tier") != MEMBER_TIER:
            return False
        # P5R3-L-1 fix: Reject relayed messages from banned relay peers
        if database.is_banned(peer_id):
            return False
        # Verify origin matches claimed sender_id
        origin = _get_message_origin(payload)
        if origin and origin != sender_id:
            return False
        # Verify original sender is also a member
        original_sender = database.get_member(sender_id)
        if not original_sender:
            return False
        # P5-H-1 fix: Reject relayed messages from banned senders
        if database.is_banned(sender_id):
            return False
        return True
    else:
        # Direct message: sender_id must match peer_id
        return sender_id == peer_id

def _relay_message(
    msg_type: HiveMessageType,
    payload: Dict[str, Any],
    sender_peer_id: str
) -> int:
    """
    Relay a received message to other hive members.

    Args:
        msg_type: The message type
        payload: The message payload (with _relay metadata if present)
        sender_peer_id: Who sent us this message

    Returns:
        Number of members relayed to
    """
    if not relay_mgr:
        return 0

    # Let relay_mgr.relay() handle should_relay + prepare_for_relay internally.
    # Do NOT call them here — double-preparation adds our_pubkey to relay_path
    # before relay() checks it, causing relay() to always return 0.
    def encode_message(p: Dict[str, Any]) -> bytes:
        return serialize(msg_type, p)

    return relay_mgr.relay(payload, sender_peer_id, encode_message)

def _prepare_broadcast_payload(payload: Dict[str, Any], ttl: int = 3) -> Dict[str, Any]:
    """
    Prepare a new message payload with relay metadata for broadcast.

    Call this when originating a new message (not relaying).
    """
    if not relay_mgr:
        return payload
    return relay_mgr.prepare_for_broadcast(payload, ttl)

def _should_process_message(payload: Dict[str, Any]) -> bool:
    """
    Check if message should be processed (deduplication check).

    Returns:
        True if this is a new message that should be processed
        False if duplicate (already seen)
    """
    if not relay_mgr:
        return True  # No relay manager, process everything
    return relay_mgr.should_process(payload)

def _check_timestamp_freshness(payload: Dict[str, Any], max_age: int,
                                label: str = "message") -> bool:
    """
    Check if a message timestamp is fresh enough to process.

    Rejects messages that are too old (replay) or too far in the future (clock skew).

    Args:
        payload: Message payload containing 'timestamp' field
        max_age: Maximum allowed age in seconds
        label: Message type label for logging

    Returns:
        True if timestamp is acceptable, False if stale/invalid
    """
    ts = payload.get("timestamp")
    if not isinstance(ts, (int, float)) or ts <= 0:
        return False
    now = int(time.time())
    age = now - int(ts)
    if age > max_age:
        if plugin:
            plugin.log(
                f"cl-hive: {label} rejected: timestamp too old ({age}s > {max_age}s)",
                level='debug'
            )
        return False
    if age < -MAX_CLOCK_SKEW_SECONDS:
        if plugin:
            plugin.log(
                f"cl-hive: {label} rejected: timestamp {-age}s in the future",
                level='debug'
            )
        return False
    return True

def _execute_member_removal(peer_id: str, reason: str = "removed") -> None:
    """
    Full member removal: DB, state manager, bridge policy, and broadcast.

    Shared by hive-remove-member, _cleanup_ghost_members, and ban execution.
    """
    # 1. Remove from database
    if not database:
        return
    database.remove_member(peer_id)

    # 2. Remove from in-memory state
    if state_manager:
        try:
            state_manager.remove_peer_state(peer_id)
        except Exception as e:
            if plugin:
                plugin.log(f"cl-hive: Failed to remove state for {peer_id[:16]}...: {e}", level='debug')

    # 4. Force the next gossip cycle to broadcast immediately so remaining
    #    members see the updated member list without the removed peer.
    if gossip_mgr:
        try:
            gossip_mgr.force_next_broadcast()
        except Exception as e:
            if plugin:
                plugin.log(f"cl-hive: Failed to force gossip broadcast after removing {peer_id[:16]}...: {e}", level='debug')

def _cleanup_ghost_members() -> int:
    """
    Remove members whose node is no longer in the gossip graph.

    The gossip graph retains node announcements for ~2 weeks, so absence
    from the graph is a strong signal the node is permanently gone.

    Returns:
        Number of members removed.
    """
    if not database or not plugin:
        return 0

    removed = 0
    try:
        all_members = database.get_all_members() or []
        for m in all_members:
            pid = m.get("peer_id")
            if not pid or pid == our_pubkey:
                continue
            try:
                nodes = plugin.rpc.listnodes(pid).get("nodes", [])
                if nodes:
                    continue  # Still in graph
            except Exception:
                continue  # RPC error — be conservative, skip

            # Node gone from gossip graph → full removal
            last_seen = m.get("last_seen") or 0
            age_days = (int(time.time()) - last_seen) // 86400 if last_seen else "?"
            _execute_member_removal(pid, reason="ghost_cleanup")
            plugin.log(
                f"cl-hive: Auto-removed ghost member {pid[:16]}... "
                f"(last_seen {age_days}d ago, not in gossip graph)",
                level='info'
            )
            removed += 1
    except Exception as e:
        if plugin:
            plugin.log(f"cl-hive: Ghost member cleanup error: {e}", level='debug')

    return removed

def _sync_membership_on_startup(plugin: Plugin) -> None:
    """
    Broadcast signed membership list to all known peers on startup.

    This ensures all nodes converge to the same membership state
    when the plugin restarts.

    SECURITY: All FULL_SYNC messages are cryptographically signed.
    """
    if not database or not gossip_mgr :
        return

    targets = _get_broadcast_targets()
    if not targets:
        return  # No eligible targets to sync

    # Create signed FULL_SYNC with membership
    full_sync_msg = _create_signed_full_sync_msg()
    if not full_sync_msg:
        plugin.log("cl-hive: Failed to create signed FULL_SYNC for startup sync", level='error')
        return

    sent_count = 0
    for member in targets:
        member_id = member["peer_id"]

        try:
            plugin.rpc.call("sendcustommsg", {
                "node_id": member_id,
                "msg": full_sync_msg.hex()
            })
            sent_count += 1
            shutdown_event.wait(0.02)  # Yield for incoming RPC
        except Exception as e:
            plugin.log(f"cl-hive: Startup sync to {member_id[:16]}...: {e}", level='debug')

    if sent_count > 0:
        plugin.log(f"cl-hive: Broadcast membership to {sent_count} peer(s) on startup")

def handle_member_left(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle MEMBER_LEFT message - a member voluntarily leaving the hive.

    Validates the signature and removes the member from the hive.
    """
    if not config or not database :
        return {"result": "continue"}

    # Deduplication check
    if not _should_process_message(payload):
        return {"result": "continue"}

    if not validate_member_left(payload):
        plugin.log(f"cl-hive: MEMBER_LEFT from {peer_id[:16]}... invalid payload", level='warn')
        return {"result": "continue"}

    leaving_peer_id = payload.get("peer_id")
    timestamp = payload.get("timestamp")
    reason = payload.get("reason")
    signature = payload.get("signature")

    if not leaving_peer_id or not timestamp or not reason or not signature:
        plugin.log(f"cl-hive: MEMBER_LEFT from {peer_id[:16]}... missing required fields", level='warn')
        return {"result": "continue"}

    # Verify sender (supports relay)
    if not _validate_relay_sender(peer_id, leaving_peer_id, payload):
        plugin.log(f"cl-hive: MEMBER_LEFT sender mismatch: {peer_id[:16]}... != {leaving_peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    # Check if member exists
    member = database.get_member(leaving_peer_id)
    if not member:
        plugin.log(f"cl-hive: MEMBER_LEFT for unknown peer {leaving_peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    if not _check_timestamp_freshness(payload, MAX_MEMBERSHIP_EVENT_AGE_SECONDS, label="MEMBER_LEFT"):
        return {"result": "continue"}

    joined_at = member.get("joined_at")
    if isinstance(joined_at, int) and timestamp < joined_at:
        plugin.log(
            f"cl-hive: MEMBER_LEFT rejected for {leaving_peer_id[:16]}... older than current membership",
            level='debug'
        )
        return {"result": "continue"}

    # Verify signature
    canonical = f"hive:leave:{leaving_peer_id}:{timestamp}:{reason}"
    try:
        result = plugin.rpc.checkmessage(canonical, signature)
        if not result.get("verified") or result.get("pubkey") != leaving_peer_id:
            plugin.log(f"cl-hive: MEMBER_LEFT signature invalid for {leaving_peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: MEMBER_LEFT signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # Phase C: Persistent idempotency check
    is_new, event_id = check_and_record(database, "MEMBER_LEFT", payload, leaving_peer_id)
    if not is_new:
        plugin.log(f"cl-hive: MEMBER_LEFT duplicate event {event_id}, skipping", level='debug')
        _relay_message(HiveMessageType.MEMBER_LEFT, payload, peer_id)
        return {"result": "continue"}
    if event_id:
        payload["_event_id"] = event_id

    joined_at_cutoff = int(member.get("joined_at") or 0)
    tombstone_id = payload.get("_event_id") or event_id or generate_event_id("MEMBER_LEFT", payload)
    if tombstone_id:
        database.record_membership_tombstone(
            event_id=tombstone_id,
            peer_id=leaving_peer_id,
            event="left",
            actor_peer_id=leaving_peer_id,
            reason=reason,
            timestamp=timestamp,
            joined_at_cutoff=joined_at_cutoff,
        )

    # Remove the member
    tier = member.get("tier")
    _execute_member_removal(leaving_peer_id, reason="left")
    plugin.log(f"cl-hive: Member {leaving_peer_id[:16]}... ({tier}) left the hive: {reason}")



    # Check if hive is now headless (no members)
    all_members = database.get_all_members()
    member_count = sum(1 for m in all_members if m.get("tier") == MEMBER_TIER)
    if member_count == 0 and len(all_members) > 0:
        plugin.log("cl-hive: WARNING - Hive has no members remaining.", level='warn')

    # Relay to other members
    _relay_message(HiveMessageType.MEMBER_LEFT, payload, peer_id)

    return {"result": "continue"}

def handle_member_removed(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle MEMBER_REMOVED message - explicit operator removal for upgraded peers.
    """
    if not database:
        return {"result": "continue"}

    target_peer_id = payload.get("peer_id")
    actor_peer_id = payload.get("actor_peer_id")
    reason = payload.get("reason", "maintenance")
    timestamp = payload.get("timestamp")
    event_id = payload.get("event_id")
    joined_at_cutoff = payload.get("joined_at_cutoff")
    signature = payload.get("signature")

    if not _is_valid_pubkey(target_peer_id) or not _is_valid_pubkey(actor_peer_id):
        return {"result": "continue"}
    if not isinstance(reason, str) or not reason:
        return {"result": "continue"}
    if not isinstance(timestamp, int) or timestamp <= 0:
        return {"result": "continue"}
    if not isinstance(event_id, str) or not event_id:
        return {"result": "continue"}
    if not isinstance(joined_at_cutoff, int) or joined_at_cutoff < 0:
        return {"result": "continue"}
    if not isinstance(signature, str) or not signature:
        return {"result": "continue"}

    if not _validate_relay_sender(peer_id, actor_peer_id, payload):
        plugin.log(
            f"cl-hive: MEMBER_REMOVED sender mismatch: {peer_id[:16]}... != {actor_peer_id[:16]}...",
            level='warn'
        )
        return {"result": "continue"}

    actor = database.get_member(actor_peer_id)
    if not actor or database.is_banned(actor_peer_id):
        plugin.log(f"cl-hive: MEMBER_REMOVED rejected from non-member {actor_peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    if not _check_timestamp_freshness(payload, MAX_MEMBERSHIP_EVENT_AGE_SECONDS, label="MEMBER_REMOVED"):
        return {"result": "continue"}

    canonical = f"hive:remove:{actor_peer_id}:{target_peer_id}:{timestamp}:{reason}"
    try:
        result = plugin.rpc.checkmessage(canonical, signature)
        if not result.get("verified") or result.get("pubkey") != actor_peer_id:
            plugin.log(f"cl-hive: MEMBER_REMOVED signature invalid for {actor_peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: MEMBER_REMOVED signature check failed: {e}", level='warn')
        return {"result": "continue"}

    database.record_membership_tombstone(
        event_id=event_id,
        peer_id=target_peer_id,
        event="removed",
        actor_peer_id=actor_peer_id,
        reason=reason,
        timestamp=timestamp,
        joined_at_cutoff=joined_at_cutoff,
    )

    member = database.get_member(target_peer_id)
    if member and int(member.get("joined_at") or 0) <= joined_at_cutoff:
        _execute_member_removal(target_peer_id, reason="removed")

    return {"result": "continue"}

# Message timestamp freshness limits (reject stale replayed messages)
MAX_GOSSIP_AGE_SECONDS = 3600           # 1 hour for gossip
MAX_INTENT_AGE_SECONDS = 600            # 10 minutes for intents (time-sensitive)
MAX_STATE_HASH_AGE_SECONDS = 3600       # 1 hour for state hash / full sync
MAX_MEMBERSHIP_EVENT_AGE_SECONDS = 30 * 86400  # 30 days for membership removals/catch-up
MAX_INTELLIGENCE_AGE_SECONDS = 7200     # 2 hours for fee/health/liquidity reports
MAX_CLOCK_SKEW_SECONDS = 300            # 5 minutes future tolerance

def _is_valid_pubkey(pubkey: Any) -> bool:
    """Check whether a value looks like a compressed secp256k1 pubkey."""
    return (
        isinstance(pubkey, str)
        and len(pubkey) == 66
        and pubkey[:2] in ("02", "03")
        and all(c in "0123456789abcdef" for c in pubkey)
    )

def handle_ban(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle BAN message - notification that a ban has been executed.

    BAN is broadcast by the banning member to notify the fleet.
    This handler is idempotent — if we've already banned the target, it's a no-op.

    The handler is intentionally lightweight - add_ban is idempotent (returns
    False if the peer is already banned).
    """
    if not database:
        return {"status": "ignored", "reason": "not_initialised"}

    target_peer_id = payload.get("peer_id")
    reason = payload.get("reason", "member_ban")
    reporter_id = payload.get("reporter", peer_id)
    timestamp = payload.get("timestamp")
    signature = payload.get("signature")
    event_id = payload.get("event_id")
    auth_fields = ("reporter", "timestamp", "signature")
    has_any_auth = any(field in payload for field in auth_fields)
    has_full_auth = all(field in payload for field in auth_fields)

    if not target_peer_id:
        plugin.log("cl-hive: BAN message missing peer_id", level='warn')
        return {"status": "ignored", "reason": "missing_peer_id"}

    if has_any_auth and not has_full_auth:
        plugin.log("cl-hive: BAN message missing auth fields", level='warn')
        return {"status": "ignored", "reason": "malformed_auth"}

    if has_full_auth:
        if not _validate_relay_sender(peer_id, reporter_id, payload):
            plugin.log(f"cl-hive: BAN sender mismatch: {peer_id[:16]}... != {reporter_id[:16]}...", level='warn')
            return {"status": "ignored", "reason": "sender_mismatch"}

        reporter = database.get_member(reporter_id)
        if not reporter or database.is_banned(reporter_id):
            plugin.log(f"cl-hive: BAN rejected from non-member {reporter_id[:16]}...", level='warn')
            return {"status": "ignored", "reason": "sender_not_member"}

        if not _check_timestamp_freshness(payload, MAX_INTENT_AGE_SECONDS, label="BAN"):
            return {"status": "ignored", "reason": "stale"}

        canonical = f"BAN:{target_peer_id}:{reason}:{timestamp}"
        try:
            result = plugin.rpc.checkmessage(canonical, signature)
            if not result.get("verified") or result.get("pubkey") != reporter_id:
                plugin.log(f"cl-hive: BAN signature invalid for {reporter_id[:16]}...", level='warn')
                return {"status": "ignored", "reason": "invalid_signature"}
        except Exception as e:
            plugin.log(f"cl-hive: BAN signature check failed: {e}", level='warn')
            return {"status": "ignored", "reason": "signature_check_failed"}
    else:
        if _is_relayed_message(payload):
            plugin.log("cl-hive: BAN ignored: unsigned legacy BAN cannot be relayed", level='debug')
            return {"status": "ignored", "reason": "legacy_relay_unsupported"}

        reporter = database.get_member(peer_id)
        if not reporter or database.is_banned(peer_id):
            plugin.log(f"cl-hive: BAN rejected from non-member {peer_id[:16]}...", level='warn')
            return {"status": "ignored", "reason": "sender_not_member"}
        reporter_id = peer_id

    # Already banned — nothing to do
    if database.is_banned(target_peer_id):
        plugin.log(f"cl-hive: BAN notification for already-banned {target_peer_id[:16]}...", level='debug')
        return {"status": "already_banned"}

    current_member = database.get_member(target_peer_id)
    joined_at_cutoff = int(current_member.get("joined_at") or 0) if current_member else 0
    tombstone_id = event_id
    if not tombstone_id:
        event_payload = {
            "peer_id": target_peer_id,
            "reporter": reporter_id,
            "timestamp": timestamp if isinstance(timestamp, int) else 0,
        }
        tombstone_id = generate_event_id("BAN", event_payload) or secrets.token_hex(16)

    # Enforce the ban
    database.add_ban(target_peer_id, reason, reporter_id, signature=signature if has_full_auth else None)
    database.record_membership_tombstone(
        event_id=tombstone_id,
        peer_id=target_peer_id,
        event="banned",
        actor_peer_id=reporter_id,
        reason=reason,
        timestamp=timestamp if isinstance(timestamp, int) and timestamp > 0 else int(time.time()),
        joined_at_cutoff=joined_at_cutoff,
    )

    # Full removal: DB, state manager, bridge policy, and forced gossip
    _execute_member_removal(target_peer_id, reason="banned")

    # Clear any intent locks held by the banned member
    if intent_mgr:
        try:
            cleared = intent_mgr.clear_intents_by_peer(target_peer_id)
            if cleared:
                plugin.log(f"cl-hive: Cleared {cleared} intent locks for banned member {target_peer_id[:16]}...")
        except Exception as e:
            plugin.log(f"cl-hive: Failed to clear intents for banned member: {e}", level='warn')

    plugin.log(f"cl-hive: BAN catch-up executed for {target_peer_id[:16]}...")

    return {"status": "banned", "peer_id": target_peer_id}

# =============================================================================
# PHASE 7: FEE INTELLIGENCE MESSAGE HANDLERS
# =============================================================================

def handle_fee_intelligence_snapshot(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle FEE_INTELLIGENCE_SNAPSHOT message from a hive member.

    This is the preferred method for receiving fee intelligence - one message
    contains observations for all peers instead of N individual messages.

    RELAY: Supports multi-hop relay for non-mesh topologies.
    """
    if not fee_intel_mgr or not database:
        return {"result": "continue"}

    # RELAY: Check deduplication before processing
    if not _should_process_message(payload):
        return {"result": "continue"}

    # Get the actual sender (may differ from peer_id for relayed messages)
    reporter_id = payload.get("reporter_id", peer_id)
    is_relayed = _is_relayed_message(payload)

    # Verify original sender is a hive member and not banned
    sender = database.get_member(reporter_id)
    if not sender or database.is_banned(reporter_id):
        plugin.log(f"cl-hive: FEE_INTELLIGENCE_SNAPSHOT from non-member {reporter_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Identity binding: for direct messages, reporter must be the sender
    if not is_relayed and reporter_id != peer_id:
        plugin.log(f"cl-hive: FEE_INTELLIGENCE_SNAPSHOT reporter mismatch from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "FEE_INTELLIGENCE_SNAPSHOT"):
        return {"result": "continue"}

    # SECURITY: Verify signature
    signature = payload.get("signature")
    if not signature:
        plugin.log(f"cl-hive: FEE_INTELLIGENCE_SNAPSHOT missing signature from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    from modules.protocol import get_fee_intelligence_snapshot_signing_payload
    signing_payload = get_fee_intelligence_snapshot_signing_payload(payload)
    try:
        verify_result = plugin.rpc.checkmessage(signing_payload, signature)
        if not verify_result.get("verified") or verify_result.get("pubkey") != reporter_id:
            plugin.log(f"cl-hive: FEE_INTELLIGENCE_SNAPSHOT invalid signature from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: FEE_INTELLIGENCE_SNAPSHOT signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # Delegate to fee intelligence manager (validate data BEFORE relaying)
    result = fee_intel_mgr.handle_fee_intelligence_snapshot(reporter_id, payload, plugin.rpc)

    if result.get("success"):
        relay_info = " (relayed)" if is_relayed else ""
        plugin.log(
            f"cl-hive: Stored fee intelligence snapshot from {reporter_id[:16]}...{relay_info} "
            f"with {result.get('peers_stored', 0)} peers",
            level='debug'
        )
        # RELAY: Forward only after successful validation/processing
        relay_count = _relay_message(HiveMessageType.FEE_INTELLIGENCE_SNAPSHOT, payload, peer_id)
        if relay_count > 0:
            plugin.log(f"cl-hive: FEE_INTELLIGENCE_SNAPSHOT relayed to {relay_count} members", level='debug')
    elif result.get("error"):
        plugin.log(
            f"cl-hive: FEE_INTELLIGENCE_SNAPSHOT rejected from {reporter_id[:16]}...: {result.get('error')}",
            level='debug'
        )

    return {"result": "continue"}

def handle_traffic_intelligence_batch(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle TRAFFIC_INTELLIGENCE_BATCH message from a hive member.

    RELAY: Supports multi-hop relay for non-mesh topologies.
    """
    if not traffic_intel_mgr or not database:
        return {"result": "continue"}

    # RELAY: Check deduplication
    if not _should_process_message(payload):
        return {"result": "continue"}

    reporter_id = payload.get("reporter_id", peer_id)
    is_relayed = _is_relayed_message(payload)

    # Verify sender is a member and not banned
    sender = database.get_member(reporter_id)
    if not sender or database.is_banned(reporter_id):
        plugin.log(f"cl-hive: TRAFFIC_INTELLIGENCE_BATCH from non-member {reporter_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Identity binding for direct messages
    if not is_relayed and reporter_id != peer_id:
        plugin.log(f"cl-hive: TRAFFIC_INTELLIGENCE_BATCH reporter mismatch", level='debug')
        return {"result": "continue"}

    # Timestamp freshness
    if not _check_timestamp_freshness(payload, 48 * 3600, "TRAFFIC_INTELLIGENCE_BATCH"):
        return {"result": "continue"}

    # Signature verification
    signature = payload.get("signature")
    if not signature:
        plugin.log(f"cl-hive: TRAFFIC_INTELLIGENCE_BATCH missing signature", level='warn')
        return {"result": "continue"}

    from modules.protocol import get_traffic_intelligence_batch_signing_payload
    signing_payload = get_traffic_intelligence_batch_signing_payload(payload)
    try:
        verify_result = plugin.rpc.checkmessage(signing_payload, signature)
        if not verify_result.get("verified") or verify_result.get("pubkey") != reporter_id:
            plugin.log(f"cl-hive: TRAFFIC_INTELLIGENCE_BATCH invalid signature", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: TRAFFIC_INTELLIGENCE_BATCH signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # Delegate to manager
    result = traffic_intel_mgr.handle_traffic_intelligence_batch(reporter_id, payload, plugin.rpc)

    if result.get("success"):
        relay_info = " (relayed)" if is_relayed else ""
        plugin.log(
            f"cl-hive: Stored traffic intelligence from {reporter_id[:16]}...{relay_info} "
            f"with {result.get('profiles_stored', 0)} profiles",
            level='debug'
        )
        from modules.protocol import HiveMessageType
        relay_count = _relay_message(HiveMessageType.TRAFFIC_INTELLIGENCE_BATCH, payload, peer_id)
        if relay_count > 0:
            plugin.log(f"cl-hive: TRAFFIC_INTELLIGENCE_BATCH relayed to {relay_count} members", level='debug')

    return {"result": "continue"}

def handle_health_report(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle HEALTH_REPORT message from a hive member.

    Used for NNLB (No Node Left Behind) coordination.

    RELAY: Supports multi-hop relay for non-mesh topologies.
    """
    if not fee_intel_mgr or not database:
        return {"result": "continue"}

    # RELAY: Check deduplication before processing
    if not _should_process_message(payload):
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "HEALTH_REPORT"):
        return {"result": "continue"}

    # Get the actual sender (may differ from peer_id for relayed messages)
    reporter_id = payload.get("reporter_id", peer_id)
    is_relayed = _is_relayed_message(payload)

    # Verify original sender is a hive member and not banned
    sender = database.get_member(reporter_id)
    if not sender or database.is_banned(reporter_id):
        plugin.log(f"cl-hive: HEALTH_REPORT from non-member {reporter_id[:16]}...", level='debug')
        return {"result": "continue"}

    # SECURITY: Verify signature
    signature = payload.get("signature")
    if not signature:
        plugin.log(f"cl-hive: HEALTH_REPORT missing signature from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    from modules.protocol import get_health_report_signing_payload
    signing_payload = get_health_report_signing_payload(payload)
    try:
        verify_result = plugin.rpc.checkmessage(signing_payload, signature)
        if not verify_result.get("verified") or verify_result.get("pubkey") != reporter_id:
            plugin.log(f"cl-hive: HEALTH_REPORT invalid signature from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: HEALTH_REPORT signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # RELAY: Forward to other members
    relay_count = _relay_message(HiveMessageType.HEALTH_REPORT, payload, peer_id)
    if relay_count > 0:
        plugin.log(f"cl-hive: HEALTH_REPORT relayed to {relay_count} members", level='debug')

    # Delegate to fee intelligence manager
    result = fee_intel_mgr.handle_health_report(reporter_id, payload, plugin.rpc)

    if result.get("success"):
        tier = result.get("tier", "unknown")
        relay_info = " (relayed)" if is_relayed else ""
        plugin.log(
            f"cl-hive: Stored health report from {reporter_id[:16]}...{relay_info} (tier={tier})",
            level='debug'
        )
    elif result.get("error"):
        plugin.log(
            f"cl-hive: HEALTH_REPORT rejected from {reporter_id[:16]}...: {result.get('error')}",
            level='debug'
        )

    return {"result": "continue"}

def handle_liquidity_need(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle LIQUIDITY_NEED message from a hive member.

    Used for cooperative rebalancing coordination.

    RELAY: Supports multi-hop relay for non-mesh topologies.
    """
    if not liquidity_coord or not database:
        return {"result": "continue"}

    # RELAY: Check deduplication before processing
    if not _should_process_message(payload):
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "LIQUIDITY_NEED"):
        return {"result": "continue"}

    # Get the actual sender (may differ from peer_id for relayed messages)
    reporter_id = payload.get("reporter_id", peer_id)
    is_relayed = _is_relayed_message(payload)

    # Verify original sender is a hive member and not banned
    sender = database.get_member(reporter_id)
    if not sender or database.is_banned(reporter_id):
        plugin.log(f"cl-hive: LIQUIDITY_NEED from non-member {reporter_id[:16]}...", level='debug')
        return {"result": "continue"}

    # SECURITY: Verify signature
    signature = payload.get("signature")
    if not signature:
        plugin.log(f"cl-hive: LIQUIDITY_NEED missing signature from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    from modules.protocol import get_liquidity_need_signing_payload
    signing_payload = get_liquidity_need_signing_payload(payload)
    try:
        verify_result = plugin.rpc.checkmessage(signing_payload, signature)
        if not verify_result.get("verified") or verify_result.get("pubkey") != reporter_id:
            plugin.log(f"cl-hive: LIQUIDITY_NEED invalid signature from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: LIQUIDITY_NEED signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # RELAY: Forward to other members
    relay_count = _relay_message(HiveMessageType.LIQUIDITY_NEED, payload, peer_id)
    if relay_count > 0:
        plugin.log(f"cl-hive: LIQUIDITY_NEED relayed to {relay_count} members", level='debug')

    # Delegate to liquidity coordinator
    result = liquidity_coord.handle_liquidity_need(reporter_id, payload, plugin.rpc)

    if result.get("success"):
        relay_info = " (relayed)" if is_relayed else ""
        plugin.log(
            f"cl-hive: Stored liquidity need from {reporter_id[:16]}...{relay_info}",
            level='debug'
        )
    elif result.get("error"):
        plugin.log(
            f"cl-hive: LIQUIDITY_NEED rejected from {reporter_id[:16]}...: {result.get('error')}",
            level='debug'
        )

    return {"result": "continue"}

def handle_liquidity_snapshot(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle LIQUIDITY_SNAPSHOT message from a hive member.

    This is the preferred method for receiving liquidity needs - one message
    contains multiple needs instead of N individual messages.

    RELAY: Supports multi-hop relay for non-mesh topologies.
    """
    if not liquidity_coord or not database:
        return {"result": "continue"}

    # RELAY: Check deduplication before processing
    if not _should_process_message(payload):
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "LIQUIDITY_SNAPSHOT"):
        return {"result": "continue"}

    # Get the actual sender (may differ from peer_id for relayed messages)
    reporter_id = payload.get("reporter_id", peer_id)
    is_relayed = _is_relayed_message(payload)

    # Verify original sender is a hive member and not banned
    sender = database.get_member(reporter_id)
    if not sender or database.is_banned(reporter_id):
        plugin.log(f"cl-hive: LIQUIDITY_SNAPSHOT from non-member {reporter_id[:16]}...", level='debug')
        return {"result": "continue"}

    # SECURITY: Verify signature
    signature = payload.get("signature")
    if not signature:
        plugin.log(f"cl-hive: LIQUIDITY_SNAPSHOT missing signature from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    from modules.protocol import get_liquidity_snapshot_signing_payload
    signing_payload = get_liquidity_snapshot_signing_payload(payload)
    try:
        verify_result = plugin.rpc.checkmessage(signing_payload, signature)
        if not verify_result.get("verified") or verify_result.get("pubkey") != reporter_id:
            plugin.log(f"cl-hive: LIQUIDITY_SNAPSHOT invalid signature from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: LIQUIDITY_SNAPSHOT signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # RELAY: Forward to other members
    relay_count = _relay_message(HiveMessageType.LIQUIDITY_SNAPSHOT, payload, peer_id)
    if relay_count > 0:
        plugin.log(f"cl-hive: LIQUIDITY_SNAPSHOT relayed to {relay_count} members", level='debug')

    # Delegate to liquidity coordinator
    result = liquidity_coord.handle_liquidity_snapshot(reporter_id, payload, plugin.rpc)

    if result.get("success"):
        relay_info = " (relayed)" if is_relayed else ""
        plugin.log(
            f"cl-hive: Stored liquidity snapshot from {reporter_id[:16]}...{relay_info} "
            f"with {result.get('needs_stored', 0)} needs",
            level='debug'
        )
    elif result.get("error"):
        plugin.log(
            f"cl-hive: LIQUIDITY_SNAPSHOT rejected from {reporter_id[:16]}...: {result.get('error')}",
            level='debug'
        )

    return {"result": "continue"}

def handle_peer_reputation_snapshot(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle PEER_REPUTATION_SNAPSHOT message from a hive member.

    This is the preferred method for receiving peer reputation - one message
    contains observations for all peers instead of N individual messages.
    """
    if not peer_reputation_mgr or not database:
        return {"result": "continue"}

    # Deduplication check
    if not _should_process_message(payload):
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "PEER_REPUTATION_SNAPSHOT"):
        return {"result": "continue"}

    # Verify sender is a hive member and not banned (supports relay)
    is_relayed = _is_relayed_message(payload)
    if is_relayed:
        relay_member = database.get_member(peer_id)
        if not relay_member or relay_member.get("tier") != MEMBER_TIER or database.is_banned(peer_id):
            return {"result": "continue"}
    else:
        sender = database.get_member(peer_id)
        if not sender or database.is_banned(peer_id):
            plugin.log(f"cl-hive: PEER_REPUTATION_SNAPSHOT from non-member {peer_id[:16]}...", level='debug')
            return {"result": "continue"}

    # SECURITY: Verify signature
    reporter_id = payload.get("reporter_id", peer_id)
    signature = payload.get("signature")
    if not signature:
        plugin.log(f"cl-hive: PEER_REPUTATION_SNAPSHOT missing signature from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    from modules.protocol import get_peer_reputation_snapshot_signing_payload
    signing_payload = get_peer_reputation_snapshot_signing_payload(payload)
    try:
        verify_result = plugin.rpc.checkmessage(signing_payload, signature)
        if not verify_result.get("verified") or verify_result.get("pubkey") != reporter_id:
            plugin.log(f"cl-hive: PEER_REPUTATION_SNAPSHOT invalid signature from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: PEER_REPUTATION_SNAPSHOT signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # Delegate to peer reputation manager
    result = peer_reputation_mgr.handle_peer_reputation_snapshot(peer_id, payload, plugin.rpc)

    if result.get("success"):
        relay_info = " (relayed)" if is_relayed else ""
        plugin.log(
            f"cl-hive: Stored peer reputation snapshot from {peer_id[:16]}...{relay_info} "
            f"with {result.get('peers_stored', 0)} peers",
            level='debug'
        )
    elif result.get("error"):
        plugin.log(
            f"cl-hive: PEER_REPUTATION_SNAPSHOT rejected from {peer_id[:16]}...: {result.get('error')}",
            level='debug'
        )

    # Relay to other members
    _relay_message(HiveMessageType.PEER_REPUTATION_SNAPSHOT, payload, peer_id)

    return {"result": "continue"}

def handle_yield_metrics_batch(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle YIELD_METRICS_BATCH message from a hive member.

    This enables fleet-wide learning about channel profitability.
    When a member shares their yield metrics, other members can
    avoid opening channels to peers known to be unprofitable.
    """
    if not yield_metrics_mgr or not database:
        return {"result": "continue"}

    # Deduplication check
    if not _should_process_message(payload):
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "YIELD_METRICS_BATCH"):
        return {"result": "continue"}

    # Verify sender is a hive member and not banned (supports relay)
    is_relayed = _is_relayed_message(payload)
    if is_relayed:
        relay_member = database.get_member(peer_id)
        if not relay_member or relay_member.get("tier") != MEMBER_TIER or database.is_banned(peer_id):
            return {"result": "continue"}
    else:
        sender = database.get_member(peer_id)
        if not sender or database.is_banned(peer_id):
            plugin.log(f"cl-hive: YIELD_METRICS_BATCH from non-member {peer_id[:16]}...", level='debug')
            return {"result": "continue"}

    # Validate payload
    from modules.protocol import validate_yield_metrics_batch, get_yield_metrics_batch_signing_payload
    if not validate_yield_metrics_batch(payload):
        plugin.log(f"cl-hive: YIELD_METRICS_BATCH validation failed from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify signature - reporter_id may differ from peer_id when relayed
    reporter_id = payload.get("reporter_id", "")
    if not is_relayed and reporter_id != peer_id:
        plugin.log(f"cl-hive: YIELD_METRICS_BATCH reporter mismatch from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify reporter is a member
    reporter = database.get_member(reporter_id)
    if not reporter or database.is_banned(reporter_id):
        plugin.log(f"cl-hive: YIELD_METRICS_BATCH from non-member reporter {reporter_id[:16]}...", level='debug')
        return {"result": "continue"}

    try:
        signing_payload = get_yield_metrics_batch_signing_payload(payload)
        verify_result = plugin.rpc.checkmessage(signing_payload, payload.get("signature", ""))
        if not verify_result.get("verified"):
            plugin.log(f"cl-hive: YIELD_METRICS_BATCH signature invalid from {peer_id[:16]}...", level='debug')
            return {"result": "continue"}
        if verify_result.get("pubkey") != reporter_id:
            plugin.log(f"cl-hive: YIELD_METRICS_BATCH pubkey mismatch from {peer_id[:16]}...", level='debug')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: YIELD_METRICS_BATCH signature check error: {e}", level='debug')
        return {"result": "continue"}

    # Process each yield metric entry
    metrics = payload.get("metrics", [])
    metrics_stored = 0

    for metric_data in metrics:
        try:
            result = yield_metrics_mgr.receive_yield_metrics_from_fleet(
                reporter_id=reporter_id,
                metrics_data=metric_data
            )
            if result:
                metrics_stored += 1
        except Exception as e:
            plugin.log(f"cl-hive: Error processing yield metric: {e}", level='debug')
            continue

    if metrics_stored > 0:
        relay_info = " (relayed)" if is_relayed else ""
        plugin.log(
            f"cl-hive: Stored {metrics_stored} yield metrics from {reporter_id[:16]}...{relay_info}",
            level='debug'
        )

    # Relay to other members
    _relay_message(HiveMessageType.YIELD_METRICS_BATCH, payload, peer_id)

    return {"result": "continue"}

# ============================================================================
# Phase 14.2: Strategic Positioning & Rationalization Handlers
# ============================================================================

def handle_corridor_value_batch(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle CORRIDOR_VALUE_BATCH message from a hive member.

    This enables fleet-wide sharing of high-value routing corridor discoveries
    for coordinated strategic positioning.
    """
    if not strategic_positioning_mgr or not database:
        return {"result": "continue"}

    # Deduplication check
    if not _should_process_message(payload):
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "CORRIDOR_VALUE_BATCH"):
        return {"result": "continue"}

    # Verify sender is a hive member and not banned (supports relay)
    is_relayed = _is_relayed_message(payload)
    if is_relayed:
        relay_member = database.get_member(peer_id)
        if not relay_member or relay_member.get("tier") != MEMBER_TIER or database.is_banned(peer_id):
            return {"result": "continue"}
    else:
        sender = database.get_member(peer_id)
        if not sender or database.is_banned(peer_id):
            plugin.log(f"cl-hive: CORRIDOR_VALUE_BATCH from non-member {peer_id[:16]}...", level='debug')
            return {"result": "continue"}

    # Validate payload
    from modules.protocol import validate_corridor_value_batch, get_corridor_value_batch_signing_payload
    if not validate_corridor_value_batch(payload):
        plugin.log(f"cl-hive: CORRIDOR_VALUE_BATCH validation failed from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify signature - reporter_id may differ from peer_id when relayed
    reporter_id = payload.get("reporter_id", "")
    if not is_relayed and reporter_id != peer_id:
        plugin.log(f"cl-hive: CORRIDOR_VALUE_BATCH reporter mismatch from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify reporter is a member
    reporter = database.get_member(reporter_id)
    if not reporter or database.is_banned(reporter_id):
        plugin.log(f"cl-hive: CORRIDOR_VALUE_BATCH from non-member reporter {reporter_id[:16]}...", level='debug')
        return {"result": "continue"}

    try:
        signing_payload = get_corridor_value_batch_signing_payload(payload)
        verify_result = plugin.rpc.checkmessage(signing_payload, payload.get("signature", ""))
        if not verify_result.get("verified"):
            plugin.log(f"cl-hive: CORRIDOR_VALUE_BATCH signature invalid from {peer_id[:16]}...", level='debug')
            return {"result": "continue"}
        if verify_result.get("pubkey") != reporter_id:
            plugin.log(f"cl-hive: CORRIDOR_VALUE_BATCH pubkey mismatch from {peer_id[:16]}...", level='debug')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: CORRIDOR_VALUE_BATCH signature check error: {e}", level='debug')
        return {"result": "continue"}

    # Process each corridor entry
    corridors = payload.get("corridors", [])
    corridors_stored = 0

    for corridor_data in corridors:
        try:
            result = strategic_positioning_mgr.receive_corridor_from_fleet(
                reporter_id=reporter_id,
                corridor_data=corridor_data
            )
            if result:
                corridors_stored += 1
        except Exception as e:
            plugin.log(f"cl-hive: Error processing corridor value: {e}", level='debug')
            continue

    if corridors_stored > 0:
        relay_info = " (relayed)" if is_relayed else ""
        plugin.log(
            f"cl-hive: Stored {corridors_stored} corridor values from {reporter_id[:16]}...{relay_info}",
            level='debug'
        )

    # Relay to other members
    _relay_message(HiveMessageType.CORRIDOR_VALUE_BATCH, payload, peer_id)

    return {"result": "continue"}

def handle_positioning_proposal(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle POSITIONING_PROPOSAL message from a hive member.

    This enables fleet-wide coordination of strategic channel open recommendations.
    """
    if not strategic_positioning_mgr or not database:
        return {"result": "continue"}

    # Deduplication check
    if not _should_process_message(payload):
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "POSITIONING_PROPOSAL"):
        return {"result": "continue"}

    # Verify sender is a hive member and not banned (supports relay)
    is_relayed = _is_relayed_message(payload)
    if is_relayed:
        relay_member = database.get_member(peer_id)
        if not relay_member or relay_member.get("tier") != MEMBER_TIER or database.is_banned(peer_id):
            return {"result": "continue"}
    else:
        sender = database.get_member(peer_id)
        if not sender or database.is_banned(peer_id):
            plugin.log(f"cl-hive: POSITIONING_PROPOSAL from non-member {peer_id[:16]}...", level='debug')
            return {"result": "continue"}

    # Validate payload
    from modules.protocol import validate_positioning_proposal, get_positioning_proposal_signing_payload
    if not validate_positioning_proposal(payload):
        plugin.log(f"cl-hive: POSITIONING_PROPOSAL validation failed from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify signature - reporter_id may differ from peer_id when relayed
    reporter_id = payload.get("reporter_id", "")
    if not is_relayed and reporter_id != peer_id:
        plugin.log(f"cl-hive: POSITIONING_PROPOSAL reporter mismatch from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify reporter is a member
    reporter = database.get_member(reporter_id)
    if not reporter or database.is_banned(reporter_id):
        plugin.log(f"cl-hive: POSITIONING_PROPOSAL from non-member reporter {reporter_id[:16]}...", level='debug')
        return {"result": "continue"}

    try:
        signing_payload = get_positioning_proposal_signing_payload(payload)
        verify_result = plugin.rpc.checkmessage(signing_payload, payload.get("signature", ""))
        if not verify_result.get("verified"):
            plugin.log(f"cl-hive: POSITIONING_PROPOSAL signature invalid from {peer_id[:16]}...", level='debug')
            return {"result": "continue"}
        if verify_result.get("pubkey") != reporter_id:
            plugin.log(f"cl-hive: POSITIONING_PROPOSAL pubkey mismatch from {peer_id[:16]}...", level='debug')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: POSITIONING_PROPOSAL signature check error: {e}", level='debug')
        return {"result": "continue"}

    # Store the positioning proposal
    try:
        result = strategic_positioning_mgr.receive_positioning_proposal_from_fleet(
            reporter_id=reporter_id,
            proposal_data=payload
        )
        if result:
            target = payload.get("target_pubkey", "")[:16]
            relay_info = " (relayed)" if is_relayed else ""
            plugin.log(
                f"cl-hive: Stored positioning proposal from {reporter_id[:16]}...{relay_info} targeting {target}...",
                level='debug'
            )
    except Exception as e:
        plugin.log(f"cl-hive: Error storing positioning proposal: {e}", level='debug')

    # Relay to other members
    _relay_message(HiveMessageType.POSITIONING_PROPOSAL, payload, peer_id)

    return {"result": "continue"}

def handle_coverage_analysis_batch(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle COVERAGE_ANALYSIS_BATCH message from a hive member.

    This enables fleet-wide sharing of peer coverage analysis for
    rationalization decisions (identifying redundant channels).
    """
    if not rationalization_mgr or not database:
        return {"result": "continue"}

    # Deduplication check
    if not _should_process_message(payload):
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "COVERAGE_ANALYSIS_BATCH"):
        return {"result": "continue"}

    # Verify sender is a hive member and not banned (supports relay)
    is_relayed = _is_relayed_message(payload)
    if is_relayed:
        relay_member = database.get_member(peer_id)
        if not relay_member or relay_member.get("tier") != MEMBER_TIER or database.is_banned(peer_id):
            return {"result": "continue"}
    else:
        sender = database.get_member(peer_id)
        if not sender or database.is_banned(peer_id):
            plugin.log(f"cl-hive: COVERAGE_ANALYSIS_BATCH from non-member {peer_id[:16]}...", level='debug')
            return {"result": "continue"}

    # Validate payload
    from modules.protocol import validate_coverage_analysis_batch, get_coverage_analysis_batch_signing_payload
    if not validate_coverage_analysis_batch(payload):
        plugin.log(f"cl-hive: COVERAGE_ANALYSIS_BATCH validation failed from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify signature - reporter_id may differ from peer_id when relayed
    reporter_id = payload.get("reporter_id", "")
    if not is_relayed and reporter_id != peer_id:
        plugin.log(f"cl-hive: COVERAGE_ANALYSIS_BATCH reporter mismatch from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify reporter is a member
    reporter = database.get_member(reporter_id)
    if not reporter or database.is_banned(reporter_id):
        plugin.log(f"cl-hive: COVERAGE_ANALYSIS_BATCH from non-member reporter {reporter_id[:16]}...", level='debug')
        return {"result": "continue"}

    try:
        signing_payload = get_coverage_analysis_batch_signing_payload(payload)
        verify_result = plugin.rpc.checkmessage(signing_payload, payload.get("signature", ""))
        if not verify_result.get("verified"):
            plugin.log(f"cl-hive: COVERAGE_ANALYSIS_BATCH signature invalid from {peer_id[:16]}...", level='debug')
            return {"result": "continue"}
        if verify_result.get("pubkey") != reporter_id:
            plugin.log(f"cl-hive: COVERAGE_ANALYSIS_BATCH pubkey mismatch from {peer_id[:16]}...", level='debug')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: COVERAGE_ANALYSIS_BATCH signature check error: {e}", level='debug')
        return {"result": "continue"}

    # Process each coverage entry
    coverage_entries = payload.get("coverage_entries", [])
    entries_stored = 0

    for coverage_data in coverage_entries:
        try:
            result = rationalization_mgr.receive_coverage_from_fleet(
                reporter_id=reporter_id,
                coverage_data=coverage_data
            )
            if result:
                entries_stored += 1
        except Exception as e:
            plugin.log(f"cl-hive: Error processing coverage entry: {e}", level='debug')
            continue

    if entries_stored > 0:
        relay_info = " (relayed)" if is_relayed else ""
        plugin.log(
            f"cl-hive: Stored {entries_stored} coverage entries from {reporter_id[:16]}...{relay_info}",
            level='debug'
        )

    # Relay to other members
    _relay_message(HiveMessageType.COVERAGE_ANALYSIS_BATCH, payload, peer_id)

    return {"result": "continue"}

def handle_close_proposal(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle CLOSE_PROPOSAL message from a hive member.

    This enables fleet-wide coordination of channel close recommendations
    for redundancy elimination and capital efficiency.
    """
    if not rationalization_mgr or not database:
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "CLOSE_PROPOSAL"):
        return {"result": "continue"}

    # Verify sender is a hive member and not banned
    sender = database.get_member(peer_id)
    if not sender or database.is_banned(peer_id):
        plugin.log(f"cl-hive: CLOSE_PROPOSAL from non-member {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Validate payload
    from modules.protocol import validate_close_proposal, get_close_proposal_signing_payload
    if not validate_close_proposal(payload):
        plugin.log(f"cl-hive: CLOSE_PROPOSAL validation failed from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify signature
    reporter_id = payload.get("reporter_id", "")
    if reporter_id != peer_id:
        plugin.log(f"cl-hive: CLOSE_PROPOSAL reporter mismatch from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    try:
        signing_payload = get_close_proposal_signing_payload(payload)
        verify_result = plugin.rpc.checkmessage(signing_payload, payload.get("signature", ""))
        if not verify_result.get("verified"):
            plugin.log(f"cl-hive: CLOSE_PROPOSAL signature invalid from {peer_id[:16]}...", level='debug')
            return {"result": "continue"}
        if verify_result.get("pubkey") != reporter_id:
            plugin.log(f"cl-hive: CLOSE_PROPOSAL pubkey mismatch from {peer_id[:16]}...", level='debug')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: CLOSE_PROPOSAL signature check error: {e}", level='debug')
        return {"result": "continue"}

    # Store the close proposal
    try:
        result = rationalization_mgr.receive_close_proposal_from_fleet(
            reporter_id=peer_id,
            proposal_data=payload
        )
        if result:
            target_member = payload.get("target_member", "")[:16]
            target_peer = payload.get("target_peer", "")[:16]
            plugin.log(
                f"cl-hive: Stored close proposal from {peer_id[:16]}... "
                f"for {target_member}... channel to {target_peer}...",
                level='debug'
            )
    except Exception as e:
        plugin.log(f"cl-hive: Error storing close proposal: {e}", level='debug')

    return {"result": "continue"}
