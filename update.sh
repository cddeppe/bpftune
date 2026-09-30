#!/bin/bash
# update.sh — bpftune fork updater (atomic, single-file)
#
# Usage:
#   sudo bash update.sh                # check + upgrade .deb and dashboard
#   sudo bash update.sh --tuner-only    # only update .deb, skip dashboard
#   sudo bash update.sh --dashboard-only # only update dashboard, skip .deb
#   sudo bash update.sh --help
#
# Idempotent: re-running when already up-to-date is a no-op.
# Checks (in order): local /mnt/backup .deb → GitHub releases → skip if not newer.

set -Eeuo pipefail

# ---------- Pretty output ----------
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

ok()    { printf "${GREEN}✓${NC} %s\n" "$1"; }
fail()  { printf "${RED}✗${NC} %s\n" "$1" >&2; exit 1; }
warn()  { printf "${YELLOW}!${NC} %s\n" "$1"; }
step()  { printf "\n${BOLD}== %s ==${NC}\n" "$1"; }

trap 'fail "Aborted at line $LINENO (exit code $?)"' ERR

# ---------- Parse args ----------
DO_TUNER=1
DO_DASHBOARD=1
FORCE_DOWNGRADE=0
SHOW_HELP=0

for arg in "$@"; do
    case "$arg" in
        --tuner-only)       DO_DASHBOARD=0 ;;
        --dashboard-only)   DO_TUNER=0 ;;
        --force-downgrade)  FORCE_DOWNGRADE=1 ;;
        --help|-h)          SHOW_HELP=1 ;;
        *)                  fail "Unknown flag: $arg (try --help)" ;;
    esac
done

if [ "$SHOW_HELP" = 1 ]; then
    awk 'NR==1,/^set -Eeuo pipefail$/' "$0" | sed '$d'
    exit 0
fi

# ---------- Pre-flight ----------
[ "$(id -u)" = "0" ] || fail "Run as root (use: sudo bash update.sh)"
command -v curl      >/dev/null || fail "curl not found"
command -v python3   >/dev/null || fail "python3 not found"
command -v systemctl >/dev/null || fail "systemctl not found"

ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)
case "$ARCH" in
    amd64|x86_64)  ARCH=amd64 ;;
    arm64|aarch64) ARCH=arm64 ;;
    *) fail "Unsupported architecture: $ARCH" ;;
esac

REPO="${BPFTUNE_REPO:-cddeppe/bpftune}"
BACKUP_DIR="${BPFTUNE_BACKUP_DIR:-/mnt/backup}"
DASH_BIN=/opt/bpftune-dashboard/bin
SERVED=/var/lib/bpftune/history

CURRENT_VER=$(dpkg-query -W -f='${Version}' bpftune 2>/dev/null || echo "not-installed")
printf "${BOLD}=== bpftune fork updater ===${NC}\n"
printf "  arch:           %s\n" "$ARCH"
printf "  current tuner: %s\n" "$CURRENT_VER"
printf "  update tuner:    %s\n" "$([ $DO_TUNER = 1 ] && echo yes || echo 'no (--dashboard-only)')"
printf "  update dashboard: %s\n" "$([ $DO_DASHBOARD = 1 ] && echo yes || echo 'no (--tuner-only)')"

# ---------- Step 1: tuner ----------
if [ "$DO_TUNER" = 1 ]; then
    step "1. Check for newer bpftune .deb"

    LOCAL_DEB=""
    if [ -d "$BACKUP_DIR" ]; then
        LOCAL_DEB=$(ls "$BACKUP_DIR"/bpftune-custom-*-"$ARCH".deb 2>/dev/null | sort -V | tail -1 || true)
    fi

    NEW_VER=""
    DEB_TO_INSTALL=""

    if [ -n "$LOCAL_DEB" ]; then
        NEW_VER=$(dpkg-deb -f "$LOCAL_DEB" Version 2>/dev/null || echo "?")
        DEB_TO_INSTALL="$LOCAL_DEB"
        printf "  found local .deb: %s (%s)\n" "$LOCAL_DEB" "$NEW_VER"
    else
        printf "  no local .deb — checking GitHub releases for %s/%s...\n" "$REPO" "$ARCH"
        DEB_URL=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" 2>/dev/null \
            | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for a in d.get('assets', []):
    if '${ARCH}' in a.get('name', '') and 'bpftune-custom' in a.get('name', ''):
        tag = d.get('tag_name', '').lstrip('v')
        if tag:
            print(tag + ' ' + a.get('browser_download_url', ''))
        break
