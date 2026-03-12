"""
protocol_handlers - Protocol message handler functions for cl-hive.

This module contains all handle_* functions and their helpers that process
incoming Hive protocol messages dispatched by _dispatch_hive_message().

Dependencies are injected at startup via init_protocol_handlers() to avoid
rewriting every function body during the extraction from the cl-hive.py
monolith.
"""

import json
import hashlib
import secrets
import threading
import time
import traceback
from typing import Dict, Optional, Any, List, Set

from pyln.client import Plugin, RpcError

from modules.protocol import (
    HIVE_MAGIC, HiveMessageType,
    MAX_MESSAGE_BYTES, is_hive_message, deserialize, serialize,
    validate_promotion_request, validate_vouch, validate_promotion,
    validate_member_left, validate_ban_proposal, validate_ban_vote,
    validate_peer_available, create_peer_available,
    validate_expansion_nominate, validate_expansion_elect, validate_expansion_decline,
    create_expansion_nominate, create_expansion_elect, create_expansion_decline,
    get_expansion_nominate_signing_payload, get_expansion_elect_signing_payload,
    get_expansion_decline_signing_payload,
    VOUCH_TTL_SECONDS, MAX_VOUCHES_IN_PROMOTION,
    create_challenge, create_welcome,
    validate_gossip, validate_state_hash, validate_full_sync, validate_intent_abort,
    get_gossip_signing_payload, get_state_hash_signing_payload,
    get_full_sync_signing_payload, get_intent_signing_payload, get_intent_abort_signing_payload,
    get_peer_available_signing_payload, compute_states_hash,
    create_settlement_offer, get_settlement_offer_signing_payload,
    validate_mcf_needs_batch, validate_mcf_solution_broadcast,
    validate_mcf_assignment_ack, validate_mcf_completion_report,
    get_mcf_needs_batch_signing_payload, get_mcf_solution_signing_payload,
    get_mcf_assignment_ack_signing_payload, get_mcf_completion_signing_payload,
    create_mcf_needs_batch,
    create_msg_ack, validate_msg_ack,
    IMPLICIT_ACK_MAP, IMPLICIT_ACK_MATCH_FIELD,
    RELIABLE_MESSAGE_TYPES,
)
from modules.handshake import CHALLENGE_TTL_SECONDS
from modules.state_manager import StateManager
from modules.gossip import GossipManager
from modules.intent_manager import Intent, IntentType
from modules.bridge import BridgeStatus
from modules.membership import MembershipTier
from modules.quality_scorer import PeerQualityScorer
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
membership_mgr = None
contribution_mgr = None
bridge = None
vpn_transport = None
relay_mgr = None
coop_expansion = None
fee_intel_mgr = None
health_aggregator = None
liquidity_coord = None
routing_map = None
peer_reputation_mgr = None
routing_pool = None
settlement_mgr = None
yield_metrics_mgr = None
fee_coordination_mgr = None
cost_reduction_mgr = None
rationalization_mgr = None
strategic_positioning_mgr = None
anticipatory_liquidity_mgr = None
task_mgr = None
splice_mgr = None
outbox_mgr = None
did_credential_mgr = None
management_schema_registry = None
cashu_escrow_mgr = None
traffic_intel_mgr = None
peer_available_limiter = None
outbox = None

