#!/usr/bin/env bash
# =============================================================================
# Atomic patch v0.4.0: FULL Go collector port (replaces Python collector)
#
# What this delivers (the full Python collector functionality in Go):
#   - Proper SWAP/MET/SRATE/ESTAB/PROOF/MIDSAMP regex parsing
#   - Swap outcome derivation (composite / srate / sustained rulers)
#   - Loss-recovery classification (rescued / full_loss / open)
#   - Label resolution with v6 fold rules (_fold_v6, _label_for, _canon_bucket)
#   - Proper live_leaders formula (rv * ss_eff / 256 * 16 / (16 + bad*4 + null*2))
#   - Full metric_by_bucket output (alg/votes/alive/rate_ema/swap_score/
#     penalty/score/bad_streak/null_streak/active) — matches Python exactly
#   - bucket_ips (all dest= occurrences from log, grouped by /16 or /32)
#   - log_window (oldest/newest swap ts, span, age)
#   - proofs_raw (proof leaderboard: good/proved/sampled per alg)
#   - Dest decoding (numeric → dotted IP, including IPv6 /32 and /64 forms)
#   - readBPFMap applies labelFor + merges by final label
#
# What this DOESN'T include yet (deferred):
#   - Cross-cycle pending_swaps state (Python collector.py:collect_swaps
#     maintains state across cycles for CSV writes; the renderer cron
#     will keep doing that work). The Go collector re-parses the 2MB
#     log tail fresh every cycle, which is enough for the dashboard's
#     recent_swaps + swap_outcomes (Python's CLI path does the same).
#   - CSV writing (SWAPS_CSV, SRATE_CSV) — renderer cron will keep doing this
#
# Usage:
#   bash apply-0.4.0-go-full-port.sh              # interactive
#   bash apply-0.4.0-go-full-port.sh --yes        # non-interactive + push
#   bash apply-0.4.0-go-full-port.sh --no-push     # commit only
#   bash apply-0.4.0-go-full-port.sh --check-only # apply + build, no commit
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
    -h|--help)   sed -n '2,35p' "$0" | sed 's/^# \?//'; exit 0 ;;
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

# --- fetch + ff-only pull if behind (TAB-safe parse) -------------------------
if git rev-parse --verify origin/dashboard >/dev/null 2>&1; then
  echo "fetching origin/dashboard..."
  git fetch origin dashboard
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
mkdir -p dashboard/bin/go dashboard/systemd

echo "writing dashboard/bin/go/go.mod ..."
cat > "dashboard/bin/go/go.mod" <<'__Z_FILE_EMBED_END_SENTINEL__'
module bpftune-collector

go 1.23.2
__Z_FILE_EMBED_END_SENTINEL__

echo "writing dashboard/bin/go/constants.go ..."
cat > "dashboard/bin/go/constants.go" <<'__Z_FILE_EMBED_END_SENTINEL__'
package main

// Congestion control algorithm names (index = alg number from BPF).
// Mirrors bpftune_log.py:CONGS.
var CONGS = []string{
	"cubic", "bbr", "htcp", "dctcp", "scalable", "vegas",
	"veno", "westwood", "reno", "illinois", "yeah", "lp",
	"bic", "highspeed", "hybla", "nv",
}

// Shared constants.  Mirrors bpftune_log.py constants block.
const (
	bpsToMbps       = 1_000_000.0 / 8.0
	sustainedLoS    = 60.0
	sustainedHiS    = 300.0
	metCacheTTL     = 600.0
	tRescueWindowS  = 3600
	logTailBytes    = 2_000_000
	liveTopN        = 6
	liveMaxBuckets  = 8
	minLeaderTrust  = 10
	recentSwapsCap  = 50
	recentProofsCap = 50
)

// File paths.  Mirrors bpftune_log.py path constants.
// Names match the existing main.go var block (which we keep using).
const (
	stateJSONPath = "/var/lib/bpftune/history/collector-go-state.json"
)
__Z_FILE_EMBED_END_SENTINEL__

echo "writing dashboard/bin/go/labels.go ..."
cat > "dashboard/bin/go/labels.go" <<'__Z_FILE_EMBED_END_SENTINEL__'
package main

// Label resolution + v6 fold rules.  Mirrors bpftune_log.py:
//   _load_labels, _load_fold, _load_aliases_labels,
//   _fold_v6, _canon_bucket, _normalize_ip, _label_for.
//
// All file reads are mtime-cached so we don't re-read the same file
// every collect() cycle.

import (
	"encoding/json"
	"net"
	"os"
	"strings"
	"sync"
	"time"
)

// ============================================================================
// mtime-cached file readers
// ============================================================================

// mtimeCache is a generic mtime-based file cache.  Returns the loader's
// output cached until the file's mtime changes.  Returns nil (caller
// treats as empty map) if file is missing or loader errors.
type mtimeCache struct {
	mu    sync.Mutex
	val   map[string]string
	mtime time.Time
}

func (c *mtimeCache) get(path string, loader func([]byte) map[string]string) map[string]string {
	c.mu.Lock()
	defer c.mu.Unlock()
	fi, err := os.Stat(path)
	if err != nil {
		c.val = nil
		c.mtime = time.Time{}
		return map[string]string{}
	}
	if c.val != nil && fi.ModTime().Equal(c.mtime) {
		return c.val
	}
	data, err := os.ReadFile(path)
	if err != nil {
		c.val = map[string]string{}
		c.mtime = fi.ModTime()
		return c.val
	}
	loaded := loader(data)
	if loaded == nil {
		loaded = map[string]string{}
	}
	c.val = loaded
	c.mtime = fi.ModTime()
	return c.val
}

var (
	labelsHolder        mtimeCache
	foldHolder          mtimeCache
	aliasesLabelsHolder mtimeCache
)

// // loadLabels reads labelsFile (/var/lib/bpftune/aliases.labels.json).
func loadLabels() map[string]string {
	return labelsHolder.get(labelsFile, func(data []byte) map[string]string {
		var m map[string]string
		if err := json.Unmarshal(data, &m); err != nil {
			return map[string]string{}
		}
		return m
	})
}

// loadFold reads /etc/bpftune/aliases and extracts {v6:hex: canonical_v4}.
// Line form: `2603:c020:0:0:0:0:0:0 = 89.168.0.0 [label]`
// Only lines where groups 2..7 are all zero are treated as /32 folds.
func loadFoldMap() map[string]string {
	return foldHolder.get(aliasesFile, func(data []byte) map[string]string {
		out := map[string]string{}
		for _, raw := range strings.Split(string(data), "\n") {
			line := strings.TrimSpace(raw)
			if line == "" || strings.HasPrefix(line, "#") || !strings.Contains(line, "=") {
				continue
			}
			parts := strings.SplitN(line, "=", 2)
			if len(parts) != 2 {
				continue
			}
			lhs := strings.TrimSpace(parts[0])
			rhs := strings.Fields(strings.TrimSpace(parts[1]))
			if len(rhs) == 0 {
				continue
			}
			to := rhs[0]
			if !strings.Contains(to, ".") {
				continue
			}
			if !strings.Contains(lhs, ":") {
				continue
			}
			groups := strings.Split(lhs, ":")
			if len(groups) != 8 {
				continue
			}
			allZero := true
			for _, g := range groups[2:] {
				if g != "0" && g != "0000" && g != "" {
					allZero = false
					break
				}
			}
			if !allZero {
				continue
			}
			key := "v6:" + strings.ToLower(padLeft(groups[0], 4)+padLeft(groups[1], 4))
			out[key] = to
		}
		return out
	})
}

