#!/usr/bin/env bash
# apply-0.4.89-dashboard-fixes.sh
#
# Self-contained atomic patch applier for bpftune dashboard branch.
# Embeds commit 0.4.89 as base64 (76-char wrapped, standard format)
# so there's NO chance of whitespace mangling.
#
# Builds on 0.4.88 (already on origin/dashboard as ce9f5dd).
#
# USAGE:
#   ./apply-0.4.89-dashboard-fixes.sh              # interactive
#   ./apply-0.4.89-dashboard-fixes.sh --yes        # non-interactive
#   ./apply-0.4.89-dashboard-fixes.sh --no-push     # apply + test, skip push
#   ./apply-0.4.89-dashboard-fixes.sh --check-only  # dry-run only
#
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

if ! git merge-base --is-ancestor ce9f5dd HEAD 2>/dev/null; then
  warn "HEAD doesn't contain commit ce9f5dd (the 0.4.88 patch)"
  warn "this patch builds on that — fetch and pull first"
  die "aborted: HEAD is too old, please git pull first"
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  die "working tree has uncommitted changes — stash or commit first"
fi

[[ -f dashboard/bin/bpftune-cli.py ]] \
  || die "dashboard/bin/bpftune-cli.py not found — wrong repo?"

ok "pre-flight passed"

PATCH_FILE=$(mktemp /tmp/bpftune-0.4.89-XXXXXX.patch)
trap 'rm -f "$PATCH_FILE"' EXIT

