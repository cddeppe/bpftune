#!/usr/bin/env python3
"""bpftune collector. Run once per minute from cron.

Buckets from `bpftool --json map dump name remote_host_map`. Each
bucket's per-alg row carries:

  mv_<alg>  metric_value
  re_<alg>  rate_ema
  ss_<alg>  swap_score  (256 = neutral, higher = swaps into this alg helped)
  bs_<alg>  bad_streak  (feeds the picker's penalty; no longer a gate)
  ns_<alg>  null_streak (feeds the picker's penalty; no longer a gate)

Swaps from all /var/log/bpftune-met-*.log (per-file byte offsets in
.swaps_pos.json).
Raw srate events from the same logs -> /var/lib/bpftune/history/srate.csv.
Snapshot: `bpftune-cli.py --json` -> /var/lib/bpftune/history/current.json

`collected_ts` is wall clock. `boot_ts` is monotonic (log seconds).

Outcome / direction
-------------------
A swap's outcome needs a post-swap met line, which almost never lands
in the same 60-second cron run as the swap.  Rows are held in
`state["pending_swaps"]` and resolved on a later run once the post
line arrives.  Rows whose [boot_ts+3, boot_ts+300] window has closed
with no post are written with outcome="no_post".  The collector never
writes an unresolved row to swaps.csv.

`direction` is "origin" when the socket's rport == 443, else "client".
Sourced from the met line's rport: the last pre-swap vote, or the post
at resolve time.  Matches tools/swap-outcomes-bydir.py.

`met_cache` is a per-cookie list of recent met events, pruned to
MET_CACHE_TTL_S.  It must hold more than the latest event so a swap
followed by two votes can still find the first in-window post.

STATE_VERSION 2: pending_swaps / rport / met-list added.  On version
mismatch the state is reset; only caches and in-flight pendings are
lost (at most 300 seconds of unresolved swaps).
"""
import csv, io, json, os, re, socket, sqlite3, struct, subprocess, sys, tempfile, time
import importlib.util
import threading, hashlib
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
# Phase 2: import the labels module so the collector can serve /api/labels
# directly, without needing the separate labels-api process on :8081.
import sys as _sys
_sys.path.insert(0, "/opt/bpftune-dashboard/bin")
try:
    import bpftune_labels_api as _labels
except ImportError:
    try:
        _sys.path.insert(0, "/root/bpftune/dashboard/bin")
        import bpftune_labels_api as _labels
    except ImportError:
        _labels = None
from pathlib import Path

# SSE server state — _last_result holds the latest collect_all() output.
# The collection loop writes to it (under _result_lock) after each cycle.
# The SSE server reads it to push updates to connected browsers.
_last_result = None
_last_result_hashes = {}
_last_full_result = None  # last 5-min full collect_all() result (for merging)
_result_lock = threading.Lock()


_LABELS_CACHE = None
_LABELS_MTIME = None

def _load_labels():
    """0.4.79: /var/lib/bpftune/aliases.labels.json {canonical_ip: label}."""
    global _LABELS_CACHE, _LABELS_MTIME
    import os as _os, json as _json
    path = "/var/lib/bpftune/aliases.labels.json"
    try:
        m = _os.path.getmtime(path)
    except OSError:
        _LABELS_CACHE = {}
        _LABELS_MTIME = None
        return _LABELS_CACHE
    if _LABELS_CACHE is not None and _LABELS_MTIME == m:
        return _LABELS_CACHE
    try:
        with open(path) as f:
            d = _json.load(f)
        _LABELS_CACHE = d if isinstance(d, dict) else {}
    except Exception:
        _LABELS_CACHE = {}
    _LABELS_MTIME = m
    return _LABELS_CACHE


_FOLD_CACHE = None
_FOLD_MTIME = None

def _load_fold():
    """0.4.79: {"v6:XXXXXXXX": "canonical"} from /32-form entries in
    /etc/bpftune/aliases.  Line form:
        2603:c020:0:0:0:0:0:0 = 89.168.0.0 [label]
    Only lines where groups 2..7 are all zero are treated as /32 folds."""
    global _FOLD_CACHE, _FOLD_MTIME
    import os as _os
    path = "/etc/bpftune/aliases"
    try:
        m = _os.path.getmtime(path)
    except OSError:
        _FOLD_CACHE = {}
        _FOLD_MTIME = None
        return _FOLD_CACHE
    if _FOLD_CACHE is not None and _FOLD_MTIME == m:
        return _FOLD_CACHE
    out = {}
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                lhs, rhs = line.split("=", 1)
                lhs = lhs.strip()
                bits = rhs.strip().split()
                if not bits:
                    continue
                to = bits[0]
                if "." not in to:
                    continue
                if ":" not in lhs:
                    continue
                groups = lhs.split(":")
                if len(groups) != 8:
                    continue
                if not all(g in ("0", "0000", "") for g in groups[2:]):
                    continue
                key = "v6:" + (groups[0].zfill(4) + groups[1].zfill(4)).lower()
                out[key] = to
    except Exception:
        out = {}
    _FOLD_CACHE = out
    _FOLD_MTIME = m
    return out


def _fold_v6(addr):
    """0.4.79: replace a v6 /32 bucket key with its canonical v4 bucket
    if that /32 is declared in /etc/bpftune/aliases."""
    if not addr or not addr.startswith("v6:"):
        return addr
    return _load_fold().get(addr, addr)


def _label_for(addr):
    if not addr:
        return addr
    return _load_labels().get(addr, addr)


HIST = Path("/var/lib/bpftune/history")
HIST.mkdir(parents=True, exist_ok=True)
BUCKETS_CSV  = HIST / "buckets.v2.csv"
SWAPS_CSV    = HIST / "swaps.csv"
SRATE_CSV    = HIST / "srate.csv"

SQLITE_DB = HIST / "bpftune.db"
_sqlite_conn = None

