#!/usr/bin/env python3
"""streak_writeback.py — sustained → kernel streak correction (v2).

bpftool expects space-separated hex bytes (not continuous hex string),
and outputs JSON.  This version parses the JSON, reconstructs the
1016-byte struct from it, patches ONLY the bad_streak/null_streak bytes
(per algorithm slot), and writes back via `bpftool map update`.
"""

import subprocess
import sys
import json
import os
import time
import struct
import ipaddress
from collections import defaultdict

RATE_HIST_BINS        = 32
NUM_TCP_CONG_ALGS     = 16
NUM_TCP_CONN_METRICS  = NUM_TCP_CONG_ALGS

OFF_METRIC_BAD_STREAK   = 42
OFF_METRIC_NULL_STREAK  = 43
SIZEOF_TCP_CONN_METRIC  = 48

OFF_RATE_HIST      = 112
SIZEOF_RATE_HIST   = 4 * RATE_HIST_BINS + 8
OFF_METRICS_ARRAY  = OFF_RATE_HIST + SIZEOF_RATE_HIST
SIZEOF_REMOTE_HOST = OFF_METRICS_ARRAY + (NUM_TCP_CONN_METRICS * SIZEOF_TCP_CONN_METRIC)

WRITEBACK_WINDOW = 8

# Throttle: skip if called again within this many seconds.
WB_THROTTLE_FILE = '/tmp/.bpftune_writeback_last'
WB_THROTTLE_SEC  = 300   # 5 minutes
# Persisted state: max swap ts we've already written back.
# Stored in the same file as the throttle mtime.


# field order must match struct remote_host in tcp_conn_tuner.h
_PREFIX_FIELDS = ('min_rtt', 'max_rate_delivered', 'instances', 'selection_count',
                  'best_i', 'best_v', 'second_i', 'second_v',
                  'rate_best_i', 'rate_best_v', 'rate_second_i', 'rate_second_v',
                  'rtt_low_streak', 'rtt_low_min')

_METRIC_FIELDS_U64 = ('state_flags', 'greedy_count', 'metric_count', 'metric_value')
_METRIC_FIELDS_U16 = ('sockets_alive', 'sockets_good', 'sockets_proved',
                      'rate_ema', 'swap_score')


