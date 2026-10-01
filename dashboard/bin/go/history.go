package main

// In-memory ring buffer for historical time-series data.
// Replaces the Python renderer (bpftune-render.py) + SQLite + CSV.
//
// Architecture:
//   - Every 30s collect cycle: capture a bucketSnapshot per bucket
//   - Multi-level ring buffer:
//       raw:     last 2h at 30s intervals (240 entries)  → serves 1h range
//       bin5m:   last 24h at 5min intervals (288 entries) → serves 24h range
//       bin1h:   last 7d at 1h intervals (168 entries)   → serves 7d range
//       bin6h:   last 30d at 6h intervals (120 entries)   → serves "all" range
//   - HTTP handlers serve /data/bucket_<id>.json, /data/meta.json,
//     /data/swaps.json, /data/fleet.json dynamically from the ring buffer
//
// The dashboard fetches these files on page load + bucket change.
// The Python renderer cron is no longer needed once this is running.
//
// On restart, the ring buffer starts empty. Charts fill in over time:
//   1h chart: fills in within 2h (first 30s has 1 point, 2h has 240 points)
//   24h chart: fills in within 24h (first 5min has 1 bin, 24h has 288 bins)
//   7d chart: fills in within 7d (first 1h has 1 bin, 7d has 168 bins)
//   all chart: fills in within 30d (first 6h has 1 bin, 30d has 120 bins)

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// ============================================================================
// Snapshot types
// ============================================================================

type bucketSnapshot struct {
	Ts         int64              `json:"ts"` // wall-clock collected_ts
	BestAlg    string             `json:"best_alg"`
	BestI      int                `json:"best_i"`
	Instances  int                `json:"instances"`
	RefRate    float64            `json:"ref_rate"`
	MinRtt     float64            `json:"min_rtt"`
	RateBestI  int                `json:"rate_best_i"`
	RateBestV  float64            `json:"rate_best_v"`
	TcpRmemMax int                `json:"tcp_rmem_max"`
	Re         map[string]float64 `json:"re"` // alg → rate_ema
	Ss         map[string]int     `json:"ss"` // alg → swap_score
	Bs         map[string]int     `json:"bs"` // alg → bad_streak
	Ns         map[string]int     `json:"ns"` // alg → null_streak
	Mv         map[string]float64 `json:"mv"` // alg → metric_value
}

// historyStore is the multi-level ring buffer.
type historyStore struct {
	mu         sync.RWMutex
	raw        map[string][]bucketSnapshot // bucketID → last 240 snapshots (2h)
	bin5m      map[string][]bucketSnapshot // bucketID → last 288 5-min bins (24h)
	bin1h      map[string][]bucketSnapshot // bucketID → last 168 1-hour bins (7d)
	bin6h      map[string][]bucketSnapshot // bucketID → last 120 6-hour bins (30d)
	cycleCount int
}

var hist = &historyStore{
	raw:   map[string][]bucketSnapshot{},
	bin5m: map[string][]bucketSnapshot{},
	bin1h: map[string][]bucketSnapshot{},
	bin6h: map[string][]bucketSnapshot{},
}

// ============================================================================
// addSnapshot — called every 30s collect cycle
// ============================================================================

func (h *historyStore) addSnapshot(bucketID string, snap bucketSnapshot) {
	h.mu.Lock()
	defer h.mu.Unlock()

	// Add to raw (cap at 240 = 2h at 30s)
	h.raw[bucketID] = append(h.raw[bucketID], snap)
	if len(h.raw[bucketID]) > 240 {
		h.raw[bucketID] = h.raw[bucketID][len(h.raw[bucketID])-240:]
	}

	h.cycleCount++

	// Every 10 cycles (5 min): aggregate raw into a 5-min bin
	if h.cycleCount%10 == 0 {
		bin := aggregateSnapshots(h.raw[bucketID])
		h.bin5m[bucketID] = append(h.bin5m[bucketID], bin)
		if len(h.bin5m[bucketID]) > 288 {
			h.bin5m[bucketID] = h.bin5m[bucketID][len(h.bin5m[bucketID])-288:]
		}
	}

	// Every 120 cycles (1 hour): aggregate 5-min bins into a 1-hour bin
	if h.cycleCount%120 == 0 {
		bin := aggregateSnapshots(h.bin5m[bucketID])
		h.bin1h[bucketID] = append(h.bin1h[bucketID], bin)
		if len(h.bin1h[bucketID]) > 168 {
			h.bin1h[bucketID] = h.bin1h[bucketID][len(h.bin1h[bucketID])-168:]
		}
	}

	// Every 720 cycles (6 hours): aggregate 1-hour bins into a 6-hour bin
	if h.cycleCount%720 == 0 {
		bin := aggregateSnapshots(h.bin1h[bucketID])
		h.bin6h[bucketID] = append(h.bin6h[bucketID], bin)
		if len(h.bin6h[bucketID]) > 120 {
			h.bin6h[bucketID] = h.bin6h[bucketID][len(h.bin6h[bucketID])-120:]
		}
	}
}

