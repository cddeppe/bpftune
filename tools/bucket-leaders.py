#!/usr/bin/env python3
"""Show per-destination bucket leaders from bpftune's remote_host_map.

Under destination-keying (0.4.18+), each bucket represents one remote
destination address.  Shows the top-3 algorithms with sample counts, plus
the swap-decision state (best_i / second_i) that the tuner uses to target
mid-socket swaps.

Verdict thresholds (top-1 metric_value vs top-2 metric_value):
    ALONE   - only one algorithm has samples
    CLEAR   - leader is >=2x better than next       (converged)
    LEAD    - leader is 1.2-2x better than next     (preference)
    TIGHT   - leader is <1.2x better than next      (bouncing)
    THIN    - leader has <3 samples; verdict not yet trustworthy

Swap-decision columns:
    best    - best_i / best_v as tracked incrementally for swap targets
    second  - second_i / second_v, used when best is not trusted enough

Usage:  sudo python3 tools/bucket-leaders.py
"""
import ipaddress, json, signal, subprocess, sys

signal.signal(signal.SIGPIPE, signal.SIG_DFL)

names = ['cubic','bbr','htcp','dctcp','scalable','vegas','veno','westwood',
         'reno','illinois','yeah','lp','bic','highspeed','hybla','nv']

TRUST_MIN = 3

try:
    raw = subprocess.check_output(
        ['sudo','bpftool','map','dump','name','remote_host_map'],
        stderr=subprocess.DEVNULL)
except (subprocess.CalledProcessError, FileNotFoundError) as e:
    sys.exit("bpftool failed: %s" % e)

data = json.loads(raw)

# Normalize across bpftool versions.  For multi-netns hosts, each top-level
# entry may carry an 'id'.  Track it so duplicate addresses become legible.
entries = []
for top in data:
    map_id = top.get('id', '-')
    if 'elements' in top:
        for el in top['elements']:
            el['__map_id'] = map_id
            entries.append(el)
    elif 'key' in top and 'value' in top:
        top['__map_id'] = map_id
        entries.append(top)

def fmt_key(b):
    if b[:10] == [0]*10 and b[10] == 255 and b[11] == 255:
        return "%d.%d.%d.%d" % (b[12], b[13], b[14], b[15])
    return str(ipaddress.IPv6Address(bytes(b)))

def fmt_val(v):
    if v == 0:
        return "0"
    if v >= 1_000_000:
        return "%.1fM" % (v / 1_000_000.0)
    if v >= 1_000:
        return "%.1fK" % (v / 1_000.0)
    return "%d" % v

def alg_name(i):
    return names[i] if 0 <= i < len(names) else "?%d" % i

rows_out = []
no_closes = []

for entry in entries:
    if 'key' not in entry or 'value' not in entry:
        continue
    v = entry['value']
    if v['instances'] == 0:
        continue
    key = fmt_key(entry['key']['in6_u']['u6_addr8'])
    map_id = entry.get('__map_id', '-')

    alg_rows = []
    for i, m in enumerate(v['metrics']):
        if m['metric_count'] == 0:
            continue
        alg_rows.append((m['metric_value'], m['metric_count'],
                         m['greedy_count'], names[i]))

    if not alg_rows:
        no_closes.append((v['instances'], key, map_id))
        continue

    alg_rows.sort()
    top3 = alg_rows[:3]
    top3_str = ' '.join('%s=%s(n%d)' % (nm, fmt_val(mv), n)
                        for mv, n, g, nm in top3)

    has_leader = v.get('best_v', 0) != 0
    best_str = "-"
    if has_leader:
        bi = v.get('best_i', -1)
        bv = v.get('best_v', 0)
        bcm = v['metrics'][bi]['metric_count'] if 0 <= bi < len(v['metrics']) else 0
        best_str = "%s=%s" % (alg_name(bi), fmt_val(bv))
        if bcm < TRUST_MIN:
            best_str += " [untrusted n%d]" % bcm
        if v.get('second_v', 0) != 0:
            si = v.get('second_i', -1)
            sv = v.get('second_v', 0)
            best_str += " / %s=%s" % (alg_name(si), fmt_val(sv))

    if len(alg_rows) >= 2 and alg_rows[0][0] > 0:
        if alg_rows[0][1] < TRUST_MIN:
            verdict = "THIN (n%d)" % alg_rows[0][1]
        else:
            ratio = alg_rows[1][0] / float(alg_rows[0][0])
            word = "CLEAR" if ratio >= 2.0 else ("LEAD" if ratio >= 1.2 else "TIGHT")
            verdict = "%s (x%.1f)" % (word, ratio)
    else:
        verdict = "ALONE" if alg_rows[0][1] >= TRUST_MIN else "THIN (n%d)" % alg_rows[0][1]

    rows_out.append({'key': key, 'map_id': map_id, 'inst': v['instances'],
                     'mrtt': v['min_rtt'], 'mrate': v['max_rate_delivered'],
                     'top3': top3_str, 'best': best_str, 'verdict': verdict})

rows_out.sort(key=lambda r: -r['inst'])

w_key = max([len("destination")] + [len(r['key']) for r in rows_out])
w_top3 = max([len("top-3")] + [len(r['top3']) for r in rows_out])
w_best = max([len("best / second")] + [len(r['best']) for r in rows_out])

hdr = ("%-" + str(w_key) + "s  %-4s  %-9s  %-7s  %-8s  %-" + str(w_top3) +
       "s  %-" + str(w_best) + "s  %s") % (
    "destination", "map", "instances", "min_rtt", "max_rate",
    "top-3 (value, sample count)", "best / second", "verdict")
print(hdr)
print("-" * len(hdr))

for r in rows_out:
    print(("%-" + str(w_key) + "s  %-4s  %-9d  %-7d  %-8s  %-" + str(w_top3) +
           "s  %-" + str(w_best) + "s  %s") % (
        r['key'], str(r['map_id']), r['inst'], r['mrtt'], fmt_val(r['mrate']),
        r['top3'], r['best'], r['verdict']))

if no_closes:
    total_nc = sum(n for n, _, _ in no_closes)
    print()
    print("%d bucket(s) with no closes yet (%d instances): %s" % (
        len(no_closes), total_nc,
        " ".join(k for _, k, _ in no_closes[:8]) +
        (" ..." if len(no_closes) > 8 else "")))

print()
print("total buckets: %d" % (len(rows_out) + len(no_closes)))
