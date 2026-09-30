#!/usr/bin/env python3
"""bpftune_log.py -- BPF log parsing, label resolution, map reader.

Base module for the bpftune dashboard data pipeline.
Imported by bpftune_data.py and bpftune-cli.py.
"""
import argparse
import ipaddress, csv, io, json, os, re, socket, struct, subprocess, sys, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TypedDict, Union


def _mtime_cache(path, cache_holder, loader):
    """mtime-based file cache: returns loader(open(path).read()) cached
    until the file's mtime changes.  Returns {} if the file is missing
    or the loader raises.

    Consolidates the _load_labels / _load_fold / _load_aliases_labels
    cache pattern (each had ~20 lines of duplicated mtime logic).
    """
    try:
        m = os.path.getmtime(path)
    except OSError:
        cache_holder['val'] = {}
        cache_holder['mtime'] = None
        return cache_holder['val']
    if cache_holder['val'] is not None and cache_holder['mtime'] == m:
        return cache_holder['val']
    try:
        with open(path) as f:
            d = loader(f)
        cache_holder['val'] = d if isinstance(d, dict) else {}
    except Exception:
        cache_holder['val'] = {}
    cache_holder['mtime'] = m
    return cache_holder['val']



_LABELS_HOLDER = {'val': None, 'mtime': None}


def _load_labels():
    """0.4.79: /var/lib/bpftune/aliases.labels.json {canonical_ip: label}."""
    import json as _json
    return _mtime_cache(LABELS_JSON_PATH,
                       _LABELS_HOLDER,
                       lambda f: _json.load(f))




def _canon_bucket(addr):
    """Collapse to /16 (v4) or /32 (v6)."""
    if not addr:
        return addr
    if addr.startswith('v6:'):
        return addr
    p = addr.split('.')
    if len(p) == 4:
        return '%s.%s.0.0' % (p[0], p[1])
    return addr



_FOLD_HOLDER = {'val': None, 'mtime': None}


def _load_fold():
    """0.4.79: {"v6:XXXXXXXX": "canonical"} from /32-form entries in
    /etc/bpftune/aliases.  Line form:
        2603:c020:0:0:0:0:0:0 = 89.168.0.0 [label]
    Only lines where groups 2..7 are all zero are treated as /32 folds."""
    def _loader(f):
        out = {}
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
        return out
    return _mtime_cache(ALIASES_FILE_PATH, _FOLD_HOLDER, _loader)



def _fold_v6(addr):
    """0.4.79: replace a v6 /32 bucket key with its canonical v4 bucket
    if that /32 is declared in /etc/bpftune/aliases.
    0.4.85: if no fold rule matches, convert v6:hex to standard IPv6
    /32 so _label_for can normalize and find the label."""
    if not addr or not addr.startswith("v6:"):
        return addr
    folded = _load_fold().get(addr)
    if folded:
        return folded
    # No fold rule — convert v6:hex to standard IPv6 /32
    try:
        n = int(addr[3:], 16)
        return "%x:%x::" % ((n >> 16) & 0xFFFF, n & 0xFFFF)
    except (ValueError, TypeError):
        return addr



_ALIASES_LABEL_HOLDER = {'val': None, 'mtime': None}


def _load_aliases_labels():
    """Load {to_ip: label} from /etc/bpftune/aliases, with mtime cache.
    Fallback for _label_for() when labels.json hasn't been enriched yet."""
    def _loader(f):
        d = {}
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            lhs, rhs = s.split("=", 1)
            rest = rhs.strip().split()
            if not rest:
                continue
            to_ip = rest[0]
            label = rest[1] if len(rest) > 1 else ""
            if label:
                try:
                    d[str(ipaddress.ip_address(to_ip))] = label
                except (ValueError, TypeError):
                    d[to_ip] = label
        return d
    return _mtime_cache(ALIASES_FILE_PATH, _ALIASES_LABEL_HOLDER, _loader)



def _normalize_ip(ip_str):
    try:
        return str(ipaddress.ip_address(ip_str))
    except (ValueError, TypeError):
        return ip_str



