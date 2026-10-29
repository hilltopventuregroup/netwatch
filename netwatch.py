#!/usr/bin/env python3
"""
netwatch — a small, dependency-free home network health collector.

Every run it ICMP-pings a list of network points, HTTP-probes a list of
services, appends one sample to a rolling in-memory-derived history file, and
(optionally) mirrors the numbers into a Prometheus pushgateway. A static page
reads the JSON and draws it.

Design rules, in priority order:

  * stdlib only. No pip installs, no virtualenv, no imports that can go missing
    six months from now.
  * Every external call is time-boxed. One dead target must never hang the run.
  * Silent on success. Empty stdout = nothing happened worth telling a human.
    That makes it safe to run from cron, systemd, or a chat-agent scheduler.
  * Speaks up only on a *change* of state, after a debounce, so a 5-minute
    cadence cannot spam you with every WiFi blip.

Usage:
    ./netwatch.py                        # run once (this is what cron calls)
    ./netwatch.py --dry-run              # measure, print a table, write nothing
    ./netwatch.py --config /path.json    # explicit config
    ./netwatch.py --print-example        # dump a starter config
    ./netwatch.py --version

Exit codes: 0 always on a normal run (including "everything is down" — the
scheduler should not flap on that), 2 on a config error.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

VERSION = "1.0.0"

DEFAULTS = {
    "host": "netwatch",
    "page_url": "",
    "lan_prefix": "192.168.1",
    "out_dir": "~/.local/share/netwatch",
    "summary_path": "",
    "pushgateway": "",
    "history_len": 288,
    "ping": {"count": 10, "interval": 0.2, "timeout": 1.0},
    "speedtest": {
        "enabled": False,
        "interval_s": 1800,
        "bytes": 25000000,
        "url": "https://speed.cloudflare.com/__down?bytes={bytes}",
    },
    "alerting": {
        "degraded_loss_pct": 5,
        "debounce_samples": 2,
        "blip_suppress_s": 600,
    },
    "targets": [],
    "services": [],
}

EXAMPLE_CONFIG = {
    "host": "netwatch-host",
    "page_url": "http://netwatch.lan/netwatch/",
    "lan_prefix": "192.168.1",
    "out_dir": "~/netwatch/data",
    "summary_path": "",
    "pushgateway": "http://127.0.0.1:9091/metrics/job/netwatch",
    "history_len": 288,
    "ping": {"count": 10, "interval": 0.2, "timeout": 1.0},
    "speedtest": {"enabled": False, "interval_s": 1800, "bytes": 25000000},
    "alerting": {"degraded_loss_pct": 5, "debounce_samples": 2, "blip_suppress_s": 600},
    "targets": [
        {"id": "router", "name": "Router", "ip": "192.168.1.1", "kind": "wired",
         "alert_loss_pct": 30,
         "note": "raise alert_loss_pct if your router deprioritises ICMP to itself"},
        {"id": "nas", "name": "NAS", "ip": "192.168.1.10", "kind": "wired",
         "note": "hardwired"},
        {"id": "pc", "name": "Desktop", "ip": "192.168.1.50", "kind": "wired",
         "mac": "aa:bb:cc:dd:ee:ff",
         "note": "mac is optional — it survives DHCP drift (see README)"},
        {"id": "ap1", "name": "AP upstairs", "ip": "192.168.1.97", "kind": "ap",
         "note": "wired backhaul"},
        {"id": "laptop", "name": "Laptop", "ip": "192.168.1.120", "kind": "wifi",
         "note": "5 GHz"},
        {"id": "wan", "name": "Internet", "ip": "1.1.1.1", "kind": "wan",
         "note": "ICMP to Cloudflare"},
    ],
    "services": [
        {"id": "router-ui", "name": "Router admin", "url": "http://192.168.1.1/"},
        {"id": "ha", "name": "Home Assistant", "url": "http://192.168.1.10:8123/"},
    ],
}

RANK = {"up": 0, "degraded": 1, "down": 2}
DEAD = {"loss": 100.0, "min": None, "avg": None, "max": None, "mdev": None}


# ---------------------------------------------------------------- config ----


def merge(base, override):
    """Shallow-ish recursive merge: nested dicts merge, everything else wins."""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def find_config(explicit):
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    env = os.environ.get("NETWATCH_CONFIG")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "netwatch.json"),
                 os.path.expanduser("~/.config/netwatch/config.json")):
        if os.path.isfile(cand):
            return cand
    return None


def load_config(path, args):
    cfg = json.loads(json.dumps(DEFAULTS))     # deep copy
    src = find_config(path)
    if src:
        try:
            with open(src) as f:
                user = json.load(f)
        except Exception as e:
            sys.stderr.write(f"netwatch: cannot read config {src}: {e}\n")
            raise SystemExit(2)
        cfg = merge(cfg, user)
    else:
        sys.stderr.write(
            "netwatch: no config found. Looked for ./netwatch.json and "
            "~/.config/netwatch/config.json — run --print-example to get a starter.\n")
        raise SystemExit(2)

    for t in cfg["targets"]:
        for req in ("id", "ip"):
            if req not in t:
                sys.stderr.write(f"netwatch: target {t} missing '{req}'\n")
                raise SystemExit(2)
        t.setdefault("name", t["id"])
        t.setdefault("kind", "other")
        t.setdefault("note", "")

    for s in cfg["services"]:
        for req in ("id", "url"):
            if req not in s:
                sys.stderr.write(f"netwatch: service {s} missing '{req}'\n")
                raise SystemExit(2)
        s.setdefault("name", s["id"])

    if args.out_dir:
        cfg["out_dir"] = args.out_dir
    if args.pushgateway:
        cfg["pushgateway"] = args.pushgateway
    if args.no_pushgateway:
        cfg["pushgateway"] = ""
    if args.no_speedtest:
        cfg["speedtest"]["enabled"] = False

    cfg["out_dir"] = os.path.abspath(os.path.expanduser(cfg["out_dir"]))
    if cfg.get("summary_path"):
        cfg["summary_path"] = os.path.abspath(os.path.expanduser(cfg["summary_path"]))
    cfg["_config_path"] = src or "(defaults)"
    return cfg


# ------------------------------------------------------------------ utils ---


def run(cmd, timeout):
    """Run a command, return stdout as text, never raise."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout or ""
    except Exception:
        return ""


