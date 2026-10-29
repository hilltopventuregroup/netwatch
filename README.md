# netwatch

A tiny home-network health collector: **one Python file, standard library only,
no daemon, no database.** It pings your network points, probes a few services,
keeps a rolling 24 h history, draws it on a static page, and stays completely
silent until something has actually been broken long enough to be worth a
message.

```console
$ ./netwatch.py --dry-run
netwatch 1.0.0 — netwatch-host — 2026-10-29T18:04:18Z

  POINT                 IP               STATUS      LOSS      AVG      MAX
  Router                192.168.1.1      up            0%      3.3      3.7
  NAS                   192.168.1.10     up            0%      2.0      2.2
  Desktop               192.168.1.50     up            0%      2.3      2.4
  AP upstairs           192.168.1.97     up            0%      6.1      9.8
  Internet              1.1.1.1          up            0%     10.5     11.9

  Home Assistant        200              up          135 ms
```

Two ideas do most of the work:

**Silent by default.** A 5-minute prober that reports every bad sample is a
noise generator — every WiFi blip becomes two messages (DOWN, then RECOVERED).
netwatch only prints on a *state change*, and only after the state has held for
two consecutive samples. Empty output means "all well", which makes it safe to
run from cron with `MAILTO` set, from a systemd timer, or from a chat-agent
scheduler that forwards stdout to a human.

**Identity, not just an address.** A ping can tell you *something* answered at
`192.168.1.42`; it cannot tell you *what*. Consumer routers that cannot hold
DHCP reservations hand out a new address after a reboot, at which point a
ping-only monitor either invents an outage or — worse — reports "server is up"
while some unrelated device answers on the old address. Give a target a `mac`
and netwatch checks the ARP table; on a silent address or a wrong-MAC answer it
sweeps the /24 and follows the device.

## What you get

- **Per-point history** — latency min/avg/max/jitter + loss %, 288 samples
  (24 h at a 5-minute cadence) as a sparkline per point.
- **Service probes** — HTTP GET with status code and response time. A 401/403
  counts as *up*: it proves something is listening and speaking HTTP.
- **Optional WAN throughput** — a fixed-size download timed end-to-end,
  rate-limited to every 30 min so it does not eat your link.
- **Prometheus metrics** — pushed to a pushgateway if you have one, or printed
  as text for `node_exporter`'s textfile collector if you don't.
- **A static dashboard** — one HTML file, no build step, no JS dependencies,
  reads `data.json` next to itself.

## Quick start

```bash
git clone https://github.com/hilltopventuregroup/netwatch.git
cd netwatch

./netwatch.py --print-example > netwatch.json   # then edit it
./netwatch.py --dry-run                         # measure, print, write nothing
./netwatch.py                                   # first real run: writes files + announces itself
./netwatch.py                                   # silent unless something changed
```

Requirements: Python 3.8+ and a `ping` binary. That is the whole list. (`ip
neigh` on Linux or `arp` on macOS is used only for the optional MAC check;
`curl` is not needed.)

Then serve the page next to the data. Simplest possible:

```bash
mkdir -p ~/netwatch/data                       # matches out_dir in netwatch.json
cp dashboard/index.html ~/netwatch/data/
cd ~/netwatch/data && python3 -m http.server 8080
# → http://localhost:8080/
```

