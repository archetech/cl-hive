# Joining the Hive -- Quick Start Guide

This guide covers how to join an existing cl-hive fleet.

## Prerequisites

- Core Lightning `v23.05+` running and synced
- On-chain funds for opening a channel (skin in the game)
- Contact with an existing fleet admin who can invite you

## Membership Model

cl-hive uses a simple two-tier membership model:

| Tier | How you get it | What you can do |
|------|----------------|-----------------|
| **Admin** | Genesis creator, or promoted by existing admin | Add/remove members, invite, ban, full control |
| **Member** | Invited by admin, joined with ticket | Route, share intelligence, participate in fleet |

There is no promotion pipeline or vouching process. An admin adds you and you are in.

## Step 1: Get an Invite Ticket

Contact a fleet admin. They generate an invite ticket:

```bash
# Admin runs on their node:
lightning-cli hive-invite 24   # Valid for 24 hours
```

The admin shares the resulting ticket string with you.

**Current Fleet Members:**

| Node | Connection String |
|------|-------------------|
| Lightning Goats CLN (nexus-01) | `0382d558331b9a0c1d141f56b71094646ad6111e34e197d47385205019b03afdc3@45.76.234.192:9735` |

**Tor (onion) address:**
- nexus-01: `xsp4whqtphjnby335a3ihtje55gidhf4pnv3blrgustplyxfnpsgeuyd.onion:9735`

**To request an invite ticket:**
- Nostr: `hex@lightning-goats.com` (npub1qkjnsgk6zrszkmk2c7ywycvh46ylp3kw4kud8y8a20m93y5synvqewl0sq)
- GitHub: Open an issue at https://github.com/lightning-goats/cl-hive/issues

## Step 2: Connect and Open Channel

**Skin in the game**: You open a channel to a fleet member first, demonstrating commitment.

```bash
# Connect to a fleet member
lightning-cli connect <member-pubkey>@<host>:<port>

# Open a channel (recommended: 1M+ sats)
lightning-cli fundchannel <member-pubkey> 1000000
```

Wait for the channel to confirm (3+ confirmations).

## Step 3: Join the Fleet

Use the invite ticket to register:

```bash
lightning-cli hive-join <ticket>
```

You are now a fleet member. No further promotion or vouching steps needed.

## Step 4: Verify Membership

```bash
lightning-cli hive-status
lightning-cli hive-members
```

You should see yourself listed as a `member`.

## Useful Commands

| Command | Description |
|---------|-------------|
| `hive-status` | View fleet membership and health |
| `hive-members` | List all fleet members |
| `hive-fee-recommendation` | Get coordinated fee recommendation |
| `hive-fleet-health` | Fleet-wide health summary |
| `hive-topology` | View planner topology analysis |
| `hive-corridor-assignments` | View corridor ownership |
| `hive-gossip-stats` | Gossip protocol statistics |

## Updating Your Node

### Hot Update (Recommended)

```bash
# If using Docker
cd cl-hive/docker/scripts
./hot-upgrade.sh --check    # Check for updates
./hot-upgrade.sh            # Apply update
```

### Manual Hot Update

```bash
# Pull changes
cd /path/to/cl-hive && git pull

# Reload the plugin
lightning-cli plugin stop /path/to/cl-hive/cl-hive.py
lightning-cli plugin start /path/to/cl-hive/cl-hive.py
```

### Data Persistence

If using Docker, Lightning data is stored in Docker volumes and persists across updates:
- `/data/lightning` -- Channel database, keys, and state

These volumes are NOT deleted by `docker-compose down`. Only `docker-compose down -v` removes volumes.

## Troubleshooting

### Node not connecting to peers
```bash
lightning-cli listpeers
```
Ensure your firewall allows inbound connections on port 9735.

### Not receiving gossip
Check that you are connected to at least one fleet member:
```bash
lightning-cli hive-members
```

## Security Notes

- Keep your `hsm_secret` backed up securely
- Fleet channels between members always use 0 fees
- All membership actions require cryptographic signatures via CLN HSM

## Getting Help

- GitHub Issues: https://github.com/lightning-goats/cl-hive/issues
- Check logs: `lightning-cli log` or `docker logs cl-hive-node`
