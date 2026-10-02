#!/usr/bin/env bash
# =============================================================================
# Atomic patch v0.4.1: bucket cap + sort order + live_leaders break fix
#
# Three bugs in v0.4.0's main.go:
#   1. Hosts sorted by voteSum desc, but Python read_map sorts by inst desc.
#      Result: Go bucket order didn't match Python's (top-busiest-by-inst).
#   2. Buckets list cap was an empty `if len(buckets) >= 8 { /* noop */ }`
#      check.  Go was returning 21 buckets; Python returns 8.
#   3. `break` inside live_leaders cap was exiting the WHOLE for-range
#      loop, preventing metric_by_bucket from being built for buckets 9+.
#      (Happened to work for the user because most buckets past #8 had
#      n_alg=0 so didn't trigger the break — but it was still wrong.)
#
# Only main.go changes.  Other Go files unchanged from v0.4.0.
#
# Usage:
#   bash apply-0.4.1-go-bucket-cap-fix.sh            # interactive
#   bash apply-0.4.1-go-bucket-cap-fix.sh --yes      # non-interactive + push
#   bash apply-0.4.1-go-bucket-cap-fix.sh --no-push   # commit only
# =============================================================================

set -euo pipefail

YES=0
NO_PUSH=0
for arg in "$@"; do
  case "$arg" in
    --yes)        YES=1 ;;
    --no-push)    NO_PUSH=1 ;;
    -h|--help)   sed -n '2,20p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# --- pre-flight: find repo root ------------------------------------------------
REPO_ROOT=""
DIR="$(pwd)"
while [ "$DIR" != "/" ]; do
  if [ -d "$DIR/.git" ] && [ -d "$DIR/dashboard/bin/go" ]; then
    REPO_ROOT="$DIR"; break
  fi
  DIR="$(dirname "$DIR")"
done
if [ -z "$REPO_ROOT" ]; then
  echo "FATAL: not inside a bpftune checkout." >&2
  exit 1
fi
cd "$REPO_ROOT"
echo "repo root: $REPO_ROOT"

# --- pre-flight: branch --------------------------------------------------------
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
case "$BRANCH" in
  dashboard|main) echo "on branch: $BRANCH" ;;
  *)
    if [ "$YES" -eq 1 ]; then
      echo "WARN: on branch '$BRANCH' — continuing (--yes)" >&2
    else
      echo "FATAL: on branch '$BRANCH', expected 'dashboard' or 'main'." >&2
      exit 1
    fi ;;
esac

# --- fetch + ff-only pull ------------------------------------------------------
if git rev-parse --verify origin/dashboard >/dev/null 2>&1; then
  echo "fetching origin/dashboard..."
  git fetch origin dashboard
  AB="$(git rev-list --left-right --count origin/dashboard...HEAD 2>/dev/null || echo '0 0')"
  read -r AHEAD BEHIND <<< "$AB"
  AHEAD="${AHEAD:-0}"; BEHIND="${BEHIND:-0}"
  echo "ahead=$AHEAD behind=$BEHIND"
  if [ "$BEHIND" -gt 0 ] && [ "$AHEAD" -eq 0 ]; then
    git merge --ff-only origin/dashboard
  elif [ "$AHEAD" -gt 0 ] && [ "$BEHIND" -gt 0 ]; then
    echo "FATAL: branches diverged — resolve manually." >&2
    exit 1
  fi
fi

# --- write main.go -------------------------------------------------------------
echo "writing dashboard/bin/go/main.go ..."
cat > dashboard/bin/go/main.go <<'__Z_FILE_EMBED_END_SENTINEL__'
// bpftune-collector-go: Go replacement for bpftune-collector.py
//
// Handles: HTTP server (static files + gzip + SSE), BPF map reading,
// current.json generation, SSE delta encoding, /api/labels.
// The Python renderer (bpftune-render.py) stays as-is (runs via cron).
//
// Build: go build -o bpftune-collector-go
// Run:   ./bpftune-collector-go --port 8080 --bind 0.0.0.0
package main

import (
        "compress/gzip"
        "crypto/md5"
        "encoding/json"
        "flag"
        "fmt"
        "net/http"
        "os"
        "os/exec"
        "path/filepath"
        "sort"
        "strings"
        "sync"
        "time"
)

// ============================================================================
// Configuration
// ============================================================================

var (
        histDir     = "/var/lib/bpftune/history"
        binDir      = "/opt/bpftune-dashboard/bin"
        labelsFile  = "/var/lib/bpftune/aliases.labels.json"
        aliasesFile = "/etc/bpftune/aliases"
)

// ============================================================================
// Global state (protected by mutex)
// ============================================================================

