#!/usr/bin/env python3
"""
Boltz v2 Reverse Swap (Loop Out) - Lightning → On-chain BTC

Sends Lightning sats through Boltz to receive on-chain BTC.
Tracks all costs in a JSON ledger for fleet accounting.

Requirements:
  - Python 3.8+, ecdsa, httpx (or requests)
  - CLN node with `pay` and `newaddr` permissions in the rune
  - Rune update needed: current rune lacks `pay` and `newaddr` methods

Usage:
  boltz-loopout.py --node hive-nexus-01 --amount 1000000 [--address bc1q...] [--dry-run]
  boltz-loopout.py --quote 1000000
  boltz-loopout.py --status <swap_id>
  boltz-loopout.py --history [--node hive-nexus-01]

Boltz v2 Reverse Swap Flow:
  1. Generate preimage + keypair
  2. Create swap on Boltz (get invoice)
  3. Pay invoice via CLN
  4. Boltz locks BTC on-chain in Taproot HTLC
  5. Cooperative claim: POST preimage to Boltz, they co-sign + broadcast
  6. Log costs

Fees (BTC→BTC reverse): 0.5% + ~530 sats miner (222 claim + 308 lockup)
Limits: 25,000 - 25,000,000 sats per swap
"""

import argparse
import hashlib
import json
import logging
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOLTZ_API = os.environ.get("BOLTZ_API", "https://api.boltz.exchange/v2")
NODES_CONFIG = os.environ.get(
    "HIVE_NODES_CONFIG",
    "/home/sat/bin/cl-hive/production/nodes.production.json",
)
SWAP_LEDGER = os.environ.get(
    "BOLTZ_SWAP_LEDGER",
    "/home/sat/bin/cl-hive/production/data/boltz-swaps.json",
)

POLL_INTERVAL = 10  # seconds between status polls
POLL_TIMEOUT = 600  # max seconds to wait for on-chain lockup
PAY_TIMEOUT = 120   # seconds to wait for CLN pay

logger = logging.getLogger("boltz-loopout")

# ---------------------------------------------------------------------------
# HTTP helpers (use httpx if available, fall back to urllib)
# ---------------------------------------------------------------------------

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False
    import urllib.request
    import urllib.error
    import ssl


def _http_get(url: str, timeout: int = 30) -> Dict:
    if _HAS_HTTPX:
        r = httpx.get(url, timeout=timeout, verify=False)
        r.raise_for_status()
        return r.json()
    else:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read())


def _http_post(url: str, data: Dict, timeout: int = 30, headers: Optional[Dict] = None) -> Tuple[int, Dict]:
    if _HAS_HTTPX:
        r = httpx.post(url, json=data, timeout=timeout, headers=headers or {}, verify=False)
        return r.status_code, r.json()
    else:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        body = json.dumps(data).encode()
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


def _cln_call(node_url: str, rune: str, method: str, params: Dict = None, timeout: int = 60) -> Dict:
    """Call CLN REST API via curl (bypasses httpx SSL issues over WireGuard)."""
    import subprocess
    url = f"{node_url}/v1/{method}"
    cmd = [
        "curl", "-sk", "-X", "POST",
        "-H", f"Rune: {rune}",
        "-H", "Content-Type: application/json",
        "-d", json.dumps(params or {}),
        "--max-time", str(max(timeout, 180)),
        url
    ]
    logger.info(f"CLN call: {method} timeout={max(timeout, 180)}s url={url}")
    # Retry up to 3 times on connection errors (WireGuard flakiness)
    last_err = None
    for attempt in range(3):
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=max(timeout, 180) + 30)
        if result.returncode == 0 and result.stdout.strip():
            break
        last_err = f"rc={result.returncode} stderr={result.stderr[:200]} stdout={result.stdout[:200]}"
        logger.warning(f"CLN {method} attempt {attempt+1}/3 failed: {last_err}")
        if attempt < 2:
            import time as _time
            _time.sleep(2)
    else:
        raise RuntimeError(f"CLN {method} curl failed after 3 attempts: {last_err}")
    if not result.stdout.strip():
        raise RuntimeError(f"CLN {method} returned empty response")
    body = json.loads(result.stdout)
    if "error" in body:
        raise RuntimeError(f"CLN {method} error: {json.dumps(body)}")
    return body


# ---------------------------------------------------------------------------
# Key generation (secp256k1 via ecdsa library)
# ---------------------------------------------------------------------------

