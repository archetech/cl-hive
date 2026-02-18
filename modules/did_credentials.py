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

import hashlib
import heapq
import json
import math
import threading
import time
import uuid
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
MAX_AGGREGATION_CACHE_ENTRIES = 10_000
MAX_CREDENTIAL_PRESENTS_PER_PEER_PER_HOUR = 20
MAX_CREDENTIAL_REVOKES_PER_PEER_PER_HOUR = 10

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

def _is_valid_pubkey(value: str) -> bool:
    """Validate a Lightning node pubkey (66-char hex starting with 02 or 03)."""
    if len(value) != 66:
        return False
    if not value.startswith(("02", "03")):
        return False
    try:
        int(value, 16)
        return True
    except ValueError:
        return False


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
    Aligned with get_did_credential_present_signing_payload() in protocol.py
    to prevent signing payload divergence (R4-2).
    """
    signing_data = {
        "credential_id": credential.get("credential_id", ""),
        "issuer_id": credential.get("issuer_id", ""),
        "subject_id": credential.get("subject_id", ""),
        "domain": credential.get("domain", ""),
        "period_start": credential.get("period_start", 0),
        "period_end": credential.get("period_end", 0),
        "metrics": credential.get("metrics", {}),
        "outcome": credential.get("outcome"),
        "issued_at": credential.get("issued_at"),
        "expires_at": credential.get("expires_at"),
        "evidence_hash": hashlib.sha256(
            json.dumps(credential.get("evidence", []), sort_keys=True, separators=(',', ':')).encode()
        ).hexdigest(),
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

    # Type check ALL metrics (not just those with ranges)
    for key, value in metrics.items():
        if isinstance(value, bool):
            return f"metric {key} must be numeric, got bool"
        if not isinstance(value, (int, float)):
            return f"metric {key} must be numeric, got {type(value).__name__}"
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return f"metric {key} must be finite"

    # Check metric value ranges
    for key, value in metrics.items():
        if key in profile.metric_ranges:
            lo, hi = profile.metric_ranges[key]
            if value < lo or value > hi:
                return f"metric {key} value {value} out of range [{lo}, {hi}]"

    # R4-3: Default upper-bound range checks for optional metrics without explicit ranges
    DEFAULT_OPTIONAL_BOUNDS: Dict[str, tuple] = {
        # hive:advisor optional
        "sla_violations": (0, 100000),
        "response_time_ms": (0, 600000),
        # hive:node optional
        "capacity_sats": (0, 21_000_000_00000000),  # 21M BTC in sats
        "forward_count": (0, 100_000_000),
        "force_close_count": (0, 100000),
        # hive:client optional
        "dispute_count": (0, 100000),
        "contract_duration_days": (0, 36500),  # ~100 years
        # agent:general optional
        "cost_efficiency": (0.0, 1000.0),
        "error_rate": (0.0, 1.0),
    }
    for key, value in metrics.items():
        if key not in profile.metric_ranges and key in DEFAULT_OPTIONAL_BOUNDS:
            lo, hi = DEFAULT_OPTIONAL_BOUNDS[key]
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
        self._aggregation_cache: Dict[str, AggregatedReputation] = {}
        self._cache_lock = threading.Lock()
        self._rate_limiters: Dict[tuple, List[int]] = {}
        self._rate_lock = threading.Lock()

    def _log(self, msg: str, level: str = "info"):
        """Log a message via the plugin."""
        try:
            self.plugin.log(f"cl-hive: did_credentials: {msg}", level=level)
        except Exception:
            pass

    def _check_rate_limit(self, peer_id: str, message_type: str, max_per_hour: int) -> bool:
        """Per-peer sliding-window rate limit."""
        now = int(time.time())
        cutoff = now - 3600
        key = (peer_id, message_type)

        with self._rate_lock:
            timestamps = self._rate_limiters.get(key, [])
            timestamps = [ts for ts in timestamps if ts > cutoff]

            if len(timestamps) >= max_per_hour:
                self._rate_limiters[key] = timestamps
                return False

            timestamps.append(now)
            self._rate_limiters[key] = timestamps

            if len(self._rate_limiters) > 1000:
                stale_keys = [
                    k for k, vals in self._rate_limiters.items()
                    if not vals or vals[-1] <= cutoff
                ]
                for k in stale_keys:
                    self._rate_limiters.pop(k, None)

        return True

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

        # Validate subject_id pubkey format
        if not _is_valid_pubkey(subject_id):
            self._log(f"invalid subject_id pubkey format", "warn")
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

        if period_end <= period_start:
            self._log("period_end must be after period_start", "warn")
            return None

        credential_id = str(uuid.uuid4())
        evidence = evidence or []

        # Build signing payload
        cred_dict = {
            "credential_id": credential_id,
            "issuer_id": self.our_pubkey,
            "subject_id": subject_id,
            "domain": domain,
            "period_start": period_start,
            "period_end": period_end,
            "metrics": metrics,
            "outcome": outcome,
            "issued_at": now,
            "expires_at": expires_at,
            "evidence": evidence,
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
            evidence_json=json.dumps(credential.evidence, sort_keys=True, separators=(',', ':')) if credential.evidence else None,
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

        # Type checks — pubkeys must be 66-char hex starting with 02 or 03
        if not isinstance(issuer_id, str) or not _is_valid_pubkey(issuer_id):
            return False, "invalid issuer_id"
        if not isinstance(subject_id, str) or not _is_valid_pubkey(subject_id):
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
        if expires_at is not None:
            if not isinstance(expires_at, int):
                self._log("credential has non-int expires_at", "warn")
                return False, "invalid expires_at type"
            if expires_at < now:
                return False, "credential expired"

        # Revocation check
        revoked_at = credential.get("revoked_at")
        if revoked_at is not None:
            return False, "credential revoked"

        # Signature verification via CLN checkmessage (fail-closed)
        if not self.rpc:
            return False, "no RPC available for signature verification"

        signing_payload = get_credential_signing_payload(credential)
        try:
            result = self.rpc.call("checkmessage", {
                "message": signing_payload,
                "zbase": signature,
                "pubkey": issuer_id,
            })
            if isinstance(result, dict):
                verified = result.get("verified", False)
                pubkey = result.get("pubkey", "")
                if not verified:
                    return False, "signature verification failed"
                if not pubkey or pubkey != issuer_id:
                    return False, f"signature pubkey {pubkey[:16]}... != issuer {issuer_id[:16]}..."
            else:
                return False, "unexpected checkmessage response"
        except Exception as e:
            return False, f"checkmessage error: {e}"

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

        # Fetch members once for issuer weight lookups
        try:
            members = self.db.get_all_members()
        except Exception:
            members = []

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
            issuer_weight = self._get_issuer_weight(issuer_id, subject_id, members=members)

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

        # Update cache (bounded)
        with self._cache_lock:
            if len(self._aggregation_cache) >= MAX_AGGREGATION_CACHE_ENTRIES:
                # Evict oldest 50% using heapq for efficiency
                keys_to_evict = heapq.nsmallest(
                    len(self._aggregation_cache) // 2,
                    self._aggregation_cache.keys(),
                    key=lambda k: self._aggregation_cache[k].computed_at,
                )
                for k in keys_to_evict:
                    del self._aggregation_cache[k]
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

        if not self._check_rate_limit(
            peer_id,
            "did_credential_present",
            MAX_CREDENTIAL_PRESENTS_PER_PEER_PER_HOUR,
        ):
            self._log(f"rate limit exceeded for credential presents from {peer_id[:16]}...", "warn")
            return False

        # Size checks
        metrics_json = json.dumps(credential.get("metrics", {}), sort_keys=True, separators=(',', ':'))
        if len(metrics_json) > MAX_METRICS_JSON_LEN:
            self._log("credential metrics too large", "warn")
            return False

        evidence_json = json.dumps(credential.get("evidence", []), sort_keys=True, separators=(',', ':'))
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

        # Require credential_id (reject if missing to preserve dedup)
        credential_id = credential.get("credential_id")
        if not credential_id or not isinstance(credential_id, str):
            self._log("credential_present: missing credential_id", "warn")
            return False
        if len(credential_id) > 64:
            self._log("credential_present: credential_id too long", "warn")
            return False

        # Validate issued_at is within reasonable range — reject if missing or non-int
        issued_at = credential.get("issued_at")
        if issued_at is None or not isinstance(issued_at, int):
            self._log(f"rejecting credential without valid issued_at from {peer_id[:16]}...", "info")
            return False
        now = int(time.time())
        # Lower bound: reject credentials older than 5 years (or before ~Nov 2023)
        min_issued_at = max(1700000000, now - 365 * 86400 * 5)
        if issued_at < min_issued_at:
            self._log(f"credential_present: issued_at {issued_at} too old (min {min_issued_at})", "warn")
            return False
        period_start = credential.get("period_start", 0)
        if issued_at < period_start:
            self._log("credential_present: issued_at before period_start", "warn")
            return False
        if issued_at > now + TIMESTAMP_TOLERANCE:
            self._log("credential_present: issued_at too far in future", "warn")
            return False

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

        if not self._check_rate_limit(
            peer_id,
            "did_credential_revoke",
            MAX_CREDENTIAL_REVOKES_PER_PEER_PER_HOUR,
        ):
            self._log(f"rate limit exceeded for credential revokes from {peer_id[:16]}...", "warn")
            return False

        if not credential_id or not isinstance(credential_id, str):
            self._log("invalid credential_revoke: missing credential_id", "warn")
            return False

        if not isinstance(issuer_id, str) or not _is_valid_pubkey(issuer_id):
            self._log("invalid credential_revoke: invalid issuer_id pubkey", "warn")
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

        # Verify revocation signature (fail-closed)
        if not signature:
            self._log("revoke: missing signature", "warn")
            return False
        if not self.rpc:
            self._log("revoke: no RPC for signature verification", "warn")
            return False

        revoke_payload = json.dumps({
            "credential_id": credential_id,
            "action": "revoke",
            "reason": reason,
        }, sort_keys=True, separators=(',', ':'))
        try:
            result = self.rpc.call("checkmessage", {
                "message": revoke_payload,
                "zbase": signature,
                "pubkey": issuer_id,
            })
            if not isinstance(result, dict):
                self._log("revoke: unexpected checkmessage response type", "warn")
                return False
            if not result.get("verified", False):
                self._log(f"revoke: signature verification failed", "warn")
                return False
            if not result.get("pubkey", "") or result.get("pubkey", "") != issuer_id:
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

    # --- Auto-Issuance and Rebroadcast (Phase 3) ---

    # Minimum interval between auto-issuing credentials for the same peer
    AUTO_ISSUE_INTERVAL = 7 * 86400  # 7 days
    # Minimum interval between rebroadcasts
    REBROADCAST_INTERVAL = 4 * 3600  # 4 hours

    def auto_issue_node_credentials(
        self,
        state_manager,
        contribution_tracker=None,
        broadcast_fn=None,
    ) -> int:
        """
        Auto-issue hive:node credentials for peers we have forwarding data on.

        Uses peer state (uptime, forwarding stats) and contribution data to
        populate the credential metrics. Only issues if no recent credential
        exists for the peer.

        Args:
            state_manager: StateManager instance for peer state data
            contribution_tracker: ContributionTracker for forwarding stats
            broadcast_fn: Callable(bytes) -> int to broadcast to fleet

        Returns:
            Number of credentials issued
        """
        if not state_manager or not self.rpc:
            return 0

        issued = 0
        now = int(time.time())
        period_start = now - 30 * 86400  # 30-day evaluation window

        try:
            all_peers = state_manager.get_all_peer_states()
        except Exception as e:
            self._log(f"auto_issue: cannot get peer states: {e}", "warn")
            return 0

        if isinstance(all_peers, dict):
            peer_states = all_peers.values()
        elif isinstance(all_peers, (list, tuple, set)):
            peer_states = all_peers
        else:
            self._log("auto_issue: unexpected peer state container", "debug")
            return 0

        for peer_state in peer_states:
            peer_id = getattr(peer_state, 'peer_id', '')
            if peer_id == self.our_pubkey:
                continue

            # Check if we already have a recent credential for this peer
            existing = self.db.get_did_credentials_by_issuer(
                self.our_pubkey, subject_id=peer_id, limit=1
            )
            if existing:
                latest = existing[0]
                if latest.get("revoked_at") is None:
                    issued_at = latest.get("issued_at", 0)
                    if now - issued_at < self.AUTO_ISSUE_INTERVAL:
                        continue  # Too recent, skip

            # Compute metrics from available data
            try:
                metrics = self._compute_node_metrics(
                    peer_id, peer_state, contribution_tracker, now
                )
            except Exception as e:
                self._log(f"auto_issue: metrics error for {peer_id[:16]}...: {e}", "debug")
                continue

            if not metrics:
                continue

            # Determine outcome based on overall performance
            avg_score = sum(metrics.get(k, 0) for k in [
                "routing_reliability", "uptime", "htlc_success_rate"
            ]) / 3.0
            if avg_score >= 0.7:
                outcome = "renew"
            elif avg_score < 0.3:
                outcome = "revoke"
            else:
                outcome = "neutral"

            # Issue the credential
            cred = self.issue_credential(
                subject_id=peer_id,
                domain="hive:node",
                metrics=metrics,
                outcome=outcome,
                period_start=period_start,
                period_end=now,
                expires_at=now + 90 * 86400,  # 90-day expiry
            )

            if cred:
                issued += 1

                # Broadcast to fleet if we have a broadcast function
                if broadcast_fn:
                    try:
                        from modules.protocol import create_did_credential_present
                        cred_dict = cred.to_dict() if hasattr(cred, 'to_dict') else {
                            "credential_id": cred.credential_id,
                            "issuer_id": cred.issuer_id,
                            "subject_id": cred.subject_id,
                            "domain": cred.domain,
                            "period_start": cred.period_start,
                            "period_end": cred.period_end,
                            "metrics": cred.metrics,
                            "outcome": cred.outcome,
                            "evidence": cred.evidence or [],
                            "signature": cred.signature,
                            "issued_at": cred.issued_at,
                            "expires_at": cred.expires_at,
                        }
                        msg = create_did_credential_present(
                            sender_id=self.our_pubkey,
                            credential=cred_dict,
                        )
                        broadcast_fn(msg)
                    except Exception as e:
                        self._log(f"auto_issue: broadcast error: {e}", "warn")

        if issued > 0:
            self._log(f"auto-issued {issued} hive:node credentials")
        return issued

    def _compute_node_metrics(
        self,
        peer_id: str,
        peer_state,
        contribution_tracker,
        now: int,
    ) -> Optional[Dict[str, Any]]:
        """Compute hive:node metrics from available peer data."""
        metrics = {}

        # Uptime: based on last_update freshness
        last_update = getattr(peer_state, 'last_update', 0)
        if last_update <= 0:
            return None  # No state data

        # Estimate uptime as fraction of time peer has been active
        # (updated within stale threshold of 1 hour)
        staleness = now - last_update
        if staleness < 3600:
            uptime = 0.99
        elif staleness < 7200:
            uptime = 0.9
        elif staleness < 86400:
            uptime = 0.7
        else:
            uptime = 0.3
        metrics["uptime"] = round(uptime, 3)

        # Routing reliability from contribution stats
        if contribution_tracker:
            try:
                stats = contribution_tracker.get_contribution_stats(peer_id, window_days=30)
                forwarded = stats.get("forwarded", 0)
                received = stats.get("received", 0)
                total = forwarded + received
                if total > 0:
                    metrics["routing_reliability"] = round(min(forwarded / max(total, 1), 1.0), 3)
                else:
                    metrics["routing_reliability"] = 0.5  # No data
            except Exception:
                metrics["routing_reliability"] = 0.5
        else:
            metrics["routing_reliability"] = 0.5  # Default

        # HTLC success rate: derived from forward count vs capacity utilization
        forward_count = getattr(peer_state, 'fees_forward_count', 0)
        if forward_count > 100:
            metrics["htlc_success_rate"] = 0.95
        elif forward_count > 10:
            metrics["htlc_success_rate"] = 0.85
        elif forward_count > 0:
            metrics["htlc_success_rate"] = 0.7
        else:
            metrics["htlc_success_rate"] = 0.5

        # Average fee PPM from fee policy (clamped to valid range)
        fee_policy = getattr(peer_state, 'fee_policy', {})
        if isinstance(fee_policy, dict):
            avg_fee_ppm = fee_policy.get("fee_ppm", 0)
        else:
            avg_fee_ppm = 0
        metrics["avg_fee_ppm"] = max(0, min(avg_fee_ppm, 50000))

        # Optional metrics
        metrics["capacity_sats"] = getattr(peer_state, 'capacity_sats', 0) or 0
        metrics["forward_count"] = forward_count or 0

        return metrics

    def rebroadcast_own_credentials(self, broadcast_fn=None) -> int:
        """
        Rebroadcast our issued credentials to fleet members.

        Used periodically (every 4 hours) to ensure new members receive
        existing credentials.

        Args:
            broadcast_fn: Callable(bytes) -> int to broadcast to fleet

        Returns:
            Number of credentials rebroadcast
        """
        if not broadcast_fn or not self.our_pubkey:
            return 0

        credentials = self.get_credentials_for_relay()
        if not credentials:
            return 0

        from modules.protocol import create_did_credential_present

        count = 0
        for cred in credentials:
            try:
                # Convert DB row to credential dict for protocol message
                metrics = cred.get("metrics_json", "{}")
                if isinstance(metrics, str):
                    metrics = json.loads(metrics)

                evidence = cred.get("evidence_json")
                if isinstance(evidence, str):
                    try:
                        evidence = json.loads(evidence)
                    except (json.JSONDecodeError, TypeError):
                        evidence = []
                elif evidence is None:
                    evidence = []

                cred_dict = {
                    "credential_id": cred["credential_id"],
                    "issuer_id": cred["issuer_id"],
                    "subject_id": cred["subject_id"],
                    "domain": cred["domain"],
                    "period_start": cred["period_start"],
                    "period_end": cred["period_end"],
                    "metrics": metrics,
                    "outcome": cred.get("outcome", "neutral"),
                    "evidence": evidence,
                    "signature": cred["signature"],
                    "issued_at": cred.get("issued_at", 0),
                    "expires_at": cred.get("expires_at"),
                }
                msg = create_did_credential_present(
                    sender_id=self.our_pubkey,
                    credential=cred_dict,
                )
                broadcast_fn(msg)
                count += 1
            except Exception as e:
                self._log(f"rebroadcast error for {cred.get('credential_id', '?')[:8]}...: {e}", "warn")

        if count > 0:
            self._log(f"rebroadcast {count} credentials to fleet")
        return count

    # --- Internal Helpers ---

    def _get_issuer_weight(self, issuer_id: str, subject_id: str, members: Optional[list] = None) -> float:
        """
        Compute issuer weight. Issuers with open channels to subject
        get up to 3.0 weight (proof-of-stake). Default 1.0.
        """
        # Check if issuer has a channel to subject via the database
        try:
            if members is None:
                try:
                    members = self.db.get_all_members()
                except Exception:
                    members = []
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

    # Metrics where lower values indicate better performance
    LOWER_IS_BETTER = frozenset({"avg_fee_ppm", "response_time_ms"})

    def _score_metrics(self, domain: str, metrics: Dict[str, Any]) -> float:
        """
        Compute a 0-100 score from domain-specific metrics.

        Each metric is normalized to 0-1 range using the profile's ranges,
        then averaged (equal weight). Metrics in LOWER_IS_BETTER are inverted
        so that lower values produce higher scores.
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
                    # Invert for metrics where lower is better
                    if key in self.LOWER_IS_BETTER:
                        normalized = 1.0 - normalized
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
