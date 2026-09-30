#!/usr/bin/env python3
"""bpftune live dashboard -- entry point.

--json : single JSON object (used by the collector).
"""
import os, sys
_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)
import argparse
import ipaddress, csv, io, json, os, re, socket, struct, subprocess, sys, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TypedDict, Union

from bpftune_log import *
from bpftune_data import *
import bpftune_log, bpftune_data  # for vars() re-export below
globals().update({k: v for k, v in vars(bpftune_log).items() if not k.startswith("__")})
globals().update({k: v for k, v in vars(bpftune_data).items() if not k.startswith("__")})

def _safe(fn, default, label):
    """Run fn(); on exception, log to stderr and return default.

    Keeps one bad panel from taking down the whole dashboard: if
    data_metric(hosts) throws because the BPF map returned malformed
    data, that panel renders empty but the other 20 still work."""
    try:
        return fn()
    except Exception as e:
        import sys
        print(f"[collect_all] {label}: {e}", file=sys.stderr)
        return default



def collect_all_incremental(offsets=None):
    """Like collect_all() but reads only new log lines since last call.

    In daemon mode, the collector holds the offsets dict in memory
    and passes it each cycle.  First call (empty offsets) reads the
    full tail so the first cycle has data.

    The _swaps_mets_srates cache is cleared each call because the
    text changes (new lines appended) — but we only parse the NEW
    lines, not the full 2MB.
    """
    logpath = find_log()
    hosts   = read_map()
    text, _new_offsets = tail_incremental(offsets)
    # Clear the _swaps_mets_srates cache — text changed
    bpftune_log._SWMS_CACHE = {}
    _run_writeback_and_get_swaps(text)
    hosts   = read_map()
    return _build_result(logpath, text, hosts)


def collect_all() -> CollectAllResult:
    """Build the full dashboard state dict consumed by index.html.

    Single entry point for all dashboard data.  Returns a dict with keys:
      generated_ts:int, log_window:dict{oldest_ts,newest_ts,span_min,swap_count,age_min},
      bucket_ips:dict{bucket_str:[ip_str,...]}, now_mono:float, hostname:str,
      build:dict{version,dash_version,service,uptime_min,started_utc,log_path},
      system:dict{kernel,default_cc,cpu_count,load_1/5/15,procs_running,procs_total,
                  host_uptime_s,mem_total_bytes,mem_avail_bytes,mem_used_bytes,mem_used_pct},
      tunables:list[{group,items:[{key,value}]}],
      buckets:list[{dest,inst,rtt_us,ref_mbps,best_alg,n_alg}] (top 8),
      metric:list[{alg,metric,votes,alive,rate_ema,swap_score,penalty,score,bad_streak,null_streak,active}],
      metric_by_bucket:dict{bucket_str:[same as metric]},
      bucket_live:dict{bucket_str:{ts:[int],cols:{col:[[ts,val],...]}}},
      live_leaders:list[{dest,inst,top:[{alg,weighted,rate_ema,swap_score,bad,null,count}]}],
      proof:list[{alg,good,proved,proven_max,sampled_avg,sampled_max,samples}],
      rate:list[{thr,n,mean,min,max}],
      swap_outcomes:dict{composite,srate,sustained (each a _finalize_outcome dict
                        with measurable,unmeasurable,win,win_pct,null,null_pct,loss,
                        loss_pct,rescued,full_loss,open,*_pct), swaps_list:[{dest,outcome,outcome_sustained,cookie,ts}]},
      divergence:list[{category,measured,win_pct,null_pct,loss_pct,..._srate,..._sustained,...}],
      churn:dict{cookies,one,mid,many,max},
      recent_swaps:list[{boot_ts,from_alg,to_alg,d,outcome,outcome_srate,outcome_sustained,mt_alg,rb_alg,dest,_bucket}],
      recent_swaps_by_bucket:dict{bucket_str:[same as recent_swaps]} (up to 16/bucket),
      recent_proofs:list[{boot_ts,alg,mbps,tier,dest}],
      proofs_raw:list[{alg,dest,rate,tier}],
      rate_raw:list[{dest,thr,srate}].

    Side effect: calls _run_writeback_and_get_swaps(text) before reading
    the BPF map, so streak corrections are visible to the leaderboard.
    """
    logpath = find_log()
    hosts   = read_map()
    text    = tail_recent()
    # Run the writeback FIRST (corrects BPF map streaks before leaderboard reads them)
    _run_writeback_and_get_swaps(text)
    # Re-read the map (to get corrected streaks)
    hosts   = read_map()
    return _build_result(logpath, text, hosts)


