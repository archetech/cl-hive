"""
Phase 4A: Cashu Task Escrow — trustless conditional payments via Cashu ecash tokens.

Manages escrow ticket lifecycle (create, validate, redeem, refund), HTLC secret
generation, danger-to-pricing mapping, signed task execution receipts, and
optional Cashu mint interaction behind per-mint circuit breakers.

All data models, protocol messages, DB tables, and algorithms are pure Python.
Actual mint HTTP interaction is isolated behind MintCircuitBreaker — mint calls
are optional and gracefully disabled when no mints are configured.

Key patterns:
- MintCircuitBreaker: per-mint circuit breaker (reuses bridge.py pattern)
- Secret encryption at rest: XOR with signmessage-derived key
- Ticket types: single, batch, milestone, performance
- Danger-to-pricing: escalating escrow windows and base amounts
"""

import hashlib
import json
import logging
import os
import threading
import time
import concurrent.futures
import urllib.request
import urllib.error
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


# =============================================================================
# CONSTANTS
# =============================================================================

VALID_TICKET_TYPES = frozenset({"single", "batch", "milestone", "performance"})
VALID_TICKET_STATUSES = frozenset({"active", "redeemed", "refunded", "expired", "pending"})

# Mint HTTP timeout
MINT_HTTP_TIMEOUT = 10
MINT_EXECUTOR_WORKERS = 2

# Secret key derivation message (signed once at startup)
SECRET_KEY_DERIVATION_MSG = "escrow_key_derivation"

# Reputation tiers for pricing modifiers
REPUTATION_TIERS = frozenset({"newcomer", "recognized", "trusted", "senior"})


# =============================================================================
# DANGER-TO-PRICING TABLE
# =============================================================================

# Each entry: (min_danger, max_danger, base_min_sats, base_max_sats, window_seconds)
DANGER_PRICING_TABLE = [
    (1, 2, 0, 5, 3600),            # 1 hour
    (3, 3, 5, 15, 7200),           # 2 hours
    (4, 4, 15, 25, 21600),         # 6 hours
    (5, 5, 25, 50, 21600),         # 6 hours
    (6, 6, 50, 100, 86400),        # 24 hours
    (7, 7, 100, 250, 86400),       # 24 hours
    (8, 8, 250, 500, 259200),      # 72 hours
    (9, 9, 500, 750, 259200),      # 72 hours
    (10, 10, 750, 1000, 345600),   # 96 hours
]

# Reputation modifiers
REP_MODIFIER = {
    "newcomer": 1.5,
    "recognized": 1.0,
    "trusted": 0.75,
    "senior": 0.5,
}


# =============================================================================
# MINT CIRCUIT BREAKER
# =============================================================================