" 2>/dev/null || true)
        if [ -n "$DEB_URL" ]; then
            NEW_VER=$(printf "%s" "$DEB_URL" | awk '{print $1}')
            URL=$(printf "%s" "$DEB_URL" | awk '{print $2}')
            curl -fsSL "$URL" -o /tmp/bpftune-"$ARCH".deb
            DEB_TO_INSTALL=/tmp/bpftune-"$ARCH".deb
            printf "  found GitHub release: %s\n" "$NEW_VER"
        fi
    fi

    if [ -z "$DEB_TO_INSTALL" ]; then
        warn "no .deb found in $BACKUP_DIR or GitHub releases — skipping tuner update"
        if [ "$CURRENT_VER" = "not-installed" ]; then
            fail "bpftune is not installed. Run install.sh first."
        fi
    elif [ "$NEW_VER" = "$CURRENT_VER" ]; then
        ok "already at latest version ($CURRENT_VER) — skipping .deb install"
    elif dpkg --compare-versions "$NEW_VER" lt "$CURRENT_VER" 2>/dev/null; then
        warn "Found $NEW_VER but installed is $CURRENT_VER — this would be a DOWNGRADE"
        if [ "$FORCE_DOWNGRADE" = 1 ]; then
            ok "proceeding with downgrade (--force-downgrade)"
            printf "  downgrading: %s → %s\n" "$CURRENT_VER" "$NEW_VER"
            systemctl stop bpftune 2>/dev/null || true
            if ! dpkg -i "$DEB_TO_INSTALL"; then
                fail "dpkg -i failed — run 'apt-get install -f' then re-run this script"
            fi
            systemctl start bpftune
            sleep 1
            ok "bpftune downgraded to $(dpkg-query -W -f='${Version}' bpftune)"
        else
            fail "Refusing to downgrade $CURRENT_VER → $NEW_VER without --force-downgrade.
   If you really want to downgrade, re-run with: bash update.sh --force-downgrade"
        fi
    else
        printf "  upgrading: %s → %s\n" "$CURRENT_VER" "$NEW_VER"
        systemctl stop bpftune 2>/dev/null || true
        if ! dpkg -i "$DEB_TO_INSTALL"; then
            fail "dpkg -i failed — run 'apt-get install -f' then re-run this script"
        fi
        systemctl start bpftune
        sleep 1
        ok "bpftune upgraded to $(dpkg-query -W -f='${Version}' bpftune)"
    fi
fi