def _build_result(logpath, text, hosts):
    """Build the collect_all() return dict. Shared by collect_all and collect_all_incremental."""
    return {
        "generated_ts":   int(time.time()),
        "log_window":     _safe(lambda: _log_window(text), {"oldest_ts":0,"newest_ts":0,"span_min":0,"swap_count":0,"age_min":0}, "log_window"),
        "bucket_ips":     _safe(lambda: data_bucket_ips(text), {}, "bucket_ips"),
        "now_mono":       _uptime_now(),
        "hostname":       os.uname().nodename,
        "build":          _safe(lambda: data_build(logpath), {"version":"?","dash_version":"?","service":"?","uptime_min":None,"started_utc":"","log_path":None}, "build"),
        "system":         _safe(data_system, {}, "system"),
        "tunables":       _safe(data_tunables, [], "tunables"),
        "buckets":        _safe(lambda: data_buckets(hosts), [], "buckets"),
        "metric":         _safe(lambda: data_metric(hosts), [], "metric"),
        "metric_by_bucket": _safe(lambda: data_metric_by_bucket(hosts), {}, "metric_by_bucket"),
        "bucket_live":      _safe(data_bucket_live, {}, "bucket_live"),
        "live_leaders":   _safe(lambda: data_live_leaders(hosts), [], "live_leaders"),
        "proof":          _safe(lambda: data_proof(text), [], "proof"),
        "rate":           _safe(lambda: data_rate(text), [], "rate"),
        "swap_outcomes":  _safe(lambda: data_swap_outcomes(text), {"composite":{"measurable":0,"unmeasurable":0,"win":0,"win_pct":0,"null":0,"null_pct":0,"loss":0,"loss_pct":0},"srate":{"measurable":0,"unmeasurable":0,"win":0,"win_pct":0,"null":0,"null_pct":0,"loss":0,"loss_pct":0},"sustained":{"measurable":0,"unmeasurable":0,"win":0,"win_pct":0,"null":0,"null_pct":0,"loss":0,"loss_pct":0},"swaps_list":[]}, "swap_outcomes"),
        "divergence":     _safe(lambda: data_divergence(text), [], "divergence"),
        "churn":          _safe(lambda: data_churn(text), {"cookies":0,"one":0,"mid":0,"many":0,"max":0}, "churn"),
        "recent_swaps":   _safe(lambda: data_recent_swaps(text), [], "recent_swaps"),
        "recent_swaps_by_bucket": _safe(lambda: data_recent_swaps_by_bucket(text), {}, "recent_swaps_by_bucket"),
        "recent_proofs":  _safe(lambda: data_recent_proofs(text), [], "recent_proofs"),
        "proofs_raw":     _safe(lambda: _proofs_with_dest(text), [], "proofs_raw"),
        "rate_raw":       _safe(lambda: _rate_samples_with_dest(text), [], "rate_raw"),
    }


