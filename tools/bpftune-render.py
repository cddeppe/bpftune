#!/usr/bin/env python3
"""bpftune history renderer.  Reads /var/lib/bpftune/history/*.csv
and writes /var/lib/bpftune/history/index.html.

Run after the collector (manually or from cron every 5 min).
All output is a single self-contained HTML file with embedded data.
No external dependencies."""

import csv, html, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HIST = Path("/var/lib/bpftune/history")
BUCKETS = HIST / "buckets.csv"
SWAPS   = HIST / "swaps.csv"
OUT     = HIST / "index.html"

CONGS = ["cubic","bbr","htcp","dctcp","scalable","vegas","veno","westwood",
         "reno","illinois","yeah","lp","bic","highspeed","hybla","nv"]


def esc(s):
    return html.escape(str(s))


def read_csv(path):
    if not path.exists():
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def day_of(ts):
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return "?"


def svg_bar(label, value, max_value, color="#3a7"):
    W, H = 240, 16
    w = int(W * (value / max_value)) if max_value else 0
    return (f'<svg width="{W}" height="{H}">'
            f'<rect x="0" y="2" width="{W}" height="12" '
            f'fill="#222" stroke="#444"/>'
            f'<rect x="0" y="2" width="{w}" height="12" fill="{color}"/>'
            f'</svg>')


def section_divergence(swaps):
    """Aggregate per-day: rate==metric vs rate!=metric, win/loss."""
    by_day = defaultdict(lambda: {
        "eq":  [0, 0, 0],   # win, null, loss
        "neq": [0, 0, 0],
        "pre": [0, 0, 0]})
    for s in swaps:
        d = day_of(s.get("ts_epoch", 0))
        if not s.get("outcome"): continue
        o = s["outcome"]
        idx = {"win": 0, "null": 1, "loss": 2}.get(o)
        if idx is None: continue
        if not s.get("mt_alg") or not s.get("rb_alg"):
            by_day[d]["pre"][idx] += 1
        elif s.get("diverges") == "1":
            by_day[d]["neq"][idx] += 1
        else:
            by_day[d]["eq"][idx] += 1

    rows = []
    for d in sorted(by_day.keys()):
        g = by_day[d]
        def rate(v):
            n = sum(v)
            return (f"{v[0]}/{v[2]}  ({int(100*v[0]/n)}w/{int(100*v[2]/n)}l, n={n})"
                    if n else "-")
        rows.append(f"<tr><td>{esc(d)}</td>"
                    f"<td>{rate(g['eq'])}</td>"
                    f"<td>{rate(g['neq'])}</td>"
                    f"<td>{rate(g['pre'])}</td></tr>")
    return ("""<h2>Swap outcomes by day (win rate in each target category)</h2>
<table>
<tr><th>day (UTC)</th>
    <th>rate == metric</th>
    <th>rate != metric</th>
    <th>pre-0.4.45</th></tr>
""" + "\n".join(rows) + "\n</table>")


def section_swaps_per_day(swaps):
    by_day = defaultdict(lambda: {"total": 0, "d1": 0, "d0": 0})
    for s in swaps:
        d = day_of(s.get("ts_epoch", 0))
        by_day[d]["total"] += 1
        if s.get("d") == "1": by_day[d]["d1"] += 1
        else:                 by_day[d]["d0"] += 1
    rows = []
    mx = max((v["total"] for v in by_day.values()), default=1)
    for d in sorted(by_day.keys()):
        v = by_day[d]
        rows.append(f"<tr><td>{esc(d)}</td><td>{v['total']}</td>"
                    f"<td>{v['d1']}</td><td>{v['d0']}</td>"
                    f"<td>{svg_bar(d, v['total'], mx)}</td></tr>")
    return ("""<h2>Swaps per day</h2>
<table>
<tr><th>day</th><th>total</th><th>d=1</th><th>d=0</th><th>bar</th></tr>
""" + "\n".join(rows) + "\n</table>")


def section_bucket_recent(buckets):
    """Last reading per bucket (from the newest minute)."""
    if not buckets:
        return "<p><i>no bucket data collected yet</i></p>"
    latest = max(b["ts_epoch"] for b in buckets)
    rows = []
    items = [b for b in buckets if b["ts_epoch"] == latest]
    items.sort(key=lambda b: -int(b["instances"]))
    for b in items[:12]:
        rows.append(f"<tr><td>{esc(b['addr'])}</td>"
                    f"<td>{esc(b['instances'])}</td>"
                    f"<td>{esc(b['min_rtt'])}</td>"
                    f"<td>{int(b['ref_rate'])/125000:.1f}</td>"
                    f"<td>{esc(b['best_alg'])}</td>"
                    f"<td>{esc(b['rate_best_v'])}</td></tr>")
    return (f"""<h2>Buckets (snapshot at ts={latest})</h2>
<table>
<tr><th>dest</th><th>inst</th><th>min_rtt_us</th><th>ref_Mbps</th>
    <th>best_i</th><th>rate_best_v</th></tr>
""" + "\n".join(rows) + "\n</table>")


def section_tcp_rmem(buckets):
    """Track tcp_rmem over time.  Not in buckets.csv — read live."""
    try:
        with open("/proc/sys/net/ipv4/tcp_rmem") as f:
            v = f.read().split()[-1]
    except Exception:
        v = "?"
    return f'<h2>Kernel</h2><p>tcp_rmem max (current): <b>{esc(v)}</b></p>'


def main():
    buckets = read_csv(BUCKETS)
    swaps   = read_csv(SWAPS)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<title>bpftune history — {esc(now)}</title>
<style>
  body {{ font-family: ui-monospace, monospace; background:#0d0d0d;
          color:#ddd; margin:1.5em; }}
  h1 {{ font-family:sans-serif; color:#eee; }}
  h2 {{ margin-top:1.5em; color:#ccc; border-bottom:1px solid #333;
        padding-bottom:.2em; }}
  table {{ border-collapse:collapse; margin:.5em 0; }}
  td, th {{ padding:.25em .75em; text-align:left;
            border-bottom:1px solid #222; }}
  th {{ color:#888; font-weight:normal; }}
  tr:hover td {{ background:#1a1a1a; }}
  p.small {{ color:#888; font-size:.9em; }}
</style>
</head><body>
<h1>bpftune history</h1>
<p class="small">generated {esc(now)} UTC — {len(buckets)} bucket rows,
   {len(swaps)} swap rows</p>

{section_divergence(swaps)}
{section_swaps_per_day(swaps)}
{section_bucket_recent(buckets)}
{section_tcp_rmem(buckets)}

</body></html>
"""
    OUT.write_text(body)
    print(f"wrote {OUT} ({len(body)} bytes)")


if __name__ == "__main__":
    main()
