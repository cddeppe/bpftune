#!/usr/bin/env python3
"""
bpftune dashboard installer. Idempotent - safe to re-run.

    sudo python3 tools/bpftune-dashboard-install.py

Writes three scripts beside itself, migrates old CSVs, installs
/etc/cron.d/bpftune-history, runs both once, verifies output.

`collected_ts` (wall clock) is the only date-safe column. Swap rows also
carry `boot_ts` (monotonic seconds from the log) - never derive a
calendar date from boot_ts.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

HIST           = "/var/lib/bpftune/history"
CRON           = "/etc/cron.d/bpftune-history"
BUCKETS_V1     = os.path.join(HIST, "buckets.v1.csv")
BUCKETS_V2     = os.path.join(HIST, "buckets.v2.csv")
BUCKETS_LEGACY = os.path.join(HIST, "buckets.csv")
SWAPS_V1       = os.path.join(HIST, "swaps.v1.csv")
SWAPS          = os.path.join(HIST, "swaps.csv")
CURRENT_JSON   = os.path.join(HIST, "current.json")

SELF_DIR  = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.join(SELF_DIR, "bpftune-collector.py")
RENDERER  = os.path.join(SELF_DIR, "bpftune-render.py")
CLI       = os.path.join(SELF_DIR, "bpftune-cli.py")


def _c(code, s):
    return "\033[" + code + "m" + s + "\033[0m"


def say(m):
    print(_c("1;34", "[installer]") + " " + m, flush=True)


def warn(m):
    print(_c("1;33", "[installer]") + " " + m, flush=True)


def die(m):
    print(_c("1;31", "[installer] FATAL:") + " " + m,
          file=sys.stderr, flush=True)
    sys.exit(1)


def write_file(path, text, mode=0o644):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def migrate():
    if os.path.exists(BUCKETS_LEGACY) and not os.path.exists(BUCKETS_V1):
        os.rename(BUCKETS_LEGACY, BUCKETS_V1)
        say("  migrated buckets.csv -> buckets.v1.csv")
    elif os.path.exists(BUCKETS_V1):
        say("  buckets.v1.csv already present")
    else:
        say("  no buckets.csv to migrate")

    if os.path.exists(SWAPS):
        with open(SWAPS, newline="") as f:
            first = f.readline().strip()
        cols = first.split(",") if first else []
        if "collected_ts" in cols:
            say("  swaps.csv already schema v2")
        elif "ts_epoch" in cols:
            os.rename(SWAPS, SWAPS_V1)
            say("  migrated swaps.csv -> swaps.v1.csv (schema v1)")
        else:
            warn("  swaps.csv has unrecognized header: " + first)
    else:
        say("  no swaps.csv yet (will be created)")


def write_cron():
    body = (
        "# managed by bpftune-dashboard-install.py\n"
        "* * * * * root %s >> /var/log/bpftune-collector.log 2>&1\n"
        "*/5 * * * * root %s >> /var/log/bpftune-collector.log 2>&1\n"
        % (COLLECTOR, RENDERER)
    )
    write_file(CRON, body, 0o644)


def count_data_rows(path):
    if not os.path.exists(path):
        return 0
    with open(path, newline="") as f:
        return max(0, sum(1 for _ in f) - 1)


def run_and_verify():
    before = count_data_rows(BUCKETS_V2)
    r = subprocess.run([sys.executable, COLLECTOR],
                       capture_output=True, text=True)
    print("    collector:", r.stdout.strip() or "(no output)")
    if r.returncode != 0:
        print(r.stderr)
        die("collector exited non-zero")

    after = count_data_rows(BUCKETS_V2)
    if after == 0:
        die("collector wrote zero rows - is bpftune running and "
            "bpftool able to dump remote_host_map?")
    if after <= before:
        warn("collector added no rows (map unchanged)")
    else:
        say("  buckets.v2.csv now has %d rows" % after)

    if not os.path.exists(CURRENT_JSON):
        warn("current.json not written - CLI JSON snapshot failed")
    else:
        say("  current.json: %d bytes" % os.path.getsize(CURRENT_JSON))

    r = subprocess.run([sys.executable, RENDERER],
                       capture_output=True, text=True)
    print("    renderer:", r.stdout.strip() or "(no output)")
    if r.returncode != 0:
        print(r.stderr)
        die("renderer exited non-zero")

    for p in (os.path.join(HIST, "index.html"),
              os.path.join(HIST, "data", "meta.json")):
        if not os.path.exists(p):
            die("expected output missing: " + p)


# =================== CLI ===================

CLI_SRC = r'''#!/usr/bin/env python3
"""bpftune live dashboard.

