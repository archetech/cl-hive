# Boltz Swaps Integration (Swap-In / Swap-Out)

**Status:** Implemented in `cl-hive` (initial version)  
**Last Updated:** 2026-02-18

---

## Scope

Integrate local `boltzcli` into `cl-hive` so operators can:

- create **swap-in** (chain -> Lightning) requests
- create **swap-out** (Lightning -> chain) requests
- run **dry-run quotes** before creating swaps

This implementation is operator-initiated via RPC and does not auto-execute swaps from settlement loops.

---

## Implementation Summary

### 1. Boltz wrapper module

New file: `modules/boltz_client.py`

- wraps `boltzcli` subprocess calls with timeout + JSON parsing
- supports:
  - `status()`
  - `quote_submarine()`
  - `quote_reverse()`
  - `create_swap_in()`
  - `create_swap_out()`
- validates swap currency (`btc`, `lbtc`)

### 2. RPC command handlers

Updated: `modules/rpc_commands.py`

- `boltz_status(ctx)`
- `boltz_swap_in(...)`
- `boltz_swap_out(...)`

Behavior:

- `dry_run=true` (default) returns quote only
- `dry_run=false` creates swap via `boltzcli`
- member-tier permission required

### 3. Plugin wiring + config

Updated: `cl-hive.py`

- initializes `BoltzClient` during plugin startup
- adds methods:
  - `hive-boltz-status`
  - `hive-boltz-swap-in`
  - `hive-boltz-swap-out`
- adds config options:
  - `hive-boltz-enabled`
  - `hive-boltz-binary`
  - `hive-boltz-timeout-seconds`
  - `hive-boltz-host`
  - `hive-boltz-port`
  - `hive-boltz-datadir`
  - `hive-boltz-tlscert`
  - `hive-boltz-macaroon`
  - `hive-boltz-tenant`
  - `hive-boltz-no-macaroons`

Updated: `cl-hive.conf.sample` with matching commented options.

---

## RPC Usage

Status:

```bash
lightning-cli hive-boltz-status
```

Swap-in quote:

```bash
lightning-cli hive-boltz-swap-in 100000 btc
```

Create swap-in:

```bash
lightning-cli hive-boltz-swap-in 100000 btc "" "" "" false false
```

Swap-out quote:

```bash
lightning-cli hive-boltz-swap-out 100000 btc
```

Create swap-out:

```bash
lightning-cli hive-boltz-swap-out 100000 btc bc1q... "" false false "rebalance" 1500 '[]' false
```

---

## Safety Defaults

- `dry_run=true` by default for both swap directions.
- If Boltz is unreachable/misconfigured, methods return explicit errors without moving funds.
- Integration is fail-open at startup: `cl-hive` keeps running if Boltz init fails.

---

## Next Steps

1. Add policy-engine constraints for swap maximums (amount, frequency, and daily budget).
2. Add receipt logging table for swap requests/results.
3. Wire `hive:rebalance/v1` `swap_in`/`swap_out` actions in `cl-hive-comms` execution path to these RPCs.
