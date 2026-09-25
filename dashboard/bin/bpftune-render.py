#!/usr/bin/env python3
"""bpftune renderer - static Chart.js dashboard + live CLI panel.

Cron: every 5 minutes. Reads buckets.v2.csv + swaps.csv + srate.csv,
writes index.html and data/*.json. The browser also fetches
current.json (every 30s).

load_csv is bounded-memory: keeps the last N rows only.
"""
import bisect, csv, io, json, math, os, time
from pathlib import Path
from collections import defaultdict, deque

HIST = "/var/lib/bpftune/history"
DATA = os.path.join(HIST, "data")

RANGES = {
    "1h":  (3600,       60),
    "24h": (86400,      300),
    "7d":  (7 * 86400,  3600),
    "all": (None,       21600),
}

MAX_ROWS_BUCKETS = 150000
MIN_BUCKET_ROWS  = 20
MAX_BUCKETS      = 60
MAX_ROWS_PER_BUCKET = 500
MIN_BUCKET_ROWS  = 5        # skip buckets too small to plot
MAX_ROWS_PER_BUCKET = 4000   # cap work per bucket
MAX_ROWS_SWAPS   =  50000
MAX_ROWS_SRATE   = 150000

EXTRA_COLS = ["ref_rate", "rate_best_i", "rate_best_v", "instances",
              "tcp_rmem_min", "tcp_rmem_def", "tcp_rmem_max"]

SUSTAINED_LO_S = 60.0
SUSTAINED_HI_S = 300.0


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "y")