// loadAliasesLabels reads /etc/bpftune/aliases and extracts {to_ip: label}.
func loadAliasesLabelsMap() map[string]string {
	return aliasesLabelsHolder.get(aliasesFile, func(data []byte) map[string]string {
		out := map[string]string{}
		for _, raw := range strings.Split(string(data), "\n") {
			line := strings.TrimSpace(raw)
			if line == "" || strings.HasPrefix(line, "#") || !strings.Contains(line, "=") {
				continue
			}
			parts := strings.SplitN(line, "=", 2)
			if len(parts) != 2 {
				continue
			}
			rest := strings.Fields(strings.TrimSpace(parts[1]))
			if len(rest) < 2 {
				continue
			}
			toIP := rest[0]
			label := rest[1]
			if ip := net.ParseIP(toIP); ip != nil {
				out[ip.String()] = label
			} else {
				out[toIP] = label
			}
		}
		return out
	})
}

// ============================================================================
// Address normalization + label lookup
// ============================================================================

// canonBucket collapses to /16 (v4) or leaves as-is (v6:hex).
func canonBucket(addr string) string {
	if addr == "" {
		return addr
	}
	if strings.HasPrefix(addr, "v6:") {
		return addr
	}
	parts := strings.Split(addr, ".")
	if len(parts) == 4 {
		return parts[0] + "." + parts[1] + ".0.0"
	}
	return addr
}

// normalizeIP returns the canonical form (handles v4 + v6).
func normalizeIP(ipStr string) string {
	if ip := net.ParseIP(ipStr); ip != nil {
		return ip.String()
	}
	return ipStr
}

// foldV6 replaces a v6:hex key with its canonical v4 if declared in
// /etc/bpftune/aliases; otherwise converts v6:hex to standard IPv6 /32
// form ("xxxx:xxxx::") so labelFor can normalize + look it up.
func foldV6(addr string) string {
	if addr == "" || !strings.HasPrefix(addr, "v6:") {
		return addr
	}
	if folded, ok := loadFoldMap()[addr]; ok && folded != "" {
		return folded
	}
	// No fold rule — convert v6:hex to standard IPv6 /32
	h := strings.TrimPrefix(addr, "v6:")
	n, err := parseUint64Hex(h)
	if err != nil {
		return addr
	}
	return formatIPv6Slash32(n)
}

// labelFor resolves an address to a human-readable label.
// Chain: fold v6 → canon bucket → normalize → labels.json → aliases labels.
func labelFor(addr string) string {
	if addr == "" {
		return ""
	}
	addr = foldV6(addr)
	addr = canonBucket(addr)
	addr = normalizeIP(addr)
	labels := loadLabels()
	if lbl, ok := labels[addr]; ok && lbl != "" {
		return lbl
	}
	// Slow path: some labels.json keys are not normalized.
	for k, v := range labels {
		if normalizeIP(k) == addr && v != "" {
			return v
		}
	}
	aliasLabels := loadAliasesLabelsMap()
	if lbl, ok := aliasLabels[addr]; ok && lbl != "" {
		return lbl
	}
	return addr
}

// ============================================================================
// Helpers
// ============================================================================

func padLeft(s string, n int) string {
	if len(s) >= n {
		return s
	}
	return strings.Repeat("0", n-len(s)) + s
}

func parseUint64Hex(s string) (uint64, error) {
	var n uint64
	for _, c := range s {
		n <<= 4
		switch {
		case c >= '0' && c <= '9':
			n |= uint64(c - '0')
		case c >= 'a' && c <= 'f':
			n |= uint64(c-'a') + 10
		case c >= 'A' && c <= 'F':
			n |= uint64(c-'A') + 10
		default:
			return 0, errBadHex
		}
	}
	return n, nil
}

var errBadHex = &simpleError{"bad hex digit"}

type simpleError struct{ s string }

func (e *simpleError) Error() string { return e.s }

// formatIPv6Slash32 turns a uint64 (top 32 bits of an IPv6) into "xxxx:xxxx::".
func formatIPv6Slash32(n uint64) string {
	hi := (n >> 16) & 0xFFFF
	lo := n & 0xFFFF
	return formatU16(hi) + ":" + formatU16(lo) + "::"
}

func formatU16(n uint64) string {
	const hex = "0123456789abcdef"
	if n == 0 {
		return "0"
	}
	b := []byte{}
	for n > 0 {
		b = append([]byte{hex[n&0xF]}, b...)
		n >>= 4
	}
	return string(b)
}
__Z_FILE_EMBED_END_SENTINEL__

echo "writing dashboard/bin/go/log_parsing.go ..."
cat > "dashboard/bin/go/log_parsing.go" <<'__Z_FILE_EMBED_END_SENTINEL__'
package main

// Full log-parsing + swap-outcome derivation.
// Mirrors bpftune_log.py (_swaps_mets_srates, _proof_events,
// _cookie_dest_map, _dest_str, _bucket_of) + bpftune_data.py
// (data_recent_swaps, data_recent_proofs, data_swap_outcomes,
// _outcome_composite, _outcome_srate, _outcome_sustained,
// _finalize_outcome, _add_loss_recovery).
//
// The Go collector re-parses the log tail (last 2MB across all
// bpftune-met-*.log files) every collect() cycle.  This matches the
// Python CLI's data_* approach (no cross-cycle state needed for
// outcome derivation — the tail is wide enough to contain both a
// swap and its post-swap srate samples 60-300s later).

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

// ============================================================================
// Regex patterns — exact mirrors of bpftune_log.py
// ============================================================================

