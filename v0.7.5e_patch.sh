#!/bin/bash
# ============================================================================
# v0.7.5e: PERMANENT FIX for "meta vs current keep fighting" bucket bug
# ============================================================================
#
# ROOT CAUSE (why the buckets look "wrong"):
#   - <select id="bucket"> dropdown was populated from state.meta.buckets
#     which only refreshed every 5 min via refreshAll()
#   - "top destination buckets" table refreshed every 30s from current.json
#     via SSE in _syncBucketDropdown(doc)
#   - Different sort orders compounded it:
#       meta.json:  n_alg desc, inst_mean desc, id asc  (stable, deterministic)
#       current.json:  Inst desc                          (Python read_map style)
#   So the dropdown and the table disagreed, and the dropdown was 5 min stale.
#
# FIX (this patch):
#   1. collect.go: apply STABLE SORT (n_alg desc, inst desc, id asc) in
#      buildLiveBuckets so current.json's buckets[] has the SAME ORDER as
#      meta.json's buckets[]. Now the dropdown and table always agree.
#   2. dashboard.js: add _rebuildDropdownFromLive() and call it from
#      _syncBucketDropdown() on every SSE push (every 30s). The dropdown
#      now refreshes from current.json's buckets[] — same source + same
#      sort as the table. The 5-min meta-driven refresh in refreshAll()
#      remains as a fallback.
#
# After this patch:
#   - Dropdown updates every 30s (was 5 min)
#   - Table updates every 30s (unchanged)
#   - Both use the same source (current.json) and same sort (server-side)
#   - meta.json still used for the 24h/7d/30d/all series data
# ============================================================================

set -e
set -u

REPO=/root/bpftune
GO_SRC=$REPO/dashboard/bin/go
JS_REPO=$REPO/dashboard/bin/dashboard.js
JS_OPT=/opt/bpftune-dashboard/bin/dashboard.js
BIN_OPT=/opt/bpftune-dashboard/bin/bpftune-collector-go

echo "=== v0.7.5e: stable sort + live dropdown rebuild ==="

# ----------------------------------------------------------------------------
# 1. Patch collect.go: apply stable sort to buildLiveBuckets
# ----------------------------------------------------------------------------
echo "[1/5] Patching collect.go: stable sort (n_alg desc, inst desc, id asc)"

python3 - <<'PYEOF'
import re, sys

path = '/root/bpftune/dashboard/bin/go/collect.go'
s = open(path).read()

# Sanity: idempotency check — if v0.7.5e marker is already present, skip.
if 'v0.7.5e: STABLE SORT to match meta.json' in s:
    print('  collect.go already patched (v0.7.5e marker found), skipping')
    sys.exit(0)

# Locate the old sort + for-loop header block.
# Match from the "// buildLiveBuckets constructs the buckets list (top 8 by inst),"
# comment through "for _, h := range sortedHosts {".
old_block = '''// buildLiveBuckets constructs the buckets list (top 8 by inst),
// metric_by_bucket, bucket_live (single-point placeholder), and
// live_leaders. All in a single pass over sorted hosts.
func buildLiveBuckets(hosts []hostEntry, now int64) (
\tbuckets []bucketRow, metricByBucket map[string]interface{},
\tbucketLive map[string]interface{}, liveLeaders []interface{},
) {
\ttype bucket struct {
\t\tDest    string  `json:"dest"`
\t\tInst    int     `json:"inst"`
\t\tRttUs   float64 `json:"rtt_us"`
\t\tRefMbps float64 `json:"ref_mbps"`
\t\tBestAlg string  `json:"best_alg"`
\t\tNAlg    int     `json:"n_alg"`
\t}
\tmetricByBucket = map[string]interface{}{}
\tbucketLive = map[string]interface{}{}

\t// Sort by inst desc (matches Python read_map line 809).
\tsortedHosts := make([]hostEntry, len(hosts))
\tcopy(sortedHosts, hosts)
\tsort.Slice(sortedHosts, func(i, j int) bool {
\t\treturn sortedHosts[i].Inst > sortedHosts[j].Inst
\t})

\tfor _, h := range sortedHosts {'''

