# Changelog

All notable changes to the bpftune dashboard (Go collector) are documented
in this file. The dashboard is a Go rewrite of the original Python dashboard,
optimized for low memory and low CPU on small VPS instances.

## [v0.8.0] — 2026-10-03

### Added
- **90-day "all" chart cap** — the "all" timerange now shows last 90 days
  at 24h bins (max 90 data points, ~2KB JSON). Previously was unlimited
  span at 6h bins (potentially thousands of bins).
- **CSV rotation** (`csv_rotate.go`) — daily at 4am, trims CSVs to 90 days
  when they exceed size thresholds (buckets.v2.csv > 200MB, swaps.csv >
  50MB, srate.csv > 50MB). Prevents endless growth.

### Changed
- `history.go`: `"all"` range changed from `{nil, 21600}` (unlimited, 6h
  bins) to `{90 * 86400, 86400}` (90 days, 24h bins).
- `renderSlowToDisk()`: calls `rotateAllCSVs()` at the start of each
  daily render cycle.

## [v0.7.9] — 2026-10-03

### Changed
- **Quiet writeback journal noise** — `streak_writeback.go` now only
  logs when something interesting happens:
  - Mask detection: only when mask CHANGES (once per boot, not every 30s)
  - v6 warning: only ONCE (ever, until restart)
  - "done: 0 hosts" summary: SKIPPED on no-patch cycles
  - Per-host + summary lines: kept (only show when patches happen)
- Result: journal is silent on most cycles, 1-2 lines when patches happen.

## [v0.7.8] — 2026-10-03