def _label_for(addr):
    """Resolve an address to a human-readable label.

    Chain: fold v6 /32 -> v4 if declared in aliases, collapse to
    /16 (v4) or /32 (v6), normalize, then look up in labels.json
    (normalized), falling back to aliases labels.  Returns the
    normalized addr if no label found."""
    if not addr:
        return addr
    addr = _fold_v6(addr)
    addr = _canon_bucket(addr)
    addr = _normalize_ip(addr)
    labels = _load_labels()
    # Direct hit on the normalized addr (fast path).
    if addr in labels:
        return labels[addr]
    # Slow path: some labels.json keys are not normalized.
    # Normalize each key once and check.  (Older code did this every
    # call; now it's a fallback after the fast path.)
    for k, v in labels.items():
        if _normalize_ip(k) == addr:
            return v
    alias_labels = _load_aliases_labels()
    if addr in alias_labels:
        return alias_labels[addr]
    return addr


# ---- Configuration constants ----
# All tunable values and file paths live here.  Edit this block to adjust
# without hunting through the file.  Constants keep the same names they
# had before consolidation (so all references still work).

# Congestion control algorithm names (index = alg number from BPF)

CONGS = ["cubic","bbr","htcp","dctcp","scalable","vegas","veno","westwood",
         "reno","illinois","yeah","lp","bic","highspeed","hybla","nv"]

# BPF log tail size (bytes) — how much of the log to parse each collect cycle

LOG_TAIL_BYTES = 2_000_000

# Bytes/sec to Mb/s conversion (1 MB = 1,000,000 bytes = 8 Mbits)

BPS_TO_MBPS = 1_000_000.0 / 8.0

# Sustained-outcome measurement window (seconds after swap)

SUSTAINED_LO_S = 60.0    # start of the sustained measurement window

SUSTAINED_HI_S = 300.0   # end of the sustained measurement window

# Leaderboard trust floor — minimum votes before an alg is considered
# (must match tcp_conn_tuner.h MIN_LEADER_TRUST)

MIN_LEADER_TRUST = 10

# Live leaders panel sizing

LIVE_TOP_N       = 6     # top-N algs to show per bucket

LIVE_MAX_BUCKETS = 8     # max buckets to include in live_leaders

# Bucket history CSV + live chart timing

BUCKET_HISTORY_CSV = "/var/lib/bpftune/history/buckets.v2.csv"

LIVE_CHART_MIN      = 65   # 65 min x 60s = 65 pts per series; covers 1h with margin

LIVE_CHART_WIDTH_S  = 60   # bin width for live chart aggregation

# Config file paths (consumed by _load_labels / _load_fold / _load_aliases_labels)

LABELS_JSON_PATH  = "/var/lib/bpftune/aliases.labels.json"

ALIASES_FILE_PATH = "/etc/bpftune/aliases"

# Known tunables to always show (even if not yet seen in journal)

KNOWN_TUNABLES = [
    "net.core.netdev_budget",
    "net.core.netdev_budget_usecs",
    "net.core.rmem_default",
    "net.ipv4.tcp_rmem",
    "net.ipv4.tcp_wmem",
]

# Text renderer box-drawing characters

RULE   = "\u2500"

DRULE  = "\u2550"

ARROW  = "\u25b8"

VBAR   = "\u2502"

MIDDOT = "\u00b7"


# ---- TypedDict schema: the contract between bpftune-cli.py and index.html ----
# These document the JSON schema that collect_all() returns.  The frontend
# reads current.json (written by the collector) and expects these fields.
# Keep in sync with the actual return values of the data_* functions.


class LogWindow(TypedDict, total=False):
    """Time range of the BPF log tail (wall-clock timestamps)."""
    oldest_ts: int
    newest_ts: int
    span_min: float
    swap_count: int
    age_min: float


class BuildInfo(TypedDict, total=False):
    """bpftune package + service status."""
    version: str
    dash_version: str
    service: str
    uptime_min: Optional[int]
    started_utc: str
    log_path: Optional[str]


