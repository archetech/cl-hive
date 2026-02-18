"""
Boltz client integration for cl-hive.

Wraps the local `boltzcli` binary for submarine swaps:
- swap-in  (chain -> lightning): `createswap`
- swap-out (lightning -> chain): `createreverseswap`
"""

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


VALID_CURRENCIES = {"btc", "lbtc"}


@dataclass
class BoltzConfig:
    """Configuration for boltzcli invocation."""
    binary: str = "boltzcli"
    timeout_seconds: int = 60
    host: str = ""
    port: int = 0
    datadir: str = ""
    tlscert: str = ""
    macaroon: str = ""
    tenant: str = ""
    password: str = ""
    no_macaroons: bool = False


class BoltzClient:
    """Thin wrapper around boltzcli subprocess calls."""

    def __init__(self, plugin: Any = None, config: Optional[BoltzConfig] = None):
        self.plugin = plugin
        self.config = config or BoltzConfig()

    def _log(self, msg: str, level: str = "info") -> None:
        if self.plugin:
            self.plugin.log(f"cl-hive: boltz: {msg}", level=level)

    def _normalize_currency(self, currency: str) -> Tuple[Optional[str], Optional[str]]:
        cur = (currency or "btc").strip().lower()
        if cur not in VALID_CURRENCIES:
            return None, "currency must be one of: btc, lbtc"
        return cur, None

    def _base_command(self) -> List[str]:
        cfg = self.config
        cmd: List[str] = [cfg.binary]
        if cfg.host:
            cmd.extend(["--host", cfg.host])
        if cfg.port > 0:
            cmd.extend(["--port", str(cfg.port)])
        if cfg.datadir:
            cmd.extend(["--datadir", cfg.datadir])
        if cfg.tlscert:
            cmd.extend(["--tlscert", cfg.tlscert])
        if cfg.macaroon:
            cmd.extend(["--macaroon", cfg.macaroon])
        if cfg.tenant:
            cmd.extend(["--tenant", cfg.tenant])
        if cfg.password:
            cmd.extend(["--password", cfg.password])
        if cfg.no_macaroons:
            cmd.append("--no-macaroons")
        return cmd

    def _run_command(
        self,
        args: List[str],
        expect_json: bool = True,
    ) -> Dict[str, Any]:
        cmd = self._base_command() + args

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=max(5, int(self.config.timeout_seconds)),
                check=False,
            )
        except FileNotFoundError:
            return {
                "ok": False,
                "error": f"boltz binary not found: {self.config.binary}",
                "command": cmd,
            }
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"boltz command timed out after {self.config.timeout_seconds}s",
                "command": cmd,
            }
        except Exception as e:
            return {
                "ok": False,
                "error": f"boltz command failed: {e}",
                "command": cmd,
            }

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0:
            return {
                "ok": False,
                "error": stderr or stdout or f"boltz exited with code {proc.returncode}",
                "command": cmd,
                "exit_code": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
            }

        if not expect_json:
            return {
                "ok": True,
                "command": cmd,
                "exit_code": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
            }

        try:
            parsed = json.loads(stdout) if stdout else {}
        except json.JSONDecodeError:
            return {
                "ok": False,
                "error": "boltz output was not valid JSON",
                "command": cmd,
                "exit_code": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
            }

        return {
            "ok": True,
            "command": cmd,
            "exit_code": proc.returncode,
            "result": parsed,
            "stderr": stderr,
        }

    def status(self) -> Dict[str, Any]:
        """Check boltz binary presence and daemon reachability."""
        binary_path = shutil.which(self.config.binary)
        if not binary_path:
            return {
                "enabled": False,
                "available": False,
                "error": f"boltz binary not found: {self.config.binary}",
            }

        version = self._run_command(["--version"], expect_json=False)
        if not version.get("ok"):
            return {
                "enabled": True,
                "available": False,
                "binary": binary_path,
                "error": version.get("error", "failed to get version"),
            }

        # Lightweight connectivity check.
        pairs = self._run_command(["getpairs", "--json"], expect_json=True)
        error = ""
        if not pairs.get("ok"):
            error = pairs.get("error", "connectivity check failed")
        return {
            "enabled": True,
            "available": bool(pairs.get("ok")),
            "binary": binary_path,
            "version": (version.get("stdout") or "").strip(),
            "error": error,
            "pairs_check": pairs,
        }

    def quote_submarine(self, amount_sats: int, currency: str = "btc") -> Dict[str, Any]:
        """Quote chain->lightning swap fees."""
        if amount_sats <= 0:
            return {"ok": False, "error": "amount_sats must be > 0"}
        cur, err = self._normalize_currency(currency)
        if err:
            return {"ok": False, "error": err}
        return self._run_command(
            ["quote", "submarine", "--json", "--send", str(amount_sats), "--from", cur.upper()],
            expect_json=True,
        )

    def quote_reverse(self, amount_sats: int, currency: str = "btc") -> Dict[str, Any]:
        """Quote lightning->chain swap fees."""
        if amount_sats <= 0:
            return {"ok": False, "error": "amount_sats must be > 0"}
        cur, err = self._normalize_currency(currency)
        if err:
            return {"ok": False, "error": err}
        return self._run_command(
            ["quote", "reverse", "--json", "--send", str(amount_sats), "--to", cur.upper()],
            expect_json=True,
        )

    def create_swap_in(
        self,
        amount_sats: int,
        currency: str = "btc",
        invoice: str = "",
        from_wallet: str = "",
        refund_address: str = "",
        external_pay: bool = False,
    ) -> Dict[str, Any]:
        """Create chain->lightning submarine swap."""
        if amount_sats <= 0:
            return {"ok": False, "error": "amount_sats must be > 0"}
        cur, err = self._normalize_currency(currency)
        if err:
            return {"ok": False, "error": err}

        args = ["createswap", "--json"]
        if from_wallet:
            args.extend(["--from-wallet", from_wallet])
        if external_pay:
            args.append("--external-pay")
        if refund_address:
            args.extend(["--refund", refund_address])
        if invoice:
            args.extend(["--invoice", invoice])
        args.extend([cur, str(amount_sats)])
        return self._run_command(args, expect_json=True)

    def create_swap_out(
        self,
        amount_sats: int,
        currency: str = "btc",
        address: str = "",
        to_wallet: str = "",
        external_pay: bool = False,
        no_zero_conf: bool = False,
        description: str = "",
        routing_fee_limit_ppm: int = 0,
        chan_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Create lightning->chain reverse swap."""
        if amount_sats <= 0:
            return {"ok": False, "error": "amount_sats must be > 0"}
        cur, err = self._normalize_currency(currency)
        if err:
            return {"ok": False, "error": err}
        if routing_fee_limit_ppm < 0:
            return {"ok": False, "error": "routing_fee_limit_ppm must be >= 0"}

        args = ["createreverseswap", "--json"]
        if to_wallet:
            args.extend(["--to-wallet", to_wallet])
        if no_zero_conf:
            args.append("--no-zero-conf")
        if external_pay:
            args.append("--external-pay")
        if description:
            args.extend(["--description", description])
        if routing_fee_limit_ppm > 0:
            args.extend(["--routing-fee-limit-ppm", str(routing_fee_limit_ppm)])
        for chan_id in (chan_ids or []):
            if chan_id:
                args.extend(["--chan-id", str(chan_id)])

        args.extend([cur, str(amount_sats)])
        if address:
            args.append(address)
        return self._run_command(args, expect_json=True)