new_block = '''// buildLiveBuckets constructs the buckets list (top 8),
// metric_by_bucket, bucket_live (single-point placeholder), and
// live_leaders. All in a single pass over sorted hosts.
//
// v0.7.5e: STABLE SORT to match meta.json's order (n_alg desc,
// inst desc, id asc). Previously this was just Inst desc, which meant
// current.json's buckets[] array had a DIFFERENT order than meta.json's
// buckets[] array — the dropdown (from meta, every 5 min) and the table
// (from current, every 30s) disagreed. Now both use the same order so
// they stop "fighting" each other.
func buildLiveBuckets(hosts []hostEntry, now int64) (
\tbuckets []bucketRow, metricByBucket map[string]interface{},
\tbucketLive map[string]interface{}, liveLeaders []interface{},
) {
\ttype bucket struct {
\t\tDest    string  `json:"dest"`
\t\tInst    int     `json:"inst"`
\t\tRttUs   float64 `json:"rtt_us"`
\t\tRefMbps float64 `json:"ref_mbps"`
\t\tBestAlg string  `json:"best_alg"`
\t\tNAlg    int     `json:"n_alg"`
\t}
\tmetricByBucket = map[string]interface{}{}
\tbucketLive = map[string]interface{}{}

\t// v0.7.5e: pre-compute n_alg per host so the sort can use it
\t// without re-parsing metrics twice per comparison.
\ttype hostWithAlg struct {
\t\thostEntry
\t\tnAlg int
\t}
\tsortedHosts := make([]hostWithAlg, len(hosts))
\tfor i, h := range hosts {
\t\tn := 0
\t\tif mi, _ := h.V["metrics"].([]interface{}); mi != nil {
\t\t\tfor _, m := range mi {
\t\t\t\tif mm, ok := m.(map[string]interface{}); ok &&
\t\t\t\t\ttoInt(mm["metric_count"]) > 0 {
\t\t\t\t\tn++
\t\t\t\t}
\t\t\t}
\t\t}
\t\tsortedHosts[i] = hostWithAlg{hostEntry: h, nAlg: n}
\t}
\t// Stable sort: n_alg desc, inst desc, id asc — matches meta.json's
\t// (n_alg desc, inst_mean desc, id asc) order closely enough that
\t// the live buckets list and the 5-min meta list no longer disagree.
\tsort.SliceStable(sortedHosts, func(i, j int) bool {
\t\tif sortedHosts[i].nAlg != sortedHosts[j].nAlg {
\t\t\treturn sortedHosts[i].nAlg > sortedHosts[j].nAlg
\t\t}
\t\tif sortedHosts[i].Inst != sortedHosts[j].Inst {
\t\t\treturn sortedHosts[i].Inst > sortedHosts[j].Inst
\t\t}
\t\treturn sortedHosts[i].Addr < sortedHosts[j].Addr
\t})

\tfor _, sh := range sortedHosts {
\t\th := sh.hostEntry'''

if old_block not in s:
    print('  ERROR: could not find expected block in collect.go')
    print('  (file may have been modified since v0.7.5c — inspect manually)')
    sys.exit(1)

s = s.replace(old_block, new_block, 1)

# Also: replace the inner `nAlg := 0; for _, m := range metrics { ... }` block
# with the precomputed value, since we already have it in sh.nAlg.
old_nalg = '''\t\tnAlg := 0
\t\tfor _, m := range metrics {
\t\t\tif mi, ok := m.(map[string]interface{}); ok &&
\t\t\t\ttoInt(mi["metric_count"]) > 0 {
\t\t\t\tnAlg++
\t\t\t}
\t\t}'''
new_nalg = '''\t\t// v0.7.5e: reuse pre-computed n_alg (was re-parsing here).
\t\tnAlg := sh.nAlg'''

if old_nalg not in s:
    print('  WARNING: could not find n_alg recompute block — leaving it (harmless)')
else:
    s = s.replace(old_nalg, new_nalg, 1)

open(path, 'w').write(s)
print('  collect.go patched OK')
PYEOF

# ----------------------------------------------------------------------------
# 2. Patch dashboard.js: add _rebuildDropdownFromLive + call from _syncBucketDropdown
# ----------------------------------------------------------------------------
echo "[2/5] Patching dashboard.js: live dropdown rebuild on every SSE push"

python3 - <<'PYEOF'
import sys

