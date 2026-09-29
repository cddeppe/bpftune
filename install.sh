#!/bin/bash
# install.sh — bpftune fork installer (atomic, single-file)
#
# Usage:
#   sudo bash install.sh                 # interactive: asks about dashboard
#   sudo bash install.sh --yes            # non-interactive, defaults to dashboard=yes
#   sudo bash install.sh --no-dashboard   # tuner only, no dashboard
#   sudo bash install.sh --dashboard-only # dashboard only, skip .deb
#   sudo bash install.sh --help
#
# Idempotent: re-running upgrades in place rather than breaking the install.
# Works on: Debian / Ubuntu on amd64 or arm64.

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
DASHBOARD_UNSPECIFIED=1
DASHBOARD=1
DEB_INSTALL=1
ASSUME_YES=0
FORCE_DOWNGRADE=0
SHOW_HELP=0

for arg in "$@"; do
    case "$arg" in
        --no-dashboard)    DASHBOARD_UNSPECIFIED=0; DASHBOARD=0; DEB_INSTALL=1 ;;
        --dashboard-only)  DASHBOARD_UNSPECIFIED=0; DASHBOARD=1; DEB_INSTALL=0 ;;
        --yes|-y)          ASSUME_YES=1 ;;
        --force-downgrade) FORCE_DOWNGRADE=1 ;;
        --help|-h)         SHOW_HELP=1 ;;
        *)                 fail "Unknown flag: $arg (try --help)" ;;
    esac
done

if [ "$SHOW_HELP" = 1 ]; then
    awk 'NR==1,/^set -Eeuo pipefail$/' "$0" | sed '$d'
    exit 0
fi

# ---------- Pre-flight ----------
[ "$(id -u)" = "0" ] || fail "Run as root (use: sudo bash install.sh)"
command -v curl    >/dev/null || fail "curl not found (apt-get install curl)"
command -v python3 >/dev/null || fail "python3 not found (apt-get install python3)"
command -v dpkg    >/dev/null || fail "dpkg not found — this script targets Debian/Ubuntu"
command -v systemctl >/dev/null || fail "systemctl not found — needs systemd"

ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)
case "$ARCH" in
    amd64|x86_64) ARCH=amd64 ;;
    arm64|aarch64) ARCH=arm64 ;;
    *) fail "Unsupported architecture: $ARCH" ;;
esac

HAVE_NGINX=0; command -v nginx >/dev/null && HAVE_NGINX=1
HAVE_GIT=0;   command -v git   >/dev/null && HAVE_GIT=1 || warn "git not found — dashboard install needs git"

REPO="${BPFTUNE_REPO:-cddeppe/bpftune}"
BACKUP_DIR="${BPFTUNE_BACKUP_DIR:-/mnt/backup}"

printf "\n${BOLD}=== bpftune fork installer ===${NC}\n"
printf "  arch:            %s\n" "$ARCH"
printf "  install .deb:    %s\n" "$([ $DEB_INSTALL = 1 ] && echo yes || echo 'no (--dashboard-only)')"
printf "  dashboard:       %s\n" "$([ $DASHBOARD = 1 ] && echo yes || echo 'no (--no-dashboard)')"
printf "  non-interactive: %s\n" "$([ $ASSUME_YES = 1 ] && echo yes || echo no)"
printf "  nginx present:   %s\n" "$([ $HAVE_NGINX = 1 ] && echo yes || echo 'no (will skip)')"
printf "  git present:      %s\n" "$([ $HAVE_GIT = 1 ] && echo yes || echo 'no')"
[ "$HAVE_GIT" = 0 ] && [ "$DASHBOARD" = 1 ] && fail "git is required for dashboard install"