# ---------- Step 2: dashboard ----------
if [ "$DO_DASHBOARD" = 1 ]; then
    step "2. Update dashboard code"

    if [ ! -d /root/bpftune/.git ]; then
        warn "no /root/bpftune git checkout — run install.sh --dashboard-only to bootstrap"
    else
        cd /root/bpftune
        OLD_HASH=$(git rev-parse --short HEAD 2>/dev/null || echo "?")
        git fetch origin --prune
        git checkout dashboard 2>/dev/null || git checkout -B dashboard origin/dashboard
        if ! git pull --rebase --autostash origin dashboard; then
            warn "git pull --rebase failed — inspect with 'cd /root/bpftune && git status'"
        fi
        NEW_HASH=$(git rev-parse --short HEAD)
        if [ "$OLD_HASH" = "$NEW_HASH" ]; then
            ok "dashboard already at latest ($NEW_HASH)"
        else
            ok "dashboard updated: $OLD_HASH → $NEW_HASH"
        fi

        mkdir -p "$DASH_BIN" "$SERVED"
        cp dashboard/bin/*.py dashboard/bin/*.js dashboard/bin/*.css "$DASH_BIN"/
        chmod 644 "$DASH_BIN"/*.py "$DASH_BIN"/*.css "$DASH_BIN"/*.js
        chmod 755 "$DASH_BIN"/bpftune-collector.py "$DASH_BIN"/bpftune-cli.py "$DASH_BIN"/labels-api.py "$DASH_BIN"/bpftune-render.py 2>/dev/null || true
        [ -f dashboard/bin/index.html ] && cp dashboard/bin/index.html "$SERVED"/
        cp "$DASH_BIN"/dashboard.css "$SERVED"/ 2>/dev/null || true
        cp "$DASH_BIN"/dashboard.js  "$SERVED"/ 2>/dev/null || true
        ok "files synced to $DASH_BIN + $SERVED"

        for svc in bpftune-collector bpftune-labels-api bpftune-met-trace; do
            if systemctl is-enabled "$svc" 2>/dev/null | grep -q enabled; then
                systemctl restart "$svc" 2>/dev/null && ok "$svc restarted" || warn "$svc restart failed"
            fi
        done

        if [ -f "$DASH_BIN"/test_bpftune_cli.py ]; then
            printf "  running tests... "
            TEST_OUT=$(python3 "$DASH_BIN"/test_bpftune_cli.py 2>&1 || true)
            if printf "%s" "$TEST_OUT" | grep -qE '^OK$'; then
                RAN=$(printf "%s" "$TEST_OUT" | grep -oE 'Ran [0-9]+ tests?' | tail -1)
                ok "tests passed ($RAN)"
            else
                printf "\n"
                printf "%s\n" "$TEST_OUT" | tail -5
                warn "tests reported failures — investigate before relying on dashboard"
            fi
        fi

        sleep 2
        if curl -fsS http://127.0.0.1:8082/current.json 2>/dev/null \
            | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'  current.json OK, {len(d)} keys')" 2>/dev/null; then
            :
        else
            warn "collector not yet serving /current.json (journalctl -u bpftune-collector -f)"
        fi

        # --- 2c. refresh bpftune-met-trace.service (fix trace_pipe truncation) ---
        MET_SVC=/etc/systemd/system/bpftune-met-trace.service
        NEW_MET=$(cat <<'METEOF'
[Unit]
Description=bpftune trace_pipe capture to /var/log/bpftune-met-live.log
After=bpftune.service
Wants=bpftune.service

[Service]
Type=simple
ExecStart=/bin/sh -c 'while true; do cat /sys/kernel/tracing/trace_pipe >> /var/log/bpftune-met-live.log 2>&1; sleep 0.1; done'
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
METEOF
)
        if [ -f "$MET_SVC" ]; then
            OLD_MD5=$(md5sum "$MET_SVC" | awk '{print $1}')
            NEW_MD5=$(printf "%s\n" "$NEW_MET" | md5sum | awk '{print $1}')
            if [ "$OLD_MD5" != "$NEW_MD5" ]; then
                printf "%s\n" "$NEW_MET" > "$MET_SVC"
                systemctl daemon-reload
                systemctl restart bpftune-met-trace 2>/dev/null && ok "bpftune-met-trace.service refreshed (trace_pipe loop)" || warn "bpftune-met-trace restart failed"
            else
                ok "bpftune-met-trace.service already up-to-date"
            fi
        fi

        # --- 2d. ensure render.py executable + data/ symlinks exist ---
        chmod 755 "$DASH_BIN"/bpftune-render.py 2>/dev/null || true
        mkdir -p "$SERVED/data"
        for f in "$SERVED"/data/*.json; do
            [ -f "$f" ] || continue
            bn=$(basename "$f")
            [ -L "$SERVED/$bn" ] || ln -sf "data/$bn" "$SERVED/$bn"
        done

        # --- 2e. ensure cron has the symlink step ---
        CRON_FILE=/etc/cron.d/bpftune-history
        if [ -f "$CRON_FILE" ] && ! grep -q 'ln -sf' "$CRON_FILE"; then
            cat > "$CRON_FILE" <<'CRONEOF'
# managed by bpftune install.sh
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
2 * * * * root /opt/bpftune-dashboard/bin/bpftune-render.py && for f in /var/lib/bpftune/history/data/*.json; do [ -f "$f" ] && ln -sf "data/$(basename "$f")" "/var/lib/bpftune/history/$(basename "$f")"; done >> /var/log/bpftune-render.log 2>&1
CRONEOF
            chmod 644 "$CRON_FILE"
            ok "cron updated with symlink step"
        fi
    fi
fi

# ---------- Summary ----------
step "Update Complete"
printf "  bpftune:   %s\n" "$(dpkg-query -W -f='${Version}' bpftune 2>/dev/null || echo 'not installed')"
if [ -d /root/bpftune/.git ]; then
    cd /root/bpftune 2>/dev/null
    printf "  dashboard: %s (branch: %s)\n" \
        "$(git rev-parse --short HEAD 2>/dev/null || echo '?')" \
        "$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
else
    printf "  dashboard: not installed\n"
fi

svc_status() {
    local s
    s=$(systemctl is-active "$1" 2>/dev/null || echo "inactive")
    case "$s" in
        active) ok "$1: active" ;;
        *)      warn "$1: $s" ;;
    esac
}
svc_status bpftune
[ "$DO_DASHBOARD" = 1 ] && [ -d /root/bpftune/.git ] && {
    svc_status bpftune-collector
    svc_status bpftune-labels-api
}

printf "\nDone.\n"
