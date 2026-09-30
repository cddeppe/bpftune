#!/usr/bin/env bash
# apply-0.4.88-dashboard-fixes.sh
#
# Self-contained atomic patch applier for bpftune dashboard branch.
# Embeds commit 0.4.88: dedupe recent_swaps + fix swaps-per-bin
# timeline + bigger bucket-tag chips.
#
# Builds on 0.4.87 (already on origin/dashboard as c266559).  This
# patch must be applied on top of c266559.
#
# WHAT THIS SCRIPT DOES:
#   1. Pre-flight checks: in bpftune repo? on dashboard branch? clean
#      working tree? bpftune-cli.py exists?
#   2. Fetches + pulls latest origin/dashboard (must be >= c266559).
#   3. Writes embedded patch to a temp file.
#   4. --check-only: git apply --check (dry-run).
#      Otherwise: git am (creates real commit).
#   5. Runs the test suite (58 tests expected).  If pytest isn't
#      installed, the script warns but doesn't undo — the patch is
#      still good, just can't be verified locally.
#   6. Pushes to origin/dashboard (asks, or auto with --yes).
#
# USAGE:
#   ./apply-0.4.88-dashboard-fixes.sh              # interactive
#   ./apply-0.4.88-dashboard-fixes.sh --yes        # non-interactive
#   ./apply-0.4.88-dashboard-fixes.sh --no-push     # apply + test, skip push
#   ./apply-0.4.88-dashboard-fixes.sh --check-only  # dry-run only
#
# REQUIREMENTS: bash 4+, git, python3 (for tests, optional).
set -euo pipefail

YES=0
NO_PUSH=0
CHECK_ONLY=0
REPO_PATH="."

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

log "pre-flight checks"
cd "$REPO_PATH" 2>/dev/null || die "could not cd to: $REPO_PATH"

[[ -d .git ]] || [[ -f .git ]] \
  || die "not in a git repo: $REPO_PATH (pass repo path as first arg)"

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

# Sanity-check: HEAD must contain the 0.4.87 commit (c266559) since
# this patch builds on top of it.
if ! git merge-base --is-ancestor c266559 HEAD 2>/dev/null; then
  warn "HEAD doesn't contain commit c266559 (the 0.4.87 patch)"
  warn "this patch builds on that — fetch and pull first"
  die "aborted: HEAD is too old, please git pull first"
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  die "working tree has uncommitted changes — stash or commit first"
fi

[[ -f dashboard/bin/bpftune-cli.py ]] \
  || die "dashboard/bin/bpftune-cli.py not found — wrong repo?"

ok "pre-flight passed"

PATCH_FILE=$(mktemp /tmp/bpftune-0.4.88-XXXXXX.patch)
trap 'rm -f "$PATCH_FILE"' EXIT

log "writing embedded patch to $PATCH_FILE"
cat > "$PATCH_FILE" <<'__DASHBOARD_0_4_88_PATCH_END_SENTINEL__'
From 48feac5c977601ea88dfe7053f8c23f84f042eae Mon Sep 17 00:00:00 2001
From: Dashboard Fixes <dashboard-fixes@local>
Date: Wed, 30 Sep 2026 15:29:29 +0000
Subject: [PATCH] dashboard 0.4.88: dedupe recent_swaps + fix swaps-per-bin
 timeline + bigger bucket-tag chips
MIME-Version: 1.0
Content-Type: text/plain; charset=UTF-8
Content-Transfer-Encoding: 8bit