def _key_args_for(ip_str):
    """Return list of bpftool hex byte args (16 strings) for the map key."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except (ValueError, TypeError):
        return None
    if isinstance(ip, ipaddress.IPv4Address):
        raw = b'\x00' * 10 + b'\xff\xff' + ip.packed
    else:
        raw = ip.packed
    return [f'{b:02x}' for b in raw]


def _bytes_to_args(buf):
    return [f'{b:02x}' for b in buf]


def _value_json_to_bytes(v):
    """Convert JSON value dict to 1016 raw bytes (BPF struct layout)."""
    buf = bytearray()
    for f in _PREFIX_FIELDS:
        buf.extend(struct.pack('<Q', int(v.get(f, 0) or 0)))
    rate = v.get('rate', {}) or {}
    bins = rate.get('bins', []) or []
    for i in range(RATE_HIST_BINS):
        buf.extend(struct.pack('<I', int(bins[i]) if i < len(bins) else 0))
    buf.extend(struct.pack('<Q', int(rate.get('total', 0) or 0)))
    metrics = v.get('metrics', []) or []
    for i in range(NUM_TCP_CONN_METRICS):
        m = metrics[i] if i < len(metrics) else {}
        for f in _METRIC_FIELDS_U64:
            buf.extend(struct.pack('<Q', int(m.get(f, 0) or 0)))
        for f in _METRIC_FIELDS_U16:
            buf.extend(struct.pack('<H', int(m.get(f, 0) or 0)))
        buf.extend(struct.pack('<B', int(m.get('bad_streak', 0) or 0)))
        buf.extend(struct.pack('<B', int(m.get('null_streak', 0) or 0)))
        buf.extend(b'\x00\x00\x00\x00')  # 4 bytes padding to 8-align next entry
    return bytes(buf)


def _patch_streaks(buf, alg_idx, bad, null):
    base = OFF_METRICS_ARRAY + (alg_idx * SIZEOF_TCP_CONN_METRIC)
    buf[base + OFF_METRIC_BAD_STREAK]  = bad  & 0xff
    buf[base + OFF_METRIC_NULL_STREAK] = null & 0xff


def _streaks_from_history(sustained_outcomes):
    bad = 0; null = 0
    for outcome in sustained_outcomes:
        if outcome == 'win':
            bad = 0; null = 0
        elif outcome == 'loss':
            bad += 1; null = 0
        else:
            null += 1
    if bad  > 255: bad  = 255
    if null > 255: null = 255
    return bad, null


def _resolve_remote_host_map_id():
    try:
        out = subprocess.check_output(['bpftool', 'map', 'show'], text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    for line in out.splitlines():
        if 'remote_host' in line and ':' in line:
            try:
                return int(line.split(':')[0].strip())
            except ValueError:
                continue
    return None


def _map_lookup_json(map_id, key_args):
    """Lookup one entry; return value as JSON dict, or None if missing."""
    try:
        out = subprocess.check_output([
            'bpftool', 'map', 'lookup', 'id', str(map_id), 'key', 'hex',
        ] + key_args, text=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError:
        return None
    try:
        return json.loads(out).get('value')
    except json.JSONDecodeError:
        return None


def _map_update_bytes(map_id, key_args, value_args):
    r = subprocess.run([
        'bpftool', 'map', 'update', 'id', str(map_id),
        'key', 'hex',
    ] + key_args + ['value', 'hex'] + value_args,
    capture_output=True, text=True)
    if r.returncode != 0:
        print(f'[writeback] WARN map update failed: {r.stderr.strip()}', file=sys.stderr)
    return r.returncode == 0


def writeback_streaks(swaps):
    # Throttle: cheap stat call.  Skip if we ran within WB_THROTTLE_SEC.
    try:
        if os.path.exists(WB_THROTTLE_FILE):
            age = time.time() - os.path.getmtime(WB_THROTTLE_FILE)
            if age < WB_THROTTLE_SEC:
                return  # too soon, silent skip
    except OSError:
        pass

    map_id = _resolve_remote_host_map_id()
    if map_id is None:
        return  # silent skip — bpftool unavailable

    # Read last-processed swap ts (stored in the throttle file content)
    last_ts = 0.0
    try:
        with open(WB_THROTTLE_FILE, 'r') as f:
            last_ts = float(f.read().strip() or 0)
    except (OSError, ValueError):
        pass

    # Only process swaps with NEW sustained outcomes (ts > last_ts)
    new_swaps = [s for s in swaps
                 if s.get('outcome_sustained')
                 and s.get('ts', 0) > last_ts]
    if not new_swaps:
        # No new work — touch throttle file so we don't re-check too often
        try:
            os.utime(WB_THROTTLE_FILE, None)
        except OSError:
            try: open(WB_THROTTLE_FILE, 'w').close()
            except OSError: pass
        return

    # Group NEW swaps by host (lookups only for hosts with new work)
    by_host = defaultdict(list)
    skipped_no_ip = 0
    for s in new_swaps:
        rh = s.get('remote_host')
        if not rh:
            skipped_no_ip += 1
            continue
        by_host[rh].append(s)

    if skipped_no_ip:
        print(f'[writeback] {skipped_no_ip} new swaps skipped (no remote_host)',
              file=sys.stderr)

    total_hosts_patched = 0
    total_slots_patched = 0

    for host, host_new_swaps in by_host.items():
        key_args = _key_args_for(host)
        if not key_args:
            continue

        # ONE targeted lookup per host (not a full map dump)
        value_json = _map_lookup_json(map_id, key_args)
        if not value_json:
            continue  # host not in BPF map (no kernel state)

        buf = bytearray(_value_json_to_bytes(value_json))
        if len(buf) != SIZEOF_REMOTE_HOST:
            print(f'[writeback] WARN: {host} buf size {len(buf)} != '
                  f'{SIZEOF_REMOTE_HOST}; skip', file=sys.stderr)
            continue

        # For each alg with new swaps, recompute streak from FULL
        # swaps_list history (in-memory, fast — no bpftool calls)
        new_algs = set(s.get('to_alg') for s in host_new_swaps
                       if s.get('to_alg') is not None)
        patches_applied = 0
        for alg_idx in new_algs:
            recent = sorted(
                [s for s in swaps  # FULL swaps list — for history
                 if s.get('remote_host') == host
                 and s.get('to_alg') == alg_idx
                 and s.get('outcome_sustained')],
                key=lambda x: x.get('ts', 0)
            )[-WRITEBACK_WINDOW:]
            if not recent:
                continue
            outcomes = [s.get('outcome_sustained') for s in recent]
            bad, null = _streaks_from_history(outcomes)
            _patch_streaks(buf, alg_idx, bad, null)
            patches_applied += 1

        if patches_applied == 0:
            continue

        # ONE targeted update per host
        value_args = _bytes_to_args(bytes(buf))
        if _map_update_bytes(map_id, key_args, value_args):
            total_hosts_patched += 1
            total_slots_patched += patches_applied
            print(f'[writeback] {host}: patched {patches_applied} alg slots',
                  file=sys.stderr)

    print(f'[writeback] done: {total_hosts_patched} hosts, '
          f'{total_slots_patched} algorithm slots corrected '
          f'(processed {len(new_swaps)} new swaps)',
          file=sys.stderr)

    # Persist the max ts we've now processed (also updates mtime,
    # which is what the throttle check at the top reads).
    max_ts = max(s.get('ts', 0) for s in new_swaps)
    try:
        with open(WB_THROTTLE_FILE, 'w') as f:
            f.write(str(max_ts))
    except OSError:
        pass


if __name__ == '__main__':
    mid = _resolve_remote_host_map_id()
    if mid is None:
        print('FAIL: no remote_host map found'); sys.exit(1)
    print(f'OK: found remote_host map id={mid}')

    # Dump all entries, find an IPv4-mapped one, verify lookup + size
    out = subprocess.run(['bpftool', 'map', 'dump', 'id', str(mid)],
                         capture_output=True, text=True)
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        print('FAIL: could not parse map dump as JSON')
        sys.exit(1)

    print(f'Map has {len(data)} entries')

    def _extract_addr8(key_obj):
        if isinstance(key_obj, list):
            return [int(x) for x in key_obj]
        if isinstance(key_obj, dict):
            in6 = key_obj.get('in6_u', {}) or {}
            return in6.get('u6_addr8', []) or []
        return []
    checked = 0
    for entry in data:
        addr8 = _extract_addr8(entry.get('key', {}))
        if len(addr8) != 16:
            continue
        if addr8[10] == 255 and addr8[11] == 255 and any(addr8[12:16]):
            ip = '.'.join(str(b) for b in addr8[12:16])
            key_args = _key_args_for(ip)
            v_json = _map_lookup_json(mid, key_args)
            if v_json:
                buf = _value_json_to_bytes(v_json)
                actual = len(buf)
                status = 'PASS' if actual == SIZEOF_REMOTE_HOST else 'FAIL'
                print(f'  {ip}: lookup OK, value {actual} bytes [{status}]')
                checked += 1
                if checked >= 3:
                    break

    if checked == 0:
        print('NOTE: no IPv4-mapped entries verified (map may be empty)')