def generate_claim_keypair() -> Tuple[bytes, bytes]:
    """Generate a secp256k1 keypair. Returns (privkey_32bytes, x_only_pubkey_32bytes)."""
    from ecdsa import SECP256k1, SigningKey

    sk = SigningKey.generate(curve=SECP256k1)
    privkey = sk.to_string()  # 32 bytes

    # Get the compressed public key (33 bytes: 02/03 prefix + x coordinate)
    vk = sk.get_verifying_key()
    point = vk.to_string()  # 64 bytes: x (32) + y (32)
    x_bytes = point[:32]
    y_bytes = point[32:]
    # Even y → 02 prefix, odd y → 03 prefix
    prefix = b'\x02' if y_bytes[-1] % 2 == 0 else b'\x03'
    compressed = prefix + x_bytes

    return privkey, compressed


def generate_preimage() -> Tuple[bytes, bytes]:
    """Generate random preimage and its SHA-256 hash."""
    preimage = secrets.token_bytes(32)
    preimage_hash = hashlib.sha256(preimage).digest()
    return preimage, preimage_hash


# ---------------------------------------------------------------------------
# Node config loading
# ---------------------------------------------------------------------------

def load_node_config(node_name: str) -> Dict:
    """Load node connection details from nodes.production.json."""
    with open(NODES_CONFIG) as f:
        config = json.load(f)

    for node in config.get("nodes", []):
        if node["name"] == node_name:
            return node

    raise ValueError(f"Node '{node_name}' not found in {NODES_CONFIG}")


def get_node_url(node: Dict) -> str:
    """Get the REST URL for a node."""
    if node.get("docker_container"):
        raise ValueError(f"Docker nodes not supported for loop-out (need REST API)")
    # Prefer rest_url if present (new config format)
    if node.get("rest_url"):
        return node["rest_url"].rstrip("/")
    host = node.get("host", "localhost")
    port = node.get("port", 3010)
    return f"https://{host}:{port}"


# ---------------------------------------------------------------------------
# Swap ledger
# ---------------------------------------------------------------------------

def load_ledger() -> Dict:
    """Load the swap ledger, creating if needed."""
    path = Path(SWAP_LEDGER)
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {"swaps": [], "totals": _empty_totals()}


def save_ledger(ledger: Dict):
    """Save the swap ledger with updated totals."""
    ledger["totals"] = _compute_totals(ledger["swaps"])
    path = Path(SWAP_LEDGER)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(ledger, f, indent=2)


def _empty_totals() -> Dict:
    return {
        "total_swaps": 0,
        "completed_swaps": 0,
        "failed_swaps": 0,
        "total_looped_out_sats": 0,
        "total_received_onchain_sats": 0,
        "total_cost_sats": 0,
        "avg_cost_ppm": 0,
    }


def _compute_totals(swaps: list) -> Dict:
    completed = [s for s in swaps if s.get("status") == "completed"]
    failed = [s for s in swaps if s.get("status") == "failed"]
    total_sent = sum(s.get("amount_invoice_sats", 0) for s in completed)
    total_received = sum(s.get("amount_onchain_sats", 0) for s in completed)
    total_cost = sum(s.get("total_cost_sats", 0) for s in completed)
    return {
        "total_swaps": len(swaps),
        "completed_swaps": len(completed),
        "failed_swaps": len(failed),
        "total_looped_out_sats": total_sent,
        "total_received_onchain_sats": total_received,
        "total_cost_sats": total_cost,
        "avg_cost_ppm": int(total_cost * 1_000_000 / total_sent) if total_sent else 0,
    }


def add_swap_record(record: Dict) -> Dict:
    """Add or update a swap record in the ledger."""
    ledger = load_ledger()
    # Update existing or append
    for i, s in enumerate(ledger["swaps"]):
        if s["id"] == record["id"]:
            ledger["swaps"][i] = record
            save_ledger(ledger)
            return record
    ledger["swaps"].append(record)
    save_ledger(ledger)
    return record


# ---------------------------------------------------------------------------
# Boltz API
# ---------------------------------------------------------------------------

def boltz_get_pairs() -> Dict:
    """Get current reverse swap pairs and fees."""
    return _http_get(f"{BOLTZ_API}/swap/reverse")