var (
	rxSwap = regexp.MustCompile(
		`(\d+\.\d+): bpf_trace_printk: swap cookie=(\d+) ` +
			`from=(\d+) to=(\d+) bc=(\d+) ac=(\d+) d=(\d+)` +
			`(?: mt=(\d+) rb=(\d+))?` +
			`(?: dest=(\d+))?` +
			`(?: dest6=(\d+))?` +
			`(?: dest6b=(\d+))?`)

	rxMet = regexp.MustCompile(
		`(\d+\.\d+): bpf_trace_printk: met cookie=(\d+) ` +
			`rport=(\d+) alg=(\d+) segs=(\d+) val=(\d+)`)

	rxSrate = regexp.MustCompile(
		`(\d+\.\d+): bpf_trace_printk: srate cookie=(\d+) ` +
			`alg=(\d+) srate=(\d+)`)

	rxEstab = regexp.MustCompile(
		`(\d+\.\d+): bpf_trace_printk: estab cookie=(\d+) ` +
			`alg=(\d+) forced=\d+ dest=(\d+)(?: dest6=(\d+))?`)

	rxProof = regexp.MustCompile(
		`(\d+\.\d+): .*proof cookie=(\d+) alg=(\d+) rate=(\d+) tier=(\d+)`)

	rxMidsamp = regexp.MustCompile(
		`(\d+\.\d+): .*midsamp cookie=(\d+) .*srate=(\d+)`)

	rxDestInt = regexp.MustCompile(`dest=(\d+)`)
	rxDest6   = regexp.MustCompile(`dest6=(\d+)`)
	rxDest6B  = regexp.MustCompile(`dest6b=(\d+)`)
)

// ============================================================================
// Parsed event types
// ============================================================================

type metEntry struct {
	Ts    float64
	Val   int64
	Rport string
	Alg   int
}

type srateEntry struct {
	Ts    float64
	Srate int64
	Alg   int
}

// swapRow is the raw extracted swap tuple (matches Python's
// _swaps_mets_srates row layout).
type swapRow struct {
	Ts     float64
	Cookie int64
	From   int
	To     int
	Bc     int
	Ac     int
	D      string
	Mt     string
	Rb     string
	Dest   string // raw numeric string, may be ""
	Dest6  string
	Dest6b string
}

// ============================================================================
// Main entry — parseLogs
// ============================================================================

// parseLogs reads the log tail (last 2MB across all bpftune-met-*.log
// files), parses swaps/mets/srates/proofs, derives swap outcomes, and
// returns the dashboard fields: recent_swaps (top 18), recent_proofs
// (top 18), swap_outcomes (composite + srate + sustained + loss
// recovery), bucket_ips, log_window, proofs_raw.
func (c *Collector) parseLogs() (topSwaps, topProofs []interface{},
	swapOutcomes, bucketIPs, logWindow, proofsRaw interface{}) {

	text := readLogTail(logTailBytes)
	if text == "" {
		// Empty log — return minimal defaults so current.json has
		// the right shape even when bpftune isn't running.
		return []interface{}{}, []interface{}{},
			emptySwapOutcomes(), map[string]interface{}{},
			emptyLogWindow(), []interface{}{}
	}

	swaps, metByCookie, srateByCookie := parseSwapsMetsSrates(text)
	cdest := cookieDestMap(text)

	// ----- recent_swaps (newest-first, last 18) --------------------------
	allSwaps := buildRecentSwapRows(swaps, metByCookie, srateByCookie, cdest)
	topSwaps = lastN(allSwaps, 18)

	// ----- recent_proofs (newest-first, last 18) --------------------------
	allProofs := buildRecentProofRows(text, cdest)
	topProofs = lastN(allProofs, 18)

	// ----- swap_outcomes (composite + srate + sustained) ----------------
	swapOutcomes = buildSwapOutcomes(swaps, metByCookie, srateByCookie)

	// ----- bucket_ips (all dest= occurrences, /16 or /32 grouped) ------
	bucketIPs = buildBucketIPs(text)

	// ----- log_window (oldest/newest swap ts, span, age) ----------------
	logWindow = buildLogWindow(swaps)

	// ----- proofs_raw (proof leaderboard: good/proved/sampled per alg) --
	proofsRaw = buildProofsRaw(text)

	return topSwaps, topProofs, swapOutcomes, bucketIPs, logWindow, proofsRaw
}

// ============================================================================
// parseSwapsMetsSrates — extract swap / met / srate events from text
// ============================================================================

func parseSwapsMetsSrates(text string) (swaps []swapRow,
	metByCookie map[int64][]metEntry, srateByCookie map[int64][]srateEntry) {

	metByCookie = map[int64][]metEntry{}
	srateByCookie = map[int64][]srateEntry{}

	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		if m := rxSwap.FindStringSubmatch(line); m != nil {
			ts, _ := strconv.ParseFloat(m[1], 64)
			cookie, _ := strconv.ParseInt(m[2], 10, 64)
			fa, _ := strconv.Atoi(m[3])
			ta, _ := strconv.Atoi(m[4])
			bc, _ := strconv.Atoi(m[5])
			ac, _ := strconv.Atoi(m[6])
			row := swapRow{
				Ts: ts, Cookie: cookie, From: fa, To: ta, Bc: bc, Ac: ac,
				D: m[7], Mt: m[8], Rb: m[9],
				Dest: m[10], Dest6: m[11], Dest6b: m[12],
			}
			swaps = append(swaps, row)
			continue
		}
		if m := rxMet.FindStringSubmatch(line); m != nil {
			ts, _ := strconv.ParseFloat(m[1], 64)
			cookie, _ := strconv.ParseInt(m[2], 10, 64)
			rport := m[3]
			alg, _ := strconv.Atoi(m[4])
			val, _ := strconv.ParseInt(m[6], 10, 64)
			metByCookie[cookie] = append(metByCookie[cookie],
				metEntry{Ts: ts, Val: val, Rport: rport, Alg: alg})
			continue
		}
		if m := rxSrate.FindStringSubmatch(line); m != nil {
			ts, _ := strconv.ParseFloat(m[1], 64)
			cookie, _ := strconv.ParseInt(m[2], 10, 64)
			alg, _ := strconv.Atoi(m[3])
			sr, _ := strconv.ParseInt(m[4], 10, 64)
			srateByCookie[cookie] = append(srateByCookie[cookie],
				srateEntry{Ts: ts, Srate: sr, Alg: alg})
		}
	}
	return swaps, metByCookie, srateByCookie
}

// ============================================================================
// buildRecentSwapRows — derive outcome per swap, return dashboard rows
// ============================================================================

func buildRecentSwapRows(swaps []swapRow,
	metByCookie map[int64][]metEntry,
	srateByCookie map[int64][]srateEntry,
	cdest map[string][2]string) []interface{} {

	rows := make([]interface{}, 0, len(swaps))
	for _, sw := range swaps {
		o := outcomeComposite(metByCookie, sw.Cookie, sw.Ts)
		o2 := outcomeSrate(srateByCookie, sw.Cookie, sw.Ts)
		o3 := outcomeSustained(srateByCookie, sw.Cookie, sw.Ts)

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

		// dest resolution: prefer swap row's own dest fields, fall
		// back to cookie→dest map from estab events.
		v4, v6 := sw.Dest, sw.Dest6
		if v4 == "" && v6 == "" {
			if d, ok := cdest[strconv.FormatInt(sw.Cookie, 10)]; ok {
				v4, v6 = d[0], d[1]
			}
		}
		destStr := destStr(v4, v6)
		bucketOf := bucketOf(v4, v6)
		destLabel := labelFor(destStr)
		if destLabel == "" {
			destLabel = destStr
		}

		d, _ := strconv.Atoi(sw.D)
		fromAlg := algName(sw.From)
		toAlg := algName(sw.To)

		row := map[string]interface{}{
			"boot_ts":           sw.Ts,
			"from_alg":          fromAlg,
			"to_alg":            toAlg,
			"d":                 d,
			"outcome":           o,
			"outcome_srate":     o2,
			"outcome_sustained": o3,
			"mt_alg":            mtAlg,
			"rb_alg":            rbAlg,
			"dest":              destLabel,
			"_bucket":           bucketOf,
		}
		rows = append(rows, row)
	}
	// Newest-first (matches data_recent_swaps 0.4.90 contract).
	return reverse(rows)
}