### Added
- **Historical swap outcome trends** — `render_swaps.go` (411 lines, already
  existed but wasn't being called) is now activated. Reads 16MB of swaps.csv
  + 8MB of srate.csv, recomputes sustained outcomes via
  `attachSustainedOutcomes()`, builds time-binned win/loss trends with
  Wilson confidence intervals. Output: `d0/d1_rate`, `d0/d1_lo`, `d0/d1_hi`,
  `d0/d1_n`, `d0/d1_rate_sustained`, etc.

### Fixed
- `renderSwapsFromCSV`: time filtering now uses `CollectedTs` (wall-clock
  epoch, column 0) instead of `BootTs` (seconds-since-boot). Was causing
  1h/24h/7d ranges to show 0 data (boot_ts << epoch, all filtered out).
- `render.go`: switched call from `hist.renderSwapsToDisk(so)` (current
  cycle only) to `renderSwapsFromCSV(time.Now().Unix())` (historical CSV).

## [v0.7.7] — 2026-10-03

### Added
- **CSV enrichment** (`csv_enrichment.go`) — `enrichSwapsForCSV()` fills in
  Outcome/SrateBefore/Direction/Rport on each swapRow before CSV write.
  Previously these 7 columns were always empty strings.
  - Outcome: outcomeSustained → outcomeComposite fallback → "no_post" if
    >300s expired
  - Direction: "origin" if rport==443 else "client" (from first met event
    3-300s after swap)
  - SrateBefore: last srate event before swap
  - Rport: from met event
- **Truth file** (`truth_writer.go`) — `writeTruthRow()` appends
  `{"bucket":"82.43.0.0","tgt":"4","cls":"win"}` to
  `swaps_truth.jsonl` for the bpftune tuner. Called from `writeSwapsCSV`
  after dedup (one truth entry per resolved swap).

### Changed
- `swapRow` struct: added Outcome, SrateBefore, Direction, Rport fields.
- `buildSwapCSVRow`: uses enriched fields instead of empty strings.
- `writeSwapsCSV`: calls `writeTruthRow` after dedup passes.
- `collect.go`: calls `enrichSwapsForCSV(allSwaps, allMets, allSrates)`
  before `writeSwapsCSV`.

## [v0.7.6] — 2026-10-03

### Added
- **Streak writeback** (`streak_writeback.go`, 596 lines) — patches BPF
  map `bad_streak`/`null_streak` from sustained outcomes (60-300s after
  swap). Mirrors deleted Python `streak_writeback.py` (232 lines).
  - Auto-detects v4/v6 prefix mask from BPF map entries
  - Converts swap dest (decimal u32) to IP via `swapDestToIP()`
  - Looks up remote_host_map entries using raw bytes from bpftool
  - Computes bad/null streak from last 8 sustained outcomes per (host, alg)
  - Patches only offsets 42/43 — preserves swap_score, rate_ema, etc.
  - Hooked from `collect()` as async goroutine (every 30s cycle)

### Fixed
- `swapDestToIP()`: converts kernel's decimal u32 (e.g., "1378604897")
  to dotted-quad IP (e.g., "82.43.215.97"). Without this, `net.ParseIP()`
  returned nil and ALL swaps were skipped.
- `mapLookupBytes()`: uses raw "value" bytes from bpftool --json (list
  of hex strings) instead of re-serializing from formatted.value. Also
  fixed JSON unmarshal type (single object, not array) — was causing
  every lookup to fail silently.

## [v0.7.5p] — 2026-10-03

### Added
- **Streaming CSV reader** (`streamCSVToSeries`) — reads the CSV line by
  line and accumulates running sums per bin, instead of loading all rows
  into memory as Go structs. Memory for slow renders (7d/30d/all):
  680MB-2.5GB → 3-5MB (200x reduction). No data loss — produces identical
  output to the old `readCSVAll + buildSeriesFromSnaps` path.

### Changed
- `renderSlowToDisk()` now calls `streamCSVToSeries()` instead of
  `readCSVAll()`. The streamed series is stored in `hist.streamedSeries`
  and used by `renderBucketToDisk()` for slow ranges (7d/30d/all).
- `renderBucketToDisk()` checks `hist.streamedSeries` first for slow
  ranges; falls back to `readBucketCSVFromMap` if not available.

## [v0.7.5n] — 2026-10-03

### Changed
- `loadCSVTailIntoRingBuffer()` now uses `readCSVTail(3600)` (8MB tail)
  instead of `readCSVAll()` (full CSV). The ring buffer only holds 1h of
  data, so reading the full 223MB CSV was wasteful — 222MB was immediately
  discarded. Saves ~600MB memory at startup.
- `renderSlowToDisk()` is now **async** — runs in a goroutine so the HTTP
  server starts in ~5s instead of 30-60s. The 7d/30d/all ranges populate
  in the background after 30-60s.

## [v0.7.5m] — 2026-10-03

### Fixed
- **SSE CPU bug** — removed per-second hash polling from the SSE handler.
  The old code marshaled `c.current` to JSON + computed MD5 every second per
  client. With 5 browser tabs, that was 10 JSON marshals/sec = constant 9%
  CPU. Now uses a 25-second keepalive comment (per SSE spec) + relies on
  `notifySSE()` channel pushes (every 30s after `collect()`).

### Changed
- `notifySSE()` now marshals the message ONCE and sends to all clients
  (was marshaling per-client).

## [v0.7.5l] — 2026-10-03

### Changed
- "Swap Target Pick" headline styled to match `lv-grid h3` (uppercase,
  10.5px, letter-spaced). "live" badge right-aligned via `margin-left: auto`.

## [v0.7.5i] — 2026-10-03

### Fixed
- `renderScoreNow()` is now called from `loadBucket.then()` (after
  `state.bucketDoc` is set). Previously it was only called from
  `renderLiveState` (SSE push), where `state.bucketDoc` was null — the
  chart stayed empty.

## [v0.7.5h] — 2026-10-03

### Changed
- Reverted swaps chart to `type: "time"` (matching rate/sscore/streaks).
- SSE `startLiveUpdates()` moved to AFTER `_loadCharts()` resolves —
  prevents the "date adapter not implemented" race condition where SSE
  fired before the adapter was registered.
- Added guard to `renderScoreNow()` — bails early if `state.bucketDoc`
  is null.

## [v0.7.5g2] — 2026-10-03

### Fixed
- `csv_reader.go`: apply `ResolveBucket()` at read time in `readCSVTail`,
  `readCSVAll`, and `readBucketCSV`. Old raw-IP rows (e.g., `2a01:7e03::`)
  now consolidate under their current label (e.g., `home-sco`). Fixes the
  "coverage shows – for labeled buckets" bug.
- Swaps chart: added `min: undefined, max: undefined` to x-axis config
  (was inheriting timestamp values from `timeOpts` base, causing category
  axis to zoom to phantom label indices).

## [v0.7.5f2] — 2026-10-03

### Added
- Fleet diagnostics in `renderFleetToDisk()` — logs `csvAll` state +
  per-bucket snap counts to stderr. Helps diagnose "coverage shows –"
  issues.
- Robust anchors for `renderFleetToDisk` patching (small line-level
  anchors instead of one big block).

### Changed
- Swaps chart switched to `type: "category"` with `"HH:MM"` string labels
  (later reverted in v0.7.5h).

## [v0.7.5e] — 2026-10-02

### Fixed
- **"Meta vs current fighting" bug** — the bucket dropdown was populated
  from `state.meta.buckets` (refreshed every 5 min), while the bucket table
  was populated from `current.json` (refreshed every 30s via SSE). They
  showed different buckets in different orders. Now both use `current.json`
  as the source of truth, with stable sort `(n_alg desc, inst desc, id asc)`
  applied server-side in `buildLiveBuckets()`.

### Added
- `_rebuildDropdownFromLive()` — rebuilds the dropdown options from
  `current.json`'s `buckets[]` on every SSE push (every 30s).
- `renderScoreNow()` — the "Swap Target Pick" chart (penalty-weighted
  score per algorithm).
- `score_<alg>` arrays computed from `re_`/`ss_`/`bs_`/`ns_`.

## [v0.7.5d] — 2026-10-02

### Changed
- Reverted swaps chart from `type: "line"` to `type: "bar"` (line type
  caused date adapter error).
- Wrapped `renderScoreNow()` in `_safeRender()` so errors don't break
  other charts.

## [v0.7.5c] — 2026-10-02

### Added
- Swaps per bin — reads from `swaps.csv` (full history) and aligns with
  the ts array from `buildSeriesFromSnaps`.
- Swaps chart uses `{x, y}` data form with `type: "time"`.

## [v0.7.5b] — 2026-10-02

### Fixed
- Coverage calculation uses `RefRate` (controld=0%, home-sco=100%).
- Score chart uses `Object.keys` instead of `window.__algs` (which was
  undefined).

## [v0.7.5] — 2026-10-02

### Added
- Score chart (`score_<alg>` arrays) — penalty-weighted `rate_ema ×
  swap_score` per algorithm.
- `rate_best_v` computation in CSV writer (for coverage calculation).
- `proofs_raw` — per-event proof data (with dest, rate, tier).
- `proof` — aggregated per-alg proof leaderboard (for "All Buckets" view).
- Five ranges: 1h, 24h, 7d, 30d, all.
- Ring buffer cap = 120 (1h at 30s intervals) — uses `[16]` arrays instead
  of maps (saves ~2GB memory).
- CSV tail reading (last 20MB for 24h charts, was 60MB full read).
- `renderToDisk` (every 5 min) generates 1h+24h only, preserves 7d/30d/all
  from existing files.
- `renderSlowToDisk` (daily 4am) regenerates all 5 ranges from full CSV.
- Sanitizer preserves dots and dashes for static file naming.

## [v0.7.4] — 2026-10-02

### Added
- Five ranges: 1h, 24h, 7d, 30d, all.
- `renderToDisk` is always fast (reads only 8MB CSV tail, not 60MB).
- `renderSlowToDisk` (daily) reads the full CSV for 7d/30d/all.
- Fast/slow split: 5-min render is lightweight, daily render does heavy
  processing.

## [v0.7.3] — 2026-10-02

### Changed
- Ring buffer cap = 120 (1h at 30s intervals).
- 24h/7d/all charts served from static files (rendered every 5 min by
  `renderToDisk`), not from the ring buffer.
- Synchronous first `renderToDisk` on startup — static files ready before
  HTTP starts.
- Synchronous first `renderSlowToDisk` — 7d+all generated on startup.

## [v0.7.0] — 2026-10-02

### Changed
- **Single source of truth for label resolution** — all label resolution
  goes through `ResolveBucket()`. No code path re-applies `labelFor` to
  already-labeled IDs.
- CSV writer writes the LABELED form (resolved at write time).
- CSV reader does NOT re-apply `labelFor` — trusts the stored label.
- **Stable sort** — `meta.json` and `current.json` buckets sorted by
  `(n_alg desc, inst_mean desc, id asc)`. The `id` tie-breaker prevents
  the "top bucket jumps between home-sco/controld" bug.
- **Static file invalidation** — when `labels.json` mtime changes, all
  `/data/*.json` files are deleted so the next `renderToDisk` cycle
  regenerates them with fresh labels.
- **Sanitizer** — replaces ALL non-alphanumeric chars with underscore for
  static filenames (`home-sco` → `home_sco`, `v6:26061a40` →
  `v6_26061a40`).

### Added
- 13 Go source files refactored from 8 monolithic files.

## [v0.7.0 initial] — 2026-10-02

### Added
- Go collector binary replacing the Python collector + renderer.
- Ring buffer with `[16]` arrays instead of maps.
- SSE (Server-Sent Events) for real-time updates.
- CSV persistence (buckets.v2.csv, swaps.csv, srate.csv).
- HTTP server with static file serving + dynamic fallback.
- Label editor API (`/api/labels` GET/POST).