def median(xs):
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    if n % 2:
        return float(s[n // 2])
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


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


def load_csv(path, max_rows=None):
    if not path or not os.path.exists(path):
        return [], []
    size = os.path.getsize(path)
    if size == 0:
        return [], []
    if max_rows is None:
        with open(path, newline="", encoding="utf-8") as f:
            rd = csv.DictReader(f)
            return list(rd.fieldnames or []), list(rd)
    want = max_rows + 1
    block = 262144
    chunks = []
    nl_count = 0
    with open(path, "rb") as f:
        pos = size
        while pos > 0 and nl_count < want:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step)
            nl_count += buf.count(b"\n")
            chunks.append(buf)
    data = b"".join(reversed(chunks))
    walked = (pos == 0)
    lines = data.split(b"\n")
    if walked:
        header_bytes = lines[0]
        body = lines[1:]
    else:
        with open(path, "r", newline="", encoding="utf-8") as f:
            header_bytes = f.readline().rstrip("\n").encode("utf-8")
        body = lines[1:]
    rows_bytes = b"\n".join(body[-max_rows:])
    if not rows_bytes.strip():
        return header_bytes.decode("utf-8").split(","), []
    header = next(csv.reader(io.StringIO(header_bytes.decode("utf-8"))))
    reader = csv.DictReader(
        io.StringIO(rows_bytes.decode("utf-8", errors="replace")),
        fieldnames=header)
    return header, list(reader)


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


def bin_series_parsed(parsed, lo, width, cols):
    """Bin rows whose cells are already floats. parsed = list of
    {"_t": float, "<col>": float|None}."""
    acc = defaultdict(list)
    for r in parsed:
        t = r["_t"]
        if lo is not None and t < lo:
            continue
        acc[int((t - (lo or 0)) // width)].append(r)
    base = lo if lo is not None else 0
    ts = []
    out = {c: [] for c in cols}
    for b in sorted(acc):
        ts.append(int(base + b * width + width / 2))
        grp = acc[b]
        for c in cols:
            total = 0.0
            count = 0
            for r in grp:
                v = r.get(c)
                if v is not None:
                    total += v
                    count += 1
            out[c].append(total / count if count else None)
    return ts, out


def write_json(name, obj):
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, name)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)



def aggregate_all(bfile, algs, now):
    """Single streaming pass over buckets.v2.csv.

    Pass 1 counts rows per addr (retains nothing). Pass 2 aggregates
    values into per-range bins as it reads. No row retention at all,
    so peak memory is a few MB regardless of file size, and every row
    in the file contributes to the 7d/all ranges.

    Returns (header, cols, docs, meta_stats).
    """
    MAX_BUCKETS = 60
    MIN_BUCKET_ROWS = 20

    with open(bfile, "r", newline="") as f:
        rd = csv.reader(f)
        try:
            header = next(rd)
        except StopIteration:
            return [], {}, [], {}
        cols = {c: i for i, c in enumerate(header)}
        ai = cols.get("addr")
        ti = cols.get("collected_ts")
        if ai is None or ti is None:
            return header, cols, [], {}
        counts = {}
        for row in rd:
            a = row[ai] if ai < len(row) else ""
            a = a or "unknown"
            if a.count(".") == 3 and not a.endswith(".0.0"):
                continue
            counts[a] = counts.get(a, 0) + 1

    ranked = sorted(counts, key=lambda a: -counts[a])
    top = [a for a in ranked if counts[a] >= MIN_BUCKET_ROWS][:MAX_BUCKETS]
    topset = set(top)

    wanted = []
    for c in EXTRA_COLS:
        if c in cols:
            wanted.append((c, cols[c]))
    for a in algs:
        for pre in ("re_", "ss_", "bs_", "ns_", "mv_"):
            c = pre + a
            if c in cols:
                wanted.append((c, cols[c]))
    wn = len(wanted)

    rkeys = list(RANGES.keys())
    rspecs = [RANGES[k] for k in rkeys]

    acc  = {}
    last_row = {}
    meta_stats = {}
    inst_i = cols.get("instances")

    with open(bfile, "r", newline="") as f:
        rd = csv.reader(f)
        next(rd)
        hlen = len(header)
        for row in rd:
            if len(row) < hlen:
                continue
            a = row[ai]
            if a not in topset:
                continue
            t_raw = row[ti]
            if not t_raw:
                continue
            try:
                t = float(t_raw)
            except ValueError:
                continue
            last_row[a] = row

            ms = meta_stats.get(a)
            if ms is None:
                ms = meta_stats[a] = {"last_ts": 0.0, "inst_sum": 0.0,
                                      "inst_n": 0, "pts24": 0}
            if t > ms["last_ts"]:
                ms["last_ts"] = t
            if t > now - 86400:
                ms["pts24"] += 1
                if inst_i is not None and inst_i < len(row):
                    iv = row[inst_i]
                    if iv:
                        try:
                            ms["inst_sum"] += float(iv)
                            ms["inst_n"] += 1
                        except ValueError:
                            pass

            buck = acc.get(a)
            if buck is None:
                buck = acc[a] = {}
            for ri, (span, width) in enumerate(rspecs):
                lo = None if span is None else now - span
                if lo is not None and t < lo:
                    continue
                b = int((t - (lo or 0)) // width)
                key = (ri, b)
                cell = buck.get(key)
                if cell is None:
                    cell = buck[key] = [[0.0, 0] for _ in range(wn)]
                for ci, (_, cidx) in enumerate(wanted):
                    raw = row[cidx] if cidx < len(row) else ""
                    if not raw:
                        continue
                    try:
                        v = float(raw)
                    except ValueError:
                        continue
                    slot = cell[ci]
                    slot[0] += v
                    slot[1] += 1

    docs = []
    for bid in top:
        buck = acc.get(bid, {})
        doc = {"id": bid, "series": {}}
        for ri, rkey in enumerate(rkeys):
            bins = sorted({k[1] for k in buck if k[0] == ri})
            span, width = rspecs[ri]
            lo = None if span is None else now - span
            base = lo if lo is not None else 0
            ts_out = [int(base + b * width + width / 2) for b in bins]
            series = {"ts": ts_out}
            is24 = (rkey == "24h")
            for ci, (cname, _) in enumerate(wanted):
                if cname.startswith("mv_") and not is24:
                    continue
                vals = []
                for b in bins:
                    cell = buck.get((ri, b))
                    if cell is None:
                        vals.append(None)
                        continue
                    sm, ct = cell[ci]
                    vals.append(sm / ct if ct else None)
                series[cname] = vals
            doc["series"][rkey] = series

        last = last_row.get(bid)
        if last is not None:
            def _g(name, dflt=""):
                i = cols.get(name)
                if i is None or i >= len(last):
                    return dflt
                v = last[i]
                return v if v != "" else dflt
            def _gf(name):
                v = _g(name)
                if v == "":
                    return None
                try:
                    return float(v)
                except ValueError:
                    return None
            doc["last"] = {
                "collected_ts": int(_gf("collected_ts") or 0),
                "best_alg":     _g("best_alg"),
                "best_i":       _gf("best_i"),
                "instances":    _gf("instances"),
                "ref_rate":     _gf("ref_rate"),
                "min_rtt":      _gf("min_rtt"),
                "rate_best_i":  _gf("rate_best_i"),
                "rate_best_v":  _gf("rate_best_v"),
                "tcp_rmem_max": _gf("tcp_rmem_max"),
                "re":           {a: _gf("re_" + a) for a in algs},
            }
        docs.append(doc)

    return header, cols, docs, meta_stats


def emit_meta(buckets, algs, now, primary=None):
    rows_24h = [r for r in buckets if (ts_of(r) or 0) > now - 86400]
    if not rows_24h:
        rows_24h = list(buckets)
    by = defaultdict(list)
    for r in rows_24h:
        by[r.get("addr") or "unknown"].append(r)
    entries = []
    for bid, rs in by.items():
        inst = [to_float(r.get("instances")) for r in rs]
        inst = [v for v in inst if v is not None]
        last = max((ts_of(r) or 0) for r in rs)
        entries.append({
            "id": bid,
            "points": len(rs),
            "instances_mean": round(sum(inst) / len(inst), 2) if inst else 0,
            "last_ts": int(last),
        })
    entries.sort(key=lambda e: (e["instances_mean"], e["last_ts"]),
                 reverse=True)
    write_json("meta.json", {
        "generated_ts":  now,
        "ranges":        list(RANGES),
        "algs":          algs,
        "buckets":       entries,
        "default_bucket": (
            primary if (primary and any(e["id"] == primary for e in entries))
            else (entries[0]["id"] if entries else "all")
        ),
        "has_tcp_rmem":  "tcp_rmem_max" in (
            load_csv(buckets_source(), max_rows=1)[0] or []),
    })


def emit_bucket(bid, rows, algs, now):
    """Transpose to column-major, then bin every column into every range
    in a single pass. Avoids the old 4x re-parse + per-cell dict.get
    hotspot (~11M dict lookups/run on a modest history)."""
    wanted = list(EXTRA_COLS)
    for a in algs:
        wanted.append("re_" + a)
        wanted.append("ss_" + a)
        wanted.append("bs_" + a)
        wanted.append("ns_" + a)
        wanted.append("mv_" + a)
    ncols = len(wanted)

    ts_list = []
    col_data = [[] for _ in range(ncols)]
    for r in rows:
        t = ts_of(r)
        if t is None:
            continue
        ts_list.append(t)
        for i, c in enumerate(wanted):
            v = r.get(c)
            col_data[i].append(to_float(v) if v not in (None, "") else None)
    n = len(ts_list)

    range_meta = []
    for rng, (span, width) in RANGES.items():
        lo = None if span is None else now - span
        if lo is None:
            raw_bins = [int(t // width) for t in ts_list]
            base = 0
        else:
            raw_bins = [(int((t - lo) // width) if t >= lo else -1)
                        for t in ts_list]
            base = lo
        uniq = sorted(set(b for b in raw_bins if b >= 0))
        idx = {b: i for i, b in enumerate(uniq)}
        row_bin = [idx.get(b, -1) if b >= 0 else -1 for b in raw_bins]
        ts_out = [int(base + b * width + width / 2) for b in uniq]
        range_meta.append({"rng": rng, "row_bin": row_bin,
                           "ts": ts_out, "nb": len(uniq)})

    nranges = len(range_meta)
    keep_mv = [m["rng"] == "24h" for m in range_meta]
    sums = [[[0.0] * m["nb"] for m in range_meta] for _ in range(ncols)]
    cnts = [[[0]   * m["nb"] for m in range_meta] for _ in range(ncols)]

    for i in range(n):
        row_bins = [m["row_bin"][i] for m in range_meta]
        for ci in range(ncols):
            v = col_data[ci][i]
            if v is None:
                continue
            for ri in range(nranges):
                j = row_bins[ri]
                if j < 0:
                    continue
                sums[ci][ri][j] += v
                cnts[ci][ri][j] += 1

    doc = {"id": bid, "series": {}}
    for ri, m in enumerate(range_meta):
        nb = m["nb"]
        series = {"ts": m["ts"]}
        mv_ok = keep_mv[ri]
        for ci, c in enumerate(wanted):
            if c.startswith("mv_") and not mv_ok:
                continue
            s_ci = sums[ci][ri]
            k_ci = cnts[ci][ri]
            series[c] = [(s_ci[j] / k_ci[j]) if k_ci[j] else None
                         for j in range(nb)]
        doc["series"][m["rng"]] = series

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

def _attach_sustained_outcomes(swaps, srate_rows):
    by_cookie = defaultdict(list)
    for r in srate_rows:
        c = to_float(r.get("cookie"))
        t = to_float(r.get("boot_ts"))
        s = to_float(r.get("srate"))
        if c is None or t is None or s is None:
            continue
        by_cookie[int(c)].append((t, s))
    for lst in by_cookie.values():
        lst.sort()

    for r in swaps:
        r["_outcome_sustained"] = None
        c = to_float(r.get("cookie"))
        t = to_float(r.get("boot_ts"))
        sb = to_float(r.get("srate_before"))
        if c is None or t is None or not sb or sb <= 0:
            continue
        lst = by_cookie.get(int(c))
        if not lst:
            continue
        ts_list = [x[0] for x in lst]
        pre = sb
        lo = bisect.bisect_left(ts_list, t + SUSTAINED_LO_S)
        hi = bisect.bisect_right(ts_list, t + SUSTAINED_HI_S)
        if lo >= hi:
            continue
        samples = [lst[i][1] for i in range(lo, hi)]
        pm = median(samples)
        if pm is None or pre <= 0:
            continue
        ratio = pm / pre
        r["_outcome_sustained"] = (
            "win"  if ratio >= 1.1 else
            "loss" if ratio <= 0.9 else
            "null")


def emit_swaps(rows, srate_rows, now):
    _attach_sustained_outcomes(rows, srate_rows)
    doc = {}
    for rng, (span, width) in RANGES.items():
        lo = None if span is None else now - span
        acc = defaultdict(lambda: {
            0: {"cw": 0, "cl": 0, "uu": 0, "ul": 0},
            1: {"cw": 0, "cl": 0, "uu": 0, "ul": 0},
        })
        for r in rows:
            t = ts_of(r)
            if t is None or (lo is not None and t < lo):
                continue
            b = int((t - (lo or 0)) // width)
            d = 1 if truthy(r.get("diverges")) else 0
            o = str(r.get("outcome", "")).strip().lower()
            if o == "win":
                acc[b][d]["cw"] += 1
            elif o == "loss":
                acc[b][d]["cl"] += 1
            o2 = r["_outcome_sustained"]
            if o2 == "win":
                acc[b][d]["uu"] += 1
            elif o2 == "loss":
                acc[b][d]["ul"] += 1

        base = lo if lo is not None else 0
        node = {"ts": [], "swaps": []}
        for d in (0, 1):
            node["d%d_rate"           % d] = []
            node["d%d_lo"             % d] = []
            node["d%d_hi"             % d] = []
            node["d%d_n"              % d] = []
            node["d%d_rate_sustained" % d] = []
            node["d%d_lo_sustained"   % d] = []
            node["d%d_hi_sustained"   % d] = []
            node["d%d_n_sustained"    % d] = []

        for b in sorted(acc):
            node["ts"].append(int(base + b * width + width / 2))
            total = 0
            for d in (0, 1):
                bucket = acc[b][d]
                cw, cl = bucket["cw"], bucket["cl"]
                cn = cw + cl
                total += cn
                lo_, hi_ = wilson(cw, cn)
                node["d%d_rate" % d].append(cw / cn if cn else None)
                node["d%d_lo"   % d].append(lo_)
                node["d%d_hi"   % d].append(hi_)
                node["d%d_n"    % d].append(cn)
                uw, ul = bucket["uu"], bucket["ul"]
                un = uw + ul
                lo2, hi2 = wilson(uw, un)
                node["d%d_rate_sustained" % d].append(uw / un if un else None)
                node["d%d_lo_sustained"   % d].append(lo2)
                node["d%d_hi_sustained"   % d].append(hi2)
                node["d%d_n_sustained"    % d].append(un)
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

    pairs = []
    for bid, rs in by.items():
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
        pairs.append((bid, round(100 * have / len(bins), 1)))

    pairs.sort(key=lambda p: -p[1])
    pairs = pairs[:25]
    labels = [p[0] for p in pairs]
    cov    = [p[1] for p in pairs]
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
    flex-wrap: wrap;
  }
  .lv-grid h3 .cnt {
    margin-left: auto; color: var(--muted-2);
    font-family: var(--mono); font-size: 10.5px;
    font-weight: 500; letter-spacing: 0; text-transform: none;
  }
  .lv-grid .note {
    margin-top: 8px; padding-top: 8px;
    border-top: 1px dashed var(--border);
    font-size: 11px; color: var(--muted-2);
    line-height: 1.4;
  }
  .lv-grid .note b { color: var(--muted); }
  .lv-grid .note code {
    font-family: var(--mono); font-size: 10.5px;
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
    white-space: nowrap;
  }
  table.tbl tbody tr:last-child td { border-bottom: none; }
  table.tbl td.mono { font-family: var(--mono); font-size: 12px; }
  table.tbl td.name { color: var(--fg); font-weight: 500; }
  table.tbl td.dim { color: var(--muted); }
  table.tbl tr.pick td { background: var(--accent-dim); }
  table.tbl tr.pick td.name::after {
    content: " \25b8 pick";
    color: var(--accent); font-size: 9.5px;
    text-transform: uppercase; letter-spacing: .05em;
    margin-left: 6px; font-weight: 600;
  }
  table.tbl tr.inactive td { opacity: .45; }

  .proof-tbl td { padding-top: 4px; padding-bottom: 4px; vertical-align: middle; }
  .proof-tbl td:nth-child(4),
  .proof-tbl td:nth-child(5),
  .proof-tbl td:nth-child(6) { width: 30%; min-width: 90px; }
  .bar-cell {
    position: relative;
    display: block;
    height: 20px;
    background: var(--subtle);
    border-radius: 3px;
    overflow: hidden;
  }
  .bar-fill {
    position: absolute; inset: 0 auto 0 0;
    border-radius: 3px;
  }
  .bar-cell.v-proven  .bar-fill { background: #59a14f; }
  .bar-cell.v-avg     .bar-fill { background: #4e79a7; }
  .bar-cell.v-sampled .bar-fill { background: #e15759; }
  .bar-num {
    position: absolute; inset: 0;
    display: flex; align-items: center; justify-content: flex-end;
    padding: 0 6px;
    font-family: var(--mono); font-size: 11px;
    font-variant-numeric: tabular-nums;
    color: var(--fg);
    text-shadow:
      0 0 2px var(--card-bg), 0 0 2px var(--card-bg),
      0 0 2px var(--card-bg), 0 0 2px var(--card-bg);
    pointer-events: none;
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

  .cell-good { color: var(--good); font-weight: 600; }
  .cell-bad  { color: var(--bad);  font-weight: 600; }
  .cell-dim  { color: var(--muted-2); }

  .list { display: flex; flex-direction: column; }
  .list .item {
    display: grid;
    grid-template-columns: 1fr auto auto auto;
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
  .list .item .pillcol {
    display: flex; gap: 4px; align-items: center;
  }
  .list .item .pillcol .lbl {
    font-size: 9.5px; color: var(--muted-2);
    text-transform: uppercase; letter-spacing: .05em;
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

  .outcome-group { margin-top: 10px; }
  .outcome-group:first-child { margin-top: 0; }
  .outcome-group .grp-k {
    font-size: 10px; color: var(--muted-2);
    text-transform: uppercase; letter-spacing: .06em;
    margin-bottom: 6px;
  }

  .tun-grid {
    display: grid;
    grid-template-columns: 1fr;
    gap: 14px 26px;
    font-size: 12.5px;
  }
  @media (min-width: 700px) {
    .tun-grid { grid-template-columns: 1fr 1fr; }
  }
  .tun-group { min-width: 0; }
  .tun-group .gname {
    font-size: 10.5px; color: var(--muted-2);
    text-transform: uppercase; letter-spacing: .06em;
    margin: 0 0 4px;
    font-family: var(--mono);
  }
  .tun-group .grow {
    display: flex; justify-content: space-between; gap: 14px;
    padding: 3px 0; border-bottom: 1px solid var(--border);
    align-items: baseline;
    font-family: var(--mono); font-size: 11.5px;
  }
  .tun-group .grow:last-child { border-bottom: none; }
  .tun-group .grow .k {
    color: var(--muted);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .tun-group .grow .v {
    color: var(--fg); text-align: right;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
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
      <h3>swap target leaderboard <span class="cnt">top row = picker's choice</span></h3>
      <div id="lv-metric"></div>
      <div class="note">
        <b>score</b> = <code>rate_ema &times; swap_score / 256 &times; penalty</code>.
        Top row is what the picker would choose right now.
      </div>
    </section>

    <section class="c6">
      <h3>proof leaderboard <span class="cnt">Mb/s</span></h3>
      <div id="lv-proof"></div>
      <div class="note">
        bar colors:
        <b style="color:#4e79a7">sampled avg</b>,
        <b style="color:#59a14f">proven max</b>,
        <b style="color:#e15759">sampled max</b>.
      </div>
    </section>

    <section class="c6">
      <h3>recent swaps <span class="cnt">target + outcome</span></h3>
      <div id="lv-swaps"></div>
    </section>

    <section class="c6">
      <h3>recent proofs <span class="cnt">Mb/s</span></h3>
      <div id="lv-proofs"></div>
    </section>

    <section class="c8">
      <h3>rate progression <span class="cnt">client &middot; Mb/s</span></h3>
      <div id="lv-rate"></div>
    </section>

    <section class="c4">
      <h3>swap outcomes <span class="cnt">sustained</span></h3>
      <div id="lv-swapout"></div>
      <div id="lv-churn" style="margin-top:12px"></div>
      <div class="note">
        <b>sustained</b> = median srate in [t+60, t+300].
      </div>
    </section>

  </div>

  <section class="card">
    <h2><span class="dot"></span>rate_ema per algorithm &mdash; Mb/s</h2>
    <div class="chart-box h-lg"><canvas id="rate"></canvas></div>
  </section>

  <section class="card">
    <h2><span class="dot"></span>swap_score per algorithm <span class="sub">256 = neutral &middot; above = swaps into this alg have been helping</span></h2>
    <div class="chart-box h-lg"><canvas id="sscore"></canvas></div>
  </section>

  <section class="card">
    <h2><span class="dot"></span>bad_streak / null_streak per algorithm <span class="sub">above 0 = picker penalty in effect</span></h2>
    <div class="chart-box h-lg"><canvas id="streaks"></canvas></div>
  </section>

  <section class="card">
    <h2><span class="dot"></span>swaps per bin</h2>
    <div class="chart-box h-sm"><canvas id="swaps"></canvas></div>
  </section>

  <section class="card">
    <h2><span class="dot"></span>rate-board coverage &mdash; last 24h</h2>
    <div class="chart-box" id="fleetbox" style="height:500px"><canvas id="fleet"></canvas></div>
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
  function fmtUptime(s) {
    if (s == null) return "-";
    var d = Math.floor(s / 86400);
    var h = Math.floor((s % 86400) / 3600);
    var m = Math.floor((s % 3600) / 60);
    return (d ? d + "d " : "") + h + "h " + m + "m";
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
    var rows = [];
    if (s.kernel)     rows.push(["kernel", s.kernel, "hi"]);
    if (s.default_cc) rows.push(["default cc", s.default_cc, "hi"]);
    if (s.cpu_count != null) rows.push(["cpu", s.cpu_count + " cores"]);
    if (s.load_1 != null) {
      rows.push(["load",
        s.load_1.toFixed(2) + " / " + s.load_5.toFixed(2) +
        " / " + s.load_15.toFixed(2)]);
    }
    if (s.procs_total != null) {
      rows.push(["processes",
        (s.procs_running == null ? "?" : s.procs_running) +
        " running / " + s.procs_total + " total"]);
    }
    if (s.host_uptime_s != null) {
      rows.push(["host uptime", fmtUptime(s.host_uptime_s)]);
    }
    if (s.mem_total_bytes != null && s.mem_total_bytes > 0) {
      var used = (s.mem_used_bytes != null)
                 ? s.mem_used_bytes
                 : (s.mem_total_bytes - (s.mem_avail_bytes || 0));
      var pct = (s.mem_used_pct != null)
                ? s.mem_used_pct.toFixed(0) + "%"
                : "";
      rows.push(["memory",
        fmtBytes(used) + " / " + fmtBytes(s.mem_total_bytes) +
        (pct ? "  " + pct : "")]);
    }
    setHTML("lv-system", rows.map(function (r) {
      return '<div class="row"><span class="k">' + esc(r[0]) + '</span>' +
             '<span class="v ' + (r[2] || "") + '">' + esc(r[1]) + '</span></div>';
    }).join(""));
  }

  function renderTunables(groups) {
    var cnt = $("lv-tun-cnt");
    if (!groups || !groups.length) {
      if (cnt) cnt.textContent = "";
      setHTML("lv-tunables",
              '<div class="placeholder">(none seen in journal this boot)</div>');
      return;
    }
    var total = 0;
    groups.forEach(function (g) { total += g.items.length; });
    if (cnt) cnt.textContent = total + " keys · " + groups.length + " groups";
    setHTML("lv-tunables", groups.map(function (g) {
      var rows = g.items.map(function (it) {
        return '<div class="grow"><span class="k">' + esc(it.key) +
               '</span><span class="v">' + esc(it.value) + '</span></div>';
      }).join("");
      return '<div class="tun-group"><div class="gname">' +
             esc(g.group) + '</div>' + rows + '</div>';
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

  function renderRecentSwapsForBucket() {
    var by = state.recentSwapsByBucket;
    if (!by) {
      // fallback: pristine behaviour, unfiltered recent swaps
      renderRecentSwaps((state.lastLiveSwaps) || []);
      return;
    }
    var bs = $("bucket");
    var addr = (bs && bs.value) ? bs.value : null;
    if (!addr && state.meta && state.meta.default_bucket) {
      addr = state.meta.default_bucket;
    }
    var rows = (addr && by[addr]) ? by[addr] : [];
    renderRecentSwaps(rows);
  }

  function renderMetricForBucket() {
    var bs = $("bucket");
    var addr = (bs && bs.value) ? bs.value : null;
    var byB = state.metricByBucket || {};
    var keys = Object.keys(byB);
    var rows = (addr && byB[addr]) ? byB[addr]
                                   : (keys.length ? byB[keys[0]] : []);
    renderMetric(rows);
  }

  function renderMetric(rows) {
    if (!rows.length) {
      setHTML("lv-metric", '<div class="placeholder">(no metrics yet)</div>');
      return;
    }
    function colorSwapScore(v) {
      if (v == null) return "cell-dim";
      if (v > 256) return "cell-good";
      if (v < 256) return "cell-bad";
      return "cell-dim";
    }
    function colorPenalty(v) {
      if (v == null) return "cell-dim";
      if (v >= 0.99) return "cell-dim";
      if (v >= 0.8)  return "";
      if (v >= 0.5)  return "cell-bad";
      return "cell-bad";
    }
    function colorStreak(v) {
      if (v == null) return "cell-dim";
      if (v === 0) return "cell-good";
      return "cell-bad";
    }
    // Top row is the picker's choice: sorted by score in the CLI, so
    // the first non-inactive row wins.
    var picked = null;
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].active) { picked = i; break; }
    }
    var html = '<table class="tbl"><thead><tr>' +
      '<th>alg</th><th>rate_ema</th><th>swap_score</th>' +
      '<th>penalty</th><th>score</th><th>metric</th>' +
      '<th>bad</th><th>null</th>' +
      '</tr></thead><tbody>';
    rows.forEach(function (r, i) {
      var cls = [];
      if (!r.active) cls.push("inactive");
      if (i === picked) cls.push("pick");
      var trClass = cls.length ? ' class="' + cls.join(" ") + '"' : "";
      var pen = (r.penalty == null) ? "-" : r.penalty.toFixed(3);
      html += '<tr' + trClass + '>' +
        '<td class="name">' + esc(r.alg) + '</td>' +
        '<td class="mono">' + (r.rate_ema == null ? "-" : r.rate_ema) + '</td>' +
        '<td class="mono ' + colorSwapScore(r.swap_score) + '">' +
          (r.swap_score == null ? "-" : r.swap_score) + '</td>' +
        '<td class="mono ' + colorPenalty(r.penalty) + '">' + pen + '</td>' +
        '<td class="mono cell-good">' +
          (r.score == null ? "-" : r.score.toFixed(1)) + '</td>' +
        '<td class="mono dim">' + r.metric.toFixed(1) + '</td>' +
        '<td class="mono ' + colorStreak(r.bad_streak) + '">' +
          (r.bad_streak == null ? "-" : r.bad_streak) + '</td>' +
        '<td class="mono ' + colorStreak(r.null_streak) + '">' +
          (r.null_streak == null ? "-" : r.null_streak) + '</td>' +
        '</tr>';
    });
    setHTML("lv-metric", html + '</tbody></table>');
  }

  function renderProof(rows) {
    if (!rows.length) {
      setHTML("lv-proof", '<div class="placeholder">(none in tail)</div>');
      return;
    }
    var peak = 1;
    rows.forEach(function (r) {
      [r.proven_max, r.sampled_avg, r.sampled_max].forEach(function (v) {
        if (v != null && v > peak) peak = v;
      });
    });
    function bar(v, cls) {
      if (v == null) {
        return '<div class="bar-cell ' + cls + '">' +
               '<div class="bar-num">-</div></div>';
      }
      var w = Math.max(2, Math.round(100 * v / peak));
      return '<div class="bar-cell ' + cls + '">' +
             '<div class="bar-fill" style="width:' + w + '%"></div>' +
             '<div class="bar-num">' + v.toFixed(1) + '</div></div>';
    }
    var html = '<table class="tbl proof-tbl"><thead><tr>' +
      '<th>alg</th>' +
      '<th>good</th><th>proved</th>' +
      '<th style="text-align:left">proven max</th>' +
      '<th style="text-align:left">sampled avg</th>' +
      '<th style="text-align:left">sampled max</th>' +
      '<th>n</th>' +
      '</tr></thead><tbody>';
    rows.forEach(function (r) {
      html += '<tr>' +
        '<td class="name">' + esc(r.alg) + '</td>' +
        '<td class="mono dim">' + r.good + '</td>' +
        '<td class="mono dim">' + r.proved + '</td>' +
        '<td>' + bar(r.proven_max,  'v-proven' ) + '</td>' +
        '<td>' + bar(r.sampled_avg, 'v-avg'    ) + '</td>' +
        '<td>' + bar(r.sampled_max, 'v-sampled') + '</td>' +
        '<td class="mono dim">' +
          (r.samples == null ? "-" : r.samples) + '</td>' +
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

  function bigTriple(so) {
    return '<div class="bigstats">' +
      '<div class="big win"><div class="k">win</div>' +
        '<div class="v">' + so.win + '</div>' +
        '<div class="p">' + so.win_pct.toFixed(0) + '%</div></div>' +
      '<div class="big null"><div class="k">null</div>' +
        '<div class="v">' + so.null + '</div>' +
        '<div class="p">' + so.null_pct.toFixed(0) + '%</div></div>' +
      '<div class="big loss"><div class="k">loss</div>' +
        '<div class="v">' + so.loss + '</div>' +
        '<div class="p">' + so.loss_pct.toFixed(0) + '%</div></div>' +
      '</div>';
  }

  function renderSwapOutcomes(payload) {
    var so = null;
    if (payload && payload.sustained) {
      so = payload.sustained;
    } else if (payload && (payload.win != null || payload.loss != null)) {
      so = payload;
    } else {
      so = {win:0,win_pct:0,null:0,null_pct:0,
            loss:0,loss_pct:0,measurable:0,unmeasurable:0};
    }
    setHTML("lv-swapout", bigTriple(so) +
      '<div class="kv" style="margin-top:10px">' +
        '<div class="row"><span class="k">measurable</span>' +
          '<span class="v">' + so.measurable + '</span></div>' +
        '<div class="row"><span class="k">unmeasurable</span>' +
          '<span class="v dim">' + so.unmeasurable + '</span></div>' +
      '</div>');
  }

  function renderDivergence(rows) {
    if (!rows.length) {
      setHTML("lv-div", '<div class="placeholder">(no swaps)</div>');
      return;
    }
    var html = '<table class="tbl"><thead><tr>' +
      '<th>category</th><th style="width:32%">composite</th>' +
      '<th style="width:32%">sustained</th>' +
      '<th>n</th>' +
      '</tr></thead><tbody>';
    rows.forEach(function (r) {
      function bar(w, n, l) {
        var out = '<div class="cellbar">';
        if ((w + n + l) > 0) {
          if (w > 0) out += '<span class="w" style="width:' + w + '%">' +
            (w >= 8 ? w.toFixed(0) + '%' : '') + '</span>';
          if (n > 0) out += '<span class="n" style="width:' + n + '%">' +
            (n >= 8 ? n.toFixed(0) + '%' : '') + '</span>';
          if (l > 0) out += '<span class="l" style="width:' + l + '%">' +
            (l >= 8 ? l.toFixed(0) + '%' : '') + '</span>';
        } else {
          out += '<span class="n" style="width:100%">no data</span>';
        }
        out += '</div>';
        return out;
      }
      html += '<tr>' +
        '<td class="name">' + esc(r.category) + '</td>' +
        '<td>' + bar(r.win_pct, r.null_pct, r.loss_pct) + '</td>' +
        '<td>' + bar(r.win_pct_sustained, r.null_pct_sustained,
                     r.loss_pct_sustained) + '</td>' +
        '<td class="mono dim">' + r.measured + ' / ' +
          r.measured_sustained + '</td>' +
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
        '<span class="meta">' + esc(r.dest || "?") +
          ' &middot; ' + r.mbps.toFixed(1) + ' Mb/s</span>' +
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
      var o = r.outcome_sustained || r.outcome || "";
      var pill = o
        ? '<span class="sp ' + o + '">' + o + '</span>'
        : '<span class="sp dash">&hellip;</span>';
      html += '<div class="item">' +
        '<span class="flow">' + esc(r.from_alg) +
          '<span class="arrow">&rarr;</span>' + esc(r.to_alg) + '</span>' +
        '<span class="meta">' + esc(r.dest || "?") +
          ' &middot; d' + r.d + '</span>' +
        pill +
        '</div>';
    });
    setHTML("lv-swaps", html + '</div>');
  }

  function renderLiveState(doc) {
    renderBuild(doc.build || {});
    renderSystem(doc.system || {});
    renderTunables(doc.tunables || []);
    renderBuckets(doc.buckets || []);
    state.metricByBucket = doc.metric_by_bucket || {};
    state.bucketLive = doc.bucket_live || {};
    state.recentSwapsByBucket = doc.recent_swaps_by_bucket || null;
    renderMetricForBucket();
    renderRecentSwapsForBucket();
    if ($("range") && $("range").value === "1h" && state.bucketDoc) {
      renderBucket();   // refresh 1h chart from live data
    }
    renderProof(doc.proof || []);
    renderRate(doc.rate || []);
    renderSwapOutcomes(doc.swap_outcomes || null);
    renderChurn(doc.churn || {});
    renderRecentProofs(doc.recent_proofs || []);
    state.lastLiveSwaps = doc.recent_swaps || [];
    renderRecentSwapsForBucket();
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

  var state  = { meta: null, bucketDoc: null, swaps: null, fleet: null, metricByBucket: null, bucketLive: {}, recentSwapsByBucket: null };
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

  function lineData(cols, series, ts, colors, scale, pointRadius) {
    scale = scale || function (v) { return v; };
    var pr = (pointRadius == null) ? 0 : pointRadius;
    return cols.map(function (c, i) {
      return {
        label: c,
        data: (series[c] || []).map(function (y, k) {
          return {x: ts[k] * 1000, y: y == null ? null : scale(y)};
        }),
        borderColor: colors[i % colors.length],
        backgroundColor: colors[i % colors.length],
        pointRadius: pr,
        pointHoverRadius: Math.max(3, pr + 1),
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
    // 0.4.76: for the 1h range, prefer the CLI-provided live
    // series (30s freshness) over the 15-minute renderer output.
    // bucket_live carries re_* columns only, so this override
    // applies to the rate chart alone; the score and streak
    // charts always use the historical series.
    var rateS = s, rateTs = ts;
    var bid = $("bucket") ? $("bucket").value : null;
    if (rng === "1h" && bid && state.bucketLive && state.bucketLive[bid]) {
      var lb = state.bucketLive[bid];
      if (lb.ts && lb.ts.length) {
        var liveS = {};
        for (var k in (lb.cols || {})) liveS[k] = lb.cols[k];
        rateS = liveS;
        rateTs = lb.ts;
      }
    }

    function makeSeries(prefix, source) {
      source = source || s;
      return algs.map(function (a) {
        return prefix + a;
      }).filter(function (c) { return c in source; });
    }

    var rateCols = makeSeries("re_", rateS);
    mk("rate", {
      type: "line",
      data: {datasets: lineData(rateCols, rateS, rateTs, PALETTE, scaleRe, 0)},
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

    var ssCols = makeSeries("ss_");
    mk("sscore", {
      type: "line",
      data: {datasets: lineData(ssCols, s, ts, PALETTE, null, 0)},
      options: timeOpts({
        scales: {
          x: {type: "time", time: {tooltipFormat: "MMM d, HH:mm"},
              grid: {display: false},
              ticks: {maxRotation: 0, autoSkipPadding: 24, padding: 4}},
          y: {beginAtZero: false, grid: {drawTicks: false},
              ticks: {maxTicksLimit: 6, padding: 6}},
        },
        plugins: {
          legend: {
            display: true, position: "bottom", align: "start",
            labels: {boxWidth: 8, boxHeight: 8, padding: 8,
                     font: {size: 10.5}},
          },
        },
      }),
    });

    var bsCols = makeSeries("bs_");
    var nsCols = makeSeries("ns_");
    var streakSets = [];
    streakSets = streakSets.concat(
      lineData(bsCols, s, ts, PALETTE, null, 0).map(function (ds) {
        ds.borderDash = [4, 3];
        ds.label = ds.label.replace(/^bs_/, "") + " bad";
        return ds;
      }));
    streakSets = streakSets.concat(
      lineData(nsCols, s, ts, PALETTE, null, 0).map(function (ds) {
        ds.label = ds.label.replace(/^ns_/, "") + " null";
        return ds;
      }));
    mk("streaks", {
      type: "line",
      data: {datasets: streakSets},
      options: timeOpts({
        scales: {
          x: {type: "time", time: {tooltipFormat: "MMM d, HH:mm"},
              grid: {display: false},
              ticks: {maxRotation: 0, autoSkipPadding: 24, padding: 4}},
          y: {beginAtZero: true, grid: {drawTicks: false},
              ticks: {maxTicksLimit: 6, padding: 6, precision: 0}},
        },
        plugins: {
          legend: {
            display: true, position: "bottom", align: "start",
            labels: {boxWidth: 8, boxHeight: 8, padding: 8,
                     font: {size: 10.5}},
          },
        },
      }),
    });
  }

  function renderDivChart(canvasId, suffix) {
    var doc = state.swaps, rng = $("range").value;
    var d = doc[rng];
    if (!d) return;
    var ts = d.ts;
    var sfx = suffix || "";

    function mkLine(key, label, color, dash) {
      return {
        label: label,
        data: (d[key] || []).map(function (y, k) {
          return {x: ts[k] * 1000, y: y};
        }),
        borderColor: color,
        backgroundColor: color,
        borderDash: dash || [],
        pointRadius: 2,
        pointHoverRadius: 4,
        borderWidth: 1.5,
        spanGaps: true,
      };
    }

    mk(canvasId, {
      type: "line",
      data: {datasets: [
        mkLine("d1_rate" + sfx, "diverges=1", "#59a14f"),
        mkLine("d1_lo"   + sfx, "d1 95% lo", "#59a14f", [4, 3]),
        mkLine("d1_hi"   + sfx, "d1 95% hi", "#59a14f", [4, 3]),
        mkLine("d0_rate" + sfx, "diverges=0", "#e15759"),
        mkLine("d0_lo"   + sfx, "d0 95% lo", "#e15759", [4, 3]),
        mkLine("d0_hi"   + sfx, "d0 95% hi", "#e15759", [4, 3]),
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
                       return !/_lo|_hi/.test(item.text);
                     }},
          },
        },
      }),
    });
  }

  function sustainedHasData() {
    var doc = state.swaps;
    if (!doc) return false;
    var d = doc["24h"] || doc["7d"] || doc["all"];
    if (!d) return false;
    var arr = d.d1_n_sustained || [];
    for (var i = 0; i < arr.length; i++) {
      if ((arr[i] || 0) > 0) return true;
    }
    return false;
  }

  function renderSwaps() {
    var doc = state.swaps, rng = $("range").value;
    var d = doc[rng];
    if (!d) return;
    var ts = d.ts;

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

  function renderDivergenceCharts() {
    renderDivChart("div", "");
    renderDivChart("div_sustained", "_sustained");
    var note = document.getElementById("div_sustained_note");
    if (note) {
      if (sustainedHasData()) {
        note.textContent = "sustained = median srate in [t+60, t+300], " +
          "compared to last srate before the swap. Excludes the cwnd-reset " +
          "dip. This is the accurate throughput measure.";
        note.style.color = "";
      } else {
        note.textContent = "no sustained data yet. Requires 0.4.53+ emitting " +
          "`srate` lines AND the collector writing srate.csv AND a swap " +
          "landing with 60+ s of post-swap votes. Will populate on its own.";
        note.style.color = "#9aa0a6";
      }
    }
  }

  function renderFleet() {
    var f = state.fleet;
    var n = (f && f.buckets) ? f.buckets.length : 0;
    var box = document.getElementById("fleetbox");
    if (box) {
      box.style.height = Math.max(200, Math.min(800, n * 22 + 40)) + "px";
    }
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
          maxBarThickness: 12,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        animation: false, indexAxis: "y",
        layout: {padding: {top: 4, right: 16, bottom: 0, left: 0}},
        scales: {
          x: {min: 0, max: 100, grid: {drawTicks: false},
              ticks: {maxTicksLimit: 6, padding: 6,
                      callback: function (v) { return v + "%"; }}},
          y: {grid: {display: false},
              ticks: {font: {size: 10.5}, autoSkip: false, padding: 4}},
        },
        plugins: {legend: {display: false}},
      },
    });
  }

  function loadBucket(id) {
    // 0.4.78.1: sanitize the same way the renderer did when it
    // wrote the file -- bucket ids like "v6:XXXXXXXX" become
    // "v6_XXXXXXXX" on disk.
    var safe = (id || "").replace(/[^A-Za-z0-9._-]/g, "_");
    return j("data/bucket_" + safe + ".json").then(function (doc) {
      state.bucketDoc = doc;
      renderBucket();
      renderNow();
      // 0.4.78: recent-swaps panel follows the dropdown too.
      // loadBucket runs after bs.value is set (both boots and
      // onchange), so this is the right place to re-render.
      renderRecentSwapsForBucket();
    });
  }

  function refreshNowCardAndChart() {
    var id = $("bucket").value;
    if (id) loadBucket(id);
  }

  function refreshAll() {
    var keep = $("bucket").value;
    Promise.all([
      j("data/meta.json"),
      j("data/swaps.json"),
      j("data/fleet.json"),
    ]).then(function (results) {
      state.meta  = results[0];
      state.swaps = results[1];
      state.fleet = results[2];

      var bs = $("bucket");
      var html = "";
      var stillThere = false;
      for (var k = 0; k < state.meta.buckets.length; k++) {
        var b = state.meta.buckets[k];
        html += '<option value="' + b.id + '">' + b.id +
                ' (' + b.points + ')</option>';
        if (b.id === keep) stillThere = true;
      }
      bs.innerHTML = html;
      bs.value = stillThere ? keep : state.meta.default_bucket;

      var stamp = new Date(state.meta.generated_ts * 1000).toISOString()
                      .replace("T", " ").slice(0, 19) + "Z";
      if (footgen) footgen.textContent = "rendered " + stamp;
      status("updated " + relTime(state.meta.generated_ts));

      return loadBucket(bs.value);
    }).then(function () {
      renderSwaps();
      renderFleet();
    }).catch(function (e) {
      err("refresh: " + (e && e.message ? e.message : e), e);
    });
  }

  function boot() {
    liveRefresh();
    setInterval(function () {
      liveRefresh();
      refreshNowCardAndChart();
    }, 30000);

    setInterval(refreshAll, 300000);

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
        var saved_bucket = null;
        try { saved_bucket = localStorage.getItem("bpftune.bucket"); } catch (e) {}
        var still_there = false;
        if (saved_bucket) {
          for (var q = 0; q < state.meta.buckets.length; q++) {
            if (state.meta.buckets[q].id === saved_bucket) { still_there = true; break; }
          }
        }
        bs.value = still_there ? saved_bucket : state.meta.default_bucket;

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

        bs.onchange = function () {
          try { localStorage.setItem("bpftune.bucket", bs.value); } catch (e) {}
          loadBucket(bs.value);
          renderMetricForBucket();
          renderRecentSwapsForBucket();
        };
        try { localStorage.setItem("bpftune.bucket", bs.value); } catch (e) {}
        rs.onchange = function () {
          renderBucket();
          renderSwaps();
        };

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
    try:
        os.nice(19)
    except (OSError, AttributeError):
        pass
    now = int(time.time())
    bfile = buckets_source()
    if not bfile:
        print("renderer: no buckets CSV found yet")
        return 1

    # Peek at the header first — algs is derived from it, and
    # aggregate_all needs algs.
    with open(bfile, "r", newline="") as _f:
        _hdr = next(csv.reader(_f))
    algs = sorted({c[3:] for c in _hdr if c.startswith("re_")})

    _, swaps = load_csv(os.path.join(HIST, "swaps.csv"),
                        max_rows=MAX_ROWS_SWAPS)
    _, srate = load_csv(os.path.join(HIST, "srate.csv"),
                        max_rows=MAX_ROWS_SRATE)
    header, cols, docs, meta_stats = aggregate_all(bfile, algs, now)

    # meta needs the same stats we gathered during streaming.
    meta_rows = []
    for bid in [d["id"] for d in docs]:
        st = meta_stats.get(bid) or {}
        meta_rows.append({
            "addr": bid,
            "instances": (st.get("inst_sum", 0) / st["inst_n"]
                          if st.get("inst_n") else 0),
            "collected_ts": st.get("last_ts", 0),
            "pts24": st.get("pts24", 0),
        })

    # 0.4.71: no primary pin.  emit_meta sorts entries by
    # instances_mean; that is the default bucket.
    emit_meta(meta_rows, algs, now)

    for doc in docs:
        bid = doc["id"]
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in bid)
        write_json("bucket_%s.json" % safe, doc)

    def _safe(b):
        return "".join(c if c.isalnum() or c in "-_." else "_" for c in b)
    emitted_safes = {_safe(d["id"]) for d in docs}
    cleaned = 0
    data_dir = Path(DATA)
    if data_dir.exists():
        for stale in data_dir.glob("bucket_*.json"):
            stem = stale.stem[len("bucket_"):]
            if stem not in emitted_safes:
                try:
                    stale.unlink()
                    cleaned += 1
                except OSError:
                    pass

    emit_swaps(swaps, srate, now)
    emit_fleet(meta_rows, now)

    # 0.4.76: the main() rewrite that added streaming aggregation
    # dropped this write; the renderer has been updating JSON for
    # weeks while index.html went stale.
    with open(HIST + "/index.html", "w") as f:
        f.write(INDEX_HTML)

    globals()["_CLEANED"] = cleaned
    globals()["_EMITTED"] = len(docs)
    globals()["_SKIPPED"] = max(0, len(meta_stats) - len(docs))
    globals()["_ROWS"] = sum(
        sum(len(v) for v in d["series"]["24h"].values()
            if isinstance(v, list)) for d in docs) or 0


    print("renderer: %d buckets (%d emitted, %d skipped, %d cleaned), "
          "%d algs, %d swaps, %d srate rows"
          % (len(meta_stats), globals().get("_EMITTED", 0),
             globals().get("_SKIPPED", 0), globals().get("_CLEANED", 0),
             len(algs), len(swaps), len(srate)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
