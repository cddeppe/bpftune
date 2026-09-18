#!/usr/bin/env python3
"""bpftune data collector.  Run once per minute from cron.

Appends to:
  /var/lib/bpftune/history/buckets.csv   one row per bucket per minute
  /var/lib/bpftune/history/swaps.csv     one row per swap as it happens

Read-only w.r.t. the tuner.  Safe to kill.  Idempotent per-minute: if
run twice in the same minute it appends two rows; harmless."""

import csv, json, os, re, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

HIST = Path("/var/lib/bpftune/history")
HIST.mkdir(parents=True, exist_ok=True)
BUCKETS_CSV = HIST / "buckets.csv"
SWAPS_CSV   = HIST / "swaps.csv"
SWAPS_POS   = HIST / ".swaps_pos"      # byte offset into the log

CONGS = ["cubic","bbr","htcp","dctcp","scalable","vegas","veno","westwood",
         "reno","illinois","yeah","lp","bic","highspeed","hybla","nv"]
MIN_INST = 2

SWAP_RX = re.compile(
    r"(\d+\.\d+): bpf_trace_printk: swap cookie=(\d+) "
    r"from=(\d+) to=(\d+) bc=(\d+) ac=(\d+) d=(\d+)"
    r"(?: mt=(\d+) rb=(\d+))?")
MET_RX = re.compile(
    r"(\d+\.\d+): bpf_trace_printk: met cookie=(\d+) "
    r"rport=(\d+) alg=(\d+) segs=(\d+) val=(\d+)")
PROOF_RX = re.compile(
    r"(\d+\.\d+): bpf_trace_printk: proof cookie=(\d+) "
    r"alg=(\d+) rate=(\d+) tier=(\d+)")


def sh(args, timeout=15):
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:
        return ""


def append_csv(path, row):
    exists = path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not exists:
            w.writeheader()
        w.writerow(row)


def find_log():
    files = list(Path("/var/log").glob("bpftune-met-*.log"))
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def collect_buckets(ts_iso, ts_epoch):
    out = sh(["bpftool","--json","map","dump","name","remote_host_map"])
    try:
        data = json.loads(out)
    except Exception:
        return 0
    if not isinstance(data, list):
        return 0
    n = 0
    for e in data:
        if not isinstance(e, dict): continue
        fmt = e.get("formatted") or {}
        v = fmt.get("value"); k = fmt.get("key") or {}
        if not isinstance(v, dict): continue
        try: inst = int(v.get("instances", 0))
        except Exception: continue
        if inst < MIN_INST: continue
        b = k.get("in6_u",{}).get("u6_addr8")
        if not isinstance(b, list) or len(b) != 16: continue
        addr = ".".join(str(x) for x in b[12:16])
        if addr == "0.0.0.1" or addr.startswith(("127.","169.254.","0.")):
            continue
        row = {
            "ts_iso": ts_iso, "ts_epoch": int(ts_epoch),
            "addr": addr, "instances": inst,
            "min_rtt": int(v.get("min_rtt", 0) or 0),
            "ref_rate": int(v.get("max_rate_delivered", 0) or 0),
            "best_i": int(v.get("best_i", 0) or 0),
            "best_alg": CONGS[int(v.get("best_i", 0) or 0) & 15],
            "rate_best_i": int(v.get("rate_best_i", 0) or 0),
            "rate_best_v": int(v.get("rate_best_v", 0) or 0),
        }
        metrics = v.get("metrics") or []
        for i in range(16):
            m = metrics[i] if i < len(metrics) and isinstance(metrics[i], dict) else {}
            row[f"mv_{CONGS[i]}"] = int(m.get("metric_value", 0) or 0)
            row[f"re_{CONGS[i]}"] = int(m.get("rate_ema", 0) or 0)
        append_csv(BUCKETS_CSV, row)
        n += 1
    return n


def collect_swaps():
    """Tail the log for new swap lines; write with mt/rb and outcome."""
    logpath = find_log()
    if not logpath: return 0
    pos = 0
    if SWAPS_POS.exists():
        try: pos = int(SWAPS_POS.read_text().strip())
        except Exception: pos = 0
    size = logpath.stat().st_size
    if size < pos: pos = 0     # rotated

    # build a small met index for the tail region we're parsing
    with open(logpath, "rb") as f:
        f.seek(pos)
        text = f.read().decode("utf-8", errors="replace")
        new_pos = f.tell()

    met = {}       # cookie -> list of (ts, val)
    for line in text.splitlines():
        m = MET_RX.search(line)
        if m:
            c = int(m.group(2))
            met.setdefault(c, []).append((float(m.group(1)), int(m.group(6))))

    n = 0
    for line in text.splitlines():
        m = SWAP_RX.search(line)
        if not m: continue
        ts = float(m.group(1))
        c  = int(m.group(2))
        fa = int(m.group(3)); ta = int(m.group(4))
        d  = int(m.group(7))
        mt_i = m.group(8); rb_i = m.group(9)
        pre = post = None
        for (mts, mval) in met.get(c, []):
            if mts < ts + 0.001: pre = mval
            elif ts + 3.0 <= mts <= ts + 300.0: post = mval; break
        outcome = ""
        if pre and post:
            r = post / pre
            outcome = "win" if r <= 0.9 else ("loss" if r >= 1.1 else "null")
        mt_alg = CONGS[int(mt_i) & 15] if mt_i and mt_i.isdigit() else ""
        rb_alg = CONGS[int(rb_i) & 15] if rb_i and rb_i.isdigit() else ""
        append_csv(SWAPS_CSV, {
            "ts_epoch": int(ts),
            "cookie": c,
            "from_alg": CONGS[fa] if fa < 16 else str(fa),
            "to_alg":   CONGS[ta] if ta < 16 else str(ta),
            "d": d,
            "mt_alg": mt_alg,
            "rb_alg": rb_alg,
            "diverges": "1" if (mt_alg and rb_alg and mt_alg != rb_alg) else "0",
            "outcome": outcome,
        })
        n += 1

    SWAPS_POS.write_text(str(new_pos))
    return n


def main():
    now = datetime.now(timezone.utc)
    ts_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    nb = collect_buckets(ts_iso, now.timestamp())
    ns = collect_swaps()
    print(f"{ts_iso}  buckets={nb}  swaps={ns}")


if __name__ == "__main__":
    main()