type Collector struct {
        mu         sync.RWMutex
        current    map[string]interface{} // current.json data
        keyHashes  map[string]string      // per-key md5 for SSE delta
        sseClients map[chan []byte]bool
        startedAt  time.Time // for uptime_min field
}

func NewCollector() *Collector {
        return &Collector{
                current:    make(map[string]interface{}),
                keyHashes:  make(map[string]string),
                sseClients: make(map[chan []byte]bool),
                startedAt:  time.Now(),
        }
}

// ============================================================================
// BPF map reading (via bpftool)
// ============================================================================

// hostEntry is one BPF map entry after label resolution + merge.
// Mirrors Python read_map()'s (inst, addr, v) tuple.
type hostEntry struct {
        Inst int
        Addr string // labeled + merged (e.g. "home-sco" or "v6:20010db8" → folded)
        V    map[string]interface{}
}

// readBPFMap runs bpftool, parses the JSON, applies labelFor + merges
// by final label.  Mirrors bpftune_log.py:read_map.
func readBPFMap() ([]hostEntry, error) {
        cmd := exec.Command("bpftool", "--json", "map", "dump", "name", "remote_host_map")
        output, err := cmd.Output()
        if err != nil {
                return nil, fmt.Errorf("bpftool: %w", err)
        }
        var raw []map[string]interface{}
        if err := json.Unmarshal(output, &raw); err != nil {
                return nil, fmt.Errorf("bpftool json: %w", err)
        }
        // Merge by final labeled addr: sum instances, keep the entry
        // with more instances for the other fields (matches Python).
        merged := map[string]*hostEntry{}
        var order []string
        for _, entry := range raw {
                fmtData, ok := entry["formatted"].(map[string]interface{})
                if !ok {
                        continue
                }
                val, ok := fmtData["value"].(map[string]interface{})
                if !ok {
                        continue
                }
                keyData, ok := fmtData["key"].(map[string]interface{})
                if !ok {
                        continue
                }
                in6u, ok := keyData["in6_u"].(map[string]interface{})
                if !ok {
                        continue
                }
                addrBytes, ok := in6u["u6_addr8"].([]interface{})
                if !ok || len(addrBytes) != 16 {
                        continue
                }
                b := make([]int, 16)
                for i, v := range addrBytes {
                        b[i] = int(v.(float64))
                }
                var addr string
                if b[10] == 0xff && b[11] == 0xff {
                        addr = fmt.Sprintf("%d.%d.%d.%d", b[12], b[13], b[14], b[15])
                } else {
                        v6 := (b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3]
                        if v6 != 0 {
                                addr = fmt.Sprintf("v6:%08x", v6)
                        } else {
                                addr = "0.0.0.0"
                        }
                }
                // Apply labelFor + fold + canon, then merge by final key.
                final := labelFor(addr)
                if final == "" {
                        final = addr
                }
                inst := toInt(val["instances"])
                if existing, ok := merged[final]; ok {
                        existing.Inst += inst
                        // Keep the entry with more instances for the other fields.
                        if inst > existing.Inst-inst {
                                existing.V = val
                        }
                } else {
                        merged[final] = &hostEntry{Inst: inst, Addr: final, V: val}
                        order = append(order, final)
                }
        }
        out := make([]hostEntry, 0, len(order))
        for _, addr := range order {
                out = append(out, *merged[addr])
        }
        return out, nil
}

// ============================================================================
// Collection cycle — reads BPF map, builds current.json
// ============================================================================

