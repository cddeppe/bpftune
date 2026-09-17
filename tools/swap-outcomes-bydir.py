#!/usr/bin/env python3
"""Split a day's swaps by direction: client-facing vs origin-facing.

tcp_conn_tuner treats every socket identically, but a TCP proxy has
two directions of socket:

  client-facing (rport != 443)
      proxy is sending video to the user.  Our CC controls the rate.
      A swap here can help.

  origin-facing (rport == 443)
      proxy is receiving video from a CDN.  Our CC controls how fast
      we send HTTP requests and ACKs upstream -- NOT how fast the
      CDN sends data down.  A swap here cannot affect the flow.

Measured 2026-09-16: 57.7% of the day's swaps were origin-facing.
2026-09-17 morning: 88%.  The aggregate d=1 win% is therefore
dominated by swaps whose outcome is structurally fixed.  The
client-facing column is the number to read.

Usage:  sudo python3 tools/swap-outcomes-bydir.py [/var/log/...log]
"""
import re, sys, os, glob
from collections import defaultdict

MET  = re.compile(r'met cookie=(\d+) rport=(\d+) alg=(\d+) segs=(\d+) val=(\d+)')
SWAP = re.compile(r'swap cookie=(\d+) from=(\d+) to=(\d+) bc=(\d+) ac=(\d+) d=(\d)')
TS   = re.compile(r'\s(\d+\.\d+):\s+bpf_trace_printk:')

def parse(path):
    seq = defaultdict(list)
    swaps = []
    with open(path) as f:
        for line in f:
            if not TS.search(line): continue
            m = MET.search(line)
            if m:
                seq[int(m.group(1))].append(
                    ('met', int(m.group(5)), int(m.group(2))))
                continue
            m = SWAP.search(line)
            if m:
                ev = {'cookie': int(m.group(1)),
                      'from': int(m.group(2)), 'to': int(m.group(3)),
                      'd': int(m.group(6)),
                      'pre': None, 'post': None, 'rport': -1}
                swaps.append(ev)
                seq[ev['cookie']].append(('swap', ev, None))
    for c, items in seq.items():
        last_met = None
        for i, it in enumerate(items):
            if it[0] == 'met':
                last_met = it
            else:
                ev = it[1]
                if last_met is not None:
                    ev['pre'] = last_met[1]
                    ev['rport'] = last_met[2]
                    for j in range(i + 1, len(items)):
                        if items[j][0] == 'met':
                            ev['post'] = items[j][1]
                            break
    return swaps

def bin_out(r):
    return 'win' if r < 0.9 else ('null' if r <= 1.1 else 'loss')

def report(label, sub):
    sub = [s for s in sub if s['post'] is not None and s['pre'] and s['pre'] > 0]
    if not sub:
        print("  %-30s n=0" % label); return
    n = len(sub)
    w = sum(1 for s in sub if bin_out(s['post'] / s['pre']) == 'win')
    nu = sum(1 for s in sub if bin_out(s['post'] / s['pre']) == 'null')
    l = n - w - nu
    print("  %-30s n=%-4d win=%5.1f%% null=%5.1f%% loss=%5.1f%%" %
          (label, n, 100.0 * w / n, 100.0 * nu / n, 100.0 * l / n))

if __name__ == "__main__":
    files = (sys.argv[1:] if len(sys.argv) > 1
             else sorted(glob.glob("/var/log/bpftune-met-*.log"))[-2:])
    for path in files:
        if not os.path.exists(path):
            print("%s: not present" % path); continue
        swaps = parse(path)
        client  = [s for s in swaps if s['rport'] not in (-1, 443)]
        origin  = [s for s in swaps if s['rport'] == 443]
        unknown = [s for s in swaps if s['rport'] == -1]
        n = len(swaps)
        print("=== %s ===" % os.path.basename(path))
        if n == 0:
            print("  (no swaps)"); print(); continue
        print("  total swaps: %d" % n)
        print("    client-facing (rport != 443): %4d (%5.1f%%)" % (len(client),  100.0 * len(client) / n))
        print("    origin-facing (rport == 443): %4d (%5.1f%%)" % (len(origin),  100.0 * len(origin) / n))
        print("    unknown:                      %4d (%5.1f%%)" % (len(unknown), 100.0 * len(unknown) / n))
        print()
        for tier in (0, 1):
            print("  d=%d (%s):" % (tier, "moderate" if tier == 0 else "desperate"))
            report("all",           [s for s in swaps  if s['d'] == tier])
            report("client-facing", [s for s in client if s['d'] == tier])
            report("origin-facing", [s for s in origin if s['d'] == tier])
            print()