def _init_sqlite():
    global _sqlite_conn
    try:
        _sqlite_conn = sqlite3.connect(str(SQLITE_DB), check_same_thread=False)
        _sqlite_conn.execute("PRAGMA journal_mode=WAL")
        _sqlite_conn.execute("PRAGMA synchronous=NORMAL")
        alg_cols = []
        for alg in CONGS:
            for prefix in ("re_", "ss_", "bs_", "ns_", "mv_"):
                alg_cols.append(f"{prefix}{alg} REAL")
        cols = ["collected_ts REAL", "addr TEXT", "best_alg TEXT",
                "instances REAL", "ref_rate REAL", "min_rtt REAL",
                "rate_best_i REAL", "rate_best_v REAL", "tcp_rmem_max REAL"
               ] + alg_cols
        _sqlite_conn.execute(f"CREATE TABLE IF NOT EXISTS buckets ({', '.join(cols)})")
        _sqlite_conn.execute("CREATE INDEX IF NOT EXISTS idx_buckets_ts ON buckets(collected_ts)")
        _sqlite_conn.execute("CREATE INDEX IF NOT EXISTS idx_buckets_addr ON buckets(addr)")
        _sqlite_conn.execute("CREATE INDEX IF NOT EXISTS idx_buckets_ts_addr ON buckets(collected_ts, addr)")
        for _w in (60, 300, 3600, 21600):
            _col = f"bin_{_w}"
            try:
                _sqlite_conn.execute(f"ALTER TABLE buckets ADD COLUMN {_col} INTEGER GENERATED ALWAYS AS (CAST(collected_ts AS INTEGER) / {_w}) STORED")
            except Exception: pass
            _sqlite_conn.execute(f"CREATE INDEX IF NOT EXISTS idx_buckets_addr_{_col} ON buckets(addr, {_col})")
        _sqlite_conn.execute("CREATE TABLE IF NOT EXISTS swaps (collected_ts REAL, boot_ts REAL, cookie TEXT, from_alg TEXT, to_alg TEXT, d INTEGER, mt_alg TEXT, rb_alg TEXT, diverges INTEGER, outcome TEXT, socket_rate_before REAL, dest TEXT, dest_raw TEXT, f_ema REAL, t_ema REAL, srate_before REAL, direction TEXT, rport TEXT)")
        _sqlite_conn.execute("CREATE INDEX IF NOT EXISTS idx_swaps_ts ON swaps(boot_ts)")
        _sqlite_conn.execute("CREATE INDEX IF NOT EXISTS idx_swaps_dest ON swaps(dest)")
        _sqlite_conn.execute("CREATE TABLE IF NOT EXISTS srate (collected_ts REAL, boot_ts REAL, cookie TEXT, alg TEXT, srate REAL)")
        _sqlite_conn.execute("CREATE INDEX IF NOT EXISTS idx_srate_ts ON srate(collected_ts)")
        _sqlite_conn.commit()
        print(f"collector: SQLite DB ready at {SQLITE_DB}", file=sys.stderr)
    except Exception as e:
        print(f"collector: SQLite init failed (CSV still works): {e}", file=sys.stderr)
        _sqlite_conn = None

# _init_sqlite() moved to after CONGS
SWAPS_POS    = HIST / ".swaps_pos.json"
CURRENT_JSON = HIST / "current.json"

STATE_VERSION = 3

SELF_DIR = Path(__file__).resolve().parent
CLI      = SELF_DIR / "bpftune-cli.py"

# Import the CLI module once at daemon startup (avoids per-cycle subprocess).
# The hyphen in bpftune-cli.py means we can't use a normal import.
_cli_mod = None
# In-memory log offsets for incremental reading (daemon mode only).
# Persists across cycles so tail_incremental() reads only new lines.
_log_offsets = {}
try:
    _spec = importlib.util.spec_from_file_location("bpftune_cli", str(CLI))
    _cli_mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_cli_mod)
except Exception as _e:
    print("collector: failed to import CLI module: %s" % _e, file=sys.stderr)

CONGS = ["cubic", "bbr", "htcp", "dctcp", "scalable", "vegas", "veno",
         "westwood", "reno", "illinois", "yeah", "lp", "bic", "highspeed",
         "hybla", "nv"]

_init_sqlite()

MIN_INST = 2

MET_CACHE_TTL_S = 600.0

SWAP_FIELDS = [
    "collected_ts", "boot_ts", "cookie", "from_alg", "to_alg", "d",
    "mt_alg", "rb_alg", "diverges", "outcome", "socket_rate_before",
    "dest", "dest_raw", "f_ema", "t_ema", "srate_before",
    "direction", "rport",
]
SRATE_FIELDS = ["collected_ts", "boot_ts", "cookie", "alg", "srate"]

# 0.4.76: sustained classification truth, consumed by the tuner's
# reanchor worker.  One line per resolved swap; the worker reads and
# truncates each pass (~30s), so growth is bounded.
SWAPS_TRUTH = HIST / "swapscore_truth.jsonl"

# 0.4.76-live: rolling JSON file the dashboard reads on a fast cadence.
# Mirrors every row that lands in swaps.csv.  No effect on the CSV.
DATA_DIR = HIST / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
SWAPS_LIVE = DATA_DIR / "swaps-live.json"
LIVE_MAX = 100

SWAP_RX = re.compile(
    r"(\d+\.\d+): bpf_trace_printk: swap cookie=(\d+) "
    r"from=(\d+) to=(\d+) bc=(\d+) ac=(\d+) d=(\d+)"
    r"(?: mt=(\d+) rb=(\d+))?"
    r"(?: dest=(\d+))?"
    r"(?: dest6=(\d+))?")
ESTAB_DEST_RX = re.compile(
    r"(\d+\.\d+): bpf_trace_printk: estab cookie=(\d+) "
    r"alg=(\d+) forced=\d+ dest=(\d+)(?: dest6=(\d+))?")
MET_RX = re.compile(
    r"(\d+\.\d+): bpf_trace_printk: met cookie=(\d+) "
    r"rport=(\d+) alg=(\d+) segs=(\d+) val=(\d+)")
SRATE_RX = re.compile(
    r"(\d+\.\d+): bpf_trace_printk: srate cookie=(\d+) "
    r"alg=(\d+) srate=(\d+)")


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


_CSV_HEADER_CACHE = {}
_CSV_BUFFERS = {}


def _csv_header(path, default_cols):
    key = str(path)
    h = _CSV_HEADER_CACHE.get(key)
    if h is not None:
        return h
    p = Path(path)
    if p.exists() and p.stat().st_size > 0:
        with open(p, "r", newline="", encoding="utf-8") as f:
            try:
                h = next(csv.reader(f))
            except StopIteration:
                h = list(default_cols)
    else:
        h = list(default_cols)
    _CSV_HEADER_CACHE[key] = h
    return h


