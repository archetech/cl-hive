"""
DID Credential Module (Phase 1 - DID Ecosystem)

Implements W3C-style Verifiable Credential issuance, verification, storage,
and reputation aggregation using CLN's HSM (signmessage/checkmessage).

Responsibilities:
- Credential issuance with HSM signatures
- Credential verification (signature, expiry, schema, self-issuance rejection)
- Credential revocation with reason tracking
- Weighted reputation aggregation with caching
- 4 credential profiles: hive:advisor, hive:node, hive:client, agent:general

Security:
- All credentials signed via CLN signmessage (zbase32)
- Self-issuance rejected (issuer == subject)
- Deterministic JSON signing payloads for reproducible signatures
- Row caps on storage to prevent unbounded growth
"""

import json
import math
import time
import uuid
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# --- Constants ---

MAX_CREDENTIALS_PER_PEER = 100
MAX_TOTAL_CREDENTIALS = 50_000
AGGREGATION_CACHE_TTL = 3600       # 1 hour
RECENCY_DECAY_LAMBDA = 0.01        # half-life ~69 days
TIMESTAMP_TOLERANCE = 300           # ±5 minutes for freshness checks
MAX_METRICS_JSON_LEN = 4096
MAX_EVIDENCE_JSON_LEN = 8192
MAX_REASON_LEN = 500

# Tier thresholds
TIER_NEWCOMER_MAX = 59
TIER_RECOGNIZED_MAX = 74
TIER_TRUSTED_MAX = 84
# 85+ = senior

VALID_DOMAINS = frozenset([
    "hive:advisor",
    "hive:node",
    "hive:client",
    "agent:general",
])

VALID_OUTCOMES = frozenset(["renew", "revoke", "neutral"])


# --- Dataclasses ---

@dataclass
class CredentialProfile:
    """Definition of a credential domain profile."""
    domain: str
    description: str
    subject_type: str       # "advisor", "node", "operator", "agent"
    issuer_type: str        # "operator", "peer_node", "advisor", "delegator"
    required_metrics: List[str]
    optional_metrics: List[str] = field(default_factory=list)
    metric_ranges: Dict[str, tuple] = field(default_factory=dict)


@dataclass
class DIDCredential:
    """A single DID reputation credential."""
    credential_id: str
    issuer_id: str
    subject_id: str
    domain: str
    period_start: int
    period_end: int
    metrics: Dict[str, Any]
    outcome: str = "neutral"
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    signature: str = ""
    issued_at: int = 0
    expires_at: Optional[int] = None
    revoked_at: Optional[int] = None
    revocation_reason: Optional[str] = None
    received_from: Optional[str] = None


@dataclass
class AggregatedReputation:
    """Cached aggregated reputation for a subject in a domain."""
    subject_id: str
    domain: str
    score: int = 50             # 0-100
    tier: str = "newcomer"      # newcomer/recognized/trusted/senior
    confidence: str = "low"     # low/medium/high
    credential_count: int = 0
    issuer_count: int = 0
    computed_at: int = 0
    components: Dict[str, Any] = field(default_factory=dict)


# --- Credential Profiles ---

