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
import csv, json, os, re, socket, struct, subprocess, sys, tempfile, time
from pathlib import Path

HIST = Path("/var/lib/bpftune/history")
HIST.mkdir(parents=True, exist_ok=True)
BUCKETS_CSV  = HIST / "buckets.v2.csv"
SWAPS_CSV    = HIST / "swaps.csv"
SRATE_CSV    = HIST / "srate.csv"
SWAPS_POS    = HIST / ".swaps_pos.json"
CURRENT_JSON = HIST / "current.json"

STATE_VERSION = 3

SELF_DIR = Path(__file__).resolve().parent
CLI      = SELF_DIR / "bpftune-cli.py"

CONGS = ["cubic", "bbr", "htcp", "dctcp", "scalable", "vegas", "veno",
         "westwood", "reno", "illinois", "yeah", "lp", "bic", "highspeed",
         "hybla", "nv"]
MIN_INST = 2

MET_CACHE_TTL_S = 600.0

SWAP_FIELDS = [
    "collected_ts", "boot_ts", "cookie", "from_alg", "to_alg", "d",
    "mt_alg", "rb_alg", "diverges", "outcome", "socket_rate_before",
    "dest", "dest_raw", "f_ema", "t_ema", "srate_before",
    "direction", "rport",
]
SRATE_FIELDS = ["collected_ts", "boot_ts", "cookie", "alg", "srate"]

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
    r"(?: dest=(\d+))?")
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
    _CSV_BUFFERS.clear()


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
        addr = ".".join(str(x) for x in b[12:16])
        result[addr] = v
    return result, out


def collect_buckets(ts_epoch, map_data):
    if not map_data:
        return 0
    rm_min, rm_def, rm_max = tcp_rmem()
    n = 0
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
    if not CLI.exists():
        return None
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
        print("collector: CLI snapshot failed: %s" % e, file=sys.stderr)
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
    print("collector: buckets=%d swaps=%d cli=%s ts=%d"
          % (nb, ns, "ok" if doc else "fail", ts_epoch))


if __name__ == "__main__":
    main()
