#!/usr/bin/env bash
# apply-0.4.87-dashboard-fixes.sh
#
# Self-contained atomic patch applier for bpftune dashboard branch.
# Embeds a single git format-patch commit (0.4.87: bucket-tag chips +
# All-Buckets chart refresh + metric lookup fix) and applies it via
# `git am` so the result is a real commit on the dashboard branch,
# ready to push to GitHub.
#
# This patch builds on the recent-swaps fix the user already shipped
# (commits 88dcf76..9118268 on origin/dashboard).  It does NOT touch
# _bucket_of or renderRecentSwapsForBucket — those were already fixed
# upstream.  If your HEAD is older than 9118268, fetch first.
#
# WHAT THIS SCRIPT DOES (in order):
#   1. Pre-flight checks: in bpftune repo? on dashboard branch (or
#      will switch)? working tree clean? bpftune-cli.py exists?
#   2. Fetches latest origin/dashboard so the patch applies on top
#      of the user's current HEAD (must be >= 9118268).
#   3. Writes the embedded patch to a temp file.
#   4. `git apply --check` first (dry-run) — aborts if it can't apply
#      cleanly so the working tree is left untouched.
#   5. `git am` for real — creates the commit on the current branch.
#   6. Runs the test suite to verify the patch is good.
#      58/58 should pass.
#   7. Pushes to origin/dashboard (with --yes, otherwise asks).
#
# USAGE:
#   ./apply-0.4.87-dashboard-fixes.sh              # interactive
#   ./apply-0.4.87-dashboard-fixes.sh --yes        # non-interactive
#   ./apply-0.4.87-dashboard-fixes.sh --no-push    # apply + test, skip push
#   ./apply-0.4.87-dashboard-fixes.sh --check-only  # dry-run, no changes
#
# REQUIREMENTS:
#   - bash 4+
#   - git
#   - python3 (for tests)
#   - The script must be run from inside a clone of cddeppe/bpftune
#     (or with the repo path as the first positional arg).
set -euo pipefail

# ---------- defaults ----------
YES=0
NO_PUSH=0
CHECK_ONLY=0
REPO_PATH="."

# ---------- arg parsing ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y)         YES=1; shift ;;
    --no-push)        NO_PUSH=1; shift ;;
    --check-only)     CHECK_ONLY=1; shift ;;
    --help|-h)
      sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      if [[ -z "${REPO_PATH_SET:-}" ]]; then
        REPO_PATH="$1"; REPO_PATH_SET=1; shift
      else
        echo "error: unexpected argument: $1" >&2; exit 2
      fi ;;
  esac
done

# ---------- helpers ----------
log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32mok\033[0m  %s\n' "$*"; }
warn() { printf '\033[1;33mwarn\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror\033[0m %s\n' "$*" >&2; exit 1; }

ask_yn() {
  [[ "$YES" == "1" ]] && { echo "y"; return; }
  local prompt="$1" default="${2:-y}"
  local hint
  if [[ "$default" == "y" ]]; then hint="Y/n"; else hint="y/N"; fi
  read -r -p "$prompt [$hint] " ans
  ans="${ans:-$default}"
  case "${ans:0:1}" in
    y|Y|'') echo "y" ;;
    *)      echo "n" ;;
  esac
}

# ---------- pre-flight ----------
log "pre-flight checks"
cd "$REPO_PATH" 2>/dev/null || die "could not cd to: $REPO_PATH"

[[ -d .git ]] || [[ -f .git ]] \
  || die "not in a git repo: $REPO_PATH (pass repo path as first arg)"

# Make sure git sees us as the bpftune repo
REMOTE_URL=$(git remote get-url origin 2>/dev/null || echo "")
if [[ "$REMOTE_URL" != *"bpftune"* ]]; then
  warn "remote origin URL doesn't contain 'bpftune': $REMOTE_URL"
  [[ "$(ask_yn 'continue anyway?' n)" == "y" ]] || exit 1
fi

BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$BRANCH" != "dashboard" ]]; then
  warn "currently on branch '$BRANCH', not 'dashboard'"
  if [[ "$(ask_yn 'switch to dashboard branch?' y)" == "y" ]]; then
    git checkout dashboard || die "checkout dashboard failed"
  else
    die "aborted: not on dashboard branch"
  fi
