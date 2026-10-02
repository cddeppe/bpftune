#!/bin/bash
# ============================================================================
# v0.7.5f: PERMANENT FIX for swaps chart date adapter error + coverage diag
# ============================================================================
#
# PROBLEM 1: "Error: This method is not implemented: Check that a complete
#   date adapter is provided" on swaps chart.
#
# ROOT CAUSE: swaps chart uses `type: "time"` x-axis which REQUIRES
#   chartjs-adapter-date-fns. SSE pushes happen every 30s starting at boot
#   (in startLiveUpdates) — but the adapter might not have finished loading
#   yet (loaded async by _loadCharts). The race means renderSwaps tries to
#   use a time scale before the adapter is registered → "method not
#   implemented" error. The chart "flip-flops" between working (when adapter
#   is loaded) and broken (when it isn't, or first push before boot).
#
# FIX: Remove the `type: "time"` dependency from the swaps chart entirely.
#   Use `type: "category"` with formatted "HH:MM" string labels. No adapter
#   needed. Chart renders reliably on every SSE push, no race condition.
#   (The rate/sscore/streaks line charts still use time axis — they only
#   render after state.bucketDoc loads, by which time the adapter IS
#   loaded, so they work.)
#
# PROBLEM 2: Coverage column shows "–" for all buckets.
#
# ROOT CAUSE: Likely the CSV file is empty/missing OR has rows under
#   OLD labels (e.g., raw "2606:1a40::") while fleet looks up NEW labels
#   (e.g., "home-sco"). The CSV-reader-trusts-stored-label design means
#   newly-labeled buckets don't get coverage from pre-label history.
#
# This patch adds stderr logging to renderFleetToDisk so we can see
#   EXACTLY why fleet is empty (CSV missing? CSV has data but bucket
#   IDs don't match? readCSVTail returns nil?). Run with:
#     journalctl -u bpftune-collector-go -n 100 --no-pager | grep fleet
#
# ============================================================================

set -e
set -u

REPO=/root/bpftune
GO_SRC=$REPO/dashboard/bin/go
JS_REPO=$REPO/dashboard/bin/dashboard.js
JS_OPT=/opt/bpftune-dashboard/bin/dashboard.js
BIN_OPT=/opt/bpftune-dashboard/bin/bpftune-collector-go

echo "=== v0.7.5f: swaps category-axis + fleet diagnostics ==="

# ----------------------------------------------------------------------------
# 1. Patch dashboard.js: swaps chart → category axis (no date adapter)
# ----------------------------------------------------------------------------
echo "[1/4] Patching dashboard.js: swaps chart to category axis"

python3 - <<'PYEOF'
import sys

path = '/root/bpftune/dashboard/bin/dashboard.js'
s = open(path).read()

# Idempotency: if already patched, skip
swaps_idx = s.find('mk("swaps", {')
if swaps_idx >= 0:
    swaps_block = s[swaps_idx:swaps_idx+1500]
    if 'type: "category"' in swaps_block and 'labels: ts.map' in swaps_block:
        print('  swaps chart already uses category axis, skipping')
        sys.exit(0)

# Find the swaps chart mk() block by anchor
start_marker = '    mk("swaps", {'
start_idx = s.find(start_marker)
if start_idx == -1:
    print('  ERROR: could not find mk("swaps", {...}) block')
    sys.exit(1)

# Find the matching close brace for the opening { in start_marker
open_brace_idx = start_idx + len(start_marker) - 1  # position of {
brace_depth = 1
i = open_brace_idx + 1
end_idx = None
while i < len(s) and brace_depth > 0:
    c = s[i]
    if c == '{':
        brace_depth += 1
    elif c == '}':
        brace_depth -= 1
        if brace_depth == 0:
            # Found matching close brace at i
            # Look for ); after it
            close_idx = s.find(');', i)
            if close_idx == -1:
                print('  ERROR: could not find ); after swaps mk() block')
                sys.exit(1)
            end_idx = close_idx + 2
            break
    i += 1

if end_idx is None:
    print('  ERROR: could not find end of mk("swaps") block')
    sys.exit(1)

old_block = s[start_idx:end_idx]

new_block = '''    mk("swaps", {
      type: "bar",
      data: {
        labels: ts.map(function (t) {
          var dt = new Date(t * 1000);
          var hh = String(dt.getHours()).padStart(2, "0");
          var mm = String(dt.getMinutes()).padStart(2, "0");
          return hh + ":" + mm;
        }),
        datasets: [{
          label: "swaps",
          data: d.swaps,
          backgroundColor: "#4e79a7",
          borderColor: "#4e79a7",
          borderRadius: 2,
          maxBarThickness: 14,
        }],
      },
      options: timeOpts({
        scales: {
          x: {type: "category", grid: {display: false},
              ticks: {maxRotation: 0, autoSkip: true, autoSkipPadding: 24,
                      padding: 4, maxTicksLimit: 8}},
          y: {beginAtZero: true, grid: {drawTicks: false},
              ticks: {maxTicksLimit: 4, padding: 6}},
        },
      }),
    });'''

s = s[:start_idx] + new_block + s[end_idx:]
open(path, 'w').write(s)
print('  swaps chart patched to use category axis (no date adapter needed)')
PYEOF