# Constants and locks (will be overwritten by init if they exist in main)
_local_fees_lock = threading.Lock()
_local_fees_earned_sats = 0
_local_fees_forward_count = 0
_local_fees_period_start = 0
_local_fees_last_broadcast = 0
_local_fees_last_broadcast_amount = 0
_local_rebalance_costs_sats = 0
FEE_BROADCAST_MIN_SATS = 10
FEE_BROADCAST_MIN_INTERVAL = 30
PHASE4B_RATE_LIMITS = {}
_phase4b_rate_lock = threading.Lock()
_phase4b_rate_windows = {}
_phase4b_netting_lock = threading.Lock()
_phase4b_netting_proposals = {}
_credential_relay_lock = threading.Lock()


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
    Handle HIVE_HELLO message (autodiscovery join request).

    A node is requesting to join the hive. Channel existence serves as
    proof of stake - no ticket required.

    Flow:
    1. Check if we're a hive member (only members can accept new nodes)
    2. Check if peer has a channel with us (proof of stake)
    3. Check if peer is already a member
    4. Send CHALLENGE if all conditions met
    """
    sender_pubkey = payload.get('pubkey')
    if not sender_pubkey:
        plugin.log(f"cl-hive: HELLO from {peer_id[:16]}... missing pubkey", level='warn')
        return {"result": "continue"}

    # Verify pubkey matches peer_id (identity binding)
    if sender_pubkey != peer_id:
        plugin.log(f"cl-hive: HELLO from {peer_id[:16]}... pubkey mismatch", level='warn')
        return {"result": "continue"}

    # Check if we're a member (only members can accept new nodes)
    our_pubkey = handshake_mgr.get_our_pubkey()
    our_member = database.get_member(our_pubkey)
    if not our_member or our_member.get('tier') != 'member':
        plugin.log(f"cl-hive: HELLO from {peer_id[:16]}... but we're not a member", level='debug')
        return {"result": "continue"}

    # SECURITY: Check if peer is banned (prevents ban evasion via rejoin)
    if database.is_banned(peer_id):
        plugin.log(f"cl-hive: HELLO from banned peer {peer_id[:16]}..., ignoring", level='warn')
        return {"result": "continue"}

    # Check if peer is already a member
    existing_member = database.get_member(peer_id)
    if existing_member:
        plugin.log(f"cl-hive: HELLO from {peer_id[:16]}... already a {existing_member.get('tier')}", level='debug')
        return {"result": "continue"}

    # Check if peer has a channel with us (proof of stake)
    try:
        channels = plugin.rpc.call("listpeerchannels", {"id": peer_id})
        peer_channels = channels.get('channels', [])
        # Look for any active channel
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

    # All checks passed - generate challenge
    # No requirements for autodiscovery join, tier is always neophyte
    nonce = handshake_mgr.generate_challenge(peer_id, requirements=0, initial_tier='neophyte')

    # Get Hive ID from metadata
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

    # Send CHALLENGE response
    challenge_msg = create_challenge(nonce, hive_id)

    try:
        plugin.rpc.call("sendcustommsg", {
            "node_id": peer_id,
            "msg": challenge_msg.hex()
        })
        plugin.log(f"cl-hive: Sent CHALLENGE to {peer_id[:16]}... (autodiscovery join)")
    except Exception as e:
        plugin.log(f"cl-hive: Failed to send CHALLENGE: {e}", level='warn')

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

    # Get initial tier from pending challenge (always neophyte for autodiscovery)
    initial_tier = pending.get('initial_tier', 'neophyte')

    # Verification passed! Add member as neophyte
    database.add_member(
        peer_id=peer_id,
        tier=initial_tier,
        joined_at=int(time.time())
    )

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
        except Exception:
            pass  # Non-critical, will be captured on next gossip or connect

    # Initialize presence tracking so uptime_pct starts accumulating (Issue #59)
    # The peer is connected (they just completed the handshake), so mark online
    database.update_presence(peer_id, is_online=True, now_ts=int(time.time()), window_seconds=30 * 86400)

    handshake_mgr.clear_challenge(peer_id)

    # Set hive fee policy for new member (0 fee to all hive members)
    if bridge and bridge.status == BridgeStatus.ENABLED:
        bridge.set_hive_policy(peer_id, is_member=True)

    # Get Hive info for WELCOME
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

    # Sign and send WELCOME with actual tier
    welcome_signing_fields = json.dumps({
        "hive_id": hive_id,
        "member_count": len(members),
        "state_hash": state_hash,
        "tier": initial_tier,
    }, sort_keys=True, separators=(',', ':'))
    welcome_sig = ""
    try:
        welcome_sig = plugin.rpc.signmessage(welcome_signing_fields).get("zbase", "")
    except Exception as e:
        plugin.log(f"cl-hive: Failed to sign WELCOME: {e}", level='warn')
    welcome_msg = create_welcome(hive_id, initial_tier, len(members), state_hash, signature=welcome_sig)

    try:
        plugin.rpc.call("sendcustommsg", {
            "node_id": peer_id,
            "msg": welcome_msg.hex()
        })
        plugin.log(f"cl-hive: Sent WELCOME to {peer_id[:16]}... (new {initial_tier})")
    except Exception as e:
        plugin.log(f"cl-hive: Failed to send WELCOME: {e}", level='warn')

    # Send our settlement offer to the new member so they have it for settlement calculations
    if settlement_mgr and handshake_mgr:
        our_pubkey = handshake_mgr.get_our_pubkey()
        our_offer = settlement_mgr.get_offer(our_pubkey)
        if our_offer:
            _send_settlement_offer_to_peer(peer_id, our_pubkey, our_offer)

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

    plugin.log(
        f"cl-hive: WELCOME received! Joined '{hive_id}' as {tier} "
        f"(Hive has {member_count} members)"
    )

    # Phase 4: Apply Hive fee policy to this peer
    if bridge and bridge.status == BridgeStatus.ENABLED:
        bridge.set_hive_policy(peer_id, is_member=True)

    # Store Hive membership info for ourselves
    if database and our_pubkey:
        now = int(time.time())
        # Always start as neophyte regardless of what the remote peer claims —
        # our tier should be determined by local governance, not trusted from
        # an untrusted remote payload.
        database.add_member(our_pubkey, tier='neophyte', joined_at=now)
        # Store hive_id in metadata
        database.update_member(our_pubkey, metadata=json.dumps({"hive_id": hive_id}))
        plugin.log(f"cl-hive: Stored membership (tier=neophyte, hive_id={hive_id})")

        # Add the peer that welcomed us as neophyte — their actual tier
        # will be resolved via state sync rather than trusted from WELCOME.
        database.add_member(peer_id, tier='neophyte', joined_at=now)

        # Auto-generate and register BOLT12 offer for settlement
        if settlement_mgr:
            offer_result = settlement_mgr.generate_and_register_offer(our_pubkey)
            if "error" in offer_result:
                plugin.log(f"cl-hive: Failed to auto-register settlement offer: {offer_result['error']}", level='warn')
            else:
                plugin.log(f"cl-hive: Settlement offer auto-registered: {offer_result.get('status')}")
                # Broadcast to hive members
                bolt12_offer = settlement_mgr.get_offer(our_pubkey)
                if bolt12_offer:
                    broadcast_count = _broadcast_settlement_offer(our_pubkey, bolt12_offer)
                    plugin.log(f"cl-hive: Broadcast settlement offer to {broadcast_count} member(s)")

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

    # SECURITY: Fast-reject ex-members before signature verification to avoid
    # graph-dependent checkmessage failures after a peer has left the hive.
    if database:
        member = database.get_member(sender_id)
        if not member:
            plugin.log(f"cl-hive: GOSSIP from non-member {sender_id[:16]}..., ignoring", level='debug')
            return {"result": "continue"}

    # SECURITY: Verify cryptographic signature
    signature = payload.get("signature")
    signing_payload = get_gossip_signing_payload(payload)

    try:
        result = plugin.rpc.checkmessage(signing_payload, signature, sender_id)
        if not result.get("verified") or result.get("pubkey") != sender_id:
            plugin.log(
                f"cl-hive: GOSSIP signature invalid from {peer_id[:16]}...",
                level='warn'
            )
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: GOSSIP signature check failed: {e}", level='warn')
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

    # SECURITY: Verify cryptographic signature
    sender_id = payload.get("sender_id")
    signature = payload.get("signature")
    signing_payload = get_state_hash_signing_payload(payload)

    try:
        result = plugin.rpc.checkmessage(signing_payload, signature)
        if not result.get("verified") or result.get("pubkey") != sender_id:
            plugin.log(
                f"cl-hive: STATE_HASH signature invalid from {peer_id[:16]}...",
                level='warn'
            )
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: STATE_HASH signature check failed: {e}", level='warn')
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

    # SECURITY: Verify cryptographic signature
    sender_id = payload.get("sender_id")
    signature = payload.get("signature")
    signing_payload = get_full_sync_signing_payload(payload)

    try:
        result = plugin.rpc.checkmessage(signing_payload, signature)
        if not result.get("verified") or result.get("pubkey") != sender_id:
            plugin.log(
                f"cl-hive: FULL_SYNC signature invalid from {peer_id[:16]}...",
                level='warn'
            )
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: FULL_SYNC signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # SECURITY: Verify sender identity matches peer_id (prevent relay attacks)
    if sender_id != peer_id:
        plugin.log(
            f"cl-hive: FULL_SYNC sender mismatch: claimed {sender_id[:16]}... but peer is {peer_id[:16]}...",
            level='warn'
        )
        return {"result": "continue"}

    # SECURITY: Verify states match the signed fleet_hash (prevent state injection)
    states = payload.get("states", [])
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
        members_synced = _apply_membership_sync(payload["members"], peer_id, plugin)

    plugin.log(f"cl-hive: FULL_SYNC from {peer_id[:16]}...: {updated} states, {members_synced} members synced")

    return {"result": "continue"}


def _apply_membership_sync(members_list: list, sender_id: str, plugin: Plugin) -> int:
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

    added = 0
    updated = 0
    for member_info in members_list:
        if not isinstance(member_info, dict):
            continue

        member_peer_id = member_info.get("peer_id")
        if not member_peer_id or not isinstance(member_peer_id, str):
            continue

        tier = member_info.get("tier", "neophyte")
        joined_at = member_info.get("joined_at", int(time.time()))
        addresses = member_info.get("addresses", [])

        # Validate tier value (2-tier system: member or neophyte)
        if tier not in ("member", "neophyte"):
            tier = "neophyte"

        # Check if we already know this member
        existing = database.get_member(member_peer_id)
        if existing:
            # Update tier if remote has higher privilege (neophyte -> member)
            # Never demote via sync (member -> neophyte requires proper protocol)
            existing_tier = existing.get("tier", "neophyte")
            needs_update = False

            if existing_tier == "neophyte" and tier == "member":
                # Tier upgrades via sync are no longer accepted.
                # Promotions must go through the vouch/quorum protocol
                # to prevent a single compromised member from unilateral promotion.
                plugin.log(
                    f"cl-hive: Ignoring tier upgrade for {member_peer_id[:16]}... from sync "
                    f"(requires vouch/quorum protocol)",
                    level='debug'
                )

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

    return added + updated


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
            "tier": m.get("tier", "neophyte"),
            "joined_at": m.get("joined_at", 0)
        }
        # Include addresses if available (Issue #38)
        addresses_json = m.get("addresses")
        if addresses_json:
            try:
                import json
                member_dict["addresses"] = json.loads(addresses_json)
            except (json.JSONDecodeError, TypeError):
                pass
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

    # Add sender identification
    full_sync_payload["sender_id"] = our_pubkey
    full_sync_payload["timestamp"] = int(time.time())

    # Sign the payload
    signing_payload = get_full_sync_signing_payload(full_sync_payload)
    try:
        sig_result = plugin.rpc.signmessage(signing_payload)
        full_sync_payload["signature"] = sig_result["zbase"]
    except Exception as e:
        plugin.log(f"cl-hive: Failed to sign FULL_SYNC: {e}", level='error')
        return None

    return serialize(HiveMessageType.FULL_SYNC, full_sync_payload)


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

    # Sign the payload
    signing_payload = get_state_hash_signing_payload(state_hash_payload)
    try:
        sig_result = plugin.rpc.signmessage(signing_payload)
        state_hash_payload["signature"] = sig_result["zbase"]
    except Exception as e:
        plugin.log(f"cl-hive: Failed to sign STATE_HASH: {e}", level='error')
        return None

    return serialize(HiveMessageType.STATE_HASH, state_hash_payload)


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
                               addresses: List[str] = None,
                               boltz_activity: Dict = None) -> Optional[bytes]:
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
        boltz_activity: Boltz swap activity summary for fleet coordination

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
        boltz_activity=boltz_activity
    )

    # Add sender identification for signature verification
    gossip_payload["sender_id"] = our_pubkey

    # Sign the payload (includes data hash for integrity)
    signing_payload = get_gossip_signing_payload(gossip_payload)
    try:
        sig_result = plugin.rpc.signmessage(signing_payload)
        gossip_payload["signature"] = sig_result["zbase"]
    except Exception as e:
        plugin.log(f"cl-hive: Failed to sign GOSSIP: {e}", level='error')
        return None

    return serialize(HiveMessageType.GOSSIP, gossip_payload)


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
                    peer_address = netaddr[0]
                    if vpn_transport:
                        vpn_transport.on_peer_connected(peer_id, peer_address)
                    if not member.get('addresses'):
                        database.update_member(peer_id, addresses=json.dumps(netaddr))
        except Exception:
            pass

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

    # Generate route probe data from successful forwards (Phase 7.4)
    if routing_map and database and our_pubkey:
        try:
            if status == "settled":
                _record_forward_as_route_probe(forward_event)
        except Exception as e:
            if plugin:
                plugin.log(f"Route probe from forward error: {e}", level="debug")

    # Record routing revenue to pool (Phase 0 - Collective Economics)
    if routing_pool and our_pubkey:
        try:
            if status == "settled":
                fee_msat = _parse_msat_value(
                    forward_event.get("fee_msat", forward_event.get("fee_msatoshi", 0))
                )
                fee_sats = fee_msat // 1000
                if fee_msat > 0 and fee_sats > 0:
                    routing_pool.record_revenue(
                        member_id=our_pubkey,
                        amount_sats=fee_sats,
                        channel_id=forward_event.get("out_channel"),
                        payment_hash=forward_event.get("payment_hash")
                    )
                    # Broadcast fee report to hive (real-time settlement)
                    _update_and_broadcast_fees(fee_sats)
        except Exception as e:
            if plugin:
                plugin.log(f"Pool revenue recording error: {e}", level="debug")

    # Update fee coordination systems (pheromones + stigmergic markers)
    if fee_coordination_mgr and our_pubkey:
        try:
            _record_forward_for_fee_coordination(forward_event, status)
        except Exception as e:
            if plugin:
                plugin.log(f"Fee coordination recording error: {e}", level="debug")


def _update_and_broadcast_fees(new_fee_sats: int):
    """
    Update local fee tracking and broadcast to hive if threshold met.

    Called on each settled forward to maintain real-time fee gossip
    for accurate settlement calculations.

    Args:
        new_fee_sats: Fees earned from this forward
    """
    global _local_fees_earned_sats, _local_fees_forward_count
    global _local_fees_period_start, _local_fees_last_broadcast
    global _local_fees_last_broadcast_amount, _local_rebalance_costs_sats

    if not our_pubkey or not database :
        return

    now = int(time.time())

    with _local_fees_lock:
        # Initialize period start if needed (weekly periods aligned to Monday 00:00 UTC)
        if _local_fees_period_start == 0:
            # Calculate start of current week
            from datetime import datetime, timezone
            dt = datetime.fromtimestamp(now, tz=timezone.utc)
            # Monday = 0, so days_since_monday = weekday
            days_since_monday = dt.weekday()
            week_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
            week_start = week_start.timestamp() - (days_since_monday * 86400)
            _local_fees_period_start = int(week_start)

        # Update local tracking
        _local_fees_earned_sats += new_fee_sats
        _local_fees_forward_count += 1

        # Check if we should broadcast - cumulative change since last broadcast
        cumulative_fee_change = _local_fees_earned_sats - _local_fees_last_broadcast_amount
        time_since_broadcast = now - _local_fees_last_broadcast

        should_broadcast = (
            cumulative_fee_change >= FEE_BROADCAST_MIN_SATS and
            time_since_broadcast >= FEE_BROADCAST_MIN_INTERVAL
        )

        # Always snapshot fee report values for DB persistence (outside lock).
        fees_to_persist = _local_fees_earned_sats
        forwards_to_persist = _local_fees_forward_count
        period_start_to_persist = _local_fees_period_start
        costs_to_persist = _local_rebalance_costs_sats
        
        if not should_broadcast:
            no_broadcast_reason = (
                f"FEE_GOSSIP: Not broadcasting - cumulative={cumulative_fee_change}sats "
                f"(need {FEE_BROADCAST_MIN_SATS}), time={time_since_broadcast}s "
                f"(need {FEE_BROADCAST_MIN_INTERVAL})"
            )
            should_return_without_broadcast = True
        else:
            no_broadcast_reason = None
            should_return_without_broadcast = False

        # Capture values for broadcast
        fees_to_broadcast = _local_fees_earned_sats
        forwards_to_broadcast = _local_fees_forward_count
        period_start = _local_fees_period_start
        costs_to_broadcast = _local_rebalance_costs_sats
        # Only update broadcast tracking when we actually broadcast —
        # otherwise small fees never accumulate to the threshold.
        if not should_return_without_broadcast:
            _local_fees_last_broadcast = now
            _local_fees_last_broadcast_amount = _local_fees_earned_sats

    # Always save fee report to database for settlement (Bug fix #3).
    # This must happen regardless of broadcast threshold to ensure low-traffic
    # nodes report their fees for settlement calculations.
    from modules.settlement import SettlementManager
    period = SettlementManager.get_period_string(period_start_to_persist)
    database.save_fee_report(
        peer_id=our_pubkey,
        period=period,
        fees_earned_sats=fees_to_persist,
        forward_count=forwards_to_persist,
        period_start=period_start_to_persist,
        period_end=now,
        rebalance_costs_sats=costs_to_persist
    )

    if should_return_without_broadcast:
        if plugin:
            plugin.log(no_broadcast_reason, level="debug")
        # Save updated totals for persistence across restarts (outside lock to
        # avoid re-entering _local_fees_lock).
        _save_fee_tracking_state()
        return

    # Broadcast outside the lock
    if plugin:
        plugin.log(
            f"FEE_GOSSIP: Broadcasting fee report - {fees_to_broadcast} sats, "
            f"costs={costs_to_broadcast}, {forwards_to_broadcast} forwards",
            level="info"
        )
    _broadcast_fee_report(fees_to_broadcast, forwards_to_broadcast, period_start, now,
                          costs_to_broadcast)

    # Save state after broadcast (captures last_broadcast values updated in the lock)
    _save_fee_tracking_state()


def _broadcast_fee_report(fees_earned: int, forward_count: int,
                          period_start: int, period_end: int,
                          rebalance_costs: int = 0):
    """
    Broadcast a FEE_REPORT message to all hive members.

    Args:
        fees_earned: Cumulative fees earned in period
        forward_count: Number of forwards in period
        period_start: Period start timestamp
        period_end: Current timestamp
        rebalance_costs: Rebalancing costs in period (for net profit settlement)
    """
    from modules.protocol import (
        create_fee_report, get_fee_report_signing_payload, HiveMessageType
    )

    if not our_pubkey or not database :
        return

    try:
        # Sign the fee report (with costs for net profit settlement)
        signing_payload = get_fee_report_signing_payload(
            our_pubkey, fees_earned, period_start, period_end, forward_count,
            rebalance_costs
        )
        sig_result = plugin.rpc.signmessage(signing_payload)
        signature = sig_result["zbase"]

        # Create the message
        fee_report_msg = create_fee_report(
            peer_id=our_pubkey,
            fees_earned_sats=fees_earned,
            period_start=period_start,
            period_end=period_end,
            forward_count=forward_count,
            signature=signature,
            rebalance_costs_sats=rebalance_costs
        )

        result = _broadcast_member_message(
            message_bytes=fee_report_msg,
            reliability="reliable",
            failure_policy="best_effort",
            log_label="fee_report",
        )
        broadcast_count = result["queued"] or result["sent"]

        if broadcast_count > 0:
            plugin.log(
                f"[FeeReport] Broadcast: {fees_earned} sats, costs={rebalance_costs}, "
                f"{forward_count} forwards -> {broadcast_count} member(s)",
                level="info"
            )
        else:
            plugin.log(
                f"[FeeReport] No members to broadcast to (eligible={result['attempted']})",
                level="warn"
            )

        # Also update our own state in state_manager
        if state_manager:
            state_manager.update_peer_fees(
                peer_id=our_pubkey,
                fees_earned_sats=fees_earned,
                forward_count=forward_count,
                period_start=period_start,
                period_end=period_end,
                rebalance_costs_sats=rebalance_costs
            )

        # Persist our own fee report to database for settlement
        from modules.settlement import SettlementManager
        period = SettlementManager.get_period_string(period_start)
        database.save_fee_report(
            peer_id=our_pubkey,
            period=period,
            fees_earned_sats=fees_earned,
            forward_count=forward_count,
            period_start=period_start,
            period_end=period_end,
            rebalance_costs_sats=rebalance_costs
        )

    except Exception as e:
        if plugin:
            plugin.log(f"cl-hive: Fee report broadcast error: {e}", level="warn")


# Cached channel_scid -> peer_id mapping for _record_forward_as_route_probe
_channel_peer_cache: Dict[str, str] = {}
_channel_peer_cache_time: float = 0
_channel_peer_cache_lock = threading.Lock()
_CHANNEL_PEER_CACHE_TTL = 300  # Refresh every 5 minutes


def _record_forward_as_route_probe(forward_event: Dict):
    """
    Record a settled forward as route probe data.

    Stores the forwarding segment (in_peer -> out_peer) locally.
    Does not include our_pubkey in the path to avoid self-referential entries.
    """
    global _channel_peer_cache, _channel_peer_cache_time

    if not routing_map or not database :
        return

    try:
        in_channel = forward_event.get("in_channel", "")
        out_channel = forward_event.get("out_channel", "")
        fee_msat = forward_event.get("fee_msat", 0)
        out_msat = forward_event.get("out_msat", 0)

        if not in_channel or not out_channel:
            return

        # Use cached channel -> peer_id mapping (refreshed every 5 min)
        # H-1 FIX: Fetch RPC data outside lock to prevent starvation/deadlock
        now = time.time()
        needs_refresh = False
        with _channel_peer_cache_lock:
            if not _channel_peer_cache or now - _channel_peer_cache_time > _CHANNEL_PEER_CACHE_TTL:
                needs_refresh = True

        if needs_refresh:
            funds = plugin.rpc.listfunds()
            new_cache = {
                ch.get("short_channel_id"): ch.get("peer_id", "")
                for ch in funds.get("channels", [])
                if ch.get("short_channel_id")
            }
            with _channel_peer_cache_lock:
                _channel_peer_cache = new_cache
                _channel_peer_cache_time = time.time()

        with _channel_peer_cache_lock:
            in_peer = _channel_peer_cache.get(in_channel, "")
            out_peer = _channel_peer_cache.get(out_channel, "")

        if not in_peer or not out_peer:
            return

        # Record as a successful path segment: in_peer -> out_peer
        # Path contains only intermediate hops (in_peer), not reporter or destination
        database.store_route_probe(
            reporter_id=our_pubkey,
            destination=out_peer,
            path=[in_peer],  # Intermediate hops only (not reporter, not destination)
            success=True,
            latency_ms=0,
            failure_reason="",
            failure_hop=-1,
            estimated_capacity_sats=out_msat // 1000 if out_msat else 0,
            total_fee_ppm=int((fee_msat * 1_000_000) / out_msat) if out_msat else 0,
            amount_probed_sats=out_msat // 1000 if out_msat else 0,
            timestamp=int(time.time())
        )
    except Exception:
        pass  # Silently ignore errors in route probe recording


def _record_forward_for_fee_coordination(forward_event: Dict, status: str):
    """
    Record a forward event for fee coordination (pheromones + stigmergic markers).

    This feeds the swarm intelligence systems with real routing data:
    - Pheromone levels: Memory of successful fee levels
    - Stigmergic markers: Signals for fleet-wide coordination
    """
    if not fee_coordination_mgr :
        return

    try:
        in_channel = forward_event.get("in_channel", "")
        out_channel = forward_event.get("out_channel", "")
        fee_msat = _parse_msat_value(
            forward_event.get("fee_msat", forward_event.get("fee_msatoshi", 0))
        )
        out_msat = _parse_msat_value(
            forward_event.get("out_msat", forward_event.get("out_msatoshi", 0))
        )

        if not out_channel:
            return

        # Get peer IDs using cached channel-to-peer mapping (avoid RPC per forward)
        peer_map = fee_coordination_mgr.adaptive_controller._channel_peer_map
        in_peer = peer_map.get(in_channel, "") if in_channel else ""
        out_peer = peer_map.get(out_channel, "")

        # Fall back to RPC on cache miss for outbound channel
        if not out_peer:
            try:
                funds = plugin.rpc.listfunds()
                channels_map = {ch.get("short_channel_id"): ch for ch in funds.get("channels", [])}
                in_peer = channels_map.get(in_channel, {}).get("peer_id", "") if in_channel else ""
                out_peer = channels_map.get(out_channel, {}).get("peer_id", "")
                # Update cache with discovered mappings
                for scid, ch in channels_map.items():
                    if scid and ch.get("peer_id"):
                        peer_map[scid] = ch["peer_id"]
            except Exception:
                return

        if not out_peer:
            return

        # Calculate fee in ppm
        fee_ppm = int((fee_msat * 1_000_000) / out_msat) if out_msat > 0 else 0
        fee_sats = fee_msat // 1000
        volume_sats = out_msat // 1000 if out_msat else 0

        # Determine success based on status
        success = status == "settled"

        # Record to fee coordination manager
        fee_coordination_mgr.record_routing_outcome(
            channel_id=out_channel,
            peer_id=out_peer,
            fee_ppm=fee_ppm,
            success=success,
            revenue_sats=fee_sats if success else 0,
            volume_sats=volume_sats if success else 0,
            source=in_peer if in_peer else None,
            destination=out_peer
        )

        if success and plugin:
            plugin.log(
                f"cl-hive: Recorded forward for fee coordination: "
                f"{out_channel} fee={fee_ppm}ppm revenue={fee_sats}sats",
                level="debug"
            )
    except Exception as e:
        if plugin:
            plugin.log(f"cl-hive: Fee coordination record error: {e}", level="debug")


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
# PHASE 5: PROMOTION PROTOCOL HANDLERS
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
        if m.get("tier") in (MembershipTier.MEMBER.value, MembershipTier.NEOPHYTE.value)
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
        except Exception:
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
        if m.get("tier") in (MembershipTier.MEMBER.value, MembershipTier.NEOPHYTE.value)
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
        except Exception:
            pass


def _emit_ack(peer_id: str, msg_id: Optional[str]) -> None:
    """
    Send MSG_ACK to peer for a successfully processed message.

    Best-effort: we don't retry acks.
    """
    if not msg_id or not plugin or not our_pubkey:
        return
    try:
        ack_msg = create_msg_ack(msg_id, "ok", our_pubkey, rpc=plugin.rpc)
        plugin.rpc.call("sendcustommsg", {
            "node_id": peer_id,
            "msg": ack_msg.hex()
        })
    except Exception:
        pass  # Best-effort ack


def handle_msg_ack(peer_id: str, payload: Dict, plugin) -> Dict:
    """Handle incoming MSG_ACK from a peer."""
    if not validate_msg_ack(payload):
        plugin.log(f"cl-hive: MSG_ACK invalid payload from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # SECURITY: Always require signature on MSG_ACK to prevent forged delivery confirmations
    sender_id = payload.get("sender_id", "")
    signature = payload.get("signature")
    if not signature:
        plugin.log(f"cl-hive: MSG_ACK rejected (unsigned) from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}
    from modules.protocol import get_msg_ack_signing_payload
    signing_payload = get_msg_ack_signing_payload(payload)
    try:
        verify_result = plugin.rpc.checkmessage(signing_payload, signature)
        if not verify_result.get("verified") or verify_result.get("pubkey") != sender_id:
            plugin.log(f"cl-hive: MSG_ACK invalid signature from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: MSG_ACK signature check failed: {e}", level='debug')
        return {"result": "continue"}

    ack_msg_id = payload.get("ack_msg_id")
    status = payload.get("status", "ok")

    # Use verified sender_id (not transport peer_id) to match outbox entries,
    # since outbox keys on the target peer_id we originally sent to.
    if outbox_mgr:
        outbox_mgr.process_ack(sender_id, ack_msg_id, status)

    return {"result": "continue"}


# =============================================================================
# PHASE 16: DID CREDENTIAL HANDLERS
# =============================================================================

def handle_did_credential_present(peer_id: str, payload: Dict, plugin) -> Dict:
    """Handle incoming DID_CREDENTIAL_PRESENT from a peer."""
    from modules.protocol import validate_did_credential_present

    if not validate_did_credential_present(payload):
        plugin.log(f"cl-hive: DID_CREDENTIAL_PRESENT invalid payload from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # P3-H-1 fix: For relayed messages, use origin for identity binding
    sender_id = payload.get("sender_id", "")
    if _is_relayed_message(payload):
        # NEW-1 fix: Verify relay peer is a known member
        if database and not database.get_member(peer_id):
            return {"result": "continue"}
        # Ban check on relay peer
        if database and database.is_banned(peer_id):
            return {"result": "continue"}
        # R5-M-5 fix: Rate limit on relay peer to prevent quota exhaustion attacks
        if not _check_relay_credential_rate(peer_id):
            plugin.log(f"cl-hive: DID_CREDENTIAL_PRESENT relay rate-limited for {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
        origin = _get_message_origin(payload)
        effective_sender = origin if origin else peer_id
        if sender_id != effective_sender:
            plugin.log(f"cl-hive: DID_CREDENTIAL_PRESENT identity mismatch (relayed) from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    else:
        if sender_id != peer_id:
            plugin.log(f"cl-hive: DID_CREDENTIAL_PRESENT identity mismatch from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}

    # Ban check against the actual sender
    actual_sender = sender_id
    if database and database.is_banned(actual_sender):
        plugin.log(f"cl-hive: DID_CREDENTIAL_PRESENT from banned peer {actual_sender[:16]}...", level='warn')
        return {"result": "continue"}

    # R5-M-4 fix: Membership check BEFORE proto_events to avoid consuming dedup rows for non-members
    if database:
        member = database.get_member(actual_sender)
        if not member:
            plugin.log(f"cl-hive: DID_CREDENTIAL_PRESENT from non-member {actual_sender[:16]}...", level='debug')
            return {"result": "continue"}

    # Timestamp freshness
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "DID_CREDENTIAL_PRESENT"):
        return {"result": "continue"}

    # P3-M-4 fix: In-memory relay dedup for credential messages
    if not _credential_relay_dedup(payload, "DID_CREDENTIAL_PRESENT"):
        return {"result": "continue"}

    # Dedup via proto_events
    _eid = None
    if database:
        is_new, _eid = check_and_record(database, "DID_CREDENTIAL_PRESENT", payload, actual_sender)
        if not is_new:
            # P3-M-3 fix: Still relay even if already processed
            _relay_message(HiveMessageType.DID_CREDENTIAL_PRESENT, payload, peer_id)
            # R5-L-6 fix: Emit ack on dedup branch so sender outbox entries are cleared
            _emit_ack(peer_id, payload.get("event_id") or _eid)
            return {"result": "continue"}  # Already processed

    # Process credential
    if did_credential_mgr:
        did_credential_mgr.handle_credential_present(actual_sender, payload)

    # P3-H-2 fix: Emit ack after successful processing
    _emit_ack(peer_id, payload.get("event_id") or _eid)

    # P3-M-3 fix: Relay to other members
    _relay_message(HiveMessageType.DID_CREDENTIAL_PRESENT, payload, peer_id)

    return {"result": "continue"}


def handle_did_credential_revoke(peer_id: str, payload: Dict, plugin) -> Dict:
    """Handle incoming DID_CREDENTIAL_REVOKE from a peer."""
    from modules.protocol import validate_did_credential_revoke

    if not validate_did_credential_revoke(payload):
        plugin.log(f"cl-hive: DID_CREDENTIAL_REVOKE invalid payload from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # P3-H-1 fix: For relayed messages, use origin for identity binding
    sender_id = payload.get("sender_id", "")
    if _is_relayed_message(payload):
        # NEW-1 fix: Verify relay peer is a known member
        if database and not database.get_member(peer_id):
            return {"result": "continue"}
        # Ban check on relay peer
        if database and database.is_banned(peer_id):
            return {"result": "continue"}
        # R5-M-5 fix: Rate limit on relay peer to prevent quota exhaustion attacks
        if not _check_relay_credential_rate(peer_id):
            plugin.log(f"cl-hive: DID_CREDENTIAL_REVOKE relay rate-limited for {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
        origin = _get_message_origin(payload)
        effective_sender = origin if origin else peer_id
        if sender_id != effective_sender:
            plugin.log(f"cl-hive: DID_CREDENTIAL_REVOKE identity mismatch (relayed) from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    else:
        if sender_id != peer_id:
            plugin.log(f"cl-hive: DID_CREDENTIAL_REVOKE identity mismatch from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}

    # Ban check against the actual sender
    actual_sender = sender_id
    if database and database.is_banned(actual_sender):
        plugin.log(f"cl-hive: DID_CREDENTIAL_REVOKE from banned peer {actual_sender[:16]}...", level='warn')
        return {"result": "continue"}

    # R5-M-4 fix: Membership check BEFORE proto_events to avoid consuming dedup rows for non-members
    if database:
        member = database.get_member(actual_sender)
        if not member:
            plugin.log(f"cl-hive: DID_CREDENTIAL_REVOKE from non-member {actual_sender[:16]}...", level='debug')
            return {"result": "continue"}

    # Timestamp freshness
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "DID_CREDENTIAL_REVOKE"):
        return {"result": "continue"}

    # P3-M-4 fix: In-memory relay dedup for credential messages
    if not _credential_relay_dedup(payload, "DID_CREDENTIAL_REVOKE"):
        return {"result": "continue"}

    # Dedup
    _eid = None
    if database:
        is_new, _eid = check_and_record(database, "DID_CREDENTIAL_REVOKE", payload, actual_sender)
        if not is_new:
            # P3-M-3 fix: Still relay even if already processed
            _relay_message(HiveMessageType.DID_CREDENTIAL_REVOKE, payload, peer_id)
            # R5-L-6 fix: Emit ack on dedup branch so sender outbox entries are cleared
            _emit_ack(peer_id, payload.get("event_id") or _eid)
            return {"result": "continue"}

    # Process revocation
    if did_credential_mgr:
        did_credential_mgr.handle_credential_revoke(actual_sender, payload)

    # P3-H-2 fix: Emit ack after successful processing
    _emit_ack(peer_id, payload.get("event_id") or _eid)

    # P3-M-3 fix: Relay to other members
    _relay_message(HiveMessageType.DID_CREDENTIAL_REVOKE, payload, peer_id)

    return {"result": "continue"}


def handle_mgmt_credential_present(peer_id: str, payload: Dict, plugin) -> Dict:
    """Handle incoming MGMT_CREDENTIAL_PRESENT from a peer."""
    from modules.protocol import validate_mgmt_credential_present

    if not validate_mgmt_credential_present(payload):
        plugin.log(f"cl-hive: MGMT_CREDENTIAL_PRESENT invalid payload from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # P3-H-1 fix: For relayed messages, use origin for identity binding
    sender_id = payload.get("sender_id", "")
    if _is_relayed_message(payload):
        # NEW-1 fix: Verify relay peer is a known member
        if database and not database.get_member(peer_id):
            return {"result": "continue"}
        # Ban check on relay peer
        if database and database.is_banned(peer_id):
            return {"result": "continue"}
        # R5-M-5 fix: Rate limit on relay peer to prevent quota exhaustion attacks
        if not _check_relay_credential_rate(peer_id):
            plugin.log(f"cl-hive: MGMT_CREDENTIAL_PRESENT relay rate-limited for {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
        origin = _get_message_origin(payload)
        effective_sender = origin if origin else peer_id
        if sender_id != effective_sender:
            plugin.log(f"cl-hive: MGMT_CREDENTIAL_PRESENT identity mismatch (relayed) from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    else:
        if sender_id != peer_id:
            plugin.log(f"cl-hive: MGMT_CREDENTIAL_PRESENT identity mismatch from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}

    # Ban check against the actual sender
    actual_sender = sender_id
    if database and database.is_banned(actual_sender):
        plugin.log(f"cl-hive: MGMT_CREDENTIAL_PRESENT from banned peer {actual_sender[:16]}...", level='warn')
        return {"result": "continue"}

    # R5-M-4 fix: Membership check BEFORE proto_events to avoid consuming dedup rows for non-members
    if database:
        member = database.get_member(actual_sender)
        if not member:
            plugin.log(f"cl-hive: MGMT_CREDENTIAL_PRESENT from non-member {actual_sender[:16]}...", level='debug')
            return {"result": "continue"}

    # Timestamp freshness
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "MGMT_CREDENTIAL_PRESENT"):
        return {"result": "continue"}

    # P3-M-4 fix: In-memory relay dedup for credential messages
    if not _credential_relay_dedup(payload, "MGMT_CREDENTIAL_PRESENT"):
        return {"result": "continue"}

    # Dedup via proto_events
    _eid = None
    if database:
        is_new, _eid = check_and_record(database, "MGMT_CREDENTIAL_PRESENT", payload, actual_sender)
        if not is_new:
            # P3-M-3 fix: Still relay even if already processed
            _relay_message(HiveMessageType.MGMT_CREDENTIAL_PRESENT, payload, peer_id)
            # R5-L-6 fix: Emit ack on dedup branch so sender outbox entries are cleared
            _emit_ack(peer_id, payload.get("event_id") or _eid)
            return {"result": "continue"}

    # Process credential
    if management_schema_registry:
        management_schema_registry.handle_mgmt_credential_present(actual_sender, payload)

    # P3-H-2 fix: Emit ack after successful processing
    _emit_ack(peer_id, payload.get("event_id") or _eid)

    # P3-M-3 fix: Relay to other members
    _relay_message(HiveMessageType.MGMT_CREDENTIAL_PRESENT, payload, peer_id)

    return {"result": "continue"}


def handle_mgmt_credential_revoke(peer_id: str, payload: Dict, plugin) -> Dict:
    """Handle incoming MGMT_CREDENTIAL_REVOKE from a peer."""
    from modules.protocol import validate_mgmt_credential_revoke

    if not validate_mgmt_credential_revoke(payload):
        plugin.log(f"cl-hive: MGMT_CREDENTIAL_REVOKE invalid payload from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # P3-H-1 fix: For relayed messages, use origin for identity binding
    sender_id = payload.get("sender_id", "")
    if _is_relayed_message(payload):
        # NEW-1 fix: Verify relay peer is a known member
        if database and not database.get_member(peer_id):
            return {"result": "continue"}
        # Ban check on relay peer
        if database and database.is_banned(peer_id):
            return {"result": "continue"}
        # R5-M-5 fix: Rate limit on relay peer to prevent quota exhaustion attacks
        if not _check_relay_credential_rate(peer_id):
            plugin.log(f"cl-hive: MGMT_CREDENTIAL_REVOKE relay rate-limited for {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
        origin = _get_message_origin(payload)
        effective_sender = origin if origin else peer_id
        if sender_id != effective_sender:
            plugin.log(f"cl-hive: MGMT_CREDENTIAL_REVOKE identity mismatch (relayed) from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    else:
        if sender_id != peer_id:
            plugin.log(f"cl-hive: MGMT_CREDENTIAL_REVOKE identity mismatch from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}

    # Ban check against the actual sender
    actual_sender = sender_id
    if database and database.is_banned(actual_sender):
        plugin.log(f"cl-hive: MGMT_CREDENTIAL_REVOKE from banned peer {actual_sender[:16]}...", level='warn')
        return {"result": "continue"}

    # R5-M-4 fix: Membership check BEFORE proto_events to avoid consuming dedup rows for non-members
    if database:
        member = database.get_member(actual_sender)
        if not member:
            plugin.log(f"cl-hive: MGMT_CREDENTIAL_REVOKE from non-member {actual_sender[:16]}...", level='debug')
            return {"result": "continue"}

    # Timestamp freshness
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "MGMT_CREDENTIAL_REVOKE"):
        return {"result": "continue"}

    # P3-M-4 fix: In-memory relay dedup for credential messages
    if not _credential_relay_dedup(payload, "MGMT_CREDENTIAL_REVOKE"):
        return {"result": "continue"}

    # Dedup
    _eid = None
    if database:
        is_new, _eid = check_and_record(database, "MGMT_CREDENTIAL_REVOKE", payload, actual_sender)
        if not is_new:
            # P3-M-3 fix: Still relay even if already processed
            _relay_message(HiveMessageType.MGMT_CREDENTIAL_REVOKE, payload, peer_id)
            # R5-L-6 fix: Emit ack on dedup branch so sender outbox entries are cleared
            _emit_ack(peer_id, payload.get("event_id") or _eid)
            return {"result": "continue"}

    # Process revocation
    if management_schema_registry:
        management_schema_registry.handle_mgmt_credential_revoke(actual_sender, payload)

    # P3-H-2 fix: Emit ack after successful processing
    _emit_ack(peer_id, payload.get("event_id") or _eid)

    # P3-M-3 fix: Relay to other members
    _relay_message(HiveMessageType.MGMT_CREDENTIAL_REVOKE, payload, peer_id)

    return {"result": "continue"}


def _verify_phase4b_signature(peer_id: str, payload: Dict, msg_type: str,
                               get_signing_payload_fn, plugin: Plugin) -> bool:
    """Verify signature for Phase 4B messages. Returns True if valid."""
    signature = payload.get("signature", "")
    if not signature:
        plugin.log(f"cl-hive: {msg_type} missing signature from {peer_id[:16]}...", level='warn')
        return False
    try:
        signing_payload = _phase4b_build_signing_payload(get_signing_payload_fn, payload)
        verify_result = plugin.rpc.call("checkmessage", {
            "message": signing_payload,
            "zbase": signature,
            "pubkey": peer_id
        })
        if not verify_result.get("verified"):
            plugin.log(f"cl-hive: {msg_type} invalid signature from {peer_id[:16]}...", level='warn')
            return False
    except Exception as e:
        plugin.log(f"cl-hive: {msg_type} signature check failed: {e}", level='warn')
        return False
    return True


def _phase4b_build_signing_payload(get_signing_payload_fn, payload: Dict[str, Any]) -> str:
    """Build signing payload from incoming message payload using function signature."""
    try:
        sig = inspect.signature(get_signing_payload_fn)
    except (TypeError, ValueError):
        return get_signing_payload_fn(payload)

    kwargs = {}
    for name, param in sig.parameters.items():
        if param.kind not in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            continue
        if name in payload:
            kwargs[name] = payload[name]
        elif param.default is inspect._empty:
            raise KeyError(f"missing signing payload field: {name}")
    return get_signing_payload_fn(**kwargs)


def _phase4b_check_rate_limit(peer_id: str, msg_type: str, plugin: Plugin) -> bool:
    """Sliding-window rate limiting for Phase 4B message handlers."""
    limit_cfg = PHASE4B_RATE_LIMITS.get(msg_type)
    if not limit_cfg:
        return True

    max_count, window_seconds = limit_cfg
    now = int(time.time())
    cutoff = now - window_seconds
    key = (peer_id, msg_type)

    with _phase4b_rate_lock:
        timestamps = _phase4b_rate_windows.get(key, [])
        timestamps = [ts for ts in timestamps if ts > cutoff]
        if len(timestamps) >= max_count:
            plugin.log(
                f"cl-hive: {msg_type} from {peer_id[:16]}... rate-limited "
                f"({len(timestamps)}/{max_count} in {window_seconds}s)",
                level='warn'
            )
            _phase4b_rate_windows[key] = timestamps
            return False

        timestamps.append(now)
        _phase4b_rate_windows[key] = timestamps

        if len(_phase4b_rate_windows) > 2000:
            stale_keys = [
                k for k, vals in _phase4b_rate_windows.items()
                if not vals or vals[-1] <= cutoff
            ]
            for k in stale_keys:
                _phase4b_rate_windows.pop(k, None)

    return True


def _phase4b_record_if_new(peer_id: str, payload: Dict, msg_type: str) -> bool:
    """Record event idempotently. Returns True if new."""
    if not database:
        return True
    is_new, _eid = check_and_record(database, msg_type, payload, peer_id)
    return is_new


def _phase4b_common_checks(peer_id: str, payload: Dict, msg_type: str,
                            plugin: Plugin) -> bool:
    """Common checks for all Phase 4B handlers. Returns True if message should be processed."""
    # Identity binding
    sender_id = payload.get("sender_id", "")
    if sender_id != peer_id:
        plugin.log(f"cl-hive: {msg_type} sender mismatch from {peer_id[:16]}...", level='warn')
        return False

    # Ban check
    if database and database.is_banned(peer_id):
        plugin.log(f"cl-hive: {msg_type} from banned peer {peer_id[:16]}...", level='warn')
        return False

    # Membership check
    if database:
        member = database.get_member(peer_id)
        if not member:
            plugin.log(f"cl-hive: {msg_type} from non-member {peer_id[:16]}...", level='debug')
            return False

    # Timestamp freshness
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, msg_type):
        return False

    # Rate limit
    if not _phase4b_check_rate_limit(peer_id, msg_type, plugin):
        return False

    return True


def handle_settlement_receipt(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """Handle SETTLEMENT_RECEIPT message."""
    from modules.protocol import validate_settlement_receipt, get_settlement_receipt_signing_payload
    from modules.settlement import SettlementTypeRegistry
    if not validate_settlement_receipt(payload):
        plugin.log(f"cl-hive: invalid SETTLEMENT_RECEIPT from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    if not _phase4b_common_checks(peer_id, payload, "SETTLEMENT_RECEIPT", plugin):
        return {"result": "continue"}

    if not _verify_phase4b_signature(peer_id, payload, "SETTLEMENT_RECEIPT",
                                      get_settlement_receipt_signing_payload, plugin):
        return {"result": "continue"}

    if not _phase4b_record_if_new(peer_id, payload, "SETTLEMENT_RECEIPT"):
        return {"result": "continue"}

    # P4R4-M-1: Validate from_peer matches actual sender to prevent forged obligations
    claimed_from = payload.get("from_peer", "")
    if claimed_from and claimed_from != peer_id:
        plugin.log(
            f"cl-hive: SETTLEMENT_RECEIPT from_peer mismatch: "
            f"claimed={claimed_from[:16]}... actual={peer_id[:16]}...",
            level='warn',
        )
        return {"result": "continue"}

    if not hasattr(settlement_mgr, '_type_registry') or settlement_mgr._type_registry is None:
        settlement_mgr._type_registry = SettlementTypeRegistry(
            cashu_escrow_mgr=cashu_escrow_mgr,
            did_credential_mgr=did_credential_mgr,
        )
    registry = settlement_mgr._type_registry
    valid_receipt, reason = registry.verify_receipt(
        payload.get("settlement_type", ""),
        payload.get("receipt_data", {}) or {},
    )
    if not valid_receipt:
        plugin.log(
            f"cl-hive: SETTLEMENT_RECEIPT rejected ({reason}) from {peer_id[:16]}...",
            level='warn',
        )
        return {"result": "continue"}

    if database:
        database.store_obligation(
            obligation_id=payload.get("receipt_id", ""),
            settlement_type=payload.get("settlement_type", ""),
            from_peer=payload.get("from_peer", ""),
            to_peer=payload.get("to_peer", ""),
            amount_sats=int(payload.get("amount_sats", 0) or 0),
            window_id=payload.get("window_id", ""),
            receipt_id=payload.get("receipt_id", ""),
            created_at=int(time.time()),
        )

    plugin.log(f"cl-hive: SETTLEMENT_RECEIPT from {peer_id[:16]}... "
               f"type={payload.get('settlement_type')} amount={payload.get('amount_sats')}")
    return {"result": "continue"}


def handle_bond_posting(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """Handle BOND_POSTING message."""
    from modules.protocol import validate_bond_posting, get_bond_posting_signing_payload
    if not validate_bond_posting(payload):
        plugin.log(f"cl-hive: invalid BOND_POSTING from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    if not _phase4b_common_checks(peer_id, payload, "BOND_POSTING", plugin):
        return {"result": "continue"}

    if not _verify_phase4b_signature(peer_id, payload, "BOND_POSTING",
                                      get_bond_posting_signing_payload, plugin):
        return {"result": "continue"}

    if not _phase4b_record_if_new(peer_id, payload, "BOND_POSTING"):
        return {"result": "continue"}

    if database:
        database.store_bond(
            bond_id=payload.get("bond_id", ""),
            peer_id=peer_id,
            amount_sats=int(payload.get("amount_sats", 0) or 0),
            token_json=None,
            posted_at=int(payload.get("timestamp", int(time.time()))),
            timelock=int(payload.get("timelock", 0) or 0),
            tier=payload.get("tier", ""),
        )

    plugin.log(f"cl-hive: BOND_POSTING from {peer_id[:16]}... "
               f"tier={payload.get('tier')} amount={payload.get('amount_sats')}")
    return {"result": "continue"}


def handle_bond_slash(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """Handle BOND_SLASH message."""
    from modules.protocol import (
        validate_bond_slash,
        get_bond_slash_signing_payload,
        get_arbitration_vote_signing_payload,
    )
    from modules.settlement import BondManager
    if not validate_bond_slash(payload):
        plugin.log(f"cl-hive: invalid BOND_SLASH from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    if not _phase4b_common_checks(peer_id, payload, "BOND_SLASH", plugin):
        return {"result": "continue"}

    if not _verify_phase4b_signature(peer_id, payload, "BOND_SLASH",
                                      get_bond_slash_signing_payload, plugin):
        return {"result": "continue"}

    if not _phase4b_record_if_new(peer_id, payload, "BOND_SLASH"):
        return {"result": "continue"}

    if not database:
        return {"result": "continue"}

    dispute_id = payload.get("dispute_id", "")
    dispute = database.get_dispute(dispute_id) if dispute_id else None
    # R5-H-2 fix: Only allow outcome "upheld" (not "slashed") to prevent repeated slashing.
    # Note: proto_events via _phase4b_record_if_new already deduplicates on (bond_id, dispute_id)
    # so the same pair cannot be processed twice. This outcome check is a defense-in-depth guard
    # against different event_id paths or manual DB tampering.
    if not dispute or dispute.get("outcome") not in ("upheld",) or not dispute.get("resolved_at"):
        plugin.log(
            f"cl-hive: BOND_SLASH rejected for unresolved/non-upheld dispute {dispute_id[:16]}...",
            level='warn',
        )
        return {"result": "continue"}

    bond_id = payload.get("bond_id", "")
    bond = database.get_bond(bond_id) if bond_id else None
    if not bond or bond.get("status") != "active":
        plugin.log(f"cl-hive: BOND_SLASH rejected, inactive bond {bond_id[:16]}...", level='warn')
        return {"result": "continue"}

    # R5-H-1 fix: Verify bond belongs to the dispute respondent
    if bond.get("peer_id") != dispute.get("respondent_peer"):
        plugin.log(
            f"cl-hive: BOND_SLASH rejected, bond owner {bond.get('peer_id', '')[:16]}... "
            f"!= dispute respondent {dispute.get('respondent_peer', '')[:16]}...",
            level='warn',
        )
        return {"result": "continue"}

    panel_members = []
    votes = {}
    try:
        if dispute.get("panel_members_json"):
            panel_members = json.loads(dispute["panel_members_json"])
    except (TypeError, ValueError):
        panel_members = []
    try:
        if dispute.get("votes_json"):
            votes = json.loads(dispute["votes_json"])
    except (TypeError, ValueError):
        votes = {}

    sender_member = database.get_member(peer_id)
    sender_tier = (sender_member or {}).get("tier", "")
    if peer_id not in panel_members and sender_tier not in ("admin", "founding"):
        plugin.log(f"cl-hive: BOND_SLASH sender {peer_id[:16]}... not authorized", level='warn')
        return {"result": "continue"}

    remaining = int(bond.get("amount_sats", 0) or 0) - int(bond.get("slashed_amount", 0) or 0)
    slash_amount = int(payload.get("slash_amount", 0) or 0)
    if slash_amount <= 0 or slash_amount > remaining:
        plugin.log(
            f"cl-hive: BOND_SLASH rejected invalid amount {slash_amount} (remaining={remaining})",
            level='warn',
        )
        return {"result": "continue"}

    quorum = (len(panel_members) // 2) + 1 if panel_members else 0
    upheld_votes = 0
    for voter_id in panel_members:
        vote_info = votes.get(voter_id)
        if not isinstance(vote_info, dict):
            continue
        if vote_info.get("vote") != "upheld":
            continue
        vote_sig = vote_info.get("signature", "")
        if not isinstance(vote_sig, str) or not vote_sig:
            plugin.log(f"cl-hive: BOND_SLASH missing vote signature for {voter_id[:16]}...", level='warn')
            return {"result": "continue"}
        vote_payload = get_arbitration_vote_signing_payload(
            dispute_id=dispute_id,
            vote=vote_info.get("vote", "upheld"),
            reason=vote_info.get("reason", ""),
        )
        try:
            verify = plugin.rpc.call("checkmessage", {
                "message": vote_payload,
                "zbase": vote_sig,
                "pubkey": voter_id,
            })
        except Exception as e:
            plugin.log(f"cl-hive: BOND_SLASH vote signature check error: {e}", level='warn')
            return {"result": "continue"}
        if not verify.get("verified"):
            plugin.log(f"cl-hive: BOND_SLASH invalid vote signature for {voter_id[:16]}...", level='warn')
            return {"result": "continue"}
        upheld_votes += 1

    if quorum <= 0 or upheld_votes < quorum:
        plugin.log(
            f"cl-hive: BOND_SLASH quorum not met for {dispute_id[:16]}... ({upheld_votes}/{quorum})",
            level='warn',
        )
        return {"result": "continue"}

    bond_mgr = BondManager(database, plugin)
    slash_result = bond_mgr.slash_bond(bond_id, slash_amount)
    if not slash_result:
        plugin.log(f"cl-hive: BOND_SLASH apply failed for bond {bond_id[:16]}...", level='warn')
        return {"result": "continue"}

    # R5-H-2 fix: Mark dispute as "slashed" so it cannot be reused for another slash.
    # Note: update_dispute_outcome uses a CAS guard (resolved_at IS NULL OR resolved_at = 0)
    # which would reject this update since the dispute is already resolved. We pass resolved_at=0
    # to bypass the CAS guard (non-resolving update path) since we're only changing outcome.
    database.update_dispute_outcome(
        dispute_id=dispute_id,
        outcome="slashed",
        slash_amount=int(dispute.get("slash_amount", 0) or 0) + int(slash_result["slashed_amount"]),
        panel_members_json=dispute.get("panel_members_json"),
        votes_json=dispute.get("votes_json"),
        resolved_at=0,
    )

    plugin.log(f"cl-hive: BOND_SLASH from {peer_id[:16]}... "
               f"bond={payload.get('bond_id', '')[:16]} amount={payload.get('slash_amount')}")
    return {"result": "continue"}


def handle_netting_proposal(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """Handle NETTING_PROPOSAL message."""
    from modules.protocol import validate_netting_proposal, get_netting_proposal_signing_payload
    from modules.settlement import NettingEngine
    if not validate_netting_proposal(payload):
        plugin.log(f"cl-hive: invalid NETTING_PROPOSAL from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    if not _phase4b_common_checks(peer_id, payload, "NETTING_PROPOSAL", plugin):
        return {"result": "continue"}

    if not _verify_phase4b_signature(peer_id, payload, "NETTING_PROPOSAL",
                                      get_netting_proposal_signing_payload, plugin):
        return {"result": "continue"}

    if not _phase4b_record_if_new(peer_id, payload, "NETTING_PROPOSAL"):
        return {"result": "continue"}

    if database:
        window_id = payload.get("window_id", "")
        obligations = database.get_obligations_for_window(window_id, status='pending', limit=10_000)
        computed_hash = NettingEngine.compute_obligations_hash(obligations)
        incoming_hash = payload.get("obligations_hash", "")
        if computed_hash != incoming_hash:
            plugin.log(
                f"cl-hive: NETTING_PROPOSAL hash mismatch for window {window_id[:16]}...",
                level='warn',
            )
            return {"result": "continue"}

        with _phase4b_netting_lock:
            _phase4b_netting_proposals[window_id] = {
                "proposer": peer_id,
                "obligations_hash": incoming_hash,
                "received_at": int(time.time()),
            }
            # L-9 audit fix: Prune stale netting proposals to prevent unbounded growth
            if len(_phase4b_netting_proposals) > 500:
                cutoff = int(time.time()) - 86400  # 24 hours
                stale_keys = [k for k, v in _phase4b_netting_proposals.items()
                              if v.get("received_at", 0) < cutoff]
                for k in stale_keys:
                    _phase4b_netting_proposals.pop(k, None)

    plugin.log(f"cl-hive: NETTING_PROPOSAL from {peer_id[:16]}... "
               f"window={payload.get('window_id', '')[:16]} type={payload.get('netting_type')}")
    return {"result": "continue"}


def handle_netting_ack(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """Handle NETTING_ACK message."""
    from modules.protocol import validate_netting_ack, get_netting_ack_signing_payload
    if not validate_netting_ack(payload):
        plugin.log(f"cl-hive: invalid NETTING_ACK from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    if not _phase4b_common_checks(peer_id, payload, "NETTING_ACK", plugin):
        return {"result": "continue"}

    if not _verify_phase4b_signature(peer_id, payload, "NETTING_ACK",
                                      get_netting_ack_signing_payload, plugin):
        return {"result": "continue"}

    if not _phase4b_record_if_new(peer_id, payload, "NETTING_ACK"):
        return {"result": "continue"}

    if database:
        window_id = payload.get("window_id", "")
        obligations_hash = payload.get("obligations_hash", "")
        accepted = bool(payload.get("accepted", False))

        # R5-M-11 fix: Hold netting lock through hash verification AND DB update
        # to prevent TOCTOU race where proposal is modified between check and update.
        with _phase4b_netting_lock:
            proposal = _phase4b_netting_proposals.get(window_id)

            if proposal and proposal.get("obligations_hash") == obligations_hash and accepted:
                # M-6 audit fix: Verify ack sender is NOT the proposer (counterparty check)
                if proposal.get("proposer") == peer_id:
                    plugin.log(f"cl-hive: NETTING_ACK from proposer {peer_id[:16]}..., ignoring", level='warn')
                else:
                    # Verify peer is party to at least one obligation in this window
                    obligations = database.get_obligations_for_window(window_id, status='pending', limit=10_000)
                    peer_is_party = any(
                        o.get("from_peer") == peer_id or o.get("to_peer") == peer_id
                        for o in obligations
                    )
                    if peer_is_party:
                        proposer_id = proposal.get("proposer", "")
                        database.update_bilateral_obligation_status(window_id, peer_id, proposer_id, "netted")
                    else:
                        plugin.log(f"cl-hive: NETTING_ACK from non-party {peer_id[:16]}..., ignoring", level='warn')

    plugin.log(f"cl-hive: NETTING_ACK from {peer_id[:16]}... "
               f"window={payload.get('window_id', '')[:16]} accepted={payload.get('accepted')}")
    return {"result": "continue"}


def handle_violation_report(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """Handle VIOLATION_REPORT message."""
    from modules.protocol import validate_violation_report, get_violation_report_signing_payload
    from modules.settlement import DisputeResolver
    if not validate_violation_report(payload):
        plugin.log(f"cl-hive: invalid VIOLATION_REPORT from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    if not _phase4b_common_checks(peer_id, payload, "VIOLATION_REPORT", plugin):
        return {"result": "continue"}

    if not _verify_phase4b_signature(peer_id, payload, "VIOLATION_REPORT",
                                      get_violation_report_signing_payload, plugin):
        return {"result": "continue"}

    if not _phase4b_record_if_new(peer_id, payload, "VIOLATION_REPORT"):
        return {"result": "continue"}

    # P4-M-4 fix: Use violator_id from payload for proper violation tracking
    violator_id = payload.get("violator_id", "")
    violation_type = payload.get("violation_type", "")

    if database:
        evidence = payload.get("evidence", {}) or {}
        # Inject violator_id into evidence so dispute resolver can reference it
        if violator_id:
            evidence["violator_id"] = violator_id
        if violation_type:
            evidence["violation_type"] = violation_type
        obligation_id = evidence.get("obligation_id")
        if isinstance(obligation_id, str) and obligation_id:
            resolver = DisputeResolver(database, plugin, rpc=plugin.rpc)
            resolver.file_dispute(obligation_id, peer_id, evidence)

    plugin.log(f"cl-hive: VIOLATION_REPORT from {peer_id[:16]}... "
               f"violator={violator_id[:16] if violator_id else 'unknown'} type={violation_type}")
    return {"result": "continue"}


def handle_arbitration_vote(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """Handle ARBITRATION_VOTE message."""
    from modules.protocol import validate_arbitration_vote, get_arbitration_vote_signing_payload
    from modules.settlement import DisputeResolver
    if not validate_arbitration_vote(payload):
        plugin.log(f"cl-hive: invalid ARBITRATION_VOTE from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    if not _phase4b_common_checks(peer_id, payload, "ARBITRATION_VOTE", plugin):
        return {"result": "continue"}

    if not _verify_phase4b_signature(peer_id, payload, "ARBITRATION_VOTE",
                                      get_arbitration_vote_signing_payload, plugin):
        return {"result": "continue"}

    if not _phase4b_record_if_new(peer_id, payload, "ARBITRATION_VOTE"):
        return {"result": "continue"}

    if database:
        dispute_id = payload.get("dispute_id", "")
        vote = payload.get("vote", "")
        reason = payload.get("reason", "")
        signature = payload.get("signature", "")
        resolver = DisputeResolver(database, plugin, rpc=plugin.rpc)
        vote_result = resolver.record_vote(
            dispute_id=dispute_id,
            voter_id=peer_id,
            vote=vote,
            reason=reason,
            signature=signature,
        )
        if isinstance(vote_result, dict) and vote_result.get("error"):
            plugin.log(
                f"cl-hive: ARBITRATION_VOTE rejected for {dispute_id[:16]}...: {vote_result['error']}",
                level='warn',
            )
            return {"result": "continue"}

        # P4R4-M-2: record_vote() already checks quorum atomically while
        # holding _dispute_lock.  A redundant external check_quorum() call
        # was removed here to avoid using stale data and double-resolution.
        if isinstance(vote_result, dict) and vote_result.get("quorum_result"):
            qr = vote_result["quorum_result"]
            plugin.log(
                f"cl-hive: dispute {dispute_id[:16]}... resolved via quorum: "
                f"outcome={qr.get('outcome')}",
            )

    plugin.log(f"cl-hive: ARBITRATION_VOTE from {peer_id[:16]}... "
               f"dispute={payload.get('dispute_id', '')[:16]} vote={payload.get('vote')}")
    return {"result": "continue"}


# =============================================================================
# PHASE 4: ESCROW MAINTENANCE LOOP
# =============================================================================

def _broadcast_promotion_vote(target_peer_id: str, voter_peer_id: str) -> bool:
    """
    Broadcast a promotion vote as a VOUCH message for cross-node sync.

    This enables the manual promotion system to sync votes across nodes
    by reusing the existing VOUCH message infrastructure.

    Args:
        target_peer_id: The neophyte being voted for
        voter_peer_id: The member casting the vote

    Returns:
        True if broadcast was successful
    """
    if not membership_mgr or not plugin or not database:
        return False

    # Use a deterministic request_id so all nodes reference the same promotion
    # Must be hex-only (protocol validation requires [0-9a-f] only)
    request_id = target_peer_id[2:34]  # First 32 hex chars after "03" prefix

    # Create and sign the vouch
    vouch_ts = int(time.time())
    canonical = membership_mgr.build_vouch_message(target_peer_id, request_id, vouch_ts)

    try:
        sig = plugin.rpc.signmessage(canonical)["zbase"]
    except Exception as e:
        plugin.log(f"Failed to sign promotion vote: {e}", level='warn')
        return False

    # Store locally in vouch table (so it's counted for regular promotion flow)
    database.add_promotion_vouch(target_peer_id, request_id, voter_peer_id, sig, vouch_ts)

    # Also ensure promotion request exists
    requests = database.get_promotion_requests(target_peer_id)
    has_request = any(r.get("request_id") == request_id for r in requests)
    if not has_request:
        database.add_promotion_request(target_peer_id, request_id, status="pending")

    vouch_payload = {
        "target_pubkey": target_peer_id,
        "request_id": request_id,
        "timestamp": vouch_ts,
        "voucher_pubkey": voter_peer_id,
        "sig": sig
    }
    result = _broadcast_member_message(
        msg_type=HiveMessageType.VOUCH,
        payload=vouch_payload,
        reliability="reliable",
        failure_policy="fail_closed",
        log_label="promotion_vote",
    )
    sent = result["queued"] or result["sent"]

    plugin.log(
        f"Broadcast promotion vote for {target_peer_id[:16]}... to {sent} members",
        level='debug'
    )
    return result["ok"]


# R5-M-5 fix: Per-relay-peer rate limiter for credential messages
# Prevents a single relay node from flooding rate limits for multiple spoofed origins.
# Maps relay_peer_id -> list of timestamps
_relay_credential_rate: Dict[str, list] = {}
_relay_credential_rate_lock = threading.Lock()
_RELAY_CREDENTIAL_RATE_MAX = 50   # max 50 relayed credential messages per hour per relay peer
_RELAY_CREDENTIAL_RATE_WINDOW = 3600  # 1 hour window
_RELAY_CREDENTIAL_RATE_DICT_MAX = 500  # max tracked relay peers


def _check_relay_credential_rate(relay_peer_id: str) -> bool:
    """Check per-relay-peer rate limit for credential messages.
    Returns True if within limit, False if rate-limited."""
    now = int(time.time())
    cutoff = now - _RELAY_CREDENTIAL_RATE_WINDOW
    with _relay_credential_rate_lock:
        timestamps = _relay_credential_rate.get(relay_peer_id, [])
        timestamps = [ts for ts in timestamps if ts > cutoff]
        if len(timestamps) >= _RELAY_CREDENTIAL_RATE_MAX:
            _relay_credential_rate[relay_peer_id] = timestamps
            return False
        timestamps.append(now)
        _relay_credential_rate[relay_peer_id] = timestamps
        # Evict stale entries if dict grows too large
        if len(_relay_credential_rate) > _RELAY_CREDENTIAL_RATE_DICT_MAX:
            stale = [k for k, v in _relay_credential_rate.items()
                     if not v or v[-1] <= cutoff]
            for k in stale:
                _relay_credential_rate.pop(k, None)
    return True


# P3-M-4 fix: In-memory dedup cache for credential relay messages
# Bounded dict: maps message_hash -> timestamp, evicts oldest when full
_credential_relay_seen: Dict[str, float] = {}
_credential_relay_lock = threading.Lock()  # NEW-3 fix: thread safety for dedup dict
_CREDENTIAL_RELAY_DEDUP_MAX = 1000
_CREDENTIAL_RELAY_DEDUP_TTL = 600  # 10 minutes


def _credential_relay_dedup(payload: Dict[str, Any], msg_type: str) -> bool:
    """
    Check if a credential message has already been seen for relay dedup.
    Returns True if message is new (should process), False if duplicate.
    """
    import hashlib
    # Build a dedup key from stable payload fields
    event_id = payload.get("event_id", "") or payload.get("_event_id", "")
    sender_id = payload.get("sender_id", "")
    ts = str(payload.get("timestamp", ""))
    dedup_input = f"{msg_type}:{sender_id}:{event_id}:{ts}"
    msg_hash = hashlib.sha256(dedup_input.encode()).hexdigest()[:32]

    now = time.time()

    with _credential_relay_lock:
        # Evict expired entries if cache is full
        if len(_credential_relay_seen) >= _CREDENTIAL_RELAY_DEDUP_MAX:
            expired = [k for k, v in _credential_relay_seen.items()
                       if now - v > _CREDENTIAL_RELAY_DEDUP_TTL]
            for k in expired:
                del _credential_relay_seen[k]
            # If still full after eviction, remove oldest entries
            if len(_credential_relay_seen) >= _CREDENTIAL_RELAY_DEDUP_MAX:
                oldest = sorted(_credential_relay_seen.items(), key=lambda x: x[1])
                for k, _ in oldest[:len(oldest) // 2]:
                    del _credential_relay_seen[k]

        if msg_hash in _credential_relay_seen:
            return False  # Already seen

        _credential_relay_seen[msg_hash] = now
        return True


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
        # Relayed message: verify peer_id is a known member or neophyte (they're relaying)
        # M-15 audit fix: Allow neophyte relay to avoid message delivery failures
        relay_peer = database.get_member(peer_id)
        if not relay_peer or relay_peer.get("tier") not in (MembershipTier.MEMBER.value, MembershipTier.NEOPHYTE.value):
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
    database.remove_member(peer_id)

    # 2. Remove from in-memory state
    if state_manager:
        try:
            state_manager.remove_peer_state(peer_id)
        except Exception:
            pass

    # 3. Revert fee policy to dynamic
    if bridge and bridge.status == BridgeStatus.ENABLED:
        try:
            bridge.set_hive_policy(peer_id, is_member=False)
        except Exception:
            pass

    # 4. Force the next gossip cycle to broadcast immediately so remaining
    #    members see the updated member list without the removed peer.
    if gossip_mgr:
        try:
            gossip_mgr.force_next_broadcast()
        except Exception:
            pass


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


def _sync_member_policies(plugin: Plugin) -> None:
    """
    Sync fee policies for all existing members on startup.

    Called during initialization to ensure all members have correct
    fee policies set in cl-revenue-ops. This handles the case where
    the plugin was restarted or policies were reset.

    Policy assignment:
    - Member: HIVE strategy (0 PPM fees)
    - Neophyte: dynamic strategy (normal fee behavior)
    """
    if not database or not bridge or bridge.status != BridgeStatus.ENABLED:
        return

    members = database.get_all_members()
    synced = 0

    for member in members:
        peer_id = member["peer_id"]
        tier = member.get("tier")

        # Skip ourselves
        if peer_id == our_pubkey:
            continue

        # SECURITY: Banned peers always get dynamic strategy
        if database.is_banned(peer_id):
            try:
                bridge.set_hive_policy(peer_id, is_member=False, bypass_rate_limit=True)
            except Exception:
                pass
            continue

        # Determine if this peer should have HIVE strategy
        # P5-M-1 fix: Only full member tier gets HIVE strategy (0-fee)
        # Neophytes should NOT get hive fees — they use dynamic strategy
        is_hive_member = tier in (MembershipTier.MEMBER.value,)

        try:
            # Use bypass_rate_limit=True for startup sync
            success = bridge.set_hive_policy(peer_id, is_member=is_hive_member, bypass_rate_limit=True)
            if success:
                synced += 1
                plugin.log(
                    f"cl-hive: Synced policy for {peer_id[:16]}... "
                    f"({'hive' if is_hive_member else 'dynamic'})",
                    level='debug'
                )
        except Exception as e:
            plugin.log(
                f"cl-hive: Failed to sync policy for {peer_id[:16]}...: {e}",
                level='debug'
            )

    if synced > 0:
        plugin.log(f"cl-hive: Synced fee policies for {synced} member(s)")

    # Cleanup stale hive policies: peers with hive strategy in cl-revenue-ops
    # that are no longer hive members (e.g. removal bridge call failed).
    member_peer_ids = {m["peer_id"] for m in members}
    try:
        result = bridge.safe_call("revenue-policy", {"action": "list"})
        policies = result.get("policies", [])
        reverted = 0
        for pol in policies:
            pid = pol.get("peer_id", "")
            strategy = pol.get("strategy", "")
            if strategy == "hive" and pid and pid not in member_peer_ids and pid != our_pubkey:
                try:
                    bridge.set_hive_policy(pid, is_member=False, bypass_rate_limit=True)
                    reverted += 1
                    plugin.log(
                        f"cl-hive: Reverted stale hive policy for non-member {pid[:16]}...",
                        level='info'
                    )
                except Exception:
                    pass
        if reverted > 0:
            plugin.log(f"cl-hive: Cleaned up {reverted} stale hive policy(s)")
    except Exception as e:
        plugin.log(f"cl-hive: Could not check for stale hive policies: {e}", level='debug')


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


def handle_promotion_request(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle PROMOTION_REQUEST message from neophyte.

    RELAY: Supports multi-hop relay for non-mesh topologies.
    """
    if not config or not config.membership_enabled or not membership_mgr:
        return {"result": "continue"}

    # RELAY: Check deduplication before processing
    if not _should_process_message(payload):
        plugin.log(f"cl-hive: PROMOTION_REQUEST duplicate from {peer_id[:16]}..., skipping", level='debug')
        return {"result": "continue"}

    if not validate_promotion_request(payload):
        plugin.log(f"cl-hive: PROMOTION_REQUEST from {peer_id[:16]}... invalid payload", level='warn')
        return {"result": "continue"}

    target_pubkey = payload["target_pubkey"]
    request_id = payload["request_id"]
    timestamp = payload["timestamp"]

    # For direct messages: target must be the sender
    # For relayed messages: target is the original neophyte, peer_id is the relay node
    is_relayed = _is_relayed_message(payload)
    if not is_relayed and target_pubkey != peer_id:
        plugin.log(f"cl-hive: PROMOTION_REQUEST from {peer_id[:16]}... target mismatch", level='warn')
        return {"result": "continue"}

    # Phase C: Persistent idempotency check
    is_new, event_id = check_and_record(database, "PROMOTION_REQUEST", payload, target_pubkey)
    if not is_new:
        plugin.log(f"cl-hive: PROMOTION_REQUEST duplicate event {event_id}, skipping", level='debug')
        _emit_ack(peer_id, event_id)
        _relay_message(HiveMessageType.PROMOTION_REQUEST, payload, peer_id)
        return {"result": "continue"}
    if event_id:
        payload["_event_id"] = event_id

    # RELAY: Forward to other members before processing
    relay_count = _relay_message(HiveMessageType.PROMOTION_REQUEST, payload, peer_id)
    if relay_count > 0:
        plugin.log(f"cl-hive: PROMOTION_REQUEST relayed to {relay_count} members", level='debug')

    # C-1 audit fix: Reject promotion requests from/for banned peers
    if database.is_banned(target_pubkey):
        plugin.log(f"cl-hive: PROMOTION_REQUEST from banned peer {target_pubkey[:16]}..., ignoring", level='warn')
        return {"result": "continue"}

    # H-4 audit fix: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_GOSSIP_AGE_SECONDS, "PROMOTION_REQUEST"):
        return {"result": "continue"}

    target_member = database.get_member(target_pubkey)
    if not target_member or target_member.get("tier") != MembershipTier.NEOPHYTE.value:
        return {"result": "continue"}

    database.add_promotion_request(target_pubkey, request_id, status="pending")

    # Phase D: Acknowledge receipt
    _emit_ack(peer_id, payload.get("_event_id"))

    our_tier = membership_mgr.get_tier(our_pubkey) if our_pubkey else None
    if our_tier not in (MembershipTier.MEMBER.value,):
        return {"result": "continue"}

    if not config.auto_vouch_enabled:
        return {"result": "continue"}

    eval_result = membership_mgr.evaluate_promotion(target_pubkey)
    if not eval_result["eligible"]:
        return {"result": "continue"}

    existing_vouches = database.get_promotion_vouches(target_pubkey, request_id)
    for vouch in existing_vouches:
        if vouch.get("voucher_peer_id") == our_pubkey:
            return {"result": "continue"}

    vouch_ts = int(time.time())
    canonical = membership_mgr.build_vouch_message(target_pubkey, request_id, vouch_ts)
    try:
        sig = plugin.rpc.signmessage(canonical)["zbase"]
    except Exception as e:
        plugin.log(f"cl-hive: Failed to sign vouch: {e}", level='warn')
        return {"result": "continue"}

    vouch_payload = {
        "target_pubkey": target_pubkey,
        "request_id": request_id,
        "timestamp": vouch_ts,
        "voucher_pubkey": our_pubkey,
        "sig": sig
    }
    _reliable_broadcast(HiveMessageType.VOUCH, vouch_payload)
    return {"result": "continue"}


