#!/usr/bin/env python3
"""
bpftune dashboard installer (schema v2). Idempotent - safe to re-run.

    sudo python3 tools/bpftune-dashboard-install.py

Writes three scripts next to itself:
    bpftune-cli.py        text dashboard
    bpftune-collector.py  CSV writer, runs the CLI, snapshots it
    bpftune-render.py     Chart.js static renderer

Then migrates old CSVs, installs /etc/cron.d/bpftune-history, runs both
once, and verifies output.

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
CURRENT_TXT    = os.path.join(HIST, "current.txt")

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

    if not os.path.exists(CURRENT_TXT):
        warn("current.txt not written - CLI snapshot failed")
    else:
        say("  current.txt: %d bytes" % os.path.getsize(CURRENT_TXT))

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
"""bpftune live dashboard. Two columns, rates in Mbps."""
import argparse, json, os, re, subprocess, sys, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

CONGS = ["cubic","bbr","htcp","dctcp","scalable","vegas","veno","westwood",
         "reno","illinois","yeah","lp","bic","highspeed","hybla","nv"]
LOG_TAIL_BYTES = 2_000_000
BPS_TO_MBPS = 1_000_000.0 / 8.0

RULE = "\u2500"      # ─
DRULE = "\u2550"     # ═
ARROW = "\u25b8"     # ▸
VBAR = "\u2502"      # │
MIDDOT = "\u00b7"    # ·


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


def col_build(logpath):
    v = sh("dpkg-query -W -f='${Version}' bpftune").strip() or "?"
    a = sh("systemctl is-active bpftune").strip() or "?"
    ts = sh("systemctl show bpftune -p ActiveEnterTimestamp --value").strip()
    up = "(unknown)"
    hhmm = ""
    m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", ts)
    if m:
        try:
            t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc)
            minutes = int((datetime.now(timezone.utc) - t).total_seconds() // 60)
            h, mm = divmod(minutes, 60)
            up = f"{h}h {mm}m"
            hhmm = m.group(1)[11:19]
        except Exception:
            pass
    return [
        f"version    {v}   service  {a}",
        f"uptime     {up}   (started {hhmm} UTC)" if hhmm else f"uptime     {up}",
        f"log        {str(logpath) if logpath else '(not found)'}",
    ]


def col_system():
    k = sh_noshell(["uname", "-r"]).strip()
    cc = sh_noshell(["sysctl", "-n", "net.ipv4.tcp_congestion_control"]).strip()
    return [
        f"kernel       {k}",
        f"default CC   {cc}",
    ]


def section_tunables(width):
    j = sh_noshell(["journalctl", "-u", "bpftune", "--no-pager", "-q"])
    names = sorted(set(re.findall(r"sysctl '(net\.[A-Za-z0-9_.]+)'", j)))
    out = [f"{ARROW} BPFTUNE-MANAGED TUNABLES", RULE*width]
    if not names:
        out.append("  (none seen in journal for this boot)")
        return out
    ncol = 34
    for n in names:
        v = sh_noshell(["sysctl", "-n", n]).strip()
        if not v:
            continue
        short = n[4:]
        if "allowed_congestion_control" in n:
            v = f"({len(v.split())} algs)"
        if len(v) <= width - ncol - 3:
            out.append(f"  {short:<{ncol}} {v}")
        else:
            out.append(f"  {short:<{ncol}}")
            for i in range(0, len(v), width - ncol - 1):
                out.append(f"  {'':<{ncol}} {v[i:i+width-ncol-1]}")
    return out


def col_buckets(hosts, n=5):
    if not hosts:
        return ["  (map unreadable)"]
    lines = [f"  {'dest':<16}{'inst':>6}{'rtt_us':>9}{'ref_Mbps':>10}{'best_i':>10}{'n_alg':>7}"]
    shown = 0
    for inst, addr, v in hosts:
        if addr in ("0.0.0.1", "?"):
            continue
        if addr.startswith(("127.", "169.254.", "0.")):
            continue
        if inst < 2:
            continue
        shown += 1
        if shown > n:
            break
        mrv = v.get("max_rate_delivered", 0) or 0
        rtt = v.get("min_rtt", 0) or 0
        bi  = v.get("best_i", 0) or 0
        metrics = v.get("metrics") or []
        used = sum(1 for m in metrics if isinstance(m, dict)
                   and int(m.get("metric_count", 0) or 0) > 0)
        try:
            rs = f"{int(mrv)/BPS_TO_MBPS:.1f}"
        except Exception:
            rs = "0.0"
        bin_ = CONGS[int(bi)] if int(bi) < 16 else str(bi)
        lines.append(f"  {addr:<16}{inst:>6}{rtt:>9}{rs:>10}{bin_:>10}{used:>7}")
    if shown == 0:
        lines.append("  (only placeholder buckets)")
    return lines


def col_metric(hosts):
    if not hosts:
        return ["  (map unreadable)"]
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
        name = CONGS[i] if i < 16 else f"alg{i}"
        rows.append((name, val, mc, a))
    if not rows:
        return ["  (no metrics yet)"]
    out = [f"  {'alg':<10}{'metric':>9}{'votes':>7}{'alive':>7}"]
    for name, val, mc, a in sorted(rows, key=lambda r: r[1] if r[1] else 1<<62):
        vm = f"{val/1e6:.1f}" if val else "0"
        out.append(f"  {name:<10}{vm:>9}{mc:>7}{a:>7}")
    return out


def col_proof(text):
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
    if not per_alg:
        return ["  (none in tail)"]
    out = [f"  {'alg':<9}{'good':>5}{'prvd':>5}{'maxM':>8}{'avgM':>8}{'n':>5}"]
    for a in sorted(per_alg.keys(), key=lambda x: -per_alg[x]["max"]):
        d = per_alg[a]
        name = CONGS[a] if a < 16 else f"alg{a}"
        mx = d["max"] / BPS_TO_MBPS
        if a in avg and avg[a][1]:
            av = avg[a][0] / avg[a][1] / BPS_TO_MBPS
            an = avg[a][1]
            out.append(f"  {name:<9}{d['t1']:>5}{d['t2']:>5}{mx:>8.1f}{av:>8.1f}{an:>5}")
        else:
            out.append(f"  {name:<9}{d['t1']:>5}{d['t2']:>5}{mx:>8.1f}{'-':>8}{'-':>5}")
    return out


def col_rate(text):
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
    if not out_map:
        return ["  (no midsamp lines)"]
    out = [f"  {'thr':>7}{'n':>5}{'mean':>9}{'min':>9}{'max':>9}"]
    for thr in sorted(out_map.keys()):
        vs = out_map[thr]
        out.append(f"  {thr:>7}{len(vs):>5}"
                   f"{sum(vs)/len(vs)/BPS_TO_MBPS:>9.1f}"
                   f"{min(vs)/BPS_TO_MBPS:>9.1f}"
                   f"{max(vs)/BPS_TO_MBPS:>9.1f}")
    return out


def _parse_swaps_and_mets(text):
    sw = []
    met = defaultdict(list)
    rx_sw = re.compile(r"(\d+\.\d+): bpf_trace_printk: "
                       r"swap cookie=(\d+) from=(\d+) to=(\d+) "
                       r"bc=(\d+) ac=(\d+) d=(\d+)")
    rx_mt = re.compile(r"(\d+\.\d+): bpf_trace_printk: "
                       r"met cookie=(\d+) rport=(\d+) alg=(\d+) segs=(\d+) val=(\d+)")
    for line in text.splitlines():
        m = rx_sw.search(line)
        if m:
            sw.append((float(m.group(1)), int(m.group(2)),
                       int(m.group(3)), int(m.group(4)),
                       int(m.group(5)), int(m.group(6)), m.group(7)))
            continue
        v = rx_mt.search(line)
        if v:
            met[int(v.group(2))].append((float(v.group(1)), int(v.group(6))))
    return sw, met


def col_swap_outcomes(text):
    sw, met = _parse_swaps_and_mets(text)
    win = null = loss = skip = 0
    reasons = defaultdict(int)
    for (ts, c, fa, ta, bc, ac, d) in sw:
        tl = met.get(c, [])
        pre = post = None
        for (mts, mval) in tl:
            if mts < ts + 0.001:
                pre = mval
            elif ts + 3.0 <= mts <= ts + 300.0:
                post = mval; break
        if not pre or not post:
            skip += 1
            reasons["no pre, no post" if not (pre or post) else ("no pre" if not pre else "no post")] += 1
            continue
        r = post / pre
        if   r <= 0.9: win += 1
        elif r >= 1.1: loss += 1
        else:          null += 1
    total = win + null + loss
    def pct(a):
        return f"{int(100.0*a/total)}%" if total else "n/a"
    out = [
        f"  measurable    {total:>4}  (unmeasurable {skip})",
        f"  win           {win:>4}  {pct(win)}",
        f"  null          {null:>4}  {pct(null)}",
        f"  loss          {loss:>4}  {pct(loss)}",
    ]
    if reasons:
        for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
            out.append(f"  no-post-type   {k:<14} {v}")
    return out


def col_churn(text):
    sw, _ = _parse_swaps_and_mets(text)
    counts = defaultdict(int)
    for s in sw:
        counts[s[1]] += 1
    if not counts:
        return ["  (no swaps in tail)"]
    one  = sum(1 for v in counts.values() if v == 1)
    mid  = sum(1 for v in counts.values() if 2 <= v <= 4)
    many = sum(1 for v in counts.values() if v >= 5)
    mx   = max(counts.values()) if counts else 0
    return [
        f"  cookies swapped     {len(counts)}",
        f"    1x                {one}",
        f"    2-4x              {mid}",
        f"    5x+               {many}",
        f"    max per cookie    {mx}",
    ]


def col_recent_swaps(text, n=6):
    sw, met = _parse_swaps_and_mets(text)
    rx_mt = re.compile(r"mt=(\d+) rb=(\d+)")
    rows = []
    for (ts, c, fa, ta, bc, ac, d) in sw:
        mtn = rbn = "-"
        mt = None
        for line in text.splitlines():
            if f"swap cookie={c} " in line and f" to={ta} " in line and f"from={fa}" in line:
                mt = rx_mt.search(line)
                break
        if mt:
            try:
                mtn = CONGS[int(mt.group(1))] if int(mt.group(1))<16 else str(int(mt.group(1)))
            except Exception:
                pass
            try:
                rbn = CONGS[int(mt.group(2))] if int(mt.group(2))<16 else str(int(mt.group(2)))
            except Exception:
                pass
        tl = met.get(c, [])
        pre = post = None
        for (mts, mval) in tl:
            if mts < ts + 0.001:
                pre = mval
            elif ts + 3.0 <= mts <= ts + 300.0:
                post = mval; break
        if not pre or not post: status = " ... "
        elif post/pre <= 0.9:   status = " WIN "
        elif post/pre >= 1.1:   status = " LOSS"
        else:                   status = " null"
        fn = CONGS[fa] if fa < 16 else str(fa)
        tn = CONGS[ta] if ta < 16 else str(ta)
        rows.append(f"  {fn:>9} -> {tn:<9} d{d} {status} mt={mtn:<8} rb={rbn:<8}")
    return rows[-n:] if rows else ["  (none in tail)"]


def col_recent_proofs(text, n=6):
    lines = [l for l in text.splitlines() if "proof cookie=" in l][-n:]
    if not lines:
        return ["  (none)"]
    out = []
    for l in lines:
        m = re.search(r"proof cookie=(\d+) alg=(\d+) rate=(\d+) tier=(\d+)", l)
        if not m:
            continue
        a = int(m.group(2)); tn = CONGS[a] if a < 16 else f"alg{a}"
        tier = "proved" if m.group(4) == "2" else "good  "
        out.append(f"  {tn:<9} {int(m.group(3))/BPS_TO_MBPS:>7.1f} Mbps   {tier}")
    return out


CW = 58
GAP = "  " + VBAR + "  "
FULL = CW*2 + len(GAP)


def twocol(title_l, lines_l, title_r, lines_r):
    out = [f"{ARROW} {title_l:<{CW-2}}{GAP}{ARROW} {title_r}",
           f"{RULE*CW}{GAP}{RULE*CW}"]
    rows = max(len(lines_l), len(lines_r))
    for i in range(rows):
        l = lines_l[i] if i < len(lines_l) else ""
        r = lines_r[i] if i < len(lines_r) else ""
        out.append(f"{l[:CW]:<{CW}}{GAP}{r[:CW]}")
    return out


def full(title, lines):
    out = [f"{ARROW} {title}", RULE*FULL]
    out += [l[:FULL] for l in lines]
    return out


def col_divergence(text):
    rx_sw = re.compile(r"(\d+\.\d+): bpf_trace_printk: swap cookie=(\d+) "
                       r"from=(\d+) to=(\d+) bc=(\d+) ac=(\d+) d=(\d+)"
                       r"(?: mt=(\d+) rb=(\d+))?")
    rx_mt = re.compile(r"(\d+\.\d+): bpf_trace_printk: met cookie=(\d+) "
                       r"rport=(\d+) alg=(\d+) segs=(\d+) val=(\d+)")
    met = defaultdict(list)
    for line in text.splitlines():
        v = rx_mt.search(line)
        if v:
            met[int(v.group(2))].append((float(v.group(1)), int(v.group(6))))
    groups = {
        "rate==metric":  [0, 0, 0, 0, 0],
        "rate!=metric":  [0, 0, 0, 0, 0],
        "pre-0.4.45":    [0, 0, 0, 0, 0],
    }
    for line in text.splitlines():
        m = rx_sw.search(line)
        if not m:
            continue
        ts, c = float(m.group(1)), int(m.group(2))
        mt_i, rb_i = m.group(8), m.group(9)
        if mt_i is None or rb_i is None: key = "pre-0.4.45"
        elif mt_i == rb_i:               key = "rate==metric"
        else:                            key = "rate!=metric"
        g = groups[key]
        g[0] += 1
        tl = met.get(c, [])
        pre = post = None
        for (mts, mval) in tl:
            if mts < ts + 0.001:
                pre = mval
            elif ts + 3.0 <= mts <= ts + 300.0:
                post = mval; break
        if not pre or not post:
            continue
        r = post / pre
        if   r <= 0.9: g[1] += 1
        elif r >= 1.1: g[4] += 1
        else:          g[2] += 1
    out = [f"  {'category':<20}{'meas':>5}{'win':>7}{'null':>7}{'loss':>7}{'skipped':>9}",
           RULE*62]
    for k in ("rate==metric", "rate!=metric", "pre-0.4.45"):
        n_all, w, nul, _u, l = groups[k]
        meas = w + nul + l
        def pct(x):
            return f"{int(100*x/meas)}%" if meas else "-"
        out.append(f"  {k:<20}{meas:>5}{pct(w):>7}{pct(nul):>7}{pct(l):>7}{n_all-meas:>9}")
    return out


def render():
    logpath = find_log()
    hosts   = read_map()
    text    = tail(logpath) if logpath else ""
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    print(DRULE * (FULL + 2))
    print(f"  bpftune  {MIDDOT}  {os.uname().nodename}  {MIDDOT}  {ts}")
    print(DRULE * (FULL + 2))
    print()
    for row in twocol("BUILD / SERVICE", col_build(logpath),
                      "SYSTEM FACTS",    col_system()):
        print(row)
    print()
    for row in full("BPFTUNE-MANAGED TUNABLES", section_tunables(FULL)[2:]):
        print(row)
    print()
    for row in full("TOP DESTINATION BUCKETS", col_buckets(hosts)):
        print(row)
    print()
    for row in twocol("ALGORITHM LEADERBOARD (metric)", col_metric(hosts),
                      "PROOF LEADERBOARD (speed)",     col_proof(text)):
        print(row)
    print()
    for row in twocol("RATE PROGRESSION (client, Mbps)", col_rate(text),
                      "SWAP OUTCOMES",                    col_swap_outcomes(text)):
        print(row)
    print()
    for row in full("DIVERGENCE (swap target: rate vs metric)", col_divergence(text)):
        print(row)
    print()
    for row in twocol("COOKIE CHURN", col_churn(text),
                      "RECENT PROOF EVENTS", col_recent_proofs(text)):
        print(row)
    print()
    for row in full("RECENT SWAPS", col_recent_swaps(text)):
        print(row)
    print()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("-i", "--interval", type=int, default=10)
    p.add_argument("--once", action="store_true")
    a = p.parse_args()
    try:
        while True:
            if not a.once:
                sys.stdout.write("\x1b[2J\x1b[H")
            render()
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
"""bpftune collector, schema v2. Run once per minute from cron.