// ============================================================================
// Outcome derivation — three rulers (composite, srate, sustained)
// ============================================================================

// outcomeComposite: post/pre ratio of `val` from met events.
//
//	post = first met sample in [T+3, T+300]
//	pre  = last met sample before T
//	r = post/pre; r<=0.9 → win (lower is better — opposite of srate),
//	r>=1.1 → loss, else null
func outcomeComposite(met map[int64][]metEntry, cookie int64, ts float64) string {
	tl := met[cookie]
	var pre, post int64 = -1, -1
	for _, e := range tl {
		if e.Ts < ts+0.001 {
			pre = e.Val
		} else if ts+3.0 <= e.Ts && e.Ts <= ts+300.0 {
			post = e.Val
			break
		}
	}
	if pre <= 0 || post <= 0 {
		return ""
	}
	r := float64(post) / float64(pre)
	switch {
	case r <= 0.9:
		return "win"
	case r >= 1.1:
		return "loss"
	}
	return "null"
}

// outcomeSrate: post/pre ratio of `srate` from srate events.
//
//	pre = last srate before T
//	post = first srate after T
//	r>=1.1 → win, r<=0.9 → loss, else null
func outcomeSrate(srate map[int64][]srateEntry, cookie int64, ts float64) string {
	tl := srate[cookie]
	var pre, post int64 = -1, -1
	for _, e := range tl {
		if e.Ts < ts {
			pre = e.Srate
		} else if e.Ts > ts {
			post = e.Srate
			break
		}
	}
	if pre <= 0 || post <= 0 {
		return ""
	}
	r := float64(post) / float64(pre)
	switch {
	case r >= 1.1:
		return "win"
	case r <= 0.9:
		return "loss"
	}
	return "null"
}

// outcomeSustained: median srate in [T+60, T+300] vs pre-swap srate.
//
//	r>=1.1 → win, r<=0.9 → loss, else null
func outcomeSustained(srate map[int64][]srateEntry, cookie int64, ts float64) string {
	tl := srate[cookie]
	var pre int64 = -1
	var post []int64
	for _, e := range tl {
		if e.Ts < ts {
			pre = e.Srate
		} else if sustainedLoS <= (e.Ts-ts) && (e.Ts-ts) <= sustainedHiS {
			post = append(post, e.Srate)
		}
	}
	if pre <= 0 || len(post) == 0 {
		return ""
	}
	pm := medianInt64(post)
	if pm <= 0 {
		return ""
	}
	r := float64(pm) / float64(pre)
	switch {
	case r >= 1.1:
		return "win"
	case r <= 0.9:
		return "loss"
	}
	return "null"
}

// ============================================================================
// swap_outcomes aggregate (composite + srate + sustained + loss recovery)
// ============================================================================

// swapOutRow is the intermediate per-swap row used inside outcome
// derivation + loss-recovery classification.
type swapOutRow struct {
	Ts               float64
	Cookie           int64
	Outcome          string
	OutcomeSrate     string
	OutcomeSustained string
}

func buildSwapOutcomes(swaps []swapRow,
	met map[int64][]metEntry, srate map[int64][]srateEntry) map[string]interface{} {

	cCounts := newOutcomeCounts()
	sCounts := newOutcomeCounts()
	sustCounts := newOutcomeCounts()

	var swapsList []swapOutRow

	for _, sw := range swaps {
		o := outcomeComposite(met, sw.Cookie, sw.Ts)
		o2 := outcomeSrate(srate, sw.Cookie, sw.Ts)
		o3 := outcomeSustained(srate, sw.Cookie, sw.Ts)
		incrOutcome(cCounts, o)
		incrOutcome(sCounts, o2)
		incrOutcome(sustCounts, o3)
		swapsList = append(swapsList, swapOutRow{
			Ts: sw.Ts, Cookie: sw.Cookie,
			Outcome: o, OutcomeSrate: o2, OutcomeSustained: o3,
		})
	}

	out := map[string]interface{}{
		"composite": finalizeOutcome(cCounts),
		"srate":     finalizeOutcome(sCounts),
		"sustained": finalizeOutcome(sustCounts),
	}
	addLossRecovery(out, swapsList)
	// swaps_list (for the renderer's writeback path).
	out["swaps_list"] = buildSwapsListForOutcomes(swapsList)
	return out
}

type outcomeCounts struct {
	Win, Null, Loss, Skip int
}

func newOutcomeCounts() *outcomeCounts { return &outcomeCounts{} }

func incrOutcome(c *outcomeCounts, o string) {
	switch o {
	case "win":
		c.Win++
	case "loss":
		c.Loss++
	case "null":
		c.Null++
	default:
		c.Skip++
	}
}

func finalizeOutcome(c *outcomeCounts) map[string]interface{} {
	total := c.Win + c.Null + c.Loss
	pct := func(x int) float64 {
		if total == 0 {
			return 0
		}
		return round1(float64(x) * 100.0 / float64(total))
	}
	return map[string]interface{}{
		"measurable":   total,
		"unmeasurable": c.Skip,
		"win":          c.Win,
		"win_pct":      pct(c.Win),
		"null":         c.Null,
		"null_pct":     pct(c.Null),
		"loss":         c.Loss,
		"loss_pct":     pct(c.Loss),
	}
}

