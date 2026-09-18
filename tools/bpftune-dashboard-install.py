#!/usr/bin/env python3
"""
bpftune dashboard installer (schema v2). Idempotent - safe to re-run.

    sudo python3 tools/bpftune-dashboard-install.py

Writes tools/bpftune-collector.py and tools/bpftune-render.py next to
itself, migrates old CSVs to .v1 sidecars, installs
/etc/cron.d/bpftune-history, runs both once, and verifies output.

Buckets are read from the ebpf map `remote_host_map` via bpftool, NOT
from the log. The log supplies swap lines only.

`collected_ts` (wall clock) is the only date-safe column. Swap rows also
carry `boot_ts` (monotonic seconds straight from the log) - never derive
a calendar date from boot_ts.
"""
from __future__ import annotations

import os
import shutil
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

SELF_DIR  = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.join(SELF_DIR, "bpftune-collector.py")
RENDERER  = os.path.join(SELF_DIR, "bpftune-render.py")


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


# ---------------------------------------------------------------- embed

COLLECTOR_SRC = r'''#!/usr/bin/env python3
"""bpftune collector, schema v2. Run once per minute from cron.

Buckets: `bpftool --json map dump name remote_host_map`.
Swaps:   tail /var/log/bpftune-met-*.log, match swap=/met= lines, score
         by comparing nearest met-val before vs first met-val 3-300s
         after the swap.

`collected_ts` is wall clock. `boot_ts` is monotonic (log seconds) -
never derive a date from it.
"""
import csv, json, os, re, subprocess, sys, time
from pathlib import Path

HIST = Path("/var/lib/bpftune/history")
HIST.mkdir(parents=True, exist_ok=True)
BUCKETS_CSV = HIST / "buckets.v2.csv"
SWAPS_CSV   = HIST / "swaps.csv"
SWAPS_POS   = HIST / ".swaps_pos"

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


def main():
    ts_epoch = int(time.time())
    nb = collect_buckets(ts_epoch)
    ns = collect_swaps()
    print("collector: buckets=%d swaps=%d ts=%d" % (nb, ns, ts_epoch))


if __name__ == "__main__":
    main()
'''

RENDERER_SRC = r'''#!/usr/bin/env python3
"""bpftune renderer - static Chart.js dashboard.

Cron: every 5 minutes. Reads buckets.v2.csv + swaps.csv, writes
index.html and data/*.json into /var/lib/bpftune/history.

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
    rows_24h = [r for r in buckets
                if (ts_of(r) or 0) > now - 86400]
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
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
<style>
  :root { color-scheme: light dark; }
  body { font: 13px/1.4 system-ui, sans-serif; margin: 0; padding: 16px; }
  header { display: flex; gap: 12px; align-items: center; flex-wrap: wrap;
           margin-bottom: 16px; }
  h2 { font-size: 14px; margin: 24px 0 6px; }
  select { font: inherit; padding: 2px 6px; }
  .wrap { max-width: 1100px; margin: 0 auto; }
  .muted { color: #888; }
</style>
<div class="wrap">
  <header>
    <h1 style="font-size:16px;margin:0">bpftune</h1>
    <label>bucket <select id="bucket"></select></label>
    <label>range <select id="range"></select></label>
    <span id="gen" class="muted"></span>
  </header>

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
const PALETTE = Array.from({length: 16}, (_, i) =>
  `hsl(${Math.round(i * 360 / 16)},70%,55%)`);

const state  = { meta: null, bucketDoc: null, swaps: null, fleet: null };
const charts = {};

function mk(id, cfg) {
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart(document.getElementById(id), cfg);
}

async function j(url) {
  const r = await fetch(url, {cache: "no-store"});
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

function lineData(cols, series, ts, colors) {
  return cols.map((c, i) => ({
    label: c,
    data: (series[c] || []).map((y, k) => ({x: ts[k] * 1000, y})),
    borderColor: colors[i % colors.length],
    pointRadius: 0,
    borderWidth: 1.4,
    spanGaps: true,
  }));
}

function timeOpts(extra) {
  const base = {
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
  const doc = state.bucketDoc, algs = state.meta.algs;
  const rng = document.getElementById("range").value;
  const s = doc.series[rng], ts = s.ts;

  mk("rate", {
    type: "line",
    data: {datasets: lineData(algs.map(a => `re_${a}`), s, ts, PALETTE)},
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
  const doc = state.swaps, rng = document.getElementById("range").value;
  const d = doc[rng];
  if (!d) return;
  const ts = d.ts;

  const mkLine = (key, color, dash) => ({
    label: key,
    data: d[key].map((y, k) => ({x: ts[k] * 1000, y})),
    borderColor: color,
    borderDash: dash || [],
    pointRadius: 0,
    borderWidth: 1.5,
    spanGaps: true,
  });

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
      labels: ts.map(t => new Date(t * 1000)),
      datasets: [{label: "swaps", data: d.swaps, backgroundColor: "#69c"}],
    },
    options: Object.assign(timeOpts(), {
      scales: {x: {type: "time"}, y: {beginAtZero: true}},
      plugins: {legend: {display: false}},
    }),
  });
}

function renderFleet() {
  const f = state.fleet;
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

async function loadBucket(id) {
  state.bucketDoc = await j(`data/bucket_${id}.json`);
  renderBucket();
}

async function boot() {
  state.meta  = await j("data/meta.json");
  state.swaps = await j("data/swaps.json");
  state.fleet = await j("data/fleet.json");

  const bs = document.getElementById("bucket");
  bs.innerHTML = state.meta.buckets
    .map(b => `<option value="${b.id}">${b.id} (${b.points})</option>`)
    .join("");
  bs.value = state.meta.default_bucket;

  const rs = document.getElementById("range");
  rs.innerHTML = state.meta.ranges
    .map(r => `<option value="${r}">${r}</option>`).join("");
  rs.value = "24h";

  document.getElementById("gen").textContent =
    "generated " + new Date(state.meta.generated_ts * 1000).toISOString();

  bs.onchange = () => loadBucket(bs.value);
  rs.onchange = () => { renderBucket(); renderSwaps(); };

  await loadBucket(state.meta.default_bucket);
  renderSwaps();
  renderFleet();
}

window.addEventListener("error", function (e) {
  var el = document.getElementById("gen");
  if (el) {
    el.textContent = "ERROR: " + e.message + " @" + e.filename + ":" + e.lineno;
    el.style.color = "red";
  }
});
window.addEventListener("unhandledrejection", function (e) {
  var el = document.getElementById("gen");
  if (el) {
    var r = e.reason;
    el.textContent = "REJECT: " + ((r && r.message) || r);
    el.style.color = "red";
  }
});
window.addEventListener("load", function () {
  if (typeof Chart === "undefined") {
    var el = document.getElementById("gen");
    if (el) { el.textContent = "Chart.js failed to load from CDN"; el.style.color = "red"; }
  }
});
boot();
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

    say("writing collector and renderer beside installer")
    write_file(COLLECTOR, COLLECTOR_SRC, 0o755)
    write_file(RENDERER,  RENDERER_SRC,  0o755)

    say("compiling")
    for p in (COLLECTOR, RENDERER):
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
