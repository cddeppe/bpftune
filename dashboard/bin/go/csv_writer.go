package main

// CSV writer — appends to the existing buckets.v2.csv, swaps.csv, srate.csv
// files.  Matches the exact column format of the Python collector so the
// historical data continues seamlessly.
//
// buckets.v2.csv columns (94 total):
//   collected_ts, addr, instances, min_rtt, ref_rate, best_i, best_alg,
//   rate_best_i, rate_best_v,
//   mv_<alg>, re_<alg>  (16 algs × 2 = 32 columns)
//   tcp_rmem_min, tcp_rmem_def, tcp_rmem_max
//   ss_<alg>  (16 algs)
//   bs_<alg>  (16 algs)
//   ns_<alg>  (16 algs)
//
// swaps.csv columns (18):
//   collected_ts, boot_ts, cookie, from_alg, to_alg, d, mt_alg, rb_alg,
//   diverges, outcome, socket_rate_before, dest, dest_raw, f_ema, t_ema,
//   srate_before, direction, rport
//
// srate.csv columns (5):
//   collected_ts, boot_ts, cookie, alg, srate

import (
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	bucketsCSVPath = "/var/lib/bpftune/history/buckets.v2.csv"
	swapsCSVPath   = "/var/lib/bpftune/history/swaps.csv"
	srateCSVPath   = "/var/lib/bpftune/history/srate.csv"
)

// Dedup sets — track which swaps/srates have already been written to CSV.
// Keyed by (cookie, boot_ts) which uniquely identifies an event.
var (
	writtenSwaps  = map[int64]map[float64]bool{}
	writtenSrates = map[int64]map[float64]bool{}
	dedupMu       sync.Mutex
)

// ============================================================================
// writeBucketsCSV — one row per bucket per 30s cycle
// ============================================================================

func writeBucketsCSV(hosts []hostEntry, now int64) {
	f, err := os.OpenFile(bucketsCSVPath, os.O_APPEND|os.O_WRONLY|os.O_CREATE, 0644)
	if err != nil {
		return
	}
	defer f.Close()

	// Read tcp_rmem values once (same for all buckets on this host)
	rmemMin, rmemDef, rmemMax := readTcpRmem()

	for _, h := range hosts {
		if h.Inst < 2 {
			continue
		}
		row := buildBucketCSVRow(h, now, rmemMin, rmemDef, rmemMax)
		f.WriteString(row + "\n")
	}
}

func buildBucketCSVRow(h hostEntry, now int64, rmemMin, rmemDef, rmemMax int) string {
	v := h.V
	metrics, _ := v["metrics"].([]interface{})

	// Build per-alg metric values
	mv := make([]string, 16)
	re := make([]string, 16)
	ss := make([]string, 16)
	bs := make([]string, 16)
	ns := make([]string, 16)
	for i := 0; i < 16; i++ {
		var mi map[string]interface{}
		if i < len(metrics) {
			mi, _ = metrics[i].(map[string]interface{})
		}
		if mi == nil {
			// No metric data — write empty (matches Python format)
			mv[i] = ""
			re[i] = ""
			ss[i] = ""
			bs[i] = ""
			ns[i] = ""
		} else {
			mv[i] = strconv.FormatFloat(toFloat(mi["metric_value"]), 'f', -1, 64)
			re[i] = strconv.FormatFloat(toFloat(mi["rate_ema"]), 'f', -1, 64)
			// ss/bs/ns are empty when metric_count is 0 (matches Python)
			mc := toInt(mi["metric_count"])
			if mc > 0 || toInt(mi["sockets_alive"]) > 0 || toFloat(mi["rate_ema"]) > 0 {
				ss[i] = strconv.Itoa(toInt(mi["swap_score"]))
				bs[i] = strconv.Itoa(toInt(mi["bad_streak"]))
				ns[i] = strconv.Itoa(toInt(mi["null_streak"]))
			} else {
				ss[i] = ""
				bs[i] = ""
				ns[i] = ""
			}
		}
	}

	bestI := toInt(v["best_i"])
	bestAlg := ""
	if bestI >= 0 && bestI < len(CONGS) {
		bestAlg = CONGS[bestI]
	}

	parts := []string{
		strconv.FormatInt(now, 10), // collected_ts
		h.Addr,                     // addr (labeled)
		strconv.Itoa(h.Inst),       // instances
		strconv.FormatFloat(toFloat(v["min_rtt"]), 'f', -1, 64),                      // min_rtt
		strconv.FormatFloat(toFloat(v["max_rate_delivered"])/bpsToMbps, 'f', -1, 64), // ref_rate
		strconv.Itoa(bestI), // best_i
		bestAlg,             // best_alg
		"0",                 // rate_best_i (TODO: compute)
		"0",                 // rate_best_v (TODO: compute)
	}
	// mv_ and re_ pairs interleaved: mv_cubic,re_cubic,mv_bbr,re_bbr,...
	for i := 0; i < 16; i++ {
		parts = append(parts, mv[i], re[i])
	}
	// tcp_rmem
	parts = append(parts,
		strconv.Itoa(rmemMin),
		strconv.Itoa(rmemDef),
		strconv.Itoa(rmemMax),
	)
	// ss_ (16)
	parts = append(parts, ss...)
	// bs_ (16)
	parts = append(parts, bs...)
	// ns_ (16)
	parts = append(parts, ns...)

	return strings.Join(parts, ",")
}

