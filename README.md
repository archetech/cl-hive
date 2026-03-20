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

### Invite a Member

```bash
lightning-cli hive-invite 24   # 24 hour validity
```

### Join an Existing Fleet

```bash
lightning-cli hive-join "HIVE1-INVITE-..."
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

- Membership management (admin adds/removes members)
- Ban proposals with distributed voting
- Intent Lock protocol for conflict-free channel opens
- Gossip-based state synchronization with anti-entropy

## Primary RPCs

| Command | Use |
|---|---|
| `hive-status` | Current membership, fleet size, and health |
| `hive-members` | Fleet roster and member state |
| `hive-genesis` | Initialize a new fleet as the first member |
| `hive-invite` | Create an invite ticket for a new member |
| `hive-join <ticket>` | Join an existing fleet |
| `hive-topology` | View planner output and underserved targets |
| `hive-fee-recommendation` | Get coordinated fee recommendation for a channel |
| `hive-fleet-health` | Fleet-wide health summary |
| `hive-corridor-assignments` | View corridor ownership assignments |
| `hive-rebalance-recommendations` | Get EV-positive rebalance suggestions |

## Integration with cl-revenue-ops

Current integration points (via the bridge module):

- Coordinated corridor fee recommendations
- Corridor ownership and competition-avoidance signals
- Peer reputation and defense intelligence
- Traffic and liquidity intelligence
- Egress desaturation bias for locally-full hive exits

`cl-hive` does not directly own Sling or fee execution. If a route, rebalance, or local fee change needs to happen, that work belongs in `cl-revenue-ops`.

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