# 0.4.76-live: rolling in-memory ring of the last LIVE_MAX rows.
# Loaded lazily at first append; flushed once by main() after the
# CSV buffers flush.  Bounded: LIVE_MAX * ~400B = ~40KB.
_LIVE_RING = None
_LIVE_DIRTY = False


def _live_load():
    global _LIVE_RING
    if _LIVE_RING is not None:
        return
    _LIVE_RING = []
    if SWAPS_LIVE.exists():
        try:
            with open(SWAPS_LIVE) as f:
                d = json.load(f)
            if isinstance(d, list):
                _LIVE_RING = d[-LIVE_MAX:]
        except Exception:
            _LIVE_RING = []


def _live_append(row):
    global _LIVE_DIRTY
    _live_load()
    _LIVE_RING.append(row)
    del _LIVE_RING[:-LIVE_MAX]
    _LIVE_DIRTY = True


def _live_flush():
    global _LIVE_DIRTY
    if not _LIVE_DIRTY or _LIVE_RING is None:
        return
    tmp = str(SWAPS_LIVE) + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(_LIVE_RING, f, separators=(",", ":"))
        os.replace(tmp, str(SWAPS_LIVE))
        _LIVE_DIRTY = False
    except Exception as e:
        print("collector: live flush failed: %s" % e, file=sys.stderr)


def buffer_csv(path, row, fields=None):
    key = str(path)
    cols = _csv_header(key, fields or list(row.keys()))
    _CSV_BUFFERS.setdefault(key, []).append([row.get(c, "") for c in cols])
    if key == str(SWAPS_CSV):
        _live_append(row)