// ============================================================================
// writeSwapsCSV — one row per NEW swap event (deduplicated)
// ============================================================================

func writeSwapsCSV(swaps []swapRow, now int64) {
	dedupMu.Lock()
	defer dedupMu.Unlock()

	f, err := os.OpenFile(swapsCSVPath, os.O_APPEND|os.O_WRONLY|os.O_CREATE, 0644)
	if err != nil {
		return
	}
	defer f.Close()

	for _, sw := range swaps {
		// Dedup by (cookie, boot_ts)
		if writtenSwaps[sw.Cookie] == nil {
			writtenSwaps[sw.Cookie] = map[float64]bool{}
		}
		if writtenSwaps[sw.Cookie][sw.Ts] {
			continue
		}
		writtenSwaps[sw.Cookie][sw.Ts] = true

		// Prune dedup set if too large (keep last 1000 per cookie)
		if len(writtenSwaps[sw.Cookie]) > 1000 {
			for k := range writtenSwaps[sw.Cookie] {
				if k < sw.Ts-3600 { // keep last hour
					delete(writtenSwaps[sw.Cookie], k)
				}
			}
		}

		row := buildSwapCSVRow(sw, now)
		f.WriteString(row + "\n")
	}
}

func buildSwapCSVRow(sw swapRow, now int64) string {
	mtAlg := ""
	if sw.Mt != "" {
		if i, err := strconv.Atoi(sw.Mt); err == nil {
			mtAlg = CONGS[i&15]
		}
	}
	rbAlg := ""
	if sw.Rb != "" {
		if i, err := strconv.Atoi(sw.Rb); err == nil {
			rbAlg = CONGS[i&15]
		}
	}
	fromAlg := algName(sw.From)
	toAlg := algName(sw.To)
	d, _ := strconv.Atoi(sw.D)
	diverges := "0"
	if mtAlg != "" && rbAlg != "" && mtAlg != rbAlg {
		diverges = "1"
	}
	dest := destIP(sw.Dest)
	destRaw := sw.Dest

	parts := []string{
		strconv.FormatInt(now, 10),             // collected_ts
		strconv.FormatFloat(sw.Ts, 'f', 6, 64), // boot_ts
		strconv.FormatInt(sw.Cookie, 10),       // cookie
		fromAlg,                                // from_alg
		toAlg,                                  // to_alg
		strconv.Itoa(d),                        // d
		mtAlg,                                  // mt_alg
		rbAlg,                                  // rb_alg
		diverges,                               // diverges
		"",                                     // outcome (filled by renderer)
		"",                                     // socket_rate_before
		dest,                                   // dest
		destRaw,                                // dest_raw
		"",                                     // f_ema
		"",                                     // t_ema
		"",                                     // srate_before
		"",                                     // direction
		"",                                     // rport
	}
	return strings.Join(parts, ",")
}

// ============================================================================
// writeSrateCSV — one row per NEW srate event (deduplicated)
// ============================================================================

