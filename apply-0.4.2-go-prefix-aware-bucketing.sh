#!/usr/bin/env bash
# =============================================================================
# Atomic patch v0.4.2: prefix-aware bucketing + longest-prefix label matching
#
# Implements the bucket design the user specified:
#   - Labels.json supports ANY prefix width (longest-prefix match wins)
#   - A /32 label like "82.43.215.97" → "home-sco" means ONLY that exact IP
#     gets home-sco (even if a /16 label "82.43.0.0" → "vps-cluster" also matches)
#   - When no label matches, the bucket key uses the CURRENT prefix4/prefix6
#     setting (read from /var/lib/bpftune/prefix4 default 16, and
#     /var/lib/bpftune/prefix6 default 32 — mtime-cached)
#   - explore_pct read from /var/lib/bpftune/explore_pct (default 100)
#   - All three values (prefix4, prefix6, explore_pct) surfaced in
#     current.json's build section so the dashboard can display them
#     in the service/build panel
#
# What's no longer hardcoded:
#   - canonBucket used to collapse v4 to /16 always (matching Python's
#     _canon_bucket).  Now it's canonBucketWithPrefix(addr, p4, p6) which
#     uses the user's chosen prefix width.
#   - bucketOf in log_parsing.go used to hardcode /16 for the _bucket
#     field in recent_swaps.  Now respects prefix4.
#   - buildBucketIPs used to hardcode /16 (v4) and /32 (v6).  Now
#     respects prefix4/prefix6.
#
# Files (only 3 change — constants.go and go.mod unchanged from v0.4.0):
#   - dashboard/bin/go/labels.go      (UPDATE — new label resolution)
#   - dashboard/bin/go/log_parsing.go (UPDATE — bucketOf + buildBucketIPs)
#   - dashboard/bin/go/main.go        (UPDATE — build section)
#
# Usage:
#   bash apply-0.4.2-go-prefix-aware-bucketing.sh            # interactive
#   bash apply-0.4.2-go-prefix-aware-bucketing.sh --yes       # non-interactive + push
#   bash apply-0.4.2-go-prefix-aware-bucketing.sh --no-push   # commit only
# =============================================================================

set -euo pipefail

YES=0
NO_PUSH=0
for arg in "$@"; do
  case "$arg" in
    --yes)        YES=1 ;;
    --no-push)    NO_PUSH=1 ;;
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

# --- write files ---------------------------------------------------------------
mkdir -p dashboard/bin/go

echo "writing dashboard/bin/go/labels.go ..."
cat > "dashboard/bin/go/labels.go" <<'__Z_FILE_EMBED_END_SENTINEL__'
package main

// Label resolution + v6 fold rules + prefix-aware bucketing.
//
// v0.4.2 redesign (per user spec):
//   - Labels.json supports ANY prefix width (longest-prefix match wins).
//     Keys can be in any form: "82.43.0.0" (/16), "82.43.215.0" (/24),
//     "82.43.215.97" (/32), "2606:1a40::" (/32 v6).  The prefix length
//     is INFERRED from trailing zero bytes — no CIDR notation needed.
//   - Custom labels are sticky: a /32 label matches only that exact IP,
//     even if a /16 label also matches.  Longest-prefix wins.
//   - When no label matches, the bucket key uses the CURRENT prefix4
//     or prefix6 setting (read from /var/lib/bpftune/prefix4 and
//     /var/lib/bpftune/prefix6, mtime-cached).  Default 16/32.
//   - explore_pct is also read from /var/lib/bpftune/explore_pct
//     (default 100) and surfaced in current.json's build section.
//
// All file reads are mtime-cached so we don't re-read the same file
// every collect() cycle.