# ---------- Step 1: install/upgrade bpftune .deb ----------
if [ "$DEB_INSTALL" = 1 ]; then
    step "1. Install bpftune (.deb)"

    ALREADY_INSTALLED=0
    if dpkg-query -W -f='${Status}' bpftune 2>/dev/null | grep -q "install ok installed"; then
        ALREADY_INSTALLED=1
        CURRENT_VER=$(dpkg-query -W -f='${Version}' bpftune)
        warn "bpftune $CURRENT_VER already installed"
        if [ "$ASSUME_YES" = 1 ]; then
            REINSTALL=n
        else
            printf "  Reinstall/upgrade? [y/N] "
            read -r REPLY; REPLY="${REPLY:-n}"
        fi
        if [[ ! "$REPLY" =~ ^[Yy]$ ]]; then
            ok "skipping .deb install (keeping $CURRENT_VER)"
        else
            _DO_DEB=1
        fi
    else
        _DO_DEB=1
    fi

    if [ "${_DO_DEB:-0}" = 1 ]; then
        DEB=""
        if [ -d "$BACKUP_DIR" ]; then
            DEB=$(ls "$BACKUP_DIR"/bpftune_*_"$ARCH".deb 2>/dev/null | sort -V | tail -1 || true)
        fi

        if [ -z "$DEB" ]; then
            printf "  No local .deb — querying GitHub releases for %s/%s...\n" "$REPO" "$ARCH"
            DEB_URL=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" 2>/dev/null \
                | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for a in d.get('assets', []):
    if '${ARCH}' in a.get('name', ''):
        print(a.get('browser_download_url', ''))
        break
" 2>/dev/null || true)
            if [ -n "$DEB_URL" ]; then
                curl -fsSL "$DEB_URL" -o /tmp/bpftune-"$ARCH".deb
                DEB=/tmp/bpftune-"$ARCH".deb
                ok "downloaded .deb from GitHub"
            else
                fail "No .deb available. Either:
   (a) build on the builder host:
       cd /root/bpftune && git pull && sudo dpkg-buildpackage -b -us -uc
   (b) create a GitHub release with the .deb assets
   (c) place a bpftune_*_${ARCH}.deb in ${BACKUP_DIR}/"
            fi
        fi

        NEW_VER=$(dpkg-deb -f "$DEB" Version 2>/dev/null || echo "?")

        SKIP_DOWNGRADE=0
        if [ "$ALREADY_INSTALLED" = 1 ] && dpkg --compare-versions "$NEW_VER" lt "$CURRENT_VER" 2>/dev/null; then
            warn "Found $NEW_VER but installed is $CURRENT_VER — this would be a DOWNGRADE"
            if [ "$FORCE_DOWNGRADE" = 1 ]; then
                ok "proceeding with downgrade (--force-downgrade)"
            elif [ "$ASSUME_YES" = 1 ]; then
                warn "--yes implies no downgrade — skipping"
                SKIP_DOWNGRADE=1
            else
                printf "  Proceed with downgrade? [y/N] "
                read -r REPLY; REPLY="${REPLY:-n}"
                [[ "$REPLY" =~ ^[Yy]$ ]] || SKIP_DOWNGRADE=1
            fi
        fi

        if [ "$SKIP_DOWNGRADE" = 1 ]; then
            ok "keeping installed version $CURRENT_VER"
        else
            printf "  installing %s\n" "$NEW_VER"
            systemctl stop bpftune 2>/dev/null || true
            if ! dpkg -i "$DEB"; then
                fail "dpkg -i failed — fix apt with 'apt-get install -f' and re-run"
            fi
            systemctl enable --now bpftune
            ok "bpftune $(dpkg-query -W -f='${Version}' bpftune) installed and started"
        fi
    fi
fi

# ---------- Step 2: optional prompt (only if unspecified) ----------
if [ "$DASHBOARD_UNSPECIFIED" = 1 ] && [ "$DASHBOARD" = 1 ] && [ "$ASSUME_YES" = 0 ]; then
    printf "\n  Install dashboard? [Y/n] "
    read -r REPLY; REPLY="${REPLY:-y}"
    [[ "$REPLY" =~ ^[Nn]$ ]] && DASHBOARD=0