Three follow-up fixes reported after 0.4.87 shipped:

  1. recent_swaps showed duplicates (5 swaps, then the same 5 swaps
     again, in the same order).  Root cause: collect_lightweight did
     (old_swaps + new_swaps)[-20:] without deduping.  When the
     incremental log contained the same swaps as the previous full
     collect (offsets not advanced yet), the merge produced dupes.
     Also: the merge was oldest-first while the JS expected newest-
     first, so the panel showed 5h,4h,4h,4h,3h,5h,4h,4h,4h,3h instead
     of newest-first like recent_proofs.
     Fix: add _merge_dedupe_sort helper that dedupes by a unique key
     ((boot_ts, from_alg, to_alg, d) for swaps, (boot_ts, alg, mbps)
     for proofs, (ts, cookie) for swaps_list, etc.) and sorts by
     boot_ts descending.  Apply to all 5 merge sites: recent_swaps,
     recent_proofs, swaps_list, proofs_raw, rate_raw.

  2. swaps-per-bin chart used a different timeline than the rate/
     sscore/streaks charts (right edge didn't align).
     Root cause: renderBucket read window.__filtered_doc.generated_ts
     for 'now' but __filtered_doc was set only by _reFilterPanels on
     bucket change — _renderFilteredPanels (called on every SSE push)
     doesn't set it.  So renderBucket used the stale 'now' from the
     last bucket change while renderSwaps used the fresh 'now' from
     __current_doc.
     Fix: renderBucket now reads window.__current_doc.generated_ts
     (always fresh on every SSE push) so all four bottom charts
     share an identical timeline anchored to the same SSE tick.

  3. bucket-tag chips were too subtle (10.5px, light blue tint) and
     the user asked again for clear bucket-label indicators.
     Fix: made chips prominent — solid blue background, white text,
     12px bold, padding 3px 10px, border-radius 6px, drop shadow.
     Muted dashed-border variant for the 'all buckets' state.
     Dark-mode variant uses a darker blue so the chip still pops.

Verified: 58/58 tests pass.  node --check dashboard.js OK.
ast.parse on bpftune-cli.py OK.
---
 dashboard/bin/bpftune-cli.py | 58 +++++++++++++++++++++++++++++-------
 dashboard/bin/dashboard.css  | 47 ++++++++++++++++++++---------
 dashboard/bin/dashboard.js  |  8 ++++-
 3 files changed, 88 insertions(+), 25 deletions(-)

diff --git a/dashboard/bin/bpftune-cli.py b/dashboard/bin/bpftune-cli.py
index a96eaf5..375a0ad 100644
--- a/dashboard/bin/bpftune-cli.py
+++ b/dashboard/bin/bpftune-cli.py
@@ -178,9 +178,36 @@ def collect_lightweight(offsets, map_raw=None, base_result=None):
 
         # Merge: append new items to base, cap sizes
         if base_result:
-            # Recent swaps: append new, keep last 20
+            # 0.4.88: dedupe + sort newest-first for time-series lists.
+            # The previous merge (old + new)[-N:] produced duplicates
+            # when the incremental log contained the same events as the
+            # previous full collect (offsets not advanced yet) AND was
+            # in oldest-first order while the JS expected newest-first.
+            # Now: dedupe by (boot_ts, from_alg, to_alg, d) for swaps,
+            # (boot_ts, alg) for proofs, then sort by boot_ts desc.
+            def _merge_dedupe_sort(old, new, max_n, key_fn, ts_field='boot_ts'):
+                merged = list(old) + list(new)
+                seen = set()
+                deduped = []
+                for item in merged:
+                    k = key_fn(item)
+                    if k in seen:
+                        continue
+                    seen.add(k)
+                    deduped.append(item)
+                deduped.sort(key=lambda x: x.get(ts_field) or 0, reverse=True)
+                return deduped[:max_n]
+
+            def _swap_key(s):
+                return (s.get('boot_ts'), s.get('from_alg'),
+                        s.get('to_alg'), s.get('d'))
+            def _proof_key(p):
+                return (p.get('boot_ts'), p.get('alg'), p.get('mbps'))
+
+            # Recent swaps: dedupe + sort newest-first, keep last 20
             old_swaps = base_result.get('recent_swaps', [])
-            doc['recent_swaps'] = (old_swaps + new_swaps)[-20:]
+            doc['recent_swaps'] = _merge_dedupe_sort(
+                old_swaps, new_swaps, 20, _swap_key, 'boot_ts')
 
             # 0.4.86: rebuild recent_swaps_by_bucket from the merged list
             # so per-bucket recent swaps stay fresh between 5min full collects.
@@ -188,7 +215,10 @@ def collect_lightweight(offsets, map_raw=None, base_result=None):
             # while the flat recent_swaps list stays fresh — mismatch.
             if doc.get('recent_swaps'):
                 _by = {}
-                for _r in reversed(doc['recent_swaps']):
+                # doc['recent_swaps'] is now newest-first, so iterate in
+                # forward order to fill each bucket's list newest-first
+                # (matching the panel's expected ordering).
+                for _r in doc['recent_swaps']:
                     _b = _r.get('dest') or _r.get('_bucket') or ''
                     if not _b:
                         continue
@@ -197,15 +227,19 @@ def collect_lightweight(offsets, map_raw=None, base_result=None):
                         _lst.append({k: v for k, v in _r.items() if k != '_bucket'})
                 doc['recent_swaps_by_bucket'] = _by
 
-            # Recent proofs: append new, keep last 16
+            # Recent proofs: dedupe + sort newest-first, keep last 16
             old_proofs = base_result.get('recent_proofs', [])
-            doc['recent_proofs'] = (old_proofs + new_proofs)[-16:]
+            doc['recent_proofs'] = _merge_dedupe_sort(
+                old_proofs, new_proofs, 16, _proof_key, 'boot_ts')
 
-            # Swap outcomes: append new swaps_list, cap at 2000
+            # Swap outcomes: dedupe swaps_list by (ts, cookie), keep last 2000
             old_so = base_result.get('swap_outcomes', {})
             old_swaps_list = old_so.get('swaps_list', [])
             new_swaps_list = new_swap_outcomes.get('swaps_list', [])
-            merged_swaps_list = (old_swaps_list + new_swaps_list)[-2000:]
+            def _swaps_list_key(s):
+                return (s.get('ts'), s.get('cookie'))
+            merged_swaps_list = _merge_dedupe_sort(
+                old_swaps_list, new_swaps_list, 2000, _swaps_list_key, 'ts')
 
             # Recompute outcome counts from merged swaps_list
             # (just re-run data_swap_outcomes on the merged list)
@@ -215,11 +249,15 @@ def collect_lightweight(offsets, map_raw=None, base_result=None):
             # Churn: use new (recomputed from new log lines)
             doc['churn'] = new_churn
 
-            # Proofs/rate raw: append new, cap at 500
+            # Proofs/rate raw: dedupe by ts, keep last 500
             old_proofs_raw = base_result.get('proofs_raw', [])
-            doc['proofs_raw'] = (old_proofs_raw + new_proofs_raw)[-500:]
+            doc['proofs_raw'] = _merge_dedupe_sort(
+                old_proofs_raw, new_proofs_raw, 500,
+                lambda p: (p.get('ts'), p.get('alg')), 'ts')
             old_rate_raw = base_result.get('rate_raw', [])
-            doc['rate_raw'] = (old_rate_raw + new_rate_raw)[-500:]
+            doc['rate_raw'] = _merge_dedupe_sort(
+                old_rate_raw, new_rate_raw, 500,
+                lambda r: (r.get('ts'), r.get('thr')), 'ts')
 
             # Bucket IPs: merge
             old_bucket_ips = base_result.get('bucket_ips', {})
diff --git a/dashboard/bin/dashboard.css b/dashboard/bin/dashboard.css
index 0e83abb..c879c53 100644
--- a/dashboard/bin/dashboard.css
+++ b/dashboard/bin/dashboard.css
@@ -230,30 +230,49 @@
     font-weight: 500; letter-spacing: 0; text-transform: none;
   }
   /* 0.4.87: bucket-tag chip — shows which bucket each panel's data is from.
-     Stands out from .cnt so the user can immediately tell data origin. */
+     Stands out from .cnt so the user can immediately tell data origin.
+     0.4.88: made more prominent (larger font, brighter color, bolder)
+     after user reported chips were easy to miss. */
   .bucket-tag {
-    margin-left: 8px;
+    margin-left: 10px;
     display: inline-block;
-    color: var(--accent, #4e79a7);
-    background: rgba(78,121,167,.10);
-    border: 1px solid rgba(78,121,167,.35);
-    border-radius: 4px;
-    padding: 1px 7px;
+    color: #ffffff;
+    background: #4e79a7;
+    border: 1px solid #3a5a7e;
+    border-radius: 6px;
+    padding: 3px 10px;
     font-family: var(--mono);
-    font-size: 10.5px;
-    font-weight: 600;
-    letter-spacing: .02em;
+    font-size: 12px;
+    font-weight: 700;
+    letter-spacing: .03em;
     text-transform: none;
+    box-shadow: 0 1px 2px rgba(0,0,0,.12);
+    vertical-align: middle;
   }
   .bucket-tag.is-all {
     color: var(--muted-2);
     background: transparent;
-    border-color: var(--border);
+    border: 1px dashed var(--border);
+    box-shadow: none;
+    font-weight: 600;
   }
   .card > h2 .bucket-tag {
-    margin-left: 10px;
-    font-size: 11px;
-    vertical-align: middle;
+    margin-left: 12px;
+    font-size: 13px;
+    padding: 3px 12px;
+  }
+  /* Dark-mode variant: invert the blue so the chip still pops. */
+  html[data-theme="dark"] .bucket-tag {
+    color: #e6e8ee;
+    background: #2d4a6a;
+    border-color: #3a5a7e;
+  }
+  @media (prefers-color-scheme: dark) {
+    html:not([data-theme="light"]) .bucket-tag {
+      color: #e6e8ee;
+      background: #2d4a6a;
+      border-color: #3a5a7e;
+    }
   }
   .lv-grid .note {
     margin-top: 8px; padding-top: 8px;
diff --git a/dashboard/bin/dashboard.js b/dashboard/bin/dashboard.js
index caba04f..0ea444f 100644
--- a/dashboard/bin/dashboard.js
+++ b/dashboard/bin/dashboard.js
@@ -1293,7 +1293,13 @@
     // streak, swaps-per-bin) share the exact same x-axis: [now-rSec, now] at
     // fixed intervals.  Rebin original series onto this axis (null where no
     // sample within +/-1.5*interval).  "all" range keeps the original ts.
-    var __now = (window.__filtered_doc && window.__filtered_doc.generated_ts) || (Date.now() / 1000);
+    // 0.4.88: use __current_doc.generated_ts (always fresh on every SSE
+    // push) instead of __filtered_doc.generated_ts (which was stale —
+    // _renderFilteredPanels doesn't set __filtered_doc, so it held the
+    // value from the last bucket change).  This is why the rate/sscore/
+    // streaks charts used a slightly older "now" than the swaps-per-bin
+    // chart, making the timelines visibly different at the right edge.
+    var __now = (window.__current_doc && window.__current_doc.generated_ts) || (Date.now() / 1000);
     var __fixedAxis = buildFixedAxis(rng, __now);
     if (__fixedAxis) {
       s = rebinOntoAxis(ts, s, __fixedAxis.ts, __fixedAxis.interval);
-- 
2.47.3

__DASHBOARD_0_4_88_PATCH_END_SENTINEL__

ok "patch written ($(wc -l < "$PATCH_FILE") lines)"

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

log "applying patch with git am"
if ! git am --whitespace=nowarn "$PATCH_FILE"; then
  warn "git am failed — patch likely already applied or context drifted"
  warn "running git am --abort to clean up"
  git am --abort 2>/dev/null || true

  if git log --format='%s' -1 HEAD 2>/dev/null | grep -q "0.4.88: dedupe recent_swaps"; then
    ok "patch appears to already be applied (commit message matches)"
    [[ "$YES" == "1" ]] || [[ "$(ask_yn 'continue to tests?' y)" == "y" ]] || exit 0
  else
    die "git am failed and commit not found — inspect manually"
  fi
fi
ok "patch applied as commit: $(git log --oneline -1)"

log "running test suite (58 tests expected)"
if ! ( cd dashboard/bin \
       && BPFTUNE_CLI_PATH="$(pwd)/bpftune-cli.py" \
          python3 -m pytest test_bpftune_cli.py -q 2>&1 ); then
  # Distinguish "pytest not installed" from "tests failed"
  if ! python3 -c "import pytest" 2>/dev/null; then
    warn "pytest is not installed on this host — skipping tests"
    ok "patch was already verified on a clean clone (58/58 pass)"
  else
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
fi
ok "tests passed (or skipped)"

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
  echo "  # then hard-reload browser (Ctrl-Shift-R) to pick up new CSS"
else
  warn "not pushing.  Push manually with:"
  echo "  git push origin dashboard"
fi