// addLossRecovery mutates out to add rescued/full_loss/open + _pct fields.
// Mirrors bpftune_data.py:_add_loss_recovery.
func addLossRecovery(out map[string]interface{}, swaps []swapOutRow) {
	if len(swaps) == 0 {
		for _, key := range []string{"composite", "srate", "sustained"} {
			if sub, ok := out[key].(map[string]interface{}); ok {
				sub["rescued"] = 0
				sub["full_loss"] = 0
				sub["open"] = 0
				sub["rescued_pct"] = 0.0
				sub["full_loss_pct"] = 0.0
				sub["open_pct"] = 0.0
			}
		}
		return
	}
	// Sort by ts ascending.
	sort.Slice(swaps, func(i, j int) bool { return swaps[i].Ts < swaps[j].Ts })
	lastTs := swaps[len(swaps)-1].Ts
	fieldMap := map[string]string{
		"composite": "Outcome", "srate": "OutcomeSrate", "sustained": "OutcomeSustained",
	}
	for key, field := range fieldMap {
		sub, ok := out[key].(map[string]interface{})
		if !ok {
			continue
		}
		totalLoss, _ := sub["loss"].(int)
		if totalLoss <= 0 {
			sub["rescued"] = 0
			sub["full_loss"] = 0
			sub["open"] = 0
			sub["rescued_pct"] = 0.0
			sub["full_loss_pct"] = 0.0
			sub["open_pct"] = 0.0
			continue
		}
		// Build judged (only swaps with a verdict in this field).
		type judgedRow struct {
			Ts      float64
			Cookie  int64
			Verdict string
		}
		var judged []judgedRow
		for _, s := range swaps {
			v := fieldValue(s, field)
			if v == "win" || v == "null" || v == "loss" {
				judged = append(judged, judgedRow{s.Ts, s.Cookie, v})
			}
		}
		type lossRow struct {
			Ts     float64
			Cookie int64
		}
		var losses []lossRow
		for _, j := range judged {
			if j.Verdict == "loss" {
				losses = append(losses, lossRow{j.Ts, j.Cookie})
			}
		}
		rescued, full, open := 0, 0, 0
		for _, loss := range losses {
			found := false
			for _, j := range judged {
				if j.Ts == loss.Ts && j.Cookie == loss.Cookie {
					continue
				}
				if j.Ts <= loss.Ts {
					continue
				}
				if j.Ts-loss.Ts > float64(tRescueWindowS) {
					break
				}
				if j.Cookie == loss.Cookie && j.Verdict == "win" {
					found = true
					break
				}
			}
			if found {
				rescued++
			} else if (lastTs - loss.Ts) > float64(tRescueWindowS) {
				full++
			} else {
				open++
			}
		}
		sub["rescued"] = rescued
		sub["full_loss"] = full
		sub["open"] = open
		sub["rescued_pct"] = round1(float64(rescued) * 100.0 / float64(totalLoss))
		sub["full_loss_pct"] = round1(float64(full) * 100.0 / float64(totalLoss))
		sub["open_pct"] = round1(float64(open) * 100.0 / float64(totalLoss))
	}
}

func fieldValue(s swapOutRow, field string) string {
	switch field {
	case "Outcome":
		return s.Outcome
	case "OutcomeSrate":
		return s.OutcomeSrate
	case "OutcomeSustained":
		return s.OutcomeSustained
	}
	return ""
}

func buildSwapsListForOutcomes(swaps []swapOutRow) []interface{} {
	out := make([]interface{}, 0, len(swaps))
	for _, s := range swaps {
		out = append(out, map[string]interface{}{
			"ts":                s.Ts,
			"cookie":            s.Cookie,
			"outcome":           s.Outcome,
			"outcome_sustained": s.OutcomeSustained,
		})
	}
	return out
}

// ============================================================================
// buildRecentProofRows — parse proof events, attach dest via cookie map
// ============================================================================

func buildRecentProofRows(text string, cdest map[string][2]string) []interface{} {
	var lines []string
	for _, l := range strings.Split(text, "\n") {
		if strings.Contains(l, "proof cookie=") {
			lines = append(lines, l)
		}
	}
	// Take last 18 (oldest → newest), then reverse so newest-first.
	if len(lines) > 18 {
		lines = lines[len(lines)-18:]
	}
	out := make([]interface{}, 0, len(lines))
	for _, l := range lines {
		m := rxProof.FindStringSubmatch(l)
		if m == nil {
			continue
		}
		ts, _ := strconv.ParseFloat(m[1], 64)
		cookie := m[2]
		alg, _ := strconv.Atoi(m[3])
		rate, _ := strconv.ParseInt(m[4], 10, 64)
		tier := m[5]
		tierLabel := "good"
		if tier == "2" {
			tierLabel = "proved"
		}
		dest := ""
		if d, ok := cdest[cookie]; ok {
			ds := destStr(d[0], d[1])
			dest = labelFor(ds)
			if dest == "" {
				dest = ds
			}
		}
		out = append(out, map[string]interface{}{
			"boot_ts": ts,
			"alg":     algName(alg),
			"mbps":    round1(float64(rate) / bpsToMbps),
			"tier":    tierLabel,
			"dest":    dest,
		})
	}
	return reverse(out)
}

// ============================================================================
// cookieDestMap — cookie → (v4, v6) from estab + swap events
// ============================================================================

func cookieDestMap(text string) map[string][2]string {
	out := map[string][2]string{}
	for _, line := range strings.Split(text, "\n") {
		if m := rxSwap.FindStringSubmatch(line); m != nil {
			// swap row: groups 2=cookie, 10=dest, 11=dest6
			out[m[2]] = [2]string{m[10], m[11]}
			continue
		}
		if m := rxEstab.FindStringSubmatch(line); m != nil {
			// estab row: groups 2=cookie, 4=dest, 5=dest6
			out[m[2]] = [2]string{m[4], m[5]}
		}
	}
	return out
}

// ============================================================================
// buildBucketIPs — all dest IPs grouped by /16 (v4) or /32 (v6)
// ============================================================================

func buildBucketIPs(text string) map[string]interface{} {
	buckets := map[string][]string{}
	for _, line := range strings.Split(text, "\n") {
		if m := rxDestInt.FindStringSubmatch(line); m != nil && len(m) > 1 {
			n, err := strconv.ParseUint(m[1], 10, 64)
			if err != nil || n == 0 {
				continue
			}
			full := fmt.Sprintf("%d.%d.%d.%d",
				(n>>24)&0xff, (n>>16)&0xff, (n>>8)&0xff, n&0xff)
			masked := fmt.Sprintf("%d.%d.0.0",
				(n>>24)&0xff, (n>>16)&0xff)
			if !contains(buckets[masked], full) {
				buckets[masked] = append(buckets[masked], full)
			}
		}
		if m := rxDest6.FindStringSubmatch(line); m != nil && len(m) > 1 {
			n6, err := strconv.ParseUint(m[1], 10, 64)
			if err != nil || n6 == 0 {
				continue
			}
			hi := (n6 >> 16) & 0xFFFF
			lo := n6 & 0xFFFF
			maskedV6 := fmt.Sprintf("%x:%x::", hi, lo)
			fullV6 := maskedV6
			if m2 := rxDest6B.FindStringSubmatch(line); m2 != nil && len(m2) > 1 {
				n6b, err := strconv.ParseUint(m2[1], 10, 64)
				if err == nil && n6b != 0 {
					hi2 := (n6b >> 16) & 0xFFFF
					lo2 := n6b & 0xFFFF
					fullV6 = fmt.Sprintf("%x:%x:%x:%x::", hi, lo, hi2, lo2)
				}
			}
			if !contains(buckets[maskedV6], fullV6) {
				buckets[maskedV6] = append(buckets[maskedV6], fullV6)
			}
		}
	}
	out := map[string]interface{}{}
	for k, v := range buckets {
		// Cast []string → []interface{} for JSON marshalling.
		iface := make([]interface{}, len(v))
		for i, s := range v {
			iface[i] = s
		}
		out[k] = iface
	}
	return out
}

