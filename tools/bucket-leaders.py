#!/usr/bin/env python3
"""Show per-destination bucket leaders from bpftune's remote_host_map.

Under destination-keying (0.4.18+), each bucket represents one remote
destination address.  Shows the top-3 algorithms per bucket (best first)
so you can see whether the tuner has converged on a single winner or
settled on a small tie.

Verdict thresholds (top-1 metric_value vs top-2 metric_value):
    ALONE   - only one algorithm has samples
    CLEAR   - leader is >=2x better than next       (converged)
    LEAD    - leader is 1.2-2x better than next     (preference)
    TIGHT   - leader is <1.2x better than next      (bouncing)

Usage:  sudo python3 tools/bucket-leaders.py
"""
import ipaddress, json, subprocess, sys

names = ['cubic','bbr','htcp','dctcp','scalable','vegas','veno','westwood',
         'reno','illinois','yeah','lp','bic','highspeed','hybla','nv']

try:
    raw = subprocess.check_output(
        ['sudo','bpftool','map','dump','name','remote_host_map'],
        stderr=subprocess.DEVNULL)
except (subprocess.CalledProcessError, FileNotFoundError) as e:
    sys.exit("bpftool failed: %s" % e)

data = json.loads(raw)

# Normalize across bpftool versions:
#   old (<=7.4):  [ { "key": ..., "value": ... }, ... ]
#   new (>=7.5):  [ { "id":..., "type":..., "elements": [ {key,value}, ... ] } ]
entries = []
for top in data:
    if 'elements' in top:
        entries.extend(top['elements'])
    elif 'key' in top and 'value' in top:
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

rows_out = []
for entry in entries:
    if 'key' not in entry or 'value' not in entry:
        continue
    v = entry['value']
    if v['instances'] == 0:
        continue
    key = fmt_key(entry['key']['in6_u']['u6_addr8'])
    alg_rows = []
    for i, m in enumerate(v['metrics']):
        if m['metric_count'] == 0:
            continue
        alg_rows.append((m['metric_value'], m['metric_count'], m['greedy_count'], names[i]))

    if not alg_rows:
        rows_out.append({'key': key, 'inst': v['instances'],
                         'mrtt': v['min_rtt'], 'mrate': v['max_rate_delivered'],
                         'top3': '(none)', 'verdict': 'no closes yet'})
        continue

    alg_rows.sort()
    top3 = alg_rows[:3]
    top3_str = ' '.join('%s=%s' % (nm, fmt_val(mv)) for mv, n, g, nm in top3)

    if len(alg_rows) >= 2 and alg_rows[0][0] > 0:
        ratio = alg_rows[1][0] / float(alg_rows[0][0])
        verdict = "CLEAR" if ratio >= 2.0 else ("LEAD" if ratio >= 1.2 else "TIGHT")
        verdict = "%s (x%.1f)" % (verdict, ratio)
    else:
        verdict = "ALONE"

    rows_out.append({'key': key, 'inst': v['instances'],
                     'mrtt': v['min_rtt'], 'mrate': v['max_rate_delivered'],
                     'top3': top3_str, 'verdict': verdict})

rows_out.sort(key=lambda r: -r['inst'])

w_key  = max([len("destination")] + [len(r['key']) for r in rows_out])
w_top3 = max([len("top-3 (best first)")] + [len(r['top3']) for r in rows_out])

hdr = ("%-" + str(w_key) + "s  %-9s  %-7s  %-8s  %-" + str(w_top3) + "s  %s") % (
    "destination", "instances", "min_rtt", "max_rate",
    "top-3 (best first)", "verdict")
print(hdr)
print("-" * len(hdr))

for r in rows_out:
    print(("%-" + str(w_key) + "s  %-9d  %-7d  %-8s  %-" + str(w_top3) + "s  %s") % (
        r['key'], r['inst'], r['mrtt'], fmt_val(r['mrate']), r['top3'], r['verdict']))

print()
print("total buckets: %d" % len(rows_out))