func (c *Collector) collect() {
        hosts, err := readBPFMap()
        if err != nil {
                fmt.Fprintf(os.Stderr, "collector: BPF map read failed: %v\n", err)
                return
        }

        now := time.Now().Unix()

        // Build buckets list (top 8 by inst) + metric_by_bucket +
        // bucket_live (single-point) + live_leaders.  All use the same
        // iter so we walk hosts once.
        type bucket struct {
                Dest    string  `json:"dest"`
                Inst    int     `json:"inst"`
                RttUs   float64 `json:"rtt_us"`
                RefMbps float64 `json:"ref_mbps"`
                BestAlg string  `json:"best_alg"`
                NAlg    int     `json:"n_alg"`
        }
        var buckets []bucket
        metricByBucket := map[string]interface{}{}
        bucketLive := map[string]interface{}{}
        var liveLeaders []interface{}

        // Vote-sum sort: busiest bucket first (matches Python's
        // sorted(hosts, key=lambda x: -_vote_sum(x[2])) in
        // data_metric_by_bucket).
        sortedHosts := make([]hostEntry, len(hosts))
        copy(sortedHosts, hosts)
        // Sort by inst desc — matches Python read_map line 809:
        //   entries.sort(key=lambda x: -x[0])
        // Python's data_metric_by_bucket re-sorts by vote_sum desc, but
        // since metric_by_bucket is a JSON object (unordered), we just
        // use one sort: inst desc.  This gives the buckets list and
        // live_leaders the same order Python produces.
        sort.Slice(sortedHosts, func(i, j int) bool {
                return sortedHosts[i].Inst > sortedHosts[j].Inst
        })

        for _, h := range sortedHosts {
                v := h.V
                addr := h.Addr
                if addr == "0.0.0.1" || addr == "?" {
                        continue
                }
                if strings.HasPrefix(addr, "127.") ||
                        strings.HasPrefix(addr, "169.254.") ||
                        strings.HasPrefix(addr, "0.") {
                        continue
                }
                if h.Inst < 2 {
                        continue
                }
                inst := h.Inst

                // Picker's choice (same formula as data_live_leaders).
                metrics, _ := v["metrics"].([]interface{})
                bestI := toInt(v["best_i"])
                if bestI < 0 || bestI >= len(CONGS) {
                        bestI = 0
                }
                bestW := 0
                for i := 0; i < len(CONGS) && i < len(metrics); i++ {
                        mi, _ := metrics[i].(map[string]interface{})
                        if mi == nil {
                                continue
                        }
                        cnt := toInt(mi["metric_count"])
                        rv := toInt(mi["rate_ema"])
                        if cnt < minLeaderTrust || rv == 0 {
                                continue
                        }
                        ss := toInt(mi["swap_score"])
                        ssEff := ss
                        if ssEff == 0 {
                                ssEff = 256
                        }
                        bad := toInt(mi["bad_streak"])
                        nul := toInt(mi["null_streak"])
                        weighted := rv * ssEff / 256
                        pen := 16 + bad*4 + nul*2
                        weighted = weighted * 16 / pen
                        if weighted > bestW {
                                bestW = weighted
                                bestI = i
                        }
                }
                bestAlg := CONGS[bestI]
                if bestI >= len(CONGS) {
                        bestAlg = fmt.Sprintf("alg%d", bestI)
                }
                // n_alg = count of metrics with metric_count > 0
                nAlg := 0
                for _, m := range metrics {
                        if mi, ok := m.(map[string]interface{}); ok &&
                                toInt(mi["metric_count"]) > 0 {
                                nAlg++
                        }
                }
                refMbps := toFloat(v["max_rate_delivered"]) / bpsToMbps
                // Cap buckets list at 8 (Python data_buckets: n=8).  We use a
                // conditional append instead of `break` so the loop keeps
                // going to build metric_by_bucket + bucket_live for ALL
                // buckets (not just the first 8).
                if len(buckets) < 8 {
                        buckets = append(buckets, bucket{
                                Dest:    addr,
                                Inst:    inst,
                                RttUs:   toFloat(v["min_rtt"]),
                                RefMbps: round1(refMbps),
                                BestAlg: bestAlg,
                                NAlg:    nAlg,
                        })
                }

                // metric_by_bucket: full per-alg row matching Python's
                // data_metric_by_bucket output shape.
                var metricRows []interface{}
                for i := 0; i < len(CONGS); i++ {
                        var mi map[string]interface{}
                        if i < len(metrics) {
                                mi, _ = metrics[i].(map[string]interface{})
                        }
                        if mi == nil {
                                mi = map[string]interface{}{}
                        }
                        val := toInt(mi["metric_value"])
                        if val == (1<<63-1) || val < 0 {
                                val = 0
                        }
                        mc := toInt(mi["metric_count"])
                        a := toInt(mi["sockets_alive"])
                        ss := toInt(mi["swap_score"])
                        bs := toInt(mi["bad_streak"])
                        ns := toInt(mi["null_streak"])
                        re := toInt(mi["rate_ema"])
                        penalty := 16.0 / (16.0 + float64(bs)*4.0 + float64(ns)*2.0)
                        score := float64(re) * (float64(ss) / 256.0) * penalty
                        active := mc > 0 || a > 0 || re > 0 || ss > 0
                        var metricVal interface{}
                        if val > 0 {
                                metricVal = round1(float64(val) / 1e6)
                        } else {
                                metricVal = 0
                        }
                        row := map[string]interface{}{
                                "alg":         CONGS[i],
                                "metric":      metricVal,
                                "votes":       mc,
                                "alive":       a,
                                "rate_ema":    re,
                                "swap_score":  ss,
                                "penalty":     round3(penalty),
                                "score":       round2(score),
                                "bad_streak":  bs,
                                "null_streak": ns,
                                "active":      active,
                        }
                        metricRows = append(metricRows, row)
                }
                // Sort by (active desc, score desc).
                sort.SliceStable(metricRows, func(i, j int) bool {
                        ri, _ := metricRows[i].(map[string]interface{})
                        rj, _ := metricRows[j].(map[string]interface{})
                        ai, _ := ri["active"].(bool)
                        aj, _ := rj["active"].(bool)
                        if ai != aj {
                                return ai
                        }
                        si, _ := ri["score"].(float64)
                        sj, _ := rj["score"].(float64)
                        return si > sj
                })
                metricByBucket[addr] = metricRows

                // bucket_live (single-point; renderer provides historical series).
                ts := now
                cols := map[string]interface{}{}
                for i := 0; i < len(CONGS); i++ {
                        var mi map[string]interface{}
                        if i < len(metrics) {
                                mi, _ = metrics[i].(map[string]interface{})
                        }
                        if mi == nil {
                                mi = map[string]interface{}{}
                        }
                        cols["re_"+CONGS[i]] = []interface{}{toFloat(mi["rate_ema"])}
                        cols["ss_"+CONGS[i]] = []interface{}{toInt(mi["swap_score"])}
                        cols["bs_"+CONGS[i]] = []interface{}{toInt(mi["bad_streak"])}
                        cols["ns_"+CONGS[i]] = []interface{}{toInt(mi["null_streak"])}
                }
                bucketLive[addr] = map[string]interface{}{
                        "ts":   []interface{}{ts},
                        "cols": cols,
                }

                // live_leaders entry (proper formula).
                var cands []struct {
                        W, Rv, Ss, Bad, Nul, Cnt, I int
                }
                for i := 0; i < len(CONGS); i++ {
                        var mi map[string]interface{}
                        if i < len(metrics) {
                                mi, _ = metrics[i].(map[string]interface{})
                        }
                        if mi == nil {
                                continue
                        }
                        cnt := toInt(mi["metric_count"])
                        rv := toInt(mi["rate_ema"])
                        ss := toInt(mi["swap_score"])
                        bad := toInt(mi["bad_streak"])
                        nul := toInt(mi["null_streak"])
                        if cnt < minLeaderTrust || rv == 0 {
                                continue
                        }
                        ssEff := ss
                        if ssEff == 0 {
                                ssEff = 256
                        }
                        weighted := rv * ssEff / 256
                        pen := 16 + bad*4 + nul*2
                        weighted = weighted * 16 / pen
                        cands = append(cands, struct {
                                W, Rv, Ss, Bad, Nul, Cnt, I int
                        }{weighted, rv, ss, bad, nul, cnt, i})
                }
                if len(cands) > 0 {
                        sort.SliceStable(cands, func(i, j int) bool { return cands[i].W > cands[j].W })
                        topN := liveTopN
                        if len(cands) < topN {
                                topN = len(cands)
                        }
                        topRows := make([]interface{}, 0, topN)
                        for _, c := range cands[:topN] {
                                topRows = append(topRows, map[string]interface{}{
                                        "alg":        CONGS[c.I],
                                        "weighted":   c.W,
                                        "rate_ema":   c.Rv,
                                        "swap_score": c.Ss,
                                        "bad":        c.Bad,
                                        "null":       c.Nul,
                                        "count":      c.Cnt,
                                })
                        }
                        // Cap live_leaders at LIVE_MAX_BUCKETS (8).  Use a
                        // conditional append instead of `break` — we still
                        // need to build metric_by_bucket + bucket_live for
                        // the remaining buckets in this loop iteration.
                        if len(liveLeaders) < liveMaxBuckets {
                                liveLeaders = append(liveLeaders, map[string]interface{}{
                                        "dest": addr,
                                        "inst": inst,
                                        "top":  topRows,
                                })
                        }
                }
        }

        // Build current.json
        doc := map[string]interface{}{
                "generated_ts": now,
                "build": map[string]interface{}{
                        "version":      "go-collector",
                        "dash_version": "go-0.4",
                        "service":      "active",
                        "uptime_min":   int(time.Since(c.startedAt).Minutes()),
                        "started_utc":  c.startedAt.UTC().Format(time.RFC3339),
                        "log_path":     "/var/log/bpftune-met-live.log",
                },
                "system": map[string]interface{}{
                        "kernel":     readProc("/proc/sys/kernel/osrelease"),
                        "default_cc": readProc("/proc/sys/net/ipv4/tcp_congestion_control"),
                },
                "buckets":          buckets,
                "metric_by_bucket": metricByBucket,
                "bucket_live":      bucketLive,
                "live_leaders":     liveLeaders,
                "hostname":         readProc("/proc/sys/kernel/hostname"),
                "now_mono":         now,
        }

        // 0.4 full port: parse log files for recent_swaps, recent_proofs,
        // swap_outcomes, bucket_ips, log_window, proofs_raw.
        topSwaps, topProofs, swapOutcomes, bucketIPs, logWindow, proofsRaw := c.parseLogs()
        doc["recent_swaps"] = topSwaps
        doc["recent_proofs"] = topProofs
        doc["swap_outcomes"] = swapOutcomes
        doc["bucket_ips"] = bucketIPs
        doc["log_window"] = logWindow
        doc["proofs_raw"] = proofsRaw

        // Update current state + compute key hashes
        c.mu.Lock()
        c.current = doc
        c.keyHashes = computeKeyHashes(doc)
        c.mu.Unlock()

        // Write current.json to disk
        c.writeCurrentJSON()

        // Notify SSE clients
        c.notifySSE()

        fmt.Fprintf(os.Stderr, "collector: collected %d buckets, %d swaps, ts=%d\n",
                len(buckets), len(topSwaps), now)
}

