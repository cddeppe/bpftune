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
