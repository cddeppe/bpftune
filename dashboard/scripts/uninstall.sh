#!/bin/bash
# Remove the dashboard from this host.

set -euo pipefail

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

if [ "$EUID" -ne 0 ]; then
    echo "[uninstall] must run as root" >&2
    exit 1
fi

echo "[uninstall] stopping services"
systemctl disable --now bpftune-dashboard-http.service 2>/dev/null || true
systemctl disable --now bpftune-met-trace.service      2>/dev/null || true

echo "[uninstall] removing unit files"
rm -f /etc/systemd/system/bpftune-dashboard-http.service
rm -f /etc/systemd/system/bpftune-met-trace.service
systemctl daemon-reload

echo "[uninstall] removing cron + logrotate"
rm -f /etc/cron.d/bpftune-history
rm -f /etc/logrotate.d/bpftune-dashboard

echo "[uninstall] removing /opt/bpftune-dashboard"
rm -rf /opt/bpftune-dashboard

if [ "$PURGE" -eq 1 ]; then
    echo "[uninstall] purging /var/lib/bpftune/history"
    rm -rf /var/lib/bpftune/history
else
    echo "[uninstall] leaving /var/lib/bpftune/history intact"
fi

echo "[uninstall] done"
