#!/bin/bash
# ============================================================================
# v0.7.5f2: PERMANENT FIX for swaps chart + fleet diagnostics (robust)
# ============================================================================
#
# Same as v0.7.5f but with a more robust render.go patch that uses
# small line-level anchors instead of one big block.  Should work
# regardless of minor whitespace/comment variations in the user's
# renderFleetToDisk.
#
# PROBLEM 1: "Error: This method is not implemented: Check that a complete
#   date adapter is provided" on swaps chart.
# FIX: Switch swaps chart x-axis from `type: "time"` to `type: "category"`
#   with "HH:MM" string labels — no date adapter needed.
#
# PROBLEM 2: Coverage column shows "–" for all buckets.
# FIX: Add stderr logging to renderFleetToDisk so we can see WHY fleet.json
#   is empty.
# ============================================================================

set -e
set -u

REPO=/root/bpftune
GO_SRC=$REPO/dashboard/bin/go
JS_REPO=$REPO/dashboard/bin/dashboard.js
JS_OPT=/opt/bpftune-dashboard/bin/dashboard.js
BIN_OPT=/opt/bpftune-dashboard/bin/bpftune-collector-go

echo "=== v0.7.5f2: swaps category-axis + fleet diagnostics (robust) ==="

# ----------------------------------------------------------------------------
# 1. dashboard.js: swaps chart → category axis (no date adapter)
# ----------------------------------------------------------------------------
echo "[1/4] Patching dashboard.js: swaps chart to category axis"

python3 - <<'PYEOF'
import sys

path = '/root/bpftune/dashboard/bin/dashboard.js'
s = open(path).read()

# Idempotency
swaps_idx = s.find('mk("swaps", {')
if swaps_idx >= 0:
    swaps_block = s[swaps_idx:swaps_idx+1500]
    if 'type: "category"' in swaps_block and 'labels: ts.map' in swaps_block:
        print('  swaps chart already uses category axis, skipping')
        sys.exit(0)

start_marker = '    mk("swaps", {'
start_idx = s.find(start_marker)
if start_idx == -1:
    print('  ERROR: could not find mk("swaps", {...}) anchor')
    sys.exit(1)

# Walk forward from start_idx to find matching ); for the opening { 
open_brace_idx = start_idx + len(start_marker) - 1  # index of {
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
print('  swaps chart patched to use category axis')
PYEOF

# ----------------------------------------------------------------------------
# 2. render.go: fleet diagnostics (using small line-level anchors)
# ----------------------------------------------------------------------------
echo "[2/4] Patching render.go: fleet diagnostics (robust anchors)"

python3 - <<'PYEOF'
import sys, re

path = '/root/bpftune/dashboard/bin/go/render.go'
s = open(path).read()

if 'fleet-diag: csvAll' in s:
    print('  render.go already has fleet diagnostics, skipping')
    sys.exit(0)

# Step 1: Find "var pairs []pair" line right after renderFleetToDisk signature
sig = 'func (h *historyStore) renderFleetToDisk(buckets []map[string]interface{})'
sig_idx = s.find(sig)
if sig_idx == -1:
    print('  ERROR: renderFleetToDisk signature not found')
    sys.exit(1)

pair_marker = 'var pairs []pair'
pair_idx = s.find(pair_marker, sig_idx)
if pair_idx == -1:
    print('  ERROR: "var pairs []pair" not found in renderFleetToDisk')
    sys.exit(1)

# Find the end of the "var pairs []pair" line (newline + 1)
eol1 = s.find('\n', pair_idx) + 1

# Detect indentation style of the function (peek at next non-blank line)
# so the inserted logging matches.
probe = s[eol1:eol1+200]
# Get leading whitespace of the first non-blank line after var pairs []pair
indent = ''
for line in probe.split('\n'):
    if line.strip() == '':
        continue
    ws = line[:len(line) - len(line.lstrip())]
    if ws:
        indent = ws
        break
if not indent:
    indent = '        '  # fallback: 8 spaces