def boltz_quote(amount_sats: int) -> Dict:
    """Calculate costs for a reverse swap of given amount."""
    pairs = boltz_get_pairs()
    btc_pair = pairs.get("BTC", {}).get("BTC", {})
    if not btc_pair:
        return {"error": "BTC/BTC reverse pair not available"}

    limits = btc_pair.get("limits", {})
    fees = btc_pair.get("fees", {})
    pct = fees.get("percentage", 0.5)
    miner_claim = fees.get("minerFees", {}).get("claim", 222)
    miner_lockup = fees.get("minerFees", {}).get("lockup", 308)

    boltz_fee_sats = int(amount_sats * pct / 100)
    total_miner = miner_claim + miner_lockup
    total_cost = boltz_fee_sats + total_miner
    onchain_amount = amount_sats - boltz_fee_sats - total_miner

    return {
        "invoice_amount_sats": amount_sats,
        "onchain_amount_sats": onchain_amount,
        "boltz_fee_pct": pct,
        "boltz_fee_sats": boltz_fee_sats,
        "miner_fee_claim_sats": miner_claim,
        "miner_fee_lockup_sats": miner_lockup,
        "total_miner_sats": total_miner,
        "total_cost_sats": total_cost,
        "cost_ppm": int(total_cost * 1_000_000 / amount_sats) if amount_sats else 0,
        "limits": limits,
        "pair_hash": btc_pair.get("hash", ""),
    }


def boltz_create_reverse_swap(
    preimage_hash: bytes,
    claim_pubkey: bytes,
    invoice_amount: int,
    address: Optional[str] = None,
    description: str = "Lightning Hive loop-out",
) -> Dict:
    """Create a reverse swap on Boltz."""
    payload: Dict[str, Any] = {
        "from": "BTC",
        "to": "BTC",
        "preimageHash": preimage_hash.hex(),
        "claimPublicKey": claim_pubkey.hex(),
        "invoiceAmount": invoice_amount,
        "description": description,
    }
    if address:
        payload["address"] = address

    status, body = _http_post(f"{BOLTZ_API}/swap/reverse", payload)
    if status >= 400:
        raise RuntimeError(f"Boltz create reverse swap failed ({status}): {json.dumps(body)}")
    return body


def boltz_get_status(swap_id: str) -> Dict:
    """Get swap status."""
    return _http_get(f"{BOLTZ_API}/swap/status?id={swap_id}")


def boltz_get_transaction(swap_id: str) -> Dict:
    """Get lockup transaction details."""
    return _http_get(f"{BOLTZ_API}/swap/reverse/{swap_id}/transaction")


def boltz_cooperative_claim(swap_id: str, preimage: bytes) -> Dict:
    """
    Post preimage for cooperative claim.
    Boltz will settle the Lightning invoice and broadcast the claim tx.
    If no transaction is provided, just the preimage settles the invoice
    and Boltz handles everything.
    """
    payload = {
        "preimage": preimage.hex(),
    }
    status, body = _http_post(f"{BOLTZ_API}/swap/reverse/{swap_id}/claim", payload)
    if status >= 400:
        raise RuntimeError(f"Boltz cooperative claim failed ({status}): {json.dumps(body)}")
    return body


# ---------------------------------------------------------------------------
# Main loop-out flow
# ---------------------------------------------------------------------------

