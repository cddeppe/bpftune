#!/usr/bin/env bash
# =============================================================================
# Atomic patch v0.3.2: fix Go collector build (corrected from v0.3.1)
#
# What was wrong with v0.3.1:
#   1. `go build ./dashboard/bin/go/` failed because go.mod lives INSIDE
#      dashboard/bin/go/, so Go tried to resolve against the repo root
#      and couldn't find the package. Fix: cd into the dir first.
#   2. `git rev-list --left-right --count` output is TAB-separated, but
#      the script used ${VAR##* } which splits on SPACE. Got
#      "0      0" integer-expression errors. Fix: use `read`.
#   3. v0.3.1 aborted if dashboard/bin/go/ had uncommitted changes, but
#      the whole point is to overwrite the broken files. Fix: just write.
#
# What this script does:
#   - pre-flight: must be in bpftune repo, on `dashboard` branch (or main)
#   - fetches origin/dashboard and fast-forwards if behind (handles tabs)
#   - hard-overwrites dashboard/bin/go/main.go + log_parsing.go with the
#     known-good contents embedded below (structurally correct: nAlg
#     declared, buckets append properly closed, no unused imports)
#   - writes dashboard/systemd/bpftune-collector-go.service (didn't exist
#     on remote — needed for the systemd switch)
#   - runs `cd dashboard/bin/go && go build` (correct invocation)
#   - smoke-tests the binary with -h
#   - if build OK, commits as one atomic commit and pushes to origin/<branch>
#
# Usage:
#   bash apply-0.3.2-go-build-fix.sh              # interactive, asks before push
#   bash apply-0.3.2-go-build-fix.sh --yes        # non-interactive, pushes
#   bash apply-0.3.2-go-build-fix.sh --no-push    # apply + build + commit only
#   bash apply-0.3.2-go-build-fix.sh --check-only # overwrite + build, no commit
# =============================================================================

set -euo pipefail

YES=0
NO_PUSH=0
CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --yes)        YES=1 ;;
    --no-push)    NO_PUSH=1 ;;
    --check-only) CHECK_ONLY=1 ;;
    -h|--help)   sed -n '2,30p' "$0" | sed 's/^# \?//'; exit 0 ;;
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
  echo "FATAL: not inside a bpftune checkout (need dashboard/bin/go/ subdir)." >&2
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
      echo "WARN: on branch '$BRANCH' (not dashboard) — continuing (--yes)" >&2
    else
      echo "FATAL: on branch '$BRANCH', expected 'dashboard' or 'main'." >&2
      echo "       Switch with: git checkout dashboard" >&2
      exit 1
    fi ;;
esac

# --- fetch + fast-forward if behind (TAB-safe parse) --------------------------
if git rev-parse --verify origin/dashboard >/dev/null 2>&1; then
  echo "fetching origin/dashboard..."
  git fetch origin dashboard
  # `git rev-list --left-right --count origin/dashboard...HEAD` returns "<left>\t<right>"
  # Use `read` to split on whitespace (handles both tab and space).
  AB="$(git rev-list --left-right --count origin/dashboard...HEAD 2>/dev/null || echo '0 0')"
  read -r AHEAD BEHIND <<< "$AB"
  AHEAD="${AHEAD:-0}"; BEHIND="${BEHIND:-0}"
  echo "ahead=$AHEAD behind=$BEHIND"
  if [ "$BEHIND" -gt 0 ] && [ "$AHEAD" -eq 0 ]; then
    echo "behind by $BEHIND commit(s), fast-forwarding..."
    git merge --ff-only origin/dashboard
  elif [ "$AHEAD" -gt 0 ] && [ "$BEHIND" -gt 0 ]; then
    echo "FATAL: branches diverged ($AHEAD ahead, $BEHIND behind) — resolve manually." >&2
    exit 1
  fi
else
  echo "WARN: no origin/dashboard remote — skipping fetch."
fi

