#!/bin/bash
# bpftune dashboard - deploy on this host.
#
# Finds (or clones) the repo, pulls the latest, runs the installer.
# Skips hosts that don't have bpftune running.
#
# Usage:  sudo bash tools/bpftune-deploy-all.sh

set -e

REPO=""
for d in "$HOME/bpftune" /home/*/bpftune /root/bpftune /opt/bpftune; do
    if [ -d "$d/.git" ]; then
        REPO="$d"
        break
    fi
done

if [ -z "$REPO" ]; then
    echo "[deploy] no repo found - cloning to $HOME/bpftune"
    git clone https://github.com/cddeppe/bpftune "$HOME/bpftune"
    REPO="$HOME/bpftune"
fi

echo "[deploy] repo: $REPO"
cd "$REPO"

if ! pgrep -x bpftune > /dev/null 2>&1; then
    echo "[deploy] bpftune is not running on this host."
    echo "[deploy] nothing to install here - skipping."
    exit 0
fi

echo "[deploy] bpftune is running"
echo "[deploy] pulling latest"
if ! git pull --ff-only 2>/dev/null; then
    echo "[deploy] pull failed - discarding generated tools and retrying"
    git checkout -- tools/bpftune-collector.py tools/bpftune-render.py 2>/dev/null || true
    git clean -fd tools/bpftune-collector.py tools/bpftune-render.py 2>/dev/null || true
    git pull --ff-only
fi

echo "[deploy] running installer"
sudo python3 tools/bpftune-dashboard-install.py

echo
echo "[deploy] done."
echo "         dashboard: http://<this-host>:8080/"
echo "         log:       /var/log/bpftune-collector.log"