def collect_lightweight(offsets, map_raw=None, base_result=None):
    """Lightweight 30s collection: BPF map + incremental log parsing.

    Reads only NEW log lines (via tail_incremental), parses new
    swaps/proofs/srates from them, merges with base_result (last
    5-min full collection).  Updates:
      - buckets, build, system, generated_ts (from BPF map)
      - recent_swaps, recent_proofs (append new, cap at 20/16)
      - swap_outcomes.swaps_list (append new, cap at 2000)
      - swap_outcomes counts (recompute from merged swaps_list)
      - churn, log_window, proofs_raw, rate_raw
    Does NOT recompute: proof (leaderboard), rate (progression),
    divergence, metric, metric_by_bucket, bucket_live, live_leaders,
    tunables — these stay from the 5-min full collection.
    """
    # Read new log lines (typically 10-50 lines, not 2MB)
    text, _new_offsets = tail_incremental(offsets)
    # Clear the _swaps_mets_srates cache — text changed
    bpftune_log._SWMS_CACHE = {}

    # Start from base_result (last full collection) or empty
    doc = dict(base_result) if base_result else {}

    # Update BPF map data
    hosts = read_map()
    logpath = find_log()
    doc['generated_ts'] = int(time.time())
    doc['hostname'] = os.uname().nodename
    doc['now_mono'] = _uptime_now()
    doc['buckets'] = _safe(lambda: data_buckets(hosts), [], 'lw_buckets')
    doc['metric_by_bucket'] = _safe(lambda: data_metric_by_bucket(hosts), {}, 'lw_metric_by_bucket')
    doc['rate'] = _safe(lambda: data_rate(text), [], 'lw_rate')
    doc['build'] = _safe(lambda: data_build(logpath), {}, 'lw_build')
    doc['system'] = _safe(data_system, {}, 'lw_system')

    if text:
        # Parse new swaps/proofs from incremental log lines
        new_swaps = _safe(lambda: data_recent_swaps(text), [], 'lw_recent_swaps')
        new_proofs = _safe(lambda: data_recent_proofs(text), [], 'lw_recent_proofs')
        new_swap_outcomes = _safe(lambda: data_swap_outcomes(text),
            {'composite':{'measurable':0,'unmeasurable':0,'win':0,'win_pct':0,'null':0,'null_pct':0,'loss':0,'loss_pct':0},
             'srate':{'measurable':0,'unmeasurable':0,'win':0,'win_pct':0,'null':0,'null_pct':0,'loss':0,'loss_pct':0},
             'sustained':{'measurable':0,'unmeasurable':0,'win':0,'win_pct':0,'null':0,'null_pct':0,'loss':0,'loss_pct':0},
             'swaps_list':[]}, 'lw_swap_outcomes')
        new_churn = _safe(lambda: data_churn(text),
            {'cookies':0,'one':0,'mid':0,'many':0,'max':0}, 'lw_churn')
        new_proofs_raw = _safe(lambda: _proofs_with_dest(text), [], 'lw_proofs_raw')
        new_rate_raw = _safe(lambda: _rate_samples_with_dest(text), [], 'lw_rate_raw')
        new_log_window = _safe(lambda: _log_window(text),
            {'oldest_ts':0,'newest_ts':0,'span_min':0,'swap_count':0,'age_min':0}, 'lw_log_window')
        new_bucket_ips = _safe(lambda: data_bucket_ips(text), {}, 'lw_bucket_ips')

        # Merge: append new items to base, cap sizes
        if base_result:
            # 0.4.88: dedupe + sort newest-first for time-series lists.
            # The previous merge (old + new)[-N:] produced duplicates
            # when the incremental log contained the same events as the
            # previous full collect (offsets not advanced yet) AND was
            # in oldest-first order while the JS expected newest-first.
            # Now: dedupe by (boot_ts, from_alg, to_alg, d) for swaps,
            # (boot_ts, alg) for proofs, then sort by boot_ts desc.
            def _merge_dedupe_sort(old, new, max_n, key_fn, ts_field='boot_ts'):
                merged = list(old) + list(new)
                seen = set()
                deduped = []
                for item in merged:
                    k = key_fn(item)
                    if k in seen:
                        continue
                    seen.add(k)
                    deduped.append(item)
                deduped.sort(key=lambda x: x.get(ts_field) or 0, reverse=True)
                return deduped[:max_n]

            def _swap_key(s):
                return (s.get('boot_ts'), s.get('from_alg'),
                        s.get('to_alg'), s.get('d'))
            def _proof_key(p):
                return (p.get('boot_ts'), p.get('alg'), p.get('mbps'))

            # Recent swaps: dedupe + sort newest-first, keep last 20
            old_swaps = base_result.get('recent_swaps', [])
            doc['recent_swaps'] = _merge_dedupe_sort(
                old_swaps, new_swaps, 32, _swap_key, 'boot_ts')

            # 0.4.86: rebuild recent_swaps_by_bucket from the merged list
            # so per-bucket recent swaps stay fresh between 5min full collects.
            # Without this, the per-bucket panel goes stale for up to 5min
            # while the flat recent_swaps list stays fresh — mismatch.
            if doc.get('recent_swaps'):
                _by = {}
                # doc['recent_swaps'] is now newest-first, so iterate in
                # forward order to fill each bucket's list newest-first
                # (matching the panel's expected ordering).
                for _r in doc['recent_swaps']:
                    _b = _r.get('dest') or _r.get('_bucket') or ''
                    if not _b:
                        continue
                    _lst = _by.setdefault(_b, [])
                    if len(_lst) < 16:
                        _lst.append({k: v for k, v in _r.items() if k != '_bucket'})
                doc['recent_swaps_by_bucket'] = _by

            # Recent proofs: dedupe + sort newest-first, keep last 16
            old_proofs = base_result.get('recent_proofs', [])
            doc['recent_proofs'] = _merge_dedupe_sort(
                old_proofs, new_proofs, 16, _proof_key, 'boot_ts')

            # Swap outcomes: dedupe swaps_list by (ts, cookie), keep last 2000
            old_so = base_result.get('swap_outcomes', {})
            old_swaps_list = old_so.get('swaps_list', [])
            new_swaps_list = new_swap_outcomes.get('swaps_list', [])
            def _swaps_list_key(s):
                return (s.get('ts'), s.get('cookie'))
            merged_swaps_list = _merge_dedupe_sort(
                old_swaps_list, new_swaps_list, 2000, _swaps_list_key, 'ts')

            # Recompute outcome counts from merged swaps_list
            # (just re-run data_swap_outcomes on the merged list)
            doc['swap_outcomes'] = new_swap_outcomes
            doc['swap_outcomes']['swaps_list'] = merged_swaps_list

            # Churn: use new (recomputed from new log lines)
            doc['churn'] = new_churn

            # Proofs/rate raw: dedupe by ts, keep last 500
            old_proofs_raw = base_result.get('proofs_raw', [])
            doc['proofs_raw'] = _merge_dedupe_sort(
                old_proofs_raw, new_proofs_raw, 500,
                lambda p: (p.get('ts'), p.get('alg')), 'ts')
            old_rate_raw = base_result.get('rate_raw', [])
            doc['rate_raw'] = _merge_dedupe_sort(
                old_rate_raw, new_rate_raw, 500,
                lambda r: (r.get('ts'), r.get('thr')), 'ts')

            # Bucket IPs: merge
            old_bucket_ips = base_result.get('bucket_ips', {})
            merged_bucket_ips = dict(old_bucket_ips)
            for k, v in new_bucket_ips.items():
                if k in merged_bucket_ips:
                    for ip in v:
                        if ip not in merged_bucket_ips[k]:
                            merged_bucket_ips[k].append(ip)
                else:
                    merged_bucket_ips[k] = v
            doc['bucket_ips'] = merged_bucket_ips
        else:
            # No base result — just use new data
            doc['recent_swaps'] = new_swaps
            doc['recent_proofs'] = new_proofs
            doc['swap_outcomes'] = new_swap_outcomes
            doc['churn'] = new_churn
            doc['proofs_raw'] = new_proofs_raw
            doc['rate_raw'] = new_rate_raw
            doc['bucket_ips'] = new_bucket_ips

        # Log window: use new (has fresh timestamps)
        doc['log_window'] = new_log_window
    else:
        # No new log lines — keep base log_window
        doc['log_window'] = doc.get('log_window',
            {'oldest_ts':0,'newest_ts':0,'span_min':0,'swap_count':0,'age_min':0})

    return doc