# ----------------------------------------------------------------------------
# 2. Patch render.go: add diagnostic logging to renderFleetToDisk
# ----------------------------------------------------------------------------
echo "[2/4] Patching render.go: fleet diagnostics"

python3 - <<'PYEOF'
import sys

path = '/root/bpftune/dashboard/bin/go/render.go'
s = open(path).read()

if 'fleet-diag: csvAll is nil' in s:
    print('  render.go already has fleet diagnostics, skipping')
    sys.exit(0)

# Anchor: insert diagnostic logging right after the for-loop body of renderFleetToDisk.
# We add: (a) a log line if h.csvAll is nil, (b) a log line per bucket showing snap count,
# (c) a final summary log.
old = '''func (h *historyStore) renderFleetToDisk(buckets []map[string]interface{}) {
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
                // v0.7.3: use pre-loaded CSV map (from renderToDisk) instead of
                // reading the full CSV per-bucket.  Falls back to per-bucket read.
                var snaps []bucketSnapshot
                if h.csvAll != nil {
                        snaps = readBucketCSVFromMap(h.csvAll, id, 86400)
                } else {
                        snaps = readBucketCSV(id, 86400)
                }
                if len(snaps) == 0 {
                        continue
                }'''

new = '''func (h *historyStore) renderFleetToDisk(buckets []map[string]interface{}) {
        type pair struct {
                bid string
                cov float64
        }
        var pairs []pair
        // v0.7.5f: fleet diagnostics — log CSV state once per render.
        csvAllNil := h.csvAll == nil
        csvAllLen := 0
        if !csvAllNil {
                csvAllLen = len(h.csvAll)
        }
        os.Stderr.WriteString("fleet-diag: csvAll=" + boolStr(csvAllNil) +
                " csvAllBuckets=" + itoa(csvAllLen) +
                " liveBuckets=" + itoa(len(buckets)) + "\\n")
        for _, b := range buckets {
                id, _ := b["dest"].(string)
                if id == "" {
                        continue
                }
                // v0.7.3: use pre-loaded CSV map (from renderToDisk) instead of
                // reading the full CSV per-bucket.  Falls back to per-bucket read.
                var snaps []bucketSnapshot
                if h.csvAll != nil {
                        snaps = readBucketCSVFromMap(h.csvAll, id, 86400)
                } else {
                        snaps = readBucketCSV(id, 86400)
                }
                if len(snaps) == 0 {
                        os.Stderr.WriteString("fleet-diag: bucket=" + id + " snaps=0 (skipped)\\n")
                        continue
                }'''

if old not in s:
    print('  ERROR: could not find renderFleetToDisk anchor block')
    sys.exit(1)

s = s.replace(old, new, 1)

# Add helper functions (boolStr, itoa) if not present
if 'func boolStr(' not in s:
    # Insert at end of file
    s += '''
// v0.7.5f: helpers for fleet diagnostics logging.
func boolStr(b bool) string {
        if b {
                return "nil"
        }
        return "set"
}

func itoa(n int) string {
        return strconv.Itoa(n)
}
'''

open(path, 'w').write(s)
print('  render.go patched with fleet diagnostics')
PYEOF

# Verify strconv is imported
python3 - <<'PYEOF'
path = '/root/bpftune/dashboard/bin/go/render.go'
s = open(path).read()
if '"strconv"' not in s:
    # Add to imports
    import re
    s = re.sub(r'import \(\s*\n', 'import (\n\t"strconv"\n', s, count=1)
    open(path, 'w').write(s)
    print('  added "strconv" import to render.go')
PYEOF

# ----------------------------------------------------------------------------
# 3. Build the Go binary
# ----------------------------------------------------------------------------
echo "[3/4] Building Go binary"
cd $GO_SRC
go build -o $BIN_OPT . 2>&1 | tail -20
echo "  built $BIN_OPT"

# ----------------------------------------------------------------------------
# 4. Copy dashboard.js to live served location + restart
# ----------------------------------------------------------------------------
echo "[4/4] Copying dashboard.js + restarting"
cp $JS_REPO $JS_OPT
systemctl restart bpftune-collector-go 2>&1 || \
  systemctl restart bpftune-dashboard-go 2>&1 || true
sleep 3

echo
echo "=== v0.7.5f applied ==="
echo
echo "To diagnose coverage:"
echo "  journalctl -u bpftune-collector-go -n 100 --no-pager | grep fleet"
echo
echo "Expected output like:"
echo "  fleet-diag: csvAll=set csvAllBuckets=N liveBuckets=M"
echo "  fleet-diag: bucket=home-sco snaps=120"
echo "  fleet-diag: bucket=140.82.0.0 snaps=0 (skipped)"
echo
echo "If csvAll=nil → CSV file is missing/empty (check /var/lib/bpftune/history/buckets.v2.csv)"
echo "If csvAll=set but snaps=0 for all → CSV has rows but bucket IDs don't match"
echo "  (e.g., CSV has '2606:1a40::' but live bucket is 'home-sco')"
echo
echo "Also check CSV directly:"
echo "  ls -la /var/lib/bpftune/history/buckets.v2.csv"
echo "  head -1 /var/lib/bpftune/history/buckets.v2.csv   # header"
echo "  tail -5 /var/lib/bpftune/history/buckets.v2.csv   # recent rows — what addr values?"
