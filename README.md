# cl-hive

`cl-hive` is the coordination layer for Core Lightning fleets. It lets independent nodes share membership, corridor, fee, liquidity, and reputation intelligence so each node can make better local decisions without central custody.

## What Operators Need To Know

- This is the fleet coordination layer, not the local execution engine.
- `cl-hive` shares state and recommendations across members. `cl-revenue-ops` still owns local fee execution, local rebalance execution, and Sling job control.
- The most important operator jobs are membership, governance, fleet visibility, and making sure coordination inputs are available to local execution.
- Recent integration work tightened the boundary: `cl-hive` exposes coordinated fee recommendations, competition signals, and saturated-hive egress bias signals; `cl-revenue-ops` applies them locally.

## Architecture

```text
cl-hive (coordination layer)
    ↓
cl-revenue-ops (local execution layer)
    ↓
Core Lightning
```

Optional companion plugins such as `cl-hive-comms` and `cl-hive-archon` extend transport and signing, but the core model stays the same: `cl-hive` coordinates, `cl-revenue-ops` executes.

## What It Does In A Fleet

- tracks hive membership and governance state
- shares corridor ownership, fee intelligence, and internal-competition hints
- shares routing, liquidity, peer-quality, and traffic-intelligence signals
- coordinates topology planning, settlement, and fleet health
- exposes read and control surfaces for MCP- or advisor-driven operation

## Install

### Requirements

- Core Lightning `v23.05+`
- Python `3.10+`
- `cl-revenue-ops`: recommended for full coordination-to-execution behavior

Optional:

- `cl-hive-comms` for Nostr transport
- `cl-hive-archon` for delegated signing and governance extensions

### Start The Plugin

```bash
git clone https://github.com/lightning-goats/cl-hive.git
cd cl-hive
pip install -r requirements.txt
lightningd --plugin=/path/to/cl-hive/cl-hive.py
```

If you are deploying from the provided container stack, start with [production.example/README.md](production.example/README.md).

## First Operator Steps

1. Start the plugin and check `hive-status`.
2. If you are founding a new fleet, use `hive-genesis`.
3. If you are joining an existing fleet, follow [docs/JOINING_THE_HIVE.md](docs/JOINING_THE_HIVE.md).
4. Confirm membership and peer visibility with `hive-members`.
5. If you also run `cl-revenue-ops`, confirm local coordination inputs from that side with `revenue-hive-status`.

## Primary RPCs

| Command | Use |
|---|---|
| `hive-status` | Current membership, fleet size, and governance mode |
| `hive-members` | Fleet roster and current member state |
| `hive-genesis` | Initialize a new hive as the first member |
| `hive-invite` | Create an invite ticket for a new participant |
| `hive-join <ticket>` | Join an existing hive |
| `hive-topology` | View planner output and underserved targets |
| `hive-pending-actions` | Review actions waiting for approval |
| `hive-pending-bans` | Review active ban proposals |
| `hive-mcf-status` | Inspect fleet rebalance optimization state |
| `hive-phase6-plugins` | Inspect optional companion plugin status |

## Works With `cl-revenue-ops`

Current integration points include:

- coordinated corridor fee recommendations
- corridor ownership and internal-competition signals
- peer reputation and defense intelligence
- traffic and liquidity intelligence
- saturated-hive egress bias signals for desaturating locally full hive exits

`cl-hive` does not directly own Sling. If a route, rebalance, or local fee change needs to be executed, that work belongs in `cl-revenue-ops`.

## More Detail

- Docs index: [docs/README.md](docs/README.md)
- Joining guide: [docs/JOINING_THE_HIVE.md](docs/JOINING_THE_HIVE.md)
- MCP/advisor surface: [docs/MCP_SERVER.md](docs/MCP_SERVER.md)
- Production example stack: [production.example/README.md](production.example/README.md)