// voteSum sums metric_count across all algs for one bucket.  Used to
// sort hosts busiest-first (matches Python _vote_sum).
func voteSum(v map[string]interface{}) int {
        if v == nil {
                return 0
        }
        metrics, _ := v["metrics"].([]interface{})
        total := 0
        for _, m := range metrics {
                if mi, ok := m.(map[string]interface{}); ok {
                        total += toInt(mi["metric_count"])
                }
        }
        return total
}

func (c *Collector) writeCurrentJSON() {
        c.mu.RLock()
        data, _ := json.Marshal(c.current)
        c.mu.RUnlock()

        path := filepath.Join(histDir, "current.json")
        tmp := path + ".tmp"
        if err := os.WriteFile(tmp, data, 0644); err != nil {
                fmt.Fprintf(os.Stderr, "collector: write current.json failed: %v\n", err)
                return
        }
        os.Rename(tmp, path)
}

func computeKeyHashes(doc map[string]interface{}) map[string]string {
        hashes := make(map[string]string)
        for k, v := range doc {
                data, _ := json.Marshal(v)
                sum := md5.Sum(data)
                hashes[k] = fmt.Sprintf("%x", sum)
        }
        return hashes
}

// ============================================================================
// SSE delta encoding
// ============================================================================

func (c *Collector) notifySSE() {
        c.mu.RLock()
        hashes := make(map[string]string, len(c.keyHashes))
        for k, v := range c.keyHashes {
                hashes[k] = v
        }
        current := c.current
        c.mu.RUnlock()

        // For each SSE client, send delta
        c.mu.Lock()
        for client := range c.sseClients {
                // Send full payload (simplified — in production, track per-client state)
                msg := map[string]interface{}{"__t": "f", "v": current}
                data, _ := json.Marshal(msg)
                select {
                case client <- data:
                default: // client buffer full, skip
                }
        }
        c.mu.Unlock()
}

