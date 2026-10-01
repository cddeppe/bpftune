#!/usr/bin/env python3
"""bpftune_data.py -- data transformation functions.

Imports from bpftune_log.py (base module).
"""
import argparse
import ipaddress, csv, io, json, os, re, socket, struct, subprocess, sys, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TypedDict, Union

from bpftune_log import *
import bpftune_log  # for vars() re-export below
globals().update({k: v for k, v in vars(bpftune_log).items() if not k.startswith("__")})

def data_build(logpath) -> BuildInfo:
    # 0.4.125: cache the full BuildInfo for 5 min.  Cache key includes
    # mtimes of /var/lib/dpkg/status, .git/HEAD, and the systemd unit
    # so any of those changing invalidates the cache immediately.
    import os as _os

    def _cache_key():
        try: dpkg_mtime = int(_os.path.getmtime("/var/lib/dpkg/status"))
        except OSError: dpkg_mtime = 0
        git_head_mtime = 0
        for cand in ("/root/bpftune/.git/HEAD", "/opt/bpftune/.git/HEAD",
                     "/usr/src/bpftune/.git/HEAD"):
            try:
                git_head_mtime = int(_os.path.getmtime(cand))
                break
            except OSError:
                continue
        try: svc_mtime = int(_os.path.getmtime("/etc/systemd/system/bpftune.service"))
        except OSError:
            try: svc_mtime = int(_os.path.getmtime("/lib/systemd/system/bpftune.service"))
            except OSError: svc_mtime = 0
        return "build:%d:%d:%d" % (dpkg_mtime, git_head_mtime, svc_mtime)

    def _compute():
        v = sh("dpkg-query -W -f=${Version} bpftune").strip() or "?"
        a = sh("systemctl is-active bpftune").strip() or "?"
        dash_v = "?"
        for repo in ("/root/bpftune", "/opt/bpftune", "/usr/src/bpftune"):
            try:
                dv = sh("cd %s && git rev-parse --short HEAD 2>/dev/null" % repo).strip()
                if dv:
                    dash_v = dv
                    break
            except Exception:
                continue
        ts = sh("systemctl show bpftune -p ActiveEnterTimestamp --value").strip()
        uptime_min = None
        started = ""
        m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", ts)
        if m:
            started = m.group(1)[11:19]
            try:
                t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                uptime_min = int((datetime.now() - t).total_seconds() // 60)
            except Exception:
                pass
        return {
            "version":      v,
            "dash_version": dash_v,
            "service":      a,
            "uptime_min":   uptime_min,
            "started_utc":  started,
            "log_path":     str(logpath) if logpath else None,
        }

    return cached(_cache_key(), 300, _compute)
def data_system() -> SystemInfo:
    # 0.4.125: read /proc/sys/* directly instead of spawning uname/sysctl.
    def _read_proc_sys(name):
        path = "/proc/sys/" + name.replace(".", "/")
        try:
            with open(path) as f:
                return f.read().strip()
        except (OSError, IOError):
            return ""

    kernel = cached("system_kernel", 300, lambda: _read_proc_sys("kernel.osrelease"))
    default_cc = _read_proc_sys("net.ipv4.tcp_congestion_control")
    out = {
        "kernel":     kernel,
        "default_cc": default_cc,
    }
    try:
        out["cpu_count"] = os.cpu_count()
    except Exception:
        pass
    try:
        la = os.getloadavg()
        out["load_1"] = round(la[0], 2)
        out["load_5"] = round(la[1], 2)
        out["load_15"] = round(la[2], 2)
    except Exception:
        pass
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        if len(parts) >= 4 and "/" in parts[3]:
            a, b = parts[3].split("/")
            out["procs_running"] = int(a)
            out["procs_total"] = int(b)
    except Exception:
        pass
    try:
        with open("/proc/uptime") as f:
            out["host_uptime_s"] = int(float(f.read().split()[0]))
    except Exception:
        pass
    try:
        mi = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                bits = rest.strip().split()
                if bits:
                    try:
                        mi[k] = int(bits[0]) * 1024
                    except ValueError:
                        pass
        total = mi.get("MemTotal")
        avail = mi.get("MemAvailable")
        if avail is None:
            avail = mi.get("MemFree")
        if total and avail is not None:
            out["mem_total_bytes"] = total
            out["mem_avail_bytes"] = avail
            out["mem_used_bytes"]  = total - avail
            out["mem_used_pct"]    = round(
                100.0 * (total - avail) / total, 1)
    except Exception:
        pass
    return out
def data_tunables() -> List[TunableGroup]:
    """Phase 1 (0.4.125): cached + no-subprocess version."""
    INTERESTING = [
        "net.core.netdev_budget",
        "net.core.netdev_budget_usecs",
        "net.core.rmem_default",
        "net.core.rmem_max",
        "net.core.wmem_default",
        "net.core.wmem_max",
        "net.ipv4.tcp_rmem",
        "net.ipv4.tcp_wmem",
        "net.ipv4.tcp_congestion_control",
        "net.ipv4.tcp_mtu_probing",
        "net.ipv4.tcp_slow_start_after_idle",
        "net.ipv4.tcp_no_metrics_save",
        "net.ipv4.tcp_window_scaling",
        "net.ipv4.tcp_timestamps",
        "net.ipv4.tcp_sack",
    ]
    extra_names = cached("tunables_journal_scan", 3600,
                         lambda: _scan_journal_for_tunables())
    all_names = sorted(set(INTERESTING) | set(extra_names) | set(KNOWN_TUNABLES))

    def _read_proc_sys(name):
        path = "/proc/sys/" + name.replace(".", "/")
        try:
            with open(path) as f:
                return f.read().strip()
        except (OSError, IOError):
            return None

    items = []
    for n in all_names:
        if "allowed_congestion_control" in n:
            continue
        v = _read_proc_sys(n)
        if not v:
            continue
        short = n[4:]
        items.append({"key": short, "value": v})

    def gkey(short):
        parts = short.split(".", 2)
        if len(parts) < 2:
            return short
        return parts[0] + "." + parts[1].split("_", 1)[0]

    groups = {}
    order = []
    for it in items:
        g = gkey(it["key"])
        if g not in groups:
            groups[g] = []
            order.append(g)
        groups[g].append(it)
    return [{"group": g, "items": groups[g]} for g in order]


def _scan_journal_for_tunables():
    """Phase 1: slow journal scan, but only runs once an hour (cached)."""
    try:
        j = sh_noshell(["journalctl", "-u", "bpftune", "--no-pager",
                        "-q", "--grep", r"sysctl 'net\."])
        return sorted(set(re.findall(r"sysctl '(net\.[A-Za-z0-9_.]+)'", j)))
    except Exception:
        return []
def data_buckets(hosts, n=8) -> List[BucketRow]:
    if not hosts:
        return []
    rows = []
    for inst, addr, v in hosts:
        if addr in ("0.0.0.1", "?"):
            continue
        if addr.startswith(("127.", "169.254.", "0.")):
            continue
        if inst < 2:
            continue
        mrv = v.get("max_rate_delivered", 0) or 0
        rtt = v.get("min_rtt", 0) or 0
        bi  = v.get("best_i", 0) or 0
        metrics = v.get("metrics") or []
        used = sum(1 for m in metrics if isinstance(m, dict)
                   and int(m.get("metric_count", 0) or 0) > 0)
        try:
            ref = int(mrv) / BPS_TO_MBPS
        except Exception:
            ref = 0.0
        # Compute picker's choice (same formula as data_live_leaders)
        picker_i = int(bi)
        picker_w = 0
        for mi in range(16):
            m = metrics[mi] if mi < len(metrics) and isinstance(metrics[mi], dict) else {}
            cnt = int(m.get("metric_count", 0) or 0)
            rv  = int(m.get("rate_ema", 0) or 0)
            ss  = int(m.get("swap_score", 0) or 0)
            bad = int(m.get("bad_streak", 0) or 0)
            nul = int(m.get("null_streak", 0) or 0)
            if cnt < MIN_LEADER_TRUST or rv == 0: continue
            ss_eff = ss if ss else 256
            weighted = rv * ss_eff // 256
            pen = 16 + bad * 4 + nul * 2
            weighted = weighted * 16 // pen
            if weighted > picker_w:
                picker_w = weighted
                picker_i = mi
        rows.append({
            "dest":     addr,
            "inst":     inst,
            "rtt_us":   int(rtt),
            "ref_mbps": round(ref, 1),
            "best_alg": CONGS[int(picker_i)] if int(picker_i) < 16 else str(picker_i),
            "n_alg":    used,
        })
        if len(rows) >= n:
            break
    return rows



def _vote_sum(v):
    """Total metric_count across all algs for one bucket. Used to pick
    the bucket whose leaderboard is most informative: a bucket that
    went quiet hours ago has a huge lifetime `instances` count but no
    votes, which would leave the leaderboard empty except for whichever
    alg was last tried on it."""
    metrics = v.get("metrics") or []
    total = 0
    for m in metrics:
        if isinstance(m, dict):
            try:
                total += int(m.get("metric_count", 0) or 0)
            except (TypeError, ValueError):
                pass
    return total



def data_metric(hosts) -> List[MetricRow]:
    """Swap target leaderboard.

    Sourced from the busiest bucket by total metric_count.

    The picker's score is a three-way product:

        score = rate_ema * (swap_score / 256) * penalty

    where penalty is the recent-failure decay:

        penalty = 16 / (16 + bad_streak*4 + null_streak*2)

    penalty is 1.0 when bad_streak and null_streak are both zero
    (no failures to decay), falls as streaks grow, and always stays
    positive - it's a multiplier, not a gate. The old hard exclusion
    (alg excluded if bad>=2 or null>=3) is gone. Sorted by score; the
    top row is what the picker would choose right now."""
    if not hosts:
        return []
    picked = max(hosts, key=lambda x: _vote_sum(x[2]))
    metrics = picked[2].get("metrics") or []
    rows = []
    for i in range(16):
        m = metrics[i] if i < len(metrics) and isinstance(metrics[i], dict) else {}
        try:
            val = int(m.get("metric_value", 0) or 0)
            mc  = int(m.get("metric_count", 0) or 0)
            a   = int(m.get("sockets_alive", 0) or 0)
            ss  = int(m.get("swap_score", 0) or 0)
            bs  = int(m.get("bad_streak", 0) or 0)
            ns  = int(m.get("null_streak", 0) or 0)
            re_ = int(m.get("rate_ema", 0) or 0)
        except Exception:
            val, mc, a, ss, bs, ns, re_ = 0, 0, 0, 0, 0, 0, 0
        if val in (0, (1<<64)-1):
            val = 0
        penalty = 16.0 / (16.0 + bs * 4.0 + ns * 2.0)
        score   = (re_ * (ss / 256.0)) * penalty
        # Only cells where the alg has any history.
        active = (mc > 0) or (a > 0) or (re_ > 0) or (ss > 0)
        rows.append({
            "alg":    CONGS[i],
            "metric": round(val / 1e6, 1) if val else 0,
            "votes":  mc,
            "alive":  a,
            "rate_ema":    re_,
            "swap_score":  ss,
            "penalty":     round(penalty, 3),
            "score":       round(score, 2),
            "bad_streak":  bs,
            "null_streak": ns,
            "active":      active,
        })
    # Sort: active algs first by score desc; inactive (never tried on
    # this bucket) fall to the bottom in stable order.
    rows.sort(key=lambda r: (0 if r["active"] else 1, -r["score"]))
    return rows



def data_metric_by_bucket(hosts) -> Dict[str, List[MetricRow]]:
    """Per-bucket picker leaderboard, keyed by bucket id.

    Same row shape as data_metric, but for every bucket (sorted by
    total vote count descending).  The frontend picks the entry for
    the bucket currently shown in the dropdown, so the leaderboard
    follows the selection instead of always showing the busiest."""
    if not hosts:
        return {}
    ordered = sorted(hosts, key=lambda x: -_vote_sum(x[2]))
    out = {}
    for inst, addr, v in ordered:
        if addr in ("0.0.0.1", "?"):
            continue
        if addr.startswith(("127.", "169.254.", "0.")):
            continue
        if inst < 2:
            continue
        metrics = v.get("metrics") or []
        rows = []
        for i in range(16):
            m = metrics[i] if i < len(metrics) and isinstance(metrics[i], dict) else {}
            try:
                val = int(m.get("metric_value", 0) or 0)
                mc  = int(m.get("metric_count", 0) or 0)
                a   = int(m.get("sockets_alive", 0) or 0)
                ss  = int(m.get("swap_score", 0) or 0)
                bs  = int(m.get("bad_streak", 0) or 0)
                ns  = int(m.get("null_streak", 0) or 0)
                re_ = int(m.get("rate_ema", 0) or 0)
            except Exception:
                val, mc, a, ss, bs, ns, re_ = 0, 0, 0, 0, 0, 0, 0
            if val in (0, (1<<64)-1):
                val = 0
            penalty = 16.0 / (16.0 + bs * 4.0 + ns * 2.0)
            score   = (re_ * (ss / 256.0)) * penalty
            active  = (mc > 0) or (a > 0) or (re_ > 0) or (ss > 0)
            rows.append({
                "alg":         CONGS[i],
                "metric":      round(val / 1e6, 1) if val else 0,
                "votes":       mc,
                "alive":       a,
                "rate_ema":    re_,
                "swap_score":  ss,
                "penalty":     round(penalty, 3),
                "score":       round(score, 2),
                "bad_streak":  bs,
                "null_streak": ns,
                "active":      active,
            })
        rows.sort(key=lambda r: (0 if r["active"] else 1, -r["score"]))
        out[addr] = rows
    return out





def data_bucket_live():
    """Live per-bucket 1h chart data, built from the tail of
    buckets.v2.csv.  Only the re_* columns, only the last
    LIVE_CHART_MIN minutes.

    The frontend prefers this for the 1h range so the chart refreshes
    with the browser's 30s tick instead of the renderer's 15-minute
    cron.  Longer ranges still come from data/bucket_*.json."""
    p = Path(BUCKET_HISTORY_CSV)
    if not p.exists():
        return {}

    # Read the real header from the top of the file.  A DictReader
    # over a mid-file chunk would treat the first data row as the
    # header and thus lose every column name.
    with open(p, newline="") as f:
        try:
            header = next(csv.reader(f))
        except StopIteration:
            return {}

    with open(p, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        take = min(size, 800 * 1024)
        f.seek(size - take)
        if f.tell() > 0:
            f.readline()   # skip partial line
        text = f.read().decode("utf-8", errors="replace")

    rd = csv.DictReader(io.StringIO(text), fieldnames=header)
    now = int(time.time())
    lo  = now - LIVE_CHART_MIN * 60
    per = {}

    for row in rd:
        addr = row.get("addr") or ""
        if not addr or addr in ("0.0.0.1",):
            continue
        if addr.startswith(("127.", "169.254.", "0.")):
            continue
        try:
            t = int(row.get("collected_ts") or 0)
        except (TypeError, ValueError):
            continue
        if t < lo:
            continue
        try:
            inst = int(row.get("instances") or 0)
        except (TypeError, ValueError):
            inst = 0
        if inst < 2:
            continue
        bucket = per.setdefault(_label_for(addr), {"ts": [], "cols": {}})
        bucket["ts"].append(t)
        for alg in CONGS:
            for pre in ("re_", "ss_", "bs_", "ns_"):
                c = row.get(pre + alg)
                try:
                    v = int(c) if c not in (None, "") else None
                except (TypeError, ValueError):
                    v = None
                bucket["cols"].setdefault(pre + alg, []).append(v)

    # bin to LIVE_CHART_WIDTH_S
    for addr, d in per.items():
        pd = {}
        for c, vals in d["cols"].items():
            bins = {}
            for ts_v, v in zip(d["ts"], vals):
                if v is None:
                    continue
                key = ts_v // LIVE_CHART_WIDTH_S
                bins.setdefault(key, []).append(v)
            keys = sorted(bins)
            pd[c] = [[k * LIVE_CHART_WIDTH_S, sum(bins[k]) / len(bins[k])]
                     for k in keys]
        cs = {c: [v for _, v in arr] for c, arr in pd.items()}
        # 0.4.79 fix: pd entries are already [bin*width, value].
        # The previous form multiplied by width a second time, so
        # a 65-minute ring came out as 65 hours on the chart.
        ts = sorted({k for arr in pd.values() for k, _ in arr})
        per[addr] = {"ts": ts, "cols": cs}

    return per



def data_recent_swaps_by_bucket(text, n_per_bucket=16) -> Dict[str, List[RecentSwap]]:
    """Same rows as data_recent_swaps, grouped by destination /16 so
    the panel can follow the bucket dropdown.  Returns
    {bucket_str: [row, ...]} ordered newest-first within each."""
    rows = data_recent_swaps(text, n=200)
    out = {}
    # 0.4.90: data_recent_swaps now returns newest-first (was oldest-first),
    # so iterate forward (not reversed) to fill each bucket newest-first.
    for r in rows:
        # 0.4.86: group by dest (labeled) instead of _bucket (raw key)
        # so keys match meta.json bucket IDs (which use the labeled dest).
        # Without this, selecting "home-sco" in the dropdown looks up
        # by["home-sco"] but the dict key is "82.43.0.0" -> empty panel.
        b = r.get("dest") or r.get("_bucket") or ""
        if not b:
            continue
        lst = out.setdefault(b, [])
        if len(lst) < n_per_bucket:
            lst.append(r)
    # strip the internal key before handing off
    for b, lst in out.items():
        for r in lst:
            r.pop("_bucket", None)
    return out



def data_proof(text) -> List[ProofRow]:
    events, samples = _proof_events(text)
    if not events and not samples:
        return []
    algs = set(events) | set(samples)
    rows = []
    for a in algs:
        e = events.get(a, {"good": 0, "proved": 0, "proven_max": 0})
        s = samples.get(a, {"sum": 0, "n": 0, "samp_max": 0})
        row = {
            "alg":         CONGS[a] if a < 16 else "alg%d" % a,
            "good":        e["good"],
            "proved":      e["proved"],
            "proven_max":  round(e["proven_max"] / BPS_TO_MBPS, 1)
                           if e["proven_max"] else None,
            "sampled_avg": round(s["sum"] / s["n"] / BPS_TO_MBPS, 1)
                           if s["n"] else None,
            "sampled_max": round(s["samp_max"] / BPS_TO_MBPS, 1)
                           if s["samp_max"] else None,
            "samples":     s["n"] or None,
        }
        rows.append(row)
    rows.sort(key=lambda r: -(r["proven_max"] or 0))
    return rows



def data_rate(text) -> List[RateRow]:
    out_map = defaultdict(list)
    for line in text.splitlines():
        if "midsamp" not in line:
            continue
        mt = re.search(r"thr=(\d+)", line)
        mr = re.search(r"rport=(\d+)", line)
        ms = re.search(r"srate=(\d+)", line)
        if not (mt and mr and ms):
            continue
        if mr.group(1) == "443":
            continue
        out_map[int(mt.group(1))].append(int(ms.group(1)))
    rows = []
    MAX_BPS = 1250000000
    for thr in sorted(out_map.keys()):
        vs = [v for v in out_map[thr] if v <= MAX_BPS]
        if not vs: vs = out_map[thr]
        rows.append({
            "thr":  thr,
            "n":    len(vs),
            "mean": round(sum(vs) / len(vs) / BPS_TO_MBPS, 1),
            "min":  round(min(vs) / BPS_TO_MBPS, 1),
            "max":  round(max(vs) / BPS_TO_MBPS, 1),
        })
    return rows



def _outcome_composite(met, c, ts):
    tl = met.get(c, [])
    pre = post = None
    for (mts, mval) in tl:
        if mts < ts + 0.001:
            pre = mval
        elif ts + 3.0 <= mts <= ts + 300.0:
            post = mval; break
    if not pre or not post:
        return None
    r = post / pre
    if r <= 0.9:  return "win"
    if r >= 1.1:  return "loss"
    return "null"



def _outcome_srate(srate, c, ts):
    tl = srate.get(c, [])
    pre = post = None
    for (sts, sr) in tl:
        if sts < ts:
            pre = sr
        elif sts > ts:
            post = sr; break
    if not pre or not post:
        return None
    r = post / pre
    if r >= 1.1:  return "win"
    if r <= 0.9:  return "loss"
    return "null"



def _outcome_sustained(srate, c, ts):
    tl = srate.get(c, [])
    pre = None
    post = []
    for (sts, sr) in tl:
        if sts < ts:
            pre = sr
        elif SUSTAINED_LO_S <= (sts - ts) <= SUSTAINED_HI_S:
            post.append(sr)
    if not pre or not post:
        return None
    pm = median(post)
    if pm is None or pre <= 0:
        return None
    r = pm / pre
    if r >= 1.1:  return "win"
    if r <= 0.9:  return "loss"
    return "null"



def _finalize_outcome(counts):
    total = counts["win"] + counts["null"] + counts["loss"]
    def pct(x):
        return round(100.0 * x / total, 1) if total else 0
    return {
        "measurable":    total,
        "unmeasurable":  counts["skip"],
        "win":           counts["win"],
        "win_pct":       pct(counts["win"]),
        "null":          counts["null"],
        "null_pct":      pct(counts["null"]),
        "loss":          counts["loss"],
        "loss_pct":      pct(counts["loss"]),
    }


# === bpftune-swap-outcomes-redesign-v1 ===

def _add_loss_recovery(outcomes_dict, swaps):
    """Mutates outcomes_dict to add rescued/full_loss/open + _pct fields."""
    if not swaps:
        for key in ('composite', 'srate', 'sustained'):
            sub = outcomes_dict.get(key)
            if isinstance(sub, dict) and 'loss' in sub:
                sub.setdefault('rescued', 0)
                sub.setdefault('full_loss', 0)
                sub.setdefault('open', 0)
                sub.setdefault('rescued_pct', 0.0)
                sub.setdefault('full_loss_pct', 0.0)
                sub.setdefault('open_pct', 0.0)
        return outcomes_dict

    swaps_sorted = sorted(swaps, key=lambda s: s.get('ts', 0))
    last_ts = swaps_sorted[-1].get('ts', 0)
    field_map = {'composite': 'outcome', 'srate': 'outcome_srate', 'sustained': 'outcome_sustained'}
    for key, field in field_map.items():
        sub = outcomes_dict.get(key)
        if not isinstance(sub, dict) or 'loss' not in sub:
            continue
        total_loss = sub.get('loss', 0) or 0
        if total_loss <= 0:
            sub['rescued'] = 0; sub['full_loss'] = 0; sub['open'] = 0
            sub['rescued_pct'] = 0.0; sub['full_loss_pct'] = 0.0; sub['open_pct'] = 0.0
            continue
        judged = [s for s in swaps_sorted if s.get(field) in ('win', 'null', 'loss')]
        losses = [s for s in judged if s.get(field) == 'loss']
        rescued = 0; full = 0; open_ = 0
        for loss in losses:
            ts = loss.get('ts', 0); cookie = loss.get('cookie'); found = False
            for s in judged:
                if s is loss: continue
                s_ts = s.get('ts', 0)
                if s_ts <= ts: continue
                if s_ts - ts > T_RESCUE_WINDOW_S: break
                if s.get('cookie') == cookie and s.get(field) == 'win':
                    found = True; break
            if found: rescued += 1
            elif (last_ts - ts) > T_RESCUE_WINDOW_S: full += 1
            else: open_ += 1
        sub['rescued'] = rescued; sub['full_loss'] = full; sub['open'] = open_
        sub['rescued_pct'] = round(rescued / total_loss * 100, 1)
        sub['full_loss_pct'] = round(full / total_loss * 100, 1)
        sub['open_pct'] = round(open_ / total_loss * 100, 1)
    return outcomes_dict
# === end bpftune-swap-outcomes-redesign-v1 ===



def data_swap_outcomes(text) -> SwapOutcomes:
    sw, met, srate = _swaps_mets_srates(text)
    swaps_list = []
    c_counts = {"win": 0, "null": 0, "loss": 0, "skip": 0}
    s_counts = {"win": 0, "null": 0, "loss": 0, "skip": 0}
    sust_counts = {"win": 0, "null": 0, "loss": 0, "skip": 0}
    for row in sw:
        ts, c = row[0], row[1]
        fa, ta = row[2], row[3]
        o = _outcome_composite(met, c, ts)
        if o is None: c_counts["skip"] += 1
        else: c_counts[o] += 1
        o2 = _outcome_srate(srate, c, ts)
        if o2 is None: s_counts["skip"] += 1
        else: s_counts[o2] += 1
        o3 = _outcome_sustained(srate, c, ts)
        if o3 is None: sust_counts["skip"] += 1
        else: sust_counts[o3] += 1
        # Destination extraction shared with _run_writeback_and_get_swaps()
        # via _extract_dest(row) so the two paths cannot drift apart.
        remote_host = _extract_dest(row)
        swaps_list.append({
            'ts': ts,
            'cookie': c,
            'from_alg':         fa,
            'to_alg':           ta,
            'remote_host':      remote_host,
            'outcome': o,
            'outcome_srate': o2,
            'outcome_sustained': o3,
        })
    try:
        from streak_writeback import writeback_streaks
        writeback_streaks(swaps_list)
    except Exception as e:
        import sys; print(f'[writeback] {e}', file=sys.stderr)
    _result = _add_loss_recovery({
        "composite": _finalize_outcome(c_counts),
        "srate":     _finalize_outcome(s_counts),
        "sustained": _finalize_outcome(sust_counts),
    }, swaps_list)
    _result["swaps_list"] = [
        {"dest": _resolve_remote_host(s.get("remote_host")) if s.get("remote_host") else None,
         "outcome": s.get("outcome"),
         "outcome_sustained": s.get("outcome_sustained"),
         "cookie": s.get("cookie"), "ts": s.get("ts")}
        for s in swaps_list
    ]
    return _result



def data_divergence(text) -> List[DivergenceRow]:
    sw, met, srate = _swaps_mets_srates(text)
    keys = ("rate==metric", "rate!=metric", "pre-0.4.45")
    groups = {k: {"total": 0,
                  "win": 0, "null": 0, "loss": 0,
                  "win_s": 0, "null_s": 0, "loss_s": 0,
                  "win_u": 0, "null_u": 0, "loss_u": 0}
              for k in keys}
    for row in sw:
        ts, c = row[0], row[1]
        mt_i, rb_i = row[7], row[8]
        if mt_i is None or rb_i is None: key = "pre-0.4.45"
        elif mt_i == rb_i:               key = "rate==metric"
        else:                            key = "rate!=metric"
        g = groups[key]
        g["total"] += 1
        o = _outcome_composite(met, c, ts)
        if o: g[o] += 1
        o2 = _outcome_srate(srate, c, ts)
        if o2: g[o2 + "_s"] += 1
        o3 = _outcome_sustained(srate, c, ts)
        if o3: g[o3 + "_u"] += 1

    rows = []
    for k in keys:
        g = groups[k]
        cmeas = g["win"] + g["null"] + g["loss"]
        smeas = g["win_s"] + g["null_s"] + g["loss_s"]
        umeas = g["win_u"] + g["null_u"] + g["loss_u"]
        def cp(x):
            return round(100.0 * x / cmeas, 1) if cmeas else 0
        def sp(x):
            return round(100.0 * x / smeas, 1) if smeas else 0
        def up(x):
            return round(100.0 * x / umeas, 1) if umeas else 0
        rows.append({
            "category":  k,
            "measured":  cmeas,
            "win_pct":   cp(g["win"]),
            "null_pct":  cp(g["null"]),
            "loss_pct":  cp(g["loss"]),
            "win":       g["win"],
            "null":      g["null"],
            "loss":      g["loss"],
            "skipped":   g["total"] - cmeas,
            "measured_srate": smeas,
            "win_pct_srate":  sp(g["win_s"]),
            "null_pct_srate": sp(g["null_s"]),
            "loss_pct_srate": sp(g["loss_s"]),
            "win_srate":      g["win_s"],
            "null_srate":     g["null_s"],
            "loss_srate":     g["loss_s"],
            "skipped_srate":  g["total"] - smeas,
            "measured_sustained": umeas,
            "win_pct_sustained":  up(g["win_u"]),
            "null_pct_sustained": up(g["null_u"]),
            "loss_pct_sustained": up(g["loss_u"]),
            "win_sustained":      g["win_u"],
            "null_sustained":     g["null_u"],
            "loss_sustained":     g["loss_u"],
            "skipped_sustained":  g["total"] - umeas,
        })
    return rows



def data_churn(text) -> ChurnInfo:
    sw, _met, _sr = _swaps_mets_srates(text)
    counts = defaultdict(int)
    for row in sw:
        counts[row[1]] += 1
    if not counts:
        return {"cookies": 0, "one": 0, "mid": 0, "many": 0, "max": 0}
    one  = sum(1 for v in counts.values() if v == 1)
    mid  = sum(1 for v in counts.values() if 2 <= v <= 4)
    many = sum(1 for v in counts.values() if v >= 5)
    return {"cookies": len(counts), "one": one, "mid": mid,
            "many": many, "max": max(counts.values())}



def data_recent_swaps(text, n=18) -> List[RecentSwap]:
    sw, met, srate = _swaps_mets_srates(text)
    rows = []
    for row in sw:
        ts, c, fa, ta = row[0], row[1], row[2], row[3]
        d, mt_i, rb_i = row[6], row[7], row[8]
        o  = _outcome_composite(met, c, ts)
        o2 = _outcome_srate(srate, c, ts)
        o3 = _outcome_sustained(srate, c, ts)
        mt_alg = (CONGS[int(mt_i) & 15]
                  if mt_i and mt_i.isdigit() else None)
        rb_alg = (CONGS[int(rb_i) & 15]
                  if rb_i and rb_i.isdigit() else None)
        rows.append({
            "boot_ts":  ts,
            "from_alg": CONGS[fa] if fa < 16 else str(fa),
            "to_alg":   CONGS[ta] if ta < 16 else str(ta),
            "d":        int(d),
            "outcome":            o,
            "outcome_srate":      o2,
            "outcome_sustained":  o3,
            "mt_alg":   mt_alg,
            "rb_alg":   rb_alg,
            "dest":     _label_for(_fold_v6(_dest_str(row[9] if len(row) > 9 else None,
                                   row[10] if len(row) > 10 else None))),
            "_bucket":  _bucket_of(row[9] if len(row) > 9 else None,
                                    row[10] if len(row) > 10 else None),
        })
    return rows[-n:][::-1]  # 0.4.90: newest-first



def data_recent_proofs(text, n=18) -> List[RecentProof]:
    lines = [l for l in text.splitlines() if "proof cookie=" in l][-n:]
    cdest = _cookie_dest_map(text)
    out = []
    for l in lines:
        m = re.search(r"(\d+\.\d+): .*proof cookie=(\d+) alg=(\d+) rate=(\d+) tier=(\d+)", l)
        if not m:
            continue
        ts = float(m.group(1))
        c = m.group(2)
        a = int(m.group(3))
        out.append({
            "boot_ts":  ts,
            "alg":  CONGS[a] if a < 16 else "alg%d" % a,
            "mbps": round(int(m.group(4)) / BPS_TO_MBPS, 1),
            "tier": "proved" if m.group(5) == "2" else "good",
            "dest": _label_for(_dest_str(*(cdest.get(c) or (None, None)))),
        })
    # 0.4.90: return newest-first (was oldest-first) so the JS
    # renderRecentProofs doesn't need to .reverse().
    return out[::-1]





def data_live_leaders(hosts) -> List[LiveLeader]:
    """Compute the picker's live ranking per bucket.

    Uses the exact formula from reanchor_best (tcp_conn_tuner.c):
        weighted = rate_ema * swap_score / 256
        pen      = 16 + bad_streak*4 + null_streak*2
        weighted = weighted * 16 / pen
    Filters to metric_count >= MIN_LEADER_TRUST, matching the
    picker's own trust floor.  Sorted descending; the first entry
    is what the reanchor would pick right now.

    Delivered via current.json, which the browser polls every 30s.
    The historical leaderboard (bucket_*.json) still comes from the
    renderer on its 5-minute cron."""
    out = []
    if not hosts:
        return out
    for inst, addr, v in hosts:
        if addr in ("0.0.0.1", "?"):
            continue
        if addr.startswith(("127.", "169.254.", "0.")):
            continue
        if inst < 2:
            continue
        metrics = v.get("metrics") or []
        cands = []
        for i in range(16):
            m = metrics[i] if i < len(metrics) and isinstance(metrics[i], dict) else {}
            try:
                cnt = int(m.get("metric_count", 0) or 0)
                rv  = int(m.get("rate_ema", 0) or 0)
                ss  = int(m.get("swap_score", 0) or 0)
                bad = int(m.get("bad_streak", 0) or 0)
                nul = int(m.get("null_streak", 0) or 0)
            except (TypeError, ValueError):
                continue
            if cnt < MIN_LEADER_TRUST:
                continue
            if rv == 0:
                continue
            ss_eff = ss if ss else 256
            weighted = rv * ss_eff // 256
            pen = 16 + bad * 4 + nul * 2
            weighted = weighted * 16 // pen
            cands.append((weighted, rv, ss, bad, nul, cnt, i))
        if not cands:
            continue
        cands.sort(key=lambda x: -x[0])
        rows = []
        for (w, rv, ss, bad, nul, cnt, i) in cands[:LIVE_TOP_N]:
            rows.append({
                "alg":        CONGS[i],
                "weighted":   int(w),
                "rate_ema":   rv,
                "swap_score": ss,
                "bad":        bad,
                "null":       nul,
                "count":      cnt,
            })
        out.append({"dest": addr, "inst": inst, "top": rows})
        if len(out) >= LIVE_MAX_BUCKETS:
            break
    return out



def data_bucket_ips(text) -> Dict[str, List[str]]:
    """Extract all dest IPs (v4 + v6) from the log, group by /16 (v4) or /32 (v6)."""
    import re, ipaddress
    buckets = {}
    for line in text.splitlines():
        # IPv4: dest=<int>
        m = re.search(r'dest=(\d+)', line)
        if m:
            n = int(m.group(1))
            if n != 0:
                full = "%d.%d.%d.%d" % ((n>>24)&0xff,(n>>16)&0xff,(n>>8)&0xff,n&0xff)
                try:
                    masked = str(ipaddress.IPv4Address(int(ipaddress.IPv4Address(full)) & 0xFFFF0000))
                except: continue
                if masked not in buckets: buckets[masked] = []
                if full not in buckets[masked]: buckets[masked].append(full)
        # IPv6: dest6=<int> (first 32 bits) + dest6b=<int> (second 32 bits, 0.4.83)
        m6 = re.search(r'dest6=(\d+)', line)
        if m6:
            n6 = int(m6.group(1))
            if n6 != 0:
                hi = (n6 >> 16) & 0xFFFF
                lo = n6 & 0xFFFF
                # /32 masked key (first 32 bits, zero-padded)
                masked_v6 = "%x:%x::" % (hi, lo)
                # Full address: include dest6b if present (64 bits)
                full_v6 = masked_v6
                m6b = re.search(r'dest6b=(\d+)', line)
                if m6b:
                    n6b = int(m6b.group(1))
                    if n6b != 0:
                        hi2 = (n6b >> 16) & 0xFFFF
                        lo2 = n6b & 0xFFFF
                        full_v6 = "%x:%x:%x:%x::" % (hi, lo, hi2, lo2)
                if masked_v6 not in buckets: buckets[masked_v6] = []
                if full_v6 not in buckets[masked_v6]: buckets[masked_v6].append(full_v6)
    return buckets





def _proofs_with_dest(text):
    """Per-proof data with labeled dest for client-side bucket filtering."""
    cdest = _cookie_dest_map(text)
    out = []
    for line in text.splitlines():
        if "proof cookie=" not in line:
            continue
        m = re.search(r"proof cookie=(\d+) alg=(\d+) rate=(\d+) tier=(\d+)", line)
        if not m:
            continue
        c = m.group(1)
        a = int(m.group(2))
        v4, v6 = cdest.get(c, (None, None))
        dest = _label_for(_dest_str(v4, v6))
        out.append({
            "alg":  CONGS[a] if a < 16 else "alg%d" % a,
            "dest": dest,
            "rate": round(int(m.group(3)) / BPS_TO_MBPS, 1),
            "tier": "proved" if m.group(4) == "2" else "good",
        })
    return out



def _rate_samples_with_dest(text):
    """Per-rate-sample with labeled dest for client-side bucket filtering."""
    cdest = _cookie_dest_map(text)
    out = []
    for line in text.splitlines():
        if "midsamp" not in line:
            continue
        mt = re.search(r"thr=(\d+)", line)
        ms = re.search(r"srate=(\d+)", line)
        mc = re.search(r"cookie=(\d+)", line)
        if not (mt and ms and mc):
            continue
        c = mc.group(1)
        v4, v6 = cdest.get(c, (None, None))
        dest = _label_for(_dest_str(v4, v6))
        out.append({
            "dest":  dest,
            "thr":   int(mt.group(1)),
            "srate": int(ms.group(1)),
        })
    return out


def _run_writeback_and_get_swaps(text):
    """Run the writeback BEFORE the leaderboard reads the BPF map.
    Returns the swaps_list for data_swap_outcomes to reuse."""
    sw, met, srate = _swaps_mets_srates(text)
    swaps_list = []
    for row in sw:
        ts, c = row[0], row[1]
        fa, ta = row[2], row[3]
        o = _outcome_composite(met, c, ts)
        o2 = _outcome_srate(srate, c, ts)
        o3 = _outcome_sustained(srate, c, ts)
        # Shared with data_swap_outcomes() via _extract_dest(row).
        remote_host = _extract_dest(row)
        swaps_list.append({'ts':ts,'cookie':c,'from_alg':fa,'to_alg':ta,
                          'remote_host':remote_host,'outcome':o,
                          'outcome_srate':o2,'outcome_sustained':o3})
    try:
        from streak_writeback import writeback_streaks
        writeback_streaks(swaps_list)
    except Exception as e:
        import sys; print(f'[writeback] {e}', file=sys.stderr)
    return swaps_list