fi

# Pull latest so the patch applies on top of the user's current HEAD
log "fetching latest from origin"
git fetch origin dashboard || die "fetch failed"

LOCAL_HEAD=$(git rev-parse HEAD)
REMOTE_HEAD=$(git rev-parse origin/dashboard)
if [[ "$LOCAL_HEAD" != "$REMOTE_HEAD" ]]; then
  if git merge-base --is-ancestor "$LOCAL_HEAD" "$REMOTE_HEAD"; then
    log "local is behind origin/dashboard, pulling..."
    git pull --ff-only origin dashboard \
      || die "pull failed (non-fast-forward — resolve manually first)"
  else
    warn "local dashboard branch has commits not on origin/dashboard"
    warn "this is fine if you know what you're doing"
  fi
fi

# Sanity-check we're past the user's recent-swaps fix (commit 9118268).
# This patch builds on that work and will conflict if applied before it.
if ! git merge-base --is-ancestor 9118268 HEAD 2>/dev/null; then
  warn "HEAD doesn't contain commit 9118268 (user's recent-swaps fix)"
  warn "this patch builds on that work — fetch and pull first"
  die "aborted: HEAD is too old, please git pull first"
fi

# Working tree must be clean
if ! git diff --quiet || ! git diff --cached --quiet; then
  die "working tree has uncommitted changes — stash or commit first"
fi

# bpftune-cli.py must exist (sanity check that we're in the right place)
[[ -f dashboard/bin/bpftune-cli.py ]] \
  || die "dashboard/bin/bpftune-cli.py not found — wrong repo?"

ok "pre-flight passed"

# ---------- write embedded patch ----------
PATCH_FILE=$(mktemp /tmp/bpftune-0.4.87-XXXXXX.patch)
trap 'rm -f "$PATCH_FILE"' EXIT

log "writing embedded patch to $PATCH_FILE"
cat > "$PATCH_FILE" <<'__DASHBOARD_0_4_87_PATCH_END_SENTINEL__'
From e43421a0527ece4fbe22a01429315f0fa6664367 Mon Sep 17 00:00:00 2001
From: Dashboard Fixes <dashboard-fixes@local>
Date: Wed, 30 Sep 2026 15:08:59 +0000
Subject: [PATCH] dashboard 0.4.87: bucket-tag chips + All-Buckets chart
 refresh + metric lookup fix
MIME-Version: 1.0
Content-Type: text/plain; charset=UTF-8
Content-Transfer-Encoding: 8bit

Builds on the recent-swaps fix already on dashboard HEAD (commits
88dcf76..9118268).  Addresses the remaining two symptoms plus a
bonus localStorage-persistence regression.

Symptoms fixed:
  * bottom charts (rate/sscore/streaks/swaps-per-bin) appeared to
    stop drawing for labeled buckets and for 'All Buckets'
    (bucket_live lookup fell back to the 15-min renderer output;
    for All Buckets, renderBucket was gated on state.bucketDoc
    which is null, so the chart only refreshed every 5 min via
    refreshAll instead of every 30s via SSE).
  * no way to confirm at a glance which bucket each panel was
    showing.  Added .bucket-tag chips to 10 panel headers showing
    'bucket: <label>' or 'all buckets'.
  * bonus: range selector choice was silently lost on reload
    (two rs.onchange assignments — the second overwrote the
    first, dropping localStorage.setItem).