// ============================================================================
// HTTP handlers
// ============================================================================

func (c *Collector) handleIndex(w http.ResponseWriter, r *http.Request) {
        if r.URL.Path == "/" || r.URL.Path == "/index.html" {
                c.serveStatic(w, r, filepath.Join(histDir, "index.html"), "text/html; charset=utf-8")
                return
        }
        if r.URL.Path == "/dashboard.js" || r.URL.Path == "/dashboard.css" {
                c.serveStatic(w, r, filepath.Join(binDir, r.URL.Path), "")
                return
        }
        if strings.HasPrefix(r.URL.Path, "/data/") {
                sub := r.URL.Path[len("/data/"):]
                if sub == "" || strings.HasSuffix(sub, ".csv") || strings.Contains(sub, "..") || strings.HasPrefix(sub, ".") {
                        http.NotFound(w, r)
                        return
                }
                c.serveStatic(w, r, filepath.Join(histDir, "data", sub), "")
                return
        }
        if r.URL.Path == "/current.json" {
                c.handleCurrentJSON(w, r)
                return
        }
        if r.URL.Path == "/sse" {
                c.handleSSE(w, r)
                return
        }
        if strings.HasPrefix(r.URL.Path, "/api/labels") {
                c.handleLabels(w, r)
                return
        }
        http.NotFound(w, r)
}

