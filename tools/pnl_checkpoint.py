#!/usr/bin/env python3
import json
import os
import ssl
import subprocess
import time
from datetime import datetime, timedelta
from urllib.request import Request, urlopen

STATE_PATH = os.path.expanduser("~/clawd/memory/pnl-streak.json")


def sh(cmd: list) -> str:
    """Run a command with argv list (no shell interpretation)."""
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"cmd failed: {cmd[0]}\n{p.stderr.strip()}")
    return p.stdout.strip()


def mcp(tool: str, **kwargs):
    args = " ".join([f"{k}={v}" for k, v in kwargs.items()])
    p = subprocess.run(
        ["mcporter", "call", f"hive.{tool}"] + args.split(),
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        raise RuntimeError(f"mcporter failed: {p.stderr.strip()}")
    return json.loads(p.stdout.strip())


def load_state():
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {"streak_days": 0, "last_date": None}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def now_ts() -> int:
    return int(time.time())


def ts_24h_ago() -> int:
    return now_ts() - 24 * 3600


def msat_to_sats_floor(msat: int) -> int:
    return int(msat) // 1000


def msat_to_sats_ceil(msat: int) -> int:
    msat = int(msat)
    return (msat + 999) // 1000


def rest_post(url: str, rune: str, payload: dict) -> dict:
    """POST to CLN REST API. Rune never touches shell or process argv."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    data = json.dumps(payload).encode()
    req = Request(url, data=data, method="POST")
    req.add_header("Rune", rune)
    req.add_header("Content-Type", "application/json")
    with urlopen(req, context=ctx, timeout=30) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else {}


def listforwards_last24h_n2() -> dict:
    return json.loads(
        sh([
            "/snap/bin/docker", "exec", "be6a3d32b6a6",
            "lightning-cli", "--rpc-file=/data/lightning/bitcoin/bitcoin/lightning-rpc",
            "listforwards",
        ])
    )


def listforwards_last24h_n1(rune: str) -> dict:
    return rest_post("https://10.8.0.1:3010/v1/listforwards", rune, {})


def forwards_pnl_from_listforwards(obj: dict) -> dict:
    since = ts_24h_ago()
    forwards = obj.get("forwards", []) if isinstance(obj, dict) else []
    fee_msat = 0
    vol_msat = 0
    cnt = 0
    for f in forwards:
        try:
            if f.get("status") != "settled":
                continue
            rt = f.get("resolved_time")
            if rt is None:
                continue
            # resolved_time can be float
            if float(rt) < since:
                continue
            fee_msat += int(f.get("fee_msat") or 0)
            vol_msat += int(f.get("out_msat") or 0)
            cnt += 1
        except Exception:
            continue

    return {
        "routing_fee_sats": msat_to_sats_floor(fee_msat),
        "forward_count": cnt,
        "volume_routed_sats": msat_to_sats_floor(vol_msat),
    }


def sling_stats_n2() -> list:
    # list-style output when called with json=true and no scid
    return json.loads(
        sh([
            "/snap/bin/docker", "exec", "be6a3d32b6a6",
            "lightning-cli", "--rpc-file=/data/lightning/bitcoin/bitcoin/lightning-rpc",
            "sling-stats", "json=true",
        ])
    )


def sling_stats_n1(rune: str) -> list:
    return rest_post("https://10.8.0.1:3010/v1/sling-stats", rune, {"json": True})


def sling_spent_total_for_active_jobs(stats_list: list, get_one_fn) -> int:
    # Sum total_spent_sats for jobs that are currently in a rebalancing state.
    # Requires per-scid sling-stats to retrieve successes.total_spent_sats.
    scids = []
    for row in stats_list or []:
        try:
            st = row.get("status")
            if isinstance(st, list):
                st = " ".join(st)
            st = str(st or "")
            if "Rebalancing" not in st:
                continue
            scid = row.get("scid")
            if scid:
                scids.append(scid)
        except Exception:
            continue

    total = 0
    for scid in scids:
        try:
            one = get_one_fn(scid)
            suc = one.get("successes_in_time_window") if isinstance(one, dict) else None
            if isinstance(suc, dict):
                total += int(suc.get("total_spent_sats") or 0)
        except Exception:
            continue
    return total


def sling_stats_one_n2(scid: str) -> dict:
    return json.loads(
        sh([
            "/snap/bin/docker", "exec", "be6a3d32b6a6",
            "lightning-cli", "--rpc-file=/data/lightning/bitcoin/bitcoin/lightning-rpc",
            "sling-stats", f"scid={scid}", "json=true",
        ])
    )


def sling_stats_one_n1(rune: str, scid: str) -> dict:
    return rest_post("https://10.8.0.1:3010/v1/sling-stats", rune, {"scid": scid, "json": True})


def main():
    now = datetime.now()
    date_key = now.strftime("%Y-%m-%d")

    # Load runes from the production nodes file (avoid printing secrets)
    nodes_cfg = json.loads(open(os.path.expanduser("~/bin/cl-hive/production/nodes.production.json")).read())
    rune_n1 = None
    rune_n2 = None
    for n in nodes_cfg.get("nodes", []):
        if n.get("name") == "hive-nexus-01":
            rune_n1 = n.get("rune")
        if n.get("name") == "hive-nexus-02":
            rune_n2 = n.get("rune")

    # Ground truth: routing fees from listforwards (last 24h)
    n1_fwd = forwards_pnl_from_listforwards(listforwards_last24h_n1(rune_n1))
    n2_fwd = forwards_pnl_from_listforwards(listforwards_last24h_n2())

    # Ground truth-ish: rebalance spend from sling stats deltas (persistent jobs)
    state = load_state()
    spent_prev = state.get("sling_spent_totals", {})

    n1_list = sling_stats_n1(rune_n1)
    n2_list = sling_stats_n2()

    n1_total = sling_spent_total_for_active_jobs(n1_list, lambda scid: sling_stats_one_n1(rune_n1, scid))
    n2_total = sling_spent_total_for_active_jobs(n2_list, sling_stats_one_n2)

    n1_spent = max(0, int(n1_total) - int(spent_prev.get("n1", 0) or 0))
    n2_spent = max(0, int(n2_total) - int(spent_prev.get("n2", 0) or 0))

    # update spend totals for next checkpoint
    state["sling_spent_totals"] = {"n1": n1_total, "n2": n2_total}

    n1 = {
        "revenue_sats": n1_fwd["routing_fee_sats"],
        "rebalance_cost_sats": n1_spent,
        "net_sats": n1_fwd["routing_fee_sats"] - n1_spent,
        "forward_count": n1_fwd["forward_count"],
        "volume_routed_sats": n1_fwd["volume_routed_sats"],
    }
    n2 = {
        "revenue_sats": n2_fwd["routing_fee_sats"],
        "rebalance_cost_sats": n2_spent,
        "net_sats": n2_fwd["routing_fee_sats"] - n2_spent,
        "forward_count": n2_fwd["forward_count"],
        "volume_routed_sats": n2_fwd["volume_routed_sats"],
    }

    fleet = {
        "revenue_sats": n1["revenue_sats"] + n2["revenue_sats"],
        "rebalance_cost_sats": n1["rebalance_cost_sats"] + n2["rebalance_cost_sats"],
        "net_sats": n1["net_sats"] + n2["net_sats"],
        "forward_count": n1["forward_count"] + n2["forward_count"],
        "volume_routed_sats": n1["volume_routed_sats"] + n2["volume_routed_sats"],
    }

    # streak logic: require net > 7000 for the date; only increment once per date
    last_date = state.get("last_date")
    streak = int(state.get("streak_days") or 0)

    if last_date != date_key:
        if fleet["net_sats"] > 7000:
            try:
                if last_date:
                    ld = datetime.strptime(last_date, "%Y-%m-%d")
                    if (now.date() - ld.date()).days == 1:
                        streak += 1
                    else:
                        streak = 1
                else:
                    streak = 1
            except Exception:
                streak = 1
        else:
            streak = 0

        state["last_date"] = date_key
        state["streak_days"] = streak

    save_state(state)

    lines = []
    lines.append(f"P&L checkpoint ({now.strftime('%a %Y-%m-%d %H:%M %Z')}):")
    lines.append("Ground truth: routing fees from listforwards (settled, last 24h)")
    lines.append("Rebalance spend: sling-stats total_spent_sats delta for active Rebalancing jobs since last checkpoint")
    lines.append(f"- nexus-01: revenue={n1['revenue_sats']}  reb_cost={n1['rebalance_cost_sats']}  net={n1['net_sats']}  forwards={n1['forward_count']}  vol={n1['volume_routed_sats']}")
    lines.append(f"- nexus-02: revenue={n2['revenue_sats']}  reb_cost={n2['rebalance_cost_sats']}  net={n2['net_sats']}  forwards={n2['forward_count']}  vol={n2['volume_routed_sats']}")
    lines.append(f"- FLEET : revenue={fleet['revenue_sats']}  reb_cost={fleet['rebalance_cost_sats']}  net={fleet['net_sats']}  forwards={fleet['forward_count']}  vol={fleet['volume_routed_sats']}")
    lines.append(f"- streak(net>7000): {streak} day(s)  (2=sane, 3=better, 5=perfect)")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