def flush_csv_buffers():
    for key, rows in _CSV_BUFFERS.items():
        if not rows:
            continue
        p = Path(key)
        needs_header = not (p.exists() and p.stat().st_size > 0)
        with open(p, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if needs_header:
                w.writerow(_CSV_HEADER_CACHE[key])
            w.writerows(rows)
        if _sqlite_conn is not None:
            try:
                _sqlite_write_buffered(key, rows)
            except Exception as e:
                print(f"collector: SQLite write failed for {key}: {e}", file=sys.stderr)
    _CSV_BUFFERS.clear()


def _sqlite_write_buffered(csv_key, rows):
    """Write buffered rows to SQLite. Filters CSV columns to only those
    that exist in the SQLite table (prevents 'no such column' errors)."""
    if csv_key == str(BUCKETS_CSV):
        table = "buckets"
        csv_cols = _CSV_HEADER_CACHE.get(csv_key, [])
    elif csv_key == str(SWAPS_CSV):
        table = "swaps"
        csv_cols = SWAP_FIELDS
    elif csv_key == str(SRATE_CSV):
        table = "srate"
        csv_cols = SRATE_FIELDS
    else:
        return
    if not csv_cols or not rows:
        return
    try:
        table_cols = [r[1] for r in _sqlite_conn.execute(
            f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        return
    col_map = [(i, c) for i, c in enumerate(csv_cols) if c in table_cols]
    if not col_map:
        return
    sqlite_cols = [c for _, c in col_map]
    csv_indices = [i for i, _ in col_map]
    placeholders = ",".join(["?"] * len(sqlite_cols))
    filtered_rows = [[row[i] for i in csv_indices] for row in rows]
    _sqlite_conn.executemany(
        f"INSERT INTO {table} ({','.join(sqlite_cols)}) VALUES ({placeholders})",
        filtered_rows
    )
    _sqlite_conn.commit()


def list_logs():
    return sorted(Path("/var/log").glob("bpftune-met-*.log"),
                  key=lambda p: p.stat().st_mtime)


def read_map_data():
    out = sh(["bpftool", "--json", "map", "dump", "name",
              "remote_host_map"])
    try:
        data = json.loads(out)
    except Exception:
        return None, ""
    if not isinstance(data, list):
        return None, ""
    result = {}
    for e in data:
        if not isinstance(e, dict):
            continue
        fmt = e.get("formatted") or {}
        v = fmt.get("value")
        k = fmt.get("key") or {}
        if not isinstance(v, dict):
            continue
        b = k.get("in6_u", {}).get("u6_addr8")
        if not isinstance(b, list) or len(b) != 16:
            continue
        if b[10] == 0xff and b[11] == 0xff:
            addr = ".".join(str(x) for x in b[12:16])
        else:
            v6 = (b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3]
            addr = "v6:%08x" % v6 if v6 else "0.0.0.0"
        result[addr] = v
    return result, out


def collect_buckets(ts_epoch, map_data):
    if not map_data:
        return 0
    rm_min, rm_def, rm_max = tcp_rmem()
    n = 0
    # 0.4.79: pre-fold and merge.  Two map keys can fold to the
    # same canonical (exact-address entry and prefix-fold entry
    # for the same location); the CSV wants one row per bucket
    # per tick, not two.
    _folded = {}
    for _a, _v in map_data.items():
        _k = _fold_v6(_a)
        if _k in _folded:
            _pv = _folded[_k]
            _pi = int(_pv.get('instances', 0) or 0)
            _ci = int(_v.get('instances', 0) or 0)
            _merged = dict(_v if _ci > _pi else _pv)
            _merged['instances'] = _pi + _ci
            _folded[_k] = _merged
        else:
            _folded[_k] = _v
    map_data = _folded
    for addr, v in map_data.items():
        try:
            inst = int(v.get("instances", 0))
        except Exception:
            continue
        if inst < MIN_INST:
            continue
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
            row["ss_" + CONGS[i]] = int(m.get("swap_score", 0) or 0)
            row["bs_" + CONGS[i]] = int(m.get("bad_streak", 0) or 0)
            row["ns_" + CONGS[i]] = int(m.get("null_streak", 0) or 0)
        row["tcp_rmem_min"] = rm_min
        row["tcp_rmem_def"] = rm_def
        row["tcp_rmem_max"] = rm_max
        buffer_csv(BUCKETS_CSV, row)
        n += 1
    return n


def _read_state():
    if not SWAPS_POS.exists():
        return None
    try:
        d = json.loads(SWAPS_POS.read_text())
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    if d.get("state_version") != STATE_VERSION:
        return None
    if "file_offsets" not in d or "met_cache" not in d:
        return None
    d.setdefault("srate_cache", {})
    d.setdefault("pending_swaps", [])
    return d


def _new_state():
    return {"state_version": STATE_VERSION,
            "file_offsets": {}, "met_cache": {},
            "srate_cache": {}, "pending_swaps": []}


def _write_state(state):
    tmp = str(SWAPS_POS) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, str(SWAPS_POS))


def _lookup_ema(map_data, dest_ip, alg_index):
    if not dest_ip or not map_data or alg_index is None:
        return ""
    key = dest_ip
    parts = dest_ip.split(".")
    if len(parts) == 4:
        key = "%s.%s.0.0" % (parts[0], parts[1])
    v = map_data.get(key)
    if not v:
        v = map_data.get(dest_ip)
    if not v:
        return ""
    try:
        ai = int(alg_index)
    except (TypeError, ValueError):
        return ""
    if ai < 0 or ai >= 16:
        return ""
    metrics = v.get("metrics") or []
    if ai >= len(metrics):
        return ""
    m = metrics[ai]
    if not isinstance(m, dict):
        return ""
    ema = m.get("rate_ema")
    if ema is None:
        return ""
    try:
        return int(ema)
    except (TypeError, ValueError):
        return ""


def _decode_dest6(s):
    try: n6 = int(s) if s not in (None, "") else 0
    except (TypeError, ValueError): n6 = 0
    if not n6:
        return ""
    return "v6:%08x" % (n6 & 0xFFFFFFFF)


def _decode_dest(n):
    if n is None or n == 1:
        return ""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    first = (n >> 24) & 0xFF
    if first == 0 or first == 127:
        return ""
    try:
        return socket.inet_ntoa(struct.pack(">I", n & 0xFFFFFFFF))
    except Exception:
        return ""


def _direction_from_rport(rport):
    if rport in ("", None):
        return ""
    return "origin" if str(rport) == "443" else "client"


def _truth_write(row):
    """Append one line for the tuner: bucket, target alg, sustained cls."""
    dest = row.get("dest") or ""
    to_alg = row.get("to_alg") or ""
    outcome = (row.get("outcome") or "").lower()
    if not dest or not to_alg or outcome not in ("win", "null", "loss"):
        return
    # 0.4.79: accept v6:XXXXXXXX (top 32 bits of remote_ip6[0])
    # as well as dotted-quad v4.  Previously any non-dotted string
    # was silently dropped, so every IPv6 swap was ignored by the
    # score reconciler.
    if dest.startswith("v6:"):
        bucket = dest
    else:
        parts = dest.split(".")
        if len(parts) != 4:
            return
        bucket = "%s.%s.0.0" % (parts[0], parts[1])
    rec = {"bucket": bucket, "tgt": to_alg, "cls": outcome}
    try:
        with open(SWAPS_TRUTH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except OSError:
        pass


def _resolve_pending(pending, met_cache, srate_cache, newest_ts, now_epoch):
    keep = []
    for row in pending:
        cookie = row["cookie"]
        boot_ts = row["boot_ts"]
        pre = row.get("srate_before")
        try:
            pre_v = float(pre) if pre not in ("", None) else None
        except (TypeError, ValueError):
            pre_v = None

        # direction still comes from the met line's rport
        post_rport = None
        for entry in met_cache.get(cookie, ()):
            ts = entry[0]
            if boot_ts + 3.0 <= ts <= boot_ts + 300.0:
                post_rport = entry[2]
                break

        # 0.4.76: sustained-ruler classification.  Median of srate
        # in [T+60, T+300] vs pre-swap srate.  Higher rate = win,
        # lower = loss (opposite of the old metric ruler).
        samples = [rate for (ts, rate) in srate_cache.get(cookie, ())
                   if boot_ts + 60.0 <= ts <= boot_ts + 300.0]
        post_v = None
        # 0.4.76b: match the renderer's rule -- accept a single sample.
        # The renderer (bpftune-render.py _attach_sustained_outcomes)
        # classifies on >=1 sample in the same window; the collector
        # was the strict one at >=2, so CSV and CLI disagreed on
        # every swap the collector marked no_post.  Single-sample
        # agreement with the eventual median is ~80% on 345 testable
        # swaps -- correct 4 out of 5, at 2x the coverage.
        if len(samples) >= 1:
            sv = sorted(samples)
            ns = len(sv)
            post_v = (float(sv[ns // 2]) if ns % 2 else
                      (sv[ns // 2 - 1] + sv[ns // 2]) / 2.0)

        if pre_v and post_v:
            ratio = post_v / pre_v
            row["outcome"] = ("win" if ratio >= 1.1 else
                              "loss" if ratio <= 0.9 else "null")
            if not row.get("direction") and post_rport not in ("", None):
                row["direction"] = _direction_from_rport(str(post_rport))
                row["rport"] = str(post_rport)
            _truth_write(row)   # 0.4.76: sustained truth for the tuner
            buffer_csv(SWAPS_CSV, row, SWAP_FIELDS)
        elif boot_ts + 300.0 < newest_ts:
            row["outcome"] = "no_post"
            buffer_csv(SWAPS_CSV, row, SWAP_FIELDS)
        else:
            keep.append(row)
    return keep


def _parse_swaps_from(text, now_epoch, met_cache, srate_cache, map_data,
                      state):
    lines = text.splitlines()

    chunk_met = {}
    chunk_srate = {}
    for line in lines:
        m = MET_RX.search(line)
        if m:
            c = int(m.group(2))
            chunk_met.setdefault(c, []).append(
                (float(m.group(1)), int(m.group(6)), int(m.group(3))))
            continue
        s = SRATE_RX.search(line)
        if s:
            c   = int(s.group(2))
            alg = int(s.group(3))
            sr  = int(s.group(4))
            chunk_srate.setdefault(c, []).append(
                (float(s.group(1)), alg, sr))

    newest_met_ts = 0.0
    for entries in chunk_met.values():
        for e in entries:
            if e[0] > newest_met_ts:
                newest_met_ts = e[0]
    newest_srate_ts = 0.0
    for entries in chunk_srate.values():
        for e in entries:
            if e[0] > newest_srate_ts:
                newest_srate_ts = e[0]
    newest_ts = max(newest_met_ts, newest_srate_ts)

    pending = state.setdefault("pending_swaps", [])

    n = 0
    for line in lines:
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
        dest_s = m.group(10)
        dest6_s = m.group(11) if m.lastindex and m.lastindex >= 11 else None

        pre = None
        pre_ts = -1.0
        pre_rport = ""
        for entry in met_cache.get(c, ()):
            ts = entry[0]
            if ts < boot_ts + 0.001 and ts > pre_ts:
                pre = entry[1]
                pre_ts = ts
                pre_rport = entry[2]
        for entry in chunk_met.get(c, ()):
            ts = entry[0]
            if ts < boot_ts + 0.001 and ts > pre_ts:
                pre = entry[1]
                pre_ts = ts
                pre_rport = entry[2]

        post = None
        post_rport = ""
        for entry in chunk_met.get(c, ()):
            ts = entry[0]
            if boot_ts + 3.0 <= ts <= boot_ts + 300.0:
                post = entry[1]
                post_rport = entry[2]
                break

        srate_pre = None
        srate_pre_ts = -1.0
        for entry_s in srate_cache.get(c, ()):
            ts = entry_s[0]
            if ts < boot_ts and ts > srate_pre_ts:
                srate_pre = entry_s[1]
                srate_pre_ts = ts
        for (sts, _a, sr) in chunk_srate.get(c, ()):
            if sts < boot_ts and sts > srate_pre_ts:
                srate_pre = sr
                srate_pre_ts = sts

        mt_alg = CONGS[int(mt_i) & 15] if mt_i and mt_i.isdigit() else ""
        rb_alg = CONGS[int(rb_i) & 15] if rb_i and rb_i.isdigit() else ""

        dest_ip = ""
        dest_raw = ""
        if dest_s:
            dest_raw = dest_s
            try:
                dest_ip = _decode_dest(int(dest_s))
            except (TypeError, ValueError):
                dest_ip = ""
        # 0.4.78.2: dest=0 means the socket is IPv6 (remote_ip4
        # unset).  Prefer the dest6 field in that case; without
        # this, every v6 swap reached _truth_write with an empty
        # dest and was silently dropped.
        if not dest_ip and dest6_s:
            dest_ip = _decode_dest6(dest6_s)
            if dest_ip and not dest_raw:
                dest_raw = dest6_s

        f_ema = _lookup_ema(map_data, dest_ip, fa)
        t_ema = _lookup_ema(map_data, dest_ip, ta)

        rp = pre_rport or post_rport or ""
        row = {
            "collected_ts": now_epoch,
            "boot_ts": boot_ts,
            "cookie": c,
            "from_alg": CONGS[fa] if fa < 16 else str(fa),
            "to_alg": CONGS[ta] if ta < 16 else str(ta),
            "d": d,
            "mt_alg": mt_alg,
            "rb_alg": rb_alg,
            "diverges": "1" if (mt_alg and rb_alg and mt_alg != rb_alg) else "0",
            "outcome": "",
            "socket_rate_before": pre if pre is not None else "",
            "dest": dest_ip,
            "dest_raw": dest_raw,
            "f_ema": f_ema,
            "t_ema": t_ema,
            "srate_before": srate_pre if srate_pre is not None else "",
            "direction": _direction_from_rport(str(rp)) if rp else "",
            "rport": str(rp) if rp else "",
        }

        # 0.4.76: entire classification deferred.  The resolve path
        # uses the sustained median srate in [T+60, T+300] vs the
        # pre-swap srate; the metric-ruler classification is gone.
        if srate_pre is not None:
            pending.append(row)
        else:
            row["outcome"] = "no_srate_pre"
            buffer_csv(SWAPS_CSV, row, SWAP_FIELDS)

        n += 1

    for c, entries in chunk_met.items():
        bucket = met_cache.setdefault(c, [])
        if not isinstance(bucket, list):
            bucket = []
            met_cache[c] = bucket
        seen = {e[0] for e in bucket if isinstance(e, list) and len(e) >= 3}
        for entry in entries:
            if entry[0] not in seen:
                bucket.append([entry[0], entry[1], entry[2]])

    for c, entries in chunk_srate.items():
        bucket = srate_cache.setdefault(c, [])
        if not isinstance(bucket, list):
            bucket = []
            srate_cache[c] = bucket
        seen = {e[0] for e in bucket
                if isinstance(e, list) and len(e) >= 2}
        for (sts, alg, sr) in entries:
            if sts not in seen:
                bucket.append([sts, sr])
            buffer_csv(SRATE_CSV, {
                "collected_ts": now_epoch,
                "boot_ts": sts,
                "cookie": c,
                "alg": CONGS[alg] if 0 <= alg < 16 else str(alg),
                "srate": sr,
            }, SRATE_FIELDS)

    # Prune met_cache to MET_CACHE_TTL_S. If we saw nothing this run,
    # skip the prune so a quiet interval does not wipe live entries.
    if newest_met_ts > 0:
        cutoff = newest_met_ts - MET_CACHE_TTL_S
        for c in list(met_cache.keys()):
            bucket = met_cache[c]
            if not isinstance(bucket, list):
                del met_cache[c]
                continue
            bucket[:] = [e for e in bucket
                         if isinstance(e, list) and len(e) >= 3
                         and e[0] >= cutoff]
            if not bucket:
                del met_cache[c]

    state["pending_swaps"] = _resolve_pending(
        pending, met_cache, srate_cache, newest_ts, now_epoch)

    return n


def collect_swaps(map_data):
    state = _read_state()
    if state is None:
        state = _new_state()
    file_offsets = state["file_offsets"]
    met_cache    = state["met_cache"]
    srate_cache  = state["srate_cache"]

    first_run = not file_offsets
    now_epoch = int(time.time())
    n = 0

    for logpath in list_logs():
        key = logpath.name
        try:
            size = logpath.stat().st_size
        except OSError:
            continue

        if first_run or key not in file_offsets:
            file_offsets[key] = size
            continue

        pos = file_offsets.get(key, 0)
        if size < pos:
            pos = 0
        if size == pos:
            continue

        try:
            with open(logpath, "rb") as f:
                f.seek(pos)
                chunk = f.read().decode("utf-8", errors="replace")
                file_offsets[key] = f.tell()
        except OSError:
            continue

        n += _parse_swaps_from(chunk, now_epoch, met_cache,
                               srate_cache, map_data, state)

    # 0.4.76: srate_cache prunes per entry, same as met_cache.
    if srate_cache:
        valid_ts = [e[0] for bucket in srate_cache.values()
                    if isinstance(bucket, list)
                    for e in bucket
                    if isinstance(e, list) and len(e) >= 2]
        if valid_ts:
            cutoff = max(valid_ts) - MET_CACHE_TTL_S
            for c in list(srate_cache.keys()):
                bucket = srate_cache[c]
                if not isinstance(bucket, list):
                    del srate_cache[c]
                    continue
                bucket[:] = [e for e in bucket
                             if isinstance(e, list) and len(e) >= 2
                             and e[0] >= cutoff]
                if not bucket:
                    del srate_cache[c]

    _write_state(state)
    return n


def run_cli_snapshot(map_raw=""):
    """Call collect_all() directly — no subprocess, no Python startup.

    The CLI module is imported once at daemon startup (_cli_mod).
    The BPF map dump is passed via env var (same interface the CLI
    already uses) so read_map() picks it up without calling bpftool.
    """
    if _cli_mod is None:
        print("collector: CLI module not imported, falling back to subprocess", file=sys.stderr)
        return _run_cli_subprocess(map_raw)
    tmp_map = None
    try:
        if map_raw:
            try:
                fd, tmp_map = tempfile.mkstemp(
                    prefix=".map-", suffix=".json", dir=str(HIST))
                with os.fdopen(fd, "w") as f:
                    f.write(map_raw)
                os.environ["BPFTUNE_MAP_DUMP_JSON"] = tmp_map
            except OSError:
                tmp_map = None
        # Use incremental reading (daemon mode): only parse new log
        # lines since last cycle.  First call reads full tail.
        # API-008: pass the last full result so bucket_ips can be merged
        # across snapshots (the log tail rotates, so IPs visible last
        # cycle would otherwise disappear from this cycle's view).
        with _result_lock:
            prev = _last_full_result
        doc = _cli_mod.collect_all(previous_result=prev)
        tmp = str(CURRENT_JSON) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(doc, f, separators=(",", ":"))
        os.replace(tmp, str(CURRENT_JSON))
        return doc
    except Exception as e:
        print("collector: CLI snapshot failed: %s" % e, file=sys.stderr)
        return None
    finally:
        if tmp_map:
            try:
                os.unlink(tmp_map)
            except OSError:
                pass
        # Clean up env var so next cycle doesn't use stale map dump
        os.environ.pop("BPFTUNE_MAP_DUMP_JSON", None)


def _run_cli_subprocess(map_raw=""):
    """Fallback: subprocess call (used if importlib import failed)."""
    env = os.environ.copy()
    tmp_map = None
    try:
        if map_raw:
            try:
                fd, tmp_map = tempfile.mkstemp(
                    prefix=".map-", suffix=".json", dir=str(HIST))
                with os.fdopen(fd, "w") as f:
                    f.write(map_raw)
                env["BPFTUNE_MAP_DUMP_JSON"] = tmp_map
            except OSError:
                tmp_map = None
        r = subprocess.run([sys.executable, str(CLI), "--json"],
                           capture_output=True, text=True, timeout=90,
                           env=env)
        if r.returncode != 0:
            print("collector: CLI exited %d" % r.returncode, file=sys.stderr)
            return None
        doc = json.loads(r.stdout)
        tmp = str(CURRENT_JSON) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(doc, f, separators=(",", ":"))
        os.replace(tmp, str(CURRENT_JSON))
        return doc
    except Exception as e:
        print("collector: CLI subprocess failed: %s" % e, file=sys.stderr)
        return None
    finally:
        if tmp_map:
            try:
                os.unlink(tmp_map)
            except OSError:
                pass


def main():
    try:
        os.nice(19)
    except (OSError, AttributeError):
        pass
    ts_epoch = int(time.time())
    map_data, map_raw = read_map_data()
    nb = collect_buckets(ts_epoch, map_data)
    ns = collect_swaps(map_data)
    flush_csv_buffers()
    _live_flush()
    doc = run_cli_snapshot(map_raw)
    if doc:
        with _result_lock:
            global _last_result, _last_full_result, _last_result_hashes
            _last_result = doc
            _last_full_result = doc
            _last_result_hashes = _compute_key_hashes(doc)
    print("collector: buckets=%d swaps=%d cli=%s ts=%d"
          % (nb, ns, "ok" if doc else "fail", ts_epoch))


def _compute_key_hashes(doc):
    """Phase 3: compute md5 of each top-level key JSON for SSE delta."""
    if not doc:
        return {}
    hashes = {}
    for k, v in doc.items():
        try:
            h = hashlib.md5(json.dumps(v, separators=(",", ":")).encode()).hexdigest()
        except Exception:
            h = ""
        hashes[k] = h
    return hashes


class SSEHandler(BaseHTTPRequestHandler):
    """HTTP handler for SSE push + in-memory current.json."""
    protocol_version = "HTTP/1.1"  # required for SSE streaming
    def do_GET(self):
        # Phase 2 piece 1: serve /api/labels directly
        if self.path.startswith("/api/labels"):
            return self._handle_labels_get()
        # Phase 2 piece 2: serve static files (drop nginx)
        if self.path in ("/", "/index.html"):
            return self._serve_static_file(
                "/var/lib/bpftune/history/index.html",
                content_type="text/html; charset=utf-8")
        if self.path in ("/dashboard.js", "/dashboard.css"):
            return self._serve_static_file(
                "/opt/bpftune-dashboard/bin" + self.path)
        if self.path.startswith("/data/"):
            sub = self.path[len("/data/"):]
            if not sub or sub.endswith(".csv") or "/" in sub or ".." in sub \
               or sub.startswith("."):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers(); return
            return self._serve_static_file(
                "/var/lib/bpftune/history/data/" + sub)
        if self.path == "/sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            # Phase 3: send full payload on connect, then deltas
            with _result_lock:
                data = _last_result
                key_hashes = dict(_last_result_hashes)
            last_pushed_hashes = {}
            if data:
                msg = json.dumps({"__t": "f", "v": data}, separators=(",", ":"))
                last_pushed_hashes = dict(key_hashes)
                try:
                    self.wfile.write(f"data: {msg}\n\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
            while True:
                time.sleep(1)
                with _result_lock:
                    data = _last_result
                    key_hashes = dict(_last_result_hashes)
                if not data:
                    continue
                changed = {}
                removed = []
                for k, h in key_hashes.items():
                    if h != last_pushed_hashes.get(k):
                        changed[k] = data.get(k)
                for k in last_pushed_hashes:
                    if k not in key_hashes:
                        removed.append(k)
                if not changed and not removed:
                    continue
                last_pushed_hashes = dict(key_hashes)
                msg = json.dumps({"__t": "d", "c": changed, "r": removed}, separators=(",", ":"))
                try:
                    self.wfile.write(f"data: {msg}\n\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
        elif self.path == "/current.json":
            # Phase 2 piece 2: gzip the ~330KB current.json -> ~70KB
            with _result_lock:
                data = _last_result
            body = json.dumps(data or {}, separators=(",", ":")).encode()
            if self._client_accepts_gzip():
                compressed = self._gzip_body(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(compressed)))
                self.send_header("Vary", "Accept-Encoding")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try: self.wfile.write(compressed)
                except (BrokenPipeError, ConnectionResetError): pass
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try: self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError): pass
        else:
            self.send_response(404)
            self.end_headers()

    # ---- Phase 2: /api/labels handlers (delegating to bpftune_labels_api) ----
    def _send_json(self, code, data):
        body = json.dumps(data).encode() if not isinstance(data, bytes) else data
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try: self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError): pass

    def _handle_labels_get(self):
        if _labels is None:
            self._send_json(503, {"error": "labels module not loaded"}); return
        try:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            labels = _labels.load_labels()
            if "ip" in qs:
                ip = qs["ip"][0]
                self._send_json(200, {"ip": ip, "label": labels.get(ip, "")})
            else:
                aliases = [r["raw"] for r in _labels.load_aliases_rules()]
                groups = _labels.build_groups_from_aliases()
                self._send_json(200, {"labels": labels, "aliases": aliases, "groups": groups})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def do_POST(self):
        if self.path.startswith("/api/labels") or self.path == "/":
            return self._handle_labels_post()
        self.send_response(404); self.end_headers()

    def _handle_labels_post(self):
        if _labels is None:
            self._send_json(503, {"error": "labels module not loaded"}); return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            req = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "invalid JSON"}); return
        try:
            labels = _labels.load_labels()
            if "ips" in req and isinstance(req["ips"], list):
                label = req.get("label", "").strip()
                ips = [ip.strip() for ip in req["ips"] if ip.strip()]
                if not label:
                    errors = []
                    for ip in ips:
                        try:
                            labels.pop(ip, None)
                            _labels._remove_ip_from_aliases(ip)
                            _labels.bpf_aliases_delete(ip)
                        except Exception as e: errors.append(str(e))
                    _labels.save_labels(labels)
                    self._send_json(200, {"ok": True, "labels": labels,
                                          "groups": _labels.build_groups_from_aliases(),
                                          "errors": errors})
                else:
                    for ip in ips: labels[ip] = label
                    _labels.save_labels(labels)
                    try: fold_results = _labels.auto_fold()
                    except Exception as e: fold_results = {"error": str(e)}
                    self._send_json(200, {"ok": True, "labels": labels,
                                          "groups": _labels.build_groups_from_aliases(),
                                          "fold_results": fold_results})
                return
            ip = req.get("ip", "").strip()
            label = req.get("label", "").strip()
            if not ip:
                self._send_json(400, {"error": "ip is required"}); return
            if not label:
                errors = []
                try:
                    labels.pop(ip, None)
                    _labels._remove_ip_from_aliases(ip)
                    _labels.bpf_aliases_delete(ip)
                except Exception as e: errors.append(str(e))
                _labels.save_labels(labels)
                self._send_json(200, {"ok": True, "ip": ip, "label": label,
                                      "labels": labels,
                                      "groups": _labels.build_groups_from_aliases(),
                                      "errors": errors})
            else:
                labels[ip] = label
                _labels.save_labels(labels)
                try: fold_results = _labels.auto_fold()
                except Exception as e: fold_results = {"error": str(e)}
                self._send_json(200, {"ok": True, "ip": ip, "label": label,
                                      "labels": labels,
                                      "groups": _labels.build_groups_from_aliases(),
                                      "fold_results": fold_results})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def do_DELETE(self):
        if self.path.startswith("/api/labels"):
            return self._handle_labels_delete()
        self.send_response(404); self.end_headers()

    def _handle_labels_delete(self):
        if _labels is None:
            self._send_json(503, {"error": "labels module not loaded"}); return
        try:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            ip = qs.get("ip", [""])[0]
            if not ip:
                self._send_json(400, {"error": "ip is required"}); return
            labels = _labels.load_labels()
            labels.pop(ip, None)
            _labels._remove_ip_from_aliases(ip)
            _labels.bpf_aliases_delete(ip)
            _labels.save_labels(labels)
            fold_results = _labels.auto_fold()
            self._send_json(200, {"ok": True, "ip": ip,
                                  "labels": _labels.load_labels(),
                                  "groups": _labels.build_groups_from_aliases(),
                                  "fold_results": fold_results})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def do_HEAD(self):
        # Phase 2 piece 2: support HEAD requests (curl -I uses HEAD).
        # Return same headers as GET but no body. For static files this
        # is a stat-only check; for /current.json it returns the size.
        # Simplest implementation: call do_GET then truncate the body
        # before it's written. But since do_GET writes directly to
        # self.wfile, we can't easily intercept. Instead, just route
        # HEAD through the same logic — the client closes the connection
        # after headers anyway.
        # The cleanest fix: handle HEAD by sending the headers do_GET
        # would send, but with Content-Length: 0 and no body.
        if self.path.startswith("/api/labels") or self.path == "/sse" or self.path == "/current.json":
            # For dynamic endpoints, return minimal headers
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        # For static files, do a stat and return Content-Length
        import os as _os
        if self.path in ("/", "/index.html"):
            path = "/var/lib/bpftune/history/index.html"
        elif self.path in ("/dashboard.js", "/dashboard.css"):
            path = "/opt/bpftune-dashboard/bin" + self.path
        elif self.path.startswith("/data/"):
            sub = self.path[len("/data/"):]
            if not sub or sub.endswith(".csv") or "/" in sub or ".." in sub or sub.startswith("."):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers(); return
            path = "/var/lib/bpftune/history/data/" + sub
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers(); return
        try:
            size = _os.path.getsize(path)
            self.send_response(200)
            # guess MIME
            for ext, ct in (("html", "text/html; charset=utf-8"),
                            ("js", "application/javascript; charset=utf-8"),
                            ("css", "text/css; charset=utf-8"),
                            ("json", "application/json; charset=utf-8")):
                if path.endswith("." + ext):
                    self.send_header("Content-Type", ct); break
            else:
                self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
        except OSError:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

    def do_OPTIONS(self):
        self._send_json(200, {"ok": True})

    # ---- Phase 2 piece 2: static file serving (drop nginx) ----
    _MIME_MAP = {
        ".html": "text/html; charset=utf-8",
        ".js":   "application/javascript; charset=utf-8",
        ".css":  "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".png":  "image/png",
        ".svg":  "image/svg+xml",
        ".ico":  "image/x-icon",
    }

    def _guess_mime(self, path):
        for ext, ct in self._MIME_MAP.items():
            if path.endswith(ext):
                return ct
        return "application/octet-stream"

    def _client_accepts_gzip(self):
        ae = self.headers.get("Accept-Encoding", "") or ""
        return "gzip" in ae.lower()

    def _gzip_body(self, body):
        import gzip as _gzip
        buf = io.BytesIO()
        with _gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=5) as gz:
            gz.write(body)
        return buf.getvalue()

    def _serve_static_file(self, path, content_type=None):
        try:
            real = os.path.realpath(path)
            allowed_bases = (
                "/var/lib/bpftune/history",
                "/opt/bpftune-dashboard/bin",
            )
            if not any(real.startswith(b) for b in allowed_bases):
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers(); return
            if not os.path.isfile(real):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers(); return
            with open(real, "rb") as f:
                body = f.read()
        except (OSError, IOError):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers(); return
        ct = content_type or self._guess_mime(real)
        if self._client_accepts_gzip() and any(real.endswith(ext) for ext in
                                               (".json", ".js", ".css", ".html")):
            compressed = self._gzip_body(body)
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(compressed)))
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try: self.wfile.write(compressed)
            except (BrokenPipeError, ConnectionResetError): pass
        else:
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try: self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError): pass

    def log_message(self, fmt, *args):
        pass


def _start_sse_server(port=None, bind=None):
    """Start the SSE HTTP server in a background thread.
    Phase 2 piece 2: port + bind default to _SSE_PORT/_SSE_BIND."""
    if port is None: port = _SSE_PORT
    if bind is None: bind = _SSE_BIND
    try:
        server = ThreadingHTTPServer((bind, port), SSEHandler)
        print("collector: HTTP server on %s:%d (serves /, /sse, /current.json, /api/labels, /data/*)" % (bind, port), file=sys.stderr)
        server.serve_forever()
    except Exception as e:
        print("collector: SSE server failed: %s" % e, file=sys.stderr)


def _lightweight_loop(_running):
    """30s lightweight collection: BPF map + incremental log parsing.

    Reads BPF map (~50ms) + new log lines via tail_incremental (10-50
    lines, not 2MB).  Parses new swaps/proofs, merges with last full
    collection result.  Uses a SEPARATE offset dict so the 30s and 5min
    loops don't conflict.

    Updates every 30s: NOW panel, buckets, recent_swaps, recent_proofs,
    swap_outcomes, churn, log_window, proofs_raw, rate_raw.
    Stays from 5min: proof leaderboard, rate progression, divergence,
    metric, bucket_live, live_leaders, tunables.
    """
    global _last_result, _last_full_result
    _lw_offsets = {}  # SEPARATE offsets for 30s loop (don't share with 5min)
    print("collector: lightweight loop started (30s interval, incremental log)", file=sys.stderr)
    while _running[0]:
        try:
            map_data, map_raw = read_map_data()
            if _cli_mod:
                # Get the last full result as base for merging
                with _result_lock:
                    base = _last_full_result
                # Collect lightweight: BPF map + incremental log parsing
                doc = _cli_mod.collect_lightweight(
                    _lw_offsets, map_raw, base_result=base)
                with _result_lock:
                    global _last_result_hashes
                    _last_result = doc
                    _last_result_hashes = _compute_key_hashes(doc)
        except Exception as e:
            print("collector: lightweight error: %s" % e, file=sys.stderr)
        # Sleep 30s in 1s increments for responsive shutdown
        for _ in range(30):
            if not _running[0]:
                break
            time.sleep(1)
    print("collector: lightweight loop stopped", file=sys.stderr)


def _parse_args(argv):
    port = 8082
    bind = "127.0.0.1"
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1]); i += 2
        elif a == "--bind" and i + 1 < len(argv):
            bind = argv[i + 1]; i += 2
        else:
            i += 1
    return port, bind


