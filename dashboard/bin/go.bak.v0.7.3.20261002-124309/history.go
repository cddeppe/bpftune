package main

// history.go — multi-level in-memory ring buffer for historical
// time-series data.  Replaces the Python renderer + SQLite + CSV.
//
// Architecture:
//   - Every 30s collect cycle: capture a bucketSnapshot per bucket
//   - Multi-level ring buffer:
//       raw:     last 24h at 30s intervals (2880 entries) → serves 1h + 24h
//       bin5m:   last 24h at 5min intervals (288 entries)  → 24h fallback
//       bin1h:   last 7d at 1h intervals (168 entries)     → serves 7d
//       bin6h:   last 30d at 6h intervals (120 entries)     → serves "all"
//   - HTTP handlers + render-to-disk functions live in render.go and
//     http_handlers.go (extracted from this file in v0.7.0).
//
// On restart, loadCSVTailIntoRingBuffer() reads the last 24h of CSV
// into the raw ring buffer so 1h/24h charts work immediately.

import (
	"sync"
)

// ============================================================================
// Snapshot types
// ============================================================================

// v0.7.2: arrays instead of maps — saves ~1.5GB memory on 104-bucket hosts.
// Each map has ~150 bytes overhead; 1.5M maps × 150B = 225MB just for empty
// map headers.  Arrays are inline in the struct — zero overhead.
type bucketSnapshot struct {
	Ts         int64       `json:"ts"` // wall-clock collected_ts
	BestAlg    string      `json:"best_alg"`
	BestI      int         `json:"best_i"`
	Instances  int         `json:"instances"`
	RefRate    float64     `json:"ref_rate"`
	MinRtt     float64     `json:"min_rtt"`
	RateBestI  int         `json:"rate_best_i"`
	RateBestV  float64     `json:"rate_best_v"`
	TcpRmemMax int         `json:"tcp_rmem_max"`
	Re         [16]float64 `json:"re"` // rate_ema per alg index
	Ss         [16]int     `json:"ss"` // swap_score per alg index
	Bs         [16]int     `json:"bs"` // bad_streak per alg index
	Ns         [16]int     `json:"ns"` // null_streak per alg index
	Mv         [16]float64 `json:"mv"` // metric_value per alg index
}

// historyStore is the multi-level ring buffer.
type historyStore struct {
	mu         sync.RWMutex
	raw        map[string][]bucketSnapshot // bucketID → last 2880 snapshots (24h)
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

	// Add to raw (cap at 2880 = 24h at 30s; raised from 240 in v0.5)
	h.raw[bucketID] = append(h.raw[bucketID], snap)
	if len(h.raw[bucketID]) > 2880 {
		h.raw[bucketID] = h.raw[bucketID][len(h.raw[bucketID])-2880:]
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
	// v0.7.2: arrays are zero-valued by default — no init needed
	out := bucketSnapshot{
		BestAlg:    snaps[len(snaps)-1].BestAlg,
		BestI:      snaps[len(snaps)-1].BestI,
		TcpRmemMax: snaps[len(snaps)-1].TcpRmemMax,
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
		// v0.7.2: array index instead of map key
		for i := 0; i < 16; i++ {
			out.Re[i] += s.Re[i]
			out.Ss[i] += s.Ss[i]
			out.Bs[i] += s.Bs[i]
			out.Ns[i] += s.Ns[i]
			out.Mv[i] += s.Mv[i]
		}
	}
	n := len(snaps)
	out.Ts = sumTs / int64(n)
	out.Instances = sumInst / n
	out.RateBestI = sumRBI / n
	out.RefRate = sumRR / float64(n)
	out.MinRtt = sumMinRtt / float64(n)
	out.RateBestV = sumRBV / float64(n)
	// v0.7.2: divide arrays by n
	for i := 0; i < 16; i++ {
		out.Re[i] /= float64(n)
		out.Ss[i] /= n
		out.Bs[i] /= n
		out.Ns[i] /= n
		out.Mv[i] /= float64(n)
	}
	return out
}

// ============================================================================
// RANGES — mirrors Python renderer RANGES dict
// ============================================================================

var ranges = map[string][2]interface{}{
	"1h":  {3600, 60},        // span=3600s, bin_width=60s
	"24h": {86400, 300},      // span=86400s, bin_width=300s (5 min)
	"7d":  {7 * 86400, 3600}, // span=604800s, bin_width=3600s (1 hour)
	"all": {nil, 21600},      // span=None (unlimited), bin_width=21600s (6 hours)
}

// ============================================================================
// captureSnapshotsFromBPF builds bucketSnapshot entries from the BPF
// map data and adds them to the history store.  Called once per collect
// cycle.  Save to disk every 10 cycles (5 min).
// ============================================================================

func captureSnapshotsFromBPF(hosts []hostEntry, now int64) {
	for _, h := range hosts {
		if h.Inst < 2 {
			continue
		}
		// v0.7.2: arrays are zero-valued by default — no init needed
		snap := bucketSnapshot{
			Ts:        now,
			BestAlg:   "",
			Instances: h.Inst,
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
			if i >= len(CONGS) || i >= 16 {
				break
			}
			mi, ok := m.(map[string]interface{})
			if !ok {
				continue
			}
			// v0.7.2: array index instead of map key
			snap.Re[i] = toFloat(mi["rate_ema"])
			snap.Ss[i] = toInt(mi["swap_score"])
			snap.Bs[i] = toInt(mi["bad_streak"])
			snap.Ns[i] = toInt(mi["null_streak"])
			snap.Mv[i] = toFloat(mi["metric_value"])
		}
		hist.addSnapshot(h.Addr, snap)
	}
	if hist.cycleCount%10 == 0 {
		hist.saveToDisk()
	}
}

// ============================================================================
// Disk persistence — save/load ring buffer to survive restarts
// ============================================================================

func (h *historyStore) saveToDisk() {
	h.mu.RLock()
	defer h.mu.RUnlock()

	path := historyPath("collector-go-history.json")
	writeFileAtomic(path, func() []byte {
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
		out, _ := jsonMarshal(data)
		return out
	})
}

func (h *historyStore) loadFromDisk() {
	h.mu.Lock()
	defer h.mu.Unlock()

	path := historyPath("collector-go-history.json")
	data, err := readFile(path)
	if err != nil {
		return
	}
	type persistData struct {
		Raw   map[string][]bucketSnapshot `json:"raw"`
		Bin5m map[string][]bucketSnapshot `json:"bin5m"`
		Bin1h map[string][]bucketSnapshot `json:"bin1h"`
		Bin6h map[string][]bucketSnapshot `json:"bin6h"`
	}
	var pd persistData
	if err := jsonUnmarshal(data, &pd); err != nil {
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