Buckets from `bpftool --json map dump name remote_host_map`.
Swaps from tail /var/log/bpftune-met-*.log.
Snapshot: runs tools/bpftune-cli.py --once and stores stdout to
          /var/lib/bpftune/history/current.txt (ANSI stripped)

`collected_ts` is wall clock. `boot_ts` is monotonic (log seconds).
"""
import csv, json, os, re, subprocess, sys, time
from pathlib import Path

HIST = Path("/var/lib/bpftune/history")
HIST.mkdir(parents=True, exist_ok=True)
BUCKETS_CSV = HIST / "buckets.v2.csv"
SWAPS_CSV   = HIST / "swaps.csv"
SWAPS_POS   = HIST / ".swaps_pos"
CURRENT_TXT = HIST / "current.txt"

SELF_DIR = Path(__file__).resolve().parent
CLI      = SELF_DIR / "bpftune-cli.py"

CONGS = ["cubic", "bbr", "htcp", "dctcp", "scalable", "vegas", "veno",
         "westwood", "reno", "illinois", "yeah", "lp", "bic", "highspeed",
         "hybla", "nv"]
MIN_INST = 2

ANSI_RX = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

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
        r = subprocess.run([sys.executable, str(CLI), "--once"],
                           capture_output=True, text=True, timeout=90)
        clean = ANSI_RX.sub("", r.stdout)
        tmp = str(CURRENT_TXT) + ".tmp"
        with open(tmp, "w") as f:
            f.write(clean)
        os.replace(tmp, str(CURRENT_TXT))
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
index.html and data/*.json. The browser also fetches current.txt
(produced by the collector from bpftune-cli.py) every 30s.

All downsampling is server-side - Chart.js never sees raw rows.

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
    --bg: #f7f8fa;
    --fg: #1c1f24;
    --muted: #6b7280;
    --muted-2: #9aa0a6;
    --border: #e5e7eb;
    --border-strong: #d1d5db;
    --card-bg: #ffffff;
    --code-bg: #fbfbfd;
    --shadow: 0 1px 2px rgba(16,24,40,.04);
    --accent: #2f6feb;
    --accent-dim: #2f6feb26;
    --radius: 8px;
    --gap: 16px;
    --mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0b0d11;
      --fg: #e6e8eb;
      --muted: #8b929b;
      --muted-2: #6b7280;
      --border: #1c2028;
      --border-strong: #2a2f39;
      --card-bg: #111419;
      --code-bg: #0a0c10;
      --shadow: 0 1px 2px rgba(0,0,0,.35);
      --accent: #5b9bff;
      --accent-dim: #5b9bff26;
    }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--fg);
    font: 14px/1.5 system-ui, -apple-system, "Segoe UI", Roboto,
          "Helvetica Neue", Arial, sans-serif;
    font-feature-settings: "tnum" 1;
    -webkit-font-smoothing: antialiased;
  }
  .wrap {
    max-width: 1180px; margin: 0 auto;
    padding: 24px 20px 64px;
  }

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
    font-size: 12px; color: var(--muted);
    text-transform: uppercase; letter-spacing: .04em;
  }
  select {
    font: inherit; font-size: 12.5px;
    text-transform: none; letter-spacing: normal;
    color: var(--fg); background: var(--card-bg);
    border: 1px solid var(--border-strong);
    border-radius: 6px;
    padding: 5px 8px;
    min-width: 0;
  }
  select:focus { outline: 2px solid var(--accent-dim); outline-offset: 0; }

  .pill {
    display: inline-flex; align-items: center;
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
    padding: 16px;
    margin-bottom: var(--gap);
  }
  .card > h2 {
    margin: 0 0 14px;
    font-size: 11px; font-weight: 600;
    letter-spacing: .08em; text-transform: uppercase;
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
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 12px 20px;
  }
  .stat { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .stat .k {
    font-size: 10.5px; color: var(--muted);
    text-transform: uppercase; letter-spacing: .06em;
  }
  .stat .v {
    font-size: 15px; font-weight: 500;
    font-variant-numeric: tabular-nums;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .stat .v.mono { font-family: var(--mono); font-size: 13px; }

  .rates {
    margin-top: 14px; padding-top: 14px;
    border-top: 1px dashed var(--border);
    display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
  }
  .rates .k {
    font-size: 10.5px; color: var(--muted);
    text-transform: uppercase; letter-spacing: .06em;
  }
  .rates .v {
    font-family: var(--mono); font-size: 12.5px;
    color: var(--fg);
  }

  pre.live {
    margin: 0;
    padding: 14px 16px;
    border-radius: 6px;
    background: var(--code-bg);
    border: 1px solid var(--border);
    font: 11.5px/1.45 var(--mono);
    color: var(--fg);
    overflow-x: auto;
    white-space: pre;
  }

  .chart-box {
    position: relative;
    width: 100%;
    min-width: 0;
  }
  .chart-box.h-sm { height: 100px; }
  .chart-box.h-md { height: 150px; }
  .chart-box.h-lg { height: 220px; }
  .chart-box.h-xl { height: 280px; }
  .chart-box > canvas { position: absolute; inset: 0; width: 100% !important;
                        height: 100% !important; }

  .footer {
    margin-top: 32px; padding-top: 16px;
    border-top: 1px solid var(--border);
    color: var(--muted-2); font-size: 11px;
    display: flex; gap: 12px; flex-wrap: wrap;
  }
  .footer .sep { color: var(--border-strong); }

  @media (max-width: 640px) {
    .wrap { padding: 16px 12px 48px; }
    .card { padding: 12px; }
    .controls { gap: 10px; }
    .stat .v { font-size: 14px; }
    .chart-box.h-xl { height: 220px; }
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

  <section class="card" id="card-now">
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

  <section class="card">
    <h2>
      <span class="dot"></span>live state
      <span class="sub">fleet &middot; bpftune-cli.py &middot; 30s</span>
    </h2>
    <pre class="live" id="livepre">loading&hellip;</pre>
  </section>

  <section class="card chart-card">
    <h2><span class="dot"></span>rate_ema per algorithm</h2>
    <div class="chart-box h-xl"><canvas id="rate"></canvas></div>
  </section>

  <section class="card chart-card">
    <h2><span class="dot"></span>reference rate</h2>
    <div class="chart-box h-sm"><canvas id="ref"></canvas></div>
  </section>

  <section class="card chart-card">
    <h2><span class="dot"></span>tcp_rmem max (bytes)</h2>
    <div class="chart-box h-sm"><canvas id="rmem"></canvas></div>
  </section>

  <section class="card chart-card">
    <h2><span class="dot"></span>divergence &mdash; win rate with 95% Wilson CI</h2>
    <div class="chart-box h-md"><canvas id="div"></canvas></div>
  </section>

  <section class="card chart-card">
    <h2><span class="dot"></span>swaps per bin</h2>
    <div class="chart-box h-sm"><canvas id="swaps"></canvas></div>
  </section>

  <section class="card chart-card">
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
      gen.style.color = isErr ? "#e15759" : "";
      if (!isErr) {
        gen.classList.add("flash");
        setTimeout(function () { gen.classList.remove("flash"); }, 300);
      }
    }
  }
  function err(msg, e) {
    status(msg, true);
    if (e) console.error(msg, e);
  }

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
    Chart.defaults.plugins.legend.labels.usePointStyle = false;
    Chart.defaults.plugins.tooltip.backgroundColor = dark ? "#1c2028" : "#fff";
    Chart.defaults.plugins.tooltip.borderColor = dark ? "#2a2f39" : "#e5e7eb";
    Chart.defaults.plugins.tooltip.borderWidth = 1;
    Chart.defaults.plugins.tooltip.titleColor = dark ? "#e6e8eb" : "#1c1f24";
    Chart.defaults.plugins.tooltip.bodyColor = dark ? "#e6e8eb" : "#1c1f24";
    Chart.defaults.plugins.tooltip.padding = 8;
    Chart.defaults.plugins.tooltip.cornerRadius = 6;
  }

  function liveRefresh() {
    fetch("current.txt", {cache: "no-store"}).then(function (r) {
      if (!r.ok) throw new Error("current.txt: " + r.status);
      return r.text();
    }).then(function (t) {
      var pre = document.getElementById("livepre");
      if (pre) pre.textContent = t;
    }).catch(function (e) {
      var pre = document.getElementById("livepre");
      if (pre) pre.textContent = "(current.txt unavailable: " + e.message + ")";
    });
  }

  var state  = { meta: null, bucketDoc: null, swaps: null, fleet: null };
  var charts = {};

  function mk(id, cfg) {
    if (charts[id]) { charts[id].destroy(); }
    var cv = document.getElementById(id);
    if (!cv) return;
    charts[id] = new Chart(cv, cfg);
  }

  function j(url) {
    return fetch(url, {cache: "no-store"}).then(function (r) {
      if (!r.ok) { throw new Error(url + ": " + r.status); }
      return r.json();
    });
  }

  function lineData(cols, series, ts, colors) {
    return cols.map(function (c, i) {
      return {
        label: c,
        data: (series[c] || []).map(function (y, k) {
          return {x: ts[k] * 1000, y: y};
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

  function fmtMbps(v) {
    return (v === null || v === undefined) ? "-" : (v / 125000).toFixed(1);
  }
  function fmtN(v) {
    return (v === null || v === undefined)
      ? "-" : Math.round(v).toLocaleString();
  }
  function fmtRTT(v) {
    return (v === null || v === undefined) ? "-" : (v / 1000).toFixed(1) + " ms";
  }
  function fmtBytes(b) {
    if (b === null || b === undefined) return "-";
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
  function setText(id, s) {
    var el = document.getElementById(id);
    if (el) el.textContent = s;
  }

  function renderNow() {
    var doc = state.bucketDoc;
    if (!doc) return;
    var bid = document.getElementById("bucket").value;
    setText("nowbucket", bid);
    var L = doc.last || {};
    setText("n_inst", fmtN(L.instances));
    setText("n_ref",  fmtMbps(L.ref_rate) + " Mb/s");
    setText("n_rtt",  fmtRTT(L.min_rtt));
    setText("n_best", L.best_alg || "-");
    setText("n_rmem", fmtBytes(L.tcp_rmem_max));
    setText("nowupdated", L.collected_ts ? "updated " + relTime(L.collected_ts) : "");

    var algs = state.meta.algs;
    var rates = [];
    for (var i = 0; i < algs.length; i++) {
      var a = algs[i];
      var v = (L.re && L.re[a] != null) ? L.re[a] : null;
      if (v != null && v > 0) rates.push({a: a, v: v});
    }
    rates.sort(function (x, y) { return y.v - x.v; });

    if (rates.length) {
      setText("n_rbest",
              rates[0].a + "  " + fmtMbps(rates[0].v) + " Mb/s");
      var line = rates.slice(0, 8).map(function (x) {
        return x.a + " " + fmtMbps(x.v);
      }).join("  ·  ");
      setText("n_rates", line);
    } else {
      setText("n_rbest", "-");
      setText("n_rates", "(no live rates for this bucket)");
    }
  }

  function renderBucket() {
    var doc = state.bucketDoc, algs = state.meta.algs;
    var rng = document.getElementById("range").value;
    var s = doc.series[rng];
    var ts = s.ts;

    mk("rate", {
      type: "line",
      data: {datasets: lineData(algs.map(function (a) {
        return "re_" + a;
      }), s, ts, PALETTE)},
      options: timeOpts({
        plugins: {
          legend: {
            display: true,
            position: "bottom",
            align: "start",
            labels: {
              boxWidth: 8, boxHeight: 8,
              padding: 8,
              font: {size: 10.5},
            },
          },
        },
      }),
    });

    mk("ref", {
      type: "line",
      data: {datasets: lineData(["ref_rate"], s, ts, ["#e15759"])},
      options: timeOpts({
        plugins: {legend: {display: false}},
      }),
    });

    mk("rmem", {
      type: "line",
      data: {datasets: lineData(["tcp_rmem_max"], s, ts, ["#4e79a7"])},
      options: timeOpts({
        plugins: {legend: {display: false}},
      }),
    });
  }

  function renderSwaps() {
    var doc = state.swaps, rng = document.getElementById("range").value;
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
        mkLine("d1_lo",   "d1 95% lo",  "#59a14f", [4, 3]),
        mkLine("d1_hi",   "d1 95% hi",  "#59a14f", [4, 3]),
        mkLine("d0_rate", "diverges=0", "#e15759"),
        mkLine("d0_lo",   "d0 95% lo",  "#e15759", [4, 3]),
        mkLine("d0_hi",   "d0 95% hi",  "#e15759", [4, 3]),
      ]},
      options: timeOpts({
        scales: {
          x: {type: "time", grid: {display: false},
              ticks: {maxRotation: 0, autoSkipPadding: 24}},
          y: {min: 0, max: 1, grid: {drawTicks: false},
              ticks: {maxTicksLimit: 5, padding: 6,
                      callback: function (v) { return Math.round(v * 100) + "%"; }}},
        },
        plugins: {
          legend: {
            display: true, position: "bottom", align: "start",
            labels: {boxWidth: 8, boxHeight: 8, padding: 8,
                     font: {size: 10.5}, filter: function (item) {
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
        plugins: {legend: {display: false}},
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
        plugins: {legend: {display: false}},
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

        var bs = document.getElementById("bucket");
        var html = "";
        for (var k = 0; k < state.meta.buckets.length; k++) {
          var b = state.meta.buckets[k];
          html += "<option value=\"" + b.id + "\">" + b.id +
                  " (" + b.points + ")</option>";
        }
        bs.innerHTML = html;
        bs.value = state.meta.default_bucket;

        var rs = document.getElementById("range");
        var rhtml = "";
        for (var m = 0; m < state.meta.ranges.length; m++) {
          rhtml += "<option value=\"" + state.meta.ranges[m] + "\">" +
                   state.meta.ranges[m] + "</option>";
        }
        rs.innerHTML = rhtml;
        rs.value = "24h";

        var stamp = new Date(state.meta.generated_ts * 1000).toISOString()
                        .replace("T", " ").slice(0, 19) + "Z";
        status("updated " + relTime(state.meta.generated_ts));
        setText("footgen", "rendered " + stamp);

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
