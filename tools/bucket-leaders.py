#!/usr/bin/env python3
"""Show per-destination bucket leaders from bpftune's remote_host_map.

Under destination-keying (0.4.18+), each bucket represents one remote
destination address. Displays instances, RTT reference, rate reference,
the winning algorithm, and how decisively it leads.

Verdict thresholds (leader_val vs next-best metric_value):
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
        rows_out.append((v['instances'], key, v['min_rtt'], v['max_rate_delivered'],
                         "(none)", 0, "no closes yet"))
        continue

    alg_rows.sort()
    lead_mv, lead_n, lead_g, lead_nm = alg_rows[0]

    if len(alg_rows) >= 2 and lead_mv > 0:
        ratio = alg_rows[1][0] / float(lead_mv)
        verdict = "CLEAR" if ratio >= 2.0 else ("LEAD" if ratio >= 1.2 else "TIGHT")
        verdict = "%s (x%.1f)" % (verdict, ratio)
    else:
        verdict = "ALONE"

    rows_out.append((v['instances'], key, v['min_rtt'], v['max_rate_delivered'],
                     lead_nm, lead_mv, verdict))

rows_out.sort(reverse=True)

# Compute destination column width dynamically so long IPv6 addresses
# (up to 39 chars compressed) don't push the rest of the row around.
maxcol = len("destination")
for _, key, *_rest in rows_out:
    if len(key) > maxcol:
        maxcol = len(key)

fmt_head = "%-" + str(maxcol) + "s %-9s %-8s %-10s %-10s %-10s %s"
fmt_data = "%-" + str(maxcol) + "s %-9d %-8d %-10d %-10s %-10d %s"

header = fmt_head % ("destination", "instances", "min_rtt", "max_rate",
                     "leader", "leader_val", "verdict")
print(header)
print("-" * len(header))

for inst, key, mrtt, mrate, lname, lval, verd in rows_out:
    print(fmt_data % (key, inst, mrtt, mrate, lname, lval, verd))

print()
print("total buckets: %d" % len(rows_out))
