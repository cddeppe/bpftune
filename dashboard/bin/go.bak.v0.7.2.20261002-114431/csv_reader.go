package main

// csv_reader.go — reads buckets.v2.csv for historical chart data.
//
// v0.7.0 change: does NOT re-apply ResolveBucket on read.  The addr
// column was written as the labeled form by csv_writer.go, so we trust
// it as-is.  This was the root cause of "custom bucket disappears":
// applying ResolveBucket again at read time would remap a labeled
// "controld" back to itself (no harm) BUT would also remap a raw
// "2606:1a40::" (no label at write time) to "controld" if a label was
// added later — splitting the bucket into two ring-buffer entries.
//
// Trade-off: if a label is added LATER, old rows stay under the old
// (unlabeled) key in the CSV.  The new BPF reads will use the new label,
// so the ring buffer will have BOTH keys for the same physical bucket.
// This is acceptable — the user can see both until the old rows age out
// of the 24h window.
//
// Memory: the full CSV file is read into memory ONCE and cached for
// 5 minutes.  The cache is a map[bucketID][]bucketSnapshot.  For a 47MB
// CSV with ~188K rows and 43 buckets, that's ~4400 rows/bucket.  Each
// bucketSnapshot has 5 maps (Re/Ss/Bs/Ns/Mv) so memory is ~100 bytes
// per snapshot + map overhead.  Total ~2MB per cache instance.

import (
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

// ============================================================================
// readCSVAll — global cache (read entire file ONCE, serve all lookups)
// ============================================================================

var (
	csvFullCache   map[string][]bucketSnapshot
	csvFullCacheAt time.Time
	csvFullCacheMu sync.Mutex
	// csvFullCacheFileMtime tracks the actual file mtime so we
	// re-read when the file grows (every 30s the collector appends
	// a new row per bucket).  The 5-minute time-based cache was
	// too coarse — it caused the 7d/all charts to be stale by up
	// to 5 minutes after a restart.
	csvFullCacheMtime time.Time
)

// readCSVAll reads the entire buckets.v2.csv into a map keyed by bucket
// ID.  Cached for 30 seconds (matches collect cycle).  If the file's
// mtime hasn't changed since last read, returns the cached value.
//
// v0.7.0: does NOT apply ResolveBucket to the addr column.  The addr
// column is already labeled (see csv_writer.go comment).
func readCSVAll() map[string][]bucketSnapshot {
	csvFullCacheMu.Lock()
	defer csvFullCacheMu.Unlock()

	fi, err := os.Stat(bucketsCSVPath)
	if err != nil {
		return nil
	}
	// Re-use cache if file hasn't changed AND cache is fresh.
	if csvFullCache != nil &&
		fi.ModTime().Equal(csvFullCacheMtime) &&
		time.Since(csvFullCacheAt) < 30*time.Second {
		return csvFullCache
	}

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
		addr := cols[1]
		ts, err := strconv.ParseInt(cols[0], 10, 64)
		if err != nil {
			continue
		}
		snap := bucketSnapshot{
			Ts: ts, Re: map[string]float64{}, Ss: map[string]int{},
			Bs: map[string]int{}, Ns: map[string]int{}, Mv: map[string]float64{},
		}
		snap.Instances, _ = strconv.Atoi(cols[2])
		snap.MinRtt, _ = strconv.ParseFloat(cols[3], 64)
		snap.RefRate, _ = strconv.ParseFloat(cols[4], 64)
		snap.BestI, _ = strconv.Atoi(cols[5])
		if snap.BestI >= 0 && snap.BestI < len(CONGS) {
			snap.BestAlg = CONGS[snap.BestI]
		}
		// rate_best_i / rate_best_v not in CSV yet (csv_writer writes 0).
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
		result[addr] = append(result[addr], snap)
	}
	csvFullCache = result
	csvFullCacheAt = time.Now()
	csvFullCacheMtime = fi.ModTime()
	return result
}

// readBucketCSVFast returns snapshots for one bucket from the global cache.
// If span > 0, filters to entries with Ts >= (now - span).
func readBucketCSVFast(bucketID string, span int64) []bucketSnapshot {
	all := readCSVAll()
	if all == nil {
		return nil
	}
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

// readBucketCSV is the older per-bucket reader.  Kept for backward compat
// with handleBucketJSON.  Now delegates to readBucketCSVFast.
func readBucketCSV(bucketID string, span int64) []bucketSnapshot {
	return readBucketCSVFast(bucketID, span)
}

// ============================================================================
// loadCSVTailIntoRingBuffer — read the last 24h of buckets.v2.csv into
// the ring buffer on startup (so 1h/24h charts work immediately).
//
// v0.7.0: uses readCSVAll (no separate file read).  Trusts the stored
// labels (no ResolveBucket).  The ring buffer key is the same as the
// BPF-map-merged key (since both are the labeled form), so the live
// 30s snapshots will append to the same ring buffer entry as the
// historical CSV rows.
// ============================================================================

func loadCSVTailIntoRingBuffer() {
	defer func() {
		if r := recover(); r != nil {
			os.Stderr.WriteString("loadCSVTailIntoRingBuffer PANICKED: " + toString(r) + "\n")
		}
	}()
	all := readCSVAll()
	if all == nil {
		os.Stderr.WriteString("loadCSVTailIntoRingBuffer: readCSVAll returned nil\n")
		return
	}
	cutoff := time.Now().Unix() - 86400
	count := 0
	for bucketID, snaps := range all {
		for _, s := range snaps {
			if s.Ts < cutoff {
				continue
			}
			hist.mu.Lock()
			hist.raw[bucketID] = append(hist.raw[bucketID], s)
			if len(hist.raw[bucketID]) > 2880 {
				hist.raw[bucketID] = hist.raw[bucketID][len(hist.raw[bucketID])-2880:]
			}
			hist.mu.Unlock()
			count++
		}
	}
	os.Stderr.WriteString("loadCSVTailIntoRingBuffer: loaded " + strconv.Itoa(count) +
		" entries for " + strconv.Itoa(len(all)) + " buckets\n")
}