func writeSrateCSV(text string, now int64) {
	dedupMu.Lock()
	defer dedupMu.Unlock()

	// Parse srate events from the log text
	type srateEvent struct {
		ts     float64
		cookie int64
		alg    int
		srate  int64
	}
	var events []srateEvent
	for _, line := range strings.Split(text, "\n") {
		m := rxSrate.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		ts, _ := strconv.ParseFloat(m[1], 64)
		cookie, _ := strconv.ParseInt(m[2], 10, 64)
		alg, _ := strconv.Atoi(m[3])
		sr, _ := strconv.ParseInt(m[4], 10, 64)
		events = append(events, srateEvent{ts, cookie, alg, sr})
	}

	if len(events) == 0 {
		return
	}

	f, err := os.OpenFile(srateCSVPath, os.O_APPEND|os.O_WRONLY|os.O_CREATE, 0644)
	if err != nil {
		return
	}
	defer f.Close()

	for _, e := range events {
		// Dedup by (cookie, boot_ts)
		if writtenSrates[e.cookie] == nil {
			writtenSrates[e.cookie] = map[float64]bool{}
		}
		if writtenSrates[e.cookie][e.ts] {
			continue
		}
		writtenSrates[e.cookie][e.ts] = true

		// Prune if too large
		if len(writtenSrates[e.cookie]) > 1000 {
			for k := range writtenSrates[e.cookie] {
				if k < e.ts-3600 {
					delete(writtenSrates[e.cookie], k)
				}
			}
		}

		alg := algName(e.alg)
		parts := []string{
			strconv.FormatInt(now, 10),            // collected_ts
			strconv.FormatFloat(e.ts, 'f', 6, 64), // boot_ts
			strconv.FormatInt(e.cookie, 10),       // cookie
			alg,                                   // alg
			strconv.FormatInt(e.srate, 10),        // srate
		}
		f.WriteString(strings.Join(parts, ",") + "\n")
	}
}

// ============================================================================
// readTcpRmem — reads /proc/sys/net/ipv4/tcp_rmem
// ============================================================================

func readTcpRmem() (min, def, max int) {
	data, err := os.ReadFile("/proc/sys/net/ipv4/tcp_rmem")
	if err != nil {
		return 4096, 87380, 6291456
	}
	parts := strings.Fields(string(data))
	if len(parts) < 3 {
		return 4096, 87380, 6291456
	}
	min, _ = strconv.Atoi(parts[0])
	def, _ = strconv.Atoi(parts[1])
	max, _ = strconv.Atoi(parts[2])
	return
}

// ============================================================================
// loadCSVTail — read the last 24h of buckets.v2.csv into the ring buffer
// (called on startup so 1h/24h charts work immediately after restart)
// ============================================================================

func loadCSVTailIntoRingBuffer() {
	data, err := os.ReadFile(bucketsCSVPath)
	if err != nil {
		return
	}
	lines := strings.Split(string(data), "\n")
	if len(lines) < 2 {
		return
	}

	// Parse header to get column indices
	header := strings.Split(lines[0], ",")
	colIdx := map[string]int{}
	for i, col := range header {
		colIdx[col] = i
	}

	// Read the last ~2880*10 = 28800 lines (24h at 30s, ~10 buckets per cycle)
	// But cap at 50000 to avoid excessive memory
	start := len(lines) - 50000
	if start < 1 {
		start = 1
	}

	cutoff := time.Now().Unix() - 86400 // 24h ago

	for i := start; i < len(lines); i++ {
		line := strings.TrimSpace(lines[i])
		if line == "" {
			continue
		}
		cols := strings.Split(line, ",")
		if len(cols) < 9 {
			continue
		}

		ts, err := strconv.ParseInt(cols[0], 10, 64)
		if err != nil || ts < cutoff {
			continue
		}
		addr := labelFor(cols[1]) // v0.5.4: label old raw IPs to match BPF map keys
		inst, _ := strconv.Atoi(cols[2])
		minRtt, _ := strconv.ParseFloat(cols[3], 64)
		refRate, _ := strconv.ParseFloat(cols[4], 64)
		bestI, _ := strconv.Atoi(cols[5])
		bestAlg := ""
		if bestI >= 0 && bestI < len(CONGS) {
			bestAlg = CONGS[bestI]
		}

		snap := bucketSnapshot{
			Ts:        ts,
			BestAlg:   bestAlg,
			BestI:     bestI,
			Instances: inst,
			RefRate:   refRate,
			MinRtt:    minRtt,
			Re:        map[string]float64{},
			Ss:        map[string]int{},
			Bs:        map[string]int{},
			Ns:        map[string]int{},
			Mv:        map[string]float64{},
		}

		// Parse mv_/re_ pairs and ss_/bs_/ns_ values
		for _, alg := range CONGS {
			mvKey := "mv_" + alg
			reKey := "re_" + alg
			ssKey := "ss_" + alg
			bsKey := "bs_" + alg
			nsKey := "ns_" + alg
			if idx, ok := colIdx[mvKey]; ok && idx < len(cols) && cols[idx] != "" {
				snap.Mv[alg], _ = strconv.ParseFloat(cols[idx], 64)
			}
			if idx, ok := colIdx[reKey]; ok && idx < len(cols) && cols[idx] != "" {
				snap.Re[alg], _ = strconv.ParseFloat(cols[idx], 64)
			}
			if idx, ok := colIdx[ssKey]; ok && idx < len(cols) && cols[idx] != "" {
				v, _ := strconv.Atoi(cols[idx])
				snap.Ss[alg] = v
			}
			if idx, ok := colIdx[bsKey]; ok && idx < len(cols) && cols[idx] != "" {
				v, _ := strconv.Atoi(cols[idx])
				snap.Bs[alg] = v
			}
			if idx, ok := colIdx[nsKey]; ok && idx < len(cols) && cols[idx] != "" {
				v, _ := strconv.Atoi(cols[idx])
				snap.Ns[alg] = v
			}
		}

		hist.addSnapshot(addr, snap)
	}
}