CREDENTIAL_PROFILES: Dict[str, CredentialProfile] = {
    "hive:advisor": CredentialProfile(
        domain="hive:advisor",
        description="Fleet advisor performance credential",
        subject_type="advisor",
        issuer_type="operator",
        required_metrics=[
            "revenue_delta_pct",
            "actions_taken",
            "uptime_pct",
            "channels_managed",
        ],
        optional_metrics=["sla_violations", "response_time_ms"],
        metric_ranges={
            "revenue_delta_pct": (-100.0, 1000.0),
            "actions_taken": (0, 100000),
            "uptime_pct": (0.0, 100.0),
            "channels_managed": (0, 10000),
        },
    ),
    "hive:node": CredentialProfile(
        domain="hive:node",
        description="Lightning node routing credential",
        subject_type="node",
        issuer_type="peer_node",
        required_metrics=[
            "routing_reliability",
            "uptime",
            "htlc_success_rate",
            "avg_fee_ppm",
        ],
        optional_metrics=["capacity_sats", "forward_count", "force_close_count"],
        metric_ranges={
            "routing_reliability": (0.0, 1.0),
            "uptime": (0.0, 1.0),
            "htlc_success_rate": (0.0, 1.0),
            "avg_fee_ppm": (0, 50000),
        },
    ),
    "hive:client": CredentialProfile(
        domain="hive:client",
        description="Node operator client credential",
        subject_type="operator",
        issuer_type="advisor",
        required_metrics=[
            "payment_timeliness",
            "sla_reasonableness",
            "communication_quality",
        ],
        optional_metrics=["dispute_count", "contract_duration_days"],
        metric_ranges={
            "payment_timeliness": (0.0, 1.0),
            "sla_reasonableness": (0.0, 1.0),
            "communication_quality": (0.0, 1.0),
        },
    ),
    "agent:general": CredentialProfile(
        domain="agent:general",
        description="General AI agent performance credential",
        subject_type="agent",
        issuer_type="delegator",
        required_metrics=[
            "task_completion_rate",
            "accuracy",
            "response_time_ms",
            "tasks_evaluated",
        ],
        optional_metrics=["cost_efficiency", "error_rate"],
        metric_ranges={
            "task_completion_rate": (0.0, 1.0),
            "accuracy": (0.0, 1.0),
            "response_time_ms": (0, 600000),
            "tasks_evaluated": (0, 1000000),
        },
    ),
}


# --- Helper functions ---

def _score_to_tier(score: int) -> str:
    """Convert a 0-100 score to a reputation tier."""
    if score <= TIER_NEWCOMER_MAX:
        return "newcomer"
    elif score <= TIER_RECOGNIZED_MAX:
        return "recognized"
    elif score <= TIER_TRUSTED_MAX:
        return "trusted"
    else:
        return "senior"


def _compute_confidence(credential_count: int, issuer_count: int) -> str:
    """Compute confidence level from credential and issuer counts."""
    if issuer_count >= 5 and credential_count >= 10:
        return "high"
    elif issuer_count >= 2 and credential_count >= 3:
        return "medium"
    return "low"


def get_credential_signing_payload(credential: Dict[str, Any]) -> str:
    """
    Build deterministic JSON string for credential signing.

    Uses sorted keys and minimal separators for reproducibility.
    """
    signing_data = {
        "issuer_id": credential["issuer_id"],
        "subject_id": credential["subject_id"],
        "domain": credential["domain"],
        "period_start": credential["period_start"],
        "period_end": credential["period_end"],
        "metrics": credential["metrics"],
        "outcome": credential["outcome"],
    }
    return json.dumps(signing_data, sort_keys=True, separators=(',', ':'))


def validate_metrics_for_profile(domain: str, metrics: Dict[str, Any]) -> Optional[str]:
    """
    Validate metrics against the profile for a domain.

    Returns None if valid, or an error string if invalid.
    """
    profile = CREDENTIAL_PROFILES.get(domain)
    if not profile:
        return f"unknown domain: {domain}"

    # Check required metrics are present
    for req in profile.required_metrics:
        if req not in metrics:
            return f"missing required metric: {req}"

    # Check all metrics are known (required or optional)
    all_known = set(profile.required_metrics) | set(profile.optional_metrics)
    for key in metrics:
        if key not in all_known:
            return f"unknown metric: {key}"

    # Check metric value ranges
    for key, value in metrics.items():
        if key in profile.metric_ranges:
            lo, hi = profile.metric_ranges[key]
            if not isinstance(value, (int, float)):
                return f"metric {key} must be numeric, got {type(value).__name__}"
            if value < lo or value > hi:
                return f"metric {key} value {value} out of range [{lo}, {hi}]"

    return None


# --- Main Manager ---