Changes:
  bpftune-render.py:emit_meta   - also emit 'label' per entry so
                                 the dropdown can show 'home-sco'
                                 instead of '76.76.0.0'.
  dashboard.js:_labelForBucketAddr
                                - new helper, returns label for
                                  any raw addr (used by metric
                                  lookup and bucket_live lookup).
  dashboard.js:renderMetricForBucket
                                - try raw then label form; remove
                                  silent fall-through to keys[0]
                                  (was invisible wrong-bucket bug).
  dashboard.js:renderBucket     - try raw then label for bucket_live
                                  lookup; new _aggregateAllBucketsLive
                                  for All-Buckets 1h chart so it
                                  refreshes every 30s instead of
                                  every 5 min.
  dashboard.js:_updateBucketTags + _BUCKET_TAG_IDS
                                - new; refreshes 10 .bucket-tag
                                  chips across all panel headers;
                                  wired into renderLiveState,
                                  _fetchAndApplyLabels,
                                  _reFilterPanels, bs.onchange,
                                  rs.onchange.
  dashboard.js:_reFilterPanels  - wrap four render calls in
                                  _safeRender so one throw no
                                  longer aborts the whole chain.
  dashboard.js:rs.onchange      - merge the two duplicate
                                  assignments (second had silently
                                  overwritten the first, dropping
                                  localStorage persistence of the
                                  range selector).
  dashboard.js:_populateBucketSelect
                                - display b.label in dropdown
                                  text, keep value=b.id so
                                  loadBucket / data/bucket_<id>.json
                                  still resolve.
  index.html                   - add 10 <span class=bucket-tag>
                                  chips (6 in live grid, 4 in
                                  chart card titles).
  dashboard.css                 - .bucket-tag chip styling,
                                  .is-all muted variant, chart-card
                                  variant.

Verified: 58/58 existing tests pass (no new tests added since
_bucket_of is no longer the bug surface after the user's
recent-swaps fix landed). node --check dashboard.js OK.
ast.parse on bpftune-render.py OK.  All 10 bucket-tag IDs in
index.html match _BUCKET_TAG_IDS in dashboard.js.
---
 dashboard/bin/bpftune-render.py |   5 +
 dashboard/bin/dashboard.css     |  26 ++++
 dashboard/bin/dashboard.js      | 203 +++++++++++++++++++++++++++++---
 dashboard/bin/index.html        |  20 ++--
 4 files changed, 228 insertions(+), 26 deletions(-)

diff --git a/dashboard/bin/bpftune-render.py b/dashboard/bin/bpftune-render.py
index b6586b4..988398d 100644
--- a/dashboard/bin/bpftune-render.py
+++ b/dashboard/bin/bpftune-render.py
@@ -432,8 +432,13 @@ def emit_meta(buckets, algs, now, primary=None):
         inst = [to_float(r.get("instances")) for r in rs]
         inst = [v for v in inst if v is not None]
         last = max((ts_of(r) or 0) for r in rs)