Any static server works — Caddy, nginx, GitHub Pages. `index.html` fetches
`data.json` from the same directory, so the two must sit together (or set
`window.NETWATCH_DATA_URL` if they don't).

## Configure

Everything lives in one JSON file: `./netwatch.json`, or `~/.config/netwatch/config.json`,
or whatever `--config` / `$NETWATCH_CONFIG` points at.

```json
{
  "host": "netwatch-host",
  "page_url": "http://netwatch.lan/netwatch/",
  "lan_prefix": "192.168.1",
  "out_dir": "~/netwatch/data",
  "pushgateway": "",
  "history_len": 288,
  "ping": { "count": 10, "interval": 0.2, "timeout": 1.0 },
  "speedtest": { "enabled": false, "interval_s": 1800, "bytes": 25000000 },
  "alerting": { "degraded_loss_pct": 5, "debounce_samples": 2, "blip_suppress_s": 600 },
  "targets": [
    { "id": "router", "name": "Router", "ip": "192.168.1.1", "kind": "wired",
      "alert_loss_pct": 30, "note": "why this point is here" },
    { "id": "pc", "name": "Desktop", "ip": "192.168.1.50", "kind": "wired",
      "mac": "aa:bb:cc:dd:ee:ff" }
  ],
  "services": [
    { "id": "router-ui", "name": "Router admin", "url": "http://192.168.1.1/" }
  ]
}
```

| key | meaning |
|---|---|
| `host` | label on the page and in the first-run message |
| `page_url` | appended to alert messages so the message is self-serving |
| `lan_prefix` | first three octets, used by the MAC sweep (`""` disables it) |
| `out_dir` | where `data.json` + `state.json` live |
| `summary_path` | optional extra compact JSON for a dashboard tile |
| `pushgateway` | push URL; `""` disables metrics |
| `history_len` | samples kept per point (288 = 24 h at 5 min) |
| `ping.count` / `.interval` / `.timeout` | packets per probe, spacing (s), per-packet timeout (s) |
| `speedtest.enabled` | WAN throughput probe, off by default |
| `alerting.degraded_loss_pct` | loss above which a point is `degraded` |
| `alerting.debounce_samples` | consecutive bad samples before anything is sent |
| `alerting.blip_suppress_s` | episodes shorter than this stay entirely quiet |
| target `kind` | `wired`, `ap`, `wifi`, `wan`, `other` — colour on the page |
| target `mac` | optional; enables drift-following and wrong-device detection |
| target `alert_loss_pct` | optional; raises *only that point's* alerting bar |
| target `note` | free text shown under the card (keep it factual) |

Status is derived, not configured: `up` = loss ≤ `degraded_loss_pct`,
`degraded` = lossy but some replies, `down` = 100 % loss.

A service counts as `up` if the HTTP response is below 500 — a `401` or `403`
still proves something is listening and speaking HTTP, which is the question
being asked. `5xx`, connection refused, timeout and DNS failure all read as
down. Set `page_url` if you want the page's own address in alert text.

## Alerting, exactly

```
sample 1:  4 of 10 replies lost → nothing            (bad_run=1, below debounce=2)
sample 2:  6 of 10 replies lost → "DEGRADED: NAS (192.168.1.10) — 60% loss"
sample 3: 10 of 10 lost         → "DOWN: NAS (192.168.1.10) — 100% loss"
sample 4…: still down           → nothing            (state held, no repeat spam)
sample N:  replies again        → "RECOVERED: NAS (…)" only if the episode
                                  lasted ≥ blip_suppress_s, else silence
```

`debounce_samples: 2` means the first bad sample is always patient, and a target
that goes straight to 100 % loss is announced on the *second* bad sample —
~10 minutes in at a 5-minute cadence. Escalations are separate events, so a
point that degrades and then dies is one message, then one more.

- `alert_status` in `state.json` is **what you were last told**, tracked
  separately from the live reading. The page records every blip; the chat does
  not. When reconstructing "what did I actually get told?", read `alert_status`,
  not `status`.
- An escalation `degraded → down` while already alerted re-alerts once.
- Service probes have no debounce — a service that stops answering for one
  sample is real, so those print immediately on transition.
- Deleting `state.json` resets all of this: the next run re-announces itself and
  re-alerts anything currently bad.

## Prometheus / Grafana

Pushed with `job="netwatch"`:

| metric | labels | meaning |
|---|---|---|
| `netwatch_up` | `target`, `ip` | 1 = status `up` |
| `netwatch_loss_pct` | `target`, `ip` | ICMP loss % |
| `netwatch_latency_avg_ms` | `target`, `ip` | mean RTT |
| `netwatch_jitter_ms` | `target` | RTT std-dev |
| `netwatch_service_up` | `svc` | 1 = service answered |
| `netwatch_wan_down_mbps` | — | only when the speed test ran |
| `netwatch_wan_ttfb_ms` | — | only when the speed test ran |
| `netwatch_last_run_seconds` | — | unix time of the sample — alert on staleness with this |

Because this pushes rather than exposes, a `job` scrape config is not needed —
just point `pushgateway` at your pushgateway. No pushgateway? Use the textfile
collector:

```cron
*/5 * * * * /usr/bin/python3 /home/you/netwatch/netwatch.py --print-metrics \
  > /var/lib/node_exporter/textfile/netwatch.prom
```

Useful starting queries:

```promql
# loss by point, last 6h
max_over_time(netwatch_loss_pct[6h]) > 1

# points that have been down longer than 15 minutes
netwatch_up == 0 and (time() - timestamp(netwatch_up == 0)) > 900

# p95 latency per point
quantile_over_time(0.95, netwatch_latency_avg_ms[1h])
```

**Pushgateway caveat:** pushed metrics are never expired. If netwatch dies, the
last values stick and `netwatch_up` keeps reading 1 — a monitor that fails
silently is worse than no monitor. Alert on it:

```promql
time() - netwatch_last_run_seconds > 900   # no sample in 15 min
```

## Scheduling

**systemd timer** (recommended: survives reboots, logs to the journal):

```bash
sudo cp deploy/netwatch@.service deploy/netwatch@.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now netwatch@$USER.timer
systemctl list-timers netwatch@$USER.timer
journalctl -u netwatch@$USER.service -n 20
```

The unit files assume `/home/<user>/netwatch` — adjust `ExecStart` if yours
lives elsewhere.

**plain cron:** see `deploy/crontab.example`.

**An agent scheduler** (a chat bot that runs a script and forwards non-empty
stdout to you): see `deploy/agent-scheduler.md`. This is the shape netwatch was
originally written for — silent-on-success maps directly onto "don't message me
unless something happened".

## Limitations, honestly

- **ICMP to the router's own address is not a network test.** Many consumer
  routers deprioritise echo replies addressed to their management plane when the
  CPU is busy: you get 10–30 % loss and 20× latency while every other point is
  clean. Keep the router on the page, but give it `alert_loss_pct` (30 works) so
  only a real 100 % loss alerts.
- **A 5-minute cadence has a 5-minute blind spot.** The debounce that kills blip
  spam also means a single-sample outage is deliberately silent. If you need
  sub-minute detection for one critical host, run a second instance with
  `debounce_samples: 1` and a shorter cadence.
- **The MAC sweep assumes a flat /24.** It pings `lan_prefix.1-254` and reads the
  ARP cache. VLANs, /16s and client-isolated WiFi break it — leave `lan_prefix`
  empty there and the check is skipped.
- **Latency includes the path.** If the collector is on WiFi, every wired point's
  number includes the radio hop. Put the collector on a cable if the numbers
  matter.
- **IPv4 only.** No IPv6 probes.
- **No auth on the page.** It exposes hostnames, IPs and status to anyone who can
  reach it. Keep it LAN/VPN-only, or put it behind your reverse proxy's auth.
- **Ping needs no privileges** on Linux with the default
  `net.ipv4.ping_group_range`, but in minimal containers you may need
  `CAP_NET_RAW` / `setcap cap_net_raw+ep /bin/ping`.

## Files

```
netwatch.py               the collector (stdlib only)
netwatch.example.json     starter config
dashboard/index.html      static page — reads data.json beside itself
deploy/netwatch@.service  systemd unit (oneshot)
deploy/netwatch@.timer    5-minute timer
deploy/crontab.example    plain cron
deploy/agent-scheduler.md scheduler-agnostic notes
tests/render_check.js     optional jsdom render check for the page
tests/fixture-data.json   sample data covering up / degraded / down / stale
```

At runtime, `out_dir` gets `data.json` (page data, rolling history) and
`state.json` (what you were last told, debounce counters, MAC-resolved
addresses). Both are written atomically via `os.replace`.

## Development

The collector is one file with no dependencies, so there is no test framework
worth pretending to have. Two cheap checks:

```bash
python3 netwatch.py --dry-run                       # does it parse your LAN correctly?
python3 netwatch.py --print-config                  # what config did it actually resolve?
```

The dashboard has an optional render check that runs the real page in jsdom and
asserts the resulting DOM:

```bash
npm i jsdom            # dev-only; not a runtime dependency
node tests/render_check.js
```

It fails on JS errors, missing cards, unrendered sparklines, and `undefined` /
`NaN` leaking into visible text — exactly the class of bug a static HTML page
will happily ship because nothing renders it in CI.

## License

MIT — see `LICENSE`.