# ---------- text renderer ----------


def _twocol(title_l, lines_l, title_r, lines_r):
    out = [f"{ARROW} {title_l:<{CW-2}}{GAP}{ARROW} {title_r}",
           f"{RULE*CW}{GAP}{RULE*CW}"]
    rows = max(len(lines_l), len(lines_r))
    for i in range(rows):
        l = lines_l[i] if i < len(lines_l) else ""
        r = lines_r[i] if i < len(lines_r) else ""
        out.append(f"{l[:CW]:<{CW}}{GAP}{r[:CW]}")
    return out



def _full(title, lines):
    out = [f"{ARROW} {title}", RULE*FULL]
    out += [l[:FULL] for l in lines]
    return out



def _system_lines(s):
    rows = []
    rows.append(f"kernel       {s.get('kernel','')}")
    rows.append(f"default CC   {s.get('default_cc','')}")
    if s.get("cpu_count") is not None:
        rows.append(f"cpu cores    {s['cpu_count']}")
    if s.get("load_1") is not None:
        rows.append("load avg     %.2f %.2f %.2f"
                    % (s["load_1"], s["load_5"], s["load_15"]))
    if s.get("procs_total") is not None:
        rows.append("processes    %s running / %s total"
                    % (s.get("procs_running", 0), s["procs_total"]))
    if s.get("host_uptime_s") is not None:
        dt = s["host_uptime_s"]
        dd, r = divmod(dt, 86400)
        hh, r = divmod(r, 3600)
        mm = r // 60
        rows.append("host uptime  %s%dh %dm" % (("%dd " % dd) if dd else "",
                                                hh, mm))
    if s.get("mem_total_bytes"):
        used = s.get("mem_used_bytes", 0)
        tot  = s["mem_total_bytes"]
        rows.append("memory       %.1f / %.1f GB  (%.0f%%)"
                    % (used / 1e9, tot / 1e9, s.get("mem_used_pct", 0)))
    return rows