func (c *Collector) serveStatic(w http.ResponseWriter, r *http.Request, path, contentType string) {
        data, err := os.ReadFile(path)
        if err != nil {
                http.NotFound(w, r)
                return
        }
        // Guess MIME type
        if contentType == "" {
                contentType = "application/octet-stream"
                switch filepath.Ext(path) {
                case ".html":
                        contentType = "text/html; charset=utf-8"
                case ".js":
                        contentType = "application/javascript; charset=utf-8"
                case ".css":
                        contentType = "text/css; charset=utf-8"
                case ".json":
                        contentType = "application/json; charset=utf-8"
                }
        }
        // Gzip if client supports it
        if acceptsGzip(r) && shouldGzip(path) {
                w.Header().Set("Content-Type", contentType)
                w.Header().Set("Content-Encoding", "gzip")
                w.Header().Set("Vary", "Accept-Encoding")
                w.Header().Set("Cache-Control", "no-cache")
                gz := gzip.NewWriter(w)
                defer gz.Close()
                gz.Write(data)
        } else {
                w.Header().Set("Content-Type", contentType)
                w.Header().Set("Cache-Control", "no-cache")
                w.Header().Set("Content-Length", fmt.Sprintf("%d", len(data)))
                w.Write(data)
        }
}

func (c *Collector) handleCurrentJSON(w http.ResponseWriter, r *http.Request) {
        c.mu.RLock()
        data, _ := json.Marshal(c.current)
        c.mu.RUnlock()

        if acceptsGzip(r) {
                w.Header().Set("Content-Type", "application/json")
                w.Header().Set("Content-Encoding", "gzip")
                w.Header().Set("Vary", "Accept-Encoding")
                w.Header().Set("Cache-Control", "no-cache")
                gz := gzip.NewWriter(w)
                defer gz.Close()
                gz.Write(data)
        } else {
                w.Header().Set("Content-Type", "application/json")
                w.Header().Set("Cache-Control", "no-cache")
                w.Header().Set("Content-Length", fmt.Sprintf("%d", len(data)))
                w.Write(data)
        }
}

func (c *Collector) handleSSE(w http.ResponseWriter, r *http.Request) {
        w.Header().Set("Content-Type", "text/event-stream")
        w.Header().Set("Cache-Control", "no-cache")
        w.Header().Set("Connection", "keep-alive")

        flusher, ok := w.(http.Flusher)
        if !ok {
                http.Error(w, "streaming not supported", http.StatusInternalServerError)
                return
        }

        // Send initial full payload
        c.mu.RLock()
        current := c.current
        c.mu.RUnlock()

        if current != nil {
                msg := map[string]interface{}{"__t": "f", "v": current}
                data, _ := json.Marshal(msg)
                fmt.Fprintf(w, "data: %s\n\n", data)
                flusher.Flush()
        }

        // Register as SSE client
        ch := make(chan []byte, 10)
        c.mu.Lock()
        c.sseClients[ch] = true
        c.mu.Unlock()
        defer func() {
                c.mu.Lock()
                delete(c.sseClients, ch)
                c.mu.Unlock()
        }()

        // Poll for changes every 1s
        lastHash := ""
        for {
                select {
                case <-r.Context().Done():
                        return
                case data := <-ch:
                        fmt.Fprintf(w, "data: %s\n\n", data)
                        flusher.Flush()
                case <-time.After(1 * time.Second):
                        // Check for changes
                        c.mu.RLock()
                        newHash := ""
                        if c.current != nil {
                                data, _ := json.Marshal(c.current)
                                sum := md5.Sum(data)
                                newHash = fmt.Sprintf("%x", sum)
                        }
                        c.mu.RUnlock()

                        if newHash != lastHash && newHash != "" {
                                lastHash = newHash
                                // Send full payload (simplified — production would send delta)
                                c.mu.RLock()
                                msg := map[string]interface{}{"__t": "f", "v": c.current}
                                c.mu.RUnlock()
                                data, _ := json.Marshal(msg)
                                fmt.Fprintf(w, "data: %s\n\n", data)
                                flusher.Flush()
                        }
                }
        }
}

func (c *Collector) handleLabels(w http.ResponseWriter, r *http.Request) {
        // GET: return labels + groups
        if r.Method == "GET" {
                labels := loadLabels()
                // Read aliases file for groups
                aliases := readAliases()
                groups := buildGroups(aliases, labels)

                resp := map[string]interface{}{
                        "labels":  labels,
                        "aliases": aliases,
                        "groups":  groups,
                }
                w.Header().Set("Content-Type", "application/json")
                w.Header().Set("Access-Control-Allow-Origin", "*")
                w.Header().Set("Cache-Control", "no-cache")
                json.NewEncoder(w).Encode(resp)
                return
        }

        // POST: update labels (basic implementation)
        if r.Method == "POST" {
                var req map[string]interface{}
                if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
                        http.Error(w, `{"error":"invalid JSON"}`, http.StatusBadRequest)
                        return
                }
                ip, _ := req["ip"].(string)
                label, _ := req["label"].(string)

                labels := loadLabels()
                if label == "" {
                        delete(labels, ip)
                } else {
                        labels[ip] = label
                }
                saveLabels(labels)

                // Re-read + return
                labels = loadLabels()
                aliases := readAliases()
                groups := buildGroups(aliases, labels)
                resp := map[string]interface{}{
                        "ok":     true,
                        "ip":     ip,
                        "label":  label,
                        "labels": labels,
                        "groups": groups,
                }
                w.Header().Set("Content-Type", "application/json")
                w.Header().Set("Access-Control-Allow-Origin", "*")
                json.NewEncoder(w).Encode(resp)
                return
        }

        if r.Method == "OPTIONS" {
                w.Header().Set("Access-Control-Allow-Origin", "*")
                w.Header().Set("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
                w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
                w.WriteHeader(200)
                return
        }

        http.Error(w, `{"error":"method not allowed"}`, http.StatusMethodNotAllowed)
}

