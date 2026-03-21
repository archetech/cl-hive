# cl-hive

`cl-hive` is the trusted fleet coordination layer for Core Lightning. It lets a small group of independently operated Lightning nodes share membership, fee intelligence, liquidity state, and topology recommendations so each node can make better local decisions -- without central custody or trust assumptions beyond fleet membership.

## Architecture

```text
cl-hive (coordination layer)
    |
cl-revenue-ops (local execution layer)
    |
Core Lightning
```

`cl-hive` coordinates. `cl-revenue-ops` executes. Neither moves funds on behalf of the other.

## Quick Start

### Requirements

- Core Lightning `v23.05+`
- Python `3.10+`
- `cl-revenue-ops` (recommended) for full coordination-to-execution behavior

### Install

```bash
git clone https://github.com/lightning-goats/cl-hive.git
cd cl-hive
pip install -r requirements.txt
lightningd --plugin=/path/to/cl-hive/cl-hive.py
```

For Docker deployment, see [docker/README.md](docker/README.md).

### Create a Fleet (Genesis)

```bash
lightning-cli hive-genesis "my-fleet"
```

### Add a Member

A new node joins by opening a channel to any existing member:

```bash
# On the new node: open a channel to a fleet member
lightning-cli connect <member-pubkey>@<host>:<port>
lightning-cli fundchannel <member-pubkey> 1000000
```

cl-hive sends a HELLO automatically. An existing member approves:

```bash
# On any existing member's node:
lightning-cli hive-pending
lightning-cli hive-approve <new-node-pubkey>
```

See [docs/JOINING_THE_HIVE.md](docs/JOINING_THE_HIVE.md) for the full joining guide.

## What It Does

### Observations (shared across fleet)

- Fee profiles and corridor ownership
- Peer reputation and quality scores
- Traffic profiles and demand forecasts
- Liquidity state and flow patterns
- Network metrics and health status

### Recommendations (generated locally)

- Topology planning: expansion targets, underserved nodes, close candidates
- Fee coordination: corridor fee alignment, competition avoidance
- Rebalancing: conflict-free assignment, hub identification
- Channel rationalization: sizing, value assessment, coverage analysis
- Strategic positioning: exchange coverage, network centrality

### Coordination (consensus across fleet)

- Membership management (any member can approve/remove/ban)
- Intent Lock protocol for conflict-free channel opens
- Gossip-based state synchronization with anti-entropy

## Primary RPCs

| Command | Use |
|---|---|
| `hive-status` | Current membership, fleet size, and health |
| `hive-members` | Fleet roster and member state |
| `hive-genesis` | Initialize a new fleet as the first member |
| `hive-approve <pubkey>` | Approve a pending join request |
| `hive-pending` | List pending join requests |
| `hive-topology` | View planner output and underserved targets |
| `hive-fee-recommendation` | Get coordinated fee recommendation for a channel |
| `hive-fleet-health` | Fleet-wide health summary |
| `hive-corridor-assignments` | View corridor ownership assignments |
| `hive-rebalance-recommendations` | Get EV-positive rebalance suggestions |
| `hive-export-hints` | Compact per-peer hints for local consumers |

## Integration with cl-revenue-ops

`cl-revenue-ops` polls `hive-export-hints` locally for compact per-peer coordination hints:

- **member**: whether the peer is a fleet member
- **corridor_role**: owner / secondary / contested / none
- **competition_bias**: lean-in (+1) / neutral (0) / back-off (-1)
- **peer_quality_score**: normalized 0-1
- **traffic_confidence**: normalized 0-1
- **rebalance_preference**: source / sink / neutral
- **channel_open_hint**: topology-based open/neutral/avoid advisory with size bucket and reason

`cl-hive` exports hints only. It does not set fees, trigger rebalances, or open channels. All local execution belongs to `cl-revenue-ops`.

## Configuration

Copy the sample config:

```bash
cp cl-hive.conf.sample ~/.lightning/cl-hive.conf
```

Key options:

| Option | Default | Description |
|--------|---------|-------------|
| `hive-member-fee-ppm` | `0` | Fee between fleet members |
| `hive-max-members` | `9` | Maximum fleet size |
| `hive-gossip-threshold` | `0.10` | Capacity change to trigger gossip |
| `hive-heartbeat-interval` | `300` | Heartbeat broadcast interval (seconds) |
| `hive-planner-interval` | `3600` | Topology analysis interval (seconds) |
| `hive-intent-hold-seconds` | `60` | Intent hold period for conflict detection |

## More Detail

- Joining guide: [docs/JOINING_THE_HIVE.md](docs/JOINING_THE_HIVE.md)
- Docker deployment: [docker/README.md](docker/README.md)
