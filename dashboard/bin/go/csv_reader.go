package main

// csv_reader.go — reads buckets.v2.csv for historical chart data.
//
// v0.7.3: NO persistent CSV cache.  The CSV is read fresh each time
// renderToDisk runs (every 5 min) and the result is freed after.
// This cuts memory by ~80MB (the old csvFullCache held 45K snapshots
// permanently + the 47MB CSV string during reads).
//
// For dynamic fallback (when static file is stale), readBucketCSV
// reads per-bucket from the CSV file.  This is slower (~50-100ms)
// but only happens when the 10-min static file cache expires, which
// is rare (renderToDisk regenerates every 5 min).

import (
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

// ============================================================================
// readCSVTail — reads only the last N bytes of the CSV (for 24h in fast mode)
// v0.7.4: reads ~8MB instead of 60MB.  7.5x less memory + CPU.
// ============================================================================

func readCSVTail(span int64) map[string][]bucketSnapshot {
	f, err := os.Open(bucketsCSVPath)
	if err != nil {
		return nil
	}
	defer f.Close()

	// Read header (first line)
	headerBytes, err := readHeader(f)
	if err != nil {
		return nil
	}
	header := strings.Split(strings.TrimSpace(headerBytes), ",")
	colIdx := map[string]int{}
	for i, col := range header {
		colIdx[col] = i
	}

	// Seek to tail — read last 8MB (covers ~24h of data for 104 buckets)
	fi, err := f.Stat()
	if err != nil {
		return nil
	}
	readSize := int64(20 * 1024 * 1024) // 8MB
	if readSize > fi.Size() {
		readSize = fi.Size()
	}
	_, err = f.Seek(fi.Size()-readSize, 0)
	if err != nil {
		return nil
	}
	data := make([]byte, readSize)
	_, err = f.Read(data)
	if err != nil {
		return nil
	}

	lines := strings.Split(string(data), "\n")
	// Skip first line (might be partial)
	if len(lines) > 1 {
		lines = lines[1:]
	}

	cutoff := time.Now().Unix() - span
	result := map[string][]bucketSnapshot{}
	for _, line := range lines {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		cols := strings.Split(line, ",")
		if len(cols) < 9 {
			continue
		}
		addr := ResolveBucket(cols[1])  // v0.7.5g: re-resolve at read time
		ts, err := strconv.ParseInt(cols[0], 10, 64)
		if err != nil || ts < cutoff {
			continue
		}
		snap := bucketSnapshot{Ts: ts}
		snap.Instances, _ = strconv.Atoi(cols[2])
		snap.MinRtt, _ = strconv.ParseFloat(cols[3], 64)
		snap.RefRate, _ = strconv.ParseFloat(cols[4], 64)
		snap.BestI, _ = strconv.Atoi(cols[5])
		if snap.BestI >= 0 && snap.BestI < len(CONGS) {
			snap.BestAlg = CONGS[snap.BestI]
		}
		for i, alg := range CONGS {
			if i >= 16 {
				break
			}
			if idx, ok := colIdx["mv_"+alg]; ok && idx < len(cols) && cols[idx] != "" {
				snap.Mv[i], _ = strconv.ParseFloat(cols[idx], 64)
			}
			if idx, ok := colIdx["re_"+alg]; ok && idx < len(cols) && cols[idx] != "" {
				snap.Re[i], _ = strconv.ParseFloat(cols[idx], 64)
			}
			if idx, ok := colIdx["ss_"+alg]; ok && idx < len(cols) && cols[idx] != "" {
				v, _ := strconv.Atoi(cols[idx])
				snap.Ss[i] = v
			}
			if idx, ok := colIdx["bs_"+alg]; ok && idx < len(cols) && cols[idx] != "" {
				v, _ := strconv.Atoi(cols[idx])
				snap.Bs[i] = v
			}
			if idx, ok := colIdx["ns_"+alg]; ok && idx < len(cols) && cols[idx] != "" {
				v, _ := strconv.Atoi(cols[idx])
				snap.Ns[i] = v
			}
		}
		result[addr] = append(result[addr], snap)
	}
	return result
}

func readHeader(f *os.File) (string, error) {
	_, err := f.Seek(0, 0)
	if err != nil {
		return "", err
	}
	buf := make([]byte, 4096)
	n, err := f.Read(buf)
	if err != nil {
		return "", err
	}
	idx := strings.IndexByte(string(buf[:n]), '\n')
	if idx < 0 {
		return string(buf[:n]), nil
	}
	return string(buf[:idx]), nil
}

// ============================================================================
// readCSVAll — reads the entire CSV into a per-bucket map (NO CACHE)
// Called by renderToDisk every 5 min.  Result is freed after use.
// ============================================================================

var (
	csvReadCache   = map[string]cachedCSVRead{}
	csvReadCacheMu sync.Mutex
)

func readCSVAll() map[string][]bucketSnapshot {
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
	result := map[string][]bucketSnapshot{}
	for i := 1; i < len(lines); i++ {
		line := strings.TrimSpace(lines[i])
		if line == "" {
			continue
		}
		cols := strings.Split(line, ",")
		if len(cols) < 9 {
			continue
		}
		// v0.7.0: trust the stored label as-is (no ResolveBucket)
		addr := ResolveBucket(cols[1])  // v0.7.5g: re-resolve at read time
		ts, err := strconv.ParseInt(cols[0], 10, 64)
		if err != nil {
			continue
		}
		snap := bucketSnapshot{Ts: ts}
		snap.Instances, _ = strconv.Atoi(cols[2])
		snap.MinRtt, _ = strconv.ParseFloat(cols[3], 64)
		snap.RefRate, _ = strconv.ParseFloat(cols[4], 64)
		snap.BestI, _ = strconv.Atoi(cols[5])
		if snap.BestI >= 0 && snap.BestI < len(CONGS) {
			snap.BestAlg = CONGS[snap.BestI]
		}
		for i, alg := range CONGS {
			if i >= 16 {
				break
			}
			if idx, ok := colIdx["mv_"+alg]; ok && idx < len(cols) && cols[idx] != "" {
				snap.Mv[i], _ = strconv.ParseFloat(cols[idx], 64)
			}
			if idx, ok := colIdx["re_"+alg]; ok && idx < len(cols) && cols[idx] != "" {
				snap.Re[i], _ = strconv.ParseFloat(cols[idx], 64)
			}
			if idx, ok := colIdx["ss_"+alg]; ok && idx < len(cols) && cols[idx] != "" {
				v, _ := strconv.Atoi(cols[idx])
				snap.Ss[i] = v
			}
			if idx, ok := colIdx["bs_"+alg]; ok && idx < len(cols) && cols[idx] != "" {
				v, _ := strconv.Atoi(cols[idx])
				snap.Bs[i] = v
			}
			if idx, ok := colIdx["ns_"+alg]; ok && idx < len(cols) && cols[idx] != "" {
				v, _ := strconv.Atoi(cols[idx])
				snap.Ns[i] = v
			}
		}
		result[addr] = append(result[addr], snap)
	}
	return result
}

// readBucketCSVFast returns snapshots for one bucket from a pre-loaded map.
// Used by renderToDisk (which calls readCSVAll once, then iterates).
func readBucketCSVFromMap(all map[string][]bucketSnapshot, bucketID string, span int64) []bucketSnapshot {
	snaps := all[bucketID]
	if span > 0 {
		cutoff := time.Now().Unix() - span
		var filtered []bucketSnapshot
		for _, s := range snaps {
			if s.Ts >= cutoff {
				filtered = append(filtered, s)
			}
		}
		return filtered
	}
	return snaps
}

// readBucketCSV reads per-bucket from the CSV file directly (slow, ~50-100ms).
// Used by handleBucketJSON dynamic fallback (rare — only when static file stale).
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
		addr := ResolveBucket(cols[1])  // v0.7.5g: re-resolve at read time
		if addr != bucketID {
			continue
		}
		ts, err := strconv.ParseInt(cols[0], 10, 64)
		if err != nil || (span > 0 && ts < cutoff) {
			continue
		}
		snap := bucketSnapshot{Ts: ts}
		snap.Instances, _ = strconv.Atoi(cols[2])
		snap.MinRtt, _ = strconv.ParseFloat(cols[3], 64)
		snap.RefRate, _ = strconv.ParseFloat(cols[4], 64)
		snap.BestI, _ = strconv.Atoi(cols[5])
		if snap.BestI >= 0 && snap.BestI < len(CONGS) {
			snap.BestAlg = CONGS[snap.BestI]
		}
		for i, alg := range CONGS {
			if i >= 16 {
				break
			}
			if idx, ok := colIdx["mv_"+alg]; ok && idx < len(cols) && cols[idx] != "" {
				snap.Mv[i], _ = strconv.ParseFloat(cols[idx], 64)
			}
			if idx, ok := colIdx["re_"+alg]; ok && idx < len(cols) && cols[idx] != "" {
				snap.Re[i], _ = strconv.ParseFloat(cols[idx], 64)
			}
			if idx, ok := colIdx["ss_"+alg]; ok && idx < len(cols) && cols[idx] != "" {
				v, _ := strconv.Atoi(cols[idx])
				snap.Ss[i] = v
			}
			if idx, ok := colIdx["bs_"+alg]; ok && idx < len(cols) && cols[idx] != "" {
				v, _ := strconv.Atoi(cols[idx])
				snap.Bs[i] = v
			}
			if idx, ok := colIdx["ns_"+alg]; ok && idx < len(cols) && cols[idx] != "" {
				v, _ := strconv.Atoi(cols[idx])
				snap.Ns[i] = v
			}
		}
		snaps = append(snaps, snap)
	}
	csvReadCacheMu.Lock()
	csvReadCache[cacheKey] = cachedCSVRead{data: snaps, readAt: time.Now()}
	if len(csvReadCache) > 50 {
		for k := range csvReadCache {
			delete(csvReadCache, k)
			break
		}
	}
	csvReadCacheMu.Unlock()
	return snaps
}

