#!/usr/bin/env python3
"""
bpftune dashboard installer (schema v2). Idempotent - safe to re-run.

    sudo python3 tools/bpftune-dashboard-install.py

Writes three scripts next to itself:
    bpftune-cli.py        text dashboard (from /tmp/bpftune-dashboard.py)
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


# =================== CLI (verbatim text dashboard) ===================

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
    up = ts
    if ts:
        try:
            t = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            up = f"{int((datetime.now(timezone.utc)-t).total_seconds()//60)}m"
        except Exception:
            pass
    return [
        f"version   {v}   service  {a}",
        f"uptime    {up}   ({ts[11:19]} UTC)" if ts else "",
        f"log       {str(logpath) if logpath else '(not found)'}",
    ]


def col_system():
    k = sh_noshell(["uname", "-r"]).strip()
    cc = sh_noshell(["sysctl", "-n", "net.ipv4.tcp_congestion_control"]).strip()
    return [
        f"kernel      {k}",
        f"default CC  {cc}",
    ]


def section_tunables(width):
    j = sh_noshell(["journalctl", "-u", "bpftune", "--no-pager", "-q"])
    names = sorted(set(re.findall(r"sysctl '(net\.[A-Za-z0-9_.]+)'", j)))
    out = [" BPFTUNE-MANAGED TUNABLES", " " + "-"*width]
    if not names:
        out.append(" (none seen in journal for this boot)")
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
            out.append(f" {short:<{ncol}} {v}")
        else:
            out.append(f" {short:<{ncol}}")
            for i in range(0, len(v), width - ncol - 1):
                out.append(f" {'':<{ncol}} {v[i:i+width-ncol-1]}")
    return out


def col_buckets(hosts, n=5):
    if not hosts:
        return ["(map unreadable)"]
    lines = [f" {'dest':<16}{'inst':>6}{'rtt_us':>9}{'ref_Mbps':>10}{'best_i':>10}{'n_alg':>7}"]
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
        lines.append(f" {addr:<16}{inst:>6}{rtt:>9}{rs:>10}{bin_:>10}{used:>7}")
    if shown == 0:
        lines.append(" (only placeholder buckets)")
    return lines


def col_metric(hosts):
    if not hosts:
        return ["(map unreadable)"]
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
        return ["(no metrics yet)"]
    out = [f" {'alg':<10}{'metric':>9}{'votes':>7}{'alive':>7}"]
    for name, val, mc, a in sorted(rows, key=lambda r: r[1] if r[1] else 1<<62):
        vm = f"{val/1e6:.1f}" if val else "0"
        out.append(f" {name:<10}{vm:>9}{mc:>7}{a:>7}")
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
        return ["(none in tail)"]
    out = [f" {'alg':<9}{'good':>5}{'prvd':>5}{'maxM':>8}{'avgM':>8}{'n':>5}"]
    for a in sorted(per_alg.keys(), key=lambda x: -per_alg[x]["max"]):
        d = per_alg[a]
        name = CONGS[a] if a < 16 else f"alg{a}"
        mx = d["max"] / BPS_TO_MBPS
        if a in avg and avg[a][1]:
            av = avg[a][0] / avg[a][1] / BPS_TO_MBPS
            an = avg[a][1]
            out.append(f" {name:<9}{d['t1']:>5}{d['t2']:>5}{mx:>8.1f}{av:>8.1f}{an:>5}")
        else:
            out.append(f" {name:<9}{d['t1']:>5}{d['t2']:>5}{mx:>8.1f}{'-':>8}{'-':>5}")
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
        return ["(no midsamp lines)"]
    out = [f" {'thr':>7}{'n':>5}{'mean':>9}{'min':>9}{'max':>9}"]
    for thr in sorted(out_map.keys()):
        vs = out_map[thr]
        out.append(f" {thr:>7}{len(vs):>5}"
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
        f" measurable   {total:>4}  (unmeasurable {skip})",
        f"   win        {win:>4}  {pct(win)}",
        f"   null       {null:>4}  {pct(null)}",
        f"   loss       {loss:>4}  {pct(loss)}",
    ]
    if reasons:
        for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
            out.append(f"   no-post-type  {k:<14} {v}")
    return out


def col_churn(text):
    sw, _ = _parse_swaps_and_mets(text)
    counts = defaultdict(int)
    for s in sw:
        counts[s[1]] += 1
    if not counts:
        return ["(no swaps in tail)"]
    one  = sum(1 for v in counts.values() if v == 1)
    mid  = sum(1 for v in counts.values() if 2 <= v <= 4)
    many = sum(1 for v in counts.values() if v >= 5)
    mx   = max(counts.values()) if counts else 0
    return [
        f" cookies swapped:    {len(counts)}",
        f"   1x               {one}",
        f"   2-4x             {mid}",
        f"   5x+              {many}",
        f"   max per cookie   {mx}",
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
        rows.append(f" {fn:>9}->{tn:<9} d{d} {status} mt={mtn:<8} rb={rbn:<8}")
    return rows[-n:] if rows else ["(none in tail)"]


def col_recent_proofs(text, n=6):
    lines = [l for l in text.splitlines() if "proof cookie=" in l][-n:]
    if not lines:
        return ["(none)"]
    out = []
    for l in lines:
        m = re.search(r"proof cookie=(\d+) alg=(\d+) rate=(\d+) tier=(\d+)", l)
        if not m:
            continue
        a = int(m.group(2)); tn = CONGS[a] if a < 16 else f"alg{a}"
        tier = "proved" if m.group(4) == "2" else "good  "
        out.append(f" {tn:<9} {int(m.group(3))/BPS_TO_MBPS:>7.1f} Mbps   {tier}")
    return out


CW = 58
GAP = "  |  "
FULL = CW*2 + len(GAP)


def twocol(title_l, lines_l, title_r, lines_r):
    out = [f" {title_l:<{CW}}{GAP}{title_r}",
           f" {'-'*CW}{GAP}{'-'*CW}"]
    rows = max(len(lines_l), len(lines_r))
    for i in range(rows):
        l = lines_l[i] if i < len(lines_l) else ""
        r = lines_r[i] if i < len(lines_r) else ""
        out.append(f" {l[:CW]:<{CW}}{GAP}{r[:CW]}")
    return out


def full(title, lines):
    out = [f" {title}", f" {'-'*FULL}"]
    out += [f" {l[:FULL]}" for l in lines]
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
    out = [" category             meas    win   null   loss  skipped",
           " " + "-"*62]
    for k in ("rate==metric", "rate!=metric", "pre-0.4.45"):
        n_all, w, nul, _u, l = groups[k]
        meas = w + nul + l
        def pct(x):
            return f"{int(100*x/meas)}%" if meas else "-"
        out.append(f" {k:<20} {meas:>5} {pct(w):>6} {pct(nul):>6} "
                   f"{pct(l):>6} {n_all-meas:>8}")
    return out


def render():
    logpath = find_log()
    hosts   = read_map()
    text    = tail(logpath) if logpath else ""
    print("=" * (FULL + 2))
    print(f" bpftune dashboard   {os.uname().nodename}   "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print("=" * (FULL + 2))
    for row in twocol("BUILD / SERVICE", col_build(logpath),
                      "SYSTEM FACTS",    col_system()):
        print(row)
    print()
    for row in full("BPFTUNE-MANAGED TUNABLES", section_tunables(FULL - 1)[2:]):
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
          /var/lib/bpftune/history/current.txt

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
        tmp = str(CURRENT_TXT) + ".tmp"
        with open(tmp, "w") as f:
            f.write(r.stdout)
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
<meta charset="utf-8">
<title>bpftune</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 13px/1.4 system-ui, sans-serif; margin: 0; padding: 16px;
         background: #fafafa; color: #111; }
  @media (prefers-color-scheme: dark) {
    body { background: #111; color: #ddd; }
    .live pre { background: #1a1a1a; color: #ddd; border-color: #333; }
  }
  header { display: flex; gap: 12px; align-items: center; flex-wrap: wrap;
           margin-bottom: 12px; }
  h1 { font-size: 16px; margin: 0; }
  h2 { font-size: 14px; margin: 24px 0 6px; }
  select { font: inherit; padding: 2px 6px; }
  .wrap { max-width: 1240px; margin: 0 auto; }
  .muted { color: #888; }
  .live { margin-bottom: 16px; }
  .live pre {
    font: 12px/1.25 ui-monospace, SFMono-Regular, Menlo, monospace;
    background: #fff; color: #111;
    border: 1px solid #ddd; border-radius: 4px;
    padding: 10px 12px;
    overflow-x: auto;
    white-space: pre;
    margin: 0;
    max-height: 80vh;
  }
  .live h2 { margin-top: 0; }
</style>
<div class="wrap">
  <header>
    <h1>bpftune</h1>
    <label>bucket <select id="bucket"></select></label>
    <label>range <select id="range"></select></label>
    <span id="gen" class="muted">initial</span>
  </header>

  <div class="live">
    <h2>live state (from bpftune-cli.py, 30s refresh)</h2>
    <pre id="livepre">loading...</pre>
  </div>

  <h2>rate_ema per algorithm</h2>
  <canvas id="rate" height="90"></canvas>

  <h2>reference rate</h2>
  <canvas id="ref" height="50"></canvas>

  <h2>tcp_rmem max (bytes)</h2>
  <canvas id="rmem" height="50"></canvas>

  <h2>divergence outcomes - win rate with 95% Wilson CI</h2>
  <canvas id="div" height="70"></canvas>

  <h2>swaps per bin</h2>
  <canvas id="swaps" height="50"></canvas>

  <h2>rate-board coverage, last 24h (% of bins with a leader)</h2>
  <canvas id="fleet" height="70"></canvas>
</div>

<script>
(function () {
  var el = document.getElementById("gen");
  if (el) el.textContent = "html+js ok @ " + new Date().toISOString().slice(11, 19);
})();
</script>

<script>
(function () {
  var gen = document.getElementById("gen");
  function status(msg, isErr) {
    if (gen) {
      gen.textContent = msg;
      gen.style.color = isErr ? "red" : "";
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
      s.onload = function () { resolve(); };
      s.onerror = function () { reject(new Error("failed to load " + url)); };
      document.head.appendChild(s);
    });
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

  var PALETTE = [];
  for (var i = 0; i < 16; i++) {
    PALETTE.push("hsl(" + Math.round(i * 360 / 16) + ",70%,55%)");
  }

  var state  = { meta: null, bucketDoc: null, swaps: null, fleet: null };
  var charts = {};

  function mk(id, cfg) {
    if (charts[id]) { charts[id].destroy(); }
    charts[id] = new Chart(document.getElementById(id), cfg);
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
        pointRadius: 0,
        borderWidth: 1.4,
        spanGaps: true,
      };
    });
  }

  function timeOpts(extra) {
    var base = {
      responsive: true,
      animation: false,
      interaction: {mode: "nearest", intersect: false},
      scales: {
        x: {type: "time", time: {tooltipFormat: "MM-dd HH:mm"}},
        y: {beginAtZero: false},
      },
      plugins: {legend: {labels: {boxWidth: 8, font: {size: 10}}}},
    };
    return Object.assign(base, extra || {});
  }

  function renderBucket() {
    var doc = state.bucketDoc, algs = state.meta.algs;
    var rng = document.getElementById("range").value;
    var s = doc.series[rng], ts = s.ts;

    mk("rate", {
      type: "line",
      data: {datasets: lineData(algs.map(function (a) {
        return "re_" + a;
      }), s, ts, PALETTE)},
      options: timeOpts({
        plugins: {legend: {position: "right",
                           labels: {boxWidth: 8, font: {size: 10}}}},
      }),
    });

    mk("ref", {
      type: "line",
      data: {datasets: lineData(["ref_rate"], s, ts, ["#c33"])},
      options: timeOpts(),
    });

    mk("rmem", {
      type: "line",
      data: {datasets: lineData(["tcp_rmem_max"], s, ts, ["#37a"])},
      options: timeOpts({plugins: {legend: {display: false}}}),
    });
  }

  function renderSwaps() {
    var doc = state.swaps, rng = document.getElementById("range").value;
    var d = doc[rng];
    if (!d) return;
    var ts = d.ts;

    function mkLine(key, color, dash) {
      return {
        label: key,
        data: d[key].map(function (y, k) {
          return {x: ts[k] * 1000, y: y};
        }),
        borderColor: color,
        borderDash: dash || [],
        pointRadius: 0,
        borderWidth: 1.5,
        spanGaps: true,
      };
    }

    mk("div", {
      type: "line",
      data: {datasets: [
        mkLine("d1_rate", "#2a7"),
        mkLine("d1_lo",   "#2a7", [4, 3]),
        mkLine("d1_hi",   "#2a7", [4, 3]),
        mkLine("d0_rate", "#a72"),
        mkLine("d0_lo",   "#a72", [4, 3]),
        mkLine("d0_hi",   "#a72", [4, 3]),
      ]},
      options: timeOpts({
        scales: {x: {type: "time"}, y: {min: 0, max: 1}},
      }),
    });

    mk("swaps", {
      type: "bar",
      data: {
        labels: ts.map(function (t) { return new Date(t * 1000); }),
        datasets: [{label: "swaps", data: d.swaps, backgroundColor: "#69c"}],
      },
      options: Object.assign(timeOpts(), {
        scales: {x: {type: "time"}, y: {beginAtZero: true}},
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
        datasets: [{label: "% bins with leader",
                    data: f.coverage_24h, backgroundColor: "#4a8"}],
      },
      options: {
        responsive: true, animation: false, indexAxis: "y",
        scales: {x: {min: 0, max: 100}},
        plugins: {legend: {display: false}},
      },
    });
  }

  function loadBucket(id) {
    return j("data/bucket_" + id + ".json").then(function (doc) {
      state.bucketDoc = doc;
      renderBucket();
    });
  }

  function boot() {
    liveRefresh();
    setInterval(liveRefresh, 30000);

    status("loading Chart.js...");
    loadScript("https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js")
      .then(function () {
        status("loading date adapter...");
        return loadScript("https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js");
      })
      .then(function () {
        status("loading data...");
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

        status("generated " + new Date(state.meta.generated_ts * 1000).toISOString());

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