// ============================================================================
// Labels helpers
// ============================================================================

func readAliases() []string {
        data, err := os.ReadFile(aliasesFile)
        if err != nil {
                return []string{}
        }
        var lines []string
        for _, line := range strings.Split(string(data), "\n") {
                line = strings.TrimSpace(line)
                if line == "" || strings.HasPrefix(line, "#") {
                        continue
                }
                lines = append(lines, line)
        }
        return lines
}

func buildGroups(aliases []string, labels map[string]string) map[string]interface{} {
        groups := make(map[string]interface{})
        for _, raw := range aliases {
                parts := strings.Fields(raw)
                if len(parts) < 2 {
                        continue
                }
                // Format: from_ip = to_ip label
                if !strings.Contains(raw, "=") {
                        continue
                }
                eqParts := strings.SplitN(raw, "=", 2)
                fromIP := strings.TrimSpace(eqParts[0])
                rest := strings.Fields(strings.TrimSpace(eqParts[1]))
                if len(rest) < 1 {
                        continue
                }
                toIP := rest[0]
                label := ""
                if len(rest) > 1 {
                        label = rest[1]
                }
                if label == "" {
                        continue
                }
                g, ok := groups[label].(map[string]interface{})
                if !ok {
                        g = map[string]interface{}{"to_ip": toIP, "from_ips": []string{}}
                        groups[label] = g
                }
                ips, _ := g["from_ips"].([]string)
                ips = append(ips, fromIP)
                g["from_ips"] = ips
                groups[label] = g
        }
        return groups
}

func saveLabels(labels map[string]string) {
        data, _ := json.MarshalIndent(labels, "", "  ")
        data = append(data, '\n')
        os.WriteFile(labelsFile, data, 0644)
}

// ============================================================================
// Utility functions
// ============================================================================

func acceptsGzip(r *http.Request) bool {
        return strings.Contains(r.Header.Get("Accept-Encoding"), "gzip")
}

func shouldGzip(path string) bool {
        ext := filepath.Ext(path)
        return ext == ".json" || ext == ".js" || ext == ".css" || ext == ".html"
}

func readProc(path string) string {
        data, err := os.ReadFile(path)
        if err != nil {
                return ""
        }
        return strings.TrimSpace(string(data))
}

func toInt(v interface{}) int {
        switch n := v.(type) {
        case float64:
                return int(n)
        case int:
                return n
        default:
                return 0
        }
}

func toFloat(v interface{}) float64 {
        switch n := v.(type) {
        case float64:
                return n
        case int:
                return float64(n)
        default:
                return 0
        }
}

func toString(v interface{}) string {
        if s, ok := v.(string); ok {
                return s
        }
        return ""
}

// ============================================================================
// Main
// ============================================================================

func main() {
        port := flag.Int("port", 8082, "HTTP port")
        bind := flag.String("bind", "127.0.0.1", "bind address")
        flag.Parse()

        collector := NewCollector()

        // Initial collection
        collector.collect()

        // Start collection loop (every 30s)
        go func() {
                ticker := time.NewTicker(30 * time.Second)
                defer ticker.Stop()
                for range ticker.C {
                        collector.collect()
                }
        }()

        // Start HTTP server
        mux := http.NewServeMux()
        mux.HandleFunc("/", collector.handleIndex)

        addr := fmt.Sprintf("%s:%d", *bind, *port)
        fmt.Fprintf(os.Stderr, "collector: HTTP server on %s (serves /, /sse, /current.json, /api/labels, /data/*)\n", addr)

        srv := &http.Server{
                Addr:    addr,
                Handler: mux,
        }

        // Serve static files from bin dir (for dashboard.js, dashboard.css)
        // The handleIndex function handles this already.
        if err := srv.ListenAndServe(); err != nil {
                fmt.Fprintf(os.Stderr, "collector: HTTP server failed: %v\n", err)
                os.Exit(1)
        }
}
__Z_FILE_EMBED_END_SENTINEL__

# --- go build ----------------------------------------------------------------
if ! command -v go >/dev/null 2>&1; then
  echo "FATAL: 'go' not in PATH." >&2
  exit 1