// aggregateSnapshots averages a list of snapshots into a single bin.
func aggregateSnapshots(snaps []bucketSnapshot) bucketSnapshot {
	if len(snaps) == 0 {
		return bucketSnapshot{}
	}
	if len(snaps) == 1 {
		return snaps[0]
	}
	// Average all numeric fields; keep the last snapshot's values for
	// non-numeric fields (best_alg, best_i, etc.)
	out := bucketSnapshot{
		BestAlg:    snaps[len(snaps)-1].BestAlg,
		BestI:      snaps[len(snaps)-1].BestI,
		TcpRmemMax: snaps[len(snaps)-1].TcpRmemMax,
		Re:         map[string]float64{},
		Ss:         map[string]int{},
		Bs:         map[string]int{},
		Ns:         map[string]int{},
		Mv:         map[string]float64{},
	}
	var sumTs int64
	var sumInst, sumRBI int
	var sumRR, sumMinRtt, sumRBV float64
	for _, s := range snaps {
		sumTs += s.Ts
		sumInst += s.Instances
		sumRBI += s.RateBestI
		sumRR += s.RefRate
		sumMinRtt += s.MinRtt
		sumRBV += s.RateBestV
		for alg, v := range s.Re {
			out.Re[alg] += v
		}
		for alg, v := range s.Ss {
			out.Ss[alg] += v
		}
		for alg, v := range s.Bs {
			out.Bs[alg] += v
		}
		for alg, v := range s.Ns {
			out.Ns[alg] += v
		}
		for alg, v := range s.Mv {
			out.Mv[alg] += v
		}
	}
	n := len(snaps)
	out.Ts = sumTs / int64(n)
	out.Instances = sumInst / n
	out.RateBestI = sumRBI / n
	out.RefRate = sumRR / float64(n)
	out.MinRtt = sumMinRtt / float64(n)
	out.RateBestV = sumRBV / float64(n)
	for alg, v := range out.Re {
		out.Re[alg] = v / float64(n)
	}
	for alg, v := range out.Ss {
		out.Ss[alg] = v / n
	}
	for alg, v := range out.Bs {
		out.Bs[alg] = v / n
	}
	for alg, v := range out.Ns {
		out.Ns[alg] = v / n
	}
	for alg, v := range out.Mv {
		out.Mv[alg] = v / float64(n)
	}
	return out
}

// ============================================================================
// RANGES — mirrors Python renderer RANGES dict
// ============================================================================

// sortedAlgs returns CONGS in alphabetical order (matches Python renderer).
func sortedAlgs() []string {
	out := make([]string, len(CONGS))
	copy(out, CONGS)
	sort.Strings(out)
	return out
}

var ranges = map[string][2]interface{}{
	"1h":  {3600, 60},        // span=3600s, bin_width=60s
	"24h": {86400, 300},      // span=86400s, bin_width=300s (5 min)
	"7d":  {7 * 86400, 3600}, // span=604800s, bin_width=3600s (1 hour)
	"all": {nil, 21600},      // span=None (unlimited), bin_width=21600s (6 hours)
}

// ============================================================================
// HTTP handler: /data/bucket_<id>.json
// ============================================================================

