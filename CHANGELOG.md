# Changelog

All notable changes to the bpftune dashboard (Go collector) are documented
in this file. The dashboard is a Go rewrite of the original Python dashboard,
optimized for low memory and low CPU on small VPS instances.

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