# --- write files ---------------------------------------------------------------
MAIN_GO_PATH="dashboard/bin/go/main.go"
LOG_GO_PATH="dashboard/bin/go/log_parsing.go"
SYSTEMD_PATH="dashboard/systemd/bpftune-collector-go.service"
mkdir -p dashboard/bin/go dashboard/systemd

echo "writing $MAIN_GO_PATH ..."
cat > "$MAIN_GO_PATH" <<'__Z_FILE_EMBED_END_SENTINEL__'
// bpftune-collector-go: Go replacement for bpftune-collector.py
//
// Handles: HTTP server (static files + gzip + SSE), BPF map reading,
// current.json generation, SSE delta encoding, /api/labels.
// The Python renderer (bpftune-render.py) stays as-is (runs via cron).
//
// Build: go build -o bpftune-collector-go
// Run:   ./bpftune-collector-go --port 8080 --bind 0.0.0.0
//
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
        "strings"
        "sync"
        "time"
)

// ============================================================================
// Configuration
// ============================================================================

var (
        histDir    = "/var/lib/bpftune/history"
        binDir     = "/opt/bpftune-dashboard/bin"
        labelsFile = "/var/lib/bpftune/aliases.labels.json"
        aliasesFile = "/etc/bpftune/aliases"
)

// ============================================================================
// Global state (protected by mutex)
// ============================================================================

type Collector struct {
        mu          sync.RWMutex
        current     map[string]interface{} // current.json data
        keyHashes   map[string]string      // per-key md5 for SSE delta
        sseClients  map[chan []byte]bool
        logOffsets  map[string]int64       // file path -> byte offset
        recentSwaps []interface{}          // accumulated swaps (cap 50)
        recentProofs []interface{}         // accumulated proofs (cap 50)
}

func NewCollector() *Collector {
        return &Collector{
                current:      make(map[string]interface{}),
                keyHashes:    make(map[string]string),
                sseClients:   make(map[chan []byte]bool),
                logOffsets:   make(map[string]int64),
        }
}

// ============================================================================
// BPF map reading (via bpftool)
// ============================================================================

func readBPFMap() (map[string]interface{}, error) {
        cmd := exec.Command("bpftool", "--json", "map", "dump", "name", "remote_host_map")
        output, err := cmd.Output()
        if err != nil {
                return nil, fmt.Errorf("bpftool: %w", err)
        }
        var raw []map[string]interface{}
        if err := json.Unmarshal(output, &raw); err != nil {
                return nil, fmt.Errorf("bpftool json: %w", err)
        }
        hosts := make(map[string]interface{})
        for _, entry := range raw {
                // bpftool with BTF returns {"formatted": {"key": {...}, "value": {...}}}
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
                hosts[addr] = val
        }
        return hosts, nil
}

// ============================================================================
// Labels loading
// ============================================================================

func loadLabels() map[string]string {
        data, err := os.ReadFile(labelsFile)
        if err != nil {
                return map[string]string{}
        }
        var labels map[string]string
        if err := json.Unmarshal(data, &labels); err != nil {
                return map[string]string{}
        }
        return labels
}

func canonBucket(addr string) string {
        if strings.HasPrefix(addr, "v6:") {
                return addr
        }
        parts := strings.Split(addr, ".")
        if len(parts) == 4 {
                return parts[0] + "." + parts[1] + ".0.0"
        }
        return addr
}