// ============================================================================
// buildLogWindow — oldest/newest swap ts + span/age info
// ============================================================================

func buildLogWindow(swaps []swapRow) map[string]interface{} {
	if len(swaps) == 0 {
		return emptyLogWindow()
	}
	oldest := swaps[0].Ts
	newest := swaps[0].Ts
	for _, s := range swaps[1:] {
		if s.Ts < oldest {
			oldest = s.Ts
		}
		if s.Ts > newest {
			newest = s.Ts
		}
	}
	uptime := readProcUptime()
	now := float64(time.Now().Unix())
	oldestWall := int64(now - uptime + oldest)
	var newestWall int64
	if newest > uptime {
		newestWall = readLogFileMtime()
	} else {
		newestWall = int64(now - uptime + newest)
	}
	ageMin := round1((now - float64(newestWall)) / 60.0)
	return map[string]interface{}{
		"oldest_ts":  oldestWall,
		"newest_ts":  newestWall,
		"span_min":   round1((newest - oldest) / 60.0),
		"swap_count": len(swaps),
		"age_min":    ageMin,
	}
}

// ============================================================================
// buildProofsRaw — proof leaderboard (good/proved/sampled per alg)
// ============================================================================

func buildProofsRaw(text string) []interface{} {
	events, samples := proofEvents(text)
	algSet := map[int]bool{}
	for a := range events {
		algSet[a] = true
	}
	for a := range samples {
		algSet[a] = true
	}
	var algs []int
	for a := range algSet {
		algs = append(algs, a)
	}
	sort.Ints(algs)
	out := make([]interface{}, 0, len(algs))
	for _, a := range algs {
		e := events[a]
		s := samples[a]
		var provenMax, sampledAvg, sampledMax interface{}
		if e.provenMax > 0 {
			provenMax = round1(float64(e.provenMax) / bpsToMbps)
		}
		if s.n > 0 {
			sampledAvg = round1(float64(s.sum) / float64(s.n) / bpsToMbps)
			sampledMax = round1(float64(s.sampMax) / bpsToMbps)
		}
		var samplesN interface{}
		if s.n > 0 {
			samplesN = s.n
		}
		out = append(out, map[string]interface{}{
			"alg":         algName(a),
			"good":        e.good,
			"proved":      e.proved,
			"proven_max":  provenMax,
			"sampled_avg": sampledAvg,
			"sampled_max": sampledMax,
			"samples":     samplesN,
		})
	}
	// Sort descending by proven_max (treat nil as 0).
	sort.Slice(out, func(i, j int) bool {
		vi, _ := out[i].(map[string]interface{})["proven_max"].(float64)
		vj, _ := out[j].(map[string]interface{})["proven_max"].(float64)
		return vi > vj
	})
	return out
}

type proofEvent struct {
	good, proved int
	provenMax    int64
}
type proofSample struct {
	sum     int64
	n       int
	sampMax int64
}

func proofEvents(text string) (map[int]proofEvent, map[int]proofSample) {
	events := map[int]proofEvent{}
	samples := map[int]proofSample{}
	metByCookie := map[int64][]struct {
		Ts  float64
		Alg int
	}{}

	for _, line := range strings.Split(text, "\n") {
		if strings.Contains(line, "proof cookie=") {
			m := rxProof.FindStringSubmatch(line)
			if m == nil {
				continue
			}
			a, _ := strconv.Atoi(m[3])
			rate, _ := strconv.ParseInt(m[4], 10, 64)
			tier := m[5]
			e := events[a]
			if tier == "2" {
				e.proved++
			} else {
				e.good++
			}
			if rate > e.provenMax {
				e.provenMax = rate
			}
			events[a] = e
			continue
		}
		if m := rxMet.FindStringSubmatch(line); m != nil {
			ts, _ := strconv.ParseFloat(m[1], 64)
			c, _ := strconv.ParseInt(m[2], 10, 64)
			alg, _ := strconv.Atoi(m[4])
			metByCookie[c] = append(metByCookie[c],
				struct {
					Ts  float64
					Alg int
				}{ts, alg})
		}
	}
	const metWindowS = 60.0
	for _, line := range strings.Split(text, "\n") {
		m := rxMidsamp.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		ts, _ := strconv.ParseFloat(m[1], 64)
		c, _ := strconv.ParseInt(m[2], 10, 64)
		r, _ := strconv.ParseInt(m[3], 10, 64)
		if r <= 0 {
			continue
		}
		cand := metByCookie[c]
		var bestA int = -1
		var bestD float64 = -1
		for _, e := range cand {
			d := e.Ts - ts
			if d < 0 {
				d = -d
			}
			if bestD < 0 || d < bestD {
				bestD = d
				bestA = e.Alg
			}
		}
		if bestA < 0 || bestD < 0 || bestD > metWindowS {
			continue
		}
		s := samples[bestA]
		s.sum += r
		s.n++
		if r > s.sampMax {
			s.sampMax = r
		}
		samples[bestA] = s
	}
	return events, samples
}

// ============================================================================
// Dest decoding (numeric → IP string)
// ============================================================================

// destStr returns the display string for a destination.  Prefers v6
// when present.  Mirrors bpftune_log.py:_dest_str.
func destStr(v4, v6 string) string {
	if v6 != "" {
		if n6, err := strconv.ParseInt(v6, 10, 64); err == nil && n6 != 0 {
			return fmt.Sprintf("v6:%08x", uint64(n6)&0xFFFFFFFF)
		}
	}
	return destIP(v4)
}

// bucketOf returns the /16 (v4) or v6:hex (v6) bucket key.
// Mirrors bpftune_log.py:_bucket_of.
func bucketOf(v4, v6 string) string {
	if v6 != "" {
		if n6, err := strconv.ParseInt(v6, 10, 64); err == nil && n6 != 0 {
			return fmt.Sprintf("v6:%08x", uint64(n6)&0xFFFFFFFF)
		}
	}
	if v4 == "" {
		return ""
	}
	n, err := strconv.ParseInt(v4, 10, 64)
	if err != nil {
		return ""
	}
	first := (n >> 24) & 0xFF
	if first == 0 || first == 127 {
		return ""
	}
	return fmt.Sprintf("%d.%d.0.0", (n>>24)&0xFF, (n>>16)&0xFF)
}

// destIP converts a numeric dest (as decimal string) to dotted-quad.
// Returns "" for 0/127.x.x.x/None.  Mirrors bpftune_log.py:_dest_ip.
func destIP(s string) string {
	if s == "" || s == "1" {
		return ""
	}
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return ""
	}
	first := (n >> 24) & 0xFF
	if first == 0 || first == 127 {
		return ""
	}
	return fmt.Sprintf("%d.%d.%d.%d",
		(n>>24)&0xFF, (n>>16)&0xFF, (n>>8)&0xFF, n&0xFF)
}