Default: human-readable text (two columns).
--json : single JSON object of the same data - used by the collector.
"""
import argparse, json, os, re, subprocess, sys, time
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


def find_log():
    files = list(Path("/var/log").glob("bpftune-met-*.log"))
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def tail(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2); size = f.tell()
            f.seek(max(0, size - LOG_TAIL_BYTES))
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def read_map():
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


# ---------- data extraction (structured) ----------

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
    return {
        "kernel":     sh_noshell(["uname", "-r"]).strip(),
        "default_cc": sh_noshell(["sysctl", "-n",
                                  "net.ipv4.tcp_congestion_control"]).strip(),
    }


def data_tunables():
    j = sh_noshell(["journalctl", "-u", "bpftune", "--no-pager", "-q"])
    names = sorted(set(re.findall(r"sysctl '(net\.[A-Za-z0-9_.]+)'", j)))
    out = []
    for n in names:
        v = sh_noshell(["sysctl", "-n", n]).strip()
        if not v:
            continue
        short = n[4:]
        if "allowed_congestion_control" in n:
            v = "%d algorithms" % len(v.split())
        out.append({"name": short, "value": v})
    return out


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


def data_metric(hosts):
    if not hosts:
        return []
    metrics = hosts[0][2].get("metrics") or []
    rows = []
    for i, m in enumerate(metrics):
        if not isinstance(m, dict):
            continue
        try:
            val = int(m.get("metric_value", 0) or 0)
            mc  = int(m.get("metric_count", 0) or 0)
            a   = int(m.get("sockets_alive", 0) or 0)
        except Exception:
            continue
        if mc == 0 and a == 0:
            continue
        if val in (0, (1<<64)-1):
            val = 0
        rows.append({
            "alg":    CONGS[i] if i < 16 else "alg%d" % i,
            "metric": round(val / 1e6, 1) if val else 0,
            "votes":  mc,
            "alive":  a,
        })
    rows.sort(key=lambda r: r["metric"] if r["metric"] else 1<<62)
    return rows


def _proof(text):
    per_alg = defaultdict(lambda: {"t1": 0, "t2": 0, "max": 0})
    for line in text.splitlines():
        if "proof cookie=" not in line:
            continue
        m = re.search(r"alg=(\d+) rate=(\d+) tier=(\d+)", line)
        if not m:
            continue
        a = int(m.group(1)); rate = int(m.group(2)); tier = m.group(3)
        per_alg[a]["t"+tier] += 1
        if rate > per_alg[a]["max"]:
            per_alg[a]["max"] = rate
    return per_alg


def _midsamp(text):
    avg = {}
    cur_alg = {}
    for line in text.splitlines():
        mm = re.search(r"met cookie=(\d+) rport=\d+ alg=(\d+) ", line)
        if mm:
            cur_alg[int(mm.group(1))] = int(mm.group(2))
            continue
        ms = re.search(r"midsamp cookie=(\d+) .* alg=(\d+) .* srate=(\d+)", line)
        if ms:
            a = int(ms.group(2)); r = int(ms.group(3))
            if r > 0:
                avg.setdefault(a, [0, 0])
                avg[a][0] += r; avg[a][1] += 1
        else:
            ms2 = re.search(r"midsamp cookie=(\d+) .* srate=(\d+)", line)
            if ms2:
                c, r = int(ms2.group(1)), int(ms2.group(2))
                if r > 0 and c in cur_alg:
                    a = cur_alg[c]
                    avg.setdefault(a, [0, 0])
                    avg[a][0] += r; avg[a][1] += 1
    return avg


def data_proof(text):
    per_alg = _proof(text)
    if not per_alg:
        return []
    avg = _midsamp(text)
    rows = []
    for a in sorted(per_alg.keys(), key=lambda x: -per_alg[x]["max"]):
        d = per_alg[a]
        row = {
            "alg":    CONGS[a] if a < 16 else "alg%d" % a,
            "good":   d["t1"],
            "proved": d["t2"],
            "max_mbps": round(d["max"] / BPS_TO_MBPS, 1),
            "avg_mbps": None,
            "n": None,
        }
        if a in avg and avg[a][1]:
            row["avg_mbps"] = round(avg[a][0] / avg[a][1] / BPS_TO_MBPS, 1)
            row["n"] = avg[a][1]
        rows.append(row)
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


def _swaps_and_mets(text):
    sw = []
    met = defaultdict(list)
    rx_sw = re.compile(r"(\d+\.\d+): bpf_trace_printk: "
                       r"swap cookie=(\d+) from=(\d+) to=(\d+) "
                       r"bc=(\d+) ac=(\d+) d=(\d+)"
                       r"(?: mt=(\d+) rb=(\d+))?")
    rx_mt = re.compile(r"(\d+\.\d+): bpf_trace_printk: "
                       r"met cookie=(\d+) rport=(\d+) alg=(\d+) segs=(\d+) val=(\d+)")
    for line in text.splitlines():
        m = rx_sw.search(line)
        if m:
            sw.append((float(m.group(1)), int(m.group(2)),
                       int(m.group(3)), int(m.group(4)),
                       int(m.group(5)), int(m.group(6)), m.group(7),
                       m.group(8), m.group(9), line))
            continue
        v = rx_mt.search(line)
        if v:
            met[int(v.group(2))].append((float(v.group(1)), int(v.group(6))))
    return sw, met


def _outcome(met, c, ts):
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


def data_swap_outcomes(text):
    sw, met = _swaps_and_mets(text)
    win = null = loss = skip = 0
    for (ts, c, fa, ta, bc, ac, d, mt_i, rb_i, line) in sw:
        o = _outcome(met, c, ts)
        if o is None:
            skip += 1
            continue
        if   o == "win":  win += 1
        elif o == "loss": loss += 1
        else:             null += 1
    total = win + null + loss
    def pct(x):
        return round(100.0 * x / total, 1) if total else 0
    return {
        "measurable":    total,
        "unmeasurable":  skip,
        "win":           win,
        "win_pct":       pct(win),
        "null":          null,
        "null_pct":      pct(null),
        "loss":          loss,
        "loss_pct":      pct(loss),
    }


def data_divergence(text):
    sw, met = _swaps_and_mets(text)
    groups = {
        "rate==metric": {"total": 0, "win": 0, "null": 0, "loss": 0},
        "rate!=metric": {"total": 0, "win": 0, "null": 0, "loss": 0},
        "pre-0.4.45":   {"total": 0, "win": 0, "null": 0, "loss": 0},
    }
    for (ts, c, fa, ta, bc, ac, d, mt_i, rb_i, line) in sw:
        if mt_i is None or rb_i is None: key = "pre-0.4.45"
        elif mt_i == rb_i:               key = "rate==metric"
        else:                            key = "rate!=metric"
        g = groups[key]
        g["total"] += 1
        o = _outcome(met, c, ts)
        if o:
            g[o] += 1
    rows = []
    for k in ("rate==metric", "rate!=metric", "pre-0.4.45"):
        g = groups[k]
        meas = g["win"] + g["null"] + g["loss"]
        def pct(x):
            return round(100.0 * x / meas, 1) if meas else 0
        rows.append({
            "category":  k,
            "measured":  meas,
            "win_pct":   pct(g["win"]),
            "null_pct":  pct(g["null"]),
            "loss_pct":  pct(g["loss"]),
            "win":       g["win"],
            "null":      g["null"],
            "loss":      g["loss"],
            "skipped":   g["total"] - meas,
        })
    return rows


def data_churn(text):
    sw, _ = _swaps_and_mets(text)
    counts = defaultdict(int)
    for s in sw:
        counts[s[1]] += 1
    if not counts:
        return {"cookies": 0, "one": 0, "mid": 0, "many": 0, "max": 0}
    one  = sum(1 for v in counts.values() if v == 1)
    mid  = sum(1 for v in counts.values() if 2 <= v <= 4)
    many = sum(1 for v in counts.values() if v >= 5)
    return {"cookies": len(counts), "one": one, "mid": mid,
            "many": many, "max": max(counts.values())}


def data_recent_swaps(text, n=10):
    sw, met = _swaps_and_mets(text)
    rows = []
    for (ts, c, fa, ta, bc, ac, d, mt_i, rb_i, line) in sw:
        o = _outcome(met, c, ts)
        mt_alg = (CONGS[int(mt_i) & 15]
                  if mt_i and mt_i.isdigit() else None)
        rb_alg = (CONGS[int(rb_i) & 15]
                  if rb_i and rb_i.isdigit() else None)
        rows.append({
            "from_alg": CONGS[fa] if fa < 16 else str(fa),
            "to_alg":   CONGS[ta] if ta < 16 else str(ta),
            "d":        int(d),
            "outcome":  o,
            "mt_alg":   mt_alg,
            "rb_alg":   rb_alg,
        })
    return rows[-n:]


def data_recent_proofs(text, n=10):
    lines = [l for l in text.splitlines() if "proof cookie=" in l][-n:]
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
        })
    return out


def collect_all():
    logpath = find_log()
    hosts   = read_map()
    text    = tail(logpath) if logpath else ""
    return {
        "generated_ts":   int(time.time()),
        "hostname":       os.uname().nodename,
        "build":          data_build(logpath),
        "system":         data_system(),
        "tunables":       data_tunables(),
        "buckets":        data_buckets(hosts),
        "metric":         data_metric(hosts),
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


def render_text(d):
    b = d["build"]
    lines = []
    lines.append(f"version    {b['version']}   service  {b['service']}")
    if b["uptime_min"] is not None:
        h, m = divmod(b["uptime_min"], 60)
        lines.append(f"uptime     {h}h {m}m   (started {b['started_utc']} UTC)")
    lines.append(f"log        {b['log_path'] or '(not found)'}")
    log_lines = lines

    sysmap = [
        f"kernel       {d['system']['kernel']}",
        f"default CC   {d['system']['default_cc']}",
    ]
    print(DRULE * (FULL + 2))
    ts = datetime.fromtimestamp(d["generated_ts"], timezone.utc)\
                .strftime('%Y-%m-%dT%H:%M:%SZ')
    print(f"  bpftune  {MIDDOT}  {d['hostname']}  {MIDDOT}  {ts}")
    print(DRULE * (FULL + 2))
    print()
    for row in _twocol("BUILD / SERVICE", log_lines,
                       "SYSTEM FACTS",    sysmap):
        print(row)
    print()
    tun = [f"  {t['name']:<34} {t['value']}" for t in d["tunables"]] or \
          ["  (none seen in journal for this boot)"]
    for row in _full("BPFTUNE-MANAGED TUNABLES", tun):
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
    mt = [f"  {'alg':<10}{'metric':>9}{'votes':>7}{'alive':>7}"]
    for r in d["metric"]:
        mt.append(f"  {r['alg']:<10}{r['metric']:>9.1f}"
                  f"{r['votes']:>7}{r['alive']:>7}")
    pr = [f"  {'alg':<9}{'good':>5}{'prvd':>5}{'maxM':>8}{'avgM':>8}{'n':>5}"]
    for r in d["proof"]:
        a = f"{r['avg_mbps']:.1f}" if r["avg_mbps"] is not None else "-"
        n = f"{r['n']}" if r["n"] is not None else "-"
        pr.append(f"  {r['alg']:<9}{r['good']:>5}{r['proved']:>5}"
                  f"{r['max_mbps']:>8.1f}{a:>8}{n:>5}")
    for row in _twocol("ALGORITHM LEADERBOARD (metric)", mt,
                       "PROOF LEADERBOARD (speed)",     pr):
        print(row)
    print()
    rt = [f"  {'thr':>7}{'n':>5}{'mean':>9}{'min':>9}{'max':>9}"]
    for r in d["rate"]:
        rt.append(f"  {r['thr']:>7}{r['n']:>5}{r['mean']:>9.1f}"
                  f"{r['min']:>9.1f}{r['max']:>9.1f}")
    so = d["swap_outcomes"]
    so_lines = [
        f"  measurable    {so['measurable']:>4}  (unmeasurable {so['unmeasurable']})",
        f"  win           {so['win']:>4}  {so['win_pct']:.0f}%",
        f"  null          {so['null']:>4}  {so['null_pct']:.0f}%",
        f"  loss          {so['loss']:>4}  {so['loss_pct']:.0f}%",
    ]
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
    for row in _full("DIVERGENCE (swap target: rate vs metric)", dv):
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
        status = {"win": " WIN ", "loss": " LOSS", "null": " null"}.get(
            r["outcome"], " ... ")
        rs.append(f"  {r['from_alg']:>9} -> {r['to_alg']:<9} d{r['d']} "
                  f"{status} mt={r['mt_alg'] or '-':<8} rb={r['rb_alg'] or '-':<8}")
    if not rs:
        rs = ["  (none in tail)"]
    for row in _full("RECENT SWAPS", rs):
        print(row)
    print()


def main():
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
'''


# =================== collector ===================

COLLECTOR_SRC = r'''#!/usr/bin/env python3
"""bpftune collector. Run once per minute from cron.

Buckets from `bpftool --json map dump name remote_host_map`.
Swaps from tail /var/log/bpftune-met-*.log.
Snapshot: `bpftune-cli.py --json` -> /var/lib/bpftune/history/current.json