# Build the diagnostic block with matching indentation
diag_block = (
    '\n' +
    indent + '// v0.7.5f: fleet diagnostics — log CSV state once per render.\n' +
    indent + 'csvAllNil := h.csvAll == nil\n' +
    indent + 'csvAllLen := 0\n' +
    indent + 'if !csvAllNil {\n' +
    indent + indent + 'csvAllLen = len(h.csvAll)\n' +
    indent + '}\n' +
    indent + 'os.Stderr.WriteString("fleet-diag: csvAll=" + boolStr(csvAllNil) +\n' +
    indent + indent + '" csvAllBuckets=" + itoa(csvAllLen) +\n' +
    indent + indent + '" liveBuckets=" + itoa(len(buckets)) + "\\n")\n'
)

# Insert AFTER "var pairs []pair\n"
s = s[:eol1] + diag_block + s[eol1:]

# Step 2: Find "if len(snaps) == 0 {" in renderFleetToDisk (after our insertion)
# and insert a log line right before "continue"
skip_anchor = 'if len(snaps) == 0 {'
# Search within the function (next 3000 chars after sig_idx)
search_region = s[sig_idx:sig_idx+5000]
skip_idx_local = search_region.find(skip_anchor)
if skip_idx_local == -1:
    print('  ERROR: "if len(snaps) == 0 {" not found in renderFleetToDisk')
    sys.exit(1)
skip_idx = sig_idx + skip_idx_local

# Find the "continue" right after skip_idx
cont_idx = s.find('continue', skip_idx)
if cont_idx == -1 or cont_idx > skip_idx + 200:
    print('  ERROR: "continue" not found after len(snaps) == 0')
    sys.exit(1)

# Find start of the line containing "continue"
line_start = s.rfind('\n', 0, cont_idx) + 1
line_indent = s[line_start:cont_idx]
# Use the same indent as the "continue" line for our diag log
diag_line = line_indent + 'os.Stderr.WriteString("fleet-diag: bucket=" + id + " snaps=0 (skipped)\\n")\n'

# Insert diag_line before the "continue" line (i.e., at line_start)
s = s[:line_start] + diag_line + s[line_start:]

# Step 3: Add helper functions (boolStr, itoa) at end of file if not present
if 'func boolStr(' not in s:
    s += '\n// v0.7.5f: helpers for fleet diagnostics logging.\n'
    s += 'func boolStr(b bool) string {\n'
    s += '\tif b {\n'
    s += '\t\treturn "nil"\n'
    s += '\t}\n'
    s += '\treturn "set"\n'
    s += '}\n\n'
    s += 'func itoa(n int) string {\n'
    s += '\treturn strconv.Itoa(n)\n'
    s += '}\n'

# Step 4: Ensure "strconv" is imported
if '"strconv"' not in s:
    s = re.sub(r'import \(\s*\n', 'import (\n\t"strconv"\n', s, count=1)

open(path, 'w').write(s)
print('  render.go patched with fleet diagnostics')
PYEOF

# ----------------------------------------------------------------------------
# 3. Build
# ----------------------------------------------------------------------------
echo "[3/4] Building Go binary"
cd $GO_SRC
go build -o $BIN_OPT . 2>&1 | tail -20
echo "  built $BIN_OPT"

# ----------------------------------------------------------------------------
# 4. Copy + restart
# ----------------------------------------------------------------------------
echo "[4/4] Copying dashboard.js + restarting"
cp $JS_REPO $JS_OPT
systemctl restart bpftune-collector-go 2>&1 || \
  systemctl restart bpftune-dashboard-go 2>&1 || true
sleep 3

echo
echo "=== v0.7.5f2 applied ==="
echo
echo "Verify swaps chart no longer throws date adapter error:"
echo "  Open browser dev console — should be no more 'date adapter' error"
echo
echo "Check fleet diagnostics:"
echo "  journalctl -u bpftune-collector-go -n 100 --no-pager | grep fleet"
echo
echo "Also useful:"
echo "  ls -la /var/lib/bpftune/history/buckets.v2.csv"
echo "  head -1 /var/lib/bpftune/history/buckets.v2.csv"
echo "  tail -5 /var/lib/bpftune/history/buckets.v2.csv"