fi

# ---------- Step 3: dashboard ----------
if [ "$DASHBOARD" = 1 ]; then
    step "2. Install dashboard"

    DASH_BIN=/opt/bpftune-dashboard/bin
    SERVED=/var/lib/bpftune/history

    # --- 3a. git clone or update ---
    if [ -d /root/bpftune/.git ]; then
        cd /root/bpftune
        git fetch origin --prune
        git checkout dashboard 2>/dev/null || git checkout -B dashboard origin/dashboard
        git pull --rebase --autostash
        ok "git checkout updated (branch: dashboard)"
    else
        rm -rf /root/bpftune
        git clone --branch dashboard --depth 50 https://github.com/${REPO} /root/bpftune
        cd /root/bpftune
        ok "git cloned (branch: dashboard)"
    fi
    DASH_HASH=$(git rev-parse --short HEAD)
    printf "  head: %s\n" "$DASH_HASH"

    # --- 3b. deploy files ---
    mkdir -p "$DASH_BIN" "$SERVED"
    cp dashboard/bin/*.py dashboard/bin/*.js dashboard/bin/*.css "$DASH_BIN"/
    chmod 644 "$DASH_BIN"/*.py "$DASH_BIN"/*.css "$DASH_BIN"/*.js
    chmod 755 "$DASH_BIN"/bpftune-collector.py "$DASH_BIN"/bpftune-cli.py "$DASH_BIN"/labels-api.py 2>/dev/null || true
    [ -f dashboard/bin/index.html ] && cp dashboard/bin/index.html "$SERVED"/
    cp "$DASH_BIN"/dashboard.css "$SERVED"/ 2>/dev/null || true
    cp "$DASH_BIN"/dashboard.js  "$SERVED"/ 2>/dev/null || true
    ok "files deployed to $DASH_BIN + $SERVED"

    # --- 3c. systemd services ---
    cat > /etc/systemd/system/bpftune-collector.service <<'EOF'
[Unit]
Description=bpftune dashboard data collector (daemon, SSE on 8082)
After=network.target bpftune.service
Wants=bpftune.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/bpftune-dashboard/bin/bpftune-collector.py --daemon
Restart=always
RestartSec=5
User=root
Nice=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    cat > /etc/systemd/system/bpftune-labels-api.service <<'EOF'
[Unit]
Description=bpftune dashboard IP label editor API (port 8081)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/bpftune-dashboard/bin/labels-api.py
Restart=always
RestartSec=2
User=root

[Install]
WantedBy=multi-user.target
EOF

    cat > /etc/systemd/system/bpftune-met-trace.service <<'EOF'
[Unit]
Description=bpftune trace_pipe capture to /var/log/bpftune-met-live.log
After=bpftune.service
Wants=bpftune.service

[Service]
Type=simple
ExecStart=/bin/sh -c 'exec cat /sys/kernel/tracing/trace_pipe > /var/log/bpftune-met-live.log 2>&1'
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable --now bpftune-collector bpftune-labels-api bpftune-met-trace 2>/dev/null \
        || warn "one or more services failed to enable immediately — see journalctl"
    ok "systemd services created + started"

    # --- 3d. nginx config ---
    if [ "$HAVE_NGINX" = 1 ]; then
        NG_CONF=/etc/nginx/conf.d/bpftune-dashboard.conf
        if [ -f "$NG_CONF" ] || grep -rq 'listen.*8080' /etc/nginx/ 2>/dev/null; then
            warn "nginx :8080 already configured — overwriting $NG_CONF"
        fi
        cat > "$NG_CONF" <<'EOF'
# managed by bpftune install.sh — do not edit; re-run install.sh to regenerate
server {
    listen 8080;
    server_name _;
    root /var/lib/bpftune/history;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /sse {
        proxy_pass http://127.0.0.1:8082/sse;
        proxy_set_header Host $host;
        proxy_set_header X-Accel-Buffering "no";
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
        proxy_http_version 1.1;
        chunked_transfer_encoding off;
    }

    location /api/labels/ {
        proxy_pass http://127.0.0.1:8081/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location = /current.json {
        proxy_pass http://127.0.0.1:8082/current.json;
        proxy_set_header Host $host;
        add_header Cache-Control "no-store, max-age=0";
    }
}
EOF
        if ! grep -q 'include.*mime.types' /etc/nginx/nginx.conf 2>/dev/null; then
            sed -i '/http {/a\    include /etc/nginx/mime.types;\n    default_type application/octet-stream;' /etc/nginx/nginx.conf
        fi
        if nginx -t 2>/dev/null; then
            systemctl reload nginx
            ok "nginx configured (port 8080, /sse, /api/labels, /current.json)"
        else
            warn "nginx -t failed — inspect $NG_CONF manually"
        fi
    else
        warn "nginx not installed — dashboard files at $DASH_BIN (serve manually)"
        warn "  install with: apt-get install -y nginx && bash install.sh --dashboard-only"
    fi

    # --- 3e. cron: hourly render + daily state backup ---
    cat > /etc/cron.d/bpftune-history <<'EOF'
# managed by bpftune install.sh
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
2 * * * * root /opt/bpftune-dashboard/bin/bpftune-render.py >> /var/log/bpftune-render.log 2>&1
EOF
    chmod 644 /etc/cron.d/bpftune-history

    cat > /etc/cron.d/bpftune-state-backup <<'EOF'
# managed by bpftune install.sh
0 3 * * * root cp /var/lib/bpftune/tcp_conn_tuner.state /mnt/backup/tcp_conn_tuner.state.$(date +\%A) 2>/dev/null || true
EOF
    chmod 644 /etc/cron.d/bpftune-state-backup
    ok "cron configured (hourly render + 3am daily state backup)"

    # --- 3f. initial collection ---
    sleep 2
    if python3 "$DASH_BIN"/bpftune-collector.py 2>&1 | tail -1; then
        ok "initial collection done — current.json ready"
    else
        warn "initial collection failed — daemon will retry (journalctl -u bpftune-collector -f)"
    fi

    # --- 3g. tests ---
    if [ -f "$DASH_BIN"/test_bpftune_cli.py ]; then
        printf "  running tests... "
        TEST_OUT=$(python3 "$DASH_BIN"/test_bpftune_cli.py 2>&1 || true)
        TEST_TAIL=$(printf "%s" "$TEST_OUT" | tail -3)
        printf "\n%s\n" "$TEST_TAIL"
        if printf "%s" "$TEST_OUT" | grep -qE '^OK$|Ran [0-9]+ tests'; then
            ok "tests passed"
        else
            warn "tests reported failures — see output above"
        fi
    fi
fi

# ---------- Summary ----------
step "Installation Summary"
printf "  bpftune version: %s\n" "$(dpkg-query -W -f='${Version}' bpftune 2>/dev/null || echo 'not installed')"
printf "  arch:            %s\n" "$ARCH"

svc_status() {
    local s
    s=$(systemctl is-active "$1" 2>/dev/null || echo "inactive")
    case "$s" in
        active)   ok "$1: active" ;;
        inactive|failed) warn "$1: $s" ;;
        *)        warn "$1: $s" ;;
    esac
}
svc_status bpftune
if [ "$DASHBOARD" = 1 ]; then
    svc_status bpftune-collector
    svc_status bpftune-labels-api
    svc_status bpftune-met-trace
    if [ "$HAVE_NGINX" = 1 ]; then
        ok "dashboard URL: http://$(hostname -f 2>/dev/null || hostname):8080/"
    fi
fi

printf "\nDone.\n"
