package main

// System info helpers — shell out to dpkg-query / git / systemctl to
// get the bpftune package version, dashboard git commit, and service
// status.  All cached for 5 minutes (service status cached 30s since
// it changes more frequently).
//
// Mirrors Python's bpftune_data.py:data_build (lines 40-70).

import (
	"os/exec"
	"regexp"
	"strings"
	"sync"
	"time"
)

// ============================================================================
// Shell-cached helpers
// ============================================================================

type shellCacheEntry struct {
	val string
	at  time.Time
}

var (
	shellCacheMu sync.Mutex
	shellCache   = map[string]shellCacheEntry{}
)

// shellCached runs cmd and caches the trimmed output for `ttl`.
// On error, returns the previous cached value (if any) or `fallback`.
func shellCached(name string, cmd *exec.Cmd, ttl time.Duration, fallback string) string {
	shellCacheMu.Lock()
	defer shellCacheMu.Unlock()
	if entry, ok := shellCache[name]; ok && entry.val != "" && time.Since(entry.at) < ttl {
		return entry.val
	}
	output, err := cmd.Output()
	if err != nil {
		// Keep old value if we have one
		if entry, ok := shellCache[name]; ok && entry.val != "" {
			return entry.val
		}
		// Cache the fallback so we don't keep retrying
		shellCache[name] = shellCacheEntry{fallback, time.Now()}
		return fallback
	}
	v := strings.TrimSpace(string(output))
	if v == "" {
		v = fallback
	}
	shellCache[name] = shellCacheEntry{v, time.Now()}
	return v
}

// ============================================================================
// bpftuneVersion — runs `dpkg-query -W -f=${Version} bpftune`
// ============================================================================

func bpftuneVersion() string {
	cmd := exec.Command("dpkg-query", "-W", "-f=${Version}", "bpftune")
	return shellCached("bpftune_version", cmd, 5*time.Minute, "?")
}

// ============================================================================
// dashVersion — runs `git -C <repo> rev-parse --short HEAD`
// Tries common repo locations: /root/bpftune, /opt/bpftune, /usr/src/bpftune
// ============================================================================

func dashVersion() string {
	// Try common repo locations (matches Python data_build).
	for _, repo := range []string{"/root/bpftune", "/opt/bpftune", "/usr/src/bpftune"} {
		cmd := exec.Command("git", "-C", repo, "rev-parse", "--short", "HEAD")
		output, err := cmd.Output()
		if err == nil {
			v := strings.TrimSpace(string(output))
			if v != "" {
				// Cache and return
				shellCacheMu.Lock()
				shellCache["dash_version"] = shellCacheEntry{v, time.Now()}
				shellCacheMu.Unlock()
				return v
			}
		}
	}
	return "?"
}

// ============================================================================
// bpftuneServiceActive — runs `systemctl is-active bpftune`
// ============================================================================

func bpftuneServiceActive() string {
	cmd := exec.Command("systemctl", "is-active", "bpftune")
	return shellCached("bpftune_service", cmd, 30*time.Second, "unknown")
}

// ============================================================================
// bpftuneServiceStartedAt — runs `systemctl show bpftune -p ActiveEnterTimestamp --value`
// Returns the time the bpftune service started, or the collector's start time
// as fallback (so uptime still works in containers without systemd).
// ============================================================================

var systemdTsRx = regexp.MustCompile(`(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})`)

func bpftuneServiceStartedAt(fallback time.Time) time.Time {
	shellCacheMu.Lock()
	if entry, ok := shellCache["bpftune_started"]; ok && time.Since(entry.at) < 30*time.Second {
		shellCacheMu.Unlock()
		// Parse back to time
		if t, err := time.ParseInLocation("2006-01-02 15:04:05", entry.val, time.Local); err == nil {
			return t
		}
	} else {
		shellCacheMu.Unlock()
	}
	cmd := exec.Command("systemctl", "show", "bpftune", "-p", "ActiveEnterTimestamp", "--value")
	output, err := cmd.Output()
	if err != nil {
		return fallback
	}
	ts := strings.TrimSpace(string(output))
	if ts == "" {
		return fallback
	}
	m := systemdTsRx.FindStringSubmatch(ts)
	if m == nil {
		return fallback
	}
	t, err := time.ParseInLocation("2006-01-02 15:04:05", m[1], time.Local)
	if err != nil {
		return fallback
	}
	shellCacheMu.Lock()
	shellCache["bpftune_started"] = shellCacheEntry{m[1], time.Now()}
	shellCacheMu.Unlock()
	return t
}

// ============================================================================
// startedUTC — returns "HH:MM:SS" form (matches Python data_build)
// ============================================================================

func startedUTC() string {
	t := bpftuneServiceStartedAt(time.Now())
	return t.UTC().Format("15:04:05")
}

// ============================================================================
// uptimeMin — minutes since bpftune service started
// ============================================================================

func uptimeMin() int {
	t := bpftuneServiceStartedAt(time.Now())
	return int(time.Since(t).Minutes())
}