def _tun_lines(tunables):
    out = []
    for g in tunables:
        out.append(f"  [{g['group']}]")
        for it in g["items"]:
            out.append(f"    {it['key']:<28} {it['value']}")
    if not out:
        out = ["  (none seen in journal for this boot)"]
    return out



def _outcome_lines(so, label):
    return [
        f"  {label}",
        f"    measurable    {so['measurable']:>4}  (unmeasurable {so['unmeasurable']})",
        f"    win           {so['win']:>4}  {so['win_pct']:.0f}%",
        f"    null          {so['null']:>4}  {so['null_pct']:.0f}%",
        f"    loss          {so['loss']:>4}  {so['loss_pct']:.0f}%",
    ]



def render_text(d):
    b = d["build"]
    lines = []
    lines.append(f"version    {b['version']}   service  {b['service']}")
    if b["uptime_min"] is not None:
        h, m = divmod(b["uptime_min"], 60)
        lines.append(f"uptime     {h}h {m}m   (started {b['started_utc']} UTC)")
    if b.get("dash_version"):
        lines[0] = lines[0] + f"   dashboard {b['dash_version']}"
    lines.append(f"log        {b['log_path'] or '(not found)'}")
    log_lines = lines

    print(DRULE * (FULL + 2))
    ts = datetime.fromtimestamp(d["generated_ts"], timezone.utc)\
                .strftime('%Y-%m-%dT%H:%M:%SZ')
    print(f"  bpftune  {MIDDOT}  {d['hostname']}  {MIDDOT}  {ts}")
    print(DRULE * (FULL + 2))
    print()
    for row in _twocol("BUILD / SERVICE", log_lines,
                       "SYSTEM FACTS",    _system_lines(d["system"])):
        print(row)
    print()
    for row in _full("BPFTUNE-MANAGED TUNABLES", _tun_lines(d["tunables"])):
        print(row)
    print()
    bk = [f"  {'dest':<16}{'inst':>6}{'rtt_us':>9}{'ref Mb/s':>10}"
          f"{'best_i':>10}{'n_alg':>7}"]
    for r in d["buckets"]:
        bk.append(f"  {r['dest']:<16}{r['inst']:>6}{r['rtt_us']:>9}"
                  f"{r['ref_mbps']:>10.1f}{r['best_alg']:>10}{r['n_alg']:>7}")
    for row in _full("TOP DESTINATION BUCKETS", bk):
        print(row)
    print()
    # Swap target picker: score = rate_ema * ss/256 * penalty.
    mt = [f"  {'alg':<10}{'metric':>9}{'re':>6}{'ss':>6}"
          f"{'pen':>6}{'score':>9}{'bad':>5}{'null':>6}"]
    for r in d["metric"]:
        pen = r.get("penalty", 1.0)
        pen_s = f"{pen:.2f}"
        mt.append(f"  {r['alg']:<10}{r['metric']:>9.1f}"
                  f"{r.get('rate_ema',0):>6}"
                  f"{r.get('swap_score',0):>6}"
                  f"{pen_s:>6}"
                  f"{r.get('score',0):>9.1f}"
                  f"{r.get('bad_streak',0):>5}"
                  f"{r.get('null_streak',0):>6}")
    pr = [f"  {'alg':<9}{'good':>5}{'prvd':>5}{'p_max':>8}"
          f"{'s_avg':>8}{'s_max':>8}{'n':>5}"]
    for r in d["proof"]:
        pm = f"{r['proven_max']:.1f}" if r["proven_max"] is not None else "-"
        sa = f"{r['sampled_avg']:.1f}" if r["sampled_avg"] is not None else "-"
        sm = f"{r['sampled_max']:.1f}" if r["sampled_max"] is not None else "-"
        n  = f"{r['samples']}" if r["samples"] is not None else "-"
        pr.append(f"  {r['alg']:<9}{r['good']:>5}{r['proved']:>5}"
                  f"{pm:>8}{sa:>8}{sm:>8}{n:>5}")
    for row in _twocol("SWAP TARGET LEADERBOARD (score = re*ss/256*pen)",
                       mt, "PROOF LEADERBOARD (speed)", pr):
        print(row)
    print()
    rt = [f"  {'thr':>7}{'n':>5}{'mean':>9}{'min':>9}{'max':>9}"]
    for r in d["rate"]:
        rt.append(f"  {r['thr']:>7}{r['n']:>5}{r['mean']:>9.1f}"
                  f"{r['min']:>9.1f}{r['max']:>9.1f}")
    so_lines = _outcome_lines(d["swap_outcomes"]["composite"], "composite") + \
               _outcome_lines(d["swap_outcomes"]["sustained"],
                              "sustained (accurate)")
    for row in _twocol("RATE PROGRESSION (client, Mb/s)", rt,
                       "SWAP OUTCOMES", so_lines):
        print(row)
    print()
    dv = [f"  {'category':<20}{'meas':>5}{'win':>7}{'null':>7}{'loss':>7}{'skipped':>9}",
          RULE*62]
    for r in d["divergence"]:
        dv.append(f"  {r['category']:<20}{r['measured']:>5}"
                  f"{r['win_pct']:>6.0f}%{r['null_pct']:>6.0f}%"
                  f"{r['loss_pct']:>6.0f}%{r['skipped']:>9}")
    for row in _full("DIVERGENCE composite", dv):
        print(row)
    print()
    dvs = [f"  {'category':<20}{'meas':>5}{'win':>7}{'null':>7}{'loss':>7}{'skipped':>9}",
           RULE*62]
    for r in d["divergence"]:
        dvs.append(f"  {r['category']:<20}{r['measured_sustained']:>5}"
                   f"{r['win_pct_sustained']:>6.0f}%"
                   f"{r['null_pct_sustained']:>6.0f}%"
                   f"{r['loss_pct_sustained']:>6.0f}%"
                   f"{r['skipped_sustained']:>9}")
    for row in _full("DIVERGENCE sustained (srate, accurate)", dvs):
        print(row)
    print()
    ch = d["churn"]
    ch_lines = [
        f"  cookies swapped     {ch['cookies']}",
        f"    1x                {ch['one']}",
        f"    2-4x              {ch['mid']}",
        f"    5x+               {ch['many']}",
        f"    max per cookie    {ch['max']}",
    ]
    rp = [f"  {r['alg']:<9} {r['mbps']:>7.1f} Mb/s   {r['tier']}"
          for r in d["recent_proofs"]] or ["  (none)"]
    for row in _twocol("COOKIE CHURN", ch_lines,
                       "RECENT PROOF EVENTS", rp):
        print(row)
    print()
    rs = []
    for r in d["recent_swaps"]:
        c = {"win": "W", "loss": "L", "null": "n"}.get(r["outcome"], "?")
        s = {"win": "W", "loss": "L", "null": "n"}.get(r["outcome_sustained"], "?")
        rs.append(f"  {r['from_alg']:>9} -> {r['to_alg']:<9} "
                  f"comp={c} sust={s} mt={r['mt_alg'] or '-':<8} "
                  f"rb={r['rb_alg'] or '-':<8}")
    if not rs:
        rs = ["  (none in tail)"]
    for row in _full("RECENT SWAPS (comp vs sustained)", rs):
        print(row)
    print()



def main():
    try:
        os.nice(19)
    except (OSError, AttributeError):
        pass
    p = argparse.ArgumentParser()
    p.add_argument("-i", "--interval", type=int, default=10)
    p.add_argument("--once", action="store_true")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    if a.json:
        json.dump(collect_all(), sys.stdout, separators=(",", ":"))
        return

    try:
        while True:
            if not a.once:
                sys.stdout.write("\x1b[2J\x1b[H")
            render_text(collect_all())
            if a.once:
                break
            print(f"  refreshing every {a.interval}s (Ctrl-C to exit)")
            time.sleep(a.interval)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()