func (h *historyStore) handleBucketJSON(w http.ResponseWriter, r *http.Request, bucketID string) {
	h.mu.RLock()
	defer h.mu.RUnlock()

	// Sanitize bucketID (for potential disk caching, not used yet)
	_ = sanitizeBucketID(bucketID)

	doc := map[string]interface{}{
		"id":     bucketID,
		"series": map[string]interface{}{},
	}

	// For each range, build the time-series
	for rngName, rngCfg := range ranges {
		span, _ := rngCfg[0].(int)
		width, _ := rngCfg[1].(int)
		if width == 0 {
			width = 60
		}

		// Pick the right data source for this range
		var snaps []bucketSnapshot
		switch rngName {
		case "1h":
			snaps = h.raw[bucketID]
			if span > 0 {
				// Filter to last `span` seconds
				cutoff := time.Now().Unix() - int64(span)
				var filtered []bucketSnapshot
				for _, s := range snaps {
					if s.Ts >= cutoff {
						filtered = append(filtered, s)
					}
				}
				snaps = filtered
			}
		case "24h":
			snaps = h.bin5m[bucketID]
			if span > 0 {
				cutoff := time.Now().Unix() - int64(span)
				var filtered []bucketSnapshot
				for _, s := range snaps {
					if s.Ts >= cutoff {
						filtered = append(filtered, s)
					}
				}
				snaps = filtered
			}
		case "7d":
			// v0.5.1: read from CSV (preserves all 30s data, cached 5 min)
			snaps = readBucketCSV(bucketID, int64(span))
		case "all":
			// v0.5.1: read from CSV (all historical data, cached 5 min)
			snaps = readBucketCSV(bucketID, 0)
		}

		// Build the series: ts array + per-alg arrays
		series := map[string]interface{}{
			"ts": []int64{},
		}
		// Initialize per-alg arrays
		for _, alg := range CONGS {
			series["re_"+alg] = []interface{}{}
			series["ss_"+alg] = []interface{}{}
			series["bs_"+alg] = []interface{}{}
			series["ns_"+alg] = []interface{}{}
			series["mv_"+alg] = []interface{}{}
		}

		// Group snaps into bins of `width` seconds
		type bin struct {
			ts    int64
			snaps []bucketSnapshot
		}
		binMap := map[int64]*bin{}
		for _, s := range snaps {
			binIdx := s.Ts / int64(width)
			if b, ok := binMap[binIdx]; ok {
				b.snaps = append(b.snaps, s)
			} else {
				binMap[binIdx] = &bin{ts: binIdx*int64(width) + int64(width)/2, snaps: []bucketSnapshot{s}}
			}
		}

		// Sort bins by timestamp
		var binIdxs []int64
		for bi := range binMap {
			binIdxs = append(binIdxs, bi)
		}
		sort.Slice(binIdxs, func(i, j int) bool { return binIdxs[i] < binIdxs[j] })

		// Build output arrays
		tsArr := make([]int64, 0, len(binIdxs))
		reArrs := map[string][]interface{}{}
		ssArrs := map[string][]interface{}{}
		bsArrs := map[string][]interface{}{}
		nsArrs := map[string][]interface{}{}
		mvArrs := map[string][]interface{}{}
		for _, alg := range CONGS {
			reArrs["re_"+alg] = make([]interface{}, 0, len(binIdxs))
			ssArrs["ss_"+alg] = make([]interface{}, 0, len(binIdxs))
			bsArrs["bs_"+alg] = make([]interface{}, 0, len(binIdxs))
			nsArrs["ns_"+alg] = make([]interface{}, 0, len(binIdxs))
			mvArrs["mv_"+alg] = make([]interface{}, 0, len(binIdxs))
		}
		for _, bi := range binIdxs {
			b := binMap[bi]
			tsArr = append(tsArr, b.ts)
			avg := aggregateSnapshots(b.snaps)
			for _, alg := range CONGS {
				reArrs["re_"+alg] = append(reArrs["re_"+alg], avg.Re[alg])
				ssArrs["ss_"+alg] = append(ssArrs["ss_"+alg], avg.Ss[alg])
				bsArrs["bs_"+alg] = append(bsArrs["bs_"+alg], avg.Bs[alg])
				nsArrs["ns_"+alg] = append(nsArrs["ns_"+alg], avg.Ns[alg])
				mvArrs["mv_"+alg] = append(mvArrs["mv_"+alg], avg.Mv[alg])
			}
		}
		series["ts"] = tsArr
		for k, v := range reArrs {
			series[k] = v
		}
		for k, v := range ssArrs {
			series[k] = v
		}
		for k, v := range bsArrs {
			series[k] = v
		}
		for k, v := range nsArrs {
			series[k] = v
		}
		for k, v := range mvArrs {
			series[k] = v
		}

		doc["series"].(map[string]interface{})[rngName] = series
	}

	// last snapshot (for the "last" field)
	if raw := h.raw[bucketID]; len(raw) > 0 {
		last := raw[len(raw)-1]
		reMap := map[string]interface{}{}
		for alg, v := range last.Re {
			reMap[alg] = v
		}
		doc["last"] = map[string]interface{}{
			"collected_ts": last.Ts,
			"best_alg":     last.BestAlg,
			"best_i":       last.BestI,
			"instances":    last.Instances,
			"ref_rate":     last.RefRate,
			"min_rtt":      last.MinRtt,
			"rate_best_i":  last.RateBestI,
			"rate_best_v":  last.RateBestV,
			"tcp_rmem_max": last.TcpRmemMax,
			"re":           reMap,
		}
	}

	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-cache")
	json.NewEncoder(w).Encode(doc)
}

