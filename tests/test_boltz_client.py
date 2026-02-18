"""
Unit tests for modules.boltz_client.
"""

import subprocess

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.boltz_client import BoltzClient, BoltzConfig


def _completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def test_status_reports_binary_missing(monkeypatch):
    client = BoltzClient(config=BoltzConfig(binary="missing-boltzcli"))
    monkeypatch.setattr("modules.boltz_client.shutil.which", lambda _: None)

    result = client.status()
    assert result["enabled"] is False
    assert result["available"] is False
    assert "not found" in result["error"]


def test_status_reports_connectivity_failure(monkeypatch):
    client = BoltzClient(config=BoltzConfig(binary="boltzcli"))
    monkeypatch.setattr("modules.boltz_client.shutil.which", lambda _: "/usr/bin/boltzcli")

    def fake_run(cmd, capture_output, text, timeout, check):  # noqa: ARG001
        if "--version" in cmd:
            return _completed(cmd, stdout="boltzcli version v2.11.0\n")
        return _completed(cmd, returncode=1, stderr="connection refused")

    monkeypatch.setattr("modules.boltz_client.subprocess.run", fake_run)
    result = client.status()

    assert result["enabled"] is True
    assert result["available"] is False
    assert result["error"] == "connection refused"
    assert result["version"] == "boltzcli version v2.11.0"


def test_create_swap_in_builds_expected_command(monkeypatch):
    captured = {}
    client = BoltzClient(config=BoltzConfig(binary="boltzcli", timeout_seconds=15))

    def fake_run(cmd, capture_output, text, timeout, check):  # noqa: ARG001
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        return _completed(cmd, stdout='{"id":"swap-1"}')

    monkeypatch.setattr("modules.boltz_client.subprocess.run", fake_run)
    result = client.create_swap_in(
        amount_sats=100_000,
        currency="btc",
        invoice="lnbc1invoice",
        from_wallet="wallet-1",
        refund_address="bc1qrefund",
        external_pay=True,
    )

    assert result["ok"] is True
    assert result["result"]["id"] == "swap-1"
    assert captured["timeout"] == 15
    assert captured["cmd"] == [
        "boltzcli",
        "createswap",
        "--json",
        "--from-wallet",
        "wallet-1",
        "--external-pay",
        "--refund",
        "bc1qrefund",
        "--invoice",
        "lnbc1invoice",
        "btc",
        "100000",
    ]


def test_create_swap_out_builds_expected_command(monkeypatch):
    captured = {}
    client = BoltzClient(config=BoltzConfig(binary="boltzcli"))

    def fake_run(cmd, capture_output, text, timeout, check):  # noqa: ARG001
        captured["cmd"] = cmd
        return _completed(cmd, stdout='{"id":"reverse-1"}')

    monkeypatch.setattr("modules.boltz_client.subprocess.run", fake_run)
    result = client.create_swap_out(
        amount_sats=200_000,
        currency="lbtc",
        address="ex1qqaddress",
        to_wallet="wallet-2",
        external_pay=True,
        no_zero_conf=True,
        description="rebalance-out",
        routing_fee_limit_ppm=2500,
        chan_ids=["123x1x0", "321x2x1"],
    )

    assert result["ok"] is True
    assert result["result"]["id"] == "reverse-1"
    assert captured["cmd"] == [
        "boltzcli",
        "createreverseswap",
        "--json",
        "--to-wallet",
        "wallet-2",
        "--no-zero-conf",
        "--external-pay",
        "--description",
        "rebalance-out",
        "--routing-fee-limit-ppm",
        "2500",
        "--chan-id",
        "123x1x0",
        "--chan-id",
        "321x2x1",
        "lbtc",
        "200000",
        "ex1qqaddress",
    ]


def test_quote_rejects_invalid_currency():
    client = BoltzClient(config=BoltzConfig(binary="boltzcli"))

    result = client.quote_submarine(amount_sats=50000, currency="doge")
    assert result["ok"] is False
    assert "currency must be one of" in result["error"]


def test_command_timeout(monkeypatch):
    client = BoltzClient(config=BoltzConfig(binary="boltzcli", timeout_seconds=7))

    def fake_run(cmd, capture_output, text, timeout, check):  # noqa: ARG001
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr("modules.boltz_client.subprocess.run", fake_run)
    result = client.quote_reverse(amount_sats=1000, currency="btc")

    assert result["ok"] is False
    assert "timed out" in result["error"]