def handle_vouch(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle VOUCH message from member endorsing a neophyte.

    RELAY: Supports multi-hop relay for non-mesh topologies.
    """
    if not config or not config.membership_enabled or not membership_mgr:
        return {"result": "continue"}

    # RELAY: Check deduplication before processing
    if not _should_process_message(payload):
        plugin.log(f"cl-hive: VOUCH duplicate from {peer_id[:16]}..., skipping", level='debug')
        return {"result": "continue"}

    if not validate_vouch(payload):
        plugin.log(f"cl-hive: VOUCH from {peer_id[:16]}... invalid payload", level='warn')
        return {"result": "continue"}

    # For direct messages: voucher must be the sender
    # For relayed messages: voucher is the original member, peer_id is the relay node
    voucher_pubkey = payload["voucher_pubkey"]
    is_relayed = _is_relayed_message(payload)
    if not is_relayed and voucher_pubkey != peer_id:
        plugin.log(f"cl-hive: VOUCH from {peer_id[:16]}... voucher mismatch", level='warn')
        return {"result": "continue"}

    # Phase C: Persistent idempotency check
    is_new, event_id = check_and_record(database, "VOUCH", payload, voucher_pubkey)
    if not is_new:
        plugin.log(f"cl-hive: VOUCH duplicate event {event_id}, skipping", level='debug')
        _emit_ack(peer_id, event_id)
        _relay_message(HiveMessageType.VOUCH, payload, peer_id)
        return {"result": "continue"}
    if event_id:
        payload["_event_id"] = event_id

    # RELAY: Forward to other members before processing
    relay_count = _relay_message(HiveMessageType.VOUCH, payload, peer_id)
    if relay_count > 0:
        plugin.log(f"cl-hive: VOUCH relayed to {relay_count} members", level='debug')

    # H-7 audit fix: Prevent self-vouching
    if voucher_pubkey == payload["target_pubkey"]:
        plugin.log(f"cl-hive: VOUCH self-vouch attempt for {voucher_pubkey[:16]}..., ignoring", level='warn')
        return {"result": "continue"}

    # H-4 audit fix: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_GOSSIP_AGE_SECONDS, "VOUCH"):
        return {"result": "continue"}

    voucher = database.get_member(voucher_pubkey)
    if not voucher or voucher.get("tier") not in (MembershipTier.MEMBER.value,):
        return {"result": "continue"}

    # P5-M-2 fix: Check ban status BEFORE storing vouch or doing expensive operations
    if database.is_banned(payload["voucher_pubkey"]):
        plugin.log(f"cl-hive: VOUCH from banned voucher {voucher_pubkey[:16]}..., ignoring", level='warn')
        return {"result": "continue"}

    target_member = database.get_member(payload["target_pubkey"])
    if not target_member or target_member.get("tier") != MembershipTier.NEOPHYTE.value:
        return {"result": "continue"}

    now = int(time.time())
    if now - payload["timestamp"] > VOUCH_TTL_SECONDS:
        return {"result": "continue"}

    canonical = membership_mgr.build_vouch_message(
        payload["target_pubkey"], payload["request_id"], payload["timestamp"]
    )
    try:
        result = plugin.rpc.checkmessage(canonical, payload["sig"])
    except Exception as e:
        plugin.log(f"cl-hive: VOUCH signature check failed: {e}", level='warn')
        return {"result": "continue"}

    if not result.get("verified") or result.get("pubkey") != payload["voucher_pubkey"]:
        return {"result": "continue"}

    local_tier = membership_mgr.get_tier(our_pubkey) if our_pubkey else None
    if local_tier not in (MembershipTier.MEMBER.value, MembershipTier.NEOPHYTE.value):
        return {"result": "continue"}

    # Ensure the promotion request exists in our database (fixes gossip sync issue)
    # When we receive a VOUCH, we may not have received the original PROMOTION_REQUEST
    # This can happen if messages arrive out of order or if we joined after the request
    existing_request = database.get_promotion_requests(payload["target_pubkey"])
    request_exists = any(r.get("request_id") == payload["request_id"] for r in existing_request)
    if not request_exists:
        database.add_promotion_request(
            payload["target_pubkey"],
            payload["request_id"],
            status="pending"
        )
        plugin.log(f"cl-hive: Created missing promotion request for {payload['target_pubkey'][:16]}... from VOUCH", level='debug')

    stored = database.add_promotion_vouch(
        payload["target_pubkey"],
        payload["request_id"],
        payload["voucher_pubkey"],
        payload["sig"],
        payload["timestamp"]
    )
    if not stored:
        return {"result": "continue"}

    # Phase D: Acknowledge receipt + implicit ack (VOUCH implies PROMOTION_REQUEST received)
    _emit_ack(peer_id, payload.get("_event_id"))
    if outbox_mgr:
        outbox_mgr.process_implicit_ack(peer_id, HiveMessageType.VOUCH, payload)

    # Only full members can trigger auto-promotion
    if local_tier not in (MembershipTier.MEMBER.value,):
        return {"result": "continue"}

    active_members = membership_mgr.get_active_members()
    quorum = membership_mgr.calculate_quorum(len(active_members))
    vouches = database.get_promotion_vouches(payload["target_pubkey"], payload["request_id"])
    # R5-L-10 fix: Filter out vouches from banned members before quorum check
    valid_vouches = [v for v in vouches if not database.is_banned(v.get("voucher_peer_id", ""))]
    if len(valid_vouches) < quorum:
        return {"result": "continue"}

    if not config.auto_promote_enabled:
        return {"result": "continue"}

    promotion_payload = {
        "target_pubkey": payload["target_pubkey"],
        "request_id": payload["request_id"],
        "vouches": [
            {
                "target_pubkey": v["target_peer_id"],
                "request_id": v["request_id"],
                "timestamp": v["timestamp"],
                "voucher_pubkey": v["voucher_peer_id"],
                "sig": v["sig"]
            } for v in valid_vouches[:MAX_VOUCHES_IN_PROMOTION]
        ]
    }
    _reliable_broadcast(HiveMessageType.PROMOTION, promotion_payload)
    return {"result": "continue"}


def handle_promotion(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    if not config or not config.membership_enabled or not membership_mgr:
        return {"result": "continue"}

    # Deduplication check
    if not _should_process_message(payload):
        return {"result": "continue"}

    if not validate_promotion(payload):
        plugin.log(f"cl-hive: PROMOTION from {peer_id[:16]}... invalid payload", level='warn')
        return {"result": "continue"}

    # Phase C: Persistent idempotency check
    is_new, event_id = check_and_record(database, "PROMOTION", payload, peer_id)
    if not is_new:
        plugin.log(f"cl-hive: PROMOTION duplicate event {event_id}, skipping", level='debug')
        _emit_ack(peer_id, event_id)
        _relay_message(HiveMessageType.PROMOTION, payload, peer_id)
        return {"result": "continue"}
    if event_id:
        payload["_event_id"] = event_id

    # For relayed messages, verify peer_id is a member (relay forwarder)
    # The actual sender verification happens via signature in vouches
    if _is_relayed_message(payload):
        relay_member = database.get_member(peer_id)
        if not relay_member or relay_member.get("tier") not in (MembershipTier.MEMBER.value,):
            return {"result": "continue"}
        # Ban check on relay peer
        if database.is_banned(peer_id):
            return {"result": "continue"}
    else:
        sender = database.get_member(peer_id)
        sender_tier = sender.get("tier") if sender else None
        if sender_tier not in (MembershipTier.MEMBER.value,):
            return {"result": "continue"}

    target_pubkey = payload["target_pubkey"]
    request_id = payload["request_id"]

    # P5-H-2 fix: Reject promotion of banned peers
    if database.is_banned(target_pubkey):
        plugin.log(f"cl-hive: PROMOTION target {target_pubkey[:16]}... is banned, ignoring", level='warn')
        return {"result": "continue"}

    target_member = database.get_member(target_pubkey)
    if not target_member:
        # Unknown target - relay but don't process locally
        _relay_message(HiveMessageType.PROMOTION, payload, peer_id)
        return {"result": "continue"}

    if target_member.get("tier") != MembershipTier.NEOPHYTE.value:
        # Already promoted locally - still relay for other nodes that may not have seen it
        _relay_message(HiveMessageType.PROMOTION, payload, peer_id)
        return {"result": "continue"}

    request = database.get_promotion_request(target_pubkey, request_id)
    if request and request.get("status") == "accepted":
        # Already processed locally - still relay for other nodes
        _relay_message(HiveMessageType.PROMOTION, payload, peer_id)
        return {"result": "continue"}

    active_members = membership_mgr.get_active_members()
    quorum = membership_mgr.calculate_quorum(len(active_members))

    seen_vouchers = set()
    valid_vouches = []
    now = int(time.time())

    for vouch in payload["vouches"]:
        if vouch["voucher_pubkey"] in seen_vouchers:
            continue
        if now - vouch["timestamp"] > VOUCH_TTL_SECONDS:
            continue
        if database.is_banned(vouch["voucher_pubkey"]):
            continue
        member = database.get_member(vouch["voucher_pubkey"])
        member_tier = member.get("tier") if member else None
        if member_tier not in (MembershipTier.MEMBER.value,):
            continue
        canonical = membership_mgr.build_vouch_message(
            vouch["target_pubkey"], vouch["request_id"], vouch["timestamp"]
        )
        try:
            result = plugin.rpc.checkmessage(canonical, vouch["sig"])
        except Exception:
            continue
        if not result.get("verified") or result.get("pubkey") != vouch["voucher_pubkey"]:
            continue
        seen_vouchers.add(vouch["voucher_pubkey"])
        valid_vouches.append(vouch)

    if len(valid_vouches) < quorum:
        # Relay even if we don't have quorum - other nodes might
        _relay_message(HiveMessageType.PROMOTION, payload, peer_id)
        return {"result": "continue"}

    database.add_promotion_request(target_pubkey, request_id, status="accepted")
    database.update_promotion_request_status(target_pubkey, request_id, status="accepted")
    membership_mgr.set_tier(target_pubkey, MembershipTier.MEMBER.value)

    # Phase D: Acknowledge receipt
    _emit_ack(peer_id, payload.get("_event_id"))

    # Relay to other members
    _relay_message(HiveMessageType.PROMOTION, payload, peer_id)

    return {"result": "continue"}


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

    leaving_peer_id = payload["peer_id"]
    timestamp = payload["timestamp"]
    reason = payload["reason"]
    signature = payload["signature"]

    # Verify sender (supports relay)
    if not _validate_relay_sender(peer_id, leaving_peer_id, payload):
        plugin.log(f"cl-hive: MEMBER_LEFT sender mismatch: {peer_id[:16]}... != {leaving_peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    # Phase C: Persistent idempotency check
    is_new, event_id = check_and_record(database, "MEMBER_LEFT", payload, leaving_peer_id)
    if not is_new:
        plugin.log(f"cl-hive: MEMBER_LEFT duplicate event {event_id}, skipping", level='debug')
        _emit_ack(peer_id, event_id)
        _relay_message(HiveMessageType.MEMBER_LEFT, payload, peer_id)
        return {"result": "continue"}
    if event_id:
        payload["_event_id"] = event_id

    # Check if member exists
    member = database.get_member(leaving_peer_id)
    if not member:
        plugin.log(f"cl-hive: MEMBER_LEFT for unknown peer {leaving_peer_id[:16]}...", level='debug')
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

    # Remove the member
    tier = member.get("tier")
    database.remove_member(leaving_peer_id)
    plugin.log(f"cl-hive: Member {leaving_peer_id[:16]}... ({tier}) left the hive: {reason}")

    # Revert their fee policy to dynamic if bridge is available
    if bridge and bridge.status == BridgeStatus.ENABLED:
        try:
            bridge.set_hive_policy(leaving_peer_id, is_member=False)
        except Exception as e:
            plugin.log(f"cl-hive: Failed to revert policy for {leaving_peer_id[:16]}...: {e}", level='debug')

    # Check if hive is now headless (no full members)
    all_members = database.get_all_members()
    member_count = sum(1 for m in all_members if m.get("tier") == MembershipTier.MEMBER.value)
    if member_count == 0 and len(all_members) > 0:
        plugin.log("cl-hive: WARNING - Hive has no full members (only neophytes). Promote neophytes to restore governance.", level='warn')

    # Phase D: Acknowledge receipt
    _emit_ack(peer_id, payload.get("_event_id"))

    # Relay to other members
    _relay_message(HiveMessageType.MEMBER_LEFT, payload, peer_id)

    return {"result": "continue"}


# =============================================================================
# BAN VOTING CONSTANTS
# =============================================================================

# Message timestamp freshness limits (reject stale replayed messages)
MAX_GOSSIP_AGE_SECONDS = 3600           # 1 hour for gossip
MAX_INTENT_AGE_SECONDS = 600            # 10 minutes for intents (time-sensitive)
MAX_STATE_HASH_AGE_SECONDS = 3600       # 1 hour for state hash / full sync
MAX_SETTLEMENT_AGE_SECONDS = 86400      # 24 hours for settlement messages
MAX_INTELLIGENCE_AGE_SECONDS = 7200     # 2 hours for fee/health/liquidity reports
MAX_CLOCK_SKEW_SECONDS = 300            # 5 minutes future tolerance

# Ban proposal voting period (7 days)
BAN_PROPOSAL_TTL_SECONDS = 7 * 24 * 3600

# Quorum threshold for ban approval (51%)
BAN_QUORUM_THRESHOLD = 0.51

# Cooldown before re-proposing ban for same peer (7 days)
BAN_COOLDOWN_SECONDS = 7 * 24 * 3600


def handle_ban_proposal(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle BAN_PROPOSAL message - a member proposing to ban another member.

    Validates the proposal and stores it for voting.
    """
    if not config or not database :
        return {"result": "continue"}

    # Deduplication check
    if not _should_process_message(payload):
        return {"result": "continue"}

    if not validate_ban_proposal(payload):
        plugin.log(f"cl-hive: BAN_PROPOSAL from {peer_id[:16]}... invalid payload", level='warn')
        return {"result": "continue"}

    target_peer_id = payload["target_peer_id"]
    proposer_peer_id = payload["proposer_peer_id"]
    proposal_id = payload["proposal_id"]
    reason = payload["reason"]
    timestamp = payload["timestamp"]
    signature = payload["signature"]

    # Verify sender (supports relay)
    if not _validate_relay_sender(peer_id, proposer_peer_id, payload):
        plugin.log(f"cl-hive: BAN_PROPOSAL sender mismatch", level='warn')
        return {"result": "continue"}

    # Phase C: Persistent idempotency check
    is_new, event_id = check_and_record(database, "BAN_PROPOSAL", payload, proposer_peer_id)
    if not is_new:
        plugin.log(f"cl-hive: BAN_PROPOSAL duplicate event {event_id}, skipping", level='debug')
        _emit_ack(peer_id, event_id)
        _relay_message(HiveMessageType.BAN_PROPOSAL, payload, peer_id)
        return {"result": "continue"}
    if event_id:
        payload["_event_id"] = event_id

    # C-2 audit fix: Reject ban proposals from banned peers
    if database.is_banned(proposer_peer_id):
        plugin.log(f"cl-hive: BAN_PROPOSAL from banned member {proposer_peer_id[:16]}..., ignoring", level='warn')
        return {"result": "continue"}

    # H-4 audit fix: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_GOSSIP_AGE_SECONDS, "BAN_PROPOSAL"):
        return {"result": "continue"}

    # Verify proposer is a full member
    proposer = database.get_member(proposer_peer_id)
    if not proposer or proposer.get("tier") not in (MembershipTier.MEMBER.value,):
        plugin.log(f"cl-hive: BAN_PROPOSAL from non-member", level='warn')
        return {"result": "continue"}

    # Verify target is a member
    target = database.get_member(target_peer_id)
    if not target:
        plugin.log(f"cl-hive: BAN_PROPOSAL for non-member {target_peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Cannot ban yourself
    if target_peer_id == proposer_peer_id:
        return {"result": "continue"}

    # Verify signature
    canonical = f"hive:ban_proposal:{proposal_id}:{target_peer_id}:{timestamp}:{reason}"
    try:
        result = plugin.rpc.checkmessage(canonical, signature)
        if not result.get("verified") or result.get("pubkey") != proposer_peer_id:
            plugin.log(f"cl-hive: BAN_PROPOSAL signature invalid", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: BAN_PROPOSAL signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # Check if proposal already exists
    existing = database.get_ban_proposal(proposal_id)
    if existing:
        return {"result": "continue"}

    # H-5 audit fix: Enforce BAN_COOLDOWN_SECONDS for same target
    recent_proposal = database.get_ban_proposal_for_target(target_peer_id)
    if recent_proposal:
        recent_ts = recent_proposal.get("proposed_at", 0)
        if int(time.time()) - recent_ts < BAN_COOLDOWN_SECONDS:
            plugin.log(f"cl-hive: BAN_PROPOSAL cooldown active for {target_peer_id[:16]}...", level='info')
            return {"result": "continue"}

    # L-19 audit fix: Reject already-expired proposals
    expires_at = timestamp + BAN_PROPOSAL_TTL_SECONDS
    if expires_at < int(time.time()):
        plugin.log(f"cl-hive: BAN_PROPOSAL already expired, ignoring", level='debug')
        return {"result": "continue"}

    # Store proposal
    # R5-H-3 fix: Extract proposal_type from payload so settlement_gaming uses reversed voting
    proposal_type = payload.get("proposal_type", "standard")
    if proposal_type not in ("standard", "settlement_gaming"):
        proposal_type = "standard"  # Sanitize unexpected values
    database.create_ban_proposal(proposal_id, target_peer_id, proposer_peer_id,
                                 reason, timestamp, expires_at,
                                 proposal_type=proposal_type)
    plugin.log(f"cl-hive: Ban proposal {proposal_id[:16]}... for {target_peer_id[:16]}... by {proposer_peer_id[:16]}...")

    # Phase D: Acknowledge receipt
    _emit_ack(peer_id, payload.get("_event_id"))

    # Relay to other members
    _relay_message(HiveMessageType.BAN_PROPOSAL, payload, peer_id)

    return {"result": "continue"}


def handle_ban_vote(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle BAN_VOTE message - a member voting on a ban proposal.

    Validates the vote, stores it, and checks if quorum is reached.
    """
    if not config or not database or not plugin or not membership_mgr:
        return {"result": "continue"}

    # Deduplication check
    if not _should_process_message(payload):
        return {"result": "continue"}

    if not validate_ban_vote(payload):
        plugin.log(f"cl-hive: BAN_VOTE from {peer_id[:16]}... invalid payload", level='warn')
        return {"result": "continue"}

    proposal_id = payload["proposal_id"]
    voter_peer_id = payload["voter_peer_id"]
    vote = payload["vote"]  # "approve" or "reject"
    timestamp = payload["timestamp"]
    signature = payload["signature"]

    # Verify sender (supports relay)
    if not _validate_relay_sender(peer_id, voter_peer_id, payload):
        plugin.log(f"cl-hive: BAN_VOTE sender mismatch", level='warn')
        return {"result": "continue"}

    # Phase C: Persistent idempotency check
    is_new, event_id = check_and_record(database, "BAN_VOTE", payload, voter_peer_id)
    if not is_new:
        plugin.log(f"cl-hive: BAN_VOTE duplicate event {event_id}, skipping", level='debug')
        _emit_ack(peer_id, event_id)
        _relay_message(HiveMessageType.BAN_VOTE, payload, peer_id)
        return {"result": "continue"}
    if event_id:
        payload["_event_id"] = event_id

    # H-4 audit fix: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_GOSSIP_AGE_SECONDS, "BAN_VOTE"):
        return {"result": "continue"}

    # Verify voter is a full member and not banned
    voter = database.get_member(voter_peer_id)
    if not voter or voter.get("tier") not in (MembershipTier.MEMBER.value,):
        return {"result": "continue"}
    if database.is_banned(voter_peer_id):
        plugin.log(f"cl-hive: BAN_VOTE from banned member {voter_peer_id[:16]}..., ignoring", level='warn')
        return {"result": "continue"}

    # Get the proposal
    proposal = database.get_ban_proposal(proposal_id)
    if not proposal or proposal.get("status") != "pending":
        return {"result": "continue"}

    # R5-M-7 fix: Reject votes on expired proposals
    if proposal.get("expires_at") and proposal["expires_at"] < int(time.time()):
        plugin.log(f"cl-hive: BAN_VOTE on expired proposal {proposal_id[:16]}...", level='info')
        return {"result": "continue"}

    # H-6 audit fix: Ban target cannot vote on their own ban
    if voter_peer_id == proposal.get("target_peer_id"):
        plugin.log(f"cl-hive: BAN_VOTE target voting on own ban, ignoring", level='warn')
        return {"result": "continue"}

    # Verify signature
    canonical = f"hive:ban_vote:{proposal_id}:{vote}:{timestamp}"
    try:
        result = plugin.rpc.checkmessage(canonical, signature)
        if not result.get("verified") or result.get("pubkey") != voter_peer_id:
            plugin.log(f"cl-hive: BAN_VOTE signature invalid", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: BAN_VOTE signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # Store vote
    database.add_ban_vote(proposal_id, voter_peer_id, vote, timestamp, signature)
    plugin.log(f"cl-hive: Ban vote from {voter_peer_id[:16]}... on {proposal_id[:16]}...: {vote}")

    # Check if quorum reached
    _check_ban_quorum(proposal_id, proposal, plugin)

    # Phase D: Acknowledge receipt + implicit ack (BAN_VOTE implies BAN_PROPOSAL received)
    _emit_ack(peer_id, payload.get("_event_id"))
    if outbox_mgr:
        outbox_mgr.process_implicit_ack(peer_id, HiveMessageType.BAN_VOTE, payload)

    # Relay to other members
    _relay_message(HiveMessageType.BAN_VOTE, payload, peer_id)

    return {"result": "continue"}


def _check_ban_quorum(proposal_id: str, proposal: Dict, plugin: Plugin) -> bool:
    """
    Check if a ban proposal has reached quorum and execute if so.

    Returns True if ban was executed.
    """
    if not database or not membership_mgr or not bridge:
        return False

    target_peer_id = proposal["target_peer_id"]
    proposal_type = proposal.get("proposal_type", "standard")

    # Get all votes
    votes = database.get_ban_votes(proposal_id)

    # Get eligible voters (members, excluding target, banned, and inactive)
    all_members = database.get_all_members()
    activity_cutoff = int(time.time()) - 7 * 86400  # 7 days
    eligible_voters = [
        m for m in all_members
        if m.get("tier") in (MembershipTier.MEMBER.value,)
        and m["peer_id"] != target_peer_id
        and not database.is_banned(m["peer_id"])
        and (m.get("last_seen") or 0) >= activity_cutoff
    ]
    eligible_count = len(eligible_voters)

    if eligible_count == 0:
        return False

    eligible_voter_ids = set(m["peer_id"] for m in eligible_voters)

    # Count votes from eligible voters
    approve_count = sum(
        1 for v in votes
        if v["vote"] == "approve" and v["voter_peer_id"] in eligible_voter_ids
    )
    reject_count = sum(
        1 for v in votes
        if v["vote"] == "reject" and v["voter_peer_id"] in eligible_voter_ids
    )

    # Determine if ban should execute based on proposal type
    should_execute = False

    if proposal_type == "settlement_gaming":
        # REVERSED VOTING: Non-participation = approve (yes to ban)
        # Members must actively vote "reject" (no) to defend the accused
        # Ban executes if less than 51% vote "reject"
        # P5-C-1 fix: Only count non-voters as approvals AFTER voting window expires
        reject_threshold = int(eligible_count * BAN_QUORUM_THRESHOLD) + 1
        proposal_timestamp = proposal.get("proposed_at", proposal.get("timestamp", 0))
        voting_window_expired = time.time() - proposal_timestamp >= BAN_PROPOSAL_TTL_SECONDS

        if voting_window_expired:
            # Window expired: non-voters are implicit approvals
            implicit_approvals = eligible_count - reject_count - approve_count
            total_approvals = approve_count + implicit_approvals

            if reject_count < reject_threshold:
                # Not enough members defended the accused - ban executes
                should_execute = True
                plugin.log(
                    f"cl-hive: Settlement gaming ban - {reject_count} reject votes "
                    f"(needed {reject_threshold} to prevent), {implicit_approvals} non-voters counted as approve"
                )
        else:
            # Window still open: can only execute if enough explicit reject votes
            # make it impossible to block (i.e., even if all remaining voters reject,
            # they can't reach threshold). Otherwise, wait for window to expire.
            remaining_voters = eligible_count - reject_count - approve_count
            if reject_count + remaining_voters < reject_threshold:
                # Mathematically impossible to reach reject threshold - execute early
                should_execute = True
                plugin.log(
                    f"cl-hive: Settlement gaming ban (early) - {reject_count} reject votes, "
                    f"{remaining_voters} remaining, threshold={reject_threshold} unreachable"
                )
    else:
        # STANDARD VOTING: Need 51% explicit approve votes
        quorum_needed = int(eligible_count * BAN_QUORUM_THRESHOLD) + 1
        if approve_count >= quorum_needed:
            should_execute = True

    if should_execute:
        # Execute ban
        database.update_ban_proposal_status(proposal_id, "approved")
        proposer_id = proposal.get("proposer_peer_id", "quorum_vote")
        database.add_ban(target_peer_id, proposal.get("reason", "quorum_ban"), proposer_id)

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

        vote_info = f"reject={reject_count}" if proposal_type == "settlement_gaming" else f"approve={approve_count}"
        plugin.log(f"cl-hive: Ban executed for {target_peer_id[:16]}... ({vote_info}/{eligible_count} votes)")

        # Broadcast BAN message
        ban_payload = {
            "peer_id": target_peer_id,
            "reason": proposal.get("reason", "quorum_ban"),
            "proposal_id": proposal_id
        }
        ban_msg = serialize(HiveMessageType.BAN, ban_payload)
        _broadcast_to_members(ban_msg)

        return True

    return False


def handle_ban(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle BAN message - notification that a ban has been executed.

    BAN is broadcast by the node that first reaches quorum in _check_ban_quorum.
    Most nodes will have already executed the ban independently when they tallied
    enough BAN_VOTEs.  This handler acts as a catch-up mechanism: if this node
    missed some votes and hasn't banned the target yet, we enforce it now.

    The handler is intentionally lightweight - add_ban is idempotent (returns
    False if the peer is already banned).
    """
    if not database:
        return {"status": "ignored", "reason": "not_initialised"}

    target_peer_id = payload.get("peer_id")
    reason = payload.get("reason", "quorum_ban")
    proposal_id = payload.get("proposal_id")

    if not target_peer_id:
        plugin.log("cl-hive: BAN message missing peer_id", level='warn')
        return {"status": "ignored", "reason": "missing_peer_id"}

    # Already banned — nothing to do
    if database.is_banned(target_peer_id):
        plugin.log(f"cl-hive: BAN notification for already-banned {target_peer_id[:16]}...", level='debug')
        return {"status": "already_banned"}

    # Enforce the ban
    database.add_ban(target_peer_id, reason, peer_id)

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

    plugin.log(f"cl-hive: BAN catch-up executed for {target_peer_id[:16]}... (proposal={proposal_id})")

    if proposal_id:
        database.update_ban_proposal_status(proposal_id, "approved")

    return {"status": "banned", "peer_id": target_peer_id}


# =============================================================================
# PHASE 6: CHANNEL COORDINATION - PEER AVAILABLE HANDLING
# =============================================================================

def handle_peer_available(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle PEER_AVAILABLE message - a hive member reporting a channel event.

    This is sent when:
    - A channel opens (local or remote initiated)
    - A channel closes (any type)
    - A peer's routing quality is exceptional

    Phase 6.1: ALL events are stored in peer_events table for topology intelligence.
    The receiving node uses this data to make informed expansion decisions.

    SECURITY: Requires cryptographic signature verification.
    """
    if not config or not database:
        return {"result": "continue"}

    if not validate_peer_available(payload):
        plugin.log(f"cl-hive: PEER_AVAILABLE from {peer_id[:16]}... invalid payload", level='warn')
        return {"result": "continue"}

    # SECURITY: Verify cryptographic signature
    reporter_peer_id = payload.get("reporter_peer_id")
    signature = payload.get("signature")
    signing_payload = get_peer_available_signing_payload(payload)

    try:
        result = plugin.rpc.checkmessage(signing_payload, signature)
        if not result.get("verified") or result.get("pubkey") != reporter_peer_id:
            plugin.log(
                f"cl-hive: PEER_AVAILABLE signature invalid from {peer_id[:16]}...",
                level='warn'
            )
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: PEER_AVAILABLE signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # SECURITY: Verify reporter matches peer_id (prevent relay attacks)
    if reporter_peer_id != peer_id:
        plugin.log(
            f"cl-hive: PEER_AVAILABLE reporter mismatch: claimed {reporter_peer_id[:16]}... but peer is {peer_id[:16]}...",
            level='warn'
        )
        return {"result": "continue"}

    # Verify sender is a hive member and not banned
    sender = database.get_member(peer_id)
    if not sender or database.is_banned(peer_id):
        plugin.log(f"cl-hive: PEER_AVAILABLE from non-member {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Apply rate limiting to prevent gossip flooding (Security Enhancement)
    if peer_available_limiter and not peer_available_limiter.is_allowed(peer_id):
        plugin.log(
            f"cl-hive: PEER_AVAILABLE from {peer_id[:16]}... rate limited (>10/min)",
            level='warn'
        )
        return {"result": "continue"}

    # Extract all fields from payload
    target_peer_id = payload["target_peer_id"]
    reporter_peer_id = payload["reporter_peer_id"]
    event_type = payload["event_type"]
    timestamp = payload["timestamp"]

    # Channel info
    channel_id = payload.get("channel_id", "")
    capacity_sats = payload.get("capacity_sats", 0)

    # Profitability data
    duration_days = payload.get("duration_days", 0)
    total_revenue_sats = payload.get("total_revenue_sats", 0)
    total_rebalance_cost_sats = payload.get("total_rebalance_cost_sats", 0)
    net_pnl_sats = payload.get("net_pnl_sats", 0)
    forward_count = payload.get("forward_count", 0)
    forward_volume_sats = payload.get("forward_volume_sats", 0)
    our_fee_ppm = payload.get("our_fee_ppm", 0)
    their_fee_ppm = payload.get("their_fee_ppm", 0)
    routing_score = payload.get("routing_score", 0.5)
    profitability_score = payload.get("profitability_score", 0.5)

    # Funding info
    our_funding_sats = payload.get("our_funding_sats", 0)
    their_funding_sats = payload.get("their_funding_sats", 0)
    opener = payload.get("opener", "")
    closer = payload.get("closer", "")
    reason = payload.get("reason", "")

    # Determine closer from event_type if not explicitly set
    if not closer and event_type.endswith('_close'):
        if event_type == 'remote_close':
            closer = 'remote'
        elif event_type == 'local_close':
            closer = 'local'
        elif event_type == 'mutual_close':
            closer = 'mutual'

    plugin.log(
        f"cl-hive: PEER_AVAILABLE from {reporter_peer_id[:16]}...: "
        f"target={target_peer_id[:16]}... event={event_type} "
        f"capacity={capacity_sats} pnl={net_pnl_sats}",
        level='info'
    )

    # =========================================================================
    # PHASE 6.1: Store ALL events for topology intelligence
    # =========================================================================
    database.store_peer_event(
        peer_id=target_peer_id,
        reporter_id=reporter_peer_id,
        event_type=event_type,
        timestamp=timestamp,
        channel_id=channel_id,
        capacity_sats=capacity_sats,
        duration_days=duration_days,
        total_revenue_sats=total_revenue_sats,
        total_rebalance_cost_sats=total_rebalance_cost_sats,
        net_pnl_sats=net_pnl_sats,
        forward_count=forward_count,
        forward_volume_sats=forward_volume_sats,
        our_fee_ppm=our_fee_ppm,
        their_fee_ppm=their_fee_ppm,
        routing_score=routing_score,
        profitability_score=profitability_score,
        our_funding_sats=our_funding_sats,
        their_funding_sats=their_funding_sats,
        opener=opener,
        closer=closer,
        reason=reason
    )

    # =========================================================================
    # Evaluate expansion opportunities (only for close events)
    # =========================================================================
    # Channel opens are informational only - no action needed
    if event_type == 'channel_open':
        return {"result": "continue"}

    # Don't open channels to ourselves
    if plugin:
        try:
            our_id = plugin.rpc.getinfo().get("id")
            if target_peer_id == our_id:
                return {"result": "continue"}
        except Exception:
            pass

    # Check if we already have a channel to this peer
    if plugin:
        try:
            channels = plugin.rpc.listpeerchannels(id=target_peer_id)
            if channels.get("channels"):
                plugin.log(
                    f"cl-hive: Already have channel to {target_peer_id[:16]}..., "
                    f"event stored for topology tracking",
                    level='debug'
                )
                return {"result": "continue"}
        except Exception:
            pass  # Peer not connected, which is fine

    # Check if target is in the ban list
    if database.is_banned(target_peer_id):
        plugin.log(f"cl-hive: Ignoring expansion to banned peer {target_peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Only consider expansion for remote-initiated closures
    # (local/mutual closes don't indicate the peer wants more channels)
    if event_type != 'remote_close':
        return {"result": "continue"}

    # Check quality thresholds before proposing expansion
    if routing_score < 0.2:
        plugin.log(
            f"cl-hive: Peer {target_peer_id[:16]}... has low routing score ({routing_score}), "
            f"not proposing expansion",
            level='debug'
        )
        return {"result": "continue"}

    cfg = config.snapshot()

    if not cfg.planner_enable_expansions:
        plugin.log(
            f"cl-hive: Expansions disabled, storing PEER_AVAILABLE for manual review",
            level='debug'
        )
        _store_peer_available_action(target_peer_id, reporter_peer_id, event_type,
                                     capacity_sats, routing_score, reason)
        return {"result": "continue"}

    # Check if on-chain feerates are low enough for channel opening
    feerate_allowed, current_feerate, feerate_reason = _check_feerate_for_expansion(
        cfg.max_expansion_feerate_perkb
    )
    if not feerate_allowed:
        plugin.log(
            f"cl-hive: On-chain fees too high for expansion ({feerate_reason}), "
            f"storing PEER_AVAILABLE for later when fees drop",
            level='info'
        )
        _store_peer_available_action(target_peer_id, reporter_peer_id, event_type,
                                     capacity_sats, routing_score,
                                     f"Deferred: {feerate_reason}")
        return {"result": "continue"}

    # =========================================================================
    # Phase 6.4: Trigger cooperative expansion round
    # =========================================================================
    if coop_expansion:
        # Start a cooperative expansion round for this peer
        round_id = coop_expansion.evaluate_expansion(
            target_peer_id=target_peer_id,
            event_type=event_type,
            reporter_id=reporter_peer_id,
            capacity_sats=capacity_sats,
            quality_score=profitability_score  # Use reported profitability as hint
        )

        if round_id:
            plugin.log(
                f"cl-hive: Started cooperative expansion round {round_id[:8]}... "
                f"for {target_peer_id[:16]}...",
                level='info'
            )
            # Broadcast our nomination to other hive members
            _broadcast_expansion_nomination(round_id, target_peer_id)
        else:
            plugin.log(
                f"cl-hive: No cooperative round started for {target_peer_id[:16]}... "
                f"(may be on cooldown or insufficient quality)",
                level='debug'
            )
    else:
        # Fallback: Store pending action for review
        if cfg.governance_mode in ('advisor', 'failsafe'):
            _store_peer_available_action(target_peer_id, reporter_peer_id, event_type,
                                         capacity_sats, routing_score, reason)
            plugin.log(
                f"cl-hive: Queued channel opportunity to {target_peer_id[:16]}... from PEER_AVAILABLE",
                level='info'
            )

    return {"result": "continue"}


def _check_feerate_for_expansion(max_feerate_perkb: int) -> tuple:
    """
    Check if current on-chain feerates allow channel expansion.

    Args:
        max_feerate_perkb: Maximum feerate threshold in sat/kB (0 = disabled)

    Returns:
        Tuple of (allowed: bool, current_feerate: int, reason: str)
    """
    if max_feerate_perkb == 0:
        return (True, 0, "feerate check disabled")

    if not plugin:
        return (False, 0, "plugin not initialized")

    try:
        feerates = plugin.rpc.feerates("perkb")
        # Use 'opening' feerate which is what fundchannel uses
        opening_feerate = feerates.get("perkb", {}).get("opening")

        if opening_feerate is None:
            # Fallback to min_acceptable if opening not available
            opening_feerate = feerates.get("perkb", {}).get("min_acceptable", 0)

        if opening_feerate == 0:
            return (True, 0, "feerate unavailable, allowing")

        if opening_feerate <= max_feerate_perkb:
            return (True, opening_feerate, "feerate acceptable")
        else:
            return (False, opening_feerate, f"feerate {opening_feerate} > max {max_feerate_perkb}")
    except Exception as e:
        # On error, be conservative and allow (don't block on RPC issues)
        return (True, 0, f"feerate check error: {e}")


def _parse_amount_msat(val) -> int:
    """Safely parse amount_msat from CLN (int or 'NNNmsat' string)."""
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        try:
            cleaned = val.replace('msat', '') if val.endswith('msat') else val
            return int(cleaned)
        except (ValueError, TypeError):
            return 0
    return 0


def _get_spendable_balance(cfg) -> int:
    """
    Get onchain balance minus reserve, or 0 if unavailable.

    This is the amount available for channel opens after accounting for
    the configured reserve percentage.

    Args:
        cfg: Config snapshot with budget_reserve_pct

    Returns:
        Spendable balance in sats, or 0 if unavailable
    """
    if not plugin:
        return 0
    try:
        funds = plugin.rpc.listfunds()
        outputs = funds.get('outputs', [])
        onchain_balance = sum(
            _parse_amount_msat(o.get('amount_msat', 0)) // 1000
            for o in outputs if o.get('status') == 'confirmed'
        )
        return int(onchain_balance * (1.0 - cfg.budget_reserve_pct))
    except Exception:
        return 0


def _cap_channel_size_to_budget(size_sats: int, cfg, context: str = "") -> tuple:
    """
    Cap channel size to available budget.

    Ensures proposed channel sizes don't exceed what we can actually afford.

    Args:
        size_sats: Proposed channel size
        cfg: Config snapshot
        context: Optional context string for logging

    Returns:
        Tuple of (capped_size, was_insufficient, was_capped)
        - capped_size: Final size (0 if insufficient funds)
        - was_insufficient: True if we can't afford minimum channel
        - was_capped: True if size was reduced to fit budget
    """
    spendable = _get_spendable_balance(cfg)

    # Enforce configured maximum channel size
    was_capped = False
    if size_sats > cfg.planner_max_channel_sats:
        size_sats = cfg.planner_max_channel_sats
        was_capped = True

    # Check if we can afford minimum channel size
    if spendable < cfg.planner_min_channel_sats:
        if context and plugin:
            plugin.log(
                f"cl-hive: {context}: insufficient funds "
                f"({spendable:,} < {cfg.planner_min_channel_sats:,} min)",
                level='debug'
            )
        return (0, True, False)

    # Cap to what we can afford
    if size_sats > spendable:
        if context and plugin:
            plugin.log(
                f"cl-hive: {context}: capping channel size from {size_sats:,} to {spendable:,}",
                level='info'
            )
        return (spendable, False, True)

    return (size_sats, False, was_capped)


def _store_peer_available_action(target_peer_id: str, reporter_peer_id: str,
                                  event_type: str, capacity_sats: int,
                                  routing_score: float, reason: str) -> None:
    """Store a PEER_AVAILABLE as a pending action for review/execution."""
    if not database:
        return

    cfg = config.snapshot() if config else None
    if not cfg:
        return

    # Determine suggested channel size
    suggested_sats = capacity_sats
    if capacity_sats == 0:
        suggested_sats = cfg.planner_default_channel_sats

    # Check affordability and cap to available budget
    capped_size, insufficient, was_capped = _cap_channel_size_to_budget(
        suggested_sats, cfg, context=f"PEER_AVAILABLE to {target_peer_id[:16]}..."
    )

    # Skip if we can't afford minimum channel
    if insufficient:
        if plugin:
            plugin.log(
                f"cl-hive: Skipping PEER_AVAILABLE action for {target_peer_id[:16]}...: "
                f"insufficient funds for minimum channel",
                level='info'
            )
        return

    database.add_pending_action(
        action_type="channel_open",
        payload={
            "target": target_peer_id,
            "amount_sats": capped_size,
            "original_amount_sats": suggested_sats if was_capped else None,
            "source": "peer_available",
            "reporter": reporter_peer_id,
            "event_type": event_type,
            "routing_score": routing_score,
            "reason": reason or f"Peer available via {event_type}",
            "budget_capped": was_capped,
        },
        expires_hours=24
    )


def broadcast_peer_available(target_peer_id: str, event_type: str,
                              channel_id: str = "",
                              capacity_sats: int = 0,
                              routing_score: float = 0.0,
                              profitability_score: float = 0.0,
                              reason: str = "",
                              # Profitability data
                              duration_days: int = 0,
                              total_revenue_sats: int = 0,
                              total_rebalance_cost_sats: int = 0,
                              net_pnl_sats: int = 0,
                              forward_count: int = 0,
                              forward_volume_sats: int = 0,
                              our_fee_ppm: int = 0,
                              their_fee_ppm: int = 0,
                              # Funding info (for opens)
                              our_funding_sats: int = 0,
                              their_funding_sats: int = 0,
                              opener: str = "") -> int:
    """
    Broadcast signed PEER_AVAILABLE to all hive members.

    SECURITY: All PEER_AVAILABLE messages are cryptographically signed.

    Args:
        target_peer_id: The external peer involved
        event_type: 'channel_open', 'channel_close', 'remote_close', etc.
        channel_id: The channel short ID
        capacity_sats: Channel capacity
        routing_score: Peer's routing quality score (0-1)
        profitability_score: Overall profitability score (0-1)
        reason: Human-readable reason

        # Profitability data (for closures):
        duration_days, total_revenue_sats, total_rebalance_cost_sats,
        net_pnl_sats, forward_count, forward_volume_sats,
        our_fee_ppm, their_fee_ppm

        # Funding info (for opens):
        our_funding_sats, their_funding_sats, opener

    Returns:
        Number of members message was sent to
    """
    if not plugin or not database:
        return 0

    try:
        our_id = plugin.rpc.getinfo().get("id")
    except Exception:
        return 0

    timestamp = int(time.time())

    # Build payload for signing
    signing_payload_dict = {
        "target_peer_id": target_peer_id,
        "reporter_peer_id": our_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "capacity_sats": capacity_sats,
    }

    # Sign the payload
    signing_str = get_peer_available_signing_payload(signing_payload_dict)
    try:
        sig_result = plugin.rpc.signmessage(signing_str)
        signature = sig_result['zbase']
    except Exception as e:
        plugin.log(f"cl-hive: Failed to sign PEER_AVAILABLE: {e}", level='error')
        return 0

    msg = create_peer_available(
        target_peer_id=target_peer_id,
        reporter_peer_id=our_id,
        event_type=event_type,
        timestamp=timestamp,
        signature=signature,
        channel_id=channel_id,
        capacity_sats=capacity_sats,
        routing_score=routing_score,
        profitability_score=profitability_score,
        reason=reason,
        duration_days=duration_days,
        total_revenue_sats=total_revenue_sats,
        total_rebalance_cost_sats=total_rebalance_cost_sats,
        net_pnl_sats=net_pnl_sats,
        forward_count=forward_count,
        forward_volume_sats=forward_volume_sats,
        our_fee_ppm=our_fee_ppm,
        their_fee_ppm=their_fee_ppm,
        our_funding_sats=our_funding_sats,
        their_funding_sats=their_funding_sats,
        opener=opener
    )

    return _broadcast_to_members(msg)


def _broadcast_expansion_nomination(round_id: str, target_peer_id: str) -> int:
    """
    Broadcast an EXPANSION_NOMINATE message to all hive members.

    Args:
        round_id: The cooperative expansion round ID
        target_peer_id: The target peer for the expansion

    Returns:
        Number of members message was sent to
    """
    if not plugin or not database or not coop_expansion:
        return 0

    try:
        our_id = plugin.rpc.getinfo().get("id")
    except Exception:
        return 0

    # Get our nomination info
    try:
        funds = plugin.rpc.listfunds()
        outputs = funds.get('outputs', [])
        available_liquidity = sum(
            _parse_amount_msat(o.get('amount_msat', 0)) // 1000
            for o in outputs if o.get('status') == 'confirmed'
        )
    except Exception:
        available_liquidity = 0

    try:
        channels = plugin.rpc.listpeerchannels()
        channel_count = len(channels.get('channels', []))
    except Exception:
        channel_count = 0

    # Check if we have a channel to target
    try:
        target_channels = plugin.rpc.listpeerchannels(id=target_peer_id)
        has_existing = len(target_channels.get('channels', [])) > 0
    except Exception:
        has_existing = False

    # Get quality score for the target
    quality_score = 0.5
    if database:
        try:
            scorer = PeerQualityScorer(database, plugin)
            result = scorer.calculate_score(target_peer_id)
            quality_score = result.overall_score
        except Exception:
            pass

    import time
    timestamp = int(time.time())

    # Build payload for signing (SECURITY: sign before sending)
    signing_payload = {
        "round_id": round_id,
        "target_peer_id": target_peer_id,
        "nominator_id": our_id,
        "timestamp": timestamp,
        "available_liquidity_sats": available_liquidity,
        "quality_score": quality_score,
        "has_existing_channel": has_existing,
        "channel_count": channel_count,
    }
    signing_message = get_expansion_nominate_signing_payload(signing_payload)

    # Sign the message with our node key
    try:
        sig_result = plugin.rpc.signmessage(signing_message)
        signature = sig_result['zbase']
    except Exception as e:
        plugin.log(f"cl-hive: Failed to sign nomination: {e}", level='error')
        return 0

    msg = create_expansion_nominate(
        round_id=round_id,
        target_peer_id=target_peer_id,
        nominator_id=our_id,
        timestamp=timestamp,
        signature=signature,
        available_liquidity_sats=available_liquidity,
        quality_score=quality_score,
        has_existing_channel=has_existing,
        channel_count=channel_count,
        reason="auto_nominate"
    )

    sent = _broadcast_to_members(msg)
    plugin.log(
        f"cl-hive: [BROADCAST] Sent signed nomination for round {round_id[:8]}... "
        f"target={target_peer_id[:16]}... to {sent} members",
        level='info'
    )

    return sent


def _broadcast_expansion_elect(round_id: str, target_peer_id: str, elected_id: str,
                                channel_size_sats: int = 0, quality_score: float = 0.5,
                                nomination_count: int = 0) -> int:
    """
    Broadcast an EXPANSION_ELECT message to all hive members.

    SECURITY: The message is signed by the coordinator (us) to prevent
    election spoofing by malicious hive members.

    Args:
        round_id: The cooperative expansion round ID
        target_peer_id: The target peer for the expansion
        elected_id: The elected member who should open the channel
        channel_size_sats: Recommended channel size
        quality_score: Target's quality score
        nomination_count: Number of nominations received

    Returns:
        Number of members message was sent to
    """
    if not plugin or not database:
        return 0

    try:
        coordinator_id = plugin.rpc.getinfo().get("id")
    except Exception:
        return 0

    import time
    timestamp = int(time.time())

    # Build payload for signing (SECURITY: sign before sending)
    signing_payload = {
        "round_id": round_id,
        "target_peer_id": target_peer_id,
        "elected_id": elected_id,
        "coordinator_id": coordinator_id,
        "timestamp": timestamp,
        "channel_size_sats": channel_size_sats,
        "quality_score": quality_score,
        "nomination_count": nomination_count,
    }
    signing_message = get_expansion_elect_signing_payload(signing_payload)

    # Sign the message with our node key
    try:
        sig_result = plugin.rpc.signmessage(signing_message)
        signature = sig_result['zbase']
    except Exception as e:
        plugin.log(f"cl-hive: Failed to sign election: {e}", level='error')
        return 0

    msg = create_expansion_elect(
        round_id=round_id,
        target_peer_id=target_peer_id,
        elected_id=elected_id,
        coordinator_id=coordinator_id,
        timestamp=timestamp,
        signature=signature,
        channel_size_sats=channel_size_sats,
        quality_score=quality_score,
        nomination_count=nomination_count,
        reason="elected_by_coordinator"
    )

    sent = _broadcast_to_members(msg)
    if sent > 0:
        plugin.log(
            f"cl-hive: Broadcast signed expansion election for round {round_id[:8]}... "
            f"elected={elected_id[:16]}... to {sent} members",
            level='info'
        )

    return sent


def _broadcast_expansion_decline(round_id: str, reason: str) -> int:
    """
    Broadcast an EXPANSION_DECLINE message to all hive members (Phase 8).

    Called when we (the elected member) cannot open the channel due to
    insufficient funds, high feerate, or other reasons. This triggers
    fallback to the next ranked candidate.

    SECURITY: The message is signed by the decliner (us) to prevent
    spoofing decline messages.

    Args:
        round_id: The cooperative expansion round ID
        reason: Why we're declining (insufficient_funds, feerate_high, etc.)

    Returns:
        Number of members message was sent to
    """
    if not plugin or not database:
        return 0

    try:
        decliner_id = plugin.rpc.getinfo().get("id")
    except Exception:
        return 0

    import time
    timestamp = int(time.time())

    # Build payload for signing (SECURITY: sign before sending)
    signing_payload = {
        "round_id": round_id,
        "decliner_id": decliner_id,
        "reason": reason,
        "timestamp": timestamp,
    }
    signing_message = get_expansion_decline_signing_payload(signing_payload)

    # Sign the message with our node key
    try:
        sig_result = plugin.rpc.signmessage(signing_message)
        signature = sig_result['zbase']
    except Exception as e:
        plugin.log(f"cl-hive: Failed to sign decline: {e}", level='error')
        return 0

    msg = create_expansion_decline(
        round_id=round_id,
        decliner_id=decliner_id,
        reason=reason,
        timestamp=timestamp,
        signature=signature,
    )

    sent = _broadcast_to_members(msg)
    if sent > 0:
        plugin.log(
            f"cl-hive: Broadcast expansion decline for round {round_id[:8]}... "
            f"(reason={reason}) to {sent} members",
            level='info'
        )

    return sent


def handle_expansion_nominate(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle EXPANSION_NOMINATE message from another hive member.

    This message indicates a member is interested in opening a channel
    to a target peer during a cooperative expansion round.

    SECURITY: Verifies cryptographic signature from the nominator.
    """
    plugin.log(
        f"cl-hive: [NOMINATE] Received from {peer_id[:16]}... "
        f"round={payload.get('round_id', '')[:8]}... "
        f"nominator={payload.get('nominator_id', '')[:16]}...",
        level='info'
    )

    if not coop_expansion or not database:
        plugin.log("cl-hive: [NOMINATE] coop_expansion or database not initialized", level='warn')
        return {"result": "continue"}

    if not validate_expansion_nominate(payload):
        plugin.log(f"cl-hive: [NOMINATE] Invalid payload from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    # Verify sender is a hive member and not banned
    sender = database.get_member(peer_id)
    if not sender or database.is_banned(peer_id):
        plugin.log(f"cl-hive: [NOMINATE] Rejected - {peer_id[:16]}... not a member or banned", level='info')
        return {"result": "continue"}

    # SECURITY: Verify the cryptographic signature
    nominator_id = payload.get("nominator_id", "")
    signature = payload.get("signature", "")
    signing_message = get_expansion_nominate_signing_payload(payload)

    try:
        verify_result = plugin.rpc.checkmessage(signing_message, signature)
        if not verify_result.get("verified", False):
            plugin.log(
                f"cl-hive: [NOMINATE] Signature verification failed for {nominator_id[:16]}...",
                level='warn'
            )
            return {"result": "continue"}
        # Verify the signature is from the claimed nominator
        recovered_pubkey = verify_result.get("pubkey", "")
        if recovered_pubkey != nominator_id:
            plugin.log(
                f"cl-hive: [NOMINATE] Signature mismatch: claimed={nominator_id[:16]}... "
                f"actual={recovered_pubkey[:16]}...",
                level='warn'
            )
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: [NOMINATE] Signature verification error: {e}", level='warn')
        return {"result": "continue"}

    # Process the nomination
    result = coop_expansion.handle_nomination(peer_id, payload)

    plugin.log(
        f"cl-hive: [NOMINATE] Processed: success={result.get('success')}, "
        f"joined={result.get('joined')}, round={result.get('round_id', '')[:8]}...",
        level='info'
    )

    # If we joined a new round and added our nomination, broadcast it to other members
    # This ensures all members' nominations propagate across the network
    if result.get('joined') and result.get('success'):
        round_id = result.get('round_id', '')
        target_peer_id = payload.get('target_peer_id', '')
        if round_id and target_peer_id:
            plugin.log(
                f"cl-hive: [NOMINATE] Re-broadcasting our nomination for round {round_id[:8]}...",
                level='info'
            )
            _broadcast_expansion_nomination(round_id, target_peer_id)

    return {"result": "continue", "nomination_result": result}


def handle_expansion_elect(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle EXPANSION_ELECT message announcing the winner of an expansion round.

    If we are the elected member, we should proceed to open the channel.

    SECURITY: Verifies cryptographic signature from the coordinator.
    """
    if not coop_expansion or not database:
        return {"result": "continue"}

    if not validate_expansion_elect(payload):
        plugin.log(f"cl-hive: Invalid EXPANSION_ELECT from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    # Verify sender is a hive member and not banned
    sender = database.get_member(peer_id)
    if not sender or database.is_banned(peer_id):
        plugin.log(f"cl-hive: EXPANSION_ELECT from non-member {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # SECURITY: Verify the cryptographic signature from coordinator
    coordinator_id = payload.get("coordinator_id", "")
    signature = payload.get("signature", "")
    signing_message = get_expansion_elect_signing_payload(payload)

    try:
        verify_result = plugin.rpc.checkmessage(signing_message, signature)
        if not verify_result.get("verified", False):
            plugin.log(
                f"cl-hive: [ELECT] Signature verification failed for coordinator {coordinator_id[:16]}...",
                level='warn'
            )
            return {"result": "continue"}
        # Verify the signature is from the claimed coordinator
        recovered_pubkey = verify_result.get("pubkey", "")
        if recovered_pubkey != coordinator_id:
            plugin.log(
                f"cl-hive: [ELECT] Signature mismatch: claimed={coordinator_id[:16]}... "
                f"actual={recovered_pubkey[:16]}...",
                level='warn'
            )
            return {"result": "continue"}
        # Verify the coordinator is a hive member
        coordinator_member = database.get_member(coordinator_id)
        if not coordinator_member or database.is_banned(coordinator_id):
            plugin.log(
                f"cl-hive: [ELECT] Coordinator {coordinator_id[:16]}... not a member or banned",
                level='warn'
            )
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: [ELECT] Signature verification error: {e}", level='warn')
        return {"result": "continue"}

    plugin.log(
        f"cl-hive: [ELECT] Verified election from coordinator {coordinator_id[:16]}...",
        level='debug'
    )

    # Process the election
    result = coop_expansion.handle_elect(peer_id, payload)

    elected_id = payload.get("elected_id", "")
    target_peer_id = payload.get("target_peer_id", "")
    channel_size = payload.get("channel_size_sats", 0)

    # Check if we were elected
    if result.get("action") == "open_channel":
        plugin.log(
            f"cl-hive: We were elected to open channel to {target_peer_id[:16]}... "
            f"(size={channel_size})",
            level='info'
        )

        # Queue the channel open via pending actions
        if database and config:
            cfg = config.snapshot()
            proposed_size = channel_size or cfg.planner_default_channel_sats

            # Check affordability before queuing
            capped_size, insufficient, was_capped = _cap_channel_size_to_budget(
                proposed_size, cfg, f"EXPANSION_ELECT for {target_peer_id[:16]}..."
            )
            if insufficient:
                plugin.log(
                    f"cl-hive: [ELECT] Declining election: insufficient funds to open channel "
                    f"(proposed={proposed_size}, min={cfg.planner_min_channel_sats})",
                    level='info'
                )
                # Phase 8: Broadcast decline to trigger fallback
                round_id = payload.get("round_id", "")
                if round_id:
                    _broadcast_expansion_decline(round_id, "insufficient_funds")
                return {"result": "declined", "reason": "insufficient_funds"}
            if was_capped:
                plugin.log(
                    f"cl-hive: [ELECT] Capping channel size from {proposed_size} to {capped_size}",
                    level='info'
                )

            action_id = database.add_pending_action(
                action_type="channel_open",
                payload={
                    "target": target_peer_id,
                    "amount_sats": capped_size,
                    "source": "cooperative_expansion",
                    "round_id": payload.get("round_id", ""),
                    "reason": "Elected by hive for cooperative expansion"
                },
                expires_hours=24
            )
            plugin.log(f"cl-hive: Queued channel open to {target_peer_id[:16]}... (action_id={action_id})", level='info')
    else:
        plugin.log(
            f"cl-hive: {elected_id[:16]}... elected for round {payload.get('round_id', '')[:8]}... "
            f"(not us)",
            level='debug'
        )

    return {"result": "continue", "election_result": result}


def handle_expansion_decline(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle EXPANSION_DECLINE message from the elected member (Phase 8).

    When the elected member cannot afford the channel open or has another
    reason to decline, this message triggers fallback to the next candidate.

    SECURITY: Verifies cryptographic signature from the decliner.
    """
    if not coop_expansion or not database:
        return {"result": "continue"}

    if not validate_expansion_decline(payload):
        plugin.log(f"cl-hive: Invalid EXPANSION_DECLINE from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    # Verify sender is a hive member and not banned
    sender = database.get_member(peer_id)
    if not sender or database.is_banned(peer_id):
        plugin.log(f"cl-hive: EXPANSION_DECLINE from non-member {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # SECURITY: Verify the cryptographic signature from decliner
    decliner_id = payload.get("decliner_id", "")
    signature = payload.get("signature", "")
    signing_message = get_expansion_decline_signing_payload(payload)

    try:
        verify_result = plugin.rpc.checkmessage(signing_message, signature)
        if not verify_result.get("verified", False):
            plugin.log(
                f"cl-hive: [DECLINE] Signature verification failed for decliner {decliner_id[:16]}...",
                level='warn'
            )
            return {"result": "continue"}
        # Verify the signature is from the claimed decliner
        recovered_pubkey = verify_result.get("pubkey", "")
        if recovered_pubkey != decliner_id:
            plugin.log(
                f"cl-hive: [DECLINE] Signature mismatch: claimed={decliner_id[:16]}... "
                f"actual={recovered_pubkey[:16]}...",
                level='warn'
            )
            return {"result": "continue"}
        # Verify the decliner is a hive member
        decliner_member = database.get_member(decliner_id)
        if not decliner_member or database.is_banned(decliner_id):
            plugin.log(
                f"cl-hive: [DECLINE] Decliner {decliner_id[:16]}... not a member or banned",
                level='warn'
            )
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: [DECLINE] Signature verification error: {e}", level='warn')
        return {"result": "continue"}

    round_id = payload.get("round_id", "")
    reason = payload.get("reason", "unknown")
    plugin.log(
        f"cl-hive: [DECLINE] Verified decline from {decliner_id[:16]}... "
        f"for round {round_id[:8]}... (reason={reason})",
        level='info'
    )

    # Process the decline - this may elect a fallback candidate
    result = coop_expansion.handle_decline(peer_id, payload)

    if result.get("action") == "fallback_elected":
        # A fallback candidate was elected
        new_elected = result.get("elected_id", "")
        our_id = None
        try:
            our_id = plugin.rpc.getinfo().get("id")
        except Exception:
            pass

        if new_elected == our_id:
            # We are the fallback candidate
            target_peer_id = result.get("target_peer_id", "")
            channel_size = result.get("channel_size_sats", 0)
            plugin.log(
                f"cl-hive: We are the fallback candidate for round {round_id[:8]}... "
                f"(target={target_peer_id[:16]}...)",
                level='info'
            )

            # Queue the channel open via pending actions
            if database and config:
                cfg = config.snapshot()
                proposed_size = channel_size or cfg.planner_default_channel_sats

                # Check affordability before queuing
                capped_size, insufficient, was_capped = _cap_channel_size_to_budget(
                    proposed_size, cfg, f"FALLBACK_ELECT for {target_peer_id[:16]}..."
                )
                if insufficient:
                    plugin.log(
                        f"cl-hive: [FALLBACK] Also declining: insufficient funds",
                        level='info'
                    )
                    # Broadcast our own decline
                    _broadcast_expansion_decline(round_id, "insufficient_funds")
                    return {"result": "declined", "reason": "insufficient_funds"}

                action_id = database.add_pending_action(
                    action_type="channel_open",
                    payload={
                        "target": target_peer_id,
                        "amount_sats": capped_size,
                        "source": "cooperative_expansion_fallback",
                        "round_id": round_id,
                        "reason": f"Fallback elected after {result.get('decline_count', 1)} decline(s)"
                    },
                    expires_hours=24
                )
                plugin.log(
                    f"cl-hive: Queued fallback channel open to {target_peer_id[:16]}... "
                    f"(action_id={action_id})",
                    level='info'
                )
        else:
            plugin.log(
                f"cl-hive: [DECLINE] Fallback elected {new_elected[:16]}... (not us)",
                level='debug'
            )

    elif result.get("action") == "cancelled":
        plugin.log(
            f"cl-hive: [DECLINE] Round {round_id[:8]}... cancelled: {result.get('reason', 'unknown')}",
            level='info'
        )

    return {"result": "continue", "decline_result": result}


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


def handle_route_probe(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle ROUTE_PROBE message from a hive member.

    Used for collective routing intelligence.
    """
    if not routing_map or not database:
        return {"result": "continue"}

    # Deduplication check
    if not _should_process_message(payload):
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "ROUTE_PROBE"):
        return {"result": "continue"}

    # Verify sender is a hive member and not banned (supports relay)
    is_relayed = _is_relayed_message(payload)
    if is_relayed:
        relay_member = database.get_member(peer_id)
        if not relay_member or relay_member.get("tier") not in (MembershipTier.MEMBER.value,):
            return {"result": "continue"}
    else:
        sender = database.get_member(peer_id)
        if not sender or database.is_banned(peer_id):
            plugin.log(f"cl-hive: ROUTE_PROBE from non-member {peer_id[:16]}...", level='debug')
            return {"result": "continue"}

    # SECURITY: Verify signature
    reporter_id = payload.get("reporter_id", peer_id)
    signature = payload.get("signature")
    if not signature:
        plugin.log(f"cl-hive: ROUTE_PROBE missing signature from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    from modules.protocol import get_route_probe_signing_payload
    signing_payload = get_route_probe_signing_payload(payload)
    try:
        verify_result = plugin.rpc.checkmessage(signing_payload, signature)
        if not verify_result.get("verified") or verify_result.get("pubkey") != reporter_id:
            plugin.log(f"cl-hive: ROUTE_PROBE invalid signature from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: ROUTE_PROBE signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # Delegate to routing map — pass verified reporter_id (not transport peer_id)
    # and skip re-verification since we already checked the signature above
    result = routing_map.handle_route_probe(
        reporter_id, payload, plugin.rpc, pre_verified=True
    )

    if result.get("success"):
        relay_info = " (relayed)" if is_relayed else ""
        plugin.log(
            f"cl-hive: Stored route probe from {reporter_id[:16]}...{relay_info}",
            level='debug'
        )
    elif result.get("error"):
        plugin.log(
            f"cl-hive: ROUTE_PROBE rejected from {reporter_id[:16]}...: {result.get('error')}",
            level='debug'
        )

    # Relay to other members
    _relay_message(HiveMessageType.ROUTE_PROBE, payload, peer_id)

    return {"result": "continue"}


def handle_route_probe_batch(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle ROUTE_PROBE_BATCH message from a hive member.

    This is the preferred method for receiving route probes - one message
    contains multiple probe observations instead of N individual messages.
    """
    if not routing_map or not database:
        return {"result": "continue"}

    # Deduplication check
    if not _should_process_message(payload):
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "ROUTE_PROBE_BATCH"):
        return {"result": "continue"}

    # Verify sender is a hive member and not banned (supports relay)
    is_relayed = _is_relayed_message(payload)
    if is_relayed:
        relay_member = database.get_member(peer_id)
        if not relay_member or relay_member.get("tier") not in (MembershipTier.MEMBER.value,):
            return {"result": "continue"}
    else:
        sender = database.get_member(peer_id)
        if not sender or database.is_banned(peer_id):
            plugin.log(f"cl-hive: ROUTE_PROBE_BATCH from non-member {peer_id[:16]}...", level='debug')
            return {"result": "continue"}

    # SECURITY: Verify signature
    reporter_id = payload.get("reporter_id", peer_id)
    signature = payload.get("signature")
    if not signature:
        plugin.log(f"cl-hive: ROUTE_PROBE_BATCH missing signature from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    from modules.protocol import get_route_probe_batch_signing_payload
    signing_payload = get_route_probe_batch_signing_payload(payload)
    try:
        verify_result = plugin.rpc.checkmessage(signing_payload, signature)
        if not verify_result.get("verified") or verify_result.get("pubkey") != reporter_id:
            plugin.log(f"cl-hive: ROUTE_PROBE_BATCH invalid signature from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: ROUTE_PROBE_BATCH signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # Delegate to routing map — pass verified reporter_id (not transport peer_id)
    # and skip re-verification since we already checked the signature above
    result = routing_map.handle_route_probe_batch(
        reporter_id, payload, plugin.rpc, pre_verified=True
    )

    if result.get("success"):
        relay_info = " (relayed)" if is_relayed else ""
        plugin.log(
            f"cl-hive: Stored route probe batch from {reporter_id[:16]}...{relay_info} "
            f"with {result.get('probes_stored', 0)} probes",
            level='debug'
        )
    elif result.get("error"):
        plugin.log(
            f"cl-hive: ROUTE_PROBE_BATCH rejected from {reporter_id[:16]}...: {result.get('error')}",
            level='debug'
        )

    # Relay to other members
    _relay_message(HiveMessageType.ROUTE_PROBE_BATCH, payload, peer_id)

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
        if not relay_member or relay_member.get("tier") not in (MembershipTier.MEMBER.value,):
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


def handle_stigmergic_marker_batch(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle STIGMERGIC_MARKER_BATCH message from a hive member.

    This enables fleet-wide learning from routing outcomes. When a member
    successfully routes traffic, they share their markers so other members
    can adjust their fees accordingly (stigmergic coordination).
    """
    if not fee_coordination_mgr or not database:
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "STIGMERGIC_MARKER_BATCH"):
        return {"result": "continue"}

    # Verify sender is a hive member and not banned (supports relay)
    is_relayed = _is_relayed_message(payload)
    if is_relayed:
        relay_member = database.get_member(peer_id)
        if not relay_member or relay_member.get("tier") not in (MembershipTier.MEMBER.value,):
            return {"result": "continue"}
    else:
        sender = database.get_member(peer_id)
        if not sender or database.is_banned(peer_id):
            plugin.log(f"cl-hive: STIGMERGIC_MARKER_BATCH from non-member {peer_id[:16]}...", level='debug')
            return {"result": "continue"}

    # Validate payload
    from modules.protocol import validate_stigmergic_marker_batch, get_stigmergic_marker_batch_signing_payload
    if not validate_stigmergic_marker_batch(payload):
        plugin.log(f"cl-hive: STIGMERGIC_MARKER_BATCH validation failed from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify signature - reporter_id may differ from peer_id when relayed
    reporter_id = payload.get("reporter_id", "")
    if not is_relayed and reporter_id != peer_id:
        plugin.log(f"cl-hive: STIGMERGIC_MARKER_BATCH reporter mismatch from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify reporter is a member
    reporter = database.get_member(reporter_id)
    if not reporter or database.is_banned(reporter_id):
        plugin.log(f"cl-hive: STIGMERGIC_MARKER_BATCH from non-member reporter {reporter_id[:16]}...", level='debug')
        return {"result": "continue"}

    try:
        signing_payload = get_stigmergic_marker_batch_signing_payload(payload)
        verify_result = plugin.rpc.checkmessage(signing_payload, payload.get("signature", ""))
        if not verify_result.get("verified"):
            plugin.log(f"cl-hive: STIGMERGIC_MARKER_BATCH signature invalid from {peer_id[:16]}...", level='debug')
            return {"result": "continue"}
        if verify_result.get("pubkey") != reporter_id:
            plugin.log(f"cl-hive: STIGMERGIC_MARKER_BATCH pubkey mismatch from {peer_id[:16]}...", level='debug')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: STIGMERGIC_MARKER_BATCH signature check error: {e}", level='debug')
        return {"result": "continue"}

    # Deduplicate only after sender, payload, and signature are validated.
    if not _should_process_message(payload):
        return {"result": "continue"}

    # Process each marker
    markers = payload.get("markers", [])
    markers_stored = 0

    for marker_data in markers:
        try:
            # Verify depositor matches reporter to prevent attribution spoofing
            claimed_depositor = marker_data.get("depositor")
            if claimed_depositor and claimed_depositor != reporter_id:
                plugin.log(
                    f"cl-hive: Marker depositor mismatch: claimed {claimed_depositor[:16]}... "
                    f"but reporter is {reporter_id[:16]}..., overriding",
                    level='debug'
                )
            # Force depositor to match the authenticated reporter
            marker_data["depositor"] = reporter_id

            # Use the existing receive_marker_from_gossip method
            result = fee_coordination_mgr.stigmergic_coord.receive_marker_from_gossip(marker_data)
            if result:
                markers_stored += 1
        except Exception as e:
            plugin.log(f"cl-hive: Error processing marker: {e}", level='debug')
            continue

    if markers_stored > 0:
        relay_info = " (relayed)" if is_relayed else ""
        plugin.log(
            f"cl-hive: Stored {markers_stored} stigmergic markers from {reporter_id[:16]}...{relay_info}",
            level='debug'
        )

    # Relay to other members
    _relay_message(HiveMessageType.STIGMERGIC_MARKER_BATCH, payload, peer_id)

    return {"result": "continue"}


def handle_pheromone_batch(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle PHEROMONE_BATCH message from a hive member.

    This enables fleet-wide learning from fee outcomes. When a member
    has successful routing at certain fees, they share their pheromone
    levels so other members can adjust their fees accordingly.
    """
    if not fee_coordination_mgr or not database:
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "PHEROMONE_BATCH"):
        return {"result": "continue"}

    # Verify sender is a hive member and not banned (supports relay)
    is_relayed = _is_relayed_message(payload)
    if is_relayed:
        relay_member = database.get_member(peer_id)
        if not relay_member or relay_member.get("tier") not in (MembershipTier.MEMBER.value,):
            return {"result": "continue"}
    else:
        sender = database.get_member(peer_id)
        if not sender or database.is_banned(peer_id):
            plugin.log(f"cl-hive: PHEROMONE_BATCH from non-member {peer_id[:16]}...", level='debug')
            return {"result": "continue"}

    # Validate payload
    from modules.protocol import validate_pheromone_batch, get_pheromone_batch_signing_payload
    if not validate_pheromone_batch(payload):
        plugin.log(f"cl-hive: PHEROMONE_BATCH validation failed from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify signature - reporter_id may differ from peer_id when relayed
    reporter_id = payload.get("reporter_id", "")
    if not is_relayed and reporter_id != peer_id:
        plugin.log(f"cl-hive: PHEROMONE_BATCH reporter mismatch from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify reporter is a member
    reporter = database.get_member(reporter_id)
    if not reporter or database.is_banned(reporter_id):
        plugin.log(f"cl-hive: PHEROMONE_BATCH from non-member reporter {reporter_id[:16]}...", level='debug')
        return {"result": "continue"}

    try:
        signing_payload = get_pheromone_batch_signing_payload(payload)
        verify_result = plugin.rpc.checkmessage(signing_payload, payload.get("signature", ""))
        if not verify_result.get("verified"):
            plugin.log(f"cl-hive: PHEROMONE_BATCH signature invalid from {peer_id[:16]}...", level='debug')
            return {"result": "continue"}
        if verify_result.get("pubkey") != reporter_id:
            plugin.log(f"cl-hive: PHEROMONE_BATCH pubkey mismatch from {peer_id[:16]}...", level='debug')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: PHEROMONE_BATCH signature check error: {e}", level='debug')
        return {"result": "continue"}

    # Deduplicate only after sender, payload, and signature are validated.
    if not _should_process_message(payload):
        return {"result": "continue"}

    # Process each pheromone entry
    pheromones = payload.get("pheromones", [])
    pheromones_stored = 0

    from modules.protocol import PHEROMONE_WEIGHTING_FACTOR

    for pheromone_data in pheromones:
        try:
            # Use the receive_pheromone_from_gossip method
            result = fee_coordination_mgr.adaptive_controller.receive_pheromone_from_gossip(
                reporter_id=reporter_id,
                pheromone_data=pheromone_data,
                weighting_factor=PHEROMONE_WEIGHTING_FACTOR
            )
            if result:
                pheromones_stored += 1
        except Exception as e:
            plugin.log(f"cl-hive: Error processing pheromone: {e}", level='debug')
            continue

    if pheromones_stored > 0:
        relay_info = " (relayed)" if is_relayed else ""
        plugin.log(
            f"cl-hive: Stored {pheromones_stored} pheromones from {reporter_id[:16]}...{relay_info}",
            level='debug'
        )

    # Relay to other members
    _relay_message(HiveMessageType.PHEROMONE_BATCH, payload, peer_id)

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
        if not relay_member or relay_member.get("tier") not in (MembershipTier.MEMBER.value,):
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


def handle_circular_flow_alert(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle CIRCULAR_FLOW_ALERT message from a hive member.

    This enables fleet-wide awareness of wasteful circular rebalancing
    patterns so all members can adjust their behavior.
    """
    if not cost_reduction_mgr or not database:
        return {"result": "continue"}

    # Deduplication check
    if not _should_process_message(payload):
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "CIRCULAR_FLOW_ALERT"):
        return {"result": "continue"}

    # Verify sender is a hive member and not banned (supports relay)
    is_relayed = _is_relayed_message(payload)
    if is_relayed:
        relay_member = database.get_member(peer_id)
        if not relay_member or relay_member.get("tier") not in (MembershipTier.MEMBER.value,):
            return {"result": "continue"}
    else:
        sender = database.get_member(peer_id)
        if not sender or database.is_banned(peer_id):
            plugin.log(f"cl-hive: CIRCULAR_FLOW_ALERT from non-member {peer_id[:16]}...", level='debug')
            return {"result": "continue"}

    # Validate payload
    from modules.protocol import validate_circular_flow_alert, get_circular_flow_alert_signing_payload
    if not validate_circular_flow_alert(payload):
        plugin.log(f"cl-hive: CIRCULAR_FLOW_ALERT validation failed from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify signature - reporter_id may differ from peer_id when relayed
    reporter_id = payload.get("reporter_id", "")
    if not is_relayed and reporter_id != peer_id:
        plugin.log(f"cl-hive: CIRCULAR_FLOW_ALERT reporter mismatch from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify reporter is a member
    reporter = database.get_member(reporter_id)
    if not reporter or database.is_banned(reporter_id):
        plugin.log(f"cl-hive: CIRCULAR_FLOW_ALERT from non-member reporter {reporter_id[:16]}...", level='debug')
        return {"result": "continue"}

    try:
        signing_payload = get_circular_flow_alert_signing_payload(payload)
        verify_result = plugin.rpc.checkmessage(signing_payload, payload.get("signature", ""))
        if not verify_result.get("verified"):
            plugin.log(f"cl-hive: CIRCULAR_FLOW_ALERT signature invalid from {peer_id[:16]}...", level='debug')
            return {"result": "continue"}
        if verify_result.get("pubkey") != reporter_id:
            plugin.log(f"cl-hive: CIRCULAR_FLOW_ALERT pubkey mismatch from {peer_id[:16]}...", level='debug')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: CIRCULAR_FLOW_ALERT signature check error: {e}", level='debug')
        return {"result": "continue"}

    # Store the circular flow alert
    try:
        result = cost_reduction_mgr.circular_detector.receive_circular_flow_alert(
            reporter_id=reporter_id,
            alert_data=payload
        )
        if result:
            members = payload.get("members_involved", [])
            cost = payload.get("total_cost_sats", 0)
            relay_info = " (relayed)" if is_relayed else ""
            plugin.log(
                f"cl-hive: Received circular flow alert from {reporter_id[:16]}...{relay_info} "
                f"({len(members)} members, {cost} sats wasted)",
                level='info'
            )
    except Exception as e:
        plugin.log(f"cl-hive: Error storing circular flow alert: {e}", level='debug')

    # Relay to other members
    _relay_message(HiveMessageType.CIRCULAR_FLOW_ALERT, payload, peer_id)

    return {"result": "continue"}


def handle_temporal_pattern_batch(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle TEMPORAL_PATTERN_BATCH message from a hive member.

    This enables fleet-wide learning about temporal flow patterns
    for coordinated liquidity positioning and fee optimization.
    """
    if not anticipatory_liquidity_mgr or not database:
        return {"result": "continue"}

    # Deduplication check
    if not _should_process_message(payload):
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "TEMPORAL_PATTERN_BATCH"):
        return {"result": "continue"}

    # Verify sender is a hive member and not banned (supports relay)
    is_relayed = _is_relayed_message(payload)
    if is_relayed:
        relay_member = database.get_member(peer_id)
        if not relay_member or relay_member.get("tier") not in (MembershipTier.MEMBER.value,):
            return {"result": "continue"}
    else:
        sender = database.get_member(peer_id)
        if not sender or database.is_banned(peer_id):
            plugin.log(f"cl-hive: TEMPORAL_PATTERN_BATCH from non-member {peer_id[:16]}...", level='debug')
            return {"result": "continue"}

    # Validate payload
    from modules.protocol import validate_temporal_pattern_batch, get_temporal_pattern_batch_signing_payload
    if not validate_temporal_pattern_batch(payload):
        plugin.log(f"cl-hive: TEMPORAL_PATTERN_BATCH validation failed from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify signature - reporter_id may differ from peer_id when relayed
    reporter_id = payload.get("reporter_id", "")
    if not is_relayed and reporter_id != peer_id:
        plugin.log(f"cl-hive: TEMPORAL_PATTERN_BATCH reporter mismatch from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify reporter is a member
    reporter = database.get_member(reporter_id)
    if not reporter or database.is_banned(reporter_id):
        plugin.log(f"cl-hive: TEMPORAL_PATTERN_BATCH from non-member reporter {reporter_id[:16]}...", level='debug')
        return {"result": "continue"}

    try:
        signing_payload = get_temporal_pattern_batch_signing_payload(payload)
        verify_result = plugin.rpc.checkmessage(signing_payload, payload.get("signature", ""))
        if not verify_result.get("verified"):
            plugin.log(f"cl-hive: TEMPORAL_PATTERN_BATCH signature invalid from {peer_id[:16]}...", level='debug')
            return {"result": "continue"}
        if verify_result.get("pubkey") != reporter_id:
            plugin.log(f"cl-hive: TEMPORAL_PATTERN_BATCH pubkey mismatch from {peer_id[:16]}...", level='debug')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: TEMPORAL_PATTERN_BATCH signature check error: {e}", level='debug')
        return {"result": "continue"}

    # Process each pattern entry
    patterns = payload.get("patterns", [])
    patterns_stored = 0

    for pattern_data in patterns:
        try:
            result = anticipatory_liquidity_mgr.receive_pattern_from_fleet(
                reporter_id=reporter_id,
                pattern_data=pattern_data
            )
            if result:
                patterns_stored += 1
        except Exception as e:
            plugin.log(f"cl-hive: Error processing temporal pattern: {e}", level='debug')
            continue

    if patterns_stored > 0:
        relay_info = " (relayed)" if is_relayed else ""
        plugin.log(
            f"cl-hive: Stored {patterns_stored} temporal patterns from {reporter_id[:16]}...{relay_info}",
            level='debug'
        )

    # Relay to other members
    _relay_message(HiveMessageType.TEMPORAL_PATTERN_BATCH, payload, peer_id)

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
        if not relay_member or relay_member.get("tier") not in (MembershipTier.MEMBER.value,):
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
        if not relay_member or relay_member.get("tier") not in (MembershipTier.MEMBER.value,):
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


def handle_physarum_recommendation(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle PHYSARUM_RECOMMENDATION message from a hive member.

    This enables fleet-wide sharing of flow-based channel lifecycle recommendations
    (strengthen/atrophy/stimulate actions based on slime mold optimization).
    """
    if not strategic_positioning_mgr or not database:
        return {"result": "continue"}

    # Deduplication check
    if not _should_process_message(payload):
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "PHYSARUM_RECOMMENDATION"):
        return {"result": "continue"}

    # Verify sender is a hive member and not banned (supports relay)
    is_relayed = _is_relayed_message(payload)
    if is_relayed:
        relay_member = database.get_member(peer_id)
        if not relay_member or relay_member.get("tier") not in (MembershipTier.MEMBER.value,):
            return {"result": "continue"}
    else:
        sender = database.get_member(peer_id)
        if not sender or database.is_banned(peer_id):
            plugin.log(f"cl-hive: PHYSARUM_RECOMMENDATION from non-member {peer_id[:16]}...", level='debug')
            return {"result": "continue"}

    # Validate payload
    from modules.protocol import validate_physarum_recommendation, get_physarum_recommendation_signing_payload
    if not validate_physarum_recommendation(payload):
        plugin.log(f"cl-hive: PHYSARUM_RECOMMENDATION validation failed from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify signature - reporter_id may differ from peer_id when relayed
    reporter_id = payload.get("reporter_id", "")
    if not is_relayed and reporter_id != peer_id:
        plugin.log(f"cl-hive: PHYSARUM_RECOMMENDATION reporter mismatch from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify reporter is a member
    reporter = database.get_member(reporter_id)
    if not reporter or database.is_banned(reporter_id):
        plugin.log(f"cl-hive: PHYSARUM_RECOMMENDATION from non-member reporter {reporter_id[:16]}...", level='debug')
        return {"result": "continue"}

    try:
        signing_payload = get_physarum_recommendation_signing_payload(payload)
        verify_result = plugin.rpc.checkmessage(signing_payload, payload.get("signature", ""))
        if not verify_result.get("verified"):
            plugin.log(f"cl-hive: PHYSARUM_RECOMMENDATION signature invalid from {peer_id[:16]}...", level='debug')
            return {"result": "continue"}
        if verify_result.get("pubkey") != reporter_id:
            plugin.log(f"cl-hive: PHYSARUM_RECOMMENDATION pubkey mismatch from {peer_id[:16]}...", level='debug')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: PHYSARUM_RECOMMENDATION signature check error: {e}", level='debug')
        return {"result": "continue"}

    # Store the Physarum recommendation
    try:
        result = strategic_positioning_mgr.receive_physarum_recommendation_from_fleet(
            reporter_id=reporter_id,
            recommendation_data=payload
        )
        if result:
            action = payload.get("action", "unknown")
            peer_short = payload.get("peer_id", "")[:16]
            relay_info = " (relayed)" if is_relayed else ""
            plugin.log(
                f"cl-hive: Stored Physarum {action} recommendation from {reporter_id[:16]}...{relay_info} for peer {peer_short}...",
                level='debug'
            )
    except Exception as e:
        plugin.log(f"cl-hive: Error storing Physarum recommendation: {e}", level='debug')

    # Relay to other members
    _relay_message(HiveMessageType.PHYSARUM_RECOMMENDATION, payload, peer_id)

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
        if not relay_member or relay_member.get("tier") not in (MembershipTier.MEMBER.value,):
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


def handle_settlement_offer(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle SETTLEMENT_OFFER message from a hive member.

    Stores the member's BOLT12 offer for use in settlement calculations.
    """
    if not settlement_mgr or not database:
        return {"result": "continue"}

    # Deduplication check
    if not _should_process_message(payload):
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_SETTLEMENT_AGE_SECONDS, "SETTLEMENT_OFFER"):
        return {"result": "continue"}

    # Extract payload fields
    offer_peer_id = payload.get("peer_id")
    bolt12_offer = payload.get("bolt12_offer")
    timestamp = payload.get("timestamp")
    signature = payload.get("signature")

    # Validate required fields
    if not all([offer_peer_id, bolt12_offer, signature]):
        plugin.log(f"cl-hive: SETTLEMENT_OFFER missing required fields from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify sender (supports relay) - offer_peer_id is the original sender
    if not _validate_relay_sender(peer_id, offer_peer_id, payload):
        plugin.log(f"cl-hive: SETTLEMENT_OFFER peer_id mismatch from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    # Verify original sender is a hive member and not banned
    sender = database.get_member(offer_peer_id)
    if not sender or database.is_banned(offer_peer_id):
        plugin.log(f"cl-hive: SETTLEMENT_OFFER from non-member {offer_peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify the signature
    signing_payload = get_settlement_offer_signing_payload(offer_peer_id, bolt12_offer)
    try:
        verify_result = plugin.rpc.call("checkmessage", {
            "message": signing_payload,
            "zbase": signature,
            "pubkey": offer_peer_id
        })
        if not verify_result.get("verified"):
            plugin.log(f"cl-hive: SETTLEMENT_OFFER invalid signature from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: SETTLEMENT_OFFER signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # Store the offer
    result = settlement_mgr.register_offer(offer_peer_id, bolt12_offer)

    if "error" not in result:
        is_relayed = _is_relayed_message(payload)
        relay_info = " (relayed)" if is_relayed else ""
        plugin.log(f"cl-hive: Stored settlement offer from {offer_peer_id[:16]}...{relay_info}")
    else:
        plugin.log(f"cl-hive: Failed to store settlement offer: {result.get('error')}", level='debug')

    # Relay to other members
    _relay_message(HiveMessageType.SETTLEMENT_OFFER, payload, peer_id)

    return {"result": "continue"}


def handle_fee_report(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle FEE_REPORT message from a hive member.

    Stores the member's fee earnings for use in settlement calculations.
    This enables real-time fee tracking across the fleet.
    """
    from modules.protocol import (
        get_fee_report_signing_payload, get_fee_report_signing_payload_legacy,
        validate_fee_report
    )

    if not state_manager or not database:
        return {"result": "continue"}

    # Deduplication check
    if not _should_process_message(payload):
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "FEE_REPORT"):
        return {"result": "continue"}

    # Validate payload schema
    if not validate_fee_report(payload):
        # Log field types for debugging
        types = {k: type(v).__name__ for k, v in payload.items()} if isinstance(payload, dict) else {}
        plugin.log(f"[FeeReport] Rejected: invalid schema from {peer_id[:16]}... types={types}", level='info')
        return {"result": "continue"}

    # Extract payload fields
    report_peer_id = payload.get("peer_id")
    fees_earned_sats = payload.get("fees_earned_sats")
    period_start = payload.get("period_start")
    period_end = payload.get("period_end")
    forward_count = payload.get("forward_count")
    signature = payload.get("signature")
    # Extract rebalance costs (backward compat - defaults to 0)
    rebalance_costs_sats = payload.get("rebalance_costs_sats", 0)

    # Phase C: Persistent idempotency check
    is_new, event_id = check_and_record(database, "FEE_REPORT", payload, report_peer_id or peer_id)
    if not is_new:
        plugin.log(f"cl-hive: FEE_REPORT duplicate event {event_id}, skipping", level='debug')
        _emit_ack(peer_id, event_id)
        _relay_message(HiveMessageType.FEE_REPORT, payload, peer_id)
        return {"result": "continue"}
    if event_id:
        payload["_event_id"] = event_id

    # Verify sender (supports relay) - report_peer_id is the original sender
    if not _validate_relay_sender(peer_id, report_peer_id, payload):
        plugin.log(f"cl-hive: FEE_REPORT peer_id mismatch from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    # Verify original sender is a hive member and not banned
    sender = database.get_member(report_peer_id)
    if not sender or database.is_banned(report_peer_id):
        plugin.log(f"[FeeReport] Rejected: non-member or banned {report_peer_id[:16]}...", level='info')
        return {"result": "continue"}

    # Verify the signature - try new format with costs first, then legacy format
    verified = False
    try:
        # Try new format (with costs) first
        signing_payload = get_fee_report_signing_payload(
            report_peer_id, fees_earned_sats, period_start, period_end, forward_count,
            rebalance_costs_sats
        )
        verify_result = plugin.rpc.call("checkmessage", {
            "message": signing_payload,
            "zbase": signature,
            "pubkey": report_peer_id
        })
        verified = verify_result.get("verified", False)

        # If new format fails and costs are 0, try legacy format (backward compat)
        if not verified and rebalance_costs_sats == 0:
            legacy_payload = get_fee_report_signing_payload_legacy(
                report_peer_id, fees_earned_sats, period_start, period_end, forward_count
            )
            verify_result = plugin.rpc.call("checkmessage", {
                "message": legacy_payload,
                "zbase": signature,
                "pubkey": report_peer_id
            })
            verified = verify_result.get("verified", False)

        if not verified:
            plugin.log(f"cl-hive: FEE_REPORT invalid signature from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: FEE_REPORT signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # Update state manager with fee data (in-memory)
    updated = state_manager.update_peer_fees(
        peer_id=report_peer_id,
        fees_earned_sats=fees_earned_sats,
        forward_count=forward_count,
        period_start=period_start,
        period_end=period_end,
        rebalance_costs_sats=rebalance_costs_sats
    )

    # Also persist to database for settlement calculations
    from modules.settlement import SettlementManager
    period = SettlementManager.get_period_string(period_start)
    database.save_fee_report(
        peer_id=report_peer_id,
        period=period,
        fees_earned_sats=fees_earned_sats,
        forward_count=forward_count,
        period_start=period_start,
        period_end=period_end,
        rebalance_costs_sats=rebalance_costs_sats
    )

    if updated:
        is_relayed = _is_relayed_message(payload)
        relay_info = " (relayed)" if is_relayed else ""
        costs_info = f", costs={rebalance_costs_sats}" if rebalance_costs_sats > 0 else ""
        plugin.log(
            f"FEE_GOSSIP: Received FEE_REPORT from {report_peer_id[:16]}...{relay_info}: {fees_earned_sats} sats{costs_info}, "
            f"{forward_count} forwards (period {period})",
            level='info'
        )

    # Relay to other members
    _relay_message(HiveMessageType.FEE_REPORT, payload, peer_id)

    return {"result": "continue"}


# =============================================================================
# PHASE 12: DISTRIBUTED SETTLEMENT MESSAGE HANDLERS
# =============================================================================

def handle_settlement_propose(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle SETTLEMENT_PROPOSE message from a hive member.

    When a member proposes a settlement for a period, we verify the data hash
    against our own gossiped FEE_REPORT data and vote if it matches.
    """
    from modules.protocol import (
        validate_settlement_propose,
        get_settlement_propose_signing_payload,
        create_settlement_ready,
        get_settlement_ready_signing_payload
    )

    if not settlement_mgr or not database or not state_manager:
        return {"result": "continue"}

    # Deduplication check
    if not _should_process_message(payload):
        return {"result": "continue"}

    # Validate payload schema
    if not validate_settlement_propose(payload):
        plugin.log(f"cl-hive: SETTLEMENT_PROPOSE invalid schema from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_SETTLEMENT_AGE_SECONDS, "SETTLEMENT_PROPOSE"):
        return {"result": "continue"}

    # Verify proposer (supports relay)
    proposer_peer_id = payload.get("proposer_peer_id")
    if not _validate_relay_sender(peer_id, proposer_peer_id, payload):
        plugin.log(
            f"cl-hive: SETTLEMENT_PROPOSE proposer mismatch from {peer_id[:16]}...",
            level='warn'
        )
        return {"result": "continue"}

    # Phase C: Persistent idempotency check
    is_new, event_id = check_and_record(database, "SETTLEMENT_PROPOSE", payload, proposer_peer_id or peer_id)
    if not is_new:
        plugin.log(f"cl-hive: SETTLEMENT_PROPOSE duplicate event {event_id}, skipping", level='debug')
        _emit_ack(peer_id, event_id)
        _relay_message(HiveMessageType.SETTLEMENT_PROPOSE, payload, peer_id)
        return {"result": "continue"}
    if event_id:
        payload["_event_id"] = event_id

    # Verify original sender is a hive member and not banned
    sender = database.get_member(proposer_peer_id)
    if not sender or database.is_banned(proposer_peer_id):
        plugin.log(f"cl-hive: SETTLEMENT_PROPOSE from non-member {proposer_peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify signature
    signature = payload.get("signature")
    signing_payload = get_settlement_propose_signing_payload(payload)
    try:
        verify_result = plugin.rpc.call("checkmessage", {
            "message": signing_payload,
            "zbase": signature,
            "pubkey": proposer_peer_id
        })
        if not verify_result.get("verified"):
            plugin.log(f"cl-hive: SETTLEMENT_PROPOSE invalid signature from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: SETTLEMENT_PROPOSE signature check failed: {e}", level='warn')
        return {"result": "continue"}

    proposal_id = payload.get("proposal_id")
    period = payload.get("period")
    data_hash = payload.get("data_hash")
    plan_hash = payload.get("plan_hash")
    contributions = payload.get("contributions", [])

    plugin.log(
        f"SETTLEMENT: Received proposal {proposal_id[:16]}... for {period} from {peer_id[:16]}..."
    )

    # Store the proposal if we don't have one for this period.
    # If we already have a different proposal_id for the same period, ignore
    # this payload for local voting/execution to avoid orphaned votes.
    existing_for_period = database.get_settlement_proposal_by_period(period)
    if existing_for_period and existing_for_period.get("proposal_id") != proposal_id:
        plugin.log(
            f"SETTLEMENT: Ignoring competing proposal {proposal_id[:16]}... for {period}; "
            f"already tracking {existing_for_period.get('proposal_id', '')[:16]}...",
            level='warn'
        )
        _emit_ack(peer_id, payload.get("_event_id"))
        _relay_message(HiveMessageType.SETTLEMENT_PROPOSE, payload, peer_id)
        return {"result": "continue"}

    if not existing_for_period:
        database.add_settlement_proposal(
            proposal_id=proposal_id,
            period=period,
            proposer_peer_id=proposer_peer_id,
            data_hash=data_hash,
            plan_hash=plan_hash,
            total_fees_sats=payload.get("total_fees_sats", 0),
            member_count=payload.get("member_count", 0)
            ,
            contributions_json=json.dumps(contributions)
        )

    # Try to verify and vote
    vote = settlement_mgr.verify_and_vote(
        proposal=payload,
        our_peer_id=our_pubkey,
        state_manager=state_manager,
        rpc=plugin.rpc
    )

    if vote:
        # Broadcast our vote via reliable delivery
        vote_payload = {
            'proposal_id': vote['proposal_id'],
            'voter_peer_id': vote['voter_peer_id'],
            'data_hash': vote['data_hash'],
            'timestamp': vote['timestamp'],
            'signature': vote['signature'],
        }
        _reliable_broadcast(HiveMessageType.SETTLEMENT_READY, vote_payload)
        plugin.log(f"SETTLEMENT: Voted on proposal {proposal_id[:16]}... (hash verified)")
    else:
        _log_settlement_vote_skip_reason(plugin, proposal_id, period, settlement_mgr)

    # Phase D: Acknowledge receipt
    _emit_ack(peer_id, payload.get("_event_id"))

    # Relay to other members
    _relay_message(HiveMessageType.SETTLEMENT_PROPOSE, payload, peer_id)

    return {"result": "continue"}


def _log_settlement_vote_skip_reason(plugin: Plugin, proposal_id: Optional[str], period: Optional[str], settlement_mgr) -> None:
    """Log a compact operational reason for why a settlement proposal was not voted locally."""
    reason_payload = getattr(settlement_mgr, "last_verify_and_vote_reason", None) or {}
    reason = reason_payload.get("reason", "unknown")
    effective_period = reason_payload.get("period") or period
    proposal_prefix = str(reason_payload.get("proposal_id") or proposal_id or "")[:16]
    plugin.log(
        f"SETTLEMENT: Proposal {proposal_prefix}... not voted locally "
        f"(reason={reason}, period={effective_period})",
        level="info",
    )


def handle_settlement_ready(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle SETTLEMENT_READY message (vote) from a hive member.

    When we receive a vote, we record it and check if quorum is reached.
    """
    from modules.protocol import (
        validate_settlement_ready,
        get_settlement_ready_signing_payload
    )

    if not settlement_mgr or not database:
        return {"result": "continue"}

    # Deduplication check
    if not _should_process_message(payload):
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_SETTLEMENT_AGE_SECONDS, "SETTLEMENT_READY"):
        return {"result": "continue"}

    # Validate payload schema
    if not validate_settlement_ready(payload):
        plugin.log(f"cl-hive: SETTLEMENT_READY invalid schema from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify voter (supports relay)
    voter_peer_id = payload.get("voter_peer_id")
    if not _validate_relay_sender(peer_id, voter_peer_id, payload):
        plugin.log(
            f"cl-hive: SETTLEMENT_READY voter mismatch from {peer_id[:16]}...",
            level='warn'
        )
        return {"result": "continue"}

    # Phase C: Persistent idempotency check
    is_new, event_id = check_and_record(database, "SETTLEMENT_READY", payload, voter_peer_id or peer_id)
    if not is_new:
        plugin.log(f"cl-hive: SETTLEMENT_READY duplicate event {event_id}, skipping", level='debug')
        _emit_ack(peer_id, event_id)
        _relay_message(HiveMessageType.SETTLEMENT_READY, payload, peer_id)
        return {"result": "continue"}
    if event_id:
        payload["_event_id"] = event_id

    # Verify original sender is a hive member and not banned
    sender = database.get_member(voter_peer_id)
    if not sender or database.is_banned(voter_peer_id):
        plugin.log(f"cl-hive: SETTLEMENT_READY from non-member {voter_peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify signature
    signature = payload.get("signature")
    signing_payload = get_settlement_ready_signing_payload(payload)
    try:
        verify_result = plugin.rpc.call("checkmessage", {
            "message": signing_payload,
            "zbase": signature,
            "pubkey": voter_peer_id
        })
        if not verify_result.get("verified"):
            plugin.log(f"cl-hive: SETTLEMENT_READY invalid signature from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: SETTLEMENT_READY signature check failed: {e}", level='warn')
        return {"result": "continue"}

    proposal_id = payload.get("proposal_id")
    data_hash = payload.get("data_hash")

    # Get the proposal
    proposal = database.get_settlement_proposal(proposal_id)
    if not proposal:
        plugin.log(f"cl-hive: SETTLEMENT_READY for unknown proposal {proposal_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify data hash matches proposal
    if data_hash != proposal.get("data_hash"):
        plugin.log(
            f"cl-hive: SETTLEMENT_READY hash mismatch for {proposal_id[:16]}...",
            level='warn'
        )
        return {"result": "continue"}

    # Record the vote
    if database.add_settlement_ready_vote(
        proposal_id=proposal_id,
        voter_peer_id=voter_peer_id,
        data_hash=data_hash,
        signature=signature
    ):
        is_relayed = _is_relayed_message(payload)
        relay_info = " (relayed)" if is_relayed else ""
        plugin.log(f"SETTLEMENT: Recorded vote from {voter_peer_id[:16]}...{relay_info} for {proposal_id[:16]}...")

        # Check if quorum reached
        settlement_mgr.check_quorum_and_mark_ready(
            proposal_id=proposal_id,
            member_count=proposal.get("member_count", 0)
        )

    # Phase D: Acknowledge receipt + implicit ack (SETTLEMENT_READY implies SETTLEMENT_PROPOSE received)
    _emit_ack(peer_id, payload.get("_event_id"))
    if outbox_mgr:
        outbox_mgr.process_implicit_ack(peer_id, HiveMessageType.SETTLEMENT_READY, payload)

    # Relay to other members
    _relay_message(HiveMessageType.SETTLEMENT_READY, payload, peer_id)

    return {"result": "continue"}


def handle_settlement_executed(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle SETTLEMENT_EXECUTED message from a hive member.

    When a member confirms they've executed their settlement payment,
    we record it and check if the settlement is complete.
    """
    from modules.protocol import (
        validate_settlement_executed,
        get_settlement_executed_signing_payload
    )

    if not settlement_mgr or not database:
        return {"result": "continue"}

    # Deduplication check
    if not _should_process_message(payload):
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_SETTLEMENT_AGE_SECONDS, "SETTLEMENT_EXECUTED"):
        return {"result": "continue"}

    # Validate payload schema
    if not validate_settlement_executed(payload):
        plugin.log(f"cl-hive: SETTLEMENT_EXECUTED invalid schema from {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify executor (supports relay)
    executor_peer_id = payload.get("executor_peer_id")
    if not _validate_relay_sender(peer_id, executor_peer_id, payload):
        plugin.log(
            f"cl-hive: SETTLEMENT_EXECUTED executor mismatch from {peer_id[:16]}...",
            level='warn'
        )
        return {"result": "continue"}

    # Phase C: Persistent idempotency check
    is_new, event_id = check_and_record(database, "SETTLEMENT_EXECUTED", payload, executor_peer_id or peer_id)
    if not is_new:
        plugin.log(f"cl-hive: SETTLEMENT_EXECUTED duplicate event {event_id}, skipping", level='debug')
        _emit_ack(peer_id, event_id)
        _relay_message(HiveMessageType.SETTLEMENT_EXECUTED, payload, peer_id)
        return {"result": "continue"}
    if event_id:
        payload["_event_id"] = event_id

    # Verify original sender is a hive member and not banned
    sender = database.get_member(executor_peer_id)
    if not sender or database.is_banned(executor_peer_id):
        plugin.log(f"cl-hive: SETTLEMENT_EXECUTED from non-member {executor_peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Verify signature
    signature = payload.get("signature")
    signing_payload = get_settlement_executed_signing_payload(payload)
    try:
        verify_result = plugin.rpc.call("checkmessage", {
            "message": signing_payload,
            "zbase": signature,
            "pubkey": executor_peer_id
        })
        if not verify_result.get("verified"):
            plugin.log(f"cl-hive: SETTLEMENT_EXECUTED invalid signature from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: SETTLEMENT_EXECUTED signature check failed: {e}", level='warn')
        return {"result": "continue"}

    proposal_id = payload.get("proposal_id")
    payment_hash = payload.get("payment_hash")
    plan_hash = payload.get("plan_hash")
    amount_paid = payload.get("total_sent_sats", payload.get("amount_paid_sats", 0)) or 0

    # Ignore executions for unknown proposals.
    if not database.get_settlement_proposal(proposal_id):
        plugin.log(
            f"cl-hive: SETTLEMENT_EXECUTED for unknown proposal {proposal_id[:16]}...",
            level='debug'
        )
        return {"result": "continue"}

    # Record the execution
    if database.add_settlement_execution(
        proposal_id=proposal_id,
        executor_peer_id=executor_peer_id,
        signature=signature,
        payment_hash=payment_hash,
        amount_paid_sats=amount_paid,
        plan_hash=plan_hash,
    ):
        is_relayed = _is_relayed_message(payload)
        relay_info = " (relayed)" if is_relayed else ""
        if amount_paid > 0:
            plugin.log(
                f"SETTLEMENT: {executor_peer_id[:16]}...{relay_info} executed payment of {amount_paid} sats "
                f"for {proposal_id[:16]}..."
            )
        else:
            plugin.log(
                f"SETTLEMENT: {executor_peer_id[:16]}...{relay_info} confirmed execution for {proposal_id[:16]}..."
            )

        # Check if settlement is complete
        settlement_mgr.check_and_complete_settlement(proposal_id)

    # Phase D: Acknowledge receipt
    _emit_ack(peer_id, payload.get("_event_id"))

    # Relay to other members
    _relay_message(HiveMessageType.SETTLEMENT_EXECUTED, payload, peer_id)

    return {"result": "continue"}


# =============================================================================
# PHASE 10: TASK DELEGATION MESSAGE HANDLERS
# =============================================================================

def handle_task_request(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle TASK_REQUEST message from a hive member.

    When another member can't complete a task (e.g., peer rejected their
    channel open), they can delegate it to us.
    """
    if not task_mgr or not database:
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "TASK_REQUEST"):
        return {"result": "continue"}

    # Verify sender is a hive member and not banned
    sender = database.get_member(peer_id)
    if not sender or database.is_banned(peer_id):
        plugin.log(f"cl-hive: TASK_REQUEST from non-member {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # SECURITY: Verify signature
    requester_id = payload.get("requester_id", peer_id)
    signature = payload.get("signature")
    if not signature:
        plugin.log(f"cl-hive: TASK_REQUEST missing signature from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    from modules.protocol import get_task_request_signing_payload
    signing_payload = get_task_request_signing_payload(payload)
    try:
        verify_result = plugin.rpc.checkmessage(signing_payload, signature)
        if not verify_result.get("verified") or verify_result.get("pubkey") != requester_id:
            plugin.log(f"cl-hive: TASK_REQUEST invalid signature from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: TASK_REQUEST signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # Phase C: Persistent idempotency check
    is_new, event_id = check_and_record(database, "TASK_REQUEST", payload, peer_id)
    if not is_new:
        plugin.log(f"cl-hive: TASK_REQUEST duplicate event {event_id}, skipping", level='debug')
        _emit_ack(peer_id, event_id)
        return {"result": "continue"}
    if event_id:
        payload["_event_id"] = event_id

    # Delegate to task manager
    result = task_mgr.handle_task_request(peer_id, payload, plugin.rpc)

    if result.get("status") == "accepted":
        plugin.log(
            f"cl-hive: Accepted task {result.get('request_id', '')} from {peer_id[:16]}...",
            level='info'
        )
    elif result.get("status") == "rejected":
        plugin.log(
            f"cl-hive: Rejected task from {peer_id[:16]}...: {result.get('reason', 'unknown')}",
            level='debug'
        )
    elif result.get("error"):
        plugin.log(
            f"cl-hive: TASK_REQUEST error from {peer_id[:16]}...: {result.get('error')}",
            level='debug'
        )

    # Phase D: Acknowledge receipt
    _emit_ack(peer_id, payload.get("_event_id"))

    return {"result": "continue"}


def handle_task_response(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle TASK_RESPONSE message from a hive member.

    When we've delegated a task to another member, they send back
    the result (accepted/rejected/completed/failed).
    """
    if not task_mgr or not database:
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_INTELLIGENCE_AGE_SECONDS, "TASK_RESPONSE"):
        return {"result": "continue"}

    # Verify sender is a hive member
    sender = database.get_member(peer_id)
    if not sender:
        plugin.log(f"cl-hive: TASK_RESPONSE from non-member {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # SECURITY: Verify signature
    responder_id = payload.get("responder_id", peer_id)
    signature = payload.get("signature")
    if not signature:
        plugin.log(f"cl-hive: TASK_RESPONSE missing signature from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    from modules.protocol import get_task_response_signing_payload
    signing_payload = get_task_response_signing_payload(payload)
    try:
        verify_result = plugin.rpc.checkmessage(signing_payload, signature)
        if not verify_result.get("verified") or verify_result.get("pubkey") != responder_id:
            plugin.log(f"cl-hive: TASK_RESPONSE invalid signature from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: TASK_RESPONSE signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # Phase C: Persistent idempotency check
    is_new, event_id = check_and_record(database, "TASK_RESPONSE", payload, peer_id)
    if not is_new:
        plugin.log(f"cl-hive: TASK_RESPONSE duplicate event {event_id}, skipping", level='debug')
        _emit_ack(peer_id, event_id)
        return {"result": "continue"}
    if event_id:
        payload["_event_id"] = event_id

    # Delegate to task manager
    result = task_mgr.handle_task_response(peer_id, payload, plugin.rpc)

    if result.get("status") == "processed":
        response_status = result.get("response_status", "")
        request_id = result.get("request_id", "")
        plugin.log(
            f"cl-hive: Task {request_id} response: {response_status}",
            level='info'
        )
    elif result.get("error"):
        plugin.log(
            f"cl-hive: TASK_RESPONSE error from {peer_id[:16]}...: {result.get('error')}",
            level='debug'
        )

    # Phase D: Acknowledge receipt + implicit ack (TASK_RESPONSE implies TASK_REQUEST received)
    _emit_ack(peer_id, payload.get("_event_id"))
    if outbox_mgr:
        outbox_mgr.process_implicit_ack(peer_id, HiveMessageType.TASK_RESPONSE, payload)

    return {"result": "continue"}


# =============================================================================
# PHASE 11: HIVE-SPLICE MESSAGE HANDLERS
# =============================================================================

def handle_splice_init_request(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle SPLICE_INIT_REQUEST message from a hive member.

    When another member wants to initiate a splice with us.
    """
    if not splice_mgr or not database:
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_SETTLEMENT_AGE_SECONDS, "SPLICE_INIT_REQUEST"):
        return {"result": "continue"}

    # Verify sender is a hive member and not banned
    sender = database.get_member(peer_id)
    if not sender or database.is_banned(peer_id):
        plugin.log(f"cl-hive: SPLICE_INIT_REQUEST from non-member {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # SECURITY: Identity binding — splice messages are NOT relayed,
    # so initiator_id must match the transport-layer peer_id
    initiator_id = payload.get("initiator_id", peer_id)
    if initiator_id != peer_id:
        plugin.log(f"cl-hive: SPLICE_INIT_REQUEST identity mismatch: initiator {initiator_id[:16]}... != peer {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    # SECURITY: Verify signature
    signature = payload.get("signature")
    if not signature:
        plugin.log(f"cl-hive: SPLICE_INIT_REQUEST missing signature from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    from modules.protocol import get_splice_init_request_signing_payload
    signing_payload = get_splice_init_request_signing_payload(payload)
    try:
        verify_result = plugin.rpc.checkmessage(signing_payload, signature)
        if not verify_result.get("verified") or verify_result.get("pubkey") != initiator_id:
            plugin.log(f"cl-hive: SPLICE_INIT_REQUEST invalid signature from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: SPLICE_INIT_REQUEST signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # Phase C: Persistent idempotency check
    is_new, event_id = check_and_record(database, "SPLICE_INIT_REQUEST", payload, peer_id)
    if not is_new:
        plugin.log(f"cl-hive: SPLICE_INIT_REQUEST duplicate event {event_id}, skipping", level='debug')
        _emit_ack(peer_id, event_id)
        return {"result": "continue"}

    # Delegate to splice manager
    result = splice_mgr.handle_splice_init_request(peer_id, payload, plugin.rpc)

    if result.get("success"):
        plugin.log(
            f"cl-hive: Accepted splice {result.get('session_id', '')} from {peer_id[:16]}...",
            level='info'
        )
    elif result.get("error"):
        plugin.log(
            f"cl-hive: SPLICE_INIT_REQUEST error from {peer_id[:16]}...: {result.get('error')}",
            level='debug'
        )

    # Phase D: Acknowledge receipt
    _emit_ack(peer_id, payload.get("_event_id"))

    return {"result": "continue"}


def handle_splice_init_response(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle SPLICE_INIT_RESPONSE message from a hive member.

    When a peer responds to our splice init request.
    """
    if not splice_mgr or not database:
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_SETTLEMENT_AGE_SECONDS, "SPLICE_INIT_RESPONSE"):
        return {"result": "continue"}

    # Verify sender is a hive member and not banned
    sender = database.get_member(peer_id)
    if not sender or database.is_banned(peer_id):
        plugin.log(f"cl-hive: SPLICE_INIT_RESPONSE from non-member/banned {peer_id[:16]}...", level='debug')
        return {"result": "continue"}

    # SECURITY: Identity binding — splice messages are NOT relayed,
    # so responder_id must match the transport-layer peer_id
    responder_id = payload.get("responder_id", peer_id)
    if responder_id != peer_id:
        plugin.log(f"cl-hive: SPLICE_INIT_RESPONSE identity mismatch: responder {responder_id[:16]}... != peer {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    # SECURITY: Verify signature
    signature = payload.get("signature")
    if not signature:
        plugin.log(f"cl-hive: SPLICE_INIT_RESPONSE missing signature from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    from modules.protocol import get_splice_init_response_signing_payload
    signing_payload = get_splice_init_response_signing_payload(payload)
    try:
        verify_result = plugin.rpc.checkmessage(signing_payload, signature)
        if not verify_result.get("verified") or verify_result.get("pubkey") != responder_id:
            plugin.log(f"cl-hive: SPLICE_INIT_RESPONSE invalid signature from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: SPLICE_INIT_RESPONSE signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # Phase C: Persistent idempotency check
    is_new, event_id = check_and_record(database, "SPLICE_INIT_RESPONSE", payload, responder_id)
    if not is_new:
        plugin.log(f"cl-hive: SPLICE_INIT_RESPONSE duplicate event {event_id}, skipping", level='debug')
        _emit_ack(peer_id, event_id)
        return {"result": "continue"}

    # Delegate to splice manager
    result = splice_mgr.handle_splice_init_response(peer_id, payload, plugin.rpc)

    if result.get("rejected"):
        plugin.log(
            f"cl-hive: Splice rejected by {peer_id[:16]}...: {result.get('reason', 'unknown')}",
            level='info'
        )
    elif result.get("success"):
        plugin.log(
            f"cl-hive: Splice {result.get('session_id', '')} response received",
            level='debug'
        )

    # Phase D: Acknowledge receipt + implicit ack (SPLICE_INIT_RESPONSE implies SPLICE_INIT_REQUEST received)
    _emit_ack(peer_id, event_id)
    if outbox_mgr:
        outbox_mgr.process_implicit_ack(peer_id, HiveMessageType.SPLICE_INIT_RESPONSE, payload)

    return {"result": "continue"}


def handle_splice_update(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle SPLICE_UPDATE message during splice negotiation.
    """
    if not splice_mgr or not database:
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_SETTLEMENT_AGE_SECONDS, "SPLICE_UPDATE"):
        return {"result": "continue"}

    # Verify sender is a hive member and not banned
    sender = database.get_member(peer_id)
    if not sender or database.is_banned(peer_id):
        return {"result": "continue"}

    # SECURITY: Identity binding — splice messages are NOT relayed
    sender_id_field = payload.get("sender_id", peer_id)
    if sender_id_field != peer_id:
        plugin.log(f"cl-hive: SPLICE_UPDATE identity mismatch: sender {sender_id_field[:16]}... != peer {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    # SECURITY: Verify signature
    signature = payload.get("signature")
    if not signature:
        plugin.log(f"cl-hive: SPLICE_UPDATE missing signature from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    from modules.protocol import get_splice_update_signing_payload
    signing_payload = get_splice_update_signing_payload(payload)
    try:
        verify_result = plugin.rpc.checkmessage(signing_payload, signature)
        if not verify_result.get("verified") or verify_result.get("pubkey") != sender_id_field:
            plugin.log(f"cl-hive: SPLICE_UPDATE invalid signature from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: SPLICE_UPDATE signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # Phase C: Persistent idempotency check
    is_new, event_id = check_and_record(database, "SPLICE_UPDATE", payload, peer_id)
    if not is_new:
        plugin.log(f"cl-hive: SPLICE_UPDATE duplicate event {event_id}, skipping", level='debug')
        _emit_ack(peer_id, event_id)
        return {"result": "continue"}

    # Delegate to splice manager
    result = splice_mgr.handle_splice_update(peer_id, payload, plugin.rpc)

    if result.get("error"):
        plugin.log(
            f"cl-hive: SPLICE_UPDATE error: {result.get('error')}",
            level='debug'
        )

    # Phase D: Acknowledge receipt
    _emit_ack(peer_id, payload.get("_event_id"))

    return {"result": "continue"}


def handle_splice_signed(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle SPLICE_SIGNED message with final PSBT or txid.
    """
    if not splice_mgr or not database:
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_SETTLEMENT_AGE_SECONDS, "SPLICE_SIGNED"):
        return {"result": "continue"}

    # Verify sender is a hive member and not banned
    sender = database.get_member(peer_id)
    if not sender or database.is_banned(peer_id):
        return {"result": "continue"}

    # SECURITY: Identity binding — splice messages are NOT relayed
    sender_id_field = payload.get("sender_id", peer_id)
    if sender_id_field != peer_id:
        plugin.log(f"cl-hive: SPLICE_SIGNED identity mismatch: sender {sender_id_field[:16]}... != peer {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    # SECURITY: Verify signature
    signature = payload.get("signature")
    if not signature:
        plugin.log(f"cl-hive: SPLICE_SIGNED missing signature from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    from modules.protocol import get_splice_signed_signing_payload
    signing_payload = get_splice_signed_signing_payload(payload)
    try:
        verify_result = plugin.rpc.checkmessage(signing_payload, signature)
        if not verify_result.get("verified") or verify_result.get("pubkey") != sender_id_field:
            plugin.log(f"cl-hive: SPLICE_SIGNED invalid signature from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: SPLICE_SIGNED signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # Phase C: Persistent idempotency check
    is_new, event_id = check_and_record(database, "SPLICE_SIGNED", payload, peer_id)
    if not is_new:
        plugin.log(f"cl-hive: SPLICE_SIGNED duplicate event {event_id}, skipping", level='debug')
        _emit_ack(peer_id, event_id)
        return {"result": "continue"}

    # Delegate to splice manager
    result = splice_mgr.handle_splice_signed(peer_id, payload, plugin.rpc)

    if result.get("txid"):
        plugin.log(
            f"cl-hive: Splice {result.get('session_id', '')} completed: txid={result.get('txid')[:16]}...",
            level='info'
        )
    elif result.get("error"):
        plugin.log(
            f"cl-hive: SPLICE_SIGNED error: {result.get('error')}",
            level='debug'
        )

    # Phase D: Acknowledge receipt
    _emit_ack(peer_id, payload.get("_event_id"))

    return {"result": "continue"}


def handle_splice_abort(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle SPLICE_ABORT message when peer aborts splice.
    """
    if not splice_mgr or not database:
        return {"result": "continue"}

    # SECURITY: Timestamp freshness check
    if not _check_timestamp_freshness(payload, MAX_SETTLEMENT_AGE_SECONDS, "SPLICE_ABORT"):
        return {"result": "continue"}

    # Verify sender is a hive member and not banned
    sender = database.get_member(peer_id)
    if not sender or database.is_banned(peer_id):
        return {"result": "continue"}

    # SECURITY: Identity binding — splice messages are NOT relayed
    sender_id_field = payload.get("sender_id", peer_id)
    if sender_id_field != peer_id:
        plugin.log(f"cl-hive: SPLICE_ABORT identity mismatch: sender {sender_id_field[:16]}... != peer {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    # SECURITY: Verify signature
    signature = payload.get("signature")
    if not signature:
        plugin.log(f"cl-hive: SPLICE_ABORT missing signature from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    from modules.protocol import get_splice_abort_signing_payload
    signing_payload = get_splice_abort_signing_payload(payload)
    try:
        verify_result = plugin.rpc.checkmessage(signing_payload, signature)
        if not verify_result.get("verified") or verify_result.get("pubkey") != sender_id_field:
            plugin.log(f"cl-hive: SPLICE_ABORT invalid signature from {peer_id[:16]}...", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: SPLICE_ABORT signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # Phase C: Persistent idempotency check
    is_new, event_id = check_and_record(database, "SPLICE_ABORT", payload, peer_id)
    if not is_new:
        plugin.log(f"cl-hive: SPLICE_ABORT duplicate event {event_id}, skipping", level='debug')
        _emit_ack(peer_id, event_id)
        return {"result": "continue"}

    # Delegate to splice manager
    result = splice_mgr.handle_splice_abort(peer_id, payload, plugin.rpc)

    if result.get("aborted"):
        plugin.log(
            f"cl-hive: Splice aborted by {peer_id[:16]}...: {result.get('reason', 'unknown')}",
            level='info'
        )

    # Phase D: Acknowledge receipt
    _emit_ack(peer_id, payload.get("_event_id"))

    return {"result": "continue"}


# =============================================================================
# MCF (Min-Cost Max-Flow) MESSAGE HANDLERS
# =============================================================================


def handle_mcf_needs_batch(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle MCF_NEEDS_BATCH message from fleet members.

    Fleet members broadcast their liquidity needs to the coordinator.
    The coordinator collects these needs to build the MCF optimization network.
    """
    if not database or not cost_reduction_mgr:
        return {"result": "continue"}

    # Validate payload structure
    if not validate_mcf_needs_batch(payload):
        plugin.log(
            f"cl-hive: Invalid MCF_NEEDS_BATCH from {peer_id[:16]}...",
            level='warn'
        )
        return {"result": "continue"}

    reporter_id = payload.get("reporter_id", "")
    timestamp = payload.get("timestamp", 0)
    signature = payload.get("signature", "")
    needs = payload.get("needs", [])

    # Identity binding: peer_id must match claimed reporter
    if peer_id != reporter_id:
        plugin.log(
            f"cl-hive: MCF_NEEDS_BATCH identity mismatch: {peer_id[:16]} != {reporter_id[:16]}",
            level='warn'
        )
        return {"result": "continue"}

    # Verify sender is a hive member
    sender = database.get_member(peer_id)
    if not sender:
        plugin.log(
            f"cl-hive: MCF_NEEDS_BATCH from non-member {peer_id[:16]}...",
            level='debug'
        )
        return {"result": "continue"}

    # Verify signature
    signing_payload = get_mcf_needs_batch_signing_payload(payload)
    try:
        result = plugin.rpc.checkmessage(signing_payload, signature)
        if not result.get("verified") or result.get("pubkey") != reporter_id:
            plugin.log(
                f"cl-hive: MCF_NEEDS_BATCH signature invalid from {peer_id[:16]}...",
                level='warn'
            )
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: MCF needs batch signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # Only the coordinator needs to process needs
    coordinator_id = cost_reduction_mgr.get_current_mcf_coordinator()
    if coordinator_id != our_pubkey:
        # Not coordinator, ignore (but don't log - this is expected)
        return {"result": "continue"}

    # Store needs for MCF optimization
    stored_count = 0
    for need in needs:
        # Add reporter_id to each need
        need["reporter_id"] = reporter_id
        need["received_at"] = int(time.time())
        if liquidity_coord:
            # Store via liquidity coordinator
            liquidity_coord.store_remote_mcf_need(need)
            stored_count += 1

    if stored_count > 0:
        plugin.log(
            f"cl-hive: Received {stored_count} MCF need(s) from {reporter_id[:16]}...",
            level='debug'
        )

    return {"result": "continue"}


def handle_mcf_solution_broadcast(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle MCF_SOLUTION_BROADCAST message from coordinator.

    The coordinator broadcasts a complete MCF solution containing assignments
    for all fleet members. Each member extracts their own assignments and
    stores them for execution.
    """
    if not database or not liquidity_coord:
        return {"result": "continue"}

    # Validate payload structure
    if not validate_mcf_solution_broadcast(payload):
        plugin.log(
            f"cl-hive: Invalid MCF_SOLUTION_BROADCAST from {peer_id[:16]}...",
            level='warn'
        )
        return {"result": "continue"}

    coordinator_id = payload.get("coordinator_id", "")
    timestamp = payload.get("timestamp", 0)
    signature = payload.get("signature", "")
    assignments = payload.get("assignments", [])

    # Reject stale or replayed solutions
    from modules.mcf_solver import MAX_SOLUTION_AGE as _MCF_MAX_SOL_AGE
    now = int(time.time())
    if timestamp > 0 and abs(now - timestamp) > _MCF_MAX_SOL_AGE:
        plugin.log(
            f"cl-hive: MCF_SOLUTION_BROADCAST stale/future timestamp from {peer_id[:16]}... "
            f"(age={now - timestamp}s, max={_MCF_MAX_SOL_AGE}s)",
            level='warn'
        )
        return {"result": "continue"}

    # Identity binding: peer_id must match claimed coordinator
    if peer_id != coordinator_id:
        plugin.log(
            f"cl-hive: MCF_SOLUTION_BROADCAST identity mismatch: {peer_id[:16]} != {coordinator_id[:16]}",
            level='warn'
        )
        return {"result": "continue"}

    # Verify sender is a hive member
    sender = database.get_member(peer_id)
    if not sender:
        plugin.log(
            f"cl-hive: MCF_SOLUTION_BROADCAST from non-member {peer_id[:16]}...",
            level='debug'
        )
        return {"result": "continue"}

    # Verify signature
    signing_payload = get_mcf_solution_signing_payload(payload)
    try:
        result = plugin.rpc.checkmessage(signing_payload, signature)
        if not result.get("verified") or result.get("pubkey") != coordinator_id:
            plugin.log(
                f"cl-hive: MCF_SOLUTION_BROADCAST signature invalid from {peer_id[:16]}...",
                level='warn'
            )
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: MCF signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # Extract our assignments
    our_id = our_pubkey
    our_assignments = [a for a in assignments if a.get("member_id") == our_id]

    if not our_assignments:
        plugin.log(
            f"cl-hive: MCF solution received with no assignments for us (total: {len(assignments)})",
            level='debug'
        )
        return {"result": "continue"}

    # Store each assignment
    accepted_count = 0
    for assignment_data in our_assignments:
        if liquidity_coord.receive_mcf_assignment(assignment_data, timestamp, coordinator_id):
            accepted_count += 1

    if accepted_count > 0:
        plugin.log(
            f"cl-hive: Received {accepted_count} MCF assignment(s) from coordinator {coordinator_id[:16]}...",
            level='info'
        )
        # Send ACK back to coordinator
        _send_mcf_ack(coordinator_id, timestamp, accepted_count)

    return {"result": "continue"}


def handle_mcf_assignment_ack(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle MCF_ASSIGNMENT_ACK message (coordinator receives from members).

    Members send this ACK after receiving their MCF assignments to confirm
    they will attempt to execute them.
    """
    if not database or not cost_reduction_mgr:
        return {"result": "continue"}

    # Validate payload structure
    if not validate_mcf_assignment_ack(payload):
        plugin.log(
            f"cl-hive: Invalid MCF_ASSIGNMENT_ACK from {peer_id[:16]}...",
            level='warn'
        )
        return {"result": "continue"}

    member_id = payload.get("member_id", "")
    timestamp = payload.get("timestamp", 0)
    solution_timestamp = payload.get("solution_timestamp", 0)
    assignment_count = payload.get("assignment_count", 0)
    signature = payload.get("signature", "")

    # Identity binding
    if peer_id != member_id:
        plugin.log(
            f"cl-hive: MCF_ASSIGNMENT_ACK identity mismatch: {peer_id[:16]} != {member_id[:16]}",
            level='warn'
        )
        return {"result": "continue"}

    # Verify sender is a hive member
    sender = database.get_member(peer_id)
    if not sender:
        return {"result": "continue"}

    # Verify signature
    signing_payload = get_mcf_assignment_ack_signing_payload(payload)
    try:
        result = plugin.rpc.checkmessage(signing_payload, signature)
        if not result.get("verified") or result.get("pubkey") != member_id:
            plugin.log(
                f"cl-hive: MCF_ASSIGNMENT_ACK signature invalid from {peer_id[:16]}...",
                level='warn'
            )
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: MCF ACK signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # Only process if we are the coordinator
    if our_pubkey != cost_reduction_mgr.get_current_mcf_coordinator():
        return {"result": "continue"}

    # Record the ACK
    cost_reduction_mgr.record_mcf_ack(member_id, solution_timestamp, assignment_count)

    plugin.log(
        f"cl-hive: MCF ACK from {member_id[:16]}... ({assignment_count} assignments)",
        level='debug'
    )

    return {"result": "continue"}


def handle_mcf_completion_report(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle MCF_COMPLETION_REPORT message (member reports assignment outcome).

    After executing (or failing to execute) an MCF assignment, members report
    the outcome so the coordinator can track fleet-wide rebalancing progress.
    """
    if not database or not cost_reduction_mgr:
        return {"result": "continue"}

    # Only the coordinator should process completion reports
    if our_pubkey != cost_reduction_mgr.get_current_mcf_coordinator():
        return {"result": "continue"}

    # Validate payload structure
    if not validate_mcf_completion_report(payload):
        plugin.log(
            f"cl-hive: Invalid MCF_COMPLETION_REPORT from {peer_id[:16]}...",
            level='warn'
        )
        return {"result": "continue"}

    member_id = payload.get("member_id", "")
    timestamp = payload.get("timestamp", 0)
    assignment_id = payload.get("assignment_id", "")
    success = payload.get("success", False)
    actual_amount = payload.get("actual_amount_sats", 0)
    actual_cost = payload.get("actual_cost_sats", 0)
    failure_reason = payload.get("failure_reason", "")
    signature = payload.get("signature", "")

    # Identity binding
    if peer_id != member_id:
        plugin.log(
            f"cl-hive: MCF_COMPLETION_REPORT identity mismatch",
            level='warn'
        )
        return {"result": "continue"}

    # Verify sender is a hive member
    sender = database.get_member(peer_id)
    if not sender:
        return {"result": "continue"}

    # Verify signature
    signing_payload = get_mcf_completion_signing_payload(payload)
    try:
        result = plugin.rpc.checkmessage(signing_payload, signature)
        if not result.get("verified") or result.get("pubkey") != member_id:
            plugin.log(
                f"cl-hive: MCF_COMPLETION_REPORT signature invalid from {peer_id[:16]}...",
                level='warn'
            )
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: MCF completion signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # Record completion (both coordinator and other members can track this)
    cost_reduction_mgr.record_mcf_completion(
        member_id=member_id,
        assignment_id=assignment_id,
        success=success,
        actual_amount_sats=actual_amount,
        actual_cost_sats=actual_cost,
        failure_reason=failure_reason
    )

    if success:
        plugin.log(
            f"cl-hive: MCF assignment {assignment_id[:20]} completed by {member_id[:16]}...: "
            f"{actual_amount} sats, cost {actual_cost} sats",
            level='info'
        )
    else:
        plugin.log(
            f"cl-hive: MCF assignment {assignment_id[:20]} failed by {member_id[:16]}...: {failure_reason}",
            level='info'
        )

    return {"result": "continue"}


def _send_mcf_ack(coordinator_id: str, solution_timestamp: int, assignment_count: int) -> bool:
    """
    Send MCF_ASSIGNMENT_ACK to the coordinator.

    Args:
        coordinator_id: Coordinator's pubkey
        solution_timestamp: Timestamp of the solution we're acknowledging
        assignment_count: Number of assignments we accepted

    Returns:
        True if sent successfully
    """
    if not liquidity_coord :
        return False

    ack_msg = liquidity_coord.create_mcf_ack_message()

    if not ack_msg:
        return False

    try:
        plugin.rpc.sendcustommsg(
            node_id=coordinator_id,
            msg=ack_msg.hex()
        )
        return True
    except Exception as e:
        plugin.log(f"cl-hive: Failed to send MCF ACK: {e}", level='debug')
        return False


def _broadcast_mcf_completion(assignment_id: str, success: bool,
                              actual_amount_sats: int, actual_cost_sats: int,
                              failure_reason: str = "") -> int:
    """
    Broadcast MCF_COMPLETION_REPORT to all hive members.

    Args:
        assignment_id: ID of the completed assignment
        success: Whether execution succeeded
        actual_amount_sats: Actual amount rebalanced
        actual_cost_sats: Actual cost incurred
        failure_reason: Reason for failure if not successful

    Returns:
        Number of members the message was sent to
    """
    if not liquidity_coord :
        return 0

    completion_msg = liquidity_coord.create_mcf_completion_message(
        assignment_id
    )

    if not completion_msg:
        return 0

    return _broadcast_to_members(completion_msg)


def _broadcast_settlement_offer(peer_id: str, bolt12_offer: str) -> int:
    """
    Broadcast a settlement offer to all hive members.

    Args:
        peer_id: The member's node public key
        bolt12_offer: The BOLT12 offer string

    Returns:
        Number of members the message was sent to
    """
    if not plugin or not handshake_mgr:
        return 0

    timestamp = int(time.time())

    # Sign the offer
    signing_payload = get_settlement_offer_signing_payload(peer_id, bolt12_offer)
    try:
        sign_result = plugin.rpc.call("signmessage", {"message": signing_payload})
        signature = sign_result.get("zbase")
        if not signature:
            plugin.log("cl-hive: Failed to sign settlement offer", level='warn')
            return 0
    except Exception as e:
        plugin.log(f"cl-hive: Failed to sign settlement offer: {e}", level='warn')
        return 0

    # Create the message
    msg = create_settlement_offer(peer_id, bolt12_offer, timestamp, signature)

    # Broadcast to all members
    sent = _broadcast_to_members(msg)
    if sent > 0:
        plugin.log(f"cl-hive: Broadcast settlement offer to {sent} member(s)")

    return sent


def _send_settlement_offer_to_peer(target_peer_id: str, our_peer_id: str, bolt12_offer: str) -> bool:
    """
    Send our settlement offer to a specific peer.

    Used when welcoming a new member to ensure they have our offer
    for settlement calculations.

    Args:
        target_peer_id: The peer to send to
        our_peer_id: Our node's public key
        bolt12_offer: Our BOLT12 offer string

    Returns:
        True if sent successfully, False otherwise
    """
    if not plugin:
        return False

    timestamp = int(time.time())

    # Sign the offer
    signing_payload = get_settlement_offer_signing_payload(our_peer_id, bolt12_offer)
    try:
        sign_result = plugin.rpc.call("signmessage", {"message": signing_payload})
        signature = sign_result.get("zbase")
        if not signature:
            plugin.log("cl-hive: Failed to sign settlement offer for peer", level='warn')
            return False
    except Exception as e:
        plugin.log(f"cl-hive: Failed to sign settlement offer: {e}", level='warn')
        return False

    # Create the message
    msg = create_settlement_offer(our_peer_id, bolt12_offer, timestamp, signature)

    # Send to the specific peer
    try:
        plugin.rpc.call("sendcustommsg", {
            "node_id": target_peer_id,
            "msg": msg.hex()
        })
        plugin.log(f"cl-hive: Sent settlement offer to new member {target_peer_id[:16]}...")
        return True
    except Exception as e:
        plugin.log(f"cl-hive: Failed to send settlement offer to {target_peer_id[:16]}...: {e}", level='debug')
        return False