def ping_cmd(ip, p):
    count, interval, timeout = str(p["count"]), str(p["interval"]), p["timeout"]
    if sys.platform == "darwin":
        # BSD ping: -W is a deadline in milliseconds, not seconds.
        return ["ping", "-c", count, "-i", interval, "-W", str(int(timeout * 1000)), ip]
    return ["ping", "-c", count, "-i", interval, "-W", str(int(max(1, timeout))), ip]


def ping(ip, p):
    """ICMP a host. Returns loss% + min/avg/max/mdev ms. Never raises."""
    out = run(ping_cmd(ip, p), timeout=p["count"] * (p["interval"] + 1) + 5)
    loss = None
    m = re.search(r"(\d+(?:\.\d+)?)% packet loss", out)
    if m:
        loss = float(m.group(1))
    stats = {"min": None, "avg": None, "max": None, "mdev": None}
    r = re.search(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms", out)
    if r:
        stats = {"min": float(r.group(1)), "avg": float(r.group(2)),
                 "max": float(r.group(3)), "mdev": float(r.group(4))}
    if loss is None:
        loss = 100.0 if stats["avg"] is None else 0.0
    return {"loss": loss, **stats}


def arp_mac(ip):
    """MAC the kernel has cached for `ip`, or None. Linux `ip neigh`, macOS `arp`."""
    if sys.platform == "darwin":
        out = run(["arp", "-n", ip], 5)
        m = re.search(r"at ([0-9a-f:]{17})", out, re.I)
    else:
        out = run(["ip", "neigh", "show", ip], 5)
        m = re.search(r"lladdr ([0-9a-f:]{17})", out, re.I)
    return m.group(1).lower() if m else None


def find_ip_by_mac(mac, prefix):
    """Sweep the /24 and return the IP that currently owns `mac` (None if gone).

    Why this exists: consumer routers often cannot hold DHCP reservations, so a
    device that reboots comes back on a different address. A ping-only check
    then reports "your server is down" (nothing at the old IP) or, worse, "your
    server is up" because some *other* device now answers at that address. When
    a target declares its MAC, a silent address or a wrong-MAC answer triggers
    this sweep and the reading follows the device instead of the address.
    """
    want = (mac or "").lower()
    if not want or not prefix:
        return None
    ips = [f"{prefix}.{i}" for i in range(1, 255)]
    procs = []
    for ip in ips:
        try:
            procs.append(subprocess.Popen(
                ["ping", "-c", "1", "-W", "1000" if sys.platform == "darwin" else "1", ip],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        except Exception:
            pass
    for pr in procs:
        try:
            pr.wait(timeout=3)
        except Exception:
            pr.kill()
    for ip in ips:
        if arp_mac(ip) == want:
            return ip
    return None


def http_check(url, timeout=8):
    """GET a URL, report code + how long the response took. Never raises."""
    req = urllib.request.Request(url, headers={"User-Agent": f"netwatch/{VERSION}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(2048)
            return {"up": 200 <= r.status < 400, "code": r.status,
                    "ms": round((time.time() - t0) * 1000, 1)}
    except urllib.error.HTTPError as e:
        # A 401/403 still proves something is listening and speaking HTTP.
        return {"up": e.code < 500, "code": e.code,
                "ms": round((time.time() - t0) * 1000, 1)}
    except Exception as e:
        return {"up": False, "code": None,
                "ms": round((time.time() - t0) * 1000, 1), "err": type(e).__name__}


def wan_speed(st):
    """Download a fixed chunk and time it. Returns Mbps + TTFB, or None.

    Uses urllib rather than curl so the script has no external binary
    dependencies beyond `ping` itself.
    """
    url = (st.get("url") or DEFAULTS["speedtest"]["url"]).format(
        bytes=int(st.get("bytes", 25000000)))
    req = urllib.request.Request(url, headers={"User-Agent": f"netwatch/{VERSION}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            first = None
            total = 0
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                if first is None:
                    first = time.time() - t0
                total += len(chunk)
        elapsed = time.time() - t0
        if elapsed <= 0 or total <= 0:
            return None
        return {"mbps": round(total * 8 / elapsed / 1e6, 1),
                "ttfb_ms": round(first * 1000, 1) if first else None}
    except Exception:
        return None


def push_gateway(url, lines):
    """Best-effort push into a Prometheus pushgateway. Silent if it is down."""
    if not url:
        return False
    body = ("\n".join(lines) + "\n").encode()
    try:
        req = urllib.request.Request(url, data=body, method="POST")
        urllib.request.urlopen(req, timeout=6).read()
        return True
    except Exception:
        return False


def alert_level(target, r, floor):
    """Severity used for *alerting*, which can differ from the page's status.

    Many consumer routers deprioritise ICMP echo replies addressed to their own
    management plane when the CPU is busy, so 10-30% self-ping loss is noise. A
    target may carry `alert_loss_pct` to raise its own bar for the degraded
    band; a clean 100% loss still alerts for every target.
    """
    if r["status"] == "down":
        return "down"
    if r["status"] == "up":
        return "up"
    return "degraded" if r["loss"] > target.get("alert_loss_pct", floor) else "up"


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


def esc(label):
    return str(label).replace("\\", "\\\\").replace('"', '\\"')


def write_summary(path, doc):
    """Optional compact feed for a dashboard tile that only serves JSON.

    Never fatal: if the path is missing or unwritable we just skip it.
    """
    if not path:
        return None
    try:
        results = doc["targets"]
        services = doc["services"]
        up = [r for r in results if r["status"] == "up"]
        down = [r["name"] for r in results if r["status"] == "down"]
        slow = [r["name"] for r in results if r["status"] == "degraded"]
        svc_up = sum(1 for s in services if s["up"])
        svc_down = [s["name"] for s in services if not s["up"]]
        wan = doc.get("wan") or {}
        mbps = wan.get("mbps")

        points = f"{len(up)}/{len(results)} up"
        if down:
            points += f" · {len(down)} down"
        elif slow:
            points += f" · {len(slow)} slow"

        summary = {
            "updated": doc["updated"],
            "updated_epoch": doc["updated_epoch"],
            "points": points,
            "services": f"{svc_up}/{len(services)} up",
            "wan": f"{mbps:.0f} Mbps" if mbps else "—",
            "wan_stale": bool(wan.get("stale")),
            "issues": ", ".join(down + svc_down) or "none",
            "down": down,
            "services_down": svc_down,
        }
        write_json(path, summary)
        os.chmod(path, 0o644)
        return summary
    except Exception:
        return None


def human_table(doc):
    """--dry-run output: something a human can read in a terminal."""
    lines = [f"netwatch {VERSION} — {doc['host']} — {doc['updated']}", ""]
    lines.append(f"  {'POINT':<22}{'IP':<17}{'STATUS':<10}{'LOSS':>6}{'AVG':>9}{'MAX':>9}")
    for r in doc["targets"]:
        avg = f"{r['avg']:.1f}" if r["avg"] is not None else "—"
        mx = f"{r['max']:.1f}" if r["max"] is not None else "—"
        lines.append(f"  {r['name'][:21]:<22}{r['ip']:<17}{r['status']:<10}"
                     f"{r['loss']:>5.0f}%{avg:>9}{mx:>9}")
    if doc["services"]:
        lines.append("")
        for s in doc["services"]:
            state = "up" if s["up"] else "DOWN"
            lines.append(f"  {s['name'][:21]:<22}{str(s['code'] or '—'):<17}{state:<10}"
                         f"{s['ms']:>5.0f} ms")
    wan = doc.get("wan") or {}
    if wan.get("mbps"):
        lines.append(f"\n  WAN: {wan['mbps']:.1f} Mbps · ttfb {wan.get('ttfb_ms') or '—'} ms")
    return "\n".join(lines)


# ------------------------------------------------------------------- main ---


def measure(cfg):
    """Collect one sample. Returns the page document (nothing is written)."""
    now = int(time.time())
    iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out_dir = cfg["out_dir"]
    data_path = os.path.join(out_dir, "data.json")
    state_path = os.path.join(out_dir, "state.json")

    prev = load_json(state_path, {})
    prev_data = load_json(data_path, {})

    prev_spark = {t["id"]: t.get("spark", []) for t in prev_data.get("targets", [])}
    wan_hist = (prev_data.get("wan") or {}).get("history", [])
    resolved = prev.get("resolved", {})
    history_len = int(cfg.get("history_len", 288))
    floor = cfg["alerting"]["degraded_loss_pct"]

    results, push_lines = [], []
    for t in cfg["targets"]:
        want_mac = (t.get("mac") or "").lower()
        ip = resolved.get(t["id"]) or t["ip"]
        p = ping(ip, cfg["ping"])

        if want_mac:
            seen = arp_mac(ip) if p["loss"] < 100 else None
            if p["loss"] >= 100 or (seen and seen != want_mac):
                found = find_ip_by_mac(want_mac, cfg.get("lan_prefix"))
                if found:
                    ip = found
                    p = ping(ip, cfg["ping"])
                    if found == t["ip"]:
                        resolved.pop(t["id"], None)
                    else:
                        resolved[t["id"]] = found
                elif p["loss"] < 100:
                    p = dict(DEAD)     # the address answers, but it isn't our device

        avg = p["avg"]
        alive = avg is not None and p["loss"] < 100
        status = "up" if (alive and p["loss"] <= floor) else ("degraded" if alive else "down")

        spark = prev_spark.get(t["id"], [])
        spark.append([now, avg, p["loss"]])
        spark = spark[-history_len:]

        results.append({
            "id": t["id"], "name": t["name"], "ip": ip, "kind": t["kind"],
            "note": t["note"], "status": status,
            "loss": p["loss"], "min": p["min"], "avg": avg, "max": p["max"],
            "jitter": p["mdev"], "spark": spark,
        })

        push_lines.append(f'netwatch_latency_avg_ms{{target="{esc(t["id"])}",ip="{esc(ip)}"}} '
                          f'{avg if avg is not None else 0}')
        push_lines.append(f'netwatch_loss_pct{{target="{esc(t["id"])}",ip="{esc(ip)}"}} {p["loss"]}')
        push_lines.append(f'netwatch_up{{target="{esc(t["id"])}",ip="{esc(ip)}"}} '
                          f'{1 if status == "up" else 0}')
        if p["mdev"] is not None:
            push_lines.append(f'netwatch_jitter_ms{{target="{esc(t["id"])}"}} {p["mdev"]}')

    # ---- WAN throughput, rate-limited --------------------------------------
    st = cfg["speedtest"]
    last_speed = prev.get("last_speed", 0)
    wan = {"mbps": None, "ttfb_ms": None, "ts": None, "history": wan_hist}
    if st.get("enabled") and now - last_speed >= int(st.get("interval_s", 1800)):
        s = wan_speed(st)
        if s:
            wan = {"mbps": s["mbps"], "ttfb_ms": s["ttfb_ms"], "ts": now,
                   "history": (wan_hist + [[now, s["mbps"]]])[-history_len:]}
            prev["last_speed"] = now
            push_lines.append(f"netwatch_wan_down_mbps {s['mbps']}")
            if s["ttfb_ms"]:
                push_lines.append(f"netwatch_wan_ttfb_ms {s['ttfb_ms']}")
        else:
            wan["error"] = "speed test failed"
    else:
        # Carry the last known reading forward so the page always shows something.
        last = prev_data.get("wan") or {}
        wan["mbps"] = last.get("mbps")
        wan["ttfb_ms"] = last.get("ttfb_ms")
        wan["ts"] = last.get("ts")
        wan["stale"] = True

    # ---- services ----------------------------------------------------------
    services = []
    for s in cfg["services"]:
        r = http_check(s["url"])
        services.append({"id": s["id"], "name": s["name"], "url": s["url"], **r})
        push_lines.append(f'netwatch_service_up{{svc="{esc(s["id"])}"}} {1 if r["up"] else 0}')

    doc = {
        "updated": iso,
        "updated_epoch": now,
        "host": cfg.get("host"),
        "version": VERSION,
        "targets": results,
        "wan": wan,
        "services": services,
    }
    return doc, prev, resolved, push_lines


def decide_alerts(cfg, doc, prev, now):
    """Return (messages, prev_state_mutations_applied). Pure-ish: mutates prev.

    `alert_status` is what the human was last TOLD about a target, tracked
    separately from the live reading so the page can show every blip while the
    chat stays quiet. `bad_run` counts consecutive bad samples.
    """
    floor = cfg["alerting"]["degraded_loss_pct"]
    debounce = int(cfg["alerting"]["debounce_samples"])
    suppress = int(cfg["alerting"]["blip_suppress_s"])

    alerts = []
    alert_status = prev.get("alert_status", {})
    bad_run = prev.get("bad_run", {})
    bad_since = prev.get("bad_since", {})

    for t, r in zip(cfg["targets"], doc["targets"]):
        tid = r["id"]
        level = alert_level(t, r, floor)
        was = alert_status.get(tid, "up")

        if level == "up":
            # Came back. Only announce it if the episode lasted long enough to
            # have been worth hearing about in the first place.
            if was != "up" and now - (bad_since.get(tid) or now) >= suppress:
                alerts.append(f"RECOVERED: {r['name']} ({r['ip']})")
            bad_run[tid] = 0
            bad_since.pop(tid, None)
            alert_status[tid] = "up"
            continue

        bad_run[tid] = bad_run.get(tid, 0) + 1
        if bad_run[tid] == 1:
            bad_since[tid] = now

        if bad_run[tid] >= debounce and RANK[level] > RANK[was]:
            if level == "down":
                alerts.append(f"DOWN: {r['name']} ({r['ip']}) — 100% loss")
            else:
                alerts.append(f"DEGRADED: {r['name']} ({r['ip']}) — {r['loss']:.0f}% loss")
            alert_status[tid] = level

    for s in doc["services"]:
        was = (prev.get("svc") or {}).get(s["id"], "unknown")
        now_s = "up" if s["up"] else "down"
        if now_s == "down" and was == "up":
            alerts.append(f"SERVICE DOWN: {s['name']}")
        elif now_s == "up" and was == "down":
            alerts.append(f"SERVICE BACK: {s['name']}")

    prev["status"] = {r["id"]: r["status"] for r in doc["targets"]}
    prev["alert_status"] = alert_status
    prev["bad_run"] = bad_run
    prev["bad_since"] = bad_since
    prev["svc"] = {s["id"]: ("up" if s["up"] else "down") for s in doc["services"]}
    return alerts


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="netwatch", description="Home network health collector (stdlib only).")
    ap.add_argument("--config", help="path to netwatch.json")
    ap.add_argument("--out-dir", help="override out_dir (data.json/state.json live here)")
    ap.add_argument("--pushgateway", help="override pushgateway URL")
    ap.add_argument("--no-pushgateway", action="store_true", help="disable metrics push")
    ap.add_argument("--no-speedtest", action="store_true", help="skip the WAN speed test")
    ap.add_argument("--dry-run", action="store_true",
                    help="measure and print a table; write no files, send no alerts")
    ap.add_argument("--print-metrics", action="store_true",
                    help="measure and print Prometheus text to stdout; write nothing")
    ap.add_argument("--print-example", action="store_true", help="print a starter config")
    ap.add_argument("--print-config", action="store_true", help="print the resolved config")
    ap.add_argument("--version", action="version", version=f"netwatch {VERSION}")
    args = ap.parse_args(argv)

    if args.print_example:
        print(json.dumps(EXAMPLE_CONFIG, indent=2))
        return 0

    cfg = load_config(args.config, args)
    if args.print_config:
        shown = {k: v for k, v in cfg.items() if not k.startswith("_")}
        print(json.dumps(shown, indent=2))
        return 0

    now = int(time.time())
    doc, prev, resolved, push_lines = measure(cfg)

    if args.print_metrics:
        # For node_exporter's textfile collector when there is no pushgateway.
        print("\n".join(push_lines))
        return 0

    if args.dry_run:
        print(human_table(doc))
        return 0

    data_path = os.path.join(cfg["out_dir"], "data.json")
    state_path = os.path.join(cfg["out_dir"], "state.json")
    write_json(data_path, doc)
    write_summary(cfg.get("summary_path"), doc)

    alerts = decide_alerts(cfg, doc, prev, now)
    prev["resolved"] = resolved
    write_json(state_path, prev)

    pushed = push_gateway(cfg.get("pushgateway"), push_lines)

    page = f" — {cfg['page_url']}" if cfg.get("page_url") else ""
    if not prev.get("announced"):
        # First ever run: say so once, so silence afterwards means "all well"
        # rather than "the cron was never installed".
        prev["announced"] = True
        write_json(state_path, prev)
        print(f"netwatch is live — {len(doc['targets'])} network points + "
              f"{len(doc['services'])} service"
              f"{'' if len(doc['services']) == 1 else 's'}{page} "
              f"(pushgateway: {'ok' if pushed else 'off/unreachable'})")
        return 0
    if alerts:
        print("netwatch: " + " | ".join(alerts) + page)
    return 0


if __name__ == "__main__":
    sys.exit(main())