+        # 0.4.87: also emit the human-readable label so the dropdown
+        # can show "home-sco" instead of "76.76.0.0".  Falls back to
+        # the raw addr if no label is defined.
+        label = _label_for(bid)
         entries.append({
             "id": bid,
+            "label": label if label and label != bid else bid,
             "points": len(rs),
             "instances_mean": round(sum(inst) / len(inst), 2) if inst else 0,
             "last_ts": int(last),
diff --git a/dashboard/bin/dashboard.css b/dashboard/bin/dashboard.css
index 27f4ed7..0e83abb 100644
--- a/dashboard/bin/dashboard.css
+++ b/dashboard/bin/dashboard.css
@@ -229,6 +229,32 @@
     font-family: var(--mono); font-size: 10.5px;
     font-weight: 500; letter-spacing: 0; text-transform: none;
   }
+  /* 0.4.87: bucket-tag chip — shows which bucket each panel's data is from.
+     Stands out from .cnt so the user can immediately tell data origin. */
+  .bucket-tag {
+    margin-left: 8px;
+    display: inline-block;
+    color: var(--accent, #4e79a7);
+    background: rgba(78,121,167,.10);
+    border: 1px solid rgba(78,121,167,.35);
+    border-radius: 4px;
+    padding: 1px 7px;
+    font-family: var(--mono);
+    font-size: 10.5px;
+    font-weight: 600;
+    letter-spacing: .02em;
+    text-transform: none;
+  }
+  .bucket-tag.is-all {
+    color: var(--muted-2);
+    background: transparent;
+    border-color: var(--border);
+  }
+  .card > h2 .bucket-tag {
+    margin-left: 10px;
+    font-size: 11px;
+    vertical-align: middle;
+  }
   .lv-grid .note {
     margin-top: 8px; padding-top: 8px;
     border-top: 1px dashed var(--border);
diff --git a/dashboard/bin/dashboard.js b/dashboard/bin/dashboard.js
index ca49576..caba04f 100644
--- a/dashboard/bin/dashboard.js
+++ b/dashboard/bin/dashboard.js
@@ -255,8 +255,18 @@
     }
     var byB = state.metricByBucket || {};
     var keys = Object.keys(byB);
-    var rows = (addr && byB[addr]) ? byB[addr]
-                                   : (keys.length ? byB[keys[0]] : []);
+    // 0.4.87: try the raw addr first (the dropdown value), then the
+    // label form (metric_by_bucket is keyed by _label_for(addr) on
+    // the python side via read_map, so for labeled buckets only the
+    // label form matches).
+    var rows = (addr && byB[addr]) ? byB[addr] : [];
+    if (!rows.length) {
+      var lbl = _labelForBucketAddr(addr);
+      if (lbl && lbl !== addr && byB[lbl]) rows = byB[lbl];
+    }
+    // 0.4.87: do NOT silently fall back to keys[0] (the first bucket).
+    // The previous behaviour showed whichever bucket happened to sort
+    // first, which looked correct but was for the wrong destination.
     renderMetric(rows);
   }
 
@@ -696,6 +706,7 @@
   function _reFilterPanels() {
     var doc = window.__current_doc;
     if (!doc) return;
+    _updateBucketTags();   // 0.4.87: keep panel headers in sync on dropdown change
     var bid = $('bucket') ? $('bucket').value : 'all';
     var blabel = 'all';
     if (bid !== 'all') {
@@ -704,10 +715,10 @@
         (bs && bs.selectedIndex >= 0 ? bs.options[bs.selectedIndex].text.replace(/ \(\d+\)$/, '') : bid);
     }
     var fdoc = bid === 'all' ? doc : _filterByBucket(doc, blabel);
-    renderProof(fdoc.proof || []);
-    renderRate(fdoc.rate || []);
-    renderSwapOutcomes(fdoc.swap_outcomes || null, fdoc.churn || {});
-    renderRecentProofs(fdoc.recent_proofs || []);
+    _safeRender('proof',       function() { renderProof(fdoc.proof || []); });
+    _safeRender('rate',        function() { renderRate(fdoc.rate || []); });
+    _safeRender('swap_outcomes', function() { renderSwapOutcomes(fdoc.swap_outcomes || null, fdoc.churn || {}); });
+    _safeRender('recent_proofs', function() { renderRecentProofs(fdoc.recent_proofs || []); });
     window.__filtered_doc = fdoc;
     renderSwaps();
   }
@@ -745,6 +756,61 @@
     return {bid: _bid, label: _blabel};
   }
 
+  // 0.4.87: lookup the human-readable label for any raw bucket addr.
+  // Used by renderMetricForBucket / renderBucket to find the right
+  // entry in dicts that the python side keys by _label_for(addr)
+  // (i.e. label if labeled, raw addr otherwise).  Returns null if no
+  // label is known for the addr.
+  function _labelForBucketAddr(addr) {
+    if (!addr || addr === 'all') return addr;
+    if (window.__labels && window.__labels[addr]) {
+      return window.__labels[addr];
+    }
+    // v6:hex form may be labeled under the canonical "xxxx:xxxx::" form.
+    if (addr.indexOf('v6:') === 0) {
+      var hex = addr.substring(3);
+      if (hex.length >= 8) {
+        var ip6 = hex.substring(0,4) + ':' + hex.substring(4,8) + '::';
+        if (window.__labels && window.__labels[ip6]) return window.__labels[ip6];
+      }
+    }
+    // The dropdown option text may already carry the label (set by
+    // _fetchAndApplyLabels after the /api/labels fetch).  Strip the
+    // trailing " (NN)" point count.
+    var bs = $('bucket');
+    if (bs && bs.options) {
+      for (var i = 0; i < bs.options.length; i++) {
+        if (bs.options[i].value === addr) {
+          return bs.options[i].text.replace(/ \(\d+\)$/, '');
+        }
+      }
+    }
+    return null;
+  }
+
+  // 0.4.87: update every .bucket-tag span with the current bucket's
+  // label so the user can see at a glance which bucket's data each
+  // panel is showing.  Called on every renderLiveState / bucket change.
+  var _BUCKET_TAG_IDS = [
+    'bucket-tag-metric', 'bucket-tag-swaps',
+    'bucket-tag-proof', 'bucket-tag-proofs',
+    'bucket-tag-rate', 'bucket-tag-swapout',
+    'bucket-tag-ratechart', 'bucket-tag-sscore',
+    'bucket-tag-streaks', 'bucket-tag-swapschart'
+  ];
+  function _updateBucketTags() {
+    var bk = _currentBucketLabel();
+    var text = bk.bid === 'all' ? 'all buckets'
+                                 : ('bucket: ' + bk.label);
+    for (var i = 0; i < _BUCKET_TAG_IDS.length; i++) {
+      var el = document.getElementById(_BUCKET_TAG_IDS[i]);
+      if (el) {
+        el.textContent = text;
+        el.classList.toggle('is-all', bk.bid === 'all');
+      }
+    }
+  }
+
   function _renderFilteredPanels(doc) {
     var bk = _currentBucketLabel();
     var _fdoc = bk.bid === 'all' ? doc : _filterByBucket(doc, bk.label);
@@ -775,6 +841,14 @@
           }
         }
       }