class SystemInfo(TypedDict, total=False):
    """Host system facts."""
    kernel: str
    default_cc: str
    cpu_count: int
    load_1: float
    load_5: float
    load_15: float
    procs_running: int
    procs_total: int
    host_uptime_s: int
    mem_total_bytes: int
    mem_avail_bytes: int
    mem_used_bytes: int
    mem_used_pct: float


class TunableItem(TypedDict):
    key: str
    value: str


class TunableGroup(TypedDict):
    group: str
    items: List[TunableItem]


class BucketRow(TypedDict, total=False):
    """One destination bucket (top 8 by instances)."""
    dest: str
    inst: int
    rtt_us: int
    ref_mbps: float
    best_alg: str
    n_alg: int


class MetricRow(TypedDict, total=False):
    """One algorithm row in the picker leaderboard."""
    alg: str
    metric: float
    votes: int
    alive: int
    rate_ema: int
    swap_score: int
    penalty: float
    score: float
    bad_streak: int
    null_streak: int
    active: bool


class LiveLeaderEntry(TypedDict, total=False):
    alg: str
    weighted: int
    rate_ema: int
    swap_score: int
    bad: int
    null: int
    count: int


class LiveLeader(TypedDict, total=False):
    dest: str
    inst: int
    top: List[LiveLeaderEntry]


class ProofRow(TypedDict, total=False):
    alg: str
    good: int
    proved: int
    proven_max: Optional[float]
    sampled_avg: Optional[float]
    sampled_max: Optional[float]
    samples: Optional[int]


class RateRow(TypedDict, total=False):
    thr: int
    n: int
    mean: float
    min: float
    max: float


class OutcomeSummary(TypedDict, total=False):
    """One outcome scale (composite / srate / sustained)."""
    measurable: int
    unmeasurable: int
    win: int
    win_pct: float
    null: int
    null_pct: float
    loss: int
    loss_pct: float
    rescued: int
    full_loss: int
    open: int
    rescued_pct: float
    full_loss_pct: float
    open_pct: float


class SwapListItem(TypedDict, total=False):
    dest: Optional[str]
    outcome: Optional[str]
    outcome_sustained: Optional[str]
    cookie: int
    ts: float


class SwapOutcomes(TypedDict, total=False):
    composite: OutcomeSummary
    srate: OutcomeSummary
    sustained: OutcomeSummary
    swaps_list: List[SwapListItem]


class DivergenceRow(TypedDict, total=False):
    category: str
    measured: int
    win_pct: float
    null_pct: float
    loss_pct: float
    win: int
    null: int
    loss: int
    skipped: int
    measured_srate: int
    win_pct_srate: float
    null_pct_srate: float
    loss_pct_srate: float
    win_srate: int
    null_srate: int
    loss_srate: int
    skipped_srate: int
    measured_sustained: int
    win_pct_sustained: float
    null_pct_sustained: float
    loss_pct_sustained: float
    win_sustained: int
    null_sustained: int
    loss_sustained: int
    skipped_sustained: int


class ChurnInfo(TypedDict):
    cookies: int
    one: int
    mid: int
    many: int
    max: int


class RecentSwap(TypedDict, total=False):
    boot_ts: float
    from_alg: str
    to_alg: str
    d: int
    outcome: Optional[str]
    outcome_srate: Optional[str]
    outcome_sustained: Optional[str]
    mt_alg: Optional[str]
    rb_alg: Optional[str]
    dest: str
    _bucket: str


class RecentProof(TypedDict, total=False):
    boot_ts: float
    alg: str
    mbps: float
    tier: str
    dest: str


class ProofRaw(TypedDict, total=False):
    alg: str
    dest: Optional[str]
    rate: float
    tier: str


class RateRaw(TypedDict, total=False):
    dest: Optional[str]
    thr: int
    srate: int