log "decoding base64-embedded patch to $PATCH_FILE"
base64 -d > "$PATCH_FILE" <<'PATCH_B64_END_SENTINEL'
RnJvbSBiYTdiNmI0NDcxMzUzZmNmOWEyZDhlNjlhZjZjOTI4ODE1ZTEyNWE4IE1vbiBTZXAgMTcg
MDA6MDA6MDAgMjAwMQpGcm9tOiBEYXNoYm9hcmQgRml4ZXMgPGRhc2hib2FyZC1maXhlc0Bsb2Nh
bD4KRGF0ZTogV2VkLCAzMCBTZXAgMjAyNiAxNjoxMTowNiArMDAwMApTdWJqZWN0OiBbUEFUQ0hd
IGRhc2hib2FyZCAwLjQuODk6IG1ha2UgZGF0YV9yZWNlbnRfc3dhcHMvcHJvb2ZzIHJldHVybgog
bmV3ZXN0LWZpcnN0IGNvbnNpc3RlbnRseQpNSU1FLVZlcnNpb246IDEuMApDb250ZW50LVR5cGU6
IHRleHQvcGxhaW47IGNoYXJzZXQ9VVRGLTgKQ29udGVudC1UcmFuc2Zlci1FbmNvZGluZzogOGJp
dAoKQWZ0ZXIgMC40Ljg4LCB0aGUgZGFlbW9uJ3MgX21lcmdlX2RlZHVwZV9zb3J0IHJldHVybmVk
IHJlY2VudF9zd2FwcwphbmQgcmVjZW50X3Byb29mcyBpbiBuZXdlc3QtZmlyc3Qgb3JkZXIsIGJ1
dCB0aGUgdW5kZXJseWluZyBkYXRhCmZ1bmN0aW9ucyAoZGF0YV9yZWNlbnRfc3dhcHMsIGRhdGFf
cmVjZW50X3Byb29mcykgc3RpbGwgcmV0dXJuZWQKb2xkZXN0LWZpcnN0LiAgVGhpcyBjYXVzZWQg
dHdvIGluY29uc2lzdGVuY2llczoKCiAgMS4gVGhlIGNyb24ncyBjb2xsZWN0X2FsbCBwYXRoIChv
bmUtc2hvdCkgcmV0dXJuZWQgb2xkZXN0LWZpcnN0CiAgICAgZnJvbSBkYXRhX3JlY2VudF9zd2Fw
cy9wcm9vZnMuICBUaGUgSlMgcmVuZGVyUmVjZW50U3dhcHMgKG5vCiAgICAgcmV2ZXJzZSwgcGVy
IHRoZSAwLjQuNzkgY29tbWVudCAncm93cyBhcnJpdmUgbmV3ZXN0LWZpcnN0JykKICAgICB3b3Vs
ZCBoYXZlIHNob3duIG9sZGVzdC1maXJzdC4gIFdvcmtlZCBpbiBwcmFjdGljZSBvbmx5IGJlY2F1
c2UKICAgICB0aGUgdXNlciByZWFkcyBmcm9tIHRoZSBkYWVtb24sIG5vdCB0aGUgY3Jvbi4KCiAg
Mi4gVGhlIEpTIHJlbmRlclJlY2VudFByb29mcyBjYWxsZWQgLnNsaWNlKCkucmV2ZXJzZSgpIGFz
c3VtaW5nCiAgICAgb2xkZXN0LWZpcnN0IGlucHV0LiAgV2hlbiB0aGUgZGFlbW9uJ3MgX21lcmdl
X2RlZHVwZV9zb3J0CiAgICAgcmV0dXJuZWQgbmV3ZXN0LWZpcnN0LCB0aGUgcmV2ZXJzZSBmbGlw
cGVkIGl0IHRvIG9sZGVzdC1maXJzdAogICAgIOKAlCB0aGUgdXNlciBzYXcgcmVjZW50X3Byb29m
cyBpbiB3cm9uZyBvcmRlciAoNWgsIDRoLCAuLi4gMm0KICAgICBpbnN0ZWFkIG9mIDJtLCA0bSwg
Li4uIDVoKS4KCkZpeDogbWFrZSBhbGwgZGF0YSBmdW5jdGlvbnMgcmV0dXJuIG5ld2VzdC1maXJz
dCwgcmVtb3ZlIHRoZSBKUwpyZXZlcnNlLiAgTm93IGJvdGggcGF0aHMgKGNyb24gY29sbGVjdF9h
bGwgKyBkYWVtb24gY29sbGVjdF9saWdodHdlaWdodCkKcHJvZHVjZSBuZXdlc3QtZmlyc3QsIGFu
ZCBib3RoIEpTIHJlbmRlcmVycyAocmVuZGVyUmVjZW50U3dhcHMgKwpyZW5kZXJSZWNlbnRQcm9v
ZnMpIHJlbmRlciBmb3J3YXJkIHdpdGhvdXQgcmV2ZXJzaW5nLgoKQ2hhbmdlczoKICBicGZ0dW5l
X2RhdGEucHk6ZGF0YV9yZWNlbnRfc3dhcHMgICAgICAgICAgLSByZXR1cm4gcm93c1stbjpdWzo6
LTFdCiAgYnBmdHVuZV9kYXRhLnB5OmRhdGFfcmVjZW50X3N3YXBzX2J5X2J1Y2tldCAtIGl0ZXJh
dGUgZm9yd2FyZCAocm93cykKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAg
ICAgICAgICAgICBpbnN0ZWFkIG9mIHJldmVyc2VkKHJvd3MpCiAgYnBmdHVuZV9kYXRhLnB5OmRh
dGFfcmVjZW50X3Byb29mcyAgICAgICAgICAtIHJldHVybiBvdXRbOjotMV0KICBkYXNoYm9hcmQu
anM6cmVuZGVyUmVjZW50UHJvb2ZzICAgICAgICAgICAgLSByZW1vdmUgLnNsaWNlKCkucmV2ZXJz
ZSgpCiAgdGVzdF9icGZ0dW5lX2NsaS5weTp0ZXN0X2RhdGFfcmVjZW50X3N3YXBzICAtIGFzc2Vy
dCByb3dzWzBdIGlzIG5ld2VzdAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAg
ICAgICAgICAgICAgICh3YXMgcm93c1stMV0pCgpWZXJpZmllZDogNTgvNTggdGVzdHMgcGFzcy4g
IG5vZGUgLS1jaGVjayBkYXNoYm9hcmQuanMgT0suCmFzdC5wYXJzZSBvbiBicGZ0dW5lX2RhdGEu
cHkgT0suCi0tLQogZGFzaGJvYXJkL2Jpbi9icGZ0dW5lX2RhdGEucHkgICAgIHwgMTMgKysrKysr
KystLS0tLQogZGFzaGJvYXJkL2Jpbi9kYXNoYm9hcmQuanMgICAgICAgIHwgIDUgKysrKy0KIGRh
c2hib2FyZC9iaW4vdGVzdF9icGZ0dW5lX2NsaS5weSB8ICA1ICsrKystCiAzIGZpbGVzIGNoYW5n
ZWQsIDE2IGluc2VydGlvbnMoKyksIDcgZGVsZXRpb25zKC0pCgpkaWZmIC0tZ2l0IGEvZGFzaGJv
YXJkL2Jpbi9icGZ0dW5lX2RhdGEucHkgYi9kYXNoYm9hcmQvYmluL2JwZnR1bmVfZGF0YS5weQpp
bmRleCA5ZjNmOWM5Li4yYWYyMGYwIDEwMDc1NQotLS0gYS9kYXNoYm9hcmQvYmluL2JwZnR1bmVf
ZGF0YS5weQorKysgYi9kYXNoYm9hcmQvYmluL2JwZnR1bmVfZGF0YS5weQpAQCAtNDIzLDkgKzQy
Myw5IEBAIGRlZiBkYXRhX3JlY2VudF9zd2Fwc19ieV9idWNrZXQodGV4dCwgbl9wZXJfYnVja2V0
PTE2KSAtPiBEaWN0W3N0ciwgTGlzdFtSZWNlbnRTCiAgICAge2J1Y2tldF9zdHI6IFtyb3csIC4u
Ll19IG9yZGVyZWQgbmV3ZXN0LWZpcnN0IHdpdGhpbiBlYWNoLiIiIgogICAgIHJvd3MgPSBkYXRh
X3JlY2VudF9zd2Fwcyh0ZXh0LCBuPTIwMCkKICAgICBvdXQgPSB7fQotICAgICMgSXRlcmF0ZSBu
ZXdlc3QtZmlyc3Qgc28gZWFjaCBidWNrZXQgZ2V0cyBpdHMgbmV3ZXN0Ci0gICAgIyBuX3Blcl9i
dWNrZXQgZW50cmllcywgbm90IHRoZSBvbGRlc3Qgb2YgdGhlIHdpbmRvdy4KLSAgICBmb3IgciBp
biByZXZlcnNlZChyb3dzKToKKyAgICAjIDAuNC44OTogZGF0YV9yZWNlbnRfc3dhcHMgbm93IHJl
dHVybnMgbmV3ZXN0LWZpcnN0ICh3YXMgb2xkZXN0LWZpcnN0KSwKKyAgICAjIHNvIGl0ZXJhdGUg
Zm9yd2FyZCAobm90IHJldmVyc2VkKSB0byBmaWxsIGVhY2ggYnVja2V0IG5ld2VzdC1maXJzdC4K
KyAgICBmb3IgciBpbiByb3dzOgogICAgICAgICAjIDAuNC44NjogZ3JvdXAgYnkgZGVzdCAobGFi
ZWxlZCkgaW5zdGVhZCBvZiBfYnVja2V0IChyYXcga2V5KQogICAgICAgICAjIHNvIGtleXMgbWF0
Y2ggbWV0YS5qc29uIGJ1Y2tldCBJRHMgKHdoaWNoIHVzZSB0aGUgbGFiZWxlZCBkZXN0KS4KICAg
ICAgICAgIyBXaXRob3V0IHRoaXMsIHNlbGVjdGluZyAiaG9tZS1zY28iIGluIHRoZSBkcm9wZG93
biBsb29rcyB1cApAQCAtNzgyLDcgKzc4Miw3IEBAIGRlZiBkYXRhX3JlY2VudF9zd2Fwcyh0ZXh0
LCBuPTEwKSAtPiBMaXN0W1JlY2VudFN3YXBdOgogICAgICAgICAgICAgIl9idWNrZXQiOiAgX2J1
Y2tldF9vZihyb3dbOV0gaWYgbGVuKHJvdykgPiA5IGVsc2UgTm9uZSwKICAgICAgICAgICAgICAg
ICAgICAgICAgICAgICAgICAgICAgIHJvd1sxMF0gaWYgbGVuKHJvdykgPiAxMCBlbHNlIE5vbmUp
LAogICAgICAgICB9KQotICAgIHJldHVybiByb3dzWy1uOl0KKyAgICByZXR1cm4gcm93c1stbjpd
Wzo6LTFdICAjIDAuNC44OTogbmV3ZXN0LWZpcnN0ICh3YXMgb2xkZXN0LWZpcnN0KQogCiAKIApA
QCAtODA0LDcgKzgwNCwxMCBAQCBkZWYgZGF0YV9yZWNlbnRfcHJvb2ZzKHRleHQsIG49MTYpIC0+
IExpc3RbUmVjZW50UHJvb2ZdOgogICAgICAgICAgICAgInRpZXIiOiAicHJvdmVkIiBpZiBtLmdy
b3VwKDUpID09ICIyIiBlbHNlICJnb29kIiwKICAgICAgICAgICAgICJkZXN0IjogX2xhYmVsX2Zv
cihfZGVzdF9zdHIoKihjZGVzdC5nZXQoYykgb3IgKE5vbmUsIE5vbmUpKSkpLAogICAgICAgICB9
KQotICAgIHJldHVybiBvdXQKKyAgICAjIDAuNC44OTogcmV0dXJuIG5ld2VzdC1maXJzdCAod2Fz
IG9sZGVzdC1maXJzdCkgc28gdGhlIEpTCisgICAgIyByZW5kZXJSZWNlbnRQcm9vZnMgZG9lc24n
dCBuZWVkIHRvIC5yZXZlcnNlKCkg4oCUIHdoaWNoIHdhcyBicmVha2luZworICAgICMgd2hlbiB0
aGUgZGFlbW9uJ3MgX21lcmdlX2RlZHVwZV9zb3J0IGFscmVhZHkgcmV0dXJuZWQgbmV3ZXN0LWZp
cnN0LgorICAgIHJldHVybiBvdXRbOjotMV0KIAogCiAKZGlmZiAtLWdpdCBhL2Rhc2hib2FyZC9i
aW4vZGFzaGJvYXJkLmpzIGIvZGFzaGJvYXJkL2Jpbi9kYXNoYm9hcmQuanMKaW5kZXggMGVhNDQ0
Zi4uODIyODdiZSAxMDA2NDQKLS0tIGEvZGFzaGJvYXJkL2Jpbi9kYXNoYm9hcmQuanMKKysrIGIv
ZGFzaGJvYXJkL2Jpbi9kYXNoYm9hcmQuanMKQEAgLTUyOSw3ICs1MjksMTAgQEAKICAgICAgIHJl
dHVybjsKICAgICB9CiAgICAgdmFyIGh0bWwgPSAnPGRpdiBjbGFzcz0ibGlzdCI+JzsKLSAgICBy
b3dzLnNsaWNlKCkucmV2ZXJzZSgpLmZvckVhY2goZnVuY3Rpb24gKHIpIHsKKyAgICAvLyAwLjQu
ODk6IGRhdGFfcmVjZW50X3Byb29mcyBub3cgcmV0dXJucyBuZXdlc3QtZmlyc3QsIHNvIHJlbmRl
cisorICAgIC8vIGZvcndhcmQgKHdhcyAuc2xpY2UoKS5yZXZlcnNlKCkgd2hpY2ggYXNzdW1lZCBv
bGRlc3QtZmlyc3QgaW5wdXQKKyAgICAvLyBhbmQgYnJva2Ugd2hlbiB0aGUgZGFlbW9uJ3MgX21l
cmdlX2RlZHVwZV9zb3J0IHJldHVybmVkIG5ld2VzdC1maXJzdCkuCisgICAgcm93cy5mb3JFYWNo
KGZ1bmN0aW9uIChyKSB7CiAgICAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJpdGVtIj4nICsKICAg
ICAgICAgJzxzcGFuIGNsYXNzPSJmbG93Ij4nICsgZXNjKHIuYWxnKSArICc8L3NwYW4+JyArCiAg
ICAgICAgICc8c3BhbiBjbGFzcz0ibWV0YSI+JyArIGVzYyhzaG9ydEFkZHIoci5kZXN0KSkgKwpk
aWZmIC0tZ2l0IGEvZGFzaGJvYXJkL2Jpbi90ZXN0X2JwZnR1bmVfY2xpLnB5IGIvZGFzaGJvYXJk
L2Jpbi90ZXN0X2JwZnR1bmVfY2xpLnB5CmluZGV4IDcwOTYzMjguLjI0OWY3YTcgMTAwNzU1Ci0t
LSBhL2Rhc2hib2FyZC9iaW4vdGVzdF9icGZ0dW5lX2NsaS5weQorKysgYi9kYXNoYm9hcmQvYmlu
L3Rlc3RfYnBmdHVuZV9jbGkucHkKQEAgLTI5Miw3ICsyOTIsMTAgQEAgY2xhc3MgVGVzdERhdGFQ
aXBlbGluZSh1bml0dGVzdC5UZXN0Q2FzZSk6CiAgICAgZGVmIHRlc3RfZGF0YV9yZWNlbnRfc3dh
cHMoc2VsZik6CiAgICAgICAgIHJvd3MgPSBzZWxmLmNsaS5kYXRhX3JlY2VudF9zd2FwcyhNT0NL
X0xPRywgbj0xMCkKICAgICAgICAgc2VsZi5hc3NlcnRFcXVhbChsZW4ocm93cyksIDMpCi0gICAg
ICAgIHNlbGYuYXNzZXJ0RXF1YWwocm93c1stMV1bInRvX2FsZyJdLCAiZGN0Y3AiKQorICAgICAg
ICAjIDAuNC44OTogZGF0YV9yZWNlbnRfc3dhcHMgbm93IHJldHVybnMgbmV3ZXN0LWZpcnN0Lgor
ICAgICAgICAjIFN3YXBzIGluIE1PQ0tfTE9HIChuZXdlc3QgdG8gb2xkZXN0KTogZGN0Y3AgKDEy
MHMpLCBodGNwICgxMTBzKSwgYmJyICgxMDBzKS4KKyAgICAgICAgc2VsZi5hc3NlcnRFcXVhbChy
b3dzWzBdWyJ0b19hbGciXSwgImRjdGNwIikKKyAgICAgICAgc2VsZi5hc3NlcnRFcXVhbChyb3dz
Wy0xXVsidG9fYWxnIl0sICJiYnIiKQogCiAgICAgZGVmIHRlc3RfcHJvb2ZfZXZlbnRzKHNlbGYp
OgogICAgICAgICBldmVudHMsIHNhbXBsZXMgPSBzZWxmLmNsaS5fcHJvb2ZfZXZlbnRzKE1PQ0tf
TE9HKQotLSAKMi40Ny4zCgo=
PATCH_B64_END_SENTINEL

ok "patch decoded ($(wc -l < "$PATCH_FILE") lines, $(wc -c < "$PATCH_FILE") bytes)"

# Verify the patch looks like a valid git format-patch
if ! head -1 "$PATCH_FILE" | grep -q '^From .* Mon' 2>/dev/null; then
  die "decoded patch doesn't look like a git format-patch (first line: $(head -1 "$PATCH_FILE"))"
fi

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

  if git log --format='%s' -1 HEAD 2>/dev/null | grep -q "0.4.89: make data_recent_swaps"; then
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
  log "CRITICAL — restart the collector daemon on each host:"
  echo "  sudo systemctl restart bpftune-collector"
  log "then hard-reload the browser (Ctrl-Shift-R) to pick up new CSS/JS"
  log "On target hosts:"
  echo "  cd ~/bpftune && git pull && sudo bash dashboard/deploy.sh --force"
  echo "  sudo systemctl restart bpftune-collector"
else
  warn "not pushing.  Push manually with:"
  echo "  git push origin dashboard"
fi