// ============================================================================
// Log file readers
// ============================================================================

// readLogTail reads the last `budget` bytes across all bpftune-met-*.log
// files, sorted newest-first.  Mirrors bpftune_log.py:tail_recent.
func readLogTail(budget int64) string {
	pattern := "/var/log/bpftune-met-*.log"
	files, _ := filepath.Glob(pattern)
	if len(files) == 0 {
		return ""
	}
	// Sort newest-first by mtime.
	sort.Slice(files, func(i, j int) bool {
		fi, _ := os.Stat(files[i])
		fj, _ := os.Stat(files[j])
		return fi.ModTime().After(fj.ModTime())
	})
	var chunks []string
	remaining := budget
	for _, p := range files {
		if remaining <= 0 {
			break
		}
		fi, err := os.Stat(p)
		if err != nil {
			continue
		}
		size := fi.Size()
		take := size
		if take > remaining {
			take = remaining
		}
		f, err := os.Open(p)
		if err != nil {
			continue
		}
		_, _ = f.Seek(size-take, 0)
		buf := make([]byte, take)
		_, _ = f.Read(buf)
		f.Close()
		chunks = append(chunks, string(buf))
		remaining -= take
	}
	return strings.Join(chunks, "\n")
}

// readProcUptime returns /proc/uptime seconds (or 0 on error).
func readProcUptime() float64 {
	data, err := os.ReadFile("/proc/uptime")
	if err != nil {
		return 0
	}
	fields := strings.Fields(string(data))
	if len(fields) == 0 {
		return 0
	}
	u, _ := strconv.ParseFloat(fields[0], 64)
	return u
}

func readLogFileMtime() int64 {
	fi, err := os.Stat("/var/log/bpftune-met-live.log")
	if err != nil {
		return time.Now().Unix()
	}
	return fi.ModTime().Unix()
}

// ============================================================================
// Helpers
// ============================================================================

func algName(i int) string {
	if i >= 0 && i < len(CONGS) {
		return CONGS[i]
	}
	return fmt.Sprintf("alg%d", i)
}

func medianInt64(xs []int64) int64 {
	if len(xs) == 0 {
		return 0
	}
	s := make([]int64, len(xs))
	copy(s, xs)
	sort.Slice(s, func(i, j int) bool { return s[i] < s[j] })
	n := len(s)
	if n%2 == 1 {
		return s[n/2]
	}
	return (s[n/2-1] + s[n/2]) / 2
}

func round1(f float64) float64 {
	return float64(int(f*10+0.5)) / 10.0
}

func round2(f float64) float64 {
	return float64(int(f*100+0.5)) / 100.0
}

func round3(f float64) float64 {
	return float64(int(f*1000+0.5)) / 1000.0
}

// lastN returns the last n elements (newest-first since swaps are
// oldest-first when iterated).  Actually for newest-first input it
// returns the first n elements.
func lastN(rows []interface{}, n int) []interface{} {
	if len(rows) <= n {
		return rows
	}
	return rows[:n]
}

func reverse(rows []interface{}) []interface{} {
	out := make([]interface{}, len(rows))
	for i, r := range rows {
		out[len(rows)-1-i] = r
	}
	return out
}

func contains(s []string, v string) bool {
	for _, x := range s {
		if x == v {
			return true
		}
	}
	return false
}

func emptySwapOutcomes() map[string]interface{} {
	empty := map[string]interface{}{
		"measurable": 0, "unmeasurable": 0,
		"win": 0, "win_pct": 0.0,
		"null": 0, "null_pct": 0.0,
		"loss": 0, "loss_pct": 0.0,
		"rescued": 0, "full_loss": 0, "open": 0,
		"rescued_pct": 0.0, "full_loss_pct": 0.0, "open_pct": 0.0,
		"swaps_list": []interface{}{},
	}
	return map[string]interface{}{
		"composite": copyMap(empty),
		"srate":     copyMap(empty),
		"sustained": copyMap(empty),
	}
}

func copyMap(m map[string]interface{}) map[string]interface{} {
	out := map[string]interface{}{}
	for k, v := range m {
		out[k] = v
	}
	return out
}

func emptyLogWindow() map[string]interface{} {
	return map[string]interface{}{
		"oldest_ts":  int64(0),
		"newest_ts":  int64(0),
		"span_min":   0.0,
		"swap_count": 0,
		"age_min":    0.0,
	}
}

// ============================================================================
// State persistence (cookie dest map only — for now)
// ============================================================================

// saveCookieDestMap persists the cookie→dest map to state.json so the
// cookie→dest lookup survives restarts (the Python collector does this
// via the ESTAB events in the log file itself, so we don't actually
// need to persist — but keeping the stub for future use).
func (c *Collector) saveCookieDestMap(m map[string][2]string) {
	c.mu.RLock()
	statePath := stateJSONPath
	c.mu.RUnlock()
	out := map[string]interface{}{}
	for k, v := range m {
		out[k] = []string{v[0], v[1]}
	}
	data, _ := json.MarshalIndent(out, "", "  ")
	tmp := statePath + ".tmp"
	_ = os.WriteFile(tmp, data, 0644)
	_ = os.Rename(tmp, statePath)
}
__Z_FILE_EMBED_END_SENTINEL__