class CollectAllResult(TypedDict, total=False):
    """Full dashboard state — the JSON contract with index.html.

    This is what collect_all() returns and what the collector writes
    to /var/lib/bpftune/history/current.json.  The frontend fetches
    it every 30s via liveRefresh().
    """
    generated_ts: int
    log_window: LogWindow
    bucket_ips: Dict[str, List[str]]
    now_mono: float
    hostname: str
    build: BuildInfo
    system: SystemInfo
    tunables: List[TunableGroup]
    buckets: List[BucketRow]
    metric: List[MetricRow]
    metric_by_bucket: Dict[str, List[MetricRow]]
    bucket_live: Dict[str, Any]
    live_leaders: List[LiveLeader]
    proof: List[ProofRow]
    rate: List[RateRow]
    swap_outcomes: SwapOutcomes
    divergence: List[DivergenceRow]
    churn: ChurnInfo
    recent_swaps: List[RecentSwap]
    recent_swaps_by_bucket: Dict[str, List[RecentSwap]]
    recent_proofs: List[RecentProof]
    proofs_raw: List[ProofRaw]
    rate_raw: List[RateRaw]



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



def median(xs):
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    if n % 2:
        return float(s[n // 2])
    return (s[n // 2 - 1] + s[n // 2]) / 2.0



def find_log():
    files = list(Path("/var/log").glob("bpftune-met-*.log"))
    return max(files, key=lambda p: p.stat().st_mtime) if files else None



def tail_recent(budget=LOG_TAIL_BYTES):
    paths = sorted(Path("/var/log").glob("bpftune-met-*.log"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not paths:
        return ""
    chunks = []
    remaining = budget
    for p in paths:
        if remaining <= 0:
            break
        try:
            size = p.stat().st_size
        except OSError:
            continue
        take = min(size, remaining)
        try:
            with open(p, "rb") as f:
                f.seek(size - take)
                chunks.append(f.read().decode("utf-8", errors="replace"))
        except OSError:
            continue
        remaining -= take
    return "".join(reversed(chunks))


# Incremental log reading — daemon mode tracks offsets across cycles.
# Only reads NEW bytes since last call.  Returns (new_text, new_offsets).
# If the file shrank (rotation), reads from start.
_incremental_offsets = {}

def tail_incremental(offsets=None):
    """Read only new log lines since last call.

    Args:
        offsets: dict {filepath_str: byte_offset}.  If None, uses
            module-level _incremental_offsets (persistent in daemon mode).

    Returns (new_text, updated_offsets).  On first call (empty offsets),
    reads the full tail (same as tail_recent) so the first cycle has data.
    """
    if offsets is None:
        offsets = _incremental_offsets
    paths = sorted(Path("/var/log").glob("bpftune-met-*.log"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not paths:
        return "", offsets
    chunks = []
    new_offsets = dict(offsets)  # copy
    for p in paths:
        try:
            size = p.stat().st_size
        except OSError:
            continue
        key = str(p)
        prev_offset = offsets.get(key, -1)
        # If file shrank (rotation) or first read, read last 2MB
        if prev_offset < 0 or size < prev_offset:
            take = min(size, LOG_TAIL_BYTES)
            start = size - take
        else:
            # Read only new bytes since last read
            start = prev_offset
            if start >= size:
                continue  # no new data
        try:
            with open(p, "rb") as f:
                f.seek(start)
                data = f.read()
            chunks.append(data.decode("utf-8", errors="replace"))
            new_offsets[key] = size
        except OSError:
            continue
    # Update module-level cache
    _incremental_offsets.clear()
    _incremental_offsets.update(new_offsets)
    return "".join(reversed(chunks)), new_offsets


def _parse_plain_map(out):
    """Parse the plain-text (non-JSON) bpftool map dump format.

    Older bpftool versions (or bpftool built without BTF support)
    emit a table like:

        [{
            key:
            00 00 00 00 00 00 00 00 00 00 ff ff 0a 00 00 01
            value:
            instances 5  min_rtt 1000  max_rate_delivered 50000  ...
        }]

    Returns a list of (instances:int, value:dict) tuples, or [] if
    no entries parsed.  The key is decoded from the 16-byte hex block
    the same way read_map() decodes u6_addr8 in JSON format."""
    entries = []
    # Split on record boundaries.  Each record starts at '{' on its own line
    # (possibly '{{' for the outer array wrapper) and ends at '}'.
    # We scan line-by-line and track depth.
    depth = 0
    in_key = False
    in_value = False
    key_bytes = []
    value_text = []
    for line in out.splitlines():
        s = line.strip()
        if not s:
            continue
        # Track brace depth to find record boundaries.
        # Opening: lines containing '{' (possibly with '[' prefix like '[{').
        # Closing: lines containing '}' (possibly with ']' suffix like '}]').
        if '{' in s and depth == 0:
            depth = 1
            in_key = in_value = False
            key_bytes = []
            value_text = []
            continue
        if '}' in s and depth > 0:
            # End of a record — emit if we have a value dict
            if value_text:
                v = {}
                # Parse "field value  field value ..." pairs from value_text
                toks = ' '.join(value_text).split()
                i = 0
                while i + 1 < len(toks):
                    name = toks[i]
                    val = toks[i + 1]
                    try:
                        # numeric fields stored as int
                        v[name] = int(val)
                    except (ValueError, TypeError):
                        v[name] = val
                    i += 2
                if v:
                    try:
                        inst = int(v.get('instances', 0) or 0)
                    except (ValueError, TypeError):
                        inst = 0
                    # Decode the key bytes (16 bytes = IPv4-mapped IPv6)
                    if len(key_bytes) == 16:
                        b = key_bytes
                        if b[10] == 0xff and b[11] == 0xff:
                            addr = '.'.join(str(x) for x in b[12:16])
                        else:
                            v6 = (b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3]
                            addr = 'v6:%08x' % v6 if v6 else '0.0.0.0'
                    else:
                        addr = '?'
                    entries.append((inst, addr, v))
            depth = 0
            in_key = in_value = False
            key_bytes = []
            value_text = []
            # Handle '},{' on same line: opening of next record
            if '{' in s:
                depth = 1
                in_key = in_value = False
                key_bytes = []
                value_text = []
            continue
        if s.startswith('key:'):
            in_key = True
            in_value = False
            continue
        if s.startswith('value:'):
            in_value = True
            in_key = False
            continue
        if in_key:
            # hex bytes: "00 00 00 00 00 00 00 00 00 00 ff ff 0a 00 00 01"
            for tok in s.split():
                try:
                    key_bytes.append(int(tok, 16))
                except (ValueError, TypeError):
                    pass
        elif in_value:
            value_text.append(s)
    return entries



def read_map():
    cached = os.environ.get("BPFTUNE_MAP_DUMP_JSON")
    if cached and os.path.exists(cached):
        try:
            with open(cached) as f:
                out = f.read()
        except OSError:
            out = ""
    else:
        out = sh("bpftool --json map dump name remote_host_map 2>/dev/null")
    # Try JSON first (modern bpftool with BTF).
    data = None
    try:
        data = json.loads(out)
    except Exception:
        pass  # falls through to plain-text parser
    if isinstance(data, list):
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
                if b[10] == 0xff and b[11] == 0xff:
                    addr = ".".join(str(x) for x in b[12:16])
                else:
                    v6 = (b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3]
                    addr = "v6:%08x" % v6 if v6 else "0.0.0.0"
            entries.append((inst, _label_for(addr), v))
    else:
        # JSON failed (empty output or parse error).  Fall back to
        # plain-text bpftool format (older bpftool, no BTF).
        if not out and not cached:
            out = sh("bpftool map dump name remote_host_map 2>/dev/null")
        plain_entries = _parse_plain_map(out)
        entries = [(inst, _label_for(addr), v) for inst, addr, v in plain_entries]
        if not entries:
            import sys
            print("[read_map] no entries (json failed, plain parse empty)", file=sys.stderr)
            return None
    # 0.4.79: two map keys can decode to the same final label
    # (an exact-alias entry and a prefix-fold entry for the same
    # physical location).  Merge by final addr -- sum instances,
    # keep the entry with more instances for the other fields.
    _merged = {}
    for _inst, _addr, _v in entries:
        if _addr in _merged:
            _p, _pv = _merged[_addr]
            _merged[_addr] = (_p + _inst, _pv)
            if _inst > _p:
                _merged[_addr] = (_p + _inst, _v)
        else:
            _merged[_addr] = (_inst, _v)
    entries = [(i, a, v) for a, (i, v) in _merged.items()]
    if not entries:
        return None
    entries.sort(key=lambda x: -x[0])
    return entries



def _proof_events(text):
    """Parse proof + midsamp events from the BPF log tail.

    No cache: called once per collect_all() cycle (by data_proof).
    _swaps_mets_srates caches because it's called 7+ times per cycle
    (by data_swap_outcomes, data_divergence, data_churn, data_recent_swaps,
    data_recent_swaps_by_bucket, _run_writeback_and_get_swaps, _log_window).
    """
    MET_WINDOW_S = 60.0
    lines = text.splitlines()

    events = defaultdict(lambda: {"good": 0, "proved": 0, "proven_max": 0})
    samples = defaultdict(lambda: {"sum": 0, "n": 0, "samp_max": 0})
    met_by_cookie = defaultdict(list)

    for line in lines:
        if "proof cookie=" in line:
            m = re.search(r"alg=(\d+) rate=(\d+) tier=(\d+)", line)
            if m:
                a = int(m.group(1))
                rate = int(m.group(2))
                tier = m.group(3)
                if tier == "2":
                    events[a]["proved"] += 1
                else:
                    events[a]["good"] += 1
                if rate > events[a]["proven_max"]:
                    events[a]["proven_max"] = rate
            continue
        mm = re.search(
            r"(\d+\.\d+): .*met cookie=(\d+) rport=\d+ alg=(\d+) ",
            line)
        if mm:
            ts = float(mm.group(1))
            c  = int(mm.group(2))
            a  = int(mm.group(3))
            met_by_cookie[c].append((ts, a))

    for line in lines:
        ms = re.search(
            r"(\d+\.\d+): .*midsamp cookie=(\d+) .* srate=(\d+)",
            line)
        if not ms:
            continue
        ts = float(ms.group(1))
        c  = int(ms.group(2))
        r  = int(ms.group(3))
        if r <= 0:
            continue
        cand = met_by_cookie.get(c)
        if not cand:
            continue
        best_a = None
        best_d = None
        for (mts, a) in cand:
            d = abs(mts - ts)
            if best_d is None or d < best_d:
                best_d = d
                best_a = a
        if best_a is None or best_d is None or best_d > MET_WINDOW_S:
            continue
        samples[best_a]["sum"] += r
        samples[best_a]["n"] += 1
        if r > samples[best_a]["samp_max"]:
            samples[best_a]["samp_max"] = r

    return events, samples



# CACHING STRATEGY:
#   File-based caches (_load_labels, _load_fold, _load_aliases_labels)
#     -> use _mtime_cache(path, holder, loader) — consolidated pattern.
#   Text-based cache (_swaps_mets_srates):
#     -> uses hash(text) as key — content-based, stable within a process.
#     -> called 7+ times per collect_all() cycle with the same text.
#   _proof_events: no cache — called once per cycle (see its docstring).

_SWMS_CACHE = {}


def _swaps_mets_srates(text):
    _key = hash(text)
    if _key in _SWMS_CACHE:
        return _SWMS_CACHE[_key]
    sw = []
    met = defaultdict(list)
    srate = defaultdict(list)
    # Named capture groups make the parsing self-documenting and
    # robust to format changes (m.group("cookie") vs m.group(2)).
    rx_sw = re.compile(r"(?P<ts>\d+\.\d+): bpf_trace_printk: "
                       r"swap cookie=(?P<cookie>\d+) from=(?P<from>\d+) to=(?P<to>\d+) "
                       r"bc=(?P<bc>\d+) ac=(?P<ac>\d+) d=(?P<d>\d+)"
                       r"(?: mt=(?P<mt>\d+) rb=(?P<rb>\d+))?"
                       r"(?: dest=(?P<dest>\d+))?"
                       r"(?: dest6=(?P<dest6>\d+))?"
                   r"(?: dest6b=(?P<dest6b>\d+))?")
    rx_mt = re.compile(r"(?P<ts>\d+\.\d+): bpf_trace_printk: "
                       r"met cookie=(?P<cookie>\d+) rport=(?P<rport>\d+) alg=(?P<alg>\d+) segs=(?P<segs>\d+) val=(?P<val>\d+)")
    rx_sr = re.compile(r"(?P<ts>\d+\.\d+): bpf_trace_printk: "
                       r"srate cookie=(?P<cookie>\d+) alg=(?P<alg>\d+) srate=(?P<srate>\d+)")
    for line in text.splitlines():
        m = rx_sw.search(line)
        if m:
            sw.append((float(m.group("ts")), int(m.group("cookie")),
                       int(m.group("from")), int(m.group("to")),
                       int(m.group("bc")), int(m.group("ac")), m.group("d"),
                       m.group("mt"), m.group("rb"), m.group("dest"), m.group("dest6"), m.group("dest6b"), line))
            continue
        v = rx_mt.search(line)
        if v:
            met[int(v.group("cookie"))].append((float(v.group("ts")), int(v.group("val"))))
            continue
        s = rx_sr.search(line)
        if s:
            srate[int(s.group("cookie"))].append((float(s.group("ts")), int(s.group("srate"))))
    _result = (sw, met, srate)
    _SWMS_CACHE.clear()
    _SWMS_CACHE[_key] = _result
    return _result



T_RESCUE_WINDOW_S = 3600



def _dest_str(v4, v6):
    """Display string for a destination.  Prefers v6 when present."""
    try: n6 = int(v6) if v6 not in (None, "") else 0
    except (TypeError, ValueError): n6 = 0
    if n6:
        return "v6:%08x" % (n6 & 0xFFFFFFFF)
    return _dest_ip(v4)



def _bucket_of(v4, v6):
    try: n6 = int(v6) if v6 not in (None, "") else 0
    except (TypeError, ValueError): n6 = 0
    if n6:
        return "v6:%08x" % (n6 & 0xFFFFFFFF)
    if not v4:
        return ""
    try: n = int(v4)
    except (TypeError, ValueError):
        return ""
    first = (n >> 24) & 0xFF
    if first in (0, 127):
        return ""
    return "%d.%d.0.0" % ((n >> 24) & 0xFF, (n >> 16) & 0xFF)



def _dest_ip(s):
    if s is None or s in ("", "1"):
        return ""
    try:
        n = int(s)
    except (TypeError, ValueError):
        return ""
    first = (n >> 24) & 0xFF
    if first == 0 or first == 127:
        return ""
    try:
        return socket.inet_ntoa(struct.pack(">I", n & 0xFFFFFFFF))
    except Exception:
        return ""



def _extract_dest(row):
    """Extract remote_host from a parsed swap row tuple.

    Prefers IPv6 when present (dest6 + dest6b), padding to a /32 or /64
    compressed-v6 form.  Falls back to IPv4 (dest) when no v6 is recorded.
    Returns None when no destination is present in the row.

    Row layout (matches the rx_sw regex in _swaps_mets_srates):
        row[9]  = dest   (str|int|None) - IPv4 as a 32-bit decimal
        row[10] = dest6  (str|int|None) - first 32 bits of IPv6  (0.4.79+)
        row[11] = dest6b (str|int|None) - second 32 bits of IPv6 (0.4.83+)

    Used by both data_swap_outcomes() and _run_writeback_and_get_swaps()
    so the two paths can never drift apart again (the writeback path
    previously dropped dest6b, silently truncating every IPv6 dest
    that had nonzero upper 32 bits).
    """
    _dest6  = row[10] if len(row) > 10 else None
    _dest6b = row[11] if len(row) > 11 else None
    if _dest6:
        try:
            _n6 = int(_dest6)
            if _n6 != 0:
                _hi = (_n6 >> 16) & 0xFFFF
                _lo = _n6 & 0xFFFF
                if _dest6b:
                    try:
                        _n6b = int(_dest6b)
                        _hi2 = (_n6b >> 16) & 0xFFFF
                        _lo2 = _n6b & 0xFFFF
                        return "%x:%x:%x:%x::" % (_hi, _lo, _hi2, _lo2)
                    except (ValueError, TypeError):
                        return "%x:%x::" % (_hi, _lo)
                return "%x:%x::" % (_hi, _lo)
        except (ValueError, TypeError):
            pass
    _dest = row[9] if len(row) > 9 else None
    if _dest:
        try:
            _n = int(_dest)
            if _n != 0:
                return "%d.%d.%d.%d" % (
                    (_n >> 24) & 0xff, (_n >> 16) & 0xff,
                    (_n >> 8) & 0xff,  _n & 0xff)
        except (ValueError, TypeError):
            pass
    return None



_SWAP_DEST_RX = re.compile(
    r"swap cookie=(\d+) from=\d+ to=\d+ bc=\d+ ac=\d+ d=\d+"
    r"(?: mt=\d+ rb=\d+)? dest=(\d+)(?: dest6=(\d+))?")

_ESTAB_DEST_RX = re.compile(
    r"estab cookie=(\d+) alg=\d+ forced=\d+ dest=(\d+)(?: dest6=(\d+))?")



def _cookie_dest_map(text):
    out = {}
    for line in text.splitlines():
        m = _SWAP_DEST_RX.search(line)
        if m:
            out[m.group(1)] = (m.group(2), m.group(3))
            continue
        m = _ESTAB_DEST_RX.search(line)
        if m:
            out[m.group(1)] = (m.group(2), m.group(3))
    return out



def _uptime_now():
    try:
        with open('/proc/uptime') as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0




def _log_window(text) -> LogWindow:
    """Time range of the log tail."""
    sw, _, _ = _swaps_mets_srates(text)
    if not sw:
        return {"oldest_ts": 0, "newest_ts": 0, "span_min": 0, "swap_count": 0, "age_min": 0}
    oldest = min(r[0] for r in sw); newest = max(r[0] for r in sw)
    try:
        with open('/proc/uptime') as f: uptime = float(f.read().split()[0])
        now = time.time()
        oldest_wall = int(now - uptime + oldest)
        if newest > uptime:
            try:
                import os as _os
                newest_wall = int(_os.path.getmtime("/var/log/bpftune-met-live.log"))
            except (OSError, ValueError):
                newest_wall = int(now)
        else:
            newest_wall = int(now - uptime + newest)
        age_min = round((now - newest_wall) / 60, 1)
    except: oldest_wall = 0; newest_wall = 0; age_min = 0
    return {"oldest_ts": oldest_wall, "newest_ts": newest_wall,
            "span_min": round((newest - oldest) / 60, 1),
            "swap_count": len(sw), "age_min": age_min}





def _resolve_remote_host(rh):
    """Convert remote_host to a form _label_for can look up.
    Handles raw 32-bit integers (e.g., '1378604897' -> '82.43.215.97')."""
    if not rh:
        return None
    labeled = _label_for(rh)
    if labeled != rh:
        return labeled
    try:
        n = int(rh)
        if n > 0:
            dotted = "%d.%d.%d.%d" % (
                (n >> 24) & 0xff, (n >> 16) & 0xff,
                (n >> 8) & 0xff, n & 0xff)
            labeled = _label_for(dotted)
            return labeled if labeled != dotted else dotted
    except (ValueError, TypeError):
        pass
    return rh


CW   = 58

GAP  = "  " + VBAR + "  "

FULL = CW*2 + len(GAP)