# Phase 2 piece 2: port + bind set from CLI args. Defaults:
# 127.0.0.1:8082 (behind nginx). Standalone: --port 8080 --bind 0.0.0.0.
_SSE_PORT, _SSE_BIND = _parse_args(sys.argv[1:])


def _daemon_loop():
    """Run main() every 60 seconds with signal handling."""
    import signal
    _running = [True]
    def _stop(sig, frame):
        _running[0] = False
        print("collector: received signal %d, shutting down" % sig,
              file=sys.stderr)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print("collector: daemon mode started (60s interval)", file=sys.stderr)
    # Start SSE server in background thread (port 8082, localhost only)
    sse_thread = threading.Thread(target=_start_sse_server, daemon=True)
    sse_thread.start()
    print("collector: SSE server started on port 8082", file=sys.stderr)
    # Start lightweight 30s collection loop (BPF map only, no log parsing)
    light_thread = threading.Thread(target=_lightweight_loop, args=(_running,), daemon=True)
    light_thread.start()
    while _running[0]:
        try:
            main()
        except Exception as e:
            print("collector: error in cycle: %s" % e, file=sys.stderr)
        # Sleep in 1s increments so SIGTERM is responsive
        for _ in range(300):
            if not _running[0]:
                break
            time.sleep(1)
    print("collector: daemon stopped", file=sys.stderr)


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        _daemon_loop()
    else:
        main()
