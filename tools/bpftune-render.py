#!/usr/bin/env python3
"""bpftune renderer — static Chart.js dashboard.

Cron: every 5 minutes. Reads buckets.v2.csv + swaps.csv, writes
index.html and data/*.json into /var/lib/bpftune/history.

All downsampling is server-side — Chart.js never sees raw rows.

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

  <h2>divergence outcomes — win rate with 95% Wilson CI</h2>
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