+      // 0.4.87: labels just landed — refresh the bucket-tag chips so
+      // panel headers reflect the new label, and re-render the panels
+      // that look up by label (metric / bucket_live chart).
+      _updateBucketTags();
+      _safeRender('metric_for_bucket', function() { renderMetricForBucket(); });
+      if ($("range") && state.bucketDoc) {
+        _safeRender('bucket_chart', function() { renderBucket(); });
+      }
     }).catch(function() {});
   }
 
@@ -786,12 +860,17 @@
     _safeRender('system', function() { renderSystem(doc.system || {}); });
     _safeRender('tunables', function() { renderTunables(doc.tunables || []); });
     _syncBucketDropdown(doc);
+    _updateBucketTags();
     state.metricByBucket = doc.metric_by_bucket || {};
     state.bucketLive = doc.bucket_live || {};
     state.recentSwapsByBucket = doc.recent_swaps_by_bucket || null;
     _safeRender('metric_for_bucket', function() { renderMetricForBucket(); });
     _safeRender('recent_swaps_for_bucket', function() { renderRecentSwapsForBucket(); });
-    if ($("range") && state.bucketDoc) {
+    // 0.4.87: always call renderBucket on every SSE push — even when
+    // "All Buckets" is selected (state.bucketDoc === null).  The new
+    // All-Buckets path aggregates state.bucketLive so the rate/sscore/
+    // streaks charts refresh on every 30s tick instead of every 5 min.
+    if ($("range")) {
       _safeRender('bucket_chart', function() { renderBucket(); });
     }
     _renderFilteredPanels(doc);
@@ -1110,24 +1189,105 @@
     return result;
   }
 