`collected_ts` is wall clock. `boot_ts` is monotonic (log seconds).
"""
import csv, json, os, re, subprocess, sys, time
from pathlib import Path

HIST = Path("/var/lib/bpftune/history")
HIST.mkdir(parents=True, exist_ok=True)
BUCKETS_CSV = HIST / "buckets.v2.csv"
SWAPS_CSV   = HIST / "swaps.csv"
SWAPS_POS   = HIST / ".swaps_pos"
CURRENT_JSON = HIST / "current.json"

SELF_DIR = Path(__file__).resolve().parent
CLI      = SELF_DIR / "bpftune-cli.py"

CONGS = ["cubic", "bbr", "htcp", "dctcp", "scalable", "vegas", "veno",
         "westwood", "reno", "illinois", "yeah", "lp", "bic", "highspeed",
         "hybla", "nv"]
MIN_INST = 2

SWAP_RX = re.compile(
    r"(\d+\.\d+): bpf_trace_printk: swap cookie=(\d+) "
    r"from=(\d+) to=(\d+) bc=(\d+) ac=(\d+) d=(\d+)"
    r"(?: mt=(\d+) rb=(\d+))?")
MET_RX = re.compile(
    r"(\d+\.\d+): bpf_trace_printk: met cookie=(\d+) "
    r"rport=(\d+) alg=(\d+) segs=(\d+) val=(\d+)")


def sh(args, timeout=15):
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:
        return ""


def tcp_rmem():
    out = sh(["sysctl", "-n", "net.ipv4.tcp_rmem"]).split()
    if len(out) != 3:
        return "", "", ""
    try:
        return int(out[0]), int(out[1]), int(out[2])
    except ValueError:
        return "", "", ""


def append_csv(path, row):
    exists = path.exists() and path.stat().st_size > 0
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not exists:
            w.writeheader()
        w.writerow(row)


def find_log():
    files = list(Path("/var/log").glob("bpftune-met-*.log"))
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def collect_buckets(ts_epoch):
    out = sh(["bpftool", "--json", "map", "dump", "name",
              "remote_host_map"])
    try:
        data = json.loads(out)
    except Exception:
        return 0
    if not isinstance(data, list):
        return 0
    rm_min, rm_def, rm_max = tcp_rmem()
    n = 0
    for e in data:
        if not isinstance(e, dict):
            continue
        fmt = e.get("formatted") or {}
        v = fmt.get("value")
        k = fmt.get("key") or {}
        if not isinstance(v, dict):
            continue
        try:
            inst = int(v.get("instances", 0))
        except Exception:
            continue
        if inst < MIN_INST:
            continue
        b = k.get("in6_u", {}).get("u6_addr8")
        if not isinstance(b, list) or len(b) != 16:
            continue
        addr = ".".join(str(x) for x in b[12:16])
        if addr == "0.0.0.1" or addr.startswith(("127.", "169.254.", "0.")):
            continue
        try:
            best_i = int(v.get("best_i", 0) or 0)
        except Exception:
            best_i = 0
        row = {
            "collected_ts": ts_epoch,
            "addr": addr,
            "instances": inst,
            "min_rtt": int(v.get("min_rtt", 0) or 0),
            "ref_rate": int(v.get("max_rate_delivered", 0) or 0),
            "best_i": best_i,
            "best_alg": CONGS[best_i & 15],
            "rate_best_i": int(v.get("rate_best_i", 0) or 0),
            "rate_best_v": int(v.get("rate_best_v", 0) or 0),
        }
        metrics = v.get("metrics") or []
        for i in range(16):
            m = metrics[i] if i < len(metrics) and isinstance(metrics[i], dict) else {}
            row["mv_" + CONGS[i]] = int(m.get("metric_value", 0) or 0)
            row["re_" + CONGS[i]] = int(m.get("rate_ema", 0) or 0)
        row["tcp_rmem_min"] = rm_min
        row["tcp_rmem_def"] = rm_def
        row["tcp_rmem_max"] = rm_max
        append_csv(BUCKETS_CSV, row)
        n += 1
    return n


def collect_swaps():
    logpath = find_log()
    if not logpath:
        return 0
    pos = 0
    if SWAPS_POS.exists():
        try:
            pos = int(SWAPS_POS.read_text().strip())
        except Exception:
            pos = 0
    size = logpath.stat().st_size
    if size < pos:
        pos = 0
    with open(logpath, "rb") as f:
        f.seek(pos)
        text = f.read().decode("utf-8", errors="replace")
        new_pos = f.tell()

    met = {}
    for line in text.splitlines():
        m = MET_RX.search(line)
        if m:
            c = int(m.group(2))
            met.setdefault(c, []).append((float(m.group(1)), int(m.group(6))))

    now_epoch = int(time.time())
    n = 0
    for line in text.splitlines():
        m = SWAP_RX.search(line)
        if not m:
            continue
        boot_ts = float(m.group(1))
        c   = int(m.group(2))
        fa  = int(m.group(3))
        ta  = int(m.group(4))
        d   = int(m.group(7))
        mt_i = m.group(8)
        rb_i = m.group(9)
        pre = post = None
        for (mts, mval) in met.get(c, []):
            if mts < boot_ts + 0.001:
                pre = mval
            elif boot_ts + 3.0 <= mts <= boot_ts + 300.0:
                post = mval
                break
        outcome = ""
        if pre and post:
            r = post / pre
            outcome = "win" if r <= 0.9 else ("loss" if r >= 1.1 else "null")
        mt_alg = CONGS[int(mt_i) & 15] if mt_i and mt_i.isdigit() else ""
        rb_alg = CONGS[int(rb_i) & 15] if rb_i and rb_i.isdigit() else ""
        append_csv(SWAPS_CSV, {
            "collected_ts": now_epoch,
            "boot_ts": boot_ts,
            "cookie": c,
            "from_alg": CONGS[fa] if fa < 16 else str(fa),
            "to_alg": CONGS[ta] if ta < 16 else str(ta),
            "d": d,
            "mt_alg": mt_alg,
            "rb_alg": rb_alg,
            "diverges": "1" if (mt_alg and rb_alg and mt_alg != rb_alg) else "0",
            "outcome": outcome,
        })
        n += 1

    SWAPS_POS.write_text(str(new_pos))
    return n


def run_cli_snapshot():
    if not CLI.exists():
        return False
    try:
        r = subprocess.run([sys.executable, str(CLI), "--json"],
                           capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            print("collector: CLI exited %d" % r.returncode, file=sys.stderr)
            return False
        doc = json.loads(r.stdout)
        tmp = str(CURRENT_JSON) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(doc, f, separators=(",", ":"))
        os.replace(tmp, str(CURRENT_JSON))
        return True
    except Exception as e:
        print("collector: CLI snapshot failed: %s" % e, file=sys.stderr)
        return False


def main():
    ts_epoch = int(time.time())
    nb = collect_buckets(ts_epoch)
    ns = collect_swaps()
    ok = run_cli_snapshot()
    print("collector: buckets=%d swaps=%d cli=%s ts=%d"
          % (nb, ns, "ok" if ok else "fail", ts_epoch))


if __name__ == "__main__":
    main()
'''


# =================== renderer ===================

RENDERER_SRC = r'''#!/usr/bin/env python3
"""bpftune renderer - static Chart.js dashboard + live CLI panel.

