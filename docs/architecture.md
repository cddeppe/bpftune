# Architecture

This document describes the internal architecture of the bpftune dashboard
collector (the Go binary). For bpftune itself (the kernel module), see the
[bpftune documentation](https://github.com/oracle/bpftune).

## Overview

The dashboard is a single Go binary that:

1. Reads BPF maps via `bpftool --json map dump` (every 30s)
2. Parses the bpftune journal log tail (every 30s)
3. Writes CSV files for historical data (every 30s)
4. Serves a web UI with real-time SSE updates (every 30s)
5. Renders static JSON files for chart data (every 5 min)
6. Generates slow ranges (7d/30d/all) from CSV (daily)

```
┌─────────────────────────────────────────────────────────────┐
│                     Kernel (BPF)                             │
│   remote_host_map  │  bpftune service  │  journal logs       │
└───────┬───────────┴──────┬───────────┴──────┬──────────────┘
        │                  │                  │
        │ bpftool dump     │ systemd          │ journalctl
        │ (every 30s)      │                  │
        │                  │                  │
┌───────▼──────────────────▼──────────────────▼──────────────┐
│                bpftune-collector-go                         │
│                                                             │
│  ┌───────────┐  ┌────────────┐  ┌───────────┐  ┌────────┐│
│  │  collect()  │  │renderToDisk│  │ streamCSV  │  │  SSE   ││
│  │  (30s)     │  │  (5 min)   │  │ ToSeries  │  │ server ││
│  │            │  │  fast=true │  │ (daily)   │  │        ││
│  └─────┬─────┘  └─────┬──────┘  └─────┬─────┘  └───┬────┘│
│        │              │                │             │      │
│        ▼              ▼                │             │      │
│   current.json   bucket_*.json        │             │      │
│   buckets.csv    meta.json            │             │      │
│   swaps.csv      fleet.json           │             │      │
│   srate.csv       swaps.json          │             │      │
│                                     │             │      │
│  ┌──────────────────────────────────▼─────────────┐│      │
│  │           Ring Buffer (in-memory)              ││      │
│  │  hist.raw[bucketID] → last 120 snapshots (1h)   ││      │
│  └─────────────────────────────────────────────────┘│      │
│                                                    │ SSE   │
│  ┌─────────────────────────────────────────────────┐│      │
│  │           HTTP Server (port 8080)               ││      │
│  │  /  → index.html                                ││      │
│  │  /current.json → live state                     ││      │
│  │  /sse → Server-Sent Events stream                ││      │
│  │  /data/bucket_*.json → static chart data        ││      │
│  │  /data/meta.json → bucket list + sort           ││      │
│  │  /data/fleet.json → coverage stats              ││      │
│  │  /data/swaps.json → binned swap counts          ││      │
│  │  /api/labels → GET/POST label edits             ││      │
│  └─────────────────────────────────────────────────┘│      │
└─────────────────────────────────────────────────────┼──────┘
                                                      │
                                            ┌────────▼─────┐
                                            │   Browser     │
                                            │ dashboard.js  │
                                            │ Chart.js + SSE│
                                            └──────────────┘
```

## Data flow

### Every 30 seconds: `collect()`

1. **`readBPFMap()`** — runs `bpftool --json map dump name remote_host_map`,
   parses the JSON, applies `ResolveBucket()` (label resolution + v6 fold +
   prefix masking), merges by final labeled addr.

2. **`parseSwapsMetsSrates()`** — reads the bpftune journal log tail,
   parses swap events, metric samples, and sustained rate (srate) events.
   Parsed ONCE per cycle (was 3x before v0.7.0).

3. **`buildLiveBuckets()`** — sorts hosts by `(n_alg desc, inst desc, id asc)`
   (stable sort, matching meta.json's order), builds the top-8 buckets list,
   `metric_by_bucket` (per-alg rows), and `bucket_live` (time series from
   ring buffer).

4. **`captureSnapshotsFromBPF()`** — stores one `bucketSnapshot` per bucket
   into the ring buffer (`hist.raw[bucketID]`). Ring buffer is capped at 120
   entries (1h at 30s intervals).

5. **`rebuildBucketLiveFromRing()`** — replaces the single-point
   `bucket_live` with the full 1h time series from the ring buffer.

6. **`writeBucketsCSV()` + `writeSwapsCSV()` + `writeSrateCSV()`** —
   appends rows to the CSV files. Deduplicated by `(cookie, boot_ts)`.

7. **`writeCurrentJSON()`** — marshals the full `current.json` to disk.

8. **`notifySSE()`** — marshals the document ONCE and sends to all
   connected SSE clients via buffered channels.

### Every 5 minutes: `renderToDisk()` (fast)

1. **`readCSVTail(86400)`** — reads the last 8MB of `buckets.v2.csv` (covers
   ~24h of data for 8 buckets). NOT the full CSV — 8MB is enough for 24h.

2. **`renderMetaToDisk()`** — writes `meta.json` (bucket list, sorted by
   `n_alg desc, inst_mean desc, id asc`).

3. **`renderSwapsToDisk()`** — writes `swaps.json` (per-range binned swap
   counts, aligned with the ts array).

4. **`renderFleetToDisk()`** — writes `fleet.json` (top 25 buckets by 24h
   coverage). Includes diagnostic logging (`fleet-diag:`).

5. **`renderBucketsToDisk(fast=true)`** — for each bucket, regenerates
   1h + 24h ranges (from ring buffer + CSV tail), preserves 7d/30d/all from
   the existing `bucket_<id>.json` file (read existing → merge → write).

### Daily at 4am: `renderSlowToDisk()` (slow)

1. **`streamCSVToSeries()`** — reads the ENTIRE `buckets.v2.csv` line by
   line using `bufio.Scanner`. For each row:
   - Parse timestamp, addr (via `ResolveBucket()`), and per-alg fields
   - Check if the row is within the span for each slow range (7d, 30d, all)
   - Add to per-bin running sums (`sumRe`, `sumSs`, `sumBs`, `sumNs`,
     `sumMv` per algorithm index)
   - Memory: O(buckets × ranges × bins) = ~3-5MB (not O(total_rows) = 680MB)

2. After scanning: compute averages from sums, produce series arrays
   (same format as `buildSeriesFromSnaps()`).

3. **`renderBucketsToDisk(fast=false)`** — for each bucket, regenerate ALL
   ranges (1h from ring buffer, 24h from CSV tail, 7d/30d/all from the
   streamed series).

4. The streamed series is stored in `hist.streamedSeries` and freed after
   rendering.

### On-demand: HTTP handlers

- **`/`** → serves `index.html`
- **`/dashboard.js`**, **`/dashboard.css`** → serves frontend files
- **`/current.json`** → serves the live state (written every 30s by `collect()`)
- **`/sse`** → Server-Sent Events stream (pushes on `notifySSE()`, 25s
  keepalive comment)
- **`/data/bucket_<id>.json`** → static file if fresh (<10 min), otherwise
  dynamic fallback (reads CSV + builds series on the fly)
- **`/data/meta.json`**, **`/data/fleet.json`**, **`/data/swaps.json`** →
  static files regenerated every 5 min
- **`/api/labels`** → GET (return labels + groups) / POST (edit labels,
  invalidate static files)

## Memory model

### Ring buffer (`hist.raw`)

- `map[string][]bucketSnapshot` — one entry per bucket ID (labeled)
- Each bucket: last 120 snapshots (1h at 30s intervals)
- Each `bucketSnapshot`: ~700 bytes (16 algs × 5 arrays + base fields)
- Total for 8 buckets: ~672KB

### CSV tail (`hist.csvAll`)

- Set by `renderToDisk()` to `readCSVTail(86400)` = last 8MB of CSV
- Freed after rendering (`defer func() { hist.csvAll = nil }()`)
- Used by `renderBucketsToDisk()` for 24h range
- Peak: ~8MB during render, then freed

### Streamed series (`hist.streamedSeries`)

- Set by `renderSlowToDisk()` to the output of `streamCSVToSeries()`
- `map[bucketID]map[rngName]map[string]interface{}` — pre-built series for
  7d/30d/all
- Freed after rendering (`defer func() { hist.streamedSeries = nil }()`)
- Peak: ~3-5MB (8 buckets × 3 ranges × ~120 bins × ~700 bytes)

### Total memory

| Component | Peak memory |
|-----------|-------------|
| Ring buffer | ~1MB |
| CSV tail (during fast render) | ~8MB |
| Streamed series (during slow render) | ~3-5MB |
| Go runtime + BPF map data | ~20-50MB |
| current.json (in memory) | ~100-300KB |
| **Total peak** | **~50-100MB** (was 680MB-2.5GB before v0.7.5) |

## Label resolution

All label resolution goes through ONE function: `ResolveBucket(addr)` in
`bucketing.go`. The resolution chain:

1. **`foldV6(addr)`** — if `v6:hex` form, check `/etc/bpftune/aliases` for
   a v6→v4 fold rule. If found, return the v4. Otherwise, convert to
   standard IPv6 `/32` form (`xxxx:xxxx::`).

2. **`longestPrefixLabel(addr)`** — try exact match in
   `aliases.labels.json`. If no exact match, iterate all labels and find
   the longest CIDR match. Fall back to `/etc/bpftune/aliases` labels.

3. **`canonBucketWithPrefix(addr, prefix4, prefix6)`** — if no label found,
   mask the IP to `/prefix4` (v4) or `/prefix6` (v6) using the values from
   `/var/lib/bpftune/prefix4` and `prefix6`.

### Where ResolveBucket is called

- **`readBPFMap()`** — once per BPF map entry (at collection time)
- **`streamCSVToSeries()`** — once per CSV row (at slow render time)
- **`readCSVTail()` + `readBucketCSV()`** — once per CSV row (at fast render
  time) — v0.7.5g2: re-resolves at read time so old raw-IP rows consolidate
  under current labels
- **Swaps per bin** (in `renderBucketToDisk`) — once per swaps.csv row

### Static file invalidation

When `labels.json` mtime changes (user edits labels via the UI):
1. `markLabelsChanged()` sets `staticFilesDirty = true`
2. Next `renderToDisk()` call calls `invalidateStaticFiles()` — deletes all
   `/data/*.json` files
3. Regenerates them with fresh labels

## SSE (Server-Sent Events)

### Server side

```go
func (c *Collector) handleSSE(w http.ResponseWriter, r *http.Request) {
    // Send initial state
    // ...
    ch := make(chan []byte, 10)
    c.sseClients[ch] = true
    defer delete(c.sseClients, ch)

    for {
        select {
        case <-r.Context().Done():
            return
        case data := <-ch:
            fmt.Fprintf(w, "data: %s\n\n", data)
            flusher.Flush()
        case <-time.After(25 * time.Second):
            fmt.Fprintf(w, ": keepalive\n\n")  // prevents proxy timeout
            flusher.Flush()
        }
    }
}
```

### `notifySSE()` — called after every `collect()`

```go
func (c *Collector) notifySSE() {
    current := c.current
    msg := map[string]interface{}{"__t": "f", "v": current}
    data, _ := json.Marshal(msg)  // marshal ONCE
    for client := range c.sseClients {
        select {
        case client <- data:
        default: // buffer full, skip
        }
    }
}
```

### Client side (dashboard.js)

```js
_sseSource = new EventSource("/sse");
_sseSource.onmessage = function(e) {
    var doc = JSON.parse(e.data);
    renderLiveState(doc);  // updates all charts + panels
};
_sseSource.onerror = function() {
    // Fall back to polling (30s interval)
    startPollingFallback();
};
```

### Boot sequence (v0.7.5h)

SSE starts AFTER Chart.js + date adapter are loaded:
```js
_loadCharts().then(function() {
    startLiveUpdates();  // SSE starts here
    return _loadData();  // load meta/swaps/fleet
}).then(function(results) {
    // populate state, render charts
});
```

This prevents the "date adapter not implemented" error that occurred when
SSE fired before the adapter was registered.

## Multi-architecture support

### Build

```bash
# Cross-compile for both architectures:
GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -o bpftune-collector-go-amd64 .
GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -o bpftune-collector-go-arm64 .
```

`CGO_ENABLED=0` ensures no C dependencies — the binary is fully static and
portable.

### Install detection

```bash
ARCH=$(uname -m)
case "$ARCH" in
    x86_64|amd64) BIN_ARCH="amd64" ;;
    aarch64|arm64) BIN_ARCH="arm64" ;;
esac
cp bpftune-collector-go-$BIN_ARCH $INSTALL_DIR/bpftune-collector-go
```

### GitHub Releases

Pre-built binaries are published as GitHub Release assets:
```
releases/latest/download/bpftune-collector-go-amd64
releases/latest/download/bpftune-collector-go-arm64
```

The install script downloads the correct one based on `uname -m`.

## File layout

```
/opt/bpftune-dashboard/bin/
  bpftune-collector-go     # the Go binary
  dashboard.js             # frontend (Chart.js + SSE client)
  index.html               # dashboard page
  dashboard.css            # styles
  labels-api.py            # Python helper for Edit Labels UI (optional)

/var/lib/bpftune/
  history/
    buckets.v2.csv         # per-bucket metrics (append-only, ~1MB/day/bucket)
    swaps.csv              # swap events (deduplicated)
    srate.csv               # sustained rate samples
    current.json           # live state (written every 30s)
    data/                  # static JSON files for charts
      bucket_<id>.json     # per-bucket series (1h/24h/7d/30d/all)
      meta.json            # bucket list + sort
      fleet.json           # coverage stats
      swaps.json            # binned swap counts
  aliases.labels.json      # IP → label map (user-editable)

/etc/bpftune/
  aliases                  # IP groups (v6→v4 fold rules + labels)

/etc/systemd/system/
  bpftune-collector-go.service  # systemd unit
```

## Performance characteristics

### CPU

- **Idle** (between collect cycles): 0%
- **Collect spike** (every 30s, 1-3s): 50-99% (bpftool + JSON marshal + CSV write)
- **Fast render** (every 5 min, 2-5s): 10-30% (read 8MB CSV tail + write JSON)
- **Slow render** (daily, 10-60s): 20-50% (stream full CSV + write 7d/30d/all)
- **Average**: 2-6% on 2-core x86, 5-10% on 1-core ARM

### Memory

- **Ring buffer**: ~1MB (8 buckets × 120 entries × ~700 bytes)
- **CSV tail** (during fast render): ~8MB (freed after)
- **Streamed series** (during slow render): ~3-5MB (freed after)
- **Go runtime + BPF data**: ~20-50MB
- **Total steady-state**: 50-150MB (was 251MB-2.5GB before v0.7.5)

### Disk

- **Binaries**: ~9MB (Go binary + frontend)
- **CSV growth**: ~1MB/day per bucket (8 buckets = ~8MB/day = ~3GB/year)
- **Static JSON**: ~500KB per bucket × 8 = ~4MB (regenerated every 5 min)

### Network

- **SSE**: ~100KB per push × 1 push/30s = ~3KB/s per client
- **Static files**: ~200KB per bucket × 8 = ~1.6MB per page load
- **current.json**: ~100KB per fetch (every 30s)