// ============================================================================
// HTTP handler: /data/meta.json
// ============================================================================

func (h *historyStore) handleMetaJSON(w http.ResponseWriter, r *http.Request, buckets []map[string]interface{}) {
	h.mu.RLock()
	defer h.mu.RUnlock()

	type entry struct {
		id            string
		label         string
		instancesMean float64
		lastTs        int64
	}
	var entries []entry
	for _, b := range buckets {
		id, _ := b["dest"].(string)
		if id == "" {
			continue
		}
		inst, _ := b["inst"].(int)
		label := id // already labeled by readBPFMap
		var instSum int
		var instCount int
		var lastTs int64
		if raw, ok := h.raw[id]; ok && len(raw) > 0 {
			for _, s := range raw {
				instSum += s.Instances
				instCount++
				if s.Ts > lastTs {
					lastTs = s.Ts
				}
			}
		}
		var instMean float64
		if instCount > 0 {
			instMean = float64(instSum) / float64(instCount)
		} else {
			instMean = float64(inst)
		}
		entries = append(entries, entry{id, label, instMean, lastTs})
	}
	// Sort by (instances_mean, last_ts) descending
	sort.Slice(entries, func(i, j int) bool {
		if entries[i].instancesMean != entries[j].instancesMean {
			return entries[i].instancesMean > entries[j].instancesMean
		}
		return entries[i].lastTs > entries[j].lastTs
	})

	bucketEntries := make([]interface{}, 0, len(entries))
	var defaultBucket string
	for _, e := range entries {
		bucketEntries = append(bucketEntries, map[string]interface{}{
			"id":             e.id,
			"label":          e.label,
			"points":          len(h.raw[e.id]),
			"instances_mean": e.instancesMean,
			"last_ts":        e.lastTs,
		})
	}
	if len(entries) > 0 {
		defaultBucket = entries[0].id
	} else {
		defaultBucket = "all"
	}

	rngList := []string{"1h", "24h", "7d", "all"}
	doc := map[string]interface{}{
		"generated_ts":   time.Now().Unix(),
		"ranges":         rngList,
		"algs":           CONGS,
		"buckets":        bucketEntries,
		"default_bucket": defaultBucket,
		"has_tcp_rmem":   false, // TODO: detect from BPF map
	}

	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-cache")
	json.NewEncoder(w).Encode(doc)
}

// ============================================================================
// HTTP handler: /data/swaps.json
// ============================================================================

func (h *historyStore) handleSwapsJSON(w http.ResponseWriter, r *http.Request, swapOutcomes map[string]interface{}) {
	// Forward the swap_outcomes from current.json (already computed by parseLogs)
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-cache")
	json.NewEncoder(w).Encode(swapOutcomes)
}

// ============================================================================
// HTTP handler: /data/fleet.json
// ============================================================================