Cron: every 5 minutes. Reads buckets.v2.csv + swaps.csv, writes
index.html and data/*.json. The browser also fetches current.json
(every 30s).

`collected_ts` is the only date-safe timestamp (falls back to `ts_epoch`
for v1 rows).
"""
import csv, json, math, os, time
from collections import defaultdict

HIST = "/var/lib/bpftune/history"
DATA = os.path.join(HIST, "data")

RANGES = {
    "1h":  (3600,       60),
    "24h": (86400,      300),
    "7d":  (7 * 86400,  3600),
    "all": (None,       21600),
}

EXTRA_COLS = ["ref_rate", "rate_best_i", "rate_best_v", "instances",
              "tcp_rmem_min", "tcp_rmem_def", "tcp_rmem_max"]


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "y")


def ts_of(row):
    t = to_float(row.get("collected_ts"))
    if t is None:
        t = to_float(row.get("ts_epoch"))
    return t


def wilson(w, n, z=1.96):
    if n == 0:
        return None, None
    p = w / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - h) / d, (c + h) / d


def buckets_source():
    for n in ("buckets.v2.csv", "buckets.v1.csv"):
        p = os.path.join(HIST, n)
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    return None


def load_csv(path):
    if not path or not os.path.exists(path):
        return [], []
    with open(path, newline="") as f:
        rd = csv.DictReader(f)
        return rd.fieldnames or [], list(rd)


def bin_series(rows, lo, width, cols):
    acc = defaultdict(list)
    for r in rows:
        t = ts_of(r)
        if t is None or (lo is not None and t < lo):
            continue
        acc[int((t - (lo or 0)) // width)].append(r)
    base = lo if lo is not None else 0
    ts, out = [], {c: [] for c in cols}
    for b in sorted(acc):
        ts.append(int(base + b * width + width / 2))
        for c in cols:
            vals = [to_float(r.get(c)) for r in acc[b]]
            vals = [v for v in vals if v is not None]
            out[c].append(sum(vals) / len(vals) if vals else None)
    return ts, out


def write_json(name, obj):
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, name)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


def emit_meta(buckets, algs, now):
    rows_24h = [r for r in buckets if (ts_of(r) or 0) > now - 86400]
    by = defaultdict(list)
    for r in rows_24h:
        by[r.get("addr") or "unknown"].append(r)
    entries = []
    for bid, rs in by.items():
        inst = [to_float(r.get("instances")) for r in rs]
        inst = [v for v in inst if v is not None]
        entries.append({
            "id": bid,
            "points": len(rs),
            "instances_mean": round(sum(inst) / len(inst), 2) if inst else 0,
        })
    entries.sort(key=lambda e: e["instances_mean"], reverse=True)

    header, _ = load_csv(buckets_source())
    write_json("meta.json", {
        "generated_ts":  now,
        "ranges":        list(RANGES),
        "algs":          algs,
        "buckets":       entries,
        "default_bucket": entries[0]["id"] if entries else "all",
        "has_tcp_rmem":  "tcp_rmem_max" in header,
    })


def emit_bucket(bid, rows, algs, now):
    doc = {"id": bid, "series": {}}
    for rng, (span, width) in RANGES.items():
        lo = None if span is None else now - span
        cols = [f"re_{a}" for a in algs] + EXTRA_COLS
        if rng == "24h":
            cols += [f"mv_{a}" for a in algs]
        ts, out = bin_series(rows, lo, width, cols)
        doc["series"][rng] = dict({"ts": ts}, **out)

    if rows:
        last = rows[-1]
        doc["last"] = {
            "collected_ts": int(to_float(last.get("collected_ts")) or 0),
            "best_alg":     last.get("best_alg") or "",
            "best_i":       to_float(last.get("best_i")),
            "instances":    to_float(last.get("instances")),
            "ref_rate":     to_float(last.get("ref_rate")),
            "min_rtt":      to_float(last.get("min_rtt")),
            "rate_best_i":  to_float(last.get("rate_best_i")),
            "rate_best_v":  to_float(last.get("rate_best_v")),
            "tcp_rmem_max": to_float(last.get("tcp_rmem_max")),
            "re":           {a: to_float(last.get("re_" + a)) for a in algs},
        }
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in bid)
    write_json("bucket_%s.json" % safe, doc)


def emit_swaps(rows, now):
    doc = {}
    for rng, (span, width) in RANGES.items():
        lo = None if span is None else now - span
        acc = defaultdict(lambda: {0: {"w": 0, "l": 0}, 1: {"w": 0, "l": 0}})
        for r in rows:
            t = ts_of(r)
            if t is None or (lo is not None and t < lo):
                continue
            b = int((t - (lo or 0)) // width)
            d = 1 if truthy(r.get("diverges")) else 0
            o = str(r.get("outcome", "")).strip().lower()
            if o == "win":
                acc[b][d]["w"] += 1
            elif o == "loss":
                acc[b][d]["l"] += 1

        base = lo if lo is not None else 0
        node = {"ts": [], "swaps": []}
        for d in (0, 1):
            node["d%d_rate" % d] = []
            node["d%d_lo"   % d] = []
            node["d%d_hi"   % d] = []
            node["d%d_n"    % d] = []
        for b in sorted(acc):
            node["ts"].append(int(base + b * width + width / 2))
            total = 0
            for d in (0, 1):
                w, l = acc[b][d]["w"], acc[b][d]["l"]
                n = w + l
                total += n
                lo_, hi_ = wilson(w, n)
                node["d%d_rate" % d].append(w / n if n else None)
                node["d%d_lo"   % d].append(lo_)
                node["d%d_hi"   % d].append(hi_)
                node["d%d_n"    % d].append(n)
            node["swaps"].append(total)
        doc[rng] = node
    write_json("swaps.json", doc)


def emit_fleet(buckets, now):
    lo = now - 86400
    width = 300
    rows = [r for r in buckets if (ts_of(r) or 0) > lo]
    by = defaultdict(list)
    for r in rows:
        by[r.get("addr") or "unknown"].append(r)

    labels, cov = [], []
    for bid, rs in sorted(by.items()):
        bins = defaultdict(list)
        for r in rs:
            t = ts_of(r)
            if t is not None:
                bins[int((t - lo) // width)].append(r)
        if not bins:
            continue
        have = sum(1 for grp in bins.values()
                   if any((to_float(x.get("rate_best_v")) or 0) > 0
                          for x in grp))
        labels.append(bid)
        cov.append(round(100 * have / len(bins), 1))
    write_json("fleet.json", {"buckets": labels, "coverage_24h": cov})


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>bpftune</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f6f7f9;
    --fg: #131720;
    --muted: #6b7280;
    --muted-2: #9aa0a6;
    --border: #e6e8ec;
    --border-strong: #d6d9df;
    --card-bg: #ffffff;
    --code-bg: #fbfbfd;
    --subtle: #f2f4f7;
    --shadow: 0 1px 2px rgba(16,24,40,.04);
    --accent: #2f6feb;
    --accent-dim: #2f6feb1a;
    --good: #12a150;
    --good-dim: #12a1501a;
    --bad: #e5484d;
    --bad-dim: #e5484d1a;
    --warn: #d97706;
    --warn-dim: #d977061a;
    --radius: 10px;
    --radius-sm: 6px;
    --gap: 16px;
    --mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0a0c10;
      --fg: #e6e8eb;
      --muted: #8b929b;
      --muted-2: #6b7280;
      --border: #1b1f27;
      --border-strong: #2a2f39;
      --card-bg: #101319;
      --code-bg: #0d1015;
      --subtle: #171b22;
      --shadow: 0 1px 2px rgba(0,0,0,.35);
      --accent: #5b9bff;
      --accent-dim: #5b9bff1f;
      --good: #34d399;
      --good-dim: #34d3991f;
      --bad: #f87171;
      --bad-dim: #f871711f;
      --warn: #fbbf24;
      --warn-dim: #fbbf241f;
    }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--fg);
    font: 14px/1.5 system-ui, -apple-system, "Segoe UI", Roboto,
          "Helvetica Neue", Arial, sans-serif;
    font-feature-settings: "tnum" 1, "cv11" 1, "ss01" 1;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1200px; margin: 0 auto; padding: 24px 20px 64px; }

  header.topbar {
    display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
    padding-bottom: 16px; margin-bottom: 20px;
    border-bottom: 1px solid var(--border);
  }
  .brand {
    font-weight: 600; font-size: 15px; letter-spacing: -0.01em;
    display: flex; align-items: center; gap: 8px;
  }
  .brand .mark {
    width: 8px; height: 8px; border-radius: 2px; background: var(--accent);
  }
  .controls {
    display: flex; gap: 16px; margin-left: auto; align-items: center;
    flex-wrap: wrap;
  }
  .control {
    display: flex; align-items: center; gap: 6px;
    font-size: 11.5px; color: var(--muted);
    text-transform: uppercase; letter-spacing: .05em;
  }
  select {
    font: inherit; font-size: 12.5px;
    text-transform: none; letter-spacing: normal;
    color: var(--fg); background: var(--card-bg);
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-sm);
    padding: 5px 8px;
  }
  select:focus { outline: 2px solid var(--accent-dim); outline-offset: 0; }

  .pill {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 11px; color: var(--muted);
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 3px 10px;
    font-family: var(--mono);
  }
  .pill.flash { color: var(--accent); }

  .card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    padding: 18px;
    margin-bottom: var(--gap);
  }
  .card > h2 {
    margin: 0 0 16px;
    font-size: 11px; font-weight: 600;
    letter-spacing: .09em; text-transform: uppercase;
    color: var(--muted);
    display: flex; align-items: center; gap: 8px;
  }
  .card > h2 .dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-dim);
    flex-shrink: 0;
  }
  .card > h2 .sub {
    margin-left: 6px; color: var(--muted-2);
    font-weight: 500; letter-spacing: .02em; text-transform: none;
    font-family: var(--mono); font-size: 11px;
  }
  .card > h2 .right {
    margin-left: auto; color: var(--muted-2);
    font-weight: 400; text-transform: none;
    letter-spacing: 0; font-family: var(--mono); font-size: 11px;
  }

  .stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 14px 22px;
  }
  .stat { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
  .stat .k {
    font-size: 10.5px; color: var(--muted);
    text-transform: uppercase; letter-spacing: .07em;
  }
  .stat .v {
    font-size: 15px; font-weight: 500;
    font-variant-numeric: tabular-nums;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .stat .v.mono { font-family: var(--mono); font-size: 13px; }

  .rates {
    margin-top: 16px; padding-top: 14px;
    border-top: 1px dashed var(--border);
    display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
  }
  .rates .k {
    font-size: 10.5px; color: var(--muted);
    text-transform: uppercase; letter-spacing: .07em;
  }
  .rates .v {
    font-family: var(--mono); font-size: 12.5px;
    color: var(--fg);
  }

  .lv-grid {
    display: grid;
    grid-template-columns: repeat(12, 1fr);
    gap: 14px;
  }
  .lv-grid > section {
    grid-column: span 12;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    padding: 14px 16px;
    min-width: 0;
  }
  @media (min-width: 900px) {
    .lv-grid > section.c6 { grid-column: span 6; }
    .lv-grid > section.c4 { grid-column: span 4; }
    .lv-grid > section.c8 { grid-column: span 8; }
  }
  .lv-grid h3 {
    margin: 0 0 10px;
    font-size: 10.5px; font-weight: 600;
    letter-spacing: .09em; text-transform: uppercase;
    color: var(--muted);
    display: flex; align-items: center; gap: 6px;
  }
  .lv-grid h3 .cnt {
    margin-left: auto; color: var(--muted-2);
    font-family: var(--mono); font-size: 10.5px;
    font-weight: 500; letter-spacing: 0; text-transform: none;
  }

  .kv { display: flex; flex-direction: column; gap: 5px; }
  .kv .row {
    display: flex; justify-content: space-between; gap: 12px;
    font-size: 12.5px; align-items: baseline;
  }
  .kv .k { color: var(--muted); }
  .kv .v {
    font-family: var(--mono); font-size: 12px;
    text-align: right; overflow-wrap: anywhere;
  }
  .kv .v.hi { color: var(--fg); font-weight: 500; }
  .kv .v.dim { color: var(--muted-2); }

  table.tbl { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  table.tbl th, table.tbl td {
    padding: 6px 4px; text-align: right;
    font-variant-numeric: tabular-nums;
    border-bottom: 1px solid var(--border);
  }
  table.tbl th:first-child, table.tbl td:first-child {
    text-align: left; padding-left: 0;
  }
  table.tbl th:last-child, table.tbl td:last-child { padding-right: 0; }
  table.tbl thead th {
    color: var(--muted); font-weight: 500;
    font-size: 10px; text-transform: uppercase; letter-spacing: .07em;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border-strong);
  }
  table.tbl tbody tr:last-child td { border-bottom: none; }
  table.tbl td.mono { font-family: var(--mono); font-size: 12px; }
  table.tbl td.name { color: var(--fg); font-weight: 500; }
  table.tbl td.dim { color: var(--muted); }

  .bar {
    position: relative; display: inline-block;
    height: 6px; width: 90px; vertical-align: middle;
    background: var(--subtle); border-radius: 3px; overflow: hidden;
    margin-right: 8px;
  }
  .bar > span {
    position: absolute; inset: 0 auto 0 0;
    background: var(--accent); border-radius: 3px;
  }

  .sp {
    display: inline-flex; align-items: center;
    font-size: 10.5px; font-weight: 600;
    padding: 1px 7px; border-radius: 999px;
    text-transform: uppercase; letter-spacing: .05em;
  }
  .sp.win  { color: var(--good); background: var(--good-dim); }
  .sp.loss { color: var(--bad);  background: var(--bad-dim); }
  .sp.null { color: var(--muted); background: var(--subtle); }
  .sp.good { color: var(--warn); background: var(--warn-dim); }
  .sp.proved { color: var(--good); background: var(--good-dim); }
  .sp.dash { color: var(--muted-2); background: var(--subtle); }

  .list { display: flex; flex-direction: column; }
  .list .item {
    display: grid;
    grid-template-columns: 1fr auto auto;
    gap: 8px 12px;
    align-items: center;
    padding: 7px 0;
    border-bottom: 1px solid var(--border);
    font-size: 12.5px;
  }
  .list .item:last-child { border-bottom: none; }
  .list .item .flow {
    font-family: var(--mono); font-size: 12px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .list .item .flow .arrow { color: var(--muted-2); padding: 0 4px; }
  .list .item .meta {
    color: var(--muted); font-family: var(--mono); font-size: 11px;
    white-space: nowrap;
  }

  .bigstats {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 10px;
  }
  .bigstats .big {
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 12px 14px;
    background: var(--subtle);
  }
  .bigstats .big .k {
    font-size: 10.5px; color: var(--muted);
    text-transform: uppercase; letter-spacing: .07em;
  }
  .bigstats .big .v {
    font-size: 22px; font-weight: 600;
    font-variant-numeric: tabular-nums; margin-top: 3px;
  }
  .bigstats .big .p {
    color: var(--muted); font-family: var(--mono);
    font-size: 11px; margin-top: 2px;
  }
  .bigstats .big.win  { border-color: var(--good); }
  .bigstats .big.win .v { color: var(--good); }
  .bigstats .big.loss { border-color: var(--bad); }
  .bigstats .big.loss .v { color: var(--bad); }
  .bigstats .big.null { border-color: var(--border-strong); }

  .tun-grid {
    display: grid;
    grid-template-columns: 1fr;
    gap: 4px 20px;
    font-size: 12.5px;
  }
  @media (min-width: 700px) {
    .tun-grid { grid-template-columns: 1fr 1fr; }
  }
  .tun-grid .row {
    display: flex; justify-content: space-between; gap: 10px;
    padding: 3px 0; border-bottom: 1px solid var(--border);
    align-items: baseline;
  }
  .tun-grid .row:last-child { border-bottom: none; }
  .tun-grid .k {
    color: var(--muted); font-family: var(--mono); font-size: 11.5px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .tun-grid .v {
    font-family: var(--mono); font-size: 11.5px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    color: var(--fg);
  }

  .cellbar {
    display: inline-flex; width: 100%; height: 18px;
    border-radius: 3px; overflow: hidden; background: var(--subtle);
    color: #fff; font-size: 10px; font-weight: 600;
  }
  .cellbar > span {
    display: flex; align-items: center; justify-content: center;
    min-width: 0; overflow: hidden; white-space: nowrap;
  }
  .cellbar .w { background: var(--good); }
  .cellbar .n { background: var(--muted-2); color: #fff; }
  .cellbar .l { background: var(--bad); }

  .chart-box { position: relative; width: 100%; min-width: 0; }
  .chart-box.h-sm { height: 100px; }
  .chart-box.h-md { height: 150px; }
  .chart-box.h-lg { height: 220px; }
  .chart-box.h-xl { height: 300px; }
  .chart-box > canvas {
    position: absolute; inset: 0; width: 100% !important;
    height: 100% !important;
  }

  .footer {
    margin-top: 32px; padding-top: 16px;
    border-top: 1px solid var(--border);
    color: var(--muted-2); font-size: 11px;
    display: flex; gap: 12px; flex-wrap: wrap;
  }
  .footer .sep { color: var(--border-strong); }

  .placeholder {
    color: var(--muted-2); font-size: 12px; font-style: italic;
    padding: 6px 0;
  }

  @media (max-width: 640px) {
    .wrap { padding: 16px 12px 48px; }
    .card { padding: 12px; }
    .controls { gap: 10px; }
    .bigstats { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div class="wrap">

  <header class="topbar">
    <div class="brand"><span class="mark"></span>bpftune</div>
    <div class="controls">
      <label class="control">bucket
        <select id="bucket"></select>
      </label>
      <label class="control">range
        <select id="range"></select>
      </label>
      <span id="gen" class="pill">initial</span>
    </div>
  </header>

  <section class="card">
    <h2>
      <span class="dot"></span>now
      <span class="sub" id="nowbucket">-</span>
      <span class="right" id="nowupdated"></span>
    </h2>
    <div class="stats">
      <div class="stat"><span class="k">instances</span>
        <span class="v mono" id="n_inst">-</span></div>
      <div class="stat"><span class="k">reference rate</span>
        <span class="v mono" id="n_ref">-</span></div>
      <div class="stat"><span class="k">min rtt</span>
        <span class="v mono" id="n_rtt">-</span></div>
      <div class="stat"><span class="k">best algorithm</span>
        <span class="v" id="n_best">-</span></div>
      <div class="stat"><span class="k">rate-best</span>
        <span class="v mono" id="n_rbest">-</span></div>
      <div class="stat"><span class="k">tcp_rmem max</span>
        <span class="v mono" id="n_rmem">-</span></div>
    </div>
    <div class="rates">
      <span class="k">rate_ema</span>
      <span class="v" id="n_rates">-</span>
    </div>
  </section>

  <div class="lv-grid" id="live">

    <section class="c6">
      <h3>build / service</h3>
      <div class="kv" id="lv-build"><div class="placeholder">loading&hellip;</div></div>
    </section>

    <section class="c6">
      <h3>system</h3>
      <div class="kv" id="lv-system"></div>
    </section>

    <section class="c12">
      <h3>bpftune-managed tunables <span class="cnt" id="lv-tun-cnt"></span></h3>
      <div class="tun-grid" id="lv-tunables"></div>
    </section>

    <section class="c12">
      <h3>top destination buckets</h3>
      <div id="lv-buckets"></div>
    </section>

    <section class="c6">
      <h3>metric leaderboard</h3>
      <div id="lv-metric"></div>
    </section>

    <section class="c6">
      <h3>proof leaderboard</h3>
      <div id="lv-proof"></div>
    </section>

    <section class="c8">
      <h3>rate progression <span class="cnt">client &middot; Mb/s</span></h3>
      <div id="lv-rate"></div>
    </section>

    <section class="c4">
      <h3>swap outcomes</h3>
      <div id="lv-swapout"></div>
    </section>

    <section class="c12">
      <h3>divergence <span class="cnt">swap target: rate vs metric</span></h3>
      <div id="lv-div"></div>
    </section>

    <section class="c6">
      <h3>recent proofs</h3>
      <div id="lv-proofs"></div>
    </section>

    <section class="c6">
      <h3>cookie churn</h3>
      <div class="kv" id="lv-churn"></div>
    </section>

    <section class="c12">
      <h3>recent swaps</h3>
      <div id="lv-swaps"></div>
    </section>

  </div>

  <section class="card">
    <h2><span class="dot"></span>rate_ema per algorithm &mdash; Mb/s</h2>
    <div class="chart-box h-xl"><canvas id="rate"></canvas></div>
  </section>

  <section class="card">
    <h2><span class="dot"></span>reference rate &mdash; Mb/s</h2>
    <div class="chart-box h-sm"><canvas id="ref"></canvas></div>
  </section>

  <section class="card">
    <h2><span class="dot"></span>tcp_rmem max (bytes)</h2>
    <div class="chart-box h-sm"><canvas id="rmem"></canvas></div>
  </section>

  <section class="card">
    <h2><span class="dot"></span>divergence &mdash; win rate with 95% Wilson CI</h2>
    <div class="chart-box h-md"><canvas id="div"></canvas></div>
  </section>

  <section class="card">
    <h2><span class="dot"></span>swaps per bin</h2>
    <div class="chart-box h-sm"><canvas id="swaps"></canvas></div>
  </section>

  <section class="card">
    <h2><span class="dot"></span>rate-board coverage &mdash; last 24h</h2>
    <div class="chart-box h-lg"><canvas id="fleet"></canvas></div>
  </section>

  <div class="footer">
    <span>bpftune dashboard</span>
    <span class="sep">|</span>
    <span>collected_ts is the only date-safe column</span>
    <span class="sep">|</span>
    <span id="footgen">-</span>
  </div>

</div>

<script>
(function () {
  var el = document.getElementById("gen");
  if (el) el.textContent = "html @ " + new Date().toISOString().slice(11, 19);
})();
</script>

<script>
(function () {
  var PALETTE = [
    "#4e79a7", "#f28e2c", "#e15759", "#76b7b2",
    "#59a14f", "#edc949", "#af7aa1", "#ff9da7",
    "#9c755f", "#bab0ab", "#1b9e77", "#d95f02",
    "#7570b3", "#e7298a", "#66a61e", "#e6ab02"
  ];

  var gen = document.getElementById("gen");
  var footgen = document.getElementById("footgen");

  function status(msg, isErr) {
    if (gen) {
      gen.textContent = msg;
      gen.style.color = isErr ? "#e5484d" : "";
      if (!isErr) {
        gen.classList.add("flash");
        setTimeout(function () { gen.classList.remove("flash"); }, 300);
      }
    }
  }
  function err(msg, e) { status(msg, true); if (e) console.error(msg, e); }

  function loadScript(url) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = url;
      s.onload = resolve;
      s.onerror = function () { reject(new Error("failed to load " + url)); };
      document.head.appendChild(s);
    });
  }

  function applyChartDefaults() {
    var dark = window.matchMedia &&
               window.matchMedia("(prefers-color-scheme: dark)").matches;
    var grid = dark ? "rgba(255,255,255,.06)" : "rgba(20,30,50,.06)";
    var tick = dark ? "#8b929b" : "#6b7280";
    Chart.defaults.font.family =
      "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";
    Chart.defaults.font.size = 11;
    Chart.defaults.color = tick;
    Chart.defaults.borderColor = grid;
    Chart.defaults.elements.line.borderWidth = 1.5;
    Chart.defaults.elements.point.radius = 0;
    Chart.defaults.elements.point.hoverRadius = 3;
    Chart.defaults.animation = false;
    Chart.defaults.plugins.legend.labels.boxWidth = 10;
    Chart.defaults.plugins.legend.labels.boxHeight = 10;
    Chart.defaults.plugins.legend.labels.padding = 8;
    Chart.defaults.plugins.tooltip.backgroundColor = dark ? "#1c2028" : "#fff";
    Chart.defaults.plugins.tooltip.borderColor = dark ? "#2a2f39" : "#e5e7eb";
    Chart.defaults.plugins.tooltip.borderWidth = 1;
    Chart.defaults.plugins.tooltip.titleColor = dark ? "#e6e8eb" : "#131720";
    Chart.defaults.plugins.tooltip.bodyColor  = dark ? "#e6e8eb" : "#131720";
    Chart.defaults.plugins.tooltip.padding = 8;
    Chart.defaults.plugins.tooltip.cornerRadius = 6;
  }

  function $(id) { return document.getElementById(id); }
  function setHTML(id, s) { var e = $(id); if (e) e.innerHTML = s; }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;",
                '"': "&quot;", "'": "&#39;" })[c];
    });
  }
  function fmtN(v) {
    return (v == null) ? "-" : Math.round(v).toLocaleString();
  }
  function fmtMbps(v) {
    return (v == null) ? "-" : (v / 125000).toFixed(1);
  }
  function fmtRe(v) {
    return (v == null) ? "-" : (v * 0.8).toFixed(1);
  }
  function scaleRe(v) {
    return v == null ? null : v * 0.8;
  }
  function fmtRTT(v) {
    return (v == null) ? "-" : (v / 1000).toFixed(1) + " ms";
  }
  function fmtBytes(b) {
    if (b == null) return "-";
    if (b >= 1e9) return (b / 1e9).toFixed(2) + " GB";
    if (b >= 1e6) return (b / 1e6).toFixed(1) + " MB";
    return Math.round(b) + " B";
  }
  function relTime(epochSec) {
    if (!epochSec) return "";
    var dt = Math.max(0, Math.floor(Date.now() / 1000) - epochSec);
    if (dt < 60) return dt + "s ago";
    if (dt < 3600) return Math.floor(dt / 60) + "m ago";
    if (dt < 86400) return Math.floor(dt / 3600) + "h ago";
    return Math.floor(dt / 86400) + "d ago";
  }

  function renderBuild(b) {
    var rows = [
      ["version",   b.version, "hi"],
      ["service",   b.service, b.service === "active" ? "hi" : ""],
    ];
    if (b.uptime_min != null) {
      var h = Math.floor(b.uptime_min / 60);
      var m = b.uptime_min % 60;
      rows.push(["uptime", h + "h " + m + "m"]);
    }
    if (b.started_utc) rows.push(["started", b.started_utc + " UTC", "dim"]);
    if (b.log_path)   rows.push(["log", b.log_path, "dim"]);
    setHTML("lv-build", rows.map(function (r) {
      return '<div class="row"><span class="k">' + esc(r[0]) + '</span>' +
             '<span class="v ' + (r[2] || "") + '">' + esc(r[1]) + '</span></div>';
    }).join(""));
  }

  function renderSystem(s) {
    var rows = [
      ["kernel",     s.kernel,     "hi"],
      ["default cc", s.default_cc, "hi"],
    ];
    setHTML("lv-system", rows.map(function (r) {
      return '<div class="row"><span class="k">' + esc(r[0]) + '</span>' +
             '<span class="v ' + (r[2] || "") + '">' + esc(r[1]) + '</span></div>';
    }).join(""));
  }

  function renderTunables(t) {
    var cnt = $("lv-tun-cnt");
    if (cnt) cnt.textContent = t.length + " keys";
    if (!t.length) {
      setHTML("lv-tunables",
              '<div class="placeholder">(none seen in journal this boot)</div>');
      return;
    }
    setHTML("lv-tunables", t.map(function (r) {
      return '<div class="row"><span class="k">' + esc(r.name) + '</span>' +
             '<span class="v">' + esc(r.value) + '</span></div>';
    }).join(""));
  }

  function renderBuckets(rows) {
    if (!rows.length) {
      setHTML("lv-buckets", '<div class="placeholder">(no buckets)</div>');
      return;
    }
    var html = '<table class="tbl"><thead><tr>' +
      '<th>dest</th><th>instances</th><th>min rtt</th>' +
      '<th>ref rate</th><th>best alg</th><th>algs</th>' +
      '</tr></thead><tbody>';
    rows.forEach(function (r) {
      html += '<tr>' +
        '<td class="mono name">' + esc(r.dest) + '</td>' +
        '<td class="mono">' + fmtN(r.inst) + '</td>' +
        '<td class="mono dim">' + (r.rtt_us / 1000).toFixed(1) + ' ms</td>' +
        '<td class="mono">' + r.ref_mbps.toFixed(1) + '</td>' +
        '<td>' + esc(r.best_alg) + '</td>' +
        '<td class="mono dim">' + r.n_alg + '</td>' +
        '</tr>';
    });
    setHTML("lv-buckets", html + '</tbody></table>');
  }

  function renderMetric(rows) {
    if (!rows.length) {
      setHTML("lv-metric", '<div class="placeholder">(no metrics yet)</div>');
      return;
    }
    var html = '<table class="tbl"><thead><tr>' +
      '<th>alg</th><th>metric</th><th>votes</th><th>alive</th>' +
      '</tr></thead><tbody>';
    rows.forEach(function (r) {
      html += '<tr>' +
        '<td class="name">' + esc(r.alg) + '</td>' +
        '<td class="mono">' + r.metric.toFixed(1) + '</td>' +
        '<td class="mono dim">' + fmtN(r.votes) + '</td>' +
        '<td class="mono dim">' + fmtN(r.alive) + '</td>' +
        '</tr>';
    });
    setHTML("lv-metric", html + '</tbody></table>');
  }

  function renderProof(rows) {
    if (!rows.length) {
      setHTML("lv-proof", '<div class="placeholder">(none in tail)</div>');
      return;
    }
    var maxv = 1;
    rows.forEach(function (r) { if (r.max_mbps > maxv) maxv = r.max_mbps; });
    var html = '<table class="tbl"><thead><tr>' +
      '<th>alg</th><th>good</th><th>proved</th>' +
      '<th>max Mb/s</th><th>avg</th><th>n</th>' +
      '</tr></thead><tbody>';
    rows.forEach(function (r) {
      var w = Math.max(2, Math.round(90 * r.max_mbps / maxv));
      html += '<tr>' +
        '<td class="name">' + esc(r.alg) + '</td>' +
        '<td class="mono dim">' + r.good + '</td>' +
        '<td class="mono dim">' + r.proved + '</td>' +
        '<td class="mono" style="white-space:nowrap">' +
          '<span class="bar"><span style="width:' + w + 'px"></span></span>' +
          r.max_mbps.toFixed(1) +
        '</td>' +
        '<td class="mono dim">' +
          (r.avg_mbps == null ? "-" : r.avg_mbps.toFixed(1)) + '</td>' +
        '<td class="mono dim">' +
          (r.n == null ? "-" : r.n) + '</td>' +
        '</tr>';
    });
    setHTML("lv-proof", html + '</tbody></table>');
  }

  function renderRate(rows) {
    if (!rows.length) {
      setHTML("lv-rate", '<div class="placeholder">(no midsamp lines)</div>');
      return;
    }
    var html = '<table class="tbl"><thead><tr>' +
      '<th>thr</th><th>n</th><th>mean</th><th>min</th><th>max</th>' +
      '</tr></thead><tbody>';
    rows.forEach(function (r) {
      html += '<tr>' +
        '<td class="mono">' + fmtN(r.thr) + '</td>' +
        '<td class="mono dim">' + r.n + '</td>' +
        '<td class="mono">'   + r.mean.toFixed(1) + '</td>' +
        '<td class="mono dim">' + r.min.toFixed(1) + '</td>' +
        '<td class="mono">'   + r.max.toFixed(1) + '</td>' +
        '</tr>';
    });
    setHTML("lv-rate", html + '</tbody></table>');
  }

  function renderSwapOutcomes(so) {
    var html = '<div class="bigstats">' +
      '<div class="big win"><div class="k">win</div>' +
        '<div class="v">' + so.win + '</div>' +
        '<div class="p">' + so.win_pct.toFixed(0) + '%</div></div>' +
      '<div class="big null"><div class="k">null</div>' +
        '<div class="v">' + so.null + '</div>' +
        '<div class="p">' + so.null_pct.toFixed(0) + '%</div></div>' +
      '<div class="big loss"><div class="k">loss</div>' +
        '<div class="v">' + so.loss + '</div>' +
        '<div class="p">' + so.loss_pct.toFixed(0) + '%</div></div>' +
      '</div>' +
      '<div class="kv" style="margin-top:10px">' +
        '<div class="row"><span class="k">measurable</span>' +
          '<span class="v">' + so.measurable + '</span></div>' +
        '<div class="row"><span class="k">unmeasurable</span>' +
          '<span class="v dim">' + so.unmeasurable + '</span></div>' +
      '</div>';
    setHTML("lv-swapout", html);
  }

  function renderDivergence(rows) {
    if (!rows.length) {
      setHTML("lv-div", '<div class="placeholder">(no swaps)</div>');
      return;
    }
    var html = '<table class="tbl"><thead><tr>' +
      '<th>category</th><th style="width:42%"></th>' +
      '<th>measured</th><th>skipped</th>' +
      '</tr></thead><tbody>';
    rows.forEach(function (r) {
      var w = r.win_pct, n = r.null_pct, l = r.loss_pct;
      var has = r.measured > 0;
      var bar = '<div class="cellbar">';
      if (has) {
        if (w > 0) bar += '<span class="w" style="width:' + w + '%">' +
                          (w >= 8 ? w.toFixed(0) + '%' : '') + '</span>';
        if (n > 0) bar += '<span class="n" style="width:' + n + '%">' +
                          (n >= 8 ? n.toFixed(0) + '%' : '') + '</span>';
        if (l > 0) bar += '<span class="l" style="width:' + l + '%">' +
                          (l >= 8 ? l.toFixed(0) + '%' : '') + '</span>';
      } else {
        bar += '<span class="n" style="width:100%">no data</span>';
      }
      bar += '</div>';
      html += '<tr>' +
        '<td class="name">' + esc(r.category) + '</td>' +
        '<td>' + bar + '</td>' +
        '<td class="mono">' + r.measured + '</td>' +
        '<td class="mono dim">' + r.skipped + '</td>' +
        '</tr>';
    });
    setHTML("lv-div", html + '</tbody></table>');
  }

  function renderChurn(c) {
    var rows = [
      ["cookies swapped", c.cookies, "hi"],
      ["one-off",         c.one,     ""],
      ["2-4x",            c.mid,     ""],
      ["5x+",             c.many,    ""],
      ["max per cookie",  c.max,     ""],
    ];
    setHTML("lv-churn", rows.map(function (r) {
      return '<div class="row"><span class="k">' + esc(r[0]) + '</span>' +
             '<span class="v ' + (r[2] || "") + '">' + r[1] + '</span></div>';
    }).join(""));
  }

  function renderRecentProofs(rows) {
    if (!rows.length) {
      setHTML("lv-proofs", '<div class="placeholder">(none)</div>');
      return;
    }
    var html = '<div class="list">';
    rows.slice().reverse().forEach(function (r) {
      html += '<div class="item">' +
        '<span class="flow">' + esc(r.alg) + '</span>' +
        '<span class="meta">' + r.mbps.toFixed(1) + ' Mb/s</span>' +
        '<span class="sp ' + r.tier + '">' + r.tier + '</span>' +
        '</div>';
    });
    setHTML("lv-proofs", html + '</div>');
  }

  function renderRecentSwaps(rows) {
    if (!rows.length) {
      setHTML("lv-swaps", '<div class="placeholder">(none in tail)</div>');
      return;
    }
    var html = '<div class="list">';
    rows.slice().reverse().forEach(function (r) {
      var sp = r.outcome ?
        '<span class="sp ' + r.outcome + '">' + r.outcome + '</span>' :
        '<span class="sp dash">&hellip;</span>';
      html += '<div class="item">' +
        '<span class="flow">' + esc(r.from_alg) +
          '<span class="arrow">&rarr;</span>' + esc(r.to_alg) + '</span>' +
        '<span class="meta">d' + r.d + ' &middot; mt=' + esc(r.mt_alg || "-") +
          ' rb=' + esc(r.rb_alg || "-") + '</span>' +
        sp +
        '</div>';
    });
    setHTML("lv-swaps", html + '</div>');
  }

  function renderLiveState(doc) {
    renderBuild(doc.build || {});
    renderSystem(doc.system || {});
    renderTunables(doc.tunables || []);
    renderBuckets(doc.buckets || []);
    renderMetric(doc.metric || []);
    renderProof(doc.proof || []);
    renderRate(doc.rate || []);
    renderSwapOutcomes(doc.swap_outcomes || {});
    renderDivergence(doc.divergence || []);
    renderChurn(doc.churn || {});
    renderRecentProofs(doc.recent_proofs || []);
    renderRecentSwaps(doc.recent_swaps || []);
  }

  function liveRefresh() {
    fetch("current.json", {cache: "no-store"}).then(function (r) {
      if (!r.ok) throw new Error("current.json: " + r.status);
      return r.json();
    }).then(function (doc) {
      renderLiveState(doc);
    }).catch(function (e) {
      setHTML("lv-build",
              '<div class="placeholder">current.json unavailable: ' +
              esc(e.message) + '</div>');
    });
  }

  function renderNow() {
    var doc = state.bucketDoc;
    if (!doc) return;
    var bid = $("bucket").value;
    var sub = $("nowbucket"); if (sub) sub.textContent = bid;
    var L = doc.last || {};
    var setT = function (id, s) { var e = $(id); if (e) e.textContent = s; };
    setT("n_inst", fmtN(L.instances));
    setT("n_ref",  fmtMbps(L.ref_rate) + " Mb/s");
    setT("n_rtt",  fmtRTT(L.min_rtt));
    setT("n_best", L.best_alg || "-");
    setT("n_rmem", fmtBytes(L.tcp_rmem_max));
    setT("nowupdated",
         L.collected_ts ? "updated " + relTime(L.collected_ts) : "");

    var algs = state.meta.algs;
    var rates = [];
    for (var i = 0; i < algs.length; i++) {
      var a = algs[i];
      var v = (L.re && L.re[a] != null) ? L.re[a] : null;
      if (v != null && v > 0) rates.push({a: a, v: v});
    }
    rates.sort(function (x, y) { return y.v - x.v; });

    if (rates.length) {
      setT("n_rbest", rates[0].a + "  " + fmtRe(rates[0].v) + " Mb/s");
      var line = rates.slice(0, 8).map(function (x) {
        return x.a + " " + fmtRe(x.v);
      }).join("  ·  ");
      setT("n_rates", line);
    } else {
      setT("n_rbest", "-");
      setT("n_rates", "(no live rates for this bucket)");
    }
  }

  var state  = { meta: null, bucketDoc: null, swaps: null, fleet: null };
  var charts = {};

  function mk(id, cfg) {
    if (charts[id]) { charts[id].destroy(); }
    var cv = $(id);
    if (!cv) return;
    charts[id] = new Chart(cv, cfg);
  }

  function j(url) {
    return fetch(url, {cache: "no-store"}).then(function (r) {
      if (!r.ok) { throw new Error(url + ": " + r.status); }
      return r.json();
    });
  }

  function lineData(cols, series, ts, colors, scale) {
    scale = scale || function (v) { return v; };
    return cols.map(function (c, i) {
      return {
        label: c,
        data: (series[c] || []).map(function (y, k) {
          return {x: ts[k] * 1000, y: y == null ? null : scale(y)};
        }),
        borderColor: colors[i % colors.length],
        backgroundColor: colors[i % colors.length],
        pointRadius: 0,
        borderWidth: 1.5,
        tension: 0.15,
        spanGaps: true,
      };
    });
  }

  function timeOpts(extra) {
    var base = {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: {mode: "nearest", intersect: false},
      layout: {padding: {top: 4, right: 8, bottom: 0, left: 0}},
      scales: {
        x: {
          type: "time",
          time: {tooltipFormat: "MMM d, HH:mm"},
          grid: {display: false},
          ticks: {maxRotation: 0, autoSkipPadding: 24, padding: 4},
        },
        y: {
          beginAtZero: false,
          grid: {drawTicks: false},
          ticks: {maxTicksLimit: 5, padding: 6},
        },
      },
      plugins: {
        legend: {display: false},
        tooltip: {displayColors: true, boxPadding: 4},
      },
    };
    return Object.assign(base, extra || {});
  }

  function renderBucket() {
    var doc = state.bucketDoc, algs = state.meta.algs;
    var rng = $("range").value;
    var s = doc.series[rng];
    var ts = s.ts;

    mk("rate", {
      type: "line",
      data: {datasets: lineData(algs.map(function (a) {
        return "re_" + a;
      }), s, ts, PALETTE, scaleRe)},
      options: timeOpts({
        plugins: {
          legend: {
            display: true, position: "bottom", align: "start",
            labels: {boxWidth: 8, boxHeight: 8, padding: 8,
                     font: {size: 10.5}},
          },
        },
      }),
    });

    mk("ref", {
      type: "line",
      data: {datasets: lineData(["ref_rate"], s, ts, ["#e15759"])},
      options: timeOpts(),
    });

    mk("rmem", {
      type: "line",
      data: {datasets: lineData(["tcp_rmem_max"], s, ts, ["#4e79a7"])},
      options: timeOpts(),
    });
  }

  function renderSwaps() {
    var doc = state.swaps, rng = $("range").value;
    var d = doc[rng];
    if (!d) return;
    var ts = d.ts;

    function mkLine(key, label, color, dash) {
      return {
        label: label,
        data: d[key].map(function (y, k) {
          return {x: ts[k] * 1000, y: y};
        }),
        borderColor: color,
        backgroundColor: color,
        borderDash: dash || [],
        pointRadius: 0,
        borderWidth: 1.5,
        spanGaps: true,
      };
    }

    mk("div", {
      type: "line",
      data: {datasets: [
        mkLine("d1_rate", "diverges=1", "#59a14f"),
        mkLine("d1_lo", "d1 95% lo",  "#59a14f", [4, 3]),
        mkLine("d1_hi", "d1 95% hi",  "#59a14f", [4, 3]),
        mkLine("d0_rate", "diverges=0", "#e15759"),
        mkLine("d0_lo", "d0 95% lo",  "#e15759", [4, 3]),
        mkLine("d0_hi", "d0 95% hi",  "#e15759", [4, 3]),
      ]},
      options: timeOpts({
        scales: {
          x: {type: "time", grid: {display: false},
              ticks: {maxRotation: 0, autoSkipPadding: 24}},
          y: {min: 0, max: 1, grid: {drawTicks: false},
              ticks: {maxTicksLimit: 5, padding: 6,
                      callback: function (v) {
                        return Math.round(v * 100) + "%";
                      }}},
        },
        plugins: {
          legend: {
            display: true, position: "bottom", align: "start",
            labels: {boxWidth: 8, boxHeight: 8, padding: 8,
                     font: {size: 10.5},
                     filter: function (item) {
                       return !/_lo$|_hi$/.test(item.text);
                     }},
          },
        },
      }),
    });

    mk("swaps", {
      type: "bar",
      data: {
        labels: ts.map(function (t) { return new Date(t * 1000); }),
        datasets: [{
          label: "swaps",
          data: d.swaps,
          backgroundColor: "#4e79a7",
          borderColor: "#4e79a7",
          borderRadius: 2,
          maxBarThickness: 14,
        }],
      },
      options: timeOpts({
        scales: {
          x: {type: "time", grid: {display: false}, ticks: {maxRotation: 0}},
          y: {beginAtZero: true, grid: {drawTicks: false},
              ticks: {maxTicksLimit: 4, padding: 6}},
        },
      }),
    });
  }

  function renderFleet() {
    var f = state.fleet;
    mk("fleet", {
      type: "bar",
      data: {
        labels: f.buckets,
        datasets: [{
          label: "% bins with leader",
          data: f.coverage_24h,
          backgroundColor: "#59a14f",
          borderColor: "#59a14f",
          borderRadius: 2,
          maxBarThickness: 10,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        animation: false, indexAxis: "y",
        layout: {padding: {top: 4, right: 12, bottom: 0, left: 0}},
        scales: {
          x: {min: 0, max: 100, grid: {drawTicks: false},
              ticks: {maxTicksLimit: 6, padding: 6,
                      callback: function (v) { return v + "%"; }}},
          y: {grid: {display: false}, ticks: {font: {size: 10},
              autoSkip: false, padding: 4}},
        },
      },
    });
  }

  function loadBucket(id) {
    return j("data/bucket_" + id + ".json").then(function (doc) {
      state.bucketDoc = doc;
      renderBucket();
      renderNow();
    });
  }

  function boot() {
    liveRefresh();
    setInterval(liveRefresh, 30000);

    status("loading charts\u2026");
    loadScript("https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js")
      .then(function () {
        return loadScript("https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js");
      })
      .then(function () {
        applyChartDefaults();
        status("loading data\u2026");
        return Promise.all([
          j("data/meta.json"),
          j("data/swaps.json"),
          j("data/fleet.json"),
        ]);
      })
      .then(function (results) {
        state.meta  = results[0];
        state.swaps = results[1];
        state.fleet = results[2];

        var bs = $("bucket");
        var html = "";
        for (var k = 0; k < state.meta.buckets.length; k++) {
          var b = state.meta.buckets[k];
          html += '<option value="' + b.id + '">' + b.id +
                  ' (' + b.points + ')</option>';
        }
        bs.innerHTML = html;
        bs.value = state.meta.default_bucket;

        var rs = $("range");
        var rhtml = "";
        for (var m = 0; m < state.meta.ranges.length; m++) {
          rhtml += '<option value="' + state.meta.ranges[m] + '">' +
                   state.meta.ranges[m] + '</option>';
        }
        rs.innerHTML = rhtml;
        rs.value = "24h";

        var stamp = new Date(state.meta.generated_ts * 1000).toISOString()
                        .replace("T", " ").slice(0, 19) + "Z";
        status("updated " + relTime(state.meta.generated_ts));
        if (footgen) footgen.textContent = "rendered " + stamp;

        bs.onchange = function () { loadBucket(bs.value); };
        rs.onchange = function () { renderBucket(); renderSwaps(); };

        return loadBucket(state.meta.default_bucket);
      })
      .then(function () {
        renderSwaps();
        renderFleet();
      })
      .catch(function (e) {
        err("FAIL: " + (e && e.message ? e.message : e), e);
      });
  }

  boot();
})();
</script>
</body>
</html>
"""


def main():
    now = int(time.time())
    bfile = buckets_source()
    if not bfile:
        print("renderer: no buckets CSV found yet")
        return 1

    header, buckets = load_csv(bfile)
    _, swaps = load_csv(os.path.join(HIST, "swaps.csv"))

    algs = sorted({c[3:] for c in header if c.startswith("re_")})

    by = defaultdict(list)
    for r in buckets:
        by[r.get("addr") or "unknown"].append(r)
    for rs in by.values():
        rs.sort(key=lambda r: ts_of(r) or 0)

    emit_meta(buckets, algs, now)
    for bid, rs in by.items():
        emit_bucket(bid, rs, algs, now)
    emit_swaps(swaps, now)
    emit_fleet(buckets, now)

    with open(os.path.join(HIST, "index.html"), "w") as f:
        f.write(INDEX_HTML)

    print("renderer: %d buckets, %d algs, %d swaps"
          % (len(by), len(algs), len(swaps)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def main():
    if os.geteuid() != 0:
        die("must run as root (writes /etc/cron.d and /var/lib/bpftune)")

    os.makedirs(HIST, exist_ok=True)

    say("migrating old CSVs")
    migrate()

    say("writing CLI, collector, renderer beside installer")
    write_file(CLI,       CLI_SRC,       0o755)
    write_file(COLLECTOR, COLLECTOR_SRC, 0o755)
    write_file(RENDERER,  RENDERER_SRC,  0o755)

    say("compiling")
    for p in (CLI, COLLECTOR, RENDERER):
        r = subprocess.run([sys.executable, "-m", "py_compile", p],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stderr)
            die("py_compile failed on " + p)

    say("installing cron: " + CRON)
    write_cron()

    say("seeding + verifying")
    run_and_verify()

    print()
    say("done")
    print("    cli:       %s" % CLI)
    print("    collector: %s" % COLLECTOR)
    print("    renderer:  %s" % RENDERER)
    print("    cron:      %s" % CRON)
    print("    site:      %s/index.html" % HIST)
    print()
    print("  open http://<this-host>:8080/ in a browser")
    print("  tail -f /var/log/bpftune-collector.log")
    print()


if __name__ == "__main__":
    main()
