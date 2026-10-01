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
	mu        sync.RWMutex
	current   map[string]interface{} // current.json data
	keyHashes map[string]string      // per-key md5 for SSE delta
	sseClients map[chan []byte]bool
}

func NewCollector() *Collector {
	return &Collector{
		current:    make(map[string]interface{}),
		keyHashes:  make(map[string]string),
		sseClients: make(map[chan []byte]bool),
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
		key := entry
		val := entry
		// bpftool returns {"key": {...}, "value": {...}}
		if k, ok := entry["key"].(map[string]interface{}); ok {
			key = k
		}
		if v, ok := entry["value"].(map[string]interface{}); ok {
			val = v
		}
		// Extract the address from the key
		addr := extractAddr(key)
		if addr == "" {
			continue
		}
		hosts[addr] = val
	}
	return hosts, nil
}

func extractAddr(key map[string]interface{}) string {
	// The BPF map key has u6_addr8 (IPv6, 16 bytes) or a 4-byte IPv4
	if addr, ok := key["dst_addr"].(float64); ok {
		return intToIP(int(addr))
	}
	// Try hex address
	if hex, ok := key["dst_addr6"].(string); ok {
		return "v6:" + hex
	}
	// Try raw bytes
	if bytes, ok := key["dst_addr8"].([]interface{}); ok && len(bytes) >= 4 {
		return fmt.Sprintf("%d.%d.%d.%d", int(bytes[0].(float64)), int(bytes[1].(float64)),
			int(bytes[2].(float64)), int(bytes[3].(float64)))
	}
	return ""
}

func intToIP(n int) string {
	return fmt.Sprintf("%d.%d.%d.%d", (n>>24)&0xFF, (n>>16)&0xFF, (n>>8)&0xFF, n&0xFF)
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
	for addr, raw := range hosts {
		v, ok := raw.(map[string]interface{})
		if !ok {
			continue
		}
		inst := toInt(v["instances"])
		if inst < 2 {
			continue
		}
		buckets = append(buckets, bucket{
			Dest:    labelFor(addr, labels),
			Inst:    inst,
			RttUs:   toFloat(v["min_rtt"]),
			RefMbps: toFloat(v["max_rate_delivered"]) / 125000.0, // B/s -> Mb/s
			BestAlg: toString(v["best_alg"]),
			NAlg:    toInt(v["n_alg"]),
		})
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
		"metric_by_bucket": map[string]interface{}{},
		"bucket_live":    map[string]interface{}{},
		"live_leaders":   []interface{}{},
		"recent_swaps":   []interface{}{},
		"recent_proofs":  []interface{}{},
		"swap_outcomes": map[string]interface{}{
			"composite": map[string]interface{}{
				"measurable": 0, "win": 0, "loss": 0, "null": 0,
			},
		},
		"bucket_ips":    map[string]interface{}{},
		"log_window":    map[string]interface{}{},
		"hostname":      readProc("/proc/sys/kernel/hostname"),
		"now_mono":      now,
	}

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