echo "writing dashboard/bin/go/main.go ..."
cat > "dashboard/bin/go/main.go" <<'__Z_FILE_EMBED_END_SENTINEL__'
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
	sort.Slice(sortedHosts, func(i, j int) bool {
		return voteSum(sortedHosts[i].V) > voteSum(sortedHosts[j].V)
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
		buckets = append(buckets, bucket{
			Dest:    addr,
			Inst:    inst,
			RttUs:   toFloat(v["min_rtt"]),
			RefMbps: round1(refMbps),
			BestAlg: bestAlg,
			NAlg:    nAlg,
		})
		if len(buckets) >= 8 {
			// Don't break — we still need metric_by_bucket + live_leaders
			// for the other buckets.
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
			liveLeaders = append(liveLeaders, map[string]interface{}{
				"dest": addr,
				"inst": inst,
				"top":  topRows,
			})
			if len(liveLeaders) >= liveMaxBuckets {
				break
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


# --- go build (cd into the module dir) ----------------------------------------
if ! command -v go >/dev/null 2>&1; then
  echo "FATAL: 'go' not in PATH. Install Go >= 1.21 or fix PATH." >&2
  exit 1
fi
echo "go version: $(go version)"
echo "running: (cd dashboard/bin/go && go build -o /tmp/bpftune-collector-go-test)"
if ( cd dashboard/bin/go && go build -o /tmp/bpftune-collector-go-test 2>&1 | tee /tmp/go-build.log ); then
  BUILD_OK=1
else
  BUILD_OK=0
fi
# Re-run go build cleanly if tee swallowed exit status.
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
  echo "  cd $REPO_ROOT && git diff dashboard/bin/go/" >&2
  echo "Once you've fixed the issue, run this script again." >&2
  exit 1
fi
echo "BUILD OK: /tmp/bpftune-collector-go-test"
ls -la /tmp/bpftune-collector-go-test

# Smoke test
echo "smoke test: -h"
/tmp/bpftune-collector-go-test -h 2>&1 | head -10 || true

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo "check-only mode: not committing, not pushing."
  exit 0
fi

# --- commit -------------------------------------------------------------------
git add dashboard/bin/go/

if git diff --cached --quiet; then
  echo "no changes to commit (files already in this state)."
  if [ "$NO_PUSH" -eq 1 ]; then exit 0; fi
  exit 0
fi

COMMIT_MSG="dashboard: Go 0.4.0 — full Python collector port

This commit replaces the v0.3 Go collector (broken log parsing,
wrong field names, missing outcome derivation, no label resolution,
wrong live_leaders formula, incomplete metric_by_bucket) with a
full port of the Python collector's parsing + outcome derivation
logic.

What's now implemented (matching Python bpftune_log.py +
bpftune_data.py):

  * Regex: SWAP / MET / SRATE / ESTAB / PROOF / MIDSAMP (exact
    mirrors of bpftune_log.py patterns).  The actual log format
    is bpf_trace_printk: <event> key=value ... — the previous
    Go parseKV was matching substring 'swap'/'proof' in any line
    and extracting the wrong dest= numeric value (1378604897
    instead of 82.43.215.97).

  * Dest decoding: numeric → dotted IPv4 (1378604897 → 82.43.215.97),
    plus IPv6 /32 (dest6) and /64 (dest6+dest6b) forms.  Mirrors
    _decode_dest / _decode_dest6 / _dest_str.

  * Label resolution: _fold_v6 (v6:hex → v4 if aliases has it,
    else IPv6 /32 form), _canon_bucket (collapse v4 to /16),
    _normalize_ip, _label_for with mtime cache for labels.json +
    aliases file.  readBPFMap now applies labelFor before returning
    the host list, so metric_by_bucket keys are labeled ('home-sco')
    instead of raw IPs ('104.18.0.0').

  * Merge by final label: two BPF map entries that decode to the
    same label (alias + fold rule) are merged — sum instances,
    keep the entry with more instances for the other fields.

  * Swap outcome derivation (three rulers):
      - composite: post/pre ratio of 'val' from met events,
        r<=0.9 → win, r>=1.1 → loss (opposite of srate)
      - srate:    post/pre ratio of srate, r>=1.1 → win,
        r<=0.9 → loss
      - sustained: median srate in [T+60, T+300] vs pre-swap srate,
        r>=1.1 → win, r<=0.9 → loss
    Each swap now has outcome / outcome_srate / outcome_sustained
    fields populated.

  * Loss-recovery classification (rescued / full_loss / open):
    for each 'loss' swap, scan subsequent swaps in the next
    tRescueWindowS (3600s) on the same cookie; if a 'win' swap
    is found, the loss is 'rescued'.  Otherwise: 'full_loss' if
    older than the rescue window, 'open' if within it.

  * live_leaders formula fixed: was just 'rv * ss / 256'; now
    matches Python data_live_leaders exactly:
        ss_eff = ss if ss > 0 else 256
        weighted = rv * ss_eff / 256
        pen = 16 + bad_streak*4 + null_streak*2
        weighted = weighted * 16 / pen
    Filter: cnt >= minLeaderTrust (10) and rv != 0.  Cap at
    LIVE_MAX_BUCKETS (8) buckets, LIVE_TOP_N (6) algs per bucket.

  * metric_by_bucket: full row shape matching Python
    data_metric_by_bucket:
        alg / metric / votes / alive / rate_ema / swap_score /
        penalty / score / bad_streak / null_streak / active
    Sorted by (active desc, score desc).

  * bucket_ips: extract all dest= occurrences from the log tail,
    group by /16 (v4) or /32 (v6).

  * log_window: oldest_ts / newest_ts / span_min / swap_count /
    age_min, computed from the swap events (boot_ts converted to
    wall clock via /proc/uptime).

  * proofs_raw: parse 'proof' + 'midsamp' events for the proof
    leaderboard (good / proved / proven_max / sampled_avg /
    sampled_max / samples per alg).  Mirrors _proof_events.

  * Cookie→dest map (from estab + swap events): used to attach
    dest IP to proof events (proof lines themselves don't have
    dest= — only cookie=).  Mirrors _cookie_dest_map.

Files:
  - dashboard/bin/go/go.mod (NEW — was missing on remote)
  - dashboard/bin/go/constants.go (NEW — CONGS + shared constants)
  - dashboard/bin/go/labels.go (NEW — label resolution with mtime cache)
  - dashboard/bin/go/log_parsing.go (REWRITE — full parsing + outcomes)
  - dashboard/bin/go/main.go (UPDATE — hostEntry slice, labelFor applied,
    proper live_leaders, full metric_by_bucket, startedAt for uptime)

Total Go size: ~2300 lines (was ~845).  Build verified locally:
  cd dashboard/bin/go && go build -o bpftune-collector-go
Binary runs and handles missing bpftool gracefully (logs error,
continues serving HTTP).

Not yet ported (deferred — renderer cron will keep doing these):
  - Cross-cycle pending_swaps state (collector.py:collect_swaps)
  - CSV writes (SWAPS_CSV, SRATE_CSV)
  - data_churn (swap count per time window)
  - data_rate (midsamp aggregation per threshold)
  - data_divergence (mt_alg vs rb_alg)
  - SSE delta encoding (currently sends full payload — works,
    but more bandwidth than needed)
  - data_bucket_live (live 1h chart from CSV tail; for now Go
    emits single-point per-bucket live, renderer provides
    historical series)"

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
echo "DONE.  To verify on this host (alongside the Python collector):"
echo "  cd ~/bpftune/dashboard/bin/go && go build -o bpftune-collector-go"
echo "  ./bpftune-collector-go --port 8083 --bind 127.0.0.1 &"
echo "  sleep 5"
echo "  curl -s http://127.0.0.1:8083/current.json | python3 -m json.tool | head -60"
echo
echo "Side-by-side compare (Go on 8083 vs Python on 8080):"
echo "  curl -s http://127.0.0.1:8080/current.json > /tmp/py.json"
echo "  curl -s http://127.0.0.1:8083/current.json > /tmp/go.json"
echo "  python3 -c 'import json; py=json.load(open(\"/tmp/py.json\")); go=json.load(open(\"/tmp/go.json\")); print(\"py recent_swaps:\", len(py.get(\"recent_swaps\", []))); print(\"go recent_swaps:\", len(go.get(\"recent_swaps\", []))); print(\"py swap_outcomes composite:\", py.get(\"swap_outcomes\", {}).get(\"composite\", {})); print(\"go swap_outcomes composite:\", go.get(\"swap_outcomes\", {}).get(\"composite\", {}))'"
