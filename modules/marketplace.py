"""Phase 5B advisor marketplace manager."""

import json
import time
import uuid
from typing import Any, Dict, List, Optional


class MarketplaceManager:
    """Advisor marketplace: profiles, discovery, contracts, and trials."""

    MAX_CACHED_PROFILES = 500
    PROFILE_STALE_DAYS = 90
    MAX_ACTIVE_TRIALS = 2
    TRIAL_COOLDOWN_DAYS = 14

    def __init__(self, database, plugin, nostr_transport, did_credential_mgr,
                 management_schema_registry, cashu_escrow_mgr):
        self.db = database
        self.plugin = plugin
        self.nostr_transport = nostr_transport
        self.did_credential_mgr = did_credential_mgr
        self.management_schema_registry = management_schema_registry
        self.cashu_escrow_mgr = cashu_escrow_mgr

        self._last_profile_publish_at = 0
        self._our_profile: Optional[Dict[str, Any]] = None

    def _log(self, msg: str, level: str = "info") -> None:
        self.plugin.log(f"cl-hive: marketplace: {msg}", level=level)

    def discover_advisors(self, criteria: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Discover advisors using cached marketplace profiles."""
        criteria = criteria or {}
        conn = self.db._get_connection()
        rows = conn.execute(
            "SELECT * FROM marketplace_profiles ORDER BY reputation_score DESC, last_seen DESC LIMIT ?",
            (self.MAX_CACHED_PROFILES,)
        ).fetchall()
        profiles = []
        min_reputation = int(criteria.get("min_reputation", 0))
        specialization = str(criteria.get("specialization", "")).strip()
        for row in rows:
            profile = dict(row)
            if int(profile.get("reputation_score", 0)) < min_reputation:
                continue
            payload = json.loads(profile.get("profile_json", "{}") or "{}")
            if specialization:
                specs = payload.get("specializations", []) if isinstance(payload, dict) else []
                if specialization not in specs:
                    continue
            profile["profile"] = payload
            profiles.append(profile)
        return profiles

    def publish_profile(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        """Publish our advisor profile and store it in cache."""
        now = int(time.time())
        advisor_did = str(profile.get("advisor_did") or profile.get("did") or "")
        if not advisor_did:
            return {"error": "advisor_did is required"}

        if self.db.count_rows("marketplace_profiles") >= self.db.MAX_MARKETPLACE_PROFILE_ROWS:
            return {"error": "marketplace profile row cap reached"}

        profile_json = json.dumps(profile, sort_keys=True, separators=(",", ":"))
        capabilities = profile.get("capabilities", {})
        pricing = profile.get("pricing", {})
        version = str(profile.get("version", "1"))
        nostr_pubkey = None
        if self.nostr_transport:
            nostr_pubkey = self.nostr_transport.get_identity().get("pubkey")

        conn = self.db._get_connection()
        conn.execute(
            "INSERT OR REPLACE INTO marketplace_profiles "
            "(advisor_did, profile_json, nostr_pubkey, version, capabilities_json, pricing_json, "
            "reputation_score, last_seen, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                advisor_did,
                profile_json,
                nostr_pubkey,
                version,
                json.dumps(capabilities, sort_keys=True, separators=(",", ":")),
                json.dumps(pricing, sort_keys=True, separators=(",", ":")),
                int(profile.get("reputation_score", 0)),
                now,
                "nostr" if self.nostr_transport else "local",
            ),
        )

        event = None
        if self.nostr_transport:
            event = self.nostr_transport.publish({
                "kind": 38380,
                "content": profile_json,
                "tags": [["t", "hive-advisor-profile"]],
            })
            self.db.set_nostr_state("event:last_marketplace_profile_id", event.get("id", ""))

        self._our_profile = profile
        self._last_profile_publish_at = now
        return {
            "ok": True,
            "advisor_did": advisor_did,
            "nostr_event_id": event.get("id") if event else None,
        }

    def _resolve_advisor_nostr_pubkey(self, advisor_did: str) -> Optional[str]:
        """Resolve advisor DID to cached Nostr pubkey when available."""
        conn = self.db._get_connection()
        row = conn.execute(
            "SELECT nostr_pubkey FROM marketplace_profiles WHERE advisor_did = ?",
            (advisor_did,),
        ).fetchone()
        if row and row["nostr_pubkey"]:
            return str(row["nostr_pubkey"])
        return None

    def propose_contract(self, advisor_did: str, node_id: str, scope: Dict[str, Any],
                         tier: str, pricing: Dict[str, Any],
                         operator_id: Optional[str] = None) -> Dict[str, Any]:
        """Create a proposed contract and send a DM proposal."""
        now = int(time.time())
        if self.db.count_rows("marketplace_contracts") >= self.db.MAX_MARKETPLACE_CONTRACT_ROWS:
            return {"error": "marketplace contract row cap reached"}

        contract_id = str(uuid.uuid4())
        conn = self.db._get_connection()
        conn.execute(
            "INSERT INTO marketplace_contracts (contract_id, advisor_did, operator_id, node_id, status, tier, "
            "scope_json, pricing_json, created_at) VALUES (?, ?, ?, ?, 'proposed', ?, ?, ?, ?)",
            (
                contract_id,
                advisor_did,
                operator_id or node_id,
                node_id,
                tier or "standard",
                json.dumps(scope or {}, sort_keys=True, separators=(",", ":")),
                json.dumps(pricing or {}, sort_keys=True, separators=(",", ":")),
                now,
            ),
        )

        dm_event_id = None
        if self.nostr_transport:
            recipient = self._resolve_advisor_nostr_pubkey(advisor_did) or advisor_did
            # Only send DM when recipient resolves to a valid 32-byte hex pubkey.
            if len(recipient) == 64 and all(c in "0123456789abcdefABCDEF" for c in recipient):
                dm_payload = {
                    "type": "contract_proposal",
                    "contract_id": contract_id,
                    "advisor_did": advisor_did,
                    "node_id": node_id,
                    "tier": tier,
                    "scope": scope or {},
                    "pricing": pricing or {},
                }
                dm_event = self.nostr_transport.send_dm(
                    recipient_pubkey=recipient,
                    plaintext=json.dumps(dm_payload, sort_keys=True, separators=(",", ":")),
                )
                dm_event_id = dm_event.get("id")
            else:
                self._log(
                    f"contract {contract_id[:8]}: no valid nostr_pubkey for advisor_did {advisor_did[:16]}...",
                    level="warn",
                )
        return {"ok": True, "contract_id": contract_id, "dm_event_id": dm_event_id}

    def accept_contract(self, contract_id: str) -> Dict[str, Any]:
        """Accept a proposed contract and publish confirmation event."""
        conn = self.db._get_connection()
        row = conn.execute(
            "SELECT * FROM marketplace_contracts WHERE contract_id = ?",
            (contract_id,),
        ).fetchone()
        if not row:
            return {"error": "contract not found"}

        now = int(time.time())
        conn.execute(
            "UPDATE marketplace_contracts SET status = 'active', contract_start = ? WHERE contract_id = ?",
            (now, contract_id),
        )

        event = None
        if self.nostr_transport:
            event = self.nostr_transport.publish({
                "kind": 38383,
                "content": json.dumps({"contract_id": contract_id, "status": "active"}, separators=(",", ":")),
                "tags": [["t", "hive-contract-confirmation"]],
            })
        return {"ok": True, "contract_id": contract_id, "nostr_event_id": event.get("id") if event else None}

    def _active_trial_count(self, node_id: str) -> int:
        conn = self.db._get_connection()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM marketplace_trials WHERE node_id = ? AND outcome IS NULL",
            (node_id,),
        ).fetchone()
        return int(row["cnt"]) if row else 0

    def _next_trial_sequence(self, node_id: str, scope: str) -> int:
        conn = self.db._get_connection()
        cutoff = int(time.time()) - (90 * 86400)
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM marketplace_trials WHERE node_id = ? AND scope = ? AND start_at > ?",
            (node_id, scope, cutoff),
        ).fetchone()
        return int(row["cnt"] or 0) + 1

    def start_trial(self, contract_id: str, duration_days: int = 14,
                    flat_fee_sats: int = 0) -> Dict[str, Any]:
        """Start a contract trial with anti-gaming constraints."""
        conn = self.db._get_connection()
        row = conn.execute(
            "SELECT * FROM marketplace_contracts WHERE contract_id = ?",
            (contract_id,),
        ).fetchone()
        if not row:
            return {"error": "contract not found"}
        contract = dict(row)
        node_id = contract["node_id"]
        scope_obj = json.loads(contract["scope_json"] or "{}")
        scope = str(scope_obj.get("scope") or "default")

        if self._active_trial_count(node_id) >= self.MAX_ACTIVE_TRIALS:
            return {"error": "max active trials reached"}

        cooldown_cutoff = int(time.time()) - (self.TRIAL_COOLDOWN_DAYS * 86400)
        prev = conn.execute(
            "SELECT mt.advisor_did FROM marketplace_trials mt "
            "JOIN marketplace_contracts mc ON mc.contract_id = mt.contract_id "
            "WHERE mt.node_id = ? AND mt.scope = ? AND mt.start_at > ? "
            "AND mt.advisor_did != ? LIMIT 1",
            (node_id, scope, cooldown_cutoff, contract["advisor_did"]),
        ).fetchone()
        if prev:
            return {"error": "trial cooldown active"}

        if self.db.count_rows("marketplace_trials") >= self.db.MAX_MARKETPLACE_TRIAL_ROWS:
            return {"error": "marketplace trial row cap reached"}

        now = int(time.time())
        trial_id = str(uuid.uuid4())
        sequence = self._next_trial_sequence(node_id, scope)
        end_at = now + max(1, int(duration_days)) * 86400
        conn.execute(
            "INSERT INTO marketplace_trials (trial_id, contract_id, advisor_did, node_id, scope, "
            "sequence_number, flat_fee_sats, start_at, end_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trial_id,
                contract_id,
                contract["advisor_did"],
                node_id,
                scope,
                sequence,
                max(0, int(flat_fee_sats)),
                now,
                end_at,
            ),
        )
        conn.execute(
            "UPDATE marketplace_contracts SET status = 'trial', trial_start = ?, trial_end = ? WHERE contract_id = ?",
            (now, end_at, contract_id),
        )
        return {"ok": True, "trial_id": trial_id, "sequence_number": sequence, "end_at": end_at}

    def evaluate_trial(self, contract_id: str, evaluation: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Evaluate trial and mark pass/fail/extended."""
        conn = self.db._get_connection()
        row = conn.execute(
            "SELECT * FROM marketplace_trials WHERE contract_id = ? ORDER BY start_at DESC LIMIT 1",
            (contract_id,),
        ).fetchone()
        if not row:
            return {"error": "trial not found"}
        trial = dict(row)
        metrics = evaluation or {}
        actions = int(metrics.get("actions_taken", 0))
        uptime = float(metrics.get("uptime_pct", 0))
        revenue_delta = float(metrics.get("revenue_delta", 0))
        outcome = "pass" if actions >= 10 and uptime >= 95 and revenue_delta >= -5 else "fail"

        conn.execute(
            "UPDATE marketplace_trials SET evaluation_json = ?, outcome = ? WHERE trial_id = ?",
            (json.dumps(metrics, sort_keys=True, separators=(",", ":")), outcome, trial["trial_id"]),
        )
        conn.execute(
            "UPDATE marketplace_contracts SET status = ? WHERE contract_id = ?",
            ("active" if outcome == "pass" else "terminated", contract_id),
        )
        return {"ok": True, "trial_id": trial["trial_id"], "outcome": outcome}

    def terminate_contract(self, contract_id: str, reason: str = "") -> Dict[str, Any]:
        """Terminate an advisor contract."""
        conn = self.db._get_connection()
        now = int(time.time())
        cursor = conn.execute(
            "UPDATE marketplace_contracts SET status = 'terminated', terminated_at = ?, termination_reason = ? "
            "WHERE contract_id = ?",
            (now, reason, contract_id),
        )
        if cursor.rowcount <= 0:
            return {"error": "contract not found"}
        return {"ok": True, "contract_id": contract_id}

    def cleanup_stale_profiles(self) -> int:
        """Expire stale advisor profiles."""
        conn = self.db._get_connection()
        cutoff = int(time.time()) - (self.PROFILE_STALE_DAYS * 86400)
        cursor = conn.execute(
            "DELETE FROM marketplace_profiles WHERE last_seen < ?",
            (cutoff,),
        )
        return int(cursor.rowcount or 0)

    def evaluate_expired_trials(self) -> int:
        """Auto-fail un-evaluated expired trials."""
        conn = self.db._get_connection()
        now = int(time.time())
        trial_rows = conn.execute(
            "SELECT trial_id, contract_id FROM marketplace_trials "
            "WHERE end_at < ? AND outcome IS NULL",
            (now,),
        ).fetchall()
        if not trial_rows:
            return 0

        conn.execute(
            "UPDATE marketplace_trials SET outcome = 'fail' WHERE end_at < ? AND outcome IS NULL",
            (now,),
        )
        contract_ids = {row["contract_id"] for row in trial_rows}
        for contract_id in contract_ids:
            conn.execute(
                "UPDATE marketplace_contracts SET status = 'terminated' "
                "WHERE contract_id = ? AND status = 'trial'",
                (contract_id,),
            )
        return len(trial_rows)

    def check_contract_renewals(self) -> List[Dict[str, Any]]:
        """List active contracts approaching expiration."""
        conn = self.db._get_connection()
        now = int(time.time())
        rows = conn.execute(
            "SELECT * FROM marketplace_contracts WHERE status = 'active' AND contract_end IS NOT NULL "
            "AND contract_end > ?",
            (now,),
        ).fetchall()
        notices = []
        for row in rows:
            contract = dict(row)
            notice_window = int(contract.get("notice_days", 7)) * 86400
            if int(contract.get("contract_end") or 0) <= now + notice_window:
                notices.append(contract)
        return notices

    def republish_profile(self) -> Optional[Dict[str, Any]]:
        """Re-publish local profile every 4 hours."""
        if not self._our_profile:
            return None
        now = int(time.time())
        if now - self._last_profile_publish_at < (4 * 3600):
            return None
        return self.publish_profile(self._our_profile)