def execute_loop_out(
    node_name: str,
    amount_sats: int,
    address: Optional[str] = None,
    dry_run: bool = False,
) -> Dict:
    """Execute a full loop-out: create swap, pay invoice, claim on-chain."""

    now = datetime.now(timezone.utc).isoformat()

    # 1. Quote
    quote = boltz_quote(amount_sats)
    if "error" in quote:
        return quote

    limits = quote["limits"]
    if amount_sats < limits.get("minimal", 25000):
        return {"error": f"Amount {amount_sats} below minimum {limits['minimal']}"}
    if amount_sats > limits.get("maximal", 25000000):
        return {"error": f"Amount {amount_sats} above maximum {limits['maximal']}"}

    logger.info(f"Quote: send {amount_sats} sats, receive ~{quote['onchain_amount_sats']} on-chain, cost {quote['total_cost_sats']} sats ({quote['cost_ppm']} ppm)")

    if dry_run:
        return {"dry_run": True, "quote": quote}

    # 2. Load node config
    node_cfg = load_node_config(node_name)
    node_url = get_node_url(node_cfg)
    rune = node_cfg["rune"]

    # 3. Get claim address if not provided
    if not address:
        logger.info("Getting new on-chain address from node...")
        addr_result = _cln_call(node_url, rune, "newaddr", {"addresstype": "bech32"})
        if "error" in addr_result:
            return {"error": f"Failed to get address: {addr_result['error']}"}
        address = addr_result.get("bech32")
        if not address:
            return {"error": f"Unexpected newaddr response: {addr_result}"}
        logger.info(f"Claim address: {address}")

    # 4. Generate preimage + keypair
    preimage, preimage_hash = generate_preimage()
    claim_privkey, claim_pubkey = generate_claim_keypair()

    logger.info(f"Preimage hash: {preimage_hash.hex()}")
    logger.info(f"Claim pubkey: {claim_pubkey.hex()}")

    # 5. Create reverse swap on Boltz
    logger.info("Creating reverse swap on Boltz...")
    swap = boltz_create_reverse_swap(
        preimage_hash=preimage_hash,
        claim_pubkey=claim_pubkey,
        invoice_amount=amount_sats,
        address=address,
    )

    swap_id = swap["id"]
    invoice = swap["invoice"]
    onchain_amount = swap.get("onchainAmount", quote["onchain_amount_sats"])
    timeout_block = swap.get("timeoutBlockHeight", 0)

    logger.info(f"Swap created: id={swap_id}")
    logger.info(f"On-chain amount: {onchain_amount} sats")
    logger.info(f"Timeout block: {timeout_block}")

    # 6. Create ledger record
    record = {
        "id": swap_id,
        "node": node_name,
        "created_at": now,
        "amount_invoice_sats": amount_sats,
        "amount_onchain_sats": onchain_amount,
        "boltz_fee_pct": quote["boltz_fee_pct"],
        "boltz_fee_sats": quote["boltz_fee_sats"],
        "miner_fee_lockup_sats": quote["miner_fee_lockup_sats"],
        "miner_fee_claim_sats": quote["miner_fee_claim_sats"],
        "total_cost_sats": amount_sats - onchain_amount,  # actual cost = sent - received
        "cost_ppm": int((amount_sats - onchain_amount) * 1_000_000 / amount_sats) if amount_sats else 0,
        "status": "created",
        "preimage_hash": preimage_hash.hex(),
        "claim_address": address,
        "timeout_block": timeout_block,
        "lockup_txid": None,
        "claim_txid": None,
        "completed_at": None,
        # Store secrets for recovery (file should be protected)
        "_preimage": preimage.hex(),
        "_claim_privkey": claim_privkey.hex(),
    }
    add_swap_record(record)

    # 7. Pay the invoice via CLN
    logger.info(f"Paying invoice via {node_name}...")
    record["status"] = "paying"
    add_swap_record(record)

    try:
        # Try xpay first (newer), fall back to pay
        try:
            pay_result = _cln_call(node_url, rune, "xpay", {
                "invstring": invoice,
            }, timeout=PAY_TIMEOUT)
        except Exception as e:
            if "Unknown command" in str(e) or "not in allowlist" in str(e):
                pay_result = _cln_call(node_url, rune, "pay", {
                    "bolt11": invoice,
                }, timeout=PAY_TIMEOUT)
            else:
                raise

        if "error" in pay_result:
            record["status"] = "failed"
            record["error"] = pay_result["error"]
            add_swap_record(record)
            return {"error": f"Payment failed: {pay_result['error']}", "swap_id": swap_id}

        logger.info(f"Payment sent! Status: {pay_result.get('status', 'unknown')}")
        record["status"] = "paid"
        record["payment_preimage"] = pay_result.get("payment_preimage", "")
        add_swap_record(record)

    except Exception as e:
        record["status"] = "failed"
        record["error"] = str(e)
        add_swap_record(record)
        return {"error": f"Payment failed: {e}", "swap_id": swap_id}

    # 8. Wait for Boltz to lock on-chain
    logger.info("Waiting for Boltz to lock on-chain funds...")
    record["status"] = "awaiting_lockup"
    add_swap_record(record)

    lockup_seen = False
    start_time = time.time()

    while time.time() - start_time < POLL_TIMEOUT:
        try:
            swap_status = boltz_get_status(swap_id)
            status_str = swap_status.get("status", "")
            logger.debug(f"Swap status: {status_str}")

            if status_str in ("transaction.mempool", "transaction.confirmed"):
                lockup_seen = True
                # Get the lockup tx
                try:
                    tx_info = boltz_get_transaction(swap_id)
                    record["lockup_txid"] = tx_info.get("id")
                    logger.info(f"Lockup tx: {record['lockup_txid']}")
                except Exception:
                    pass
                break
            elif status_str == "swap.expired":
                record["status"] = "expired"
                add_swap_record(record)
                return {"error": "Swap expired before lockup", "swap_id": swap_id}
            elif status_str.startswith("transaction.failed") or status_str.startswith("swap.error"):
                record["status"] = "failed"
                record["error"] = status_str
                add_swap_record(record)
                return {"error": f"Swap failed: {status_str}", "swap_id": swap_id}

        except Exception as e:
            logger.warning(f"Status poll error: {e}")

        time.sleep(POLL_INTERVAL)

    if not lockup_seen:
        record["status"] = "timeout_lockup"
        add_swap_record(record)
        return {"error": "Timed out waiting for on-chain lockup", "swap_id": swap_id,
                "note": "Swap may still complete - check with --status"}

    # 9. Cooperative claim
    logger.info("Posting preimage for cooperative claim...")
    record["status"] = "claiming"
    add_swap_record(record)

    try:
        claim_result = boltz_cooperative_claim(swap_id, preimage)
        logger.info(f"Cooperative claim result: {json.dumps(claim_result)}")

        # The claim may return empty {} on success (Boltz handles broadcasting)
        record["status"] = "completed"
        record["completed_at"] = datetime.now(timezone.utc).isoformat()
        add_swap_record(record)

        # Clean up secrets from the record after success
        # (keep them in case we need recovery, but mark complete)

    except Exception as e:
        logger.error(f"Cooperative claim failed: {e}")
        record["status"] = "claim_failed"
        record["error"] = str(e)
        add_swap_record(record)
        return {
            "error": f"Cooperative claim failed: {e}",
            "swap_id": swap_id,
            "note": "Funds are locked on-chain. Manual script-path claim may be needed.",
            "preimage": preimage.hex(),
            "claim_privkey": claim_privkey.hex(),
            "lockup_address": swap.get("lockupAddress"),
            "swap_tree": swap.get("swapTree"),
        }

    # 10. Final summary
    actual_cost = amount_sats - onchain_amount
    return {
        "status": "completed",
        "swap_id": swap_id,
        "node": node_name,
        "sent_sats": amount_sats,
        "received_onchain_sats": onchain_amount,
        "total_cost_sats": actual_cost,
        "cost_ppm": int(actual_cost * 1_000_000 / amount_sats),
        "claim_address": address,
        "lockup_txid": record.get("lockup_txid"),
    }


