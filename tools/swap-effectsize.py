#!/usr/bin/env python3
"""Bin tcp_conn_tuner mid-socket swaps by effect size.

The IMP/WRS counter used through 0.4.36 sign-tests post/pre against
1.0.  The metric's own noise band spans ~+-5-10%, so the ~2/3 of
swaps that land in a 0.9-1.1 ratio band get labeled by sign alone and
carry no information -- post=5,000,001 vs pre=5,000,000 is a "WRS",
the reverse is an "IMP".  sign-IMP overstates the real win rate by
~2.3x.

This re-bins by effect size so the underlying distribution is visible:

    ratio          label        meaning
    < 0.5          big_win      algorithm unlocked the pipe
    0.5 - 0.9      mod_win      solid improvement
    0.9 - 1.1      null         no meaningful change (noise)
    1.1 - 1.5      mod_loss     real regression
    1.5 - 2.0      big_loss     clear regression
    > 2.0          severe_loss  severe regression

Splits results by tier (d=0 moderate, d=1 desperate) because the two
tiers have very different win rates under effect-size binning.

Reads /var/log/bpftune-met-YYYY-MM-DD.log (the last 4 by filename).
For each swap event, the post value is the next met line for the same
socket cookie.

Usage:  sudo python3 tools/swap-effectsize.py
        sudo python3 tools/swap-effectsize.py /var/log/bpftune-met-2026-09-16.log
"""
import re, sys, os, glob
from collections import defaultdict

met_re  = re.compile(r'met cookie=(\d+) rport=(\d+) alg=(\d+) segs=(\d+) val=(\d+) rtt=(\d+) rate=(\d+) loss=(\d+) smrtt=(\d+) bmrtt=(\d+) avgrtt=(\d+)')
swap_re = re.compile(r'swap cookie=(\d+) from=(\d+) to=(\d+) bc=(\d+) ac=(\d+) d=(\d)')

BINS = [
    (0.0, 0.5, "big_win"),
    (0.5, 0.9, "mod_win"),
    (0.9, 1.1, "null"),
    (1.1, 1.5, "mod_loss"),
    (1.5, 2.0, "big_loss"),
    (2.0, float("inf"), "severe_loss"),
]

def classify(r):
    for lo, hi, name in BINS:
        if lo <= r < hi:
            return name
    return "?"

def parse_day(path):
    last_met = {}
    pending  = defaultdict(list)
    swaps    = []
    with open(path) as f:
        for line in f:
            m = met_re.search(line)
            if m:
                c = int(m.group(1)); v = int(m.group(5))
                if v == 0 or v == (1 << 64) - 1:
                    continue
                for sw in pending.get(c, []):
                    if sw['post'] is None:
                        sw['post'] = v
                pending[c] = []
                last_met[c] = v
                continue
            m = swap_re.search(line)
            if m:
                c = int(m.group(1))
                if c not in last_met:
                    continue
                sw = {'cookie': c, 'pre': last_met[c],
                      'from': int(m.group(2)), 'to': int(m.group(3)),
                      'd': int(m.group(6)), 'post': None}
                swaps.append(sw)
                pending[c].append(sw)
    return swaps

def report(name, swaps):
    done = [s for s in swaps if s['post'] is not None and s['pre'] > 0]
    if not done:
        print("%s: no observations\n" % name)
        return
    n = len(done)
    bins = defaultdict(list)
    for s in done:
        s['ratio'] = s['post'] / s['pre']
        bins[classify(s['ratio'])].append(s)

    print("=== %s: %d swaps with post-observation ===" % (name, n))
    print("%-14s %6s %8s" % ("bin", "n", "pct"))
    for _lo, _hi, bname in BINS:
        v = bins.get(bname, [])
        print("%-14s %6d %7.1f%%" % (bname, len(v), 100.0*len(v)/n))

    imp = sum(1 for s in done if s['ratio'] < 1.0)
    print("  old sign test : IMP=%d (%.1f%%)  WRS=%d (%.1f%%)" %
          (imp, 100.0*imp/n, n-imp, 100.0*(n-imp)/n))

    mw = len(bins.get("big_win", [])) + len(bins.get("mod_win", []))
    mn = len(bins.get("null", []))
    ml = n - mw - mn
    print("  effect-size   : win=%d (%.1f%%)  null=%d (%.1f%%)  loss=%d (%.1f%%)" %
          (mw, 100.0*mw/n, mn, 100.0*mn/n, ml, 100.0*ml/n))

    for d in (0, 1):
        sub = [s for s in done if s['d'] == d]
        if not sub:
            continue
        sn = len(sub)
        simp = sum(1 for s in sub if s['ratio'] < 1.0)
        smw = sum(1 for s in sub if s['ratio'] < 0.9)
        sml = sum(1 for s in sub if s['ratio'] > 1.1)
        smn = sn - smw - sml
        print("  d=%d: n=%d  signIMP=%.1f%%  effect: win=%.1f%% null=%.1f%% loss=%.1f%%" %
              (d, sn, 100.0*simp/sn, 100.0*smw/sn, 100.0*smn/sn, 100.0*sml/sn))

    regr = sorted([s for s in done if s['ratio'] > 1.1],
                  key=lambda s: -s['ratio'])
    if regr:
        print("  regressions (>1.1):")
        for s in regr[:12]:
            print("    cookie=%-6d d=%d  %2d->%-2d  pre=%12d post=%12d ratio=%.3f" %
                  (s['cookie'], s['d'], s['from'], s['to'],
                   s['pre'], s['post'], s['ratio']))
    print()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        files = sys.argv[1:]
    else:
        files = sorted(glob.glob("/var/log/bpftune-met-*.log"))[-4:]
    if not files:
        sys.exit("no met logs found")
    print("analyzing: %s\n" % " ".join(os.path.basename(f) for f in files))
    agg = []
    for f in files:
        swaps = parse_day(f)
        agg.extend(swaps)
        report(os.path.basename(f), swaps)
    if len(files) > 1:
        report("AGGREGATE", agg)