func (h *historyStore) handleFleetJSON(w http.ResponseWriter, r *http.Request, buckets []map[string]interface{}) {
	h.mu.RLock()
	defer h.mu.RUnlock()

	type pair struct {
		bid string
		cov float64
	}
	var pairs []pair
	for _, b := range buckets {
		id, _ := b["dest"].(string)
		if id == "" {
			continue
		}
		// Coverage = fraction of 5-min bins in last 24h with rate_best_v > 0
		snaps := readBucketCSV(id, 86400)
		if len(snaps) == 0 {
			continue
		}
		var have, seen int
		for _, s := range snaps {
			seen++
			if s.RefRate > 0 {
				have++
			}
		}
		if seen == 0 {
			continue
		}
		pairs = append(pairs, pair{id, round1(float64(have) * 100.0 / float64(seen))})
	}
	sort.Slice(pairs, func(i, j int) bool { return pairs[i].cov > pairs[j].cov })
	if len(pairs) > 25 {
		pairs = pairs[:25]
	}
	labels := make([]string, len(pairs))
	cov := make([]float64, len(pairs))
	for i, p := range pairs {
		labels[i] = p.bid
		cov[i] = p.cov
	}
	doc := map[string]interface{}{
		"buckets":      labels,
		"coverage_24h": cov,
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-cache")
	json.NewEncoder(w).Encode(doc)
}

// ============================================================================
// Disk persistence — save/load ring buffer to survive restarts
// ============================================================================

func (h *historyStore) saveToDisk() {
	h.mu.RLock()
	defer h.mu.RUnlock()

	path := filepath.Join(histDir, "collector-go-history.json")
	tmp := path + ".tmp"

	// Build a compact representation
	type persistData struct {
		Raw   map[string][]bucketSnapshot `json:"raw"`
		Bin5m map[string][]bucketSnapshot `json:"bin5m"`
		Bin1h map[string][]bucketSnapshot `json:"bin1h"`
		Bin6h map[string][]bucketSnapshot `json:"bin6h"`
	}
	data := persistData{
		Raw:   h.raw,
		Bin5m: h.bin5m,
		Bin1h: h.bin1h,
		Bin6h: h.bin6h,
	}
	out, err := json.Marshal(data)
	if err != nil {
		return
	}
	if err := os.WriteFile(tmp, out, 0644); err != nil {
		return
	}
	os.Rename(tmp, path)
}

func (h *historyStore) loadFromDisk() {
	h.mu.Lock()
	defer h.mu.Unlock()

	path := filepath.Join(histDir, "collector-go-history.json")
	data, err := os.ReadFile(path)
	if err != nil {
		return // file doesn't exist yet — fresh start
	}
	type persistData struct {
		Raw   map[string][]bucketSnapshot `json:"raw"`
		Bin5m map[string][]bucketSnapshot `json:"bin5m"`
		Bin1h map[string][]bucketSnapshot `json:"bin1h"`
		Bin6h map[string][]bucketSnapshot `json:"bin6h"`
	}
	var pd persistData
	if err := json.Unmarshal(data, &pd); err != nil {
		return
	}
	if pd.Raw != nil {
		h.raw = pd.Raw
	}
	if pd.Bin5m != nil {
		h.bin5m = pd.Bin5m
	}
	if pd.Bin1h != nil {
		h.bin1h = pd.Bin1h
	}
	if pd.Bin6h != nil {
		h.bin6h = pd.Bin6h
	}
}

// ============================================================================
// Helpers
// ============================================================================

func sanitizeBucketID(id string) string {
	var b strings.Builder
	for _, c := range id {
		if (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' {
			b.WriteRune(c)
		} else {
			b.WriteByte('_')
		}
	}
	return b.String()
}

// captureSnapshotsFromBPF builds bucketSnapshot entries from the BPF map data
// and adds them to the history store.  Called once per collect() cycle.
func captureSnapshotsFromBPF(hosts []hostEntry, now int64) {
	for _, h := range hosts {
		if h.Inst < 2 {
			continue
		}
		snap := bucketSnapshot{
			Ts:        now,
			BestAlg:   "", // filled below
			Instances: h.Inst,
			Re:        map[string]float64{},
			Ss:        map[string]int{},
			Bs:        map[string]int{},
			Ns:        map[string]int{},
			Mv:        map[string]float64{},
		}
		v := h.V
		metrics, _ := v["metrics"].([]interface{})
		bestI := toInt(v["best_i"])
		if bestI >= 0 && bestI < len(CONGS) {
			snap.BestAlg = CONGS[bestI]
		}
		snap.BestI = bestI
		snap.RefRate = toFloat(v["max_rate_delivered"]) / bpsToMbps
		snap.MinRtt = toFloat(v["min_rtt"])
		for i, m := range metrics {
			if i >= len(CONGS) {
				break
			}
			mi, ok := m.(map[string]interface{})
			if !ok {
				continue
			}
			alg := CONGS[i]
			snap.Re[alg] = toFloat(mi["rate_ema"])
			snap.Ss[alg] = toInt(mi["swap_score"])
			snap.Bs[alg] = toInt(mi["bad_streak"])
			snap.Ns[alg] = toInt(mi["null_streak"])
			snap.Mv[alg] = toFloat(mi["metric_value"])
		}
		hist.addSnapshot(h.Addr, snap)
	}
	// Save to disk every 10 cycles (5 min) to avoid excessive I/O
	if hist.cycleCount%10 == 0 {
		hist.saveToDisk()
	}
}

// bucketsAsMaps converts the buckets list (from collect()) to []map[string]interface{}
// for the HTTP handlers that need it.
func bucketsAsMaps(buckets interface{}) []map[string]interface{} {
	if buckets == nil {
		return nil
	}
	// Use JSON round-trip — works regardless of the input type
	// (handles both typed struct slices and []interface{})
	data, err := json.Marshal(buckets)
	if err != nil {
		return nil
	}
	var out []map[string]interface{}
	if err := json.Unmarshal(data, &out); err != nil {
		return nil
	}
	return out
}

// suppress unused import warnings (fmt used in error paths)
var _ = fmt.Sprintf