+  // 0.4.87: aggregate all state.bucketLive entries into one combined
+  // {ts, cols} for the "All Buckets" 1h view.  Without this, the rate /
+  // swap-score / streaks charts only refreshed every 5 min via refreshAll
+  // when "All Buckets" was selected (because state.bucketDoc is null in
+  // that case and renderBucket was gated on it).  Now the charts refresh
+  // on every SSE push (~30s) using the same source as the per-bucket view.
+  //
+  // Per-bin aggregation: SUM across buckets for re_/ss_ (the picker's
+  // totals across the whole fleet); MAX across buckets for bs_/ns_
+  // (worst streak anywhere, since a single bad streak is the
+  // operator's signal).  Empty bins stay null.
+  function _aggregateAllBucketsLive() {
+    var keys = Object.keys(state.bucketLive || {});
+    if (!keys.length) return null;
+    var tsSet = {};
+    for (var i = 0; i < keys.length; i++) {
+      var lb = state.bucketLive[keys[i]];
+      if (lb && lb.ts) {
+        for (var j = 0; j < lb.ts.length; j++) tsSet[lb.ts[j]] = true;
+      }
+    }
+    var ts = Object.keys(tsSet).map(Number).sort(function(a, b) { return a - b; });
+    if (!ts.length) return null;
+    var tsIdx = {};
+    for (var k = 0; k < ts.length; k++) tsIdx[ts[k]] = k;
+    var cols = {};
+    var sumPrefixes = { re_: true, ss_: true };
+    var maxPrefixes = { bs_: true, ns_: true };
+    for (var b = 0; b < keys.length; b++) {
+      var lb2 = state.bucketLive[keys[b]];
+      if (!lb2 || !lb2.cols) continue;
+      var lbTs = lb2.ts || [];
+      for (var col in lb2.cols) {
+        if (!cols[col]) cols[col] = new Array(ts.length).fill(null);
+        var vals = lb2.cols[col];
+        var isMax = !!maxPrefixes[col.substring(0, 3)];
+        for (var v = 0; v < vals.length; v++) {
+          var idx = tsIdx[lbTs[v]];
+          if (idx == null) continue;
+          var cur = cols[col][idx];
+          var val = vals[v];
+          if (val == null) continue;
+          if (cur == null) {
+            cols[col][idx] = val;
+          } else if (isMax) {
+            if (val > cur) cols[col][idx] = val;
+          } else {
+            cols[col][idx] = cur + val;
+          }
+        }
+      }
+    }
+    return {ts: ts, cols: cols};
+  }
+
   function renderBucket() {
     var doc = state.bucketDoc, algs = state.meta.algs;
+    if (!algs) return;   // 0.4.87: meta not loaded yet — boot() still running
     var rng = $("range").value;
-    var s = doc.series[rng];
-    var ts = s.ts;
-    // 0.4.79: for the 1h range, prefer the CLI-provided ring
-    // (60s freshness) for all four series.  bucket_live now
-    // carries re_/ss_/bs_/ns_, so the same source serves the
-    // rate, score, and streak charts.  Other ranges still load
-    // the 15-minute renderer output on demand.
     var bid = $("bucket") ? $("bucket").value : null;
-    if (rng === "1h" && bid && state.bucketLive && state.bucketLive[bid]) {
+    var isAll = (bid === 'all' || !bid);
+    var s, ts;
+    // 0.4.87: for "All Buckets" 1h, aggregate state.bucketLive so the
+    // chart refreshes on every SSE push instead of every 5 min.
+    if (isAll && rng === "1h") {
+      var agg = _aggregateAllBucketsLive();
+      if (agg) { s = agg.cols; ts = agg.ts; }
+    }
+    // For specific bucket 1h, use that bucket's bucket_live entry.
+    if (s == null && rng === "1h" && bid && state.bucketLive && state.bucketLive[bid]) {
       var lb = state.bucketLive[bid];
       if (lb.ts && lb.ts.length) {
         s = {};
         for (var k in (lb.cols || {})) s[k] = lb.cols[k];
         ts = lb.ts;
       }
+    } else if (s == null && rng === "1h" && bid && state.bucketLive) {
+      // 0.4.87: bucket_live is keyed by _label_for(addr) on the python
+      // side (bpftune_data.py:385), so for labeled buckets the dropdown's
+      // raw addr (b.id from meta.json) doesn't match.  Try the label
+      // form before falling back to the 15-min renderer output.
+      var _lbl = _labelForBucketAddr(bid);
+      if (_lbl && _lbl !== bid && state.bucketLive[_lbl]) {
+        var _lb = state.bucketLive[_lbl];
+        if (_lb.ts && _lb.ts.length) {
+          s = {};
+          for (var _k in (_lb.cols || {})) s[_k] = _lb.cols[_k];
+          ts = _lb.ts;
+        }
+      }
+    }
+    // Fall back to the renderer's 15-min bucket_<id>.json output for
+    // longer ranges, or for 1h if bucket_live didn't have the entry.
+    if (s == null) {
+      if (!doc) return;   // "All Buckets" + range > 1h: no historical aggregate
+      var sDoc = doc.series[rng];
+      if (!sDoc) return;
+      s = sDoc;
+      ts = sDoc.ts;
     }
     // Build a fixed-axis ts anchored to NOW so all four charts (rate, score,
     // streak, swaps-per-bin) share the exact same x-axis: [now-rSec, now] at
@@ -1448,7 +1608,11 @@ function _populateBucketSelect(desiredBucket) {
   var stillThere = false;
   for (var k = 0; k < state.meta.buckets.length; k++) {
     var b = state.meta.buckets[k];
-    html += '<option value="' + b.id + '">' + b.id +
+    // 0.4.87: prefer b.label (added in emit_meta) for the visible
+    // text; fall back to b.id.  The <option>.value stays as b.id so
+    // loadBucket / data/bucket_<id>.json lookups keep working.
+    var disp = (b.label && b.label !== b.id) ? b.label : b.id;
+    html += '<option value="' + b.id + '">' + disp +
             ' (' + b.points + ')</option>';
     if (b.id === desiredBucket) stillThere = true;
   }
@@ -1546,11 +1710,18 @@ function _populateBucketSelect(desiredBucket) {
           loadBucket(bs.value);
           renderMetricForBucket();
           renderRecentSwapsForBucket();
+          _updateBucketTags();   // 0.4.87: panel headers follow the dropdown
         };
         try { localStorage.setItem("bpftune.bucket", bs.value); } catch (e) {}
+        // 0.4.87: merge the two rs.onchange assignments — the previous
+        // code overwrote the first (which persisted to localStorage)
+        // with the second (which didn't), so the user's range choice
+        // was lost on reload.
         rs.onchange = function () {
+          try { localStorage.setItem("bpftune.range", rs.value); } catch (e) {}
           renderBucket();
           renderSwaps();
+          _updateBucketTags();
         };
 
         /* 0.4.79 fix: load the bucket the dropdown actually shows
diff --git a/dashboard/bin/index.html b/dashboard/bin/index.html
index d24d31e..11233eb 100644
--- a/dashboard/bin/index.html
+++ b/dashboard/bin/index.html
@@ -79,7 +79,7 @@
     </section>
 
     <section class="c8">
-      <h3>swap target leaderboard <span class="cnt">top row = picker's choice</span></h3>
+      <h3>swap target leaderboard <span class="cnt">top row = picker's choice</span> <span class="bucket-tag" id="bucket-tag-metric"></span></h3>
       <div id="lv-metric"></div>
       <div class="note">
         <b>score</b> = <code>Rate EMA &times; Swap Score / 256 &times; penalty</code>.
@@ -88,12 +88,12 @@
     </section>
 
     <section class="c4">
-      <h3>recent swaps <span class="cnt">target + outcome</span></h3>
+      <h3>recent swaps <span class="cnt">target + outcome</span> <span class="bucket-tag" id="bucket-tag-swaps"></span></h3>
       <div id="lv-swaps"></div>
     </section>
 
     <section class="c8">
-      <h3>proof leaderboard <span class="cnt">Mb/s</span></h3>
+      <h3>proof leaderboard <span class="cnt">Mb/s</span> <span class="bucket-tag" id="bucket-tag-proof"></span></h3>
       <div id="lv-proof"></div>
       <div class="note">
         bar colors:
@@ -104,17 +104,17 @@
     </section>
 
     <section class="c4">
-      <h3>recent proofs <span class="cnt">Mb/s</span></h3>
+      <h3>recent proofs <span class="cnt">Mb/s</span> <span class="bucket-tag" id="bucket-tag-proofs"></span></h3>
       <div id="lv-proofs"></div>
     </section>
 
     <section class="c8">
-      <h3>rate progression <span class="cnt">client &middot; Mb/s</span></h3>
+      <h3>rate progression <span class="cnt">client &middot; Mb/s</span> <span class="bucket-tag" id="bucket-tag-rate"></span></h3>
       <div id="lv-rate"></div>
     </section>
 
     <section class="c4">
-      <h3>swap outcomes <span class="cnt">sustained</span> <span id="time-window-outcomes" class="cnt"></span></h3>
+      <h3>swap outcomes <span class="cnt">sustained</span> <span id="time-window-outcomes" class="cnt"></span> <span class="bucket-tag" id="bucket-tag-swapout"></span></h3>
       <div id="lv-swapout"></div>
       <div class="note">
         <b>sustained</b> = median srate in [t+60, t+300].
@@ -123,22 +123,22 @@
   </div>
 
   <section class="card">
-    <h2><span class="dot"></span>Rate EMA per algorithm &mdash; Mb/s</h2>
+    <h2><span class="dot"></span>Rate EMA per algorithm &mdash; Mb/s <span class="bucket-tag" id="bucket-tag-ratechart"></span></h2>
     <div class="chart-box h-lg"><canvas id="rate"></canvas></div>
   </section>
 
   <section class="card">
-    <h2><span class="dot"></span>Swap Score per algorithm <span class="sub">256 = neutral &middot; above = swaps into this alg have been helping</span></h2>
+    <h2><span class="dot"></span>Swap Score per algorithm <span class="sub">256 = neutral &middot; above = swaps into this alg have been helping</span> <span class="bucket-tag" id="bucket-tag-sscore"></span></h2>
     <div class="chart-box h-lg"><canvas id="sscore"></canvas></div>
   </section>
 
   <section class="card">
-    <h2><span class="dot"></span>Bad Streak / Null Streak per algorithm <span class="sub">above 0 = picker penalty in effect</span></h2>
+    <h2><span class="dot"></span>Bad Streak / Null Streak per algorithm <span class="sub">above 0 = picker penalty in effect</span> <span class="bucket-tag" id="bucket-tag-streaks"></span></h2>
     <div class="chart-box h-lg"><canvas id="streaks"></canvas></div>
   </section>
 
   <section class="card">
-    <h2><span class="dot"></span>swaps per bin</h2>
+    <h2><span class="dot"></span>swaps per bin <span class="bucket-tag" id="bucket-tag-swapschart"></span></h2>
     <div class="chart-box h-sm"><canvas id="swaps"></canvas></div>
   </section>
 
-- 
2.47.3

__DASHBOARD_0_4_87_PATCH_END_SENTINEL__

ok "patch written ($(wc -l < "$PATCH_FILE") lines)"

# ---------- check-only mode ----------
if [[ "$CHECK_ONLY" == "1" ]]; then
  log "check-only mode: dry-running git apply --check"
  if git apply --check "$PATCH_FILE" 2>/tmp/am-check.err; then
    ok "patch applies cleanly"
    exit 0
  else
    cat /tmp/am-check.err >&2
    die "patch does NOT apply cleanly — see error above"
  fi
fi

# ---------- apply via git am ----------
log "applying patch with git am"
if ! git am --whitespace=nowarn "$PATCH_FILE"; then
  warn "git am failed — patch likely already applied or context drifted"
  warn "running git am --abort to clean up"
  git am --abort 2>/dev/null || true

  # Check if the patch is already applied (commit subject match)
  if git log --format='%s' -1 HEAD 2>/dev/null | grep -q "0.4.87: bucket-tag chips"; then
    ok "patch appears to already be applied (commit message matches)"
    [[ "$YES" == "1" ]] || [[ "$(ask_yn 'continue to tests?' y)" == "y" ]] || exit 0
  else
    die "git am failed and commit not found — inspect manually"
  fi
fi
ok "patch applied as commit: $(git log --oneline -1)"

# ---------- run tests ----------
log "running test suite (58 tests expected)"
if ! ( cd dashboard/bin \
       && BPFTUNE_CLI_PATH="$(pwd)/bpftune-cli.py" \
          python3 -m pytest test_bpftune_cli.py -q 2>&1 ); then
  warn "TESTS FAILED"
  warn "the commit is still in your local history"
  warn "to undo:  git reset --hard HEAD~1"
  if [[ "$(ask_yn 'undo the commit now?' y)" == "y" ]]; then
    git reset --hard HEAD~1
    ok "commit undone, working tree restored"
    exit 1
  fi
  exit 1
fi
ok "tests passed"

# ---------- push ----------
if [[ "$NO_PUSH" == "1" ]]; then
  log "skip push (--no-push)"
  exit 0
fi

log "ready to push to origin/dashboard"
git log --oneline origin/dashboard..HEAD
echo
if [[ "$(ask_yn 'push now?' y)" == "y" ]]; then
  git push origin dashboard
  ok "pushed to origin/dashboard"
  log "done.  On the target host(s):"
  echo "  cd ~/bpftune && git pull"
  echo "  sudo bash dashboard/deploy.sh --force"
else
  warn "not pushing.  Push manually with:"
  echo "  git push origin dashboard"
fi