# ---------------------------------------------------------------------------
# Status / History commands
# ---------------------------------------------------------------------------

def check_status(swap_id: str) -> Dict:
    """Check status of a swap from ledger + Boltz API."""
    ledger = load_ledger()
    local = None
    for s in ledger["swaps"]:
        if s["id"] == swap_id:
            local = s
            break

    try:
        remote = boltz_get_status(swap_id)
    except Exception as e:
        remote = {"error": str(e)}

    return {
        "local_record": local,
        "boltz_status": remote,
    }


def show_history(node_filter: Optional[str] = None, limit: int = 20) -> Dict:
    """Show swap history with cost summary."""
    ledger = load_ledger()
    swaps = ledger["swaps"]
    if node_filter:
        swaps = [s for s in swaps if s.get("node") == node_filter]

    return {
        "swaps": swaps[-limit:],
        "totals": _compute_totals(swaps),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Boltz v2 Reverse Swap (Loop Out) - Lightning → On-chain BTC",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --quote 1000000                          # Get cost estimate
  %(prog)s --node hive-nexus-01 --amount 1000000    # Execute loop-out
  %(prog)s --node hive-nexus-01 --amount 500000 --address bc1q...  # Specific address
  %(prog)s --node hive-nexus-01 --amount 500000 --dry-run  # Dry run
  %(prog)s --status abc123                           # Check swap status
  %(prog)s --history                                 # View all swaps
  %(prog)s --history --node hive-nexus-02            # View node-specific swaps

NOTE: CLN rune must include 'pay' (or 'xpay') and 'newaddr' methods.
""",
    )

    parser.add_argument("--node", help="Node name (e.g. hive-nexus-01)")
    parser.add_argument("--amount", type=int, help="Amount in sats to loop out")
    parser.add_argument("--address", help="Destination BTC address (default: node newaddr)")
    parser.add_argument("--dry-run", action="store_true", help="Quote only, don't execute")
    parser.add_argument("--quote", type=int, metavar="AMOUNT", help="Get cost quote for amount")
    parser.add_argument("--status", metavar="SWAP_ID", help="Check swap status")
    parser.add_argument("--history", action="store_true", help="Show swap history")
    parser.add_argument("--limit", type=int, default=20, help="History limit")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if args.quote:
        result = boltz_quote(args.quote)
        print(json.dumps(result, indent=2))
        return

    if args.status:
        result = check_status(args.status)
        print(json.dumps(result, indent=2))
        return

    if args.history:
        result = show_history(args.node, args.limit)
        print(json.dumps(result, indent=2))
        return

    if args.node and args.amount:
        result = execute_loop_out(
            node_name=args.node,
            amount_sats=args.amount,
            address=args.address,
            dry_run=args.dry_run,
        )
        print(json.dumps(result, indent=2))
        if result.get("status") == "completed":
            sys.exit(0)
        else:
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
