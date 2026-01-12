# Running Genesis in Production

This guide covers initializing a new Hive fleet in production.

## Prerequisites

### 1. Core Lightning v25+

```bash
lightningd --version
# Should be v25.02 or later
```

### 2. cl-revenue-ops Plugin (v1.4.0+)

```bash
lightning-cli revenue-status
# Should show version >= 1.4.0
```

### 3. cl-hive Plugin Installed

```bash
lightning-cli plugin list | grep cl-hive
# Should show cl-hive.py as active
```

### 4. Configuration

Copy the sample config to your lightning directory:

```bash
cp cl-hive.conf.sample ~/.lightning/cl-hive.conf
```

Add to your main config:

```bash
echo "include /path/to/cl-hive.conf" >> ~/.lightning/config
```

Or add options directly to `~/.lightning/config`.

## Configuration Options

Review and adjust these settings before genesis:

| Option | Default | Description |
|--------|---------|-------------|
| `hive-governance-mode` | `advisor` | `advisor` (recommended), `autonomous`, or `oracle` |
| `hive-member-fee-ppm` | `0` | Fee for routing between full members |
| `hive-max-members` | `9` | Maximum hive size (Dunbar cap) |
| `hive-market-share-cap` | `0.10` | Anti-monopoly cap (10%) |
| `hive-probation-days` | `30` | Days as neophyte before promotion |
| `hive-vouch-threshold` | `0.51` | Vouch percentage for promotion |
| `hive-planner-enable-expansions` | `false` | Enable auto channel proposals |

**Important**: Start with `hive-governance-mode=advisor` to review all actions before execution.

## Running Genesis

### Step 1: Verify Plugin Status

```bash
lightning-cli hive-status
```

Expected output:
```json
{
   "status": "genesis_required",
   "governance_mode": "advisor",
   ...
}
```

### Step 2: Run Genesis

```bash
lightning-cli hive-genesis
```

Or with a custom hive ID:

```bash
lightning-cli hive-genesis "my-fleet-2026"
```

Expected output:
```json
{
   "status": "genesis_complete",
   "hive_id": "hive-abc123...",
   "admin_pubkey": "03abc123...",
   "genesis_ticket": "HIVE1-ADMIN-...",
   "message": "Hive created. You are the founding admin."
}
```

### Step 3: Verify Genesis

```bash
lightning-cli hive-status
```

Expected output:
```json
{
   "status": "active",
   "governance_mode": "advisor",
   "members": {
      "total": 1,
      "admin": 1,
      "member": 0,
      "neophyte": 0
   },
   ...
}
```

### Step 4: Check Bridge Status

```bash
lightning-cli hive-status
```

Verify the bridge to cl-revenue-ops is enabled. If it shows disabled:

```bash
lightning-cli hive-reinit-bridge
```

## Inviting Members

### Generate Invite Ticket

For a neophyte (probationary member):
```bash
lightning-cli hive-invite
```

For a bootstrap admin (only works once, creates 2nd admin):
```bash
lightning-cli hive-invite 24 0 admin
```

Output:
```json
{
   "ticket": "HIVE1-INVITE-...",
   "expires_at": "2026-01-13T15:00:00Z",
   "tier": "neophyte",
   "valid_hours": 24
}
```

### Share Ticket Securely

Share the ticket with the joining node operator via a secure channel (Signal, encrypted email, etc.).

### Joining Node

On the joining node:
```bash
lightning-cli hive-join "HIVE1-INVITE-..."
```

## Post-Genesis Checklist

- [ ] Verify `hive-status` shows `status: active`
- [ ] Verify bridge is enabled (`hive-reinit-bridge` if needed)
- [ ] Generate invite for second admin (bootstrap)
- [ ] Second admin joins and verifies membership
- [ ] Test gossip between nodes (check `hive-topology`)
- [ ] Review `hive-pending-actions` periodically (advisor mode)

## Monitoring

### Check Hive Health

```bash
# Member list and stats
lightning-cli hive-members

# Topology and coordination
lightning-cli hive-topology

# Pending governance actions (advisor mode)
lightning-cli hive-pending-actions
```

### Logs

Monitor plugin logs for issues:

```bash
# CLN logs
tail -f ~/.lightning/bitcoin/log | grep cl-hive

# Or with journalctl
journalctl -u lightningd -f | grep cl-hive
```

## Troubleshooting

### Bridge Disabled at Startup

If you see:
```
UNUSUAL plugin-cl-hive.py: [Bridge] Bridge disabled: cl-revenue-ops not available
```

This is a startup race condition. Fix with:
```bash
lightning-cli hive-reinit-bridge
```

### Genesis Already Complete

If you see:
```json
{"error": "Hive already initialized"}
```

Genesis can only run once. Check current status:
```bash
lightning-cli hive-status
lightning-cli hive-members
```

### Plugin Not Found

If cl-hive commands fail:
```bash
# Check plugin is loaded
lightning-cli plugin list | grep cl-hive

# Restart plugin
lightning-cli plugin stop cl-hive.py
lightning-cli plugin start /path/to/cl-hive.py
```

### Version Mismatch

Ensure all hive members run compatible versions:
```bash
lightning-cli hive-status | jq .version
```

## Security Considerations

1. **Protect invite tickets** - They grant membership access
2. **Use advisor mode initially** - Review all automated decisions
3. **Backup the database** - Located at `~/.lightning/cl_hive.db`
4. **Secure admin nodes** - Admin nodes control governance
5. **Monitor for leeches** - Check contribution ratios regularly

## Next Steps

After genesis and initial member setup:

1. **Configure CLBOSS integration** (if using CLBOSS)
2. **Enable expansion proposals** when ready: `lightning-cli hive-enable-expansions true`
3. **Set up AI advisor** for automated governance (see `tools/ai_advisor.py`)
4. **Review and approve** pending actions regularly
