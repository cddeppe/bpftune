#!/usr/bin/env python3
"""Show every algorithm's per-bucket metric, plus the spread.

bucket-leaders.py truncates to top-3, which hides the full
distribution.  Whether the metric discriminates algorithms at all
is answered by the spread across all trusted algorithms
(metric_count >= TRUST_MIN), not by the gap between the top two.

0.4.40 rationale: pre-change, the spread across all 16 algorithms
on the home bucket was 34-65%, against a ~200%+ gap if the
algorithms actually differed.  The util gate was added to widen
that spread by excluding votes where the socket wasn't using its
window.  This tool measures whether that worked.

Columns:
    n_algs   algorithms with metric_count >= TRUST_MIN
    best     lowest metric_value among trusted  (winner)
    med      median metric_value among trusted
    worst    highest metric_value among trusted (loser)
    spread   (worst - best) / best * 100  -- the number we want big
    verdict  NONE (0-1 algs), THIN (2-3), NARROW (<30%),
             MODERATE (30-100%), WIDE (>100%)

Usage:  sudo python3 tools/bucket-spread.py
"""

import ipaddress, json, signal, subprocess, sys
from statistics import median

signal.signal(signal.SIGPIPE, signal.SIG_DFL)

NAMES = ['cubic','bbr','htcp','dctcp','scalable','vegas','veno','westwood',
         'reno','illinois','yeah','lp','bic','highspeed','hybla','nv']
TRUST_MIN = 3


def verdict(spread_pct, n_algs):
    if n_algs == 0:
        return "NONE"
    if n_algs <= 3:
        return "THIN"
    if spread_pct < 30:
        return "NARROW"
    if spread_pct < 100:
        return "MODERATE"
    return "WIDE"


def ip_from_key(k):
    if k[:10] == [0]*10 and k[10] == 255 and k[11] == 255:
        return "%d.%d.%d.%d" % (k[12], k[13], k[14], k[15])
    return str(ipaddress.IPv6Address(bytes(k)))


def main():
    try:
        raw = subprocess.check_output(
            ['sudo', 'bpftool', 'map', 'dump', 'name', 'remote_host_map'],
            stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        sys.exit("bpftool failed: %s" % e)

    data = json.loads(raw)

    print("%-40s %6s %6s %7s   %-22s %-22s %-22s %8s %9s" %
          ("destination", "inst", "n_alg", "votes",
           "best", "median", "worst", "spread%", "verdict"))
    print("-" * 170)

    rows = []
    for top in data:
        for e in top.get("elements", [top]):
            k = e.get("key", {}).get("in6_u", {}).get("u6_addr8", [])
            if not k:
                continue
            v = e["value"]
            vals = []
            for i, m in enumerate(v.get("metrics", [])):
                if m["metric_count"] >= TRUST_MIN and m["metric_value"] > 0:
                    vals.append((i, m["metric_value"], m["metric_count"]))
            if not vals:
                continue
            vals.sort(key=lambda x: x[1])
            best = vals[0][1]
            worst = vals[-1][1]
            spread = (worst - best) / best * 100 if best else 0
            inst = v.get("instances", 0)
            rows.append({
                'ip': ip_from_key(k),
                'inst': inst,
                'n_algs': len(vals),
                'votes': sum(c for _, _, c in vals),
                'best': best,
                'worst': worst,
                'med': int(median(mv for _, mv, _ in vals)),
                'spread': spread,
                'verdict': verdict(spread, len(vals)),
                'best_alg': NAMES[vals[0][0]],
                'worst_alg': NAMES[vals[-1][0]],
            })

    # busiest buckets first
    rows.sort(key=lambda r: -r['inst'])
    for r in rows:
        print("%-40s %6d %6d %7d   %-6s/%-14d %-6s/%-14d %-6s/%-14d %7.1f%% %9s" % (
            r['ip'], r['inst'], r['n_algs'], r['votes'],
            r['best_alg'], r['best'],
            "-", r['med'],
            r['worst_alg'], r['worst'],
            r['spread'], r['verdict']))


if __name__ == "__main__":
    main()