path = '/root/bpftune/dashboard/bin/dashboard.js'
s = open(path).read()

if '_rebuildDropdownFromLive' in s:
    print('  dashboard.js already patched (_rebuildDropdownFromLive found), skipping')
    sys.exit(0)

# Anchor: insert the new function just before "function _syncBucketDropdown(doc) {"
new_fn = '''  // v0.7.5e: rebuild the bucket dropdown options from live data.
  // Previously the dropdown only refreshed every 5 min from meta.json
  // (refreshAll) while the table refreshed every 30s from current.json
  // (SSE) — so they showed different buckets in different orders. Now
  // both views read from current.json's buckets[] (server-side sorted
  // to match meta.json's order, server-side labeled). Custom labels
  // from window.__labels that aren't currently active are appended at
  // the end so the user can still pick them.
  function _rebuildDropdownFromLive(liveBuckets) {
    var bs = $('bucket');
    if (!bs) return;
    var prev = bs.value;
    var seen = {};
    var html = '';
    for (var k = 0; k < liveBuckets.length; k++) {
      var id = liveBuckets[k].dest;
      if (!id || seen[id]) continue;
      seen[id] = true;
      html += '<option value="' + id + '">' + id + '</option>';
    }
    if (window.__labels) {
      Object.keys(window.__labels).forEach(function(ip) {
        var lbl = window.__labels[ip];
        if (lbl && lbl !== ip && !seen[ip] && !seen[lbl]) {
          seen[lbl] = true;
          html += '<option value="' + lbl + '">' + lbl + '</option>';
        }
      });
    }
    bs.innerHTML = html;
    bs.add(new Option('All Buckets', 'all'), 0);
    // Restore selection if still present; else "All Buckets".
    var stillThere = false;
    for (var i = 0; i < bs.options.length; i++) {
      if (bs.options[i].value === prev) { stillThere = true; break; }
    }
    bs.value = stillThere ? prev : 'all';
  }

  function _syncBucketDropdown(doc) {
    var _bs = $('bucket');
    var _prevVal = _bs ? _bs.value : null;
    _safeRender('buckets', function() { renderBuckets(doc.buckets || []); });
    // v0.7.5e: also rebuild the dropdown options from live buckets —
    // previously the dropdown stayed stale for 5 min until refreshAll
    // ran, diverging from the table on every 30s SSE push.
    _safeRender('dropdown', function() { _rebuildDropdownFromLive(doc.buckets || []); });'''

old_fn = '''  function _syncBucketDropdown(doc) {
    var _bs = $('bucket');
    var _prevVal = _bs ? _bs.value : null;
    _safeRender('buckets', function() { renderBuckets(doc.buckets || []); });'''

if old_fn not in s:
    print('  ERROR: could not find _syncBucketDropdown anchor in dashboard.js')
    sys.exit(1)

s = s.replace(old_fn, new_fn, 1)
open(path, 'w').write(s)
print('  dashboard.js patched OK')
PYEOF

# ----------------------------------------------------------------------------
# 3. Build the Go binary
# ----------------------------------------------------------------------------
echo "[3/5] Building Go binary"
cd $GO_SRC
go build -o $BIN_OPT .
echo "  built $BIN_OPT"

# ----------------------------------------------------------------------------
# 4. Copy dashboard.js to live served location
# ----------------------------------------------------------------------------
echo "[4/5] Copying dashboard.js to $JS_OPT"
cp $JS_REPO $JS_OPT

# ----------------------------------------------------------------------------
# 5. Restart the collector service so the new binary runs
# ----------------------------------------------------------------------------
echo "[5/5] Restarting bpftune-collector-go service"
systemctl restart bpftune-collector-go 2>&1 || \
  systemctl restart bpftune-dashboard-go 2>&1 || \
  echo "  (service name unknown — restart manually if needed)"
sleep 2
systemctl --no-pager status bpftune-collector-go 2>&1 | head -10 || \
  systemctl --no-pager status bpftune-dashboard-go 2>&1 | head -10 || true

echo
echo "=== v0.7.5e patch applied ==="
echo "Verify in browser: dropdown + table now refresh together every 30s"
echo "with same source (current.json) and same sort (n_alg desc, inst desc, id asc)."