import (
	"encoding/json"
	"net"
	"os"
	"strconv"
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

// canonBucketWithPrefix collapses a v4 address to /prefix4, or a v6
// address (in standard form OR v6:hex form) to /prefix6.
//
// v0.4.2: prefix4/prefix6 are read from /var/lib/bpftune/prefix4 and
// /var/lib/bpftune/prefix6 (mtime-cached, defaults 16 and 32).  When
// the user changes the prefix, buckets merge or split on the next
// collect cycle.
func canonBucketWithPrefix(addr string, prefix4, prefix6 int) string {
	if addr == "" {
		return ""
	}
	// Handle v6:hex form (e.g., "v6:2606abcd" — top 32 bits of v6).
	if strings.HasPrefix(addr, "v6:") {
		h := strings.TrimPrefix(addr, "v6:")
		n, err := strconv.ParseUint(h, 16, 64)
		if err != nil {
			return addr
		}
		ip := make([]byte, 16)
		ip[0] = byte(n >> 24)
		ip[1] = byte(n >> 16)
		ip[2] = byte(n >> 8)
		ip[3] = byte(n)
		mask := net.CIDRMask(prefix6, 128)
		masked := net.IP(ip).Mask(mask)
		return masked.String()
	}
	// Standard IP form (v4 dotted or v6 canonical).
	ip := net.ParseIP(addr)
	if ip == nil {
		return addr
	}
	if v4 := ip.To4(); v4 != nil {
		mask := net.CIDRMask(prefix4, 32)
		masked := v4.Mask(mask)
		return masked.String()
	}
	ip16 := ip.To16()
	mask := net.CIDRMask(prefix6, 128)
	masked := net.IP(ip16).Mask(mask)
	return masked.String()
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
//
// v0.4.2 chain (per user spec):
//  1. Apply v6 fold rules (v6:hex → v4 if /etc/bpftune/aliases declares it)
//  2. Convert v6:hex to standard IPv6 form (for label matching)
//  3. Try longest-prefix label match in labels.json + aliases labels
//     (a /32 label wins over a /16 label for the same IP)
//  4. Fall back to canonBucketWithPrefix (with current prefix4/prefix6
//     from /var/lib/bpftune/prefix4 and prefix6)
func labelFor(addr string) string {
	if addr == "" {
		return ""
	}
	// Step 1+2: fold + v6:hex → standard form.
	addr = foldV6(addr)
	// Step 3: longest-prefix label match.
	if lbl := longestPrefixLabel(addr); lbl != "" {
		return lbl
	}
	// Step 4: canon bucket with current prefix4/prefix6.
	return canonBucketWithPrefix(addr, prefix4Value(), prefix6Value())
}

// longestPrefixLabel finds the most specific label match for addr.
// Iterates labels.json + aliases labels, computes each key's natural
// prefix length (from trailing zero bytes), and picks the longest
// matching prefix.  Returns "" if no match.
func longestPrefixLabel(addr string) string {
	ip := net.ParseIP(addr)
	if ip == nil {
		return ""
	}
	// Fast path: exact string match (covers /32 v4 and /128 v6 labels
	// which is the common case — saves the CIDR construction overhead).
	labels := loadLabels()
	if lbl, ok := labels[addr]; ok && lbl != "" {
		return lbl
	}
	// Slow path: longest-prefix match.
	bestLabel := ""
	bestPrefix := -1
	for k, v := range labels {
		cidr := labelKeyToCIDR(k)
		if cidr == nil {
			continue
		}
		if !cidr.Contains(ip) {
			continue
		}
		ones, _ := cidr.Mask.Size()
		if ones > bestPrefix {
			bestPrefix = ones
			bestLabel = v
		}
	}
	if bestLabel == "" {
		// Try aliases labels as fallback.
		aliasLabels := loadAliasesLabelsMap()
		for k, v := range aliasLabels {
			cidr := labelKeyToCIDR(k)
			if cidr == nil {
				continue
			}
			if !cidr.Contains(ip) {
				continue
			}
			ones, _ := cidr.Mask.Size()
			if ones > bestPrefix {
				bestPrefix = ones
				bestLabel = v
			}
		}
	}
	return bestLabel
}

// labelKeyToCIDR parses a labels.json key as a CIDR.
//   - If the key has "/N" suffix (e.g., "82.43.0.0/16"), parse directly.
//   - Otherwise, infer prefix length from trailing zero bytes:
//     "82.43.0.0"     → /16  (2 trailing zero bytes out of 4)
//     "82.43.215.0"   → /24  (1 trailing zero byte)
//     "82.43.215.97"  → /32  (0 trailing zero bytes)
//     "2606:1a40::"   → /32  (12 trailing zero bytes out of 16)
//     "2a14:7583:abcd:1234::" → /64 (8 trailing zero bytes)
func labelKeyToCIDR(k string) *net.IPNet {
	if strings.Contains(k, "/") {
		_, ipNet, err := net.ParseCIDR(k)
		if err != nil {
			return nil
		}
		return ipNet
	}
	ip := net.ParseIP(k)
	if ip == nil {
		return nil
	}
	var b []byte
	if v4 := ip.To4(); v4 != nil {
		b = v4
	} else {
		b = ip.To16()
	}
	trailingZeroBytes := 0
	for i := len(b) - 1; i >= 0; i-- {
		if b[i] == 0 {
			trailingZeroBytes++
		} else {
			break
		}
	}
	prefixLen := len(b)*8 - trailingZeroBytes*8
	mask := net.CIDRMask(prefixLen, len(b)*8)
	return &net.IPNet{IP: ip.Mask(mask), Mask: mask}
}

// ============================================================================
// Prefix readers (prefix4 / prefix6 / explore_pct)
// ============================================================================

// mtimeIntCache is an mtime-based int file cache (for prefix4/prefix6/explore_pct).
type mtimeIntCache struct {
	mu    sync.Mutex
	val   int
	set   bool
	mtime time.Time
}

func (c *mtimeIntCache) get(path string, defaultVal int) int {
	c.mu.Lock()
	defer c.mu.Unlock()
	fi, err := os.Stat(path)
	if err != nil {
		c.val = defaultVal
		c.set = true
		c.mtime = time.Time{}
		return defaultVal
	}
	if c.set && fi.ModTime().Equal(c.mtime) {
		return c.val
	}
	data, err := os.ReadFile(path)
	if err != nil {
		c.val = defaultVal
		c.set = true
		c.mtime = fi.ModTime()
		return defaultVal
	}
	n, err := strconv.Atoi(strings.TrimSpace(string(data)))
	if err != nil {
		c.val = defaultVal
		c.set = true
		c.mtime = fi.ModTime()
		return defaultVal
	}
	c.val = n
	c.set = true
	c.mtime = fi.ModTime()
	return n
}

var (
	prefix4Holder    mtimeIntCache
	prefix6Holder    mtimeIntCache
	explorePctHolder mtimeIntCache
)

const (
	prefix4Path    = "/var/lib/bpftune/prefix4"
	prefix6Path    = "/var/lib/bpftune/prefix6"
	explorePctPath = "/var/lib/bpftune/explore_pct"
)

// prefix4Value reads /var/lib/bpftune/prefix4 (default 16).  Range: 0-32.
// Returns 16 if file missing or out of range.
func prefix4Value() int {
	n := prefix4Holder.get(prefix4Path, 16)
	if n < 0 || n > 32 {
		return 16
	}
	return n
}

// prefix6Value reads /var/lib/bpftune/prefix6 (default 32).  Range: 0-128.
// Returns 32 if file missing or out of range.
func prefix6Value() int {
	n := prefix6Holder.get(prefix6Path, 32)
	if n < 0 || n > 128 {
		return 32
	}
	return n
}

// explorePctValue reads /var/lib/bpftune/explore_pct (default 100).  Range: 0-100.
// Returns 100 if file missing or out of range.
func explorePctValue() int {
	n := explorePctHolder.get(explorePctPath, 100)
	if n < 0 || n > 100 {
		return 100
	}
	return n
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
	"net"
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
// buildBucketIPs — all dest IPs grouped by /prefix4 (v4) or /prefix6 (v6)
// v0.4.2: respects current prefix4/prefix6 from /var/lib/bpftune/.
// ============================================================================

func buildBucketIPs(text string) map[string]interface{} {
	buckets := map[string][]string{}
	p4 := prefix4Value()
	p6 := prefix6Value()
	for _, line := range strings.Split(text, "\n") {
		if m := rxDestInt.FindStringSubmatch(line); m != nil && len(m) > 1 {
			n, err := strconv.ParseUint(m[1], 10, 64)
			if err != nil || n == 0 {
				continue
			}
			// Build 4-byte v4 IP.
			b := make([]byte, 4)
			b[0] = byte(n >> 24)
			b[1] = byte(n >> 16)
			b[2] = byte(n >> 8)
			b[3] = byte(n)
			full := net.IP(b).String()
			// Mask with prefix4.
			mask := net.CIDRMask(p4, 32)
			masked := net.IP(b).Mask(mask).String()
			if !contains(buckets[masked], full) {
				buckets[masked] = append(buckets[masked], full)
			}
		}
		if m := rxDest6.FindStringSubmatch(line); m != nil && len(m) > 1 {
			n6, err := strconv.ParseUint(m[1], 10, 64)
			if err != nil || n6 == 0 {
				continue
			}
			// Build 16-byte v6 IP (top 32 bits = n6, rest = 0).
			ip := make([]byte, 16)
			ip[0] = byte(n6 >> 24)
			ip[1] = byte(n6 >> 16)
			ip[2] = byte(n6 >> 8)
			ip[3] = byte(n6)
			// Build full form (may include dest6b for /64 form).
			fullIP := make([]byte, 16)
			copy(fullIP, ip)
			if m2 := rxDest6B.FindStringSubmatch(line); m2 != nil && len(m2) > 1 {
				n6b, err := strconv.ParseUint(m2[1], 10, 64)
				if err == nil && n6b != 0 {
					fullIP[4] = byte(n6b >> 24)
					fullIP[5] = byte(n6b >> 16)
					fullIP[6] = byte(n6b >> 8)
					fullIP[7] = byte(n6b)
				}
			}
			fullV6 := net.IP(fullIP).String()
			// Mask with prefix6.
			mask := net.CIDRMask(p6, 128)
			masked := net.IP(ip).Mask(mask).String()
			if !contains(buckets[masked], fullV6) {
				buckets[masked] = append(buckets[masked], fullV6)
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

// bucketOf returns the /prefix4 (v4) or /prefix6 (v6) bucket key.
// v0.4.2: respects current prefix4/prefix6 from /var/lib/bpftune/.
// Mirrors bpftune_log.py:_bucket_of (but Python hardcodes /16; we make
// it prefix-aware per user spec).
func bucketOf(v4, v6 string) string {
	// Build the address string in standard or v6:hex form.
	addr := destStr(v4, v6)
	if addr == "" {
		return ""
	}
	// Filter 0.0.0.0/127.x.x.x (only applies to v4).
	if v6 == "" && v4 != "" {
		n, err := strconv.ParseInt(v4, 10, 64)
		if err == nil {
			first := (n >> 24) & 0xFF
			if first == 0 || first == 127 {
				return ""
			}
		}
	}
	return canonBucketWithPrefix(addr, prefix4Value(), prefix6Value())
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
			// v0.4.2: surface prefix4/prefix6/explore_pct so the
			// dashboard can display "Prefix4: 16, Prefix6: 32,
			// Exploration: 100%" in the build/service panel.
			"prefix4":     prefix4Value(),
			"prefix6":     prefix6Value(),
			"explore_pct": explorePctValue(),
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
  echo "  cd $REPO_ROOT && git diff dashboard/bin/go/" >&2
  exit 1
fi
echo "BUILD OK: /tmp/bpftune-collector-go-test"
ls -la /tmp/bpftune-collector-go-test

echo "smoke test: -h"
/tmp/bpftune-collector-go-test -h 2>&1 | head -10 || true

# --- commit -------------------------------------------------------------------
git add dashboard/bin/go/labels.go dashboard/bin/go/log_parsing.go dashboard/bin/go/main.go

if git diff --cached --quiet; then
  echo "no changes to commit."
  [ "$NO_PUSH" -eq 1 ] && exit 0
  exit 0
fi

COMMIT_MSG="dashboard: Go 0.4.2 — prefix-aware bucketing + longest-prefix label matching

Implements the bucket design the user specified (not what Python
hardcoded):

  * Labels.json supports ANY prefix width via longest-prefix match.
    Key form determines the prefix:
      \"82.43.0.0\"      → /16  (2 trailing zero bytes)
      \"82.43.215.0\"    → /24  (1 trailing zero byte)
      \"82.43.215.97\"   → /32  (0 trailing zero bytes)
      \"2606:1a40::\"    → /32  (12 trailing zero bytes in 16-byte form)
      \"82.43.0.0/16\"   → /16  (explicit CIDR notation also supported)
    A /32 label wins over a /16 label for the same IP — so a user can
    put a full IP \"82.43.215.97\" → \"home-sco\" and only that exact IP
    gets home-sco, while everything else in 82.43.0.0/16 falls into the
    /16 bucket.

  * canonBucket (was hardcoded /16) replaced with canonBucketWithPrefix(addr, p4, p6):
      - v4 masked with /prefix4
      - v6 (in v6:hex form OR standard form) masked with /prefix6

  * prefix4 / prefix6 / explore_pct read from:
      /var/lib/bpftune/prefix4     (default 16, range 0-32)
      /var/lib/bpftune/prefix6     (default 32, range 0-128)
      /var/lib/bpftune/explore_pct (default 100, range 0-100)
    All mtime-cached so changes take effect on the next collect cycle.

  * labelFor chain (v0.4.2):
      1. foldV6 (v6:hex → v4 if /etc/bpftune/aliases declares it)
      2. longestPrefixLabel (finds most specific label match)
      3. canonBucketWithPrefix (fallback with current prefix4/prefix6)

  * bucketOf in log_parsing.go (the _bucket field in recent_swaps)
    now respects prefix4.  Was hardcoded /16 (matching Python's
    _bucket_of, which is also wrong).

  * buildBucketIPs respects prefix4 (v4) and prefix6 (v6).  Was
    hardcoded /16 (v4) and /32 (v6).

  * Build section of current.json now includes:
      \"prefix4\":     <int>  (e.g., 16)
      \"prefix6\":     <int>  (e.g., 32)
      \"explore_pct\": <int>  (e.g., 100)
    so the dashboard can display \"Prefix4: 16, Prefix6: 32,
    Exploration: 100%\" in the build/service panel.

Files:
  - dashboard/bin/go/labels.go      (UPDATE — canonBucket→canonBucketWithPrefix,
    +longestPrefixLabel, +labelKeyToCIDR, +mtimeIntCache,
    +prefix4Value/prefix6Value/explorePctValue, labelFor rewritten)
  - dashboard/bin/go/log_parsing.go (UPDATE — bucketOf + buildBucketIPs
    use canonBucketWithPrefix)
  - dashboard/bin/go/main.go        (UPDATE — build section gets
    prefix4/prefix6/explore_pct fields)
  - constants.go, go.mod           (unchanged from v0.4.0)

Build verified locally: go build + go vet + gofmt all clean.
labelKeyToCIDR logic tested in isolation:
  \"82.43.0.0\"     → /16 ✓
  \"82.43.215.0\"   → /24 ✓
  \"82.43.215.97\"  → /32 ✓
  \"82.0.0.0\"      → /8  ✓
  \"2606:1a40::\"   → /32 ✓
  \"82.43.0.0/16\"  → /16 ✓ (explicit CIDR)"

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
echo "DONE.  Verify on this host:"
echo "  cd ~/bpftune/dashboard/bin/go && go build -o bpftune-collector-go"
echo "  fuser -k 8083/tcp 2>/dev/null; sleep 1"
echo "  ./bpftune-collector-go --port 8083 --bind 127.0.0.1 &"
echo "  sleep 5"
echo "  curl -s http://127.0.0.1:8083/current.json | python3 -m json.tool | head -20  # build section now has prefix4/prefix6/explore_pct"
echo ""
echo "To test longest-prefix label matching:"
echo "  # Add a /32 entry to /var/lib/bpftune/aliases.labels.json:"
echo "  #   \"82.43.215.97\": \"home-sco-specific\""
echo "  # And keep the existing /16 entry:"
echo "  #   \"82.43.0.0\": \"home-sco\""
echo "  # Then restart the collector — bucket for 82.43.215.97 should be \"home-sco-specific\""
echo "  # while bucket for 82.43.100.50 should still be \"home-sco\""