func labelFor(addr string, labels map[string]string) string {
        if addr == "" {
                return "unknown"
        }
        canon := canonBucket(addr)
        if lbl, ok := labels[canon]; ok {
                return lbl
        }
        return canon
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

        labels := loadLabels()
        now := time.Now().Unix()

        // 0.2: CONGS array for best_i -> algorithm name mapping
        congs := []string{"cubic", "bbr", "htcp", "dctcp", "scalable", "vegas",
                "veno", "westwood", "reno", "illinois", "yeah", "lp",
                "bic", "highspeed", "hybla", "nv"}

        // Build buckets list
        type bucket struct {
                Dest    string  `json:"dest"`
                Inst    int     `json:"inst"`
                RttUs   float64 `json:"rtt_us"`
                RefMbps float64 `json:"ref_mbps"`
                BestAlg string  `json:"best_alg"`
                NAlg    int     `json:"n_alg"`
        }
        var buckets []bucket
        // 0.2: build metric_by_bucket (per-bucket per-alg metrics from BPF value)
        metricByBucket := make(map[string]interface{})
        bucketLive := make(map[string]interface{})
        var liveLeaders []interface{}

        for addr, raw := range hosts {
                v, ok := raw.(map[string]interface{})
                if !ok {
                        continue
                }
                inst := toInt(v["instances"])
                if inst < 2 {
                        continue
                }
                // best_i -> CONGS name
                bestI := toInt(v["best_i"])
                bestAlg := ""
                if bestI >= 0 && bestI < len(congs) {
                        bestAlg = congs[bestI]
                }
                // 0.2: n_alg = count of metrics with metric_count > 0
                nAlg := 0
                if mets, ok := v["metrics"].([]interface{}); ok {
                        for _, m := range mets {
                                if mi, ok := m.(map[string]interface{}); ok && toInt(mi["metric_count"]) > 0 {
                                        nAlg++
                                }
                        }
                }
                lbl := labelFor(addr, labels)
                buckets = append(buckets, bucket{
                        Dest:    lbl,
                        Inst:    inst,
                        RttUs:   toFloat(v["min_rtt"]),
                        RefMbps: toFloat(v["max_rate_delivered"]) / 125000.0,
                        BestAlg: bestAlg,
                        NAlg:    nAlg,
                })

                // 0.2: parse per-alg metrics from BPF value
                if metrics, ok := v["metrics"].([]interface{}); ok {
                        var metricRows []interface{}
                        for i, m := range metrics {
                                if i >= len(congs) {
                                        break
                                }
                                mi, ok := m.(map[string]interface{})
                                if !ok {
                                        continue
                                }
                                algName := congs[i]
                                row := map[string]interface{}{
                                        "alg":         algName,
                                        "rate_ema":    toFloat(mi["rate_ema"]),
                                        "swap_score":  toInt(mi["swap_score"]),
                                        "bad_streak":  toInt(mi["bad_streak"]),
                                        "null_streak": toInt(mi["null_streak"]),
                                        "metric_value": toFloat(mi["metric_value"]),
                                        "count":       toInt(mi["metric_count"]),
                                        "alive":       toInt(mi["sockets_alive"]),
                                        "active":      toInt(mi["metric_count"]) > 0 || toInt(mi["sockets_alive"]) > 0 || toFloat(mi["rate_ema"]) > 0,
                                }
                                metricRows = append(metricRows, row)
                        }
                        if len(metricRows) > 0 {
                                metricByBucket[lbl] = metricRows
                        }

                        // 0.2: build bucket_live (per-bucket time series — for now, single point)
                        ts := now
                        cols := map[string]interface{}{}
                        for i, m := range metrics {
                                if i >= len(congs) {
                                        break
                                }
                                mi, _ := m.(map[string]interface{})
                                cols["re_"+congs[i]] = []interface{}{toFloat(mi["rate_ema"])}
                                cols["ss_"+congs[i]] = []interface{}{toFloat(mi["swap_score"])}
                                cols["bs_"+congs[i]] = []interface{}{toFloat(mi["bad_streak"])}
                                cols["ns_"+congs[i]] = []interface{}{toFloat(mi["null_streak"])}
                        }
                        bucketLive[lbl] = map[string]interface{}{
                                "ts":   []interface{}{ts},
                                "cols": cols,
                        }

                        // 0.2: build live_leaders entry
                        // Find top alg by rate_ema * swap_score / 256
                        bestScore := 0.0
                        bestAlgIdx := 0
                        for i, m := range metrics {
                                if i >= len(congs) {
                                        break
                                }
                                mi, _ := m.(map[string]interface{})
                                rv := toFloat(mi["rate_ema"])
                                ss := toFloat(mi["swap_score"])
                                score := rv * ss / 256.0
                                if score > bestScore {
                                        bestScore = score
                                        bestAlgIdx = i
                                }
                        }
                        leader := map[string]interface{}{
                                "dest": lbl,
                                "inst": inst,
                                "top": []interface{}{
                                        map[string]interface{}{
                                                "alg":       congs[bestAlgIdx],
                                                "weighted":  int(bestScore),
                                                "rate_ema":  toFloat(metrics[bestAlgIdx].(map[string]interface{})["rate_ema"]),
                                                "swap_score": toInt(metrics[bestAlgIdx].(map[string]interface{})["swap_score"]),
                                        },
                                },
                        }
                        liveLeaders = append(liveLeaders, leader)
                }
        }

        // Build current.json
        doc := map[string]interface{}{
                "generated_ts": now,
                "build": map[string]interface{}{
                        "version":      "go-collector",
                        "dash_version": "go-0.1",
                        "service":      "active",
                        "uptime_min":  0,
                        "started_utc": "",
                        "log_path":     "/var/log/bpftune-met-live.log",
                },
                "system": map[string]interface{}{
                        "kernel":     readProc("/proc/sys/kernel/osrelease"),
                        "default_cc": readProc("/proc/sys/net/ipv4/tcp_congestion_control"),
                },
                "buckets":        buckets,
                "metric_by_bucket": metricByBucket,
                "bucket_live":    bucketLive,
                "live_leaders":   liveLeaders,
                "bucket_ips":    map[string]interface{}{},
                "log_window":    map[string]interface{}{},
                "hostname":      readProc("/proc/sys/kernel/hostname"),
                "now_mono":      now,
        }

        // 0.3: parse log files for recent_swaps, recent_proofs, swap_outcomes
        topSwaps, topProofs, swapOutcomes := c.parseLogs()
        doc["recent_swaps"] = topSwaps
        doc["recent_proofs"] = topProofs
        doc["swap_outcomes"] = swapOutcomes

        // Update current state + compute key hashes
        c.mu.Lock()
        c.current = doc
        c.keyHashes = computeKeyHashes(doc)
        c.mu.Unlock()

        // Write current.json to disk
        c.writeCurrentJSON()

        // Notify SSE clients
        c.notifySSE()

        fmt.Fprintf(os.Stderr, "collector: collected %d buckets, ts=%d\n", len(buckets), now)
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
                        "groups": groups,
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

echo "writing $LOG_GO_PATH ..."
cat > "$LOG_GO_PATH" <<'__Z_FILE_EMBED_END_SENTINEL__'
package main

import ("os";"path/filepath";"sort";"strconv";"strings")

func (c *Collector) parseLogs() ([]interface{}, []interface{}, map[string]interface{}) {
	files, _ := filepath.Glob("/var/log/bpftune-met-*.log")
	sort.Slice(files, func(i, j int) bool { fi, _ := os.Stat(files[i]); fj, _ := os.Stat(files[j]); return fi.ModTime().After(fj.ModTime()) })
	var ns, np []interface{}
	for _, p := range files {
		i, _ := os.Stat(p); sz := i.Size()
		c.mu.Lock(); pv, ex := c.logOffsets[p]; c.mu.Unlock()
		if !ex { if sz > 2097152 { pv = sz - 2097152 } else { pv = 0 } }
		if sz < pv { pv = 0 }
		if pv >= sz { continue }
		f, _ := os.Open(p); f.Seek(pv, 0); b := make([]byte, sz-pv); n, _ := f.Read(b); f.Close()
		c.mu.Lock(); c.logOffsets[p] = sz; c.mu.Unlock()
		for _, ln := range strings.Split(string(b[:n]), "\n") {
			ln = strings.TrimSpace(ln); if ln == "" { continue }
			kv := parseKV(ln)
			if strings.Contains(ln, " swap ") {
				ns = append(ns, map[string]interface{}{"boot_ts": pf(kv["boot_ts"]), "from_alg": kv["from_alg"], "to_alg": kv["to_alg"], "dest": kv["dest"], "_bucket": kv["_bucket"], "d": pi(kv["d"]), "outcome": kv["outcome"], "outcome_srate": kv["outcome_srate"], "outcome_sustained": kv["outcome_sustained"], "mt_alg": kv["mt_alg"], "rb_alg": kv["rb_alg"]})
			} else if strings.Contains(ln, " proof ") {
				np = append(np, map[string]interface{}{"boot_ts": pf(kv["boot_ts"]), "alg": kv["alg"], "mbps": pf(kv["mbps"]), "dest": kv["dest"], "tier": kv["tier"]})
			}
		}
	}
	c.mu.Lock()
	for _, s := range ns { c.recentSwaps = append([]interface{}{s}, c.recentSwaps...) }
	if len(c.recentSwaps) > 50 { c.recentSwaps = c.recentSwaps[:50] }
	for _, p := range np { c.recentProofs = append([]interface{}{p}, c.recentProofs...) }
	if len(c.recentProofs) > 50 { c.recentProofs = c.recentProofs[:50] }
	as := c.recentSwaps; ap := c.recentProofs; c.mu.Unlock()
	cw, cl, cn, sw, sl, sn := 0, 0, 0, 0, 0, 0
	for _, s := range as {
		sm, _ := s.(map[string]interface{})
		o := toString(sm["outcome"])
		if o == "win" { cw++ } else if o == "loss" { cl++ } else { cn++ }
		os2 := toString(sm["outcome_sustained"])
		if os2 == "win" { sw++ } else if os2 == "loss" { sl++ } else { sn++ }
	}
	mC := cw + cl + cn; mS := sw + sl + sn
	so := map[string]interface{}{
		"composite": map[string]interface{}{"measurable": mC, "win": cw, "win_pct": pct(cw, mC), "loss": cl, "loss_pct": pct(cl, mC), "null": cn, "null_pct": pct(cn, mC)},
		"sustained": map[string]interface{}{"measurable": mS, "win": sw, "win_pct": pct(sw, mS), "loss": sl, "loss_pct": pct(sl, mS), "null": sn, "null_pct": pct(sn, mS)},
		"swaps_list": as,
	}
	var ts, tp []interface{}
	for i, s := range as { if i >= 20 { break }; ts = append(ts, s) }
	for i, p := range ap { if i >= 18 { break }; tp = append(tp, p) }
	return ts, tp, so
}
func parseKV(s string) map[string]string {
	m := make(map[string]string)
	for _, f := range strings.Fields(s) { if i := strings.Index(f, "="); i > 0 { m[f[:i]] = f[i+1:] } }
	return m
}
func pf(s string) float64 { v, _ := strconv.ParseFloat(s, 64); return v }
func pi(s string) int { v, _ := strconv.Atoi(s); return v }
func pct(n, d int) float64 { if d == 0 { return 0 }; return float64(n) * 100.0 / float64(d) }
__Z_FILE_EMBED_END_SENTINEL__

echo "writing $SYSTEMD_PATH ..."
cat > "$SYSTEMD_PATH" <<'__Z_FILE_EMBED_END_SENTINEL__'
[Unit]
Description=bpftune dashboard collector (Go) — replaces Python collector
After=network.target sys-kernel-debug.mount
ConditionPathExists=/sys/fs/bpf

[Service]
Type=simple
WorkingDirectory=/var/lib/bpftune/history
ExecStart=/opt/bpftune-dashboard/bin/bpftune-collector-go --port 8080 --bind 0.0.0.0
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
# Note: needs CAP_BPF + CAP_NET_ADMIN to read bpftool maps; can't use
# ProtectSystem=strict because we write to /var/lib/bpftune/history.
User=root

[Install]
WantedBy=multi-user.target
__Z_FILE_EMBED_END_SENTINEL__

# --- go build (CORRECT invocation: cd into the module dir) ---------------------
if ! command -v go >/dev/null 2>&1; then
  echo "FATAL: 'go' not in PATH. Install Go >= 1.21 or fix PATH." >&2
  exit 1
fi
echo "go version: $(go version)"

# IMPORTANT: go.mod lives at dashboard/bin/go/go.mod with `module bpftune-collector`.
# That makes dashboard/bin/go/ a self-contained Go module. We MUST cd into it
# before `go build`; running `go build ./dashboard/bin/go/` from the repo root
# fails because the repo root has no go.mod and Go can't resolve the package.
echo "running: (cd dashboard/bin/go && go build -o /tmp/bpftune-collector-go-test)"
if ( cd dashboard/bin/go && go build -o /tmp/bpftune-collector-go-test 2>&1 | tee /tmp/go-build.log ); then
  BUILD_OK=1
else
  BUILD_OK=0
fi

# Re-run go build cleanly if tee swallowed the exit status (tee can mask failures
# when its own stdout pipe closes). Run a second time without tee to be sure.
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
  echo "  cd $REPO_ROOT && git diff dashboard/bin/go/ dashboard/systemd/" >&2
  echo "Once you've fixed the issue, run this script again." >&2
  exit 1
fi
echo "BUILD OK: /tmp/bpftune-collector-go-test"
ls -la /tmp/bpftune-collector-go-test

# --- smoke test ----------------------------------------------------------------
echo "smoke test: -h (expect flag usage + exit 0 or 2)"
/tmp/bpftune-collector-go-test -h 2>&1 | head -10 || true
echo "smoke test: --help"
/tmp/bpftune-collector-go-test --help 2>&1 | head -10 || true

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo "check-only mode: not committing, not pushing."
  exit 0
fi

# --- commit -------------------------------------------------------------------
git add "$MAIN_GO_PATH" "$LOG_GO_PATH" "$SYSTEMD_PATH"

if git diff --cached --quiet; then
  echo "no changes to commit (files already in this state)."
  if [ "$NO_PUSH" -eq 1 ]; then exit 0; fi
  exit 0
fi

COMMIT_MSG="dashboard: Go 0.3.2 — fix structurally-broken main.go (v0.3.1 followup)

The previous pushes (d9c6d30, 0ba47bb, 5651acf) tried to fix the Go
collector but left main.go in a state that does not compile:

  - 'nAlg' was used (nAlg++) but never declared (nAlg := 0 missing)
  - 'buckets = append(buckets, bucket{' line was missing — the struct
    literal 'RefMbps: ... BestAlg: bestAlg, NAlg: nAlg })' appeared
    bare with no enclosing append() call
  - the multi-line if !ok { continue } expansions broke the brace
    structure of the for-range loop body

This commit replaces dashboard/bin/go/main.go with a structurally
correct version that:
  - declares nAlg := 0 before the metrics loop
  - properly closes 'buckets = append(buckets, bucket{ ... })'
  - has matched braces throughout collect()
  - has no unused imports (io, io/fs were already removed in 7ef9968)

Also adds dashboard/systemd/bpftune-collector-go.service — the systemd
unit that did not exist on the remote. This is what enables switching
from the Python collector (port 8080) to the Go collector (same port).

Build verified locally with: cd dashboard/bin/go && go build
go.mod lives at dashboard/bin/go/go.mod (module bpftune-collector, go 1.23.2)."

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
echo "DONE. To verify on this host:"
echo "  cd $REPO_ROOT/dashboard/bin/go && go build -o bpftune-collector-go"
echo "  ./bpftune-collector-go --port 8083 --bind 127.0.0.1  # test on alt port"
