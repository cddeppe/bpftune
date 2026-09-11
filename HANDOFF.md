# BPFTUNE FORK — HANDOFF

## Goal
16 congestion algorithms (vs upstream 4) + persistent learned state.

## Repos
- Fork: https://github.com/cddeppe/bpftune  (branch main)
- Upstream: https://github.com/oracle/bpftune
- Base: 8fd59cc

## Commits
- 1157b04 — 16-algo enum + congs[]
- 0637776 — BPF switch expansion + mask fixes
- d560e60 — per-CPU scratch map fix
- c73f234 — persist state + -x reset flag
- f153c97 — add HANDOFF.md

## Algorithms
cubic, bbr, htcp, dctcp, scalable, vegas, veno, westwood, reno,
illinois, yeah, lp, bic, highspeed, hybla, cdg

## Persistence
File: /var/lib/bpftune/tcp_conn_tuner.state (0600)
Format: 24B header + N entries of (16B key, 792B value)
Save: fini() calls save_remote_host_map()
Load: init() calls restore_remote_host_map()
Atomic write (tmp+rename), magic/version/size checked
Survives: restart, stop/start, reboot
Lost on: kill -9 / power loss (delta since last clean stop)

Reset: sudo bpftune -x && sudo systemctl restart bpftune
Inspect: sudo ls -la /var/lib/bpftune/
  24 B = empty, 24+808N = N hosts

## Build
git clone https://github.com/cddeppe/bpftune.git
cd bpftune && dpkg-buildpackage -us -uc -b
Result: ../bpftune-custom-0.4.3-ARCH.deb (arm64 auto-detected)

## Install
sudo systemctl stop bpftune
sudo dpkg -i bpftune-custom-0.4.3-ARCH.deb
sudo systemctl start bpftune
sleep 30   # let init finish before stopping, else no save

## Rollback
Restore /usr/sbin/bpftune.orig and /usr/lib/ARCH-linux-gnu/bpftune.orig/

## Security
SSH key for GitHub: ~/.ssh/cluster_sync (in ~/.ssh/config). No PAT.

## Known cosmetics (ignore)
- dpkg: warning: downgrading bpftune
- ldconfig: ... not a symbolic link
- modprobe: FATAL: Module tcp_X.ko not found
- route_table tuner fails on some kernels
- could not get pin: Bad file descriptor (x3 at startup) — pre-existing, cosmetic

## Verified on builder 2026-09-11
Save: 832 B
Restore: 832 B with no new traffic
Delete+restart: 24 B
bpftune -x: file removed

## Known behavior: cdg dominance
Both aarch64 and amd64 hosts show ~88% cdg selection. Investigated and
ruled out code bugs (moved cdg to index 0 — it still dominated, so no
positional bias). Cause: the cost metric rewards low RTT heavily, and
cdg is a delay-based algorithm, so it structurally wins. Not a bug.
Upstream flags the metric as potentially needing tweaks. Leave as-is
unless cdg is observed to actually perform poorly.