class DIDCredentialManager:
    """
    DID credential issuance, verification, storage, and reputation aggregation.

    Uses CLN HSM (signmessage/checkmessage) for cryptographic signing.
    Follows the SettlementManager pattern for database and plugin integration.
    """

    def __init__(self, database, plugin, rpc=None, our_pubkey=""):
        """
        Initialize the DID credential manager.

        Args:
            database: HiveDatabase instance for persistence
            plugin: Reference to the pyln Plugin for logging
            rpc: ThreadSafeRpcProxy for Lightning RPC calls
            our_pubkey: Our node's public key
        """
        self.db = database
        self.plugin = plugin
        self.rpc = rpc
        self.our_pubkey = our_pubkey
        self._local = threading.local()
        self._aggregation_cache: Dict[str, AggregatedReputation] = {}
        self._cache_lock = threading.Lock()

    def _log(self, msg: str, level: str = "info"):
        """Log a message via the plugin."""
        try:
            self.plugin.log(f"cl-hive: did_credentials: {msg}", level=level)
        except Exception:
            pass

    # --- Credential Issuance ---

    def issue_credential(
        self,
        subject_id: str,
        domain: str,
        metrics: Dict[str, Any],
        outcome: str = "neutral",
        evidence: Optional[List[Dict[str, Any]]] = None,
        period_start: Optional[int] = None,
        period_end: Optional[int] = None,
        expires_at: Optional[int] = None,
    ) -> Optional[DIDCredential]:
        """
        Issue a new DID credential signed by our node's HSM.

        Args:
            subject_id: Pubkey of the credential subject
            domain: Credential domain (e.g. 'hive:node')
            metrics: Domain-specific metrics dict
            outcome: 'renew', 'revoke', or 'neutral'
            evidence: Optional list of evidence references
            period_start: Epoch start of evaluation period (default: 30 days ago)
            period_end: Epoch end of evaluation period (default: now)
            expires_at: Optional expiry epoch

        Returns:
            DIDCredential on success, None on failure
        """
        if not self.rpc:
            self._log("cannot issue credential: no RPC available", "warn")
            return None

        if not self.our_pubkey:
            self._log("cannot issue credential: no pubkey", "warn")
            return None

        # Self-issuance rejected
        if subject_id == self.our_pubkey:
            self._log("rejected self-issuance attempt", "warn")
            return None

        # Validate domain
        if domain not in VALID_DOMAINS:
            self._log(f"invalid domain: {domain}", "warn")
            return None

        # Validate outcome
        if outcome not in VALID_OUTCOMES:
            self._log(f"invalid outcome: {outcome}", "warn")
            return None

        # Validate metrics against profile
        err = validate_metrics_for_profile(domain, metrics)
        if err:
            self._log(f"metrics validation failed: {err}", "warn")
            return None

        # Check row cap
        count = self.db.count_did_credentials()
        if count >= MAX_TOTAL_CREDENTIALS:
            self._log(f"credential store at cap ({MAX_TOTAL_CREDENTIALS})", "warn")
            return None

        # Check per-peer cap
        peer_count = self.db.count_did_credentials_for_subject(subject_id)
        if peer_count >= MAX_CREDENTIALS_PER_PEER:
            self._log(f"credentials for {subject_id[:16]}... at cap ({MAX_CREDENTIALS_PER_PEER})", "warn")
            return None

        now = int(time.time())
        if period_start is None:
            period_start = now - 30 * 86400  # 30 days ago
        if period_end is None:
            period_end = now

        credential_id = str(uuid.uuid4())
        evidence = evidence or []

        # Build signing payload
        cred_dict = {
            "issuer_id": self.our_pubkey,
            "subject_id": subject_id,
            "domain": domain,
            "period_start": period_start,
            "period_end": period_end,
            "metrics": metrics,
            "outcome": outcome,
        }
        signing_payload = get_credential_signing_payload(cred_dict)

        # Sign with HSM
        try:
            result = self.rpc.signmessage(signing_payload)
            signature = result.get("zbase", "") if isinstance(result, dict) else str(result)
        except Exception as e:
            self._log(f"HSM signing failed: {e}", "error")
            return None

        if not signature:
            self._log("HSM returned empty signature", "error")
            return None

        credential = DIDCredential(
            credential_id=credential_id,
            issuer_id=self.our_pubkey,
            subject_id=subject_id,
            domain=domain,
            period_start=period_start,
            period_end=period_end,
            metrics=metrics,
            outcome=outcome,
            evidence=evidence,
            signature=signature,
            issued_at=now,
            expires_at=expires_at,
        )

        # Store
        stored = self.db.store_did_credential(
            credential_id=credential.credential_id,
            issuer_id=credential.issuer_id,
            subject_id=credential.subject_id,
            domain=credential.domain,
            period_start=credential.period_start,
            period_end=credential.period_end,
            metrics_json=json.dumps(credential.metrics, sort_keys=True),
            outcome=credential.outcome,
            evidence_json=json.dumps(credential.evidence) if credential.evidence else None,
            signature=credential.signature,
            issued_at=credential.issued_at,
            expires_at=credential.expires_at,
            received_from=None,
        )

        if not stored:
            self._log("failed to store credential", "error")
            return None

        self._log(f"issued credential {credential_id[:8]}... for {subject_id[:16]}... domain={domain}")

        # Invalidate aggregation cache for this subject
        self._invalidate_cache(subject_id, domain)

        return credential

    # --- Credential Verification ---

    def verify_credential(self, credential: Dict[str, Any]) -> tuple:
        """
        Verify a credential's signature, expiry, schema, and self-issuance.

        Args:
            credential: Dict with credential fields

        Returns:
            (is_valid: bool, reason: str)
        """
        # Required fields
        for field_name in ["issuer_id", "subject_id", "domain", "period_start",
                           "period_end", "metrics", "outcome", "signature"]:
            if field_name not in credential:
                return False, f"missing field: {field_name}"

        issuer_id = credential["issuer_id"]
        subject_id = credential["subject_id"]
        domain = credential["domain"]
        signature = credential["signature"]
        outcome = credential["outcome"]
        metrics = credential["metrics"]

        # Type checks
        if not isinstance(issuer_id, str) or len(issuer_id) < 10:
            return False, "invalid issuer_id"
        if not isinstance(subject_id, str) or len(subject_id) < 10:
            return False, "invalid subject_id"
        if not isinstance(signature, str) or not signature:
            return False, "invalid signature"
        if not isinstance(metrics, dict):
            return False, "metrics must be a dict"

        # Self-issuance rejection
        if issuer_id == subject_id:
            return False, "self-issuance rejected"

        # Domain validation
        if domain not in VALID_DOMAINS:
            return False, f"invalid domain: {domain}"

        # Outcome validation
        if outcome not in VALID_OUTCOMES:
            return False, f"invalid outcome: {outcome}"

        # Metrics validation
        err = validate_metrics_for_profile(domain, metrics)
        if err:
            return False, f"metrics invalid: {err}"

        # Period validation
        period_start = credential.get("period_start", 0)
        period_end = credential.get("period_end", 0)
        if not isinstance(period_start, int) or not isinstance(period_end, int):
            return False, "period_start/period_end must be integers"
        if period_end <= period_start:
            return False, "period_end must be after period_start"

        # Expiry check
        now = int(time.time())
        expires_at = credential.get("expires_at")
        if expires_at is not None and isinstance(expires_at, int) and expires_at < now:
            return False, "credential expired"

        # Revocation check
        revoked_at = credential.get("revoked_at")
        if revoked_at is not None:
            return False, "credential revoked"

        # Signature verification via CLN checkmessage
        if self.rpc:
            signing_payload = get_credential_signing_payload(credential)
            try:
                result = self.rpc.checkmessage(signing_payload, signature)
                if isinstance(result, dict):
                    verified = result.get("verified", False)
                    pubkey = result.get("pubkey", "")
                    if not verified:
                        return False, "signature verification failed"
                    if pubkey and pubkey != issuer_id:
                        return False, f"signature pubkey {pubkey[:16]}... != issuer {issuer_id[:16]}..."
                else:
                    return False, "unexpected checkmessage response"
            except Exception as e:
                return False, f"checkmessage error: {e}"
        else:
            # No RPC — can't verify signature, accept with warning
            self._log("no RPC available for signature verification", "warn")

        return True, "valid"

    # --- Credential Revocation ---

    def revoke_credential(self, credential_id: str, reason: str) -> bool:
        """
        Revoke a credential we issued.

        Args:
            credential_id: UUID of the credential
            reason: Revocation reason (max 500 chars)

        Returns:
            True if revoked successfully
        """
        if not reason or len(reason) > MAX_REASON_LEN:
            self._log(f"invalid revocation reason length", "warn")
            return False

        # Fetch the credential
        cred = self.db.get_did_credential(credential_id)
        if not cred:
            self._log(f"credential {credential_id[:8]}... not found", "warn")
            return False

        # Only the issuer can revoke
        if cred.get("issuer_id") != self.our_pubkey:
            self._log(f"cannot revoke: not the issuer", "warn")
            return False

        # Already revoked?
        if cred.get("revoked_at") is not None:
            self._log(f"credential {credential_id[:8]}... already revoked", "warn")
            return False

        now = int(time.time())
        success = self.db.revoke_did_credential(credential_id, reason, now)

        if success:
            self._log(f"revoked credential {credential_id[:8]}...: {reason}")
            # Invalidate cache
            subject_id = cred.get("subject_id", "")
            domain = cred.get("domain", "")
            if subject_id:
                self._invalidate_cache(subject_id, domain)

        return success

    # --- Reputation Aggregation ---

    def aggregate_reputation(
        self, subject_id: str, domain: Optional[str] = None
    ) -> Optional[AggregatedReputation]:
        """
        Compute weighted reputation score for a subject.

        Uses exponential recency decay, issuer weighting (proof-of-stake via
        open channels), and evidence strength multipliers.

        Args:
            subject_id: Pubkey of the subject
            domain: Optional domain filter (None = cross-domain '_all')

        Returns:
            AggregatedReputation or None if no credentials found
        """
        cache_key = f"{subject_id}:{domain or '_all'}"

        # Check cache
        with self._cache_lock:
            cached = self._aggregation_cache.get(cache_key)
            if cached and (int(time.time()) - cached.computed_at) < AGGREGATION_CACHE_TTL:
                return cached

        # Fetch credentials
        credentials = self.db.get_did_credentials_for_subject(
            subject_id, domain=domain, limit=MAX_CREDENTIALS_PER_PEER
        )

        if not credentials:
            return None

        # Filter out revoked
        active_creds = [c for c in credentials if c.get("revoked_at") is None]
        if not active_creds:
            return None

        now = int(time.time())
        total_weight = 0.0
        weighted_score_sum = 0.0
        issuers = set()
        components = {}

        for cred in active_creds:
            issuer_id = cred.get("issuer_id", "")
            cred_domain = cred.get("domain", "")
            issued_at = cred.get("issued_at", 0)
            metrics = cred.get("metrics_json", "{}")
            evidence = cred.get("evidence_json")

            # Parse JSON
            if isinstance(metrics, str):
                try:
                    metrics = json.loads(metrics)
                except (json.JSONDecodeError, TypeError):
                    continue
            if not isinstance(metrics, dict):
                continue

            # 1. Recency factor: e^(-λ × age_days)
            age_days = max(0, (now - issued_at) / 86400.0)
            recency = math.exp(-RECENCY_DECAY_LAMBDA * age_days)

            # 2. Issuer weight: 1.0 default, up to 3.0 for channel peers
            issuer_weight = self._get_issuer_weight(issuer_id, subject_id)

            # 3. Evidence strength
            evidence_strength = self._compute_evidence_strength(evidence)

            # Combined weight
            weight = issuer_weight * recency * evidence_strength
            if weight <= 0:
                continue

            # Compute metric score for this credential (0-100)
            metric_score = self._score_metrics(cred_domain, metrics)

            # Outcome modifier
            outcome = cred.get("outcome", "neutral")
            if outcome == "renew":
                metric_score = min(100, metric_score * 1.1)
            elif outcome == "revoke":
                metric_score = max(0, metric_score * 0.7)

            weighted_score_sum += weight * metric_score
            total_weight += weight
            issuers.add(issuer_id)

            # Track per-metric components
            for key, value in metrics.items():
                if key not in components:
                    components[key] = {"sum": 0.0, "weight": 0.0, "count": 0}
                components[key]["sum"] += weight * (value if isinstance(value, (int, float)) else 0)
                components[key]["weight"] += weight
                components[key]["count"] += 1

        if total_weight <= 0:
            return None

        score = int(round(weighted_score_sum / total_weight))
        score = max(0, min(100, score))
        tier = _score_to_tier(score)
        confidence = _compute_confidence(len(active_creds), len(issuers))

        # Compute component averages
        component_avgs = {}
        for key, comp in components.items():
            if comp["weight"] > 0:
                component_avgs[key] = round(comp["sum"] / comp["weight"], 4)

        result = AggregatedReputation(
            subject_id=subject_id,
            domain=domain or "_all",
            score=score,
            tier=tier,
            confidence=confidence,
            credential_count=len(active_creds),
            issuer_count=len(issuers),
            computed_at=int(time.time()),
            components=component_avgs,
        )

        # Update cache
        with self._cache_lock:
            self._aggregation_cache[cache_key] = result

        # Persist to DB cache
        self.db.store_did_reputation_cache(
            subject_id=subject_id,
            domain=result.domain,
            score=result.score,
            tier=result.tier,
            confidence=result.confidence,
            credential_count=result.credential_count,
            issuer_count=result.issuer_count,
            computed_at=result.computed_at,
            components_json=json.dumps(result.components),
        )

        return result

    def get_credit_tier(self, subject_id: str) -> str:
        """
        Get the reputation tier for a subject (cross-domain).

        Returns: 'newcomer', 'recognized', 'trusted', or 'senior'
        """
        # Try cache first
        with self._cache_lock:
            cached = self._aggregation_cache.get(f"{subject_id}:_all")
            if cached and (int(time.time()) - cached.computed_at) < AGGREGATION_CACHE_TTL:
                return cached.tier

        # Try DB cache
        db_cached = self.db.get_did_reputation_cache(subject_id, "_all")
        if db_cached and (int(time.time()) - db_cached.get("computed_at", 0)) < AGGREGATION_CACHE_TTL:
            return db_cached.get("tier", "newcomer")

        # Compute fresh
        result = self.aggregate_reputation(subject_id)
        if result:
            return result.tier
        return "newcomer"

    # --- Incoming Credential Handling ---

    def handle_credential_present(
        self, peer_id: str, payload: Dict[str, Any]
    ) -> bool:
        """
        Handle an incoming DID_CREDENTIAL_PRESENT message.

        Validates, verifies signature, stores, and invalidates cache.

        Args:
            peer_id: Peer who sent the message
            payload: Message payload with credential data

        Returns:
            True if credential was accepted and stored
        """
        credential = payload.get("credential")
        if not isinstance(credential, dict):
            self._log("invalid credential_present: missing credential dict", "warn")
            return False

        # Size checks
        metrics_json = json.dumps(credential.get("metrics", {}))
        if len(metrics_json) > MAX_METRICS_JSON_LEN:
            self._log("credential metrics too large", "warn")
            return False

        evidence_json = json.dumps(credential.get("evidence", []))
        if len(evidence_json) > MAX_EVIDENCE_JSON_LEN:
            self._log("credential evidence too large", "warn")
            return False

        # Verify
        is_valid, reason = self.verify_credential(credential)
        if not is_valid:
            self._log(f"rejected credential from {peer_id[:16]}...: {reason}", "warn")
            return False

        # Check row cap
        count = self.db.count_did_credentials()
        if count >= MAX_TOTAL_CREDENTIALS:
            self._log(f"credential store at cap, rejecting", "warn")
            return False

        # Check per-subject cap
        subject_id = credential["subject_id"]
        peer_count = self.db.count_did_credentials_for_subject(subject_id)
        if peer_count >= MAX_CREDENTIALS_PER_PEER:
            self._log(f"credentials for {subject_id[:16]}... at cap", "warn")
            return False

        # Check for duplicate credential_id
        credential_id = credential.get("credential_id", str(uuid.uuid4()))
        existing = self.db.get_did_credential(credential_id)
        if existing:
            return True  # Idempotent — already have it

        # Store
        stored = self.db.store_did_credential(
            credential_id=credential_id,
            issuer_id=credential["issuer_id"],
            subject_id=credential["subject_id"],
            domain=credential["domain"],
            period_start=credential["period_start"],
            period_end=credential["period_end"],
            metrics_json=metrics_json,
            outcome=credential.get("outcome", "neutral"),
            evidence_json=evidence_json if credential.get("evidence") else None,
            signature=credential["signature"],
            issued_at=credential.get("issued_at", int(time.time())),
            expires_at=credential.get("expires_at"),
            received_from=peer_id,
        )

        if stored:
            self._log(f"stored credential {credential_id[:8]}... from {peer_id[:16]}...")
            self._invalidate_cache(subject_id, credential["domain"])

        return stored

    def handle_credential_revoke(
        self, peer_id: str, payload: Dict[str, Any]
    ) -> bool:
        """
        Handle an incoming DID_CREDENTIAL_REVOKE message.

        Args:
            peer_id: Peer who sent the message
            payload: Message payload with credential_id and reason

        Returns:
            True if revocation was processed
        """
        credential_id = payload.get("credential_id")
        reason = payload.get("reason", "")
        issuer_id = payload.get("issuer_id", "")
        signature = payload.get("signature", "")

        if not credential_id or not isinstance(credential_id, str):
            self._log("invalid credential_revoke: missing credential_id", "warn")
            return False

        if not reason or len(reason) > MAX_REASON_LEN:
            self._log("invalid credential_revoke: bad reason", "warn")
            return False

        # Fetch credential
        cred = self.db.get_did_credential(credential_id)
        if not cred:
            self._log(f"revoke: credential {credential_id[:8]}... not found", "debug")
            return False

        # Verify issuer matches
        if cred.get("issuer_id") != issuer_id:
            self._log(f"revoke: issuer mismatch for {credential_id[:8]}...", "warn")
            return False

        # Already revoked?
        if cred.get("revoked_at") is not None:
            return True  # Idempotent

        # Verify revocation signature
        if self.rpc and signature:
            revoke_payload = json.dumps({
                "credential_id": credential_id,
                "action": "revoke",
                "reason": reason,
            }, sort_keys=True, separators=(',', ':'))
            try:
                result = self.rpc.checkmessage(revoke_payload, signature)
                if isinstance(result, dict):
                    if not result.get("verified", False):
                        self._log(f"revoke: signature verification failed", "warn")
                        return False
                    if result.get("pubkey", "") != issuer_id:
                        self._log(f"revoke: signature pubkey mismatch", "warn")
                        return False
            except Exception as e:
                self._log(f"revoke: checkmessage error: {e}", "warn")
                return False

        now = int(time.time())
        success = self.db.revoke_did_credential(credential_id, reason, now)

        if success:
            subject_id = cred.get("subject_id", "")
            domain = cred.get("domain", "")
            self._log(f"processed revocation for {credential_id[:8]}...")
            if subject_id:
                self._invalidate_cache(subject_id, domain)

        return success

    # --- Maintenance ---

    def cleanup_expired(self) -> int:
        """Remove expired credentials. Returns count removed."""
        now = int(time.time())
        count = self.db.cleanup_expired_did_credentials(now)
        if count > 0:
            self._log(f"cleaned up {count} expired credentials")
        return count

    def refresh_stale_aggregations(self) -> int:
        """Refresh aggregation cache entries older than TTL. Returns count refreshed."""
        now = int(time.time())
        stale_cutoff = now - AGGREGATION_CACHE_TTL

        # Get all cached entries from DB
        stale_entries = self.db.get_stale_did_reputation_cache(stale_cutoff, limit=50)
        refreshed = 0

        for entry in stale_entries:
            subject_id = entry.get("subject_id", "")
            domain = entry.get("domain", "_all")
            if subject_id:
                domain_filter = domain if domain != "_all" else None
                result = self.aggregate_reputation(subject_id, domain=domain_filter)
                if result:
                    refreshed += 1

        if refreshed > 0:
            self._log(f"refreshed {refreshed} stale reputation entries")
        return refreshed

    def get_credentials_for_relay(self, subject_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get credentials suitable for relay to other peers.

        Returns credentials we issued (not received) that are active.
        """
        credentials = self.db.get_did_credentials_by_issuer(
            self.our_pubkey, subject_id=subject_id, limit=100
        )
        result = []
        now = int(time.time())
        for cred in credentials:
            if cred.get("revoked_at") is not None:
                continue
            expires = cred.get("expires_at")
            if expires is not None and expires < now:
                continue
            result.append(cred)
        return result

    # --- Internal Helpers ---

    def _get_issuer_weight(self, issuer_id: str, subject_id: str) -> float:
        """
        Compute issuer weight. Issuers with open channels to subject
        get up to 3.0 weight (proof-of-stake). Default 1.0.
        """
        # Check if issuer has a channel to subject via the database
        try:
            members = self.db.get_all_members()
            issuer_is_member = any(m.get("peer_id") == issuer_id for m in members)
            subject_is_member = any(m.get("peer_id") == subject_id for m in members)

            if issuer_is_member and subject_is_member:
                return 2.0  # Both are hive members — strong signal

            if issuer_is_member:
                return 1.5  # Issuer is a member — moderate signal

        except Exception:
            pass

        return 1.0

    def _compute_evidence_strength(self, evidence_json) -> float:
        """
        Compute evidence strength multiplier.

        ×0.3 = no evidence
        ×0.7 = 1-5 evidence refs
        ×1.0 = 5+ evidence refs
        """
        if not evidence_json:
            return 0.3

        if isinstance(evidence_json, str):
            try:
                evidence = json.loads(evidence_json)
            except (json.JSONDecodeError, TypeError):
                return 0.3
        elif isinstance(evidence_json, list):
            evidence = evidence_json
        else:
            return 0.3

        if not isinstance(evidence, list) or len(evidence) == 0:
            return 0.3
        elif len(evidence) < 5:
            return 0.7
        else:
            return 1.0

    def _score_metrics(self, domain: str, metrics: Dict[str, Any]) -> float:
        """
        Compute a 0-100 score from domain-specific metrics.

        Each metric is normalized to 0-1 range using the profile's ranges,
        then averaged (equal weight).
        """
        profile = CREDENTIAL_PROFILES.get(domain)
        if not profile:
            return 50.0  # Unknown domain — neutral

        scores = []
        for key in profile.required_metrics:
            value = metrics.get(key)
            if value is None or not isinstance(value, (int, float)):
                continue

            if key in profile.metric_ranges:
                lo, hi = profile.metric_ranges[key]
                if hi > lo:
                    normalized = (value - lo) / (hi - lo)
                    normalized = max(0.0, min(1.0, normalized))
                    scores.append(normalized)

        if not scores:
            return 50.0

        return (sum(scores) / len(scores)) * 100.0

    def _invalidate_cache(self, subject_id: str, domain: str):
        """Invalidate aggregation cache entries for a subject."""
        with self._cache_lock:
            keys_to_remove = [
                k for k in self._aggregation_cache
                if k.startswith(f"{subject_id}:")
            ]
            for k in keys_to_remove:
                del self._aggregation_cache[k]
