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

_LABELS_CACHE = None
_LABELS_MTIME = None

def _load_labels():
    """0.4.79: {canonical_ip: label} from aliases.labels.json."""
    global _LABELS_CACHE, _LABELS_MTIME
    path = "/var/lib/bpftune/aliases.labels.json"
    try:
        m = os.path.getmtime(path)
    except OSError:
        _LABELS_CACHE = {}
        _LABELS_MTIME = None
        return _LABELS_CACHE
    if _LABELS_CACHE is not None and _LABELS_MTIME == m:
        return _LABELS_CACHE
    try:
        with open(path) as f:
            d = json.load(f)
        _LABELS_CACHE = d if isinstance(d, dict) else {}
    except Exception:
        _LABELS_CACHE = {}
    _LABELS_MTIME = m
    return _LABELS_CACHE


def _canon_bucket(addr):
    """0.4.79: collapse to /16 (v4) or /32 (v6).  During the
    prefix4/6 testing the collector wrote some rows at /24 and
    /32, so identical physical buckets ended up as two keys.
    This makes meta.json and bucket_live agree."""
    if not addr:
        return addr
    if addr.startswith('v6:'):
        return addr
    p = addr.split('.')
    if len(p) == 4:
        return '%s.%s.0.0' % (p[0], p[1])
    return addr


def _label_for(addr):
    if not addr:
        return addr
    addr = _canon_bucket(addr)
    return _load_labels().get(addr, addr)

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
    MIN_BUCKET_ROWS = 5

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
            a = _label_for(a)
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
    rbv_i  = cols.get("rate_best_v")

    with open(bfile, "r", newline="") as f:
        rd = csv.reader(f)
        next(rd)
        hlen = len(header)
        for row in rd:
            if len(row) < hlen:
                continue
            a = row[ai]
            a = _label_for(a)
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
                # 0.4.78.2: 5-min bin coverage for the fleet chart.
                # A bucket is "covered" in a bin if any row in that
                # bin had rate_best_v > 0 (i.e. a rate leader existed).
                if "co_seen" not in ms:
                    ms["co_seen"] = set()
                    ms["co_have"] = set()
                b5 = int(t // 300)
                ms["co_seen"].add(b5)
                if rbv_i is not None and rbv_i < len(row):
                    rv = row[rbv_i]
                    if rv:
                        try:
                            if float(rv) > 0:
                                ms["co_have"].add(b5)
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
        # 0.4.87: also emit the human-readable label so the dropdown
        # can show "home-sco" instead of "76.76.0.0".  Falls back to
        # the raw addr if no label is defined.
        label = _label_for(bid)
        entries.append({
            "id": bid,
            "label": label if label and label != bid else bid,
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


def emit_fleet(meta_stats, now):
    """Per-bucket: fraction of 5-minute bins in the last 24h where the
    bucket's rate_best_v was > 0.  meta_stats carries the two sets
    (co_seen / co_have) filled by aggregate_all's streaming pass.
    The prior body tried to re-derive this from meta_rows, which only
    ever carried addr / instances / collected_ts -- rate_best_v was
    never present, so every bucket came back 0.0."""
    pairs = []
    for bid, ms in meta_stats.items():
        seen = ms.get("co_seen") or set()
        if not seen:
            continue
        have = len(ms.get("co_have") or ())
        pairs.append((bid, round(100.0 * have / len(seen), 1)))
    pairs.sort(key=lambda p: -p[1])
    pairs = pairs[:25]
    labels = [p[0] for p in pairs]
    cov    = [p[1] for p in pairs]
    write_json("fleet.json", {"buckets": labels, "coverage_24h": cov})


# INDEX_HTML template is now read from index.html in the same directory
# (no more embedded template - patches go to index.html, render.py picks them up)
_SOURCE_INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
with open(_SOURCE_INDEX) as _f:
    INDEX_HTML = _f.read()


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
    emit_fleet(meta_stats, now)

    # 0.4.76: the main() rewrite that added streaming aggregation
    # dropped this write; the renderer has been updating JSON for
    # weeks while index.html went stale.
    # 0.4.83: index.html is a symlink to /opt/bpftune-dashboard/bin/index.html
    # No need to write here — nginx serves the symlink directly.
    # with open(HIST + "/index.html", "w") as f:
    #     f.write(INDEX_HTML)

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
