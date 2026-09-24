#!/usr/bin/env python3
"""bpftune live dashboard.

Default: human-readable text (two columns).
--json : single JSON object of the same data - used by the collector.

Three outcome scales are reported:

  composite  - met `val=` ratio (post/pre), from the composite metric.
  srate      - first per-vote `srate=` (raw bytes/sec) after the swap.
  sustained  - median of srate samples in [t+60, t+300]. Excludes the
               immediate cwnd-reset dip after a swap; this is the
               accurate throughput measure. Higher is better.
"""
import argparse, json, os, re, socket, struct, subprocess, sys, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

CONGS = ["cubic","bbr","htcp","dctcp","scalable","vegas","veno","westwood",
         "reno","illinois","yeah","lp","bic","highspeed","hybla","nv"]
LOG_TAIL_BYTES = 2_000_000
BPS_TO_MBPS = 1_000_000.0 / 8.0

RULE   = "\u2500"
DRULE  = "\u2550"
ARROW  = "\u25b8"
VBAR   = "\u2502"
MIDDOT = "\u00b7"

SUSTAINED_LO_S = 60.0
SUSTAINED_HI_S = 300.0


def sh(cmd, timeout=15):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=timeout).stdout
    except Exception:
        return ""


def sh_noshell(args, timeout=12):
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:
        return ""