class MintCircuitState(Enum):
    """Mint circuit breaker states."""
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class MintCircuitBreaker:
    """
    Per-mint circuit breaker. Reuses pattern from bridge.py CircuitBreaker.

    State transitions:
    - CLOSED -> OPEN: After 5 consecutive failures
    - OPEN -> HALF_OPEN: After 60s timeout
    - HALF_OPEN -> CLOSED: After 3 consecutive successes
    - HALF_OPEN -> OPEN: On any failure
    """

    def __init__(self, mint_url: str, max_failures: int = 5,
                 reset_timeout: int = 60,
                 half_open_success_threshold: int = 3):
        self.mint_url = mint_url
        self.max_failures = max_failures
        self.reset_timeout = reset_timeout
        self.half_open_success_threshold = half_open_success_threshold

        self._lock = threading.RLock()
        self._state = MintCircuitState.CLOSED
        self._failure_count = 0
        self._half_open_success_count = 0
        self._last_failure_time = 0
        self._last_success_time = 0

    @property
    def state(self) -> MintCircuitState:
        """Get current state, checking for automatic OPEN -> HALF_OPEN."""
        with self._lock:
            if self._state == MintCircuitState.OPEN:
                now = int(time.time())
                if now - self._last_failure_time >= self.reset_timeout:
                    self._state = MintCircuitState.HALF_OPEN
            return self._state

    def is_available(self) -> bool:
        """Check if mint requests can be made (not OPEN)."""
        return self.state != MintCircuitState.OPEN

    def record_success(self) -> None:
        """Record a successful mint call."""
        with self._lock:
            self._failure_count = 0
            self._last_success_time = int(time.time())
            if self._state == MintCircuitState.HALF_OPEN:
                self._half_open_success_count += 1
                if self._half_open_success_count >= self.half_open_success_threshold:
                    self._state = MintCircuitState.CLOSED
                    self._half_open_success_count = 0
            else:
                self._half_open_success_count = 0

    def record_failure(self) -> None:
        """Record a failed mint call."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = int(time.time())
            if self._state == MintCircuitState.HALF_OPEN:
                self._state = MintCircuitState.OPEN
                self._half_open_success_count = 0
            elif self._failure_count >= self.max_failures:
                self._state = MintCircuitState.OPEN

    def reset(self) -> None:
        """Reset circuit breaker to initial state."""
        with self._lock:
            self._state = MintCircuitState.CLOSED
            self._failure_count = 0
            self._half_open_success_count = 0
            self._last_failure_time = 0

    def get_stats(self) -> Dict[str, Any]:
        """Get circuit breaker statistics."""
        with self._lock:
            return {
                "mint_url": self.mint_url,
                "state": self.state.value,
                "failure_count": self._failure_count,
                "half_open_success_count": self._half_open_success_count,
                "last_failure_time": self._last_failure_time,
                "last_success_time": self._last_success_time,
            }


# =============================================================================
# CASHU ESCROW MANAGER
# =============================================================================

class CashuEscrowManager:
    """
    Cashu escrow ticket lifecycle: create, validate, redeem, refund.

    Manages HTLC secrets, danger-based pricing, task execution receipts,
    and optional Cashu mint HTTP interaction behind circuit breakers.
    """

    MAX_ACTIVE_TICKETS = 500
    MAX_ESCROW_TICKET_ROWS = 50_000
    MAX_ESCROW_SECRET_ROWS = 50_000
    MAX_ESCROW_RECEIPT_ROWS = 100_000
    SECRET_RETENTION_DAYS = 90

    def __init__(self, database, plugin, rpc=None, our_pubkey: str = "",
                 acceptable_mints: Optional[List[str]] = None):
        """
        Initialize the Cashu escrow manager.

        Args:
            database: HiveDatabase instance
            plugin: pyln Plugin for logging
            rpc: RPC interface for signmessage/checkmessage
            our_pubkey: Our node's public key
            acceptable_mints: List of acceptable Cashu mint URLs
        """
        self.db = database
        self.plugin = plugin
        self.rpc = rpc
        self.our_pubkey = our_pubkey
        self.acceptable_mints = acceptable_mints or []

        # Per-mint circuit breakers
        self._mint_breakers: Dict[str, MintCircuitBreaker] = {}
        self._breaker_lock = threading.Lock()
        self._mint_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=MINT_EXECUTOR_WORKERS,
            thread_name_prefix="cl-hive-cashu",
        )

        # Lock for ticket status transitions (redeem/refund atomicity)
        self._ticket_lock = threading.Lock()

        # Encryption key for secrets at rest (derived at startup)
        self._secret_key: Optional[bytes] = None
        self._derive_secret_key()

    def _log(self, msg: str, level: str = 'info') -> None:
        """Log with prefix."""
        self.plugin.log(f"cl-hive: escrow: {msg}", level=level)

    def _derive_secret_key(self) -> None:
        """Derive secret encryption key from signmessage. Best-effort at init."""
        if not self.rpc:
            return
        try:
            result = self.rpc.signmessage(SECRET_KEY_DERIVATION_MSG)
            sig = result.get("zbase", "") if isinstance(result, dict) else ""
            if sig:
                # Use SHA256 of the signature as the XOR key (32 bytes)
                self._secret_key = hashlib.sha256(sig.encode('utf-8')).digest()
        except Exception as e:
            self._log(f"secret key derivation failed (non-fatal): {e}", level='warn')

    def _encrypt_secret(self, secret_hex: str) -> str:
        """XOR-encrypt a hex secret with the derived key. Returns hex."""
        if not self._secret_key:
            self._log("secret key unavailable — storing secret as plaintext", level='warn')
            return secret_hex  # No key available, store plaintext
        secret_bytes = bytes.fromhex(secret_hex)
        key = self._secret_key
        encrypted = bytes(s ^ key[i % len(key)] for i, s in enumerate(secret_bytes))
        return encrypted.hex()

    def _decrypt_secret(self, encrypted_hex: str) -> str:
        """XOR-decrypt a hex secret with the derived key. Returns hex."""
        # XOR is symmetric
        return self._encrypt_secret(encrypted_hex)

    def _get_breaker(self, mint_url: str) -> MintCircuitBreaker:
        """Get or create circuit breaker for a mint URL."""
        with self._breaker_lock:
            if mint_url not in self._mint_breakers:
                self._mint_breakers[mint_url] = MintCircuitBreaker(mint_url)
            return self._mint_breakers[mint_url]

    def _mint_http_call(self, mint_url: str, path: str,
                        method: str = "GET",
                        body: Optional[bytes] = None) -> Optional[Dict]:
        """
        Make an HTTP call to a Cashu mint with circuit breaker protection.

        Returns parsed JSON response or None on failure.
        """
        breaker = self._get_breaker(mint_url)
        if not breaker.is_available():
            self._log(f"mint circuit OPEN for {mint_url}, skipping", level='debug')
            return None

        url = mint_url.rstrip('/') + path

        if not self._mint_executor:
            self._log("mint executor unavailable, skipping call", level='warn')
            return None

        def _http_request() -> Dict:
            req = urllib.request.Request(url, data=body, method=method)
            if body:
                req.add_header('Content-Type', 'application/json')
            with urllib.request.urlopen(req, timeout=MINT_HTTP_TIMEOUT) as resp:
                return json.loads(resp.read(1_048_576).decode('utf-8'))

        try:
            future = self._mint_executor.submit(_http_request)
            data = future.result(timeout=MINT_HTTP_TIMEOUT + 1)
            breaker.record_success()
            return data
        except concurrent.futures.TimeoutError:
            future.cancel()
            breaker.record_failure()
            self._log(f"mint call timed out {mint_url}{path}", level='debug')
            return None
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                json.JSONDecodeError, ValueError, RuntimeError) as e:
            breaker.record_failure()
            self._log(f"mint call failed {mint_url}{path}: {e}", level='debug')
            return None

    def shutdown(self) -> None:
        """Shutdown mint executor threads."""
        executor = self._mint_executor
        self._mint_executor = None
        if not executor:
            return
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception as e:
            self._log(f"mint executor shutdown failed: {e}", level='debug')

    # =========================================================================
    # SECRET MANAGEMENT
    # =========================================================================

    def generate_secret(self, task_id: str, ticket_id: str) -> Optional[str]:
        """
        Generate and persist an HTLC secret for a task.

        Returns H(secret) hex string, or None on failure.
        """
        if not self.db:
            return None

        # Check row cap
        count = self.db.count_escrow_secrets()
        if count >= self.MAX_ESCROW_SECRET_ROWS:
            self._log("escrow_secrets at cap, rejecting", level='warn')
            return None

        # Generate 32 bytes of randomness
        secret_bytes = os.urandom(32)
        secret_hex = secret_bytes.hex()
        hash_hex = hashlib.sha256(secret_bytes).hexdigest()

        # Encrypt and store
        encrypted = self._encrypt_secret(secret_hex)
        success = self.db.store_escrow_secret(
            task_id=task_id,
            ticket_id=ticket_id,
            secret_hex=encrypted,
            hash_hex=hash_hex,
        )
        if not success:
            return None

        return hash_hex

    def reveal_secret(self, task_id: str, caller_id: Optional[str] = None,
                      require_receipt: bool = True) -> Optional[str]:
        """
        Return the HTLC preimage for a completed task.

        Args:
            task_id: The task whose secret to reveal.
            caller_id: If provided, must match ticket's operator_id.
            require_receipt: If True (default), a successful receipt must
                exist for this ticket before the secret is revealed.

        Returns decrypted secret hex, or None if authorization fails or not found.
        """
        if not self.db:
            return None

        record = self.db.get_escrow_secret(task_id)
        if not record:
            return None

        ticket_id = record.get('ticket_id', '')

        # Authorization: caller must be the operator
        if caller_id is not None:
            ticket = self.db.get_escrow_ticket(ticket_id) if ticket_id else None
            if not ticket or ticket.get('operator_id') != caller_id:
                self._log(f"reveal_secret denied: caller {caller_id[:16]}... "
                          f"is not ticket operator", level='warn')
                return None

        # Require a successful receipt before revealing the secret
        if require_receipt and ticket_id:
            receipts = self.db.get_escrow_receipts(ticket_id)
            has_success = any(r.get('success') == 1 or r.get('success') is True
                             for r in (receipts or []))
            if not has_success:
                self._log(f"reveal_secret denied: no successful receipt "
                          f"for ticket {ticket_id[:16]}...", level='warn')
                return None

        secret_hex = self._decrypt_secret(record['secret_hex'])

        # Mark as revealed
        self.db.reveal_escrow_secret(task_id, int(time.time()))

        return secret_hex

    # =========================================================================
    # TICKET CREATION & VALIDATION
    # =========================================================================

    def get_pricing(self, danger_score: int,
                    reputation_tier: str = "newcomer") -> Dict[str, Any]:
        """
        Calculate dynamic pricing based on danger score and reputation.

        Returns dict with base_sats, escrow_window_seconds, rep_modifier.
        """
        danger_score = max(1, min(10, danger_score))
        rep_tier = reputation_tier if reputation_tier in REP_MODIFIER else "newcomer"
        modifier = REP_MODIFIER[rep_tier]

        for min_d, max_d, base_min, base_max, window in DANGER_PRICING_TABLE:
            if min_d <= danger_score <= max_d:
                # Linear interpolation within the band
                if max_d > min_d:
                    t = (danger_score - min_d) / (max_d - min_d)
                else:
                    t = 0.5
                base_sats = int(base_min + t * (base_max - base_min))
                adjusted = max(0, int(base_sats * modifier))
                return {
                    "base_sats": base_sats,
                    "adjusted_sats": adjusted,
                    "escrow_window_seconds": window,
                    "rep_modifier": modifier,
                    "rep_tier": rep_tier,
                    "danger_score": danger_score,
                }

        # Fallback for danger_score 10
        base_sats = 1000
        return {
            "base_sats": base_sats,
            "adjusted_sats": max(0, int(base_sats * modifier)),
            "escrow_window_seconds": 345600,
            "rep_modifier": modifier,
            "rep_tier": rep_tier,
            "danger_score": danger_score,
        }

    def create_ticket(self, agent_id: str, task_id: str,
                      danger_score: int, amount_sats: int,
                      mint_url: str, ticket_type: str = "single",
                      schema_id: Optional[str] = None,
                      action: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Create an escrow ticket with HTLC conditions.

        Args:
            agent_id: Agent receiving the escrow
            task_id: Associated task ID
            danger_score: Danger level (1-10)
            amount_sats: Escrow amount in sats
            mint_url: Cashu mint URL
            ticket_type: single/batch/milestone/performance
            schema_id: Optional management schema ID
            action: Optional management action

        Returns:
            Ticket dict or None on failure.
        """
        if not self.db:
            return None

        if ticket_type not in VALID_TICKET_TYPES:
            self._log(f"invalid ticket_type: {ticket_type}", level='warn')
            return None

        if amount_sats <= 0 or amount_sats > 10_000_000:
            self._log(f"invalid amount_sats: {amount_sats}", level='warn')
            return None

        if danger_score < 1 or danger_score > 10:
            self._log(f"invalid danger_score: {danger_score}", level='warn')
            return None

        if not mint_url:
            self._log("empty mint_url", level='warn')
            return None

        if mint_url not in self.acceptable_mints:
            self._log(f"mint not in acceptable list: {mint_url}", level='warn')
            return None

        # Check row caps
        count = self.db.count_escrow_tickets()
        if count >= self.MAX_ESCROW_TICKET_ROWS:
            self._log("escrow_tickets at cap, rejecting", level='warn')
            return None

        # Check active ticket limit
        active = self.db.list_escrow_tickets(
            status='active',
            limit=self.MAX_ACTIVE_TICKETS + 1,
        )
        if len(active) >= self.MAX_ACTIVE_TICKETS:
            self._log("active ticket limit reached", level='warn')
            return None

        # Generate HTLC secret
        ticket_id = hashlib.sha256(
            f"{agent_id}:{task_id}:{int(time.time())}:{os.urandom(8).hex()}".encode()
        ).hexdigest()[:32]

        htlc_hash = self.generate_secret(task_id, ticket_id)
        if not htlc_hash:
            self._log("failed to generate HTLC secret", level='warn')
            return None

        # Calculate escrow window from pricing
        pricing = self.get_pricing(danger_score)
        timelock = int(time.time()) + pricing['escrow_window_seconds']

        # Build NUT-10/11/14 condition structure (data model only)
        token_conditions = {
            "nut10": {"kind": "HTLC", "data": htlc_hash},
            "nut11": {"pubkey": agent_id},
            "nut14": {"timelock": timelock, "refund_pubkey": self.our_pubkey},
        }
        token_json = json.dumps({
            "mint": mint_url,
            "amount": amount_sats,
            "conditions": token_conditions,
            "ticket_type": ticket_type,
        }, sort_keys=True, separators=(',', ':'))

        # Store ticket
        success = self.db.store_escrow_ticket(
            ticket_id=ticket_id,
            ticket_type=ticket_type,
            agent_id=agent_id,
            operator_id=self.our_pubkey,
            mint_url=mint_url,
            amount_sats=amount_sats,
            token_json=token_json,
            htlc_hash=htlc_hash,
            timelock=timelock,
            danger_score=danger_score,
            schema_id=schema_id,
            action=action,
            status='active',
            created_at=int(time.time()),
        )

        if not success:
            return None

        self._log(f"created {ticket_type} ticket {ticket_id[:16]}... "
                  f"for agent {agent_id[:16]}... amount={amount_sats}sats")

        return {
            "ticket_id": ticket_id,
            "ticket_type": ticket_type,
            "agent_id": agent_id,
            "operator_id": self.our_pubkey,
            "mint_url": mint_url,
            "amount_sats": amount_sats,
            "htlc_hash": htlc_hash,
            "timelock": timelock,
            "danger_score": danger_score,
            "schema_id": schema_id,
            "action": action,
            "status": "active",
            "token_json": token_json,
        }

    def validate_ticket(self, token_json: str) -> Tuple[bool, str]:
        """
        Verify token structure and conditions (no mint call).

        Returns (is_valid, error_message).
        """
        try:
            token = json.loads(token_json)
        except (json.JSONDecodeError, TypeError):
            return False, "invalid JSON"

        if not isinstance(token, dict):
            return False, "token must be a dict"

        # Check required fields
        for field in ("mint", "amount", "conditions", "ticket_type"):
            if field not in token:
                return False, f"missing field: {field}"

        if not isinstance(token["amount"], int) or token["amount"] < 0:
            return False, "invalid amount"

        if token["ticket_type"] not in VALID_TICKET_TYPES:
            return False, f"invalid ticket_type: {token['ticket_type']}"

        conditions = token.get("conditions", {})
        if not isinstance(conditions, dict):
            return False, "conditions must be a dict"

        # Verify NUT-10 HTLC condition
        nut10 = conditions.get("nut10", {})
        if not isinstance(nut10, dict):
            return False, "nut10 must be a dict"
        if nut10.get("kind") != "HTLC":
            return False, "nut10.kind must be HTLC"
        if not isinstance(nut10.get("data"), str) or len(nut10["data"]) != 64:
            return False, "nut10.data must be 64-char hex hash"
        try:
            bytes.fromhex(nut10["data"])
        except ValueError:
            return False, "nut10.data must be valid hex"

        # Verify NUT-11 P2PK
        nut11 = conditions.get("nut11", {})
        if not isinstance(nut11, dict):
            return False, "nut11 must be a dict"
        if not isinstance(nut11.get("pubkey"), str) or len(nut11["pubkey"]) < 10:
            return False, "nut11.pubkey invalid"

        # Verify NUT-14 timelock
        nut14 = conditions.get("nut14", {})
        if not isinstance(nut14, dict):
            return False, "nut14 must be a dict"
        if not isinstance(nut14.get("timelock"), int) or nut14["timelock"] < 0:
            return False, "nut14.timelock invalid"

        return True, ""

    # =========================================================================
    # MINT INTERACTION (optional)
    # =========================================================================

    def check_ticket_with_mint(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        """
        Pre-flight check via POST /v1/checkstate.

        Returns mint response or None if unavailable.
        """
        ticket = self.db.get_escrow_ticket(ticket_id)
        if not ticket:
            return None

        mint_url = ticket.get('mint_url', '')
        if not mint_url:
            return None

        body = json.dumps({
            "Ys": [ticket.get('htlc_hash', '')]
        }).encode('utf-8')

        return self._mint_http_call(mint_url, '/v1/checkstate', method='POST', body=body)

    def redeem_ticket(self, ticket_id: str, preimage: str,
                      caller_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Agent-side redemption: swap tokens with preimage (mint call).

        Args:
            ticket_id: Ticket to redeem.
            preimage: HTLC preimage hex string.
            caller_id: If provided, must match ticket's agent_id.

        Returns result dict or None on failure.
        """
        # Validate preimage is valid hex before anything else
        try:
            preimage_bytes = bytes.fromhex(preimage)
        except ValueError:
            return {"error": "preimage is not valid hex"}

        with self._ticket_lock:
            ticket = self.db.get_escrow_ticket(ticket_id)
            if not ticket:
                return {"error": "ticket not found"}

            if ticket['status'] != 'active':
                return {"error": f"ticket status is {ticket['status']}, expected active"}

            # Authorization: caller must be the agent
            if caller_id is not None and caller_id != ticket['agent_id']:
                return {"error": "caller is not the ticket agent"}

            # Verify preimage matches hash
            preimage_hash = hashlib.sha256(preimage_bytes).hexdigest()
            if preimage_hash != ticket['htlc_hash']:
                return {"error": "preimage does not match HTLC hash"}

            # Update status under lock
            now = int(time.time())
            self.db.update_escrow_ticket_status(ticket_id, 'redeemed', now)

            # Re-read to confirm the transition took effect
            updated = self.db.get_escrow_ticket(ticket_id)
            if not updated or updated['status'] != 'redeemed':
                return {"error": "ticket status transition failed (race condition)"}

        # Attempt mint swap (optional) — outside the lock
        mint_result = None
        mint_url = ticket.get('mint_url', '')
        if mint_url:
            body = json.dumps({
                "inputs": [{"htlc_preimage": preimage}],
                "token": ticket.get('token_json', ''),
            }).encode('utf-8')
            mint_result = self._mint_http_call(mint_url, '/v1/swap', method='POST', body=body)

        self._log(f"ticket {ticket_id[:16]}... redeemed by {ticket['agent_id'][:16]}...")

        return {
            "ticket_id": ticket_id,
            "status": "redeemed",
            "preimage_valid": True,
            "mint_result": mint_result,
            "redeemed_at": now,
        }

    def refund_ticket(self, ticket_id: str,
                      caller_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Operator reclaim after timelock expiry (mint call).

        Args:
            ticket_id: Ticket to refund.
            caller_id: If provided, must match ticket's operator_id.

        Returns result dict or None on failure.
        """
        with self._ticket_lock:
            ticket = self.db.get_escrow_ticket(ticket_id)
            if not ticket:
                return {"error": "ticket not found"}

            if ticket['status'] not in ('active', 'expired'):
                return {"error": f"ticket status is {ticket['status']}, cannot refund"}

            # Authorization: caller must be the operator
            if caller_id is not None and caller_id != ticket['operator_id']:
                return {"error": "caller is not the ticket operator"}

            now = int(time.time())
            if now < ticket['timelock']:
                return {"error": "timelock not yet expired", "timelock": ticket['timelock']}

            # Update status under lock
            self.db.update_escrow_ticket_status(ticket_id, 'refunded', now)

            # Re-read to confirm the transition took effect
            updated = self.db.get_escrow_ticket(ticket_id)
            if not updated or updated['status'] != 'refunded':
                return {"error": "ticket status transition failed (race condition)"}

        # Attempt mint refund (optional) — outside the lock
        mint_result = None
        mint_url = ticket.get('mint_url', '')
        if mint_url:
            body = json.dumps({
                "inputs": [{"refund_pubkey": self.our_pubkey}],
                "token": ticket.get('token_json', ''),
            }).encode('utf-8')
            mint_result = self._mint_http_call(mint_url, '/v1/swap', method='POST', body=body)

        self._log(f"ticket {ticket_id[:16]}... refunded to operator")

        return {
            "ticket_id": ticket_id,
            "status": "refunded",
            "mint_result": mint_result,
            "refunded_at": now,
        }

    # =========================================================================
    # RECEIPTS
    # =========================================================================

    def create_receipt(self, ticket_id: str, schema_id: str, action: str,
                       params: Dict, result: Optional[Dict],
                       success: bool) -> Optional[Dict[str, Any]]:
        """
        Create a signed task execution receipt.

        Returns receipt dict or None on failure.
        """
        if not self.db:
            return None

        count = self.db.count_escrow_receipts()
        if count >= self.MAX_ESCROW_RECEIPT_ROWS:
            self._log("escrow_receipts at cap, rejecting", level='warn')
            return None

        receipt_id = hashlib.sha256(
            f"{ticket_id}:{schema_id}:{action}:{int(time.time())}:{os.urandom(8).hex()}".encode()
        ).hexdigest()[:32]

        params_json = json.dumps(params, sort_keys=True, separators=(',', ':'))
        result_json = json.dumps(result, sort_keys=True, separators=(',', ':')) if result else None

        # Sign the receipt
        signing_payload = json.dumps({
            "receipt_id": receipt_id,
            "ticket_id": ticket_id,
            "schema_id": schema_id,
            "action": action,
            "params_hash": hashlib.sha256(params_json.encode()).hexdigest(),
            "result_hash": hashlib.sha256(result_json.encode()).hexdigest() if result_json else "",
            "success": success,
        }, sort_keys=True, separators=(',', ':'))

        node_signature = ""
        if self.rpc:
            try:
                sig_result = self.rpc.signmessage(signing_payload)
                node_signature = sig_result.get("zbase", "") if isinstance(sig_result, dict) else ""
            except Exception as e:
                self._log(f"receipt signing failed: {e}", level='warn')

        # Check if preimage was revealed for this ticket
        ticket = self.db.get_escrow_ticket(ticket_id)
        preimage_revealed = 0
        if ticket:
            secret = self.db.get_escrow_secret_by_ticket(ticket_id)
            if secret and secret.get('revealed_at'):
                preimage_revealed = 1

        now = int(time.time())
        stored = self.db.store_escrow_receipt(
            receipt_id=receipt_id,
            ticket_id=ticket_id,
            schema_id=schema_id,
            action=action,
            params_json=params_json,
            result_json=result_json,
            success=1 if success else 0,
            preimage_revealed=preimage_revealed,
            node_signature=node_signature,
            created_at=now,
        )

        if not stored:
            return None

        return {
            "receipt_id": receipt_id,
            "ticket_id": ticket_id,
            "schema_id": schema_id,
            "action": action,
            "success": success,
            "preimage_revealed": bool(preimage_revealed),
            "node_signature": node_signature,
            "created_at": now,
        }

    # =========================================================================
    # MAINTENANCE
    # =========================================================================

    def cleanup_expired_tickets(self) -> int:
        """Mark expired active tickets. Returns count of newly expired."""
        if not self.db:
            return 0

        now = int(time.time())
        tickets = self.db.list_escrow_tickets(status='active', limit=self.MAX_ACTIVE_TICKETS)
        expired_count = 0
        for t in tickets:
            if t['timelock'] < now:
                self.db.update_escrow_ticket_status(t['ticket_id'], 'expired', now)
                expired_count += 1

        if expired_count > 0:
            self._log(f"expired {expired_count} tickets")
        return expired_count

    def retry_pending_operations(self) -> int:
        """Retry failed mint operations for pending tickets. Returns retry count."""
        if not self.db:
            return 0

        pending = self.db.list_escrow_tickets(status='pending')
        retried = 0
        for t in pending:
            mint_url = t.get('mint_url', '')
            if not mint_url:
                continue
            breaker = self._get_breaker(mint_url)
            if breaker.is_available():
                # Try check state
                result = self.check_ticket_with_mint(t['ticket_id'])
                if result is not None:
                    # Mint responded — promote pending ticket to active
                    self.db.update_escrow_ticket_status(
                        t['ticket_id'], 'active', int(time.time()))
                    retried += 1

        return retried

    def prune_old_secrets(self) -> int:
        """Delete revealed secrets older than SECRET_RETENTION_DAYS. Returns count."""
        if not self.db:
            return 0

        cutoff = int(time.time()) - (self.SECRET_RETENTION_DAYS * 86400)
        return self.db.prune_escrow_secrets(cutoff)

    def get_mint_status(self, mint_url: str) -> Dict[str, Any]:
        """Get circuit breaker state for a mint URL."""
        breaker = self._get_breaker(mint_url)
        return breaker.get_stats()

    def get_all_mint_statuses(self) -> List[Dict[str, Any]]:
        """Get circuit breaker stats for all known mints."""
        with self._breaker_lock:
            return [b.get_stats() for b in self._mint_breakers.values()]