// ============================================================================
// readBucketCSV — read buckets.v2.csv for 7d/all chart requests
// (cached for 5 min to avoid re-reading the large file on every request)
// ============================================================================

var (
	csvReadCache   = map[string]cachedCSVRead{}
	csvReadCacheMu sync.Mutex
)

type cachedCSVRead struct {
	data   []bucketSnapshot
	readAt time.Time
}

func readBucketCSV(bucketID string, span int64) []bucketSnapshot {
	csvReadCacheMu.Lock()
	cacheKey := bucketID + ":" + strconv.FormatInt(span, 10)
	if cached, ok := csvReadCache[cacheKey]; ok && time.Since(cached.readAt) < 5*time.Minute {
		csvReadCacheMu.Unlock()
		return cached.data
	}
	csvReadCacheMu.Unlock()

	data, err := os.ReadFile(bucketsCSVPath)
	if err != nil {
		return nil
	}
	lines := strings.Split(string(data), "\n")
	if len(lines) < 2 {
		return nil
	}

	header := strings.Split(lines[0], ",")
	colIdx := map[string]int{}
	for i, col := range header {
		colIdx[col] = i
	}

	var cutoff int64
	if span > 0 {
		cutoff = time.Now().Unix() - span
	} else {
		cutoff = 0 // "all" — read everything
	}

	var snaps []bucketSnapshot
	for i := 1; i < len(lines); i++ {
		line := strings.TrimSpace(lines[i])
		if line == "" {
			continue
		}
		cols := strings.Split(line, ",")
		if len(cols) < 9 {
			continue
		}

		// Filter by bucket ID (addr column)
		addr := labelFor(cols[1]) // v0.5.4: label to match bucket ID
		if addr != bucketID {
			continue
		}

		ts, err := strconv.ParseInt(cols[0], 10, 64)
		if err != nil || ts < cutoff {
			continue
		}

		inst, _ := strconv.Atoi(cols[2])
		minRtt, _ := strconv.ParseFloat(cols[3], 64)
		refRate, _ := strconv.ParseFloat(cols[4], 64)
		bestI, _ := strconv.Atoi(cols[5])
		bestAlg := ""
		if bestI >= 0 && bestI < len(CONGS) {
			bestAlg = CONGS[bestI]
		}

		snap := bucketSnapshot{
			Ts:        ts,
			BestAlg:   bestAlg,
			BestI:     bestI,
			Instances: inst,
			RefRate:   refRate,
			MinRtt:    minRtt,
			Re:        map[string]float64{},
			Ss:        map[string]int{},
			Bs:        map[string]int{},
			Ns:        map[string]int{},
			Mv:        map[string]float64{},
		}

		for _, alg := range CONGS {
			if idx, ok := colIdx["mv_"+alg]; ok && idx < len(cols) && cols[idx] != "" {
				snap.Mv[alg], _ = strconv.ParseFloat(cols[idx], 64)
			}
			if idx, ok := colIdx["re_"+alg]; ok && idx < len(cols) && cols[idx] != "" {
				snap.Re[alg], _ = strconv.ParseFloat(cols[idx], 64)
			}
			if idx, ok := colIdx["ss_"+alg]; ok && idx < len(cols) && cols[idx] != "" {
				v, _ := strconv.Atoi(cols[idx])
				snap.Ss[alg] = v
			}
			if idx, ok := colIdx["bs_"+alg]; ok && idx < len(cols) && cols[idx] != "" {
				v, _ := strconv.Atoi(cols[idx])
				snap.Bs[alg] = v
			}
			if idx, ok := colIdx["ns_"+alg]; ok && idx < len(cols) && cols[idx] != "" {
				v, _ := strconv.Atoi(cols[idx])
				snap.Ns[alg] = v
			}
		}

		snaps = append(snaps, snap)
	}

	// Cache the result
	csvReadCacheMu.Lock()
	csvReadCache[cacheKey] = cachedCSVRead{data: snaps, readAt: time.Now()}
	// Prune cache if too many entries
	if len(csvReadCache) > 100 {
		for k := range csvReadCache {
			delete(csvReadCache, k)
			break
		}
	}
	csvReadCacheMu.Unlock()

	return snaps
}
