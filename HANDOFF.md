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
- ea3358d — package filename note
- 66b98c9 — skip IPv4 routes without a gateway (fixes metric pollution)
- ac1ebaa — handle algorithms the kernel rejects (metric loop fix + IPv6 gateway)

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

## IPv4 gateway fix (66b98c9)
IPv4 connections without a gateway (on-link, same subnet) were being
bucketed under ::ffff:0.0.0.0 mixed with anything else reading as 0.
Since min_rtt is a monotonic low-water mark, a fast on-link RTT
poisoned the metric for all other connections sharing that key.
IPv6 already checked RTF_GATEWAY; IPv4 now checks rt_uses_gateway too.
No-gateway routes are skipped entirely.

## Three upstream bugs fixed (0.4.5)

**1. IPv4 on-link metric pollution (66b98c9)**
IPv4 routes without a gateway (same subnet) had rt_gw4 = 0, so all
on-link connections got bucketed under ::ffff:0.0.0.0 mixed with
anything else reading as 0. min_rtt is a monotonic low-water mark,
so a fast on-link RTT (11 us) poisoned the metric for every other
connection sharing that key (30 ms internet RTT). Fix: check
rt_uses_gateway, skip no-gateway IPv4 routes.

**2. IPv6 no-gateway fall-through (ac1ebaa)**
Same class of bug: IPv6 code checked RTF_GATEWAY correctly but used
'break' instead of 'return', falling out of the switch with an
all-zero key. Fix: return 1 instead of break.

**3. Failed bpf_setsockopt poisoned the metric (ac1ebaa)**
bpf_setsockopt(TCP_CONGESTION) for cdg returns -ENOTSUPP on this
kernel. The old code incremented tcp_cong_choices[] BEFORE checking
the return value, and never updated sk_storage on failure. Since the
metric-update path requires sk_storage, cdg's metric_value stayed at
0 forever; 0 is the minimum, so cdg won every comparison, failed
every set, and looped. Summary showed cdg at ~90% while no socket
ever actually ran cdg.
Fix: set_cong() returns the error; caller sets metric_value = ~0ULL
on failure so the algorithm stops winning comparisons for that host.
Counter now only increments on success.

## BUILD REQUIREMENT: make clean before dpkg-buildpackage

The Makefile does not track the .bpf.c -> .skel.h -> .o dependency
chain properly. Editing tcp_conn_tuner.bpf.c and running
dpkg-buildpackage alone will silently reuse stale objects and produce
a .deb with OLD code. This caused ~2 hours of confusion during the
0.4.4 cycle: the "IPv4 gateway fix" appeared not to change anything
because the built .deb contained pre-fix code.

ALWAYS:
    cd ~/bpftune
    make clean >/dev/null 2>&1
    dpkg-buildpackage -us -uc -b

Verify after building (the truly definitive check):
    sudo bpftool prog load src/tcp_conn_tuner.bpf.o /sys/fs/bpf/test_x
If it loads silently, the verifier is happy. If it prints
"BPF program is too large" or any error, the build is broken.

Non-definitive but useful: strings on the .so can confirm whether a
specific code change is present.

## Why cdg was never the winner

Before 0.4.5, every summary showed cdg at ~85-90% of all counts.
Investigated as a real behavioral preference, but it was an artifact
of bug #3: cdg was picked, failed to set, its metric never updated,
and it kept winning. Real sockets always ran the system default
(scalable) or whatever bpf_setsockopt had successfully set.

Post-0.4.5, cdg is 0 across the fleet, and selection is spread across
the algorithms that actually work on this kernel.

## Kernel notes

- bpf_setsockopt(TCP_CONGESTION) returns -ENOTSUPP for cdg on
  kernel 6.12 (Debian 13) from the BPF context, even though a plain
  setsockopt(2) from userspace works. The userspace and BPF paths
  differ; do not assume a userspace viability test predicts BPF
  behavior.
- Other 15 algorithms set successfully.

## Version history
- 0.4-2-custom: 16-algorithm expansion (early)
- 0.4-3-custom: state persistence + -x reset flag
- 0.4-4-custom: IPv4 gateway fix (BUT the .deb was stale;
                 never actually shipped a working version of it
                 on its own — superseded by 0.4-5)
- 0.4-5-custom: metric loop fix, IPv6 gateway fix, this doc

## Why cdg is permanently unusable via BPF

Kernel hardcodes a refusal in net/core/filter.c:sol_tcp_sockopt_congestion():

    /* "cdg" is the only cc that alloc a ptr in inet_csk_ca area.
     * The bpf-tcp-cc may overwrite this ptr after switching to cdg. */
    if (*optlen >= sizeof("cdg") - 1 && !strncmp("cdg", optval, *optlen))
        return -ENOTSUPP;

cdg allocates per-connection state in inet_csk_ca; a BPF TCP CC prog
could clobber it.  Kernel refuses cdg from bpf_setsockopt for safety.
This is upstream and universal -- not a fork bug, not a kernel bug.

Consequence: cdg will never succeed via this tuner's set_cong() path.
The 0.4-5 fix (mark metric unusable on failure) handles it correctly.
Optional tidier alternative: drop cdg from congs[] entirely.

## Two summary blocks (not a bug, per-netns)
On hosts with more than one network namespace, `bpftune -q summary`
prints one CongAlg block per netns.  Modern systemd sandboxes like
polkitd each get their own netns even without any container or
explicit `ip netns add`; `ip netns list` will not show them, but
`lsns -t net` will.  The tuner tracks per-netns state separately,
which is by design (see "bpftune supports per-netns policy" in the
startup log).

Effect: two blocks, one all zeros (sandbox netns), one with real
counts (main netns).  Cosmetic confusion only.  Not caused by any
fork change.

## apt can silently replace this fork
Installing an upstream bpftune package (or running upgrade operations
that pull it in) will overwrite this fork's files.  Symptom: summary
shows upstream's 4 algorithms instead of 16.  Check with `lsof -p
$(pidof bpftune) | grep tcp_conn_tuner` -- fork loads from
/usr/lib/bpftune/, upstream from /usr/lib/<arch>-linux-gnu/bpftune/.

Fix: `sudo apt-mark hold bpftune libbpftune0` on each host.
Reinstall from /mnt/backup if already replaced.