// readBucketCSVFast is kept for backward compat (fleet handler).
// Calls readBucketCSV with a 5-min cache.
func readBucketCSVFast(bucketID string, span int64) []bucketSnapshot {
	return readBucketCSV(bucketID, span)
}

type cachedCSVRead struct {
	data   []bucketSnapshot
	readAt time.Time
}

// ============================================================================
// loadCSVTailIntoRingBuffer — read the last 1h of CSV into the ring buffer
// v0.7.3: only loads 1h (was 24h) since ring buffer is only 1h.
// ============================================================================

func loadCSVTailIntoRingBuffer() {
	defer func() {
		if r := recover(); r != nil {
			os.Stderr.WriteString("loadCSVTailIntoRingBuffer PANICKED: " + toString(r) + "\n")
		}
	}()
	all := readCSVTail(3600)
	if all == nil {
		os.Stderr.WriteString("loadCSVTailIntoRingBuffer: readCSVAll returned nil\n")
		return
	}
	// v0.7.3: only load last 1h (ringCap * 30s = 1h)
	cutoff := time.Now().Unix() - int64(ringCap*30)
	count := 0
	for bucketID, snaps := range all {
		for _, s := range snaps {
			if s.Ts < cutoff {
				continue
			}
			hist.mu.Lock()
			hist.raw[bucketID] = append(hist.raw[bucketID], s)
			if len(hist.raw[bucketID]) > ringCap {
				hist.raw[bucketID] = hist.raw[bucketID][len(hist.raw[bucketID])-ringCap:]
			}
			hist.mu.Unlock()
			count++
		}
	}
	os.Stderr.WriteString("loadCSVTailIntoRingBuffer: loaded " + strconv.Itoa(count) +
		" entries for " + strconv.Itoa(len(all)) + " buckets\n")
}
