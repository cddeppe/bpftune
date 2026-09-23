#!/bin/bash
# bpftune-dashboard deploy.
#
#   sudo bash dashboard/deploy.sh

set -euo pipefail

FORCE=0
PORT=""
NO_HTTP=0

while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE=1; shift ;;
        --port)  PORT="$2"; shift 2 ;;
        --no-http) NO_HTTP=1; shift ;;
        -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [ "$EUID" -ne 0 ]; then
    echo "[deploy] must run as root: sudo bash $0" >&2
    exit 1
fi

if ! command -v bpftool >/dev/null 2>&1; then
    echo "[deploy] bpftool not installed - nothing to consume."
    exit 0
fi

if [ "$FORCE" -ne 1 ]; then
    if ! bpftool map dump name remote_host_map > /dev/null 2>&1; then
        echo "[deploy] remote_host_map not present - bpftune not loaded here."
        echo "[deploy] skipping this host (use --force to override)."
        exit 0
    fi
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

if [ -d "$REPO/.git" ]; then
    echo "[deploy] repo: $REPO"
    cd "$REPO"
    echo "[deploy] pulling latest"
    git pull --ff-only || true
else
    echo "[deploy] not a git checkout at $REPO - skipping pull"
fi

INSTALL_ARGS=()
[ -n "$PORT" ] && INSTALL_ARGS+=(--port "$PORT")
[ "$FORCE" -eq 1 ] && INSTALL_ARGS+=(--force)
[ "$NO_HTTP" -eq 1 ] && INSTALL_ARGS+=(--no-http)

echo "[deploy] running installer"
python3 "$HERE/install.py" "${INSTALL_ARGS[@]}"

echo
echo "[deploy] done."
echo "         dashboard: http://<this-host>:${PORT:-8080}/"
echo "         collector log: /var/log/bpftune-collector.log"
echo "         services: systemctl status bpftune-met-trace bpftune-dashboard-http"