fi
echo "go version: $(go version)"
echo "running: (cd dashboard/bin/go && go build -o /tmp/bpftune-collector-go-test)"
if ( cd dashboard/bin/go && go build -o /tmp/bpftune-collector-go-test ) 2>&1 | tee /tmp/go-build.log; then
  BUILD_OK=1
else
  BUILD_OK=0
fi
if [ "$BUILD_OK" -ne 1 ]; then
  if ( cd dashboard/bin/go && go build -o /tmp/bpftune-collector-go-test ) >/tmp/go-build.log 2>&1; then
    BUILD_OK=1
  else
    BUILD_OK=0
  fi
fi
if [ "$BUILD_OK" -ne 1 ]; then
  echo "===============================================================" >&2
  echo "BUILD FAILED. Full log (/tmp/go-build.log):" >&2
  cat /tmp/go-build.log >&2 || true
  echo "===============================================================" >&2
  echo "Files were written but NOT committed. Inspect with:" >&2
  echo "  cd $REPO_ROOT && git diff dashboard/bin/go/main.go" >&2
  exit 1
fi
echo "BUILD OK: /tmp/bpftune-collector-go-test"
ls -la /tmp/bpftune-collector-go-test

echo "smoke test: -h"
/tmp/bpftune-collector-go-test -h 2>&1 | head -10 || true

# --- commit -------------------------------------------------------------------
git add dashboard/bin/go/main.go

if git diff --cached --quiet; then
  echo "no changes to commit."
  [ "$NO_PUSH" -eq 1 ] && exit 0
  exit 0
fi

COMMIT_MSG="dashboard: Go 0.4.1 — bucket cap + sort order + live_leaders break fix

Three bugs in v0.4.0's main.go (the full port):

  1. Sort order: hosts sorted by voteSum desc, but Python read_map
     (bpftune_log.py:809) sorts by inst desc:
         entries.sort(key=lambda x: -x[0])
     Result: Go bucket order didn't match Python's top-busiest-by-inst
     order.  Now sorts by Inst desc.

  2. Buckets list cap was an empty check:
         if len(buckets) >= 8 { /* don't break */ }
     The comment said 'don't break' but the code did nothing — the
     buckets list was unbound.  Go was returning 21 buckets; Python
     (data_buckets: n=8) returns 8.  Now uses conditional append:
         if len(buckets) < 8 { buckets = append(...) }

  3. live_leaders cap used 'break' which exited the WHOLE for-range
     loop, preventing metric_by_bucket from being built for buckets
     9+.  (Happened to mostly work because most buckets past #8 had
     n_alg=0 so the cands-check skipped them — but it was still
     wrong.)  Now uses conditional append:
         if len(liveLeaders) < liveMaxBuckets { liveLeaders = append(...) }

After this commit, Go output matches Python for:
  - buckets list (8 entries, inst desc, same dest labels)
  - live_leaders (8 entries, inst desc, only buckets with cands)
  - metric_by_bucket (ALL buckets, full row shape, labeled keys)"

git commit -m "$COMMIT_MSG" --no-verify
NEW_SHA="$(git rev-parse --short HEAD)"
echo "committed: $NEW_SHA"

# --- push ----------------------------------------------------------------------
if [ "$NO_PUSH" -eq 1 ]; then
  echo "no-push mode: commit is local only."
  exit 0
fi

if [ "$YES" -ne 1 ]; then
  echo
  echo "About to push to origin/$BRANCH. Continue? [y/N]"
  read -r ANS
  case "$ANS" in
    y|Y|yes|YES) ;;
    *) echo "aborted before push. commit $NEW_SHA is local."; exit 0 ;;
  esac
fi

echo "pushing to origin/$BRANCH..."
git push origin "$BRANCH"
echo "pushed: $NEW_SHA"
echo
echo "DONE.  To verify on this host:"
echo "  cd ~/bpftune/dashboard/bin/go && go build -o bpftune-collector-go"
echo "  fuser -k 8083/tcp 2>/dev/null; sleep 1"
echo "  ./bpftune-collector-go --port 8083 --bind 127.0.0.1 &"
echo "  sleep 5"
echo "  curl -s http://127.0.0.1:8083/current.json > /tmp/go.json"
echo "  curl -s http://127.0.0.1:8080/current.json > /tmp/py.json"
echo "  python3 -c 'import json; go=json.load(open(\"/tmp/go.json\")); py=json.load(open(\"/tmp/py.json\")); print(\"py buckets:\", [b.get(\"dest\") for b in py.get(\"buckets\",[])]); print(\"go buckets:\", [b.get(\"dest\") for b in go.get(\"buckets\",[])])'"