def median(xs):
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    if n % 2:
        return float(s[n // 2])
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def find_log():
    files = list(Path("/var/log").glob("bpftune-met-*.log"))
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def tail_recent(budget=LOG_TAIL_BYTES):
    paths = sorted(Path("/var/log").glob("bpftune-met-*.log"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not paths:
        return ""
    chunks = []
    remaining = budget
    for p in paths:
        if remaining <= 0:
            break
        try:
            size = p.stat().st_size
        except OSError:
            continue
        take = min(size, remaining)
        try:
            with open(p, "rb") as f:
                f.seek(size - take)
                chunks.append(f.read().decode("utf-8", errors="replace"))
        except OSError:
            continue
        remaining -= take
    return "".join(reversed(chunks))


def read_map():
    cached = os.environ.get("BPFTUNE_MAP_DUMP_JSON")
    if cached and os.path.exists(cached):
        try:
            with open(cached) as f:
                out = f.read()
        except OSError:
            out = ""
    else:
        out = sh("bpftool --json map dump name remote_host_map 2>/dev/null")
    try:
        data = json.loads(out)
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    entries = []
    for e in data:
        if not isinstance(e, dict):
            continue
        fmt = e.get("formatted") or {}
        v = fmt.get("value"); k = fmt.get("key") or {}
        if not isinstance(v, dict):
            continue
        try:
            inst = int(v.get("instances", 0))
        except Exception:
            continue
        addr = "?"
        b = k.get("in6_u", {}).get("u6_addr8")
        if isinstance(b, list) and len(b) == 16:
            addr = ".".join(str(x) for x in b[12:16])
        entries.append((inst, addr, v))
    if not entries:
        return None
    entries.sort(key=lambda x: -x[0])
    return entries


def data_build(logpath):
    v = sh("dpkg-query -W -f='${Version}' bpftune").strip() or "?"
    a = sh("systemctl is-active bpftune").strip() or "?"
    ts = sh("systemctl show bpftune -p ActiveEnterTimestamp --value").strip()
    uptime_min = None
    started = ""
    m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", ts)
    if m:
        started = m.group(1)[11:19]
        try:
            t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc)
            uptime_min = int((datetime.now(timezone.utc) - t).total_seconds() // 60)
        except Exception:
            pass
    return {
        "version":     v,
        "service":     a,
        "uptime_min":  uptime_min,
        "started_utc": started,
        "log_path":    str(logpath) if logpath else None,
    }


def data_system():
    out = {
        "kernel":     sh_noshell(["uname", "-r"]).strip(),
        "default_cc": sh_noshell(["sysctl", "-n",
                                  "net.ipv4.tcp_congestion_control"]).strip(),
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


def data_tunables():
    j = sh_noshell(["journalctl", "-b", "-u", "bpftune",
                                        "--no-pager", "-q",
                                        "--since", "-24h"])
    names = sorted(set(re.findall(r"sysctl '(net\.[A-Za-z0-9_.]+)'", j)))
    items = []
    for n in names:
        v = sh_noshell(["sysctl", "-n", n]).strip()
        if not v:
            continue
        short = n[4:]
        if "allowed_congestion_control" in n:
            v = "%d algorithms" % len(v.split())
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


def data_buckets(hosts, n=8):
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
        rows.append({
            "dest":     addr,
            "inst":     inst,
            "rtt_us":   int(rtt),
            "ref_mbps": round(ref, 1),
            "best_alg": CONGS[int(bi)] if int(bi) < 16 else str(bi),
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


def data_metric(hosts):
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


def _proof_events(text):
    MET_WINDOW_S = 60.0
    lines = text.splitlines()

    events = defaultdict(lambda: {"good": 0, "proved": 0, "proven_max": 0})
    samples = defaultdict(lambda: {"sum": 0, "n": 0, "samp_max": 0})
    met_by_cookie = defaultdict(list)

    for line in lines:
        if "proof cookie=" in line:
            m = re.search(r"alg=(\d+) rate=(\d+) tier=(\d+)", line)
            if m:
                a = int(m.group(1))
                rate = int(m.group(2))
                tier = m.group(3)
                if tier == "2":
                    events[a]["proved"] += 1
                else:
                    events[a]["good"] += 1
                if rate > events[a]["proven_max"]:
                    events[a]["proven_max"] = rate
            continue
        mm = re.search(
            r"(\d+\.\d+): .*met cookie=(\d+) rport=\d+ alg=(\d+) ",
            line)
        if mm:
            ts = float(mm.group(1))
            c  = int(mm.group(2))
            a  = int(mm.group(3))
            met_by_cookie[c].append((ts, a))

    for line in lines:
        ms = re.search(
            r"(\d+\.\d+): .*midsamp cookie=(\d+) .* srate=(\d+)",
            line)
        if not ms:
            continue
        ts = float(ms.group(1))
        c  = int(ms.group(2))
        r  = int(ms.group(3))
        if r <= 0:
            continue
        cand = met_by_cookie.get(c)
        if not cand:
            continue
        best_a = None
        best_d = None
        for (mts, a) in cand:
            d = abs(mts - ts)
            if best_d is None or d < best_d:
                best_d = d
                best_a = a
        if best_a is None or best_d is None or best_d > MET_WINDOW_S:
            continue
        samples[best_a]["sum"] += r
        samples[best_a]["n"] += 1
        if r > samples[best_a]["samp_max"]:
            samples[best_a]["samp_max"] = r

    return events, samples


def data_proof(text):
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


def data_rate(text):
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
    for thr in sorted(out_map.keys()):
        vs = out_map[thr]
        rows.append({
            "thr":  thr,
            "n":    len(vs),
            "mean": round(sum(vs) / len(vs) / BPS_TO_MBPS, 1),
            "min":  round(min(vs) / BPS_TO_MBPS, 1),
            "max":  round(max(vs) / BPS_TO_MBPS, 1),
        })
    return rows


def _swaps_mets_srates(text):
    sw = []
    met = defaultdict(list)
    srate = defaultdict(list)
    rx_sw = re.compile(r"(\d+\.\d+): bpf_trace_printk: "
                       r"swap cookie=(\d+) from=(\d+) to=(\d+) "
                       r"bc=(\d+) ac=(\d+) d=(\d+)"
                       r"(?: mt=(\d+) rb=(\d+))?"
                       r"(?: dest=(\d+))?")
    rx_mt = re.compile(r"(\d+\.\d+): bpf_trace_printk: "
                       r"met cookie=(\d+) rport=(\d+) alg=(\d+) segs=(\d+) val=(\d+)")
    rx_sr = re.compile(r"(\d+\.\d+): bpf_trace_printk: "
                       r"srate cookie=(\d+) alg=(\d+) srate=(\d+)")
    for line in text.splitlines():
        m = rx_sw.search(line)
        if m:
            sw.append((float(m.group(1)), int(m.group(2)),
                       int(m.group(3)), int(m.group(4)),
                       int(m.group(5)), int(m.group(6)), m.group(7),
                       m.group(8), m.group(9), m.group(10), line))
            continue
        v = rx_mt.search(line)
        if v:
            met[int(v.group(2))].append((float(v.group(1)), int(v.group(6))))
            continue
        s = rx_sr.search(line)
        if s:
            srate[int(s.group(2))].append((float(s.group(1)), int(s.group(4))))
    return sw, met, srate


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


def data_swap_outcomes(text):
    sw, met, srate = _swaps_mets_srates(text)
    c_counts = {"win": 0, "null": 0, "loss": 0, "skip": 0}
    s_counts = {"win": 0, "null": 0, "loss": 0, "skip": 0}
    sust_counts = {"win": 0, "null": 0, "loss": 0, "skip": 0}
    for row in sw:
        ts, c = row[0], row[1]
        o = _outcome_composite(met, c, ts)
        if o is None: c_counts["skip"] += 1
        else: c_counts[o] += 1
        o2 = _outcome_srate(srate, c, ts)
        if o2 is None: s_counts["skip"] += 1
        else: s_counts[o2] += 1
        o3 = _outcome_sustained(srate, c, ts)
        if o3 is None: sust_counts["skip"] += 1
        else: sust_counts[o3] += 1
    return {
        "composite": _finalize_outcome(c_counts),
        "srate":     _finalize_outcome(s_counts),
        "sustained": _finalize_outcome(sust_counts),
    }


def data_divergence(text):
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


def data_churn(text):
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


def _dest_ip(s):
    if s is None or s in ("", "1"):
        return ""
    try:
        n = int(s)
    except (TypeError, ValueError):
        return ""
    first = (n >> 24) & 0xFF
    if first == 0 or first == 127:
        return ""
    try:
        return socket.inet_ntoa(struct.pack(">I", n & 0xFFFFFFFF))
    except Exception:
        return ""


_SWAP_DEST_RX = re.compile(
    r"swap cookie=(\d+) from=\d+ to=\d+ bc=\d+ ac=\d+ d=\d+"
    r"(?: mt=\d+ rb=\d+)? dest=(\d+)")
_ESTAB_DEST_RX = re.compile(
    r"estab cookie=(\d+) alg=\d+ forced=\d+ dest=(\d+)")


def _cookie_dest_map(text):
    out = {}
    for line in text.splitlines():
        m = _SWAP_DEST_RX.search(line)
        if m:
            out[m.group(1)] = m.group(2)
            continue
        m = _ESTAB_DEST_RX.search(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def data_recent_swaps(text, n=10):
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
            "from_alg": CONGS[fa] if fa < 16 else str(fa),
            "to_alg":   CONGS[ta] if ta < 16 else str(ta),
            "d":        int(d),
            "outcome":            o,
            "outcome_srate":      o2,
            "outcome_sustained":  o3,
            "mt_alg":   mt_alg,
            "rb_alg":   rb_alg,
            "dest":     _dest_ip(row[9] if len(row) > 9 else None),
        })
    return rows[-n:]


def data_recent_proofs(text, n=10):
    lines = [l for l in text.splitlines() if "proof cookie=" in l][-n:]
    cdest = _cookie_dest_map(text)
    out = []
    for l in lines:
        m = re.search(r"proof cookie=(\d+) alg=(\d+) rate=(\d+) tier=(\d+)", l)
        if not m:
            continue
        a = int(m.group(2))
        out.append({
            "alg":  CONGS[a] if a < 16 else "alg%d" % a,
            "mbps": round(int(m.group(3)) / BPS_TO_MBPS, 1),
            "tier": "proved" if m.group(4) == "2" else "good",
            "dest": _dest_ip(cdest.get(m.group(1))),
        })
    return out


MIN_LEADER_TRUST = 10   # matches tcp_conn_tuner.h
LIVE_TOP_N       = 6
LIVE_MAX_BUCKETS = 8


def data_live_leaders(hosts):
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


def collect_all():
    logpath = find_log()
    hosts   = read_map()
    text    = tail_recent()
    return {
        "generated_ts":   int(time.time()),
        "hostname":       os.uname().nodename,
        "build":          data_build(logpath),
        "system":         data_system(),
        "tunables":       data_tunables(),
        "buckets":        data_buckets(hosts),
        "metric":         data_metric(hosts),
        "live_leaders":   data_live_leaders(hosts),
        "proof":          data_proof(text),
        "rate":           data_rate(text),
        "swap_outcomes":  data_swap_outcomes(text),
        "divergence":     data_divergence(text),
        "churn":          data_churn(text),
        "recent_swaps":   data_recent_swaps(text),
        "recent_proofs":  data_recent_proofs(text),
    }


# ---------- text renderer ----------

CW   = 58
GAP  = "  " + VBAR + "  "
FULL = CW*2 + len(GAP)


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
    bk = [f"  {'dest':<16}{'inst':>6}{'rtt_us':>9}{'ref_Mbps':>10}"
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
    for row in _twocol("RATE PROGRESSION (client, Mbps)", rt,
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
    rp = [f"  {r['alg']:<9} {r['mbps']:>7.1f} Mbps   {r['tier']}"
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
